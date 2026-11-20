from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
import threading
from gemini_session import get_gemini_session_cookies
from log_utils import debug_log, debug_log_throttled, log_line, reset_request_log, sanitize_headers, start_request_log

# Парсинг аргументов
IS_TEMP_CHAT = "--temp" in sys.argv
IS_DEBUG = "--debug" in sys.argv
IS_MOBILE = (
    "--mobile" in sys.argv
    or
    'com.termux' in os.environ.get('PREFIX', '')
    or 'ANDROID_STORAGE' in os.environ
    or hasattr(sys, 'getandroidapilevel')
    or sys.platform.startswith('linux')
)

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

STATE_FILE = "google_state.json"
TOKEN_STATE_KEY = "snlm0e"
TOKEN_UPDATED_AT_KEY = "snlm0e_updated_at"
SESSION_INVALID_EXIT_CODE = 86
GEMINI_ORIGIN = "https://gemini.google.com"

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
session_initialized = False
web_client_session_id = str(uuid.uuid4()).upper()
browser_request_context_cache = {}
browser_request_context_updated_at = 0.0
browser_request_context_lock = threading.Lock()
BROWSER_REQUEST_CONTEXT_TTL_SECONDS = 600.0
web_model_catalog = None
web_model_catalog_updated_at = 0.0
web_model_catalog_lock = asyncio.Lock()
MODEL_CATALOG_RPC = "otAQ7b"
stream_generate_reqid_lock = threading.Lock()
stream_generate_reqid_counter = (int(time.time() * 1000) % 9000000) + 1000000
DEFAULT_STREAM_METADATA = ["", "", "", None, None, None, None, None, None, ""]
ACTIVE_TURN_MARKER_START = "<<<GEMINIWEB2API_ACTIVE_TURN_START>>>"
ACTIVE_TURN_MARKER_END = "<<<GEMINIWEB2API_ACTIVE_TURN_END>>>"
ACTIVE_TURN_METADATA_MARKER = "GEMINIWEB2API_ACTIVE_TURN_METADATA"


@dataclass(frozen=True)
class WebTextModel:
    mode_id: str
    model_type: int
    capacity: int = 2
    capacity_field: int = 12


LEGACY_TEXT_MODELS = {
    "gemini-3.5-flash": WebTextModel("56fdd199312815e2", 1),
    "gemini-3.5-flash-extended": WebTextModel("56fdd199312815e2", 1),
    "gemini-3.1-pro-preview": WebTextModel("e6fa609c3fa255c0", 3),
    "gemini-3.1-pro-extended": WebTextModel("e6fa609c3fa255c0", 3),
}


class WebModelError(Exception):
    def __init__(self, message, status_code, code):
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    def payload(self):
        return {"error": {"message": str(self), "code": self.code}}


def get_default_browser_hl():
    accept_language = str(HEADERS.get("Accept-Language") or GLOBAL_CLIENT.headers.get("Accept-Language") or "ru")
    primary_language = accept_language.split(",", 1)[0].strip()
    if not primary_language:
        return "ru"
    return primary_language.split("-", 1)[0] or "ru"


def next_stream_generate_reqid():
    global stream_generate_reqid_counter

    with stream_generate_reqid_lock:
        stream_generate_reqid_counter += 1
        if stream_generate_reqid_counter > 9999999:
            stream_generate_reqid_counter = 1000000
        return str(stream_generate_reqid_counter)


def extract_browser_request_context(app_html):
    if not isinstance(app_html, str) or not app_html:
        return {}

    context = {}
    cfb2h_match = re.search(r'"cfb2h":"([^"]+)"', app_html)
    if cfb2h_match:
        context["bl"] = cfb2h_match.group(1)

    literal_fsid_match = re.search(r'"f\.sid":"([^"]+)"', app_html)
    if literal_fsid_match:
        context["f.sid"] = literal_fsid_match.group(1)
        context["f.sid_source"] = "literal_f.sid"
    else:
        fdrfje_match = re.search(r'"FdrFJe":"([^"]+)"', app_html)
        if fdrfje_match:
            context["f.sid"] = fdrfje_match.group(1)
            context["f.sid_source"] = "FdrFJe"

    hl_match = re.search(r'"hl":"([^"]+)"', app_html)
    if hl_match:
        context["hl"] = hl_match.group(1)
    elif context:
        context["hl"] = get_default_browser_hl()
    return context


def cache_browser_request_context(context):
    global browser_request_context_cache, browser_request_context_updated_at
    if not isinstance(context, dict) or not context:
        return

    with browser_request_context_lock:
        browser_request_context_cache = {
            key: value for key, value in context.items() if isinstance(value, str) and value.strip()
        }
        browser_request_context_updated_at = time.monotonic()


def get_cached_browser_request_context(max_age_seconds=None):
    with browser_request_context_lock:
        context = dict(browser_request_context_cache)
        updated_at = browser_request_context_updated_at

    if not context:
        return {}

    if max_age_seconds is not None and updated_at:
        age_seconds = time.monotonic() - updated_at
        if age_seconds > max_age_seconds:
            return {}
    return context


def build_stream_generate_url(browser_context, reqid=None):
    params = {"rt": "c"}
    if isinstance(browser_context, dict):
        if browser_context.get("bl"):
            params["bl"] = browser_context["bl"]
        if browser_context.get("f.sid"):
            params["f.sid"] = browser_context["f.sid"]
    if isinstance(reqid, str) and reqid:
        params["_reqid"] = reqid
    context_hl = browser_context.get("hl") if isinstance(browser_context, dict) else None
    params["hl"] = str(context_hl or get_default_browser_hl())
    return f"https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?{httpx.QueryParams(params)}"


def build_stream_generate_headers(
    base_headers, mode_id, request_uuid, model_type=1, thinking_level=1,
    capacity=2, capacity_field=12, client_session_id=None,
):
    headers = base_headers.copy()
    headers["Accept-Language"] = headers.get("Accept-Language", "ru,en;q=0.9,en-GB;q=0.8,en-US;q=0.7")
    headers["Origin"] = "https://gemini.google.com"
    headers["Referer"] = "https://gemini.google.com/"
    model_header = [
        1, None, None, None, mode_id, None, None, 0, [4, 5, 6, 8],
        None, None, None, None, None, model_type, thinking_level, client_session_id or request_uuid,
    ]
    # JSPB fields are numbered starting at 1.
    model_header[capacity_field - 1] = capacity
    headers["x-goog-ext-525001261-jspb"] = json.dumps(model_header, separators=(',', ':'))
    headers["x-goog-ext-525005358-jspb"] = json.dumps([request_uuid, 1], separators=(',', ':'))
    headers["x-goog-ext-73010989-jspb"] = "[0]"
    headers["x-goog-ext-73010990-jspb"] = "[0, 0, 0]"
    return headers


def build_attachment_file_data(doc_id, filename="conversation-transcript.txt", mime_type="text/plain"):
    return [[[doc_id, 16, None, mime_type], filename]]


def build_chat_json_file_data(doc_id, filename="chat.json"):
    return build_attachment_file_data(doc_id, filename, "application/json")


def build_stream_generate_payload(
    prompt, file_data, request_uuid, language, temporary_chat, model_type, thinking_level,
):
    inner_req_list: list[object] = [None] * 81
    inner_req_list[0] = [prompt, 0, None, file_data, None, None, 0]
    inner_req_list[1] = [language]
    inner_req_list[2] = DEFAULT_STREAM_METADATA.copy()
    inner_req_list[6] = [1]
    inner_req_list[7] = 1
    inner_req_list[10] = 1
    inner_req_list[11] = 0
    inner_req_list[17] = [[0]]
    inner_req_list[18] = 0
    inner_req_list[27] = 1
    inner_req_list[30] = [4]
    inner_req_list[41] = [1]
    if temporary_chat:
        inner_req_list[45] = 1
    inner_req_list[53] = 0
    inner_req_list[59] = request_uuid
    inner_req_list[61] = []
    inner_req_list[68] = 1
    inner_req_list[79] = model_type
    inner_req_list[80] = thinking_level
    return json.dumps(inner_req_list, separators=(',', ':'))

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

def print_final_session_failure(reason):
    if IS_MOBILE:
        print_sys(f"[❌] {reason}: не удалось обновить токен. Сделай свежий JSON-экспорт cookies со вкладки Gemini и запусти start.py --refresh.")
        return

    print_sys(f"[❌] {reason}: не удалось использовать токен, получить новый по кукам, и автоматический refresh тоже не помог. Возможно, Google сейчас лежит, VPN не подходит, куки нужно сбросить или запустить start.py --reauth.")

async def run_desktop_auto_refresh(reason):
    if IS_MOBILE:
        return False

    print_sys(f"[!] {reason}: токен не удалось восстановить по кукам. Пробуем один автоматический refresh на ПК...")
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        print_sys(f"[*] Старый файл {STATE_FILE} удален перед авто-refresh.")

    auth_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.py")
    args = [sys.executable, auth_script]
    if PROXY_URL:
        args.extend(["--proxy", PROXY_URL])

    try:
        process = await asyncio.create_subprocess_exec(*args)
        returncode = await process.wait()
    except Exception as e:
        print_sys(f"[❌] Не удалось запустить авто-refresh: {e}")
        return False

    if returncode != 0 or not os.path.exists(STATE_FILE):
        print_sys("[!] Автоматический refresh не смог сохранить новую сессию.")
        return False

    print_sys("[*] Автоматический refresh завершен. Переинициализируем сессию...")
    session_ok = await init_session()
    if session_ok:
        print_sys("[+] Сессия успешно восстановлена после авто-refresh.")
        return True

    print_sys("[!] После авто-refresh сессию восстановить не удалось.")
    return False

async def get_or_recover_request_snlm0e(reason, allow_token_refresh_retry=True, allow_desktop_refresh_retry=True):
    token = await get_snlm0e()
    if token:
        return token, allow_token_refresh_retry, allow_desktop_refresh_retry

    if allow_token_refresh_retry:
        refreshed = await refresh_snlm0e_from_cookies(f"{reason} (токен не найден)")
        if refreshed:
            return refreshed, False, allow_desktop_refresh_retry

    if allow_desktop_refresh_retry and not IS_MOBILE:
        refreshed_session = await run_desktop_auto_refresh(f"{reason} (токен не найден)")
        if refreshed_session:
            token = await get_snlm0e()
            if token:
                return token, False, False

    print_final_session_failure(reason)
    return None, False, False

