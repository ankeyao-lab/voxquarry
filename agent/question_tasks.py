"""User-anchored answer requirements and coverage of retained prose."""
from __future__ import annotations

import re

from .contracts import AgentError
from .answer_checks import dual_ranking_size


def explicit_requirements(question: str) -> list[dict[str, str]]:
    """Keep the whole request plus explicit facets, without inventing new asks."""
    value = question.casefold()
    value = re.sub(r"(?:不要|不用|不需要|不讨论|不问|不是问|排除)\s*(?:给我|提供|分析)?\s*(?:优缺点|利弊|正负面|正负向)", "", value)
    requirements = [("Q1", question)]
    def requested(pattern):
        for match in re.finditer(pattern, value):
            clause = re.split(r"[，,。；;！？?\n]", value[:match.start()])[-1]
            if not re.search(r"(?:不要|不用|不需要|不讨论|不问|不是问|排除)\s*(?:给我|提供|分析)?\s*$|(?:not|without|exclude)\s+(?:the\s+)?$", clause):
                return True
        return False
    dual = requested(r"优缺点|利弊|正负面|正负向")
    for task_id, pattern, text in (
        ("Q2", r"(?<![a-z])pros(?![a-z])|优点|优势|正面", "回答用户问到的优点或正面体验"),
        ("Q3", r"(?<![a-z])cons(?![a-z])|缺点|劣势|负面|抱怨|投诉", "回答用户问到的缺点或负面体验"),
        ("Q4", r"场景|什么情况下|什么时候|什么条件|什么操作|when\b", "说明这些体验在什么场景或条件下发生"),
        ("Q5", r"排序|排名|排行|\btop\s*\d|前\s*(?:\d+|十)\s*(?:名|项|条|个|的?\s*(?:pros|cons|优点|缺点))", "给出要求的排序并解释排序依据；不能凑数"),
        ("Q6", r"纠错|纠正|是不是|是否.*正确|误判", "核对受到质疑的具体说法，并说明应如何改正"),
        ("Q7", r"统计|多少(?:条|人)|数量", "给出用户要求的实际统计数量，说明数字对应什么；不能只写个人估计或建议"),
        ("Q8", r"旧机器|旧机(?:器)?比较", "单独回答旧机器的比较，不能混成当前产品的实际体验"),
        ("Q9", r"假设|不确定判断|不确定的说法", "单独回答假设或不确定判断，不能当成已发生的实际问题"),
    ):
        if requested(pattern) or (dual and task_id in {"Q2", "Q3"}) or (task_id == "Q5" and dual_ranking_size(value)):
            requirements.append((task_id, text))
    return [{"task_id": task_id, "question": text} for task_id, text in requirements]


def merge_plan(question: str, suggested: list[dict[str, str]]) -> list[dict[str, str]]:
    result = explicit_requirements(question)
    for index, item in enumerate(suggested, 1):
        if not item["anchor"].strip() or item["anchor"] not in question:
            raise AgentError("llm_contract_invalid", "问题拆分没有对应用户原话", http_status=502)
        result.append({"task_id": f"U{index}", "question": item["question"], "anchor": item["anchor"]})
    return result


def retained_coverage(tasks, review, statements):
    """Use reviewed coverage of retained prose, not the writer's intent tags."""
    kept = {row["statement_id"]: row for row in statements}
    checks = {row["task_id"]: row for row in review.get("task_checks", [])}
    result = []
    for task in tasks:
        task_id = task["task_id"]
        check = checks.get(task_id, {})
        ids = check.get("statement_ids", [])
        supported = (isinstance(ids, list) and bool(ids) and all(isinstance(sid, str) for sid in ids)
                     and len(ids) == len(set(ids)) and all(sid in kept for sid in ids))
        answered = check.get("status") == "answered" and supported
        result.append({**task, "status": "answered" if answered else "unanswered",
                       "statement_ids": ids if answered else [],
                       "reason": "" if answered else check.get("reason") or "这部分尚未完成回答。"})
    return result
