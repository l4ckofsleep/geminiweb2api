from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import json
import re
import os
import uuid
import time
import base64
import hashlib
import asyncio
import sys

# Проверяем наличие флага --temp
IS_TEMP_CHAT = "--temp" in sys.argv

OUTPUT_DIR = "generated_images"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "x-same-domain": "1" 
}

GLOBAL_CLIENT = httpx.AsyncClient(headers=HEADERS, timeout=150.0, follow_redirects=True)

async def init_session():
    print("[*] Загрузка сессии из google_state.json...")
    GLOBAL_CLIENT.cookies.clear()
    
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
                GLOBAL_CLIENT.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
                if cookie['name'] == 'SAPISID':
                    sapisid = cookie['value']
                if cookie['name'] == '__Secure-1PSID':
                    has_base_cookie = True
                    
        if has_base_cookie and sapisid:
            timestamp = str(int(time.time() * 1000))
            hash_str = f"{timestamp} {sapisid} https://gemini.google.com"
            sha1 = hashlib.sha1(hash_str.encode()).hexdigest()
            GLOBAL_CLIENT.headers.update({"Authorization": f"SAPISIDHASH {timestamp}_{sha1}"})
            print("[+] Сессия успешно загружена из файла!")
            return True
        else:
            print("[!] Внимание: В файле сессии не найдены нужные куки. Возможно, сессия устарела.")
            return False
    except Exception as e:
        print(f"[!] Ошибка чтения файла сессии: {e}")
        return False

