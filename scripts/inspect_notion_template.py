"""Dump full schema of all child databases under a Notion page.

Usage:
    python scripts/inspect_notion_template.py <page_id>
    python scripts/inspect_notion_template.py 5ba14e38972e83b381ff010c9621e4cd
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()


def dump_property(name: str, prop: dict) -> str:
    """Pretty-print a Notion property definition."""
    ptype = prop["type"]
    line = f"    - {name:32s}  [{ptype}]"

    if ptype == "select":
        opts = prop["select"].get("options", [])
        names = [o["name"] for o in opts[:6]]
        line += f"  ({len(opts)} options): " + ", ".join(names)
        if len(opts) > 6:
            line += f", ... +{len(opts) - 6} more"

    elif ptype == "multi_select":
        opts = prop["multi_select"].get("options", [])
        names = [o["name"] for o in opts[:6]]
        line += f"  ({len(opts)} options): " + ", ".join(names)
        if len(opts) > 6:
            line += f", ... +{len(opts) - 6} more"

    elif ptype == "relation":
        rel = prop["relation"]
        target = rel.get("database_id", "?")
        line += f"\n        -> Relation to DB {target}"
        if rel.get("type") == "dual_property":
            dual = rel.get("dual_property", {})
            line += (f"\n        (dual: reverse property = "
                     f"'{dual.get('synced_property_name', '?')}' "
                     f"on target DB)")
        elif rel.get("type") == "single_property":
            line += "\n        (single-direction; no reverse property)"

    elif ptype == "rollup":
        ru = prop["rollup"]
        line += (f"\n        via relation '{ru.get('relation_property_name')}'"
                 f"  ->  property '{ru.get('rollup_property_name')}'"
                 f"  func={ru.get('function')}")

    elif ptype == "formula":
        expr = prop["formula"].get("expression") or ""
        # Truncate long formulas
        line += f"\n        formula: {expr[:200]}"
        if len(expr) > 200:
            line += " ... (truncated)"

    elif ptype == "number":
        fmt = prop["number"].get("format", "number")
        line += f"  format={fmt}"

    elif ptype == "status":
        opts = prop["status"].get("options", [])
        groups = prop["status"].get("groups", [])
        line += f"  ({len(opts)} options, {len(groups)} groups)"

    return line


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump Notion page DB schemas.")
    parser.add_argument("page_id", help="Notion page ID (with or without dashes).")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN missing in .env", file=sys.stderr)
        return 1

    notion = Client(auth=token)
    page_id = args.page_id

    # Walk page children, collect child_database blocks
    print(f"Walking children of page {page_id} ...\n")
    db_blocks: list[tuple[str, str]] = []
    cursor: str | None = None
    has_more = True
    try:
        while has_more:
            kwargs: dict = {"block_id": page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            result = notion.blocks.children.list(**kwargs)
            for blk in result["results"]:
                btype = blk["type"]
                if btype == "child_database":
                    title = blk["child_database"].get("title", "?")
                    db_blocks.append((blk["id"], title))
                    print(f"  found child_database: '{title}' (id={blk['id']})")
                elif btype == "child_page":
                    title = blk["child_page"].get("title", "?")
                    print(f"  found child_page: '{title}' (id={blk['id']}) - skipped")
            has_more = result.get("has_more", False)
            cursor = result.get("next_cursor")
    except Exception as e:
        print(f"\nERROR listing children: {type(e).__name__}: {e}", file=sys.stderr)
        print("\nMake sure the integration is authorized on this page:", file=sys.stderr)
        print("  Open the page -> ... -> Connections -> Add finance-tracker",
              file=sys.stderr)
        return 1

    if not db_blocks:
        print("\n(no child databases found under this page)")
        return 0

    # For each database, dump full schema
    for db_id, title in db_blocks:
        print()
        print("=" * 75)
        print(f"DATABASE: {title}")
        print(f"  block id: {db_id}")
        print("=" * 75)

        try:
            db = notion.databases.retrieve(db_id)
        except Exception as e:
            print(f"  ERROR retrieving DB: {e}")
            continue

        # Title from db object
        db_title = db.get("title", [])
        if db_title:
            print(f"  DB title: {db_title[0].get('plain_text', '?')}")

        # New data_sources model (Notion 2025+)
        data_sources = db.get("data_sources") or []
        if data_sources:
            for ds_meta in data_sources:
                ds_id = ds_meta["id"]
                ds_name = ds_meta.get("name", "?")
                print(f"\n  Data source: '{ds_name}' (id={ds_id})")
                try:
                    ds = notion.data_sources.retrieve(ds_id)
                except Exception as e:
                    print(f"    ERROR retrieving data_source: {e}")
                    continue
                props = ds.get("properties", {})
                print(f"  Properties ({len(props)}):")
                for name in sorted(props.keys()):
                    print(dump_property(name, props[name]))
        else:
            # Old model — properties directly on db
            props = db.get("properties", {})
            print(f"  Properties ({len(props)}):")
            for name in sorted(props.keys()):
                print(dump_property(name, props[name]))

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
