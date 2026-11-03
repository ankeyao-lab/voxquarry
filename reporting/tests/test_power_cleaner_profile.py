from __future__ import annotations

import json
import re
from pathlib import Path
import unittest

from reporting.analysis import build_report
from reporting.category_vocabulary import category_catalog, category_id_for
from reporting.contracts import default_scope, freeze_selected_jobs
from reporting.ope_product_report import category_profile
from reporting.product_insight import build_product_insight_bundle
from reporting.tests.test_reporting import fixture_facts, manifest


class PowerCleanerProfileTests(unittest.TestCase):
    def test_power_cleaner_has_stable_identity_and_narrow_legacy_aliases(
        self,
    ) -> None:
        self.assertEqual(category_id_for("Power Cleaner"), "power_cleaner")
        self.assertEqual(
            category_id_for("Cordless Power Washer"), "power_cleaner"
        )
        self.assertEqual(
            category_id_for("Cordless Power Washers"), "power_cleaner"
        )
        # This exact mapping is only compatibility for a historical task
        # category label.  It is not PDP-title inference and must never be
        # redirected to the new Power Cleaner identity.
        self.assertEqual(category_id_for("Power Washer"), "pressure_washer")
        self.assertNotEqual(
            category_id_for("Power Cleaner"),
            category_id_for("Power Scrubber"),
        )
        self.assertNotEqual(
            category_id_for("Power Cleaner"),
            category_id_for("Patio Cleaner"),
        )
        for unsafe_identity in (
            "Cordless Pressure Washer",
            "Cleaner",
            "Cordless",
            "EZClean",
            "DCPW550",
            "RY18PW",
            "HYPRESSO 18/24",
        ):
            with self.subTest(identity=unsafe_identity):
                self.assertNotEqual(
                    category_id_for(unsafe_identity), "power_cleaner"
                )

    def test_power_cleaner_profile_is_specific_and_traceable(self) -> None:
        profile = category_profile("power_cleaner")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.profile_id, "power-cleaner/1.0.0")
        self.assertEqual(profile.category_label, "Power Cleaner")
        self.assertIn("便携", profile.hero_title)

        text = (
            "I used the compact power cleaner to rinse mud and road salt "
            "from my truck at the trailhead. The siphon hose pulled water "
            "from a bucket, but it took time to prime. The multi-pattern "
            "nozzle was gentle on paint, the connector leaked, and one "
            "battery finished the quick cleanup."
        )
        scenario_rules = [
            rule
            for dimension in profile.dimensions
            if dimension.dimension_id
            in {"task_context", "material_context", "workflow_context"}
            for rule in dimension.rules
        ]
        self.assertGreaterEqual(
            sum(
                bool(re.search(rule.expression, text, re.IGNORECASE))
                for rule in scenario_rules
            ),
            5,
        )
        self.assertGreaterEqual(
            sum(
                bool(re.search(rule.expression, text, re.IGNORECASE))
                for rule in profile.aspect_rules
            ),
            5,
        )

        facts = fixture_facts()
        for fact in facts:
            fact["category"] = "Power Cleaner"
            fact["review_text"] = f"{text} {fact['review_text']}"
            fact["has_text"] = True
        source_manifest = manifest(facts)
        source_manifest["scope"] = {
            **source_manifest["scope"],
            "category_id": "power_cleaner",
            "categories": ["Power Cleaner"],
            "exclude_categories": [],
        }
        source = build_report(facts, source_manifest)
        report, evidence, groups, ledger = build_product_insight_bundle(
            facts, source
        )
        self.assertEqual(
            report["product_insight"]["category_profile_id"],
            "power-cleaner/1.0.0",
        )
        payload = json.dumps(report["product_insight"], ensure_ascii=False)
        self.assertIn("便携取水", payload)
        self.assertNotIn("Power Scrubber任务", payload)
        self.assertNotIn("Pressure Washer任务", payload)
        self.assertTrue(evidence)
        self.assertTrue(groups)
        self.assertTrue(ledger)

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_batch_generation_freezes_resolved_jobs_in_public_scope(
        self,
    ) -> None:
        request = {
            "report_name": "VoxQuarry｜Power Cleaner 量化洞察报告",
            "scope": {
                **default_scope(),
                "category_id": "power_cleaner",
                "categories": ["Power Cleaner"],
            },
        }
        self.assertEqual(request["scope"]["job_ids"], [])
        frozen = freeze_selected_jobs(
            request,
            ["job-b", "job-a", "job-b"],
        )
        self.assertEqual(
            frozen["scope"]["job_ids"],
            ["job-b", "job-a"],
        )
        self.assertEqual(request["scope"]["job_ids"], [])


if __name__ == "__main__":
    unittest.main()
