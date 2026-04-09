from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
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
import random

# Парсинг аргументов
IS_TEMP_CHAT = "--temp" in sys.argv
IS_DEBUG = "--debug" in sys.argv

PROXY_URL = None
if "--proxy" in sys.argv:
    try:
        PROXY_URL = sys.argv[sys.argv.index("--proxy") + 1]
    except IndexError:
        pass

PORT = 1717
if "--port" in sys.argv:
    try:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    except (IndexError, ValueError):
        pass

OUTPUT_DIR = "generated_images"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LOG_FILE = "logs.txt"
STATE_FILE = "google_state.json"
TOKEN_STATE_KEY = "snlm0e"
TOKEN_UPDATED_AT_KEY = "snlm0e_updated_at"
SESSION_INVALID_EXIT_CODE = 86

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "x-same-domain": "1" 
}

client_kwargs = {
    "headers": HEADERS,
    "timeout": 150.0,
    "follow_redirects": True
}
if PROXY_URL:
    client_kwargs["proxy"] = PROXY_URL
    client_kwargs["verify"] = False # Отключаем паранойю на случай кривых прокси

GLOBAL_CLIENT = httpx.AsyncClient(**client_kwargs)

# --- Глобальный кэш для снижения спама запросами ---
CACHED_SNLM0E = None
CURRENT_MODEL_ID = None
session_initialized = False

def load_state_file():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except Exception:
        return None

def save_state_file(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        return True
    except Exception:
        return False

def load_saved_snlm0e(state=None):
    if state is None:
        state = load_state_file()
    if not isinstance(state, dict):
        return None
    token = state.get(TOKEN_STATE_KEY)
    if isinstance(token, str):
        token = token.strip()
        if token:
            return token
    return None

def persist_snlm0e(token):
    if not isinstance(token, str):
        return
    token = token.strip()
    if not token:
        return

    state = load_state_file() or {}
    if not isinstance(state, dict):
        state = {}
    state[TOKEN_STATE_KEY] = token
    state[TOKEN_UPDATED_AT_KEY] = int(time.time())
    save_state_file(state)

def clear_saved_snlm0e():
    state = load_state_file()
    if not isinstance(state, dict):
        return
    changed = False
    for key in [TOKEN_STATE_KEY, TOKEN_UPDATED_AT_KEY]:
        if key in state:
            state.pop(key, None)
            changed = True
    if changed:
        save_state_file(state)

def can_refresh_token_from_cookies():
    return bool(GLOBAL_CLIENT.headers.get("Authorization"))

def invalidate_cached_snlm0e(clear_saved=False):
    global CACHED_SNLM0E
    CACHED_SNLM0E = None
    if clear_saved:
        clear_saved_snlm0e()

async def refresh_snlm0e_from_cookies(reason):
    if not can_refresh_token_from_cookies():
        print_sys(f"[!] {reason}: не удалось обновить токен по кукам — в памяти нет рабочей cookie-сессии.")
        return None

    print_sys(f"[!] {reason}: сохраненный токен не подошел. Пробуем тихо обновить его по кукам...")
    invalidate_cached_snlm0e(clear_saved=True)
    refreshed = await get_snlm0e(force_refresh=True)
    if refreshed:
        print_sys("[+] Токен успешно обновлен по кукам.")
        return refreshed

    print_sys("[!] Обновить токен по кукам не удалось.")
    return None

def print_sys(msg):
    """Кастомный принт: пишет в консоль (затирая крутилку) и сохраняет в logs.txt"""
    t = time.strftime("%H:%M:%S")
    formatted_msg = f"[{t}] {msg}"
    
    sys.stdout.write(f"\r\033[K{formatted_msg}\n")
    sys.stdout.flush()
    
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted_msg + "\n")
    except Exception:
        pass

async def spinner_task(message="Ожидание ответа..."):
    """Асинхронная крутилка, которая работает в фоне"""
    chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    i = 0
    try:
        while True:
            sys.stdout.write(f'\r\033[K[*] {chars[i]} {message}')
            sys.stdout.flush()
            i = (i + 1) % len(chars)
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()

async def get_snlm0e(force_refresh=False):
    """Умное получение токена авторизации с кэшированием"""
    global CACHED_SNLM0E
    if CACHED_SNLM0E and not force_refresh:
        return CACHED_SNLM0E

    if not force_refresh:
        saved_token = load_saved_snlm0e()
        if saved_token:
            CACHED_SNLM0E = saved_token
            if IS_DEBUG: print_sys("[DEBUG] Загружен сохраненный токен SNlM0e из google_state.json.")
            return CACHED_SNLM0E
        
    if IS_DEBUG: print_sys("[DEBUG] Скачивание главной страницы для получения токена SNlM0e...")
    try:
        resp = await GLOBAL_CLIENT.get("https://gemini.google.com/app", timeout=30.0)
        match = re.search(r'"SNlM0e":"(.*?)"', resp.text) or re.search(r'\["SNlM0e","(.*?)"\]', resp.text)
        if not match: 
            print_sys("[❌] КРИТИЧЕСКАЯ ОШИБКА: Токен SNlM0e не найден. Куки протухли или нужен VPN.")
            return None
        CACHED_SNLM0E = match.group(1)
        persist_snlm0e(CACHED_SNLM0E)
        if IS_DEBUG: print_sys("[DEBUG] Токен SNlM0e успешно обновлен и кэширован.")
        return CACHED_SNLM0E
    except Exception as e: 
        print_sys(f"[❌] Ошибка соединения при получении токена: {e}")
        return None

