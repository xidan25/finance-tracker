"""End-to-end pipeline: fetch -> parse -> classify -> insert transactions.

Each email is processed independently; per-email failures are logged into
sync_runs.error_log and don't block the rest of the batch.
"""

import json
import sqlite3
from typing import Any

from finance_tracker.classifier import ClassifierResult, classify_merchant
from finance_tracker.db import get_connection, transaction
from finance_tracker import fx
from finance_tracker.email_client import (
    ParsedEmail,
    fetch_new_emails,
    get_last_uid,
    set_last_uid,
)
from finance_tracker.merchants import (
    MerchantRule,
    get_merchant_rule,
    increment_confirmation,
    insert_merchant_rule,
    normalize_merchant,
)
from finance_tracker.parser import (
    ParsedTransaction,
    is_ingestable,
    parse_transaction,
)


# ---------------------------------------------------------------------------
# Transactions table helpers
# ---------------------------------------------------------------------------


def transaction_exists(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM transactions WHERE source_email_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def insert_transaction(
    conn: sqlite3.Connection,
    email: ParsedEmail,
    parsed: ParsedTransaction,
    classification: ClassifierResult,
    classification_source: str,
    card_nickname: str,
) -> int:
    """Insert a transaction row. Returns row id."""
    merchant_normalized = normalize_merchant(parsed.merchant_raw)
    txn_date = email.received_at.date().isoformat()

    # Normalize every transaction to the SGD base so totals are single-currency.
    # Original amount/currency are kept verbatim in amount_original/currency_original.
    amount_base, fx_rate = fx.to_base(conn, parsed.amount, parsed.currency)
    base_currency = fx.get_base_currency(conn)

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                source_email_id, source_received_at,
                transaction_date, posted_date, status,
                amount_original, currency_original,
                amount_base, currency_base, fx_rate, was_foreign,
                card_nickname, merchant_raw, merchant_normalized, merchant_display,
                category, tags,
                classification_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email.message_id,
                email.received_at.isoformat(),
                txn_date,
                txn_date if parsed.status == "posted" else None,
                parsed.status,
                parsed.amount,
                parsed.currency,
                amount_base,          # converted to SGD base (see fx.to_base)
                base_currency,        # always SGD
                fx_rate,              # rate used (1.0 for SGD-native rows)
                1 if parsed.currency.upper() != base_currency else 0,
                card_nickname,
                parsed.merchant_raw,
                merchant_normalized,
                parsed.merchant_raw,   # merchant_display starts = merchant_raw
                classification.category,
                json.dumps(classification.tags),
                classification_source,
            ),
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Sync run audit log
# ---------------------------------------------------------------------------


def start_sync_run(conn: sqlite3.Connection, trigger: str) -> int:
    with transaction(conn):
        cur = conn.execute("INSERT INTO sync_runs (trigger) VALUES (?)", (trigger,))
        return cur.lastrowid


def finalize_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    emails_processed: int,
    transactions_created: int,
    errors: int,
    error_log: list[str],
    status: str,
) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE sync_runs
            SET ended_at = CURRENT_TIMESTAMP,
                emails_processed = ?,
                transactions_created = ?,
                errors = ?,
                error_log = ?,
                status = ?
            WHERE id = ?
            """,
            (
                emails_processed,
                transactions_created,
                errors,
                json.dumps(error_log),
                status,
                run_id,
            ),
        )


# ---------------------------------------------------------------------------
# Per-email ingest
# ---------------------------------------------------------------------------


def ingest_one(
    conn: sqlite3.Connection,
    email: ParsedEmail,
    card_nickname: str,
) -> tuple[int | None, str]:
    """Ingest one email. Returns (transaction_id, source_marker)."""
    if transaction_exists(conn, email.message_id):
        return None, "duplicate"

    if not is_ingestable(email.sender, email.subject, email.body_text):
        return None, "skipped"

    parsed = parse_transaction(email.sender, email.body_text, email.subject)
    if parsed is None:
        return None, "parse_failed"

    merchant_normalized = normalize_merchant(parsed.merchant_raw)

    rule = get_merchant_rule(conn, merchant_normalized)
    if rule is not None:
        classification = ClassifierResult(
            category=rule.category, tags=rule.tags, rationale="cache",
        )
        source = "cache"
        increment_confirmation(conn, merchant_normalized)
    else:
        classification = classify_merchant(
            parsed.merchant_raw, parsed.amount, parsed.currency,
        )
        source = "llm"
        insert_merchant_rule(
            conn,
            merchant_normalized,
            classification.category,
            classification.tags,
            "llm-confirmed",
        )

    txn_id = insert_transaction(
        conn, email, parsed, classification, source, card_nickname
    )
    return txn_id, source


# ---------------------------------------------------------------------------
# Batch ingest
# ---------------------------------------------------------------------------


def ingest_label(
    label: str,
    user: str,
    password: str,
    card_nickname: str,
    max_emails: int | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Fetch + ingest new emails from a Gmail label."""
    conn = get_connection()
    run_id = start_sync_run(conn, trigger)

    counts = {
        "emails_processed": 0,
        "transactions_created": 0,
        "duplicates": 0,
        "skipped": 0,
        "parse_failures": 0,
        "cache_hits": 0,
        "llm_calls": 0,
        "errors": 0,
    }
    error_log: list[str] = []
    starting_uid = get_last_uid(conn, label)
    max_uid = starting_uid

    try:
        for email in fetch_new_emails(
            user=user,
            password=password,
            label=label,
            since_uid=starting_uid,
            max_emails=max_emails,
        ):
            counts["emails_processed"] += 1
            try:
                txn_id, source = ingest_one(conn, email, card_nickname)
                if txn_id is None:
                    if source == "duplicate":
                        counts["duplicates"] += 1
                    elif source == "skipped":
                        counts["skipped"] += 1
                    elif source == "parse_failed":
                        counts["parse_failures"] += 1
                        error_log.append(
                            f"UID {email.uid}: regex parser failed; subject={email.subject!r}"
                        )
                else:
                    counts["transactions_created"] += 1
                    if source == "cache":
                        counts["cache_hits"] += 1
                    elif source == "llm":
                        counts["llm_calls"] += 1
                if email.uid > max_uid:
                    max_uid = email.uid
            except Exception as e:
                counts["errors"] += 1
                error_log.append(
                    f"UID {email.uid}: {type(e).__name__}: {e}"
                )

        if (counts["transactions_created"] + counts["duplicates"]) > 0 and max_uid > starting_uid:
            set_last_uid(conn, label, max_uid)

        status = "success" if counts["errors"] == 0 else "partial"
        finalize_sync_run(
            conn, run_id,
            emails_processed=counts["emails_processed"],
            transactions_created=counts["transactions_created"],
            errors=counts["errors"],
            error_log=error_log,
            status=status,
        )
    except Exception as e:
        finalize_sync_run(
            conn, run_id,
            emails_processed=counts["emails_processed"],
            transactions_created=counts["transactions_created"],
            errors=counts["errors"] + 1,
            error_log=error_log + [f"FATAL: {type(e).__name__}: {e}"],
            status="failure",
        )
        conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "run_id": run_id,
        **counts,
        "error_log": error_log,
        "starting_uid": starting_uid,
        "max_uid": max_uid,
    }
