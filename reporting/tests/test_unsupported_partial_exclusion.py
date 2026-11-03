from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from reporting.analysis import build_report
from reporting.evidence_store import csv_lines, ndjson_lines
from reporting.markdown_export import report_to_markdown
from reporting.source import build_review_fact, catalog, materialize_snapshot
from reporting.source_completeness import SourceCompletenessError
from reporting.source_completeness import enrich_evidence_rows
from scripts.build_reporting_snapshot_v2 import build_snapshot


LEGACY_FIELDS = [
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "source_site",
    "source_product_id",
    "is_syndicated",
    "submission_time",
    "title",
    "is_recommended",
    "user_nickname",
    "review_text",
    "rating",
    "verified_purchaser",
    "where_purchase",
    "age",
]
CURRENT_FIELDS = LEGACY_FIELDS + [
    "display_site",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
]
_UNSET = object()


def _row(review_id: str, *, channel: str, product_id: str) -> dict[str, str]:
    return {
        "channel": channel,
        "product_id": product_id,
        "product_title": "Synthetic Pressure Washer",
        "product_url": f"https://example.test/{product_id}",
        "review_id": review_id,
        "source_site": "example.test",
        "source_product_id": product_id,
        "is_syndicated": "false",
        "submission_time": "2026-07-01",
        "title": "Fixture",
        "is_recommended": "true",
        "user_nickname": "fixture",
        "review_text": "Useful pressure and simple setup.",
        "rating": "5",
        "verified_purchaser": "",
        "where_purchase": "",
        "age": "",
        "display_site": "example.test",
        "origin_source_name": "",
        "origin_source_url": "",
        "origin_source_site": "",
        "market_region": "North America",
        "market_region_evidence": "source_market",
        "review_word_count": "5",
        "is_valid_text_review": "true",
        "is_eligible": "true",
        "exclusion_reason": "",
    }