async def init_session():
    print_sys("[*] Загрузка сессии из google_state.json...")
    GLOBAL_CLIENT.cookies.clear()
    GLOBAL_CLIENT.headers.pop("Authorization", None)
    global CACHED_SNLM0E, CURRENT_MODEL_ID, session_initialized
    CACHED_SNLM0E = None
    CURRENT_MODEL_ID = None
    session_initialized = False
    
    if not os.path.exists(STATE_FILE):
        print_sys("[!] Ошибка: Файл google_state.json не найден.")
        return False
        
    try:
        state = load_state_file()
        if not isinstance(state, dict):
            print_sys("[!] Ошибка: Файл google_state.json поврежден или пуст.")
            return False
            
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
        
        saved_token = load_saved_snlm0e(state)
        if saved_token:
            CACHED_SNLM0E = saved_token
            session_initialized = True
            print_sys("[+] Найден сохраненный токен SNlM0e. Сначала работаем через него, а куки оставляем для keep-alive и аварийного обновления.")
            return True

        if has_base_cookie and sapisid:
            print_sys("[+] Сессия загружена из файла. Сохраненного токена нет — получаем новый по кукам...")
            token = await get_snlm0e(force_refresh=True)
            if token:
                session_initialized = True
                print_sys("[+] Отлично! Новый токен получен и сохранен. Доступ к Gemini разрешен.")
                return True

            print_sys("[!] Куки найдены, но новый токен по ним получить не удалось.")
            return False
        else:
            print_sys("[!] Внимание: В файле сессии не найдены нужные куки. Возможно, сессия устарела.")
            return False
    except Exception as e:
        print_sys(f"[!] Ошибка чтения файла сессии: {e}")
        return False

async def keep_alive_worker():
    """Умный фоновый воркер с плавающим интервалом (защита от анти-бота)"""
    while True:
        try:
            sleep_time = random.randint(240, 480)
            await asyncio.sleep(sleep_time)
            
            if IS_DEBUG: print_sys(f"[DEBUG] Keep-alive: Продление сессии (пауза была {sleep_time//60} мин)...")
            
            token = await get_snlm0e(force_refresh=True)
            
            if token:
                if IS_DEBUG: print_sys("[DEBUG] Keep-alive: Сессия активна.")
            else:
                print_sys("[!] Keep-alive: Сессия убита Гуглом. Сделай --refresh.")
        except asyncio.CancelledError:
            break
        except Exception:
            pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not session_initialized:
        _ = await init_session()
    task = asyncio.create_task(keep_alive_worker())
    yield
    task.cancel()
    await GLOBAL_CLIENT.aclose()

app = FastAPI(lifespan=lifespan)

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        print_sys(f"\n[⚠️] ПРЕДУПРЕЖДЕНИЕ: Неизвестный запрос! Кто-то стучится на {request.method} {request.url.path}")
    return JSONResponse({"error": "Not found"}, status_code=exc.status_code)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def set_model_preference(snlm0e, mode_id):
    if IS_DEBUG: print_sys(f"[DEBUG] Отправка сигнала переключения модели (Mode ID: {mode_id})...")
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
                if IS_DEBUG: print_sys("[-] Сервер вернул 200, но внутри скрытая ошибка! Переключение могло не сработать.")
                return False
            if IS_DEBUG: print_sys("[+] Модель на сервере (UI) успешно изменена!")
            return True
    except Exception as e:
        if IS_DEBUG: print_sys(f"[❌] Исключение при переключении модели: {e}")
    return False

async def upload_document_to_gemini(text_content, filename="chat.json"):
    if IS_DEBUG: print_sys(f"[DEBUG] Выгрузка файла истории {filename} на сервера Google...")
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
        if res.status_code != 200: 
            print_sys(f"[❌] Ошибка загрузки документа (Старт): HTTP {res.status_code}")
            return None
        
        upload_url = res.headers.get("X-Goog-Upload-URL")
        if not upload_url: 
            print_sys("[❌] Ошибка: Гугл не выдал X-Goog-Upload-URL.")
            return None
            
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
                print_sys(f"[+] Файл истории успешно прикреплен (ID: {upload_id[:15]}...)")
                return upload_id
            return resp_text.strip()
    except Exception as e:
        print_sys(f"[❌] Исключение при загрузке документа: {e}")
    return None

