"""One-time: create the Monthly Reports database in Notion.

Creates a new database under NOTION_PARENT_PAGE_ID with the schema needed
by notion_writer. Prints the env vars to add to .env after success.

Idempotent: re-running prints the existing IDs if the DB already exists by
title under the parent page.
"""

import os
import sys

from dotenv import load_dotenv
from notion_client import Client


DB_TITLE = "Monthly Reports"


def find_existing_db(notion: Client, parent_page_id: str) -> tuple[str, str] | None:
    """Return (db_id, data_source_id) if a DB with DB_TITLE already exists."""
    cursor: str | None = None
    has_more = True
    while has_more:
        kwargs: dict = {"block_id": parent_page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.blocks.children.list(**kwargs)
        for blk in result["results"]:
            if blk["type"] != "child_database":
                continue
            title = blk["child_database"].get("title", "")
            if title == DB_TITLE:
                db = notion.databases.retrieve(blk["id"])
                ds = (db.get("data_sources") or [{}])[0]
                return blk["id"], ds.get("id", "")
        has_more = result.get("has_more", False)
        cursor = result.get("next_cursor")
    return None


def main() -> int:
    load_dotenv()

    token = os.environ.get("NOTION_TOKEN")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not token or not parent:
        print("ERROR: NOTION_TOKEN / NOTION_PARENT_PAGE_ID missing.", file=sys.stderr)
        return 1

    notion = Client(auth=token)

    existing = find_existing_db(notion, parent)
    if existing:
        db_id, ds_id = existing
        print(f"Found existing '{DB_TITLE}' DB:")
        print(f"  database_id    = {db_id}")
        print(f"  data_source_id = {ds_id}")
        print()
        print("If not yet in .env, add:")
        print(f"  NOTION_MONTHLY_REPORTS_DB_ID={db_id}")
        print(f"  NOTION_MONTHLY_REPORTS_DATA_SOURCE_ID={ds_id}")
        return 0

    print(f"Creating '{DB_TITLE}' under parent page {parent} ...")
    # Under Notion's v2 API, databases.create silently drops the `properties`
    # payload — they have to be PATCHed onto the data_source after creation.
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": parent},
        title=[{"type": "text", "text": {"content": DB_TITLE}}],
    )
    db_id = db["id"]
    ds_id = (db.get("data_sources") or [{}])[0].get("id", "")

    print(f"  database_id    = {db_id}")
    print(f"  data_source_id = {ds_id}")
    print("Patching schema onto data_source ...")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={
            "Name":              {"name": "Name"},
            "Month":             {"date": {}},
            # Plain number: reports are single-currency (SGD or CAD), labelled
            # in the page Name and body — the column carries no fixed symbol.
            "Total Spent":       {"number": {"format": "number"}},
            "Total Income":      {"number": {"format": "number"}},
            "Transaction Count": {"number": {"format": "number"}},
            "Hero Line":         {"rich_text": {}},
            "Generated At":      {"date": {}},
        },
    )
    print("✓ Schema applied.")
    print()
    print("Add to your .env:")
    print(f"  NOTION_MONTHLY_REPORTS_DB_ID={db_id}")
    print(f"  NOTION_MONTHLY_REPORTS_DATA_SOURCE_ID={ds_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
