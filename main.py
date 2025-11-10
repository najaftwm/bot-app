# ─────────────────────────────────────────────────────────────────────────────
# main.py – works locally *and* on Railway
# ─────────────────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time, threading, requests, os, pickle, json, re
from urllib.parse import quote

app = Flask(__name__)

# ───── CONFIG ─────
BACKEND_RECEIVE_URL = "https://wapi.twmresearchalert.com/backendphp/api/receiveMessage.php"
API_KEY = "q6ktqrPs3wZ4kvZAzNdi7"

SESSION_FILE = "session.pkl"
LOCAL_STORAGE_FILE = "local_storage.json"
CHROME_USER_DATA = "/chrome-data"

driver = None
wait = None     

# ───── Chrome options (headless on Railway, non-headless locally) ─────
def get_chrome_options():
    opts = Options()
    # ---- comment the next line when you want to *see* the browser locally ----
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    return opts

# ───── Driver init + session handling ─────
def init_driver():
    global driver, wait
    if driver:
        return driver

    print("\nStarting Chrome...")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=get_chrome_options())
    wait = WebDriverWait(driver, 30)

    driver.get("https://web.whatsapp.com/")
    print("Opened WhatsApp Web – waiting for QR / login")

    # ---- first run → show QR (only once) ------------------------------------
    try:
        WebDriverWait(driver, 300).until(
            EC.presence_of_element_located((By.XPATH, '//canvas'))  # QR canvas
        )
        print("QR code is visible – scan it with your phone now.")
    except Exception:
        pass

    # ---- wait until we are logged in ----------------------------------------
    WebDriverWait(driver, 300).until(
        EC.presence_of_element_located((By.XPATH, '//div[@data-testid="chat-list"]'))
    )
    print("Logged in!")

    # ---- persist session ----------------------------------------------------
    save_session()
    return driver

def save_session():
    if not driver: return
    try:
        # cookies
        with open(SESSION_FILE, "wb") as f:
            pickle.dump(driver.get_cookies(), f)

        # localStorage (WhatsApp stores a lot there)
        ls = driver.execute_script("""
            let o = {};
            for (let i=0;i<localStorage.length;i++){
                let k = localStorage.key(i);
                o[k] = localStorage.getItem(k);
            }
            return o;
        """)
        with open(LOCAL_STORAGE_FILE, "w") as f:
            json.dump(ls, f)

        print("Session saved to disk")
    except Exception as e:
        print("Session save error:", e)

def load_session():
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        with open(SESSION_FILE, "rb") as f:
            for c in pickle.load(f):
                driver.add_cookie(c)

        if os.path.exists(LOCAL_STORAGE_FILE):
            with open(LOCAL_STORAGE_FILE) as f:
                ls = json.load(f)
                for k, v in ls.items():
                    driver.execute_script(f"localStorage.setItem('{k}', '{v}');")

        driver.get("https://web.whatsapp.com/")
        time.sleep(4)
        print("Session loaded")
        return True
    except Exception as e:
        print("Session load error:", e)
        return False

# ───── SEND MESSAGE ─────
def send_whatsapp_message(phone: str, message: str) -> bool:
    drv = init_driver()
    print(f"\nSending to {phone}: {message}")

    # Normalise phone (digits only, keep country code)
    norm = re.sub(r"\D", "", phone.lstrip('+'))
    enc = quote(message)

    drv.get(f"https://web.whatsapp.com/send?phone={norm}&text={enc}")
    time.sleep(5)

    # ---- locate input box ----------------------------------------------------
    selectors = [
        '//div[@contenteditable="true"][@data-tab="10"]',
        '//div[@contenteditable="true"][@role="textbox"]',
        '//div[@contenteditable="true"][contains(@class,"selectable-text")]',
    ]
    inp = None
    for sel in selectors:
        try:
            inp = wait.until(EC.presence_of_element_located((By.XPATH, sel)))
            break
        except:
            continue
    if not inp:
        print("Input box not found")
        return False

    inp.click()
    inp.clear()
    inp.send_keys(message)
    time.sleep(0.8)
    inp.send_keys(Keys.ENTER)
    time.sleep(2)
    print("Message sent")
    return True

# ───── Flask routes ─────
@app.route('/send_message', methods=['POST'])
def api_send():
    data = request.get_json(force=True)
    phone = data.get("phone_number") or data.get("phone")
    msg   = data.get("message")
    if not phone or not msg:
        return jsonify({"ok": False, "error": "phone_number & message required"}), 400
    return jsonify({"ok": send_whatsapp_message(phone, msg)})

