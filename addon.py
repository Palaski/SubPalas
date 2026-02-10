import os
import logging
import threading
import time
import json
import re
import shutil
import subprocess
from queue import Queue, Empty

import requests
import google.generativeai as genai
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# ----------------------------
# Config
# ----------------------------

app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("SubPalas")

# Use /tmp (Render-friendly) by default
BASE_TMP = os.getenv("TMPDIR", "/tmp")
CACHE_DIR = os.path.join(BASE_TMP, "subtitle_cache")
TEMP_DIR = os.path.join(BASE_TMP, "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "SubPalas v2.1-lite-patched")

# Gemini config
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "org.subpalas.lite",
    "version": "2.1.1",
    "name": "SubPalas (Lite)",
    "description": "Legendas PT-BR Otimizadas. Sincronia única (Fast) ou Tradução IA.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# ----------------------------
# Job Queue (single worker + dedupe)
# ----------------------------

JOB_Q = Queue(maxsize=200)
IN_PROGRESS = set()
IN_PROGRESS_LOCK = threading.Lock()

def enqueue_job(imdb_id, season, episode, cache_key) -> bool:
    """Enqueue only once per cache_key to avoid thread storms / duplicate work."""
    with IN_PROGRESS_LOCK:
        if cache_key in IN_PROGRESS:
            return False
        IN_PROGRESS.add(cache_key)
    try:
        JOB_Q.put_nowait((imdb_id, season, episode, cache_key))
        return True
    except Exception:
        with IN_PROGRESS_LOCK:
            IN_PROGRESS.discard(cache_key)
        return False

def worker_loop():
    while True:
        try:
            imdb_id, season, episode, cache_key = JOB_Q.get(timeout=1)
        except Empty:
            continue

        try:
            run_process(imdb_id, season, episode, cache_key)
        except Exception as e:
            logger.exception(f"Worker error {cache_key}: {e}")
        finally:
            with IN_PROGRESS_LOCK:
                IN_PROGRESS.discard(cache_key)
            JOB_Q.task_done()

threading.Thread(target=worker_loop, daemon=True).start()

# ----------------------------
# Utilities
# ----------------------------

def get_file_hash(imdb_id, season=None, episode=None):
    base = f"{imdb_id}"
    if season is not None and episode is not None:
        base += f"_S{season}E{episode}"
    return base

def cleanup_temp(paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def generate_loading_srt(msg="Carregando..."):
    return (
        "1\n00:00:00,000 --> 00:00:10,000\n"
        f"SubPalas: {msg}\nAguarde o processamento...\n\n"
        "2\n00:00:10,500 --> 00:00:20,000\n"
        "Se demorar, volte e selecione novamente.\n"
    )

# ----------------------------
# Network
# ----------------------------

def download_file(url, dest_path) -> bool:
    if not url:
        return False
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        # Separate connect/read timeouts (less stuck threads)
        with requests.get(url, stream=True, timeout=(6, 30), headers=headers) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

        if os.path.getsize(dest_path) < 100:
            return False
        return True
    except Exception as e:
        logger.error(f"Download Error: {e}")
        return False

def get_download_link(file_id, headers):
    try:
        res = requests.post(
            "https://api.opensubtitles.com/api/v1/download",
            headers=headers,
            json={"file_id": file_id},
            timeout=(6, 20),
        )
        res.raise_for_status()
        return res.json().get("link")
    except Exception:
        return None

# ----------------------------
# OpenSubtitles Search
# ----------------------------

def search_subtitles(imdb_id, lang, season=None, episode=None):
    if not OS_API_KEY:
        return None
    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    try:
        clean_id = int(imdb_id.replace("tt", ""))
        params = {
            "imdb_id": clean_id,
            "languages": lang,
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if season is not None and episode is not None:
            params.update({"season_number": season, "episode_number": episode})

        res = requests.get(
            "https://api.opensubtitles.com/api/v1/subtitles",
            headers=headers,
            params=params,
            timeout=(6, 20),
        )
        res.raise_for_status()
        data = res.json()

        if data.get("total_count", 0) > 0:
            return data.get("data")
    except Exception as e:
        logger.error(f"Erro busca {lang}: {e}")
    return None

# ----------------------------
# Gemini Translation
# ----------------------------

_gemini_model = None
def get_gemini_model():
    global _gemini_model
    if not GEMINI_API_KEY:
        return None
    if _gemini_model is None:
        # flash = faster + cheaper
        _gemini_model = genai.GenerativeModel("gemini-2.5-flash")
    return _gemini_model

def translate_batch_gemini(texts):
    model = get_gemini_model()
    if not model:
        return None

    prompt = (
        "Translate these subtitles to Brazilian Portuguese (PT-BR). "
        "Keep it natural, concise, and slang-appropriate. "
        "Return ONLY a JSON array of strings. No markdown.\n"
        f"Input: {json.dumps(texts, ensure_ascii=False)}"
    )
    try:
        response = model.generate_content(prompt)
        text_resp = (response.text or "").replace("```json", "").replace("```", "").strip()
        translated = json.loads(text_resp)
        if isinstance(translated, list) and len(translated) == len(texts):
            # Ensure strings
            if all(isinstance(x, str) for x in translated):
                return translated
    except Exception:
        pass
    return None

# ----------------------------
# Streaming SRT parsing (fallback)
# ----------------------------

def iter_srt_blocks_from_stream(resp):
    """
    Parse SRT in streaming mode.
    Yields list[str] for each subtitle block separated by blank line.
    """
    buf = []
    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").rstrip("\r")
        if line == "":
            if buf:
                yield buf
                buf = []
        else:
            buf.append(line)
    if buf:
        yield buf

def normalize_srt_block(block_lines):
    # Expected: index line, timestamp line, then 1+ text lines
    if len(block_lines) < 3:
        return None
    head = "\n".join(block_lines[:2])
    text = " ".join(block_lines[2:]).strip()
    if not text:
        return None
    return head, text

def run_translation_fallback(imdb_id, season, episode, cache_key):
    """
    Fallback: download EN -> translate in small batches -> write incrementally
    to avoid RAM spikes.
    """
    logger.info("FALLBACK: Iniciando tradução via IA...")

    results_en = search_subtitles(imdb_id, "en", season, episode)
    if not results_en:
        logger.error("FALLBACK: Nem legenda em inglês foi encontrada.")
        return

    best_file = results_en[0]["attributes"]["files"][0]
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    url_en = get_download_link(best_file["file_id"], headers)
    if not url_en:
        logger.error("FALLBACK: Sem link de download EN.")
        return

    final_path = os.path.join(CACHE_DIR, f"{cache_key}_synced.srt")

    try:
        with requests.get(url_en, stream=True, timeout=(6, 40), headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()

            with open(final_path, "w", encoding="utf-8") as out:
                out.write("0\n00:00:01,000 --> 00:00:05,000\n[SubPalas: Tradução IA (Fallback)]\n\n")

                batch_texts = []
                batch_heads = []
                batch_size = 20

                for block_lines in iter_srt_blocks_from_stream(resp):
                    parsed = normalize_srt_block(block_lines)
                    if not parsed:
                        continue
                    head, text = parsed
                    batch_heads.append(head)
                    batch_texts.append(text)

                    if len(batch_texts) >= batch_size:
                        translated = translate_batch_gemini(batch_texts) if GEMINI_API_KEY else None
                        target = translated if translated else batch_texts
                        for h, t in zip(batch_heads, target):
                            out.write(f"{h}\n{t}\n\n")
                        batch_texts.clear()
                        batch_heads.clear()
                        time.sleep(0.6)  # mild pacing (API + CPU)

                if batch_texts:
                    translated = translate_batch_gemini(batch_texts) if GEMINI_API_KEY else None
                    target = translated if translated else batch_texts
                    for h, t in zip(batch_heads, target):
                        out.write(f"{h}\n{t}\n\n")

        logger.info("FALLBACK: Tradução concluída.")
    except Exception as e:
        logger.error(f"FALLBACK Error: {e}")

# ----------------------------
# Core Process (single sync)
# ----------------------------

def run_process(imdb_id, season, episode, cache_key):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_synced.srt")

    # If already exists and looks valid, do nothing
    if os.path.exists(final_path) and os.path.getsize(final_path) > 100:
        return

    logger.info(f"--- PROCESSANDO (LITE PATCHED): {cache_key} ---")

    # 1) Search PT-BR
    ptbr_results = search_subtitles(imdb_id, "pt-br", season, episode)

    if not ptbr_results:
        logger.warning("PT-BR não encontrada. Ativando Fallback IA.")
        run_translation_fallback(imdb_id, season, episode, cache_key)
        return

    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}

    try:
        url_pt = get_download_link(ptbr_results[0]["attributes"]["files"][0]["file_id"], headers)
        if not url_pt:
            logger.error("Sem link PT-BR.")
            return

        path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
        if not download_file(url_pt, path_pt):
            return

        # 2) Search EN reference (best one)
        en_results = search_subtitles(imdb_id, "en", season, episode)
        if not en_results:
            shutil.copy(path_pt, final_path)  # No ref -> original
            return

        best_ref_url = None
        ref_type = "POPULAR"

        def safe_fname(item):
            try:
                return (item["attributes"]["files"][0].get("file_name") or "").lower()
            except Exception:
                return ""

        # Prefer WEB
        for item in en_results:
            fname = safe_fname(item)
            if ("web" in fname) or ("amzn" in fname) or ("nf" in fname) or ("netflix" in fname):
                best_ref_url = get_download_link(item["attributes"]["files"][0]["file_id"], headers)
                ref_type = "WEB-DL"
                break

        # else HDTV
        if not best_ref_url:
            for item in en_results:
                fname = safe_fname(item)
                if "hdtv" in fname:
                    best_ref_url = get_download_link(item["attributes"]["files"][0]["file_id"], headers)
                    ref_type = "HDTV"
                    break

        # else most popular
        if not best_ref_url:
            best_ref_url = get_download_link(en_results[0]["attributes"]["files"][0]["file_id"], headers)

        if not best_ref_url:
            shutil.copy(path_pt, final_path)
            return

        path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref.srt")
        if not download_file(best_ref_url, path_ref):
            shutil.copy(path_pt, final_path)
            return

        cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", final_path, "--encoding", "utf-8"]
        logger.info(f"Sincronizando com {ref_type}...")

        try:
            # CRITICAL: do NOT capture output (RAM killer)
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=120
            )
            logger.info("Sincronia concluída com sucesso.")
        except Exception as e:
            logger.error(f"Erro ffsubsync: {e}")
            shutil.copy(path_pt, final_path)

    finally:
        cleanup_temp([
            os.path.join(TEMP_DIR, f"{cache_key}_pt.srt"),
            os.path.join(TEMP_DIR, f"{cache_key}_ref.srt"),
        ])

# ----------------------------
# Routes
# ----------------------------

@app.route("/")
def index():
    return "SubPalas Lite v2.1.1 (patched) Running"

@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)

@app.route("/subtitles/<type>/<id>/<extra>.json")
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None
    cache_key = get_file_hash(imdb_id, season, episode)

    # enqueue (single worker + dedupe)
    enqueue_job(imdb_id, season, episode, cache_key)

    host = request.host_url.rstrip("/")
    return jsonify({
        "subtitles": [
            {
                "id": f"sp_sync_{cache_key}",
                "url": f"{host}/static_subs/{cache_key}_synced.srt",
                "lang": "pob",
                "format": "srt"
            }
        ]
    })

@app.route("/static_subs/<filename>")
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)

    # Patch: respond immediately if not ready (avoid holding connections 45s)
    if os.path.exists(path) and os.path.getsize(path) > 100:
        resp = make_response(send_from_directory(CACHE_DIR, filename))
        resp.headers["Cache-Control"] = "public, max-age=31536000"
        return resp

    return generate_loading_srt("Sincronizando..."), 200, {"Content-Type": "application/x-subrip"}

# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7000"))
    app.run(host="0.0.0.0", port=port)
