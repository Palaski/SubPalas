import os
import logging
import threading
import time
import requests
import re
import json
from urllib.parse import parse_qs, unquote

# IA (Gemini)
import google.generativeai as genai

from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# -----------------------------------------------------------------------------
# App / Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoTranslateAI")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync/1.0")
PORT = int(os.environ.get("PORT", 7000))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "community.autotranslate.ai",
    "version": "0.3.3",
    "name": "AutoSync AI (Gemini)",
    "description": "Tradução de alta qualidade via Google Gemini + Busca inteligente de release.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"],
}

# -----------------------------------------------------------------------------
# HTTP session (padrões)
# -----------------------------------------------------------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
})

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def get_file_hash(imdb_id, season=None, episode=None, video_hash=None):
    base = f"{imdb_id}"
    if season and episode:
        base += f"_S{season}E{episode}"
    if video_hash:
        base += f"_{video_hash}"
    return base

def clean_filename(name: str) -> str:
    """Normaliza nome do arquivo para ajudar no match."""
    return name.replace('.', ' ').replace('-', ' ').lower()

def wrap_text(s: str, width: int = 44) -> str:
    """Quebra linhas longas para TVs mais exigentes (opcional)."""
    words = s.split()
    if not words:
        return s
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)

# -----------------------------------------------------------------------------
# LLM Translation (Gemini)
# -----------------------------------------------------------------------------
def translate_batch_gemini(texts):
    """
    Usa Gemini Flash para traduzir uma lista de falas.
    Retorna lista traduzida ou None se falhar.
    """
    if not GEMINI_API_KEY:
        return None

    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = (
        "Você é um tradutor profissional de legendas (EN -> PT-BR). "
        "Traduza a lista de falas JSON abaixo para Português do Brasil. "
        "Regras: 1) Mantenha gírias e tom natural; 2) Seja conciso (limite de caracteres para TV); "
        "3) Retorne APENAS um array JSON de strings, ex: [\"Olá\", \"Tudo bem?\"]; "
        "4) Não inclua explicações, cabeçalhos ou markdown.\n\n"
        f"Input: {json.dumps(texts, ensure_ascii=False)}"
    )

    try:
        response = model.generate_content(prompt)
        text_resp = (response.text or "").strip()

        # pode vir com ```json ... ```
        if "```" in text_resp:
            if "```json" in text_resp:
                text_resp = text_resp.split("```json", 1)[-1].split("```", 1)[0].strip()
            else:
                text_resp = text_resp.split("```", 1)[-1].split("```", 1)[0].strip()

        # se por algum motivo vier com texto extra, tenta isolar o array
        if not text_resp.strip().startswith("["):
            if "[" in text_resp and "]" in text_resp:
                text_resp = "[" + text_resp.split("[", 1)[-1].rsplit("]", 1)[0] + "]"

        translated = json.loads(text_resp)

        if isinstance(translated, list) and len(translated) == len(texts):
            return translated
        logger.warning("Gemini: tamanho do array não bate ou formato inesperado.")
        return None
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return None

