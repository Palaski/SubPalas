import os
import logging
import threading
import time
import requests
import re
import json
import google.generativeai as genai
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoTranslateAI")

CACHE_DIR = os.path.join(os.getcwd(), "subtitle_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# --- Configurações ---
OS_API_KEY = os.getenv("OS_API_KEY", "")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
# Nova chave obrigatória para a IA
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") 
USER_AGENT = os.getenv("USER_AGENT", "StremioAutoSync v1.0")

# Configura o Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MANIFEST = {
    "id": "community.autotranslate.ai",
    "version": "0.3.0",
    "name": "AutoSync AI (Gemini)",
    "description": "Tradução de alta qualidade via Google Gemini + Busca inteligente de release.",
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

def clean_filename(name):
    """Normaliza nome do arquivo para ajudar no match."""
    return name.replace('.', ' ').replace('-', ' ').lower()

# --- LLM Translation Logic (Gemini) ---

def translate_batch_gemini(texts):
    """
    Usa Gemini Flash para traduzir uma lista de frases.
    Retorna lista traduzida ou None se falhar.
    """
    if not GEMINI_API_KEY: return None

    model = genai.GenerativeModel('gemini-1.5-flash')
    
    # Prompt otimizado para legendas
    prompt = (
        "Você é um tradutor profissional de legendas (EN -> PT-BR). "
        "Traduza a lista de frases JSON abaixo para Português do Brasil. "
        "Regras: 1. Mantenha gírias e tom natural. 2. Seja conciso (limite de caracteres de TV). "
        "3. Retorne APENAS um array JSON de strings strings ex: ['Olá', 'Tudo bem?']. "
        "Não explique nada.\n\n"
        f"Input: {json.dumps(texts)}"
    )

    try:
        response = model.generate_content(prompt)
        # Tenta extrair o JSON da resposta (as vezes vem com markdown ```json ... ```)
        text_resp = response.text
        if "```" in text_resp:
            text_resp = text_resp.split("```json")[-1].split("```")[0].strip()
            if not text_resp.strip().startswith("["): # Tenta pegar só o array se o split falhar
                 text_resp = text_resp.split("[", 1)[-1].rsplit("]", 1)[0]
                 text_resp = "[" + text_resp + "]"
        
        translated = json.loads(text_resp)
        
        if isinstance(translated, list) and len(translated) == len(texts):
            return translated
        else:
            logger.warning("Gemini: Tamanho do array não bate.")
            return None
            
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return None

# --- Search Logic (Melhorada) ---

def get_download_link(file_id, headers):
    try:
        res = requests.post("[https://api.opensubtitles.com/api/v1/download](https://api.opensubtitles.com/api/v1/download)", headers=headers, json={"file_id": file_id})
        return res.json().get('link')
    except: return None

def search_english_sub(imdb_id, season=None, episode=None, video_hash=None, filename_hint=None):
    """
    Busca hierárquica:
    1. Hash (Perfeito)
    2. Release Name Match (Muito Bom)
    3. Genérico (Fallback)
    """
    if not OS_API_KEY: return None, "No API Key"
    headers = {"Api-Key": OS_API_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    try: clean_id = int(imdb_id.replace("tt", ""))
    except: return None, "Bad ID"

    # 1. Busca por HASH
    if video_hash:
        logger.info(f"--> Buscando HASH: {video_hash}")
        try:
            res = requests.get("[https://api.opensubtitles.com/api/v1/subtitles](https://api.opensubtitles.com/api/v1/subtitles)", headers=headers, params={"moviehash": video_hash, "languages": "en"}, timeout=10)
            data = res.json()
            if data.get('total_count', 0) > 0:
                return get_download_link(data['data'][0]['attributes']['files'][0]['file_id'], headers), "Hash Match"
        except Exception as e: logger.error(f"Erro Hash: {e}")

    # 2. Busca Genérica + Filtro de Release (Tenta corrigir o sync "totalmente fora")
    logger.info("--> Buscando por Nome/IMDB")
    params = {"imdb_id": clean_id, "languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if season: params.update({"season_number": season, "episode_number": episode})
    
    try:
        res = requests.get("[https://api.opensubtitles.com/api/v1/subtitles](https://api.opensubtitles.com/api/v1/subtitles)", headers=headers, params=params, timeout=10)
        data = res.json()
        
        if data.get('total_count', 0) > 0:
            results = data['data']
            best_file_id = results[0]['attributes']['files'][0]['file_id'] # Default: mais baixada
            method = "IMDB Generic"

            # Se tivermos uma dica do nome do arquivo (vindo do TorBox ou Extra)
            if filename_hint:
                hint_clean = clean_filename(filename_hint)
                logger.info(f"Refinando busca para release: {filename_hint}")
                
                # Palavras chave criticas: WEB-DL, HDTV, BLURAY, AMZN, NETFLIX
                keywords = [k for k in ['web', 'hdtv', 'bluray', 'dvd', 'amzn', 'nf', 'ntb'] if k in hint_clean]
                
                for item in results:
                    f_name = item['attributes']['files'][0]['file_name'].lower()
                    # Se todas as keywords criticas baterem
                    if all(k in f_name for k in keywords):
                        best_file_id = item['attributes']['files'][0]['file_id']
                        method = f"Release Match ({' '.join(keywords)})"
                        break
            
            return get_download_link(best_file_id, headers), method
    except: pass
    
    return None, "Not Found"

# --- Worker ---

def worker(imdb_id, season, episode, cache_key, video_hash, filename_hint=None):
    final_path = os.path.join(CACHE_DIR, f"{cache_key}_translated.srt")
    if os.path.exists(final_path): return

    logger.info(f"WORKER AI: Iniciando {cache_key}")
    
    # Tenta encontrar a melhor fonte EN
    # Passamos o filename_hint se disponivel (pode vir do extra params no futuro)
    url_en, method = search_english_sub(imdb_id, season, episode, video_hash, filename_hint)
    
    if not url_en:
        logger.error("WORKER: Sem fonte EN.")
        return

    try:
        r = requests.get(url_en)
        r.encoding = r.apparent_encoding
        content = r.text.replace('\r\n', '\n')
    except: return

    # Parse SRT
    blocks = re.split(r'\n\n+', content)
    subs = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 3:
            # Guarda o bloco original para caso de falha
            subs.append({'head': "\n".join(lines[:2]), 'text': " ".join(lines[2:])})

    # Processamento AI (Batch)
    final_srt_content = []
    final_srt_content.append(f"0\n00:00:01,000 --> 00:00:05,000\n[AI Sync: {method}]\n")

    batch_size = 20 # Gemini lida bem com blocos de ~20 falas
    
    for i in range(0, len(subs), batch_size):
        chunk = subs[i:i+batch_size]
        texts_en = [s['text'] for s in chunk]
        
        # Tenta traduzir com Gemini
        texts_pt = translate_batch_gemini(texts_en)
        
        if not texts_pt:
            # Fallback: Se Gemini falhar (rate limit/erro), usa original EN
            texts_pt = texts_en 
            logger.warning(f"Batch {i} falhou, usando EN.")

        # Monta o SRT
        for idx, sub in enumerate(chunk):
            # Proteção contra alinhamento quebrado
            if idx < len(texts_pt):
                final_srt_content.append(f"{sub['head']}\n{texts_pt[idx]}\n")
            else:
                final_srt_content.append(f"{sub['head']}\n{sub['text']}\n")
                
        # Pequeno sleep para respeitar rate limit do tier free (RPM)
        time.sleep(1.5) 

    # Salva
    with open(final_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(final_srt_content))
                
    logger.info(f"WORKER: Concluído {cache_key}")

# --- Rotas ---

@app.route('/')
def index(): return "AutoSync AI v0.3.0"

@app.route('/manifest.json')
def manifest(): return jsonify(MANIFEST)

@app.route('/subtitles/<type>/<id>/<extra>.json')
def subtitles(type, id, extra):
    parts = id.split(":")
    imdb_id, season, episode = parts[0], int(parts[1]) if len(parts)>1 else None, int(parts[2]) if len(parts)>2 else None
    
    video_hash = None
    filename_hint = None # Tenta extrair nome do arquivo da URL se possível
    
    if "videoHash=" in extra:
        try: video_hash = extra.split("videoHash=")[1].split("&")[0]
        except: pass
    
    # Alguns addons passam o 'filename' no extra, vamos tentar pegar
    if "filename=" in extra:
        try: filename_hint = extra.split("filename=")[1].split("&")[0]
        except: pass

    cache_key = get_file_hash(imdb_id, season, episode, video_hash)
    threading.Thread(target=worker, args=(imdb_id, season, episode, cache_key, video_hash, filename_hint)).start()
    
    host = request.host_url.rstrip('/')
    return jsonify({"subtitles": [{"id": f"ai_{cache_key}", "url": f"{host}/static_subs/{cache_key}_translated.srt", "lang": "pob", "format": "srt"}]})

@app.route('/static_subs/<filename>')
def serve_subs(filename):
    path = os.path.join(CACHE_DIR, filename)
    # Timeout generoso pois AI demora um pouco mais
    for _ in range(60): 
        if os.path.exists(path):
            resp = make_response(send_from_directory(CACHE_DIR, filename))
            resp.headers['Cache-Control'] = 'public, max-age=3600'
            return resp
        time.sleep(1)
    return ("1\n00:00:01,000 --> 00:00:10,000\nTraduzindo com IA...\nAguarde...\n\n", 200, {'Content-Type': 'application/x-subrip'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 7000)))
