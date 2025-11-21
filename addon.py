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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash").strip()
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync/1.0")
PORT = int(os.environ.get("PORT", 7000))

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "community.autotranslate.ai",
    "version": "0.4.0",
    "name": "AutoSync AI (Gemini)",
    "description": "Tradução PT-BR via Gemini (one-shot) + busca inteligente de release (sem proxy de vídeo).",
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
    return name.replace('.', ' ').replace('-', ' ').lower()

def wrap_text(s: str, width: int = 44) -> str:
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

def sanitize_input_lines(lines):
    out = []
    for t in lines:
        t = (t or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
        out.append(t)
    return out

# -----------------------------------------------------------------------------
# JSON Parser robusto (para saída em array quando usar batch)
# -----------------------------------------------------------------------------
def _parse_json_array_strict(s: str, expected_len: int | None = None):
    try:
        if "```" in s:
            s = re.sub(r"```(?:json)?", "", s).strip()
        if "[" in s and "]" in s:
            s = "[" + s.split("[", 1)[-1].rsplit("]", 1)[0] + "]"
        s = s.replace("“", "\"").replace("”", "\"").replace("’", "'")
        s = re.sub(r",\s*\]", "]", s)
        data = json.loads(s)
        if isinstance(data, list):
            data = [str(x).replace("\r\n", "\n").replace("\r", "\n") for x in data]
            if expected_len is None or len(data) == expected_len:
                return data
    except Exception:
        pass
    try:
        inner = s.strip()
        if inner.startswith("[") and inner.endswith("]"):
            inner = inner[1:-1]
        parts = re.split(r'"\s*,\s*"', inner)
        parts = [p.strip().strip('"') for p in parts if p.strip()]
        if expected_len is None or len(parts) == expected_len:
            return parts
    except Exception:
        pass
    raise ValueError("invalid json array from model")

# -----------------------------------------------------------------------------
# LLM Translation (Gemini) - BATCH (fallback)
# -----------------------------------------------------------------------------
def translate_batch_gemini(texts):
    if not GEMINI_API_KEY:
        return None

    texts = sanitize_input_lines(texts)

    try:
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": { "type": "array", "items": { "type": "string" } },
                "temperature": 0.2,
            },
            system_instruction=(
                "Você é um tradutor de legendas EN->PT-BR. "
                "Retorne APENAS um array JSON de strings (um item por fala), sem markdown."
            ),
        )
    except Exception as e:
        logger.error(f"Gemini init error: {e}")
        return None

    prompt = (
        "Traduza cada fala do array abaixo para PT-BR, mantendo a ordem e o total de itens. "
        "Responda somente com um array JSON de strings.\n\n"
        f"{json.dumps(texts, ensure_ascii=False)}"
    )

    for attempt in range(2):
        try:
            resp = model.generate_content(prompt)
            raw = (resp.text or "").strip()
            return _parse_json_array_strict(raw, expected_len=len(texts))
        except Exception as e:
            logger.error(f"Gemini Error (attempt {attempt+1}/2): {e}")
            time.sleep(0.8)
    return None

