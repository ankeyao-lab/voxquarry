from __future__ import annotations

import json
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.category_vocabulary import CATEGORY_DEFINITIONS
from reporting.factors import load_dictionary
from reporting.ope_html import build_html
from reporting.ope_product_report import (
    ASPECT_RULES,
    CATEGORY_PROFILES,
    DIMENSIONS,
    NUMERIC_DIMENSION_IDS,
    build_ope_product_report,
)
from reporting.smart_method import (
    SMART_METHOD_DEFINITIONS,
    SMART_METHOD_VERSION,
)
from reporting.tests.test_reporting import fixture_facts, manifest
from reporting.source import canonical_json, sha256_text


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def ope_facts() -> list[dict]:
    facts = fixture_facts()
    additions = [
        (
            " I am a 72 year old retired homeowner with back pain. "
            "I mow my half-acre Bermuda lawn every weekend. The hill has "
            "thick wet grass. Self propelled speed is too fast and dangerous."
        ),
        (
            " I switched from a gas mower. It is quiet, easy to start, "
            "and folds for vertical storage in my garage."
        ),
        (
            " I use two 40V 6Ah batteries. Battery life is terrible and "
            "I cannot finish my yard on one charge."
        ),
        (
            " The bag clogs in wet grass and side discharge is difficult. "
            "I wish the bag were easier to empty."
        ),
        (
            " The mower stopped working and customer service was awful. "
            "I need a useful diagnostic code in a smart app."
        ),
    ]
    for index, addition in enumerate(additions):
        facts[index]["review_text"] += addition
        facts[index]["has_text"] = True
        facts[index]["brand"] = "RYOBI"
    return facts


