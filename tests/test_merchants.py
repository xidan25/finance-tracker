"""Tests for merchant normalization + cache CRUD."""

import json

import pytest

from finance_tracker.db import get_connection, init_db
from finance_tracker.merchants import (
    get_merchant_rule,
    increment_confirmation,
    insert_merchant_rule,
    normalize_merchant,
    update_merchant_rule,
)


# ---------------------------------------------------------------------------
# normalize_merchant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FREEDOM MOBILE", "freedom mobile"),
        ("STARBUCKS #1234", "starbucks"),
        ("STARBUCKS  #5678", "starbucks"),
        ("Tim Hortons #423", "tim hortons"),
        ("SHELL STORE 1234", "shell"),
        ("AMAZON.CA*MK7P3", "amazon.ca"),
        ("UBER *EATS", "uber"),
        ("  Stripe  ", "stripe"),
        ("554 WATSONS CP TOWER", "watsons cp tower"),    # leading numeric ID stripped
        ("789 WATSONS ION ORCHARD", "watsons ion orchard"),
    ],
)
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected


# ---------------------------------------------------------------------------
# Cache CRUD (uses tmp_db fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_finance.db"
    init_db(db_path)
    return db_path


def test_get_merchant_rule_miss_returns_none(tmp_db):
    conn = get_connection(tmp_db)
    assert get_merchant_rule(conn, "freedom mobile") is None


def test_insert_and_get_merchant_rule(tmp_db):
    conn = get_connection(tmp_db)
    insert_merchant_rule(
        conn, "starbucks", "Food & Drink", ["coffee"], "preset"
    )
    rule = get_merchant_rule(conn, "starbucks")
    assert rule is not None
    assert rule.category == "Food & Drink"
    assert rule.tags == ["coffee"]
    assert rule.source == "preset"
    assert rule.confirmation_count == 1


def test_increment_confirmation(tmp_db):
    conn = get_connection(tmp_db)
    insert_merchant_rule(conn, "tim hortons", "Food & Drink", ["coffee"], "preset")
    increment_confirmation(conn, "tim hortons")
    increment_confirmation(conn, "tim hortons")
    rule = get_merchant_rule(conn, "tim hortons")
    assert rule.confirmation_count == 3


def test_update_merchant_rule(tmp_db):
    conn = get_connection(tmp_db)
    insert_merchant_rule(conn, "uber", "Transport", [], "llm-confirmed")
    update_merchant_rule(conn, "uber", "Transport", ["taxi"], "manual")
    rule = get_merchant_rule(conn, "uber")
    assert rule.tags == ["taxi"]
    assert rule.source == "manual"
