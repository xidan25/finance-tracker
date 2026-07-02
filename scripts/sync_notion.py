"""CLI: sync SQLite transactions to/from Notion."""

import argparse
import sys

from dotenv import load_dotenv

from finance_tracker.notion_sync import sync


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Sync transactions to/from Notion.")
    parser.add_argument(
        "--push-only", action="store_true",
        help="Only push new SQLite rows to Notion (skip pulling user edits).",
    )
    parser.add_argument(
        "--pull-only", action="store_true",
        help="Only pull user edits from Notion (skip pushing new rows).",
    )
    args = parser.parse_args()

    if args.push_only and args.pull_only:
        print("ERROR: --push-only and --pull-only are mutually exclusive.",
              file=sys.stderr)
        return 1

    do_pull = not args.push_only
    do_push = not args.pull_only

    print(f"Syncing  pull={do_pull}  push={do_push}")
    print()

    summary = sync(do_pull=do_pull, do_push=do_push)

    if "pull" in summary:
        p = summary["pull"]
        print("=" * 60)
        print(f"Pull (Notion -> SQLite)")
        print("=" * 60)
        print(f"  Rows updated:    {p['updated']}")
        print(f"  Rule cache updates: {p['rule_updates']}")
        if p["error_log"]:
            print(f"  Errors:")
            for e in p["error_log"]:
                print(f"    - {e}")
        print()

    if "push" in summary:
        q = summary["push"]
        print("=" * 60)
        print(f"Push (SQLite -> Notion)")
        print("=" * 60)
        print(f"  Pages created:   {q['created']}")
        print(f"  Failed:          {q['failed']}")
        if q["error_log"]:
            print(f"  Errors:")
            for e in q["error_log"]:
                print(f"    - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
