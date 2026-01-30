import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from mailer import send_email

STATE_PATH = Path("state.json")
SYMBOL = os.getenv("SYMBOL", "KBANK")
LANG = os.getenv("LANG", "th")  # "th" or "en"

API_URL = "https://www.set.or.th/api/set/news/search"


def ddmmyyyy(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        # If state is corrupted, fail safe by resetting
        return {"seen_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_id(item: dict) -> str:
    # Prefer stable IDs returned by SET
    for k in ("id", "newsId", "news_id"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)

    # Fallback to URL if present
    for k in ("url", "link", "detailUrl", "detailsUrl"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)

    # Last resort: stable JSON string
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def extract_title(item: dict) -> str:
    for k in ("headline", "title", "subject"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return "(no title)"


def extract_publish_dt(item: dict) -> str:
    # We don't assume exact field names; log whichever exists
    for k in ("datetime", "dateTime", "publishDate", "publish_date", "date"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def build_detail_url(item: dict) -> str:
    # If API provides a direct URL, use it
    for k in ("url", "link", "detailUrl", "detailsUrl"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)

    # Otherwise build a SET news detail URL (commonly works)
    _id = extract_id(item)
    lang_path = "th" if LANG == "th" else "en"
    return f"https://www.set.or.th/{lang_path}/market/news-and-alert/newsdetails?id={_id}&symbol={SYMBOL}"


def _browser_headers_html() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def _browser_headers_json(referer: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "Origin": "https://www.set.or.th",
        "X-Requested-With": "XMLHttpRequest",
    }


def _find_list_of_dicts(obj):
    """Return the first list found that looks like a list[dict]."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_list_of_dicts(v)
            if found is not None:
                return found
    return None

def fetch_news(from_date: str, to_date: str) -> list[dict]:
    params = {
        "symbol": SYMBOL,
        "fromDate": from_date,
        "toDate": to_date,
        "keyword": "",
        "lang": LANG,
    }

    session = requests.Session()
    warm_url = f"https://www.set.or.th/{'th' if LANG=='th' else 'en'}/market/product/stock/quote/{SYMBOL}/news"

    # warm up cookies
    session.get(warm_url, headers=_browser_headers_html(), timeout=30)

    r = session.get(
        API_URL,
        params=params,
        headers=_browser_headers_json(referer=warm_url),
        timeout=30,
    )

    if r.status_code == 403:
        raise RuntimeError(f"403 Forbidden from SET API. Body (first 300 chars): {r.text[:300]}")
    r.raise_for_status()

    data = r.json()

    # DEBUG: show the response shape in Actions logs (optional)
    if os.getenv("DEBUG_JSON", "0") == "1":
        import json
        print("DEBUG_JSON response keys/type:", type(data))
        print(json.dumps(data, ensure_ascii=False)[:2000])  # first 2000 chars

    found = _find_list_of_dicts(data)
    return found or []]

def main() -> None:
    # --- optional test modes ---
    smtp_test = os.getenv("SMTP_TEST", "0") == "1"
    force_send = os.getenv("FORCE_SEND", "0") == "1"
    dry_run = os.getenv("DRY_RUN", "0") == "1"

    if smtp_test:
        print("SMTP_TEST=1 detected. Sending test email now...")
        send_email(
            subject=f"SMTP TEST: SET watcher ({SYMBOL})",
            body="If you got this email, Gmail SMTP secrets are working.",
            attachments=[],
        )
        print("SMTP test email sent.")
        return

    state = load_state()
    seen = set(state.get("seen_ids", []))

    lookback_days = int(os.getenv("LOOKBACK_DAYS", "14"))
    max_new_items = int(os.getenv("MAX_NEW_ITEMS", "5"))

    today = datetime.now()
    from_dt = today - timedelta(days=lookback_days)

    print(f"Symbol={SYMBOL} Lang={LANG}")
    print(f"Lookback: {lookback_days} day(s)  Range: {ddmmyyyy(from_dt)} -> {ddmmyyyy(today)}")
    print(f"Seen IDs in state: {len(seen)}")

    items = fetch_news(ddmmyyyy(from_dt), ddmmyyyy(today))
    print(f"Fetched {len(items)} item(s) from SET API")

    # Show sample IDs for debugging
    for it in items[:3]:
        print("Sample:", extract_id(it), "|", extract_publish_dt(it), "|", extract_title(it)[:80])

    # Determine new items
    new_items: list[dict] = []
    for it in items:
        _id = extract_id(it)
        if _id not in seen:
            new_items.append(it)

    print(f"Computed new items: {len(new_items)}")

    # If nothing new, optionally force-send a demo (latest 1 item)
    if not new_items and force_send:
        print("FORCE_SEND=1 enabled. Using latest 1 item as demo.")
        new_items = items[:1]

    if not new_items:
        print("No new news.")
        return

    # Cap
    new_items = new_items[:max_new_items]
    print(f"Will notify {len(new_items)} item(s) (capped by MAX_NEW_ITEMS={max_new_items})")

    # Build email body
    lines: list[str] = []
    newly_seen_ids: list[str] = []

    for it in new_items:
        _id = extract_id(it)
        title = extract_title(it)
        pub = extract_publish_dt(it)
        url = build_detail_url(it)

        block = f"- {title}"
        if pub:
            block += f"\n  Date: {pub}"
        block += f"\n  Link: {url}"

        lines.append(block)
        newly_seen_ids.append(_id)

    subject = f"SET Alert ({SYMBOL}): {len(new_items)} item(s)"
    body = "\n\n".join(lines)

    # Send email (or dry run)
    if os.getenv("SMTP_HOST"):
        if dry_run:
            print("DRY_RUN=1 enabled. Would send email:")
            print("SUBJECT:", subject)
            print("BODY:\n", body)
        else:
            send_email(subject=subject, body=body, attachments=[])
            print("Email sent.")
    else:
        print("SMTP not configured; printing instead:\n")
        print(subject)
        print(body)

    # Update state only if we successfully reached here (even in DRY_RUN)
    for _id in newly_seen_ids:
        seen.add(_id)

    state["seen_ids"] = list(seen)
    save_state(state)
    print(f"State updated. Total seen IDs now: {len(seen)}")

if __name__ == "__main__":
    main()
