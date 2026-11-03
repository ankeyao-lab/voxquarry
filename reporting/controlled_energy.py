from __future__ import annotations

import re
from collections import Counter
from typing import Any

from reporting.category_vocabulary import category_id_for
from reporting.voltage_platform import VoltagePlatformError, voltage_platform_fact


SCHEMA_VERSION = "voxquarry.task-energy/v1"
ENERGY_TYPES = ("corded", "battery", "fuel", "other", "unknown")
REPORT_ENERGY_TYPES = ("all",) + ENERGY_TYPES
ENERGY_LABELS = {
    "all": "全部",
    "corded": "有线",
    "battery": "电池",
    "fuel": "燃油",
    "other": "其他",
    "unknown": "未知",
}
READINESS_STATUSES = {"ready", "metadata_incomplete"}
VOLTAGE_RE = re.compile(
    r"^[1-9][0-9]*(?:\.[0-9]+)?V"
    r"(?:/[1-9][0-9]*(?:\.[0-9]+)?V)?$"
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SNAPSHOT_ID_RE = re.compile(r"^prod_v2_[0-9a-f]{24}$")
ENERGY_COLUMNS = (
    "energy_schema_version",
    "energy_type",
    "energy_readiness_status",
    "energy_evidence_source",
    "energy_canonical_voltage",
    "energy_unknown_reason",
)


class ControlledEnergyError(ValueError):
    """Stable fail-closed error for the Wotu task-energy fact."""

    def __init__(self, message: str, *, code: str = "controlled_energy_invalid"):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _is_absent(value: Any) -> bool:
    return value is None or value == ""


def _job_category_id(job: dict[str, Any]) -> str:
    return _clean(job.get("category_id")) or category_id_for(
        _clean(job.get("category"))
    )


def _legacy_unknown(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": None,
        "energy_type": "unknown",
        "label": ENERGY_LABELS["unknown"],
        "readiness_status": None,
        "evidence_source": None,
        "canonical_voltage": None,
        "unknown_reason": "legacy_null",
        "contract_status": "legacy_null",
        "product_observation_hash": None,
    }


def _scope_energy_fact(
    job: dict[str, Any], *, allow_legacy_incomplete: bool = False
) -> dict[str, Any]:
    """Project pre-contract rows as unknown only for the unfiltered all scope."""
    if allow_legacy_incomplete and _is_absent(job.get("energy_schema_version")):
        return _legacy_unknown(job)
    return controlled_energy_fact(job)


def controlled_energy_fact(job: dict[str, Any]) -> dict[str, Any]:
    """Validate one task fact without inferring energy from any other field."""
    values = {column: job.get(column) for column in ENERGY_COLUMNS}
    if all(_is_absent(value) for value in values.values()):
        return _legacy_unknown(job)
    if any(_is_absent(values[column]) for column in (
        "energy_schema_version",
        "energy_type",
        "energy_readiness_status",
        "energy_evidence_source",
    )):
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} 能源事实字段不完整"
        )

    schema_version = _clean(values["energy_schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} 能源 schema 不受支持",
            code="controlled_energy_schema_unsupported",
        )
    energy_type = _clean(values["energy_type"])
    if energy_type not in ENERGY_TYPES:
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} energy_type 无效"
        )
    readiness_status = _clean(values["energy_readiness_status"])
    if readiness_status not in READINESS_STATUSES:
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} energy_readiness_status 无效"
        )
    evidence_source = _clean(values["energy_evidence_source"])
    canonical_voltage = _clean(values["energy_canonical_voltage"]) or None
    unknown_reason = _clean(values["energy_unknown_reason"]) or None
    observation_hash = _clean(job.get("product_observation_hash")) or None
    if not evidence_source:
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} energy_evidence_source 缺失"
        )
    if observation_hash is None or SHA256_RE.fullmatch(observation_hash) is None:
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} 产品观察哈希缺失或无效"
        )

    if energy_type == "battery":
        if readiness_status == "ready":
            if (
                canonical_voltage is None
                or VOLTAGE_RE.fullmatch(canonical_voltage) is None
                or unknown_reason is not None
            ):
                raise ControlledEnergyError(
                    f"job_id={_clean(job.get('id'))} battery 能源字段矛盾"
                )
        elif canonical_voltage is not None or unknown_reason != "battery_voltage_required":
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} battery 缺电压状态矛盾"
            )
    elif energy_type == "unknown":
        if (
            readiness_status != "metadata_incomplete"
            or canonical_voltage is not None
            or unknown_reason != "energy_type_required"
        ):
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} unknown 能源字段矛盾"
            )
    elif energy_type == "other":
        if (
            readiness_status != "ready"
            or unknown_reason is not None
            or (
                canonical_voltage is not None
                and VOLTAGE_RE.fullmatch(canonical_voltage) is None
            )
        ):
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} other 能源字段矛盾"
            )
    elif (
        readiness_status != "ready"
        or canonical_voltage is not None
        or unknown_reason is not None
    ):
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} {energy_type} 能源字段矛盾"
        )

    try:
        platform = voltage_platform_fact(job)
    except VoltagePlatformError as exc:
        raise ControlledEnergyError(str(exc)) from exc
    if energy_type == "battery" and readiness_status == "ready":
        if (
            platform["status"] != "known"
            or platform["canonical_voltage"] != canonical_voltage
        ):
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} battery 与电压平台不一致"
            )
    elif energy_type == "battery":
        if platform["status"] != "unknown":
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} battery 缺电压与电压平台不一致"
            )
    elif energy_type == "other":
        if canonical_voltage is None:
            # Immutable snapshots produced before ADR-0053 may carry the old
            # ``other + non_dc`` projection. Accept that voltage-free shape
            # during the rolling migration; current producers no longer infer
            # non-DC from ``other`` alone.
            if platform["status"] not in {"unknown", "non_dc"}:
                raise ControlledEnergyError(
                    f"job_id={_clean(job.get('id'))} other 无电压与平台不一致"
                )
        elif (
            platform["status"] not in {"known", "unknown"}
            or platform["canonical_voltage"] != canonical_voltage
        ):
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} other 电压与平台不一致"
            )
    elif energy_type in {"corded", "fuel"}:
        if platform["status"] != "non_dc":
            raise ControlledEnergyError(
                f"job_id={_clean(job.get('id'))} {energy_type} 不得声明 DC 平台"
            )
    elif energy_type == "unknown" and platform["status"] == "known":
        raise ControlledEnergyError(
            f"job_id={_clean(job.get('id'))} unknown 不得声明已知 DC 平台"
        )

    return {
        "schema_version": schema_version,
        "energy_type": energy_type,
        "label": ENERGY_LABELS[energy_type],
        "readiness_status": readiness_status,
        "evidence_source": evidence_source,
        "canonical_voltage": canonical_voltage,
        "unknown_reason": unknown_reason,
        "contract_status": "verified",
        "product_observation_hash": observation_hash,
    }


