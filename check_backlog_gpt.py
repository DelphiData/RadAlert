import os, re, io, asyncio, json, base64
from datetime import datetime
import pytz
import requests
from playwright.async_api import async_playwright

LOGIN_URL = "https://avrteleris.com/AVR/Index.aspx"
TZ = pytz.timezone("America/New_York")

AVR_USER = os.environ["AVR_USER"]
AVR_PASS = os.environ["AVR_PASS"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

THRESHOLD = int(os.environ.get("THRESHOLD", "25"))
AGE_MINUTES = int(os.environ.get("AGE_MINUTES", "60"))
SITE_LABEL = os.environ.get("SITE_LABEL", "Baptist Health Corbin (AVR)")
MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")  # any chat.completions vision model

def within_window_now():
    now = datetime.now(TZ)
    wd, hr = now.weekday(), now.hour  # Mon=0..Sun=6
    if wd in range(0,5) and hr in (18,20,22):  # Mon-Fri 6p,8p,10p
        return True
    if wd == 5 and hr in (4,6,8,10,12,14,16,18,20,22):  # Sat 4a..10p q2h
        return True
    return False

def to_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()

def ask_gpt_vision(image_data_url: str, table_html: str, now_iso_et: str) -> dict:
    """
    Sends image + html to the vision model. Expects strict JSON back.
    """
    system = (
        "You are a meticulous auditor. You extract counts from a radiology worklist screenshot and HTML."
        " Output STRICT JSON only, no prose."
    )
    user_prompt = f"""
You are given a screenshot and the corresponding HTML of the 'Worklist' table from a radiology prelim system.
Goal: Count all CT and MRI procedures that are > {AGE_MINUTES} minutes old at the current time (ET).

Rules:
- Count procedures, not rows.
- If a row's 'Study Requested' contains multiple CT/MRI studies (e.g., 'CT ABD PELVIS W/ IV, CT CHEST W/O' or 'MRI BRAIN, MRI C-SPINE'), count each CT/MRI occurrence separately.
- A procedure qualifies if its row's Date + Time (request time) is more than {AGE_MINUTES} minutes before NOW_ET.
- Ignore all non-CT, non-MRI studies (e.g., XRAY).
- If data is ambiguous, be conservative but do not hallucinate.
- Parse timestamps from the table; assume they are ET unless explicitly labeled otherwise.

NOW_ET (ISO8601): {now_iso_et}

Return JSON ONLY in this schema:
{{
  "count_ct_mri_over_60": <int>,
  "by_modality": {{
      "CT": <int>,
      "MRI": <int>
  }},
  "sample_ids_or_rows": [<up to 5 brief identifiers or row snippets you used>]
}}
"""

    # chat.completions style payload that supports image + text
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
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    # Ensure it's JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # try to salvage a JSON object inside
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=20)

async def run_once():
    if not within_window_now():
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # --- LOGIN (adjust selectors if needed) ---
        await page.fill('input[type="text"], input[name*="user" i]', AVR_USER)
        await page.fill('input[type="password"]', AVR_PASS)
        await page.click('input[type="submit"], button:has-text("Login"), button:has-text("Sign In")')
        await page.wait_for_load_state("networkidle")

        # If the app needs a click to show Worklist:
        # await page.click('a:has-text("Worklist")')
        # await page.wait_for_load_state("networkidle")

        # Try to narrow to the worklist container. If unknown, capture full page.
        # Get HTML of the main table area to help GPT read cleanly:
        table_html = ""
        try:
            # Heuristic: table following a heading 'Worklist'
            worklist = page.locator("text=Worklist").locator("xpath=..")
            table = worklist.locator("xpath=.//table").first
            table_html = await table.evaluate("(el) => el.outerHTML")
        except:
            try:
                table_html = await page.locator("xpath=(//table)[1]").evaluate("(el)=>el.outerHTML")
            except:
                table_html = await page.content()

        # Screenshot of the visible page (you can also screenshot table bounding box if stable)
        png = await page.screenshot(full_page=True)
        await ctx.close(); await browser.close()

    now_et_iso = datetime.now(TZ).isoformat()
    data_url = to_data_url(png)

    result = ask_gpt_vision(data_url, table_html, now_et_iso)
    ct_mri = int(result.get("count_ct_mri_over_60", 0))
    by_mod = result.get("by_modality", {})
    ct = by_mod.get("CT", 0); mri = by_mod.get("MRI", 0)

    if ct_mri > THRESHOLD:
        stamp = datetime.now(TZ).strftime("%-I:%M %p %Z")
        msg = (f"🟡 <b>Backlog alert</b> — {SITE_LABEL}\n"
               f"CT/MRI > {AGE_MINUTES} min old: <b>{ct_mri}</b> "
               f"(CT: {ct}, MRI: {mri}) at {stamp}\n"
               f"{LOGIN_URL}")
        send_telegram(msg)

if __name__ == "__main__":
    asyncio.run(run_once())
