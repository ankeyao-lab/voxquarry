from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from reporting.markdown_export import (
    content_disposition,
    report_export_basename,
    report_to_markdown,
)

if find_spec("fastapi") is not None:
    from fastapi.testclient import TestClient

    from reporting.app import create_app
else:
    TestClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment,misc]


class MarkdownExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = {
            "report_id": "rpt_demo",
            "title": "北美吹风机洞察",
            "generated_at": "2026-07-30T08:15:00Z",
            "analysis_version": "voxquarry-reporting/0.7.0",
            "data_snapshot_id": "snapshot_v2",
            "scope": {
                "categories": ["Leaf Blower/Vacuum"],
                "brands": ["EGO", "RYOBI"],
                "channels": ["lowes", "amazon_reviews_2023"],
                "models": ["LB6700"],
                "market_regions": ["North America"],
                "voltage_platform_keys": ["ego|56V"],
                "include_unknown_voltage_platform": False,
                "voltage_platforms": [
                    {
                        "platform_key": "ego|56V",
                        "brand": "EGO",
                        "canonical_voltage": "56V",
                        "display_name": "EGO 56V",
                    }
                ],
            },
            "samples": {
                "review_entities": 1200,
                "written_review_entities": 900,
                "average_rating": 4.4,
                "positive_rate": 0.84,
                "negative_rate": 0.08,
            },
            "product_insight": {
                "sample": {
                    "review_entities": 1200,
                    "written_review_entities": 900,
                    "average_rating": 4.4,
                    "positive_rate": 0.84,
                    "negative_rate": 0.08,
                },
                "smart_opportunities": [
                    {
                        "code": "A1",
                        "title": "故障诊断与恢复引导",
                        "method": "能力补全",
                        "user_need": "停机后快速判断原因并恢复任务。",
                        "smart_function": "结合故障码和状态给出原因候选与恢复步骤。",
                        "claim_basis": "problem_to_capability_hypothesis",
                        "source_problem_entities": 82,
                        "required_data": "故障码、部件状态",
                        "guardrail": "显示不确定性并保留人工入口。",
                    }
                ],
                "evidence_access": {
                    "evidence_rows": 430,
                    "evidence_groups": 18,
                    "unique_review_entities": 311,
                },
            },
        }

    def test_filename_contains_title_category_and_time(self) -> None:
        basename = report_export_basename(self.report)
        self.assertIn("北美吹风机洞察", basename)
        self.assertIn("Leaf Blower-Vacuum", basename)
        self.assertIn("20260730-0815", basename)
        self.assertIn("voxquarry-reporting-0.7.0", basename)
        disposition = content_disposition(self.report, "md")
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn(".md", disposition)

    def test_markdown_contains_scope_metrics_and_detailed_smart_fields(
        self,
    ) -> None:
        markdown = report_to_markdown(self.report)
        self.assertIn(
            "# 北美吹风机洞察｜Leaf Blower/Vacuum｜2026-07-30｜voxquarry-reporting/0.7.0",
            markdown,
        )
        self.assertIn("正文实体：900", markdown)
        self.assertIn("品牌：EGO、RYOBI", markdown)
        self.assertIn("电压平台：EGO 56V", markdown)
        self.assertIn("用户需求：停机后快速判断原因并恢复任务。", markdown)
        self.assertIn(
            "智能功能：结合故障码和状态给出原因候选与恢复步骤。",
            markdown,
        )
        self.assertIn("完整证据行：430", markdown)

    def test_markdown_discloses_unknown_and_historical_platform_scope(self) -> None:
        unknown = json.loads(json.dumps(self.report))
        unknown["scope"]["voltage_platform_keys"] = []
        unknown["scope"]["include_unknown_voltage_platform"] = True
        unknown["scope"]["voltage_platforms"] = []
        self.assertIn(
            "电压平台：未知 / 未绑定",
            report_to_markdown(unknown),
        )

        historical = json.loads(json.dumps(self.report))
        for field in (
            "voltage_platform_keys",
            "include_unknown_voltage_platform",
            "voltage_platforms",
        ):
            historical["scope"].pop(field, None)
        self.assertIn(
            "电压平台：未明确（历史报告）",
            report_to_markdown(historical),
        )

    def test_markdown_discloses_controlled_energy_and_selection_audit(self) -> None:
        report = json.loads(json.dumps(self.report))
        report["scope"].update({
            "category_id": "pressure_washer",
            "categories": ["Pressure Washer"],
            "energy_type": "battery",
            "energy": {
                "schema_version": "voxquarry.task-energy/v1",
                "energy_type": "battery",
                "label": "电池",
            },
            "energy_selection": {
                "schema_version": "voxquarry.reporting-energy-selection/v1",
                "source_snapshot": {
                    "snapshot_id": "prod_v2_" + "2" * 24,
                    "schema_version": "voxquarry-reporting-snapshot/v2",
                    "content_sha256": "sha256:" + "8" * 64,
                },
                "category_reportable_job_count": 9,
                "category_energy_job_count": 3,
                "selected_job_count": 3,
                "excluded_other_energy_job_count": 6,
                "selected_governed_job_count": 3,
                "selected_legacy_null_job_count": 0,
            },
        })
        markdown = report_to_markdown(report)
        self.assertIn("能源类型：电池（battery）", markdown)
        self.assertIn("当前能源可报告任务：3", markdown)
        self.assertIn("本报告纳入任务：3", markdown)
        self.assertIn("排除其他能源任务：6", markdown)

        historical = json.loads(json.dumps(self.report))
        historical["scope"].pop("energy_type", None)
        self.assertIn(
            "能源类型：未知（历史报告未保存）",
            report_to_markdown(historical),
        )

    @unittest.skipIf(TestClient is None, "FastAPI runtime dependency unavailable")
    def test_list_and_detail_exports_resolve_to_same_saved_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            with patch(
                "reporting.app.build_catalog_payload",
                return_value={"tasks": [], "defaults": {"scope": {}}},
            ):
                app = create_app(snapshot, root / "var")
            app.state.reporting.store.store_report(self.report)

            with TestClient(app) as client:
                for extension, endpoint in (
                    ("json", "json"),
                    ("md", "markdown"),
                ):
                    from_list = client.get(
                        f"/reporting/api/v1/reports/rpt_demo/export/{endpoint}"
                    )
                    from_detail = client.get(
                        f"/reporting/api/v1/reports/rpt_demo/export/{endpoint}"
                    )
                    self.assertEqual(from_list.status_code, 200)
                    self.assertEqual(from_detail.status_code, 200)
                    self.assertEqual(
                        hashlib.sha256(from_list.content).hexdigest(),
                        hashlib.sha256(from_detail.content).hexdigest(),
                    )
                    self.assertEqual(
                        from_list.headers["content-disposition"],
                        from_detail.headers["content-disposition"],
                    )
                    self.assertIn(f".{extension}", from_list.headers["content-disposition"])
            self.assertEqual(
                app.state.reporting.store.get_report("rpt_demo")["title"],
                "北美吹风机洞察",
            )


if __name__ == "__main__":
    unittest.main()
