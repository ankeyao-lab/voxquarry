from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.atomic_v2 import run_atomic_scripts
from reporting.insight_bundle import (
    build_insight_bundle,
    validate_insight_bundle,
)
from reporting.scene_analysis import build_scene_analysis
from reporting.stakeholder_artifact import build_stakeholder_artifact
from reporting.tests.test_reporting import fixture_facts, manifest


def stakeholder_facts() -> list[dict]:
    facts = fixture_facts()
    additions = [
        (
            " I am a 70 year old homeowner with limited strength. "
            "I mow my half acre hill. I need self propelled speed and easy "
            "lightweight maneuvering. Assembly was easy, quiet, and "
            "worth the price. "
            "I am very impressed and recommend it."
        ),
        (
            " I need more runtime, power, battery charging, bagging and "
            "mulching. Bluetooth connectivity and automatic controls "
            "would help."
        ),
        (
            " The mower stopped working, broke, and keeps stalling. "
            "I tried a repair, called warranty service, and returned it."
        ),
    ]
    for index, addition in enumerate(additions):
        facts[index]["review_text"] += addition
        facts[index]["has_text"] = True
    for index, fact in enumerate(facts):
        fact["channel"] = "homedepot" if index < 5 else "ryobi"
        fact["brand"] = "RYOBI"
    return facts


class StakeholderReportTests(unittest.TestCase):
    def test_scene_insights_and_artifact_remain_evidence_bound(self) -> None:
        facts = stakeholder_facts()
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            atomic = run_atomic_scripts(
                facts,
                snapshot_id="snap_stakeholder_fixture",
                output_root=output_root,
            )
            ledger_path = output_root / atomic["ledger"]["relative_path"]
            with gzip.open(
                ledger_path, "rt", encoding="utf-8"
            ) as handle:
                ledger_rows = [json.loads(line) for line in handle]

        report = build_report(
            facts,
            manifest(facts),
            report_name="Stakeholder fixture",
        )
        scene = build_scene_analysis(facts, ledger_rows, report)
        evidence_ids = {
            item["evidence_id"] for item in scene["evidence"]
        }
        self.assertEqual(
            scene["written_review_entities"],
            sum(fact["has_text"] for fact in facts),
        )
        self.assertFalse(scene["method"]["llm_used"])
        self.assertTrue(
            all(
                set(row["evidence_ids"]).issubset(evidence_ids)
                for collection in (
                    "profiles",
                    "usage_scenarios",
                    "attribute_experience",
                    "failure_chains",
                    "needs_by_attribute",
                    "product_opportunities",
                    "explicit_smart_signals",
                    "smart_hypotheses",
                )
                for row in scene[collection]
            )
        )

        bundle = build_insight_bundle(report, scene)
        validate_insight_bundle(bundle, report, scene)
        self.assertTrue(bundle["generator"]["llm_used"])
        self.assertFalse(bundle["generator"]["third_party_api_used"])
        self.assertFalse(bundle["generator"]["core_metrics_modified"])

        artifact = build_stakeholder_artifact(report, scene, bundle)
        datasets = artifact["snapshot"]["datasets"]
        self.assertEqual(len(datasets["script_insights"]), 28)
        self.assertEqual(len(datasets["script_execution"]), 28)
        self.assertGreater(len(datasets["insights"]), 0)
        self.assertTrue(
            all(
                isinstance(value, (str, int, float, bool, type(None)))
                for rows in datasets.values()
                for row in rows
                for value in row.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
