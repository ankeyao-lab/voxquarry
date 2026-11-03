from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from reporting.analysis import build_report
from reporting.agent_tools import (
    AgentToolError,
    AgentToolService,
    authority_from_gateway,
)
from reporting.brand_persona import (
    BRAND_PERSONA_CACHE_VERSION,
    BRAND_PERSONA_METHOD_VERSION,
    PERSONA_NARRATIVE_POLICY_VERSION,
    PERSONA_NARRATIVE_SCHEMA_VERSION,
    BrandPersonaContractError,
    build_brand_persona_analysis,
    normalize_brand_persona_scope,
    resolve_brand_persona_job_ids,
    UNKNOWN_CATEGORY_ID,
    UNKNOWN_CATEGORY_NAME,
)
from reporting.brand_assets import brand_asset
from reporting.brand_persona_store import (
    BrandPersonaStore,
    public_brand_persona_analysis,
)
from reporting.category_vocabulary import category_catalog
from reporting.config import (
    ANALYSIS_VERSION,
    DEFAULT_FRONTEND_ROOT,
    DEFAULT_VAR_ROOT,
    PACKAGE_ROOT,
    MARKET_REGIONS,
)
from reporting.contracts import (
    ContractError,
    FACTOR_IDS,
    SCENE_FACTORS,
    automatic_report_title,
    default_scope,
    normalize_request,
    normalize_scope,
)
from reporting.controlled_energy import (
    ControlledEnergyError,
    resolve_controlled_energy_scope,
)
from reporting.factors import explicit_age_group
from reporting.markdown_export import (
    content_disposition,
    report_export_basename,
    report_to_markdown,
)
from reporting.job_runtime import JobSupervisor, RuntimePolicy, read_worker_status
from reporting.evidence_store import (
    EvidenceRepository,
    csv_lines,
    ndjson_lines,
)
from reporting.evidence_reviews import enrich_review_text, prepare_review_text
from reporting.product_insight import (
    build_product_insight_bundle,
    product_insight_support,
    write_extraction_ledger,
)
from reporting.report_display_name import (
    REPORT_MANAGEMENT_TOKEN_ENV,
    ReportManagementAuthorityError,
    authority_from_report_management_gateway,
)
from reporting.voltage_platform import (
    VoltagePlatformError,
    resolve_voltage_platform_scope,
)
from reporting.persona_services import (
    QwenPersonaClient,
    optional_env_boolean,
    search_pexels_image,
)
from reporting.source import (
    build_review_fact,
    catalog,
    load_jobs,
    materialize_snapshot,
    model_scope_value,
    validate_scope_job_ids,
)
from reporting.source_completeness import enrich_evidence_rows
from reporting.storage import (
    ReportDisplayNameConflict,
    ReportDisplayNameLedgerError,
    ReportStore,
)
from reporting.structured_refresh_history import read_diagnostics_sidecar
from reporting.validation import validate_report


SNAPSHOT_ID_PATTERN = re.compile(r"^snap_[0-9a-f]{24}$")


