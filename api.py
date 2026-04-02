from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import requests
import json
import re
import os
import uuid
import time
import base64
import hashlib

app = Flask(__name__)
CORS(app) 
OUTPUT_DIR = "generated_images"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
}

GLOBAL_SESSION = requests.Session()
GLOBAL_SESSION.headers.update(HEADERS)

def init_session():
    print("[*] Загрузка сессии из google_state.json...")
    GLOBAL_SESSION.cookies.clear()
    
    state_file = "google_state.json"
    
    if not os.path.exists(state_file):
        print("[!] Ошибка: Файл google_state.json не найден.")
        return False
        
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
            
        sapisid = None
        has_base_cookie = False
        
        for cookie in state.get("cookies", []):
            if cookie['name'] in ['__Secure-1PSID', '__Secure-1PSIDTS', 'SAPISID']:
                GLOBAL_SESSION.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
                if cookie['name'] == 'SAPISID':
                    sapisid = cookie['value']
                if cookie['name'] == '__Secure-1PSID':
                    has_base_cookie = True
                    
        if has_base_cookie and sapisid:
            timestamp = str(int(time.time() * 1000))
            hash_str = f"{timestamp} {sapisid} https://gemini.google.com"
            sha1 = hashlib.sha1(hash_str.encode()).hexdigest()
            GLOBAL_SESSION.headers.update({"Authorization": f"SAPISIDHASH {timestamp}_{sha1}"})
            print("[+] Сессия успешно загружена из файла!")
            return True
        else:
            print("[!] Внимание: В файле сессии не найдены нужные куки. Возможно, сессия устарела.")
            return False
            
    except Exception as e:
        print(f"[!] Ошибка чтения файла сессии: {e}")
        return False

def upload_image_to_gemini(session, image_bytes):
    mime_type = "image/jpeg"
    ext = "jpg"
    if image_bytes.startswith(b'\x89PNG'):
        mime_type = "image/png"
        ext = "png"
    elif image_bytes.startswith(b'GIF8'):
        mime_type = "image/gif"
        ext = "gif"
    elif image_bytes.startswith(b'RIFF') and b'WEBP' in image_bytes[8:12]:
        mime_type = "image/webp"
        ext = "webp"

    print(f"[*] Загрузка файла ({mime_type}, {len(image_bytes)} bytes)...")
    url = "https://content-push.googleapis.com/upload/"
    
    headers_start = {
        "Authority": "content-push.googleapis.com",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Tenant-Id": "bard-storage",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Push-ID": "feeds/mcudyrk2a4khkz",  
        "Authorization": "Basic c2F2ZXM6cyNMdGhlNmxzd2F2b0RsN3J1d1U=" 
    }
    
    try:
        res = requests.post(
            url, 
            headers=headers_start, 
            data=b"", 
            timeout=15, 
            proxies=session.proxies if hasattr(session, 'proxies') else None
        )
        
        if res.status_code != 200:
            print(f"[!] Ошибка инициализации загрузки: {res.status_code}")
            return None, None, None
            
        upload_url = res.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            print("[!] Ошибка: Сервер не вернул X-Goog-Upload-URL.")
            return None, None, None
            
        headers_upload = {
            "Authority": "content-push.googleapis.com",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "Content-Type": "application/octet-stream" 
        }
        
        res_upload = requests.post(
            upload_url, 
            headers=headers_upload, 
            data=image_bytes, 
            timeout=30,
            proxies=session.proxies if hasattr(session, 'proxies') else None
        )
        
        if res_upload.status_code == 200:
            upload_id = res_upload.text.strip()
            print(f"[+] Файл успешно загружен. ID: {upload_id[:35]}...")
            return upload_id, mime_type, ext
        else:
            print(f"[!] Ошибка отправки данных: {res_upload.status_code}")
    except Exception as e:
        print(f"[!] Исключение при загрузке: {e}")
    return None, None, None