async def recover_request_snlm0e(reason, allow_token_refresh_retry=True, allow_desktop_refresh_retry=True):
    invalidate_cached_snlm0e(clear_saved=True)
    print_sys("[*] Кэш и сохранение токена сброшены.")

    if allow_token_refresh_retry:
        refreshed = await refresh_snlm0e_from_cookies(reason)
        if refreshed:
            return refreshed, False, allow_desktop_refresh_retry

    if allow_desktop_refresh_retry and not IS_MOBILE:
        refreshed_session = await run_desktop_auto_refresh(reason)
        if refreshed_session:
            token = await get_snlm0e()
            if token:
                return token, False, False

    print_final_session_failure(reason)
    return None, False, False

def print_sys(msg):
    """Кастомный принт: пишет в консоль и в дневной лог api"""
    log_line("api", str(msg))


def print_debug(label, data=None, max_len=8000):
    debug_log("api", IS_DEBUG, label, data, max_len=max_len)


def print_debug_throttled(throttle_key, label, data=None, max_len=8000, interval_seconds=8.0, force=False):
    debug_log_throttled(
        "api",
        IS_DEBUG,
        throttle_key,
        label,
        data=data,
        max_len=max_len,
        interval_seconds=interval_seconds,
        force=force,
    )


def start_api_request_logging(request: Request, route_label: str):
    request_id = uuid.uuid4().hex[:8]
    token, log_path = start_request_log("api", route_label, request_id=request_id)
    print_sys(f"\n{'='*50}\n🗂️ ЛОГ ЗАПРОСА: {os.path.basename(log_path)}\n🌐 {request.method} {request.url.path}\n{'='*50}")
    print_debug("Request log context", {
        "route_label": route_label,
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "log_path": log_path,
    })
    return token, log_path

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
        return None
        
    if IS_DEBUG: print_sys("[DEBUG] Скачивание главной страницы для получения токена SNlM0e...")
    try:
        resp = await GLOBAL_CLIENT.get(f"{GEMINI_ORIGIN}/app", timeout=30.0)
        if resp.status_code != 200 or resp.url.host != "gemini.google.com":
            print_sys(f"[!] Не удалось открыть Gemini Web для обновления сессии (HTTP {resp.status_code}, хост {resp.url.host}).")
            return None
        browser_request_context = extract_browser_request_context(resp.text)
        print_debug("SNlM0e fetch response", {
            "status_code": resp.status_code,
            "headers": sanitize_headers(resp.headers),
            "browser_request_context": browser_request_context,
        }, max_len=12000)
        match = re.search(r'"SNlM0e":"(.*?)"', resp.text) or re.search(r'\["SNlM0e","(.*?)"\]', resp.text)
        if not match: 
            print_sys("[❌] КРИТИЧЕСКАЯ ОШИБКА: Токен SNlM0e не найден. Куки протухли или нужен VPN.")
            return None
        CACHED_SNLM0E = match.group(1)
        cache_browser_request_context(browser_request_context)
        persist_snlm0e(CACHED_SNLM0E)
        if IS_DEBUG: print_sys("[DEBUG] Токен SNlM0e успешно обновлен и кэширован.")
        return CACHED_SNLM0E
    except Exception as e: 
        print_sys(f"[❌] Ошибка соединения при получении токена: {e}")
        return None

async def init_session():
    global CACHED_SNLM0E, session_initialized, web_client_session_id
    global browser_request_context_cache, browser_request_context_updated_at
    global web_model_catalog, web_model_catalog_updated_at

    print_sys("[*] Загрузка сессии из google_state.json...")
    GLOBAL_CLIENT.cookies.clear()
    GLOBAL_CLIENT.headers.pop("Authorization", None)
    CACHED_SNLM0E = None
    session_initialized = False
    web_client_session_id = str(uuid.uuid4()).upper()
    web_model_catalog = None
    web_model_catalog_updated_at = 0.0
    with browser_request_context_lock:
        browser_request_context_cache = {}
        browser_request_context_updated_at = 0.0
    
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
        
        cookies = get_gemini_session_cookies(state.get("cookies"))
        for cookie in cookies:
            GLOBAL_CLIENT.cookies.set(
                cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'],
            )
            if cookie['name'] == 'SAPISID':
                sapisid = cookie['value']
            if cookie['name'] == '__Secure-1PSID':
                has_base_cookie = True

        print_debug("Loaded session state", {
            "cookie_names": [cookie["name"] for cookie in cookies],
            "has_base_cookie": has_base_cookie,
            "has_sapisid": bool(sapisid),
            "has_saved_token": bool(load_saved_snlm0e(state)),
            "saved_token_updated_at": state.get(TOKEN_UPDATED_AT_KEY),
        })
                    
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

async def get_text_request_context(snlm0e):
    context = get_cached_browser_request_context(max_age_seconds=BROWSER_REQUEST_CONTEXT_TTL_SECONDS)
    if context:
        return CACHED_SNLM0E or snlm0e, context
    if can_refresh_token_from_cookies():
        refreshed = await get_snlm0e(force_refresh=True)
        if refreshed:
            return refreshed, get_cached_browser_request_context()
    return snlm0e, {}


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
                if IS_MOBILE:
                    print_sys("[!] Keep-alive: Не удалось обновить сессию. Импортируй свежий JSON-экспорт cookies через start.py --refresh.")
                    continue

                refreshed_session = await run_desktop_auto_refresh("Keep-alive")
                if refreshed_session:
                    restored_token = await get_snlm0e()
                    if restored_token:
                        print_sys("[+] Keep-alive: Сессия успешно восстановлена автоматическим refresh.")
                        continue

                print_sys("[!] Keep-alive: Автоматический refresh не помог. Запусти start.py --reauth и войди заново.")
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

@app.exception_handler(WebModelError)
async def web_model_error_handler(request: Request, exc: WebModelError):
    return JSONResponse(exc.payload(), status_code=exc.status_code)


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        token, _ = start_api_request_logging(request, "not-found")
        try:
            print_sys(f"\n[⚠️] ПРЕДУПРЕЖДЕНИЕ: Неизвестный запрос! Кто-то стучится на {request.method} {request.url.path}")
        finally:
            reset_request_log(token)
    return JSONResponse({"error": "Not found"}, status_code=exc.status_code)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def find_string_with_prefix(obj, prefix):
    if isinstance(obj, str):
        return obj if obj.startswith(prefix) else None
    if isinstance(obj, list):
        for item in obj:
            found = find_string_with_prefix(item, prefix)
            if found:
                return found
    elif isinstance(obj, dict):
        for value in obj.values():
            found = find_string_with_prefix(value, prefix)
            if found:
                return found
    return None


def extract_batch_response_payloads(response_text, rpc_id=None):
    payloads = []
    for line in response_text.splitlines():
        clean_line = line.strip()
        if not clean_line or clean_line.startswith(")]}'"):
            continue
        clean_line = re.sub(r'^\d+\s*', '', clean_line)
        if not clean_line.startswith('['):
            continue
        try:
            parsed_line = json.loads(clean_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed_line, list):
            continue
        for item in parsed_line:
            if isinstance(item, list) and len(item) > 2 and isinstance(item[2], str) and item[2]:
                if rpc_id is not None and (item[0] != "wrb.fr" or item[1] != rpc_id):
                    continue
                try:
                    payloads.append(json.loads(item[2]))
                except json.JSONDecodeError:
                    continue
    return payloads


def parse_web_model_catalog(response_text):
    # GetUserStatus carries the model list and the session's capacity flags.
    for payload in extract_batch_response_payloads(response_text, MODEL_CATALOG_RPC):
        if not isinstance(payload, list) or len(payload) < 16 or not isinstance(payload[15], list):
            continue
        tier_flags = payload[16] if len(payload) > 16 and isinstance(payload[16], list) else []
        capabilities = payload[17] if len(payload) > 17 and isinstance(payload[17], list) else []
        capacity_field = 12
        if 21 in tier_flags or 22 in tier_flags:
            capacity, capacity_field = (1 if 21 in tier_flags else 2), 13
        elif 115 in capabilities:
            capacity = 4
        elif 16 in tier_flags or 106 in capabilities:
            capacity = 3
        else:
            capacity = 2 if 8 in tier_flags or 19 in capabilities else 1

        models = {}
        for entry in payload[15]:
            if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
                continue
            model_type = next(
                (entry[i] for i in (17, 9) if len(entry) > i and type(entry[i]) is int),
                None,
            )
            if not entry[0] or model_type is None:
                continue
            # The versioned label may live in a different field from the localized picker title.
            for i in (19, 12, 11, 2, 1, 10):
                if len(entry) <= i or not isinstance(entry[i], str):
                    continue
                label = entry[i].strip().lower()
                if re.fullmatch(r"(?:gemini[ -]+)?\d+(?:\.\d+)?[ -]+[a-z][a-z0-9]*(?:[ -]+[a-z0-9]+)*", label):
                    model_name = re.sub(r" +", "-", label)
                    if not model_name.startswith("gemini-"):
                        model_name = f"gemini-{model_name}"
                    models[model_name] = WebTextModel(entry[0], model_type, capacity, capacity_field)
                    break
        return models
    return None


