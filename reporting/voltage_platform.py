from __future__ import annotations

import re
import unicodedata
from typing import Any


SCHEMA_VERSION = "voxquarry.voltage-platform/v1"
STATUS_VALUES = {"known", "unknown", "non_dc"}
VOLTAGE_RE = re.compile(
    r"^[1-9][0-9]*(?:\.[0-9]+)?V"
    r"(?:/[1-9][0-9]*(?:\.[0-9]+)?V)?$"
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

PLATFORM_COLUMNS = (
    "voltage_platform_schema_version",
    "voltage_platform_status",
    "voltage_platform_key",
    "voltage_platform_brand",
    "voltage_platform_voltage",
    "voltage_platform_label",
    "voltage_platform_evidence_source",
    "voltage_platform_null_reason",
)


class VoltagePlatformError(ValueError):
    """Stable fail-closed error for Wotu voltage-platform task facts."""

    def __init__(self, message: str, *, code: str = "voltage_platform_invalid"):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def normalized_brand(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold()
    pieces: list[str] = []
    pending_separator = False
    for character in text:
        if character.isalnum():
            if pending_separator and pieces:
                pieces.append("-")
            pieces.append(character)
            pending_separator = False
        else:
            pending_separator = True
    return "".join(pieces).strip("-")


def _is_absent(value: Any) -> bool:
    return value is None or value == ""


def _legacy_unknown(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": None,
        "status": "unknown",
        "platform_key": None,
        "brand": _clean(job.get("brand")) or None,
        "canonical_voltage": None,
        "display_name": None,
        "evidence_source": None,
        "null_reason": "legacy_unbound",
        "product_observation_hash": None,
    }


def voltage_platform_fact(job: dict[str, Any]) -> dict[str, Any]:
    """Validate and project the versioned task-level Wotu platform fact."""
    values = {column: job.get(column) for column in PLATFORM_COLUMNS}
    if all(_is_absent(value) for value in values.values()):
        return _legacy_unknown(job)

    schema_version = _clean(values["voltage_platform_schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise VoltagePlatformError(
            f"job_id={_clean(job.get('id'))} 电压平台 schema 不受支持"
        )
    status = _clean(values["voltage_platform_status"])
    if status not in STATUS_VALUES:
        raise VoltagePlatformError(
            f"job_id={_clean(job.get('id'))} 电压平台 status 无效"
        )
    platform_key = _clean(values["voltage_platform_key"]) or None
    brand = _clean(values["voltage_platform_brand"]) or None
    voltage = _clean(values["voltage_platform_voltage"]) or None
    display_name = _clean(values["voltage_platform_label"]) or None
    evidence_source = _clean(values["voltage_platform_evidence_source"]) or None
    null_reason = _clean(values["voltage_platform_null_reason"]) or None
    observation_hash = _clean(job.get("product_observation_hash")) or None
    if observation_hash is None or SHA256_RE.fullmatch(observation_hash) is None:
        raise VoltagePlatformError(
            f"job_id={_clean(job.get('id'))} 产品观察哈希缺失或无效"
        )

    if status == "known":
        if not all((platform_key, brand, voltage, display_name, evidence_source)):
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} known 电压平台字段不完整"
            )
        if null_reason is not None or VOLTAGE_RE.fullmatch(voltage or "") is None:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} known 电压平台字段矛盾"
            )
        expected_key = f"{normalized_brand(brand)}|{voltage}"
        if not normalized_brand(brand) or platform_key != expected_key:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} platform_key 与品牌+电压不一致"
            )
        if display_name != f"{brand} {voltage}":
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} 电压平台展示快照不一致"
            )
    elif status == "unknown":
        if platform_key is not None or display_name is not None:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} {status} 不得声明平台身份"
            )
        if null_reason not in {"brand_unknown", "voltage_unknown"}:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} unknown null_reason 无效"
            )
        if voltage is not None and VOLTAGE_RE.fullmatch(voltage) is None:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} unknown 电压格式无效"
            )
    else:
        if platform_key is not None or voltage is not None:
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} non_dc 不得声明平台身份"
            )
        if null_reason != "not_dc" or display_name != "非 DC":
            raise VoltagePlatformError(
                f"job_id={_clean(job.get('id'))} non_dc 契约字段矛盾"
            )

    return {
        "schema_version": schema_version,
        "status": status,
        "platform_key": platform_key,
        "brand": brand,
        "canonical_voltage": voltage,
        "display_name": display_name,
        "evidence_source": evidence_source,
        "null_reason": null_reason,
        "product_observation_hash": observation_hash,
    }