def fetch_stream_patient(url, data, stage_name=""):
    raw_text = ""
    start_time = time.time()
    timeout_sec = 150 
    
    try:
        resp = GLOBAL_SESSION.post(url, data=data, stream=True, timeout=(30, 150))
        for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
            if chunk:
                raw_text += chunk
                
            if '"$' in raw_text and len(raw_text) > 1000: 
                if re.search(r'"(\$[A-Za-z0-9+/\-=_]{50,})"', raw_text):
                    print(f"[*] [{stage_name}] Блок данных получен.")
                    time.sleep(2)
                    break
            elif 'lh3.googleusercontent.com' in raw_text:
                print(f"[*] [{stage_name}] URL изображения получен.")
                time.sleep(2)
                break
                
            if '400,null,null,null,3]' in raw_text or 'er",null,null,null,null,400' in raw_text:
                print(f"[!] [{stage_name}] Сервер вернул ошибку 400.")
                break
                
            if time.time() - start_time > timeout_sec:
                print(f"[!] [{stage_name}] Превышено время ожидания.")
                break
    except Exception as e:
        print(f"[!] [{stage_name}] Ошибка соединения: {e}")
        
    return raw_text

def download_blob_via_batchexecute(snlm0e, blob, chat_id, r_id, rc_id, prompt):
    print("[*] Дешифровка данных через batchexecute...")
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=c8o8Fe&rt=c"
    dummy_id = "r2h8onr2h8onr2h8"
    inner_json = f"""[[[null,null,null,[null,null,null,null,null,{json.dumps(blob)}]],["http://googleusercontent.com/image_generation_content/0",0],null,[19,{json.dumps(prompt)}],null,null,null,null,null,"{dummy_id}"],[{json.dumps(r_id)},{json.dumps(rc_id)},{json.dumps(chat_id)},null,"{dummy_id}"],1,0]"""
    req_data = {"f.req": json.dumps([[["c8o8Fe", inner_json, None, "generic"]]], separators=(',', ':')), "at": snlm0e}
    
    try:
        resp = GLOBAL_SESSION.post(url, data=req_data, timeout=15)
        urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', resp.text)
        if urls:
            return urls[-1]
    except Exception as e:
        print(f"[!] Ошибка дешифровки: {e}")
    return None

