from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from reporting.analysis import build_report
from reporting.config import ANALYSIS_VERSION
from reporting.contracts import (
    ContractError,
    automatic_report_title,
    default_scope,
    normalize_request,
)
from reporting.storage import ReportStore
from reporting.tests.test_reporting import fixture_facts, manifest
from reporting.validation import validate_report


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def report_summary(report: dict) -> dict:
    tables = {table["table_id"]: table for table in report["tables"]}
    return {
        "analysis_version": report["analysis_version"],
        "llm_used": report["method"]["llm_used"],
        "metrics": {
            metric["metric_id"]: metric["value"]
            for metric in report["metrics"]
        },
        "samples": report["samples"],
        "schema_version": report["schema_version"],
        "sections": [
            section["section_id"] for section in report["sections"]
        ],
        "table_ids": [table["table_id"] for table in report["tables"]],
        "top_attribute": tables["attribute_performance"]["rows"][0][
            "factor_id"
        ],
        "top_pain": tables["pain_points"]["rows"][0]["factor_id"],
        "top_use_case": tables["use_cases"]["rows"][0]["factor_id"],
    }


class ContractAndStorageTests(unittest.TestCase):
    def test_automatic_report_title_summarizes_primary_scope(self) -> None:
        self.assertEqual(
            automatic_report_title(
                {"brands": ["Milwaukee"], "channels": []}
            ),
            "Milwaukee 品牌全渠道量化洞察",
        )
        self.assertEqual(
            automatic_report_title(
                {"brands": ["EGO", "RYOBI"], "channels": ["lowes"]}
            ),
            "2 个品牌 lowes 渠道量化洞察",
        )
        self.assertEqual(
            automatic_report_title(
                {
                    "brands": ["EGO", "RYOBI"],
                    "channels": ["ego", "lowes"],
                }
            ),
            "2 个品牌 2 个渠道量化洞察",
        )
        self.assertEqual(
            automatic_report_title(
                {
                    "brands": ["Milwaukee"],
                    "channels": [],
                    "voltage_platforms": [{
                        "brand": "Milwaukee",
                        "canonical_voltage": "18V",
                    }],
                }
            ),
            "Milwaukee 18V 平台全渠道量化洞察",
        )
        self.assertEqual(
            automatic_report_title(
                {
                    "brands": ["EGO", "Greenworks"],
                    "channels": [],
                    "voltage_platforms": [
                        {"brand": "EGO", "canonical_voltage": "56V"},
                        {
                            "brand": "Greenworks",
                            "canonical_voltage": "56V",
                        },
                    ],
                }
            ),
            "2 个电压平台全渠道量化洞察",
        )
        self.assertEqual(
            automatic_report_title(
                {
                    "brands": ["EGO"],
                    "channels": [],
                    "include_unknown_voltage_platform": True,
                }
            ),
            "EGO 未知电压平台全渠道量化洞察",
        )

    def test_empty_report_name_uses_scope_summary_not_category_or_time(self) -> None:
        request = normalize_request(
            {
                "scope": {
                    "categories": ["String Trimmer"],
                    "brands": ["Milwaukee"],
                    "channels": [],
                }
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        self.assertEqual(
            request["report_name"],
            "Milwaukee 品牌全渠道量化洞察",
        )

    def test_pressure_washer_title_and_contract_require_one_energy_type(self) -> None:
        request = normalize_request(
            {
                "scope": {
                    "category_id": "pressure_washer",
                    "categories": ["Pressure Washer"],
                    "brands": ["EGO"],
                    "energy_type": "battery",
                }
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        self.assertEqual(
            request["report_name"],
            "电池 EGO 品牌全渠道量化洞察",
        )
        with self.assertRaisesRegex(ContractError, "能源类型"):
            normalize_request(
                {
                    "scope": {
                        "category_id": "pressure_washer",
                        "categories": ["Pressure Washer"],
                    }
                },
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )
        with self.assertRaisesRegex(ContractError, "仅适用于"):
            normalize_request(
                {
                    "scope": {
                        "category_id": "leaf_blower",
                        "categories": ["Leaf Blower/Vacuum"],
                        "energy_type": "fuel",
                    }
                },
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )

    def test_voltage_platform_scope_accepts_wotu_keys_and_drops_client_refs(self) -> None:
        self.assertEqual(default_scope()["voltage_platform_keys"], [])
        with self.assertRaisesRegex(ContractError, r"brand\|voltage"):
            normalize_request(
                {
                    "scope": {
                        "categories": ["String Trimmer"],
                        "voltage_platform_keys": ["ego-56v"],
                    }
                },
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )
        request = normalize_request(
            {
                "scope": {
                    "categories": ["String Trimmer"],
                    "voltage_platform_keys": ["ego|56V"],
                    "include_unknown_voltage_platform": True,
                    "voltage_platforms": [{"platform_key": "client-forged"}],
                    "voltage_platform_refs": [{"job_id": "client-forged"}],
                    "product_master": {"snapshot_id": "client-forged"},
                    "model_refs": [{"model_id": "client-forged"}],
                }
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        self.assertEqual(
            request["scope"]["voltage_platform_keys"],
            ["ego|56V"],
        )
        self.assertTrue(request["scope"]["include_unknown_voltage_platform"])
        self.assertNotIn("voltage_platforms", request["scope"])
        self.assertNotIn("voltage_platform_refs", request["scope"])
        self.assertNotIn("product_master", request["scope"])
        self.assertNotIn("model_refs", request["scope"])

    def test_cross_category_bundle_has_explicit_rejection(self) -> None:
        for scope in (
            {"category_id": "cross_category_bundle"},
            {"categories": ["Cross-category Bundle"]},
        ):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(
                    ContractError,
                    "Cross-category Bundle.*不支持生成单品类量化报告",
                ):
                    normalize_request(
                        {"scope": scope},
                        defaults=default_scope(),
                        analysis_version=ANALYSIS_VERSION,
                    )

    def test_request_contract_normalizes_order_and_scope(self) -> None:
        request = normalize_request(
            {
                "report_name": "  Mower   review  ",
                "primary_direction": "product-opportunity",
                "analysis_modules": [
                    "smart-opportunity",
                    "pain-and-failure",
                    "smart-opportunity",
                ],
                "factors": [
                    "smart-signals",
                    "product-opportunities",
                    "pain-points",
                ],
                "scope": {
                    "categories": ["割草机"],
                    "ratings": [1, "5", 1],
                    "keywords": [" runtime ", "battery", "battery"],
                    "keyword_mode": "exact",
                    "keyword_field": "body",
                },
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        self.assertEqual(request["report_name"], "Mower review")
        self.assertEqual(
            request["analysis_modules"],
            ["pain-and-failure", "smart-opportunity"],
        )
        self.assertEqual(request["scope"]["ratings"], [5, 1])
        self.assertEqual(
            request["scope"]["keywords"], ["battery", "runtime"]
        )
        self.assertEqual(request["scope"]["keyword_mode"], "exact")
        self.assertEqual(request["scope"]["keyword_field"], "body")
        self.assertEqual(request["scope"]["job_ids"], [])
        self.assertEqual(
            request["scope"]["category_id"], "walk_behind_mower"
        )
        self.assertEqual(
            request["scope"]["categories"], ["Walk-Behind Mower"]
        )

    def test_request_contract_rejects_unrelated_primary_factors(self) -> None:
        with self.assertRaises(ContractError):
            normalize_request(
                {
                    "primary_direction": "product-opportunity",
                    "factors": ["ratings"],
                    "scope": {"categories": ["Walk-Behind Mower"]},
                },
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )

    def test_request_contract_requires_one_stable_category(self) -> None:
        for categories in ([], ["Leaf Blower", "Snow Blower"]):
            with self.subTest(categories=categories):
                with self.assertRaises(ContractError):
                    normalize_request(
                        {
                            "primary_direction": "product-opportunity",
                            "scope": {"categories": categories},
                        },
                        defaults=default_scope(),
                        analysis_version=ANALYSIS_VERSION,
                    )

        with self.assertRaisesRegex(ContractError, "不一致"):
            normalize_request(
                {
                    "primary_direction": "product-opportunity",
                    "scope": {
                        "category_id": "leaf_blower",
                        "categories": ["Snow Blower"],
                    },
                },
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )

    def test_request_defaults_to_complete_factor_pack(self) -> None:
        request = normalize_request(
            {
                "primary_direction": "product-opportunity",
                "scope": {"categories": ["Snow Shovel"]},
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        self.assertEqual(
            request["factors"],
            [
                "ratings",
                "recommendation",
                "written-coverage",
                "attributes",
                "pain-points",
                "use-cases",
                "explicit-age",
                "user-traits",
                "keyword-document-frequency",
                "factor-cooccurrence",
                "kano-candidates",
                "product-opportunities",
                "smart-signals",
            ],
        )

    def test_selected_scene_and_factors_control_report_sections(self) -> None:
        facts = fixture_facts()
        report = build_report(
            facts,
            manifest(facts),
            research_plan={
                "schema_version": "research-plan/v1",
                "id": "fixture-opportunity",
                "version": "1.0.0",
                "primary_direction": "product-opportunity",
                "analysis_modules": [],
                "factors": ["product-opportunities"],
                "scope": {},
                "comparison": {"minimum_n": 20, "alpha": 0.05},
                "quality_gates": {},
                "outputs": ["tables", "findings"],
            },
        )
        self.assertEqual(
            [section["section_id"] for section in report["sections"]],
            ["atomic_v2", "product_opportunity"],
        )
        self.assertEqual(
            [table["table_id"] for table in report["tables"]],
            [
                "atomic_script_execution",
                "atomic_label_summary",
                "product_opportunities",
            ],
        )
        validate_report(report)

    def test_golden_report_summary(self) -> None:
        facts = fixture_facts()
        report = build_report(
            facts, manifest(facts), report_name="Fixture golden"
        )
        expected = json.loads(
            (FIXTURE_DIR / "golden-report-summary-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report_summary(report), expected)

    def test_archive_restore_and_retry_are_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            facts = fixture_facts()
            report = build_report(facts, manifest(facts))
            store.store_report(report)
            job = store.create_job(
                {
                    "report_name": report["title"],
                    "scope": report["scope"],
                    "analysis_version": "test",
                }
            )
            store.update_job(
                job["job_id"],
                "completed",
                "已完成",
                100,
                report_id=report["report_id"],
            )
            store.set_archived(report["report_id"], True)
            self.assertEqual(store.list_reports(), [])
            self.assertEqual(store.list_cards_with_jobs(), [])
            self.assertTrue(
                store.list_reports(include_archived=True)[0]["archived"]
            )
            archived_cards = store.list_cards_with_jobs(
                include_archived=True
            )
            self.assertEqual(len(archived_cards), 1)
            self.assertEqual(
                archived_cards[0]["report_id"], report["report_id"]
            )
            self.assertTrue(archived_cards[0]["archived"])
            store.set_archived(report["report_id"], False)
            self.assertFalse(store.list_reports()[0]["archived"])
            restored_cards = store.list_cards_with_jobs()
            self.assertEqual(len(restored_cards), 1)
            self.assertEqual(
                restored_cards[0]["report_id"], report["report_id"]
            )

            failed_job = store.create_job(
                {"scope": {}, "analysis_version": "test"},
                idempotency_key="archive-retry-fixture",
            )
            store.update_job(
                failed_job["job_id"],
                "failed",
                "生成失败",
                100,
                error={"code": "TEST", "message": "fixture", "retryable": True},
            )
            retried = store.retry_job(failed_job["job_id"])
            self.assertNotEqual(retried["job_id"], failed_job["job_id"])
            self.assertEqual(retried["status"], "queued")

            blocked = store.create_job(
                {"scope": {}, "analysis_version": "test"}
            )
            store.update_job(
                blocked["job_id"],
                "failed",
                "范围未达到报告门槛",
                100,
                error={
                    "code": "REPORT_SCOPE_NOT_READY",
                    "message": "fixture scope is below pilot threshold",
                    "retryable": False,
                },
            )
            self.assertEqual(
                store.retry_job(blocked["job_id"])["job_id"],
                blocked["job_id"],
            )

            legacy = store.create_job(
                {"scope": {}, "analysis_version": "legacy-test"}
            )
            store.update_job(
                legacy["job_id"],
                "failed",
                "生成失败",
                100,
                error={
                    "code": "REPORT_GENERATION_FAILED",
                    "message": "当前范围只有 132 条正文，低于 pilot 门槛 500 条。",
                    "retryable": True,
                },
            )
            self.assertEqual(
                store.retry_job(legacy["job_id"])["job_id"],
                legacy["job_id"],
            )
            legacy_card = next(
                item
                for item in store.list_cards_with_jobs()
                if item.get("job_id") == legacy["job_id"]
            )
            self.assertFalse(legacy_card["retryable"])

    def test_completed_jobs_without_authoritative_reports_never_become_cards(self) -> None:
        """Production had 52 completed ghost cards with an empty report_id."""
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            for index in range(52):
                job = store.create_job(
                    {
                        "report_name": f"Production ghost fixture {index + 1}",
                        "scope": {"category_id": "patio_cleaner"},
                        "analysis_version": "test",
                    },
                    idempotency_key=f"production-ghost-{index}",
                )
                store.update_job(
                    job["job_id"],
                    "completed",
                    "已完成",
                    100,
                )
            cards = store.list_cards_with_jobs()
            self.assertEqual(cards, [])
            self.assertFalse(
                any(
                    card.get("status") == "completed"
                    and not card.get("report_id")
                    for card in cards
                )
            )

    def test_card_cache_retries_when_job_is_replaced_during_list_build(self) -> None:
        """An older job read must not be cached under the newer file state."""
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            facts = fixture_facts()
            report = build_report(facts, manifest(facts))
            job = store.create_job(
                {
                    "report_name": report["title"],
                    "scope": report["scope"],
                    "analysis_version": "test",
                },
                idempotency_key="two-phase-card-cache",
            )
            store.update_job(job["job_id"], "completed", "已完成", 100)

            original_list_jobs = store.list_jobs
            switched = False

            def list_jobs_during_publish():
                nonlocal switched
                stale_jobs = original_list_jobs()
                if not switched:
                    switched = True
                    store.store_report(report)
                    store.update_job(
                        job["job_id"],
                        "completed",
                        "已完成",
                        100,
                        report_id=report["report_id"],
                    )
                return stale_jobs

            store.list_jobs = list_jobs_during_publish  # type: ignore[method-assign]
            cards = store.list_cards_with_jobs()
            store.list_jobs = original_list_jobs  # type: ignore[method-assign]

            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["status"], "completed")
            self.assertEqual(cards[0]["report_id"], report["report_id"])
            self.assertEqual(
                store.list_cards_with_jobs()[0]["report_id"],
                report["report_id"],
            )

    def test_card_signature_detects_job_replace_when_directory_mtime_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            job = store.create_job(
                {"scope": {}, "analysis_version": "file-signature-test"}
            )
            directory_stat = store.jobs_dir.stat()
            before = store._card_source_signature()
            store.update_job(job["job_id"], "analyzing", "分析中", 33)
            os.utime(
                store.jobs_dir,
                ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns),
            )
            self.assertEqual(
                store.jobs_dir.stat().st_mtime_ns,
                directory_stat.st_mtime_ns,
            )
            self.assertNotEqual(store._card_source_signature(), before)

    def test_retry_attempts_collapse_into_one_successful_report_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            request = {
                "report_name": "Patio Cleaner | 量化洞察 | 2026-08-11 11:05",
                "scope": {
                    "category_id": "patio_cleaner",
                    "categories": ["Patio Cleaner"],
                    "job_ids": ["job-a", "job-b", "job-c"],
                },
                "analysis_version": "test",
            }
            failed = store.create_job(request)
            store.update_job(
                failed["job_id"],
                "failed",
                "服务中断",
                56,
                error={
                    "code": "REPORT_SERVICE_INTERRUPTED",
                    "message": "报告服务更新期间中断，可安全重试。",
                    "retryable": True,
                },
            )
            retried = store.retry_job(failed["job_id"])
            facts = fixture_facts()
            report = build_report(
                facts,
                manifest(facts),
                report_name=request["report_name"],
            )
            store.store_report(report)
            store.update_job(
                retried["job_id"],
                "completed",
                "已完成",
                100,
                report_id=report["report_id"],
            )

            matching = [
                card
                for card in store.list_cards_with_jobs()
                if card.get("title") == request["report_name"]
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["status"], "completed")
            self.assertEqual(matching[0]["attempt_count"], 2)
            self.assertTrue(matching[0]["job_group_id"].startswith("rgrp_"))
            self.assertEqual(
                [row["status"] for row in matching[0]["attempts"]],
                ["completed", "failed"],
            )
            history = store.get_job_group(matching[0]["job_group_id"])
            self.assertEqual(history["attempt_count"], 2)
            self.assertEqual(history["attempts"][0]["status"], "completed")
            self.assertEqual(history["attempts"][1]["progress"], 56)
            self.assertEqual(retried["retry_of"], failed["job_id"])
            self.assertEqual(retried["root_job_id"], failed["job_id"])

    def test_service_restart_requeues_unfinished_jobs_without_fake_100_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            running = store.create_job(
                {"report_name": "running", "scope": {}, "analysis_version": "test"}
            )
            queued = store.create_job(
                {"report_name": "queued", "scope": {}, "analysis_version": "test"},
                idempotency_key="queued-recovery-fixture",
            )
            store.update_job(
                running["job_id"], "extracting", "全量原子提取", 56
            )

            recovered = store.requeue_interrupted_jobs()
            self.assertEqual(recovered, [running["job_id"]])
            running_after = store.get_job(running["job_id"])
            queued_after = store.get_job(queued["job_id"])
            self.assertEqual(running_after["status"], "queued")
            self.assertEqual(running_after["progress"], 0)
            self.assertEqual(running_after["last_progress"], 56)
            self.assertEqual(
                running_after["stage_label"], "服务恢复，等待重新生成"
            )
            self.assertEqual(queued_after["status"], "queued")
            self.assertEqual(queued_after["progress"], 0)

    def test_only_failed_report_jobs_can_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(Path(directory))
            failed = store.create_job(
                {"report_name": "failed fixture", "scope": {}, "analysis_version": "test"}
            )
            store.update_job(
                failed["job_id"],
                "failed",
                "生成失败",
                100,
                error={"code": "TEST", "message": "fixture", "retryable": True},
            )

            deleted = store.delete_failed_job(failed["job_id"])
            self.assertEqual(
                deleted,
                {"job_id": failed["job_id"], "deleted": True},
            )
            self.assertIsNone(store.get_job(failed["job_id"]))
            self.assertNotIn(
                failed["job_id"],
                {item.get("job_id") for item in store.list_cards_with_jobs()},
            )

            queued = store.create_job(
                {"report_name": "queued fixture", "scope": {}, "analysis_version": "test"},
                idempotency_key="queued-delete-guard",
            )
            with self.assertRaisesRegex(ValueError, "only failed report jobs"):
                store.delete_failed_job(queued["job_id"])
            self.assertIsNotNone(store.get_job(queued["job_id"]))

            facts = fixture_facts()
            report = build_report(facts, manifest(facts), report_name="immutable fixture")
            store.store_report(report)
            linked = store.create_job(
                {"report_name": "linked fixture", "scope": {}, "analysis_version": "test"},
                idempotency_key="linked-delete-guard",
            )
            store.update_job(
                linked["job_id"],
                "failed",
                "生成失败",
                100,
                report_id=report["report_id"],
                error={"code": "TEST", "message": "fixture", "retryable": False},
            )
            with self.assertRaisesRegex(ValueError, "immutable report artifact"):
                store.delete_failed_job(linked["job_id"])
            self.assertIsNotNone(store.get_report(report["report_id"]))

            with self.assertRaises(KeyError):
                store.delete_failed_job("../reports/rpt_fixture")
            self.assertIsNotNone(store.get_report(report["report_id"]))


if __name__ == "__main__":
    unittest.main()
