"""Bidirectional sync between SQLite transactions and Notion data sources.

V2 (two-database architecture):
  - Category is now a Relation pointing to Categories DB (was: Select)
  - Status, Foreign columns no longer pushed to Notion (kept in SQLite)
  - Pull/push both translate Category between SQLite string and Notion page_id

Field ownership (unchanged from V1):
  Notion-owned (overwrite SQLite on pull):
    merchant_display, notes, category, tags
  SQLite-owned (always pushed; never read back from Notion):
    everything else (amount, dates, raw merchant, currency, source, etc.)
"""

import json
import os
import sqlite3
from typing import Any

from notion_client import Client

from finance_tracker.db import get_connection, transaction
from finance_tracker.merchants import update_merchant_rule


# ---------------------------------------------------------------------------
# Notion property -> Python value extractors
# ---------------------------------------------------------------------------


def _get_title_text(prop: dict) -> str:
    items = prop.get("title", []) or []
    return "".join(i.get("plain_text", "") for i in items)


def _get_rich_text(prop: dict) -> str:
    items = prop.get("rich_text", []) or []
    return "".join(i.get("plain_text", "") for i in items)


def _get_select_name(prop: dict) -> str | None:
    sel = prop.get("select")
    return sel["name"] if sel else None


def _get_multi_select_names(prop: dict) -> list[str]:
    items = prop.get("multi_select", []) or []
    return [i["name"] for i in items]


def _get_relation_ids(prop: dict) -> list[str]:
    items = prop.get("relation", []) or []
    return [i["id"] for i in items]


# ---------------------------------------------------------------------------
# Python value -> Notion property builders
# ---------------------------------------------------------------------------


def _title(text: str | None) -> dict:
    text = text or ""
    return {"title": [{"text": {"content": text}}]} if text else {"title": []}


def _rich_text(text: str | None) -> dict:
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"text": {"content": text}}]}


def _select(name: str | None) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _multi_select(names: list[str]) -> dict:
    return {"multi_select": [{"name": n} for n in (names or [])]}


def _date(iso: str | None) -> dict:
    return {"date": {"start": iso}} if iso else {"date": None}


def _number(n: float | None) -> dict:
    return {"number": float(n)} if n is not None else {"number": None}


def _relation(page_ids: list[str]) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


# ---------------------------------------------------------------------------
# Categories cache: name <-> page_id
# ---------------------------------------------------------------------------


def fetch_categories_map(notion: Client, ds_id: str) -> dict[str, str]:
    """Query Categories DB, return {category_name: page_id}."""
    out: dict[str, str] = {}
    cursor: str | None = None
    has_more = True
    while has_more:
        kwargs: dict = {"data_source_id": ds_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)
        for page in result["results"]:
            name = _get_title_text(page["properties"].get("Name", {}))
            if name:
                out[name] = page["id"]
        has_more = result.get("has_more", False)
        cursor = result.get("next_cursor")
    return out


# ---------------------------------------------------------------------------
# SQLite row -> Notion properties
# ---------------------------------------------------------------------------


# The Notion Transactions table is a pure SPENDING ledger: income/refund rows
# are never pushed (see push_new_transactions). Income/net is viewed only in the
# monthly report. Income stays in SQLite so the report still counts it.
INCOME_CATEGORIES = {"Income & Refund"}


def build_notion_properties(
    row: sqlite3.Row, categories_map: dict[str, str]
) -> dict[str, Any]:
    """Convert a SQLite transactions row to Notion properties payload."""
    tags = json.loads(row["tags"]) if row["tags"] else []
    cat_name = row["category"]
    cat_page_id = categories_map.get(cat_name) if cat_name else None
    cat_relation_ids = [cat_page_id] if cat_page_id else []

    # Amount is the SGD-normalized value; show the original currency (and the
    # original amount when it was converted) so nothing is hidden.
    orig_cur = (row["currency_original"] or "").upper()
    if orig_cur and orig_cur != (row["currency_base"] or "").upper():
        currency_text = f"{orig_cur} {row['amount_original']:.2f}"
    else:
        currency_text = orig_cur or row["currency_base"]

    return {
        "Merchant":       _title(row["merchant_display"] or row["merchant_raw"]),
        "Tags":           _multi_select(tags),
        "Date":           _date(row["transaction_date"]),
        "Card":           _select(row["card_nickname"]),
        "Amount":         _number(row["amount_base"]),  # spending (positive)
        "Category":       _relation(cat_relation_ids),
        "Currency":       _rich_text(currency_text),
        "Merchant (raw)": _rich_text(row["merchant_raw"]),
        "Source":         _select(row["classification_source"]),
        "DB ID":          _rich_text(str(row["id"])),
        "Notes":          _rich_text(row["notes"]),
        # Status / Foreign intentionally NOT pushed (V2)
    }


# ---------------------------------------------------------------------------
# Notion page -> SQLite update dict (only Notion-owned fields)
# ---------------------------------------------------------------------------


def extract_notion_owned_values(
    page: dict, reverse_categories_map: dict[str, str]
) -> dict[str, Any]:
    """Extract Notion-owned fields. reverse_map = {page_id: category_name}."""
    props = page.get("properties", {})
    cat_page_ids = _get_relation_ids(props.get("Category", {}))
    cat_name = reverse_categories_map.get(cat_page_ids[0]) if cat_page_ids else None
    return {
        "merchant_display": _get_title_text(props.get("Merchant", {})),
        "notes":            _get_rich_text(props.get("Notes", {})) or None,
        "category":         cat_name,
        "tags":             _get_multi_select_names(props.get("Tags", {})),
    }


