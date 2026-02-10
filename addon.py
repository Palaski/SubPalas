import os
import logging
import threading
import time
import requests
import re
import subprocess
import shutil
from flask import Flask, jsonify, request, send_from_directory, Response, make_response
from flask_cors import CORS

# --- Configurações ---

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AutoSyncAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

MANIFEST = {
    "id": "community.autosync.ptbr",
    "version": "0.0.9",
    "name": "AutoSync PT-BR (Triple Ref)",
    "description": "3 Versões: WEB (v1), HDTV (v2) e BluRay (v3).",
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
        try:
            if f and os.path.exists(f): os.remove(f)
        except: pass

def generate_loading_srt(variant_name):
    return (
        "1\n00:00:00,000 --> 00:00:10,000\n"
        f"Sincronizando ({variant_name})... Aguarde...\n\n"
        "2\n00:00:10,500 --> 00:00:20,000\n"
        "Se demorar muito, tente novamente em 10s.\n"
    )

# --- Network Logic (Fix 403) ---

def download_file(url, dest_path):
    if not url: return False
    try:
        # CORREÇÃO: Adicionado User-Agent para evitar Erro 403 Forbidden
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*"
        }
        # Timeout para não travar a thread
        with requests.get(url, stream=True, timeout=20, headers=headers) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Erro download {url}: {e}")
        return False

def get_download_link(file_id, headers):
    try:
        res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                            headers=headers, json={"file_id": file_id}, timeout=10)
        data = res.json()
        return data.get('link')
    except Exception as e:
        logger.error(f"Erro API Link {file_id}: {e}")
        return None

