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
    "รายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน",
).strip()
FILTER_MODE = os.getenv("FILTER_MODE", "exact").strip().lower()  # exact | contains


# ---------- helpers ----------
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

    # Fallback keys
    if isinstance(data, dict):
        for k in ("news", "data", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v

    return []


# ---------- detail page extraction (LINE-BASED, not table-based) ----------
def fetch_news_detail_text_lines(session: requests.Session, detail_url: str) -> Optional[list[str]]:
    """
    Fetch newsdetails page and return a list of clean text lines.
    This works because the buyback content is plain text lines, not <table><td>.
    """
    try:
        resp = session.get(detail_url, headers=_browser_headers_html(), timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"Detail fetch failed: {detail_url} | {repr(e)}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Preserve newlines (key point)
    raw = soup.get_text("\n", strip=True)

    # Keep only the relevant section to reduce noise
    start_markers = ["รายงานผลการซื้อหุ้นคืน", "รายละเอียดแบบเต็ม"]
    end_marker = "สารสนเทศฉบับนี้จัดทำและเผยแพร่"

    start_idx = 0
    for m in start_markers:
        i = raw.find(m)
        if i != -1:
            start_idx = i
            break

    end_idx = raw.find(end_marker)
    if end_idx == -1:
        end_idx = len(raw)

    clipped = raw[start_idx:end_idx]

    lines = []
    for ln in clipped.split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            lines.append(ln)

    return lines


def parse_kv_from_lines(lines: list[str]) -> dict[str, str]:
    """
    Parse lines formatted like:
    'เรื่อง : แบบรายงาน...'
    into { 'เรื่อง': 'แบบรายงาน...' }
    """
    kv: dict[str, str] = {}
    for ln in lines:
        if ":" in ln:
            left, right = ln.split(":", 1)
            key = left.strip()
            val = right.strip()
            if key and val:
                kv[key] = val
    return kv


def format_buyback_summary(kv: dict[str, str], link: str, api_dt: str) -> str:
    def g(k: str) -> str:
        return kv.get(k, "-")

    # Labels on the page (Thai)
    lines = [
        "รายงานผลการซื้อหุ้นคืน (สรุป)",
        f"Time (SET): {api_dt or '-'}",
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
    return "\n".join(lines)


# ---------- main ----------
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

    lookback_days = int(os.getenv("LOOKBACK_DAYS", "60"))
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

    # Force demo using the latest matching item
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

    # ONE EMAIL ONLY: email up to MAX_NEW_ITEMS, but mark ALL new items as seen.
    notify_items = all_new_items[:max_new_items]

    blocks: list[str] = []
    for it in notify_items:
        api_dt = extract_datetime(it)
        url = extract_url(it)

        lines = fetch_news_detail_text_lines(session, url)
        if not lines:
            blocks.append(
                f"รายงานผลการซื้อหุ้นคืน (สรุป)\nTime (SET): {api_dt or '-'}\nLink: {url}\n\n(ไม่สามารถดึงข้อความจากหน้าได้)"
            )
            continue

        kv = parse_kv_from_lines(lines)

        # If parsing produced nothing, include a small debug snippet
        if not kv:
            snippet = "\n".join(lines[:30])
            blocks.append(
                f"รายงานผลการซื้อหุ้นคืน (สรุป)\nTime (SET): {api_dt or '-'}\nLink: {url}\n\n"
                f"(ไม่สามารถ parse เป็น key:value ได้)\n\n--- snippet ---\n{snippet}"
            )
            continue

        blocks.append(format_buyback_summary(kv, link=url, api_dt=api_dt))

    subject = f"SET Alert ({SYMBOL}): {len(all_new_items)} new item(s) [filtered]"
    body = ("\n\n" + ("-" * 60) + "\n\n").join(blocks)

    if os.getenv("SMTP_HOST"):
        if dry_run:
            print("DRY_RUN=1 enabled. Would send email:")
            print("SUBJECT:", subject)
            print("BODY:\n", body[:2500])
        else:
            send_email(subject=subject, body=body, attachments=[])
            print("Email sent.")
    else:
        print("SMTP not configured; printing instead:\n")
        print(subject)
        print(body[:2500])

    # Mark ALL new items as seen
    for it in all_new_items:
        seen.add(extract_id(it))

    state["seen_ids"] = list(seen)
    save_state(state)
    print(f"State updated. Total seen IDs now: {len(seen)}")


if __name__ == "__main__":
    main()
