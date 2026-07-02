"""One-shot: re-run normalize_merchant() over existing data.

Use after changing the regex in merchants.py so that:
  - transactions.merchant_normalized matches the new logic
  - merchant_rules keys are re-keyed (and merged when duplicates appear)
"""

import sys

from finance_tracker.db import get_connection, transaction
from finance_tracker.merchants import normalize_merchant


def main() -> int:
    conn = get_connection()
    try:
        # 1. Re-normalize transactions table
        rows = conn.execute(
            "SELECT id, merchant_raw, merchant_normalized FROM transactions"
        ).fetchall()
        txn_changes = 0
        for r in rows:
            new_norm = normalize_merchant(r["merchant_raw"])
            if new_norm != r["merchant_normalized"]:
                with transaction(conn):
                    conn.execute(
                        "UPDATE transactions SET merchant_normalized = ? WHERE id = ?",
                        (new_norm, r["id"]),
                    )
                print(f"  txn #{r['id']}: {r['merchant_normalized']!r} -> {new_norm!r}")
                txn_changes += 1
        print(f"  Transactions updated: {txn_changes}")
        print()

        # 2. Re-key merchant_rules (and merge collisions by max confirmation_count)
        rules = conn.execute(
            "SELECT merchant_normalized, category, tags, source, confirmation_count "
            "FROM merchant_rules"
        ).fetchall()

        new_map: dict[str, dict] = {}
        any_change = False
        for r in rules:
            old_key = r["merchant_normalized"]
            new_key = normalize_merchant(old_key)
            if new_key != old_key:
                any_change = True
            entry = {
                "merchant_normalized": new_key,
                "category": r["category"],
                "tags": r["tags"],
                "source": r["source"],
                "confirmation_count": r["confirmation_count"],
            }
            if new_key in new_map:
                # Conflict: keep the one with higher confirmation_count
                existing = new_map[new_key]
                if entry["confirmation_count"] > existing["confirmation_count"]:
                    new_map[new_key] = entry
                print(f"  rules merged: '{old_key}' overlaps -> '{new_key}'")
            else:
                new_map[new_key] = entry
                if new_key != old_key:
                    print(f"  rule re-keyed: {old_key!r} -> {new_key!r}")

        if any_change:
            with transaction(conn):
                conn.execute("DELETE FROM merchant_rules")
                for k, e in new_map.items():
                    conn.execute(
                        "INSERT INTO merchant_rules "
                        "(merchant_normalized, category, tags, source, confirmation_count) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (e["merchant_normalized"], e["category"],
                         e["tags"], e["source"], e["confirmation_count"]),
                    )
            print(f"  merchant_rules: rebuilt with {len(new_map)} rule(s)")
        else:
            print("  merchant_rules: no changes needed")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
