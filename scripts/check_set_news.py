import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import requests

from mailer import send_email

STATE_PATH = Path("state.json")
DOWNLOAD_DIR = Path("downloads")
SYMBOL = os.getenv("SYMBOL", "KBANK")
LANG = os.getenv("LANG", "th")  # "th" or "en"

API_URL = "https://www.set.or.th/api/set/news/search"

def ddmmyyyy(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))

def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def fetch_news(from_date: str, to_date: str) -> list[dict]:
    params = {
        "symbol": SYMBOL,
        "fromDate": from_date,
        "toDate": to_date,
        "keyword": "",
        "lang": LANG,
    }

    session = requests.Session()

    # 1) Warm up cookies by visiting the real page first
    warm_url = f"https://www.set.or.th/{'th' if LANG=='th' else 'en'}/market/product/stock/quote/{SYMBOL}/news"
    session.get(
        warm_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        timeout=30,
    )

    # 2) Call the API with browser-like headers + referer
    r = session.get(
        API_URL,
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": warm_url,
            "Origin": "https://www.set.or.th",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )

    # If still blocked, show the response body in logs for debugging
    if r.status_code == 403:
        raise RuntimeError(f"403 Forbidden from SET API. Response text: {r.text[:300]}")

    r.raise_for_status()
    data = r.json()
    
    if isinstance(data, dict):
        for k in ("news", "data", "result"):
            if k in data and isinstance(data[k], list):
                return data[k]
    if isinstance(data, list):
        return data
    return []

def extract_id(item: dict) -> str:
    # prefer stable numeric/string id
    for k in ("id", "newsId", "news_id"):
        if k in item and item[k]:
            return str(item[k])
    # fallback to URL if present
    for k in ("url", "link"):
        if k in item and item[k]:
            return str(item[k])
    return json.dumps(item, sort_keys=True)

def extract_title(item: dict) -> str:
    for k in ("headline", "title", "subject"):
        if k in item and item[k]:
            return str(item[k])
    return "(no title)"

def extract_detail_url(item: dict) -> str | None:
    # Sometimes the API returns a details URL; if not, construct common pattern if fields exist
    for k in ("url", "link", "detailUrl", "detailsUrl"):
        if k in item and item[k]:
            return str(item[k])
    # If API gives id + symbol, SET details pages often look like:
    # https://www.set.or.th/en/market/news-and-alert/newsdetails?id=XXXX&symbol=KBANK
    _id = extract_id(item)
    return f"https://www.set.or.th/{'en' if LANG=='en' else 'th'}/market/news-and-alert/newsdetails?id={_id}&symbol={SYMBOL}"

def download_attachments(item: dict) -> list[Path]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # API sometimes returns "documents" / "files" with URLs
    candidates = []
    for k in ("documents", "files", "attachments"):
        if k in item and isinstance(item[k], list):
            candidates.extend(item[k])

    paths: list[Path] = []
    headers = {"User-Agent": "Mozilla/5.0 (GitHub Actions)"}

    for doc in candidates:
        if not isinstance(doc, dict):
            continue
        url = doc.get("url") or doc.get("link")
        name = doc.get("name") or doc.get("fileName") or None
        if not url:
            continue

        # Basic filename
        if not name:
            name = url.split("?")[0].split("/")[-1] or "attachment.bin"

        out = DOWNLOAD_DIR / name
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        out.write_bytes(resp.content)
        paths.append(out)

    return paths

def main():
    # --- SMTP test mode (manual run) ---
    if os.getenv("SMTP_TEST", "0") == "1":
        send_email(
            subject=f"SMTP TEST: SET watcher ({SYMBOL})",
            body="If you got this email, SMTP secrets are working.",
            attachments=[]
        )
        print("SMTP test email sent.")
        return
        
    state = load_state()
    seen = set(state.get("seen_ids", []))

    today = datetime.now()
    from_dt = today - timedelta(days=int(os.getenv("LOOKBACK_DAYS", "14")))

    items = fetch_news(ddmmyyyy(from_dt), ddmmyyyy(today))

    # Sort newest-first if date exists
    # (leave as-is if not)
    def sort_key(x):
        for k in ("datetime", "dateTime", "publishDate", "date"):
            if k in x and x[k]:
                return str(x[k])
        return ""
    items = sorted(items, key=sort_key, reverse=True)

    new_items = []
    for it in items:
        _id = extract_id(it)
        if _id not in seen:
            new_items.append(it)

    if not new_items:
        print("No new news.")
        return

    max_items = int(os.getenv("MAX_NEW_ITEMS", "5"))
    new_items = new_items[:max_items]
  
    lines = []
    all_attachments: list[Path] = []

    for it in new_items:
        _id = extract_id(it)
        title = extract_title(it)
        url = extract_detail_url(it)

        lines.append(f"- {title}\n  {url}")

        # Try to download attachments if API includes them (safe if none)
        try:
            all_attachments.extend(download_attachments(it))
        except Exception as e:
            print(f"Attachment download failed for {_id}: {e}")

        seen.add(_id)

    state["seen_ids"] = list(seen)
    save_state(state)

    subject = f"SET Alert ({SYMBOL}): {len(new_items)} new item(s)"
    body = "\n\n".join(lines)

    # email only if SMTP env vars exist
    if os.getenv("SMTP_HOST"):
        send_email(subject=subject, body=body, attachments=all_attachments)
        print("Email sent.")
    else:
        print("SMTP not configured; printing news:\n")
        print(body)
   
    # --- SMTP test mode (manual run) ---
    if os.getenv("SMTP_TEST", "0") == "1":
        send_email(
            subject=f"SMTP TEST: SET watcher ({SYMBOL})",
            body="If you got this email, SMTP secrets are working.",
            attachments=[]
        )
        print("SMTP test email sent.")
        return

if __name__ == "__main__":
    main()
