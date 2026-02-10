import os
import logging
import threading
import time
import requests
import json
import subprocess
import shutil
import google.generativeai as genai
import re
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

# --- Configurações ---

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SubPalas")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
TEMP_DIR = os.path.join(os.getcwd(), "temp_processing")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Chaves de API
OS_API_KEY = os.getenv("OS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") 
USER_AGENT = os.getenv("USER_AGENT", "SubPalas v2.1")

# Configura Gemini se houver chave
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "org.subpalas.lite",
    "version": "2.1.0",
    "name": "SubPalas (Lite)",
    "description": "Legendas PT-BR Otimizadas. Sincronia única (Fast) ou Tradução IA.",
    "types": ["movie", "series"],
    "resources": ["subtitles"],
    "idPrefixes": ["tt"]
}

# --- Utilitários ---

def get_file_hash(imdb_id, season=None, episode=None):
    base = f"{imdb_id}"
    if season and episode: base += f"_S{season}E{episode}"
    return base

def cleanup_temp(files):
    for f in files:
        try:
            if f and os.path.exists(f): os.remove(f)
        except: pass

def generate_loading_srt(msg="Carregando..."):
    return (
        "1\n00:00:00,000 --> 00:00:10,000\n"
        f"SubPalas: {msg}\nAguarde o processamento...\n\n"
        "2\n00:00:10,500 --> 00:00:20,000\n"
        "Se demorar, volte e selecione novamente.\n"
    )

# --- Network Logic ---

def download_file(url, dest_path):
    if not url: return False
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        with requests.get(url, stream=True, timeout=20, headers=headers) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        if os.path.getsize(dest_path) < 100: return False
        return True
    except Exception as e:
        logger.error(f"Download Error: {e}")
        return False

def get_download_link(file_id, headers):
    try:
        res = requests.post("https://api.opensubtitles.com/api/v1/download", 
                            headers=headers, json={"file_id": file_id}, timeout=10)
        return res.json().get('link')
    except: return None

# --- Busca OpenSubtitles ---

def search_subtitles(imdb_id, lang, season=None, episode=None):
    if not OS_API_KEY: return None
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try:
        clean_id = int(imdb_id.replace("tt", ""))
        params = {"imdb_id": clean_id, "languages": lang, "order_by": "download_count", "order_direction": "desc"}
        if season: params.update({"season_number": season, "episode_number": episode})
        
        res = requests.get("https://api.opensubtitles.com/api/v1/subtitles", headers=headers, params=params, timeout=10)
        data = res.json()
        
        if data.get('total_count', 0) > 0:
            return data['data'] # Retorna lista bruta
    except Exception as e:
        logger.error(f"Erro busca {lang}: {e}")
    return None

# --- Gemini Translation Logic ---

def translate_batch_gemini(texts):
    if not GEMINI_API_KEY: return None
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = (
        "Translate these subtitles to Brazilian Portuguese (PT-BR). "
        "Keep it natural, concise, and slang-appropriate. "
        "Return ONLY a JSON array of strings. No markdown.\n"
        f"Input: {json.dumps(texts)}"
    )
    try:
        response = model.generate_content(prompt)
        text_resp = response.text.replace('```json', '').replace('```', '').strip()
        translated = json.loads(text_resp)
        if isinstance(translated, list) and len(translated) == len(texts):
            return translated
    except: pass
    return None

def run_translation_fallback(imdb_id, season, episode, cache_key):
    """Lógica de Fallback: Baixa EN -> Traduz -> Salva"""
    logger.info("FALLBACK: Iniciando tradução via IA...")
    
    results_en = search_subtitles(imdb_id, "en", season, episode)
    if not results_en:
        logger.error("FALLBACK: Nem legenda em inglês foi encontrada.")
        return

    best_file = results_en[0]['attributes']['files'][0]
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    url_en = get_download_link(best_file['file_id'], headers)
    
    try:
        r = requests.get(url_en, headers={"User-Agent": USER_AGENT})
        content = r.text.replace('\r\n', '\n')
        
        blocks = re.split(r'\n\n+', content)
        subs = []
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                subs.append({'head': "\n".join(lines[:2]), 'text': " ".join(lines[2:])})

        final_content = [f"0\n00:00:01,000 --> 00:00:05,000\n[SubPalas: Tradução IA (Fallback)]\n"]
        
        batch_size = 20
        for i in range(0, len(subs), batch_size):
            chunk = subs[i:i+batch_size]
            texts = [s['text'] for s in chunk]
            translated = translate_batch_gemini(texts)
            target_texts = translated if translated else texts
            
            for idx, txt in enumerate(target_texts):
                final_content.append(f"{chunk[idx]['head']}\n{txt}\n")
            time.sleep(1)

        final_path = os.path.join(CACHE_DIR, f"{cache_key}_synced.srt")
        with open(final_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(final_content))
            
        logger.info("FALLBACK: Tradução concluída.")
        
    except Exception as e:
        logger.error(f"FALLBACK Error: {e}")

