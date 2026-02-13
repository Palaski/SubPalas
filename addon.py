import os
import re
import time
import json
import logging
import threading
import shutil
import requests
from typing import Optional, List, Dict, Tuple
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# ============================================================
#  Simple Subtitle Addon
#  - Busca melhor PT-BR no OpenSubtitles
#  - Se não houver PT-BR, busca melhor EN e traduz via Gemini (REST)
#  - 1 legenda apenas (addon "comum")
#  - Cache em disco
#  - Thread para processamento assíncrono
#  - OpenSubtitles com login/token (BearER) + backoff p/ 429
# ============================================================

# -------------------------
# Flask / Config
# -------------------------

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SimpleSubtitleAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# -------------------------
# ENV VARS (Render)
# -------------------------
# OpenSubtitles
OS_API_KEY = os.getenv("OS_API_KEY", "").strip()
OS_USERNAME = os.getenv("OS_USERNAME", "").strip()
OS_PASSWORD = os.getenv("OS_PASSWORD", "").strip()

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Identificação do app (NÃO use User-Agent de browser)
USER_AGENT = os.getenv("APP_USER_AGENT", "SubpalasAddon v0.1.0").strip()

# OpenSubtitles base
OS_BASE_DEFAULT = os.getenv("OS_BASE_URL", "https://api.opensubtitles.com/api/v1").rstrip("/")
# Se seu ambiente bloquear, teste:
# OS_BASE_DEFAULT = "https://www.opensubtitles.com/api/v1"

# Gemini model via REST (pode mudar se quiser)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview").strip()

# -------------------------
# Manifest (Stremio-like)
# -------------------------

MANIFEST = {
    "id": "community.subs.ptbr.simple",
    "version": "0.2.0",
    "name": "Subs PT-BR (fallback EN->PT via Gemini)",
    "description": "Pega a melhor PT-BR. Se não tiver, baixa EN e traduz via Gemini.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"],
}

# ============================================================
# Locks / Cache keys
# ============================================================

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

def get_lock(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]

def get_cache_key(imdb_id: str, season: Optional[int], episode: Optional[int]) -> str:
    base = imdb_id
    if season is not None and episode is not None:
        base += f"_S{season}E{episode}"
    return base

def generate_loading_srt(msg: str) -> str:
    return (
        "1\n"
        "00:00:00,000 --> 00:00:12,000\n"
        f"{msg}\n\n"
        "2\n"
        "00:00:12,500 --> 00:00:25,000\n"
        "Se demorar muito, tente novamente.\n"
    )

def cleanup_temp(files: List[str]) -> None:
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

def download_file(url: str, dest_path: str) -> bool:
    try:
        with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=30) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Download falhou: {e}")
        return False

# ============================================================
# OpenSubtitles (login/token + backoff)
# ============================================================

_os_token: Optional[str] = None
_os_base_url: str = OS_BASE_DEFAULT
_os_token_ts: float = 0.0
_os_token_lock = threading.Lock()

def os_headers(with_auth: bool = True) -> Dict[str, str]:
    h = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-User-Agent": USER_AGENT,
    }
    if with_auth and _os_token:
        h["Authorization"] = f"Bearer {_os_token}"
    return h

def _sleep_backoff(resp: requests.Response, attempt: int) -> None:
    # Respeita Retry-After se vier, senão backoff progressivo.
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            wait = int(ra)
        else:
            wait = min(10, 1 + attempt * 2)
    except Exception:
        wait = min(10, 1 + attempt * 2)
    time.sleep(max(1, wait))

def os_login(force: bool = False) -> bool:
    """
    Faz login e pega token. Em alguns casos o login pode retornar base_url.
    Evita logar sempre (rate limit).
    """
    global _os_token, _os_base_url, _os_token_ts

    if not (OS_API_KEY and OS_USERNAME and OS_PASSWORD):
        logger.error("OpenSubtitles: faltam OS_API_KEY / OS_USERNAME / OS_PASSWORD.")
        return False

    with _os_token_lock:
        if _os_token and not force:
            # token já existe; você pode adicionar TTL aqui se quiser renovar periodicamente.
            return True

        url = f"{_os_base_url}/login"
        payload = {"username": OS_USERNAME, "password": OS_PASSWORD}

        for attempt in range(5):
            try:
                r = requests.post(url, headers=os_headers(with_auth=False), json=payload, timeout=20)

                if r.status_code == 429:
                    logger.warning("OpenSubtitles /login 429 (rate limit). Backoff...")
                    _sleep_backoff(r, attempt)
                    continue

                r.raise_for_status()
                data = r.json()

                token = data.get("token")
                if not token:
                    logger.error(f"OpenSubtitles /login sem token. body={str(data)[:300]}")
                    return False

                _os_token = token
                _os_token_ts = time.time()

                # Alguns fluxos devolvem base_url (ex: vip-api). Use se existir.
                base_url = data.get("base_url")
                if base_url:
                    _os_base_url = base_url.rstrip("/")
                    logger.info(f"OpenSubtitles base_url atualizado para: {_os_base_url}")

                return True

            except Exception as e:
                body = ""
                try:
                    body = r.text[:300]  # type: ignore
                except Exception:
                    pass
                logger.error(f"OpenSubtitles /login falhou: {e} body={body}")
                time.sleep(min(10, 1 + attempt * 2))

        return False

