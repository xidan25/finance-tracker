"""Push a monthly report to the Notion Monthly Reports database.

Renders the same data as `monthly_report.render_markdown` but as Notion
blocks (heading_2/3, paragraph, quote, bulleted_list_item, code, table).

Architecturally independent from prompt content: prompt changes affect the
text inside blocks, not the block layout.
"""

import os
from datetime import datetime
from typing import Any

from notion_client import Client


# ---------------------------------------------------------------------------
# Block builders (Notion API requires verbose nested dicts)
# ---------------------------------------------------------------------------


def _rt(text: str) -> list[dict]:
    """Build a rich_text list from a plain string, chunked under 2000 chars."""
    out: list[dict] = []
    if not text:
        return out
    s = str(text)
    while s:
        chunk, s = s[:1900], s[1900:]
        out.append({"type": "text", "text": {"content": chunk}})
    return out


def heading_2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(text)}}


def heading_3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rt(text)}}


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}


def bulleted(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(text)}}


def quote(text: str) -> dict:
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": _rt(text)}}


def code(text: str, language: str = "plain text") -> dict:
    return {"object": "block", "type": "code",
            "code": {"rich_text": _rt(text), "language": language}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def table(rows: list[list[str]], has_column_header: bool = True) -> dict:
    if not rows:
        return paragraph("(empty table)")
    width = max(len(r) for r in rows)
    table_rows = []
    for r in rows:
        cells = [
            _rt(c) for c in (list(r) + [""] * (width - len(r)))
        ]
        table_rows.append(
            {"object": "block", "type": "table_row",
             "table_row": {"cells": cells}}
        )
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": has_column_header,
            "has_row_header": False,
            "children": table_rows,
        },
    }


# ---------------------------------------------------------------------------
# Compose blocks from aggregated data + insights
# ---------------------------------------------------------------------------


def _delta(pct: float | None) -> str:
    if pct is None:
        return "—"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "▬")
    sign = "+" if pct >= 0 else ""
    return f"{arrow}{sign}{pct:.0f}%"


def _bars(curve: list, label_w: int = 11, width: int = 32) -> str:
    if not curve:
        return ""
    max_a = max(a for _, a in curve) or 1
    return "\n".join(
        f"{str(lbl):<{label_w}} {'█' * max(1, int(width * a / max_a))} {a:8.2f}"
        for lbl, a in curve
    )


