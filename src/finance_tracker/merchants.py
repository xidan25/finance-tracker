"""Merchant normalization + merchant_rules cache CRUD.

Normalize: strip store numbers, lowercase, collapse whitespace.
Cache: read/write per-merchant category & tags in merchant_rules table.
"""

import json
import re
import sqlite3
from dataclasses import dataclass

from finance_tracker.db import transaction


# Strip patterns applied during normalization (order matters)
_STRIP_PATTERNS = [
    re.compile(r"^\d+\s+"),                            # leading numeric ID: "554 WATSONS"
    re.compile(r"\s*#\s*\d+"),                         # "STARBUCKS #1234"
    re.compile(r"\s+store\s+\d+", re.IGNORECASE),      # "SHELL STORE 1234"
    re.compile(r"\*+[A-Z0-9]{3,}"),                    # "AMAZON.CA*MK7P3" reference codes
    re.compile(r"\s+\d{4,}"),                          # trailing 4+ digit IDs
    re.compile(r"\s+#\s*[A-Z0-9]+"),                   # alphanumeric "#ABC123"
]


@dataclass
class MerchantRule:
    merchant_normalized: str
    category: str
    tags: list[str]
    source: str               # 'preset' | 'llm-confirmed' | 'manual'
    confirmation_count: int


def normalize_merchant(merchant_raw: str) -> str:
    """Lowercase + strip store numbers / reference codes / noise."""
    s = merchant_raw.strip()
    for pat in _STRIP_PATTERNS:
        s = pat.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lower()
    s = re.sub(r"[.\-_,;:]+$", "", s)  # trailing punctuation
    return s


# ---------------------------------------------------------------------------
# Cache CRUD
# ---------------------------------------------------------------------------


def get_merchant_rule(
    conn: sqlite3.Connection, merchant_normalized: str
) -> MerchantRule | None:
    row = conn.execute(
        "SELECT merchant_normalized, category, tags, source, confirmation_count "
        "FROM merchant_rules WHERE merchant_normalized = ?",
        (merchant_normalized,),
    ).fetchone()
    if not row:
        return None
    tags = json.loads(row["tags"]) if row["tags"] else []
    return MerchantRule(
        merchant_normalized=row["merchant_normalized"],
        category=row["category"],
        tags=tags,
        source=row["source"],
        confirmation_count=row["confirmation_count"],
    )


def insert_merchant_rule(
    conn: sqlite3.Connection,
    merchant_normalized: str,
    category: str,
    tags: list[str],
    source: str,
) -> None:
    """Insert a new merchant rule."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO merchant_rules "
            "(merchant_normalized, category, tags, source) "
            "VALUES (?, ?, ?, ?)",
            (merchant_normalized, category, json.dumps(tags), source),
        )


def increment_confirmation(
    conn: sqlite3.Connection, merchant_normalized: str
) -> None:
    """Increment confirmation_count for an existing rule (cache hit)."""
    with transaction(conn):
        conn.execute(
            "UPDATE merchant_rules SET confirmation_count = confirmation_count + 1 "
            "WHERE merchant_normalized = ?",
            (merchant_normalized,),
        )


def update_merchant_rule(
    conn: sqlite3.Connection,
    merchant_normalized: str,
    category: str,
    tags: list[str],
    source: str = "manual",
) -> None:
    """Update a rule's category/tags (e.g., from manual Notion edit)."""
    with transaction(conn):
        conn.execute(
            "UPDATE merchant_rules SET category = ?, tags = ?, source = ? "
            "WHERE merchant_normalized = ?",
            (category, json.dumps(tags), source, merchant_normalized),
        )