def is_garbage_node(text):
    if not isinstance(text, str): return False
    if text.startswith('http') or text.startswith('c_') or text.startswith('r_') or text.startswith('rc_'): return True
    
    # ИСПРАВЛЕНО: Теперь скрипт учитывает и обычный пробел, и брайлевский невидимый пробел (U+2800)
    if len(text) > 400 and " " not in text and "⠀" not in text: return True
    
    if re.match(r'^[A-Za-z0-9_/\+\-]{40,}={0,2}', text): return True
        
    garbage_prefixes = [
        "Constructing the Scene", "Analyzing Scene Flow", "Composing Sensory Details",
        "Validating Output Criteria", "Refining Character Response", "Observing Seraphim",
        "Verifying Formatting", "Assessing Tactical", "Composing the Scene",
        "Refining the Output", "Finalizing the Scene", "Expanding the Scene",
        "Evaluating the Narrative", "Assessing the Reaction", "Composing the Response",
        "Refining the Russian", "Defining the Objective", "<think>\nDefining the Objective"
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

def normalize_thinking_tags(text, tag_name):
    if not isinstance(text, str):
        return ""

    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"
    text = re.sub(r'(?i)<think>|<thinking>', open_tag, text)
    text = re.sub(r'(?i)</think>|</thinking>', close_tag, text)
    return text

def choose_thinking_tag(prefill_text, generated_text):
    prefill_lower = prefill_text.lower() if isinstance(prefill_text, str) else ""
    generated_lower = generated_text.lower() if isinstance(generated_text, str) else ""
    if "<thinking>" in prefill_lower or "<thinking>" in generated_lower:
        return "thinking"
    return "think"

def strip_google_leading_garbage(text):
    if not isinstance(text, str) or not text:
        return ""
    return re.sub(r'^[A-Za-z0-9_/\+\-]{40,}={0,2}[^\n]*\n*', '', text, count=1)

def trim_prefill_echo(text, normalized_prefill):
    if not normalized_prefill:
        return text

    leading_ws_len = len(text) - len(text.lstrip())
    leading_ws = text[:leading_ws_len]
    body = text[leading_ws_len:]

    if not body.startswith(normalized_prefill):
        return text

    trimmed = body[len(normalized_prefill):]
    if normalized_prefill.endswith('>'):
        trimmed = '\n' + trimmed.lstrip(' \t')
    else:
        trimmed = trimmed.lstrip(' \t')

    return leading_ws + trimmed

def finalize_thinking_newlines(text, close_tag):
    text = re.sub(rf'(?i)\s*({re.escape(close_tag)})\s*', rf'\n\1\n\n', text)
    return re.sub(r'\n{3,}', '\n\n', text)

def enforce_opening_think_newline(text, tag_name):
    if not text:
        return ""

    open_tag = f"<{tag_name}>"
    return re.sub(rf'(?is)^\s*{re.escape(open_tag)}(?!\n)', f"{open_tag}\n", text, count=1)

def split_infoblock_suffix(text):
    if not isinstance(text, str) or not text:
        return "", ""

    match = re.search(r'(?is)^(.*?)(<infoblock\b.*?</infoblock>)\s*$', text)
    if not match:
        return text, ""

    return match.group(1), match.group(2)

def extract_leading_think_block(text, tag_name):
    if not isinstance(text, str) or not text:
        return "", text, False

    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"
    full_block_pattern = rf'(?is)^\s*({re.escape(open_tag)}.*?{re.escape(close_tag)})(.*)$'
    match = re.match(full_block_pattern, text)
    if match:
        return match.group(1), match.group(2), True

    stripped = text.lstrip()
    if stripped.startswith(open_tag):
        content = stripped[len(open_tag):].strip('\n\r \t')
        if content:
            repaired_block = f"{open_tag}\n{content}\n{close_tag}"
            return repaired_block, "", False

    return "", text, False

def format_think_block(think_block, tag_name, is_valid_block):
    if not think_block:
        return ""

    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"
    think_block = think_block.strip()

    if is_valid_block:
        think_block = re.sub(rf'(?is)^\s*{re.escape(open_tag)}\s*', f"{open_tag}\n", think_block, count=1)
        think_block = re.sub(rf'(?is)\s*{re.escape(close_tag)}\s*$', f"\n{close_tag}", think_block, count=1)
        return think_block.strip()

    if think_block.startswith(open_tag) and think_block.endswith(close_tag):
        think_content = think_block[len(open_tag):-len(close_tag)].strip('\n\r \t')
    else:
        think_content = think_block.strip('\n\r \t')

    if not think_content:
        return ""

    return f"{open_tag}\n{think_content}\n{close_tag}"

def normalize_infoblock_html(infoblock_html):
    if not infoblock_html:
        return ""

    infoblock_html = infoblock_html.strip()

    def encode_url_for_html_attr(url):
        return (
            url
            .replace('&', '&amp;')
            .replace(':', '&#58;')
            .replace('/', '&#47;')
            .replace('?', '&#63;')
            .replace('=', '&#61;')
        )

    def protect_url_attributes(match):
        attr_name = match.group(1)
        quote = match.group(2)
        url = match.group(3)
        markdown_url_match = re.match(r'^\[(https?://[^\]]+)\]\((https?://[^\)]+)\)$', url)
        if markdown_url_match:
            first_url = markdown_url_match.group(1)
            second_url = markdown_url_match.group(2)
            url = second_url if second_url == first_url or second_url else first_url

        protected_url = encode_url_for_html_attr(url)
        return f'{attr_name}={quote}{protected_url}{quote}'

    return re.sub(
        r'(?i)\b(src|href)\s*=\s*(["\'])(.*?)(\2)',
        protect_url_attributes,
        infoblock_html
    )

def postprocess_generated_text(generated_text, prefill_text):
    generated_text = strip_google_leading_garbage(generated_text)
    tag_name = choose_thinking_tag(prefill_text, generated_text)
    close_tag = f"</{tag_name}>"

    final_text = normalize_thinking_tags(generated_text, tag_name)
    normalized_prefill = normalize_thinking_tags(prefill_text, tag_name).strip()
    preserve_prefill_newline = normalized_prefill.endswith('>')
    final_text = trim_prefill_echo(final_text, normalized_prefill)

    main_text, infoblock_html = split_infoblock_suffix(final_text)
    think_block, body_text, is_valid_think_block = extract_leading_think_block(main_text, tag_name)

    think_block = format_think_block(think_block, tag_name, is_valid_think_block)
    if think_block:
        body_text = body_text.lstrip('\n\r \t')
    elif preserve_prefill_newline and body_text.startswith(('\n', '\r')):
        body_text = body_text.rstrip(' \t\n\r')
    else:
        body_text = body_text.strip()
    infoblock_html = normalize_infoblock_html(infoblock_html)

    parts = []
    if think_block:
        parts.append(think_block)
    if body_text:
        parts.append(body_text)

    final_text = "\n\n".join(parts)
    if final_text and infoblock_html:
        final_text = f"{final_text}\n\n{infoblock_html}"
    elif infoblock_html:
        final_text = infoblock_html

    final_text = enforce_opening_think_newline(final_text, tag_name)
    final_text = finalize_thinking_newlines(final_text, close_tag)
    return final_text.rstrip()

async def generate_text_core(request: Request, prompt, model_name="nano-banana-pro", file_content=None, allow_token_refresh_retry=True):
    global CURRENT_MODEL_ID, CACHED_SNLM0E
    
    print_sys("🚀 [ЭТАП 1] Подготовка данных...")
    doc_part = "null"
    if file_content:
        doc_id = await upload_document_to_gemini(file_content, filename="chat.json")
        if doc_id: doc_part = f'[[[{json.dumps(doc_id)},16,null,"application/json"],"chat.json"]]'
        else: print_sys("⚠️ Предупреждение: Не удалось прикрепить историю (chat.json). Генерация продолжится без неё.")

    print_sys("🔑 [ЭТАП 2] Проверка токена и настройка модели...")
    snlm0e = await get_snlm0e()
    if not snlm0e: 
        return None

    mode_id = "56fdd199312815e2" 
    if "thinking" in model_name.lower(): mode_id = "e051ce1aa80aa576"
    elif "pro" in model_name.lower(): mode_id = "e6fa609c3fa255c0"
        
    if CURRENT_MODEL_ID != mode_id:
        success = await set_model_preference(snlm0e, mode_id)
        if success:
            CURRENT_MODEL_ID = mode_id
            await asyncio.sleep(1.0)
    else:
        if IS_DEBUG: print_sys(f"[*] Модель уже настроена правильно, пропускаем лишний запрос.")

    stream_url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    candidate_id = uuid.uuid4().hex
    device_id = str(uuid.uuid4()).upper()

    temp_chat_flag = "1" if IS_TEMP_CHAT else "null"

    payload_str = f"""[[{json.dumps(prompt)},0,null,{doc_part},null,null,0],["ru"],["","","",null,null,null,null,null,null,""],"",{json.dumps(candidate_id)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,null,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2]"""
    req_data = {"f.req": json.dumps([None, payload_str], separators=(',', ':')), "at": snlm0e}

    req_headers = GLOBAL_CLIENT.headers.copy()
    req_headers["x-goog-ext-525001261-jspb"] = f'[1,null,null,null,"{mode_id}",null,null,null,null,null,null,2]'

    print_sys(f"📡 [ЭТАП 3] Отправка запроса в Google (Модель: {model_name})...")
    
    spinner = asyncio.create_task(spinner_task("Гугл думает над ответом..."))
    
    try:
        full_text = ""
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            
            if resp.status_code != 200:
                spinner.cancel()
                print_sys(f"[❌] ОШИБКА GOOGLE API: Сервер вернул статус HTTP {resp.status_code}")
                if resp.status_code in [400, 401, 403]:
                    invalidate_cached_snlm0e(clear_saved=True)
                    print_sys("[*] Кэш и сохранение токена сброшены.")
                    if allow_token_refresh_retry:
                        refreshed = await refresh_snlm0e_from_cookies("Текстовый запрос")
                        if refreshed:
                            return await generate_text_core(request, prompt, model_name=model_name, file_content=file_content, allow_token_refresh_retry=False)
                return None
                
            async for line in resp.aiter_lines():
                if await request.is_disconnected():
                    spinner.cancel()
                    print_sys("🛑 [ПРЕРВАНО] Клиент (Таверна) отменил запрос (нажата кнопка Stop). Разрываем соединение.")
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
                                        if IS_DEBUG: print_sys(f"[DEBUG] Raw JSON: {inner_json_str[:150]}...")
                                        inner_data = json.loads(inner_json_str)
                                        extracted = find_actual_response(inner_data)
                                        if len(extracted) > len(full_text):
                                            full_text = extracted
                    except Exception: continue
        
        spinner.cancel()
        print_sys("✅ [ЭТАП 4] Поток завершен. Анализ результата...")
        
        if not full_text:
            print_sys("[❌] ОШИБКА: Гугл вернул абсолютно пустой текст!")
            return None
            
        print_sys(f"[+] Сырой текст успешно извлечен (Длина: {len(full_text)} символов).")
        clean_text = re.sub(r'(?m)^\s*\\\s*$', '', full_text)
        clean_text = clean_text.replace('\\<', '<').replace('\\>', '>').replace('\\/', '/')
        return clean_text.strip()
    except asyncio.CancelledError:
        print_sys("🛑 [ОТМЕНЕНО] Генерация текста принудительно остановлена.")
        raise
    except httpx.ReadTimeout:
        spinner.cancel()
        print_sys("[❌] ОШИБКА: Тайм-аут. Гугл думал слишком долго (более 150 сек).")
        return None
    except Exception as e:
        spinner.cancel()
        print_sys(f"[❌] КРИТИЧЕСКАЯ ОШИБКА при чтении потока: {e}")
        return None
    finally:
        if not spinner.done():
            spinner.cancel()

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
        if res.status_code != 200:
            print_sys(f"[❌] Ошибка загрузки изображения (Старт): HTTP {res.status_code}")
            return None, None, None
        upload_url = res.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            print_sys("[❌] Ошибка загрузки изображения: Google не вернул X-Goog-Upload-URL.")
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
        res_upload = await GLOBAL_CLIENT.post(upload_url, headers=headers_upload, content=image_bytes, timeout=30.0)
        if res_upload.status_code == 200:
            return res_upload.text.strip(), mime_type, ext
        print_sys(f"[❌] Ошибка загрузки изображения (Финализация): HTTP {res_upload.status_code}")
    except Exception as e:
        print_sys(f"[❌] Исключение при загрузке изображения: {e}")
    return None, None, None

async def download_blob_via_batchexecute(snlm0e, blob, chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=True):
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=c8o8Fe&rt=c"
    dummy_id = "r2h8onr2h8onr2h8"
    inner_json = f"""[[[null,null,null,[null,null,null,null,null,{json.dumps(blob)}]],["http://googleusercontent.com/image_generation_content/0",0],null,[19,{json.dumps(prompt)}],null,null,null,null,null,"{dummy_id}"],[{json.dumps(r_id)},{json.dumps(rc_id)},{json.dumps(chat_id)},null,"{dummy_id}"],1,0]"""
    req_data = {"f.req": json.dumps([[["c8o8Fe", inner_json, None, "generic"]]], separators=(',', ':')), "at": snlm0e}
    try:
        resp = await GLOBAL_CLIENT.post(url, data=req_data, timeout=15.0)
        if resp.status_code != 200:
            print_sys(f"[❌] Ошибка получения blob-картинки: HTTP {resp.status_code}")
            if resp.status_code in [400, 401, 403] and allow_token_refresh_retry:
                invalidate_cached_snlm0e(clear_saved=True)
                refreshed = await refresh_snlm0e_from_cookies("Получение blob-картинки")
                if refreshed:
                    return await download_blob_via_batchexecute(refreshed, blob, chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=False)
            return None
        urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', resp.text)
        if urls:
            return urls[-1]
        print_sys("[❌] Blob-ответ получен, но URL картинки в нем не найден.")
    except Exception as e:
        print_sys(f"[❌] Исключение при получении blob-картинки: {e}")
    return None

async def generate_image_core(request: Request, prompt, reference_images_b64=None, model_name="nano-banana-pro", allow_token_refresh_retry=True):
    print_sys(f"\n[*] Старт генерации картинки...")
    image_part = "null"
    if reference_images_b64:
        ref_data_list = []
        for b64 in reference_images_b64:
            try:
                img_bytes = base64.b64decode(b64)
                ref_id, mime_type, ext = await upload_image_to_gemini(img_bytes)
                if ref_id is not None: ref_data_list.append((ref_id, mime_type, ext))
                else: print_sys("[!] Референсное изображение не удалось загрузить, пропускаем его.")
            except Exception as e:
                print_sys(f"[❌] Невалидное или битое референсное изображение: {e}")
        if ref_data_list:
            images_json_list = []
            for i, (ref_id, mime_type, ext) in enumerate(ref_data_list):
                images_json_list.append(f'[[{json.dumps(ref_id)},1,null,{json.dumps(mime_type)}],"reference_{i}.{ext}"]')
            image_part = "[" + ",".join(images_json_list) + "]"

    snlm0e = await get_snlm0e()
    if not snlm0e: return None

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
    
    spinner = asyncio.create_task(spinner_task("Рисуем картинку (Этап 1)..."))
    try:
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            if resp.status_code != 200:
                print_sys(f"[❌] ОШИБКА GOOGLE API (Картинки, этап 1): HTTP {resp.status_code}")
                if resp.status_code in [400, 401, 403] and allow_token_refresh_retry:
                    invalidate_cached_snlm0e(clear_saved=True)
                    refreshed = await refresh_snlm0e_from_cookies("Генерация картинки (этап 1)")
                    if refreshed:
                        return await generate_image_core(request, prompt, reference_images_b64=reference_images_b64, model_name=model_name, allow_token_refresh_retry=False)
                return None
            async for line in resp.aiter_lines():
                if request and await request.is_disconnected():
                    print_sys("🛑 [ПРЕРВАНО] Клиент отменил генерацию картинки на 1 этапе.")
                    return None
                if line: raw_1 += line + "\n"
    except asyncio.CancelledError:
        print_sys("🛑 [ОТМЕНЕНО] Генерация картинки принудительно остановлена на 1 этапе.")
        raise
    except httpx.ReadTimeout:
        print_sys("[❌] Тайм-аут генерации картинки на 1 этапе.")
    except Exception as e:
        print_sys(f"[❌] Исключение в потоке генерации картинки (Этап 1): {e}")
    finally:
        if not spinner.done(): spinner.cancel()
        
    if not raw_1:
        print_sys("[❌] Google не вернул данных на 1 этапе генерации картинки.")
        return None
    
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
        print_sys("[+] Картинка успешно сгенерирована на 1 этапе!")
        final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=allow_token_refresh_retry) if blobs else None)
    else:
        print_sys("[-] На 1 этапе только текст. Запуск 2 этапа (Redo with Pro)...")
        tokens = re.findall(r'(Aw[A-Za-z0-9_-]{20,}|![A-Za-z0-9_-]{20,})', raw_1)
        state_token = max(tokens, key=len) if tokens else ""

        if not is_pro_model:
            print_sys("[!] Второй этап генерации доступен только для Pro-модели. Останавливаемся после 1 этапа.")
        elif not chat_id:
            print_sys("[❌] Второй этап невозможен: chat_id не найден в ответе 1 этапа.")
        elif not state_token:
            print_sys("[❌] Второй этап невозможен: state token не найден в ответе 1 этапа.")
        else:
            candidate_2 = uuid.uuid4().hex  
            payload_2_str = f"""[[{json.dumps(prompt)},0,null,{image_part},null,null,0,null,null,[null,null,null,null,null,null,[null,[1]]]],["ru"],[{json.dumps(chat_id)},"","",null,null,null,null,null,null,""],{json.dumps(state_token)},{json.dumps(candidate_2)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2,null,null,null,7]"""
            req_2 = {"f.req": json.dumps([None, payload_2_str], separators=(',', ':')), "at": snlm0e}
            
            raw_target = ""
            spinner_2 = asyncio.create_task(spinner_task("Улучшаем качество (Этап 2)..."))
            try:
                async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_2, headers=req_headers, timeout=150.0) as resp:
                    if resp.status_code != 200:
                        print_sys(f"[❌] ОШИБКА GOOGLE API (Картинки, этап 2): HTTP {resp.status_code}")
                        if resp.status_code in [400, 401, 403] and allow_token_refresh_retry:
                            invalidate_cached_snlm0e(clear_saved=True)
                            refreshed = await refresh_snlm0e_from_cookies("Генерация картинки (этап 2)")
                            if refreshed:
                                return await generate_image_core(request, prompt, reference_images_b64=reference_images_b64, model_name=model_name, allow_token_refresh_retry=False)
                        return None
                    async for line in resp.aiter_lines():
                        if request and await request.is_disconnected():
                            print_sys("🛑 [ПРЕРВАНО] Клиент отменил генерацию картинки на 2 этапе.")
                            return None
                        if line: raw_target += line + "\n"
            except asyncio.CancelledError:
                print_sys("🛑 [ОТМЕНЕНО] Генерация картинки принудительно остановлена на 2 этапе.")
                raise
            except httpx.ReadTimeout:
                print_sys("[❌] Тайм-аут генерации картинки на 2 этапе.")
            except Exception as e:
                print_sys(f"[❌] Исключение в потоке генерации картинки (Этап 2): {e}")
            finally:
                if not spinner_2.done(): spinner_2.cancel()
                
            if not raw_target:
                print_sys("[❌] Google не вернул данных на 2 этапе генерации картинки.")
                return None
            
            urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', raw_target)
            blobs = re.findall(r'"(\$[A-Za-z0-9+/\-=_]{50,})"', raw_target)
            final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=allow_token_refresh_retry) if blobs else None)
    
    if final_url:
        final_url = re.sub(r'=[swh]\d+.*$', '', final_url)
        high_res_url = f"{final_url}=s0"
        try:
            img_r = await GLOBAL_CLIENT.get(high_res_url)
            if img_r.status_code == 200:
                filepath = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.png")
                with open(filepath, 'wb') as f: f.write(img_r.content)
                return filepath
            print_sys(f"[❌] Ошибка скачивания финальной картинки: HTTP {img_r.status_code}")
        except Exception as e:
            print_sys(f"[❌] Исключение при сохранении финальной картинки: {e}")
    else:
        print_sys("[❌] Финальный URL картинки не был получен.")
    return None