# -----------------------------------------------------------------------------
# LLM Translation (Gemini) - ONE SHOT (preferido)
# -----------------------------------------------------------------------------
def _one_shot_try(model_name: str, srt_text: str) -> str | None:
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={
                "response_mime_type": "text/plain",
                "temperature": 0.2,
            },
            system_instruction=(
                "Você é um tradutor profissional de legendas EN->PT-BR. "
                "Receberá um arquivo SRT completo e deve devolver o MESMO SRT, "
                "preservando integralmente: numeração, timestamps e quebras de bloco. "
                "Traduza APENAS o texto das falas para PT-BR, conciso e natural. "
                "NÃO adicione comentários, cabeçalhos ou markdown."
            ),
        )
        prompt = (
            "Traduza para PT-BR mantendo a estrutura SRT idêntica (numeração e timestamps iguais). "
            "Responda com SRT puro:\n\n"
            f"{srt_text}"
        )
        resp = model.generate_content(prompt)
        out = (resp.text or "").strip()
        # validação leve: deve conter algum timestamp SRT
        if re.search(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", out):
            return out
        return out or None
    except Exception as e:
        msg = str(e)
        logger.error(f"Gemini one-shot error [{model_name}]: {msg}")
        # backoff amigável se o erro sugeriu retry
        m = re.search(r"retry in\s+(\d+)\s*s", msg.lower())
        if m:
            wait_s = min(int(m.group(1)), 35)
            time.sleep(wait_s)
        else:
            time.sleep(1.5)
        return None

def translate_srt_one_shot(srt_text: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    srt_clean = srt_text.replace("\r\n", "\n").strip()

    # tenta modelo principal e 2 alternativos (menos disputados)
    candidates = [
        GEMINI_MODEL,
        "models/gemini-flash-latest",
        "models/gemini-2.0-flash-lite",
    ]
    for mid in candidates:
        out = _one_shot_try(mid, srt_clean)
        if out:
            if mid != GEMINI_MODEL:
                logger.info(f"Gemini one-shot: usou modelo alternativo {mid}")
            return out
    return None

# -----------------------------------------------------------------------------
# OpenSubtitles (URLs corretas)
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
        r = session.get(url_en, timeout=25)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        content = r.text.replace("\r\n", "\n")
    except Exception as e:
        logger.error(f"Download EN error: {e}")
        return

    # ------- TENTATIVA 1: one-shot (1 request por episódio) -------
    srt_pt = translate_srt_one_shot(content)
    if srt_pt:
        banner = (
            f"0\n00:00:01,000 --> 00:00:05,000\n"
            f"[AI Sync: {method} | Model: {GEMINI_MODEL} | mode=one-shot]\n\n"
        )
        try:
            with open(final_path, "w", encoding="utf-8") as f:
                f.write(banner + srt_pt)
            logger.info(f"WORKER: Concluído {cache_key} (one-shot)")
            return
        except Exception as e:
            logger.error(f"Write SRT error (one-shot): {e}")
            # cai pro fallback por segurança

    # ------- FALLBACK: batching (menos requisições, throttle) -------
    blocks = re.split(r"\n\n+", content)
    subs = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            subs.append({
                "head": "\n".join(lines[:2]),
                "text": " ".join(lines[2:]).strip()
            })

    final_srt_content = []
    final_srt_content.append(
        f"0\n00:00:01,000 --> 00:00:05,000\n"
        f"[AI Sync: {method} | Model: {GEMINI_MODEL} | mode=batch]\n"
    )

    batch_size = 40
    throttle = 2.2  # segura para não levar 429 no free tier (~10 req/min)

    for i in range(0, len(subs), batch_size):
        chunk = subs[i:i + batch_size]
        texts_en = [s["text"] for s in chunk]

        texts_pt = None
        for attempt in range(2):
            texts_pt = translate_batch_gemini(texts_en)
            if texts_pt:
                break
            time.sleep(throttle + attempt)

        if not texts_pt:
            texts_pt = texts_en
            logger.warning(f"Batch {i} falhou (sem tradução), usando EN.")

        for idx, sub in enumerate(chunk):
            line_pt = texts_pt[idx] if idx < len(texts_pt) else sub["text"]
            line_pt = wrap_text(line_pt, width=44)
            final_srt_content.append(f"{sub['head']}\n{line_pt}\n")

        time.sleep(throttle)

    try:
        with open(final_path, "w", encoding="utf-8") as f:
            f.write("\n".join(final_srt_content))
        logger.info(f"WORKER: Concluído {cache_key} (fallback batch)")
    except Exception as e:
        logger.error(f"Write SRT error (batch): {e}")

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
        cleaned = extra.replace(".json", "")
        q = parse_qs(cleaned, keep_blank_values=True)
        if "videoHash" in q and q["videoHash"]:
            video_hash = q["videoHash"][0]
        if "filename" in q and q["filename"]:
            filename_hint = q["filename"][0]  # mantemos encoded; heurística trata
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
