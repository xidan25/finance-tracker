"""Display recent transactions from the database (read-only)."""

import argparse
import json

from finance_tracker.db import get_connection


def main() -> int:
    parser = argparse.ArgumentParser(description="Show recent transactions.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--show-rules", action="store_true",
                        help="Also dump the merchant_rules cache.")
    args = parser.parse_args()

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, transaction_date, status,
                      amount_original, currency_original, was_foreign,
                      merchant_raw, merchant_normalized,
                      category, tags, classification_source
               FROM transactions ORDER BY id DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()

        if not rows:
            print("(no transactions yet)")
        else:
            print(f"{'#':>3}  {'date':10}  {'status':7}  {'amount':>10}  "
                  f"{'merchant':25}  {'category':22}  tags  src")
            print("-" * 110)
            for r in rows:
                tags = json.loads(r["tags"]) if r["tags"] else []
                amount = f"{r['amount_original']:.2f} {r['currency_original']}"
                if r["was_foreign"]:
                    amount += "*"  # mark foreign
                merchant = (r["merchant_raw"] or "")[:25]
                category = (r["category"] or "")[:22]
                print(f"{r['id']:>3}  {r['transaction_date']}  {r['status']:7}  "
                      f"{amount:>10}  {merchant:25}  {category:22}  "
                      f"{tags}  {r['classification_source']}")

        if args.show_rules:
            print()
            print("=== merchant_rules cache ===")
            rules = conn.execute(
                "SELECT merchant_normalized, category, tags, source, "
                "confirmation_count FROM merchant_rules "
                "ORDER BY confirmation_count DESC, merchant_normalized"
            ).fetchall()
            if not rules:
                print("(no rules cached yet)")
            for r in rules:
                tags = json.loads(r["tags"]) if r["tags"] else []
                print(f"  {r['merchant_normalized']:25}  {r['category']:22}  "
                      f"{tags}  src={r['source']}  count={r['confirmation_count']}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
