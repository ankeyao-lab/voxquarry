from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.factors import _contains_term, explicit_age_group, extract_atoms
from reporting.source import parse_boolean, parse_rating, sha256_text
from reporting.validation import ReportValidationError, validate_report


FIXTURE = Path(__file__).parent / "fixtures" / "mower-reviews.csv"


def fixture_facts() -> list[dict]:
    facts = []
    with FIXTURE.open("r", encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            rating = parse_rating(row["rating"])
            syndicated = parse_boolean(row["is_syndicated"])
            occurrence_id = f"occ_test_{index:02d}"
            facts.append(
                {
                    "review_occurrence_id": occurrence_id,
                    "review_entity_id": f"rev_test_{index:02d}",
                    "review_id": row["review_id"],
                    "canonical_entity_rank": 1,
                    "is_duplicate_occurrence": False,
                    "job_id": "fixture-job",
                    "job_finished_at": "2025-05-10T00:00:00Z",
                    "channel": row["channel"],
                    "product_id": row["product_id"],
                    "product_title": row["product_title"],
                    "product_url": "",
                    "brand": "EGO",
                    "category": "割草机",
                    "model": "TEST-M1",
                    "source_site_raw": row["source_site"],
                    "source_site_normalized": row["source_site"],
                    "source_product_id": row["source_product_id"],
                    "collection_method": "primary",
                    "validation_status": "COMPLETE",
                    "is_syndicated_raw": row["is_syndicated"],
                    "is_syndicated": syndicated,
                    "syndication_parse_status": (
                        "parsed" if syndicated is not None else "unknown"
                    ),
                    "submission_time_raw": row["submission_time"],
                    "submission_date": row["submission_time"],
                    "time_parse_status": "parsed",
                    "time_parse_rule": "date-iso",
                    "review_title": row["title"],
                    "review_text": row["review_text"],
                    "has_text": bool(row["review_text"].strip()),
                    "rating_raw": row["rating"],
                    "rating": rating,
                    "rating_valid": rating is not None,
                    "is_recommended_raw": row["is_recommended"],
                    "is_recommended": parse_boolean(row["is_recommended"]),
                    "recommended_parse_status": "parsed",
                    "verified_purchaser_raw": row["verified_purchaser"],
                    "verified_purchaser": None,
                    "verified_parse_status": "unknown",
                    "where_purchase": row["where_purchase"],
                    "age": row["age"],
                    "business_key_status": "complete",
                    "eligibility_status": "eligible",
                    "exclusion_reasons": [],
                    "source_artifact_path": "fixtures/mower-reviews.csv",
                    "source_row_number": index + 1,
                    "source_artifact_sha256": "sha256:fixture",
                    "fact_schema_version": "review-fact/v1",
                    "parser_version": "review-parser/1.0.0",
                    "normalization_version": "review-normalization/1.0.0",
                    "row_content_sha256": f"sha256:{sha256_text(json.dumps(row, sort_keys=True))}",
                }
            )
    return facts


def manifest(facts: list[dict]) -> dict:
    return {
        "snapshot_id": "snap_fixture000000000000000001",
        "scope": {
            "categories": ["割草机"],
            "exclude_categories": ["骑乘割草机"],
            "has_text": "any",
            "syndicated": "include",
            "fallback_source": "include",
        },
        "record_counts": {
            "occurrences": len(facts),
            "complete_entity_keys": len(facts),
            "unique_entities": len(facts),
            "written_reviews": sum(fact["has_text"] for fact in facts),
            "excluded_occurrences": 0,
        },
        "quality_summary": {
            "unknown_identity_occurrences": 0,
            "duplicate_occurrences": 0,
            "fallback_occurrences": 0,
        },
        "hashes": {"logical_content": "sha256:fixturelogicalcontent"},
    }


class ReportingTests(unittest.TestCase):
    def test_denominators_and_core_hash_are_deterministic(self) -> None:
        facts = fixture_facts()
        first = build_report(facts, manifest(facts), report_name="Fixture report")
        second = build_report(facts, manifest(facts), report_name="Fixture report")
        metrics = {item["metric_id"]: item for item in first["metrics"]}
        self.assertEqual(metrics["sample_size"]["value"], 10)
        self.assertEqual(metrics["written_review_count"]["value"], 9)
        self.assertEqual(metrics["written_coverage"]["numerator"], 9)
        self.assertEqual(metrics["written_coverage"]["denominator"], 10)
        self.assertEqual(metrics["positive_rating_rate"]["numerator"], 6)
        self.assertEqual(metrics["positive_rating_rate"]["denominator"], 10)
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(
            first["hashes"]["core_result"], second["hashes"]["core_result"]
        )

    def test_literal_factor_matching_keeps_unicode_and_word_boundaries(self) -> None:
        cases = [
            ("battery powered", "BATTERY", True),
            ("batterylife", "battery", False),
            ("x_battery", "battery", False),
            ("ébattery", "battery", False),
            ("battery容量", "battery", False),
            ("battery-powered", "battery", True),
            ("battery-powered", "battery-powered", True),
            ("can't start", "can't", True),
            ("a+b works", "a+b", True),
            ("axb works", "a.b", False),
            ("a.b works", "a.b", True),
            ("[quiet] motor", "[quiet]", True),
            ("quiet motor", "[quiet]", False),
            ("strasse works", "Straße", True),
            ("not noisy", "noisy", True),
            ("quiet motor", "battery", False),
            ("word", "", True),
            (" ", "", True),
        ]
        for text, term, expected in cases:
            with self.subTest(text=text, term=term):
                self.assertEqual(_contains_term(text, term), expected)

    def test_factor_atoms_preserve_negation_and_evidence(self) -> None:
        atoms, evidence, profile = extract_atoms(fixture_facts())
        quiet_atom = next(
            item
            for item in atoms
            if item["review_occurrence_id"] == "occ_test_05"
            and item["factor_id"] == "noise"
        )
        self.assertTrue(quiet_atom["negated"])
        self.assertGreater(profile["covered_entities"], 0)
        evidence_ids = {item["evidence_id"] for item in evidence}
        self.assertIn(quiet_atom["evidence_id"], evidence_ids)

    def test_structured_age_values_are_normalized(self) -> None:
        facts = fixture_facts()
        groups = [explicit_age_group(fact)[0] for fact in facts[:6]]
        self.assertEqual(
            groups,
            [
                "65岁及以上",
                "55–64岁",
                "45–54岁",
                "35–44岁",
                "25–34岁",
                "18–24岁",
            ],
        )

    def test_report_contract_and_low_sample_warning(self) -> None:
        facts = fixture_facts()
        report = build_report(facts, manifest(facts))
        required = {
            "report_id",
            "schema_version",
            "analysis_version",
            "research_plan",
            "data_snapshot_id",
            "scope",
            "samples",
            "metric_definitions",
            "metrics",
            "tables",
            "charts",
            "findings",
            "evidence",
            "limitations",
            "generated_at",
            "hashes",
        }
        self.assertTrue(required.issubset(report))
        sample_metric = next(
            item for item in report["metrics"] if item["metric_id"] == "sample_size"
        )
        self.assertIn("low_sample", sample_metric["quality_flags"])
        self.assertFalse(report["method"]["llm_used"])
        validate_report(report)

        report["metrics"][0]["value"] = 999
        with self.assertRaises(ReportValidationError):
            validate_report(report)

    def test_default_research_plan_is_category_neutral(self) -> None:
        report = build_report(fixture_facts(), manifest(fixture_facts()))

        self.assertEqual(
            report["research_plan"]["id"],
            "full-category-research",
        )
        self.assertEqual(
            report["research_plan"]["resolved_version"],
            "full-category/1.2.0",
        )
        self.assertEqual(
            report["research_plan"]["dictionary_version"],
            "voxquarry-review-factors/1.3.0",
        )


if __name__ == "__main__":
    unittest.main()