# -----------------------------------------------------------------------------
# OpenSubtitles (URLs corrigidas)
# -----------------------------------------------------------------------------
def get_download_link(file_id, headers):
    try:
        url = "https://api.opensubtitles.com/api/v1/download"
        r = session.post(url, headers=headers, json={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        return r.json().get("link")
    except Exception as e:
        logger.error(f"OS download error: {e}")
        return None

def search_english_sub(imdb_id, season=None, episode=None, video_hash=None, filename_hint=None):
    """
    Busca hierárquica:
    1) Hash (perfeito)
    2) IMDB + S/E (download_count)
    3) Refinar por release usando filename_hint
    """
    if not OS_API_KEY:
        return None, "No API Key"

    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    try:
        clean_id = int(imdb_id.replace("tt", ""))
    except Exception:
        return None, "Bad ID"

    # 1) moviehash
    if video_hash:
        try:
            logger.info(f"--> Buscando HASH: {video_hash}")
            url = "https://api.opensubtitles.com/api/v1/subtitles"
            params = {"moviehash": video_hash, "languages": "en"}
            res = session.get(url, headers=headers, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            if data.get("total_count", 0) > 0:
                file_id = data["data"][0]["attributes"]["files"][0]["file_id"]
                dl = get_download_link(file_id, headers)
                if dl:
                    return dl, "Hash Match"
        except Exception as e:
            logger.error(f"Erro Hash: {e}")

    # 2) IMDB + S/E, ordenado por popularidade
    try:
        logger.info("--> Buscando por Nome/IMDB")
        url = "https://api.opensubtitles.com/api/v1/subtitles"
        params = {
            "imdb_id": clean_id,
            "languages": "en",
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if season:
            params.update({"season_number": season, "episode_number": episode})

        res = session.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()

        if data.get("total_count", 0) == 0:
            return None, "Not Found"

        results = data["data"]
        best = results[0]
        method = "IMDB Generic"

        # 3) Refinar por release se tiver dica de filename
        if filename_hint:
            hint_clean = clean_filename(unquote(filename_hint))
            keywords = [k for k in ["web", "webrip", "web-dl", "hdtv", "bluray", "amzn", "nf", "ntb"]
                        if k in hint_clean]
            for item in results:
                try:
                    f_name = item["attributes"]["files"][0]["file_name"].lower()
                    if all(k in f_name for k in keywords):
                        best = item
                        method = f"Release Match ({' '.join(keywords)})"
                        break
                except Exception:
                    continue

        file_id = best["attributes"]["files"][0]["file_id"]
        dl = get_download_link(file_id, headers)
        if dl:
            return dl, method
    except Exception as e:
        logger.error(f"OS imdb search error: {e}")

    return None, "Not Found"

# -----------------------------------------------------------------------------
# Worker
# -----------------------------------------------------------------------------
def worker(imdb_id, season, episode, cache_key, video_hash, filename_hint=None):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_translated.srt")
    if os.path.exists(final_path):
        return

    logger.info(f"WORKER AI: Iniciando {cache_key}")

    url_en, method = search_english_sub(
        imdb_id, season, episode, video_hash, filename_hint
    )
    if not url_en:
        logger.error("WORKER: Sem fonte EN.")
        return

    try:
        r = session.get(url_en, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        content = r.text.replace("\r\n", "\n")
    except Exception as e:
        logger.error(f"Download EN error: {e}")
        return

    # Parse SRT
    blocks = re.split(r"\n\n+", content)
    subs = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            subs.append({
                "head": "\n".join(lines[:2]),
                "text": " ".join(lines[2:]).strip()
            })

    # Sinalização de método
    final_srt_content = []
    final_srt_content.append(
        f"0\n00:00:01,000 --> 00:00:05,000\n[AI Sync: {method}]\n"
    )

    batch_size = 18  # um pouco mais conservador
    for i in range(0, len(subs), batch_size):
        chunk = subs[i:i + batch_size]
        texts_en = [s["text"] for s in chunk]

        texts_pt = translate_batch_gemini(texts_en)
        if not texts_pt:
            texts_pt = texts_en
            logger.warning(f"Batch {i} falhou, usando EN.")

        for idx, sub in enumerate(chunk):
            line_pt = texts_pt[idx] if idx < len(texts_pt) else sub["text"]
            # opcional: wrap para TVs
            line_pt = wrap_text(line_pt, width=44)
            final_srt_content.append(f"{sub['head']}\n{line_pt}\n")

        # respeitar limites do tier free
        time.sleep(1.5)

    try:
        with open(final_path, "w", encoding="utf-8") as f:
            f.write("\n".join(final_srt_content))
        logger.info(f"WORKER: Concluído {cache_key}")
    except Exception as e:
        logger.error(f"Write SRT error: {e}")

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return f"AutoSync AI {MANIFEST['version']}"

@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)

# suporta com e sem extra
@app.route("/subtitles/<type>/<id>.json", defaults={"extra": ""})
@app.route("/subtitles/<type>/<id>/<extra>.json")
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    video_hash = None
    filename_hint = None

    try:
        # extra vem url-encoded (videoHash=...&filename=...)
        q = parse_qs(extra.replace(".json", ""), keep_blank_values=True)
        if "videoHash" in q and q["videoHash"]:
            video_hash = q["videoHash"][0]
        if "filename" in q and q["filename"]:
            filename_hint = q["filename"][0]  # manter url-encoded para heurística leve
    except Exception as e:
        logger.warning(f"extra parse error: {e}")

    cache_key = get_file_hash(imdb_id, season, episode, video_hash)
    threading.Thread(
        target=worker,
        args=(imdb_id, season, episode, cache_key, video_hash, filename_hint),
        daemon=True,
    ).start()

    host = request.host_url.rstrip("/")
    return jsonify({
        "subtitles": [{
            "id": f"ai_{cache_key}",
            "url": f"{host}/static_subs/{cache_key}_translated.srt",
            "lang": "pob",
            "format": "srt",
            "name": "PT-BR (AI)"
        }]
    })

@app.route("/static_subs/<filename>")
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)
    # espera até 60s pela primeira geração
    for _ in range(60):
        if os.path.exists(path):
            resp = make_response(send_from_directory(CACHE_DIR, filename))
            resp.headers["Cache-Control"] = "public, max-age=3600"
            return resp
        time.sleep(1)
    # placeholder amigável
    placeholder = (
        "1\n00:00:01,000 --> 00:00:10,000\n"
        "Traduzindo com IA...\nAguarde alguns segundos e volte a abrir as legendas.\n\n"
    )
    return (placeholder, 200, {"Content-Type": "application/x-subrip"})

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