def generate_image_core(prompt, reference_images_b64=None, model_name="nano-banana-pro"):
    print(f"\n[*] Старт генерации: {prompt[:150]}... [ПРОМПТ ОБРЕЗАН ДЛЯ ЛОГА]")
    print(f"[*] Выбранная модель: {model_name}")

    image_part = "null"
    if reference_images_b64:
        print(f"[*] Обработка референсов ({len(reference_images_b64)} шт.)...")
        ref_data_list = []
        for b64 in reference_images_b64:
            try:
                img_bytes = base64.b64decode(b64)
                ref_id, mime_type, ext = upload_image_to_gemini(GLOBAL_SESSION, img_bytes)
                if ref_id is not None:
                    ref_data_list.append((ref_id, mime_type, ext))
            except Exception as e:
                print(f"[!] Ошибка декодирования референса: {e}")
        
        if ref_data_list:
            images_json_list = []
            for i, (ref_id, mime_type, ext) in enumerate(ref_data_list):
                images_json_list.append(f'[[{json.dumps(ref_id)},1,null,{json.dumps(mime_type)}],"reference_{i}.{ext}"]')
            
            image_part = "[" + ",".join(images_json_list) + "]"

    try:
        resp = GLOBAL_SESSION.get("https://gemini.google.com/app", timeout=30)
        match = re.search(r'"SNlM0e":"(.*?)"', resp.text)
        if not match: return None
        snlm0e = match.group(1)
    except Exception: return None

    stream_url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    device_id = str(uuid.uuid4()).upper()

    print("[*] Этап 1: Базовая модель (nano-banana-2)...")
    candidate_1 = uuid.uuid4().hex
    
    payload_1_str = f"""[[{json.dumps(prompt)},0,null,{image_part},null,null,0],["ru"],["","","",null,null,null,null,null,null,""],"",{json.dumps(candidate_1)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,null,null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2]"""
    req_1 = {"f.req": json.dumps([None, payload_1_str], separators=(',', ':')), "at": snlm0e}
    
    raw_1 = fetch_stream_patient(stream_url, req_1, stage_name="Этап 1")
    
    chat_id_m = re.search(r'(c_[a-f0-9]{16})', raw_1)
    r_id_m = re.search(r'(r_[a-f0-9]{16,32})', raw_1)
    rc_id_m = re.search(r'(rc_[a-f0-9]{16,32})', raw_1)
    tokens = re.findall(r'(Aw[A-Za-z0-9_-]{20,}|![A-Za-z0-9_-]{20,})', raw_1)
    
    chat_id = chat_id_m.group(1) if chat_id_m else ""
    r_id = r_id_m.group(1) if r_id_m else ""
    rc_id = rc_id_m.group(1) if rc_id_m else ""
    state_token = max(tokens, key=len) if tokens else ""

    is_pro_model = "pro" in model_name.lower()

    if is_pro_model and chat_id:
        print(f"[*] Этап 2: Pro генерация (Chat ID: {chat_id})...")
        candidate_2 = uuid.uuid4().hex  
        
        payload_2_str = f"""[[{json.dumps(prompt)},0,null,{image_part},null,null,0,null,null,[null,null,null,null,null,null,[null,[1]]]],["ru"],[{json.dumps(chat_id)},"","",null,null,null,null,null,null,""],{json.dumps(state_token)},{json.dumps(candidate_2)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,null,null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2,null,null,null,7]"""
        req_2 = {"f.req": json.dumps([None, payload_2_str], separators=(',', ':')), "at": snlm0e}
        
        raw_target = fetch_stream_patient(stream_url, req_2, stage_name="Этап 2")
    else:
        if not is_pro_model:
            print("[*] Выбрана базовая модель, Этап 2 пропущен.")
        raw_target = raw_1
    
    urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', raw_target)
    blobs = re.findall(r'"(\$[A-Za-z0-9+/\-=_]{50,})"', raw_target)
    
    final_url = urls[-1] if urls else (download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt) if blobs else None)
    
    if final_url:
        final_url = re.sub(r'=[swh]\d+.*$', '', final_url)
        high_res_url = f"{final_url}=s0"
        
        try:
            img_r = GLOBAL_SESSION.get(high_res_url)
            if img_r.status_code == 200:
                filepath = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.png")
                with open(filepath, 'wb') as f: f.write(img_r.content)
                return filepath
        except Exception: pass
            
        try:
            img_r = GLOBAL_SESSION.get(final_url)
            if img_r.status_code == 200:
                filepath = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.png")
                with open(filepath, 'wb') as f: f.write(img_r.content)
                return filepath
        except Exception: pass
            
    return None

@app.route('/v1/models', methods=['GET', 'OPTIONS'])
@app.route('/v1beta/models', methods=['GET', 'OPTIONS'])
def list_models():
    if request.method == 'OPTIONS': return jsonify({}), 200
    
    models = [
        {"id": "nano-banana-pro", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "nano-banana-2", "object": "model", "created": 1712050000, "owned_by": "google"}
    ]
    return jsonify({"object": "list", "data": models, "models": models})

