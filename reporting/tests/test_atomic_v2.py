from __future__ import annotations

import gzip
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from reporting.analytics_artifact import build_analytics_artifact
from reporting.analysis import build_report
from reporting.atomic_v2 import AtomicScript, SCRIPT_REGISTRY, run_atomic_scripts
from reporting.mobile_report import build_mobile_html
from reporting.tests.test_reporting import fixture_facts, manifest


class AtomicV2Tests(unittest.TestCase):
    def test_duplicate_entities_count_once_per_label_but_keep_execution_rows(self) -> None:
        facts = [
            {"review_entity_id": entity, "has_text": True, "review_text": text}
            for entity, text in [
                ("b", "quiet quiet"), ("a", "power"),
                ("a", "quiet"), ("a", "nothing"), ("c", "nothing"),
            ]
        ]
        result = run_atomic_scripts(facts, snapshot_id="duplicates")
        labels = {row["label"]: row["review_entities"] for row in result["labels"] if row["script_id"] == "E01"}
        self.assertEqual(labels["noise"], 2)
        self.assertEqual(labels["power"], 1)
        self.assertEqual(result["covered_entities"], 2)
        self.assertEqual(result["ledger"]["record_count"], len(facts) * 28)
        self.assertEqual(result["execution"][0]["matched"], 3)

    def test_label_aggregation_does_not_retain_all_entity_memberships(self) -> None:
        # Exclude fixture allocation and serialization: this checks the working
        # set of aggregate counts when many entities match many labels.
        count = 20_000
        facts = [{"review_entity_id": str(i), "has_text": True} for i in range(count)]
        script = AtomicScript("E01", "Memory fixture", "", (), ())
        matches = [{"label": f"label_{i}"} for i in range(16)]
        def record(writer, row):
            writer.record_count += 1
        with patch("reporting.atomic_v2.SCRIPT_REGISTRY", (script,)), patch(
            "reporting.atomic_v2.COMPILED_REGISTRY", ((script, ()),)
        ), patch("reporting.atomic_v2._match_script", new=lambda *args: matches), patch(
            "reporting.atomic_v2._LedgerWriter.write", new=record
        ):
            tracemalloc.start()
            try:
                result = run_atomic_scripts(facts, snapshot_id="memory", evidence_per_script=0)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(result["covered_entities"], count)
        self.assertTrue(all(row["review_entities"] == count for row in result["labels"]))
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_registry_has_exactly_28_versioned_non_llm_scripts(self) -> None:
        self.assertEqual(len(SCRIPT_REGISTRY), 28)
        self.assertEqual(
            [script.script_id for script in SCRIPT_REGISTRY],
            [f"E{index:02d}" for index in range(1, 29)],
        )

    def test_every_written_sample_has_one_status_per_script(self) -> None:
        facts = fixture_facts()
        written = sum(fact["has_text"] for fact in facts)
        with tempfile.TemporaryDirectory() as directory:
            result = run_atomic_scripts(
                facts,
                snapshot_id="snap_fixture",
                output_root=Path(directory),
            )
            self.assertEqual(
                result["ledger"]["record_count"],
                written * 28,
            )
            for row in result["execution"]:
                self.assertEqual(row["eligible"], written)
                self.assertEqual(
                    row["matched"] + row["no_match"] + row["failed"],
                    written,
                )
            ledger = Path(directory) / result["ledger"]["relative_path"]
            self.assertTrue(ledger.is_file())
            with gzip.open(ledger, "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(len(rows), written * 28)

    def test_ledger_hash_and_summary_are_deterministic(self) -> None:
        facts = fixture_facts()
        first = run_atomic_scripts(facts, snapshot_id="snap_fixture")
        second = run_atomic_scripts(facts, snapshot_id="snap_fixture")
        self.assertEqual(
            first["ledger"]["logical_content_sha256"],
            second["ledger"]["logical_content_sha256"],
        )
        self.assertEqual(first["execution"], second["execution"])
        with tempfile.TemporaryDirectory() as directory:
            persisted = run_atomic_scripts(
                facts,
                snapshot_id="snap_fixture",
                output_root=Path(directory),
            )
        self.assertEqual(first["ledger"], persisted["ledger"])

    def test_report_surfaces_all_scripts_in_artifact_and_mobile_html(self) -> None:
        facts = fixture_facts()
        report = build_report(facts, manifest(facts))
        artifact = build_analytics_artifact(report)
        execution = artifact["snapshot"]["datasets"]["script_execution"]
        self.assertEqual(len(execution), 28)
        self.assertTrue(
            all(
                isinstance(value, (str, int, float, bool, type(None)))
                for row in execution
                for value in row.values()
            )
        )
        mobile = build_mobile_html(report)
        self.assertIn('name="viewport"', mobile)
        self.assertIn(report["report_id"], mobile)
        self.assertEqual(
            sum(f'id="E{index:02d}"' in mobile for index in range(1, 29)),
            28,
        )


if __name__ == "__main__":
    unittest.main()
