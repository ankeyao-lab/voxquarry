"""Resolve a question to an authorized report or a bounded preparation candidate."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import AgentError

ALIASES = {"walk_behind_mower": ("割草机", "草坪机", "mower"), "leaf_blower": ("吹叶机", "blower"),
           "pressure_washer": ("高压清洗机", "洗车机", "pressure washer"), "chainsaw": ("链锯", "油锯"),
           "string_trimmer": ("打草机",), "hedge_trimmer": ("绿篱机",), "snow_blower": ("扫雪机", "除雪机"),
           "lowes": ("lowe's", "lowe’s", "劳氏"), "homedepot": ("home depot", "家得宝"),
           "North America": ("北美",), "Europe": ("欧洲",), "Australia": ("澳大利亚",)}


def normalized(value: str) -> str:
    return re.sub(r"[\s’'._+/-]+", "", unicodedata.normalize("NFKC", value).casefold())


def mentioned(value: str, question: str) -> bool:
    values = (value, *ALIASES.get(value, ()))
    query = unicodedata.normalize("NFKC", question).casefold()
    return any(normalized(v) and re.search(r"(?<![a-z0-9])" + r"[\s’'._+/-]*".join(re.escape(c) for c in normalized(v)) + r"(?![a-z0-9])", query) for v in values)


def question_dates(question: str) -> dict[str, str]:
    dates = re.findall(r"20\d{2}-\d{2}-\d{2}", question)
    if len(dates) == 2:
        try:
            start, end = (datetime.strptime(value, "%Y-%m-%d").date() for value in dates)
        except ValueError:
            raise AgentError("report_selection_required", "日期无效，请用有效的开始和结束日期。", http_status=409) from None
        if start > end:
            raise AgentError("report_selection_required", "开始日期不能晚于结束日期，请确认时间。", http_status=409)
        return {"start": start.isoformat(), "end": end.isoformat()}
    if dates:
        raise AgentError("report_selection_required", "请给出开始和结束日期，例如2026-01-01至2026-06-30。", http_status=409)
    recent = re.search(r"(?:最近|近|过去)\s*(\d{1,3})\s*天", question)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if recent:
        if int(recent[1]) == 0:
            raise AgentError("report_selection_required", "最近几天需要大于零，请确认时间。", http_status=409)
        return {"start": (today - timedelta(days=max(0, int(recent[1]) - 1))).isoformat(), "end": today.isoformat()}
    if re.search(r"最近|过去|今年|去年|本月|上月|季度|\d月", question):
        raise AgentError("report_selection_required", "请给出具体的开始和结束日期，或最近多少天。", http_status=409)
    year = re.search(r"(20\d{2})年", question)
    if year:
        return {"start": f"{year[1]}-01-01", "end": f"{year[1]}-12-31"}
    return {}


def choose_report(question: str, reports: list[dict[str, Any]], candidates: list[dict[str, Any]],
                  previous_report_id: str | None = None) -> tuple[str, dict[str, Any]]:
    options = reports + candidates
    dims: dict[str, set[str]] = {}
    for key in ("models", "brands", "channels", "category_id", "market_regions"):
        values = set()
        for option in options:
            value = (option.get("scope") or {}).get(key, [])
            values.update([value] if isinstance(value, str) and value else value if isinstance(value, list) else [])
        matched = {value for value in values if mentioned(value, question)}
        if key == "channels":
            brands = {normalized(brand) for option in options for brand in (option.get("scope") or {}).get("brands", [])}
            matched = {value for value in matched if normalized(value) not in brands or re.search(
                re.escape(value) + r"\s*(?:官网|官方|渠道|网站|\.com)|(?:官网|官方|渠道|网站)\s*" + re.escape(value), question, re.I)}
        if matched:
            dims[key] = matched
    dates = question_dates(question)
    unknown_product = bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]{1,8}[- ]?\d{3,}[A-Za-z0-9-]*(?![A-Za-z0-9])", question)) and "models" not in dims
    if unknown_product:
        raise AgentError("report_selection_required", "没有找到这个型号的报告或已有评论，请确认品牌和完整型号。", http_status=409)
    if not dims and previous_report_id and not re.search(r"换个产品|另一款|其他品牌|别的产品", question):
        previous = next((r for r in reports if r["report_id"] == previous_report_id), None)
        if previous and not dates:
            return "report", previous
        if previous:
            dims = {k: set(v if isinstance(v, list) else [v]) for k, v in previous.get("scope", {}).items()
                    if k in ("models", "brands", "channels", "category_id", "market_regions") and v}
    if not dims:
        raise AgentError("report_selection_required", "想研究哪款产品？告诉我品牌和型号，或用加号指定报告。", http_status=409)

    def matches(option: dict[str, Any], check_dates: bool) -> bool:
        scope = option.get("scope") or {}
        for key, values in dims.items():
            actual = scope.get(key, [])
            actual = {actual} if isinstance(actual, str) else set(actual)
            if not values.issubset(actual):
                return False
            if key in {"models", "brands", "channels"} and actual != values:
                return False
        return not check_dates or (scope.get("date_range") or {}) == dates

    matching = [r for r in reports if matches(r, True)]
    # Several versions of the exact same scope are one choice, newest first.
    distinct = {}
    for report in sorted(matching, key=lambda r: r.get("generated_at") or "", reverse=True):
        scope = report.get("scope") or {}
        signature = repr(sorted((k, repr(v)) for k, v in scope.items() if k != "job_ids"))
        distinct.setdefault(signature, report)
    if len(distinct) == 1:
        return "report", next(iter(distinct.values()))
    if not distinct:
        matching = [c for c in candidates if matches(c, False)]
        if "channels" not in dims:
            # Prefer all available channels for one product over its per-channel variants.
            matching = [c for c in matching if not any(c is not other and
                        c["scope"].get("models") == other["scope"].get("models") and
                        c["scope"].get("market_regions") == other["scope"].get("market_regions") and
                        set(c["scope"]["job_ids"]) < set(other["scope"]["job_ids"]) for other in matching)]
        if len(matching) == 1:
            return "generate", {**matching[0], "date_range": dates}
    else:
        matching = list(distinct.values())
    labels = list(dict.fromkeys(str(c.get("title") or "") for c in matching))[:4]
    if labels:
        raise AgentError("report_selection_required", "找到几份相关资料，请指定产品或渠道：" + "；".join(labels) + "。", http_status=409)
    raise AgentError("research_data_unavailable", "没有找到符合这些条件的报告或已有评论。请确认产品、渠道和时间。", http_status=409)
