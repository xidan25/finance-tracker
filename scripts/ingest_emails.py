"""CLI: fetch new Gmail emails and ingest them into the transactions table."""

import argparse
import os
import sys

from dotenv import load_dotenv

from finance_tracker.ingest import ingest_label


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Ingest new credit card emails into the transactions table."
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("GMAIL_LABEL"),
        help="Gmail label to fetch (default: GMAIL_LABEL env var).",
    )
    parser.add_argument(
        "--max", type=int, default=5, dest="max_emails",
        help="Max emails to ingest (default 5; 0 = unlimited).",
    )
    parser.add_argument(
        "--card", default=None,
        help="Card nickname (default: same as label).",
    )
    parser.add_argument(
        "--trigger", default="manual",
        choices=["manual", "scheduled", "shortcut"],
        help="Trigger source (recorded in sync_runs).",
    )
    args = parser.parse_args()

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env",
              file=sys.stderr)
        return 1
    if not args.label:
        print("ERROR: --label or GMAIL_LABEL env var must be provided",
              file=sys.stderr)
        return 1

    card = args.card or args.label
    cap = args.max_emails if args.max_emails > 0 else None

    print(f"Ingesting label={args.label!r}  card={card!r}  "
          f"max={'unlimited' if cap is None else cap}")
    print()

    summary = ingest_label(
        label=args.label,
        user=user,
        password=password,
        card_nickname=card,
        max_emails=cap,
        trigger=args.trigger,
    )

    print("=" * 60)
    print(f"Sync run #{summary['run_id']} done")
    print("=" * 60)
    print(f"Emails processed:      {summary['emails_processed']}")
    print(f"Transactions created:  {summary['transactions_created']}")
    print(f"  via cache:           {summary['cache_hits']}")
    print(f"  via LLM:             {summary['llm_calls']}")
    print(f"Duplicates skipped:    {summary['duplicates']}")
    print(f"Parse failures:        {summary['parse_failures']}")
    print(f"Errors:                {summary['errors']}")
    print(f"UID range:             {summary['starting_uid']} -> {summary['max_uid']}")
    if summary["error_log"]:
        print("Error log:")
        for line in summary["error_log"]:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
