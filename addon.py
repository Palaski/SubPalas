import os
import logging
import threading
import json
import shutil
import subprocess
import requests
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from datetime import datetime, timedelta

# --- Configurações Iniciais ---

app = Flask(__name__)

# HABILITA O CORS (Essencial para funcionar no Stremio Web/TV)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoSyncAddon")

# Diretórios de trabalho
CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configurações da API OpenSubtitles
# Tenta pegar do ambiente, se não tiver, usa uma string vazia (vai dar erro se não configurar no Render)
OS_API_KEY = os.getenv("OS_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

# Manifesto do Addon
MANIFEST = {
    "id": "community.autosync.ptbr",
    "version": "0.0.2",
    "name": "AutoSync PT-BR (Pivot)",
    "description": "Sincroniza legendas PT-BR usando EN como referência (Anti-Drift).",
    "types": ["movie", "series"],
    "catalogs": [],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Funções Utilitárias ---

def get_file_hash(imdb_id, season=None, episode=None):
    """Gera um ID único para o arquivo baseada no filme/episódio."""
    base = f"{imdb_id}"
    if season and episode:
        base += f"_S{season}E{episode}"
    return base

def cleanup_temp(files):
    """Remove arquivos temporários para não lotar o disco."""
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except Exception as e:
                logger.error(f"Erro ao limpar temp {f}: {e}")

# --- Integração OpenSubtitles ---

def search_opensubtitles(imdb_id, language, season=None, episode=None):
    """
    Busca a melhor legenda (mais baixada) na API do OpenSubtitles.com
    """
    if not OS_API_KEY:
        logger.error("OS_API_KEY não configurada!")
        return None

    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    
    # Limpa o 'tt' do ID se necessário
    clean_id = imdb_id.replace("tt", "")
    try:
        imdb_int = int(clean_id)
    except:
        logger.error(f"ID inválido: {imdb_id}")
        return None

    params = {
        "imdb_id": imdb_int,
        "languages": language,
        "order_by": "download_count", 
        "order_direction": "desc"
    }
    
    if season and episode:
        params["season_number"] = season
        params["episode_number"] = episode

    try:
        # 1. Buscar metadados
        url = "https://api.opensubtitles.com/api/v1/subtitles"
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('total_count', 0) > 0:
            # Pega o ID do arquivo da primeira opção (a mais popular)
            first_result = data['data'][0]
            file_id = first_result['attributes']['files'][0]['file_id']
            
            # 2. Pedir link de download
            dl_payload = {"file_id": file_id}
            dl_res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                                 headers=headers, json=dl_payload)
            dl_data = dl_res.json()
            return dl_data.get('link')
            
    except Exception as e:
        logger.error(f"Erro na busca OpenSubtitles ({language}): {e}")
    
    return None

def download_file(url, dest_path):
    """Baixa o arquivo para o disco."""
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Erro download {url}: {e}")
        return False

# --- Lógica de Sincronização (Background) ---

def run_synchronization(imdb_id, season=None, episode=None):
    """
    Processo principal: Baixa EN, Baixa PT-BR, Sincroniza.
    """
    cache_key = get_file_hash(imdb_id, season, episode)
    final_filename = f"{cache_key}_synced.srt"
    final_path = os.path.join(CACHE_DIR, final_filename)
    
    # Se já existe, para.
    if os.path.exists(final_path):
        return

    logger.info(f"Iniciando sincronização para {cache_key}...")

    ref_path = os.path.join(TEMP_DIR, f"{cache_key}_ref_en.srt")
    target_path = os.path.join(TEMP_DIR, f"{cache_key}_target_pt.srt")
    
    try:
        # 1. Buscar Reference (EN)
        url_ref = search_opensubtitles(imdb_id, "en", season, episode)
        if not url_ref:
            logger.warning("Reference EN não encontrada. Abortando.")
            return

        # 2. Buscar Target (PT-BR)
        url_target = search_opensubtitles(imdb_id, "pt-br", season, episode)
        if not url_target:
            logger.warning("Target PT-BR não encontrada. Abortando.")
            return

        # 3. Download dos arquivos
        if not download_file(url_ref, ref_path): return
        if not download_file(url_target, target_path): return

        # 4. Executar FFsubsync
        # Comando: ffsubsync reference.srt -i target.srt -o output.srt
        cmd = [
            "ffsubsync", 
            ref_path, 
            "-i", target_path, 
            "-o", final_path,
            "--encoding", "utf-8"
        ]
        
        logger.info(f"Rodando: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"Sincronia concluída com sucesso: {final_filename}")
        else:
            logger.error(f"Erro no ffsubsync: {result.stderr}")

    except Exception as e:
        logger.error(f"Exceção no worker: {e}")
    finally:
        # Limpeza
        cleanup_temp([ref_path, target_path])

# --- Rotas da API ---

@app.route('/')
def index():
    return "AutoSync PT-BR Addon is Running correctly with CORS!"

@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    """
    Rota chamada pelo Stremio para pedir legendas.
    """
    # Parse do ID (Ex: tt12345 ou tt12345:1:1)
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None

    cache_key = get_file_hash(imdb_id, season, episode)
    filename = f"{cache_key}_synced.srt"
    file_path = os.path.join(CACHE_DIR, filename)

    # Detecta a URL do seu servidor automaticamente
    host_url = request.host_url.rstrip('/')
    
    # 1. Se a legenda já existe no cache, retorna ela
    if os.path.exists(file_path):
        logger.info(f"HIT Cache: Entregando {filename}")
        return jsonify({
            "subtitles": [{
                "id": f"autosync_{cache_key}",
                "url": f"{host_url}/static_subs/{filename}",
                "lang": "pob",
                "format": "srt"
            }]
        })
    
    # 2. Se não existe, dispara o processo em background e retorna vazio
    else:
        logger.info(f"MISS Cache: Iniciando thread para {cache_key}")
        # Inicia thread sem travar a requisição
        thread = threading.Thread(target=run_synchronization, args=(imdb_id, season, episode))
        thread.start()
        
        # Retorna lista vazia para o Stremio não ficar carregando infinitamente
        return jsonify({"subtitles": []})

@app.route('/static_subs/<path:filename>')
def serve_subs(filename):
    """Rota para baixar o arquivo .srt gerado"""
    return send_from_directory(CACHE_DIR, filename)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
