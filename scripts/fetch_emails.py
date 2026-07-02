"""CLI to fetch new Gmail emails by label and print summaries.

Usage examples:
    python scripts/fetch_emails.py --max 1 --dry-run    # peek at the first new email
    python scripts/fetch_emails.py --max 5              # process up to 5 new emails
    python scripts/fetch_emails.py --since-uid 0        # re-fetch from beginning
"""

import argparse
import os
import sys
from textwrap import shorten

from dotenv import load_dotenv

from finance_tracker.db import get_connection
from finance_tracker.email_client import (
    fetch_new_emails,
    get_last_uid,
    set_last_uid,
)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Fetch new Gmail emails by label."
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("GMAIL_LABEL"),
        help="Gmail label to fetch (default: GMAIL_LABEL env var).",
    )
    parser.add_argument(
        "--max", type=int, default=5, dest="max_emails",
        help="Max emails to fetch (default 5; 0 = unlimited).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do not update last_uid in meta table.",
    )
    parser.add_argument(
        "--since-uid", type=int, default=None,
        help="Override starting UID (default: read last_uid from meta).",
    )
    args = parser.parse_args()

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print(
            "ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env",
            file=sys.stderr,
        )
        return 1
    if not args.label:
        print(
            "ERROR: --label or GMAIL_LABEL env var must be provided",
            file=sys.stderr,
        )
        return 1

    print(f"Connecting to Gmail as {user}")
    print(f"Label:           {args.label}")

    db = get_connection()
    try:
        if args.since_uid is None:
            last_uid = get_last_uid(db, args.label)
        else:
            last_uid = args.since_uid
        print(f"Starting from UID > {last_uid}")
        if args.max_emails > 0:
            print(f"Max to fetch:    {args.max_emails}")
        else:
            print("Max to fetch:    unlimited")
        print()

        cap = args.max_emails if args.max_emails > 0 else None
        count = 0
        max_uid_seen = last_uid

        for em in fetch_new_emails(
            user=user,
            password=password,
            label=args.label,
            since_uid=last_uid,
            max_emails=cap,
        ):
            count += 1
            max_uid_seen = max(max_uid_seen, em.uid)
            print(f"=== Email {count}  (UID {em.uid}) ===")
            print(f"  Subject:  {em.subject}")
            print(f"  From:     {em.sender}")
            print(f"  Received: {em.received_at.isoformat()}")
            print(f"  Msg-ID:   {em.message_id}")
            print(f"  Has HTML: {'yes' if em.body_html else 'no'}")
            preview = shorten(
                em.body_text.replace("\n", " | "),
                width=300,
                placeholder=" ...",
            )
            print(f"  Body preview: {preview}")
            print()

        print(f"Fetched {count} new email(s).")

        if count > 0 and not args.dry_run:
            set_last_uid(db, args.label, max_uid_seen)
            print(f"Updated last_uid to {max_uid_seen}.")
        elif args.dry_run:
            print("(dry-run: last_uid NOT updated)")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
