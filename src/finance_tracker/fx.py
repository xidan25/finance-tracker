"""Currency conversion to the system base currency (SGD).

The tracker consolidates every transaction into one base currency (SGD) so that
totals are a single meaningful number. A single fixed CAD→SGD rate is stored in
the meta table (key 'fx_rate_cad_sgd'); SGD converts at 1.0.

The original amount/currency are always preserved on the row (amount_original /
currency_original). amount_base/currency_base/fx_rate hold the SGD-normalized
values, so re-converting later with a better rate is always possible.
"""

import sqlite3

BASE_CURRENCY = "SGD"

# Seed value (1 CAD ≈ 0.923 SGD, 2026-06). Override anytime via:
#   UPDATE meta SET value='0.95' WHERE key='fx_rate_cad_sgd';
_DEFAULT_CAD_SGD = 0.923
_META_KEY_CAD_SGD = "fx_rate_cad_sgd"


def get_base_currency(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'base_currency'"
    ).fetchone()
    return row[0] if row else BASE_CURRENCY


def get_cad_sgd_rate(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_META_KEY_CAD_SGD,)
    ).fetchone()
    try:
        return float(row[0]) if row else _DEFAULT_CAD_SGD
    except (TypeError, ValueError):
        return _DEFAULT_CAD_SGD


def rate_to_base(conn: sqlite3.Connection, currency: str) -> float:
    """Multiplier converting `currency` into the SGD base. Raises for unknowns."""
    c = (currency or "").upper()
    if c == BASE_CURRENCY:
        return 1.0
    if c == "CAD":
        return get_cad_sgd_rate(conn)
    raise ValueError(
        f"No fixed FX rate configured for {c}->{BASE_CURRENCY}. "
        f"Add one (e.g. meta key 'fx_rate_{c.lower()}_sgd') and extend fx.rate_to_base."
    )


def to_base(conn: sqlite3.Connection, amount: float, currency: str) -> tuple[float, float]:
    """Return (amount_in_base_SGD, fx_rate_used)."""
    rate = rate_to_base(conn, currency)
    return round(amount * rate, 2), rate
