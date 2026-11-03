import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from reporting.atomic_v2 import SCRIPT_REGISTRY, registry_manifest
from reporting.ope_product_report import CATEGORY_PROFILES
from reporting.product_insight import REPORT_READINESS_THRESHOLDS
from reporting.research_definition import get_research_definition
from reporting.source import canonical_json
from reporting.wenquan_datasets import DatasetError


class ResearchDefinitionTests(unittest.TestCase):
    def test_registered_category_uses_its_actual_profile_and_rules(self):
        for category, profile in CATEGORY_PROFILES.items():
            with self.subTest(category=category):
                result = get_research_definition(category)
                self.assertTrue(result["supported"])
                self.assertEqual(result["profile"]["profile_id"], profile.profile_id)
                self.assertEqual(result["profile"]["hero_title"], profile.hero_title)
                self.assertEqual(result["scripts"], registry_manifest())
                self.assertEqual([r["script_id"] for r in result["script_rules"]], [s.script_id for s in SCRIPT_REGISTRY])
                self.assertEqual(result["sample_policy"]["formal_report_thresholds"], REPORT_READINESS_THRESHOLDS)
                self.assertFalse(result["usage"]["formal_report_created"])

    def test_definition_hash_binds_rules_and_implementation(self):
        result = get_research_definition("leaf_blower")
        self.assertEqual(result, get_research_definition("leaf_blower"))
        payload = {k: v for k, v in result.items() if k != "definition_sha256"}
        self.assertEqual(result["definition_sha256"], "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest())
        self.assertNotEqual(result["definition_id"], get_research_definition("walk_behind_mower")["definition_id"])
        changed = copy.deepcopy(result["source_versions"]["implementation_sha256"])
        changed["atomic_v2.py"] = "sha256:" + "0" * 64
        with patch("reporting.research_definition._implementation_hashes", return_value=changed):
            self.assertNotEqual(result["definition_id"], get_research_definition("leaf_blower")["definition_id"])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_unknown_category_does_not_inherit_mower_rules(self):
        result = get_research_definition("unregistered_widget")
        self.assertFalse(result["supported"])
        self.assertIsNone(result["profile"])
        self.assertIsNone(result["research_plan"])
        self.assertEqual(result["scripts"], [])
        self.assertEqual(result["script_rules"], [])

    def test_only_category_identifiers_are_accepted(self):
        for category in (None, [], {}, "../../etc/passwd", "/etc", "leaf_blower\x00", "https://example.test"):
            with self.subTest(category=category), self.assertRaises(DatasetError):
                get_research_definition(category)


if __name__ == "__main__":
    unittest.main()