def is_image_model(model_name):
    return "nano-banana" in str(model_name or "").lower()

def normalize_requested_model(model_name):
    normalized = str(model_name or "").lower()
    if normalized in ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3.0-pro-preview"]:
        return "gemini-3.1-pro-preview"
    return normalized

def build_chat_history_from_gemini_contents(contents):
    chat_history = []

    for item in contents:
        if not isinstance(item, dict):
            continue

        role = item.get("role", "user")
        normalized_role = "assistant" if role == "model" else "user"
        parts = item.get("parts", [])
        text_parts = []

        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)

        combined_text = "\n".join(text_parts).strip()
        if combined_text:
            chat_history.append({
                "role": normalized_role,
                "content": combined_text
            })

    prefill_text = ""
    if chat_history and chat_history[-1].get("role") == "assistant":
        prefill_text = chat_history[-1].get("content", "").strip()

    return chat_history, prefill_text

def has_google_search_tool(data):
    tools = data.get("tools", []) if isinstance(data, dict) else []
    if not isinstance(tools, list):
        return False

    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("google_search"), dict):
            return True

    return False

def build_roleplay_safe_prompt(enable_google_search=False):
    prompt = "Пожалуйста, внимательно прочитай прикрепленный файл chat.json. Это ролевая игра. Ответь на самое последнее сообщение от лица персонажа (Assistant), строго следуя всем правилам и контексту, описанным внутри файла. ВАЖНО: Если в истории задан строгий шаблон для размышлений, ты ОБЯЗАН начать свой ответ с точного копирования и заполнения этого шаблона. Отвечай СТРОГО обычным текстом (plain text) и стандартным Markdown. КАТЕГОРИЧЕСКИ ЗАПРЕЩАЕТСЯ генерировать интерактивные виджеты, карточки, чипы Google (immersive_entry_chip). Не упоминай сам файл chat.json в ответе."

    if enable_google_search:
        prompt += " Дополнительно: для этого ответа ОБЯЗАТЕЛЬНО используй встроенный Google-поиск и попробуй найти релевантные детали из содержимого ролевой сцены — например персонажей, локации, музыку, предметы, организации, исторические/культурные отсылки или любые другие сущности, которые могут помочь ответу. Не упоминай отдельно, что ты делал поиск, если это не требуется по контексту; просто тихо используй найденное, когда это реально полезно сцене."

    return prompt

