"""DESTRUCTIVE: wipe SQLite data + archive all Notion pages.

Optionally prime last_uid to fetch the newest N emails next time.

After running with --yes --prime 30:
  SQLite:
    transactions   -> empty
    merchant_rules -> empty
    sync_runs      -> empty
    meta.last_uid_<label>  -> set to (max_uid_in_label - 30)
  Notion:
    All pages archived (moved to Notion trash)
    Database structure preserved (12 columns + Notes = 13)
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from notion_client import Client

from finance_tracker.db import get_connection, transaction
from finance_tracker.email_client import (
    connect as imap_connect,
    select_label,
    set_last_uid,
)


def wipe_sqlite() -> dict[str, int]:
    """Truncate transactions, merchant_rules, sync_runs; remove last_uid_* meta."""
    conn = get_connection()
    counts = {}
    try:
        counts["transactions"] = conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0]
        counts["merchant_rules"] = conn.execute(
            "SELECT COUNT(*) FROM merchant_rules"
        ).fetchone()[0]
        counts["sync_runs"] = conn.execute(
            "SELECT COUNT(*) FROM sync_runs"
        ).fetchone()[0]
        counts["last_uid_meta_keys"] = conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'last_uid_%'"
        ).fetchone()[0]

        with transaction(conn):
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM merchant_rules")
            conn.execute("DELETE FROM sync_runs")
            conn.execute("DELETE FROM meta WHERE key LIKE 'last_uid_%'")
            # Reset autoincrement so new IDs start at 1
            conn.execute(
                "DELETE FROM sqlite_sequence "
                "WHERE name IN ('transactions', 'merchant_rules', 'sync_runs')"
            )
    finally:
        conn.close()
    return counts


def archive_notion_pages(notion: Client, ds_id: str) -> int:
    """Archive every page in the data source. Returns count archived."""
    archived = 0
    cursor: str | None = None
    has_more = True
    while has_more:
        kwargs: dict = {"data_source_id": ds_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result["results"]:
            try:
                notion.pages.update(page_id=page["id"], archived=True)
                archived += 1
            except Exception as e:
                print(
                    f"  ERROR archiving page {page.get('id', '?')}: {e}",
                    file=sys.stderr,
                )
        has_more = result.get("has_more", False)
        cursor = result.get("next_cursor")
    return archived


def find_max_uid(user: str, password: str, label: str) -> int:
    """Connect to Gmail, return the max UID in the given label (0 if empty)."""
    imap = imap_connect(user, password)
    try:
        select_label(imap, label)
        status, data = imap.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"UID SEARCH failed: {data}")
        uids_raw = data[0].decode().split() if data and data[0] else []
        return max((int(u) for u in uids_raw), default=0)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Wipe SQLite + Notion data (DESTRUCTIVE)."
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm destructive action — required to actually run.",
    )
    parser.add_argument(
        "--prime", type=int, default=None,
        help="After wipe, set last_uid = (max_uid - N) so next ingest fetches N newest.",
    )
    args = parser.parse_args()

    if not args.yes:
        print("This will (when --yes is given):")
        print("  1. DELETE all rows in transactions, merchant_rules, sync_runs")
        print("  2. REMOVE last_uid_* entries from meta")
        print("  3. ARCHIVE all pages in the Notion DB (moves them to trash)")
        print("  4. PRESERVE the Notion DB structure (columns intact)")
        print()
        print("With --prime N: also set last_uid = (max_in_label - N), so the next")
        print("`ingest_emails.py --max N` fetches the newest N emails in the label.")
        print()
        print("Run again with --yes to proceed. This cannot be undone.")
        return 1

    # === Step 1: wipe SQLite ===
    print("Wiping SQLite ...")
    counts = wipe_sqlite()
    print(f"  Deleted: {counts['transactions']} transactions, "
          f"{counts['merchant_rules']} rules, "
          f"{counts['sync_runs']} sync_runs, "
          f"{counts['last_uid_meta_keys']} meta last_uid keys")

    # === Step 2: archive Notion pages ===
    token = os.environ.get("NOTION_TOKEN")
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID")
    if not token or not ds_id:
        print("WARNING: NOTION_TOKEN/NOTION_DATA_SOURCE_ID not set; "
              "skipping Notion wipe.", file=sys.stderr)
    else:
        print()
        print("Archiving Notion pages ...")
        notion = Client(auth=token)
        archived = archive_notion_pages(notion, ds_id)
        print(f"  Archived: {archived} pages")

    # === Step 3: prime last_uid ===
    if args.prime is not None:
        label = os.environ.get("GMAIL_LABEL")
        user = os.environ.get("GMAIL_USER")
        password = os.environ.get("GMAIL_APP_PASSWORD")
        if not (label and user and password):
            print("WARNING: GMAIL_* env vars not set; skipping prime.",
                  file=sys.stderr)
        else:
            print()
            print(f"Priming last_uid for newest {args.prime} fetch ...")
            max_uid = find_max_uid(user, password, label)
            target = max(0, max_uid - args.prime)
            print(f"  Max UID in label: {max_uid}")
            print(f"  Setting last_uid to: {target}")
            print(f"  Next `ingest_emails.py --max {args.prime}` will fetch "
                  f"UIDs {target + 1} to {max_uid}")
            db = get_connection()
            try:
                set_last_uid(db, label, target)
            finally:
                db.close()

    print()
    print("Done. Recommended next steps:")
    if args.prime:
        print(f"  python scripts/ingest_emails.py --max {args.prime}")
        print(f"  python scripts/sync_notion.py --push-only")
    else:
        print("  (Set --prime N to prime; otherwise ingest will start from UID 1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
