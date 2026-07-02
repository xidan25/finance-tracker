"""Monthly report: Layer 1 (SQL analytics) + Layer 2 (LLM data-led read).

Layer 1 = pure SQL aggregation. Deterministic, reproducible. Covers four
analytical dimensions: trend (MoM + 3-month average), tag-level (coffee /
eating-out vs cooking-in), time rhythm (weekly + weekday/weekend), and
anomalies/concentration (outliers, one-offs, top-5 share).

Layer 2 = one Claude tool-use call that READS those numbers into findings,
each anchored to a metric, using the month's notes only as evidence to explain
a number. It is analysis-led, not a diary; it keeps one short "life texture"
paragraph at the end. The analytical read runs regardless of note count; only
the life-texture paragraph leans on notes.

Every amount is in the SGD base currency (see fx.py); amounts are summed across
both cards because all rows are normalized to SGD.
"""

import calendar
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from finance_tracker.db import get_connection


INCOME_CATEGORIES = {"Income & Refund"}
MODEL = "claude-sonnet-4-6"

# Food & Drink is "eating out"; Groceries is the "cooking in" proxy.
EATING_OUT_CATEGORY = "Food & Drink"
GROCERIES_CATEGORY = "Groceries"

# A transaction this big relative to the month's average expense is flagged as
# an outlier worth a second look.
OUTLIER_FACTOR = 3.0


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def previous_month(month: str) -> str:
    year, mon = map(int, month.split("-"))
    mon -= 1
    if mon == 0:
        mon, year = 12, year - 1
    return f"{year:04d}-{mon:02d}"


def _prior_months(month: str, n: int) -> list[str]:
    out, m = [], month
    for _ in range(n):
        m = previous_month(m)
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# Layer 1 — SQL aggregates
# ---------------------------------------------------------------------------


def get_base_currency(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'base_currency'"
    ).fetchone()
    return row[0] if row else "SGD"


def _expense_total(conn: sqlite3.Connection, month: str) -> float:
    s, e = month_bounds(month)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_base), 0.0) FROM transactions "
        "WHERE transaction_date BETWEEN ? AND ? "
        "  AND category NOT IN ('Income & Refund')",
        (s, e),
    ).fetchone()
    return float(row[0])


def _category_map(conn: sqlite3.Connection, month: str) -> dict[str, float]:
    s, e = month_bounds(month)
    return {
        r[0]: float(r[1])
        for r in conn.execute(
            "SELECT category, SUM(amount_base) FROM transactions "
            "WHERE transaction_date BETWEEN ? AND ? "
            "  AND category NOT IN ('Income & Refund') "
            "GROUP BY category",
            (s, e),
        ).fetchall()
    }


def _pct_change(now: float, ref: float | None) -> float | None:
    if not ref:
        return None
    return 100 * (now - ref) / ref


