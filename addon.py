import os
import logging
import threading
import time
import requests
import re
import difflib
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
from deep_translator import GoogleTranslator

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoTranslateAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# --- Configs ---
OS_API_KEY = os.getenv("OS_API_KEY", "")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "") # Nova Variável!
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

MANIFEST = {
    "id": "community.autotranslate.ptbr",
    "version": "0.2.0",
    "name": "AutoSync + TorBox Integration",
    "description": "Usa API do TorBox para extrair SRTs nativos ou traduzir a partir da release exata.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Utils ---

def get_file_hash(imdb_id, season=None, episode=None, video_hash=None):
    base = f"{imdb_id}"
    if season and episode: base += f"_S{season}E{episode}"
    if video_hash: base += f"_{video_hash}"
    return base

def similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

# --- TorBox Logic ---

def torbox_get_best_srt(video_hash, season, episode):
    """
    Tenta encontrar um SRT dentro do pacote do TorBox.
    Retorna (conteudo_srt, metodo) ou (None, None).
    """
    if not TORBOX_API_KEY or not video_hash: return None, None
    
    headers = {"Authorization": f"Bearer {TORBOX_API_KEY}"}
    try:
        # 1. Verificar se o hash existe no cache do TorBox
        # A API do TorBox pode variar, usando endpoint de check cached
        res = requests.get(f"https://api.torbox.app/v1/api/torrents/checkcached?hash={video_hash}&format=list&list_files=true", headers=headers, timeout=5)
        data = res.json()
        
        if not data.get('success') or not data.get('data'):
            return None, None

        torrent_data = data['data'] # Pode ser uma lista ou dict dependendo da resposta exata
        if isinstance(torrent_data, list): torrent_data = torrent_data[0]
        
        files = torrent_data.get('files', [])
        torrent_id = torrent_data.get('id') # Se disponivel, ou hash serve
        
        # 2. Filtrar arquivos SRT
        srt_files = [f for f in files if f['name'].lower().endswith('.srt')]
        if not srt_files: return None, None
        
        # 3. Encontrar o SRT correto (se for season pack)
        target_srt = None
        highest_score = 0
        
        # Se for serie, tenta dar match no SxxEyy
        search_str = f"S{season:02d}E{episode:02d}".lower() if season else ""
        
        for f in srt_files:
            fname = f['name'].lower()
            score = 0
            
            # Match de episódio
            if season and search_str in fname: score += 50
            
            # Prioridade de linguagem
            if 'pt-br' in fname or 'por' in fname or 'pob' in fname: score += 30
            elif 'eng' in fname or 'en.' in fname: score += 10 # English é fallback para tradução
            
            if score > highest_score:
                highest_score = score
                target_srt = f
        
        if not target_srt or highest_score < 10: return None, None

        # 4. Baixar o SRT
        # Precisamos pedir o link de download pro TorBox
        # Nota: O endpoint exato de request link pode variar, adaptando para o padrão comum
        link_req = requests.get(f"https://api.torbox.app/v1/api/torrents/requestdl?token={TORBOX_API_KEY}&torrent_id={torrent_id}&file_id={target_srt['id']}", timeout=5)
        link_data = link_req.json()
        
        if link_data.get('success') and link_data.get('data'):
            download_url = link_data['data']
            
            # Baixa conteúdo
            r = requests.get(download_url)
            r.encoding = r.apparent_encoding
            content = r.text.replace('\r\n', '\n')
            
            if 'pt-br' in target_srt['name'].lower() or 'por' in target_srt['name'].lower():
                return content, "TorBox Native (PT-BR)"
            else:
                return content, "TorBox Native (EN -> Translate)"

    except Exception as e:
        logger.error(f"TorBox Error: {e}")
        
    return None, None

# --- OpenSubtitles Logic ---

def get_os_download_link(file_id, headers):
    try:
        res = requests.post("https://api.opensubtitles.com/api/v1/download", headers=headers, json={"file_id": file_id})
        return res.json().get('link')
    except: return None

def search_english_sub(imdb_id, season=None, episode=None, video_hash=None):
    if not OS_API_KEY: return None, "No API Key"
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try: clean_id = int(imdb_id.replace("tt", ""))
    except: return None, "Bad ID"

    # 1. Busca por HASH (Prioridade Máxima)
    if video_hash:
        logger.info(f"--> Buscando HASH OS: {video_hash}")
        try:
            res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params={"moviehash": video_hash, "languages": "en"}, timeout=10)
            data = res.json()
            if data.get('total_count', 0) > 0:
                return get_os_download_link(data['data'][0]['attributes']['files'][0]['file_id'], headers), "OS Hash Match"
        except: pass

    # 2. Busca Genérica IMDB
    logger.info("--> Buscando IMDB Fallback")
    params = {"imdb_id": clean_id, "languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if season: params.update({"season_number": season, "episode_number": episode})
    
    try:
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get('total_count', 0) > 0:
            return get_os_download_link(data['data'][0]['attributes']['files'][0]['file_id'], headers), "OS IMDB Match"
    except: pass
    
    return None, None

# --- Worker Principal ---

def worker(imdb_id, season, episode, cache_key, video_hash):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_translated.srt")
    if os.path.exists(final_path): return

    logger.info(f"WORKER: Iniciando {cache_key} Hash: {video_hash}")
    
    content = None
    method = None
    needs_translation = False

    # FASE 1: Tentar TorBox (Extração Direta)
    if video_hash and TORBOX_API_KEY:
        logger.info("Tentando TorBox Extraction...")
        tb_content, tb_method = torbox_get_best_srt(video_hash, season, episode)
        if tb_content:
            content = tb_content
            method = tb_method
            if "Translate" in method: needs_translation = True
    
    # FASE 2: OpenSubtitles (Se TorBox falhou)
    if not content:
        url_en, method = search_english_sub(imdb_id, season, episode, video_hash)
        if url_en:
            try:
                r = requests.get(url_en)
                r.encoding = r.apparent_encoding
                content = r.text.replace('\r\n', '\n')
                needs_translation = True
            except: pass

    if not content:
        logger.error("WORKER: Nenhuma fonte encontrada.")
        return

    # FASE 3: Tradução (Se necessário)
    final_content = content
    
    if needs_translation:
        logger.info(f"Traduzindo via Google ({method})...")
        try:
            translator = GoogleTranslator(source='en', target='pt')
            blocks = re.split(r'\n\n+', content)
            subs = []
            for block in blocks:
                lines = block.strip().split('\n')
                if len(lines) >= 3:
                    subs.append({'head': "\n".join(lines[:2]), 'text': " ".join(lines[2:])})
            
            translated_subs = []
            batch_size = 30
            
            # Header do arquivo
            translated_subs.append(f"0\n00:00:01,000 --> 00:00:05,000\n[AutoSync: {method}]\n")
            
            for i in range(0, len(subs), batch_size):
                chunk = subs[i:i+batch_size]
                texts = [s['text'] for s in chunk]
                
                try:
                    bulk = " ||| ".join(texts)
                    if len(bulk) < 4500:
                        res = translator.translate(bulk)
                        if res:
                            split = res.split(" ||| ")
                            if len(split) == len(texts):
                                for idx, txt in enumerate(split):
                                    translated_subs.append(f"{chunk[idx]['head']}\n{txt.strip()}\n")
                                continue
                except: pass
                
                # Fallback para original se falhar batch
                for s in chunk:
                    translated_subs.append(f"{s['head']}\n{s['text']}\n")
            
            final_content = "\n".join(translated_subs)
            
        except Exception as e:
            logger.error(f"Erro Tradução: {e}")
            # Em erro, salva o original (EN) mas com nome translated para não falhar request
            final_content = content

    # Salvar
    with open(final_path, 'w', encoding='utf-8') as f:
        f.write(final_content)
                
    logger.info(f"WORKER: Sucesso {cache_key}")

@app.route('/')
def index(): return "AutoSync + TorBox v0.2.0"

@app.route('/manifest.json')
def manifest(): return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id, season, episode = parts[0], int(parts[1]) if len(parts)>1 else None, int(parts[2]) if len(parts)>2 else None
    
    video_hash = None
    if "videoHash=" in extra:
        try: video_hash = extra.split("videoHash=")[1].split("&")[0]
        except: pass
    
    cache_key = get_file_hash(imdb_id, season, episode, video_hash)
    threading.Thread(target=worker, args=(imdb_id, season, episode, cache_key, video_hash)).start()
    
    host = request.host_url.rstrip('/')
    return jsonify({"subtitles": [{"id": f"as_{cache_key}", "url": f"{host}/static_subs/{cache_key}_translated.srt", "lang": "pob", "format": "srt"}]})

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)
    for _ in range(40):
        if os.path.exists(path):
            resp = make_response(send_from_directory(CACHE_DIR, filename))
            resp.headers['Cache-Control'] = 'no-cache'
            return resp
        time.sleep(1)
    return ("1\n00:00:01,000 --> 00:00:10,000\nBuscando no TorBox & Traduzindo...\n\n", 200, {'Content-Type': 'application/x-subrip'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 7000)))
