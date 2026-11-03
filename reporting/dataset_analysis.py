"""Deterministic, bounded analysis of a frozen shared dataset for private briefs.

This deliberately does not call build_product_insight_bundle with readiness
disabled, publish a report, or modify a shared source/snapshot. Narrative is
authored by the research agent after checking the original review evidence.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import re
import tempfile

from . import source
from .atomic_v2 import run_atomic_scripts
from .config import LOW_SAMPLE_N
from .ope_product_report import OpeIndex, _aspect_rows, _build_dimension_census, category_profile
from .product_insight import REPORT_READINESS_THRESHOLDS, report_readiness_status
from .research_definition import get_research_definition
from .wenquan_datasets import DatasetError


SCHEMA = "wenquan-dataset-analysis/v1"
VERSION = "wenquan-dataset-analysis/1.0.0"
MAX_ANALYSIS_ENTITIES = 10000
MAX_ANALYSIS_BYTES = 64 * 1024**2


def _hash(value):
    return "sha256:" + hashlib.sha256(source.canonical_json(value).encode("utf-8")).hexdigest()


def _read_facts(backend, dataset_id):
    facts, size = [], 0
    # This is a fixed read-only query over the existing hash-verified index.
    # It retains the same canonical entity selection as query_dataset.
    with backend._index(dataset_id) as (db, manifest):
        total = db.execute("SELECT COUNT(*) FROM facts WHERE canonical=1").fetchone()[0]
        if total > MAX_ANALYSIS_ENTITIES:
            raise DatasetError("analysis_capacity_exceeded", "资料超过单次规则分析容量；请按产品或时间缩小分析范围，不会自动抽样", 422)
        for (payload,) in db.execute("SELECT payload FROM facts WHERE canonical=1 ORDER BY seq"):
            size += len(payload.encode("utf-8"))
            if size > MAX_ANALYSIS_BYTES:
                raise DatasetError("analysis_capacity_exceeded", "完整评论超过单次规则分析容量；请缩小范围，不会截断原文", 422)
            facts.append(json.loads(payload))
    return facts, manifest


def _category(facts, manifest):
    categories = {str(fact.get("category_id") or "") for fact in facts}
    if not facts:
        categories = {str(source._job_category_id(item["job"]) or "") for item in manifest.get("inputs", [])}
    if len(categories) != 1 or "" in categories:
        raise DatasetError("analysis_category_ambiguous", "请选择一个明确品类的数据；不会把其他品类的分析规则套到这些评论", 422)
    return categories.pop()


def _sample(facts):
    counts = Counter(fact.get("rating") for fact in facts if type(fact.get("rating")) is int and 1 <= fact["rating"] <= 5)
    rated = sum(counts.values())
    written = sum(bool(fact.get("has_text")) for fact in facts)
    return {"review_entities": len(facts), "written_entities": written, "rating_n": rated,
            "rating_counts": [{"rating": value, "count": counts[value]} for value in range(1, 6)],
            "average_rating": round(sum(value * count for value, count in counts.items()) / rated, 6) if rated else None,
            "positive_rating_rate": (counts[4] + counts[5]) / rated if rated else None,
            "negative_rating_rate": (counts[1] + counts[2]) / rated if rated else None,
            "written_coverage": written / len(facts) if facts else None}


def _evidence_ids(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("evidence_ids") and isinstance(item, list):
                found.update(item)
            else:
                found.update(_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_ids(item))
    return found


def analyze_dataset(backend, dataset_id, definition_id=None):
    if definition_id is not None and (not isinstance(definition_id, str) or not re.fullmatch(r"rdef_[0-9a-f]{64}", definition_id)):
        raise DatasetError("invalid_request", "分析定义标识无效", 422)
    facts, manifest = _read_facts(backend, dataset_id)
    category_id = _category(facts, manifest)
    definition = get_research_definition(category_id)
    if definition_id is not None and definition_id != definition["definition_id"]:
        raise DatasetError("analysis_definition_mismatch", "分析定义与实际品类或当前规则版本不一致，请重新读取定义", 409)
    analysis_id = "ana_" + _hash({"dataset_id": dataset_id, "manifest": manifest["manifest_sha256"],
        "definition_id": definition["definition_id"], "version": VERSION}).split(":")[1]
    cache = backend.storage_root / dataset_id / (analysis_id + '.json')
    if cache.is_file() and not cache.is_symlink():
        try:
            cached = json.loads(cache.read_text())
            digest = cached.pop('analysis_sha256')
            if cached.get('analysis_id') == analysis_id and _hash(cached) == digest:
                return {**cached, 'analysis_sha256': digest}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    sample = _sample(facts)
    standard = report_readiness_status(sample["written_entities"])
    profile = category_profile(category_id)
    outcome = "no_review_text" if not sample["written_entities"] else "no_registered_profile" if profile is None else "ready"
    products = {source.canonical_json({key: fact.get(key) for key in
        ("channel", "source_product_id", "product_id", "brand", "model", "product_url", "market_region")}) for fact in facts}
    if not facts:
        products = {source.canonical_json({"channel": item["job"].get("channel"),
            "source_product_id": item["job"].get("source_product_id"),
            "brand": item["job"].get("brand"), "model": item["job"].get("model"),
            "product_url": item["job"].get("target_url"), "market_region": item["job"].get("market_region"),
            "identity_source": "collection_metadata"}) for item in manifest.get("inputs", [])}
    result = {
        "schema_version": SCHEMA, "contract": SCHEMA, "version": VERSION,
        "status": outcome, "dataset_id": dataset_id,
        "manifest_sha256": manifest["manifest_sha256"], "category_id": category_id,
        "definition_id": definition["definition_id"], "definition_sha256": definition["definition_sha256"],
        "source_versions": definition["source_versions"],
        "products": [json.loads(item) for item in sorted(products)],
        "analysis_scope": {
            "product_source_count": len(products),
            "single_product_source": len(products) == 1,
            "basis": "冻结数据中实际的渠道、商品标识与链接；多个来源不自动合并为同一个物理型号。",
            "instruction": "本结果覆盖整个所选数据集，产品列表不是筛选提示；多产品来源时先核对与用户链接的对应关系，单品问题应重新打开精确范围后分析。",
        },
        "sample": sample,
        "readiness": {
            "formal_sample_status": standard,
            "formal_sample_threshold_met": standard in {"pilot", "limited", "full"},
            "formal_report_thresholds": dict(REPORT_READINESS_THRESHOLDS),
            "formal_report_created": False, "document_kind": "private_research_brief",
            "experience_analysis_available": outcome == "ready",
            "minimum_comparison_n": LOW_SAMPLE_N,
            "comparison_policy": "分组不足最低样本量时不作显著差异判断；达到数量不代表具备因果证据。",
        },
        "source_completeness": manifest.get("source_completeness", []),
        "source_complete": manifest.get("source_complete"),
        "raw_payload_complete": manifest.get("raw_payload_complete"),
        "population": "canonical_eligible_review_entities",
        "scope": manifest["scope"],
        "chapters": definition["chapters"],
        "dimension_census": [], "aspect_experience": [], "atomic_scripts": None, "evidence": [],
        "field_definitions": {
            "sample.positive_rating_rate": "4–5星评论实体数 / 有效评分实体数；是整评星级，不是句级语义。",
            "sample.negative_rating_rate": "1–2星评论实体数 / 有效评分实体数；是整评星级，不是句级语义。",
            "aspect_experience.explicit_negative_entities": "现有品类规则在同句词法中命中负向的去重实体数；命中不保证语义正确。",
            "aspect_experience.explicit_negative_rate": "显式负向规则命中实体数 / 该属性提及实体数。",
            "aspect_experience.explicit_positive_entities": "现有品类规则在同句词法中命中正向的去重实体数；命中不保证语义正确。",
            "aspect_experience.explicit_positive_rate": "显式正向规则命中实体数 / 该属性提及实体数。",
            "aspect_experience.low_star_entities": "该属性提及实体中整评为1–2星的实体数，与显式语义负向不同。",
            "aspect_experience.low_star_rate": "该属性提及且有评分的实体中，1–2星实体数 / rating_n。",
            "aspect_experience.high_star_rate": "该属性提及且有评分的实体中，4–5星实体数 / rating_n。",
            "dimension_census.coverage": "该维度明确规则命中实体数 / 全部有正文实体数；未命中不等于不存在。",
        },
        "instructions": [
            "这是冻结数据上的确定性研究线索，未创建或发布正式报告；不要把样本状态当作语义验收。",
            "引用具体经历前，用query_dataset的review_ids按evidence.record_id读取完整评论和source_id。片段不代表完整条件。",
            "按问题选用有支持的章节；规则未命中不是没有问题，小样本不推断普遍发生率，不凑齐排名。",
            "既有研究计划仅作方法参考；本分析可读取已验证部分采集，结论只覆盖实际获得的评论。",
        ],
    }
    if outcome == "ready":
        index = OpeIndex(facts, profile_id=profile.profile_id, category_label=profile.category_label,
                         dimensions=profile.dimensions, numeric_dimension_ids=profile.numeric_dimension_ids,
                         aspect_rules=profile.aspect_rules)
        index.run()
        result["dimension_census"] = _build_dimension_census(index)
        result["aspect_experience"] = _aspect_rows(index)
        atomic = run_atomic_scripts(facts, snapshot_id=dataset_id, output_root=None)
        # run_atomic_scripts computes a logical full ledger even without a
        # disk artifact. Never advertise a nonexistent path as downloadable.
        atomic["ledger"] = {key: value for key, value in atomic["ledger"].items() if key != "relative_path"}
        atomic["ledger"]["stored"] = False
        result["atomic_scripts"] = atomic
        used = _evidence_ids([result["dimension_census"], result["aspect_experience"]])
        by_entity = {str(fact.get("review_entity_id") or fact.get("review_occurrence_id")): fact for fact in facts}
        by_occurrence = {fact.get("review_occurrence_id"): fact for fact in facts}
        quotes = [index.evidence.rows[key] for key in sorted(used)] + atomic["evidence"]
        for evidence in quotes:
            fact = by_entity.get(str(evidence.get("review_entity_id"))) or by_occurrence.get(evidence.get("review_occurrence_id"))
            if fact is None:
                raise DatasetError("analysis_evidence_invalid", "分析证据没有对应冻结评论", 409)
            result["evidence"].append({**evidence, "record_id": fact["record_id"],
                "dataset_id": dataset_id, "manifest_sha256": manifest["manifest_sha256"],
                "full_review_required": True})
    else:
        result["instructions"].append("没有可用正文时不要写网评体验结论；没有登记品类时不要套用其他品类规则。")
    result["analysis_id"] = analysis_id
    result["analysis_sha256"] = _hash(result)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache.parent, prefix='.analysis-', delete=False) as output:
        temporary = output.name
        json.dump(result, output, ensure_ascii=False)
    try:
        os.replace(temporary, cache)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result