async def handle_gemini_text_generation(request: Request, data, requested_model):
    contents = data.get("contents", [])
    if not isinstance(contents, list) or not contents:
        return JSONResponse({"error": "No contents provided"}, status_code=400)

    chat_history, prefill_text = build_chat_history_from_gemini_contents(contents)
    if not chat_history:
        return JSONResponse({"error": "No text content provided"}, status_code=400)

    file_content = json.dumps(chat_history, ensure_ascii=False, indent=2)
    safe_prompt = build_roleplay_safe_prompt(enable_google_search=has_google_search_tool(data))
    effective_model = normalize_requested_model(requested_model)

    generated_text = await generate_text_core(request, safe_prompt, model_name=effective_model, file_content=file_content)
    if generated_text is None:
        return JSONResponse({"error": {"message": "Failed to generate text. Check server logs for details.", "type": "server_error"}}, status_code=500)

    final_text = postprocess_generated_text(generated_text, prefill_text)
    return JSONResponse({
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": final_text}
                    ]
                }
            }
        ]
    })

@app.get('/v1/models')
@app.get('/v1beta/models')
@app.options('/v1/models')
@app.options('/v1beta/models')
async def list_models(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    
    print_sys(f"\n{'='*50}\n🔍 ЗАПРОС СПИСКА МОДЕЛЕЙ\n{'='*50}")
    models = [
        {"id": "nano-banana-pro", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "nano-banana-2", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.0-flash-preview", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.0-flash-thinking-preview", "object": "model", "created": 1712050000, "owned_by": "google"},
        {"id": "gemini-3.1-pro-preview", "object": "model", "created": 1712050000, "owned_by": "google"}
    ]
    
    for m in models:
        print_sys(f"  - {m['id']}")
    print_sys("[+] Список моделей успешно отправлен в клиент.")
    
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

    if not is_image_model(requested_model):
        return await handle_gemini_text_generation(request, data, requested_model)
    
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
    
    if not image_path:
        return JSONResponse({"error": "Failed to generate image. Check server logs for details."}, status_code=500)
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

# =====================================================================
# НИЖЕ СНОВА ИДЕТ АКТУАЛЬНАЯ ЛОГИКА ГЕНЕРАЦИИ ТЕКСТА
# =====================================================================

@app.post('/v1/chat/completions')
@app.options('/v1/chat/completions')
async def chat_completions(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    
    print_sys(f"\n{'='*50}\n📥 НОВЫЙ ЗАПРОС ОТ ТАВЕРНЫ\n{'='*50}")
    
    try: data = await request.json()
    except Exception: data = {}
    
    messages = data.get('messages', [])
    if not messages: 
        print_sys("[❌] Ошибка: Таверна прислала пустой список сообщений.")
        return JSONResponse({"error": "No messages provided"}, status_code=400)
        
    chat_history = []
    for msg in messages:
        chat_history.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", "")
        })
    
    file_content = json.dumps(chat_history, ensure_ascii=False, indent=2)
    safe_prompt = build_roleplay_safe_prompt()

    requested_model = str(data.get('model', 'nano-banana-pro')).lower()
    effective_model = normalize_requested_model(requested_model)
    is_stream = data.get('stream', False)
    
    prefill_text = ""
    if messages and messages[-1].get("role") == "assistant":
        prefill_text = messages[-1].get("content", "").strip()
        print_sys(f"[*] Обнаружен префилл от Таверны (Длина: {len(prefill_text)} символов).")

    if is_stream:
        async def sse_stream():
            cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())
            
            # Запускаем генерацию Гугла в виде фоновой задачи
            task = asyncio.create_task(generate_text_core(request, safe_prompt, model_name=effective_model, file_content=file_content))
            try:
                # Пока задача не завершена, каждые 10 секунд кидаем пустышку (пульс), чтобы браузер не убил сокет
                while not task.done():
                    if await request.is_disconnected():
                        print_sys("🛑 [ПРЕРВАНО] SSE-клиент отключился во время ожидания ответа. Отменяем генерацию.")
                        task.cancel()
                        return

                    _, pending = await asyncio.wait([task], timeout=10.0)
                    if pending:
                        ping_chunk = {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": requested_model,
                            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(ping_chunk)}\n\n"

                try:
                    # Получаем реальный результат
                    generated_text = task.result()
                except asyncio.CancelledError:
                    print_sys(f"🏁 ЗАВЕРШЕНО. Стрим был отменен клиентом.\n{'='*50}")
                    return
                except Exception as e:
                    print_sys(f"[❌] ОШИБКА ФОНОВОЙ ГЕНЕРАЦИИ: {e}")
                    err_chunk = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": requested_model, "choices": [{"index": 0, "delta": {"content": "\n[❌ Ошибка генерации. Проверьте логи сервера]"}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    print_sys(f"🏁 ЗАВЕРШЕНО С ОШИБКОЙ.\n{'='*50}")
                    return

                if generated_text is None:
                    err_chunk = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": requested_model, "choices": [{"index": 0, "delta": {"content": "\n[❌ Ошибка генерации. Проверьте логи сервера]"}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    print_sys(f"🏁 ЗАВЕРШЕНО С ОШИБКОЙ.\n{'='*50}")
                    return

                print_sys("✨ [ЭТАП 5] Единая безопасная постобработка ответа...")
                final_text = postprocess_generated_text(generated_text, prefill_text)

                print_sys(f"✅ [ЭТАП 6] Текст готов к отправке (Длина: {len(final_text)}). Выдаем финальный результат...")

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
                print_sys(f"🏁 ЗАВЕРШЕНО. Сообщение доставлено в Таверну.\n{'='*50}")
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
             
        return StreamingResponse(sse_stream(), media_type='text/event-stream')

    else:
        # Резервный механизм, если стриминг выключен
        generated_text = await generate_text_core(request, safe_prompt, model_name=effective_model, file_content=file_content)

        if generated_text is None:
            print_sys("[❌] ИТОГ: Генерация прервана или завершилась сбоем. Отправляем ошибку в Таверну.")
            return JSONResponse({"error": {"message": "Request cancelled by user or failed (Check console logs)", "type": "server_error"}}, status_code=500)

        print_sys("✨ [ЭТАП 5] Единая безопасная постобработка ответа...")
        final_text = postprocess_generated_text(generated_text, prefill_text)

        print_sys(f"✅ [ЭТАП 6] Текст готов к отправке (Длина: {len(final_text)}).")
        print_sys(f"🏁 ЗАВЕРШЕНО. Сообщение доставлено в Таверну (Без стрима).\n{'='*50}")
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": final_text}, "finish_reason": "stop"}]
        })

if __name__ == "__main__":
    import uvicorn

    async def run_server():
        session_ok = await init_session()
        if not session_ok:
            print_sys("[!] Куки устарели или стали недействительными. Возвращаемся в лаунчер и пробуем один автоматический refresh...")
            raise SystemExit(SESSION_INVALID_EXIT_CODE)

        print_sys(f"\n[*] Geminiweb2API запущен! (Порт: {PORT})")
        config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(run_server())
