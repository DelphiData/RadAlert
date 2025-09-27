import os, re, asyncio, json, base64
from datetime import datetime
import pytz
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

print("=== RadAlert LOGIN HANDLER v3 ===")

# ----------------------------
# Config (from environment)
# ----------------------------
LOGIN_URL = "https://avrteleris.com/AVR/Index.aspx"
TZ = pytz.timezone("America/New_York")

AVR_USER = os.environ["AVR_USER"]
AVR_PASS = os.environ["AVR_PASS"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")  # vision-capable chat model

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]  # channel handle like @my_channel or numeric id

THRESHOLD = int(os.environ.get("THRESHOLD", "25"))
AGE_MINUTES = int(os.environ.get("AGE_MINUTES", "60"))
SITE_LABEL = os.environ.get("SITE_LABEL", "Baptist Health Corbin (AVR)")

# DRY RUN: when true we always post the model's JSON (no threshold gating),
# and we also send helper screenshots for troubleshooting.
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Optional override if you know a precise selector:
LOGIN_CLICK_SELECTOR = os.environ.get("LOGIN_CLICK_SELECTOR", "").strip()  # e.g., text=/RESULTS REPORTING SYSTEM/i


# ----------------------------
# Helpers
# ----------------------------
def within_window_now():
    """Mon–Fri at 6p, 8p, 10p ET; Sat 4a–10p ET q2h. DRY_RUN bypasses."""
    now = datetime.now(TZ)
    wd, hr = now.weekday(), now.hour
    if wd in range(0, 5) and hr in (18, 20, 22):
        return True
    if wd == 5 and hr in (4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
        return True
    return False

def to_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()

def send_telegram_text(text: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    CHUNK = 3500
    if len(text) <= CHUNK:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=30)
    else:
        i = 0
        while i < len(text):
            chunk = text[i:i+CHUNK]
            requests.post(url, json={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "HTML"}, timeout=30)
            i += CHUNK

def send_telegram_photo(png_bytes: bytes, caption: str = ""):
    try:
        files = {"photo": ("image.png", png_bytes)}
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT_ID, "caption": caption},
            files=files, timeout=30
        )
    except Exception as e:
        send_telegram_text(f"Could not send screenshot: {e}")

def ask_gpt_vision(image_data_url: str, table_html: str, now_iso_et: str) -> dict:
    system = (
        "You are a meticulous auditor. You extract counts from a radiology worklist screenshot and corresponding HTML. "
        "Output STRICT JSON only, no prose."
    )
    user_prompt = f"""
You are given a screenshot and the corresponding HTML of the 'Worklist' table from a radiology prelim system.

Goal: Count all CT and MRI procedures that are > {AGE_MINUTES} minutes old at the current time (ET).

Counting rules (IMPORTANT):
- Count PROCEDURES, not rows.
- If a single row's 'Study Requested' contains multiple CT/MRI items (e.g., 'CT ABD PELVIS W/ IV, CT CHEST W/O' or 'MRI BRAIN, MRI C-SPINE'),
  count EACH CT/MRI occurrence separately.
- A procedure qualifies if the row's Date + Time (request time) is more than {AGE_MINUTES} minutes before NOW_ET.
- Ignore all non-CT, non-MRI studies (e.g., XRAY, US, etc.).
- If anything is ambiguous, be conservative and do not invent data.
- Assume timestamps are ET unless otherwise labeled.

NOW_ET (ISO8601): {now_iso_et}

Return JSON ONLY with this exact schema (no extra keys, no commentary):
{{
  "count_ct_mri_over_60": <int>,
  "by_modality": {{
    "CT": <int>,
    "MRI": <int>
  }},
  "sample_ids_or_rows": [<up to 5 short identifiers or row snippets you actually used>]
}}
"""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "text", "text": f"TABLE_HTML:\n{table_html[:120000]}"},
                {"type": "image_url", "image_url": {"url": image_data_url}}
            ]}
        ],
        "temperature": 0
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise
        return json.loads(m.group(0))

# ----------------------------
# Login helpers
# ----------------------------
async def click_prelogin_triggers(target, label: str) -> bool:
    """Try several selectors to reveal the login form on a page or frame."""
    candidates = []
    if LOGIN_CLICK_SELECTOR:
        candidates.append(LOGIN_CLICK_SELECTOR)

    # Good guesses for what you showed in the screenshot:
    candidates += [
        'text=/RESULTS REPORTING SYSTEM/i',
        'a:has-text("RESULTS REPORTING SYSTEM")',
        'text=/Preliminary Reports/i',
        'a:has-text("Preliminary Reports")',
        # Click the left card generically: the first <a> in the hero area
        'xpath=(//a)[1]'
    ]

    for sel in candidates:
        try:
            await target.click(sel, timeout=1500)
            # give it a moment to render form
            await target.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False

