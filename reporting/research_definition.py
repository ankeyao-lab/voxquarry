"""Versioned Reporting definitions for private, report-independent research.

Rules are read from Reporting's registered implementations. This module does
not accept paths, execute supplied code, or lower formal report admission.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from .atomic_v2 import SCRIPT_REGISTRY, registry_manifest
from .config import ATOMIC_SCRIPT_REGISTRY_VERSION, LOW_SAMPLE_N
from .ope_product_report import EXTRACTOR_VERSION, category_profile
from .product_insight import REPORT_READINESS_THRESHOLDS
from .source import canonical_json
from .wenquan_datasets import DatasetError


SCHEMA = "wenquan-research-definition/v1"
VERSION = "wenquan-research-definition/1.0.0"
ROOT = Path(__file__).parent

# Same chapter identities as the current product report, not the retired
# mower-only ope_html narrative. These describe a document, not conclusions.
CHAPTERS = (
    ("sample", "样本概览", "sample", "说明实际产品、评论数量、来源和采集时间。"),
    ("summary", "决策摘要", None, "提炼有原文依据、需要产品经理关注的发现。"),
    ("profile", "用户画像", "dimension_census", "仅描述评论明确表达的人和角色，不猜人口画像。"),
    ("scenario", "使用场景", "dimension_census", "逐条保留具体任务、对象、环境和使用条件。"),
    ("experience", "Pros & Cons", "aspect_experience", "区分实际优缺点、词法信号和整条评论星级；排序说明依据。"),
    ("kano", "Kano", "atomic_scripts", "有证据时提出待验证需求假设，不冒充问卷Kano结论。"),
    ("opportunity", "产品机会", "atomic_scripts", "把可复核的体验问题转为待验证的改进机会。"),
    ("smart", "智能化机会", "atomic_scripts", "仅在实际任务和未满足需求支持时提出智能化假设。"),
    ("limits", "边界", "readiness", "仅说明会改变结论的缺口与下一步。"),
)


def _hash(value):
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _implementation_hashes():
    # Fixed packaged modules only. A model cannot select files on the host.
    names = ("atomic_v2.py", "ope_product_report.py", "category_profile_specs.py",
             "product_insight.py", "research_definition.py", "dataset_analysis.py")
    return {name: "sha256:" + hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in names}


def get_research_definition(category_id):
    if not isinstance(category_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", category_id):
        raise DatasetError("invalid_request", "品类标识无效", 422)
    profile = category_profile(category_id)
    plan = json.loads((ROOT / "research_plans" / "full-category-v1.json").read_text(encoding="utf-8"))
    scripts = registry_manifest()
    rules = [{"script_id": script.script_id, "patterns": [
        {"label": label, "expression": expression} for label, expression in script.patterns
    ]} for script in SCRIPT_REGISTRY]
    result = {
        "schema_version": SCHEMA, "contract": SCHEMA, "version": VERSION,
        "category_id": profile.category_id if profile else category_id,
        "supported": profile is not None,
        "profile": asdict(profile) if profile else None,
        "chapters": [{"id": key, "title": title, "analysis_field": field, "purpose": purpose}
                     for key, title, field, purpose in CHAPTERS],
        "research_plan": plan if profile else None,
        "scripts": scripts if profile else [],
        "script_rules": rules if profile else [],
        "source_versions": {
            "profile": profile.profile_id if profile else None,
            "extractor": EXTRACTOR_VERSION,
            "script_registry": ATOMIC_SCRIPT_REGISTRY_VERSION,
            "research_plan": plan["version"],
            "implementation_sha256": _implementation_hashes(),
            "profile_sha256": _hash(asdict(profile)) if profile else None,
            "script_rules_sha256": _hash(rules),
            "research_plan_sha256": _hash(plan),
        },
        "sample_policy": {
            "formal_report_thresholds": dict(REPORT_READINESS_THRESHOLDS),
            "minimum_comparison_n": LOW_SAMPLE_N,
            "count_basis": "去重后的合规有效正文评论实体",
            "private_brief_requires_formal_threshold": False,
            "zero_text": "只交付可核验产品资料、评分统计和采集结果，不生成网评体验结论。",
            "small_sample": "保留实际评论数和单条经历；不能据此推断普遍发生率或强行凑齐排名。",
        },
        "usage": {
            "document_kind": "private_research_brief",
            "formal_report_created": False,
            "research_plan_role": "参考对应章节与已登记分析方法；不是正式报告已通过全部发布条件的证明。",
            "chapter_policy": "按问题和证据选用章节；缺少支持时省略或简述无法判断，不生成占位结论。",
            "rule_policy": "规则命中是研究线索，须读取对应评论全文核对对象、条件和语义后再形成结论。",
        },
    }
    result["definition_id"] = "rdef_" + _hash(result).split(":")[1]
    result["definition_sha256"] = _hash(result)
    return result