def platform_catalog(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for job in jobs:
        fact = voltage_platform_fact(job)
        if fact["status"] != "known":
            continue
        row = {
            "schema_version": fact["schema_version"],
            "platform_key": fact["platform_key"],
            "brand": fact["brand"],
            "canonical_voltage": fact["canonical_voltage"],
            "display_name": fact["display_name"],
        }
        existing = rows.get(str(fact["platform_key"]))
        if existing is not None and existing != row:
            raise VoltagePlatformError(
                f"platform_key={fact['platform_key']} 存在冲突展示快照"
            )
        rows[str(fact["platform_key"])] = row
    return sorted(
        rows.values(),
        key=lambda row: (
            str(row["brand"]).casefold(),
            str(row["canonical_voltage"]),
            str(row["platform_key"]),
        ),
    )


def _task_ref(job: dict[str, Any], fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": _clean(job.get("id")),
        "schema_version": fact["schema_version"],
        "status": fact["status"],
        "platform_key": fact["platform_key"],
        "brand": fact["brand"],
        "canonical_voltage": fact["canonical_voltage"],
        "display_name": fact["display_name"],
        "evidence_source": fact["evidence_source"],
        "null_reason": fact["null_reason"],
        "product_observation_hash": fact["product_observation_hash"],
    }


def resolve_voltage_platform_scope(
    scope: dict[str, Any], jobs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Resolve client keys against fixed task facts and freeze their audit."""
    resolved = dict(scope)
    resolved.pop("voltage_platforms", None)
    resolved.pop("voltage_platform_refs", None)
    requested_keys = sorted(
        {
            _clean(value)
            for value in resolved.get("voltage_platform_keys") or []
            if _clean(value)
        }
    )
    include_unknown = resolved.get("include_unknown_voltage_platform", False)
    if not isinstance(include_unknown, bool):
        raise VoltagePlatformError("include_unknown_voltage_platform 必须是布尔值")
    resolved["voltage_platform_keys"] = requested_keys
    resolved["include_unknown_voltage_platform"] = include_unknown
    if not requested_keys and not include_unknown:
        return resolved

    catalog_rows = {row["platform_key"]: row for row in platform_catalog(jobs)}
    unknown_keys = sorted(set(requested_keys) - set(catalog_rows))
    if unknown_keys:
        raise VoltagePlatformError(
            "未知电压平台：" + "、".join(unknown_keys),
            code="voltage_platform_not_found",
        )
    jobs_by_id = {_clean(job.get("id")): job for job in jobs if _clean(job.get("id"))}
    requested_job_ids = sorted(set(resolved.get("job_ids") or []))
    if set(requested_job_ids) - set(jobs_by_id):
        raise VoltagePlatformError("电压平台范围包含不存在的 job_id")

    refs = []
    for job_id in requested_job_ids:
        job = jobs_by_id[job_id]
        fact = voltage_platform_fact(job)
        matches = (
            fact["status"] == "known" and fact["platform_key"] in requested_keys
        ) or (fact["status"] == "unknown" and include_unknown)
        if not matches:
            raise VoltagePlatformError(
                f"job_id={job_id} 不属于所选电压平台"
            )
        refs.append(_task_ref(job, fact))
    resolved["voltage_platforms"] = [catalog_rows[key] for key in requested_keys]
    resolved["voltage_platform_refs"] = refs
    return resolved