@app.route('/receive_message', methods=['POST'])
def api_receive():
    data = request.get_json(force=True)
    phone = data.get("phone_number") or data.get("phone")
    msg   = data.get("message")
    if not phone or not msg:
        return jsonify({"ok": False, "error": "phone_number & message required"}), 400

    print(f"\nIncoming from {phone}: {msg}")
    try:
        requests.post(
            BACKEND_RECEIVE_URL,
            json={"phone_number": phone, "message": msg},
            timeout=10,
        )
    except Exception as e:
        print("Backend notify error:", e)
    return jsonify({"ok": True})

# ───── Incoming-monitor helpers ─────
def _get_last_incoming_message(drv):
    try:
        # Ensure latest messages are loaded and visible
        try:
            drv.find_element(By.TAG_NAME, 'body').send_keys(Keys.END)
            time.sleep(0.5)
        except Exception:
            pass

        # message-in rows contain incoming messages
        rows = drv.find_elements(By.XPATH, '//*[contains(@class, "message-in") and (self::div or self::li)]')
        if not rows:
            # Fallback: bubbles that have copyable-text with data-pre-plain-text
            rows = drv.find_elements(
                By.XPATH,
                '//div[contains(@class, "copyable-text") and @data-pre-plain-text]/ancestor::*[contains(@class, "message-in")]',
            )
        if not rows:
            print("ℹ No incoming message rows found yet")
            return None

        last = rows[-1]

        # Try to get data-id from the element or any of its ancestors
        data_id = ""
        node = last
        for _ in range(6):
            try:
                data_id = node.get_attribute("data-id") or node.get_attribute("data-message-id") or ""
                if data_id:
                    break
                node = node.find_element(By.XPATH, "..")
            except Exception:
                break

        # Build a stable row id using WhatsApp's pre-plain-text (includes timestamp/sender) + text
        pre_plain = ""
        try:
            pre_el = last.find_element(By.XPATH, './/div[contains(@class, "copyable-text")]')
            pre_plain = pre_el.get_attribute('data-pre-plain-text') or ""
        except Exception:
            pass

        # Try multiple patterns for message text
        text = ""
        patterns = [
            './/div[contains(@class, "copyable-text")]//span[contains(@class, "selectable-text")]',
            './/span[contains(@class, "selectable-text")]//span[@dir="ltr" or @dir="auto"]',
            './/div[contains(@class, "copyable-text")]//div[@role="textbox"]',
        ]
        for xp in patterns:
            try:
                el = last.find_element(By.XPATH, xp)
                text = (el.text or "").strip()
                if text:
                    break
            except Exception:
                continue
        if not text:
            # Fallback: take all text under the bubble
            text = (last.text or "").strip()
        if not text:
            print("ℹ Found incoming row but could not read text yet")
            return None

        # Compose stable id
        stable_id = f"{pre_plain}|{text}" if pre_plain else (data_id or str(hash(last)))

        # Parse phone from data-id like "false_919987464015@c.us_..."
        phone_guess = ""
        try:
            m = re.search(r"(\d{10,15})@c\.us", data_id or "")
            if m:
                digits = m.group(1)
                phone_guess = f"+{digits}"
        except Exception:
            pass

        return (stable_id, text, phone_guess)
    except Exception as e:
        print("ℹ _get_last_incoming_message error:", e)
        return None


# ───── Incoming-monitor (very small version – now functional) ─────
def start_incoming_monitor():
    def _run():
        drv = init_driver()
        seen = set()
        while True:
            try:
                result = _get_last_incoming_message(drv)
                if not result:
                    time.sleep(2)
                    continue

                stable_id, text, phone_guess = result
                if stable_id in seen:
                    time.sleep(2)
                    continue

                seen.add(stable_id)

                if not phone_guess:
                    print(f"\nIncoming (monitor) message without phone id: {text}")
                    time.sleep(2)
                    continue

                print(f"\nIncoming (monitor) from {phone_guess}: {text}")
                try:
                    requests.post(
                        BACKEND_RECEIVE_URL,
                        json={"phone": phone_guess, "message": text},
                        timeout=10,
                    )
                except Exception as notify_err:
                    print("Backend notify error:", notify_err)

                time.sleep(2)
            except Exception as e:
                print("monitor:", e)
                time.sleep(5)
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# ───── Entry point ─────
if __name__ == '__main__':
    print("\nWhatsApp Bot – local test")
    print("Tip: comment out '--headless' in get_chrome_options() to see the browser")
    start_incoming_monitor()
    # gunicorn is used on Railway; locally we use Flask dev server
    app.run(host='0.0.0.0', port=5000, debug=False)