from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.evidence_store import EvidenceRepository
from reporting.product_insight import (
    build_product_insight_bundle,
    product_insight_support,
    report_readiness_status,
    write_extraction_ledger,
)
from reporting.ope_product_report import CATEGORY_PROFILES, category_profile
from reporting.tests.test_reporting import fixture_facts, manifest
from reporting.validation import validate_report


class ProductInsightBundleTests(unittest.TestCase):
    def test_production_readiness_thresholds_are_deterministic(self) -> None:
        cases = {
            0: "no_data",
            1: "exploratory",
            499: "exploratory",
            500: "pilot",
            999: "pilot",
            1000: "limited",
            2999: "limited",
            3000: "full",
        }
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(report_readiness_status(count), expected)
                support = product_insight_support(
                    {
                        "scope": {
                            "category_id": "walk_behind_mower",
                            "categories": ["Walk behind Lawnmower"],
                        }
                    },
                    valid_text_reviews=count,
                )
                self.assertEqual(support["status"], expected)
                self.assertEqual(
                    support["can_generate"],
                    expected in {"full", "limited", "pilot", "exploratory"},
                )

    def test_production_gate_blocks_sub_pilot_scope(self) -> None:
        facts = fixture_facts()
        source = build_report(
            facts,
            manifest(facts),
            report_name="Sub-pilot fixture",
        )
        bundle, *_ = build_product_insight_bundle(facts, source, enforce_readiness=True)
        self.assertEqual(source["product_insight_availability"]["status"], "exploratory")
        self.assertTrue(any(x["code"] == "report_readiness_exploratory" for x in source["limitations"]))

    def test_full_profile_is_embedded_and_full_evidence_is_sidecar_ready(
        self,
    ) -> None:
        facts = fixture_facts()
        facts[0]["review_entity_id"] = None
        source = build_report(
            facts,
            manifest(facts),
            report_name="Mower insight fixture",
        )
        source_id = source["report_id"]
        report, evidence, groups, ledger = build_product_insight_bundle(
            facts, source
        )
        self.assertNotEqual(report["report_id"], source_id)
        self.assertEqual(
            report["product_insight_availability"]["status"], "full"
        )
        self.assertLess(
            len(report["product_insight"]["evidence"]),
            len(evidence) + 1,
        )
        self.assertEqual(
            report["product_insight"]["evidence_access"]["evidence_rows"],
            len(evidence),
        )
        self.assertGreater(len(groups), 0)
        self.assertEqual(
            report["product_insight"]["evidence_audit"]["evidence_groups"],
            len(groups),
        )
        self.assertGreaterEqual(
            report["product_insight"]["evidence_audit"][
                "evidence_reference_slots"
            ],
            len(groups),
        )
        evidence_entries = []

        def collect_entries(value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "evidence":
                        continue
                    if key.endswith("evidence_ids") and isinstance(item, list):
                        prefix = key[: -len("evidence_ids")]
                        evidence_entries.append(
                            {
                                "ids": list(dict.fromkeys(item)),
                                "group_id": value.get(
                                    f"{prefix}evidence_group_id"
                                ),
                                "full_rows": value.get(
                                    f"{prefix}full_evidence_rows"
                                ),
                                "full_entities": value.get(
                                    f"{prefix}full_evidence_entities"
                                ),
                            }
                        )
                    else:
                        collect_entries(item)
            elif isinstance(value, list):
                for item in value:
                    collect_entries(item)

        collect_entries(report["product_insight"])
        populated_entries = [
            entry for entry in evidence_entries if entry["ids"]
        ]
        self.assertGreater(len(populated_entries), 0)
        for entry in populated_entries:
            self.assertTrue(entry["group_id"])
            self.assertGreaterEqual(
                entry["full_rows"],
                len(entry["ids"]),
            )
            self.assertGreater(entry["full_entities"], 0)
        self.assertGreater(len(ledger), 0)
        validate_report(report)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = EvidenceRepository(root)
            repository.store(
                report["report_id"],
                evidence,
                groups,
                expected_manifest=report["product_insight"][
                    "evidence_access"
                ],
            )
            group_id = next(iter(groups))
            page = repository.query(
                report["report_id"], group_id, limit=50
            )
            self.assertGreater(page["page"]["total_evidence_rows"], 0)
            catalog = {
                row["evidence_group_id"]: row
                for row in report["product_insight"][
                    "evidence_group_catalog"
                ]
            }
            for catalog_group_id, metadata in catalog.items():
                stored_group = repository.group(
                    report["report_id"], catalog_group_id
                )
                self.assertEqual(
                    metadata["full_evidence_rows"],
                    stored_group["evidence_rows"],
                )
                self.assertEqual(
                    metadata["full_evidence_entities"],
                    stored_group["unique_review_entities"],
                )
            artifact = write_extraction_ledger(
                root, report["report_id"], ledger
            )
            self.assertEqual(artifact["rows"], len(ledger))
            with gzip.open(artifact["path"], "rt", encoding="utf-8") as handle:
                self.assertEqual(sum(1 for _line in handle), len(ledger))

    def test_new_category_profile_produces_full_report(self) -> None:
        facts = fixture_facts()
        for fact in facts:
            fact["category"] = "Snow Blower"
        source_manifest = manifest(facts)
        source_manifest["scope"] = {
            **source_manifest["scope"],
            "categories": ["Snow Blower"],
            "exclude_categories": [],
        }
        source = build_report(facts, source_manifest)
        report, evidence, groups, ledger = build_product_insight_bundle(
            facts, source
        )
        self.assertEqual(
            report["product_insight_availability"]["status"], "full"
        )
        self.assertEqual(
            report["product_insight"]["category_profile_id"],
            "snow-blower/1.2.0",
        )
        self.assertEqual(
            report["product_insight"]["category_label"],
            "Snow Blower",
        )
        self.assertGreater(len(evidence), 0)
        self.assertGreater(len(groups), 0)
        self.assertGreater(len(ledger), 0)
        validate_report(report)

    def test_all_stable_category_profiles_are_registered(self) -> None:
        self.assertEqual(len(CATEGORY_PROFILES), 89)
        expected = {
            "Air Compressor": (
                "air_compressor",
                "air-compressor/1.0.0",
            ),
            "Battery Pack": ("battery_pack", "battery-pack/1.0.0"),
            "Charger": ("charger", "charger/1.0.0"),
            "Cordless Handheld Vacuum": (
                "cordless_handheld_vacuum",
                "handheld-vacuum/1.1.0",
            ),
            "Cordless Outdoor Cleaner": (
                "cordless_outdoor_cleaner",
                "outdoor-cleaner/1.0.0",
            ),
            "Cordless Stick Vacuum": (
                "cordless_stick_vacuum",
                "cordless-stick-vacuum/1.0.0",
            ),
            "Cooler": ("cooler", "cooler/1.0.0"),
            "Hard Floor Cleaner": (
                "hard_floor_cleaner",
                "hard-floor-cleaner/1.0.0",
            ),
            "Inflator": ("inflator", "inflator/1.0.0"),
            "Jobsite Fan": ("fan", "fan/1.0.0"),
            "LED Lighting": ("led_lighting", "led-lighting/1.0.0"),
            "Power Scrubber": (
                "power_scrubber",
                "power-scrubber/1.0.0",
            ),
            "Power Cleaner": (
                "power_cleaner",
                "power-cleaner/1.0.0",
            ),
            "Radio/Speaker": (
                "radio_speaker",
                "radio-speaker/1.0.0",
            ),
            "Snow Shovel": ("snow_shovel", "snow-shovel/1.0.0"),
            "Spot Cleaner": ("spot_cleaner", "spot-cleaner/1.0.0"),
            "Wet/Dry Vacuum": (
                "wet_dry_vacuum",
                "wet-dry-vacuum/1.0.0",
            ),
        }
        for category, (category_id, profile_id) in expected.items():
            with self.subTest(category=category):
                profile = category_profile(category)
                self.assertIsNotNone(profile)
                self.assertEqual(profile.category_id, category_id)
                self.assertEqual(profile.profile_id, profile_id)
                self.assertEqual(
                    profile.category_label,
                    "Fan" if category == "Jobsite Fan" else "Handheld Vacuum" if category == "Cordless Handheld Vacuum" else category,
                )
                self.assertTrue(profile.hero_title)
                self.assertTrue(
                    {
                        "task_context",
                        "material_context",
                        "workflow_context",
                    }.issubset(
                        {
                            dimension.dimension_id
                            for dimension in profile.dimensions
                        }
                    )
                )
                self.assertGreaterEqual(len(profile.aspect_rules), 8)

    def test_seven_new_profiles_build_full_reports(self) -> None:
        cases = {
            "Cordless Handheld Vacuum": (
                "cordless_handheld_vacuum",
                "I use this handheld vacuum in my car. Strong suction "
                "picks up pet hair and crumbs, and the crevice tool reaches "
                "between the seats.",
            ),
            "Cordless Outdoor Cleaner": (
                "cordless_outdoor_cleaner",
                "I draw water from a bucket to wash mud from my truck. "
                "The nozzle pressure and portable setup work well.",
            ),
            "Inflator": (
                "inflator",
                "I preset 35 psi for my car tire. The gauge is accurate, "
                "inflation is fast, and automatic shutoff works.",
            ),
            "Jobsite Fan": (
                "fan",
                "This jobsite fan moves strong airflow through my workshop. "
                "The speed control is quiet and the battery runtime is great.",
            ),
            "LED Lighting": (
                "led_lighting",
                "The LED work light is bright across my garage. The tripod "
                "is stable, the head pivots, and battery runtime is excellent.",
            ),
            "Snow Shovel": (
                "snow_shovel",
                "This snow shovel clears eight inches from my sidewalk and "
                "throws powder snow well without clogging.",
            ),
            "Wet/Dry Vacuum": (
                "wet_dry_vacuum",
                "The wet dry vacuum has strong suction for sawdust and picks "
                "up spilled water. The hose and drain plug work well.",
            ),
        }
        for category, (category_id, review_text) in cases.items():
            with self.subTest(category=category):
                facts = fixture_facts()
                for index, fact in enumerate(facts):
                    fact["category_id"] = category_id
                    fact["category"] = category
                    fact["review_text"] = review_text
                    fact["review_title"] = category
                    fact["has_text"] = True
                    fact["review_entity_id"] = (
                        f"{category_id}-fixture-{index}"
                    )
                source_manifest = manifest(facts)
                source_manifest["scope"] = {
                    **source_manifest["scope"],
                    "category_id": category_id,
                    "categories": [category],
                    "exclude_categories": [],
                }
                source = build_report(facts, source_manifest)
                report, evidence, groups, ledger = (
                    build_product_insight_bundle(facts, source)
                )
                self.assertEqual(
                    report["product_insight_availability"]["category_id"],
                    category_id,
                )
                self.assertEqual(
                    report["product_insight"]["category_id"],
                    category_id,
                )
                self.assertGreater(len(evidence), 0)
                self.assertGreater(len(groups), 0)
                self.assertGreater(len(ledger), 0)
                validate_report(report)

    def test_unknown_category_is_blocked_without_degraded_report(self) -> None:
        facts = fixture_facts()
        for fact in facts:
            fact["category"] = "未注册测试品类"
        source_manifest = manifest(facts)
        source_manifest["scope"] = {
            **source_manifest["scope"],
            "categories": ["未注册测试品类"],
            "exclude_categories": [],
        }
        source = build_report(facts, source_manifest)
        with self.assertRaisesRegex(ValueError, "只生成完整报告"):
            build_product_insight_bundle(facts, source)


if __name__ == "__main__":
    unittest.main()
