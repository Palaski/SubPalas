import os
import logging
import threading
import time
import requests
import re
import subprocess  # <--- ADICIONADO: Faltava esta importação
from flask import Flask, jsonify, request, send_from_directory, Response, make_response
from flask_cors import CORS

# --- Configurações ---

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoSyncAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

MANIFEST = {
    "id": "community.autosync.ptbr",
    "version": "0.0.5",  # Incrementei a versão para forçar update no Stremio se necessário
    "name": "AutoSync PT-BR (Instant)",
    "description": "Legendas PT-BR sincronizadas. Selecione, aguarde o aviso de 'Sincronizando' sumir e aproveite.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Utilitários ---

def get_file_hash(imdb_id, season=None, episode=None):
    base = f"{imdb_id}"
    if season and episode:
        base += f"_S{season}E{episode}"
    return base

def cleanup_temp(files):
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass

def generate_loading_srt():
    """Gera um SRT válido avisando o usuário para esperar."""
    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:10,000\n"
        "Sincronizando legenda... Aguarde...\n\n"
        "2\n"
        "00:00:10,500 --> 00:00:20,000\n"
        "Se esta mensagem persistir,\nselecione 'None' e depois 'AutoSync' novamente.\n"
    )
    return srt_content

# --- OpenSubtitles e Download ---

def get_download_link(file_id, headers):
    try:
        res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                            headers=headers, json={"file_id": file_id})
        return res.json().get('link')
    except:
        return None

def search_references_opensubtitles(imdb_id, season=None, episode=None):
    if not OS_API_KEY: return []
    
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try: clean_id = int(imdb_id.replace("tt", ""))
    except: return []

    params = {"imdb_id": clean_id, "languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if season: params.update({"season_number": season, "episode_number": episode})

    references = []
    try:
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        
        if data.get('total_count', 0) > 0:
            results = data['data']
            found_web, found_other = False, False
            
            # Prioriza encontrar 1 WEB e 1 HDTV
            for item in results:
                if len(references) >= 2: break
                f = item['attributes']['files'][0]
                fname = f['file_name'].lower()
                is_web = any(x in fname for x in ['web', 'amzn', 'nf', 'hulu'])
                
                link = get_download_link(f['file_id'], headers)
                if not link: continue

                if is_web and not found_web:
                    references.append({'url': link, 'type': 'WEB'})
                    found_web = True
                elif not is_web and not found_other:
                    references.append({'url': link, 'type': 'HDTV'})
                    found_other = True
            
            # Fallback se não achou tipos específicos
            if not references and len(results) > 0:
                 link = get_download_link(results[0]['attributes']['files'][0]['file_id'], headers)
                 if link: references.append({'url': link, 'type': 'Default'})
                 
    except Exception as e:
        logger.error(f"Erro busca EN: {e}")
    
    return references

def search_best_ptbr(imdb_id, season=None, episode=None):
    if not OS_API_KEY: return None
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try:
        params = {"imdb_id": int(imdb_id.replace("tt", "")), "languages": "pt-br", "order_by": "download_count", "order_direction": "desc"}
        if season: params.update({"season_number": season, "episode_number": episode})
        
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params)
        data = res.json()
        if data.get('total_count', 0) > 0:
            return get_download_link(data['data'][0]['attributes']['files'][0]['file_id'], headers)
    except:
        pass
    return None

def download_file(url, dest_path):
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return True
    except:
        return False

# --- Core Logic ---

def run_sync_thread(imdb_id, season, episode, cache_key):
    """Thread que baixa e processa as legendas V1 e V2"""
    v1_path = os.path.join(CACHE_DIR, f"{cache_key}_v1.srt")
    
    # Se V1 já existe, assumimos que já foi processado (evita trabalho duplo)
    if os.path.exists(v1_path):
        return

    logger.info(f"Processando {cache_key}...")
    
    # 1. Baixar PT-BR
    url_pt = search_best_ptbr(imdb_id, season, episode)
    if not url_pt:
        logger.error("PT-BR nao achado")
        return 
        
    path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
    if not download_file(url_pt, path_pt): return

    # 2. Baixar Referencias EN
    refs = search_references_opensubtitles(imdb_id, season, episode)
    
    files_clean = [path_pt]

    # 3. Sincronizar
    # Se não achou nenhuma ref, copiamos a PT original como V1 só pra não falhar
    if not refs:
        import shutil
        shutil.copy(path_pt, v1_path)
    
    for i, ref in enumerate(refs):
        # v1 é a principal, v2 é a alternativa
        version_label = f"v{i+1}" 
        final_path = os.path.join(CACHE_DIR, f"{cache_key}_{version_label}.srt")
        path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref_{i}.srt")
        files_clean.append(path_ref)

        if download_file(ref['url'], path_ref):
            cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", final_path, "--encoding", "utf-8"]
            logger.info(f"Syncing {version_label}...")
            try:
                subprocess.run(cmd, capture_output=True, check=True)
            except Exception as e:
                logger.error(f"Erro ao rodar ffsubsync: {e}")
    
    cleanup_temp(files_clean)
    logger.info(f"Concluido {cache_key}")

# --- Rotas ---

@app.route('/')
def index():
    return "AutoSync Instant is Running"

@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None
    
    cache_key = get_file_hash(imdb_id, season, episode)
    
    # Dispara a thread SEMPRE que solicitado (se ja existir, a thread aborta cedo)
    threading.Thread(target=run_sync_thread, args=(imdb_id, season, episode, cache_key)).start()

    host = request.host_url.rstrip('/')
    
    return jsonify({
        "subtitles": [
            {
                "id": f"as_v1_{cache_key}",
                "url": f"{host}/static_subs/{cache_key}_v1.srt",
                "lang": "pob",
                "format": "srt"
            },
            {
                "id": f"as_v2_{cache_key}",
                "url": f"{host}/static_subs/{cache_key}_v2.srt",
                "lang": "pob",
                "format": "srt"
            }
        ]
    })

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    """
    Tenta servir o arquivo. Se não existir, espera até 15s.
    Se ainda não existir, retorna legenda de 'Loading'.
    """
    file_path = os.path.join(CACHE_DIR, filename)
    
    # Loop de espera (Polling)
    max_retries = 15 # 15 segundos
    for _ in range(max_retries):
        if os.path.exists(file_path):
            # Arquivo pronto!
            response = make_response(send_from_directory(CACHE_DIR, filename))
            response.headers['Cache-Control'] = 'public, max-age=31536000'
            return response
        time.sleep(1)
    
    # Timeout: Retorna legenda de Loading
    logger.info(f"Timeout servindo {filename}, enviando loading...")
    response = make_response(generate_loading_srt())
    response.headers['Content-Type'] = 'application/x-subrip'
    response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