async def get_web_model_catalog(snlm0e, browser_context):
    global web_model_catalog, web_model_catalog_updated_at
    async with web_model_catalog_lock:
        if web_model_catalog is not None and time.monotonic() - web_model_catalog_updated_at < BROWSER_REQUEST_CONTEXT_TTL_SECONDS:
            return web_model_catalog
        session_id = web_client_session_id
        params = {
            "rpcids": MODEL_CATALOG_RPC, "source-path": "/app", "rt": "c",
            "_reqid": next_stream_generate_reqid(),
            "hl": browser_context.get("hl") or get_default_browser_hl(),
        }
        params.update({key: browser_context[key] for key in ("bl", "f.sid") if browser_context.get(key)})
        headers = {
            "Origin": GEMINI_ORIGIN,
            "Referer": f"{GEMINI_ORIGIN}/",
            "x-goog-ext-525001261-jspb": json.dumps(
                [1, None, None, None, None, None, None, None, [4, 5, 6, 8],
                 None, None, None, None, None, None, None, session_id],
                separators=(',', ':'),
            ),
            "x-goog-ext-73010989-jspb": "[0]",
        }
        try:
            response = await GLOBAL_CLIENT.post(
                f"{GEMINI_ORIGIN}/_/BardChatUi/data/batchexecute",
                params=params, headers=headers, timeout=15.0,
                data={"f.req": json.dumps([[[MODEL_CATALOG_RPC, "[]", None, "generic"]]]), "at": snlm0e},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print_sys(f"[!] Каталог моделей Gemini Web недоступен ({type(exc).__name__}).")
            return None
        models = parse_web_model_catalog(response.text)
        if models is None:
            print_sys("[!] Gemini Web не вернул каталог моделей для этой сессии.")
            return None
        if session_id != web_client_session_id:
            return None
        web_model_catalog = models
        web_model_catalog_updated_at = time.monotonic()
        return models


async def resolve_web_text_model(model_name, snlm0e, browser_context, force_extended):
    catalog = await get_web_model_catalog(snlm0e, browser_context)
    if catalog is not None and model_name in catalog and model_name not in LEGACY_TEXT_MODELS:
        return catalog[model_name], 1
    legacy_name = normalize_legacy_text_model(model_name, force_extended)
    legacy_model = LEGACY_TEXT_MODELS.get(legacy_name)
    if legacy_model:
        if catalog:
            legacy_model = catalog.get(legacy_name) or next(
                (model for model in catalog.values() if model.model_type == legacy_model.model_type),
                legacy_model,
            )
        return legacy_model, 2 if legacy_name.endswith("-extended") else 1
    if catalog is None:
        raise WebModelError("Could not load the Gemini Web model list. Check the server logs.", 502, "model_catalog_unavailable")
    raise WebModelError(f"Model '{model_name}' is not available in this Gemini Web session.", 404, "model_not_found")


async def recover_text_from_read_chat(snlm0e, chat_id, browser_context=None, attempts=4):
    if not chat_id:
        return None

    payload = json.dumps([chat_id, 10, None, 1, [1], [4], None, 1], separators=(',', ':'))
    headers = GLOBAL_CLIENT.headers.copy()
    headers["Origin"] = "https://gemini.google.com"
    headers["Referer"] = "https://gemini.google.com/"
    headers["X-Same-Domain"] = "1"
    headers["x-goog-ext-525001261-jspb"] = "[1,null,null,null,null,null,null,null,[4]]"
    headers["x-goog-ext-73010989-jspb"] = "[0]"

    for attempt in range(1, attempts + 1):
        params = {"rpcids": "hNvQHb", "_reqid": next_stream_generate_reqid(), "rt": "c"}
        if isinstance(browser_context, dict):
            if browser_context.get("bl"):
                params["bl"] = browser_context["bl"]
            if browser_context.get("f.sid"):
                params["f.sid"] = browser_context["f.sid"]

        url = f"https://gemini.google.com/_/BardChatUi/data/batchexecute?{httpx.QueryParams(params)}"
        req_data = {
            "f.req": json.dumps([[ ["hNvQHb", payload, None, "generic"] ]], separators=(',', ':')),
            "at": snlm0e,
        }
        print_debug("READ_CHAT recovery request", {
            "attempt": attempt,
            "chat_id": chat_id,
            "url": url,
            "headers": sanitize_headers(headers),
            "form": req_data,
        }, max_len=12000)
        try:
            resp = await GLOBAL_CLIENT.post(url, data=req_data, headers=headers, timeout=30.0)
            print_debug("READ_CHAT recovery response", {
                "attempt": attempt,
                "status_code": resp.status_code,
                "headers": sanitize_headers(resp.headers),
                "body_preview": resp.text[:6000],
            }, max_len=16000)
            if resp.status_code == 200:
                for payload_obj in extract_batch_response_payloads(resp.text):
                    recovered_text = find_actual_response(payload_obj)
                    if recovered_text:
                        return recovered_text
        except Exception as e:
            print_debug("READ_CHAT recovery exception", {"attempt": attempt, "error": repr(e)})

        if attempt < attempts:
            await asyncio.sleep(min(15 * attempt, 45))
    return None

async def upload_document_to_gemini(text_content, filename="conversation-transcript.txt", mime_type="text/plain"):
    if IS_DEBUG: print_sys(f"[DEBUG] Выгрузка файла истории {filename} на сервера Google...")
    url = "https://content-push.googleapis.com/upload/"
    file_bytes = text_content.encode('utf-8')
    
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
    print_debug("Document upload start request", {
        "url": url,
        "filename": filename,
        "mime_type": mime_type,
        "file_size_bytes": len(file_bytes),
        "headers": sanitize_headers(headers_start),
    })
    print_debug("Document upload content", {
        "filename": filename,
        "mime_type": mime_type,
        "file_size_bytes": len(file_bytes),
        "content_chars": len(text_content),
        "content_head": text_content[:20000],
        "content_tail": text_content[-20000:] if len(text_content) > 20000 else "",
    }, max_len=50000)
    
    try:
        res = await GLOBAL_CLIENT.post(url, headers=headers_start, content=b"", timeout=15.0)
        print_debug("Document upload start response", {
            "status_code": res.status_code,
            "headers": sanitize_headers(res.headers),
            "body_preview": res.text[:4000],
        }, max_len=12000)
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
        print_debug("Document upload finalize request", {
            "upload_url": upload_url,
            "headers": sanitize_headers(headers_upload),
            "file_size_bytes": len(file_bytes),
        })
        res_upload = await GLOBAL_CLIENT.post(upload_url, headers=headers_upload, content=file_bytes, timeout=30.0)
        print_debug("Document upload finalize response", {
            "status_code": res_upload.status_code,
            "headers": sanitize_headers(res_upload.headers),
            "body_preview": res_upload.text[:4000],
        }, max_len=12000)
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

INTERNAL_GOOGLE_THINKING_PREFIXES = (
    "Constructing the Scene", "Analyzing Scene Flow", "Composing Sensory Details",
    "Validating Output Criteria", "Refining Character Response", "Observing Seraphim",
    "Verifying Formatting", "Assessing Tactical", "Composing the Scene",
    "Refining the Output", "Finalizing the Scene", "Expanding the Scene",
    "Evaluating the Narrative", "Assessing the Reaction", "Composing the Response",
    "Refining the Russian", "Defining the Objective", "Prioritizing Sensory Details",
    "Initiating the Analysis", "Developing the Continuation", "Refining the Response",
    "Defining the Immediate Action", "Intensifying the Focus", "Inspecting the Details",
    "Verifying the Output", "Assessing the Scenario", "Defining the Setting",
    "Developing the Conflict", "Analyzing the Tension", "Refining Scene Detail",
    "Examining Sensory Details",
)


def strip_markdown_heading_markup(text):
    if not isinstance(text, str):
        return ""
    return text.strip().strip("*#").strip()


def is_internal_google_thinking_heading(line):
    raw_line = line.strip() if isinstance(line, str) else ""
    heading = strip_markdown_heading_markup(raw_line)
    if not heading:
        return False

    for prefix in INTERNAL_GOOGLE_THINKING_PREFIXES:
        if heading.startswith(prefix):
            return True

    has_heading_markup = raw_line.startswith(("**", "#"))
    is_short_title = (
        len(heading) <= 80
        and not re.search(r'[.!?,;:]', heading)
        and heading[:1].isupper()
        and heading == heading.title()
    )
    if not has_heading_markup and not is_short_title:
        return False

    return bool(re.match(
        r'^(Initiating|Developing|Refining|Defining|Analyzing|Assessing|Composing|'
        r'Finalizing|Verifying|Inspecting|Prioritizing|Examining|Intensifying|'
        r'Constructing|Evaluating)\b',
        heading,
    ))


def looks_like_google_internal_thinking(text):
    if not isinstance(text, str) or not text.strip():
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    if is_internal_google_thinking_heading(lines[0]):
        return True

    sample = text[:2500]
    bold_heading_count = len(re.findall(r'(?m)^\s*\*\*[^*\n]{3,90}\*\*\s*$', sample))
    first_person_planning_count = len(re.findall(r"\b(?:I'm|I am|I've|I have)\b", sample))
    if bold_heading_count >= 2 and first_person_planning_count >= 2:
        return True

    lowered = sample.lower()
    return (
        bold_heading_count >= 1
        and "chat.json" in lowered
        and ("source of truth" in lowered or "scenario" in lowered)
    )


def is_garbage_node(text):
    if not isinstance(text, str): return False
    stripped = text.strip()
    if stripped.startswith('http') or stripped.startswith('//') or stripped.startswith('c_') or stripped.startswith('r_') or stripped.startswith('rc_'): return True
    if re.match(r'^\$[A-Za-z0-9+/_=-]{40,}$', stripped): return True
    if re.match(r'^[\w.-]+\.(?:json|txt|md|csv|pdf|png|jpe?g|webp|gif)$', stripped, re.IGNORECASE): return True
    if re.match(r'^[a-z]+/[a-z0-9.+-]+$', stripped, re.IGNORECASE): return True
    if re.match(r'^[A-Z]{2,}(?:_[A-Z0-9]+)+$', stripped): return True
    if re.match(r'^[A-Z]{2}$', stripped): return True
    
    # ИСПРАВЛЕНО: Теперь скрипт учитывает и обычный пробел, и брайлевский невидимый пробел (U+2800)
    if len(text) > 400 and " " not in text and "⠀" not in text: return True
    
    if re.match(r'^[A-Za-z0-9_/\+\-]{40,}={0,2}', text): return True
        
    if len(text) < 300 and looks_like_google_internal_thinking(text):
        return True
    return False

def is_internal_google_think_block(think_block, tag_name):
    if not isinstance(think_block, str) or not think_block.strip():
        return False

    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"
    think_content = think_block.strip()

    if think_content.startswith(open_tag) and think_content.endswith(close_tag):
        think_content = think_content[len(open_tag):-len(close_tag)]

    return starts_with_internal_google_thinking(think_content)

def starts_with_internal_google_thinking(text):
    if not isinstance(text, str) or not text.strip():
        return False

    first_nonempty_line = ""
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            first_nonempty_line = stripped_line
            break

    if not first_nonempty_line:
        return False

    return is_internal_google_thinking_heading(first_nonempty_line)


def response_candidate_rank(text):
    if not isinstance(text, str) or not text.strip() or is_garbage_node(text):
        return (-1000000, 0)

    stripped = text.strip()
    score = 0

    if looks_like_google_internal_thinking(stripped):
        score -= 10000

    if "<think" in stripped.lower() and not is_internal_google_think_block(stripped, "think"):
        score += 250
    if "◈NORICORE◈" in stripped:
        score += 500
    if re.search(r'[А-Яа-яЁё]', stripped):
        score += 40
    if re.search(r'[.!?。！？][\s"\')\]]', stripped):
        score += 30

    score += min(len(stripped), 12000) // 12
    if len(stripped) < 30:
        score -= 100

    return (score, len(stripped))


def is_better_response_candidate(candidate, current):
    if not candidate:
        return False
    candidate_rank = response_candidate_rank(candidate)
    if candidate_rank[0] <= -1000000:
        return False
    if not current:
        return True
    return candidate_rank > response_candidate_rank(current)

def find_actual_response(obj):
    if isinstance(obj, str):
        if is_garbage_node(obj): return ""
        return obj
    best = ""
    if isinstance(obj, list):
        for item in obj:
            candidate = find_actual_response(item)
            if is_better_response_candidate(candidate, best): best = candidate
    elif isinstance(obj, dict):
        for val in obj.values():
            candidate = find_actual_response(val)
            if is_better_response_candidate(candidate, best): best = candidate
    return best


def extract_stream_candidate_text(payload):
    # StreamGenerate sends cumulative text in the first candidate, alongside thought summaries and metadata.
    text = payload
    for index in (4, 0, 1, 0):
        if not isinstance(text, list) or len(text) <= index:
            return ""
        text = text[index]
    return text if isinstance(text, str) and text.strip() else ""


def normalize_thinking_tags(text, tag_name):
    if not isinstance(text, str):
        return ""

    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"
    text = re.sub(r'(?i)<think>|<thinking>', open_tag, text)
    text = re.sub(r'(?i)</think>|</thinking>', close_tag, text)
    return text


THINKING_TEMPLATE_LINES = {
    "<think_template>",
    "</think_template>",
    "<thinking_template>",
    "</thinking_template>",
}


def strip_thinking_template_lines(text):
    if not isinstance(text, str) or not text:
        return ""

    return "".join(
        line for line in text.splitlines(keepends=True)
        if line.strip() not in THINKING_TEMPLATE_LINES
    )

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

def strip_leading_internal_google_think_blocks(text, tag_name):
    if not isinstance(text, str) or not text:
        return ""

    remaining_text = text

    while True:
        think_block, body_text, _ = extract_leading_think_block(remaining_text, tag_name)
        if not think_block:
            return remaining_text

        if not is_internal_google_think_block(think_block, tag_name):
            return remaining_text

        remaining_text = body_text.lstrip('\n\r \t')

def strip_leading_internal_google_prefill_think_content(text, tag_name):
    if not isinstance(text, str) or not text:
        return ""

    close_tag = f"</{tag_name}>"
    close_tag_index = text.find(close_tag)
    if close_tag_index < 0:
        return text

    think_content = text[:close_tag_index]
    if not starts_with_internal_google_thinking(think_content):
        return text

    return text[close_tag_index + len(close_tag):].lstrip('\n\r \t')

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
    open_tag = f"<{tag_name}>"
    close_tag = f"</{tag_name}>"

    final_text = strip_thinking_template_lines(normalize_thinking_tags(generated_text, tag_name))
    normalized_prefill = strip_thinking_template_lines(normalize_thinking_tags(prefill_text, tag_name)).strip()
    preserve_prefill_newline = normalized_prefill.endswith('>')
    prefill_opens_think_block = normalized_prefill.endswith(open_tag)
    final_text = trim_prefill_echo(final_text, normalized_prefill)
    if prefill_opens_think_block:
        final_text = strip_leading_internal_google_prefill_think_content(final_text, tag_name)
        if close_tag in final_text and not final_text.lstrip().startswith(open_tag):
            final_text = f"{open_tag}\n{final_text.lstrip()}"
    final_text = strip_leading_internal_google_think_blocks(final_text, tag_name)

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

async def generate_text_core(
    request: Request,
    prompt,
    model_name="nano-banana-pro",
    file_content=None,
    file_name=None,
    file_mime_type="text/plain",
    allow_token_refresh_retry=True,
    allow_desktop_refresh_retry=True,
    allow_bad_request_retry=True,
    force_extended=False,
):
    request_debug_id = f"text-{uuid.uuid4().hex[:8]}"

    print_sys("🔑 [ЭТАП 1] Проверка токена и выбор модели...")
    snlm0e, allow_token_refresh_retry, allow_desktop_refresh_retry = await get_or_recover_request_snlm0e(
        "Текстовый запрос",
        allow_token_refresh_retry=allow_token_refresh_retry,
        allow_desktop_refresh_retry=allow_desktop_refresh_retry,
    )
    if not snlm0e: 
        return None

    snlm0e, browser_request_context = await get_text_request_context(snlm0e)
    model, thinking_level = await resolve_web_text_model(
        model_name, snlm0e, browser_request_context, force_extended,
    )

    print_sys("📄 [ЭТАП 2] Подготовка данных...")
    file_data = None
    if file_content:
        file_hash = hashlib.sha256(file_content.encode("utf-8")).hexdigest()[:12]
        filename = file_name or f"conversation-transcript-{request_debug_id}-{file_hash}.txt"
        doc_id = await upload_document_to_gemini(file_content, filename=filename, mime_type=file_mime_type)
        if doc_id:
            file_data = build_attachment_file_data(doc_id, filename=filename, mime_type=file_mime_type)
        else:
            print_sys("⚠️ Предупреждение: Не удалось прикрепить transcript-файл истории. Генерация продолжится без него.")

    request_uuid = str(uuid.uuid4()).upper()
    stream_generate_reqid = next_stream_generate_reqid()
    stream_url = build_stream_generate_url(browser_request_context, reqid=stream_generate_reqid)
    language = browser_request_context.get("hl") or get_default_browser_hl()

    payload_str = build_stream_generate_payload(
        prompt, file_data, request_uuid, language, IS_TEMP_CHAT, model.model_type, thinking_level,
    )
    req_data = {"f.req": json.dumps([None, payload_str], separators=(',', ':')), "at": snlm0e}

    req_headers = build_stream_generate_headers(
        GLOBAL_CLIENT.headers, model.mode_id, request_uuid,
        model_type=model.model_type, thinking_level=thinking_level,
        capacity=model.capacity, capacity_field=model.capacity_field, client_session_id=web_client_session_id,
    )
    print_debug(f"{request_debug_id} prepared text request", {
        "model_name": model_name,
        "mode_id": model.mode_id,
        "request_uuid": request_uuid,
        "stream_generate_reqid": stream_generate_reqid,
        "browser_request_context": browser_request_context,
        "language": language,
        "temp_chat": IS_TEMP_CHAT,
        "prompt_preview": prompt,
        "doc_attached": file_content is not None,
        "file_data_preview": file_data,
        "url": stream_url,
        "headers": sanitize_headers(req_headers),
        "form": req_data,
    }, max_len=20000)

    print_sys(f"📡 [ЭТАП 3] Отправка запроса в Google (Модель: {model_name})...")
    
    spinner = asyncio.create_task(spinner_task("Гугл думает над ответом..."))
    recovery_chat_id = None
    
    try:
        full_text = ""
        raw_line_count = 0
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            print_debug(f"{request_debug_id} stream response headers", {
                "status_code": resp.status_code,
                "headers": sanitize_headers(resp.headers),
            }, max_len=12000)
            
            if resp.status_code != 200:
                error_body = (await resp.aread()).decode("utf-8", errors="replace")
                print_debug(f"{request_debug_id} non-200 stream body", error_body, max_len=20000)
                spinner.cancel()
                print_sys(f"[❌] ОШИБКА GOOGLE API: Сервер вернул статус HTTP {resp.status_code}")
                if resp.status_code == 400 and allow_bad_request_retry and can_refresh_token_from_cookies():
                    refreshed = await get_snlm0e(force_refresh=True)
                    if refreshed and refreshed != snlm0e:
                        return await generate_text_core(
                            request, prompt, model_name=model_name, file_content=file_content,
                            file_name=file_name, file_mime_type=file_mime_type,
                            allow_token_refresh_retry=allow_token_refresh_retry,
                            allow_desktop_refresh_retry=allow_desktop_refresh_retry,
                            allow_bad_request_retry=False,
                            force_extended=force_extended,
                        )
                if resp.status_code in (401, 403):
                    recovered, next_token_retry, next_desktop_refresh_retry = await recover_request_snlm0e(
                        "Текстовый запрос",
                        allow_token_refresh_retry=allow_token_refresh_retry,
                        allow_desktop_refresh_retry=allow_desktop_refresh_retry,
                    )
                    if recovered:
                        return await generate_text_core(
                            request,
                            prompt,
                            model_name=model_name,
                            file_content=file_content,
                            file_name=file_name,
                            file_mime_type=file_mime_type,
                            allow_token_refresh_retry=next_token_retry,
                            allow_desktop_refresh_retry=next_desktop_refresh_retry,
                            allow_bad_request_retry=allow_bad_request_retry,
                            force_extended=force_extended,
                        )
                return None
                
            async for line in resp.aiter_lines():
                if await request.is_disconnected():
                    spinner.cancel()
                    print_sys("🛑 [ПРЕРВАНО] Клиент (Таверна) отменил запрос (нажата кнопка Stop). Разрываем соединение.")
                    return None
                
                if line:
                    raw_line_count += 1
                    print_debug_throttled(f"{request_debug_id}:raw-stream-line", f"{request_debug_id} raw stream line #{raw_line_count}", line, max_len=12000)
                    try:
                        clean_line = re.sub(r'^\d+\s*', '', line)
                        if clean_line.startswith('['):
                            parsed_data = json.loads(clean_line)
                            print_debug_throttled(f"{request_debug_id}:parsed-stream-item", f"{request_debug_id} parsed stream item #{raw_line_count}", parsed_data, max_len=16000)
                            if not isinstance(parsed_data, list):
                                continue
                            for item in parsed_data:
                                if not isinstance(item, list) or len(item) < 3 or item[0] != "wrb.fr" or not item[2]:
                                    continue
                                inner_data = json.loads(item[2])
                                recovery_chat_id = recovery_chat_id or find_string_with_prefix(inner_data, "c_")
                                extracted = extract_stream_candidate_text(inner_data)
                                if extracted:
                                    full_text = extracted
                    except Exception as e:
                        print_debug(f"{request_debug_id} stream parse exception #{raw_line_count}", repr(e))
                        continue
        
        spinner.cancel()
        print_sys("✅ [ЭТАП 4] Поток завершен. Анализ результата...")
        print_debug(f"{request_debug_id} final raw extracted text", {
            "raw_line_count": raw_line_count,
            "length": len(full_text),
            "preview": full_text[:6000],
        }, max_len=16000)
        
        if not full_text:
            print_sys("[❌] ОШИБКА: Гугл вернул абсолютно пустой текст!")
            recovered_text = await recover_text_from_read_chat(snlm0e, recovery_chat_id, browser_request_context)
            if recovered_text:
                print_sys("[+] Ответ восстановлен через READ_CHAT после пустого потока.")
                return recovered_text
            return None
            
        print_sys(f"[+] Сырой текст успешно извлечен (Длина: {len(full_text)} символов).")
        clean_text = re.sub(r'(?m)^\s*\\\s*$', '', full_text)
        clean_text = clean_text.replace('\\<', '<').replace('\\>', '>').replace('\\/', '/')
        clean_text = clean_text.strip()
        print_debug(f"{request_debug_id} cleaned text", {
            "length": len(clean_text),
            "preview": clean_text[:6000],
        }, max_len=16000)

        if not clean_text:
            print_sys("[❌] ОШИБКА: После очистки ответ от Google оказался пустым!")
            return None

        return clean_text
    except asyncio.CancelledError:
        print_sys("🛑 [ОТМЕНЕНО] Генерация текста принудительно остановлена.")
        raise
    except WebModelError:
        raise
    except httpx.ReadTimeout:
        spinner.cancel()
        print_sys("[❌] ОШИБКА: Тайм-аут. Гугл думал слишком долго (более 150 сек).")
        recovered_text = await recover_text_from_read_chat(snlm0e, recovery_chat_id, browser_request_context)
        if recovered_text:
            print_sys("[+] Ответ восстановлен через READ_CHAT после тайм-аута потока.")
            return recovered_text
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
    print_debug("Image upload start request", {
        "url": url,
        "mime_type": mime_type,
        "image_size_bytes": len(image_bytes),
        "headers": sanitize_headers(headers_start),
    })
    try:
        res = await GLOBAL_CLIENT.post(url, headers=headers_start, content=b"", timeout=15.0)
        print_debug("Image upload start response", {
            "status_code": res.status_code,
            "headers": sanitize_headers(res.headers),
            "body_preview": res.text[:4000],
        }, max_len=12000)
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
        print_debug("Image upload finalize request", {
            "upload_url": upload_url,
            "headers": sanitize_headers(headers_upload),
            "image_size_bytes": len(image_bytes),
        })
        res_upload = await GLOBAL_CLIENT.post(upload_url, headers=headers_upload, content=image_bytes, timeout=30.0)
        print_debug("Image upload finalize response", {
            "status_code": res_upload.status_code,
            "headers": sanitize_headers(res_upload.headers),
            "body_preview": res_upload.text[:4000],
        }, max_len=12000)
        if res_upload.status_code == 200:
            return res_upload.text.strip(), mime_type, ext
        print_sys(f"[❌] Ошибка загрузки изображения (Финализация): HTTP {res_upload.status_code}")
    except Exception as e:
        print_sys(f"[❌] Исключение при загрузке изображения: {e}")
    return None, None, None

async def download_blob_via_batchexecute(snlm0e, blob, chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=True, allow_desktop_refresh_retry=True):
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=c8o8Fe&rt=c"
    dummy_id = "r2h8onr2h8onr2h8"
    inner_json = f"""[[[null,null,null,[null,null,null,null,null,{json.dumps(blob)}]],["http://googleusercontent.com/image_generation_content/0",0],null,[19,{json.dumps(prompt)}],null,null,null,null,null,"{dummy_id}"],[{json.dumps(r_id)},{json.dumps(rc_id)},{json.dumps(chat_id)},null,"{dummy_id}"],1,0]"""
    req_data = {"f.req": json.dumps([[["c8o8Fe", inner_json, None, "generic"]]], separators=(',', ':')), "at": snlm0e}
    print_debug("Blob download request", {
        "url": url,
        "chat_id": chat_id,
        "r_id": r_id,
        "rc_id": rc_id,
        "headers": sanitize_headers(GLOBAL_CLIENT.headers),
        "form": req_data,
    }, max_len=16000)
    try:
        resp = await GLOBAL_CLIENT.post(url, data=req_data, timeout=15.0)
        print_debug("Blob download response", {
            "status_code": resp.status_code,
            "headers": sanitize_headers(resp.headers),
            "body_preview": resp.text[:4000],
        }, max_len=12000)
        if resp.status_code != 200:
            print_sys(f"[❌] Ошибка получения blob-картинки: HTTP {resp.status_code}")
            if resp.status_code in [400, 401, 403]:
                recovered, next_token_retry, next_desktop_refresh_retry = await recover_request_snlm0e(
                    "Получение blob-картинки",
                    allow_token_refresh_retry=allow_token_refresh_retry,
                    allow_desktop_refresh_retry=allow_desktop_refresh_retry,
                )
                if recovered:
                    return await download_blob_via_batchexecute(
                        recovered,
                        blob,
                        chat_id,
                        r_id,
                        rc_id,
                        prompt,
                        allow_token_refresh_retry=next_token_retry,
                        allow_desktop_refresh_retry=next_desktop_refresh_retry,
                    )
            return None
        urls = re.findall(r'(https://lh3\.googleusercontent\.com/[a-zA-Z0-9_/\-\=]+)', resp.text)
        if urls:
            return urls[-1]
        print_sys("[❌] Blob-ответ получен, но URL картинки в нем не найден.")
    except Exception as e:
        print_sys(f"[❌] Исключение при получении blob-картинки: {e}")
    return None

async def generate_image_core(request: Request, prompt, reference_images_b64=None, model_name="nano-banana-pro", allow_token_refresh_retry=True, allow_desktop_refresh_retry=True):
    request_debug_id = f"image-{uuid.uuid4().hex[:8]}"
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

    snlm0e, allow_token_refresh_retry, allow_desktop_refresh_retry = await get_or_recover_request_snlm0e(
        "Генерация картинки",
        allow_token_refresh_retry=allow_token_refresh_retry,
        allow_desktop_refresh_retry=allow_desktop_refresh_retry,
    )
    if not snlm0e: return None

    stream_url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c"
    device_id = str(uuid.uuid4()).upper()
    candidate_1 = uuid.uuid4().hex
    request_uuid = str(uuid.uuid4()).upper()
    
    is_pro_model = "pro" in model_name.lower()
    is_extended = "extended" in model_name.lower()
    mode_id = "e6fa609c3fa255c0" if is_pro_model else "56fdd199312815e2"
    model_type = 3 if is_pro_model else 1
    thinking_level = 2 if is_extended else 1
    
    req_headers = build_stream_generate_headers(GLOBAL_CLIENT.headers, mode_id, request_uuid, model_type=model_type, thinking_level=thinking_level)
    
    if is_pro_model:
        msg_block = f'{json.dumps(prompt)},0,null,{image_part},null,null,0,null,null,[null,null,null,null,null,null,[null,[1]]]'
    else:
        msg_block = f'{json.dumps(prompt)},0,null,{image_part},null,null,0'
        
    temp_chat_flag = "1" if IS_TEMP_CHAT else "null"
        
    payload_1_str = f"""[[{msg_block}],["ru"],["","","",null,null,null,null,null,null,""],"",{json.dumps(candidate_1)},null,[1],1,null,null,1,0,null,null,null,null,null,[[0]],0,null,null,null,null,null,null,null,null,1,null,null,[4],null,1,null,null,null,null,null,null,null,null,[1],null,null,null,{temp_chat_flag},null,null,null,null,null,null,null,0,null,null,null,null,null,{json.dumps(device_id)},null,[],null,null,null,null,null,null,2]"""
    req_data = {"f.req": json.dumps([None, payload_1_str], separators=(',', ':')), "at": snlm0e}
    print_debug(f"{request_debug_id} image stage1 request", {
        "model_name": model_name,
        "mode_id": mode_id,
        "candidate_1": candidate_1,
        "device_id": device_id,
        "reference_count": len(reference_images_b64 or []),
        "prompt_preview": prompt,
        "url": stream_url,
        "headers": sanitize_headers(req_headers),
        "form": req_data,
    }, max_len=20000)
    
    raw_1 = ""
    
    spinner = asyncio.create_task(spinner_task("Рисуем картинку (Этап 1)..."))
    try:
        async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_data, headers=req_headers, timeout=150.0) as resp:
            print_debug(f"{request_debug_id} image stage1 response headers", {
                "status_code": resp.status_code,
                "headers": sanitize_headers(resp.headers),
            }, max_len=12000)
            if resp.status_code != 200:
                error_body = (await resp.aread()).decode("utf-8", errors="replace")
                print_debug(f"{request_debug_id} image stage1 non-200 body", error_body, max_len=20000)
                print_sys(f"[❌] ОШИБКА GOOGLE API (Картинки, этап 1): HTTP {resp.status_code}")
                if resp.status_code in [400, 401, 403]:
                    recovered, next_token_retry, next_desktop_refresh_retry = await recover_request_snlm0e(
                        "Генерация картинки (этап 1)",
                        allow_token_refresh_retry=allow_token_refresh_retry,
                        allow_desktop_refresh_retry=allow_desktop_refresh_retry,
                    )
                    if recovered:
                        return await generate_image_core(
                            request,
                            prompt,
                            reference_images_b64=reference_images_b64,
                            model_name=model_name,
                            allow_token_refresh_retry=next_token_retry,
                            allow_desktop_refresh_retry=next_desktop_refresh_retry,
                        )
                return None
            async for line in resp.aiter_lines():
                if request and await request.is_disconnected():
                    print_sys("🛑 [ПРЕРВАНО] Клиент отменил генерацию картинки на 1 этапе.")
                    return None
                if line:
                    print_debug_throttled(f"{request_debug_id}:image-stage1-raw-line", f"{request_debug_id} image stage1 raw line", line, max_len=12000)
                    raw_1 += line + "\n"
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
    print_debug(f"{request_debug_id} image stage1 parsed summary", {
        "raw_length": len(raw_1),
        "raw_preview": raw_1[:6000],
        "url_count": len(urls),
        "blob_count": len(blobs),
    }, max_len=16000)
    
    chat_id_m = re.search(r'(c_[a-f0-9]{16})', raw_1)
    r_id_m = re.search(r'(r_[a-f0-9]{16,32})', raw_1)
    rc_id_m = re.search(r'(rc_[a-f0-9]{16,32})', raw_1)
    chat_id = chat_id_m.group(1) if chat_id_m else ""
    r_id = r_id_m.group(1) if r_id_m else ""
    rc_id = rc_id_m.group(1) if rc_id_m else ""
    
    final_url = None
    
    if urls or blobs:
        print_sys("[+] Картинка успешно сгенерирована на 1 этапе!")
        final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=allow_token_refresh_retry, allow_desktop_refresh_retry=allow_desktop_refresh_retry) if blobs else None)
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
            print_debug(f"{request_debug_id} image stage2 request", {
                "chat_id": chat_id,
                "state_token": state_token,
                "candidate_2": candidate_2,
                "device_id": device_id,
                "url": stream_url,
                "headers": sanitize_headers(req_headers),
                "form": req_2,
            }, max_len=20000)
            
            raw_target = ""
            spinner_2 = asyncio.create_task(spinner_task("Улучшаем качество (Этап 2)..."))
            try:
                async with GLOBAL_CLIENT.stream("POST", stream_url, data=req_2, headers=req_headers, timeout=150.0) as resp:
                    print_debug(f"{request_debug_id} image stage2 response headers", {
                        "status_code": resp.status_code,
                        "headers": sanitize_headers(resp.headers),
                    }, max_len=12000)
                    if resp.status_code != 200:
                        error_body = (await resp.aread()).decode("utf-8", errors="replace")
                        print_debug(f"{request_debug_id} image stage2 non-200 body", error_body, max_len=20000)
                        print_sys(f"[❌] ОШИБКА GOOGLE API (Картинки, этап 2): HTTP {resp.status_code}")
                        if resp.status_code in [400, 401, 403]:
                            recovered, next_token_retry, next_desktop_refresh_retry = await recover_request_snlm0e(
                                "Генерация картинки (этап 2)",
                                allow_token_refresh_retry=allow_token_refresh_retry,
                                allow_desktop_refresh_retry=allow_desktop_refresh_retry,
                            )
                            if recovered:
                                return await generate_image_core(
                                    request,
                                    prompt,
                                    reference_images_b64=reference_images_b64,
                                    model_name=model_name,
                                    allow_token_refresh_retry=next_token_retry,
                                    allow_desktop_refresh_retry=next_desktop_refresh_retry,
                                )
                        return None
                    async for line in resp.aiter_lines():
                        if request and await request.is_disconnected():
                            print_sys("🛑 [ПРЕРВАНО] Клиент отменил генерацию картинки на 2 этапе.")
                            return None
                        if line:
                            print_debug_throttled(f"{request_debug_id}:image-stage2-raw-line", f"{request_debug_id} image stage2 raw line", line, max_len=12000)
                            raw_target += line + "\n"
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
            print_debug(f"{request_debug_id} image stage2 parsed summary", {
                "raw_length": len(raw_target),
                "raw_preview": raw_target[:6000],
                "url_count": len(urls),
                "blob_count": len(blobs),
            }, max_len=16000)
            final_url = urls[-1] if urls else (await download_blob_via_batchexecute(snlm0e, blobs[-1], chat_id, r_id, rc_id, prompt, allow_token_refresh_retry=allow_token_refresh_retry, allow_desktop_refresh_retry=allow_desktop_refresh_retry) if blobs else None)
    
    if final_url:
        final_url = re.sub(r'=[swh]\d+.*$', '', final_url)
        high_res_url = f"{final_url}=s0"
        try:
            img_r = await GLOBAL_CLIENT.get(high_res_url)
            print_debug(f"{request_debug_id} final image download", {
                "url": high_res_url,
                "status_code": img_r.status_code,
                "headers": sanitize_headers(img_r.headers),
                "content_length": len(img_r.content),
            }, max_len=12000)
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
    normalized = str(model_name or "").strip().lower()

    if normalized.startswith("/v1beta/models/"):
        normalized = normalized[len("/v1beta/models/"):]
    # Допускаем повторяющийся `models/` префикс — некоторые клиенты шлют `models/gemini-...`,
    # после которого ST/SDK ещё раз добавляют `models/` в URL.
    while normalized.startswith("models/"):
        normalized = normalized[len("models/"):]
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0]
    return normalized


