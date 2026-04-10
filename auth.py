from playwright.sync_api import sync_playwright
import time
import os
import sys
import json

STATE_FILE = "google_state.json"
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

def login_and_save_state():
    print("\n[*] Инициализация браузера для входа...")
    
    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    
    # Ищем прокси в аргументах запуска
    proxy_config = None
    if "--proxy" in sys.argv:
        try:
            idx = sys.argv.index("--proxy")
            proxy_url = sys.argv[idx + 1]
            proxy_config = {"server": proxy_url}
            print(f"[*] Playwright использует прокси: {proxy_url}")
        except IndexError:
            pass

    with sync_playwright() as p:
        try:
            print("[*] Пытаемся запустить браузер с сохранением профиля...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome", 
                headless=False,
                proxy=proxy_config,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )
        except Exception as e:
            print("[!] Chrome не найден. Пытаемся запустить Microsoft Edge...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="msedge", 
                headless=False,
                proxy=proxy_config,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )

        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print("[*] Проверка сохраненной сессии в браузере...")
        try:
            page.goto("https://gemini.google.com/app", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception:
            pass

        cookies = context.cookies()
        is_logged_in = any(c.get('name') == '__Secure-1PSID' for c in cookies)

        if is_logged_in:
            print("[+] Аккаунт найден! Вы уже авторизованы.")
            print("[*] Молча воруем свежие куки и обновляем сессию...")
        else:
            print("[-] Сессия не найдена или устарела.")
            print("[*] Открываем страницу входа Google...")
            page.goto("https://accounts.google.com/")

            print("\n" + "="*50)
            print("[!] ВНИМАНИЕ: Пожалуйста, войдите в свой Google аккаунт.")
            print("[!] Браузер запомнит вас, и в следующий раз (через --refresh) этого делать не придется!")
            print("[!] КОГДА ВОЙДЕТЕ — вернитесь в эту консоль и нажмите ENTER.")
            print("="*50 + "\n")
            
            input("👉 Нажмите ENTER, когда закончите авторизацию... ")

            print("[*] Отлично! Переходим на gemini.google.com для сохранения сессии...")
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

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)

        print("\n[+] УСПЕХ! Сессия надежно сохранена в 'google_state.json'.")
        
        context.close()

if __name__ == "__main__":
    login_and_save_state()