def search_references_opensubtitles(imdb_id, season=None, episode=None):
    if not OS_API_KEY:
        logger.error("ERRO: API Key não configurada.")
        return {}
    
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try: clean_id = int(imdb_id.replace("tt", ""))
    except: return {}

    params = {"imdb_id": clean_id, "languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if season: params.update({"season_number": season, "episode_number": episode})

    references = {} 
    try:
        logger.info(f"Buscando EN para {imdb_id}...")
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        
        if data.get('total_count', 0) > 0:
            results = data['data']
            for item in results:
                if len(references) >= 3: break
                f = item['attributes']['files'][0]
                fname = f['file_name'].lower()
                fid = f['file_id']
                
                rtype = None
                if any(x in fname for x in ['web', 'amzn', 'nf', 'hulu']): rtype = 'WEB'
                elif any(x in fname for x in ['bluray', 'bdrip', 'brrip']): rtype = 'BLURAY'
                elif any(x in fname for x in ['hdtv', 'tv', 'pdtv']): rtype = 'HDTV'
                
                if rtype and rtype not in references:
                    link = get_download_link(fid, headers)
                    if link: references[rtype] = link
            
            if not references and len(results) > 0:
                 link = get_download_link(results[0]['attributes']['files'][0]['file_id'], headers)
                 if link: references['DEFAULT'] = link
        else:
            logger.warning("Nenhuma legenda EN encontrada.")

    except Exception as e:
        logger.error(f"Erro busca EN: {e}")
    
    return references

def search_best_ptbr(imdb_id, season=None, episode=None):
    if not OS_API_KEY: return None
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try:
        params = {"imdb_id": int(imdb_id.replace("tt", "")), "languages": "pt-br", "order_by": "download_count", "order_direction": "desc"}
        if season: params.update({"season_number": season, "episode_number": episode})
        
        logger.info(f"Buscando PT-BR para {imdb_id}...")
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get('total_count', 0) > 0:
            return get_download_link(data['data'][0]['attributes']['files'][0]['file_id'], headers)
        else:
            logger.warning("Nenhuma legenda PT-BR encontrada.")
    except Exception as e:
        logger.error(f"Erro busca PT-BR: {e}")
    return None

# --- Core Logic ---

def run_sync_thread(imdb_id, season, episode, cache_key):
    v1_marker = os.path.join(CACHE_DIR, f"{cache_key}_v1.srt")
    if os.path.exists(v1_marker): 
        logger.info("Já processado anteriormente.")
        return

    logger.info(f"--- INICIO DO PROCESSO: {cache_key} ---")
    
    # PASSO 1: Baixar PT-BR
    url_pt = search_best_ptbr(imdb_id, season, episode)
    if not url_pt:
        logger.error("FALHA PASSO 1: Sem URL PT-BR.")
        return 
        
    path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
    if not download_file(url_pt, path_pt): 
        logger.error("FALHA PASSO 1: Erro no download PT-BR.")
        return
    logger.info("PASSO 1 OK: PT-BR baixada.")

    # PASSO 2: Buscar Referencias EN
    refs_dict = search_references_opensubtitles(imdb_id, season, episode)
    files_clean = [path_pt]

    if not refs_dict:
        logger.warning("PASSO 2 ALERTA: Sem ref EN. Usando original.")
        shutil.copy(path_pt, v1_marker)
        return

    logger.info(f"PASSO 2 OK: Encontradas {len(refs_dict)} referências.")

    # PASSO 3: Sincronizar
    priority_order = ['WEB', 'HDTV', 'BLURAY', 'DEFAULT']
    final_refs = []
    for p in priority_order:
        if p in refs_dict: final_refs.append((p, refs_dict[p]))
    
    for i, (rtype, url) in enumerate(final_refs):
        version_label = f"v{i+1}" 
        final_path = os.path.join(CACHE_DIR, f"{cache_key}_{version_label}.srt")
        path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref_{rtype}.srt")
        files_clean.append(path_ref)

        logger.info(f"PASSO 3.{i}: Baixando Ref {rtype}...")
        if download_file(url, path_ref):
            cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", final_path, "--encoding", "utf-8"]
            logger.info(f"PASSO 3.{i}: Rodando ffsubsync...")
            try:
                # Timeout aumentado para 90s para evitar corte prematuro em arquivos grandes
                subprocess.run(cmd, capture_output=True, check=True, timeout=90)
                logger.info(f"PASSO 3.{i}: SUCESSO! Arquivo gerado.")
            except subprocess.TimeoutExpired:
                logger.error(f"PASSO 3.{i} FALHA: ffsubsync demorou demais.")
            except FileNotFoundError:
                logger.critical("ERRO CRÍTICO: ffsubsync não instalado no PATH.")
                break
            except Exception as e:
                logger.error(f"PASSO 3.{i} FALHA: {e}")
        else:
            logger.error(f"PASSO 3.{i} FALHA: Erro baixar referencia.")
    
    cleanup_temp(files_clean)
    logger.info(f"--- FIM DO PROCESSO: {cache_key} ---")

# --- Rotas ---

@app.route('/')
def index(): return "AutoSync Triple Ref v0.0.9 Running"

@app.route('/manifest.json')
def manifest(): return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id, season, episode = parts[0], int(parts[1]) if len(parts)>1 else None, int(parts[2]) if len(parts)>2 else None
    
    cache_key = get_file_hash(imdb_id, season, episode)
    
    threading.Thread(target=run_sync_thread, args=(imdb_id, season, episode, cache_key)).start()

    host = request.host_url.rstrip('/')
    
    return jsonify({
        "subtitles": [
            {"id": f"as_v1_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v1.srt", "lang": "pob", "format": "srt"},
            {"id": f"as_v2_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v2.srt", "lang": "pob", "format": "srt"},
            {"id": f"as_v3_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v3.srt", "lang": "pob", "format": "srt"}
        ]
    })

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    file_path = os.path.join(CACHE_DIR, filename)
    variant = "WEB-DL" if "_v1" in filename else "HDTV" if "_v2" in filename else "BluRay"

    for _ in range(30):
        if os.path.exists(file_path):
            response = make_response(send_from_directory(CACHE_DIR, filename))
            response.headers['Cache-Control'] = 'public, max-age=31536000'
            return response
        time.sleep(1)
    
    response = make_response(generate_loading_srt(variant))
    response.headers['Content-Type'] = 'application/x-subrip'
    response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-cache'
    return response

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