def energy_catalog(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    governed: Counter[str] = Counter()
    legacy: Counter[str] = Counter()
    for job in jobs:
        fact = controlled_energy_fact(job)
        energy_type = fact["energy_type"]
        counts[energy_type] += 1
        if fact["contract_status"] == "legacy_null":
            legacy[energy_type] += 1
        else:
            governed[energy_type] += 1
    return [
        {
            "schema_version": SCHEMA_VERSION,
            "energy_type": energy_type,
            "label": ENERGY_LABELS[energy_type],
            "task_count": int(counts[energy_type]),
            "governed_task_count": int(governed[energy_type]),
            "legacy_null_task_count": int(legacy[energy_type]),
        }
        for energy_type in ENERGY_TYPES
        if counts[energy_type]
    ]


def task_energy_ref(job: dict[str, Any], fact: dict[str, Any] | None = None) -> dict[str, Any]:
    fact = fact or controlled_energy_fact(job)
    return {
        "job_id": _clean(job.get("id")),
        "schema_version": fact["schema_version"],
        "energy_type": fact["energy_type"],
        "label": fact["label"],
        "readiness_status": fact["readiness_status"],
        "evidence_source": fact["evidence_source"],
        "canonical_voltage": fact["canonical_voltage"],
        "unknown_reason": fact["unknown_reason"],
        "contract_status": fact["contract_status"],
        "product_observation_hash": fact["product_observation_hash"],
    }


def resolve_controlled_energy_scope(
    scope: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    reportable_job_ids: set[str] | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one PW energy code and freeze task-level audit references."""
    resolved = dict(scope)
    for field in ("energy", "energy_refs", "energy_selection"):
        resolved.pop(field, None)
    category_id = _clean(resolved.get("category_id"))
    requested_energy = _clean(resolved.get("energy_type"))
    if category_id != "pressure_washer":
        if requested_energy:
            raise ControlledEnergyError("能源类型筛选当前仅适用于 Pressure Washer")
        resolved["energy_type"] = ""
        return resolved
    if requested_energy not in REPORT_ENERGY_TYPES:
        raise ControlledEnergyError(
            "Pressure Washer 报告必须且只能选择一个能源类型",
            code="controlled_energy_required",
        )
    resolved["energy_type"] = requested_energy

    source_identity = {
        key: _clean((source_snapshot or {}).get(key))
        for key in ("snapshot_id", "schema_version", "content_sha256")
    }
    if (
        SNAPSHOT_ID_RE.fullmatch(source_identity["snapshot_id"]) is None
        or source_identity["schema_version"]
        != "voxquarry-reporting-snapshot/v2"
        or SHA256_RE.fullmatch(source_identity["content_sha256"]) is None
    ):
        raise ControlledEnergyError(
            "Pressure Washer 能源报告缺少可验证的全量 Snapshot 身份",
            code="controlled_energy_source_snapshot_unavailable",
        )

    allowed_ids = reportable_job_ids
    scoped_jobs = [
        job
        for job in jobs
        if _clean(job.get("id"))
        and (allowed_ids is None or _clean(job.get("id")) in allowed_ids)
        and _job_category_id(job) == category_id
    ]
    by_id = {_clean(job.get("id")): job for job in scoped_jobs}
    requested_ids = sorted(set(resolved.get("job_ids") or []))
    missing = sorted(set(requested_ids) - set(by_id))
    if missing:
        raise ControlledEnergyError(
            "能源范围包含不可报告的 job_id：" + "、".join(missing)
        )
    refs = []
    for job_id in requested_ids:
        fact = _scope_energy_fact(
            by_id[job_id], allow_legacy_incomplete=requested_energy == "all"
        )
        if requested_energy != "all" and fact["energy_type"] != requested_energy:
            raise ControlledEnergyError(
                f"job_id={job_id} 不属于所选能源类型"
            )
        refs.append(task_energy_ref(by_id[job_id], fact))
    if not refs:
        raise ControlledEnergyError("所选能源类型没有可报告任务")

    category_facts = {
        _clean(job.get("id")): _scope_energy_fact(
            job, allow_legacy_incomplete=requested_energy == "all"
        )
        for job in scoped_jobs
    }
    category_counts = Counter(
        fact["energy_type"] for fact in category_facts.values()
    )
    category_review_counts: Counter[str] = Counter()
    for job in scoped_jobs:
        fact = category_facts[_clean(job.get("id"))]
        category_review_counts[fact["energy_type"]] += int(
            job.get("unique_reviews") or 0
        )
    selected_energy_count = (
        len(scoped_jobs)
        if requested_energy == "all"
        else int(category_counts[requested_energy])
    )
    resolved["energy"] = {
        "schema_version": SCHEMA_VERSION,
        "energy_type": requested_energy,
        "label": ENERGY_LABELS[requested_energy],
    }
    resolved["energy_refs"] = refs
    resolved["energy_selection"] = {
        "schema_version": "voxquarry.reporting-energy-selection/v1",
        "source_snapshot": source_identity,
        "category_reportable_job_count": len(scoped_jobs),
        "category_energy_job_count": selected_energy_count,
        "category_energy_task_counts": {
            energy_type: int(category_counts[energy_type])
            for energy_type in ENERGY_TYPES
        },
        "category_energy_review_counts": {
            energy_type: int(category_review_counts[energy_type])
            for energy_type in ENERGY_TYPES
        },
        "selected_job_count": len(refs),
        "excluded_other_energy_job_count": (
            len(scoped_jobs) - selected_energy_count
        ),
        "selected_governed_job_count": sum(
            ref["contract_status"] == "verified" for ref in refs
        ),
        "selected_legacy_null_job_count": sum(
            ref["contract_status"] == "legacy_null" for ref in refs
        ),
    }
    return resolved
