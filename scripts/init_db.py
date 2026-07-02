"""One-shot DB initializer."""

from finance_tracker.db import (
    DEFAULT_DB_PATH,
    get_connection,
    get_schema_version,
    init_db,
)


def main() -> None:
    print(f"Initializing database at: {DEFAULT_DB_PATH}")
    init_db()

    conn = get_connection()
    try:
        version = get_schema_version(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        print(f"Schema initialized (version {version})")
        print(f"  Tables: {', '.join(t['name'] for t in tables)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
