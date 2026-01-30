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
        return {"seen_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_id(item: dict) -> str:
    for k in ("id", "newsId", "news_id"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)
    for k in ("url", "link", "detailUrl", "detailsUrl"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v)
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def extract_headline(item: dict) -> str:
    for k in ("headline", "title", "subject"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return "(no headline)"


def extract_datetime(item: dict) -> str:
    for k in ("datetime", "dateTime", "publishDate", "publish_date", "date"):
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


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


def fetch_news(from_date: str, to_date: str) -> list[dict[str, Any]]:
    params = {
        "symbol": SYMBOL,
        "fromDate": from_date,
        "toDate": to_date,
        "keyword": "",
        "lang": LANG,
    }

    session = requests.Session()

    warm_url = f"https://www.set.or.th/{'th' if LANG=='th' else 'en'}/market/product/stock/quote/{SYMBOL}/news"
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

    # ✅ Confirmed structure from your DEBUG: {"totalCount": ..., "newsInfoList": [...]}
    if isinstance(data, dict) and isinstance(data.get("newsInfoList"), list):
        return data["newsInfoList"]

    # Fallbacks (in case SET changes structure later)
    if isinstance(data, dict):
        for k in ("news", "data", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v

    return []


def main() -> None:
    # Optional test flags
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

    # Compute ALL truly new items
    all_new_items: list[dict] = []
    for it in items:
        _id = extract_id(it)
        if _id not in seen:
            all_new_items.append(it)

    print(f"Computed new items: {len(all_new_items)}")

    # If nothing new, optionally force a demo
    if not all_new_items and force_send:
        print("FORCE_SEND=1 enabled. Using latest 1 item as demo.")
        all_new_items = items[:1]

    if not all_new_items:
        print("No new news.")
        return

    # ✅ ONE EMAIL ONLY behavior:
    # We email at most MAX_NEW_ITEMS, but we mark ALL new items as seen.
    notify_items = all_new_items[:max_new_items]

    # Build “headlines only” email body
    lines: list[str] = []
    for it in notify_items:
        headline = extract_headline(it)
        dt = extract_datetime(it)
        # If you want *only* headline and nothing else, remove the date line below.
        if dt:
            lines.append(f"- {headline} ({dt})")
        else:
            lines.append(f"- {headline}")

    subject = f"SET Alert ({SYMBOL}): {len(all_new_items)} new item(s)"
    body = "\n".join(lines)

    # Send or dry run
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

    # ✅ Mark ALL new items as seen (even those not emailed due to cap)
    for it in all_new_items:
        seen.add(extract_id(it))

    state["seen_ids"] = list(seen)
    save_state(state)
    print(f"State updated. Total seen IDs now: {len(seen)}")


if __name__ == "__main__":
    main()
