import os
import logging
import threading
import time
import requests
import subprocess
import shutil
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# =========================
# Configurações
# =========================

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoSyncAddon")

# Em PaaS (Render etc.), /tmp é o lugar mais seguro pro filesystem efêmero.
BASE_TMP = os.getenv("BASE_TMP", "/tmp")

CACHE_DIR = os.path.join(BASE_TMP, "subtitle_cache")
TEMP_DIR = os.path.join(BASE_TMP, "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
OS_USERNAME = os.getenv("OS_USERNAME", "")
OS_PASSWORD = os.getenv("OS_PASSWORD", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

_OS_DEFAULT_BASE_URL = "https://api.opensubtitles.com/api/v1"
_os_base_url = _OS_DEFAULT_BASE_URL

_token_lock = threading.Lock()
_os_token = None
_os_token_expiry = 0

# Evita disparar múltiplas threads pro mesmo item em paralelo (por worker)
_inflight_lock = threading.Lock()
_inflight = set()

MANIFEST = {
    "id": "community.autosync.ptbr",
    "version": "0.1.0",
    "name": "AutoSync PT-BR (Triple Ref)",
    "description": "3 Versões: WEB (v1), HDTV (v2) e BluRay (v3). Teste as opções se houver drift.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# =========================
# Utilitários
# =========================

def get_file_hash(imdb_id, season=None, episode=None):
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

def generate_loading_srt(variant_name: str):
    return (
        "1\n"
        "00:00:00,000 --> 00:00:10,000\n"
        f"Sincronizando ({variant_name})... Aguarde...\n\n"
        "2\n"
        "00:00:10,500 --> 00:00:20,000\n"
        "Se esta mensagem persistir por >30s,\nselecione outra versão na lista.\n"
    )

def variant_from_filename(filename: str) -> str:
    if "_v1" in filename:
        return "WEB-DL"
    if "_v2" in filename:
        return "HDTV"
    return "BluRay"

def normalize_base_url(raw: str) -> str:
    """
    Garante que a base_url:
      - tenha scheme (https://)
      - termine em /api/v1
    """
    if not raw:
        return _OS_DEFAULT_BASE_URL

    raw = raw.strip()

    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "https://" + raw.lstrip("/")

    parsed = urlparse(raw)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")

    if path.endswith("/api/v1"):
        return base + path
    if path.endswith("/api"):
        return base + path + "/v1"

    return base + "/api/v1"

# =========================
# OpenSubtitles Auth + Requests
# =========================

def os_login():
    global _os_token, _os_base_url, _os_token_expiry

    if not (OS_API_KEY and OS_USERNAME and OS_PASSWORD):
        return None, _os_base_url

    now = time.time()
    with _token_lock:
        if _os_token and now < _os_token_expiry:
            return _os_token, _os_base_url

        headers = {
            "Api-Key": OS_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = {"username": OS_USERNAME, "password": OS_PASSWORD}

        r = None
        try:
            r = requests.post(f"{_OS_DEFAULT_BASE_URL}/login", headers=headers, json=payload, timeout=12)
            r.raise_for_status()
            j = r.json()

            _os_token = j.get("token")
            returned_base = j.get("base_url")
            _os_base_url = normalize_base_url(returned_base) if returned_base else _OS_DEFAULT_BASE_URL

            _os_token_expiry = now + 23 * 3600

            if not _os_token:
                logger.error(f"Login respondeu sem token. Body={str(j)[:300]}")
                _os_token_expiry = 0
                return None, _os_base_url

            logger.info(f"OpenSubtitles base_url em uso: {_os_base_url}")
            return _os_token, _os_base_url

        except Exception as e:
            body = ""
            if r is not None:
                try:
                    body = r.text[:300]
                except Exception:
                    body = ""
            logger.error(f"OpenSubtitles login falhou: {e} | resp={body}")
            _os_token = None
            _os_token_expiry = 0
            _os_base_url = _OS_DEFAULT_BASE_URL
            return None, _os_base_url

def os_headers(require_auth=False):
    token, base_url = os_login() if require_auth else (None, _os_base_url)
    h = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if require_auth and token:
        h["Authorization"] = f"Bearer {token}"
    return h, base_url

def get_download_link(file_id):
    headers, base_url = os_headers(require_auth=True)

    res = None
    try:
        res = requests.post(f"{base_url}/download", headers=headers, json={"file_id": file_id}, timeout=12)

        if res.status_code in (401, 403):
            global _os_token, _os_token_expiry
            with _token_lock:
                _os_token = None
                _os_token_expiry = 0

            headers, base_url = os_headers(require_auth=True)
            res = requests.post(f"{base_url}/download", headers=headers, json={"file_id": file_id}, timeout=12)

        res.raise_for_status()
        return res.json().get("link")

    except Exception as e:
        body = ""
        status = getattr(res, "status_code", None)
        if res is not None:
            try:
                body = res.text[:300]
            except Exception:
                body = ""
        logger.error(f"Erro /download file_id={file_id}: {e} | status={status} | body={body}")
        return None

def search_best_ptbr(imdb_id, season=None, episode=None):
    if not OS_API_KEY:
        return None

    headers, base_url = os_headers(require_auth=False)
    res = None

    try:
        params = {
            "imdb_id": int(imdb_id.replace("tt", "")),
            "languages": "pt-BR",
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if season is not None and episode is not None:
            params.update({"season_number": season, "episode_number": episode})

        res = requests.get(f"{base_url}/subtitles", headers=headers, params=params, timeout=12)
        res.raise_for_status()
        data = res.json()

        logger.info(f"PT-BR total_count={data.get('total_count')} page={data.get('page')}")

        if data.get("total_count", 0) > 0 and data.get("data"):
            files = data["data"][0].get("attributes", {}).get("files", []) or []
            if files and files[0].get("file_id"):
                return get_download_link(files[0]["file_id"])

    except Exception as e:
        body = ""
        if res is not None:
            try:
                body = res.text[:300]
            except Exception:
                body = ""
        logger.error(f"Erro busca PT-BR: {e} | body={body}")

    return None

def search_references_opensubtitles(imdb_id, season=None, episode=None):
    if not OS_API_KEY:
        return {}

    headers, base_url = os_headers(require_auth=False)
    res = None

    try:
        clean_id = int(imdb_id.replace("tt", ""))
    except Exception:
        return {}

    params = {
        "imdb_id": clean_id,
        "languages": "en",
        "order_by": "download_count",
        "order_direction": "desc",
    }
    if season is not None and episode is not None:
        params.update({"season_number": season, "episode_number": episode})

    references = {}

    try:
        res = requests.get(f"{base_url}/subtitles", headers=headers, params=params, timeout=12)
        res.raise_for_status()
        data = res.json()

        results = data.get("data", []) or []
        if data.get("total_count", 0) > 0 and results:
            for item in results:
                if len(references) >= 3:
                    break

                files = item.get("attributes", {}).get("files", []) or []
                if not files:
                    continue

                f = files[0]
                fname = (f.get("file_name") or "").lower()
                file_id = f.get("file_id")
                if not file_id:
                    continue

                rtype = None
                if any(x in fname for x in ["web", "amzn", "nf", "hulu", "netflix", "disney", "web-dl", "webrip"]):
                    rtype = "WEB"
                elif any(x in fname for x in ["bluray", "bdrip", "brrip", "blue", "bdr"]):
                    rtype = "BLURAY"
                elif any(x in fname for x in ["hdtv", "pdtv", "dsr"]):
                    rtype = "HDTV"

                if rtype and rtype not in references:
                    link = get_download_link(file_id)
                    if link:
                        references[rtype] = link

            if not references and results:
                first_files = results[0].get("attributes", {}).get("files", []) or []
                if first_files and first_files[0].get("file_id"):
                    link = get_download_link(first_files[0]["file_id"])
                    if link:
                        references["DEFAULT"] = link

    except Exception as e:
        body = ""
        if res is not None:
            try:
                body = res.text[:300]
            except Exception:
                body = ""
        logger.error(f"Erro busca EN: {e} | body={body}")

    return references

def download_file(url, dest_path):
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Falha download: {e} url={url}")
        return False

# =========================
# Core Logic
# =========================

def _mark_inflight(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True

def _unmark_inflight(key: str):
    with _inflight_lock:
        _inflight.discard(key)

def run_sync_thread(imdb_id, season, episode, cache_key):
    if not _mark_inflight(cache_key):
        return

    try:
        logger.info(f"Processando TRIPLE SYNC para {cache_key}...")

        url_pt = search_best_ptbr(imdb_id, season, episode)
        if not url_pt:
            logger.error("Não achou PT-BR no OpenSubtitles (ou /download falhou).")
            return

        path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
        if not download_file(url_pt, path_pt):
            logger.error("Falhou download PT-BR.")
            return

        refs_dict = search_references_opensubtitles(imdb_id, season, episode)
        files_clean = [path_pt]

        if not refs_dict:
            logger.error("Não achou referências EN. Fallback: v1 = PT-BR puro.")
            try:
                shutil.copy(path_pt, os.path.join(CACHE_DIR, f"{cache_key}_v1.srt"))
            except Exception:
                pass
            cleanup_temp(files_clean)
            return

        priority_order = ["WEB", "HDTV", "BLURAY", "DEFAULT"]
        final_refs = []
        for p in priority_order:
            if p in refs_dict:
                final_refs.append((p, refs_dict[p]))
            if len(final_refs) >= 3:
                break

        for i, (rtype, url) in enumerate(final_refs):
            version_label = f"v{i+1}"
            final_path = os.path.join(CACHE_DIR, f"{cache_key}_{version_label}.srt")
            tmp_out = final_path + ".tmp"

            path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref_{rtype}.srt")
            files_clean.append(path_ref)

            if not download_file(url, path_ref):
                logger.error(f"Falhou download referência {rtype}.")
                continue

            cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", tmp_out, "--encoding", "utf-8"]
            logger.info(f"Syncing {version_label} ({rtype})...")

            try:
                p = subprocess.run(cmd, capture_output=True, text=True, check=True)
                if p.stderr:
                    logger.info(f"ffsubsync stderr ({version_label}): {p.stderr[-300:]}")
                os.replace(tmp_out, final_path)

            except subprocess.CalledProcessError as e:
                logger.error(
                    f"ffsubsync falhou ({version_label}) rc={e.returncode} "
                    f"stderr={e.stderr[-500:] if e.stderr else ''}"
                )
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass
            except FileNotFoundError:
                logger.error("ffsubsync não está instalado no servidor (FileNotFoundError).")
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass
                break
            except Exception as e:
                logger.error(f"Erro inesperado ffsubsync ({version_label}): {e}")
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass

        cleanup_temp(files_clean)
        logger.info(f"Concluído {cache_key}")

    finally:
        _unmark_inflight(cache_key)

# =========================
# Rotas
# =========================

@app.route("/")
def index():
    return "AutoSync Triple Ref Running"

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

    # dispara async (não depende de placeholder em disco)
    threading.Thread(
        target=run_sync_thread,
        args=(imdb_id, season, episode, cache_key),
        daemon=True
    ).start()

    host = request.host_url.rstrip("/")

    return jsonify({
        "subtitles": [
            {"id": f"as_v1_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v1.srt", "lang": "pob", "format": "srt"},
            {"id": f"as_v2_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v2.srt", "lang": "pob", "format": "srt"},
            {"id": f"as_v3_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v3.srt", "lang": "pob", "format": "srt"},
        ]
    })

@app.route("/static_subs/<filename>")
def serve_subs(filename):
    """
    Regra nova:
      - se existe arquivo final: serve
      - se não existe: devolve placeholder inline IMEDIATO
    Isso remove totalmente o problema "placeholder não apareceu" (multi-worker).
    """
    file_path = os.path.join(CACHE_DIR, filename)

    if os.path.exists(file_path):
        response = make_response(send_from_directory(CACHE_DIR, filename))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    variant = variant_from_filename(filename)
    response = make_response(generate_loading_srt(variant))
    response.headers["Content-Type"] = "application/x-subrip"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7000))
    app.run(host="0.0.0.0", port=port)