def build_blocks(agg: dict[str, Any], insights: dict[str, Any] | None) -> list[dict]:
    blocks: list[dict] = []
    cur = agg["currency"]

    # ① Overview --------------------------------------------------------
    blocks.append(heading_2("① 概览"))
    blocks.append(bulleted(f"总支出：{cur} {agg['total_spent']:.2f}"))
    blocks.append(bulleted(
        f"环比 {agg['prev_month']}（{cur} {agg['prev_total']:.2f}）：{_delta(agg['delta_prev_pct'])}"
        f" ｜ vs 近 3 月均值（{cur} {agg['avg3_total']:.2f}）：{_delta(agg['delta_avg3_pct'])}"
    ))
    blocks.append(bulleted(
        f"笔数 {agg['n_expense_txns']} ｜ 平均单笔 {cur} {agg['avg_expense']:.2f}"
        f" ｜ 活跃 {agg['active_days']}/{agg['days_elapsed']} 天（无消费 {agg['no_spend_days']} 天）"
    ))
    if agg["total_income"] > 0:
        blocks.append(bulleted(f"收入 {cur} {agg['total_income']:.2f} ｜ 净流 {cur} {agg['net']:.2f}"))
    if agg["delta_avg3_pct"] is not None and agg["delta_avg3_pct"] > 100:
        blocks.append(paragraph("⚠️ 注：前几个月交易量偏少（账户爬坡期），环比/趋势百分比仅供参考。"))

    # ② Structure -------------------------------------------------------
    blocks.append(heading_2("② 结构：钱去哪了"))
    blocks.append(heading_3("分类（含趋势）"))
    cat_rows = [["分类", f"金额({cur})", "占比", "笔数", "均单", "环比", "vs近3月"]]
    for c in agg["category_breakdown"]:
        cat_rows.append([
            c["category"], f"{c['amount']:.2f}", f"{c['pct']:.0f}%", str(c["count"]),
            f"{c['avg_ticket']:.2f}", _delta(c["delta_prev_pct"]), _delta(c["delta_avg3_pct"]),
        ])
    blocks.append(table(cat_rows))

    blocks.append(heading_3("Tag 视角"))
    if agg["eat_out_share_pct"] is not None:
        blocks.append(bulleted(
            f"外食(Food & Drink) {cur} {agg['eating_out_amount']:.2f} ｜ 自炊(Groceries) "
            f"{cur} {agg['groceries_amount']:.2f} → 外食占比 {agg['eat_out_share_pct']:.0f}%"
        ))
    blocks.append(bulleted(
        f"咖啡 {agg['coffee_count']} 笔 {cur} {agg['coffee_amount']:.2f} ｜ "
        f"下馆子(dining out) {agg['dining_out_count']} 次"
    ))
    if agg["tag_breakdown"]:
        tag_rows = [["Tag", f"金额({cur})", "笔数"]]
        for t in agg["tag_breakdown"][:8]:
            tag_rows.append([t["tag"], f"{t['amount']:.2f}", str(t["count"])])
        blocks.append(table(tag_rows))

    blocks.append(heading_3("商家（按金额 / 按频次）"))
    m_rows = [["商家(金额)", cur, "商家(频次)", "次"]]
    for i in range(max(len(agg["top_merchants"]), len(agg["top_by_freq"]))):
        a = agg["top_merchants"][i] if i < len(agg["top_merchants"]) else None
        f = agg["top_by_freq"][i] if i < len(agg["top_by_freq"]) else None
        m_rows.append([
            a["merchant"] if a else "", f"{a['amount']:.2f}" if a else "",
            f["merchant"] if f else "", str(f["count"]) if f else "",
        ])
    blocks.append(table(m_rows))
    if agg["recurring_merchants"]:
        rec = "、".join(f"{m['merchant']}×{m['count']}" for m in agg["recurring_merchants"])
        blocks.append(bulleted(f"高频复购(≥3 次)：{rec}"))

    # ③ Rhythm ----------------------------------------------------------
    blocks.append(heading_2("③ 节奏"))
    blocks.append(bulleted(
        f"工作日 {cur} {agg['weekday_amount']:.2f}（{agg['weekday_count']} 笔） ｜ "
        f"周末 {cur} {agg['weekend_amount']:.2f}（{agg['weekend_count']} 笔）"
    ))
    if agg["busiest_day"]:
        blocks.append(bulleted(f"最高消费日：{agg['busiest_day'][0]}（{cur} {agg['busiest_day'][1]:.2f}）"))
    if agg["weekly_curve"]:
        blocks.append(heading_3("周趋势"))
        blocks.append(code(_bars([(w["label"], w["amount"]) for w in agg["weekly_curve"]], label_w=12)))
    if agg["daily_curve"]:
        blocks.append(heading_3("每日支出"))
        blocks.append(code(_bars(agg["daily_curve"])))

    # ④ Anomalies -------------------------------------------------------
    blocks.append(heading_2("④ 异常与集中度"))
    blocks.append(bulleted(f"集中度：金额 Top 5 笔占总支出 {agg['top5_share_pct']:.0f}%"))
    blocks.append(heading_3("Top 5 单笔"))
    for i, t in enumerate(agg["top_single"], 1):
        flag = " [一次性]" if t["count_this_month"] == 1 else ""
        line = f"{i}. {t['date']} · {t['merchant']} · {cur} {t['amount']:.2f}{flag}"
        if t["note"]:
            line += f" — {t['note']}"
        blocks.append(bulleted(line))
    if agg["outliers"]:
        blocks.append(bulleted(f"离群大额（≥ 平均单笔 3 倍）：{len(agg['outliers'])} 笔"))
        for o in agg["outliers"]:
            tag = "一次性" if o["count_this_month"] == 1 else f"{o['count_this_month']} 次"
            blocks.append(bulleted(f"  {o['date']} · {o['merchant']} · {cur} {o['amount']:.2f}（{tag}）"))
    if agg["new_merchants"]:
        shown = agg["new_merchants"][:12]
        extra = len(agg["new_merchants"]) - len(shown)
        line = "、".join(shown) + (f" …（+{extra}）" if extra > 0 else "")
        blocks.append(bulleted(f"本月新商家（{len(agg['new_merchants'])} 家）：{line}"))

    # ⑤ LLM read --------------------------------------------------------
    if insights:
        blocks.append(divider())
        blocks.append(heading_2("⑤ 解读"))
        if insights.get("findings"):
            blocks.append(heading_3("发现"))
            for f in insights["findings"]:
                blocks.append(bulleted(f))
        if insights.get("category_read"):
            blocks.append(heading_3("钱去哪了"))
            blocks.append(paragraph(insights["category_read"]))
        if insights.get("habits"):
            blocks.append(heading_3("习惯"))
            for h in insights["habits"]:
                blocks.append(bulleted(h))
        if insights.get("anomalies"):
            blocks.append(heading_3("一次性 vs 结构性"))
            blocks.append(paragraph(insights["anomalies"]))
        if insights.get("blind_spots"):
            blocks.append(heading_3("数据看不到的"))
            blocks.append(paragraph(insights["blind_spots"]))
        if insights.get("life_texture"):
            blocks.append(heading_3("一点生活质感"))
            blocks.append(paragraph(insights["life_texture"]))

    blocks.append(divider())
    blocks.append(paragraph(f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}"))
    return blocks


# ---------------------------------------------------------------------------
# Page property builder
# ---------------------------------------------------------------------------


def build_properties(agg: dict[str, Any], insights: dict[str, Any] | None) -> dict:
    props = {
        "Name":              {"title": _rt(f"{agg['month']} ({agg['currency']})")},
        "Month":             {"date": {"start": agg["start"]}},
        "Total Spent":       {"number": round(agg["total_spent"], 2)},
        "Total Income":      {"number": round(agg["total_income"], 2)},
        "Transaction Count": {"number": agg["n_transactions"]},
        "Generated At":      {"date": {"start": datetime.now().date().isoformat()}},
    }
    # Repurpose the "Hero Line" column as a one-line summary for the DB list view.
    summary = ""
    if insights:
        summary = insights.get("category_read") or (
            insights["findings"][0] if insights.get("findings") else ""
        )
    if summary:
        props["Hero Line"] = {"rich_text": _rt(summary[:200])}
    return props


# ---------------------------------------------------------------------------
# Push / find / update
# ---------------------------------------------------------------------------


def find_existing_page(notion: Client, ds_id: str, month: str) -> str | None:
    """Look up a Monthly Reports page by Name == month. Returns page_id or None."""
    result = notion.data_sources.query(
        data_source_id=ds_id,
        filter={"property": "Name", "title": {"equals": month}},
    )
    pages = result.get("results", [])
    return pages[0]["id"] if pages else None


def push_report(
    agg: dict[str, Any],
    insights: dict[str, Any] | None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create (or replace) a Monthly Reports page for agg['month']."""
    token = os.environ["NOTION_TOKEN"]
    ds_id = os.environ["NOTION_MONTHLY_REPORTS_DATA_SOURCE_ID"]
    notion = Client(auth=token)

    properties = build_properties(agg, insights)
    blocks = build_blocks(agg, insights)

    existing_id = find_existing_page(notion, ds_id, f"{agg['month']} ({agg['currency']})")
    if existing_id and not overwrite:
        return {
            "action": "skipped",
            "page_id": existing_id,
            "reason": "page exists; use --overwrite to replace",
        }

    if existing_id:
        # Archive old, create new (cleanest way to replace content)
        notion.pages.update(page_id=existing_id, archived=True)

    page = notion.pages.create(
        parent={"data_source_id": ds_id},
        properties=properties,
    )
    page_id = page["id"]

    # Notion API caps children per request at 100. Chunk if needed.
    for i in range(0, len(blocks), 100):
        notion.blocks.children.append(
            block_id=page_id, children=blocks[i:i + 100]
        )

    return {
        "action": "replaced" if existing_id else "created",
        "page_id": page_id,
        "n_blocks": len(blocks),
        "url": page.get("url"),
    }