class OpeProductReportTests(unittest.TestCase):
    def test_smart_method_v2_matches_the_approved_product_method(self) -> None:
        definitions = {
            row["code"]: row for row in SMART_METHOD_DEFINITIONS
        }
        self.assertEqual(SMART_METHOD_VERSION, "smart-opportunity-method/2.0.0")
        self.assertEqual(list(definitions), [f"A{index}" for index in range(1, 9)])
        self.assertIn("反应、感知或判断上的盲区", definitions["A1"]["core"])
        self.assertIn("具体的恨点", definitions["A2"]["core"])
        self.assertIn("消除不确定性", definitions["A3"]["core"])
        self.assertEqual(definitions["A4"]["priority"], "first_priority")
        self.assertIn("机器适应人", definitions["A5"]["core"])
        self.assertIn("自己变成的那个人", definitions["A6"]["core"])
        self.assertIn("状态已经变好了", definitions["A7"]["core"])
        self.assertIn("工具→助手→管家", definitions["A8"]["core"])
        smart_signals = {
            row["id"]: row for row in load_dictionary()["smart_signals"]
        }
        self.assertEqual(smart_signals["predictive_service"]["framework"], "A1")
        self.assertEqual(smart_signals["runtime_guidance"]["framework"], "A3")
        self.assertEqual(smart_signals["adaptive_load"]["framework"], "A4")
        self.assertEqual(smart_signals["personalized_mode"]["framework"], "A8")

    def test_all_snapshot_categories_have_versioned_profiles(self) -> None:
        expected = {
            definition.category_id
            for definition in CATEGORY_DEFINITIONS
        }
        self.assertEqual(set(CATEGORY_PROFILES), expected)
        self.assertTrue(
            all(profile.profile_id for profile in CATEGORY_PROFILES.values())
        )
        self.assertTrue(
            all(profile.hero_title for profile in CATEGORY_PROFILES.values())
        )
        self.assertGreaterEqual(
            len(
                {
                    profile.hero_title
                    for profile in CATEGORY_PROFILES.values()
                }
            ),
            12,
        )

    def test_audience_identity_excludes_task_and_purchase_relationships(
        self,
    ) -> None:
        facts = fixture_facts()
        for fact in facts:
            fact["review_title"] = ""
            fact["review_text"] = "The product works as expected."
            fact["has_text"] = True
            fact["age"] = ""
        facts[0]["review_text"] = (
            "I am a professional landscaper and use this for my clients."
        )
        facts[1]["review_text"] = (
            "I bought this for my wife and we both use it."
        )
        facts[2]["review_text"] = "I use it in my yard every weekend."
        facts[3]["review_text"] = (
            "As an engineer, I appreciate the design validation."
        )
        facts[4]["review_text"] = (
            "I do not have time to spend all weekend acting like "
            "I'm an electrician."
        )
        facts[5]["review_text"] = (
            "I do not think you could use this as a commercial landscaper."
        )
        facts[6]["review_text"] = (
            "I use it every day in my job as a gardener."
        )
        facts[7]["review_text"] = (
            "I bought this to mow my own lawn every weekend."
        )
        facts[8]["review_text"] = (
            "I am still waiting for customer service to call back."
        )
        source_report = build_report(
            facts,
            manifest(facts),
            report_name="Audience identity fixture",
        )
        report, _ledger = build_ope_product_report(facts, source_report)
        dimensions = {
            row["dimension_id"]: row
            for row in report["dimension_census"]
        }
        occupation = dimensions["occupation"]
        relationship = dimensions["purchase_relationship"]
        responsibility = dimensions["operating_responsibility"]
        self.assertEqual(occupation["review_entities"], 3)
        self.assertEqual(relationship["review_entities"], 1)
        self.assertEqual(responsibility["review_entities"], 2)
        self.assertEqual(occupation["unknown_entities"], 7)
        occupation_values = {
            value["value_id"]: value["review_entities"]
            for value in occupation["values"]
        }
        self.assertEqual(
            occupation_values["engineering_technical_professional"],
            1,
        )
        self.assertEqual(
            occupation_values["landscape_tree_professional"],
            2,
        )
        self.assertNotIn(
            "家庭成员",
            " ".join(value["label"] for value in occupation["values"]),
        )
        self.assertTrue(
            all(
                not row["persona_id"].startswith(
                    (
                        "task_context_",
                        "material_context_",
                        "workflow_context_",
                    )
                )
                for row in report["personas"]
            )
        )
        self.assertIn(
            "explicit_professional_operator",
            {row["persona_id"] for row in report["personas"]},
        )

    def test_full_sample_dimensions_and_evidence_are_reproducible(self) -> None:
        facts = ope_facts()
        source_report = build_report(
            facts,
            manifest(facts),
            report_name="OPE fixture",
        )
        first, first_ledger = build_ope_product_report(
            facts, source_report
        )
        second, second_ledger = build_ope_product_report(
            facts, source_report
        )
        written = sum(bool(fact["has_text"]) for fact in facts)
        extractor_count = (
            len(DIMENSIONS) + len(NUMERIC_DIMENSION_IDS) + len(ASPECT_RULES)
        )

        self.assertEqual(
            len(first_ledger),
            written * extractor_count,
        )
        self.assertEqual(first_ledger, second_ledger)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(
            first["content_hash"],
            "sha256:" + sha256_text(canonical_json({
                key: value for key, value in first.items()
                if key != "content_hash"
            })),
        )
        self.assertEqual(first["coverage"]["failed_executions"], 0)
        self.assertEqual(first["coverage"]["extractors"], extractor_count)
        self.assertFalse(first["method"]["third_party_llm_api_used"])
        self.assertFalse(first["method"]["core_metrics_modified_by_llm"])

        dimension_ids = {
            row["dimension_id"] for row in first["dimension_census"]
        }
        self.assertIn("grass_species", dimension_ids)
        self.assertIn("yard_size", dimension_ids)
        self.assertIn("cut_height", dimension_ids)
        self.assertIn("battery_capacity", dimension_ids)
        self.assertEqual(len(first["kano"]), 4)
        # A1–A8 are a vocabulary, not an eight-card checklist. A profile may
        # surface multiple evidence-backed rows for one type and omit a type
        # without a usable source problem pool.
        self.assertGreater(len(first["smart_opportunities"]), 0)
        self.assertLessEqual(len(first["smart_opportunities"]), 16)
        self.assertEqual(
            first["smart_methodology"]["version"],
            SMART_METHOD_VERSION,
        )
        self.assertTrue(first["sample"]["interpretation"])
        for row in first["smart_opportunities"]:
            self.assertEqual(
                row["methodology_version"], SMART_METHOD_VERSION
            )
            self.assertIn(row["code"], {f"A{index}" for index in range(1, 9)})

        smart_by_code = {
            row["code"]: row for row in first["smart_opportunities"]
        }
        if "A2" in smart_by_code:
            self.assertNotIn("充电 / 换电", smart_by_code["A2"]["title"])
        if "A3" in smart_by_code:
            self.assertIn("完成", smart_by_code["A3"]["title"])
        if "A4" in smart_by_code:
            self.assertIn("自适应", smart_by_code["A4"]["title"])
        self.assertGreater(len(first["evidence"]), 0)
        evidence_by_id = {
            row["evidence_id"]: row for row in first["evidence"]
        }
        for persona in first["personas"]:
            self.assertTrue(
                all(
                    evidence_by_id[evidence_id]["kind"].startswith(
                        "dimension:"
                    )
                    for evidence_id in persona["evidence_ids"]
                )
            )

    def test_html_is_single_file_utf8_responsive_and_interactive(self) -> None:
        facts = ope_facts()
        source_report = build_report(
            facts,
            manifest(facts),
            report_name="OPE HTML fixture",
        )
        report, _ledger = build_ope_product_report(facts, source_report)
        rendered = build_html(report)

        self.assertIn('<meta charset="utf-8">', rendered)
        self.assertIn('name="viewport"', rendered)
        self.assertIn('id="profile"', rendered)
        self.assertIn('id="scenario"', rendered)
        self.assertIn('id="experience"', rendered)
        self.assertIn('id="kano"', rendered)
        self.assertIn('id="opportunity"', rendered)
        self.assertIn('id="smart"', rendered)
        self.assertIn('id="evidence-dialog"', rendered)
        self.assertIn("@media(max-width:760px)", rendered)
        self.assertNotIn("<script src=", rendered)
        self.assertNotIn("<link rel=\"stylesheet\"", rendered)

    def test_source_age_and_evidence_diversity_are_auditable(self) -> None:
        facts = ope_facts()
        age_values = ["65orOver", "55to64", "45to54", "35to44", "25to34"]
        for fact, age in zip(facts, age_values):
            fact["brand"] = "EGO"
            fact["age"] = age
        source_report = build_report(
            facts,
            manifest(facts),
            report_name="EGO OPE fixture",
        )
        source_report["scope"]["brands"] = ["EGO"]
        report, _ledger = build_ope_product_report(facts, source_report)

        age = next(
            row
            for row in report["dimension_census"]
            if row["dimension_id"] == "explicit_age"
        )
        source_evidence = [
            row
            for row in report["evidence"]
            if row.get("evidence_type") == "source_field"
        ]
        self.assertEqual(report["brand_label"], "EGO")
        self.assertGreaterEqual(age["review_entities"], 5)
        self.assertEqual(len(source_evidence), 5)
        self.assertTrue(
            all(not row["is_original_review_quote"] for row in source_evidence)
        )
        evidence_by_id = {
            row["evidence_id"]: row for row in report["evidence"]
        }
        for dimension in report["dimension_census"]:
            for value in dimension["values"]:
                for evidence_id in value["evidence_ids"]:
                    self.assertEqual(
                        evidence_by_id[evidence_id]["label"],
                        value["value_id"],
                    )

        for aspect in report["aspect_experience"]:
            for key in ("positive_evidence_ids", "negative_evidence_ids"):
                selected = [
                    evidence_by_id[value] for value in aspect[key]
                ]
                entities = [
                    row["review_entity_id"]
                    for row in selected
                    if row.get("review_entity_id")
                ]
                self.assertEqual(len(entities), len(set(entities)))

        self.assertIn(
            "evidence-diversity/4.1.0",
            report["evidence_audit"]["selection_version"],
        )
        self.assertEqual(
            report["evidence_audit"]["within_group_duplicate_entities"], 0
        )
        self.assertEqual(
            report["evidence_audit"]["within_group_duplicate_exact_quotes"], 0
        )
        rendered = build_html(report)
        self.assertIn("<title>EGO Mower", rendered)
        self.assertIn("结构化来源字段", rendered)

    def test_blower_profile_uses_category_specific_dimensions_and_aspects(
        self,
    ) -> None:
        facts = fixture_facts()
        blower_text = [
            (
                "I clear wet leaves from a long driveway. Turbo has great power. "
                "The warranty period starts on the purchase date. "
                "The string trimmer head stopped starting."
            ),
            (
                "The battery dies halfway through fall cleanup and needs charging. "
                "This blower won't start."
            ),
            "This blower is too heavy and my arm gets tired after 30 minutes.",
            "Variable speed helps around flower beds without blowing mulch everywhere.",
            "The concentrator nozzle moves pine needles but is hard to attach.",
        ]
        for index, fact in enumerate(facts):
            fact["category"] = "吹风机"
            fact["model"] = "LB7654"
            fact["review_text"] = blower_text[index % len(blower_text)]
            fact["has_text"] = True
        source_manifest = manifest(facts)
        source_manifest["scope"] = {
            **source_manifest["scope"],
            "categories": ["吹风机"],
            "exclude_categories": [],
        }
        source_report = build_report(
            facts,
            source_manifest,
            report_name="EGO blower fixture",
        )
        report, ledger, groups = build_ope_product_report(
            facts,
            source_report,
            include_evidence_groups=True,
        )
        dimension_ids = {
            row["dimension_id"] for row in report["dimension_census"]
        }
        aspect_ids = {
            row["aspect_id"] for row in report["aspect_experience"]
        }
        self.assertEqual(report["category_profile_id"], "blower/1.2.0")
        self.assertIn("debris_type", dimension_ids)
        self.assertIn("work_area", dimension_ids)
        self.assertNotIn("grass_species", dimension_ids)
        self.assertNotIn("cut_height", dimension_ids)
        self.assertIn("blowing_power", aspect_ids)
        self.assertIn("weight_balance", aspect_ids)
        self.assertNotIn("cut_quality", aspect_ids)
        starting = next(
            row
            for row in report["aspect_experience"]
            if row["aspect_id"] == "starting"
        )
        self.assertGreaterEqual(starting["review_entities"], 1)
        starting_quotes = [
            row["quote"]
            for row in report["evidence"]
            if row["kind"] == "aspect:starting"
        ]
        self.assertTrue(
            all("warranty period starts" not in quote for quote in starting_quotes)
        )
        self.assertTrue(
            all("string trimmer" not in quote for quote in starting_quotes)
        )
        self.assertEqual(
            len(ledger),
            len(facts) * report["coverage"]["extractors"],
        )
        self.assertGreater(len(groups), 0)
        self.assertTrue(
            all(
                row.get("evidence_group_id")
                for row in report["product_opportunities"]
                if row.get("evidence_ids")
            )
        )
        golden = json.loads(
            (
                FIXTURE_DIR / "golden-blower-profile-summary-v1.json"
            ).read_text(encoding="utf-8")
        )
        summary = {
            "schema_version": report["schema_version"],
            "category_profile_id": report["category_profile_id"],
            "analysis_version": report["analysis_version"],
            "evidence_selection_version": report[
                "evidence_selection_version"
            ],
            "extractors": report["coverage"]["extractors"],
            "entity_extractor_executions": report["coverage"][
                "entity_extractor_executions"
            ],
            "dimension_ids": [
                row["dimension_id"] for row in report["dimension_census"]
            ],
            "aspect_ids": [
                row["aspect_id"] for row in report["aspect_experience"]
            ],
            "persona_ids": [
                row["persona_id"] for row in report["personas"]
            ],
            "kano_categories": [
                row["category_id"] for row in report["kano"]
            ],
            "opportunity_ids": [
                row["opportunity_id"]
                for row in report["product_opportunities"]
            ],
            "smart_codes": [
                row["code"] for row in report["smart_opportunities"]
            ],
            "evidence_groups": len(groups),
            "evidence_reference_slots": report["evidence_audit"][
                "evidence_reference_slots"
            ],
            "failed_executions": report["coverage"]["failed_executions"],
        }
        self.assertEqual(summary, golden)


if __name__ == "__main__":
    unittest.main()
