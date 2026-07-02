"""CLI: generate a monthly finance report (Markdown only for now).

Examples:
    python scripts/generate_monthly_report.py --month 2026-05
    python scripts/generate_monthly_report.py --month 2026-05 --no-llm
    python scripts/generate_monthly_report.py --month 2026-05 --overwrite
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from finance_tracker.monthly_report import generate_report


def previous_month_str() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1:04d}-12"
    return f"{today.year:04d}-{today.month - 1:02d}"


def main() -> int:
    load_dotenv()

    p = argparse.ArgumentParser(description="Generate a monthly finance report.")
    p.add_argument("--month", help="YYYY-MM (default: previous month).")
    p.add_argument("--no-llm", action="store_true", help="Skip Layer 2 LLM analysis.")
    p.add_argument("--export-prompt", action="store_true",
                   help="No API call: write Layer 1 + a prompt/data doc to paste "
                        "into claude.ai (Max) for the narrative. Implies --no-llm.")
    p.add_argument("--overwrite", action="store_true",
                   help="Replace existing report (md file and Notion page).")
    p.add_argument("--out-dir", default="reports", help="Output dir (default: reports/).")
    p.add_argument("--push-notion", action="store_true",
                   help="Also push the report to the Notion Monthly Reports DB.")
    args = p.parse_args()

    month = args.month or previous_month_str()
    out_path = Path(args.out_dir) / f"{month}.md"

    print(f"Generating report for {month} -> {out_path}")
    print()

    try:
        result = generate_report(
            month=month,
            out_path=out_path,
            do_llm=not args.no_llm and not args.export_prompt,
            overwrite=args.overwrite,
        )
    except (FileExistsError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.export_prompt:
        from finance_tracker.monthly_report import build_export_doc
        prompt_path = Path(args.out_dir) / f"{month}-prompt.md"
        prompt_path.write_text(build_export_doc(result["agg"]), encoding="utf-8")
        print(f"Prompt doc:   {prompt_path}  ← 复制进 claude.ai (Max) 跑叙事，零 API")

    print("=" * 60)
    print(f"Report:       {result['path']}")
    print(f"Currency:     {result['currency']}")
    print(f"Transactions: {result['n_transactions']}")
    print(f"Total spent:  {result['currency']} {result['total_spent']:.2f}")
    print(f"Notes:        {result['n_with_notes']}")
    print(f"Layer 2 LLM:  {'yes' if result['had_llm'] else 'skipped'}")

    if args.push_notion:
        from finance_tracker.notion_writer import push_report
        print()
        print("Pushing to Notion ...")
        # Reuse the aggregate + insights already computed by generate_report —
        # no second LLM call.
        try:
            r = push_report(result["agg"], result["insights"], overwrite=args.overwrite)
        except Exception as e:
            print(f"ERROR pushing to Notion: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"Notion:       {r['action']}  ({r.get('url') or r.get('reason')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
