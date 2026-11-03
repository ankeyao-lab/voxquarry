"""Deterministic rankings of saved report counts, not corrected review semantics."""
from __future__ import annotations
import re
from copy import deepcopy
from typing import Any
from .answer_checks import dual_ranking_size


def report_ranking_requested(question: str) -> int:
    """Only an explicit request for the report's numbers can skip originals."""
    size = dual_ranking_size(question) or 0
    value = question.casefold()
    if not size or not re.search(r"报告|\breport\b", value):
        return 0
    value = re.sub(r"不(?:用|必|要|需要)(?:解释|分析|核对原文|读原文)", "", value)
    if re.search(r"纠错|纠正|重新|原始|原文|场景|为什么|原因|优先|交叉|严重|建议|改进|具体|哪里|解释|分析|体验|\b(?:explain|why|experience|correct)\b", value):
        return 0
    limited = re.search(r"(?:只|仅)(?:需要|需|要)?(?:看|查|列|给|展示|返回|提供)|(?:只要|只需|仅需).*(?:榜单|排名|数字|数量|主题)|\b(?:only|just)\b", value)
    return size if limited else 0


RANKING_EXPERIENCE_BRIEF = (
    "程序双榜只确定报告已有分类的排序，不代表重新逐条确认的好评或抱怨人数。"
    "分别回答每项具体哪里好、哪里差，用相关完整评论核对实际对象和条件；主题名称不能代替评价。"
    "排序按报告已有正面、负面分类评论数分别降序，同数并列，引用原报告表格；具体体验另引原文。"
    "某项没有可支持的具体体验时说明尚未核对，不把主题名猜写成优缺点，不拆分同一问题凑足数量。"
    "原文发现规则误判时直接纠正该例，不声称已重新统计全部评论；用户另指定排序依据时不能冒用报告频次替代。"
)


def report_ranking_context(action: dict[str, Any]) -> dict[str, Any]:
    """A calculation aid that cites the original table, never a new source."""
    return {"rankings": deepcopy(action["rankings"]),
            "source_ids": [row["source_id"] for row in action["citations"]],
            "basis": "报告同句规则已分类的正面/负面评论数；不是星级，也不是新做的语义统计。",
            "instruction": RANKING_EXPERIENCE_BRIEF}


def build_report_ranking(rows: list[dict[str, Any]], size: int, source_ids: list[str]) -> dict[str, Any] | None:
    if not rows or not source_ids or not 1 <= size <= 30:
        return None
    fields = ("explicit_positive_entities", "explicit_negative_entities")
    seen: set[str] = set()
    for row in rows:
        identifier = row.get("aspect_id")
        if not isinstance(identifier, str) or identifier in seen or not isinstance(row.get("label"), str):
            return None
        seen.add(identifier)
        if any(type(row.get(field)) is not int or row[field] < 0 for field in fields):
            return None
    sections, tables = [], []
    for heading, field in zip(("优点", "缺点"), fields):
        ordered = sorted((r for r in rows if r[field] > 0), key=lambda r: (-r[field], r["aspect_id"]))
        table, previous, rank = [], None, 0
        for position, row in enumerate(ordered[:size], 1):
            if row[field] != previous:
                rank = position
            previous = row[field]
            table.append({"rank": rank, "label": row["label"], "count": row[field], "aspect_id": row["aspect_id"]})
        body = [f"### {heading}前 {size}", "| 排名 | 评价主题 | 提到的评论数 |", "| --- | --- | ---: |"]
        body.extend(f"| {r['rank']} | {r['label'].replace('|', '／').replace(chr(10), ' ')} | {r['count']} |" for r in table)
        if len(table) < size:
            body.append(f"报告中有{len(table)}项{heading}记录，已全部列出。")
        tied = []
        if len(ordered) > size and ordered[size][field] == ordered[size - 1][field]:
            tied = [{"aspect_id": r["aspect_id"], "label": r["label"], "count": r[field]}
                    for r in ordered[size:] if r[field] == ordered[size - 1][field]]
            body.append("末位还有同数项目：" + "、".join(r["label"] for r in tied) + "。同数项目没有高低之分。")
        sections.append("\n".join(body))
        tables.append({"title": heading, "rows": table, "field": field, "cutoff_ties": tied})
    content = ("按报告已有的正面、负面分类评论数分别排序，数量相同并列。这些数字沿用报告分类，没有重新核对每条原文。\n\n"
               + "\n\n".join(sections) + "\n\n" + " ".join(f"[{sid}]" for sid in source_ids))
    return {"type": "answer", "status": "answered", "answer": content,
            "citations": [{"source_id": sid} for sid in source_ids], "limitations": [], "rankings": tables}