async def keep_alive_worker():
    while True:
        try:
            await asyncio.sleep(300)
            print("\n[*] Keep-alive: Проверка активности сессии...")
            resp = await GLOBAL_CLIENT.get("https://gemini.google.com/app", timeout=30.0)
            if resp.status_code == 200:
                if '"SNlM0e":"' in resp.text or '["SNlM0e","' in resp.text:
                    print("[+] Keep-alive: Сессия активна и успешно продлена.")
                else:
                    print("[!] Keep-alive: Сессия кажется невалидной (рекомендуется --refresh).")
            else:
                print(f"[!] Keep-alive: Ошибка сервера {resp.status_code}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[!] Keep-alive: Ошибка соединения: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_session()
    task = asyncio.create_task(keep_alive_worker())
    yield
    task.cancel()
    await GLOBAL_CLIENT.aclose()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def set_model_preference(snlm0e, mode_id):
    print(f"[*] Отправка сигнала переключения модели (Mode ID: {mode_id})...")
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=L5adhe&rt=c"
    
    null_array = [None] * 99
    null_array.append(mode_id)
    inner_json_data = [null_array, [["last_selected_mode_id_on_web"]]]
    inner_json_str = json.dumps(inner_json_data, separators=(',', ':'))
    
    req_data = {
        "f.req": json.dumps([[["L5adhe", inner_json_str, None, "generic"]]], separators=(',', ':')),
        "at": snlm0e
    }
    
    try:
        resp = await GLOBAL_CLIENT.post(url, data=req_data, timeout=15.0)
        if resp.status_code == 200:
            if "er" in resp.text and "generic" not in resp.text:
                print("[!] Сервер вернул 200, но внутри скрытая ошибка! Переключение могло не сработать.")
                return False
            print("[+] Модель на сервере (UI) успешно изменена!")
            return True
    except Exception as e:
        print(f"[!] Исключение при переключении модели: {e}")
    return False

async def upload_document_to_gemini(text_content, filename="chat.json"):
    print(f"[*] Выгрузка файла {filename} на сервера Google...")
    url = "https://content-push.googleapis.com/upload/"
    file_bytes = text_content.encode('utf-8')
    mime_type = "text/plain" 
    
    headers_start = {
        "Authority": "content-push.googleapis.com",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(file_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Tenant-Id": "bard-storage",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Push-ID": "feeds/mcudyrk2a4khkz",  
        "Authorization": "Basic c2F2ZXM6cyNMdGhlNmxzd2F2b0RsN3J1d1U=" 
    }
    
    try:
        res = await GLOBAL_CLIENT.post(url, headers=headers_start, content=b"", timeout=15.0)
        if res.status_code != 200: return None
        upload_url = res.headers.get("X-Goog-Upload-URL")
        if not upload_url: return None
            
        headers_upload = {
            "Authority": "content-push.googleapis.com",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "Content-Type": "application/octet-stream" 
        }
        res_upload = await GLOBAL_CLIENT.post(upload_url, headers=headers_upload, content=file_bytes, timeout=30.0)
        if res_upload.status_code == 200:
            resp_text = res_upload.text
            match = re.search(r'(/contrib_service/[a-zA-Z0-9_/\-\=]+)', resp_text)
            if match:
                upload_id = match.group(1)
                print(f"[+] Файл успешно загружен. Внутренний ID: {upload_id[:35]}...")
                return upload_id
            return resp_text.strip()
    except Exception as e:
        print(f"[!] Исключение при загрузке документа: {e}")
    return None

def is_garbage_node(text):
    if not isinstance(text, str): return False
    if text.startswith('http') or text.startswith('c_') or text.startswith('r_') or text.startswith('rc_'): return True
    if len(text) > 400 and " " not in text: return True
    if re.match(r'^[A-Za-z0-9_/\+\-]{40,}={0,2}', text): return True
        
    garbage_prefixes = [
        "Constructing the Scene", "Analyzing Scene Flow", "Composing Sensory Details",
        "Validating Output Criteria", "Refining Character Response", "Observing Seraphim",
        "Verifying Formatting", "Assessing Tactical", "Composing the Scene",
        "Refining the Output", "Finalizing the Scene", "Expanding the Scene",
        "Evaluating the Narrative", "Assessing the Reaction", "Composing the Response",
        "Refining the Russian"
    ]
    for prefix in garbage_prefixes:
        if text.startswith(prefix): return True
    return False

def find_actual_response(obj):
    longest = ""
    if isinstance(obj, str):
        if is_garbage_node(obj): return ""
        return obj
    if isinstance(obj, list):
        for item in obj:
            candidate = find_actual_response(item)
            if len(candidate) > len(longest): longest = candidate
    elif isinstance(obj, dict):
        for val in obj.values():
            candidate = find_actual_response(val)
            if len(candidate) > len(longest): longest = candidate
    return longest

def format_blocks(text):
    """Жесткое форматирование, как ты просил."""
    if not text: return text
    
    # 1. Срезаем внутренний мусор Гугла
    text = re.sub(r'^[A-Za-z0-9_/\+\-]{40,}={0,2}[^\n]*\n*', '', text)
    
    # 2. Нормализуем теги
    text = text.replace('<thinking>', '<think>').replace('</thinking>', '</think>')
    
    # 3. ЖЕСТКО ставим перенос строки после <think>
    text = re.sub(r'(?i)<think>\s*', '<think>\n', text)
    
    # 4. ЖЕСТКО ставим перенос строки до и после </think>
    text = re.sub(r'(?i)\s*</think>\s*', '\n</think>\n\n', text)
    
    # 5. Убираем лишние пустоты
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

async def generate_text_core(request: Request, prompt, model_name="nano-banana-pro", file_content=None):
    print(f"\n[*] Старт генерации текста (Режим монолита)...")
    doc_part = "null"
    if file_content:
        doc_id = await upload_document_to_gemini(file_content, filename="chat.json")
        if doc_id: doc_part = f'[[[{json.dumps(doc_id)},16,null,"application/json"],"chat.json"]]'

    try:
        resp = await GLOBAL_CLIENT.get("https://gemini.google.com/app", timeout=30.0)
        match = re.search(r'"SNlM0e":"(.*?)"', resp.text) or re.search(r'\["SNlM0e","(.*?)"\]', resp.text)
        if not match: return None
        snlm0e = match.group(1)
    except Exception: return None

    mode_id = "56fdd199312815e2" 
    if "thinking" in model_name.lower(): mode_id = "e051ce1aa80aa576"
    elif "pro" in model_name.lower(): mode_id = "e6fa609c3fa255c0"
        
    await set_model_preference(snlm0e, mode_id)
    await asyncio.sleep(1.0)

    stream_url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    candidate_id = uuid.uuid4().hex
    device_id = str(uuid.uuid4()).upper()

    temp_chat_flag = "1" if IS_TEMP_CHAT else "null"

    payload_str = f"""[[{json.dumps(prompt)},0,null,{doc_part},null,null,0],["ru"],["","","",null,null,null,null,null,null,""],"",{json.dumps(candidate_id)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,null,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2]"""
    req_data = {"f.req": json.dumps([None, payload_str], separators=(',', ':')), "at": snlm0e}

    req_headers = GLOBAL_CLIENT.headers.copy()
    req_headers["x-goog-ext-525001261-jspb"] = f'[1,null,null,null,"{mode_id}",null,null,null,null,null,null,2]'

    try:
        print("[*] Ожидание ответа от сервера Google...")
        full_text = ""
        
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            async for line in resp.aiter_lines():
                if await request.is_disconnected():
                    print("\n[!] 🛑 Клиент (Таверна) отменил запрос! ЖЕСТКО Разрываем соединение с Google.")
                    return None
                    
                if line:
                    try:
                        clean_line = re.sub(r'^\d+\s*', '', line)
                        if clean_line.startswith('['):
                            parsed_data = json.loads(clean_line)
                            if isinstance(parsed_data, list) and len(parsed_data) > 0 and isinstance(parsed_data[0], list):
                                item = parsed_data[0]
                                if len(item) > 2 and item[0] == "wrb.fr":
                                    inner_json_str = item[2]
                                    if inner_json_str:
                                        inner_data = json.loads(inner_json_str)
                                        extracted = find_actual_response(inner_data)
                                        if len(extracted) > len(full_text):
                                            full_text = extracted
                    except Exception: continue
        
        if full_text:
            print("[+] Текст успешно получен!")
            clean_text = re.sub(r'(?m)^\s*\\\s*$', '', full_text)
            clean_text = clean_text.replace('\\<', '<').replace('\\>', '>').replace('\\/', '/')
            return clean_text.strip()
        return None
    except Exception as e:
        print(f"[!] Ошибка соединения: {e}")
        return None

async def upload_image_to_gemini(image_bytes):
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
        res = await GLOBAL_CLIENT.post(url, headers=headers_start, content=b"", timeout=15.0)
        if res.status_code != 200: return None, None, None
        upload_url = res.headers.get("X-Goog-Upload-URL")
        if not upload_url: return None, None, None
            
        headers_upload = {
            "Authority": "content-push.googleapis.com",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "Content-Type": "application/octet-stream" 
        }
        res_upload = await GLOBAL_CLIENT.post(upload_url, headers=headers_upload, content=image_bytes, timeout=30.0)
        if res_upload.status_code == 200: return res_upload.text.strip(), mime_type, ext
    except Exception: pass
    return None, None, None

async def download_blob_via_batchexecute(snlm0e, blob, chat_id, r_id, rc_id, prompt):
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=c8o8Fe&rt=c"
    dummy_id = "r2h8onr2h8onr2h8"
    inner_json = f"""[[[null,null,null,[null,null,null,null,null,{json.dumps(blob)}]],["http://googleusercontent.com/image_generation_content/0",0],null,[19,{json.dumps(prompt)}],null,null,null,null,null,"{dummy_id}"],[{json.dumps(r_id)},{json.dumps(rc_id)},{json.dumps(chat_id)},null,"{dummy_id}"],1,0]"""
    req_data = {"f.req": json.dumps([[["c8o8Fe", inner_json, None, "generic"]]], separators=(',', ':')), "at": snlm0e}
    try:
        resp = await GLOBAL_CLIENT.post(url, data=req_data, timeout=15.0)
        urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', resp.text)
        if urls: return urls[-1]
    except Exception: pass
    return None

async def generate_image_core(request: Request, prompt, reference_images_b64=None, model_name="nano-banana-pro"):
    print(f"\n[*] Старт генерации картинки...")
    image_part = "null"
    if reference_images_b64:
        ref_data_list = []
        for b64 in reference_images_b64:
            try:
                img_bytes = base64.b64decode(b64)
                ref_id, mime_type, ext = await upload_image_to_gemini(img_bytes)
                if ref_id is not None: ref_data_list.append((ref_id, mime_type, ext))
            except Exception: pass
        if ref_data_list:
            images_json_list = []
            for i, (ref_id, mime_type, ext) in enumerate(ref_data_list):
                images_json_list.append(f'[[{json.dumps(ref_id)},1,null,{json.dumps(mime_type)}],"reference_{i}.{ext}"]')
            image_part = "[" + ",".join(images_json_list) + "]"

    try:
        resp = await GLOBAL_CLIENT.get("https://gemini.google.com/app", timeout=30.0)
        match = re.search(r'"SNlM0e":"(.*?)"', resp.text) or re.search(r'\["SNlM0e","(.*?)"\]', resp.text)
        if not match: return None
        snlm0e = match.group(1)
    except Exception: return None

    stream_url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    device_id = str(uuid.uuid4()).upper()
    candidate_1 = uuid.uuid4().hex
    
    is_pro_model = "pro" in model_name.lower()
    mode_id = "e6fa609c3fa255c0" if is_pro_model else "56fdd199312815e2"
    
    req_headers = GLOBAL_CLIENT.headers.copy()
    req_headers["x-goog-ext-525001261-jspb"] = f'[1,null,null,null,"{mode_id}",null,null,null,null,null,null,2]'
    
    if is_pro_model:
        msg_block = f'{json.dumps(prompt)},0,null,{image_part},null,null,0,null,null,[null,null,null,null,null,null,[null,[1]]]'
    else:
        msg_block = f'{json.dumps(prompt)},0,null,{image_part},null,null,0'
        
    temp_chat_flag = "1" if IS_TEMP_CHAT else "null"
        
    payload_1_str = f"""[[{msg_block}],["ru"],["","","",null,null,null,null,null,null,""],"",{json.dumps(candidate_1)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2]"""
    req_data = {"f.req": json.dumps([None, payload_1_str], separators=(',', ':')), "at": snlm0e}
    
    raw_1 = ""
    try:
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            async for line in resp.aiter_lines():
                if request and await request.is_disconnected(): return None
                if line: raw_1 += line + "\n"
    except Exception: pass
    if not raw_1: return None
    
    urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', raw_1)
    blobs = re.findall(r'"(\$[A-Za-z0-9+/\-=_]{50,})"', raw_1)
    
    chat_id_m = re.search(r'(c_[a-f0-9]{16})', raw_1)
    r_id_m = re.search(r'(r_[a-f0-9]{16,32})', raw_1)
    rc_id_m = re.search(r'(rc_[a-f0-9]{16,32})', raw_1)
    chat_id = chat_id_m.group(1) if chat_id_m else ""
    r_id = r_id_m.group(1) if r_id_m else ""
    rc_id = rc_id_m.group(1) if rc_id_m else ""
    
    final_url = None
    
    if urls or blobs:
        print("[+] Картинка успешно сгенерирована на 1 этапе!")
        final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt) if blobs else None)
    else:
        print("[-] На 1 этапе только текст. Запуск 2 этапа (Redo with Pro)...")
        tokens = re.findall(r'(Aw[A-Za-z0-9_-]{20,}|![A-Za-z0-9_-]{20,})', raw_1)
        state_token = max(tokens, key=len) if tokens else ""

        if is_pro_model and chat_id:
            candidate_2 = uuid.uuid4().hex  
            payload_2_str = f"""[[{json.dumps(prompt)},0,null,{image_part},null,null,0,null,null,[null,null,null,null,null,null,[null,[1]]]],["ru"],[{json.dumps(chat_id)},"","",null,null,null,null,null,null,""],{json.dumps(state_token)},{json.dumps(candidate_2)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2,null,null,null,7]"""
            req_2 = {"f.req": json.dumps([None, payload_2_str], separators=(',', ':')), "at": snlm0e}
            
            raw_target = ""
            try:
                async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_2, headers=req_headers, timeout=150.0) as resp:
                    async for line in resp.aiter_lines():
                        if request and await request.is_disconnected(): return None
                        if line: raw_target += line + "\n"
            except Exception: pass
            if not raw_target: return None
            
            urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', raw_target)
            blobs = re.findall(r'"(\$[A-Za-z0-9+/\-=_]{50,})"', raw_target)
            final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt) if blobs else None)
    
    if final_url:
        final_url = re.sub(r'=[swh]\d+.*$', '', final_url)
        high_res_url = f"{final_url}=s0"
        try:
            img_r = await GLOBAL_CLIENT.get(high_res_url)
            if img_r.status_code == 200:
                filepath = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.png")
                with open(filepath, 'wb') as f: f.write(img_r.content)
                return filepath
        except Exception: pass
    return None

