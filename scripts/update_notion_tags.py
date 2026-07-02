"""Apply V3 taxonomy changes to the Notion Transactions Tags property.

Adds new options:
  - drinks      (replaces coffee; coffee stays as legacy)
  - bakery      (BreadTalk-style bakeries)
  - convenience (Cheers/7-Eleven/Lawson)

Note: Notion's API does NOT support renaming multi_select options via
data_sources.update or databases.update (the rename payload is silently
ignored). So `coffee` is left in place as a legacy option; the user manually
re-tags any rows currently tagged coffee -> drinks in the Notion UI.

All existing options are preserved untouched.

Usage:
    python scripts/update_notion_tags.py            # dry-run, prints diff
    python scripts/update_notion_tags.py --apply    # actually patch Notion
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from notion_client import Client


ADD = [
    {"name": "drinks", "color": "orange"},
    {"name": "bakery", "color": "orange"},
    {"name": "convenience", "color": "orange"},
]


def build_new_options(existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (new_options_payload, human_diff_lines)."""
    diff: list[str] = []
    existing_names = {o["name"] for o in existing}

    new_options: list[dict] = [
        {"id": o["id"], "name": o["name"]} for o in existing
    ]
    for add in ADD:
        if add["name"] in existing_names:
            diff.append(f"  SKIP    add {add['name']!r}  (already exists)")
            continue
        new_options.append(add)
        diff.append(f"  ADD     {add['name']!r:14s} color={add['color']}")

    return new_options, diff


def main() -> int:
    load_dotenv()

    p = argparse.ArgumentParser(description="Migrate Notion Tags to V3 taxonomy.")
    p.add_argument("--apply", action="store_true",
                   help="Actually patch Notion. Default is dry-run.")
    args = p.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not token or not ds_id:
        print("ERROR: NOTION_TOKEN / NOTION_DATA_SOURCE_ID missing in .env",
              file=sys.stderr)
        return 1

    notion = Client(auth=token)

    ds = notion.data_sources.retrieve(ds_id)
    existing = ds["properties"].get("Tags", {}).get("multi_select", {}).get("options", [])
    print(f"Current Tags options: {len(existing)}")

    new_options, diff = build_new_options(existing)

    print()
    print("Planned changes:")
    if not diff:
        print("  (no changes)")
    else:
        for line in diff:
            print(line)
    print()
    print(f"Resulting options: {len(new_options)}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to actually patch Notion.")
        return 0

    print()
    print("Applying ...")
    notion.data_sources.update(
        data_source_id=ds_id,
        properties={"Tags": {"multi_select": {"options": new_options}}},
    )

    # Verify
    ds_after = notion.data_sources.retrieve(ds_id)
    after = ds_after["properties"].get("Tags", {}).get("multi_select", {}).get("options", [])
    after_names = sorted(o["name"] for o in after)
    print(f"After update: {len(after)} options")
    print()
    print("New names check:")
    for needed in ("drinks", "bakery", "convenience"):
        present = needed in after_names
        print(f"  {'✓' if present else '✗'} {needed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
