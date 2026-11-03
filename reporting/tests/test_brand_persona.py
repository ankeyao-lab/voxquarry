from __future__ import annotations

import unittest
import tempfile
import json
import os
import time
from pathlib import Path
from importlib.util import find_spec
from unittest.mock import Mock, patch

from reporting.brand_persona import (
    BRAND_PERSONA_CACHE_VERSION,
    BRAND_PERSONA_METHOD_VERSION,
    BrandPersonaContractError,
    build_brand_persona_analysis,
    normalize_brand_persona_scope,
    resolve_brand_persona_job_ids,
    validate_persona_narrative,
)
from reporting.persona_services import (
    QwenPersonaClient,
    QwenPersonaImageClient,
    optional_env_boolean,
    search_pexels_image,
)
from reporting.brand_persona_store import (
    BrandPersonaStore,
    public_brand_persona_analysis,
)

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient
    from reporting.app import ReportingService, create_app
else:
    TestClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment,misc]
    ReportingService = None  # type: ignore[assignment,misc]


def _fact(
    index: int,
    *,
    category_id: str,
    category: str,
    model: str,
    text: str,
    rating: int = 5,
) -> dict:
    return {
        "review_occurrence_id": f"occ_persona_{index}",
        "review_entity_id": f"rev_persona_{index}",
        "review_id": f"persona-{index}",
        "canonical_entity_rank": 1,
        "is_duplicate_occurrence": False,
        "job_id": f"job-{category_id}",
        "channel": "ego",
        "brand": "EGO",
        "category_id": category_id,
        "category": category,
        "model": model,
        "market_region": "North America",
        "review_title": "",
        "review_text": text,
        "has_text": bool(text),
        "rating": rating,
        "rating_valid": True,
        "age": "",
        "submission_date": "2026-01-01",
        "business_key_status": "complete",
        "source_site_raw": "example.test",
        "product_url": "https://example.test/product",
    }


