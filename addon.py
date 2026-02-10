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
USER_AGENT = os.getenv("USER_AGENT", "SubPalas v2.0")

# Configura Gemini se houver chave
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "org.subpalas.hybrid",
    "version": "2.0.0",
    "name": "SubPalas", # Nome atualizado conforme pedido
    "description": "Legendas PT-BR. Tenta Sincronizar (ffsubsync). Se falhar, Traduz (Gemini).",
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
        f"SubPalas: {msg}\nIsso pode levar de 10 a 60 segundos.\n\n"
        "2\n00:00:10,500 --> 00:00:20,000\n"
        "Se não carregar, tente selecionar novamente.\n"
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
            return data['data'] # Retorna lista bruta para processar
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
    """Lógica de Fallback: Baixa EN -> Traduz -> Salva como v1"""
    logger.info("FALLBACK: Iniciando tradução via IA...")
    
    # Busca EN
    results_en = search_subtitles(imdb_id, "en", season, episode)
    if not results_en:
        logger.error("FALLBACK: Nem legenda em inglês foi encontrada.")
        return

    # Pega o link da mais popular
    best_file = results_en[0]['attributes']['files'][0]
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    url_en = get_download_link(best_file['file_id'], headers)
    
    # Baixa e Traduz
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
            
            # Se falhar, usa original
            target_texts = translated if translated else texts
            
            for idx, txt in enumerate(target_texts):
                final_content.append(f"{chunk[idx]['head']}\n{txt}\n")
            time.sleep(1) # Rate limit safe

        # Salva como v1 (Principal)
        final_path = os.path.join(CACHE_DIR, f"{cache_key}_v1.srt")
        with open(final_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(final_content))
            
        logger.info("FALLBACK: Tradução concluída com sucesso.")
        
    except Exception as e:
        logger.error(f"FALLBACK Error: {e}")

# --- Core Logic Principal ---

def run_process(imdb_id, season, episode, cache_key):
    v1_marker = os.path.join(CACHE_DIR, f"{cache_key}_v1.srt")
    if os.path.exists(v1_marker) and os.path.getsize(v1_marker) > 100: 
        return # Cache Hit

    logger.info(f"--- PROCESSANDO: {cache_key} ---")
    
    # 1. Tenta achar PT-BR (Estratégia Principal)
    ptbr_results = search_subtitles(imdb_id, "pt-br", season, episode)
    
    if not ptbr_results:
        # Se não achou PT-BR, vai para o Fallback (Tradução)
        logger.warning("PT-BR não encontrada. Ativando Fallback IA.")
        run_translation_fallback(imdb_id, season, episode, cache_key)
        return

    # Se achou PT-BR, segue com a Sincronia (ffsubsync)
    logger.info("PT-BR encontrada. Iniciando Sincronia...")
    
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    url_pt = get_download_link(ptbr_results[0]['attributes']['files'][0]['file_id'], headers)
    
    path_pt = os.path.join(TEMP_DIR, f"{cache_key}_pt.srt")
    if not download_file(url_pt, path_pt): return

    # Busca Ref EN para sincronia
    en_results = search_subtitles(imdb_id, "en", season, episode)
    if not en_results:
        # Se não tem ref EN, salva PT original como v1
        shutil.copy(path_pt, v1_marker)
        return

    # Seleciona 3 Refs (WEB, HDTV, BLURAY)
    refs_map = {}
    for item in en_results:
        if len(refs_map) >= 3: break
        f = item['attributes']['files'][0]
        fname = f['file_name'].lower()
        
        rtype = 'DEFAULT'
        if any(x in fname for x in ['web', 'amzn']): rtype = 'WEB'
        elif 'bluray' in fname: rtype = 'BLURAY'
        elif 'hdtv' in fname: rtype = 'HDTV'
        
        if rtype not in refs_map:
            link = get_download_link(f['file_id'], headers)
            if link: refs_map[rtype] = link

    # Executa ffsubsync
    files_clean = [path_pt]
    
    # Ordem: Se tiver WEB, v1 = WEB. Se não tiver, v1 = Primeira que achou.
    # Garantir que v1 sempre exista é crucial.
    targets = [('WEB', 'v1'), ('HDTV', 'v2'), ('BLURAY', 'v3')]
    
    # Fallback se não achou tipos especificos: usa a primeira que tiver para v1
    if not refs_map:
         # Logica de segurança extrema
         shutil.copy(path_pt, v1_marker)
    else:
        # Preenche slots
        used_default = False
        for rtype, label in targets:
            url_ref = refs_map.get(rtype)
            
            # Se não tem WEB especifico, usa DEFAULT ou a primeira disponível para v1
            if not url_ref and label == 'v1' and not used_default:
                url_ref = list(refs_map.values())[0]
                used_default = True
            
            if url_ref:
                path_ref = os.path.join(TEMP_DIR, f"{cache_key}_ref_{label}.srt")
                final_path = os.path.join(CACHE_DIR, f"{cache_key}_{label}.srt")
                files_clean.append(path_ref)
                
                if download_file(url_ref, path_ref):
                    cmd = ["ffsubsync", path_ref, "-i", path_pt, "-o", final_path, "--encoding", "utf-8"]
                    try:
                        subprocess.run(cmd, capture_output=True, check=True, timeout=90)
                        logger.info(f"Sync OK: {label} ({rtype})")
                    except: pass

    cleanup_temp(files_clean)

