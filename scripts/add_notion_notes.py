"""Patch: add a Notes column to the existing Notion data_source.

Run after schema_v2 migration so SQLite-side notes column exists
in tandem with the Notion-side Notes column.
"""

import os
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not token:
        print("ERROR: NOTION_TOKEN missing in .env", file=sys.stderr)
        return 1
    if not ds_id:
        print("ERROR: NOTION_DATA_SOURCE_ID missing in .env", file=sys.stderr)
        return 1

    notion = Client(auth=token)

    # Check if Notes already exists (idempotent)
    ds = notion.data_sources.retrieve(ds_id)
    props = ds.get("properties", {})
    if "Notes" in props:
        print("Notes column already exists — nothing to do.")
        return 0

    print("Adding Notes column to data_source...")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={"Notes": {"rich_text": {}}},
    )

    # Verify
    ds = notion.data_sources.retrieve(ds_id)
    props = ds.get("properties", {})
    print(f"\nProperties now: {len(props)} total")
    if "Notes" in props:
        print("✓ Notes column successfully added")
        return 0
    else:
        print("ERROR: Notes column not present after update")
        return 1


if __name__ == "__main__":
    sys.exit(main())