class BrandPersonaTests(unittest.TestCase):
    def test_v3_runs_rich_cross_category_modules_and_multi_claim_personas(self) -> None:
        facts = [
            _fact(
                100,
                category_id="walk_behind_mower",
                category="Walk-Behind Mower",
                model="LM2135SP",
                text=(
                    "I am a contractor and maintain rental properties every week. "
                    "I already own EGO batteries and chose this for powerful performance, "
                    "runtime and easy setup. The lightweight balance is comfortable."
                ),
            ),
            _fact(
                101,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text=(
                    "I run a landscaping business with multiple customer sites. "
                    "The same battery platform is convenient and gets the job done, "
                    "but charging is slow and customer service sent a replacement."
                ),
                rating=2,
            ),
            _fact(
                102,
                category_id="string_trimmer",
                category="String Trimmer",
                model="ST1623T",
                text=(
                    "I switched from gas after years of experience. Easy to use and "
                    "lightweight were my selection criteria. The battery failed and I returned it."
                ),
                rating=1,
            ),
            _fact(
                103,
                category_id="hedge_trimmer",
                category="Hedge Trimmer",
                model="HT2601",
                text=(
                    "I am retired and have arthritis. I maintain my own property and need "
                    "safe control, low noise, easy handling and reliable runtime."
                ),
                rating=4,
            ),
        ]
        result = build_brand_persona_analysis(
            facts,
            {"brand": "EGO", "job_ids": sorted({row["job_id"] for row in facts})},
            snapshot_id="snap_persona_v2_fixture",
        )
        self.assertEqual(result["schema_version"], "brand-persona-workbench/v3")
        self.assertEqual(result["method_version"], BRAND_PERSONA_METHOD_VERSION)
        self.assertEqual(result["cache_version"], BRAND_PERSONA_CACHE_VERSION)
        self.assertFalse(result["deterministic_report"]["llm_used"])
        self.assertIn("experience_risk_matrix", result["deterministic_report"])
        self.assertIn("battery_chain", result["deterministic_report"])
        self.assertEqual(
            [row["review_entities"] for row in result["personas"]],
            sorted(
                [row["review_entities"] for row in result["personas"]],
                reverse=True,
            ),
        )
        self.assertEqual(result["semantic_versions"]["ope_extractor"], "ope-context/3.4.4")
        for module in (
            "executive_readout",
            "decision_drivers",
            "experience_systems",
            "lifecycle_stages",
            "cross_category_signals",
            "cooccurrence_signals",
            "temporal_trends",
            "failure_chains",
            "execution_ledger",
        ):
            self.assertIn(module, result)
        self.assertTrue(result["decision_drivers"])
        self.assertTrue(result["experience_systems"])
        self.assertTrue(result["lifecycle_stages"])
        self.assertTrue(result["failure_chains"])
        self.assertEqual(result["execution_ledger"]["failed_extractors"], 0)
        self.assertEqual(
            result["execution_ledger"]["expected_entity_executions"],
            result["execution_ledger"]["completed_entity_executions"],
        )
        self.assertTrue(any(len(row["claim_ids"]) >= 3 for row in result["personas"]))
        for persona in result["personas"]:
            memberships = [
                row
                for row in result["evidence"]
                if row.get("kind") == "persona_membership"
                and row.get("label") == persona["persona_id"]
            ]
            self.assertEqual(len(memberships), persona["review_entities"])
            self.assertEqual(persona["evidence_count"], persona["review_entities"])
        self.assertTrue(
            all(
                hypothesis["status"] == "evidence_backed_hypothesis"
                for persona in result["personas"]
                for hypothesis in persona["product_hypotheses"]
            )
        )

    def test_low_sample_does_not_claim_skil_richness_benchmark_pass(self) -> None:
        result = build_brand_persona_analysis(
            [
                _fact(
                    104,
                    category_id="leaf_blower",
                    category="Leaf Blower",
                    model="LB7654",
                    text="Easy to use and lightweight.",
                )
            ],
            {"brand": "EGO", "job_ids": ["job-leaf_blower"]},
            snapshot_id="snap_low_sample_fixture",
        )
        self.assertEqual(
            result["quality"]["information_richness"]["status"],
            "insufficient_for_benchmark",
        )

    def test_single_category_sample_is_scope_limited_not_cross_category_pass(self) -> None:
        facts = [
            _fact(
                200 + index,
                category_id="walk_behind_mower",
                category="Walk-Behind Mower",
                model="LM2135SP" if index % 2 else "LM2114",
                text=(
                    "I maintain my own large yard every week. I chose this mower for "
                    "powerful performance, runtime, easy setup and battery compatibility."
                ),
            )
            for index in range(320)
        ]
        result = build_brand_persona_analysis(
            facts,
            {"brand": "EGO", "job_ids": sorted({row["job_id"] for row in facts})},
            snapshot_id="snap_single_category_fixture",
        )
        richness = result["quality"]["information_richness"]
        self.assertEqual(richness["status"], "scope_limited")
        self.assertEqual(richness["checks"]["cross_category_count"], 1)

    def test_temporal_comparison_uses_same_month_day_window(self) -> None:
        facts = []
        for index in range(120):
            prior = _fact(
                600 + index,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="Easy setup and reliable performance.",
            )
            prior["submission_date"] = "2025-06-01"
            facts.append(prior)
            current = _fact(
                800 + index,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text=(
                    "Battery runtime and charging are important."
                    if index < 60
                    else "Easy setup and reliable performance."
                ),
            )
            current["submission_date"] = "2026-06-01"
            facts.append(current)
        after_cutoff = _fact(
            999,
            category_id="leaf_blower",
            category="Leaf Blower",
            model="LB7654",
            text="Battery runtime and charging are important.",
        )
        after_cutoff["submission_date"] = "2025-12-31"
        facts.append(after_cutoff)
        result = build_brand_persona_analysis(
            facts,
            {"brand": "EGO", "job_ids": ["job-leaf_blower"]},
            snapshot_id="snap_same_period_fixture",
        )
        temporal = result["temporal_trends"]
        self.assertEqual(temporal["status"], "available")
        self.assertEqual(temporal["comparison"]["through_month_day"], "06-01")
        self.assertEqual(temporal["comparison"]["baseline_written"], 120)
        self.assertEqual(temporal["comparison"]["current_written"], 120)

    def test_scope_requires_one_brand_and_derives_explicit_jobs(self) -> None:
        tasks = [
            {
                "id": "mower-job",
                "brand": "EGO",
                "category_id": "walk_behind_mower",
                "category": "Walk-Behind Mower",
                "model": "LM2135SP",
                "channel": "ego",
                "market_region": "North America",
            },
            {
                "id": "blower-job",
                "brand": "EGO",
                "category_id": "leaf_blower",
                "category": "Leaf Blower",
                "model": "LB7654",
                "channel": "lowes",
                "market_region": "North America",
            },
            {
                "id": "other-brand-job",
                "brand": "RYOBI",
                "category_id": "leaf_blower",
                "category": "Leaf Blower",
                "model": "P21014",
                "channel": "ryobi",
                "market_region": "North America",
            },
        ]
        scope = normalize_brand_persona_scope(
            {
                "brand": "EGO",
                "category_ids": ["walk_behind_mower", "leaf_blower"],
                "models": [],
                "channels": [],
                "market_regions": [],
                "date_range": {},
            }
        )
        self.assertEqual(
            resolve_brand_persona_job_ids(tasks, scope),
            ["blower-job", "mower-job"],
        )
        with self.assertRaises(BrandPersonaContractError):
            normalize_brand_persona_scope({"brand": ["EGO", "RYOBI"]})

    def test_cross_category_analysis_keeps_unknowns_and_evidence(self) -> None:
        facts = [
            _fact(
                1,
                category_id="walk_behind_mower",
                category="Walk-Behind Mower",
                model="LM2135SP",
                text=(
                    "I am a retired contractor and maintain two rental properties. "
                    "The battery platform lets me finish both yards."
                ),
            ),
            _fact(
                2,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="I use it in my yard and it works well.",
                rating=4,
            ),
            _fact(
                3,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="",
                rating=5,
            ),
        ]
        result = build_brand_persona_analysis(
            facts,
            {
                "brand": "EGO",
                "category_ids": ["leaf_blower", "walk_behind_mower"],
                "job_ids": ["job-leaf_blower", "job-walk_behind_mower"],
            },
            snapshot_id="snap_persona_fixture",
        )
        self.assertEqual(result["sample"]["review_entities"], 3)
        self.assertEqual(result["sample"]["written_review_entities"], 2)
        self.assertEqual(result["sample"]["ratings_only_entities"], 1)
        self.assertEqual(result["scope"]["brand"], "EGO")
        self.assertGreater(result["quality"]["identity_unknown_entities"], 0)
        self.assertTrue(result["evidence"])
        self.assertTrue(
            all(row["brand"] == "EGO" for row in result["evidence"])
        )
        # A generic "my yard" sentence must not be promoted to occupation.
        occupation = next(
            row
            for row in result["dimensions"]
            if row["dimension_id"] == "occupation"
        )
        self.assertLessEqual(occupation["review_entities"], 1)
        self.assertEqual(
            {row["category_id"] for row in result["task_context"]},
            {"leaf_blower", "walk_behind_mower"},
        )

    def test_retired_rule_requires_reviewer_identity_not_retired_tool(self) -> None:
        facts = [
            _fact(
                10,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="BL4713C-13",
                text="Retired electric blower, no more extension cord to carry.",
            ),
            _fact(
                11,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="BL4713C-13",
                text="I am retired and use this around my property.",
            ),
        ]
        result = build_brand_persona_analysis(
            facts,
            {"brand": "EGO", "job_ids": ["job-leaf_blower"]},
            snapshot_id="snap_retired_precision_fixture",
        )
        life_stage = next(
            row
            for row in result["dimensions"]
            if row["dimension_id"] == "life_stage_ability"
        )
        retired = next(
            row for row in life_stage["values"] if row["value_id"] == "retired"
        )
        self.assertEqual(retired["review_entities"], 1)

    def test_customer_language_requires_real_commercial_work_context(self) -> None:
        facts = [
            _fact(
                30,
                category_id="fan",
                category="Fan",
                model="FN1800E",
                text="I recommend this fan for customers.",
            ),
            _fact(
                31,
                category_id="string_trimmer",
                category="String Trimmer",
                model="ST1511T",
                text="I wish the brand offered discounts for customers.",
            ),
            _fact(
                32,
                category_id="chainsaw",
                category="Chain Saw",
                model="CS1800",
                text="I cut down a small avocado tree for a client and it performed beautifully.",
            ),
            _fact(
                33,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="I ran into a guy who did lawn care professionally and he recommended it.",
            ),
            _fact(
                34,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="I work in lawn care and use this blower after every job.",
            ),
            _fact(
                35,
                category_id="led_lighting",
                category="LED Lighting",
                model="PCL660B",
                text="I received this light as a gift from my client and use it at home.",
            ),
            _fact(
                36,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="I use this blower to dry my clients vehicles after detailing.",
            ),
        ]
        result = build_brand_persona_analysis(
            facts,
            {
                "brand": "EGO",
                "job_ids": ["job-fan", "job-string_trimmer", "job-chainsaw"],
            },
            snapshot_id="snap_commercial_context_fixture",
        )
        responsibility = next(
            row
            for row in result["dimensions"]
            if row["dimension_id"] == "operating_responsibility"
        )
        commercial = next(
            row
            for row in responsibility["values"]
            if row["value_id"] == "commercial_service_operator"
        )
        self.assertEqual(commercial["review_entities"], 2)
        occupation = next(
            row
            for row in result["dimensions"]
            if row["dimension_id"] == "occupation"
        )
        landscape = next(
            row
            for row in occupation["values"]
            if row["value_id"] == "landscape_tree_professional"
        )
        self.assertEqual(landscape["review_entities"], 1)

    def test_source_age_is_not_reinterpreted_as_reviewer_demographics(self) -> None:
        review_age = _fact(
            13,
            category_id="leaf_blower",
            category="Leaf Blower",
            model="LB7654",
            text="This has worked well for my yard.",
        )
        review_age["age"] = "65+"
        explicit_age = _fact(
            14,
            category_id="leaf_blower",
            category="Leaf Blower",
            model="LB7654",
            text="I am a 72 year old homeowner and use this in my yard.",
        )
        result = build_brand_persona_analysis(
            [review_age, explicit_age],
            {"brand": "EGO", "job_ids": ["job-leaf_blower"]},
            snapshot_id="snap_age_semantics_fixture",
        )
        age = next(
            row
            for row in result["dimensions"]
            if row["dimension_id"] == "self_reported_age"
        )
        self.assertEqual(age["review_entities"], 1)
        older = next(
            row
            for row in result["personas"]
            if row["persona_id"] == "older_or_retired_operator"
        )
        self.assertEqual(older["review_entities"], 1)

    def test_incomplete_entity_keys_are_preserved_but_not_called_canonical(self) -> None:
        canonical = _fact(
            20,
            category_id="leaf_blower",
            category="Leaf Blower",
            model="LB7654",
            text="I use this blower every week around my yard.",
        )
        duplicate = dict(canonical)
        duplicate["review_occurrence_id"] = "occurrence-duplicate"
        duplicate["canonical_entity_rank"] = 2
        duplicate["is_duplicate_occurrence"] = True
        incomplete = _fact(
            21,
            category_id="leaf_blower",
            category="Leaf Blower",
            model="LB7654",
            text="Lightweight and easy to use.",
        )
        incomplete["business_key_status"] = "incomplete"
        incomplete["review_entity_id"] = ""
        incomplete["review_occurrence_id"] = "occurrence-incomplete"
        result = build_brand_persona_analysis(
            [canonical, duplicate, incomplete],
            {"brand": "EGO", "job_ids": ["job-leaf_blower"]},
            snapshot_id="snap_incomplete_identity_fixture",
        )
        quality = result["data_quality"]
        self.assertEqual(result["sample"]["review_entities"], 2)
        self.assertEqual(quality["analysis_review_entities"], 2)
        self.assertEqual(quality["unique_complete_review_entities"], 1)
        self.assertEqual(quality["canonical_review_entities"], 1)
        self.assertEqual(quality["incomplete_identity_occurrences"], 1)
        self.assertEqual(quality["duplicate_occurrences_excluded"], 1)

    def test_unknown_category_is_visible_but_profile_rules_are_disabled(self) -> None:
        result = build_brand_persona_analysis(
            [
                _fact(
                    12,
                    category_id="unknown_category",
                    category="Unknown Category",
                    model="UNMAPPED-1",
                    text="Works as expected.",
                )
            ],
            {"brand": "EGO", "job_ids": ["job-unknown"]},
            snapshot_id="snap_unknown_category_fixture",
        )
        self.assertEqual(
            result["sample_composition"]["categories"][0]["category"],
            "Unknown Category",
        )
        self.assertEqual(
            result["quality"]["unsupported_profile_categories"],
            ["unknown_category"],
        )
        self.assertEqual(result["personas"], [])

    def test_llm_persona_can_only_reference_deterministic_claims(self) -> None:
        allowed_claims = {
            "claim_professional": {
                "claim_id": "claim_professional",
                "metric_value": 12,
                "evidence_ids": ["ev_1"],
            }
        }
        valid = {
            "personas": [
                {
                    "persona_id": "professional_operator",
                    "name": "跨场地专业作业者",
                    "summary": "明确承担客户或多场地任务，关注连续完成度。",
                    "first_person_statement": "我要一次完成任务，并且随时知道如何恢复。",
                    "who_they_are": "明确承担跨场地任务并以稳定完成度评价产品的人。",
                    "claim_ids": ["claim_professional"],
                    "day_in_life": [
                        {"stage": "准备", "narrative": "先判断任务和装备是否匹配。", "claim_ids": ["claim_professional"]},
                        {"stage": "作业", "narrative": "持续观察完成度和中断风险。", "claim_ids": ["claim_professional"]}
                    ],
                    "journey": [
                        {"stage": "选择", "narrative": "用任务标准筛选产品。", "claim_ids": ["claim_professional"]},
                        {"stage": "复用", "narrative": "根据真实体验决定继续使用。", "claim_ids": ["claim_professional"]}
                    ],
                    "image_brief": {
                        "portrait_query": "professional outdoor power equipment operator portrait",
                        "context_query": "professional outdoor power equipment operator working",
                        "avoid": ["brand logo", "unsafe operation"],
                    },
                }
            ]
        }
        checked = validate_persona_narrative(valid, allowed_claims)
        self.assertEqual(checked["personas"][0]["claim_ids"], ["claim_professional"])

        invalid = {
            "personas": [
                {
                    "persona_id": "invented",
                    "name": "年轻家庭用户",
                    "summary": "占全部用户 42%。",
                    "first_person_statement": "我是虚构用户。",
                    "who_they_are": "虚构群体。",
                    "claim_ids": ["missing_claim"],
                    "day_in_life": [], "journey": [],
                    "image_brief": {"portrait_query": "young family", "context_query": "young family", "avoid": []},
                }
            ]
        }
        with self.assertRaises(BrandPersonaContractError):
            validate_persona_narrative(invalid, allowed_claims)

        eight_claims = {
            f"claim_{index}": {
                "claim_id": f"claim_{index}",
                "persona_id": f"persona_{index}",
            }
            for index in range(8)
        }
        eight_personas = {
            "personas": [
                {
                    "persona_id": f"persona_{index}",
                    "name": f"证据群组 {chr(65 + index)}",
                    "summary": "由确定性证据定义的任务群组。",
                    "first_person_statement": "我需要稳定完成任务。",
                    "who_they_are": "由明确评论证据定义的任务群组。",
                    "claim_ids": [f"claim_{index}"],
                    "day_in_life": [
                        {"stage": "准备", "narrative": "先确认任务边界。", "claim_ids": [f"claim_{index}"]},
                        {"stage": "执行", "narrative": "按任务标准完成工作。", "claim_ids": [f"claim_{index}"]}
                    ],
                    "journey": [
                        {"stage": "选择", "narrative": "比较可验证的完成标准。", "claim_ids": [f"claim_{index}"]},
                        {"stage": "使用", "narrative": "根据体验决定是否继续。", "claim_ids": [f"claim_{index}"]}
                    ],
                    "image_brief": {
                        "portrait_query": "outdoor equipment operator portrait",
                        "context_query": "outdoor equipment operator working",
                        "avoid": ["brand logo"],
                    },
                }
                for index in range(8)
            ]
        }
        self.assertEqual(
            len(validate_persona_narrative(eight_personas, eight_claims)["personas"]),
            8,
        )

        with self.assertRaises(BrandPersonaContractError):
            validate_persona_narrative(
                {
                    "personas": [
                        {
                            "persona_id": "professional_operator",
                            "name": "跨场地专业作业者",
                            "summary": "以连续任务完成度衡量设备。",
                            "first_person_statement": "我需要连续完成任务。",
                            "who_they_are": "明确承担连续任务的人。",
                            "claim_ids": ["claim_other"],
                            "day_in_life": [{"stage": "准备", "narrative": "先确认任务。", "claim_ids": ["claim_other"]}, {"stage": "执行", "narrative": "持续完成工作。", "claim_ids": ["claim_other"]}],
                            "journey": [{"stage": "选择", "narrative": "比较产品。", "claim_ids": ["claim_other"]}, {"stage": "使用", "narrative": "验证体验。", "claim_ids": ["claim_other"]}],
                            "image_brief": {"portrait_query": "operator portrait", "context_query": "operator outdoors", "avoid": []},
                        }
                    ]
                },
                {
                    "claim_other": {
                        "claim_id": "claim_other",
                        "persona_id": "residential_self_maintainer",
                    }
                },
            )

    def test_qwen_and_pexels_clients_keep_secrets_server_side(self) -> None:
        captured = {}

        def post_json(url, headers, body, timeout):
            captured["qwen"] = (url, headers, body, timeout)
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"personas":[{"persona_id":"professional_operator",'
                                '"name":"跨场地专业作业者",'
                                '"summary":"明确承担客户或多场地任务，关注连续完成度。",'
                                '"first_person_statement":"我要稳定完成跨场地任务。",'
                                '"who_they_are":"明确承担跨场地任务并关注连续完成度的人。",'
                                '"claim_ids":["claim_professional"],'
                                '"day_in_life":[{"stage":"准备","narrative":"先确认任务边界。","claim_ids":["claim_professional"]},{"stage":"执行","narrative":"持续关注完成度。","claim_ids":["claim_professional"]}],'
                                '"journey":[{"stage":"选择","narrative":"用任务标准比较产品。","claim_ids":["claim_professional"]},{"stage":"使用","narrative":"用真实体验验证选择。","claim_ids":["claim_professional"]}],'
                                '"image_brief":{"portrait_query":"professional outdoor power equipment operator portrait","context_query":"professional outdoor power equipment operator working",'
                                '"avoid":["brand logo","unsafe operation"]}}]}'
                            )
                        }
                    }
                ]
            }

        client = QwenPersonaClient(
            api_key="secret-qwen",
            base_url="https://example.test/v1",
            model="qwen3.7-plus",
            post_json=post_json,
        )
        result = client.generate(
            {
                "claim_professional": {
                    "claim_id": "claim_professional",
                    "review_entities": 12,
                    "coverage": 0.1,
                    "evidence_ids": ["ev_1"],
                }
            }
        )
        self.assertEqual(result["personas"][0]["persona_id"], "professional_operator")
        self.assertEqual(captured["qwen"][1]["Authorization"], "Bearer secret-qwen")
        self.assertNotIn("secret-qwen", str(captured["qwen"][2]))
        self.assertNotIn("enable_thinking", captured["qwen"][2])

        thinking_client = QwenPersonaClient(
            api_key="secret-qwen",
            base_url="https://example.test/v1",
            model="deepseek-v4-flash-0731",
            post_json=post_json,
            enable_thinking=True,
        )
        thinking_client.generate(
            {
                "claim_professional": {
                    "claim_id": "claim_professional",
                    "review_entities": 12,
                    "coverage": 0.1,
                    "evidence_ids": ["ev_1"],
                }
            }
        )
        self.assertIs(captured["qwen"][2]["enable_thinking"], True)

        thinking_client.enable_thinking = False
        thinking_client.generate(
            {
                "claim_professional": {
                    "claim_id": "claim_professional",
                    "review_entities": 12,
                    "coverage": 0.1,
                    "evidence_ids": ["ev_1"],
                }
            }
        )
        self.assertIs(captured["qwen"][2]["enable_thinking"], False)

        def get_json(url, headers, timeout):
            captured["pexels"] = (url, headers, timeout)
            return {
                "photos": [
                    {
                        "id": 42,
                        "url": "https://www.pexels.com/photo/42/",
                        "photographer": "Test Artist",
                        "photographer_url": "https://www.pexels.com/@artist/",
                        "alt": "Operator outdoors",
                        "src": {
                            "landscape": "https://images.pexels.com/photos/42/pexels-photo-42.jpeg",
                            "portrait": "https://images.pexels.com/photos/42/pexels-photo-42-portrait.jpeg",
                            "medium": "https://images.pexels.com/photos/42/pexels-photo-42.jpeg",
                        },
                    }
                ]
            }

        image = search_pexels_image(
            "professional outdoor power equipment operator",
            api_key="secret-pexels",
            orientation="portrait",
            get_json=get_json,
        )
        self.assertEqual(image["provider"], "Pexels")
        self.assertEqual(image["photo_id"], 42)
        self.assertTrue(image["illustrative_only"])
        self.assertIn("portrait", image["image_url"])
        self.assertIn("orientation=portrait", captured["pexels"][0])
        self.assertEqual(captured["pexels"][1]["Authorization"], "secret-pexels")
        self.assertEqual(captured["pexels"][1]["Accept"], "application/json")
        self.assertIn("VoxQuarry", captured["pexels"][1]["User-Agent"])

    def test_qwen_image_is_disabled_before_post_or_download(self) -> None:
        post_json = Mock()
        get_bytes = Mock()
        client = QwenPersonaImageClient(
            api_key="secret-dashscope",
            model="qwen-image-3.0-pro",
            post_json=post_json,
            get_bytes=get_bytes,
        )
        for prompt in ("safe adult operator", ""):
            with self.subTest(prompt=prompt):
                with self.assertRaisesRegex(BrandPersonaContractError, "已关闭"):
                    client.generate(prompt)
        post_json.assert_not_called()
        get_bytes.assert_not_called()

    def test_qwen_image_default_transport_cannot_make_network_requests(self) -> None:
        with patch("reporting.persona_services.urllib.request.urlopen") as network:
            client = QwenPersonaImageClient(api_key="secret-dashscope")
            with self.assertRaisesRegex(BrandPersonaContractError, "已关闭"):
                client.generate("safe adult operator")
        network.assert_not_called()
        with self.assertRaisesRegex(BrandPersonaContractError, "官方地址"):
            QwenPersonaImageClient(
                api_key="secret-dashscope",
                model="qwen-image-3.0-pro",
                endpoint="https://attacker.example/generation",
            )

    def test_persona_image_asset_is_content_addressed_and_retrievable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BrandPersonaStore(Path(directory))
            stored = store.store_persona_image(
                analysis_id="bpa_1234567890abcdef12345678",
                persona_id="professional_operator",
                asset_bytes=b"\x89PNG\r\n\x1a\nfixture",
                mime_type="image/png",
                provider="Qwen Image",
                model="qwen-image-3.0-pro",
                prompt_sha256="sha256:" + "1" * 64,
            )
            self.assertEqual(stored["schema_version"], "brand-persona-image/v1")
            self.assertTrue(stored["asset_sha256"].startswith("sha256:"))
            self.assertIn("professional_operator", stored["image_url"])
            self.assertNotIn("aliyuncs.com", str(stored))
            path, media_type = store.get_persona_image_asset(
                stored["asset_sha256"]
            )
            self.assertEqual(media_type, "image/png")
            self.assertEqual(path.read_bytes(), b"\x89PNG\r\n\x1a\nfixture")

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_generation_keeps_qwen_text_without_image_calls_when_keys_are_set(self) -> None:
        analysis_id = "bpa_1234567890abcdef12345678"
        deterministic_persona = {
            "persona_id": "professional_operator",
            "title": "专业作业者",
            "definition": "明确表达跨场地作业责任的评论群体。",
            "jtbd": "稳定完成多个场地的维护任务",
            "claim_ids": ["claim_professional"],
            "lifecycle_stages": [],
            "top_categories": [
                {"category": "Walk-Behind Mower", "review_entities": 12}
            ],
        }
        generated_persona = {
            "persona_id": "professional_operator",
            "name": "跨场地专业作业者",
            "summary": "以稳定完成任务和恢复能力评价工具。",
            "first_person_statement": "我要稳定完成任务，并能处理意外中断。",
            "who_they_are": "由明确作业责任证据定义的群体。",
            "claim_ids": ["claim_professional"],
            "day_in_life": [
                {
                    "stage": "执行",
                    "narrative": "持续核验完成质量。",
                    "claim_ids": ["claim_professional"],
                }
            ],
            "journey": [
                {
                    "stage": "选择",
                    "narrative": "按任务标准比较产品。",
                    "claim_ids": ["claim_professional"],
                }
            ],
            "image_brief": {
                "portrait_query": "professional outdoor operator portrait",
                "context_query": "safe property maintenance",
                "avoid": ["brand logo"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snapshot").mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "generated_at": "fixture"},
            ):
                service = ReportingService(root / "snapshot", root / "var")
            service.brand_personas.cache(
                {
                    "analysis_id": analysis_id,
                    "schema_version": "brand-persona-workbench/v3",
                    "method_version": "brand-persona-deterministic/3.0.0",
                    "cache_version": "fixture",
                    "snapshot_id": "snapshot-fixture",
                    "content_hash": "sha256:" + "3" * 64,
                    "scope": {"brand": "EGO"},
                    "deterministic_personas": [deterministic_persona],
                    "claim_pool": [
                        {
                            "claim_id": "claim_professional",
                            "persona_id": "professional_operator",
                            "evidence_ids": ["ev_1"],
                        }
                    ],
                    "personas": [],
                }
            )
            progress = []
            with patch.dict(
                os.environ,
                {
                    "VOXQUARRY_LLM_API_KEY": "secret-dashscope",
                    "VOXQUARRY_QWEN_API_KEY": "secret-qwen",
                    "QWEN_API_KEY": "secret-qwen-compat",
                    "VOXQUARRY_QWEN_MODEL": "deepseek-v4-flash-0731",
                    "VOXQUARRY_PERSONA_IMAGE_MODEL": "qwen-image-3.0-pro",
                    "VOXQUARRY_PERSONA_IMAGE_ENDPOINT": QwenPersonaImageClient.DEFAULT_ENDPOINT,
                },
                clear=True,
            ), patch(
                "reporting.app.QwenPersonaClient.generate",
                return_value={"personas": [generated_persona]},
            ) as text_generation, patch(
                "reporting.persona_services.QwenPersonaImageClient.generate"
            ) as image_generation, patch(
                "reporting.persona_services.urllib.request.urlopen"
            ) as network:
                result = service.generate_brand_personas(
                    analysis_id,
                    image_progress=lambda completed, total: progress.append(
                        (completed, total)
                    ),
                )
            self.assertEqual(progress, [(1, 1)])
            text_generation.assert_called_once()
            image_generation.assert_not_called()
            network.assert_not_called()
            self.assertEqual(result["persona_narrative"]["status"], "qwen_validated")
            self.assertTrue(result["persona_narrative"]["llm_used"])
            self.assertEqual(result["personas"][0]["title"], generated_persona["name"])
            self.assertEqual(result["persona_narrative"]["image_status"], "unconfigured")
            self.assertEqual(result["persona_narrative"]["persona_image_count"], 0)
            self.assertEqual(result["persona_narrative"]["persona_image_target"], 1)
            self.assertIsNone(result["personas"][0]["image"])
            self.assertNotIn("secret-dashscope", str(result))

            # An explicitly configured existing stock-photo service still fills
            # missing images without enabling Qwen Image or another text call.
            stock_image = {
                "provider": "Pexels",
                "image_url": "https://images.pexels.com/fixture.jpg",
            }
            with patch.dict(
                os.environ,
                {"VOXQUARRY_LLM_API_KEY": "secret-dashscope", "PEXELS_API_KEY": "secret-pexels"},
                clear=True,
            ), patch(
                "reporting.app.QwenPersonaClient.generate"
            ) as text_generation, patch(
                "reporting.persona_services.QwenPersonaImageClient.generate"
            ) as image_generation, patch(
                "reporting.app.search_pexels_image", return_value=stock_image
            ) as pexels, patch(
                "reporting.persona_services.urllib.request.urlopen"
            ) as network:
                with_pexels = service.generate_brand_personas(analysis_id)
            pexels.assert_called_once_with(
                generated_persona["image_brief"]["portrait_query"],
                api_key="secret-pexels",
                orientation="portrait",
            )
            text_generation.assert_not_called()
            image_generation.assert_not_called()
            network.assert_not_called()
            self.assertEqual(with_pexels["personas"][0]["image"], stock_image)
            self.assertEqual(with_pexels["persona_narrative"]["image_status"], "ready")
            service.close()

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_image_refresh_preserves_validated_text_and_all_saved_images(self) -> None:
        analysis_id = "bpa_abcdef1234567890abcdef12"
        deterministic_persona = {
            "persona_id": "professional_operator",
            "title": "专业作业者",
            "definition": "明确表达跨场地作业责任的评论群体。",
            "jtbd": "稳定完成多个场地的维护任务",
            "claim_ids": ["claim_professional"],
            "lifecycle_stages": [],
            "top_categories": [
                {"category": "Walk-Behind Mower", "review_entities": 12}
            ],
        }
        validated_persona = {
            **deterministic_persona,
            "title": "跨场地专业作业者",
            "summary": "以稳定完成任务和恢复能力评价工具。",
            "first_person_statement": "我要稳定完成任务，并能处理意外中断。",
            "who_they_are": "由明确作业责任证据定义的群体。",
            "day_in_life": [],
            "journey": [],
            "image_brief": {
                "portrait_query": "professional outdoor operator portrait",
                "context_query": "safe property maintenance",
                "avoid": ["brand logo"],
            },
            "narrative_origin": "qwen_derived_text",
            "image": {
                "provider": "Pexels",
                "image_url": "https://images.pexels.com/old.jpg",
            },
            "context_image": {
                "provider": "Pexels",
                "image_url": "https://images.pexels.com/context.jpg",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snapshot").mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "generated_at": "fixture"},
            ):
                service = ReportingService(root / "snapshot", root / "var")
            service.brand_personas.cache(
                {
                    "analysis_id": analysis_id,
                    "schema_version": "brand-persona-workbench/v3",
                    "method_version": "brand-persona-deterministic/3.0.0",
                    "cache_version": "fixture",
                    "snapshot_id": "snapshot-fixture",
                    "content_hash": "sha256:" + "7" * 64,
                    "scope": {"brand": "EGO"},
                    "deterministic_personas": [deterministic_persona],
                    "claim_pool": [
                        {
                            "claim_id": "claim_professional",
                            "persona_id": "professional_operator",
                            "evidence_ids": ["ev_1"],
                        }
                    ],
                    "personas": [validated_persona],
                    "persona_narrative": {
                        "status": "qwen_validated",
                        "schema_version": "brand-persona-narrative/v3",
                        "policy_version": "persona-claim-lock/3.0.0",
                        "llm_used": True,
                        "model": "deepseek-v4-flash-0731",
                        "thinking_enabled": True,
                        "persona_image_count": 1,
                        "persona_image_target": 1,
                        "image_status": "ready",
                        "image_providers": ["Pexels"],
                    },
                }
            )
            with patch.dict(
                os.environ,
                {"VOXQUARRY_LLM_API_KEY": "secret-dashscope", "PEXELS_API_KEY": "secret-pexels"},
                clear=True,
            ), patch(
                "reporting.app.QwenPersonaClient.generate",
                side_effect=AssertionError("validated Persona text must be reused"),
            ) as text_generation, patch(
                "reporting.persona_services.QwenPersonaImageClient.generate"
            ) as image_generation, patch(
                "reporting.app.search_pexels_image"
            ) as pexels, patch(
                "reporting.persona_services.urllib.request.urlopen"
            ) as network:
                saved_qwen_image = service.brand_personas.store_persona_image(
                    analysis_id=analysis_id,
                    persona_id="professional_operator",
                    asset_bytes=b"\x89PNG\r\n\x1a\nprevious-image",
                    mime_type="image/png",
                    provider="Qwen Image",
                    model="qwen-image-3.0-pro",
                    prompt_sha256="sha256:" + "8" * 64,
                )
                for saved_image in (validated_persona["image"], saved_qwen_image):
                    with self.subTest(provider=saved_image["provider"]):
                        current = service.brand_personas.get(analysis_id)
                        previous_persona = {**validated_persona, "image": saved_image}
                        service.brand_personas.update_personas(
                            analysis_id, [previous_persona], current["persona_narrative"]
                        )
                        result = service.generate_brand_personas(analysis_id)
                        self.assertEqual(result["personas"][0], previous_persona)
                        self.assertEqual(result["persona_narrative"]["status"], "qwen_validated")
                        self.assertEqual(result["persona_narrative"]["persona_image_count"], 1)
                        self.assertEqual(
                            result["persona_narrative"]["image_providers"],
                            [saved_image["provider"]],
                        )
                        persisted = service.brand_personas.get(analysis_id)
                        self.assertEqual(persisted["personas"][0], previous_persona)
                        self.assertEqual(persisted["persona_narrative"]["image_status"], "ready")
                path, mime_type = service.brand_personas.get_persona_image_asset(
                    saved_qwen_image["asset_sha256"]
                )
                self.assertEqual(path.read_bytes(), b"\x89PNG\r\n\x1a\nprevious-image")
                self.assertEqual(mime_type, "image/png")
            text_generation.assert_not_called()
            image_generation.assert_not_called()
            pexels.assert_not_called()
            network.assert_not_called()
            service.close()

    @unittest.skipIf(ReportingService is None, "FastAPI runtime dependency unavailable")
    def test_background_refresh_does_not_backfill_images_from_qwen_text_keys(self) -> None:
        analysis_id = "bpa_abcdef1234567890abcdef12"
        persona = {"persona_id": "professional_operator", "title": "专业作业者"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snapshot").mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "generated_at": "fixture"},
            ):
                service = ReportingService(root / "snapshot", root / "var")
            analysis = service.brand_personas.cache(
                {
                    "analysis_id": analysis_id,
                    "schema_version": "brand-persona-workbench/v3",
                    "snapshot_id": "snapshot-fixture",
                    "scope": {"brand": "EGO"},
                    "content_hash": "sha256:" + "9" * 64,
                    "deterministic_personas": [persona],
                    "personas": [persona],
                    "persona_narrative": {
                        "status": "qwen_validated",
                        "persona_image_count": 0,
                    },
                }
            )
            for key_name in ("VOXQUARRY_LLM_API_KEY", "VOXQUARRY_QWEN_API_KEY", "QWEN_API_KEY"):
                for purpose in ("custom", "featured_full"):
                    with self.subTest(key=key_name, purpose=purpose), patch.dict(
                        os.environ, {key_name: "secret-qwen"}, clear=True
                    ), patch.object(
                        service, "analyze_brand_persona", return_value=analysis
                    ), patch.object(
                        service, "generate_brand_personas"
                    ) as generation, patch(
                        "reporting.persona_services.urllib.request.urlopen"
                    ) as network:
                        job = service.brand_personas.create_job(
                            {"brand": "EGO"}, purpose=purpose
                        )
                        service._run_brand_persona_job(job["job_id"])
                        completed = service.brand_personas.get_job(job["job_id"])
                        self.assertEqual(completed["status"], "completed", completed)
                        generation.assert_not_called()
                        network.assert_not_called()
            service.close()

    def test_cache_preserves_validated_text_but_merges_new_image_metadata(self) -> None:
        analysis_id = "bpa_fedcba0987654321fedcba09"
        base = {
            "analysis_id": analysis_id,
            "schema_version": "brand-persona-workbench/v3",
            "cache_version": "fixture",
            "snapshot_id": "snapshot-fixture",
            "content_hash": "sha256:" + "9" * 64,
            "scope": {"brand": "EGO"},
            "personas": [
                {
                    "persona_id": "professional_operator",
                    "summary": "已经通过 claim-lock 的叙事。",
                    "image": {
                        "provider": "Pexels",
                        "image_url": "https://images.pexels.com/old.jpg",
                    },
                }
            ],
            "persona_narrative": {
                "status": "qwen_validated",
                "model": "deepseek-v4-flash-0731",
                "persona_image_count": 1,
                "image_providers": ["Pexels"],
            },
        }
        incoming = {
            **base,
            "personas": [
                {
                    "persona_id": "professional_operator",
                    "summary": "不能覆盖已验证叙事。",
                    "image": {
                        "provider": "Qwen Image",
                        "image_url": "/reporting/api/v1/new-image",
                        "asset_sha256": "sha256:" + "a" * 64,
                    },
                }
            ],
            "persona_narrative": {
                "status": "deterministic_fallback",
                "persona_image_count": 1,
                "persona_image_target": 1,
                "image_status": "ready",
                "image_providers": ["Qwen Image"],
                "images_are_illustrative": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            store = BrandPersonaStore(Path(directory))
            store.cache(base)
            result = store.cache(incoming)
            self.assertEqual(
                result["personas"][0]["summary"], "已经通过 claim-lock 的叙事。"
            )
            self.assertEqual(
                result["personas"][0]["image"]["provider"], "Qwen Image"
            )
            self.assertEqual(result["persona_narrative"]["status"], "qwen_validated")
            self.assertEqual(
                result["persona_narrative"]["image_providers"], ["Qwen Image"]
            )
            self.assertEqual(result["persona_narrative"]["image_status"], "ready")

    def test_qwen_retries_one_invalid_claim_id_without_weakening_lock(self) -> None:
        calls = []
        base = {
            "persona_id": "professional_operator",
            "name": "跨场地专业作业者",
            "summary": "以稳定完成任务和恢复能力评价工具。",
            "first_person_statement": "我要稳定完成任务，并能处理意外中断。",
            "who_they_are": "由明确的专业作业责任证据定义的群体。",
            "claim_ids": ["claim_professional"],
            "day_in_life": [
                {"stage": "准备", "narrative": "先确认任务边界。", "claim_ids": ["claim_professional"]},
                {"stage": "执行", "narrative": "持续验证完成质量。", "claim_ids": ["claim_professional"]},
            ],
            "journey": [
                {"stage": "选择", "narrative": "按任务标准比较产品。", "claim_ids": ["claim_professional"]},
                {"stage": "复用", "narrative": "根据恢复体验决定复用。", "claim_ids": ["claim_professional"]},
            ],
            "image_brief": {
                "portrait_query": "professional outdoor equipment operator portrait",
                "context_query": "professional outdoor equipment operator working",
                "avoid": ["brand logo"],
            },
        }

        def post_json(_url, _headers, body, _timeout):
            calls.append(body)
            row = json.loads(json.dumps(base))
            if len(calls) == 1:
                row["journey"][1]["claim_ids"] = ["invented_claim"]
            return {
                "choices": [{"message": {"content": json.dumps({"personas": [row]}, ensure_ascii=False)}}]
            }

        result = QwenPersonaClient(
            api_key="secret",
            base_url="https://example.test/v1",
            model="deepseek-v4-flash-0731",
            post_json=post_json,
            enable_thinking=True,
        ).generate(
            {
                "claim_professional": {
                    "claim_id": "claim_professional",
                    "persona_id": "professional_operator",
                    "evidence_ids": ["ev_1"],
                }
            }
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            result["personas"][0]["journey"][1]["claim_ids"],
            ["claim_professional"],
        )
        self.assertIn("不得新增", calls[1]["messages"][-1]["content"])

    def test_llm_thinking_environment_is_strict_and_optional(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOXQUARRY_LLM_ENABLE_THINKING", None)
            self.assertIsNone(
                optional_env_boolean("VOXQUARRY_LLM_ENABLE_THINKING")
            )
        for value in ("true", "TRUE", "1", "yes", "on"):
            with patch.dict(
                os.environ,
                {"VOXQUARRY_LLM_ENABLE_THINKING": value},
            ):
                self.assertIs(
                    optional_env_boolean("VOXQUARRY_LLM_ENABLE_THINKING"),
                    True,
                )
        for value in ("false", "FALSE", "0", "no", "off"):
            with patch.dict(
                os.environ,
                {"VOXQUARRY_LLM_ENABLE_THINKING": value},
            ):
                self.assertIs(
                    optional_env_boolean("VOXQUARRY_LLM_ENABLE_THINKING"),
                    False,
                )
        with patch.dict(
            os.environ,
            {"VOXQUARRY_LLM_ENABLE_THINKING": "sometimes"},
        ):
            with self.assertRaises(BrandPersonaContractError):
                optional_env_boolean("VOXQUARRY_LLM_ENABLE_THINKING")

    def test_runtime_store_keeps_full_evidence_out_of_public_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BrandPersonaStore(Path(directory))
            analysis = {
                "analysis_id": "bpa_1234567890abcdef12345678",
                "schema_version": "brand-persona-workbench/v1",
                "method_version": "brand-persona-deterministic/1.0.0",
                "cache_version": "brand-persona-cache/fixture",
                "snapshot_id": "snapshot-fixture",
                "content_hash": "sha256:fixture",
                "scope": {"brand": "SKIL"},
                "evidence": [{"evidence_id": "ev_1", "quote": "source quote"}],
                "personas": [],
            }
            cached = store.cache(analysis)
            self.assertNotIn("evidence", cached)
            self.assertEqual(cached["evidence_storage"]["evidence_rows"], 1)
            self.assertEqual(cached["evidence_storage"]["integrity_check"], "ok")
            evidence_page = store.query_evidence(
                analysis["analysis_id"], offset=0, limit=10
            )
            self.assertIsNotNone(evidence_page)
            self.assertEqual(evidence_page[1], 1)
            self.assertEqual(evidence_page[0][0]["quote"], "source quote")

            filtered = dict(analysis)
            filtered["analysis_id"] = "bpa_abcdefabcdefabcdefabcdef"
            filtered["content_hash"] = "sha256:filtered"
            filtered["evidence"] = [
                {
                    "evidence_id": "ev_driver_1",
                    "kind": "decision_driver",
                    "label": "performance_power",
                    "quote": "powerful mower",
                    "channel": "ego",
                    "market_region": "North America",
                },
                {
                    "evidence_id": "ev_system_1",
                    "kind": "experience_system",
                    "label": "battery_charger",
                    "quote": "slow charging",
                    "channel": "lowes",
                    "market_region": "North America",
                },
            ]
            store.cache(filtered)
            first_page = store.query_evidence(
                filtered["analysis_id"], offset=0, limit=1, kind="decision_driver"
            )
            second_page = store.query_evidence(
                filtered["analysis_id"], offset=1, limit=1
            )
            self.assertEqual(first_page[1], 1)
            self.assertEqual(first_page[0][0]["evidence_id"], "ev_driver_1")
            self.assertEqual(second_page[1], 2)
            self.assertEqual(second_page[0][0]["evidence_id"], "ev_system_1")
            public = public_brand_persona_analysis(cached)
            self.assertNotIn("evidence", public)
            self.assertEqual(public["evidence_access"]["evidence_rows"], 1)
            saved = store.save(analysis["analysis_id"])
            self.assertEqual(saved["artifact_type"], "brand_persona")
            self.assertEqual(store.save(analysis["analysis_id"])["saved_at"], saved["saved_at"])
            self.assertEqual(store.list_saved()[0]["artifact_id"], saved["artifact_id"])
            self.assertEqual(
                store.get_saved(saved["artifact_id"])["analysis_id"],
                analysis["analysis_id"],
            )

            enriched = dict(analysis)
            enriched["personas"] = [{"persona_id": "validated"}]
            enriched["persona_narrative"] = {"status": "qwen_validated"}
            store.cache(enriched)
            cached = store.cache(analysis)
            self.assertEqual(cached["personas"], [{"persona_id": "validated"}])
            self.assertEqual(
                store.get(analysis["analysis_id"])["personas"],
                [{"persona_id": "validated"}],
            )
            job = store.create_job({"brand": "SKIL"})
            self.assertEqual(job["status"], "queued")
            running = store.update_job(
                job["job_id"], status="running", stage="全量提取", progress=45
            )
            self.assertEqual(running["progress"], 45)
            self.assertEqual(store.list_jobs()[0]["job_id"], job["job_id"])
            store.bind_request(
                snapshot_id="snapshot-fixture",
                scope={"brand": "SKIL"},
                cache_version=analysis["cache_version"],
                analysis_id=analysis["analysis_id"],
            )
            self.assertEqual(
                store.get_for_request(
                    snapshot_id="snapshot-fixture",
                    scope={"brand": "SKIL"},
                    cache_version=analysis["cache_version"],
                )["analysis_id"],
                analysis["analysis_id"],
            )

    def test_cache_identity_changes_with_behavior_version(self) -> None:
        facts = [
            _fact(
                index,
                category_id="leaf_blower",
                category="Leaf Blower",
                model="LB7654",
                text="I use this blower every week and value power, runtime and easy handling.",
            )
            for index in range(320)
        ]
        scope = {"brand": "EGO", "job_ids": sorted({row["job_id"] for row in facts})}
        current = build_brand_persona_analysis(
            facts,
            scope,
            snapshot_id="snap_cache_identity",
        )
        with patch(
            "reporting.brand_persona.BRAND_PERSONA_CACHE_VERSION",
            BRAND_PERSONA_CACHE_VERSION + "|test-rule-upgrade",
        ):
            upgraded = build_brand_persona_analysis(
                facts,
                scope,
                snapshot_id="snap_cache_identity",
            )
        self.assertNotEqual(current["analysis_id"], upgraded["analysis_id"])
        self.assertNotEqual(current["content_hash"], upgraded["content_hash"])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_brand_persona_api_derives_jobs_and_pages_evidence(self) -> None:
        task = {
            "id": "mower-job",
            "brand": "EGO",
            "category_id": "walk_behind_mower",
            "category": "Walk-Behind Mower",
            "model": "LM2135SP",
            "channel": "ego",
            "market_region": "North America",
            "eligible_reviews": 1,
            "written_reviews": 1,
        }
        facts = [
            _fact(
                1,
                category_id="walk_behind_mower",
                category="Walk-Behind Mower",
                model="LM2135SP",
                text="I am a retired contractor and maintain rental properties.",
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [task], "generated_at": "fixture"},
            ), patch.dict(
                os.environ,
                {"VOXQUARRY_AGENT_TOOLS_ENABLED": "true"},
            ):
                app = create_app(snapshot, root / "var")
            quality = {
                "market_contract_mode": "current",
                "selected_job_ids": ["mower-job"],
                "excluded_latest_runs": 0,
                "duplicate_occurrences": 0,
            }
            with patch(
                "reporting.app.build_review_fact",
                return_value=(facts, quality, []),
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/reporting/api/v1/brand-persona/query",
                        json={"scope": {"brand": "EGO"}},
                    )
                    self.assertEqual(response.status_code, 200)
                    catalog_payload = client.get(
                        "/reporting/api/v1/brand-persona/catalog"
                    ).json()
                    self.assertIn(
                        catalog_payload["brands"][0]["logo_source"],
                        {"official_domain_favicon", "official_page_brand_asset"},
                    )
                    self.assertTrue(
                        catalog_payload["brands"][0]["logo_url"].startswith("https://")
                    )
                    analysis = response.json()
                    self.assertEqual(analysis["scope"]["job_ids"], ["mower-job"])
                    self.assertNotIn("evidence", analysis)
                    runtime_store = BrandPersonaStore(root / "var")
                    image = runtime_store.store_persona_image(
                        analysis_id=analysis["analysis_id"],
                        persona_id="professional_operator",
                        asset_bytes=b"\x89PNG\r\n\x1a\napi-fixture",
                        mime_type="image/png",
                        provider="Qwen Image",
                        model="qwen-image-3.0-pro",
                        prompt_sha256="sha256:" + "2" * 64,
                    )
                    runtime_store.update_personas(
                        analysis["analysis_id"],
                        [{"persona_id": "professional_operator", "image": image}],
                        {
                            "status": "qwen_validated",
                            "persona_image_count": 1,
                            "persona_image_target": 1,
                            "image_status": "ready",
                        },
                    )
                    image_response = client.get(image["image_url"])
                    self.assertEqual(image_response.status_code, 200)
                    self.assertEqual(image_response.headers["content-type"], "image/png")
                    self.assertIn("immutable", image_response.headers["cache-control"])
                    self.assertEqual(
                        image_response.content, b"\x89PNG\r\n\x1a\napi-fixture"
                    )
                    self.assertEqual(
                        client.get(
                            image["image_url"].replace(
                                "professional_operator", "invented_persona"
                            )
                        ).status_code,
                        404,
                    )
                    evidence = client.get(
                        "/reporting/api/v1/brand-persona/analyses/"
                        f"{analysis['analysis_id']}/evidence?limit=1"
                    )
                    self.assertEqual(evidence.status_code, 200)
                    self.assertEqual(evidence.json()["page"]["limit"], 1)
                    self.assertIn("facets", evidence.json())
                    exported = client.get(
                        "/reporting/api/v1/brand-persona/analyses/"
                        f"{analysis['analysis_id']}/evidence/export?format=ndjson"
                    )
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("application/x-ndjson", exported.headers["content-type"])
                    self.assertIn("evidence_id", exported.text)
                    tool = client.post(
                        "/reporting/api/v1/tools/get_brand_persona",
                        json={"analysis_id": analysis["analysis_id"]},
                    )
                    self.assertEqual(tool.status_code, 200)
                    self.assertEqual(tool.json()["analysis_id"], analysis["analysis_id"])
                    created = client.post(
                        "/reporting/api/v1/brand-persona/jobs",
                        json={"scope": {"brand": "EGO"}},
                    )
                    self.assertEqual(created.status_code, 200)
                    job = created.json()
                    self.assertEqual(job["status"], "queued")
                    for _attempt in range(100):
                        current = client.get(
                            "/reporting/api/v1/brand-persona/jobs/"
                            + job["job_id"]
                        ).json()
                        if current["status"] in {"completed", "failed"}:
                            break
                        time.sleep(0.02)
                    self.assertEqual(current["status"], "completed")
                    self.assertEqual(current["progress"], 100)
                    self.assertTrue(current["analysis_id"].startswith("bpa_"))

                    featured = client.post(
                        "/reporting/api/v1/brand-persona/featured/EGO/refresh?trigger=initial_backfill"
                    )
                    self.assertEqual(featured.status_code, 200)
                    featured_job = featured.json()
                    self.assertEqual(featured_job["purpose"], "featured_full")
                    self.assertEqual(featured_job["trigger"], "initial_backfill")
                    invalid_trigger = client.post(
                        "/reporting/api/v1/brand-persona/featured/EGO/refresh?trigger=unsafe"
                    )
                    self.assertEqual(invalid_trigger.status_code, 422)
                    for _attempt in range(100):
                        current = client.get(
                            "/reporting/api/v1/brand-persona/jobs/"
                            + featured_job["job_id"]
                        ).json()
                        if current["status"] in {"completed", "failed"}:
                            break
                        time.sleep(0.02)
                    self.assertEqual(current["status"], "completed")
                    featured_catalog = client.get(
                        "/reporting/api/v1/brand-persona/featured"
                    )
                    self.assertEqual(featured_catalog.status_code, 200)
                    item = featured_catalog.json()["brands"][0]
                    self.assertEqual(item["brand"], "EGO")
                    self.assertEqual(item["status"], "ready")
                    self.assertTrue(item["updated_at"])
                    loaded = client.get(
                        "/reporting/api/v1/brand-persona/featured/EGO"
                    )
                    self.assertEqual(loaded.status_code, 200)
                    self.assertEqual(
                        loaded.json()["analysis_id"], current["analysis_id"]
                    )

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_catalog_normalizes_missing_category_without_silent_drop(self) -> None:
        task = {
            "id": "unknown-job",
            "brand": "SKIL",
            "category_id": "",
            "category": "",
            "model": "TEST",
            "channel": "lowes",
            "market_region": "North America",
            "eligible_reviews": 12,
            "written_reviews": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [task], "generated_at": "fixture"},
            ):
                app = create_app(snapshot, root / "var")
            with TestClient(app) as client:
                catalog = client.get(
                    "/reporting/api/v1/brand-persona/catalog"
                ).json()
                self.assertEqual(catalog["brands"][0]["category_count"], 1)
                self.assertEqual(
                    catalog["tasks"][0]["category_id"], "unknown_category"
                )
                self.assertEqual(
                    catalog["tasks"][0]["category"], "Unknown Category"
                )


if __name__ == "__main__":
    unittest.main()