@app.route('/v1/images/generations', methods=['POST', 'OPTIONS'])
@app.route('/v1beta/models/<model>:generateContent', methods=['POST', 'OPTIONS'])
def unified_image_generation(model=None):
    if request.method == 'OPTIONS': return jsonify({}), 200
    
    print("\n[*] Обновление сессии...")
    init_session()
    
    data = request.get_json(silent=True) or {}
    
    # === РЕЖИМ ШПИОНА: СМОТРИМ ЧТО ШЛЕТ ТАВЕРНА ===
    print("\n[DEBUG] Сырые данные от клиента:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    # ===============================================
    
    is_gemini_format = False
    prompt = data.get('prompt')
    
    requested_model = data.get('model') or model or "nano-banana-pro"
    
    reference_images_b64 = []
    
    ref_single = data.get('image')
    if ref_single:
        if ',' in ref_single:
            ref_single = ref_single.split(',', 1)[1]
        reference_images_b64.append(ref_single)
    
    if 'contents' in data:
        is_gemini_format = True
        try:
            for part in data['contents'][0]['parts']:
                if 'text' in part:
                    prompt = part['text']
                if 'inlineData' in part:
                    b64_data = part['inlineData']['data']
                    if ',' in b64_data:
                        b64_data = b64_data.split(',', 1)[1]
                    reference_images_b64.append(b64_data)
        except Exception:
            pass

    # === НОВАЯ ЛОГИКА: ВСКРЫВАЕМ ХИТРЫЕ ЗАПРОСЫ ТАВЕРНЫ ===
    requested_size = data.get('size')
    requested_aspect = data.get('aspect_ratio')

    # Вдруг Таверна запихнула весь свой лог прямо в текстовую строку prompt? Проверяем!
    if isinstance(prompt, str) and prompt.strip().startswith('{') and prompt.strip().endswith('}'):
        try:
            hidden_data = json.loads(prompt)
            prompt = hidden_data.get('prompt', prompt) # Вытаскиваем чистый текст
            
            # Ищем формат внутри этого спрятанного JSON
            requested_size = hidden_data.get('image_size') or hidden_data.get('size') or requested_size
            requested_aspect = hidden_data.get('aspect_ratio') or requested_aspect
            print("\n[*] Бинго! Распаковали JSON, спрятанный внутри промпта!")
        except Exception:
            pass
            
    if not prompt or not str(prompt).strip():
        prompt = "A highly detailed, photorealistic masterpiece"

    # Зачищаем переносы строк, чтобы Гугл не подавился и не обрезал промпт на первом же энтере
    prompt = str(prompt).replace('\n', ' ').replace('\r', ' ')
    
    # Формируем спасительный префикс и клеим В НАЧАЛО
    format_instructions = []
    if requested_aspect:
        format_instructions.append(f"Aspect ratio: {requested_aspect}")
    if requested_size:
        format_instructions.append(f"Resolution: {requested_size}")
        
    if format_instructions:
        prompt = f"[SYSTEM INSTRUCTION: MUST USE FORMAT - {', '.join(format_instructions)}] {prompt}"
        print(f"[*] Успешно приклеили формат в начало: {format_instructions}")
    else:
        print("\n[!] ВНИМАНИЕ: Скрипт так и не нашел настроек формата в запросе от клиента!")
    # ========================================================

    image_path = generate_image_core(prompt, reference_images_b64=reference_images_b64, model_name=requested_model)
    
    if not image_path or not os.path.exists(image_path):
        return jsonify({"error": {"message": "Failed to generate image", "type": "server_error"}}), 500

    created_timestamp = int(time.time())
    
    with open(image_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode('utf-8')
    
    if is_gemini_format:
        return jsonify({
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": b64_data
                                }
                            }
                        ]
                    }
                }
            ]
        })
    else:
        response_format = data.get('response_format', 'url') 
        if response_format == 'b64_json':
            return jsonify({"created": created_timestamp, "data": [{"b64_json": b64_data}]})
        else:
            filename = os.path.basename(image_path)
            image_url = f"{request.host_url}images/{filename}"
            return jsonify({"created": created_timestamp, "data": [{"url": image_url}]})

@app.route('/images/<filename>', methods=['GET'])
def serve_image(filename):
    file_path = os.path.join(OUTPUT_DIR, filename)
    return send_file(file_path, mimetype='image/png') if os.path.exists(file_path) else ("Not found", 404)

if __name__ == "__main__":
    init_session()
    print("\n[*] Сервер API запущен (Порт: 1717)")
    app.run(host='0.0.0.0', port=1717, threaded=True)