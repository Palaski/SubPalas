import os
import logging
import threading
import time
import requests
import re
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
from deep_translator import GoogleTranslator

# --- Configurações ---

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoTranslateAddon")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

OS_API_KEY = os.getenv("OS_API_KEY", "")
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

MANIFEST = {
    "id": "community.autotranslate.ptbr",
    "version": "0.1.1",
    "name": "AutoTranslate PT-BR (Hash Match)",
    "description": "Traduz a legenda EN exata do seu arquivo (via Hash) para PT-BR. Sincronia perfeita.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Utilitários ---

def get_file_hash(imdb_id, season=None, episode=None, video_hash=None):
    base = f"{imdb_id}"
    if season and episode:
        base += f"_S{season}E{episode}"
    if video_hash:
        base += f"_{video_hash}"
    return base

def generate_loading_srt(status="Traduzindo..."):
    return (
        "1\n00:00:00,000 --> 00:00:10,000\n"
        f"{status}\nAguarde alguns segundos...\n\n"
        "2\n00:00:10,500 --> 00:00:20,000\n"
        "Buscando legenda compatível com seu arquivo de vídeo.\n"
    )

# --- OpenSubtitles ---

def get_download_link(file_id, headers):
    try:
        res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                            headers=headers, json={"file_id": file_id})
        return res.json().get('link')
    except:
        return None

def search_english_sub(imdb_id, season=None, episode=None, video_hash=None):
    """
    Busca inteligente:
    1. Tenta buscar pelo HASH exato do vídeo (Perfeição).
    2. Se falhar, busca por IMDB ID + Mais Baixada (Alta probabilidade).
    """
    if not OS_API_KEY: return None, "No API Key"
    
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try: clean_id = int(imdb_id.replace("tt", ""))
    except: return None, "Bad ID"

    # Tentativa 1: Busca por HASH (Se disponível)
    if video_hash:
        logger.info(f"Buscando por Hash: {video_hash}")
        params_hash = {
            "moviehash": video_hash,
            "languages": "en",
            "order_by": "download_count", 
            "order_direction": "desc"
        }
        try:
            res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params_hash, timeout=10)
            data = res.json()
            if data.get('total_count', 0) > 0:
                logger.info("HASH MATCH! Encontramos a legenda exata para este arquivo.")
                fid = data['data'][0]['attributes']['files'][0]['file_id']
                return get_download_link(fid, headers), "Hash Match (Perfect)"
        except Exception as e:
            logger.error(f"Erro busca Hash: {e}")

    # Tentativa 2: Busca Genérica por IMDB (Fallback)
    logger.info("Hash falhou ou inexistente. Usando busca genérica IMDB.")
    params = {"imdb_id": clean_id, "languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if season: params.update({"season_number": season, "episode_number": episode})

    try:
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get('total_count', 0) > 0:
            fid = data['data'][0]['attributes']['files'][0]['file_id']
            return get_download_link(fid, headers), "IMDB Match (Best Guess)"
    except Exception as e:
        logger.error(f"Erro busca IMDB: {e}")
    
    return None, "Not Found"

# --- Motor de Tradução ---

def translate_worker(imdb_id, season, episode, cache_key, video_hash):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_translated.srt")
    if os.path.exists(final_path): return

    logger.info(f"Iniciando workflow para {cache_key}...")
    
    # 1. Baixar Legenda EN (Com logica de Hash)
    url_en, method = search_english_sub(imdb_id, season, episode, video_hash)
    
    if not url_en:
        logger.error("Nenhuma legenda EN encontrada para traduzir.")
        return

    logger.info(f"Legenda fonte encontrada via: {method}")

    try:
        r = requests.get(url_en)
        r.raise_for_status()
        # Tenta decodificar, fallback para latin1 se utf-8 falhar (comum em subs antigas)
        r.encoding = r.apparent_encoding
        original_content = r.text.replace('\r\n', '\n')
    except:
        return

    # 2. Traduzir
    blocks = re.split(r'\n\n+', original_content)
    translator = GoogleTranslator(source='en', target='pt')
    
    texts_to_translate = []
    headers = []
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 3:
            headers.append((lines[0], lines[1]))
            text_part = " ".join(lines[2:])
            texts_to_translate.append(text_part)

    batch_size = 50
    translated_texts = []
    
    for i in range(0, len(texts_to_translate), batch_size):
        batch = texts_to_translate[i:i+batch_size]
        try:
            for text in batch:
                if text.strip():
                    translated_texts.append(translator.translate(text))
                else:
                    translated_texts.append("")
        except Exception as e:
            logger.error(f"Erro tradução: {e}")
            # Em caso de erro, preenche com vazio para não quebrar a sincronia dos próximos
            translated_texts.extend(["[Erro Tradução]"] * len(batch))

    # 3. Salvar
    with open(final_path, 'w', encoding='utf-8') as f:
        # Adiciona cabecalho indicando o metodo usado
        f.write(f"0\n00:00:00,000 --> 00:00:05,000\n[AutoTranslate via {method}]\n\n")
        for i, (num, time_code) in enumerate(headers):
            if i < len(translated_texts):
                f.write(f"{i+1}\n{time_code}\n{translated_texts[i]}\n\n")
    
    logger.info(f"Tradução concluída: {cache_key}")

# --- Rotas ---

@app.route('/')
def index():
    return "AutoTranslate Hash-Enhanced Running"

@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 else None
    episode = int(parts[2]) if len(parts) > 2 else None
    
    # Extração do Video Hash da URL do Stremio
    # O formato costuma ser: videoHash=1234abc ou apenas o hash solto dependendo do player
    video_hash = None
    if "videoHash=" in extra:
        try:
            video_hash = extra.split("videoHash=")[1].split("&")[0]
        except:
            pass
    
    cache_key = get_file_hash(imdb_id, season, episode, video_hash)
    
    threading.Thread(target=translate_worker, args=(imdb_id, season, episode, cache_key, video_hash)).start()
    
    host = request.host_url.rstrip('/')
    
    return jsonify({
        "subtitles": [{
            "id": f"autotrans_{cache_key}",
            "url": f"{host}/static_subs/{cache_key}_translated.srt",
            "lang": "pob",
            "format": "srt"
        }]
    })

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    file_path = os.path.join(CACHE_DIR, filename)
    
    max_retries = 40 
    for _ in range(max_retries):
        if os.path.exists(file_path):
            response = make_response(send_from_directory(CACHE_DIR, filename))
            response.headers['Cache-Control'] = 'public, max-age=31536000'
            return response
        time.sleep(1)
    
    response = make_response(generate_loading_srt("Buscando Hash & Traduzindo..."))
    response.headers['Content-Type'] = 'application/x-subrip'
    response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-cache'
    return response

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
