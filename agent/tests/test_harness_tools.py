"""Tool-contract regressions; these do not assert model answer quality."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, ValidationError

from agent.harness_tools import research_tools, sdk_research_tools


class HarnessToolContractTests(unittest.TestCase):
    def test_native_catalog_retires_workflow_wrappers_but_preserves_report_and_evidence_coverage(self):
        native = {tool['name']: tool for tool in sdk_research_tools()}
        legacy = {tool['name'] for tool in sdk_research_tools('wenquan-dsh/v1')}
        self.assertEqual(len(native), 24)
        self.assertEqual(len(legacy), 33)
        self.assertTrue({'create_report', 'get_metric', 'query_report_reviews', 'aggregate_report_reviews', 'publish_artifact'} <= native.keys())
        self.assertFalse({'submit_answer', 'publish_report', 'prepare_product_research', 'analyze_product_reviews', 'search_web'} & native.keys())
        for name in ['create_report', 'collect_product', 'publish_artifact', 'open_dataset', 'export_dataset', 'read_attachment']:
            self.assertFalse(native[name]['concurrency_safe'])
        self.assertTrue(native['aggregate_dataset']['concurrency_safe'])

    def setUp(self):
        self.tools = {tool["name"]: tool for tool in research_tools()}

    def validate(self, name, arguments):
        Draft202012Validator(self.tools[name]["parameters"]).validate(arguments)

    def test_report_tools_keep_the_authoritative_pagination_contract(self):
        # Reducing what goes into one model context must not remove full reads.
        root = Path(__file__).resolve().parents[2] / "reporting/schemas"
        for name in ("list_reports", "get_report_section", "query_report_reviews", "aggregate_report_reviews"):
            with self.subTest(name=name):
                schema = json.loads((root / f"zhuojian.{name}-v1.schema.json").read_text())
                schema.pop("$schema", None)
                schema.pop("$id", None)
                exposed = self.tools[name]['parameters']
                if 'oneOf' in exposed:
                    self.assertEqual(exposed['oneOf'][0], schema)
                    self.assertEqual(exposed['oneOf'][1]['properties']['requests']['items'], schema)
                else:
                    self.assertEqual(exposed, schema)
        for section in ("", "/tables/3/rows", "/evidence/7"):
            self.validate("get_report_section", {"report_id": "rpt_fixture123", "section": section,
                                                  "offset": 500, "limit": 100})

    def test_full_comment_reads_and_full_exports_remain_available(self):
        ids = [f"rev_fixture_{index:03d}" for index in range(100)]
        for population in ("entities", "occurrences"):
            self.validate("query_dataset", {"dataset_id": "ds_fixture", "review_ids": ids,
                                              "population": population, "limit": 100})
        self.validate("query_report_reviews", {"report_id": "rpt_fixture123", "review_ids": ids,
                                                  "population": "entities", "limit": 100})
        # No fixed first-N ceiling is introduced by the context presentation change.
        for offset in (0, 100, 800, 25000):
            self.validate("query_dataset", {"dataset_id": "ds_fixture", "offset": offset, "limit": 100})
        self.validate("query_dataset", {"dataset_id": "ds_fixture", "cursor": "opaque-next-page"})
        self.validate("export_dataset", {"dataset_id": "ds_fixture"})

    def test_selective_file_reads_preserve_code_and_paging_access(self):
        path = "/inputs/tool-result-" + "a" * 64 + ".json"
        self.validate("run_python", {"code": f"import json\ndata = json.load(open({path!r}))\nprint(data.keys())",
                                     "timeout_seconds": 120})
        for offset in (0, 60000, 120000):
            self.validate("read_file", {"path": path, "offset": offset, "limit": 60000})
        # The new guidance cannot create an unmediated network or filesystem tool.
        with self.assertRaises(ValidationError):
            self.validate("run_python", {"code": "print(1)", "network": True})
        with self.assertRaises(ValidationError):
            self.validate("read_file", {"path": path, "limit": 60001})

    def test_all_native_schemas_remain_valid_and_request_local(self):
        for tool in self.tools.values():
            Draft202012Validator.check_schema(tool["parameters"])
        self.tools["query_dataset"]["parameters"]["properties"].clear()
        fresh = {tool["name"]: tool for tool in research_tools()}
        self.assertIn("review_ids", fresh["query_dataset"]["parameters"]["properties"])

    def test_code_timeout_matches_analysis_limit_and_saved_source_cannot_select_a_session(self):
        for name, argument in (("run_python", {"code": "print(1)"}), ("run_shell", {"command": "echo 1"})):
            self.validate(name, {**argument, "timeout_seconds": 300})
            for value in (0, 301, True):
                with self.assertRaises(ValidationError):
                    self.validate(name, {**argument, "timeout_seconds": value})
        self.validate("read_saved_source", {"source_id": "src_fixture"})
        for value in ({}, {"source_id": "src_fixture", "session_id": "ses_other"}, {"source_id": "src_fixture", "owner_user_id": "another"}):
            with self.assertRaises(ValidationError):
                self.validate("read_saved_source", value)


if __name__ == "__main__":
    unittest.main()
