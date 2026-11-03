"""Derived, model-facing meanings for known Reporting JSON shapes.

The immutable source JSON remains the citation and verification authority. This
view preserves values and field names; it clarifies known fields and replaces
ambiguous column labels without inferring the meaning of arbitrary positive or
negative fields. Definitions follow the Reporting producers, not label wording.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def exact_source_quote(quote: str, original: str) -> str | None:
    """Return actual source text; tolerate only equal-length typography quotes.

    A provider may smarten ASCII quotation marks without changing any words.
    Recover that exact original span rather than storing its reformatted quote.
    No word, whitespace, dash, case, omission or paraphrase matching is allowed.
    """
    if not isinstance(quote, str) or not quote.strip():
        return None
    if quote in original:
        return quote
    typography = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
    start = original.translate(typography).find(quote.translate(typography))
    return original[start:start + len(quote)] if start >= 0 else None


_FACTOR_TABLES = frozenset({
    "attribute_performance", "pain_points", "use_cases", "explicit_needs",
    "user_traits", "smart_opportunities", "kano_candidates", "product_opportunities",
})
_RATING_TABLES = frozenset({
    "channel_comparison", "brand_comparison", "model_comparison", "time_trend",
})
_FACTOR_FIELDS = {
    "positive_count": "提及该主题且给整机4–5星的评论数",
    "positive_share": "提及该主题的有效评分评论中，整机4–5星占比",
    "negative_count": "提及该主题且给整机1–2星的评论数",
    "negative_share": "提及该主题的有效评分评论中，整机1–2星占比",
    "net_rating_polarity": "整机4–5星数减1–2星数，再除以有效评分提及数",
}
_STAR_FIELDS = {
    "high_star_entities": "这些评论中给整机4–5星的评论数",
    "high_star_rate": "这些有效评分评论中，整机4–5星占比",
    "low_star_entities": "这些评论中给整机1–2星的评论数",
    "low_star_rate": "这些有效评分评论中，整机1–2星占比",
    "positive_rating_count": "这些记录中给整机4–5星的记录数",
    "negative_rating_count": "这些记录中给整机1–2星的记录数",
    "positive_rating_rate": "这些有效评分评论中，整机4–5星占比",
    "negative_rating_rate": "这些有效评分评论中，整机1–2星占比",
}
_RATE_FIELDS = {
    "positive_rate": "这些有效评分评论中，整机4–5星占比",
    "negative_rate": "这些有效评分评论中，整机1–2星占比",
}
_EXPLICIT_FIELDS = {
    "explicit_positive_entities": "同句规则识别为该主题正向表达的评论数",
    "explicit_positive_rate": "同句规则识别为该主题正向表达的评论数 / review_entities",
    "explicit_negative_entities": "同句规则识别为该主题负向表达的评论数",
    "explicit_negative_rate": "同句规则识别为该主题负向表达的评论数 / review_entities",
}
_RATING_NOTE = (
    "这里统计的是这些评论给整机的星级，不是对所提功能的态度。"
    "整机高星评论也可能抱怨该功能；不能据此说功能获赞、无人抱怨或值得优先改进。"
)
_EXPLICIT_NOTE = (
    "这些是程序按同句词法识别的主题表达，独立于整机星级，正负集合可以重叠。"
    "规则可能误读否定、比较或猜测，具体体验与纠错须核对完整原文。"
)


def _numeric_fields(value: Mapping[str, Any], definitions: Mapping[str, str]) -> dict[str, str]:
    return {key: text for key, text in definitions.items() if key in value and (
        value[key] is None or isinstance(value[key], (int, float)) and not isinstance(value[key], bool)
    )}


def _factor_row(value: Mapping[str, Any]) -> bool:
    # analysis._atom_rows, _kano_candidates and _opportunity_rows.
    return ("factor_id" in value or "opportunity_id" in value) and (
        "mention_count" in value or "rated_mentions" in value
    )


def _rating_row(value: Mapping[str, Any]) -> bool:
    # analysis._group_rows and brand_persona[_semantics] rating summaries.
    return bool({"rating_n", "valid_rating_entities", "valid_rating_count"} & value.keys()) or (
        {"sample_size", "average_rating", "positive_rate", "negative_rate"} <= value.keys()
    )


def _explicit_row(value: Mapping[str, Any]) -> bool:
    return ("aspect_id" in value or "item_id" in value) and "review_entities" in value


def _meanings(value: Mapping[str, Any], table_id: str | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    rating_fields = _numeric_fields(value, _STAR_FIELDS)
    factor = _factor_row(value) or table_id in _FACTOR_TABLES
    if factor:
        rating_fields.update(_numeric_fields(value, _FACTOR_FIELDS))
        mentions = _numeric_fields(value, {
            "mention_count": "评论命中该主题词典规则的数量",
            "mention_rate": "评论命中该主题词典规则的数量 / 全部文字评论数",
        })
        if mentions:
            result["rule_mentions"] = {
                "fields": mentions,
                "meaning": "词典命中不自动证明某种体验发生、表达赞扬或投诉；具体意思须读原文。",
            }
    if _rating_row(value) or table_id in _RATING_TABLES:
        rating_fields.update(_numeric_fields(value, _RATE_FIELDS))
        if "average_rating" in value:
            rating_fields["average_rating"] = "这些有效评分评论的整机平均星级"
    # Same names have a different meaning in the experience module. A histogram
    # is specific to brand_persona._rating_summary; do not use a name heuristic.
    if "histogram" in value and "valid_rating_entities" in value:
        rating_fields.update({
            key: text for key, text in {
                "positive_entities": "给整机4–5星的评论数",
                "negative_entities": "给整机1–2星的评论数",
            }.items() if key in value
        })
    if rating_fields:
        result["whole_product_rating"] = {"fields": rating_fields, "meaning": _RATING_NOTE}
        for denominator in ("rated_mentions", "rating_n", "valid_rating_entities", "valid_rating_count"):
            if denominator in value:
                result["whole_product_rating"]["denominator_field"] = denominator
                break
        if factor and "classification" in value:
            result["report_classification"] = {
                "fields": {"classification": "程序根据提及率和整机星级生成的候选分类"},
                "meaning": "不是已验证的功能态度或Kano结论，应结合原文独立判断。",
            }
    if _explicit_row(value):
        explicit = _numeric_fields(value, _EXPLICIT_FIELDS)
        # brand_persona_semantics._experience_module uses these aliases.
        if "explicit_positive_rate" in value and "positive_entities" in value:
            explicit["positive_entities"] = _EXPLICIT_FIELDS["explicit_positive_entities"]
        if "explicit_negative_rate" in value and "negative_entities" in value:
            explicit["negative_entities"] = _EXPLICIT_FIELDS["explicit_negative_entities"]
        if explicit:
            result["rule_aspect_expression"] = {
                "fields": explicit, "denominator_field": "review_entities", "meaning": _EXPLICIT_NOTE,
            }
    if {"intersection_count", "group_match_counts"} <= value.keys():
        result["keyword_matching"] = {
            "fields": {
                "intersection_count": "满足基础筛选且同时命中各词组的记录数；无词组时为基础筛选结果数",
                "group_match_counts": "相同基础筛选内，各词组分别命中的记录数",
            },
            "meaning": "这是文字匹配，不是体验、投诉或赞扬的分类人数，也不证明话题之间存在因果关系。",
        }
    if {"term", "document_count", "ngram"} <= value.keys():
        result["keyword_matching"] = {
            "fields": {
                key: text for key, text in {
                    "document_count": "包含该词或词组的文字评论数",
                    "document_rate": "包含该词或词组的文字评论数 / 全部文字评论数",
                }.items() if key in value
            },
            "meaning": "词频不是投诉或赞扬人数；同一词可能用于比较、否定或不同对象。",
        }
    return result


def _column_meanings(value: Mapping[str, Any], table_id: str | None) -> dict[str, str]:
    fields = dict(_STAR_FIELDS)
    if table_id in _FACTOR_TABLES:
        fields.update(_FACTOR_FIELDS)
    if table_id in _RATING_TABLES:
        fields.update(_RATE_FIELDS)
    # Also supports table shapes obtained through a JSON section with rows and
    # columns but no table ID, using only definitions recognized in actual rows.
    for row in value.get("rows", []) if isinstance(value.get("rows"), list) else []:
        if isinstance(row, Mapping):
            for group in _meanings(row, table_id).values():
                fields.update(group["fields"])
    return fields


def _view(value: Any, table_id: str | None = None, definition_sink=None, path: str = "") -> Any:
    if isinstance(value, list):
        return [_view(item, table_id, definition_sink, path + "/" + str(index)) for index, item in enumerate(value)]
    if not isinstance(value, Mapping):
        return deepcopy(value)
    current_table = value.get("table_id") if isinstance(value.get("table_id"), str) else table_id
    # A table's definitions apply to its rows and columns, not arbitrary nested
    # metadata that might happen to use an identically named field.
    result = {
        key: deepcopy(item) if key == "_evidence_semantics" else _view(
            item, current_table if key in {"rows", "columns"} else None, definition_sink,
            path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        for key, item in value.items()
    }
    meanings = _meanings(value, table_id)
    metric_id = value.get("metric_id")
    if isinstance(metric_id, str) and metric_id in _STAR_FIELDS:
        meanings["whole_product_rating"] = {
            "fields": {"value": _STAR_FIELDS[metric_id]}, "source_field": metric_id,
            "meaning": _RATING_NOTE,
        }
        if "denominator" in value:
            meanings["whole_product_rating"]["denominator_field"] = "denominator"
        for label_key in ("name", "label"):
            if label_key in result:
                result[label_key] = _STAR_FIELDS[metric_id]
    # References to intrinsically named star metrics retain their source field.
    field = value.get("field")
    intrinsic = _STAR_FIELDS.get(field) if isinstance(field, str) else None
    if intrinsic and "value" in value:
        meanings["whole_product_rating"] = {
            "fields": {"value": intrinsic}, "source_field": field, "meaning": _RATING_NOTE,
        }
    if meanings:
        # The reserved block is recomputed, making repeated application stable.
        result["_evidence_semantics"] = meanings
        if definition_sink is not None:
            # Publish only definitions derived above from known producer shapes,
            # never a reserved block supplied by source documents themselves.
            for kind, definition in meanings.items():
                definition_sink(kind, deepcopy(definition), path or "/")
    if isinstance(result.get("columns"), list):
        labels = _column_meanings(value, current_table)
        for column in result["columns"]:
            if isinstance(column, dict) and isinstance(column.get("field"), str):
                if column["field"] in labels and "label" in column:
                    column["label"] = labels[column["field"]]
    return result


def model_evidence_view(value: Any, *, definition_sink=None) -> Any:
    """Return an independent JSON view; never modify the source or recompute it.

    Apply only at the model boundary, after reading/validating original sources.
    Source hashes, citation quote checks and user-visible original JSON must
    continue to use the original value. Unknown fields and prose are preserved.
    """
    return _view(value, definition_sink=definition_sink)