def os_get_with_retry(path: str, params: Dict, tries: int = 6) -> Optional[requests.Response]:
    """
    GET com retry p/ 401 (relogin) e 429 (backoff).
    """
    if not OS_API_KEY:
        return None

    # garante token
    if not _os_token:
        if not os_login():
            return None

    url = f"{_os_base_url}{path}"

    for attempt in range(tries):
        try:
            r = requests.get(url, headers=os_headers(with_auth=True), params=params, timeout=25)

            if r.status_code == 401:
                # token inválido/expirado -> relogin e tenta de novo
                logger.warning("OpenSubtitles GET 401. Re-login...")
                if not os_login(force=True):
                    return None
                continue

            if r.status_code == 429:
                logger.warning("OpenSubtitles GET 429 (rate limit). Backoff...")
                _sleep_backoff(r, attempt)
                continue

            r.raise_for_status()
            return r

        except Exception as e:
            body = ""
            try:
                body = r.text[:300]  # type: ignore
            except Exception:
                pass
            logger.error(f"OpenSubtitles GET falhou: {e} body={body}")
            time.sleep(min(10, 1 + attempt * 2))

    return None

def os_post_with_retry(path: str, payload: Dict, tries: int = 6) -> Optional[requests.Response]:
    """
    POST com retry p/ 401 (relogin) e 429 (backoff).
    """
    if not OS_API_KEY:
        return None

    if not _os_token:
        if not os_login():
            return None

    url = f"{_os_base_url}{path}"

    for attempt in range(tries):
        try:
            r = requests.post(url, headers=os_headers(with_auth=True), json=payload, timeout=25)

            if r.status_code == 401:
                logger.warning("OpenSubtitles POST 401. Re-login...")
                if not os_login(force=True):
                    return None
                continue

            if r.status_code == 429:
                logger.warning("OpenSubtitles POST 429 (rate limit). Backoff...")
                _sleep_backoff(r, attempt)
                continue

            r.raise_for_status()
            return r

        except Exception as e:
            body = ""
            try:
                body = r.text[:300]  # type: ignore
            except Exception:
                pass
            logger.error(f"OpenSubtitles POST falhou: {e} body={body}")
            time.sleep(min(10, 1 + attempt * 2))

    return None

def os_get_download_link(file_id: int) -> Optional[str]:
    """
    /download: precisa Bearer token.
    """
    r = os_post_with_retry("/download", {"file_id": file_id})
    if not r:
        return None
    try:
        data = r.json()
        return data.get("link")
    except Exception:
        logger.error(f"OpenSubtitles /download: resposta inválida: {r.text[:300]}")
        return None

def os_search_best_download_link(imdb_id: str, lang: str, season: Optional[int], episode: Optional[int]) -> Optional[str]:
    """
    Busca a mais baixada no idioma e retorna link de download.
    lang: "pt-br" ou "en" etc.
    """
    try:
        clean_id = int(imdb_id.replace("tt", ""))
    except Exception:
        return None

    params = {
        "imdb_id": clean_id,
        "languages": lang,
        "order_by": "download_count",
        "order_direction": "desc",
    }
    if season is not None and episode is not None:
        params.update({"season_number": season, "episode_number": episode})

    r = os_get_with_retry("/subtitles", params=params)
    if not r:
        return None

    try:
        data = r.json()
        if data.get("total_count", 0) <= 0:
            return None

        first = data["data"][0]
        f0 = first["attributes"]["files"][0]
        file_id = int(f0["file_id"])
        return os_get_download_link(file_id)

    except Exception:
        logger.error(f"OpenSubtitles /subtitles: JSON inesperado: {r.text[:400]}")
        return None

# ============================================================
# Gemini translation (REST)
# ============================================================

