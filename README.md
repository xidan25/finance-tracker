# finance-tracker

A small, self-hosted personal finance tracker. It reads your credit-card
alert emails over IMAP, classifies each transaction with an LLM, stores them in
a local SQLite database, and syncs them to Notion. It can also generate a
monthly spending report.

Everything runs on your own machine against your own accounts — there is no
hosted service and no data leaves your control except the API calls you
configure (Anthropic + Notion).

## How it works

```
Gmail (card alerts)  ──IMAP──▶  ingest  ──LLM classify──▶  SQLite  ──▶  Notion
                                                             │
                                                             └──▶  monthly report
```

- **Ingest** — polls one Gmail label per card, parses the alert emails, and
  extracts merchant / amount / currency / date.
- **Classify** — Anthropic Claude assigns a category and normalizes the
  merchant name.
- **Store** — everything lands in a local SQLite DB (`data/finance.db`).
  Amounts are also converted to a single base currency (SGD) so totals are one
  meaningful number; the original amount/currency are always preserved.
- **Sync** — pushes rows to a Notion database so you can browse/filter them.
- **Report** — generates a Markdown monthly summary.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (or plain `pip` + venv)
- A Gmail account with card-alert emails and an **App Password**
- An **Anthropic API key** — https://console.anthropic.com
- A **Notion** internal integration + a page to hold the databases

## Setup

### 1. Install

```bash
git clone <your-fork-url> finance-tracker
cd finance-tracker
uv sync            # or: python -m venv .venv && pip install -e .
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Then edit `.env`:

- **`ANTHROPIC_API_KEY`** — from the Anthropic console.
- **`GMAIL_USER` / `GMAIL_APP_PASSWORD`** — the App Password is a 16-char token
  from https://myaccount.google.com/apppasswords (not your login password).
- **`GMAIL_LABELS`** — create one Gmail label per card and add a filter that
  routes that card's alert emails into it. List the labels comma-separated;
  each label name doubles as the card nickname.
- **`NOTION_TOKEN` / `NOTION_PARENT_PAGE_ID`** — create an integration at
  https://www.notion.so/my-integrations, share your target page with it, and
  copy the page ID from the page URL.

### 3. Create the databases

Run the one-shot setup scripts. Each prints an ID — paste it back into `.env`
where indicated:

```bash
python scripts/init_db.py                    # local SQLite schema
python scripts/init_notion_db.py             # -> NOTION_DATABASE_ID / NOTION_DATA_SOURCE_ID
python scripts/setup_monthly_reports_db.py   # -> NOTION_MONTHLY_REPORTS_* (optional, for reports)
```

## Usage

**Full daily run** (ingest new emails → sync to Notion → desktop notification):

```bash
python scripts/run_daily.py
```

**Individual steps:**

```bash
python scripts/ingest_newest.py --label "scotiabank visa"   # ingest one card
python scripts/sync_notion.py                               # push SQLite -> Notion
python scripts/show_transactions.py                         # print recent rows
python scripts/generate_monthly_report.py --month 2026-06   # monthly report
```

### Schedule it (macOS, optional)

A launchd template runs the daily job at 09:00:

```bash
sed "s|__INSTALL_DIR__|$PWD|g" launchd/finance-tracker.daily.plist.template \
  > ~/Library/LaunchAgents/finance-tracker.daily.plist
launchctl load ~/Library/LaunchAgents/finance-tracker.daily.plist
```

## Tests

```bash
uv run pytest
```

## Notes

- `data/finance.db`, `logs/`, `reports/`, and `.env` are gitignored — your
  actual financial data never gets committed.
- The email parsers are tuned to specific banks' alert formats. Adding a new
  card usually means extending `src/finance_tracker/parser.py`.

## License

MIT — see [LICENSE](LICENSE).
