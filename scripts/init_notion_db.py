"""One-shot: create the Notion database under NOTION_PARENT_PAGE_ID.

Reads NOTION_TOKEN and NOTION_PARENT_PAGE_ID from .env.
Prints the new database ID — copy it back into .env as NOTION_DATABASE_ID.

Properties created (column order in Notion):
  Merchant (Title) -> Tags (Multi-select) -> Date -> Card -> Amount (plain number)
  -> Category -> Status -> Currency -> Foreign -> Merchant (raw) -> Source -> DB ID
"""

import os
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

CATEGORIES = [
    ("Food & Drink", "orange"),
    ("Groceries", "green"),
    ("Transport", "blue"),
    ("Shopping", "pink"),
    ("Housing & Utilities", "brown"),
    ("Entertainment", "purple"),
    ("Health", "red"),
    ("Bills & Subscriptions", "yellow"),
    ("Income & Refund", "green"),
    ("Other", "gray"),
]

TAGS = [
    ("coffee", "orange"), ("restaurant", "orange"), ("takeout", "orange"),
    ("bar", "purple"), ("dessert", "pink"),
    ("supermarket", "green"),
    ("taxi", "blue"), ("public-transit", "blue"),
    ("flight", "blue"), ("bike-share", "blue"),
    ("clothing", "pink"), ("electronics", "default"), ("home", "brown"),
    ("skincare", "pink"), ("gift", "red"),
    ("rent", "brown"), ("electricity", "yellow"), ("water", "blue"),
    ("internet", "purple"), ("gas-bill", "yellow"),
    ("movies", "purple"), ("games", "purple"), ("activities", "purple"),
    ("pharmacy", "red"), ("doctor", "red"), ("dental", "red"),
    ("fitness", "red"), ("supplements", "red"), ("gym", "red"),
    ("phone", "yellow"), ("software", "yellow"),
    ("salary", "green"), ("freelance", "green"), ("refund", "green"),
    ("cashback", "green"), ("interest", "green"),
]

STATUSES = [
    ("pending", "yellow"),
    ("posted", "green"),
    ("reversed", "red"),
]

CARDS = [
    ("scotiabank visa", "blue"),
    ("dbs visa", "green"),
]

SOURCES = [
    ("cache", "gray"),
    ("llm", "blue"),
    ("manual", "purple"),
    ("preset", "default"),
    ("llm-confirmed", "blue"),
]


def _select_options(items):
    return [{"name": name, "color": color} for name, color in items]


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    parent_id = os.environ.get("NOTION_PARENT_PAGE_ID")

    if not token:
        print("ERROR: NOTION_TOKEN missing in .env", file=sys.stderr)
        return 1
    if not parent_id:
        print("ERROR: NOTION_PARENT_PAGE_ID missing in .env", file=sys.stderr)
        return 1

    notion = Client(auth=token)

    properties = {
        "Merchant":         {"title": {}},
        "Tags":             {"multi_select": {"options": _select_options(TAGS)}},
        "Date":             {"date": {}},
        "Card":             {"select": {"options": _select_options(CARDS)}},
        # All amounts are normalized to SGD base (see fx.py); the Currency column
        # records each row's original currency + amount. This table is spend-only;
        # income/refund rows are not pushed here (viewed in the monthly report).
        "Amount":           {"number": {"format": "singapore_dollar"}},
        "Category":         {"select": {"options": _select_options(CATEGORIES)}},
        "Status":           {"select": {"options": _select_options(STATUSES)}},
        "Currency":         {"rich_text": {}},
        "Foreign":          {"checkbox": {}},
        "Merchant (raw)":   {"rich_text": {}},
        "Source":           {"select": {"options": _select_options(SOURCES)}},
        "DB ID":            {"rich_text": {}},
    }

    print(f"Creating database under parent page {parent_id} ...")
    try:
        resp = notion.databases.create(
            parent={"type": "page_id", "page_id": parent_id},
            title=[{"type": "text", "text": {"content": "Transactions"}}],
            properties=properties,
        )
    except Exception as e:
        print(f"ERROR creating database: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    db_id = resp["id"]
    db_url = resp["url"]
    print()
    print("Database created!")
    print(f"  Database ID:  {db_id}")
    print(f"  URL:          {db_url}")
    print()
    print("Next step: add this to .env:")
    print(f"  NOTION_DATABASE_ID={db_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