def gemini_translate_lines_rest(lines: List[str], target_lang: str = "pt-BR") -> List[str]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não definido")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = (
        f"Traduza as linhas abaixo para {target_lang}.\n"
        "Regras obrigatórias:\n"
        "1) Mantenha EXATAMENTE a mesma quantidade de linhas.\n"
        "2) Traduza cada linha separadamente, na mesma ordem.\n"
        "3) Não adicione comentários, nem numeração, nem aspas.\n"
        "4) Preserve tags tipo <i>, <b> e marcadores como {...}.\n"
        "5) Preserve quebras de linha: 1 linha de entrada = 1 linha de saída.\n\n"
        "LINHAS:\n" + "\n".join(lines)
    )

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    r = requests.post(url, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()

    text = ""
    try:
        text = data["candidates"][0]["content"]["parts"][0].get("text", "")
    except Exception:
        # pode vir bloqueado/sem candidato
        raise RuntimeError(f"Resposta Gemini inesperada: {json.dumps(data)[:500]}")

    out_lines = (text or "").strip().splitlines()

    # Ajuste defensivo
    if len(out_lines) != len(lines):
        logger.warning(f"Gemini retornou {len(out_lines)} linhas, esperado {len(lines)}. Ajustando.")
        if len(out_lines) < len(lines):
            out_lines += [""] * (len(lines) - len(out_lines))
        else:
            out_lines = out_lines[: len(lines)]

    return out_lines

# ============================================================
# SRT parse/translate (preserva índice/tempo)
# ============================================================

def split_srt_blocks(srt_text: str) -> List[Dict]:
    blocks: List[Dict] = []
    raw_blocks = re.split(r"\n\s*\n", srt_text.strip(), flags=re.MULTILINE)
    for b in raw_blocks:
        lines = [l.rstrip("\r") for l in b.splitlines()]
        lines = [l for l in lines if l.strip() != ""]
        if len(lines) < 2:
            continue
        idx = lines[0].strip()
        t = lines[1].strip()
        txt = lines[2:] if len(lines) > 2 else []
        blocks.append({"idx": idx, "time": t, "lines": txt})
    return blocks

def rebuild_srt(blocks: List[Dict]) -> str:
    out: List[str] = []
    for b in blocks:
        out.append(str(b["idx"]))
        out.append(str(b["time"]))
        out.extend(b["lines"])
        out.append("")
    return "\n".join(out).strip() + "\n"

def translate_srt_via_gemini(srt_text: str) -> str:
    blocks = split_srt_blocks(srt_text)

    positions: List[Tuple[int, int]] = []
    all_lines: List[str] = []
    for bi, b in enumerate(blocks):
        for li, line in enumerate(b["lines"]):
            positions.append((bi, li))
            all_lines.append(line)

    if not all_lines:
        return srt_text

    # Batch conservador
    BATCH = 50
    translated: List[str] = [""] * len(all_lines)

    for start in range(0, len(all_lines), BATCH):
        chunk = all_lines[start : start + BATCH]
        out_chunk = gemini_translate_lines_rest(chunk, target_lang="pt-BR")
        translated[start : start + BATCH] = out_chunk

    for i, (bi, li) in enumerate(positions):
        blocks[bi]["lines"][li] = translated[i]

    return rebuild_srt(blocks)

# ============================================================
# Core job
# ============================================================

def build_subtitle(cache_key: str, imdb_id: str, season: Optional[int], episode: Optional[int]) -> None:
    final_path = os.path.join(CACHE_DIR, f"{cache_key}.srt")
    lock = get_lock(cache_key)

    with lock:
        if os.path.exists(final_path):
            return

        logger.info(f"Gerando legenda para {cache_key}...")

        tmp_files: List[str] = []

        # 1) PT-BR direto
        url_pt = os_search_best_download_link(imdb_id, "pt-br", season, episode)
        if url_pt:
            tmp_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
            tmp_files.append(tmp_pt)

            if download_file(url_pt, tmp_pt):
                shutil.copy(tmp_pt, final_path)
                cleanup_temp(tmp_files)
                logger.info(f"OK PT-BR cache: {final_path}")
                return

        # 2) fallback EN -> traduz
        url_en = os_search_best_download_link(imdb_id, "en", season, episode)
        if not url_en:
            cleanup_temp(tmp_files)
            logger.warning("Sem PT-BR e sem EN (ou OpenSubtitles bloqueou).")
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

# ============================================================
# Routes
# ============================================================

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

    # dispara processamento em background
    threading.Thread(
        target=build_subtitle,
        args=(cache_key, imdb_id, season, episode),
        daemon=True
    ).start()

    host = request.host_url.rstrip("/")
    return jsonify({
        "subtitles": [
            {
                "id": f"simple_{cache_key}",
                "url": f"{host}/static_subs/{cache_key}.srt",
                "lang": "pob",
                "format": "srt"
            }
        ]
    })

@app.route("/static_subs/<filename>")
def serve_subs(filename):
    file_path = os.path.join(CACHE_DIR, filename)

    # aguarda até 25s o job terminar
    for _ in range(25):
        if os.path.exists(file_path):
            response = make_response(send_from_directory(CACHE_DIR, filename))
            response.headers["Cache-Control"] = "public, max-age=31536000"
            return response
        time.sleep(1)

    response = make_response(generate_loading_srt("Gerando legenda... (PT-BR ou tradução EN->PT)"))
    response.headers["Content-Type"] = "application/x-subrip"
    return response

# ============================================================
# Local run
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7000))
    app.run(host="0.0.0.0", port=port)
