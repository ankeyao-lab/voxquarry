from __future__ import annotations

import collections
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agent.contracts import sha256_value
from agent.evals.runner import build_receipt, write_receipt
from agent.tool_registry import ZHUOJIAN_TOOL_NAMES, ToolRegistry


class EvalDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.dataset = json.loads(
            (root / "evals" / "wenquan-golden-v1.json").read_text(encoding="utf-8")
        )
        self.schema = json.loads(
            (root / "schemas" / "voxquarry-wenquan-eval-dataset-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_dataset_has_exact_40_case_accepted_distribution(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        Draft202012Validator(self.schema).validate(self.dataset)
        cases = self.dataset["cases"]
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({case["case_id"] for case in cases}), 40)
        self.assertEqual(
            collections.Counter(case["category"] for case in cases),
            {
                "scope": 7,
                "readiness": 5,
                "reuse": 5,
                "full_flow": 5,
                "recovery": 5,
                "export_artifact": 4,
                "memory_identity": 4,
                "security": 3,
                "pressure_washer_pilot": 2,
            },
        )

    def test_cases_only_reference_governed_read_tools_or_citation_validator(self) -> None:
        allowed = set(ZHUOJIAN_TOOL_NAMES) | {"zhuojian.citation_validator@v1"}
        used = {
            tool
            for case in self.dataset["cases"]
            for tool in case["expected"]["tool_sequence"]
        }
        self.assertTrue(used.issubset(allowed))
        serialized = json.dumps(self.dataset, ensure_ascii=False).lower()
        self.assertIn("shell", serialized)
        self.assertIn("sql", serialized)
        self.assertIn("cross_user_session", serialized)
        self.assertIn("memory_write_when_disabled", serialized)
        self.assertIn("citation_validator", serialized)

    def test_dataset_is_explicitly_synthetic_not_production_evidence(self) -> None:
        self.assertEqual(self.dataset["privacy_status"], "synthetic")
        self.assertEqual(self.dataset["dataset_version"], "wenquan-golden/v1")
        self.assertTrue(
            all(len(case["user_message"]) >= 6 for case in self.dataset["cases"])
        )

    def test_e0_e1_receipt_runs_every_case_three_times_and_keeps_planner_blocked(self) -> None:
        dataset_path = Path(__file__).resolve().parents[1] / "evals" / "wenquan-golden-v1.json"
        receipt = build_receipt(
            dataset_path=dataset_path,
            implementation_ref="c" * 40,
            repeat_count=3,
            seed=20260828,
        )
        receipt_schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "voxquarry-wenquan-eval-receipt-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(receipt_schema).validate(receipt)
        self.assertEqual(receipt["summary"]["attempt_count"], 120)
        self.assertEqual(receipt["summary"]["failed_attempts"], 0)
        self.assertGreater(receipt["summary"]["blocked_attempts"], 0)
        self.assertEqual(receipt["summary"]["overall_status"], "blocked")
        self.assertEqual(
            collections.Counter(item["case_id"] for item in receipt["attempts"]),
            {case["case_id"]: 3 for case in self.dataset["cases"]},
        )
        semantic = receipt["metrics"]["semantic_tool_sequence_accuracy"]
        self.assertEqual(semantic["numerator"], 0)
        self.assertEqual(semantic["denominator"], 120)
        self.assertEqual(semantic["threshold"], 0.95)
        self.assertFalse(semantic["passed"])
        self.assertTrue(receipt["metrics"]["deterministic_contract_accuracy"]["passed"])
        self.assertTrue(
            any(
                item["blocker_code"] == "semantic_planner_not_implemented"
                for item in receipt["attempts"]
            )
        )
        unhashed = dict(receipt)
        observed_hash = unhashed.pop("receipt_sha256")
        self.assertEqual(observed_hash, sha256_value(unhashed))
        with tempfile.TemporaryDirectory() as temporary:
            first = write_receipt(receipt, Path(temporary))
            second = write_receipt(receipt, Path(temporary))
            self.assertEqual(first, second)
            self.assertEqual(first.stem, observed_hash.removeprefix("sha256:"))

    @staticmethod
    def _git_blob_sha256(ref: str, path: str, *, cwd: Path) -> str:
        blob = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=cwd,
            check=True,
            capture_output=True,
        ).stdout
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    # Historical receipts are intentionally not distributed; receipt hashing is tested above.


if __name__ == "__main__":
    unittest.main()
