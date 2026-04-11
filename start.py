import os
import sys
import json
import subprocess
import shutil
import urllib.request
import urllib.error

STATE_FILE = "google_state.json"
PROFILE_DIR = "chrome_profile"
SESSION_INVALID_EXIT_CODE = 86
TOKEN_STATE_KEY = "snlm0e"
TOKEN_UPDATED_AT_KEY = "snlm0e_updated_at"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")
ONLINE_VERSION_URL = "https://raw.githubusercontent.com/l4ckofsleep/geminiweb2api/main/VERSION"
ONLINE_RELEASE_URL = "https://api.github.com/repos/l4ckofsleep/geminiweb2api/releases/latest"

def read_local_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
            return version or "unknown"
    except Exception:
        return "unknown"

def parse_version_tuple(version):
    if not isinstance(version, str):
        return ()

    normalized = version.strip().lower()
    if normalized.startswith("v"):
        normalized = normalized[1:]

    parts = []
    for chunk in normalized.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            return ()
        parts.append(int(digits))

    return tuple(parts)

def fetch_text(url, timeout=2.0):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "geminiweb2api-version-check/1.0",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")

def fetch_online_version():
    try:
        version_text = fetch_text(ONLINE_VERSION_URL).strip()
        if version_text:
            return version_text
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
    except Exception:
        return None

    try:
        release_payload = json.loads(fetch_text(ONLINE_RELEASE_URL))
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None

    if not isinstance(release_payload, dict):
        return None

    for key in ["tag_name", "name"]:
        value = release_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None

def print_version_mismatch_notice(local_version):
    online_version = fetch_online_version()
    if not online_version or local_version == "unknown":
        return

    local_tuple = parse_version_tuple(local_version)
    online_tuple = parse_version_tuple(online_version)

    if local_tuple and online_tuple:
        if local_tuple < online_tuple:
            print(f"[!] Доступна новая версия: {online_version} (у вас {local_version})")
        elif local_tuple > online_tuple:
            print(f"[*] Локальная версия новее онлайн-репозитория: {local_version} > {online_version}")
        return

    if local_version != online_version:
        print(f"[!] Версия отличается от онлайн-репозитория: локальная {local_version}, онлайн {online_version}")

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

def is_mobile():
    if 'com.termux' in os.environ.get('PREFIX', ''): return True
    if 'ANDROID_STORAGE' in os.environ: return True
    if hasattr(sys, 'getandroidapilevel'): return True
    if sys.platform.startswith('linux'): return True
    return False

def run_auth_mobile():
    is_linux = sys.platform.startswith('linux') and not hasattr(sys, 'getandroidapilevel') and 'ANDROID_STORAGE' not in os.environ
    print("\n" + "="*50)
    if is_linux:
        print("🐧 ОБНАРУЖЕН LINUX. Включаем ручную авторизацию по кукам.")
    else:
        print("📱 ОБНАРУЖЕНО МОБИЛЬНОЕ УСТРОЙСТВО (Android/Termux)")
    print("="*50)
    if is_linux:
        print("На Linux Playwright-авторизация может не работать или блокироваться Google как небезопасный браузер.")
        print("Поэтому здесь используем ручной вход по кукам.")
        print("Тебе нужно сделать это один раз вручную:")
        print("1. Открой Firefox / Chrome / другой обычный браузер.")
        print("2. Установи расширение 'Cookie-Editor' (или аналог для просмотра куков).")
        print("3. Зайди на gemini.google.com и залогинься.")
        print("4. Если Gemini не открывается без desktop-режима сайта, включи его в браузере.")
        print("5. Открой менеджер куков и скопируй ДВА кука:")
    else:
        print("Из-за защиты Android скрипт не может сам достать куки.")
        print("Тебе нужно сделать это один раз вручную:")
        print("1. Установи Kiwi Browser или Firefox из Google Play.")
        print("2. Установи расширение 'Cookie-Editor' через меню дополнений.")
        print("3. Зайди на gemini.google.com и залогинься.")
        print("4. ⚡ ВАЖНО: Открой меню браузера (три точки) и включи 'Версия для ПК' (Desktop site)!")
        print("5. Дождись перезагрузки страницы, открой Cookie-Editor и скопируй ДВА кука:")
    print("   __Secure-1PSID и SAPISID.")
    print("-" * 50)
    print("💡 Если какого-то кука все равно нет, попробуй отправить боту любое сообщение и проверить снова.")
    print("-" * 50)

    psid = input("👉 Вставь значение __Secure-1PSID: ").strip()
    sapisid = input("👉 Вставь значение SAPISID: ").strip()

    if not psid or not sapisid:
        print("[!] Ошибка: нужны оба токена. Внимательно прочитай инструкцию выше и запусти скрипт заново.")
        sys.exit(1)

    state = {
        "cookies": [
            {"name": "__Secure-1PSID", "value": psid, "domain": ".google.com"},
            {"name": "SAPISID", "value": sapisid, "domain": ".google.com"}
        ]
    }
    state.update(load_existing_token_state())

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

    print("\n[+] УСПЕХ! Токены сохранены в файл.")

