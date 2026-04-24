from playwright.sync_api import sync_playwright
import os
import sys
import json
from log_utils import debug_log, log_line

STATE_FILE = "google_state.json"
TOKEN_STATE_KEY = "snlm0e"
TOKEN_UPDATED_AT_KEY = "snlm0e_updated_at"
IS_DEBUG = "--debug" in sys.argv


def print_sys(msg):
    log_line("auth", str(msg))


def print_debug(label, data=None, max_len=8000):
    debug_log("auth", IS_DEBUG, label, data, max_len=max_len)

def has_saved_browser_profile(profile_dir):
    if not os.path.isdir(profile_dir):
        return False

    try:
        with os.scandir(profile_dir) as entries:
            return any(True for _ in entries)
    except OSError:
        return False

def get_browser_launch_candidates():
    candidates = []
    candidates.extend([
        {"name": "Google Chrome", "channel": "chrome"},
        {"name": "Microsoft Edge", "channel": "msedge"},
        {"name": "Chromium", "channel": "chromium"}
    ])

    if sys.platform.startswith("win"):
        candidates.extend([
            {
                "name": "Opera",
                "executable_paths": [
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Opera", "opera.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES", ""), "Opera", "launcher.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Opera", "launcher.exe")
                ]
            },
            {
                "name": "Brave",
                "executable_paths": [
                    os.path.join(os.environ.get("PROGRAMFILES", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
                ]
            },
            {
                "name": "Vivaldi",
                "executable_paths": [
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Vivaldi", "Application", "vivaldi.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES", ""), "Vivaldi", "Application", "vivaldi.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Vivaldi", "Application", "vivaldi.exe")
                ]
            }
        ])
    elif sys.platform == "darwin":
        candidates.extend([
            {"name": "Opera", "executable_paths": ["/Applications/Opera.app/Contents/MacOS/Opera"]},
            {"name": "Brave", "executable_paths": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]},
            {"name": "Vivaldi", "executable_paths": ["/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"]}
        ])
    else:
        candidates.extend([
            {
                "name": "Opera",
                "executable_paths": [
                    "/usr/bin/opera",
                    "/usr/bin/opera-stable",
                    "/snap/bin/opera"
                ]
            },
            {
                "name": "Brave",
                "executable_paths": [
                    "/usr/bin/brave-browser",
                    "/usr/bin/brave-browser-stable",
                    "/snap/bin/brave"
                ]
            },
            {
                "name": "Vivaldi",
                "executable_paths": [
                    "/usr/bin/vivaldi",
                    "/usr/bin/vivaldi-stable"
                ]
            }
        ])

    return candidates

def first_existing_path(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None

def launch_browser_context(playwright, profile_dir, proxy_config, headless_mode):
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars"
    ]
    if not headless_mode:
        browser_args.append("--start-minimized")

    last_error = None
    for candidate in get_browser_launch_candidates():
        launch_kwargs = {
            "user_data_dir": profile_dir,
            "headless": headless_mode,
            "proxy": proxy_config,
            "args": browser_args
        }

        channel = candidate.get("channel")
        if channel:
            launch_kwargs["channel"] = channel
        else:
            executable_path = first_existing_path(candidate.get("executable_paths", []))
            if not executable_path:
                continue
            launch_kwargs["executable_path"] = executable_path

        try:
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
            return context, candidate["name"]
        except Exception as error:
            last_error = error

    if last_error is not None:
        raise last_error
    raise RuntimeError("Не найден ни один поддерживаемый браузер для Playwright auth flow.")

def inspect_browser_session(context, debug_label=None):
    page = context.pages[0] if context.pages else context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    print_sys("[*] Проверка сохраненной сессии в браузере...")
    try:
        page.goto("https://gemini.google.com/app", timeout=60000)
        page.wait_for_timeout(4000)
    except Exception:
        pass

    cookies = context.cookies()
    has_psid = any(c.get('name') == '__Secure-1PSID' for c in cookies)
    has_sapisid = any(c.get('name') == 'SAPISID' for c in cookies)

    if debug_label:
        print_sys(f"[*] {debug_label}: __Secure-1PSID={'OK' if has_psid else 'MISSING'}, SAPISID={'OK' if has_sapisid else 'MISSING'}.")

    print_debug("Browser session inspection", {
        "debug_label": debug_label,
        "cookie_count": len(cookies),
        "has_psid": has_psid,
        "has_sapisid": has_sapisid,
        "current_url": page.url,
    })

    return page, has_psid, has_sapisid

def load_existing_token_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return {}
    except Exception:
        return {}

    preserved = {}
    for key in [TOKEN_STATE_KEY, TOKEN_UPDATED_AT_KEY]:
        if key in state:
            preserved[key] = state[key]
    return preserved

def login_and_save_state():
    print_sys("\n[*] Инициализация браузера для входа...")
    
    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    has_profile = has_saved_browser_profile(profile_dir)
    
    # Ищем прокси в аргументах запуска
    proxy_config = None
    if "--proxy" in sys.argv:
        try:
            idx = sys.argv.index("--proxy")
            proxy_url = sys.argv[idx + 1]
            proxy_config = {"server": proxy_url}
            print_sys(f"[*] Playwright использует прокси: {proxy_url}")
        except IndexError:
            pass

    print_debug("Auth startup context", {
        "profile_dir": profile_dir,
        "has_saved_profile": has_profile,
        "proxy_configured": bool(proxy_config),
        "argv": sys.argv,
    })

    with sync_playwright() as p:
        attempted_headless_refresh = False
        if has_profile:
            print_sys("[*] Найден сохраненный браузерный профиль. Сначала пробуем тихий headless-refresh...")
            context, browser_name = launch_browser_context(p, profile_dir, proxy_config, headless_mode=True)
            attempted_headless_refresh = True
            print_sys(f"[*] Запущен {browser_name} в headless-режиме для проверки refresh.")
            page, has_psid, has_sapisid = inspect_browser_session(context, debug_label="Headless-refresh debug")
            is_logged_in = has_psid
            print_debug("Headless refresh result", {
                "browser_name": browser_name,
                "is_logged_in": is_logged_in,
                "has_psid": has_psid,
                "has_sapisid": has_sapisid,
            })

            if not is_logged_in:
                print_sys("[!] Headless-refresh не достал рабочую сессию. Откатываемся на обычное окно браузера...")
                context.close()
                context, browser_name = launch_browser_context(p, profile_dir, proxy_config, headless_mode=False)
                print_sys(f"[*] Запущен {browser_name} с видимым окном для обычного refresh/login.")
                page, has_psid, has_sapisid = inspect_browser_session(context)
                is_logged_in = has_psid
        else:
            print_sys("[*] Сохраненный браузерный профиль не найден. Сразу запускаем обычное окно браузера...")
            context, browser_name = launch_browser_context(p, profile_dir, proxy_config, headless_mode=False)
            print_sys(f"[*] Запущен {browser_name} с видимым окном для первичной авторизации.")
            page, has_psid, has_sapisid = inspect_browser_session(context)
            is_logged_in = has_psid

        if is_logged_in:
            if attempted_headless_refresh:
                print_sys("[+] Headless-refresh нашел рабочую сессию. Сохраняем свежие куки без открытия окна.")
            else:
                print_sys("[+] Аккаунт найден! Вы уже авторизованы.")
                print_sys("[*] Молча воруем свежие куки и обновляем сессию...")
        else:
            print_sys("[-] Сессия не найдена или устарела.")
            print_sys("[*] Открываем страницу входа Google...")
            page.goto("https://accounts.google.com/")

            print_sys("\n" + "="*50)
            print_sys("[!] ВНИМАНИЕ: Пожалуйста, войдите в свой Google аккаунт.")
            print_sys("[!] Браузер запомнит вас, и в следующий раз (через --refresh) этого делать не придется!")
            print_sys("[!] КОГДА ВОЙДЕТЕ — вернитесь в эту консоль и нажмите ENTER.")
            print_sys("="*50 + "\n")
            
            input("👉 Нажмите ENTER, когда закончите авторизацию... ")

            print_sys("[*] Отлично! Переходим на gemini.google.com для сохранения сессии...")
            try:
                page.goto("https://gemini.google.com/app", timeout=60000)
            except Exception:
                page.wait_for_timeout(3000)
                page.goto("https://gemini.google.com/app", timeout=60000)
                
            page.wait_for_timeout(4000) 

        state = context.storage_state()
        if not isinstance(state, dict):
            state = {}
        state.update(load_existing_token_state())
        print_debug("Auth storage state before save", {
            "cookie_count": len(state.get("cookies", [])),
            "origin_count": len(state.get("origins", [])),
            "preserved_token_keys": [key for key in [TOKEN_STATE_KEY, TOKEN_UPDATED_AT_KEY] if key in state],
        })

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)

        print_sys("\n[+] УСПЕХ! Сессия надежно сохранена в 'google_state.json'.")
        print_debug("Auth state saved", {
            "state_file": STATE_FILE,
            "cookie_count": len(state.get("cookies", [])),
        })
        
        context.close()

if __name__ == "__main__":
    login_and_save_state()
