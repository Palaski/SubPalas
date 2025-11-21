import os
import logging
import threading
import json
import shutil
import subprocess
import requests
import re
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from datetime import datetime, timedelta

# --- Configurações Iniciais ---

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
    "version": "0.0.3",
    "name": "AutoSync PT-BR (Dual Ref)",
    "description": "Sincroniza PT-BR usando múltiplas referências (WEB e HDTV) para corrigir drift de comerciais.",
    "types": ["movie", "series"],
    "catalogs": [],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Funções Utilitárias ---

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
            except Exception as e:
                logger.error(f"Erro ao limpar temp {f}: {e}")

# --- Integração OpenSubtitles ---

def get_download_link(file_id, headers):
    """Obtém o link de download direto dado um file_id."""
    try:
        payload = {"file_id": file_id}
        res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                            headers=headers, json=payload)
        data = res.json()
        return data.get('link')
    except Exception as e:
        logger.error(f"Erro ao pegar link download {file_id}: {e}")
        return None

def search_references_opensubtitles(imdb_id, season=None, episode=None):
    """
    Busca até 2 referências em INGLÊS:
    1. Uma versão WEB-DL (Prioridade)
    2. Uma versão HDTV/BlueRay (Alternativa)
    Retorna uma lista: [{'type': 'WEB', 'url': ...}, {'type': 'HDTV', 'url': ...}]
    """
    if not OS_API_KEY:
        return []

    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    
    clean_id = imdb_id.replace("tt", "")
    try:
        imdb_int = int(clean_id)
    except:
        return []

    params = {
        "imdb_id": imdb_int,
        "languages": "en",
        "order_by": "download_count", 
        "order_direction": "desc"
    }
    
    if season and episode:
        params["season_number"] = season
        params["episode_number"] = episode

    references = []
    
    try:
        url = "https://api.opensubtitles.com/api/v1/subtitles"
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('total_count', 0) > 0:
            results = data['data']
            
            # Estratégia: Encontrar um WEB e um HDTV
            found_web = False
            found_other = False
            
            for item in results:
                # Limite de 2 referências para economizar CPU
                if len(references) >= 2: break
                
                attributes = item['attributes']
                files = attributes.get('files', [])
                if not files: continue
                
                file_obj = files[0]
                filename = file_obj['file_name'].lower()
                file_id = file_obj['file_id']
                
                is_web = any(x in filename for x in ['web', 'amzn', 'nf', 'hulu', 'disney'])
                
                # Se achamos um WEB e ainda não temos um WEB
                if is_web and not found_web:
                    link = get_download_link(file_id, headers)
                    if link:
                        references.append({'type': 'WEB-DL', 'url': link, 'name': filename})
                        found_web = True
                        continue

                # Se achamos um HDTV/Outro e ainda não temos
                if not is_web and not found_other:
                    link = get_download_link(file_id, headers)
                    if link:
                        references.append({'type': 'HDTV/Bluray', 'url': link, 'name': filename})
                        found_other = True
                        continue
            
            # Fallback: Se não achou tipos distintos, pega os top 2
            if not references and len(results) > 0:
                f0 = results[0]['attributes']['files'][0]
                link = get_download_link(f0['file_id'], headers)
                if link: references.append({'type': 'Ref-1', 'url': link})
                
                if len(results) > 1:
                    f1 = results[1]['attributes']['files'][0]
                    link2 = get_download_link(f1['file_id'], headers)
                    if link2: references.append({'type': 'Ref-2', 'url': link2})

    except Exception as e:
        logger.error(f"Erro busca referencias EN: {e}")
    
    return references

def search_best_ptbr(imdb_id, season=None, episode=None):
    """Busca apenas a melhor legenda PT-BR para servir de base."""
    if not OS_API_KEY: return None

    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    
    params = {
        "imdb_id": int(imdb_id.replace("tt", "")),
        "languages": "pt-br",
        "order_by": "download_count", 
        "order_direction": "desc"
    }
    if season and episode:
        params["season_number"] = season
        params["episode_number"] = episode

    try:
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params)
        data = res.json()
        if data.get('total_count', 0) > 0:
            fid = data['data'][0]['attributes']['files'][0]['file_id']
            return get_download_link(fid, headers)
    except:
        return None
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

