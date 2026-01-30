import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from mailer import send_email

# =========================
# Configuration
# =========================
STATE_PATH = Path("state.json")
SYMBOL = os.getenv("SYMBOL", "KBANK")
LANG = os.getenv("LANG", "th")  # "th" or "en"

API_URL = "https://www.set.or.th/api/set/news/search"

HEADLINE_FILTER = os.getenv(
    "HEADLINE_FILTER",
    "รายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน",
).strip()
FILTER_MODE = os.getenv("FILTER_MODE", "exact").strip().lower()  # exact | contains

EMAIL_SUBJECT_TEMPLATE = "[SET Alert] {SYMBOL} Buyback - {DATE}"

# =========================
# Utility functions
# =========================
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
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def extract_id(item: dict) -> str:
    for k in ("id", "newsId", "news_id"):
        v = item.get(k)
        if v:
            return str(v)
    return json.dumps(item, sort_keys=True, ensure_ascii=False)

def extract_headline(item: dict) -> str:
    return str(item.get("headline", "")).strip()

def extract_datetime(item: dict) -> str:
    return str(item.get("datetime", "")).strip()

def extract_url(item: dict) -> str:
    if item.get("url"):
        return item["url"]
    _id = extract_id(item)
    lang = "th" if LANG == "th" else "en"
    return f"https://www.set.or.th/{lang}/market/news-and-alert/newsdetails?id={_id}&symbol={SYMBOL}"

def headline_matches(headline: str) -> bool:
    if FILTER_MODE == "contains":
        return HEADLINE_FILTER in headline
    return headline == HEADLINE_FILTER

# =========================
# HTTP helpers
# =========================
def _browser_headers_html() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8",
    }

def _browser_headers_json(referer: str) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": referer,
        "Origin": "https://www.set.or.th",
        "X-Requested-With": "XMLHttpRequest",
    }

def make_session() -> tuple[requests.Session, str]:
    session = requests.Session()
    warm_url = f"https://www.set.or.th/th/market/product/stock/quote/{SYMBOL}/news"
    session.get(warm_url, headers=_browser_headers_html(), timeout=30)
    return session, warm_url

def fetch_news(
    session: requests.Session, warm_url: str, from_date: str, to_date: str
) -> list[dict[str, Any]]:
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
        headers=_browser_headers_json(warm_url),
        timeout=30,
    )
    r.raise_for_status()

    data = r.json()
    return data.get("newsInfoList", [])

# =========================
# Detail page parsing (line-based)
# =========================
def fetch_news_detail_text_lines(
    session: requests.Session, url: str
) -> Optional[list[str]]:
    try:
        r = session.get(url, headers=_browser_headers_html(), timeout=30)
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw = soup.get_text("\n", strip=True)

    start = raw.find("รายงานผลการซื้อหุ้นคืน")
    end = raw.find("สารสนเทศฉบับนี้จัดทำและเผยแพร่")
    if start == -1:
        start = 0
    if end == -1:
        end = len(raw)

    clipped = raw[start:end]

    lines = []
    for ln in clipped.split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            lines.append(ln)

    return lines

def parse_kv_from_lines(lines: list[str]) -> dict[str, str]:
    kv = {}
    for ln in lines:
        if ":" in ln:
            k, v = ln.split(":", 1)
            kv[k.strip()] = v.strip()
    return kv

