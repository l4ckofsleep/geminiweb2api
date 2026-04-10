import os
import sys
import json
import subprocess
import shutil

STATE_FILE = "google_state.json"
PROFILE_DIR = "chrome_profile"
SESSION_INVALID_EXIT_CODE = 86
TOKEN_STATE_KEY = "snlm0e"
TOKEN_UPDATED_AT_KEY = "snlm0e_updated_at"

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
    return False

def run_auth_mobile():
    print("\n" + "="*50)
    print("📱 ОБНАРУЖЕНО МОБИЛЬНОЕ УСТРОЙСТВО (Android/Termux)")
    print("="*50)
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
    print("=" * 40)
    print("🍌 Geminiweb2api")
    print("=" * 40)

    extra_api_args = []
    proxy_url = None

    if "--temp" in sys.argv:
        print("[*] Активирован режим ВРЕМЕННОГО ЧАТА (--temp)")
        extra_api_args.append("--temp")

    if "--debug" in sys.argv:
        print("[*] Активирован режим ОТЛАДКИ (--debug).")
        extra_api_args.append("--debug")

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
    
    mobile = is_mobile()

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