# --- Core Logic Principal (Single Sync) ---

def run_process(imdb_id, season, episode, cache_key):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_synced.srt")
    
    # Se já existe e é válido, não faz nada
    if os.path.exists(final_path) and os.path.getsize(final_path) > 100: 
        return

    logger.info(f"--- PROCESSANDO (LITE): {cache_key} ---")
    
    # 1. Busca PT-BR
    ptbr_results = search_subtitles(imdb_id, "pt-br", season, episode)
    
    if not ptbr_results:
        logger.warning("PT-BR não encontrada. Ativando Fallback IA.")
        run_translation_fallback(imdb_id, season, episode, cache_key)
        return

    logger.info("PT-BR encontrada. Buscando melhor referência EN...")
    
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    url_pt = get_download_link(ptbr_results[0]['attributes']['files'][0]['file_id'], headers)
    
    path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
    if not download_file(url_pt, path_pt): return

    # 2. Busca Ref EN (Apenas 1 Melhor Opção)
    en_results = search_subtitles(imdb_id, "en", season, episode)
    if not en_results:
        shutil.copy(path_pt, final_path) # Sem ref, usa original
        return

    # Seletor Inteligente de Única Referência (Prioridade WEB > HDTV > Popular)
    best_ref_url = None
    ref_type = "POPULAR"
    
    # Tenta achar WEB-DL primeiro (Melhor para Stremio)
    for item in en_results:
        fname = item['attributes']['files'][0]['file_name'].lower()
        if 'web' in fname or 'amzn' in fname or 'nf' in fname:
            best_ref_url = get_download_link(item['attributes']['files'][0]['file_id'], headers)
            ref_type = "WEB-DL"
            break
    
    # Se não achou, tenta HDTV
    if not best_ref_url:
        for item in en_results:
            fname = item['attributes']['files'][0]['file_name'].lower()
            if 'hdtv' in fname:
                best_ref_url = get_download_link(item['attributes']['files'][0]['file_id'], headers)
                ref_type = "HDTV"
                break
    
    # Se nada, pega a primeira (Mais popular)
    if not best_ref_url:
        best_ref_url = get_download_link(en_results[0]['attributes']['files'][0]['file_id'], headers)

    if best_ref_url:
        path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref.srt")
        if download_file(best_ref_url, path_ref):
            cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", final_path, "--encoding", "utf-8"]
            logger.info(f"Sincronizando com {ref_type}...")
            try:
                subprocess.run(cmd, capture_output=True, check=True, timeout=120)
                logger.info("Sincronia concluída com sucesso.")
            except Exception as e:
                logger.error(f"Erro ffsubsync: {e}")
                shutil.copy(path_pt, final_path) # Fallback para original em erro
        else:
            shutil.copy(path_pt, final_path)
    else:
        shutil.copy(path_pt, final_path)

    cleanup_temp([path_pt, os.path.join(TEMP_DIR, f"{cache_key}_ref.srt")])

# --- Rotas ---

@app.route('/')
def index(): return "SubPalas Lite v2.1 Running"

@app.route('/manifest.json')
def manifest(): return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id, season, episode = parts[0], int(parts[1]) if len(parts)>1 else None, int(parts[2]) if len(parts)>2 else None
    cache_key = get_file_hash(imdb_id, season, episode)
    
    # Dispara processamento leve (1 thread apenas)
    threading.Thread(target=run_process, args=(imdb_id, season, episode, cache_key)).start()
    
    host = request.host_url.rstrip('/')
    
    # Retorna apenas UMA opção unificada
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

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)
    
    # Espera até 45s (suficiente para 1 sync)
    for _ in range(45):
        if os.path.exists(path) and os.path.getsize(path) > 100:
            resp = make_response(send_from_directory(CACHE_DIR, filename))
            resp.headers['Cache-Control'] = 'public, max-age=31536000'
            return resp
        time.sleep(1)
        
    return generate_loading_srt("Sincronizando..."), 200, {'Content-Type': 'application/x-subrip'}

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