def report_review_json_lines(
    report: dict[str, Any], fact_path: Path
) -> tuple[int, Iterator[str]]:
    """Stream the immutable Review Fact population used by one report.

    The export deliberately exposes normalized report inputs rather than
    connector-specific raw audit payloads.  This preserves report
    reproducibility and prevents a later active snapshot from changing an
    existing report export.
    """
    record_count = 0
    with gzip.open(fact_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                json.loads(line)
                record_count += 1

    def lines() -> Iterator[str]:
        header = {
            "schema_version": "report-review-export/v1",
            "report_id": report["report_id"],
            "data_snapshot_id": report["data_snapshot_id"],
            "record_count": record_count,
            "record_type": "review-fact",
            "records": [],
        }
        prefix = json.dumps(
            {key: value for key, value in header.items() if key != "records"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        yield prefix[:-1] + ',"records":['
        first = True
        with gzip.open(fact_path, "rt", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if not first:
                    yield ","
                yield line
                first = False
        yield "]}\n"

    return record_count, lines()


SCENE_CATALOG = [
    {
        "id": "satisfaction-overview",
        "label": "满意度总览",
        "question": "评分、好差评率、推荐率和趋势如何？",
    },
    {
        "id": "pain-and-failure",
        "label": "痛点与故障分析",
        "question": "哪些故障、症状与使用后果最突出？",
    },
    {
        "id": "product-attribute-performance",
        "label": "产品属性表现",
        "question": "各属性的提及、Pros / Cons 和 Kano 候选如何？",
    },
    {
        "id": "needs-and-use-cases",
        "label": "用户需求和使用场景",
        "question": "用户要完成什么任务，受什么环境和能力约束？",
    },
    {
        "id": "product-opportunity",
        "label": "产品机会",
        "question": "哪些未满足结果形成可追溯的网评证据机会？",
    },
    {
        "id": "brand-product-comparison",
        "label": "品牌 / 产品竞品对比",
        "question": "品牌和产品在评分、覆盖和效应量上有何差异？",
    },
    {
        "id": "channel-comparison",
        "label": "渠道差异",
        "question": "渠道样本、正文覆盖与表现有何差异？",
    },
    {
        "id": "model-time-comparison",
        "label": "型号 / 时间段对比",
        "question": "型号和时期的表现是否有稳定变化？",
    },
    {
        "id": "user-profile",
        "label": "用户特征与画像",
        "question": "评论中明确表达了哪些年龄、特征和规则群组？",
    },
    {
        "id": "smart-opportunity",
        "label": "智能化机会",
        "question": "A1–A8 中哪些感知、判断、动作与反馈机会有证据？",
    },
]
FACTOR_CATALOG = [
    {"id": "ratings", "label": "评分、好评率与趋势", "status": "available"},
    {"id": "recommendation", "label": "推荐率与字段覆盖", "status": "available"},
    {"id": "written-coverage", "label": "正文与字段覆盖", "status": "available"},
    {"id": "attributes", "label": "属性提及与 Pros / Cons", "status": "available"},
    {"id": "pain-points", "label": "痛点、故障与后果", "status": "available"},
    {"id": "use-cases", "label": "需求与使用场景", "status": "available"},
    {
        "id": "explicit-age",
        "label": "明确年龄与用户特征",
        "status": "conditional",
    },
    {"id": "user-traits", "label": "规则用户群组", "status": "conditional"},
    {"id": "keyword-document-frequency", "label": "n-gram 文档频次", "status": "available"},
    {
        "id": "factor-cooccurrence",
        "label": "原子因子文档共现",
        "status": "available",
    },
    {"id": "kano-candidates", "label": "网评 Kano 候选", "status": "conditional"},
    {"id": "product-opportunities", "label": "产品机会证据", "status": "available"},
    {"id": "smart-signals", "label": "A1–A8 智能化信号", "status": "conditional"},
    {
        "id": "verified-purchaser",
        "label": "验证购买",
        "status": "unavailable",
        "reason": "当前数据无法区分 false 与 missing",
    },
]


def build_catalog_payload(snapshot_root: Path) -> dict[str, Any]:
    """Build the creation catalog from the latest readable source state."""
    source_catalog = catalog(snapshot_root)
    registry = category_catalog()
    profile_support_by_id = {
        str(definition["category_id"]): product_insight_support(
            {
                "scope": {
                    "category_id": definition["category_id"],
                    "categories": [definition["display_name"]],
                }
            }
        )
        for definition in registry
    }
    factors = []
    for factor in FACTOR_CATALOG:
        recommended_scenes = [
            scene_id
            for scene_id, factor_ids in SCENE_FACTORS.items()
            if factor["id"] in factor_ids
        ]
        factors.append({**factor, "recommended_scenes": recommended_scenes})
    return {
        **source_catalog,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "complete_first_partial_fallback_reporting_artifacts",
        "scenes": SCENE_CATALOG,
        "factors": factors,
        "category_registry": registry,
        "category_profiles": {
            category: product_insight_support(
                {"scope": {"categories": [category]}}
            )
            for category in source_catalog["categories"]
        },
        "category_profiles_by_id": profile_support_by_id,
        "defaults": {
            "scope": default_scope(),
            "primary_direction": "product-opportunity",
            "analysis_modules": [
                item["id"]
                for item in SCENE_CATALOG
                if item["id"] != "product-opportunity"
            ],
            "factors": [
                item["id"]
                for item in FACTOR_CATALOG
                if item["status"] != "unavailable"
            ],
        },
    }


class ReportingService:
    def __init__(
        self,
        snapshot_root: Path,
        var_root: Path,
        *,
        execution_mode: str | None = None,
    ):
        self.snapshot_root = snapshot_root
        self.var_root = var_root
        self.store = ReportStore(var_root)
        self.evidence = EvidenceRepository(var_root)
        self.brand_personas = BrandPersonaStore(var_root)
        # A production snapshot is immutable for the lifetime of this service.
        # Building the catalog for every request rescans hundreds of validated
        # artifacts and can starve the UI while a CPU-heavy report is running.
        # Activating a new snapshot restarts the container, so one cached
        # payload per process remains current and deterministic.
        self.catalog_payload = build_catalog_payload(snapshot_root)
        self._threads: dict[str, threading.Thread] = {}
        self._brand_persona_threads: dict[str, threading.Thread] = {}
        # Report generation performs full-scope extraction and Evidence SQLite
        # writes.  A single process must never run those CPU / memory intensive
        # stages concurrently; additional requests stay in their durable
        # ``queued`` state until this slot is available.
        self._run_slot = threading.Lock()
        self._active_job_id: str | None = None
        environment = dict(os.environ)
        if execution_mode is None and "VOXQUARRY_REPORT_EXECUTION_MODE" not in environment:
            # Unit tests and local vertical slices retain the historical
            # in-process behavior.  The production image explicitly selects
            # external mode and never executes analysis inside the API.
            environment["VOXQUARRY_REPORT_EXECUTION_MODE"] = "thread"
        elif execution_mode is not None:
            environment["VOXQUARRY_REPORT_EXECUTION_MODE"] = execution_mode
        self.runtime_policy = RuntimePolicy.from_environment(environment)
        self.execution_mode = self.runtime_policy.execution_mode
        self.supervisor: JobSupervisor | None = None
        if self.execution_mode == "process":
            self.supervisor = JobSupervisor(
                snapshot_root=self.snapshot_root,
                var_root=self.var_root,
                policy=self.runtime_policy,
            )
            self.supervisor.start()
        elif self.execution_mode == "thread":
            self.store.requeue_interrupted_jobs()
            self.brand_personas.requeue_interrupted_jobs()
            for job in self.store.list_jobs():
                if job.get("status") == "queued":
                    self.start_job(str(job["job_id"]))
            for job in self.brand_personas.list_jobs():
                if job.get("status") == "queued":
                    self.start_brand_persona_job(str(job["job_id"]))

    def close(self) -> None:
        if self.supervisor is not None:
            self.supervisor.close(terminate=True)

    def _brand_persona_tasks(self) -> list[dict[str, Any]]:
        """Normalize task metadata without changing the immutable source catalog."""
        tasks: list[dict[str, Any]] = []
        for source in self.catalog_payload.get("tasks") or []:
            task = dict(source)
            if not str(task.get("category_id") or "").strip():
                source_category = str(task.get("category") or "").strip()
                if source_category:
                    task["source_category"] = source_category
                task["category_id"] = UNKNOWN_CATEGORY_ID
                task["category"] = UNKNOWN_CATEGORY_NAME
            elif not str(task.get("category") or "").strip():
                task["category"] = UNKNOWN_CATEGORY_NAME
            tasks.append(task)
        return tasks

    def brand_persona_catalog(self) -> dict[str, Any]:
        tasks = self._brand_persona_tasks()
        brand_tasks: dict[str, list[dict[str, Any]]] = {}
        canonical_labels: dict[str, str] = {}
        for task in tasks:
            label = str(task.get("brand") or "").strip()
            if not label:
                continue
            key = label.casefold()
            canonical_labels.setdefault(key, label)
            brand_tasks.setdefault(key, []).append(task)
        brands = []
        for key, rows in brand_tasks.items():
            categories = sorted(
                {
                    str(row.get("category_id") or "")
                    for row in rows
                    if str(row.get("category_id") or "")
                }
            )
            models = sorted(
                {model_scope_value(row.get("model")) for row in rows}
            )
            brands.append(
                {
                    "brand": canonical_labels[key],
                    "logo_asset": next(
                        (
                            str(row.get("brand_logo_asset") or row.get("brand_logo_url") or "").strip()
                            for row in rows
                            if str(row.get("brand_logo_asset") or row.get("brand_logo_url") or "").strip()
                        ),
                        "",
                    ),
                    **brand_asset(canonical_labels[key]),
                    "task_count": len(rows),
                    "review_entities_estimate": sum(
                        int(row.get("eligible_reviews") or 0) for row in rows
                    ),
                    "written_review_entities_estimate": sum(
                        int(row.get("written_reviews") or 0) for row in rows
                    ),
                    "category_count": len(categories),
                    "model_count": len(models),
                    "category_ids": categories,
                    "models": models,
                    "channels": sorted(
                        {str(row.get("channel") or "") for row in rows}
                    ),
                    "market_regions": sorted(
                        {
                            str(row.get("market_region") or "")
                            for row in rows
                            if str(row.get("market_region") or "")
                        }
                    ),
                }
            )
        brands.sort(
            key=lambda row: (
                -int(row["review_entities_estimate"]),
                str(row["brand"]).casefold(),
            )
        )
        return {
            "schema_version": "brand-persona-catalog/v1",
            "method_version": BRAND_PERSONA_METHOD_VERSION,
            "generated_at": self.catalog_payload.get("generated_at"),
            "source_snapshot_immutable": True,
            "brands": brands,
            "tasks": tasks,
        }

    def brand_persona_featured_catalog(self) -> dict[str, Any]:
        catalog = self.brand_persona_catalog()
        snapshot_id = self._snapshot_id(self.snapshot_root)
        jobs = self.brand_personas.list_jobs()
        rows: list[dict[str, Any]] = []
        for brand_row in catalog.get("brands") or []:
            brand = str(brand_row.get("brand") or "")
            pointer = self.brand_personas.get_featured_pointer(brand)
            latest_job = next(
                (
                    job
                    for job in jobs
                    if job.get("purpose") == "featured_full"
                    and str((job.get("scope") or {}).get("brand") or "").casefold()
                    == brand.casefold()
                ),
                None,
            )
            status = "never_generated"
            if pointer:
                status = (
                    "ready"
                    if pointer.get("snapshot_id") == snapshot_id
                    else "stale"
                )
            if latest_job and latest_job.get("status") in {"queued", "running"}:
                status = "updating"
            elif latest_job and latest_job.get("status") == "failed" and (
                not pointer
                or str(latest_job.get("updated_at") or "")
                > str(pointer.get("updated_at") or "")
            ):
                status = "failed"
            rows.append(
                {
                    **brand_row,
                    "status": status,
                    "updated_at": (pointer or {}).get("updated_at"),
                    "analysis_id": (pointer or {}).get("analysis_id"),
                    "artifact_id": (pointer or {}).get("artifact_id"),
                    "snapshot_id": (pointer or {}).get("snapshot_id"),
                    "current_snapshot_id": snapshot_id,
                    "job": latest_job,
                    "has_published_analysis": bool(pointer),
                }
            )
        return {
            "schema_version": "brand-persona-featured-catalog/v1",
            "snapshot_id": snapshot_id,
            "generated_at": catalog.get("generated_at"),
            "schedule": {
                "timezone": "Asia/Shanghai",
                "weekday": "Sunday",
                "local_time": "04:00",
            },
            "brands": rows,
        }

    def get_featured_brand_persona(self, brand: str) -> dict[str, Any] | None:
        pointer = self.brand_personas.get_featured_pointer(brand)
        if pointer is None:
            return None
        analysis = self.brand_personas.get(str(pointer.get("analysis_id") or ""))
        if analysis is None:
            return None
        result = public_brand_persona_analysis(analysis)
        result["featured"] = pointer
        result["featured"]["is_current_snapshot"] = (
            pointer.get("snapshot_id") == self._snapshot_id(self.snapshot_root)
        )
        return result

    def request_featured_brand_persona_refresh(
        self, brand: str, *, trigger: str = "user"
    ) -> dict[str, Any]:
        trigger = str(trigger or "user").strip()
        if trigger not in {"user", "schedule", "initial_backfill"}:
            raise ValueError("unsupported featured brand persona trigger")
        scope = normalize_brand_persona_scope({"brand": brand})
        resolve_brand_persona_job_ids(self._brand_persona_tasks(), scope)
        for job in self.brand_personas.list_jobs():
            if (
                job.get("purpose") == "featured_full"
                and job.get("status") in {"queued", "running"}
                and str((job.get("scope") or {}).get("brand") or "").casefold()
                == scope["brand"].casefold()
            ):
                return job
        job = self.brand_personas.create_job(
            scope,
            purpose="featured_full",
            trigger=trigger,
        )
        self.start_brand_persona_job(str(job["job_id"]))
        return job

    @staticmethod
    def _snapshot_id(snapshot_root: Path) -> str:
        for name in ("snapshot-manifest.json", "manifest.json"):
            path = snapshot_root / name
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            snapshot_id = str(value.get("snapshot_id") or "").strip()
            if snapshot_id:
                return snapshot_id
        return f"snapshot-root:{snapshot_root.name}"

    def analyze_brand_persona(
        self, raw_scope: Any, *, progress: Any = None
    ) -> dict[str, Any]:
        def advance(value: int, stage: str) -> None:
            if progress is not None:
                progress(value, stage)

        advance(5, "校验品牌与筛选范围")
        scope = normalize_brand_persona_scope(raw_scope)
        tasks = self._brand_persona_tasks()
        job_ids = resolve_brand_persona_job_ids(
            tasks, scope
        )
        scope["job_ids"] = job_ids
        advance(15, f"固化 {len(job_ids)} 个任务")
        snapshot_id = self._snapshot_id(self.snapshot_root)
        cached = self.brand_personas.get_for_request(
            snapshot_id=snapshot_id,
            scope=scope,
            cache_version=BRAND_PERSONA_CACHE_VERSION,
        )
        if cached is not None:
            advance(98, "复用同快照、同范围、同版本分析")
            return public_brand_persona_analysis(cached)
        fact_scope = {
            "job_ids": job_ids,
            "brands": [scope["brand"]],
            "date_range": dict(scope.get("date_range") or {}),
            "has_text": "any",
            "syndicated": "include",
            "fallback_source": "include",
        }
        facts, quality, _ = build_review_fact(self.snapshot_root, fact_scope)
        advance(35, f"读取 {len(facts):,} 条合规评论")
        if quality.get("market_contract_mode") != "current":
            raise BrandPersonaContractError(
                "品牌画像只消费统一 review-fact/v2 快照"
            )
        task_by_job = {
            str(task.get("id") or ""): task
            for task in tasks
            if str(task.get("id") or "")
        }
        for fact in facts:
            task = task_by_job.get(str(fact.get("job_id") or ""))
            if task is None:
                continue
            if not str(fact.get("category_id") or "").strip():
                fact["category_id"] = task["category_id"]
            fact_category = str(fact.get("category") or "").strip()
            if not fact_category or (
                task["category_id"] == UNKNOWN_CATEGORY_ID
                and fact_category == UNKNOWN_CATEGORY_ID
            ):
                fact["category"] = task["category"]
        advance(45, "运行全量语义提取器")
        analysis = build_brand_persona_analysis(
            facts,
            scope,
            snapshot_id=snapshot_id,
        )
        analysis["source_quality"] = {
            "selected_job_ids": list(quality.get("selected_job_ids") or job_ids),
            "excluded_latest_runs": int(quality.get("excluded_latest_runs") or 0),
            "duplicate_occurrences": int(quality.get("duplicate_occurrences") or 0),
            "market_contract_mode": quality.get("market_contract_mode"),
        }
        advance(88, "写入 Evidence SQLite 与分析缓存")
        analysis = self.brand_personas.cache(analysis)
        self.brand_personas.bind_request(
            snapshot_id=snapshot_id,
            scope=scope,
            cache_version=BRAND_PERSONA_CACHE_VERSION,
            analysis_id=str(analysis["analysis_id"]),
        )
        advance(98, "组装可读画像")
        return public_brand_persona_analysis(analysis)

    def cached_brand_persona(self, raw_scope: Any) -> dict[str, Any] | None:
        """Read a content-addressed analysis without running extraction."""
        scope = normalize_brand_persona_scope(raw_scope)
        job_ids = resolve_brand_persona_job_ids(self._brand_persona_tasks(), scope)
        scope["job_ids"] = job_ids
        cached = self.brand_personas.get_for_request(
            snapshot_id=self._snapshot_id(self.snapshot_root),
            scope=scope,
            cache_version=BRAND_PERSONA_CACHE_VERSION,
        )
        return public_brand_persona_analysis(cached) if cached is not None else None

    def _run_brand_persona_job(self, job_id: str) -> None:
        job = self.brand_personas.get_job(job_id)
        if job is None:
            return
        try:
            def progress(value: int, stage: str) -> None:
                self.brand_personas.update_job(
                    job_id,
                    status="running",
                    stage=stage,
                    progress=value,
                )

            with self._run_slot:
                analysis = self.analyze_brand_persona(
                    job.get("scope") or {}, progress=progress
                )
                cached = self.brand_personas.get(
                    str(analysis.get("analysis_id") or "")
                ) or {}
                narrative = cached.get("persona_narrative") or {}
                has_personas = bool(cached.get("personas"))
                llm_configured = bool(
                    os.environ.get("VOXQUARRY_QWEN_API_KEY")
                    or os.environ.get("VOXQUARRY_LLM_API_KEY")
                    or os.environ.get("QWEN_API_KEY")
                )
                image_configured = bool(os.environ.get("PEXELS_API_KEY"))
                image_target = len(cached.get("deterministic_personas") or [])
                images_complete = (
                    image_target > 0
                    and int(narrative.get("persona_image_count") or 0)
                    >= image_target
                )
                if (
                    not has_personas
                    or (llm_configured and narrative.get("status") != "qwen_validated")
                    or (image_configured and not images_complete)
                ):
                    progress(91, "生成有证据约束的 Persona")
                    analysis = self.generate_brand_personas(
                        str(analysis.get("analysis_id") or ""),
                        image_progress=lambda completed, total: progress(
                            91
                            + min(
                                4,
                                round(4 * completed / max(1, total)),
                            ),
                            f"整理 Persona 配图 {completed}/{total}",
                        ),
                    )
                if job.get("purpose") == "featured_full":
                    progress(96, "发布全量品牌画像")
                    artifact = self.brand_personas.save(
                        str(analysis.get("analysis_id") or "")
                    )
                    self.brand_personas.publish_featured(
                        brand=str((job.get("scope") or {}).get("brand") or ""),
                        analysis_id=str(analysis.get("analysis_id") or ""),
                        artifact_id=str(artifact.get("artifact_id") or ""),
                        snapshot_id=str(analysis.get("snapshot_id") or ""),
                        content_hash=str(analysis.get("content_hash") or ""),
                        source_job_id=job_id,
                        trigger=str(job.get("trigger") or "user"),
                    )
            self.brand_personas.update_job(
                job_id,
                status="completed",
                stage=(
                    "全量品牌画像已更新"
                    if job.get("purpose") == "featured_full"
                    else "确定性画像已完成"
                ),
                progress=100,
                analysis_id=str(analysis.get("analysis_id") or ""),
            )
        except (BrandPersonaContractError, ValueError) as exc:
            current = self.brand_personas.get_job(job_id) or {}
            self.brand_personas.update_job(
                job_id,
                status="failed",
                stage="范围不具备分析条件",
                progress=int(current.get("progress") or 0),
                error={
                    "code": "BRAND_PERSONA_SCOPE_NOT_READY",
                    "message": str(exc),
                    "retryable": False,
                },
            )
        except Exception as exc:
            current = self.brand_personas.get_job(job_id) or {}
            self.brand_personas.update_job(
                job_id,
                status="failed",
                stage="分析失败",
                progress=int(current.get("progress") or 0),
                error={
                    "code": "BRAND_PERSONA_ANALYSIS_FAILED",
                    "message": str(exc),
                    "retryable": True,
                },
            )

    def start_brand_persona_job(self, job_id: str) -> None:
        if self.execution_mode in {"process", "external"}:
            if self.supervisor is not None:
                self.supervisor.wake()
            return
        if self.execution_mode == "worker":
            return
        existing = self._brand_persona_threads.get(job_id)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=self._run_brand_persona_job,
            args=(job_id,),
            name=f"brand-persona-{job_id}",
            daemon=True,
        )
        self._brand_persona_threads[job_id] = thread
        thread.start()

    @staticmethod
    def _fallback_image_query(persona_id: str) -> str:
        queries = {
            "professional_operator": "professional outdoor equipment operator working safely",
            "residential_self_maintainer": "homeowner maintaining garden with cordless tools",
            "multi_property_caretaker": "property caretaker outdoor maintenance equipment",
            "older_or_retired_operator": "experienced adult garden maintenance cordless tool",
            "first_time_or_switcher": "person learning cordless garden tool safely",
            "battery_ecosystem_owner": "organized cordless power tool battery workshop",
            "high_intensity_operator": "outdoor equipment operator completing extended property maintenance",
            "experienced_brand_switcher": "experienced homeowner comparing cordless outdoor equipment safely",
        }
        return queries.get(persona_id, "outdoor power equipment user working safely")

    def generate_brand_personas(
        self,
        analysis_id: str,
        *,
        image_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        analysis = self.brand_personas.get(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        deterministic = list(analysis.get("deterministic_personas") or [])
        claims = {
            str(row.get("claim_id")): row
            for row in analysis.get("claim_pool") or []
            if row.get("claim_id")
        }
        qwen_key = (
            os.environ.get("VOXQUARRY_QWEN_API_KEY")
            or os.environ.get("VOXQUARRY_LLM_API_KEY")
            or os.environ.get("QWEN_API_KEY")
            or ""
        )
        narrative_status = "deterministic_fallback"
        model = None
        thinking_enabled: bool | None = None
        failure = None
        composed: list[dict[str, Any]] = []
        existing_narrative = analysis.get("persona_narrative") or {}
        existing_personas = list(analysis.get("personas") or [])
        if (
            existing_narrative.get("status") == "qwen_validated"
            and existing_personas
        ):
            # The deterministic analysis identity intentionally excludes derived
            # presentation media.  A featured refresh may therefore revisit the
            # same analysis_id only to fill missing or obsolete images.  Reuse
            # the already claim-locked Persona prose instead of paying for and
            # risking a second LLM generation whose fallback could overwrite it.
            narrative_status = "qwen_validated"
            model = existing_narrative.get("model")
            thinking_enabled = existing_narrative.get("thinking_enabled")
            composed = [dict(row) for row in existing_personas]
        elif qwen_key and claims:
            model = os.environ.get("VOXQUARRY_QWEN_MODEL", "qwen3.7-plus")
            base_url = os.environ.get(
                "VOXQUARRY_QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            try:
                thinking_enabled = optional_env_boolean(
                    "VOXQUARRY_LLM_ENABLE_THINKING"
                )
                generated = QwenPersonaClient(
                    api_key=qwen_key,
                    base_url=base_url,
                    model=model,
                    enable_thinking=thinking_enabled,
                ).generate(claims)
                by_id = {row["persona_id"]: row for row in deterministic}
                for row in generated["personas"]:
                    source = by_id.get(row["persona_id"])
                    if source is None:
                        source = next(
                            (
                                item
                                for item in deterministic
                                if set(item.get("claim_ids") or [])
                                & set(row.get("claim_ids") or [])
                            ),
                            None,
                        )
                    if source is None:
                        continue
                    composed.append(
                        {
                            **source,
                            "title": row["name"],
                            "summary": row["summary"],
                            "first_person_statement": row["first_person_statement"],
                            "who_they_are": row["who_they_are"],
                            "day_in_life": row["day_in_life"],
                            "journey": row["journey"],
                            "claim_ids": row["claim_ids"],
                            "image_brief": row["image_brief"],
                            "narrative_origin": "qwen_derived_text",
                        }
                    )
                if composed:
                    narrative_status = "qwen_validated"
            except Exception as exc:  # deterministic fallback remains usable
                failure = str(exc)
                composed = []
        if not composed:
            composed = [dict(row) for row in deterministic]
            for row in composed:
                row["summary"] = row.get("definition") or ""
                jtbd = str(row.get("jtbd") or "把任务稳定完成").rstrip("。！？!? ")
                row["first_person_statement"] = (
                    f"我希望{jtbd}，并且每一步都可预期、可恢复。"
                )
                row["who_they_are"] = (
                    f"这是由评论中明确表达的身份、责任或经验信号组成的群体："
                    f"{row.get('definition') or ''}"
                )
                lifecycle = list(row.get("lifecycle_stages") or [])
                row["day_in_life"] = [
                    {
                        "stage": "准备与判断",
                        "narrative": f"先确认工具、能源与场地是否足以完成“{row.get('jtbd') or '当前任务'}”。",
                        "claim_ids": list(row.get("claim_ids") or [])[:1],
                    },
                    {
                        "stage": "持续作业",
                        "narrative": "在真实负载中关注连续完成度、操作负担与中断风险。",
                        "claim_ids": list(row.get("claim_ids") or [])[:1],
                    },
                    {
                        "stage": "收尾与恢复",
                        "narrative": "完成清理、收纳或故障恢复，并判断下次是否仍愿意使用这一平台。",
                        "claim_ids": list(row.get("claim_ids") or [])[:1],
                    },
                ]
                row["journey"] = [
                    {
                        "stage": item.get("title") or "使用阶段",
                        "narrative": "该阶段由明确评论信号支持，需结合原文下钻理解具体触点。",
                        "claim_ids": list(row.get("claim_ids") or [])[:1],
                    }
                    for item in lifecycle[:4]
                ] or row["day_in_life"]
                base_query = self._fallback_image_query(row["persona_id"])
                row["image_brief"] = {
                    "portrait_query": base_query + " environmental portrait",
                    "context_query": base_query + " working context wide shot",
                    "avoid": ["brand logo", "unsafe operation"],
                }
        pexels_key = os.environ.get("PEXELS_API_KEY", "")
        previous_images = {
            str(row.get("persona_id") or ""): {
                "image": row.get("image"),
                "context_image": row.get("context_image"),
            }
            for row in analysis.get("personas") or []
            if row.get("image") or row.get("context_image")
        }
        image_failures: list[str] = []
        image_count = 0
        image_service_available = bool(pexels_key)
        image_providers: set[str] = set()
        image_target = len(composed)
        for image_index, row in enumerate(composed, start=1):
            persona_id = str(row.get("persona_id") or "")
            previous = previous_images.get(persona_id) or {}
            previous_image = previous.get("image")
            # Qwen image generation is permanently disabled. Preserve all saved
            # media; text credentials must never enable or replace images.
            row["image"] = previous_image
            row["context_image"] = previous.get("context_image")
            if not row["image"] and pexels_key and image_service_available:
                try:
                    row["image"] = search_pexels_image(
                        row["image_brief"]["portrait_query"],
                        api_key=pexels_key,
                        orientation="portrait",
                    )
                except Exception as exc:
                    status = getattr(exc, "code", None)
                    image_failures.append(
                        f"Pexels HTTP {status}"
                        if isinstance(status, int)
                        else "Pexels " + type(exc).__name__
                    )
                    if status in {401, 403}:
                        image_service_available = False
            if row.get("image"):
                image_count += 1
                image_providers.add(str(row["image"].get("provider") or "unknown"))
            if image_progress is not None:
                image_progress(image_index, image_target)
        narrative = {
            "status": narrative_status,
            "schema_version": PERSONA_NARRATIVE_SCHEMA_VERSION,
            "policy_version": PERSONA_NARRATIVE_POLICY_VERSION,
            "llm_used": narrative_status == "qwen_validated",
            "model": model if narrative_status == "qwen_validated" else None,
            "thinking_enabled": (
                thinking_enabled if narrative_status == "qwen_validated" else None
            ),
            "fallback_reason": failure,
            "images_provider": (
                ", ".join(sorted(image_providers)) if image_providers else None
            ),
            "image_providers": sorted(image_providers),
            "persona_image_count": image_count,
            "persona_image_target": image_target,
            "image_status": (
                "ready"
                if image_target and image_count == image_target
                else "partial"
                if image_count
                else "unavailable"
                if pexels_key
                else "unconfigured"
            ),
            "image_error": image_failures[0] if image_failures else None,
            "images_are_illustrative": True,
        }
        updated = self.brand_personas.update_personas(
            analysis_id, composed, narrative
        )
        return public_brand_persona_analysis(updated)

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return bool(job and job.get("cancel_requested"))

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job:
            return
        request = job["request"]
        scope = request.get("scope") or {}
        try:
            self.store.update_job(job_id, "snapshotting", "固化数据快照", 12)
            source_root = self.snapshot_root
            facts, snapshot = materialize_snapshot(source_root, scope, self.var_root)
            from voxquarry.languages import require_verified_language
            require_verified_language(facts, source_root)
            if self._cancelled(job_id):
                self.store.update_job(job_id, "cancelled", "已取消", 100)
                return
            self.store.update_job(
                job_id,
                "validating",
                "数据校验",
                38,
                sample_estimate={
                    "occurrences": len(facts),
                    "review_entities": snapshot["record_counts"]["unique_entities"]
                    + snapshot["quality_summary"]["unknown_identity_occurrences"],
                    "written_review_entities": snapshot["record_counts"][
                        "written_reviews"
                    ],
                },
                data_snapshot_id=snapshot["snapshot_id"],
            )
            if not facts:
                raise ValueError("当前范围没有通过完整性门禁的评论记录")
            if self._cancelled(job_id):
                self.store.update_job(job_id, "cancelled", "已取消", 100)
                return
            self.store.update_job(job_id, "extracting", "全量原子提取", 56)
            plan = {
                **json.loads(
                    (
                        PACKAGE_ROOT
                        / "research_plans"
                        / "full-category-v1.json"
                    ).read_text(encoding="utf-8")
                ),
                "primary_direction": request.get(
                    "primary_direction", "product-opportunity"
                ),
                "analysis_modules": request.get("analysis_modules") or [],
                "factors": request.get("factors") or [],
                "scope": scope,
            }
            report = build_report(
                facts,
                snapshot,
                research_plan=plan,
                report_name=request.get("report_name"),
                artifact_root=self.var_root,
            )
            if self._cancelled(job_id):
                self.store.update_job(job_id, "cancelled", "已取消", 100)
                return
            self.store.update_job(
                job_id,
                "aggregating",
                "聚合品类洞察与证据组",
                74,
            )
            (
                report,
                full_evidence,
                evidence_groups,
                extraction_ledger,
            ) = build_product_insight_bundle(
                facts,
                report,
                enforce_readiness=True,
            )
            validate_report(report)
            if self._cancelled(job_id):
                self.store.update_job(job_id, "cancelled", "已取消", 100)
                return
            self.store.update_job(job_id, "rendering", "组装报告", 88)
            runtime_artifacts: dict[str, Any] = {}
            if full_evidence:
                stored_evidence = self.evidence.store(
                    report["report_id"],
                    full_evidence,
                    evidence_groups,
                    expected_manifest=report["product_insight"][
                        "evidence_access"
                    ],
                )
                runtime_artifacts["evidence_store"] = {
                    key: value
                    for key, value in stored_evidence.items()
                    if key not in {"path"}
                }
            ledger_artifact = write_extraction_ledger(
                self.var_root,
                report["report_id"],
                extraction_ledger,
            )
            if ledger_artifact:
                runtime_artifacts["ope_extraction_ledger"] = {
                    key: value
                    for key, value in ledger_artifact.items()
                    if key != "path"
                }
            if runtime_artifacts:
                report["runtime_artifacts"] = runtime_artifacts
            self.store.update_job(job_id, "validating", "校验报告与证据", 96)
            validate_report(report)
            # These large intermediate collections have already been persisted.
            # Release them before the bounded text-index preparation step.
            del facts, full_evidence, evidence_groups, extraction_ledger
            prepare_review_text(self.var_root, report)
            self.store.store_report(report)
            self.store.update_job(
                job_id,
                "completed",
                "已完成",
                100,
                report_id=report["report_id"],
            )
        except ValueError as exc:
            # Scope/profile readiness is deterministic for an immutable
            # snapshot. Retrying the exact same request only repeats the same
            # failure; retain a readable job record instead.
            current = self.store.get_job(job_id) or {}
            self.store.update_job(
                job_id,
                "failed",
                "范围未达到报告门槛",
                int(current.get("progress") or 0),
                error={
                    "code": "REPORT_SCOPE_NOT_READY",
                    "message": str(exc),
                    "retryable": False,
                },
            )
        except Exception as exc:
            current = self.store.get_job(job_id) or {}
            self.store.update_job(
                job_id,
                "failed",
                "生成失败",
                int(current.get("progress") or 0),
                error={
                    "code": "REPORT_GENERATION_FAILED",
                    "message": str(exc),
                    "retryable": True,
                },
            )

    def _run_job_in_slot(self, job_id: str) -> None:
        with self._run_slot:
            if self._cancelled(job_id):
                self.store.update_job(job_id, "cancelled", "已取消", 100)
                return
            self._active_job_id = job_id
            try:
                self._run_job(job_id)
            finally:
                self._active_job_id = None

    def start_job(self, job_id: str) -> None:
        if self.execution_mode in {"process", "external"}:
            if self.supervisor is not None:
                self.supervisor.wake()
            return
        if self.execution_mode == "worker":
            return
        existing = self._threads.get(job_id)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=self._run_job_in_slot,
            args=(job_id,),
            name=f"report-{job_id}",
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()

    def run_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        job = self.store.create_job(request)
        if job["status"] != "completed":
            self._run_job_in_slot(job["job_id"])
        updated = self.store.get_job(job["job_id"])
        if not updated or updated["status"] != "completed":
            raise RuntimeError(str((updated or {}).get("error") or "report failed"))
        report = self.store.get_report(updated["report_id"])
        if report is None:
            raise RuntimeError("completed report artifact is missing")
        return report


AGENT_CAPABILITY_PREFIXES = (
    "/reporting/api/v1/agent-reports",
    "/reporting/api/v1/agent-tools",
    "/reporting/api/v1/agent-citations",
    "/reporting/api/v1/tools",
)


def _strict_agent_tools_enabled() -> bool:
    """Gate all Agent HTTP capabilities at route-registration time.

    Missing means disabled.  A present value must be the exact canonical
    literal so a typo can never silently enable or partially expose tooling.
    """

    raw = os.environ.get("VOXQUARRY_AGENT_TOOLS_ENABLED")
    if raw is None:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise RuntimeError(
        "VOXQUARRY_AGENT_TOOLS_ENABLED must be exactly true or false"
    )


def _is_agent_capability_path(path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in AGENT_CAPABILITY_PREFIXES
    )


def create_app(
    snapshot_root: Path,
    var_root: Path = DEFAULT_VAR_ROOT,
    frontend_root: Path = DEFAULT_FRONTEND_ROOT,
    execution_mode: str | None = None,
) -> FastAPI:
    if not snapshot_root.is_dir():
        raise RuntimeError(f"snapshot root does not exist: {snapshot_root}")
    agent_tools_enabled = _strict_agent_tools_enabled()
    workflow_flag = os.environ.get("VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED", "false")
    if workflow_flag not in {"true", "false"}:
        raise RuntimeError("VOXQUARRY_WENQUAN_REPORT_GENERATION_ENABLED must be exactly true or false")
    report_workflow_enabled = workflow_flag == "true"
    service = ReportingService(
        snapshot_root.resolve(),
        var_root.resolve(),
        execution_mode=execution_mode,
    )
    app = FastAPI(title="VoxQuarry Reporting API", version="0.4.0")
    app.state.reporting = service
    app.state.agent_tools_enabled = agent_tools_enabled

    @app.exception_handler(ReportDisplayNameLedgerError)
    async def report_display_name_ledger_error(
        _request: Request, _error: ReportDisplayNameLedgerError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "report_display_name_ledger_unavailable",
                    "message": "报告名称审计状态不可用，请联系管理员核验",
                }
            },
        )
    if agent_tools_enabled:
        app.state.agent_tools = AgentToolService(service.store, service.evidence)
    if not agent_tools_enabled or not report_workflow_enabled:
        # StaticFiles would otherwise answer unmatched POST requests with 405.
        # Keep disabled Agent capabilities absent from the route registry and
        # OpenAPI while returning the same non-disclosing 404 for every method.
        @app.middleware("http")
        async def hide_disabled_agent_routes(
            request: Request, call_next: Callable[..., Any]
        ) -> Response:
            path = request.url.path
            if (not agent_tools_enabled and _is_agent_capability_path(path)) or (not report_workflow_enabled and (path == "/reporting/api/v1/agent-reports" or path.startswith("/reporting/api/v1/agent-reports/"))):
                return JSONResponse(
                    status_code=404, content={"detail": "Not Found"}
                )
            return await call_next(request)

    @app.get("/reporting/api/v1/health")
    def health() -> dict[str, Any]:
        worker = read_worker_status(service.var_root)
        if service.execution_mode == "thread":
            jobs = service.store.list_jobs()
            worker = {
                "schema_version": "report-worker-status/v1",
                "execution_mode": "thread",
                "concurrency": 1,
                "active": (
                    [{"kind": "report", "job_id": service._active_job_id}]
                    if service._active_job_id
                    else []
                ),
                "queued_report_jobs": sum(
                    1 for job in jobs if job.get("status") == "queued"
                ),
            }
        worker_payload = worker or {
            "schema_version": "report-worker-status/v1",
            "status": "starting",
            "active": [],
        }
        active_report = next(
            (
                item
                for item in worker_payload.get("active") or []
                if item.get("kind") == "report"
            ),
            None,
        )
        return {
            "status": "ok",
            "snapshot_root_read_only": True,
            "report_job_concurrency": int(
                worker_payload.get("concurrency")
                or service.runtime_policy.concurrency
            ),
            "active_report_job_id": (active_report or {}).get("job_id"),
            "queued_report_jobs": int(
                worker_payload.get("queued_report_jobs") or 0
            ),
            "execution_mode": service.execution_mode,
            "worker": worker_payload,
            "structured_refresh": read_diagnostics_sidecar(service.var_root),
            "query_cache": service.wenquan_cache_health() if hasattr(service, "wenquan_cache_health") else {"status": "disabled"},
        }

    @app.on_event("shutdown")
    def shutdown_reporting_service() -> None:
        service.close()

    # Local vertical-slice compatibility responses. They keep the existing
    # collection shell quiet while this read-only reporting process serves the
    # shared frontend; production continues to proxy these paths to scraper.
    @app.get("/scraper/api/jobs")
    def preview_scraper_jobs() -> list[Any]:
        return []

    @app.get("/scraper/api/stats")
    def preview_scraper_stats(by: str = "channel") -> dict[str, Any]:
        return {"by": by, "groups": []}

    @app.get("/scraper/api/feedback")
    def preview_feedback() -> list[Any]:
        return []

    @app.get("/scraper/api/master-data")
    def preview_master_data() -> dict[str, Any]:
        source_catalog = service.catalog_payload
        return {
            "brands": [
                {"id": f"preview-brand-{index}", "name": name}
                for index, name in enumerate(source_catalog["brands"], start=1)
            ],
            "categories": [
                {"id": f"preview-category-{index}", "name": name}
                for index, name in enumerate(source_catalog["categories"], start=1)
            ],
        }

    @app.get("/queue-status")
    def preview_queue() -> dict[str, Any]:
        return {"jobs": []}

    @app.get("/reporting/api/v1/catalog")
    def get_catalog() -> dict[str, Any]:
        return service.catalog_payload

    @app.get("/reporting/api/v1/brand-persona/catalog")
    def get_brand_persona_catalog() -> dict[str, Any]:
        return service.brand_persona_catalog()

    @app.get("/reporting/api/v1/brand-persona/featured")
    def get_featured_brand_persona_catalog() -> dict[str, Any]:
        return service.brand_persona_featured_catalog()

    @app.get("/reporting/api/v1/brand-persona/featured/{brand}")
    def get_featured_brand_persona(brand: str) -> dict[str, Any]:
        analysis = service.get_featured_brand_persona(brand)
        if analysis is None:
            raise HTTPException(
                status_code=404, detail="featured brand persona not found"
            )
        return analysis

    @app.post("/reporting/api/v1/brand-persona/featured/{brand}/refresh")
    def refresh_featured_brand_persona(
        brand: str, trigger: str = "user"
    ) -> dict[str, Any]:
        try:
            return service.request_featured_brand_persona_refresh(
                brand, trigger=trigger
            )
        except (BrandPersonaContractError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/reporting/api/v1/brand-persona/query")
    def query_brand_persona(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if service.execution_mode == "external":
                cached = service.cached_brand_persona(payload.get("scope"))
                if cached is not None:
                    return cached
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "BRAND_PERSONA_ASYNC_REQUIRED",
                        "message": "该范围尚无缓存，请创建品牌画像任务并读取任务进度。",
                    },
                )
            return service.analyze_brand_persona(payload.get("scope"))
        except (BrandPersonaContractError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/reporting/api/v1/brand-persona/jobs")
    def create_brand_persona_job(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            scope = normalize_brand_persona_scope(payload.get("scope"))
            # Resolve now so an invalid/empty scope fails before a durable job
            # appears. The exact job IDs are re-resolved and frozen by the
            # worker against the same immutable process snapshot.
            resolve_brand_persona_job_ids(service._brand_persona_tasks(), scope)
        except (BrandPersonaContractError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        job = service.brand_personas.create_job(scope)
        service.start_brand_persona_job(str(job["job_id"]))
        return job

    @app.get("/reporting/api/v1/brand-persona/jobs/{job_id}")
    def get_brand_persona_job(job_id: str) -> dict[str, Any]:
        job = service.brand_personas.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="brand persona job not found")
        return job

    @app.get("/reporting/api/v1/brand-persona/analyses/{analysis_id}")
    def get_brand_persona_analysis(analysis_id: str) -> dict[str, Any]:
        analysis = service.brand_personas.get(analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")
        return public_brand_persona_analysis(analysis)

    @app.get(
        "/reporting/api/v1/brand-persona/analyses/{analysis_id}/personas/"
        "{persona_id}/images/{asset_sha256}"
    )
    def get_brand_persona_image(
        analysis_id: str, persona_id: str, asset_sha256: str
    ) -> FileResponse:
        analysis = service.brand_personas.get(analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")
        persona = next(
            (
                row
                for row in analysis.get("personas") or []
                if str(row.get("persona_id") or "") == persona_id
            ),
            None,
        )
        image = (persona or {}).get("image") or {}
        expected = str(image.get("asset_sha256") or "").removeprefix("sha256:")
        if not expected or expected != asset_sha256:
            raise HTTPException(status_code=404, detail="brand persona image not found")
        try:
            path, media_type = service.brand_personas.get_persona_image_asset(
                asset_sha256
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="brand persona image not found")
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.post("/reporting/api/v1/brand-persona/analyses/{analysis_id}/personas")
    def create_brand_persona_narrative(analysis_id: str) -> dict[str, Any]:
        if service.execution_mode == "external":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "BRAND_PERSONA_ASYNC_REQUIRED",
                    "message": "Persona 必须通过品牌画像任务在隔离 Worker 中生成。",
                },
            )
        try:
            return service.generate_brand_personas(analysis_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")

    @app.get("/reporting/api/v1/brand-persona/analyses/{analysis_id}/evidence")
    def query_brand_persona_evidence(
        analysis_id: str,
        offset: int = 0,
        limit: int = 40,
        q: Optional[str] = None,
        category_id: Optional[str] = None,
        model: Optional[str] = None,
        channel: Optional[str] = None,
        market_region: Optional[str] = None,
        rating: Optional[str] = None,
        kind: Optional[str] = None,
        label: Optional[str] = None,
        evidence_ids: Optional[str] = None,
    ) -> dict[str, Any]:
        analysis = service.brand_personas.get(analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")
        if offset < 0 or limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="offset/limit 超出允许范围")
        rows = list(analysis.get("evidence") or [])
        requested_ids = {
            value.strip()
            for value in str(evidence_ids or "").split(",")
            if value.strip()
        }
        sidecar = service.brand_personas.query_evidence(
            analysis_id,
            offset=offset,
            limit=limit,
            q=str(q or "").strip(),
            category_id=str(category_id or ""),
            model=str(model or ""),
            channel=str(channel or ""),
            market_region=str(market_region or ""),
            rating=str(rating or ""),
            kind=str(kind or ""),
            label=str(label or ""),
            evidence_ids=requested_ids,
        )
        if sidecar is not None:
            items, total = sidecar
            facets = service.brand_personas.evidence_facets(
                analysis_id,
                kind=str(kind or ""),
                label=str(label or ""),
                evidence_ids=requested_ids,
            ) or {}
            return {
                "analysis_id": analysis_id,
                "items": items,
                "page": {
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "has_more": offset + limit < total,
                },
                "facets": facets,
                "storage_schema_version": "brand-persona-evidence/v1",
            }
        if requested_ids:
            rows = [row for row in rows if row.get("evidence_id") in requested_ids]
        query = str(q or "").strip().casefold()
        if query:
            rows = [
                row
                for row in rows
                if query
                in " ".join(
                    str(row.get(key) or "")
                    for key in ("quote", "matched_text", "label", "model", "category")
                ).casefold()
            ]
        if category_id:
            rows = [row for row in rows if row.get("category_id") == category_id]
        if model:
            rows = [row for row in rows if row.get("model") == model]
        if channel:
            rows = [row for row in rows if row.get("channel") == channel]
        if market_region:
            rows = [
                row for row in rows if row.get("market_region") == market_region
            ]
        if rating:
            rows = [row for row in rows if str(row.get("rating") or "") == str(rating)]
        if kind:
            rows = [row for row in rows if row.get("kind") == kind]
        if label:
            rows = [row for row in rows if row.get("label") == label]
        total = len(rows)
        return {
            "analysis_id": analysis_id,
            "items": rows[offset : offset + limit],
            "page": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + limit < total,
            },
        }

    @app.get(
        "/reporting/api/v1/brand-persona/analyses/{analysis_id}/evidence/export"
    )
    def export_brand_persona_evidence(
        analysis_id: str,
        format: str = "csv",
        q: Optional[str] = None,
        category_id: Optional[str] = None,
        model: Optional[str] = None,
        channel: Optional[str] = None,
        market_region: Optional[str] = None,
        rating: Optional[str] = None,
        kind: Optional[str] = None,
        label: Optional[str] = None,
        evidence_ids: Optional[str] = None,
    ) -> StreamingResponse:
        analysis = service.brand_personas.get(analysis_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")
        if format not in {"csv", "ndjson"}:
            raise HTTPException(status_code=422, detail="format 只能是 csv / ndjson")
        requested_ids = {
            value.strip()
            for value in str(evidence_ids or "").split(",")
            if value.strip()
        }
        rows = service.brand_personas.iter_evidence(
            analysis_id,
            q=str(q or "").strip(),
            category_id=str(category_id or ""),
            model=str(model or ""),
            channel=str(channel or ""),
            market_region=str(market_region or ""),
            rating=str(rating or ""),
            kind=str(kind or ""),
            label=str(label or ""),
            evidence_ids=requested_ids,
        )
        extension = "csv" if format == "csv" else "ndjson"
        return StreamingResponse(
            csv_lines(rows) if format == "csv" else ndjson_lines(rows),
            media_type=(
                "text/csv; charset=utf-8"
                if format == "csv"
                else "application/x-ndjson; charset=utf-8"
            ),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{analysis_id}-evidence.{extension}"'
                )
            },
        )

    @app.post("/reporting/api/v1/brand-persona/analyses/{analysis_id}/save")
    def save_brand_persona_analysis(
        analysis_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return service.brand_personas.save(
                analysis_id, title=str(payload.get("title") or "")
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="brand persona analysis not found")

    @app.get("/reporting/api/v1/brand-persona/artifacts")
    def list_brand_persona_artifacts() -> dict[str, Any]:
        return {
            "artifact_type": "brand_persona",
            "items": service.brand_personas.list_saved(),
        }

    @app.get("/reporting/api/v1/brand-persona/artifacts/{artifact_id}")
    def get_brand_persona_artifact(artifact_id: str) -> dict[str, Any]:
        artifact = service.brand_personas.get_saved(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="brand persona artifact not found")
        return public_brand_persona_analysis(artifact)

    @app.post("/reporting/api/v1/estimate")
    def estimate(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            scope = normalize_scope(
                payload.get("scope"), default_scope()
            )
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        facts, quality, _ = build_review_fact(service.snapshot_root, scope)
        entities = [
            fact
            for fact in facts
            if fact["business_key_status"] == "incomplete"
            or fact["canonical_entity_rank"] == 1
        ]
        written_entities = sum(fact["has_text"] for fact in entities)
        rating_entities = sum(
            fact["rating"] is not None for fact in entities
        )
        recommendation_entities = sum(
            fact["is_recommended"] is not None for fact in entities
        )
        explicit_age_entities = sum(
            explicit_age_group(fact)[0] is not None for fact in entities
        )
        factor_samples = {
            "ratings": rating_entities,
            "recommendation": recommendation_entities,
            "written-coverage": len(entities),
            "attributes": written_entities,
            "pain-points": written_entities,
            "use-cases": written_entities,
            "explicit-age": explicit_age_entities,
            "user-traits": written_entities,
            "keyword-document-frequency": written_entities,
            "factor-cooccurrence": written_entities,
            "kano-candidates": written_entities,
            "product-opportunities": written_entities,
            "smart-signals": written_entities,
            "verified-purchaser": 0,
        }
        return {
            "occurrences": len(facts),
            "review_entities": len(entities),
            "unknown_identity_occurrences": quality["unknown_identity_occurrences"],
            "written_review_entities": written_entities,
            "written_coverage": (
                sum(fact["has_text"] for fact in entities) / len(entities)
                if entities
                else None
            ),
            "excluded_latest_runs": quality["excluded_latest_runs"],
            "fallback_occurrences": quality["fallback_occurrences"],
            "factor_samples": factor_samples,
        }

    @app.get("/reporting/api/v1/reports")
    def list_reports(include_archived: bool = False) -> dict[str, Any]:
        cards = service.store.list_cards_with_jobs(
            include_archived=include_archived
        )
        return {
            "items": cards,
            "summary": {
                "total": len(cards),
                "generating": sum(
                    item["status"]
                    not in {"completed", "failed", "cancelled"}
                    for item in cards
                ),
                "completed": sum(item["status"] == "completed" for item in cards),
                "attention": sum(
                    item["status"] in {"failed", "cancelled"} for item in cards
                ),
            },
        }

    @app.post("/reporting/api/v1/reports")
    def create_report(
        payload: dict[str, Any],
        idempotency_key: Optional[str] = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> dict[str, Any]:
        try:
            automatic_name = not " ".join(
                str(payload.get("report_name") or "").split()
            ).strip()
            request = normalize_request(
                payload,
                defaults=default_scope(),
                analysis_version=ANALYSIS_VERSION,
            )
            source_jobs = load_jobs(service.snapshot_root)
            catalog_payload = catalog(service.snapshot_root)
            request["scope"] = resolve_voltage_platform_scope(
                request["scope"], source_jobs
            )
            request["scope"] = resolve_controlled_energy_scope(
                request["scope"],
                catalog_payload["tasks"],
                reportable_job_ids={
                    str(task.get("id") or "")
                    for task in catalog_payload["tasks"]
                    if str(task.get("id") or "")
                },
                source_snapshot=catalog_payload.get("source_snapshot"),
            )
            if automatic_name:
                request["report_name"] = automatic_report_title(request["scope"])
            validate_scope_job_ids(service.snapshot_root, request["scope"],
                                   source_jobs=source_jobs, catalog_payload=catalog_payload)
        except VoltagePlatformError as exc:
            raise HTTPException(status_code=422, detail=exc.as_detail())
        except ControlledEnergyError as exc:
            raise HTTPException(status_code=422, detail=exc.as_detail())
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        job = service.store.create_job(
            request, idempotency_key=idempotency_key
        )
        if job["status"] not in {"completed", "failed", "cancelled"}:
            service.start_job(job["job_id"])
        return job

    @app.get("/reporting/api/v1/report-jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = service.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="report job not found")
        return job

    @app.get("/reporting/api/v1/report-job-groups/{job_group_id}")
    def get_report_job_group(job_group_id: str) -> dict[str, Any]:
        history = service.store.get_job_group(job_group_id)
        if history is None:
            raise HTTPException(status_code=404, detail="report job history not found")
        return history

    @app.post("/reporting/api/v1/report-jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return service.store.request_cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="report job not found")

    @app.post("/reporting/api/v1/report-jobs/{job_id}/retry")
    def retry_job(job_id: str) -> dict[str, Any]:
        try:
            job = service.store.retry_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="report job not found")
        if job["status"] not in {"completed", "failed", "cancelled"}:
            service.start_job(job["job_id"])
        return job

    @app.delete("/reporting/api/v1/report-jobs/{job_id}")
    def delete_failed_job(job_id: str) -> dict[str, Any]:
        try:
            return service.store.delete_failed_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="report job not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/reporting/api/v1/reports/{report_id}")
    def get_report(report_id: str) -> dict[str, Any]:
        report = service.store.get_report_view(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return report

    @app.put("/reporting/api/v1/reports/{report_id}/display-name")
    def set_report_display_name(
        report_id: str, payload: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        extras = sorted(set(payload) - {"display_name", "expected_version"})
        if extras:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_display_name_request",
                    "message": "请求包含未知字段：" + ", ".join(extras),
                },
            )
        if "display_name" not in payload or "expected_version" not in payload:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_display_name_request",
                    "message": "display_name 与 expected_version 均为必填字段",
                },
            )
        try:
            authority = authority_from_report_management_gateway(
                request.headers,
                os.environ.get(REPORT_MANAGEMENT_TOKEN_ENV),
            )
        except ReportManagementAuthorityError as error:
            raise HTTPException(
                status_code=error.http_status,
                detail={"code": error.code, "message": error.message},
            ) from error
        try:
            return service.store.set_report_display_name(
                report_id,
                display_name=payload["display_name"],
                expected_version=payload["expected_version"],
                actor=authority.actor(),
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail={"code": "report_not_found", "message": "报告不存在"},
            ) from error
        except ReportDisplayNameConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "report_display_name_conflict",
                    "message": "名称已变化，请确认后重试",
                    "current": error.current,
                },
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_report_display_name",
                    "message": str(error),
                },
            ) from error
        except ReportDisplayNameLedgerError as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "report_display_name_ledger_unavailable",
                    "message": "报告名称审计状态不可用，请联系管理员核验",
                },
            ) from error

    @app.get("/reporting/api/v1/reports/{report_id}/metrics/{metric_id}")
    def get_metric(report_id: str, metric_id: str) -> dict[str, Any]:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        metric = next(
            (item for item in report["metrics"] if item["metric_id"] == metric_id),
            None,
        )
        definition = next(
            (
                item
                for item in report["metric_definitions"]
                if item["metric_id"] == metric_id
            ),
            None,
        )
        if metric is None:
            raise HTTPException(status_code=404, detail="metric not found")
        return {"metric": metric, "definition": definition}

    @app.get("/reporting/api/v1/reports/{report_id}/metrics")
    def query_metrics(
        report_id: str, metric_id: Optional[str] = None
    ) -> dict[str, Any]:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        metrics = report.get("metrics") or []
        if metric_id:
            metrics = [
                item for item in metrics if item["metric_id"] == metric_id
            ]
        definitions = {
            item["metric_id"]: item
            for item in report.get("metric_definitions") or []
        }
        return {
            "report_id": report_id,
            "items": [
                {
                    "metric": metric,
                    "definition": definitions.get(metric["metric_id"]),
                }
                for metric in metrics
            ],
        }

    @app.get("/reporting/api/v1/reports/{report_id}/evidence/{evidence_id}")
    def get_evidence(report_id: str, evidence_id: str, include_review_text: bool = False) -> dict[str, Any]:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        evidence_rows = list(report.get("evidence") or [])
        evidence_rows.extend(
            (report.get("product_insight") or {}).get("evidence") or []
        )
        evidence = next(
            (
                item
                for item in evidence_rows
                if item["evidence_id"] == evidence_id
            ),
            None,
        )
        if evidence is None:
            try:
                evidence = service.evidence.get(report_id, evidence_id)
            except FileNotFoundError:
                evidence = None
        if evidence is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return enrich_review_text(service.var_root, report, [evidence])[0] if include_review_text else evidence

    @app.get("/reporting/api/v1/reports/{report_id}/evidence")
    def query_evidence(
        report_id: str,
        factor_id: Optional[str] = None,
        channel: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_review_text: bool = False,
    ) -> dict[str, Any]:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        bounded_limit = max(1, min(limit, 100))
        items = [
            item
            for item in report.get("evidence") or []
            if (not factor_id or item.get("factor_id") == factor_id)
            and (not channel or item.get("channel") == channel)
        ]
        return {
            "report_id": report_id,
            "items": (enrich_review_text(service.var_root, report, items[offset:offset+bounded_limit])
                      if include_review_text else items[offset:offset+bounded_limit]),
            "total": len(items),
            "offset": offset,
            "truncated": len(items) > offset + bounded_limit,
        }

    @app.get(
        "/reporting/api/v1/reports/{report_id}/"
        "evidence-groups/{evidence_group_id}"
    )
    def query_evidence_group(
        report_id: str,
        evidence_group_id: str,
        offset: int = 0,
        limit: int = 50,
        channel: Optional[str] = None,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        market_region: Optional[str] = None,
        rating: Optional[int] = None,
        polarity: Optional[str] = None,
        evidence_type: Optional[str] = None,
        q: Optional[str] = None,
        include_facets: bool = True,
        include_review_text: bool = False,
    ) -> dict[str, Any]:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset 不能小于 0")
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit 必须在 1–100")
        if rating is not None and rating not in {1, 2, 3, 4, 5}:
            raise HTTPException(status_code=422, detail="rating 必须在 1–5")
        if market_region and market_region not in MARKET_REGIONS:
            raise HTTPException(
                status_code=422, detail="market_region 不是规范市场地区"
            )
        if q and len(q) > 200:
            raise HTTPException(status_code=422, detail="q 不能超过 200 字符")
        try:
            payload = service.evidence.query(
                report_id,
                evidence_group_id,
                offset=offset,
                limit=limit,
                channel=channel,
                brand=brand,
                model=model,
                market_region=market_region,
                rating=rating,
                polarity=polarity,
                evidence_type=evidence_type,
                q=q,
                include_facets=include_facets,
            )
            # Legacy clients paginate excerpts. Full immutable-fact scans are
            # explicit so a new presentation field cannot slow those clients.
            if include_review_text:
                payload["items"] = enrich_review_text(service.var_root, report, payload["items"])
            return payload
        except (FileNotFoundError, KeyError):
            raise HTTPException(
                status_code=404, detail="evidence group not found"
            )

    @app.get(
        "/reporting/api/v1/reports/{report_id}/"
        "evidence-groups/{evidence_group_id}/export"
    )
    def export_evidence_group(
        report_id: str,
        evidence_group_id: str,
        format: str = "csv",
        channel: Optional[str] = None,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        market_region: Optional[str] = None,
        rating: Optional[int] = None,
        polarity: Optional[str] = None,
        evidence_type: Optional[str] = None,
        q: Optional[str] = None,
    ) -> StreamingResponse:
        if service.store.get_report(report_id) is None:
            raise HTTPException(status_code=404, detail="report not found")
        if format not in {"csv", "ndjson"}:
            raise HTTPException(
                status_code=422, detail="format 只能是 csv / ndjson"
            )
        if rating is not None and rating not in {1, 2, 3, 4, 5}:
            raise HTTPException(status_code=422, detail="rating 必须在 1–5")
        if market_region and market_region not in MARKET_REGIONS:
            raise HTTPException(
                status_code=422, detail="market_region 不是规范市场地区"
            )
        try:
            if service.evidence.group(report_id, evidence_group_id) is None:
                raise KeyError(evidence_group_id)
        except (FileNotFoundError, KeyError):
            raise HTTPException(
                status_code=404, detail="evidence group not found"
            )
        rows = service.evidence.iter_filtered(
            report_id,
            evidence_group_id,
            channel=channel,
            brand=brand,
            model=model,
            market_region=market_region,
            rating=rating,
            polarity=polarity,
            evidence_type=evidence_type,
            q=q,
        )
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        rows = enrich_evidence_rows(
            rows,
            (report.get("quality") or {}).get("source_completeness") or [],
            (report.get("quality") or {}).get(
                "non_reportable_exclusions"
            ) or [],
        )
        extension = "csv" if format == "csv" else "ndjson"
        return StreamingResponse(
            csv_lines(rows) if format == "csv" else ndjson_lines(rows),
            media_type=(
                "text/csv; charset=utf-8"
                if format == "csv"
                else "application/x-ndjson; charset=utf-8"
            ),
            headers={
                "Content-Disposition": content_disposition(
                    report,
                    f"evidence-{evidence_group_id}.{extension}",
                )
            },
        )

    @app.post("/reporting/api/v1/reports/{report_id}/archive")
    def archive_report(report_id: str) -> dict[str, Any]:
        try:
            return service.store.set_archived(report_id, True)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid report_id")
        except KeyError:
            raise HTTPException(status_code=404, detail="report not found")

    @app.post("/reporting/api/v1/reports/{report_id}/restore")
    def restore_report(report_id: str) -> dict[str, Any]:
        try:
            return service.store.set_archived(report_id, False)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid report_id")
        except KeyError:
            raise HTTPException(status_code=404, detail="report not found")

    @app.get("/reporting/api/v1/reports/{report_id}/export/json")
    def export_json(report_id: str) -> FileResponse:
        path = service.store.reports_dir / f"{report_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="report not found")
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"{report_export_basename(report)}.json",
        )

    @app.get(
        "/reporting/api/v1/reports/{report_id}/export/reviews-json"
    )
    def export_report_reviews(report_id: str) -> StreamingResponse:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        snapshot_id = str(report.get("data_snapshot_id") or "")
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise HTTPException(
                status_code=409,
                detail="该历史报告没有可导出的不可变 Review Fact 快照",
            )
        snapshot_root = (service.var_root / "snapshots").resolve()
        fact_path = (
            snapshot_root / snapshot_id / "review_fact.preview.jsonl.gz"
        ).resolve()
        if snapshot_root not in fact_path.parents or not fact_path.is_file():
            raise HTTPException(
                status_code=409,
                detail="该历史报告没有可导出的不可变 Review Fact 快照",
            )
        try:
            _record_count, lines = report_review_json_lines(report, fact_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=409,
                detail="报告 Review Fact 工件无法通过完整性读取",
            ) from exc
        return StreamingResponse(
            lines,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": content_disposition(
                    report, "reviews.json"
                ),
                "X-VoxQuarry-Export-Schema": "report-review-export/v1",
            },
        )

    @app.get("/reporting/api/v1/reports/{report_id}/export/markdown")
    def export_markdown(report_id: str) -> Response:
        report = service.store.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        return Response(
            content=report_to_markdown(report),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": content_disposition(report, "md")
            },
        )

    agent_router = APIRouter()

    if report_workflow_enabled:
        from reporting.agent_report_workflow import AgentReportWorkflow
        report_workflow = AgentReportWorkflow(service, create_report, os.environ.get("VOXQUARRY_AGENT_GATEWAY_TOKEN", ""))

        @agent_router.post("/reporting/api/v1/agent-reports/{operation}")
        def prepare_agent_report(operation: str, payload: dict[str, Any], request: Request) -> Response:
            try:
                authority = authority_from_gateway(request.headers, os.environ.get("VOXQUARRY_AGENT_GATEWAY_TOKEN"))
                return JSONResponse(content=report_workflow.invoke(operation, payload, authority))
            except AgentToolError as error:
                return JSONResponse(status_code=error.http_status, content={"contract": "wenquan-report-workflow/v1", "error": {"code": error.code, "message": error.message, "retryable": error.retryable}})
            except HTTPException as error:
                return JSONResponse(status_code=error.status_code, content={"contract": "wenquan-report-workflow/v1", "error": {"code": "report_preparation_invalid", "message": "这批评论暂时无法生成报告，可以用加号选择已有报告继续。", "retryable": False}})

    @agent_router.post("/reporting/api/v1/agent-tools/{tool_name}")
    def invoke_agent_tool(
        tool_name: str,
        payload: dict[str, Any],
        request: Request,
    ) -> Response:
        authority = None
        try:
            authority = authority_from_gateway(
                request.headers,
                os.environ.get("VOXQUARRY_AGENT_GATEWAY_TOKEN"),
            )
            result = app.state.agent_tools.invoke(
                tool_name, payload, authority
            )
            return JSONResponse(content=result)
        except AgentToolError as error:
            return JSONResponse(
                status_code=error.http_status,
                content=app.state.agent_tools.error_envelope(
                    tool_name, authority, error
                ),
            )

    @agent_router.post("/reporting/api/v1/agent-citations/validate")
    def validate_agent_citations(
        payload: dict[str, Any],
        request: Request,
    ) -> Response:
        authority = None
        try:
            authority = authority_from_gateway(
                request.headers,
                os.environ.get("VOXQUARRY_AGENT_GATEWAY_TOKEN"),
            )
            if set(payload) != {"citations"}:
                raise AgentToolError(
                    "invalid_request",
                    "citation validator 只接受 citations 字段",
                )
            result = app.state.agent_tools.validate_citations(
                payload["citations"], authority
            )
            return JSONResponse(content=result)
        except AgentToolError as error:
            return JSONResponse(
                status_code=error.http_status,
                content=app.state.agent_tools.error_envelope(
                    "zhuojian.citation_validator@v1",
                    authority,
                    error,
                ),
            )

    # Retired Agent aliases are intentionally unavailable. Product UI routes
    # above remain unchanged; trusted Agent traffic must use the six
    # Gateway-scoped, read-only contracts.
    @agent_router.post("/reporting/api/v1/tools/create_report")
    def tool_create_report(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="Agent mutation 已关闭；AI 研究只允许六个只读分析报告工具",
        )

    @agent_router.post("/reporting/api/v1/tools/query_brand_persona")
    def tool_query_brand_persona(payload: dict[str, Any]) -> dict[str, Any]:
        """Read a cached deterministic workbench result for one brand scope."""
        return query_brand_persona(payload)

    @agent_router.post("/reporting/api/v1/tools/create_brand_persona")
    def tool_create_brand_persona(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="Agent mutation 已关闭；品牌画像更新只能由产品工作流触发",
        )

    @agent_router.post("/reporting/api/v1/tools/get_brand_persona")
    def tool_get_brand_persona(payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(payload.get("artifact_id") or "")
        analysis_id = str(payload.get("analysis_id") or "")
        if artifact_id:
            artifact = service.brand_personas.get_saved(artifact_id)
            if artifact is None:
                raise HTTPException(status_code=404, detail="brand persona artifact not found")
            return public_brand_persona_analysis(artifact)
        if analysis_id:
            analysis = service.brand_personas.get(analysis_id)
            if analysis is None:
                raise HTTPException(status_code=404, detail="brand persona analysis not found")
            return public_brand_persona_analysis(analysis)
        raise HTTPException(
            status_code=422, detail="artifact_id 或 analysis_id 必填"
        )

    @agent_router.post("/reporting/api/v1/tools/get_brand_persona_evidence")
    def tool_get_brand_persona_evidence(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        analysis_id = str(payload.get("analysis_id") or "")
        if not analysis_id:
            raise HTTPException(status_code=422, detail="analysis_id 必填")
        return query_brand_persona_evidence(
            analysis_id,
            offset=int(payload.get("offset") or 0),
            limit=int(payload.get("limit") or 40),
            q=payload.get("q"),
            category_id=payload.get("category_id"),
            model=payload.get("model"),
            evidence_ids=payload.get("evidence_ids"),
        )

    @agent_router.post("/reporting/api/v1/tools/get_report")
    def tool_get_report(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 Gateway 工具 zhuojian.get_report@v1",
        )

    @agent_router.post("/reporting/api/v1/tools/list_reports")
    def tool_list_reports(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 Gateway 工具 zhuojian.list_reports@v1",
        )

    @agent_router.post("/reporting/api/v1/tools/query_report_metric")
    def tool_query_report_metric(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 Gateway 工具 zhuojian.get_metric@v1",
        )

    @agent_router.post("/reporting/api/v1/tools/get_report_evidence")
    def tool_get_report_evidence(payload: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 Gateway 工具 zhuojian.query_evidence@v1",
        )

    @agent_router.post("/reporting/api/v1/tools/search_report_evidence")
    def tool_search_report_evidence(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 Gateway 工具 zhuojian.query_evidence@v1",
        )

    @agent_router.post("/reporting/api/v1/tools/query_product_insight")
    def tool_query_product_insight(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="请使用 zhuojian.get_report@v1 读取已保存报告章节",
        )

    if agent_tools_enabled:
        app.include_router(agent_router)
        from reporting.wenquan_data_routes import research_data_router
        app.include_router(research_data_router(service))

    app.mount("/", StaticFiles(directory=frontend_root, html=True), name="frontend")
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoxQuarry reporting service")
    parser.add_argument(
        "command",
        choices=("serve", "generate-report", "generate-mower"),
        help="Action to run",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=(
            Path(os.environ["VOXQUARRY_SNAPSHOT_ROOT"])
            if os.environ.get("VOXQUARRY_SNAPSHOT_ROOT")
            else None
        ),
    )
    parser.add_argument("--var-root", type=Path, default=DEFAULT_VAR_ROOT)
    parser.add_argument("--category")
    parser.add_argument("--brand", action="append", dest="brands")
    parser.add_argument("--report-name")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.snapshot_root is None:
        raise SystemExit(
            "--snapshot-root or VOXQUARRY_SNAPSHOT_ROOT is required"
        )
    if args.command in {"generate-report", "generate-mower"}:
        category = (
            "割草机"
            if args.command == "generate-mower"
            else str(args.category or "").strip()
        )
        if not category:
            raise SystemExit("generate-report requires --category")
        scope = {
            **default_scope(),
            "categories": [category],
            "exclude_categories": [],
            "brands": sorted(set(args.brands or [])),
        }
        service = ReportingService(args.snapshot_root, args.var_root)
        request = normalize_request(
            {
                "report_name": args.report_name,
                "scope": scope,
                "primary_direction": "product-opportunity",
                "analysis_modules": [
                    item["id"]
                    for item in SCENE_CATALOG
                    if item["id"] != "product-opportunity"
                ],
                "factors": list(FACTOR_IDS),
            },
            defaults=default_scope(),
            analysis_version=ANALYSIS_VERSION,
        )
        report = service.run_sync(request)
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "title": report["title"],
                    "samples": report["samples"],
                    "hashes": report["hashes"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    import uvicorn

    uvicorn.run(
        create_app(args.snapshot_root, args.var_root),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
