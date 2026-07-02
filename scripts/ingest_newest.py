"""Ingest the N most recent emails by INTERNALDATE (true date-based newest).

Bypasses last_uid tracking — emails selected purely by date, regardless of UID.
Sets last_uid to the max UID in the selected set after ingestion, so subsequent
incremental `ingest_emails.py` runs only fetch newly-arrived emails.
"""

import argparse
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv

from finance_tracker.db import get_connection
from finance_tracker.email_client import (
    connect as imap_connect,
    fetch_raw,
    parse_email,
    select_label,
    set_last_uid,
)
from finance_tracker.ingest import (
    finalize_sync_run,
    ingest_one,
    start_sync_run,
)


def list_uids_by_date(imap, label: str) -> list[tuple[int, datetime]]:
    """Return [(uid, datetime)] for every email in label, sorted desc by date."""
    select_label(imap, label)
    status, data = imap.uid("SEARCH", None, "ALL")
    if status != "OK":
        raise RuntimeError(f"UID SEARCH failed: {data}")
    all_uids = data[0].decode().split() if data and data[0] else []
    if not all_uids:
        return []

    uid_range = ",".join(all_uids)
    status, data = imap.uid("FETCH", uid_range, "(INTERNALDATE)")
    if status != "OK":
        raise RuntimeError(f"UID FETCH INTERNALDATE failed: {data}")

    out: list[tuple[int, datetime]] = []
    for item in data:
        if isinstance(item, tuple):
            text = item[0].decode("utf-8", errors="replace") if item else ""
        elif isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
        else:
            continue
        m = re.search(r"UID\s+(\d+)\s+INTERNALDATE\s+\"([^\"]+)\"", text)
        if not m:
            continue
        uid = int(m.group(1))
        try:
            d = datetime.strptime(m.group(2), "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            continue
        out.append((uid, d))

    out.sort(key=lambda x: x[1], reverse=True)
    return out


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Ingest the N newest emails by date (INTERNALDATE).",
    )
    parser.add_argument("--count", type=int, default=30,
                        help="Number of newest emails to ingest (by date).")
    parser.add_argument("--label", default=os.environ.get("GMAIL_LABEL"))
    parser.add_argument("--card", default=None,
                        help="Card nickname (default: same as label).")
    parser.add_argument("--trigger", default="manual",
                        choices=["manual", "scheduled", "shortcut"])
    args = parser.parse_args()

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password or not args.label:
        print("ERROR: GMAIL_USER / GMAIL_APP_PASSWORD / GMAIL_LABEL must be set.",
              file=sys.stderr)
        return 1
    card = args.card or args.label

    # ---- Phase 1: list & select target UIDs ----
    print(f"Connecting to Gmail as {user}")
    imap = imap_connect(user, password)
    try:
        print(f"Indexing all emails in label '{args.label}' by date ...")
        all_dated_uids = list_uids_by_date(imap, args.label)
        print(f"  Total emails in label: {len(all_dated_uids)}")
        if not all_dated_uids:
            print("  Nothing to ingest.")
            return 0

        target_dated = all_dated_uids[:args.count]
        target_uids = [u for u, _ in target_dated]
        oldest_d = target_dated[-1][1].strftime("%Y-%m-%d")
        newest_d = target_dated[0][1].strftime("%Y-%m-%d")
        max_target_uid = max(target_uids)

        print(f"  Selected newest {len(target_dated)} by date:")
        print(f"    Date range: {oldest_d} to {newest_d}")
        print(f"    UID range:  {min(target_uids)} to {max_target_uid}")
        print()

        # ---- Phase 2: fetch raw bodies for selected UIDs ----
        print("Fetching raw email bodies ...")
        emails = []
        for uid in target_uids:
            try:
                raw, internaldate = fetch_raw(imap, uid)
                emails.append(parse_email(raw, uid, internaldate, args.label))
            except Exception as e:
                print(f"  ERROR fetching UID {uid}: {e}", file=sys.stderr)
        print(f"  Successfully fetched {len(emails)} emails")
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass

    # ---- Phase 3: ingest each email through the existing pipeline ----
    conn = get_connection()
    run_id = start_sync_run(conn, args.trigger)
    counts = {
        "emails_processed": 0,
        "transactions_created": 0,
        "duplicates": 0,
        "parse_failures": 0,
        "cache_hits": 0,
        "llm_calls": 0,
        "errors": 0,
    }
    error_log: list[str] = []
    try:
        for em in emails:
            counts["emails_processed"] += 1
            try:
                txn_id, source = ingest_one(conn, em, card)
                if txn_id is None:
                    if source == "duplicate":
                        counts["duplicates"] += 1
                    elif source == "parse_failed":
                        counts["parse_failures"] += 1
                        error_log.append(
                            f"UID {em.uid}: regex parser failed; subject={em.subject!r}"
                        )
                else:
                    counts["transactions_created"] += 1
                    if source == "cache":
                        counts["cache_hits"] += 1
                    elif source == "llm":
                        counts["llm_calls"] += 1
            except Exception as e:
                counts["errors"] += 1
                error_log.append(f"UID {em.uid}: {type(e).__name__}: {e}")

        # Anchor last_uid so future incremental runs only fetch genuinely new emails
        if max_target_uid > 0:
            set_last_uid(conn, args.label, max_target_uid)

        status = "success" if counts["errors"] == 0 else "partial"
        finalize_sync_run(
            conn, run_id,
            emails_processed=counts["emails_processed"],
            transactions_created=counts["transactions_created"],
            errors=counts["errors"],
            error_log=error_log,
            status=status,
        )
    finally:
        conn.close()

    print()
    print("=" * 60)
    print(f"Sync run #{run_id} done")
    print("=" * 60)
    print(f"Emails processed:      {counts['emails_processed']}")
    print(f"Transactions created:  {counts['transactions_created']}")
    print(f"  via cache:           {counts['cache_hits']}")
    print(f"  via LLM:             {counts['llm_calls']}")
    print(f"Duplicates skipped:    {counts['duplicates']}")
    print(f"Parse failures:        {counts['parse_failures']}")
    print(f"Errors:                {counts['errors']}")
    print(f"last_uid set to:       {max_target_uid}")
    if error_log:
        print("Error log:")
        for line in error_log:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