def format_buyback_summary(kv: dict[str, str], link: str, api_dt: str) -> str:
    g = lambda k: kv.get(k, "-")

    return "\n".join(
        [
            "รายงานผลการซื้อหุ้นคืน (สรุป)",
            f"Time (SET): {api_dt}",
            f"Link: {link}",
            "",
            f"เรื่อง: {g('เรื่อง')}",
            f"วันที่รายงานผล: {g('วันที่รายงานผล')}",
            f"วิธีการซื้อหุ้นคืน: {g('วิธีการซื้อหุ้นคืน')}",
            f"วันที่ครบกำหนดโครงการ: {g('วันที่ครบกำหนดโครงการ')}",
            f"วันที่คณะกรรมการมีมติ: {g('วันที่คณะกรรมการมีมติ')}",
            f"จำนวนหุ้นซื้อคืนสูงสุดตามโครงการ (หุ้น): {g('จำนวนหุ้นซื้อคืนสูงสุดตามโครงการ (หุ้น)')}",
            f"%ของจำนวนหุ้นซื้อคืนสูงสุดต่อจำนวนหุ้นที่ชำระแล้ว: {g('%ของจำนวนหุ้นซื้อคืนสูงสุดต่อจำนวนหุ้นที่ชำระแล้ว')}",
            "",
            "1) ผลการซื้อหุ้นคืน",
            f"วันที่ซื้อหุ้นคืน: {g('วันที่ซื้อหุ้นคืน')}",
            f"จำนวนหุ้นซื้อคืน(หุ้น): {g('จำนวนหุ้นซื้อคืน(หุ้น)')}",
            f"ราคาที่ซื้อต่อหุ้นหรือราคาสูงสุด(บาท/หุ้น): {g('ราคาที่ซื้อต่อหุ้นหรือราคาสูงสุด(บาท/หุ้น)')}",
            f"ราคาต่ำสุด(บาท/หุ้น): {g('ราคาต่ำสุด(บาท/หุ้น)')}",
            f"มูลค่ารวม(บาท): {g('มูลค่ารวม(บาท)')}",
            "",
            "2) จำนวนหุ้นซื้อคืนทั้งสิ้น",
            f"จำนวนรวมของหุ้นซื้อคืนในโครงการจนถึงปัจจุบัน: {g('จำนวนรวมของหุ้นซื้อคืนในโครงการจนถึงปัจจุบัน')}",
            f"%ของจำนวนหุ้นซื้อคืนต่อจำนวนหุ้นที่ชำระแล้ว: {g('%ของจำนวนหุ้นซื้อคืนต่อจำนวนหุ้นที่ชำระแล้ว')}",
            f"มูลค่ารวมที่ซื้อคืน(บาท): {g('มูลค่ารวมที่ซื้อคืน(บาท)')}",
        ]
    )

# =========================
# Main
# =========================
def main() -> None:
    force_send = os.getenv("FORCE_SEND", "0") == "1"

    state = load_state()
    seen = set(state.get("seen_ids", []))

    lookback_days = int(os.getenv("LOOKBACK_DAYS", "60"))
    max_new_items = int(os.getenv("MAX_NEW_ITEMS", "5"))

    today = datetime.now()
    from_dt = today - timedelta(days=lookback_days)

    session, warm_url = make_session()
    items = fetch_news(
        session, warm_url, ddmmyyyy(from_dt), ddmmyyyy(today)
    )

    # Filter by headline
    filtered = [
        it for it in items if headline_matches(extract_headline(it))
    ]

    new_items = [
        it for it in filtered if extract_id(it) not in seen
    ]

    if not new_items and force_send and filtered:
        new_items = filtered[:1]

    if not new_items:
        print("No new news.")
        return

    notify_items = new_items[:max_new_items]

    # Subject date from first item
    first_dt = extract_datetime(notify_items[0])
    try:
        subject_date = datetime.fromisoformat(first_dt[:19]).strftime("%d%m%Y")
    except Exception:
        subject_date = today.strftime("%d%m%Y")

    subject = EMAIL_SUBJECT_TEMPLATE.format(
        SYMBOL=SYMBOL, DATE=subject_date
    )

    blocks = []
    for it in notify_items:
        url = extract_url(it)
        api_dt = extract_datetime(it)

        lines = fetch_news_detail_text_lines(session, url)
        if not lines:
            continue

        kv = parse_kv_from_lines(lines)
        blocks.append(format_buyback_summary(kv, url, api_dt))

    body = ("\n\n" + "-" * 60 + "\n\n").join(blocks)

    send_email(subject=subject, body=body, attachments=[])
    print("Email sent.")

    for it in new_items:
        seen.add(extract_id(it))

    state["seen_ids"] = list(seen)
    save_state(state)

if __name__ == "__main__":
    main()
