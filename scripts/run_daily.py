"""Daily orchestrator: ingest new emails -> sync Notion -> notify.

Triggered by launchd at 09:00 daily (see launchd/finance-tracker.daily.plist.template),
or manually via run_daily.sh / Shortcuts.app.

Notification policy:
  - any error  -> noisy notification (Basso) + log
  - success with new rows -> silent notification "X new, CA$Y"
  - success with no new rows -> no notification
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from finance_tracker.db import get_connection
from finance_tracker.ingest import ingest_label
from finance_tracker.notion_sync import sync


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "logs" / "run_daily.log"


def log_block(lines: list[str]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write(f"[{ts}]\n")
        for line in lines:
            f.write(line + "\n")
        f.write("\n")


def notify(title: str, body: str, sound: str | None = None) -> None:
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    if sound:
        script += f' sound name "{sound}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def get_max_id() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_new_summary(prev_max_id: int) -> tuple[int, dict[str, float]]:
    """Return (new_count, {currency: new_spend}) for rows newer than prev_max_id.

    Count covers all new rows; the spend sum EXCLUDES income/refund so income is
    never added on top of spending. Sums are grouped by currency.
    """
    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id > ?", (prev_max_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT currency_base, COALESCE(SUM(amount_base), 0.0) "
            "FROM transactions WHERE id > ? AND category NOT IN ('Income & Refund') "
            "GROUP BY currency_base",
            (prev_max_id,),
        ).fetchall()
        by_currency = {r[0]: float(r[1]) for r in rows}
        return int(count), by_currency
    finally:
        conn.close()


def main() -> int:
    load_dotenv()

    log_lines: list[str] = []
    errors: list[str] = []

    # One label per card. GMAIL_LABELS (comma-separated) takes precedence;
    # fall back to the single GMAIL_LABEL for backward compatibility.
    labels_raw = os.environ.get("GMAIL_LABELS") or os.environ.get("GMAIL_LABEL") or ""
    labels = [l.strip() for l in labels_raw.split(",") if l.strip()]
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not labels or not user or not password:
        msg = "GMAIL_LABELS/GMAIL_LABEL / GMAIL_USER / GMAIL_APP_PASSWORD missing"
        log_block([f"FATAL: {msg}"])
        notify("Finance tracker error", msg, sound="Basso")
        return 1

    prev_max_id = get_max_id()

    for label in labels:
        try:
            ing = ingest_label(
                label=label,
                user=user,
                password=password,
                card_nickname=label,
                max_emails=50,
                trigger="scheduled",
            )
            log_lines.append(
                f"Ingest [{label}] run #{ing['run_id']}: "
                f"emails={ing['emails_processed']} "
                f"created={ing['transactions_created']} "
                f"cache={ing['cache_hits']} llm={ing['llm_calls']} "
                f"dups={ing['duplicates']} skipped={ing['skipped']} "
                f"parse_fail={ing['parse_failures']} "
                f"errors={ing['errors']}"
            )
            if ing["errors"]:
                errors.extend(f"ingest[{label}]: {e}" for e in ing["error_log"])
        except Exception as e:
            errors.append(f"Ingest [{label}] crashed: {type(e).__name__}: {e}")
            log_lines.append(errors[-1])

    try:
        sm = sync(do_pull=True, do_push=True)
        if "pull" in sm:
            p = sm["pull"]
            log_lines.append(
                f"Pull: updated={p['updated']} rule_updates={p['rule_updates']}"
            )
            if p["error_log"]:
                errors.extend(f"pull: {e}" for e in p["error_log"])
        if "push" in sm:
            q = sm["push"]
            log_lines.append(f"Push: created={q['created']} failed={q['failed']}")
            if q["error_log"]:
                errors.extend(f"push: {e}" for e in q["error_log"])
    except Exception as e:
        errors.append(f"Sync crashed: {type(e).__name__}: {e}")
        log_lines.append(errors[-1])

    new_count, new_by_currency = get_new_summary(prev_max_id)
    sums_str = ", ".join(
        f"{cur} {amt:.2f}" for cur, amt in sorted(new_by_currency.items())
    )
    log_lines.append(f"New rows: count={new_count} sums=[{sums_str}]")

    if errors:
        log_lines.append("ERRORS:")
        log_lines.extend(f"  - {e}" for e in errors)
        log_block(log_lines)
        body = errors[0][:200]
        if len(errors) > 1:
            body += f" (+{len(errors) - 1} more)"
        notify("Finance tracker error", body, sound="Basso")
        return 1

    log_block(log_lines)

    if new_count > 0:
        notify("Finance tracker", f"{new_count} new · {sums_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