@app.get('/v1/models')
@app.get('/v1beta/models')
@app.options('/v1/models')
@app.options('/v1beta/models')
async def list_models(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    models = [
        {"id": "nano-banana-pro", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "nano-banana-2", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.0-flash-preview", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.0-flash-thinking-preview", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.1-pro-preview", "object": "model", "created": 1712050000, "owned_by": "google"}
    ]
    return JSONResponse({"object": "list", "data": models, "models": models})

@app.post('/v1/images/generations')
@app.post('/v1beta/models/{model}:generateContent')
@app.options('/v1/images/generations')
@app.options('/v1beta/models/{model}:generateContent')
async def unified_image_generation(request: Request, model: str = None):
    if request.method == 'OPTIONS': return JSONResponse({})
    
    try: data = await request.json()
    except Exception: data = {}
    
    is_gemini_format = False
    prompt = data.get('prompt')
    requested_model = data.get('model') or model or "nano-banana-pro"
    reference_images_b64 = []
    
    ref_single = data.get('image')
    if ref_single:
        if ',' in ref_single: ref_single = ref_single.split(',', 1)[1]
        reference_images_b64.append(ref_single)
    
    if 'contents' in data:
        is_gemini_format = True
        try:
            for part in data['contents'][0]['parts']:
                if 'text' in part: prompt = part['text']
                if 'inlineData' in part:
                    b64_data = part['inlineData']['data']
                    if ',' in b64_data: b64_data = b64_data.split(',', 1)[1]
                    reference_images_b64.append(b64_data)
        except Exception: pass

    requested_size = data.get('size')
    requested_aspect = data.get('aspect_ratio')
    gen_config = data.get('generationConfig', {})
    img_config = gen_config.get('imageConfig', {})
    
    if not requested_aspect: requested_aspect = img_config.get('aspectRatio')
    if not requested_size: requested_size = img_config.get('imageSize')

    if isinstance(prompt, str) and prompt.strip().startswith('{') and prompt.strip().endswith('}'):
        try:
            hidden_data = json.loads(prompt)
            prompt = hidden_data.get('prompt', prompt) 
            requested_size = hidden_data.get('image_size') or hidden_data.get('size') or requested_size
            requested_aspect = hidden_data.get('aspect_ratio') or requested_aspect
        except Exception: pass
            
    if not prompt or not str(prompt).strip(): prompt = "A highly detailed, photorealistic masterpiece"
    prompt = str(prompt).replace('\n', ' ').replace('\r', ' ')
    
    format_instructions = []
    if requested_aspect: format_instructions.append(f"Aspect ratio: {requested_aspect}")
    if requested_size: format_instructions.append(f"Resolution: {requested_size}")
        
    if format_instructions:
        prompt = f"[SYSTEM INSTRUCTION: MUST USE FORMAT - {', '.join(format_instructions)}] {prompt}"

    image_path = await generate_image_core(request, prompt, reference_images_b64=reference_images_b64, model_name=requested_model)
    
    if not image_path: return JSONResponse({"error": "Failed"}, status_code=500)
    with open(image_path, "rb") as f: b64_data = base64.b64encode(f.read()).decode('utf-8')

    created_timestamp = int(time.time())
    
    if is_gemini_format:
        return JSONResponse({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": b64_data}}]}}]})
    else:
        response_format = data.get('response_format', 'url') 
        if response_format == 'b64_json':
            return JSONResponse({"created": created_timestamp, "data": [{"b64_json": b64_data}]})
        else:
            filename = os.path.basename(image_path)
            image_url = f"{request.base_url}images/{filename}"
            return JSONResponse({"created": created_timestamp, "data": [{"url": image_url}]})

@app.get('/images/{filename}')
async def serve_image(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    return FileResponse(file_path, media_type='image/png') if os.path.exists(file_path) else JSONResponse({"error": "Not found"}, status_code=404)

@app.post('/v1/chat/completions')
@app.options('/v1/chat/completions')
async def chat_completions(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    
    try: data = await request.json()
    except Exception: data = {}
    
    messages = data.get('messages', [])
    if not messages: return JSONResponse({"error": "No messages provided"}, status_code=400)
        
    chat_history = []
    for msg in messages:
        chat_history.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", "")
        })
    
    file_content = json.dumps(chat_history, ensure_ascii=False, indent=2)
    safe_prompt = "Пожалуйста, внимательно прочитай прикрепленный файл chat.json. Это ролевая игра. Ответь на самое последнее сообщение от лица персонажа (Assistant), строго следуя всем правилам и контексту, описанным внутри файла. ВАЖНО: Если в истории задан строгий шаблон для размышлений, ты ОБЯЗАН начать свой ответ с точного копирования и заполнения этого шаблона. Не упоминай сам файл chat.json в ответе."

    requested_model = data.get('model', 'nano-banana-pro').lower()
    is_stream = data.get('stream', False)
    
    prefill_text = ""
    if messages and messages[-1].get("role") == "assistant":
        prefill_text = messages[-1].get("content", "").strip()

    # 1. Запрашиваем голый сырой текст у Гугла
    generated_text = await generate_text_core(request, safe_prompt, model_name=requested_model, file_content=file_content)

    if generated_text is None:
        return JSONResponse({"error": {"message": "Request cancelled by user or failed", "type": "server_error"}}, status_code=500)

    # 2. Очищаем текст от эха префилла Гугла (если он его вернул)
    if prefill_text and generated_text.startswith(prefill_text):
        generated_text = generated_text[len(prefill_text):]

    # 3. Склеиваем префилл и ответ для глобального форматирования
    full_message = prefill_text + "\n" + generated_text if prefill_text else generated_text
    
    # 4. Идеально форматируем всю строку целиком
    full_message = format_blocks(full_message)

    # 5. Вычитаем из отформатированного текста наш префилл "в лоб"
    if prefill_text:
        # Считаем количество непустых символов в префилле Таверны
        prefill_len_non_ws = len(re.sub(r'\s', '', prefill_text))
        chars_chopped = 0
        split_idx = 0
        
        # Находим точку разреза
        for i, char in enumerate(full_message):
            if not char.isspace():
                chars_chopped += 1
            if chars_chopped == prefill_len_non_ws:
                split_idx = i + 1
                break
        
        # Отрезаем и сохраняем перенос строки
        final_text = full_message[split_idx:].lstrip(' \t')
        
        # Если префилл был тегом (например <think>), гарантируем перенос строки
        if prefill_text.endswith('>') and not final_text.startswith('\n'):
            final_text = '\n' + final_text
    else:
        final_text = full_message

    # 6. Возврат (Фейковый стриминг сохраняется ради кнопки Stop)
    if is_stream:
        async def sse_stream():
            cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())
            
            response_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": {"content": final_text}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(response_chunk)}\n\n"
            
            final_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(sse_stream(), media_type='text/event-stream')
        
    else:
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_text
                    },
                    "finish_reason": "stop"
                }
            ]
        })

if __name__ == "__main__":
    import uvicorn
    print("\n[*] Запуск FastAPI сервера (Порт: 1717)")
    uvicorn.run("api:app", host="0.0.0.0", port=1717, log_level="warning")