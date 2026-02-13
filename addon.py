import os
import re
import logging
import threading
import time
import requests
import shutil
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# Gemini SDK oficial (Google Gen AI)
from google import genai

# --- Configurações ---

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SimpleSubtitleAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

USER_AGENT = "SubpalasAddon v0.1.0"

MANIFEST = {
    "id": "community.subs.ptbr.simple",
    "version": "0.1.0",
    "name": "Subs PT-BR (fallback EN->PT via Gemini)",
    "description": "Pega a melhor PT-BR. Se não tiver, baixa EN e traduz via Gemini.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# OpenSubtitles base
OS_BASE = "https://api.opensubtitles.com/api/v1"
# Se seu download estiver falhando, troque para:
# OS_BASE = "https://www.opensubtitles.com/api/v1"
# (isso já resolveu 500/instabilidade pra muita gente em casos antigos)  2


# --- Utilitários ---

def get_cache_key(imdb_id, season=None, episode=None):
    base = f"{imdb_id}"
    if season is not None and episode is not None:
        base += f"_S{season}E{episode}"
    return base

def cleanup_temp(files):
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

def generate_loading_srt(msg):
    return (
        "1\n"
        "00:00:00,000 --> 00:00:12,000\n"
        f"{msg}\n\n"
        "2\n"
        "00:00:12,500 --> 00:00:24,000\n"
        "Se demorar muito, tente novamente.\n"
    )

def download_file(url, dest_path):
    try:
        with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=20) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Download falhou: {e}")
        return False


# --- OpenSubtitles ---

def os_headers():
    return {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-User-Agent": USER_AGENT,
    }

def get_download_link(file_id):
    try:
        res = requests.post(
            f"{OS_BASE}/download",
            headers=os_headers(),
            json={"file_id": file_id},
            timeout=15
        )
        res.raise_for_status()
        return res.json().get("link")
    except Exception as e:
        logger.error(f"Erro /download: {e}")
        return None