class UnsupportedPartialSource:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[tuple] = []
        self._database = root / "data" / "jobs.sqlite3"
        self._database.parent.mkdir(parents=True)

    def add(
        self,
        job_id: str,
        *,
        channel: str,
        product_id: str,
        contract_mode: str,
        source_reconciled: bool | None,
        old_stop_reason: str | None,
        governed: bool = False,
        cache_matches: bool = True,
        cache_report_artifact: bool = False,
        cache_shape: str = "channel_envelope",
        raw_ids: list[str] | None = None,
        raw_records: list[dict] | None = None,
        sparse_legacy_validation: bool = False,
        old_import_schema: object = _UNSET,
        text_validity_shape: str = "modern",
        modern_population: tuple[int, int, int] | None = None,
        nonempty_policy_hint: object = _UNSET,
        validation_aggregate_overrides: dict[str, int] | None = None,
        declared_total: int | None = None,
        created_at: str = "2026-07-01T00:00:00Z",
        finished_at: str = "2026-07-02T00:00:00Z",
    ) -> None:
        if modern_population is not None:
            collected, _, _ = modern_population
            text_validity_shape = "modern_only"
        else:
            collected = (
                3
                if text_validity_shape in {"legacy", "modern_only", "mixed"}
                else 2
            )
        declared = collected + 1 if declared_total is None else declared_total
        rows = [
            _row(f"{job_id}-review-{index}", channel=channel, product_id=product_id)
            for index in range(collected)
        ]
        if text_validity_shape == "legacy":
            rows[1].update(
                {
                    "review_text": "So far so good",
                    "review_word_count": "4",
                    "is_valid_text_review": "false",
                }
            )
            rows[2].update(
                {
                    "review_text": "",
                    "review_word_count": "0",
                    "is_valid_text_review": "false",
                }
            )
        elif text_validity_shape == "modern_only":
            if modern_population is None:
                empty_rows, short_rows = 1, 1
            else:
                _, empty_rows, short_rows = modern_population
            for row in rows[:empty_rows]:
                row.update(
                    {
                        "review_text": "",
                        "review_word_count": "0",
                        "is_valid_text_review": "false",
                    }
                )
            for row in rows[empty_rows : empty_rows + short_rows]:
                row.update(
                    {
                        "review_text": "Works very well today",
                        "review_word_count": "4",
                        "is_valid_text_review": "true",
                    }
                )
        elif text_validity_shape == "ambiguous_with_empty":
            rows[1].update(
                {
                    "review_text": "",
                    "review_word_count": "0",
                    "is_valid_text_review": "false",
                }
            )
        elif text_validity_shape == "mixed":
            rows[1].update(
                {
                    "review_text": "Works very well today",
                    "review_word_count": "4",
                    "is_valid_text_review": "true",
                }
            )
            rows[2].update(
                {
                    "review_text": "So far so good",
                    "review_word_count": "4",
                    "is_valid_text_review": "false",
                }
            )
        elif text_validity_shape == "invalid_flag":
            rows[0]["is_valid_text_review"] = "not-a-boolean"
        elif text_validity_shape != "modern":
            raise AssertionError(
                f"unknown text validity shape: {text_validity_shape}"
            )
        run_dir = self.root / "data" / "runs" / job_id / channel
        run_dir.mkdir(parents=True)
        fields = CURRENT_FIELDS if contract_mode == "current" else LEGACY_FIELDS
        with (run_dir / "reviews.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        raw_ids = raw_ids if raw_ids is not None else [row["review_id"] for row in rows]
        raw_records = raw_records or [{"id": value} for value in raw_ids]
        (run_dir / "reviews.raw.jsonl").write_text(
            "".join(json.dumps(value) + "\n" for value in raw_records),
            encoding="utf-8",
        )
        flagged_valid = sum(
            row["is_valid_text_review"].casefold() == "true" for row in rows
        )
        ratings_only = sum(not row["review_text"].strip() for row in rows)
        short_nonempty = sum(
            0 < int(row["review_word_count"]) < 5 for row in rows
        )
        report = {
            "channel": channel,
            "status": "PARTIAL_SUCCESS",
            "product": {
                "id": product_id,
                "title": "Synthetic Pressure Washer",
                "url": f"https://example.test/{product_id}",
                "brand": "Fixture",
                "model": product_id,
                "category": "Pressure Washer",
                "market_region": "North America",
            },
            "source_total": declared,
            "source_unique_reviews": collected,
            "unique_reviews": collected,
            "eligible_reviews": collected,
            "excluded_reviews": 0,
            "valid_text_reviews": flagged_valid,
            "short_text_reviews": (
                short_nonempty
                if text_validity_shape == "modern_only"
                else collected - flagged_valid
            ),
            "ratings_only": ratings_only,
            "market_region": "North America",
            "notes": {},
        }
        report.update(validation_aggregate_overrides or {})
        if source_reconciled is not None:
            report["source_reconciled"] = source_reconciled
        if old_stop_reason is not None:
            report["stop_reason"] = old_stop_reason
        if old_import_schema is not _UNSET:
            report["notes"]["import_schema_version"] = old_import_schema
        if nonempty_policy_hint is not _UNSET:
            report["notes"][
                "nonempty_text_is_valid_text_review"
            ] = nonempty_policy_hint
        elif text_validity_shape == "modern_only":
            report["notes"]["nonempty_text_is_valid_text_review"] = True
        if governed:
            report.update(
                {
                    "validation_schema_version": (
                        "voxquarry.external-review-import-validation/v1"
                    ),
                    "source_records_collected": collected,
                    "source_missing_records": declared - collected,
                    "source_reconciled": False,
                    "stop_reason": "external_import_declared_incomplete",
                }
            )
            report["notes"] = {
                "import_schema_version": "voxquarry.review-import/v1"
            }
        if sparse_legacy_validation:
            for field in (
                "source_unique_reviews",
                "eligible_reviews",
                "excluded_reviews",
                "valid_text_reviews",
                "market_region",
            ):
                report.pop(field, None)
        (run_dir / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8"
        )
        cache = dict(report) if cache_matches else {**report, "source_total": 999}
        if cache_report_artifact:
            cache["artifacts"] = {
                "report": "data/runs/reporting-derived-validation.json"
            }
        if cache_shape == "channel_envelope":
            cache_value = {channel: cache}
        elif cache_shape == "bare":
            cache_value = cache
        elif cache_shape == "wrong_channel":
            cache_value = {"wrong-channel": cache}
        elif cache_shape == "non_object_payload":
            cache_value = {channel: "not-an-object"}
        elif cache_shape == "multiple_channels":
            cache_value = {channel: cache, "other-channel": cache}
        elif cache_shape == "ambiguous_bare_and_envelope":
            cache_value = {channel: cache, "status": "PARTIAL_SUCCESS"}
        else:
            raise AssertionError(f"unknown cache shape: {cache_shape}")
        self.rows.append(
            (
                job_id,
                "partial_success",
                channel,
                "Fixture",
                "Pressure Washer",
                product_id,
                "North America",
                f"https://example.test/{product_id}",
                created_at,
                finished_at,
                None,
                f"{job_id}/{channel}",
                json.dumps(cache_value, ensure_ascii=False),
            )
        )

    def finish(self) -> None:
        with sqlite3.connect(self._database) as connection:
            connection.execute(
                """
                CREATE TABLE jobs (
                    id TEXT, status TEXT, channel TEXT, brand TEXT,
                    category TEXT, model TEXT, market_region TEXT,
                    target_url TEXT, created_at TEXT, finished_at TEXT,
                    superseded_by TEXT, active_run_path TEXT, result_json TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", self.rows
            )


class UnsupportedPartialExclusionTests(unittest.TestCase):
    def _build_three_shapes(self, root: Path) -> dict:
        source = UnsupportedPartialSource(root / "source")
        source.add(
            "c56c9defd21a",
            channel="homedepot",
            product_id="legacy-a",
            contract_mode="legacy",
            source_reconciled=None,
            old_stop_reason="pagination did not reconcile",
            sparse_legacy_validation=True,
            created_at="2026-07-02T00:00:00Z",
            finished_at="2026-07-03T00:00:00Z",
        )
        # A supported older result for the same channel + product identity is
        # deliberately present.  Excluding the selected current partial must
        # not trigger a second discovery pass that resurrects this run.
        source.add(
            "governed-older-same-product",
            channel="homedepot",
            product_id="legacy-a",
            contract_mode="current",
            source_reconciled=False,
            old_stop_reason="external_import_declared_incomplete",
            governed=True,
            created_at="2026-06-01T00:00:00Z",
            finished_at="2026-06-02T00:00:00Z",
        )
        source.add(
            "6565e98386d5",
            channel="einhell",
            product_id="current-b",
            contract_mode="current",
            source_reconciled=False,
            old_stop_reason="external_export_incomplete",
            old_import_schema="voxquarry.review-import/v1",
            text_validity_shape="modern_only",
        )
        source.add(
            "2d78575be689",
            channel="lowes",
            product_id="current-c",
            contract_mode="current",
            source_reconciled=False,
            old_stop_reason=None,
            cache_report_artifact=True,
        )
        source.add(
            "governed-current",
            channel="homedepot",
            product_id="governed-current",
            contract_mode="current",
            source_reconciled=False,
            old_stop_reason="external_import_declared_incomplete",
            governed=True,
        )
        source.finish()
        return build_snapshot(
            Namespace(
                source_root=source.root,
                output_root=root / "snapshot",
                connector_commit="connector-test",
                reporting_commit="reporting-test",
            )
        )

    def test_source_total_overcount_is_excluded_only_with_physical_identity_proof(self):
        for raw_ids in (None, ["wrong-one", "wrong-two"]):
            with self.subTest(raw_ids=raw_ids), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add("overcount", channel="lowes", product_id="legacy",
                           contract_mode="current", source_reconciled=False,
                           old_stop_reason="legacy count mismatch", declared_total=1, raw_ids=raw_ids)
                source.add("governed", channel="homedepot", product_id="current",
                           contract_mode="current", source_reconciled=False,
                           old_stop_reason="external_import_declared_incomplete", governed=True)
                source.finish()
                args = Namespace(source_root=source.root, output_root=root / "snapshot",
                                 connector_commit="fixture", reporting_commit="fixture")
                if raw_ids:
                    with self.assertRaises((RuntimeError, SourceCompletenessError)):
                        build_snapshot(args)
                    continue
                manifest = build_snapshot(args)
                self.assertEqual(manifest["selection"]["job_ids"], ["governed"])
                exclusion = manifest["selection"]["non_reportable_exclusions"][0]
                self.assertTrue(exclusion["declared_total_conflict"])
                self.assertEqual(exclusion["counts"]["excess_reviews"], 1)
                self.assertEqual(exclusion["counts"]["missing_reviews"], 0)

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_exclusion_audit_changes_snapshot_content_hash(self) -> None:
        hashes = []
        for index, stop_reason in enumerate(
            ("pagination did not reconcile", "legacy source limit")
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    "legacy-partial",
                    channel="lowes",
                    product_id="legacy-partial",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason=stop_reason,
                )
                source.add(
                    "governed-current",
                    channel="homedepot",
                    product_id="governed-current",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_import_declared_incomplete",
                    governed=True,
                )
                source.finish()
                manifest = build_snapshot(
                    Namespace(
                        source_root=source.root,
                        output_root=root / f"snapshot-{index}",
                        connector_commit="connector-test",
                        reporting_commit="reporting-test",
                    )
                )
                hashes.append(manifest["content_sha256"])
        self.assertNotEqual(hashes[0], hashes[1])

    def test_historical_five_word_text_contract_is_detected_not_rewritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = UnsupportedPartialSource(root / "source")
            source.add(
                "2d78575be689",
                channel="lowes",
                product_id="current-c",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason=None,
                cache_report_artifact=True,
                text_validity_shape="legacy",
            )
            source.add(
                "governed-current",
                channel="homedepot",
                product_id="governed-current",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason="external_import_declared_incomplete",
                governed=True,
            )
            source.finish()
            manifest = build_snapshot(
                Namespace(
                    source_root=source.root,
                    output_root=root / "snapshot",
                    connector_commit="connector-test",
                    reporting_commit="reporting-test",
                )
            )
            exclusion = next(
                row
                for row in manifest["selection"]["non_reportable_exclusions"]
                if row["job_id"] == "2d78575be689"
            )
            detection = exclusion["text_validity_contract_detection"]
            self.assertEqual(
                detection["version"],
                "historical-text-validity-contract-detection/1.1.0",
            )
            self.assertEqual(
                detection["matching_policies"], ["minimum-five-words"]
            )
            self.assertEqual(
                detection["detected_policy"], "minimum-five-words"
            )
            self.assertFalse(detection["ambiguous_compatible_policies"])
            self.assertEqual(detection["modern_mismatch_rows"], 1)
            self.assertEqual(
                detection["observed_counts"],
                {
                    "csv_rows": 3,
                    "flagged_valid_text_rows": 1,
                    "nonempty_text_rows": 2,
                    "empty_text_rows": 1,
                    "short_nonempty_text_rows": 1,
                    "validation_valid_text_reviews": 1,
                    "validation_short_text_reviews": 2,
                    "validation_ratings_only": 1,
                },
            )
            self.assertEqual(
                detection["short_text_definition"],
                {
                    "version": "policy-specific-short-text/v1",
                    "expected_by_policy": {
                        "minimum-five-words": {
                            "definition": "historical-false-valid-flag/v1",
                            "expected": 2,
                            "basis": "csv_rows - flagged_valid_text_rows",
                        }
                    },
                    "matching_short_definitions": [
                        "historical-false-valid-flag/v1"
                    ],
                    "ambiguous": False,
                    "observed": 2,
                },
            )
            self.assertEqual(exclusion["excluded_records"], 3)
            self.assertIn("stop_reason", exclusion["missing_contract_fields"])
            self.assertEqual(
                exclusion["cache_delta"],
                [
                    {
                        "path": "artifacts.report",
                        "kind": "cache_only_non_authoritative_derived_key",
                    }
                ],
            )

    def test_four_observed_b_shape_counts_use_external_import_short_policy(
        self,
    ) -> None:
        observed_shapes = (
            ("6565e98386d5", 4350, 2, 122),
            ("1555212ed152", 3866, 1, 89),
            ("58fd04846d32", 1849, 2, 75),
            ("bbb239c555f8", 2642, 7, 173),
        )
        for job_id, rows, empty, short in observed_shapes:
            with self.subTest(job_id=job_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    job_id,
                    channel="homedepot",
                    product_id=job_id,
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_export_incomplete",
                    old_import_schema="voxquarry.review-import/v1",
                    modern_population=(rows, empty, short),
                )
                source.add(
                    "governed-current",
                    channel="homedepot",
                    product_id="governed-current",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_import_declared_incomplete",
                    governed=True,
                )
                source.finish()
                manifest = build_snapshot(
                    Namespace(
                        source_root=source.root,
                        output_root=root / "snapshot",
                        connector_commit="connector-test",
                        reporting_commit="reporting-test",
                    )
                )
                exclusion = next(
                    row
                    for row in manifest["selection"][
                        "non_reportable_exclusions"
                    ]
                    if row["job_id"] == job_id
                )
                detection = exclusion["text_validity_contract_detection"]
                self.assertEqual(
                    detection["matching_policies"], ["any-nonempty-text"]
                )
                self.assertEqual(
                    detection["observed_counts"]["csv_rows"], rows
                )
                self.assertEqual(
                    detection["observed_counts"]["empty_text_rows"], empty
                )
                self.assertEqual(
                    detection["observed_counts"]["short_nonempty_text_rows"],
                    short,
                )
                self.assertEqual(
                    detection["short_text_definition"][
                        "matching_short_definitions"
                    ],
                    ["external-import-short-nonempty/v1"],
                )
                self.assertEqual(
                    detection["short_text_definition"]["observed"], short
                )

    def test_invalid_text_contract_or_aggregates_remain_hard_failures(self) -> None:
        variants = (
            ({"text_validity_shape": "mixed"}, "text-validity semantics"),
            ({"text_validity_shape": "invalid_flag"}, "eligibility fields"),
            (
                {"validation_aggregate_overrides": {"valid_text_reviews": 1}},
                "valid_text_reviews contradicts",
            ),
            (
                {"validation_aggregate_overrides": {"short_text_reviews": 1}},
                "short_text_reviews contradicts",
            ),
            (
                {"validation_aggregate_overrides": {"ratings_only": 1}},
                "ratings_only contradicts",
            ),
        )
        for index, (options, message) in enumerate(variants):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    f"bad-text-contract-{index}",
                    channel="lowes",
                    product_id=f"bad-text-contract-{index}",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason=None,
                    **options,
                )
                source.finish()
                with self.assertRaisesRegex(SourceCompletenessError, message):
                    build_snapshot(
                        Namespace(
                            source_root=source.root,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )
                self.assertFalse((root / "snapshot").exists())

    def test_ambiguous_text_flags_do_not_hide_short_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = UnsupportedPartialSource(root / "source")
            source.add(
                "ambiguous-short-definition",
                channel="lowes",
                product_id="ambiguous-short-definition",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason=None,
                text_validity_shape="ambiguous_with_empty",
                validation_aggregate_overrides={"short_text_reviews": 0},
            )
            source.add(
                "governed-current",
                channel="homedepot",
                product_id="governed-current",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason="external_import_declared_incomplete",
                governed=True,
            )
            source.finish()
            manifest = build_snapshot(
                Namespace(
                    source_root=source.root,
                    output_root=root / "snapshot",
                    connector_commit="connector-test",
                    reporting_commit="reporting-test",
                )
            )
            detection = manifest["selection"]["non_reportable_exclusions"][0][
                "text_validity_contract_detection"
            ]
            self.assertEqual(
                detection["matching_policies"],
                ["any-nonempty-text", "minimum-five-words"],
            )
            self.assertIsNone(detection["detected_policy"])
            self.assertTrue(detection["ambiguous_compatible_policies"])
            self.assertEqual(
                detection["short_text_definition"][
                    "matching_short_definitions"
                ],
                ["external-import-short-nonempty/v1"],
            )
            self.assertFalse(
                detection["short_text_definition"]["ambiguous"]
            )

    def test_explicit_nonempty_policy_hint_must_be_boolean_and_match_csv(
        self,
    ) -> None:
        variants = (
            ("legacy", True, "contradicts CSV"),
            ("modern_only", False, "contradicts CSV"),
            ("modern_only", "true", "invalid explicit text-validity policy"),
        )
        for index, (shape, hint, message) in enumerate(variants):
            with self.subTest(shape=shape, hint=hint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    f"bad-policy-hint-{index}",
                    channel="homedepot",
                    product_id=f"bad-policy-hint-{index}",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_export_incomplete",
                    text_validity_shape=shape,
                    nonempty_policy_hint=hint,
                )
                source.finish()
                with self.assertRaisesRegex(SourceCompletenessError, message):
                    build_snapshot(
                        Namespace(
                            source_root=source.root,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )
                self.assertFalse((root / "snapshot").exists())

    def test_physical_or_cache_contradictions_remain_hard_failures(self) -> None:
        variants = (
            {"cache_matches": False},
            {"raw_ids": ["wrong-one", "wrong-two"]},
            {"raw_ids": ["only-one"]},
        )
        for index, options in enumerate(variants):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    f"bad-{index}",
                    channel="lowes",
                    product_id=f"bad-{index}",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason=None,
                    **options,
                )
                source.add(
                    "governed-current",
                    channel="homedepot",
                    product_id="governed-current",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_import_declared_incomplete",
                    governed=True,
                )
                source.finish()
                with self.assertRaises((SourceCompletenessError, RuntimeError)):
                    build_snapshot(
                        Namespace(
                            source_root=source.root,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )
                self.assertFalse((root / "snapshot").exists())

        # Same physical row count cannot hide a duplicated mapped identity and
        # an unmapped CSV identity.  One raw row may expose several governed
        # identifier fields, but the record itself must map to exactly one CSV
        # review and no CSV review may be matched twice.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = UnsupportedPartialSource(root / "source")
            source.add(
                "duplicate-map",
                channel="lowes",
                product_id="duplicate-map",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason=None,
                raw_records=[
                    {"id": "duplicate-map-review-0", "guid": "transport-0"},
                    {"id": "duplicate-map-review-0", "guid": "transport-1"},
                ],
            )
            source.finish()
            with self.assertRaises(SourceCompletenessError):
                build_snapshot(
                    Namespace(
                        source_root=source.root,
                        output_root=root / "snapshot",
                        connector_commit="connector-test",
                        reporting_commit="reporting-test",
                    )
                )
            self.assertFalse((root / "snapshot").exists())

    def test_raw_record_with_multiple_identifiers_maps_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = UnsupportedPartialSource(root / "source")
            source.add(
                "multi-id",
                channel="ryobi",
                product_id="multi-id",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason=None,
                raw_records=[
                    {"id": "transport-0", "guid": "multi-id-review-0"},
                    {"id": "transport-1", "guid": "multi-id-review-1"},
                ],
            )
            source.add(
                "governed-current",
                channel="homedepot",
                product_id="governed-current",
                contract_mode="current",
                source_reconciled=False,
                old_stop_reason="external_import_declared_incomplete",
                governed=True,
            )
            source.finish()
            manifest = build_snapshot(
                Namespace(
                    source_root=source.root,
                    output_root=root / "snapshot",
                    connector_commit="connector-test",
                    reporting_commit="reporting-test",
                )
            )
            exclusion = manifest["selection"]["non_reportable_exclusions"][0]
            self.assertEqual(exclusion["job_id"], "multi-id")
            self.assertEqual(exclusion["counts"]["raw_records"], 2)

    def test_cache_requires_unambiguous_current_channel_envelope(self) -> None:
        for index, cache_shape in enumerate(
            (
                "bare",
                "wrong_channel",
                "non_object_payload",
                "multiple_channels",
                "ambiguous_bare_and_envelope",
            )
        ):
            with self.subTest(cache_shape=cache_shape), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    f"bad-envelope-{index}",
                    channel="lowes",
                    product_id=f"bad-envelope-{index}",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason=None,
                    cache_shape=cache_shape,
                )
                source.finish()
                with self.assertRaises(SourceCompletenessError):
                    build_snapshot(
                        Namespace(
                            source_root=source.root,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )
                self.assertFalse((root / "snapshot").exists())

    def test_unknown_or_non_string_import_schema_remains_hard_failure(self) -> None:
        for index, (value, message) in enumerate(
            (
                ("voxquarry.review-import/v999", "unexpected import_schema_version"),
                (["voxquarry.review-import/v1"], "must be a string"),
            )
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = UnsupportedPartialSource(root / "source")
                source.add(
                    f"unknown-import-schema-{index}",
                    channel="einhell",
                    product_id=f"unknown-import-schema-{index}",
                    contract_mode="current",
                    source_reconciled=False,
                    old_stop_reason="external_export_incomplete",
                    old_import_schema=value,
                )
                source.finish()
                with self.assertRaisesRegex(SourceCompletenessError, message):
                    build_snapshot(
                        Namespace(
                            source_root=source.root,
                            output_root=root / "snapshot",
                            connector_commit="connector-test",
                            reporting_commit="reporting-test",
                        )
                    )
                self.assertFalse((root / "snapshot").exists())


if __name__ == "__main__":
    unittest.main()
