from __future__ import annotations

import ast
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.contracts import ContractError, default_scope, normalize_scope
from reporting.product_insight import build_product_insight_bundle
from reporting.source import (
    MARKET_REGIONS,
    MarketContractError,
    build_review_fact,
    catalog,
    materialize_snapshot,
)
from reporting.validation import validate_report


NEW_FIELDS = [
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "display_site",
    "source_site",
    "source_product_id",
    "market_region",
    "market_region_evidence",
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
    "is_eligible",
    "is_valid_text_review",
]


def _row(
    review_id: str,
    region: str,
    *,
    text: str = (
        "I use this blower every week to clear wet leaves. "
        "The battery runtime lasts 30 minutes and has plenty of power."
    ),
    eligible: str = "true",
    valid_text: str = "true",
    channel: str = "ego",
) -> dict[str, str]:
    return {
        "channel": channel,
        "product_id": f"product-{review_id}",
        "product_title": "Fixture blower",
        "product_url": f"https://example.test/{review_id}",
        "review_id": review_id,
        "display_site": "example.test",
        "source_site": "example.test",
        "source_product_id": f"source-{review_id}",
        "market_region": region,
        "market_region_evidence": "source_market",
        "is_syndicated": "false",
        "submission_time": "2026-07-01",
        "title": "Fixture",
        "is_recommended": "true",
        "user_nickname": "fixture",
        "review_text": text,
        "rating": "5",
        "verified_purchaser": "",
        "where_purchase": "",
        "age": "",
        "is_eligible": eligible,
        "is_valid_text_review": valid_text,
    }


class SnapshotFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        backup = root / "data" / "backups"
        backup.mkdir(parents=True)
        self.database = backup / "jobs-consistent-snapshot-market.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE jobs (
                    id TEXT,
                    status TEXT,
                    channel TEXT,
                    brand TEXT,
                    category TEXT,
                    model TEXT,
                    market_region TEXT,
                    target_url TEXT,
                    created_at TEXT,
                    finished_at TEXT,
                    superseded_by TEXT
                )
                """
            )

    def add_run(
        self,
        job_id: str,
        region: str,
        rows: list[dict[str, str]],
        *,
        channel: str = "ego",
        legacy: bool = False,
        report_region: str | None = None,
        archive_through: str | None = None,
        excluded_reviews: int = 0,
        report_overrides: dict[str, object] | None = None,
    ) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    "completed",
                    channel,
                    "EGO" if channel != "amazon_reviews_2023" else "RYOBI",
                    "Leaf Blower",
                    f"MODEL-{job_id}",
                    region,
                    f"https://example.test/{job_id}",
                    "2026-07-01",
                    "2026-07-02",
                    None,
                ),
            )
        run_dir = self.root / "data" / "runs" / job_id / channel
        run_dir.mkdir(parents=True)
        fields = (
            [
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
            if legacy
            else NEW_FIELDS
        )
        with (run_dir / "reviews.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fields, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
        report = {
            "status": "COMPLETE",
            "source_total": len(rows) + excluded_reviews,
            "unique_reviews": len(rows),
            "product": {"id": f"product-{job_id}"},
        }
        if not legacy:
            report.update(
                {
                    "market_region": report_region or region,
                    "source_unique_reviews": len(rows) + excluded_reviews,
                    "eligible_reviews": sum(
                        row["is_eligible"] == "true" for row in rows
                    ),
                    "excluded_reviews": excluded_reviews,
                    "valid_text_reviews": sum(
                        row["is_eligible"] == "true"
                        and row["is_valid_text_review"] == "true"
                        for row in rows
                    ),
                }
            )
        if archive_through:
            report["archive_through"] = archive_through
        report.update(report_overrides or {})
        (run_dir / "validation_report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )


class MarketRegionTests(unittest.TestCase):
    def test_scope_accepts_only_four_canonical_regions(self) -> None:
        for region in MARKET_REGIONS:
            with self.subTest(region=region):
                scope = normalize_scope(
                    {"market_regions": [region]}, default_scope()
                )
                self.assertEqual(scope["market_regions"], [region])
        self.assertEqual(
            normalize_scope({}, default_scope())["market_regions"], []
        )
        with self.assertRaisesRegex(ContractError, "market_regions"):
            normalize_scope(
                {"market_regions": ["Asia"]}, default_scope()
            )

    def test_create_report_api_maps_unknown_region_contract_to_422(self) -> None:
        with self.assertRaisesRegex(ContractError, "market_regions"):
            normalize_scope(
                {"market_regions": ["Asia"]}, default_scope()
            )

        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        contract_handlers = [
            handler
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Name)
            and handler.type.id == "ContractError"
        ]
        self.assertTrue(contract_handlers)
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "HTTPException"
                and any(
                    keyword.arg == "status_code"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == 422
                    for keyword in call.keywords
                )
                for handler in contract_handlers
                for call in ast.walk(handler)
            )
        )

    def test_v2_package_rejects_any_unmigrated_legacy_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "legacy-in-v2",
                "North America",
                [_row("legacy-review", "North America")],
                legacy=True,
            )
            (root / "snapshot-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "voxquarry-reporting-snapshot/v2",
                        "contracts": {"review_fact": "review-fact/v2"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MarketContractError,
                "v2 报告快照包含未升级任务",
            ):
                catalog(root)

    def test_new_contract_includes_all_eligible_records_and_regions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "north-america",
                "North America",
                [
                    _row("na-valid", "North America"),
                    _row(
                        "na-short",
                        "North America",
                        text="Too short",
                    ),
                    _row(
                        "na-rating-only",
                        "North America",
                        text="",
                        valid_text="false",
                    ),
                ],
                excluded_reviews=1,
            )
            fixture.add_run(
                "europe",
                "Europe",
                [_row("eu-valid", "Europe")],
            )
            scope = {
                **default_scope(),
                "category_id": "leaf_blower",
                "categories": ["Leaf Blower"],
                "job_ids": ["north-america", "europe"],
                "market_regions": ["North America", "Europe"],
            }
            facts, quality, _ = build_review_fact(root, scope)
            self.assertEqual(
                {fact["review_id"] for fact in facts},
                {
                    "na-valid",
                    "na-short",
                    "na-rating-only",
                    "eu-valid",
                },
            )
            self.assertEqual(quality["market_contract_mode"], "current")
            self.assertEqual(
                quality["market_region_summary"]["North America"],
                {
                    "record_count": 4,
                    "written_review_count": 2,
                    "eligible_valid_text_count": 2,
                    "missing_contract_record_count": 0,
                },
            )
            self.assertEqual(quality["excluded_ineligible_records"], 1)
            self.assertEqual(quality["excluded_invalid_text_records"], 0)
            self.assertEqual(quality["non_text_records"], 1)
            (root / "snapshot-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "voxquarry-reporting-snapshot/v2",
                        "snapshot_id": "fixture-snapshot-v2",
                        "created_at": "2026-08-10T00:00:00+00:00",
                        "content_sha256": "sha256:fixture",
                        "contracts": {"review_fact": "review-fact/v2"},
                    }
                ),
                encoding="utf-8",
            )
            task_catalog = catalog(root)
            north_america_task = next(
                task
                for task in task_catalog["tasks"]
                if task["id"] == "north-america"
            )
            self.assertEqual(north_america_task["source_unique_reviews"], 4)
            self.assertEqual(north_america_task["eligible_reviews"], 3)
            self.assertEqual(north_america_task["excluded_reviews"], 1)
            self.assertEqual(
                task_catalog["source_snapshot"],
                {
                    "snapshot_id": "fixture-snapshot-v2",
                    "schema_version": "voxquarry-reporting-snapshot/v2",
                    "created_at": "2026-08-10T00:00:00+00:00",
                    "content_sha256": "sha256:fixture",
                },
            )

            selected_scope = {
                **scope,
                "job_ids": ["europe"],
                "market_regions": ["Europe"],
            }
            selected, manifest = materialize_snapshot(
                root, selected_scope, root / "var"
            )
            self.assertEqual([fact["review_id"] for fact in selected], ["eu-valid"])
            self.assertEqual(
                manifest["schema_version"], "snapshot-manifest/v2"
            )
            self.assertEqual(
                manifest["filters"]["market_regions"], ["Europe"]
            )
            self.assertEqual(manifest["filters"]["job_ids"], ["europe"])
            self.assertEqual(
                manifest["content_hash"],
                manifest["hashes"]["input_manifest"],
            )
            self.assertEqual(
                manifest["record_counts"]["eligible_valid_text"], 1
            )
            report = build_report(
                selected, manifest, report_name="Europe fixture"
            )
            validate_report(report)
            self.assertEqual(
                report["samples"]["market_region_summary"],
                [
                    {
                        "market_region": "Europe",
                        "eligible_valid_text_entities": 1,
                        "share": 1.0,
                    }
                ],
            )
            market_table = next(
                table
                for table in report["tables"]
                if table["table_id"] == "market_region_composition"
            )
            self.assertEqual(
                market_table["rows"][0]["market_region"], "Europe"
            )
            (
                report,
                full_evidence,
                _,
                _,
            ) = build_product_insight_bundle(selected, report)
            validate_report(report)
            self.assertEqual(
                report["product_insight"]["sample"]["market_regions"],
                [
                    {
                        "market_region": "Europe",
                        "eligible_valid_text_entities": 1,
                        "share": 1.0,
                    }
                ],
            )
            self.assertTrue(full_evidence)
            self.assertTrue(
                all(
                    row.get("market_region") == "Europe"
                    and row.get("market_region_evidence")
                    for row in full_evidence
                )
            )

            _, same_manifest = materialize_snapshot(
                root, selected_scope, root / "var"
            )
            self.assertEqual(
                same_manifest["snapshot_id"], manifest["snapshot_id"]
            )
            _, all_manifest = materialize_snapshot(
                root, scope, root / "var"
            )
            self.assertNotEqual(
                all_manifest["snapshot_id"], manifest["snapshot_id"]
            )
            self.assertNotEqual(
                all_manifest["content_hash"], manifest["content_hash"]
            )

    def test_legacy_is_reproducible_but_cannot_mix_or_filter_regions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "legacy",
                "Other",
                [_row("legacy-review", "Other")],
                legacy=True,
            )
            legacy_scope = {
                **default_scope(),
                "category_id": "leaf_blower",
                "categories": ["Leaf Blower"],
                "job_ids": ["legacy"],
            }
            facts, quality, _ = build_review_fact(root, legacy_scope)
            self.assertEqual(len(facts), 1)
            self.assertEqual(quality["market_contract_mode"], "legacy")
            self.assertEqual(quality["legacy_contract_record_count"], 1)
            legacy_task = catalog(root)["tasks"][0]
            self.assertEqual(legacy_task["market_region"], "")
            self.assertEqual(
                legacy_task["market_contract_mode"], "legacy"
            )
            _, legacy_manifest = materialize_snapshot(
                root, legacy_scope, root / "var"
            )
            self.assertEqual(
                legacy_manifest["schema_version"], "snapshot-manifest/v1"
            )
            self.assertNotIn("filters", legacy_manifest)

            with self.assertRaisesRegex(MarketContractError, "旧契约"):
                build_review_fact(
                    root,
                    {
                        **legacy_scope,
                        "market_regions": ["North America"],
                    },
                )

            fixture.add_run(
                "current",
                "Europe",
                [_row("current-review", "Europe")],
            )
            with self.assertRaisesRegex(MarketContractError, "混合"):
                build_review_fact(
                    root,
                    {
                        **legacy_scope,
                        "job_ids": ["legacy", "current"],
                    },
                )

    def test_job_comment_and_amazon_region_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "mismatch",
                "Europe",
                [_row("wrong-region", "North America")],
            )
            with self.assertRaisesRegex(MarketContractError, "地区不一致"):
                build_review_fact(
                    root,
                    {
                        **default_scope(),
                        "category_id": "leaf_blower",
                        "categories": ["Leaf Blower"],
                        "job_ids": ["mismatch"],
                    },
                )

        for case, row_update, report_overrides, message in (
            (
                "missing-region-evidence",
                {"market_region_evidence": ""},
                {},
                "缺少 market_region_evidence",
            ),
            (
                "missing-valid-text",
                {"is_valid_text_review": ""},
                {},
                "必须是明确布尔值",
            ),
            (
                "validation-count-mismatch",
                {},
                {"eligible_reviews": 2},
                "eligible_reviews 与标准 CSV 不一致",
            ),
        ):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    fixture = SnapshotFixture(root)
                    row = _row(case, "Europe")
                    row.update(row_update)
                    fixture.add_run(
                        case,
                        "Europe",
                        [row],
                        report_overrides=report_overrides,
                    )
                    with self.assertRaisesRegex(
                        MarketContractError, message
                    ):
                        build_review_fact(
                            root,
                            {
                                **default_scope(),
                                "category_id": "leaf_blower",
                                "categories": ["Leaf Blower"],
                                "job_ids": [case],
                            },
                        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "validation-region-mismatch",
                "Europe",
                [_row("validation-region-mismatch", "Europe")],
                report_region="Australia",
            )
            with self.assertRaisesRegex(
                MarketContractError, "validation report 地区不一致"
            ):
                catalog(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "ineligible-csv-row",
                "Europe",
                [
                    _row(
                        "must-not-be-materialized",
                        "Europe",
                        eligible="false",
                    )
                ],
            )
            with self.assertRaisesRegex(
                MarketContractError, "CSV 只能包含 is_eligible=true"
            ):
                build_review_fact(
                    root,
                    {
                        **default_scope(),
                        "category_id": "leaf_blower",
                        "categories": ["Leaf Blower"],
                        "job_ids": ["ineligible-csv-row"],
                    },
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "amazon",
                "Europe",
                [
                    _row(
                        "amazon-review",
                        "Europe",
                        channel="amazon_reviews_2023",
                    )
                ],
                channel="amazon_reviews_2023",
                archive_through="2023-09-30",
            )
            with self.assertRaisesRegex(
                MarketContractError, "Amazon.*North America"
            ):
                catalog(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "amazon-missing-archive",
                "North America",
                [
                    _row(
                        "amazon-missing-archive-review",
                        "North America",
                        channel="amazon_reviews_2023",
                    )
                ],
                channel="amazon_reviews_2023",
            )
            with self.assertRaisesRegex(
                MarketContractError, "archive_through=2023-09-30"
            ):
                catalog(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "amazon-valid",
                "North America",
                [
                    _row(
                        "amazon-valid-review",
                        "North America",
                        channel="amazon_reviews_2023",
                    )
                ],
                channel="amazon_reviews_2023",
                archive_through="2023-09-30",
            )
            tasks = catalog(root)["tasks"]
            self.assertEqual(tasks[0]["market_region"], "North America")
            self.assertEqual(tasks[0]["market_contract_mode"], "current")
            amazon_scope = {
                **default_scope(),
                "category_id": "leaf_blower",
                "categories": ["Leaf Blower"],
                "job_ids": ["amazon-valid"],
                "market_regions": ["North America"],
            }
            facts, quality, _ = build_review_fact(root, amazon_scope)
            self.assertEqual(
                quality["historical_sources"],
                [
                    {
                        "channel": "amazon_reviews_2023",
                        "job_id": "amazon-valid",
                        "source_dataset": "",
                        "source_subset": "",
                        "archive_through": "2023-09-30",
                        "market_region": "North America",
                    }
                ],
            )
            _, manifest = materialize_snapshot(
                root,
                amazon_scope,
                root / "var",
            )
            report = build_report(
                facts,
                manifest,
                report_name="Amazon historical fixture",
            )
            validate_report(report)
            self.assertEqual(
                report["samples"]["historical_sources"][0][
                    "archive_through"
                ],
                "2023-09-30",
            )
            limitation = next(
                item
                for item in report["limitations"]
                if item["code"] == "amazon_static_history"
            )
            self.assertIn("不代表当前市场表现", limitation["message"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = SnapshotFixture(root)
            fixture.add_run(
                "other-valid",
                "Other",
                [_row("other-valid-review", "Other")],
            )
            facts, _, _ = build_review_fact(
                root,
                {
                    **default_scope(),
                    "category_id": "leaf_blower",
                    "categories": ["Leaf Blower"],
                    "job_ids": ["other-valid"],
                    "market_regions": ["Other"],
                },
            )
            self.assertEqual(
                [fact["market_region"] for fact in facts], ["Other"]
            )

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.


if __name__ == "__main__":
    unittest.main()
