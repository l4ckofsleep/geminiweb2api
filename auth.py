from playwright.sync_api import sync_playwright
import time
import tempfile

def login_and_save_state():
    print("\n[*] Инициализация браузера для входа...")
    
    temp_dir = tempfile.mkdtemp()

    with sync_playwright() as p:
        try:
            print("[*] Пытаемся запустить ваш системный Google Chrome...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                channel="chrome", 
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )
        except Exception as e:
            print("[!] Chrome не найден. Пытаемся запустить Microsoft Edge...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                channel="msedge", 
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )

        page = context.pages[0] if context.pages else context.new_page()

        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print("[*] Открываем страницу входа Google...")
        page.goto("https://accounts.google.com/")

        print("\n" + "="*50)
        print("[!] ВНИМАНИЕ: Пожалуйста, войдите в свой Google аккаунт в открывшемся браузере.")
        print("[!] Пройдите все этапы: логин, пароль, 2FA (если есть).")
        print("[!] КОГДА ВОЙДЕТЕ — вернитесь в эту консоль и нажмите ENTER.")
        print("="*50 + "\n")
        
        input("👉 Нажмите ENTER, когда закончите авторизацию... ")

        print("[*] Отлично! Переходим на gemini.google.com для сохранения сессии...")
        
        # Даем Гуглу 3 секунды закончить свои авто-редиректы
        page.wait_for_timeout(3000)
        
        try:
            page.goto("https://gemini.google.com/app")
        except Exception:
            # Если редирект Гугла всё еще мешает, ждем еще пару секунд и пробуем снова
            print("[*] Ждем завершения переадресации Google...")
            page.wait_for_timeout(3000)
            page.goto("https://gemini.google.com/app")
            
        # Ждем прогрузки токенов на странице Gemini
        page.wait_for_timeout(4000) 

        context.storage_state(path="google_state.json")
        print("\n[+] УСПЕХ! Сессия надежно сохранена в 'google_state.json'.")
        
        context.close()

if __name__ == "__main__":
    login_and_save_state()