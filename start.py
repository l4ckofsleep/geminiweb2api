import os
import sys
import json
import subprocess
import shutil

STATE_FILE = "google_state.json"
PROFILE_DIR = "chrome_profile"

def is_mobile():
    # Проверяем наличие специфичных для Android и Termux переменных окружения
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
    print("4. Открой расширение Cookie-Editor и скопируй ТРИ кука: __Secure-1PSID, __Secure-1PSIDTS и SAPISID.")
    print("-" * 50)

    psid = input("👉 Вставь значение __Secure-1PSID: ").strip()
    psidts = input("👉 Вставь значение __Secure-1PSIDTS: ").strip()
    sapisid = input("👉 Вставь значение SAPISID: ").strip()

    if not psid or not psidts or not sapisid:
        print("[!] Ошибка: нужны все три токена. Запусти скрипт заново.")
        sys.exit(1)

    # Сохраняем в таком же формате, как это делает Playwright на ПК
    state = {
        "cookies": [
            {"name": "__Secure-1PSID", "value": psid, "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": psidts, "domain": ".google.com"},
            {"name": "SAPISID", "value": sapisid, "domain": ".google.com"}
        ]
    }

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

    print("\n[+] УСПЕХ! Токены сохранены в файл.")

def run_auth_pc():
    print("\n" + "="*50)
    print("💻 ОБНАРУЖЕН ПК (Windows/Mac/Linux)")
    print("="*50)
    print("[*] Запуск автоматической авторизации через Playwright...")
    subprocess.run([sys.executable, "auth.py"])

def run_api():
    print("\n[*] Запуск главного сервера API...")
    subprocess.run([sys.executable, "api.py"])

def main():
    print("=" * 40)
    print("🍌 Nano Banana API Launcher")
    print("=" * 40)

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
    
    if not os.path.exists(STATE_FILE):
        if is_mobile():
            run_auth_mobile()
        else:
            run_auth_pc()

    if os.path.exists(STATE_FILE):
        run_api()
    else:
        print("\n[!] Ошибка: Авторизация не была завершена.")
        print("[!] Файл google_state.json не создан. Сервер не может быть запущен.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Выход...")