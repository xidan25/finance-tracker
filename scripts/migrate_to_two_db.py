"""One-shot migration to the two-database architecture (Categories + Transactions).

Run AFTER `wipe_all.py --yes` so the existing Notion pages are archived.
Idempotency: NOT idempotent on Categories DB creation. Run once.

Steps performed:
  1. Drop columns from Transactions DB:  Status, Foreign, Category (Select)
  2. Add 'Add to Month' formula to Transactions DB
  3. Create new 'Categories' DB under NOTION_PARENT_PAGE_ID
  4. Add dual Relation Categories.Expenses <-> Transactions.Category
  5. Add rollups on Categories: Total Spent, Spent This Month
  6. Pre-populate 10 Category pages with emoji icons
  7. Print env vars to add to .env
"""

import os
import sys
import time

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()


CATEGORIES = [
    ("Food & Drink",          "🍴"),
    ("Groceries",             "🛒"),
    ("Transport",             "🚗"),
    ("Shopping",              "🛍"),
    ("Housing & Utilities",   "🏠"),
    ("Entertainment",         "🎬"),
    ("Health",                "❤️"),
    ("Bills & Subscriptions", "📱"),
    ("Income & Refund",       "💰"),
    ("Other",                 "📦"),
]


# Returns Amount if transaction Date is in the current calendar month, else 0.
ADD_TO_MONTH_FORMULA = (
    'if(formatDate(now(), "YYYY-MM") == formatDate(prop("Date"), "YYYY-MM"), '
    'prop("Amount"), 0)'
)


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    parent_id = os.environ.get("NOTION_PARENT_PAGE_ID")
    txn_db_id = os.environ.get("NOTION_DATABASE_ID")
    txn_ds_id = os.environ.get("NOTION_DATA_SOURCE_ID")

    for var, val in [
        ("NOTION_TOKEN", token),
        ("NOTION_PARENT_PAGE_ID", parent_id),
        ("NOTION_DATABASE_ID", txn_db_id),
        ("NOTION_DATA_SOURCE_ID", txn_ds_id),
    ]:
        if not val:
            print(f"ERROR: {var} missing in .env", file=sys.stderr)
            return 1

    notion = Client(auth=token)

    # Optional: resume mode. If categories DB was already created (e.g. previous
    # run partially succeeded), set these env vars to skip step 3.
    existing_cat_db_id = os.environ.get("NOTION_CATEGORIES_DB_ID")
    existing_cat_ds_id = os.environ.get("NOTION_CATEGORIES_DATA_SOURCE_ID")

    # ----- Step 1: Drop Status, Foreign, old Category Select on Transactions -----
    print("Step 1: Dropping Status, Foreign, Category (Select) from Transactions DB...")
    try:
        notion.data_sources.update(
            data_source_id=txn_ds_id,
            properties={
                "Status": None,
                "Foreign": None,
                "Category": None,
            },
        )
        print("  ✓ Dropped 3 properties")
    except Exception as e:
        print(f"  WARN dropping properties: {type(e).__name__}: {e}")
        print("  (continuing — Notion may have already removed some)")

    # ----- Step 2: Add 'Add to Month' formula to Transactions -----
    print("\nStep 2: Adding 'Add to Month' formula to Transactions DB...")
    notion.data_sources.update(
        data_source_id=txn_ds_id,
        properties={
            "Add to Month": {"formula": {"expression": ADD_TO_MONTH_FORMULA}},
        },
    )
    print("  ✓ Added formula")

    # ----- Step 3: Create or reuse Categories DB -----
    if existing_cat_db_id and existing_cat_ds_id:
        cat_db_id = existing_cat_db_id
        cat_ds_id = existing_cat_ds_id
        print(f"\nStep 3: Reusing existing Categories DB (from env)")
        print(f"  Categories DB: id={cat_db_id}")
        print(f"      data_source: {cat_ds_id}")
    else:
        print("\nStep 3: Creating Categories DB under parent page...")
        cat_db = notion.databases.create(
            parent={"type": "page_id", "page_id": parent_id},
            title=[{"type": "text", "text": {"content": "Categories"}}],
        )
        cat_db_id = cat_db["id"]
        cat_ds_id = cat_db["data_sources"][0]["id"]
        print(f"  ✓ Categories DB: id={cat_db_id}")
        print(f"      data_source: {cat_ds_id}")

    # ----- Step 4: Add Expenses Relation on Categories (auto-creates reverse) -----
    # NEW Notion API uses data_source_id (not database_id) for relations.
    print("\nStep 4: Adding 'Expenses' dual Relation on Categories DB...")
    # If Expenses relation already exists (from a previous failed run), skip.
    cat_ds = notion.data_sources.retrieve(cat_ds_id)
    if "Expenses" in cat_ds.get("properties", {}):
        print("  Expenses relation already present — skipping creation")
    else:
        try:
            notion.data_sources.update(
                data_source_id=cat_ds_id,
                properties={
                    "Expenses": {
                        "relation": {
                            "data_source_id": txn_ds_id,
                            "type": "dual_property",
                            "dual_property": {"synced_property_name": "Category"},
                        }
                    }
                },
            )
            print("  ✓ Dual relation created (reverse synced as 'Category' on Transactions)")
        except Exception as e:
            print(f"  Retrying without synced_property_name: {e}")
            notion.data_sources.update(
                data_source_id=cat_ds_id,
                properties={
                    "Expenses": {
                        "relation": {
                            "data_source_id": txn_ds_id,
                            "type": "dual_property",
                            "dual_property": {},
                        }
                    }
                },
            )
            # Wait briefly for Notion to propagate, then find auto-named reverse
            time.sleep(1)
            txn_ds = notion.data_sources.retrieve(txn_ds_id)
            for prop_name, prop in txn_ds["properties"].items():
                rel = prop.get("relation", {}) or {}
                # New API may return data_source_id; fallback to database_id for safety
                target_id = rel.get("data_source_id") or rel.get("database_id")
                if (
                    prop["type"] == "relation"
                    and target_id in (cat_ds_id, cat_db_id)
                    and prop_name != "Category"
                ):
                    print(f"  Renaming auto-generated '{prop_name}' to 'Category'...")
                    notion.data_sources.update(
                        data_source_id=txn_ds_id,
                        properties={prop_name: {"name": "Category"}},
                    )
                    print("  ✓ Reverse property renamed to 'Category'")
                    break

    # ----- Step 5: Add rollups on Categories DB -----
    print("\nStep 5: Adding rollups (Total Spent, Spent This Month) on Categories...")
    notion.data_sources.update(
        data_source_id=cat_ds_id,
        properties={
            "Total Spent": {
                "rollup": {
                    "relation_property_name": "Expenses",
                    "rollup_property_name": "Amount",
                    "function": "sum",
                }
            },
            "Spent This Month": {
                "rollup": {
                    "relation_property_name": "Expenses",
                    "rollup_property_name": "Add to Month",
                    "function": "sum",
                }
            },
        },
    )
    print("  ✓ Rollups added")

    # ----- Step 6: Pre-populate 10 Category pages (idempotent) -----
    print("\nStep 6: Pre-populating 10 Category pages...")
    # Get existing category names so we don't duplicate
    existing_names: set[str] = set()
    cursor: str | None = None
    has_more = True
    while has_more:
        kwargs: dict = {"data_source_id": cat_ds_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result["results"]:
            name_prop = page["properties"].get("Name", {})
            items = name_prop.get("title", []) or []
            name_text = "".join(i.get("plain_text", "") for i in items)
            if name_text:
                existing_names.add(name_text)
        has_more = result.get("has_more", False)
        cursor = result.get("next_cursor")

    created = 0
    skipped = 0
    for name, emoji in CATEGORIES:
        if name in existing_names:
            print(f"  - {emoji}  {name}  (already exists, skipping)")
            skipped += 1
            continue
        try:
            notion.pages.create(
                parent={"data_source_id": cat_ds_id},
                icon={"type": "emoji", "emoji": emoji},
                properties={
                    "Name": {"title": [{"text": {"content": name}}]},
                },
            )
            print(f"  ✓ {emoji}  {name}")
            created += 1
        except Exception as e:
            print(f"  ERROR creating '{name}': {e}", file=sys.stderr)
    print(f"  Created {created}, skipped {skipped} (total target 10)")

    # ----- Done -----
    print()
    print("=" * 70)
    print("Migration complete!")
    print("=" * 70)
    print()
    print("Add these two lines to your .env file:")
    print(f"  NOTION_CATEGORIES_DB_ID={cat_db_id}")
    print(f"  NOTION_CATEGORIES_DATA_SOURCE_ID={cat_ds_id}")
    print()
    print("Then run:")
    print("  python scripts/ingest_newest.py --count 30")
    print("  python scripts/sync_notion.py --push-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
