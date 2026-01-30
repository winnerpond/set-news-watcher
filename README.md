# SET News Watcher (KBANK Buyback Alert)

A GitHub Actions automation that monitors SET (Stock Exchange of Thailand) news for a specific stock symbol and sends an email alert when a new **buyback report** news item appears.

Current default use case:
- Symbol: **KBANK**
- News headline filter (Thai): **รายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน**
- Email subject format: **[SET Alert] KBANK Buyback - ddMMyyyy**
- Email body: Extracted structured fields from the news detail page (no PDF download)

## How it works

1. Runs on a schedule (cron) and/or manual trigger (`workflow_dispatch`)
2. Calls SET news API to list recent news items for the symbol
3. Filters only the target headline (exact match or contains)
4. Compares news IDs with `state.json` to detect **new** items only
5. Opens the SET news detail page and extracts the buyback report fields
6. Sends one email alert
7. Updates `state.json` and commits it back to the repo (so future runs won’t re-send)

## Project structure
set-news-watcher/
│
├── .github/
│   └── workflows/
│       └── set-news-watcher.yml        # GitHub Actions workflow
│
├── app/
│   ├── __init__.py
│   ├── config.py                       # Constants & env configuration
│   ├── fetch_set_news.py               # SET API + detail page fetching
│   ├── parse_buyback.py                # Buyback content extraction
│   ├── notifier.py                     # Email formatting & sending
│   └── state.py                        # state.json read/write logic
│
├── scripts/
│   └── run_watcher.py                  # Entry point (calls app logic)
│
├── state/
│   └── state.json                      # Persisted seen news IDs
│
├── requirements.txt
├── README.md
└── .gitignore

## Requirements

- Python 3.11
- GitHub Actions runner (Ubuntu)
- Gmail SMTP app password (recommended)

Python libraries:
- `requests`
- `beautifulsoup4`


## Setup
### 1) Gmail App Password

Use a Gmail account with 2-step verification enabled, then create an **App Password**.
You will store it in GitHub Secrets (never hardcode it).

### 2) GitHub Secrets
Create these repository secrets:

| Secret name | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `your.email@gmail.com` |
| `SMTP_PASS` | Gmail App Password (16 chars) |
| `EMAIL_FROM` | `your.email@gmail.com` |
| `EMAIL_TO` | `you@company.com` (or multiple separated by comma if your mailer supports i

> Note: `EMAIL_TO` can be your bank email if your corporate policy allows receiving external email.

### 3) Workflow configuration (env)

In your workflow file, you can configure:

- `SYMBOL` (default KBANK)
- `LANG` (th/en)
- `LOOKBACK_DAYS` (recommended 60 for infrequent announcements)
- `MAX_NEW_ITEMS` (kept for safety; normally you’ll only send one email)
- `HEADLINE_FILTER` and `FILTER_MODE` (exact/contains)
- `FORCE_SEND` (test only)

Example env:

```yaml
env:
  SYMBOL: "KBANK"
  LANG: "th"
  LOOKBACK_DAYS: "60"
  MAX_NEW_ITEMS: "5"
  HEADLINE_FILTER: "รายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน"
  FILTER_MODE: "exact"
