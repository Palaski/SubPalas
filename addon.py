import os
import logging
import threading
import hashlib
import json
import shutil
import subprocess
import requests
from flask import Flask, jsonify, request, send_from_directory, abort
from datetime import datetime, timedelta

# Configuração
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoSyncAddon")

# Configurações de Diretório e Cache
CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configurações da API OpenSubtitles (Necessário obter chave em opensubtitles.com)
OS_API_KEY = os.getenv("OS_API_KEY", "SUA_API_KEY_AQUI")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

MANIFEST = {
    "id": "community.autosync.ptbr",
    "version": "0.0.1",
    "name": "AutoSync PT-BR (Pivot)",
    "description": "Sincroniza legendas PT-BR automaticamente usando EN como referência (Anti-Drift).",
    "types": ["movie", "series"],
    "catalogs": [],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Utilitários ---

def get_file_hash(imdb_id, season=None, episode=None):
    """Cria um identificador único para o cache."""
    base = f"{imdb_id}"
    if season and episode:
        base += f"_S{season}E{episode}"
    return base

def cleanup_temp(files):
    """Remove arquivos temporários."""
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except Exception as e:
                logger.error(f"Erro ao limpar temp {f}: {e}")

# --- Integração OpenSubtitles (Simplificada) ---

def search_opensubtitles(imdb_id, language, season=None, episode=None):
    """
    Busca legendas na API do OpenSubtitles.com.
    Retorna a URL da melhor legenda encontrada.
    """
    headers = {
        "Api-Key": OS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }
    
    # Remove 'tt' do ID se necessário para conversão numérica, mas a API aceita strings
    params = {
        "imdb_id": int(imdb_id.replace("tt", "")),
        "languages": language,
        "order_by": "download_count", # Confiança baseada em popularidade
        "order_direction": "desc"
    }
    
    if season and episode:
        params["season_number"] = season
        params["episode_number"] = episode

    try:
        url = "https://api.opensubtitles.com/api/v1/subtitles"
        response = requests.get(url, headers=headers, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data['total_count'] > 0:
            # Pega o ID do arquivo para pedir o link de download
            file_id = data['data'][0]['attributes']['files'][0]['file_id']
            
            # Request link de download
            dl_payload = {"file_id": file_id}
            dl_res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                                 headers=headers, json=dl_payload)
            dl_data = dl_res.json()
            return dl_data.get('link')
            
    except Exception as e:
        logger.error(f"Erro na busca OpenSubtitles ({language}): {e}")
    
    return None

def download_file(url, dest_path):
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

# --- Core: Sincronização ---

def run_synchronization(imdb_id, season=None, episode=None):
    """
    Função worker que executa o download e o ffsubsync.
    """
    cache_key = get_file_hash(imdb_id, season, episode)
    final_filename = f"{cache_key}_synced.srt"
    final_path = os.path.join(CACHE_DIR, final_filename)
    
    # Se já existe, não faz nada
    if os.path.exists(final_path):
        return

    logger.info(f"Iniciando sincronização para {cache_key}")

    # Caminhos temporários
    ref_path = os.path.join(TEMP_DIR, f"{cache_key}_ref_en.srt")
    target_path = os.path.join(TEMP_DIR, f"{cache_key}_target_pt.srt")
    
    try:
        # 1. Buscar Reference (EN)
        url_ref = search_opensubtitles(imdb_id, "en", season, episode)
        if not url_ref:
            logger.warning("Reference EN não encontrada.")
            return

        # 2. Buscar Target (PT-BR)
        url_target = search_opensubtitles(imdb_id, "pt-br", season, episode)
        if not url_target:
            logger.warning("Target PT-BR não encontrada.")
            return

        # 3. Download
        if not download_file(url_ref, ref_path) or not download_file(url_target, target_path):
            return

        # 4. Executar FFsubsync
        # ffs ref.srt -i target.srt -o output.srt
        # O comando pode variar dependendo de como o ffsubsync foi instalado. 
        # Geralmente é 'ffs' ou 'ffsubsync'.
        cmd = [
            "ffsubsync", 
            ref_path, 
            "-i", target_path, 
            "-o", final_path,
            "--encoding", "utf-8" # Forçar utf-8 para evitar problemas com acentos
        ]
        
        logger.info(f"Executando ffsubsync: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"Sucesso! Sincronizado salvo em: {final_path}")
        else:
            logger.error(f"Falha no ffsubsync: {result.stderr}")

    except Exception as e:
        logger.error(f"Erro crítico no worker: {e}")
    finally:
        cleanup_temp([ref_path, target_path])

# --- Rotas do Addon ---

@app.route('/')
def index():
    return "AutoSync PT-BR Addon is Running"

@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    """
    Rota principal chamada pelo Stremio.
    id: geralmente 'tt12345' ou 'tt12345:1:1' (series)
    """
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None

    # Verifica se já temos no cache
    cache_key = get_file_hash(imdb_id, season, episode)
    filename = f"{cache_key}_synced.srt"
    file_path = os.path.join(CACHE_DIR, filename)

    # URL pública onde o arquivo estará acessível
    # Em produção, isso deve ser o domínio do seu servidor
    host_url = request.host_url.rstrip('/')
    
    if os.path.exists(file_path):
        logger.info(f"Cache Hit: {filename}")
        return jsonify({
            "subtitles": [{
                "id": f"autosync_{cache_key}",
                "url": f"{host_url}/static_subs/{filename}",
                "lang": "pob", # Código ISO 639-2 para Portuguese (Brazil)
                "format": "srt"
            }]
        })
    else:
        # Cache Miss: Disparar processamento em background
        logger.info(f"Cache Miss: Disparando thread para {id}")
        thread = threading.Thread(target=run_synchronization, args=(imdb_id, season, episode))
        thread.start()
        
        # Retorna vazio por enquanto para não travar o Stremio
        # O usuário pode tentar novamente em alguns segundos ou usar outra legenda enquanto isso
        return jsonify({"subtitles": []})

@app.route('/static_subs/<path:filename>')
def serve_subs(filename):
    return send_from_directory(CACHE_DIR, filename)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
