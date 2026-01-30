import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from mailer import send_email

STATE_PATH = Path("state.json")
SYMBOL = os.getenv("SYMBOL", "KBANK")
LANG = os.getenv("LANG", "th")  # "th" or "en"

API_URL = "https://www.set.or.th/api/set/news/search"

# Filter controls
HEADLINE_FILTER = os.getenv(
    "HEADLINE_FILTER",
    "รายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน"
).strip()
FILTER_MODE = os.getenv("FILTER_MODE", "exact").strip().lower()  # exact | contains

# How much text from detail page to include in email
DETAIL_MAX_CHARS = int(os.getenv("DETAIL_MAX_CHARS", "1200"))


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


def extract_url(item: dict) -> str:
    v = item.get("url")
    if v and str(v).strip():
        return str(v).strip()
    # fallback build
    _id = extract_id(item)
    lang_path = "th" if LANG == "th" else "en"
    return f"https://www.set.or.th/{lang_path}/market/news-and-alert/newsdetails?id={_id}&symbol={SYMBOL}"


def headline_matches(headline: str) -> bool:
    if not HEADLINE_FILTER:
        return True
    if FILTER_MODE == "contains":
        return HEADLINE_FILTER in headline
    return headline == HEADLINE_FILTER


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


def make_session() -> tuple[requests.Session, str]:
    """Create a requests session and warm it up against the quote/news page to avoid 403."""
    session = requests.Session()
    warm_url = f"https://www.set.or.th/{'th' if LANG=='th' else 'en'}/market/product/stock/quote/{SYMBOL}/news"
    session.get(warm_url, headers=_browser_headers_html(), timeout=30)
    return session, warm_url


def fetch_news(session: requests.Session, warm_url: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    params = {
        "symbol": SYMBOL,
        "fromDate": from_date,
        "toDate": to_date,
        "keyword": "",
        "lang": LANG,
    }

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

    # Confirmed structure: {"totalCount": ..., "newsInfoList": [...]}
    if isinstance(data, dict) and isinstance(data.get("newsInfoList"), list):
        return data["newsInfoList"]

    # fallback keys
    if isinstance(data, dict):
        for k in ("news", "data", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v

    return []


def _clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t


def fetch_news_detail_text(session: requests.Session, detail_url: str) -> Optional[str]:
    """
    Fetch the news detail page and extract meaningful text.
    We keep it robust by using visible text from the page, then truncating.
    """
    try:
        resp = session.get(detail_url, headers=_browser_headers_html(), timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"Detail fetch failed: {detail_url} | {repr(e)}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Try a few likely containers first (if SET changes layout, we fall back to full text)
    candidates = []

    # common-ish containers (best-effort)
    for sel in [
        "main",
        "article",
        "div[class*='news']",
        "div[class*='content']",
        "div[class*='detail']",
    ]:
        el = soup.select_one(sel)
        if el:
            candidates.append(el.get_text(" ", strip=True))

    if not candidates:
        candidates.append(soup.get_text(" ", strip=True))

    text = _clean_text(max(candidates, key=len))

    # Keep only a reasonable chunk
    if len(text) > DETAIL_MAX_CHARS:
        text = text[:DETAIL_MAX_CHARS].rstrip() + "…"

    return text


def main() -> None:
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
    print(f"Headline filter mode={FILTER_MODE} filter='{HEADLINE_FILTER}'")

    session, warm_url = make_session()
    items = fetch_news(session, warm_url, ddmmyyyy(from_dt), ddmmyyyy(today))
    print(f"Fetched {len(items)} item(s) from SET API")

    # Filter by headline first
    filtered_items: list[dict] = []
    for it in items:
        hl = extract_headline(it)
        if headline_matches(hl):
            filtered_items.append(it)
    print(f"Filtered items (headline match): {len(filtered_items)}")

    # Compute new items only from filtered list
    all_new_items: list[dict] = []
    for it in filtered_items:
        _id = extract_id(it)
        if _id not in seen:
            all_new_items.append(it)
    print(f"Computed new items (after filter): {len(all_new_items)}")

    # Force demo using latest matching item
    if not all_new_items and force_send:
        if filtered_items:
            print("FORCE_SEND=1 enabled. Using latest matching item as demo.")
            all_new_items = filtered_items[:1]
        else:
            print("FORCE_SEND=1 enabled, but no matching items exist in lookback window.")
            return

    if not all_new_items:
        print("No new news.")
        return

    # ONE EMAIL ONLY:
    notify_items = all_new_items[:max_new_items]

    # Build email body (headline + timestamp + link + extracted detail text)
    blocks: list[str] = []
    for it in notify_items:
        headline = extract_headline(it)
        dt = extract_datetime(it)
        url = extract_url(it)

        detail_text = fetch_news_detail_text(session, url) or "(Could not extract detail text)"

        block = (
            f"• {headline}\n"
            f"  Time: {dt or '(no timestamp)'}\n"
            f"  Link: {url}\n"
            f"  Detail: {detail_text}\n"
        )
        blocks.append(block)

    subject = f"SET Alert ({SYMBOL}): {len(all_new_items)} new item(s) [filtered]"
    body = "\n\n".join(blocks)

    if os.getenv("SMTP_HOST"):
        if dry_run:
            print("DRY_RUN=1 enabled. Would send email:")
            print("SUBJECT:", subject)
            print("BODY:\n", body[:2000])
        else:
            send_email(subject=subject, body=body, attachments=[])
            print("Email sent.")
    else:
        print("SMTP not configured; printing instead:\n")
        print(subject)
        print(body[:2000])

    # Mark ALL new items as seen (even those not emailed due to cap)
    for it in all_new_items:
        seen.add(extract_id(it))
    state["seen_ids"] = list(seen)
    save_state(state)
    print(f"State updated. Total seen IDs now: {len(seen)}")


if __name__ == "__main__":
    main()
