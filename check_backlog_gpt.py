import os, re, io, asyncio, json, base64
from datetime import datetime
import pytz
import requests
from playwright.async_api import async_playwright

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

# DRY RUN: when true we always post the model's JSON (no threshold gating)
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ----------------------------
# Helpers
# ----------------------------
def within_window_now():
    """
    Enforce schedule:
      - Mon–Fri at 6p, 8p, 10p ET
      - Sat 4a–10p ET every 2 hours
    Run the workflow hourly and let this gate actual execution.
    """
    now = datetime.now(TZ)
    wd, hr = now.weekday(), now.hour  # Mon=0..Sun=6
    if wd in range(0, 5) and hr in (18, 20, 22):
        return True
    if wd == 5 and hr in (4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
        return True
    return False

def to_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()

def send_telegram_text(text: str):
    """Sends a Telegram message; splits if longer than Telegram limits (~4096 chars)."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    CHUNK = 3500  # leave room for HTML tags
    if len(text) <= CHUNK:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=30)
    else:
        # chunk across multiple messages
        i = 0
        while i < len(text):
            chunk = text[i:i+CHUNK]
            requests.post(url, json={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "HTML"}, timeout=30)
            i += CHUNK

def ask_gpt_vision(image_data_url: str, table_html: str, now_iso_et: str) -> dict:
    """
    Sends image + HTML to a vision model and expects STRICT JSON back.
    Counts procedures (CT/MRI) older than AGE_MINUTES; supports multiple procedures in one row.
    """
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

    # force JSON parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise
        return json.loads(m.group(0))

# ----------------------------
# Main one-shot run
# ----------------------------
async def run_once():
    # Gate by time windows unless DRY_RUN is explicitly used to test outside windows as well
    if not DRY_RUN and not within_window_now():
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # --- LOGIN (adjust selectors if your login form differs) ---
        # Tries generic username/password selectors first.
        await page.fill('input[type="text"], input[name*="user" i]', AVR_USER)
        await page.fill('input[type="password"]', AVR_PASS)
        await page.click('input[type="submit"], button:has-text("Login"), button:has-text("Sign In")')
        await page.wait_for_load_state("networkidle")

        # If there is a "Worklist" tab to click, uncomment:
        # await page.click('a:has-text("Worklist")')
        # await page.wait_for_load_state("networkidle")

        # Extract HTML for the worklist table (best-effort; fall back to page HTML)
        table_html = ""
        try:
            # Find a table near the Worklist heading
            worklist_heading = page.locator("text=Worklist").first
            worklist_container = worklist_heading.locator("xpath=..")
            table = worklist_container.locator("xpath=.//table").first
            table_html = await table.evaluate("(el) => el.outerHTML")
        except Exception:
            try:
                # Fall back to the first table on the page
                table_html = await page.locator("xpath=(//table)[1]").evaluate("(el)=>el.outerHTML")
            except Exception:
                # Last resort: entire page HTML
                table_html = await page.content()

        # Screenshot for the model (full page is simplest & robust)
        png_bytes = await page.screenshot(full_page=True)

        await ctx.close()
        await browser.close()

    now_et_iso = datetime.now(TZ).isoformat()
    data_url = to_data_url(png_bytes)

    # Ask GPT Vision
    result = ask_gpt_vision(data_url, table_html, now_et_iso)

    # DRY-RUN: always post the JSON to Telegram (for validation)
    if DRY_RUN:
        pretty = json.dumps(result, indent=2)
        msg = f"🔍 <b>Dry-run JSON dump</b>\n<pre>{pretty}</pre>"
        send_telegram_text(msg)
        return

    # LIVE MODE: only alert when threshold is crossed
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
