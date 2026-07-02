"""Patch the existing Notion data_source with the 12 properties.

The old init script's databases.create(properties=...) was silently dropped
under Notion's new data-sources API. This script updates the data_source
directly via data_sources.update().

Run once. After it succeeds, add NOTION_DATA_SOURCE_ID to .env.
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
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not token:
        print("ERROR: NOTION_TOKEN missing in .env", file=sys.stderr)
        return 1
    if not db_id:
        print("ERROR: NOTION_DATABASE_ID missing in .env", file=sys.stderr)
        return 1

    notion = Client(auth=token)

    # Discover the data_source under the database
    db = notion.databases.retrieve(db_id)
    data_sources = db.get("data_sources") or []
    if not data_sources:
        print("ERROR: database has no data_sources?", file=sys.stderr)
        return 1
    ds_id = data_sources[0]["id"]
    print(f"Database:    {db_id}")
    print(f"Data source: {ds_id}")
    print()

    # Build the properties payload:
    # - "Name" (existing default Title) -> rename to "Merchant"
    # - Add the other 11 properties
    properties_update = {
        "Name":             {"name": "Merchant"},
        "Tags":             {"multi_select": {"options": _select_options(TAGS)}},
        "Date":             {"date": {}},
        "Card":             {"select": {"options": _select_options(CARDS)}},
        "Amount":           {"number": {"format": "canadian_dollar"}},
        "Category":         {"select": {"options": _select_options(CATEGORIES)}},
        "Status":           {"select": {"options": _select_options(STATUSES)}},
        "Currency":         {"rich_text": {}},
        "Foreign":          {"checkbox": {}},
        "Merchant (raw)":   {"rich_text": {}},
        "Source":           {"select": {"options": _select_options(SOURCES)}},
        "DB ID":            {"rich_text": {}},
    }

    print("Updating data source with 12 properties...")
    try:
        notion.data_sources.update(
            data_source_id=ds_id,
            properties=properties_update,
        )
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Verify
    ds = notion.data_sources.retrieve(ds_id)
    props = ds.get("properties", {})
    print(f"\nAfter update: {len(props)} properties")
    for name, p in props.items():
        t = p["type"]
        extra = ""
        if t in ("select", "multi_select"):
            opts = p[t].get("options", [])
            extra = f"  ({len(opts)} options)"
        print(f"  - {name:20s}  [{t}]{extra}")

    print()
    print("=" * 60)
    print("Add this to your .env file:")
    print(f"  NOTION_DATA_SOURCE_ID={ds_id}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