def run_auth_pc(proxy_url=None):
    print("\n" + "="*50)
    print("💻 ОБНАРУЖЕН ПК (Windows/Mac/Linux)")
    print("="*50)
    print("[*] Запуск автоматической авторизации через Playwright...")
    args = [sys.executable, "auth.py"]
    if proxy_url:
        args.extend(["--proxy", proxy_url])
    return subprocess.run(args)

def run_auto_refresh_pc(proxy_url=None):
    print("\n[!] Старая сессия больше не подходит. Пробуем один раз автоматически обновить куки...")
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        print(f"[*] Старый файл {STATE_FILE} удален. Профиль браузера сохранен.")
    return run_auth_pc(proxy_url)

def run_api(extra_args):
    print("\n[*] Запуск главного сервера API...")
    args = [sys.executable, "api.py"] + extra_args
    return subprocess.run(args)

def main():
    local_version = read_local_version()
    print("=" * 40)
    print(f"🍌 Geminiweb2api v{local_version}")
    print("=" * 40)
    print_version_mismatch_notice(local_version)

    extra_api_args = []
    proxy_url = None

    if "--temp" in sys.argv:
        print("[*] Активирован режим ВРЕМЕННОГО ЧАТА (--temp)")
        extra_api_args.append("--temp")

    if "--debug" in sys.argv:
        print("[*] Активирован режим ОТЛАДКИ (--debug).")
        extra_api_args.append("--debug")

    if "--mobile" in sys.argv:
        print("[*] Активирован принудительный МОБИЛЬНЫЙ режим (--mobile).")
        extra_api_args.append("--mobile")

    if "--proxy" in sys.argv:
        try:
            idx = sys.argv.index("--proxy")
            proxy_url = sys.argv[idx + 1]
            print(f"[*] Активирован ПРОКСИ: {proxy_url}")
            extra_api_args.extend(["--proxy", proxy_url])
        except IndexError:
            print("[!] Ошибка: Укажи адрес после флага --proxy")
            sys.exit(1)
            
    if "--port" in sys.argv:
        try:
            idx = sys.argv.index("--port")
            port_val = sys.argv[idx + 1]
            print(f"[*] Выбран нестандартный ПОРТ: {port_val}")
            extra_api_args.extend(["--port", port_val])
        except IndexError:
            print("[!] Ошибка: Укажи порт после флага --port")
            sys.exit(1)

    if "--reauth" in sys.argv:
        print("\n[!] Запрошена ЖЕСТКАЯ переавторизация (--reauth).")
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(f"[*] Старый файл {STATE_FILE} удален.")
        if os.path.exists(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            print(f"[*] Профиль браузера очищен. Потребуется полный вход.")
            
    elif "--refresh" in sys.argv:
        print("\n[!] Запрошено МЯГКОЕ обновление сессии (--refresh).")
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(f"[*] Старый файл {STATE_FILE} удален. Профиль браузера сохранен.")
    
    mobile = is_mobile() or ("--mobile" in sys.argv)

    if not os.path.exists(STATE_FILE):
        if mobile:
            run_auth_mobile()
        else:
            run_auth_pc(proxy_url)

    if os.path.exists(STATE_FILE):
        api_result = run_api(extra_api_args)
        if (not mobile) and api_result.returncode == SESSION_INVALID_EXIT_CODE:
            refresh_result = run_auto_refresh_pc(proxy_url)
            if refresh_result.returncode != 0 or not os.path.exists(STATE_FILE):
                print("\n[❌] Не удалось автоматически обновить сессию.")
                print("[!] Возможно, Google сейчас лежит, VPN не подходит, аккаунт разлогинен или куки уже окончательно умерли.")
                print("[!] Проверь, что Gemini открывается в браузере, попробуй сменить VPN или запусти start.py --reauth и войди заново.")
                sys.exit(1)

            api_result = run_api(extra_api_args)
            if api_result.returncode == SESSION_INVALID_EXIT_CODE:
                print("\n[❌] Мы один раз обновили сессию, но Google всё равно не принял новые куки.")
                print("[!] Возможно, Google сейчас лежит, VPN не подходит, аккаунт разлогинен или куки уже окончательно протухли.")
                print("[!] Попробуй сменить VPN или запусти start.py --reauth и пройди вход заново.")
                sys.exit(1)

            if api_result.returncode != 0:
                sys.exit(api_result.returncode)
        elif api_result.returncode != 0:
            sys.exit(api_result.returncode)
    else:
        print("\n[!] Ошибка: Авторизация не была завершена.")
        print("[!] Файл google_state.json не создан. Сервер не может быть запущен.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Выход...")
