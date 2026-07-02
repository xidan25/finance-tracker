"""Database connection and migration helpers.

Uses Python's stdlib sqlite3 — no ORM. Provides:
  - get_connection(): opens a configured connection (Row factory, WAL, FK on)
  - init_db():        runs all pending migrations to bring DB to latest version
  - transaction():    context manager for safe commit/rollback
  - get_schema_version(): reads meta.schema_version
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "finance.db"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Migration sequence: each file brings DB from version N-1 to N.
# Add new entries here when introducing future schema versions.
MIGRATIONS = [
    (1, "schema_v1.sql"),
    (2, "schema_v2.sql"),
]


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: intentionally NOT using detect_types=PARSE_DECLTYPES.
    # Python 3.12+ deprecated the default DATE/TIMESTAMP converters and they
    # also can't parse ISO 8601 with 'T' separator. We store timestamps as
    # plain ISO strings and let callers do any datetime parsing they need.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager: commit on success, rollback on exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return current schema_version from meta table; 0 if uninitialized."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Initialize / migrate the database to the latest schema version.

    Idempotent: running multiple times is safe.
    Apply each migration only if current_version < target_version.
    """
    conn = get_connection(db_path)
    try:
        current = get_schema_version(conn)
        for target_version, filename in MIGRATIONS:
            if current < target_version:
                path = MIGRATIONS_DIR / filename
                if not path.exists():
                    raise FileNotFoundError(
                        f"Migration file missing: {path}"
                    )
                sql = path.read_text(encoding="utf-8")
                conn.executescript(sql)
                conn.commit()
                current = target_version
    finally:
        conn.close()