def aggregate_month(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    currency = get_base_currency(conn)

    start, end = month_bounds(month)
    today = date.today().isoformat()
    end_effective = min(end, today)
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end_effective)
    days_elapsed = (end_d - start_d).days + 1

    rows = conn.execute(
        "SELECT id, transaction_date, merchant_raw, merchant_display, "
        "       merchant_normalized, amount_base, category, tags, notes "
        "FROM transactions "
        "WHERE transaction_date BETWEEN ? AND ? "
        "ORDER BY transaction_date, id",
        (start, end_effective),
    ).fetchall()

    expenses = [r for r in rows if r["category"] not in INCOME_CATEGORIES]
    incomes = [r for r in rows if r["category"] in INCOME_CATEGORIES]

    total_spent = sum(r["amount_base"] for r in expenses)
    total_income = sum(r["amount_base"] for r in incomes)
    avg = total_spent / len(expenses) if expenses else 0.0

    # --- Trend: this month vs last month vs 3-month average ---------------
    prev_month_str = previous_month(month)
    prev_total = _expense_total(conn, prev_month_str)
    prior3 = _prior_months(month, 3)
    prior3_totals = [_expense_total(conn, m) for m in prior3]
    avg3_total = sum(prior3_totals) / 3 if prior3_totals else 0.0

    prev_cat_map = _category_map(conn, prev_month_str)
    prior3_cat_maps = [_category_map(conn, m) for m in prior3]

    def _avg3_cat(cat: str) -> float:
        return sum(m.get(cat, 0.0) for m in prior3_cat_maps) / 3

    # --- Per-category breakdown (amount, share, count, avg ticket, deltas) -
    cat_amounts: dict[str, float] = defaultdict(float)
    cat_counts: dict[str, int] = defaultdict(int)
    for r in expenses:
        cat_amounts[r["category"]] += r["amount_base"]
        cat_counts[r["category"]] += 1
    cat_breakdown = sorted(
        (
            {
                "category": c,
                "amount": cat_amounts[c],
                "count": cat_counts[c],
                "pct": 100 * cat_amounts[c] / total_spent if total_spent else 0,
                "avg_ticket": cat_amounts[c] / cat_counts[c] if cat_counts[c] else 0,
                "delta_prev_pct": _pct_change(cat_amounts[c], prev_cat_map.get(c)),
                "delta_avg3_pct": _pct_change(cat_amounts[c], _avg3_cat(c)),
            }
            for c in cat_amounts
        ),
        key=lambda x: -x["amount"],
    )

    # --- Tag-level: coffee, dining-out, eat-out vs cook-in -----------------
    tag_amounts: dict[str, float] = defaultdict(float)
    tag_counts: dict[str, int] = defaultdict(int)
    for r in expenses:
        for t in (json.loads(r["tags"]) if r["tags"] else []):
            tag_amounts[t] += r["amount_base"]
            tag_counts[t] += 1
    tag_breakdown = sorted(
        ({"tag": t, "amount": a, "count": tag_counts[t]} for t, a in tag_amounts.items()),
        key=lambda x: -x["amount"],
    )
    eating_out = cat_amounts.get(EATING_OUT_CATEGORY, 0.0)
    groceries = cat_amounts.get(GROCERIES_CATEGORY, 0.0)
    eat_cook_denom = eating_out + groceries
    eat_out_share = 100 * eating_out / eat_cook_denom if eat_cook_denom else None

    # --- Merchants: by spend, by frequency, recurring (>=3) ----------------
    m_amounts: dict[str, float] = defaultdict(float)
    m_counts: dict[str, int] = defaultdict(int)
    for r in expenses:
        m = r["merchant_display"] or r["merchant_raw"]
        m_amounts[m] += r["amount_base"]
        m_counts[m] += 1
    merchants = [
        {"merchant": m, "amount": m_amounts[m], "count": m_counts[m]}
        for m in m_amounts
    ]
    top_merchants = sorted(merchants, key=lambda x: -x["amount"])[:5]
    top_by_freq = sorted(merchants, key=lambda x: (-x["count"], -x["amount"]))[:5]
    recurring_merchants = sorted(
        (m for m in merchants if m["count"] >= 3), key=lambda x: -x["count"]
    )

    # --- Time rhythm: weekday/weekend, weekly buckets, daily curve ---------
    weekday_amt = weekend_amt = 0.0
    weekday_cnt = weekend_cnt = 0
    weekly: dict[int, float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    for r in expenses:
        d = date.fromisoformat(r["transaction_date"])
        if d.weekday() >= 5:
            weekend_amt += r["amount_base"]; weekend_cnt += 1
        else:
            weekday_amt += r["amount_base"]; weekday_cnt += 1
        weekly[(d - start_d).days // 7] += r["amount_base"]
        daily[r["transaction_date"]] += r["amount_base"]
    daily_curve = sorted(daily.items())
    weekly_curve = []
    for wk in sorted(weekly):
        wk_start = start_d.fromordinal(start_d.toordinal() + wk * 7)
        wk_end_ord = min(wk_start.toordinal() + 6, end_d.toordinal())
        wk_end = date.fromordinal(wk_end_ord)
        weekly_curve.append(
            {"label": f"{wk_start.month}/{wk_start.day}–{wk_end.month}/{wk_end.day}",
             "amount": weekly[wk]}
        )
    busiest_day = max(daily_curve, key=lambda x: x[1]) if daily_curve else None

    # --- Concentration & anomalies ----------------------------------------
    by_amount = sorted(expenses, key=lambda r: -r["amount_base"])
    top5_amount = sum(r["amount_base"] for r in by_amount[:5])
    top5_share = 100 * top5_amount / total_spent if total_spent else 0
    outliers = [
        {
            "date": r["transaction_date"],
            "merchant": r["merchant_display"] or r["merchant_raw"],
            "amount": r["amount_base"],
            "category": r["category"],
            "count_this_month": m_counts[r["merchant_display"] or r["merchant_raw"]],
            "note": r["notes"] or "",
        }
        for r in by_amount
        if avg and r["amount_base"] >= OUTLIER_FACTOR * avg
    ]

    top_single_out = [
        {
            "date": r["transaction_date"],
            "merchant": r["merchant_display"] or r["merchant_raw"],
            "amount": r["amount_base"],
            "count_this_month": m_counts[r["merchant_display"] or r["merchant_raw"]],
            "note": r["notes"] or "",
        }
        for r in by_amount[:5]
    ]

    # --- New merchants (first appearance in DB) ---------------------------
    seen_before = {
        r["merchant_normalized"]
        for r in conn.execute(
            "SELECT DISTINCT merchant_normalized FROM transactions "
            "WHERE transaction_date < ?",
            (start,),
        ).fetchall()
    }
    this_month_norm = {
        r["merchant_normalized"]: (r["merchant_display"] or r["merchant_raw"])
        for r in rows
    }
    new_merchants = sorted(
        display for norm, display in this_month_norm.items() if norm not in seen_before
    )

    return {
        "month": month,
        "currency": currency,
        "start": start,
        "end": end_effective,
        "days_elapsed": days_elapsed,
        "n_transactions": len(rows),
        "n_expense_txns": len(expenses),
        "n_income_txns": len(incomes),
        "total_spent": total_spent,
        "total_income": total_income,
        "net": total_income - total_spent,
        "avg_expense": avg,
        "active_days": len(daily),
        "no_spend_days": max(days_elapsed - len(daily), 0),
        # trend
        "prev_month": prev_month_str,
        "prev_total": prev_total,
        "delta_prev_pct": _pct_change(total_spent, prev_total),
        "avg3_total": avg3_total,
        "delta_avg3_pct": _pct_change(total_spent, avg3_total),
        # structure
        "category_breakdown": cat_breakdown,
        "tag_breakdown": tag_breakdown,
        "eating_out_amount": eating_out,
        "groceries_amount": groceries,
        "eat_out_share_pct": eat_out_share,
        "coffee_amount": tag_amounts.get("coffee", 0.0),
        "coffee_count": tag_counts.get("coffee", 0),
        "dining_out_count": tag_counts.get("dining out", 0),
        "top_merchants": top_merchants,
        "top_by_freq": top_by_freq,
        "recurring_merchants": recurring_merchants,
        # rhythm
        "weekday_amount": weekday_amt,
        "weekend_amount": weekend_amt,
        "weekday_count": weekday_cnt,
        "weekend_count": weekend_cnt,
        "weekly_curve": weekly_curve,
        "daily_curve": daily_curve,
        "busiest_day": busiest_day,
        # anomalies
        "top5_share_pct": top5_share,
        "outliers": outliers,
        "top_single": top_single_out,
        "new_merchants": new_merchants,
        "transactions": [
            {
                "date": r["transaction_date"],
                "merchant": r["merchant_display"] or r["merchant_raw"],
                "amount": round(r["amount_base"], 2),
                "category": r["category"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "notes": r["notes"] or "",
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Layer 2 — LLM data-led read
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
你是帮用户做月度消费复盘的分析助手。这份报告以**数据分析为主**：你拿到的是已经算
好的量化指标（分类、趋势、tag、时间节奏、异常），你的任务是把这些数字读成人能看懂
的"发现"，notes 只在能解释某个数字时作为证据引用。

立场（比文采重要）：
1. **每条发现都必须钉在一个具体数字上**（金额 / 占比 / 笔数 / 环比 / vs 近3月均值 /
   频次）。没有数字支撑的感想不要写。
2. **区分一次性和结构性**。一笔大额如果是 one-off（还人情、买大件、送礼），要标出
   来，别让它把"这个月花得多"的结论带歪——必要时给出"剔掉这笔后"的口径。
2b. **警惕薄基线**。如果对比基线（上月 / 近 3 月）的金额很小或笔数很少（账户刚启用、
   数据在爬坡期），不要把几百 % 的涨幅当成行为变化——直接说明"基线太薄、对比不可
   靠"，必要时改用绝对值或占比来描述。
3. **不做 KOL 式总结**。不要"本月主题是…""消费画像是…"这种中心句，不要三字四字
   口号式标签。
4. **诚实承认数据看不到的**：没有房租 → 可能住在别人那儿；没有收入入账 → 可能在用
   储蓄 / 收入走数据外的渠道；没有医疗健身 ≠ 不健康。消费数据只是生活的一小片切面。
5. 不要给"下个月可以…"式建议。你是来读数的，不是来教用户过日子的。

输出（调用 generate_insights tool）：
- findings：3-6 条数据发现，每条钉一个数字 +（若 notes 能解释）一句上下文。
- category_read：钱主要去哪了、跟上月 / 近3月比的结构变化，2-4 句，带数字。
- habits：量化的重复行为（≥3 次或有明显规律的），逐条，每条带次数 / 金额。
- anomalies：点名一次性大额 / 离群笔，说明是 one-off 还是结构性，避免误读。
- blind_spots：老实说从消费数据看不出来的事。
- life_texture：**只有一小段（≤120 字）**。从 notes 里捞一点生活质感 / 情绪 / 有意思
  的细节，轻轻带过，引用原话用「」。这是整份报告唯一允许"有点温度"的地方，别写长；
  如果 notes 很少没什么可写，就写一两句或留空。

语气：分析口吻、简洁、用「你」称呼或隐去主语，不要翻译腔。

—— 以下示例均为**虚构**，只示范写法和颗粒度，不要照搬其中任何内容 ——

findings 好例：
✅「Groceries S$253、38 笔，均单 S$6.7，比近 3 月均值高 20%，是这个月占比最大的结构性
   开销——在家做饭的频率明显上来了。」
✅「打车 6 次共 S$42，全部落在周五、周六晚上；工作日 0 次。是个只在周末出现的固定开销。」
findings 坏例：
❌「这个月你过得挺充实。」（没数字）
❌「消费画像：精致穷 + 吃货。」（KOL 口号）

anomalies 好例：
✅「最大一笔 S$120 是给同事凑的生日礼物（notes：『帮 team 凑份子』），一次性，不构成
   购物趋势；剔掉后 Shopping 其实低于上月。」

blind_spots 好例：
✅「整月没有房租、没有任何收入入账。消费数据看不出你怎么住、和谁住、收入从哪来——
   这些都在数据之外。」

life_texture 好例（短、轻、引用原话）：
✅「读 notes 时最有意思的是你跟面包较劲：同一家踩雷好几次（『太干』『太薄』），转头
   在别处写『这个就对了』。一个执念很明确的人。」

所有内容必须基于给到的数字和真实 notes，不要编造、不要美化、不要装全知。
"""


_INSIGHTS_TOOL = {
    "name": "generate_insights",
    "description": "把已算好的月度指标读成数据驱动的发现；notes 仅作证据。",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-6 条数据发现，每条钉一个具体数字，可附 notes 上下文。",
            },
            "category_read": {
                "type": "string",
                "description": "钱主要去哪了 + 跟上月/近3月的结构变化，2-4 句，带数字。",
            },
            "habits": {
                "type": "array",
                "items": {"type": "string"},
                "description": "量化的重复行为，每条带次数/金额。没有就给空数组。",
            },
            "anomalies": {
                "type": "string",
                "description": "点名一次性大额/离群笔，说明 one-off 还是结构性。没有就说没有明显离群。",
            },
            "blind_spots": {
                "type": "string",
                "description": "从消费数据看不出来的事（房租/收入/健康等缺位）。",
            },
            "life_texture": {
                "type": "string",
                "description": "≤120 字，一小段生活质感，引用原话。notes 少就一两句或留空。",
            },
        },
        "required": [
            "findings",
            "category_read",
            "habits",
            "anomalies",
            "blind_spots",
            "life_texture",
        ],
    },
}


def _insights_payload(agg: dict[str, Any]) -> dict[str, Any]:
    notes_txns = [t for t in agg["transactions"] if t["notes"]]
    cur = agg["currency"]
    return {
        "month": agg["month"],
        "currency": cur,
        "period": f"{agg['start']} → {agg['end']}",
        "kpis": {
            "total_spent": round(agg["total_spent"], 2),
            "prev_month": agg["prev_month"],
            "prev_total": round(agg["prev_total"], 2),
            "delta_prev_pct": round(agg["delta_prev_pct"], 1) if agg["delta_prev_pct"] is not None else None,
            "avg3_total": round(agg["avg3_total"], 2),
            "delta_avg3_pct": round(agg["delta_avg3_pct"], 1) if agg["delta_avg3_pct"] is not None else None,
            "n_expense_txns": agg["n_expense_txns"],
            "avg_expense": round(agg["avg_expense"], 2),
            "active_days": agg["active_days"],
            "no_spend_days": agg["no_spend_days"],
            "income": round(agg["total_income"], 2),
        },
        "category_breakdown": [
            {
                "category": c["category"],
                "amount": round(c["amount"], 2),
                "pct": round(c["pct"], 1),
                "count": c["count"],
                "avg_ticket": round(c["avg_ticket"], 2),
                "delta_prev_pct": round(c["delta_prev_pct"], 1) if c["delta_prev_pct"] is not None else None,
                "delta_avg3_pct": round(c["delta_avg3_pct"], 1) if c["delta_avg3_pct"] is not None else None,
            }
            for c in agg["category_breakdown"]
        ],
        "tags": [{"tag": t["tag"], "amount": round(t["amount"], 2), "count": t["count"]} for t in agg["tag_breakdown"]],
        "eating_out_vs_groceries": {
            "eating_out": round(agg["eating_out_amount"], 2),
            "groceries": round(agg["groceries_amount"], 2),
            "eat_out_share_pct": round(agg["eat_out_share_pct"], 1) if agg["eat_out_share_pct"] is not None else None,
            "coffee_amount": round(agg["coffee_amount"], 2),
            "coffee_count": agg["coffee_count"],
            "dining_out_count": agg["dining_out_count"],
        },
        "time_rhythm": {
            "weekday_amount": round(agg["weekday_amount"], 2),
            "weekend_amount": round(agg["weekend_amount"], 2),
            "weekday_count": agg["weekday_count"],
            "weekend_count": agg["weekend_count"],
            "weekly": [{"week": w["label"], "amount": round(w["amount"], 2)} for w in agg["weekly_curve"]],
            "busiest_day": {"date": agg["busiest_day"][0], "amount": round(agg["busiest_day"][1], 2)} if agg["busiest_day"] else None,
        },
        "concentration": {
            "top5_share_pct": round(agg["top5_share_pct"], 1),
            "outliers": [{"date": o["date"], "merchant": o["merchant"], "amount": round(o["amount"], 2), "count_this_month": o["count_this_month"], "note": o["note"]} for o in agg["outliers"]],
        },
        "top_merchants": [{"merchant": m["merchant"], "amount": round(m["amount"], 2), "count": m["count"]} for m in agg["top_merchants"]],
        "recurring_merchants": [{"merchant": m["merchant"], "count": m["count"], "amount": round(m["amount"], 2)} for m in agg["recurring_merchants"]],
        "new_merchants": agg["new_merchants"],
        "transactions_with_notes": notes_txns,
    }


def generate_insights(agg: dict[str, Any]) -> dict[str, Any] | None:
    """Data-led read. Runs whenever there are transactions (notes optional)."""
    if agg["n_transactions"] == 0:
        return None

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    payload = _insights_payload(agg)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        tools=[_INSIGHTS_TOOL],
        tool_choice={"type": "tool", "name": "generate_insights"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"以下是 {agg['month']} 已算好的月度指标：\n\n"
                    f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
                    f"请调用 generate_insights tool，把这些数字读成数据驱动的发现。"
                ),
            }
        ],
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "generate_insights":
            return dict(block.input)
    raise RuntimeError(f"No tool_use block in response: {resp.content}")


# Section labels for the ⑤ read, shared by the exporter (match render_markdown).
_INSIGHT_LABELS = {
    "findings": "发现",
    "category_read": "钱去哪了",
    "habits": "习惯",
    "anomalies": "一次性 vs 结构性",
    "blind_spots": "数据看不到的",
    "life_texture": "一点生活质感",
}


def build_export_doc(agg: dict[str, Any]) -> str:
    """Self-contained prompt+data doc to paste into claude.ai (no API call).

    Reuses the exact API system prompt and the computed payload, then overrides
    the tool-call instruction (chat has no tools) with a plain-Markdown spec, so
    the narrative can be tuned on a flat-fee subscription instead of per-token.
    """
    payload = _insights_payload(agg)
    props = _INSIGHTS_TOOL["input_schema"]["properties"]
    out: list[str] = []
    out.append(f"# {agg['month']} 月报叙事 · 粘进 claude.ai 跑")
    out.append("")
    out.append("> 把下面 `=====` 之间的**全部内容**复制进 claude.ai（Max）对话框发送，")
    out.append("> 它会输出 6 节叙事；满意后把结果粘回月报 Markdown 的「⑤ 解读」即可。")
    out.append("> Layer 1（前四段统计）已在主报告里生成，这里只补 LLM 叙事层。")
    out.append("")
    out.append("=====")
    out.append("")
    out.append(_SYSTEM_PROMPT)
    out.append("")
    out.append("---")
    out.append("")
    out.append("## ⚠️ 输出方式调整（覆盖上文）")
    out.append("")
    out.append(
        "你现在在普通对话里运行，**没有工具可调**。请忽略上文「调用 generate_insights "
        "tool」的说法，改为**直接用 Markdown 输出**下面 6 节（不要开场白、不要解释）："
    )
    out.append("")
    for key, label in _INSIGHT_LABELS.items():
        out.append(f"### {label}")
        out.append(props[key]["description"])
        out.append("")
    out.append("## 本月已算好的数据")
    out.append("")
    out.append("```json")
    out.append(json.dumps(payload, ensure_ascii=False, indent=2))
    out.append("```")
    out.append("")
    out.append("=====")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _delta_str(pct: float | None) -> str:
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "▬")
    return f"{arrow}{sign}{pct:.0f}%"


def render_bars(curve: list, label_w: int = 12, width: int = 32) -> str:
    """curve: list of (label, amount) tuples."""
    if not curve:
        return ""
    max_a = max(a for _, a in curve) or 1
    lines = []
    for label, a in curve:
        bars = "█" * max(1, int(width * a / max_a))
        lines.append(f"{str(label):<{label_w}} {bars} {a:8.2f}")
    return "\n".join(lines)


def render_markdown(agg: dict[str, Any], insights: dict[str, Any] | None) -> str:
    yr, mo = agg["month"].split("-")
    cur = agg["currency"]
    out: list[str] = []

    out.append(f"# {yr} 年 {int(mo)} 月 · 消费复盘（{cur}）")
    out.append("")

    # ① Overview ----------------------------------------------------------
    out.append("## ① 概览")
    out.append("")
    out.append(f"- **总支出**：{cur} {agg['total_spent']:.2f}")
    out.append(
        f"  - 环比 {agg['prev_month']}（{cur} {agg['prev_total']:.2f}）："
        f"{_delta_str(agg['delta_prev_pct'])} ｜ vs 近 3 月均值（{cur} {agg['avg3_total']:.2f}）："
        f"{_delta_str(agg['delta_avg3_pct'])}"
    )
    out.append(
        f"- **笔数** {agg['n_expense_txns']} ｜ **平均单笔** {cur} {agg['avg_expense']:.2f} "
        f"｜ **活跃** {agg['active_days']}/{agg['days_elapsed']} 天（无消费 {agg['no_spend_days']} 天）"
    )
    if agg["total_income"] > 0:
        out.append(f"- **收入** {cur} {agg['total_income']:.2f} ｜ **净流** {cur} {agg['net']:.2f}")
    if agg["delta_avg3_pct"] is not None and agg["delta_avg3_pct"] > 100:
        out.append(
            "- ⚠️ *注：前几个月交易量偏少（账户爬坡期），上面的环比 / 趋势百分比仅供参考。*"
        )
    out.append("")

    # ② Structure ---------------------------------------------------------
    out.append("## ② 结构：钱去哪了")
    out.append("")
    out.append("### 分类（含趋势）")
    out.append("")
    out.append(f"| 分类 | 金额({cur}) | 占比 | 笔数 | 均单 | 环比 | vs近3月 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in agg["category_breakdown"]:
        out.append(
            f"| {c['category']} | {c['amount']:.2f} | {c['pct']:.0f}% | {c['count']} "
            f"| {c['avg_ticket']:.2f} | {_delta_str(c['delta_prev_pct'])} "
            f"| {_delta_str(c['delta_avg3_pct'])} |"
        )
    out.append("")

    out.append("### Tag 视角")
    out.append("")
    if agg["eat_out_share_pct"] is not None:
        out.append(
            f"- **外食 vs 自炊**：外食(Food & Drink) {cur} {agg['eating_out_amount']:.2f} ｜ "
            f"自炊(Groceries) {cur} {agg['groceries_amount']:.2f} → 外食占比 "
            f"**{agg['eat_out_share_pct']:.0f}%**"
        )
    out.append(
        f"- **咖啡** {agg['coffee_count']} 笔 {cur} {agg['coffee_amount']:.2f} ｜ "
        f"**下馆子(dining out)** {agg['dining_out_count']} 次"
    )
    if agg["tag_breakdown"]:
        out.append("")
        out.append(f"| Tag | 金额({cur}) | 笔数 |")
        out.append("|---|---:|---:|")
        for t in agg["tag_breakdown"][:8]:
            out.append(f"| {t['tag']} | {t['amount']:.2f} | {t['count']} |")
    out.append("")

    out.append("### 商家")
    out.append("")
    out.append("**按金额 Top 5 ｜ 按频次 Top 5**")
    out.append("")
    out.append(f"| 商家(金额) | {cur} | 商家(频次) | 次 |")
    out.append("|---|---:|---|---:|")
    n_rows = max(len(agg["top_merchants"]), len(agg["top_by_freq"]))
    for i in range(n_rows):
        a = agg["top_merchants"][i] if i < len(agg["top_merchants"]) else None
        f = agg["top_by_freq"][i] if i < len(agg["top_by_freq"]) else None
        a_name = a["merchant"] if a else ""
        a_amt = f"{a['amount']:.2f}" if a else ""
        f_name = f["merchant"] if f else ""
        f_cnt = str(f["count"]) if f else ""
        out.append(f"| {a_name} | {a_amt} | {f_name} | {f_cnt} |")
    if agg["recurring_merchants"]:
        out.append("")
        rec = "、".join(f"{m['merchant']}×{m['count']}" for m in agg["recurring_merchants"])
        out.append(f"- **高频复购(≥3 次)**：{rec}")
    out.append("")

    # ③ Rhythm ------------------------------------------------------------
    out.append("## ③ 节奏")
    out.append("")
    wd_tot, we_tot = agg["weekday_amount"], agg["weekend_amount"]
    out.append(
        f"- **工作日 vs 周末**：工作日 {cur} {wd_tot:.2f}（{agg['weekday_count']} 笔）"
        f" ｜ 周末 {cur} {we_tot:.2f}（{agg['weekend_count']} 笔）"
    )
    if agg["busiest_day"]:
        out.append(f"- **最高消费日**：{agg['busiest_day'][0]}（{cur} {agg['busiest_day'][1]:.2f}）")
    out.append("")
    if agg["weekly_curve"]:
        out.append("**周趋势**")
        out.append("")
        out.append("```")
        out.append(render_bars([(w["label"], w["amount"]) for w in agg["weekly_curve"]]))
        out.append("```")
        out.append("")
    out.append("**每日支出**")
    out.append("")
    out.append("```")
    out.append(render_bars(agg["daily_curve"], label_w=11))
    out.append("```")
    out.append("")

    # ④ Anomalies ---------------------------------------------------------
    out.append("## ④ 异常与集中度")
    out.append("")
    out.append(f"- **集中度**：金额 Top 5 笔占总支出 **{agg['top5_share_pct']:.0f}%**")
    out.append("")
    out.append("**Top 5 单笔**")
    out.append("")
    for i, t in enumerate(agg["top_single"], 1):
        flags = []
        if t["count_this_month"] == 1:
            flags.append("一次性")
        flag = f" `[{'、'.join(flags)}]`" if flags else ""
        note = f" — *{t['note']}*" if t["note"] else ""
        out.append(f"{i}. **{t['date']}** · {t['merchant']} · {cur} {t['amount']:.2f}{flag}{note}")
    out.append("")
    if agg["outliers"]:
        out.append(f"- **离群大额**（≥ 平均单笔 {OUTLIER_FACTOR:.0f} 倍）：")
        for o in agg["outliers"]:
            tag = "一次性" if o["count_this_month"] == 1 else f"{o['count_this_month']} 次"
            out.append(f"  - {o['date']} · {o['merchant']} · {cur} {o['amount']:.2f}（{tag}）")
    if agg["new_merchants"]:
        shown = agg["new_merchants"][:12]
        extra = len(agg["new_merchants"]) - len(shown)
        line = "、".join(shown) + (f" …（+{extra}）" if extra > 0 else "")
        out.append(f"- **本月新商家**（{len(agg['new_merchants'])} 家）：{line}")
    out.append("")

    # ⑤ LLM read ----------------------------------------------------------
    if insights:
        out.append("---")
        out.append("")
        out.append("## ⑤ 解读")
        out.append("")
        if insights.get("findings"):
            out.append("### 发现")
            out.append("")
            for f in insights["findings"]:
                out.append(f"- {f}")
            out.append("")
        if insights.get("category_read"):
            out.append("### 钱去哪了")
            out.append("")
            out.append(insights["category_read"])
            out.append("")
        if insights.get("habits"):
            out.append("### 习惯")
            out.append("")
            for h in insights["habits"]:
                out.append(f"- {h}")
            out.append("")
        if insights.get("anomalies"):
            out.append("### 一次性 vs 结构性")
            out.append("")
            out.append(insights["anomalies"])
            out.append("")
        if insights.get("blind_spots"):
            out.append("### 数据看不到的")
            out.append("")
            out.append(insights["blind_spots"])
            out.append("")
        if insights.get("life_texture"):
            out.append("### 一点生活质感")
            out.append("")
            out.append(insights["life_texture"])
            out.append("")

    out.append("---")
    out.append("")
    out.append(f"*生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    if insights:
        n = sum(1 for t in agg["transactions"] if t["notes"])
        out.append(f"*解读由 {MODEL} 基于 {agg['n_expense_txns']} 笔支出、{n} 条 notes 撰写*")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def generate_report(
    month: str,
    out_path: Path,
    do_llm: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    if out_path.exists() and not overwrite:
        raise FileExistsError(f"{out_path} exists; use --overwrite to replace.")

    conn = get_connection()
    try:
        agg = aggregate_month(conn, month)
    finally:
        conn.close()

    if agg["n_transactions"] == 0:
        raise RuntimeError(f"No transactions found for {month}")

    insights = generate_insights(agg) if do_llm else None

    md = render_markdown(agg, insights)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    return {
        "path": str(out_path),
        "currency": agg["currency"],
        "n_transactions": agg["n_transactions"],
        "total_spent": agg["total_spent"],
        "n_with_notes": sum(1 for t in agg["transactions"] if t["notes"]),
        "had_llm": insights is not None,
        # Exposed so callers (e.g. --push-notion) can reuse without a 2nd LLM call.
        "agg": agg,
        "insights": insights,
    }