def search_best_sub(imdb_id, lang, season=None, episode=None):
    """
    Retorna link de download da legenda mais baixada para o idioma desejado.
    lang exemplo: 'pt-br', 'en'
    """
    if not OS_API_KEY:
        return None

    try:
        clean_id = int(imdb_id.replace("tt", ""))
    except Exception:
        return None

    params = {
        "imdb_id": clean_id,
        "languages": lang,
        "order_by": "download_count",
        "order_direction": "desc"
    }
    if season is not None and episode is not None:
        params.update({"season_number": season, "episode_number": episode})

    try:
        res = requests.get(f"{OS_BASE}/subtitles", headers=os_headers(), params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        if data.get("total_count", 0) <= 0:
            return None

        first = data["data"][0]
        f = first["attributes"]["files"][0]
        return get_download_link(f["file_id"])
    except Exception as e:
        logger.error(f"Erro /subtitles ({lang}): {e}")
        return None


# --- SRT: parse e tradução ---

_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s-->\s\d{2}:\d{2}:\d{2},\d{3}$")

def split_srt_blocks(srt_text):
    """
    Retorna lista de blocos; cada bloco é dict:
    { 'idx': '1', 'time': '00:.. --> ..', 'lines': [texto...] }
    """
    blocks = []
    raw_blocks = re.split(r"\n\s*\n", srt_text.strip(), flags=re.MULTILINE)
    for b in raw_blocks:
        lines = [l.rstrip("\r") for l in b.splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        idx = lines[0].strip()
        t = lines[1].strip()
        txt_lines = lines[2:] if len(lines) > 2 else []
        # tolerante: se não bater formato padrão, ainda mantém
        blocks.append({"idx": idx, "time": t, "lines": txt_lines})
    return blocks

def rebuild_srt(blocks):
    out = []
    for b in blocks:
        out.append(str(b["idx"]))
        out.append(str(b["time"]))
        out.extend(b["lines"])
        out.append("")  # linha em branco
    return "\n".join(out).strip() + "\n"

def gemini_translate_lines(lines, target_lang="pt-BR"):
    """
    Traduz uma lista de linhas (strings) preservando 1 linha -> 1 linha.
    Usa Gemini API (SDK oficial). 3
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não definido")

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Prompt bem direto: manter número de linhas e não adicionar nada.
    prompt = (
        f"Traduza as linhas abaixo para {target_lang}.\n"
        "Regras obrigatórias:\n"
        "1) Mantenha EXATAMENTE a mesma quantidade de linhas.\n"
        "2) Traduza cada linha separadamente, na mesma ordem.\n"
        "3) Não adicione comentários, nem numeração, nem aspas.\n"
        "4) Preserve tags tipo <i>, <b> e marcadores como {...}.\n\n"
        "LINHAS:\n" + "\n".join(lines)
    )

    # Modelo: escolha um estável/rápido no seu caso.
    # A doc mostra exemplos de models via client.models.generate_content. 4
    resp = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=prompt
    )

    text = (resp.text or "").strip()
    out_lines = text.splitlines()

    # Correção defensiva: se o modelo devolver menos/mais linhas, ajusta sem explodir.
    if len(out_lines) != len(lines):
        logger.warning(f"Gemini retornou {len(out_lines)} linhas, esperado {len(lines)}. Ajustando.")
        # pad / truncate
        if len(out_lines) < len(lines):
            out_lines += [""] * (len(lines) - len(out_lines))
        else:
            out_lines = out_lines[:len(lines)]

    return out_lines

def translate_srt_via_gemini(srt_text):
    """
    Traduz apenas texto do SRT, mantendo idx e timestamps.
    Faz batching pra reduzir risco de estouro de contexto.
    """
    blocks = split_srt_blocks(srt_text)

    # Junta todas as linhas de texto (preservando posições vazias)
    positions = []  # (block_i, line_i)
    all_lines = []
    for bi, b in enumerate(blocks):
        for li, line in enumerate(b["lines"]):
            positions.append((bi, li))
            all_lines.append(line)

    # Nada pra traduzir
    if not all_lines:
        return srt_text

    # Batching: 60 linhas por lote é um valor bem seguro na prática
    BATCH = 60
    translated = [""] * len(all_lines)

    for start in range(0, len(all_lines), BATCH):
        chunk = all_lines[start:start+BATCH]
        # (opcional) pula linhas “vazias”
        out_chunk = gemini_translate_lines(chunk, target_lang="pt-BR")
        translated[start:start+BATCH] = out_chunk

    # Recoloca no lugar
    for i, (bi, li) in enumerate(positions):
        blocks[bi]["lines"][li] = translated[i]

    return rebuild_srt(blocks)


# --- Core job (thread) ---

def build_subtitle(cache_key, imdb_id, season, episode):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}.srt")
    if os.path.exists(final_path):
        return

    tmp_files = []
    logger.info(f"Gerando legenda para {cache_key}...")

    # 1) tenta PT-BR
    url_pt = search_best_sub(imdb_id, "pt-br", season, episode)
    if url_pt:
        tmp_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
        tmp_files.append(tmp_pt)
        if download_file(url_pt, tmp_pt):
            shutil.copy(tmp_pt, final_path)
            cleanup_temp(tmp_files)
            logger.info(f"OK PT-BR cache: {final_path}")
            return

    # 2) fallback: EN -> traduz
    url_en = search_best_sub(imdb_id, "en", season, episode)
    if not url_en:
        cleanup_temp(tmp_files)
        logger.warning("Sem PT-BR e sem EN.")
        return

    tmp_en = os.path.join(TEMP_DIR, f"{cache_key}_en.srt")
    tmp_files.append(tmp_en)
    if not download_file(url_en, tmp_en):
        cleanup_temp(tmp_files)
        return

    try:
        with open(tmp_en, "r", encoding="utf-8", errors="replace") as f:
            en_text = f.read()

        pt_text = translate_srt_via_gemini(en_text)

        with open(final_path, "w", encoding="utf-8") as f:
            f.write(pt_text)

        logger.info(f"OK EN->PT via Gemini cache: {final_path}")
    except Exception as e:
        logger.error(f"Falha traduzindo via Gemini: {e}")

    cleanup_temp(tmp_files)


# --- Rotas ---

@app.route("/")
def index():
    return "Simple Subtitle Addon Running"

@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)

@app.route("/subtitles/<type>/<id>/<extra>.json")
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None

    cache_key = get_cache_key(imdb_id, season, episode)
    threading.Thread(target=build_subtitle, args=(cache_key, imdb_id, season, episode), daemon=True).start()

    host = request.host_url.rstrip("/")
    return jsonify({
        "subtitles": [
            {
                "id": f"simple_{cache_key}",
                "url": f"{host}/static_subs/{cache_key}.srt",
                "lang": "pob",   # pt-BR no “dialeto” usado por alguns players/addons
                "format": "srt"
            }
        ]
    })

@app.route("/static_subs/<filename>")
def serve_subs(filename):
    file_path = os.path.join(CACHE_DIR, filename)

    # espera até 25s pelo job
    for _ in range(25):
        if os.path.exists(file_path):
            response = make_response(send_from_directory(CACHE_DIR, filename))
            response.headers["Cache-Control"] = "public, max-age=31536000"
            return response
        time.sleep(1)

    response = make_response(generate_loading_srt("Gerando legenda... (PT-BR ou tradução EN->PT)"))
    response.headers["Content-Type"] = "application/x-subrip"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7000))
    app.run(host="0.0.0.0", port=port)