async def try_fill_on_frame(frame, user, pw) -> bool:
    """Try many selectors for user/pass/submit on a given frame. Return True if submitted."""
    user_selectors = [
        'input[name="username"]', 'input[name="user"]', 'input[id*="user" i]',
        'input[placeholder*="User" i]', 'input[aria-label*="User" i]',
        'input[type="email"]', 'input[type="text"]'
    ]
    pass_selectors = [
        'input[name="password"]', 'input[id*="pass" i]',
        'input[placeholder*="Pass" i]', 'input[aria-label*="Pass" i]',
        'input[type="password"]'
    ]
    submit_selectors = [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Login")', 'button:has-text("Sign In")',
        'input[value*="Login" i]', 'input[value*="Sign In" i]'
    ]

    user_ok = False
    for sel in user_selectors:
        try:
            await frame.fill(sel, user, timeout=1200)
            user_ok = True
            break
        except Exception:
            continue

    pass_ok = False
    for sel in pass_selectors:
        try:
            await frame.fill(sel, pw, timeout=1200)
            pass_ok = True
            break
        except Exception:
            continue

    if user_ok and pass_ok:
        for sel in submit_selectors:
            try:
                await frame.click(sel, timeout=1200)
                return True
            except Exception:
                continue
    return False

async def perform_login(page, user, pw) -> bool:
    """Reveal the login form (page + iframes), then fill and submit."""
    # 0) Try clicking triggers on main page
    await click_prelogin_triggers(page, "page")

    # 1) Try to fill on main page
    if await try_fill_on_frame(page, user, pw):
        return True

    # 2) Try clicking/filling on each iframe
    for f in page.frames:
        if f is page.main_frame:
            continue
        await click_prelogin_triggers(f, "frame")
        if await try_fill_on_frame(f, user, pw):
            return True

    # 3) Last resort: click again on page and retry fill
    await click_prelogin_triggers(page, "page-retry")
    if await try_fill_on_frame(page, user, pw):
        return True

    return False

# ----------------------------
# Main one-shot run
# ----------------------------
async def run_once():
    if not DRY_RUN and not within_window_now():
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Go to landing
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Perform login
        logged_in = await perform_login(page, AVR_USER, AVR_PASS)

        if not logged_in:
            png_login = await page.screenshot(full_page=True)
            if DRY_RUN:
                send_telegram_photo(png_login, "RadAlert: could not find login fields. Screenshot.")
            raise RuntimeError("Login fields not found. Check Telegram screenshot (DRY_RUN).")

        # Wait for post-login network to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Extract HTML for the worklist table (best-effort; fall back to page HTML)
        table_html = ""
        try:
            worklist_heading = page.locator("text=Worklist").first
            worklist_container = worklist_heading.locator("xpath=..")
            table = worklist_container.locator("xpath=.//table").first
            table_html = await table.evaluate("(el) => el.outerHTML")
        except Exception:
            try:
                table_html = await page.locator("xpath=(//table)[1]").evaluate("(el)=>el.outerHTML")
            except Exception:
                table_html = await page.content()

        # Screenshot for the model (full page is simplest & robust)
        png_bytes = await page.screenshot(full_page=True)

        if DRY_RUN:
            send_telegram_photo(png_bytes, "RadAlert DRY_RUN: page screenshot after login.")

        await ctx.close()
        await browser.close()

    now_et_iso = datetime.now(TZ).isoformat()
    data_url = to_data_url(png_bytes)

    # Ask GPT Vision
    result = ask_gpt_vision(data_url, table_html, now_et_iso)

    if DRY_RUN:
        pretty = json.dumps(result, indent=2)
        msg = f"🔍 <b>Dry-run JSON dump</b>\n<pre>{pretty}</pre>"
        send_telegram_text(msg)
        return

    # LIVE MODE
    ct_mri = int(result.get("count_ct_mri_over_60", 0))
    by_mod = result.get("by_modality", {})
    ct = by_mod.get("CT", 0)
    mri = by_mod.get("MRI", 0)

    if ct_mri > THRESHOLD:
        stamp = datetime.now(TZ).strftime("%-I:%M %p %Z")
        msg = (
            f"🟡 <b>Backlog alert</b> — {SITE_LABEL}\n"
            f"CT/MRI > {AGE_MINUTES} min old: <b>{ct_mri}</b> (CT: {ct}, MRI: {mri}) at {stamp}\n"
            f"{LOGIN_URL}"
        )
        send_telegram_text(msg)

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    asyncio.run(run_once())