# ---------------------------------------------------------------------------
# Push: SQLite -> Notion
# ---------------------------------------------------------------------------


def push_new_transactions(
    conn: sqlite3.Connection,
    notion: Client,
    ds_id: str,
    categories_map: dict[str, str],
) -> dict[str, int]:
    # Income/refund rows are never pushed — the Notion table is spend-only.
    rows = conn.execute(
        "SELECT * FROM transactions "
        "WHERE notion_page_id IS NULL AND category NOT IN ('Income & Refund') "
        "ORDER BY id"
    ).fetchall()

    created = 0
    failed = 0
    error_log: list[str] = []

    for row in rows:
        try:
            properties = build_notion_properties(row, categories_map)
            page = notion.pages.create(
                parent={"data_source_id": ds_id},
                properties=properties,
            )
            page_id = page["id"]
            with transaction(conn):
                conn.execute(
                    "UPDATE transactions "
                    "SET notion_page_id = ?, "
                    "    last_notion_sync = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (page_id, row["id"]),
                )
            created += 1
        except Exception as e:
            failed += 1
            error_log.append(
                f"transaction #{row['id']} ({row['merchant_raw']}): "
                f"{type(e).__name__}: {e}"
            )

    return {"created": created, "failed": failed, "error_log": error_log}


# ---------------------------------------------------------------------------
# Pull: Notion -> SQLite
# ---------------------------------------------------------------------------


def pull_notion_edits(
    conn: sqlite3.Connection,
    notion: Client,
    ds_id: str,
    reverse_categories_map: dict[str, str],
) -> dict[str, Any]:
    updated = 0
    rule_updates = 0
    error_log: list[str] = []
    cursor: str | None = None
    has_more = True

    while has_more:
        kwargs: dict = {"data_source_id": ds_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        result = notion.data_sources.query(**kwargs)

        for page in result["results"]:
            try:
                page_id = page["id"]
                last_edited = page["last_edited_time"]

                row = conn.execute(
                    "SELECT id, merchant_normalized, merchant_display, notes, "
                    "       category, tags, last_notion_sync "
                    "FROM transactions WHERE notion_page_id = ?",
                    (page_id,),
                ).fetchone()
                if not row:
                    continue

                last_sync = row["last_notion_sync"]
                if last_sync and last_edited <= last_sync:
                    continue

                notion_vals = extract_notion_owned_values(page, reverse_categories_map)
                cur_tags_list = json.loads(row["tags"]) if row["tags"] else []

                changes: dict[str, Any] = {}
                if notion_vals["merchant_display"] != (row["merchant_display"] or ""):
                    changes["merchant_display"] = notion_vals["merchant_display"]
                if (notion_vals["notes"] or None) != (row["notes"] or None):
                    changes["notes"] = notion_vals["notes"]
                if notion_vals["category"] != row["category"]:
                    changes["category"] = notion_vals["category"]
                if sorted(notion_vals["tags"]) != sorted(cur_tags_list):
                    changes["tags"] = json.dumps(notion_vals["tags"])

                set_clauses: list[str] = []
                values: list[Any] = []
                for k, v in changes.items():
                    set_clauses.append(f"{k} = ?")
                    values.append(v)
                set_clauses.append("last_notion_sync = CURRENT_TIMESTAMP")
                if changes:
                    set_clauses.insert(0, "classification_source = 'manual'")
                    sql = (
                        f"UPDATE transactions SET {', '.join(set_clauses)} "
                        f"WHERE id = ?"
                    )
                    values.append(row["id"])
                    with transaction(conn):
                        conn.execute(sql, values)
                    updated += 1

                    if "category" in changes or "tags" in changes:
                        update_merchant_rule(
                            conn,
                            merchant_normalized=row["merchant_normalized"],
                            category=notion_vals["category"] or row["category"],
                            tags=notion_vals["tags"],
                            source="manual",
                        )
                        rule_updates += 1
                else:
                    with transaction(conn):
                        conn.execute(
                            "UPDATE transactions "
                            "SET last_notion_sync = CURRENT_TIMESTAMP "
                            "WHERE id = ?",
                            (row["id"],),
                        )
            except Exception as e:
                error_log.append(
                    f"page {page.get('id', '?')}: {type(e).__name__}: {e}"
                )

        has_more = result.get("has_more", False)
        cursor = result.get("next_cursor")

    return {"updated": updated, "rule_updates": rule_updates, "error_log": error_log}


# ---------------------------------------------------------------------------
# Top-level sync orchestrator
# ---------------------------------------------------------------------------


def sync(
    do_pull: bool = True,
    do_push: bool = True,
) -> dict[str, Any]:
    """Run a full sync (pull then push). Returns summary dict."""
    token = os.environ["NOTION_TOKEN"]
    transactions_ds_id = os.environ["NOTION_DATA_SOURCE_ID"]
    categories_ds_id = os.environ["NOTION_CATEGORIES_DATA_SOURCE_ID"]
    notion = Client(auth=token)

    # Build categories cache (used by both push and pull)
    categories_map = fetch_categories_map(notion, categories_ds_id)
    reverse_map = {pid: name for name, pid in categories_map.items()}

    summary: dict[str, Any] = {}
    conn = get_connection()
    try:
        if do_pull:
            summary["pull"] = pull_notion_edits(
                conn, notion, transactions_ds_id, reverse_map
            )
        if do_push:
            summary["push"] = push_new_transactions(
                conn, notion, transactions_ds_id, categories_map
            )
    finally:
        conn.close()
    return summary