# --- Lógica de Sincronização ---

def run_synchronization(imdb_id, season=None, episode=None):
    cache_key = get_file_hash(imdb_id, season, episode)
    
    # Verifica se já temos alguma versão syncada
    # Se já existir qualquer arquivo começando com esse cache_key no dir, assumimos que processou
    existing = [f for f in os.listdir(CACHE_DIR) if f.startswith(cache_key)]
    if existing:
        return

    logger.info(f"Iniciando sincronização DUAL para {cache_key}...")

    target_url = search_best_ptbr(imdb_id, season, episode)
    if not target_url:
        logger.warning("Nenhuma legenda PT-BR encontrada.")
        return

    references = search_references_opensubtitles(imdb_id, season, episode)
    if not references:
        logger.warning("Nenhuma referência EN encontrada.")
        return

    # Baixar Target PT-BR
    target_path = os.path.join(TEMP_DIR, f"{cache_key}_target.srt")
    if not download_file(target_url, target_path): return

    files_to_clean = [target_path]

    # Processar cada referência encontrada (Geralmente WEB e HDTV)
    for idx, ref in enumerate(references):
        ref_type = ref['type']
        ref_url = ref['url']
        
        # Nome do arquivo final: id_S01E01_WEB-DL.srt
        safe_type = re.sub(r'[^a-zA-Z0-9]', '', ref_type) # Remove chars ruins
        final_filename = f"{cache_key}_{safe_type}.srt"
        final_path = os.path.join(CACHE_DIR, final_filename)
        
        ref_path = os.path.join(TEMP_DIR, f"{cache_key}_ref_{idx}.srt")
        files_to_clean.append(ref_path)

        if download_file(ref_url, ref_path):
            cmd = [
                "ffsubsync", 
                ref_path, 
                "-i", target_path, 
                "-o", final_path,
                "--encoding", "utf-8"
            ]
            logger.info(f"Sincronizando variante {ref_type}...")
            subprocess.run(cmd, capture_output=True)
    
    cleanup_temp(files_to_clean)
    logger.info(f"Processamento concluído para {cache_key}")

# --- Rotas ---

@app.route('/')
def index():
    return "AutoSync PT-BR (Dual Mode) Running."

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
    host_url = request.host_url.rstrip('/')

    # Buscar arquivos processados no cache que batem com esse ID
    available_subs = []
    if os.path.exists(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            if f.startswith(cache_key) and f.endswith(".srt"):
                # Extrair o tipo do nome do arquivo (ex: tt123_WEB-DL.srt -> WEB-DL)
                # Formato esperado: {cache_key}_{TYPE}.srt
                try:
                    tag = f.replace(cache_key + "_", "").replace(".srt", "")
                except:
                    tag = "Synced"

                available_subs.append({
                    "id": f"autosync_{tag}_{cache_key}",
                    "url": f"{host_url}/static_subs/{f}",
                    "lang": "pob",
                    "format": "srt",
                    "url_expire": f"{host_url}/static_subs/{f}" # Stremio caching trick
                })

    if available_subs:
        # Adicionar rótulos descritivos
        for sub in available_subs:
            # O ID define o texto que aparece na lista? Não, o Stremio não deixa mudar o texto fácil.
            # Mas podemos usar IDs diferentes para tentar agrupar.
            # Infelizmente o Stremio mostra apenas "Portuguese".
            # Vamos tentar hackear o 'lang' ou torcer para o usuário testar as opções.
            pass
            
        return jsonify({"subtitles": available_subs})
    
    else:
        # Cache Miss
        thread = threading.Thread(target=run_synchronization, args=(imdb_id, season, episode))
        thread.start()
        return jsonify({"subtitles": []})

@app.route('/static_subs/<path:filename>')
def serve_subs(filename):
    return send_from_directory(CACHE_DIR, filename)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