def normalize_legacy_text_model(model_name, force_extended=False):
    if model_name == "nano-banana-pro":
        return "gemini-3.1-pro-preview"
    if model_name == "nano-banana-2":
        return "gemini-3.5-flash"

    is_thinking = force_extended or any(token in model_name for token in ("extended", "thinking", "think"))

    if model_name.startswith("gemini-") and "pro" in model_name:
        return "gemini-3.1-pro-extended" if is_thinking else "gemini-3.1-pro-preview"
    if model_name.startswith("gemini-") and "flash" in model_name:
        return "gemini-3.5-flash-extended" if is_thinking else "gemini-3.5-flash"
    return model_name

def coerce_message_content_to_text(content):
    """Превращает любое поле `content` (OpenAI-стиля) в плоскую строку.

    OpenAI допускает массив частей (`[{"type":"text","text":"..."}, {"type":"image_url",...}]`)
    и `null` для tool-сообщений. Если оставить такие значения как есть, Gemini увидит в chat.json
    структурированный объект вместо текста и фактически проигнорирует это сообщение, из-за чего
    последнее сообщение юзера/префилл «пропадает» из контекста.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            # Картинки/файлы — фиксируем плейсхолдером, чтобы сообщение не выпадало целиком.
            if item.get("type") == "image_url" or "image_url" in item:
                parts.append("[image attachment]")
            elif item.get("type") == "input_audio" or "audio_url" in item:
                parts.append("[audio attachment]")
            elif "inline_data" in item or "inlineData" in item:
                parts.append("[inline attachment]")
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return ""
    return str(content)


def extract_system_instruction_text(system_instruction):
    """Парсит `systemInstruction` из Gemini API формата в плоский текст."""
    if not system_instruction:
        return ""
    if isinstance(system_instruction, str):
        return system_instruction.strip()
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts")
        text_chunks = []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        text_chunks.append(text)
                elif isinstance(part, str) and part:
                    text_chunks.append(part)
        # Бывает и плоский { "text": "..." } без parts.
        flat_text = system_instruction.get("text")
        if isinstance(flat_text, str) and flat_text:
            text_chunks.append(flat_text)
        return "\n".join(text_chunks).strip()
    if isinstance(system_instruction, list):
        return "\n".join(
            extract_system_instruction_text(item) for item in system_instruction
        ).strip()
    return ""


def build_chat_history_from_gemini_contents(contents):
    chat_history = []

    for item in contents:
        if not isinstance(item, dict):
            continue

        role = item.get("role", "user")
        normalized_role = "assistant" if role == "model" else "user"
        parts = item.get("parts", [])
        text_parts = []
        has_attachment = False

        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
                    continue
                if "inline_data" in part or "inlineData" in part:
                    has_attachment = True
                elif "file_data" in part or "fileData" in part:
                    has_attachment = True

        combined_text = "\n".join(text_parts).strip()
        if not combined_text and has_attachment:
            # Не теряем сообщение, если оно состоит только из вложения —
            # ставим плейсхолдер, чтобы последнее user-сообщение не исчезло из chat.json.
            combined_text = "[attachment]"

        if combined_text:
            chat_history.append({
                "role": normalized_role,
                "content": combined_text
            })

    prefill_text = ""
    if chat_history and chat_history[-1].get("role") == "assistant":
        prefill_text = str(chat_history[-1].get("content", "") or "").strip()

    return chat_history, prefill_text

def has_google_search_tool(data):
    tools = data.get("tools", []) if isinstance(data, dict) else []
    if not isinstance(tools, list):
        return False

    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("google_search"), dict):
            return True

    return False


def truncate_prompt_excerpt(text, limit=3500):
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


CONTROL_XML_BLOCK_TAGS = (
    "anti_loop_system",
    "explicit_content_protocol",
    "explicit_extreme_protocol",
    "focus",
    "genre_slowburn",
    "infoblock_protocol",
    "instructions",
    "internal_test",
    "language",
    "length",
    "literary_quality",
    "narrative_engine",
    "narrative_momentum",
    "narrative_perspective",
    "professional_standards",
    "response_structure",
    "sensory_enhancement",
    "task",
    "test_mode",
    "think_template",
    "user_control",
)


def strip_control_blocks_from_message(text):
    if not isinstance(text, str) or not text:
        return ""

    stripped = text
    for tag in CONTROL_XML_BLOCK_TAGS:
        stripped = re.sub(
            rf'(?is)<{re.escape(tag)}\b[^>]*>.*?</{re.escape(tag)}>',
            '',
            stripped,
        )

    stripped = re.sub(r'(?im)^\s*```.*?^\s*```\s*', '', stripped)
    stripped = re.sub(r'\n{3,}', '\n\n', stripped)
    return stripped.strip()


def extract_open_think_prefill(text):
    if not isinstance(text, str) or not text.strip():
        return ""

    match = re.search(r'(?is)(<think(?:ing)?>)\s*$', text.strip())
    if not match:
        return ""
    return match.group(1).lower()


def find_assistant_prefill_text(chat_history):
    if not isinstance(chat_history, list):
        return ""

    for item in reversed(chat_history):
        if not isinstance(item, dict):
            continue
        if str(item.get("role", "")).lower() != "assistant":
            continue
        content = str(item.get("content", "") or "").strip()
        open_think = extract_open_think_prefill(content)
        if open_think:
            return open_think
    return ""


def find_latest_user_text(chat_history):
    if not isinstance(chat_history, list):
        return ""
    for item in reversed(chat_history):
        if not isinstance(item, dict):
            continue
        if str(item.get("role", "")).lower() != "user":
            continue
        content = strip_control_blocks_from_message(str(item.get("content", "") or ""))
        if content:
            return content
    return ""


def chat_history_chars(chat_history):
    if not isinstance(chat_history, list):
        return 0
    total = 0
    for message in chat_history:
        if not isinstance(message, dict):
            continue
        total += len(str(message.get("role", "") or ""))
        total += len(str(message.get("content", "") or ""))
    return total


def active_turn_metadata_content(active_turn_id, latest_user_text="", prefill_text=""):
    lines = [
        f"{ACTIVE_TURN_METADATA_MARKER}: {active_turn_id}",
        "This is routing metadata, not a roleplay message.",
        "The assistant must answer only the user message between the active-turn markers.",
        "All other user messages are historical context, not the current request.",
        "Do not continue or answer any earlier turn.",
    ]
    if prefill_text:
        lines.append(f"Required response prefix: {prefill_text}")
    if latest_user_text:
        lines.extend([
            ACTIVE_TURN_MARKER_START,
            latest_user_text,
            ACTIVE_TURN_MARKER_END,
        ])
    return "\n".join(lines)


def build_generation_chat_history(chat_history, latest_user_text="", prefill_text="", active_turn_id=""):
    if not isinstance(chat_history, list):
        return []

    generation_history = [
        dict(item)
        for item in chat_history
        if isinstance(item, dict)
    ]

    latest_user_text = latest_user_text or find_latest_user_text(chat_history)
    prefill_text = prefill_text or find_assistant_prefill_text(chat_history)
    active_turn_id = active_turn_id or uuid.uuid4().hex

    if latest_user_text:
        marked_index = None
        for idx in range(len(generation_history) - 1, -1, -1):
            item = generation_history[idx]
            if str(item.get("role", "")).lower() == "user":
                content = str(item.get("content", "") or "")
                if strip_control_blocks_from_message(content) == latest_user_text:
                    marked_index = idx
                    break

        if marked_index is not None:
            original = str(generation_history[marked_index].get("content", "") or "")
            generation_history[marked_index]["content"] = (
                f"{ACTIVE_TURN_MARKER_START}\n"
                f"{original}\n"
                f"{ACTIVE_TURN_MARKER_END}"
            )

        metadata_content = active_turn_metadata_content(active_turn_id, latest_user_text, prefill_text)
        generation_history.insert(0, {
            "role": "system",
            "content": metadata_content,
        })
        generation_history.append({
            "role": "user",
            "content": metadata_content,
        })

    return generation_history


def normalize_transcript_role(role):
    role = str(role or "user").strip().lower()
    if role in {"system", "developer"}:
        return "System"
    if role in {"assistant", "model"}:
        return "Assistant"
    if role in {"tool", "function"}:
        return "Tool"
    return "User"


def build_conversation_transcript(chat_history, latest_user_text="", prefill_text="", active_turn_id=""):
    if not isinstance(chat_history, list):
        return ""

    latest_user_text = latest_user_text or find_latest_user_text(chat_history)
    active_turn_id = active_turn_id or uuid.uuid4().hex
    active_message_index = None
    if latest_user_text:
        for idx in range(len(chat_history) - 1, -1, -1):
            item = chat_history[idx]
            if not isinstance(item, dict):
                continue
            if normalize_transcript_role(item.get("role")) != "User":
                continue
            content = str(item.get("content", "") or "")
            if strip_control_blocks_from_message(content) == latest_user_text:
                active_message_index = idx
                break

    lines = [
        "Complete conversation transcript",
        f"Active turn id: {active_turn_id}",
        "",
        "Every message below is preserved in chronological order.",
        "Use this transcript as context. The active user message is marked explicitly.",
        "Do not answer earlier user messages unless the active message asks about them.",
    ]
    if prefill_text:
        lines.append(f"The next assistant response must begin with this prefix: {prefill_text}")
    lines.append("")

    for zero_index, item in enumerate(chat_history):
        if not isinstance(item, dict):
            continue

        index = zero_index + 1
        role = normalize_transcript_role(item.get("role"))
        content = str(item.get("content", "") or "")
        is_active_user = active_message_index == zero_index

        lines.append(f"----- MESSAGE {index} | {role} -----")
        if is_active_user:
            lines.append(ACTIVE_TURN_MARKER_START)
            lines.append(content)
            lines.append(ACTIVE_TURN_MARKER_END)
        else:
            lines.append(content)
        lines.append(f"----- END MESSAGE {index} -----")
        lines.append("")

    if latest_user_text and active_message_index is None:
        lines.extend([
            "----- ACTIVE USER MESSAGE ROUTING COPY -----",
            ACTIVE_TURN_MARKER_START,
            latest_user_text,
            ACTIVE_TURN_MARKER_END,
            "----- END ACTIVE USER MESSAGE ROUTING COPY -----",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def build_roleplay_safe_prompt(enable_google_search=False, latest_user_text="", prefill_text=""):
    prompt = (
        "Use the attached text transcript as the complete conversation context for a fictional chat. "
        "Continue as the assistant character defined by that transcript. "
        "Answer only the active user message marked in the transcript. "
        "Do not continue earlier turns and do not answer routing or metadata text. "
        "Do not use browser, account, location, weather, current date, current time, or other real-world user metadata unless it is written inside the transcript. "
        "When scene metadata needs a date or time, invent an in-world value that fits the fictional setting. "
        "Reply in plain text only and do not mention the attached file."
    )
    if latest_user_text:
        latest_user_prompt = truncate_prompt_excerpt(latest_user_text, limit=12000)
        prompt += (
            " The active user message is repeated here for routing; if this repeat is abbreviated, "
            "use the full marked version in the attached transcript:\n"
            f"{ACTIVE_TURN_MARKER_START}\n{latest_user_prompt}\n{ACTIVE_TURN_MARKER_END}"
        )
    if prefill_text:
        prompt += (
            f"\nThe SillyTavern preset requires the next answer to start with the literal prefix {prefill_text}. "
            "Do not treat that prefix as already written. Emit it as the first characters of your answer, "
            "fill the block, close it, then write the visible reply."
        )
    if enable_google_search:
        prompt += (
            " You MUST use the Google Search tool ONLY to find details relevant to the roleplay scene "
            "(characters, locations, items, lore, etc.). Never use it to look up the user's real-world data. "
            "Do not mention that you searched unless the scene requires it."
        )
    return prompt

async def handle_gemini_text_generation(request: Request, data, requested_model, stream: bool = False):
    print_debug("Gemini text request payload", {
        "path": request.url.path,
        "requested_model": requested_model,
        "stream": stream,
        "payload": data,
    }, max_len=16000)
    contents = data.get("contents", [])
    if not isinstance(contents, list) or not contents:
        return JSONResponse({"error": "No contents provided"}, status_code=400)

    chat_history, prefill_text = build_chat_history_from_gemini_contents(contents)

    system_text = extract_system_instruction_text(
        data.get("systemInstruction") or data.get("system_instruction")
    )
    if system_text:
        # Кладём системный промпт первым сообщением chat.json — иначе character card/persona
        # из Google AI Studio source просто теряются.
        chat_history.insert(0, {"role": "system", "content": system_text})

    if not chat_history:
        return JSONResponse({"error": "No text content provided"}, status_code=400)

    if not prefill_text:
        prefill_text = find_assistant_prefill_text(chat_history)

    latest_user_text = find_latest_user_text(chat_history)
    active_turn_id = uuid.uuid4().hex
    file_content = build_conversation_transcript(chat_history, latest_user_text, prefill_text, active_turn_id)
    print_debug("Gemini text active turn", {
        "chat_history_len": len(chat_history),
        "chat_history_chars": chat_history_chars(chat_history),
        "transcript_chars": len(file_content),
        "active_turn_id": active_turn_id,
        "prefill_text": prefill_text,
        "latest_user_preview": latest_user_text[:1000],
    }, max_len=4000)
    safe_prompt = build_roleplay_safe_prompt(
        enable_google_search=has_google_search_tool(data),
        latest_user_text=latest_user_text,
        prefill_text=prefill_text,
    )
    if stream:
        return StreamingResponse(
            gemini_stream_generate_content(
                request, safe_prompt, requested_model, file_content, prefill_text
            ),
            media_type="text/event-stream",
        )

    generated_text = await generate_text_core(
        request, safe_prompt, model_name=requested_model, file_content=file_content, force_extended=True,
    )
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


async def gemini_stream_generate_content(request: Request, safe_prompt, effective_model, file_content, prefill_text):
    """SSE-стрим в Gemini API формате (`:streamGenerateContent?alt=sse`).

    Внутри сервер всё равно ждёт полный ответ от Google и отдаёт его одним финальным чанком —
    но клиенту это видно как корректный SSE-поток, аналогично OpenAI-ветке.
    """
    task = asyncio.create_task(
        generate_text_core(
            request, safe_prompt, model_name=effective_model, file_content=file_content, force_extended=True,
        )
    )
    try:
        while not task.done():
            if await request.is_disconnected():
                print_sys("🛑 [ПРЕРВАНО] SSE-клиент (Gemini) отключился, отменяем генерацию.")
                task.cancel()
                return

            _, pending = await asyncio.wait([task], timeout=10.0)
            if pending:
                # Empty parts keep the SSE socket alive on slow generations.
                ping = {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}}]}
                yield f"data: {json.dumps(ping)}\n\n"

        try:
            generated_text = task.result()
        except asyncio.CancelledError:
            print_sys("🏁 ЗАВЕРШЕНО. Gemini-стрим был отменен клиентом.")
            return
        except WebModelError as exc:
            yield f"data: {json.dumps(exc.payload())}\n\n"
            return
        except Exception as e:
            print_sys(f"[❌] ОШИБКА ФОНОВОЙ GEMINI-ГЕНЕРАЦИИ: {e}")
            err = {"error": {"message": "Generation failed. Check server logs.", "type": "server_error"}}
            yield f"data: {json.dumps(err)}\n\n"
            return

        if generated_text is None:
            err = {"error": {"message": "Generation failed. Check server logs.", "type": "server_error"}}
            yield f"data: {json.dumps(err)}\n\n"
            return

        final_text = postprocess_generated_text(generated_text, prefill_text)
        payload = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": final_text}]},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ]
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

@app.get('/v1/models')
@app.get('/v1beta/models')
@app.options('/v1/models')
@app.options('/v1beta/models')
async def list_models(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    token, _ = start_api_request_logging(request, "models")
    try:
        print_sys(f"\n{'='*50}\n🔍 ЗАПРОС СПИСКА МОДЕЛЕЙ\n{'='*50}")
        catalog = None
        snlm0e = await get_snlm0e()
        if snlm0e or can_refresh_token_from_cookies():
            snlm0e, browser_context = await get_text_request_context(snlm0e)
            if snlm0e:
                catalog = await get_web_model_catalog(snlm0e, browser_context)
        new_models = [model for model in (catalog or {}) if model not in LEGACY_TEXT_MODELS]
        model_ids = [*new_models, *LEGACY_TEXT_MODELS, "nano-banana-pro", "nano-banana-2"]
        for model in model_ids:
            print_sys(f"  - {model}")
        print_sys("[+] Список моделей успешно отправлен в клиент.")

        openai_models = [
            {"id": model, "object": "model", "created": 1712050000, "owned_by": "google"}
            for model in model_ids
        ]
        if request.url.path.startswith("/v1beta/"):
            gemini_models = [
                {
                    **item,
                    "name": f"models/{item['id']}",
                    "displayName": item["id"],
                    "supportedGenerationMethods": (
                        ["generateContent"] if is_image_model(item["id"]) else ["generateContent", "streamGenerateContent"]
                    ),
                }
                for item in openai_models
            ]
            return JSONResponse({"object": "list", "data": openai_models, "models": gemini_models})
        return JSONResponse({"object": "list", "data": openai_models, "models": openai_models})
    finally:
        reset_request_log(token)

@app.post('/v1/images/generations')
@app.post('/v1beta/models/{model}:generateContent')
@app.post('/v1beta/models/{model}:streamGenerateContent')
@app.post('/v1beta/models/models/{model}:generateContent')
@app.post('/v1beta/models/models/{model}:streamGenerateContent')
@app.options('/v1/images/generations')
@app.options('/v1beta/models/{model}:generateContent')
@app.options('/v1beta/models/{model}:streamGenerateContent')
@app.options('/v1beta/models/models/{model}:generateContent')
@app.options('/v1beta/models/models/{model}:streamGenerateContent')
async def unified_image_generation(request: Request, model: str = None):
    if request.method == 'OPTIONS': return JSONResponse({})
    token, _ = start_api_request_logging(request, "generation")
    try:
        try: data = await request.json()
        except Exception: data = {}
        print_debug("Unified generation incoming payload", {
            "path": request.url.path,
            "path_model": model,
            "payload": data,
        }, max_len=16000)

        is_gemini_format = False
        prompt = data.get('prompt')
        body_model = data.get('model')
        requested_model = body_model or model or "nano-banana-pro"
        is_v1beta = request.url.path.startswith('/v1beta/')
        is_stream_path = ':streamGenerateContent' in request.url.path
        effective_model = normalize_requested_model(requested_model)
        reference_images_b64 = []

        if is_v1beta:
            print_sys(
                f"[*] Gemini-compatible request: path model={model!r}, body model={body_model!r}, requested={requested_model!r}, effective={effective_model!r}, stream={is_stream_path}"
            )

        if not is_image_model(effective_model):
            return await handle_gemini_text_generation(
                request, data, effective_model, stream=is_stream_path
            )

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
            except Exception:
                pass

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
            except Exception:
                pass

        if not prompt or not str(prompt).strip(): prompt = "A highly detailed, photorealistic masterpiece"
        prompt = str(prompt).replace('\n', ' ').replace('\r', ' ')

        format_instructions = []
        if requested_aspect: format_instructions.append(f"Aspect ratio: {requested_aspect}")
        if requested_size: format_instructions.append(f"Resolution: {requested_size}")

        if format_instructions:
            prompt = f"[SYSTEM INSTRUCTION: MUST USE FORMAT - {', '.join(format_instructions)}] {prompt}"

        image_path = await generate_image_core(request, prompt, reference_images_b64=reference_images_b64, model_name=effective_model)

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
    finally:
        reset_request_log(token)

@app.get('/images/{filename}')
async def serve_image(request: Request, filename: str):
    token, _ = start_api_request_logging(request, "image-file")
    try:
        file_path = os.path.join(OUTPUT_DIR, filename)
        print_debug("Image file request", {"filename": filename, "exists": os.path.exists(file_path)})
        return FileResponse(file_path, media_type='image/png') if os.path.exists(file_path) else JSONResponse({"error": "Not found"}, status_code=404)
    finally:
        reset_request_log(token)

# =====================================================================
# НИЖЕ СНОВА ИДЕТ АКТУАЛЬНАЯ ЛОГИКА ГЕНЕРАЦИИ ТЕКСТА
# =====================================================================

@app.post('/v1/chat/completions')
@app.options('/v1/chat/completions')
async def chat_completions(request: Request):
    if request.method == 'OPTIONS': return JSONResponse({})
    token, _ = start_api_request_logging(request, "chat-completions")
    print_sys(f"\n{'='*50}\n📥 НОВЫЙ ЗАПРОС ОТ ТАВЕРНЫ\n{'='*50}")
    is_stream = False

    try:
        try: data = await request.json()
        except Exception: data = {}
        print_debug("Chat completions incoming payload", {
            "path": request.url.path,
            "payload": data,
        }, max_len=16000)

        messages = data.get('messages', [])
        if not messages:
            print_sys("[❌] Ошибка: Таверна прислала пустой список сообщений.")
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        chat_history = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            text = coerce_message_content_to_text(msg.get("content"))
            if not text:
                continue
            chat_history.append({
                "role": msg.get("role", "user"),
                "content": text,
            })

        if not chat_history:
            print_sys("[❌] Ошибка: после нормализации не осталось текстовых сообщений.")
            return JSONResponse({"error": "No text content provided"}, status_code=400)

        requested_model = str(data.get('model', 'nano-banana-pro')).lower()
        effective_model = normalize_requested_model(requested_model)
        is_stream = data.get('stream', False)
        print_debug("Chat completions resolved request", {
            "requested_model": requested_model,
            "effective_model": effective_model,
            "is_stream": is_stream,
            "message_count": len(messages),
            "chat_history_len": len(chat_history),
        })

        prefill_text = ""
        if chat_history and chat_history[-1].get("role") == "assistant":
            prefill_text = str(chat_history[-1].get("content", "") or "").strip()
            if prefill_text:
                print_sys(f"[*] Обнаружен префилл от Таверны (Длина: {len(prefill_text)} символов).")

        if not prefill_text:
            prefill_text = find_assistant_prefill_text(chat_history)

        latest_user_text = find_latest_user_text(chat_history)
        active_turn_id = uuid.uuid4().hex
        file_content = build_conversation_transcript(chat_history, latest_user_text, prefill_text, active_turn_id)
        print_debug("Chat completions active turn", {
            "chat_history_len": len(chat_history),
            "chat_history_chars": chat_history_chars(chat_history),
            "transcript_chars": len(file_content),
            "active_turn_id": active_turn_id,
            "prefill_text": prefill_text,
            "latest_user_preview": latest_user_text[:1000],
        }, max_len=4000)
        safe_prompt = build_roleplay_safe_prompt(
            latest_user_text=latest_user_text,
            prefill_text=prefill_text,
        )

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
                    except WebModelError as exc:
                        yield f"data: {json.dumps(exc.payload())}\n\n"
                        yield "data: [DONE]\n\n"
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
                    print_debug("SSE final text", {
                        "cmpl_id": cmpl_id,
                        "length": len(final_text),
                        "preview": final_text[:6000],
                    }, max_len=16000)

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
                    reset_request_log(token)
              
            return StreamingResponse(sse_stream(), media_type='text/event-stream')

        # Резервный механизм, если стриминг выключен
        generated_text = await generate_text_core(request, safe_prompt, model_name=effective_model, file_content=file_content)

        if generated_text is None:
            print_sys("[❌] ИТОГ: Генерация прервана или завершилась сбоем. Отправляем ошибку в Таверну.")
            return JSONResponse({"error": {"message": "Request cancelled by user or failed (Check console logs)", "type": "server_error"}}, status_code=500)

        print_sys("✨ [ЭТАП 5] Единая безопасная постобработка ответа...")
        final_text = postprocess_generated_text(generated_text, prefill_text)
        print_debug("Non-stream final text", {
            "length": len(final_text),
            "preview": final_text[:6000],
        }, max_len=16000)

        print_sys(f"✅ [ЭТАП 6] Текст готов к отправке (Длина: {len(final_text)}).")
        print_sys(f"🏁 ЗАВЕРШЕНО. Сообщение доставлено в Таверну (Без стрима).\n{'='*50}")
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": final_text}, "finish_reason": "stop"}]
        })
    finally:
        if not is_stream:
            reset_request_log(token)

if __name__ == "__main__":
    import uvicorn

    async def run_server():
        session_ok = await init_session()
        if not session_ok:
            if IS_MOBILE:
                print_sys("[❌] Gemini не принял сессию. Проверь, что Gemini открыт в браузере, затем импортируй свежий JSON-экспорт cookies через start.py --refresh.")
                raise SystemExit(1)

            print_sys("[!] Куки устарели или стали недействительными. Возвращаемся в лаунчер и пробуем один автоматический refresh...")
            raise SystemExit(SESSION_INVALID_EXIT_CODE)

        print_sys(f"\n[*] Geminiweb2API запущен! (Порт: {PORT})")
        config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(run_server())
