"""Smoke tests for the database layer."""

import json
import sqlite3

import pytest

from finance_tracker.db import (
    MIGRATIONS,
    get_connection,
    get_schema_version,
    init_db,
    transaction,
)


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a freshly initialized database in a temp dir."""
    db_path = tmp_path / "test_finance.db"
    init_db(db_path)
    return db_path


def test_schema_creates_all_tables(tmp_db):
    conn = get_connection(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    table_names = {r["name"] for r in rows}
    assert table_names == {"meta", "transactions", "merchant_rules", "sync_runs"}


def test_schema_version_is_latest(tmp_db):
    conn = get_connection(tmp_db)
    latest = max(target for target, _ in MIGRATIONS)
    assert get_schema_version(conn) == latest


def test_insert_and_query_transaction(tmp_db):
    conn = get_connection(tmp_db)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO transactions (
                source_email_id, transaction_date, status,
                amount_original, currency_original,
                amount_base, currency_base,
                card_nickname, merchant_raw, merchant_normalized,
                category, tags, classification_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-test-001", "2026-04-29", "pending",
                12.50, "CAD",
                12.50, "CAD",
                "scotiabank visa", "STARBUCKS #1234", "starbucks",
                "Food & Drink", json.dumps(["coffee"]), "preset",
            ),
        )

    row = conn.execute(
        "SELECT * FROM transactions WHERE source_email_id = ?",
        ("msg-test-001",),
    ).fetchone()
    assert row is not None
    assert row["merchant_normalized"] == "starbucks"
    assert row["category"] == "Food & Drink"
    assert json.loads(row["tags"]) == ["coffee"]
    assert row["amount_original"] == 12.50
    assert row["status"] == "pending"


def test_unique_source_email_id_dedup(tmp_db):
    """Same source_email_id should be rejected by UNIQUE constraint."""
    conn = get_connection(tmp_db)

    def _insert():
        conn.execute(
            """INSERT INTO transactions (
                source_email_id, status, amount_original, currency_original,
                amount_base, currency_base, card_nickname
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("dup-id", "pending", 1.0, "CAD", 1.0, "CAD", "scotiabank visa"),
        )

    _insert()
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        _insert()


def test_merchant_rules_insert(tmp_db):
    conn = get_connection(tmp_db)
    with transaction(conn):
        conn.execute(
            """INSERT INTO merchant_rules (
                merchant_normalized, category, tags, source
            ) VALUES (?, ?, ?, ?)""",
            ("starbucks", "Food & Drink", json.dumps(["coffee"]), "preset"),
        )

    row = conn.execute(
        "SELECT * FROM merchant_rules WHERE merchant_normalized = ?",
        ("starbucks",),
    ).fetchone()
    assert row["category"] == "Food & Drink"
    assert row["confirmation_count"] == 1