# --- Rotas ---

@app.route('/')
def index(): return "SubPalas v2.0 Running"

@app.route('/manifest.json')
def manifest(): return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id, season, episode = parts[0], int(parts[1]) if len(parts)>1 else None, int(parts[2]) if len(parts)>2 else None
    cache_key = get_file_hash(imdb_id, season, episode)
    
    threading.Thread(target=run_process, args=(imdb_id, season, episode, cache_key)).start()
    host = request.host_url.rstrip('/')
    
    # Verifica o que temos disponível
    subs = []
    
    # Se existe v1 (Principal)
    # Se só existir v1 (caso de tradução), retornamos só ele.
    # Se existirem v2/v3 (caso de sync), retornamos todos.
    
    # Nota: Como o processo é assincrono, na primeira chamada não sabemos o que vai existir.
    # Retornamos os slots 'otimistas'. Se o arquivo nunca for criado (ex: só criou v1), o Stremio vai dar 404 no v2/v3 e não mostra nada.
    
    subs.append({"id": f"sp_v1_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v1.srt", "lang": "pob", "format": "srt"})
    subs.append({"id": f"sp_v2_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v2.srt", "lang": "pob", "format": "srt"})
    subs.append({"id": f"sp_v3_{cache_key}", "url": f"{host}/static_subs/{cache_key}_v3.srt", "lang": "pob", "format": "srt"})

    return jsonify({"subtitles": subs})

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)
    
    # Tenta esperar um pouco
    for _ in range(40):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            resp = make_response(send_from_directory(CACHE_DIR, filename))
            resp.headers['Cache-Control'] = 'public, max-age=31536000'
            return resp
        time.sleep(1)
        
    # Se estourar o tempo e for v2 ou v3, verificamos se v1 existe. 
    # Se v1 existe mas v2 não, provavelmente estamos no modo Tradução (só gera v1).
    # Nesse caso, retornamos 404 para o Stremio entender que não tem essa opção.
    if "_v2" in filename or "_v3" in filename:
        v1_path = path.replace("_v2", "_v1").replace("_v3", "_v1")
        if os.path.exists(v1_path):
            return "Not Found", 404

    # Se for v1 e estourou, manda aviso de carregando
    return generate_loading_srt("Processando..."), 200, {'Content-Type': 'application/x-subrip'}

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7000))
    app.run(host='0.0.0.0', port=port)
