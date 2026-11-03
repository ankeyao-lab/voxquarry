"""Fail-closed consumer for Chengmu ``retail-product-master/v2``.

The projection is copied into an immutable reporting snapshot.  Reporting
never reads Chengmu's internal tables and never derives a model or voltage
platform identity from editable display text.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "retail-product-master/v2"
HASH_ALGORITHM = "sha256-canonical-json/v1"
PROJECTION_FILENAME = "product-master-v2.json"
ANALYSIS_POLICY_VERSION = "retail-analysis-eligibility/v1"

MODEL_ID_RE = re.compile(r"^pvr_[0-9a-f]{20}$")
BRAND_ID_RE = re.compile(r"^brd_[0-9a-f]{20}$")
PLATFORM_ID_RE = re.compile(r"^vpf_[0-9a-f]{20}$")
SNAPSHOT_ID_RE = re.compile(r"^pms_[0-9a-f]{64}$")
RECORD_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProductMasterError(ValueError):
    """Stable error for an invalid or unavailable product-master projection."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "product_master_contract_invalid",
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field

    def as_detail(self) -> dict[str, str]:
        detail = {"code": self.code, "message": str(self)}
        if self.field:
            detail["field"] = self.field
        return detail


TASK_BINDING_FIELDS = (
    "product_master_schema_version",
    "product_master_snapshot_id",
    "product_master_model_id",
    "product_master_entity_version",
    "product_master_record_snapshot_hash",
)


def _number(value: object) -> str:
    if isinstance(value, bool):
        raise ProductMasterError("boolean is not a canonical number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProductMasterError("product master contains a non-finite number")
        number = Decimal(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ProductMasterError("product master contains a non-finite number")
        number = value
    else:
        raise ProductMasterError("unsupported canonical number")
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def canonical_json(value: object) -> str:
    """Implement Chengmu's ``sha256-canonical-json/v1`` serialization."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float, Decimal)):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ProductMasterError("product master object keys must be strings")
        return "{" + ",".join(
            f"{canonical_json(key)}:{canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise ProductMasterError(
        f"unsupported product master value type: {type(value).__name__}"
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_keys(
    value: object,
    *,
    field: str,
    required: set[str],
    allowed: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductMasterError(f"{field} must be an object")
    missing = sorted(required - set(value))
    if missing:
        raise ProductMasterError(f"{field} missing fields: {', '.join(missing)}")
    if allowed is not None:
        extra = sorted(set(value) - allowed)
        if extra:
            raise ProductMasterError(f"{field} has unknown fields: {', '.join(extra)}")
    return value


def _matches(pattern: re.Pattern[str], value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProductMasterError(f"{field} is invalid")
    text = value
    if pattern.fullmatch(text) is None:
        raise ProductMasterError(f"{field} is invalid")
    return text


def _date_time_or_none(value: object, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ProductMasterError(f"{field} is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ProductMasterError(f"{field} is invalid") from exc
    if parsed.utcoffset() is None:
        raise ProductMasterError(f"{field} must include a timezone")


def _validate_record(record: object, index: int) -> dict[str, Any]:
    field = f"records[{index}]"
    required = {
        "model_id", "entity_version", "product_kind", "category", "brand",
        "model_number", "product_name", "energy_type", "voltage_platform",
        "voltage_platform_null_reason", "bundle", "analysis_eligibility",
        "review_status", "reviewed_at", "record_snapshot_hash",
    }
    row = _require_keys(record, field=field, required=required, allowed=required)
    _matches(MODEL_ID_RE, row["model_id"], f"{field}.model_id")
    if not isinstance(row["entity_version"], int) or row["entity_version"] < 1:
        raise ProductMasterError(f"{field}.entity_version is invalid")
    if row["product_kind"] not in {"single_product", "bundle"}:
        raise ProductMasterError(f"{field}.product_kind is invalid")
    if not isinstance(row["model_number"], str) or not row["model_number"].strip():
        raise ProductMasterError(f"{field}.model_number is empty")
    if row["product_name"] is not None and not isinstance(row["product_name"], str):
        raise ProductMasterError(f"{field}.product_name is invalid")
    category = _require_keys(
        row["category"], field=f"{field}.category",
        required={"category_id", "code", "name", "name_zh"},
        allowed={"category_id", "code", "name", "name_zh"},
    )
    if not isinstance(category["category_id"], str) or not re.fullmatch(
        r"cat_[a-z0-9_]+", category["category_id"]
    ):
        raise ProductMasterError(f"{field}.category.category_id is invalid")
    if not isinstance(category["code"], str) or not re.fullmatch(
        r"[a-z0-9_]+", category["code"]
    ):
        raise ProductMasterError(f"{field}.category.code is invalid")
    if (
        not isinstance(category["name"], str)
        or not category["name"].strip()
        or not isinstance(category["name_zh"], str)
        or not category["name_zh"].strip()
    ):
        raise ProductMasterError(f"{field}.category display name is empty")
    brand = _require_keys(
        row["brand"], field=f"{field}.brand",
        required={"brand_id", "canonical_name"},
        allowed={"brand_id", "canonical_name"},
    )
    brand_id = _matches(BRAND_ID_RE, brand["brand_id"], f"{field}.brand.brand_id")
    if (
        not isinstance(brand["canonical_name"], str)
        or not brand["canonical_name"].strip()
    ):
        raise ProductMasterError(f"{field}.brand.canonical_name is empty")
    if row["energy_type"] not in {"Gas", "AC", "DC", None}:
        raise ProductMasterError(f"{field}.energy_type is invalid")
    platform = row["voltage_platform"]
    null_reason = row["voltage_platform_null_reason"]
    if platform is None:
        if null_reason not in {"not_dc", "brand_unresolved", "voltage_unverified"}:
            raise ProductMasterError(f"{field}.voltage_platform_null_reason is invalid")
        if row["energy_type"] in {"Gas", "AC", None} and null_reason != "not_dc":
            raise ProductMasterError(f"{field} non-DC model must use not_dc")
    else:
        platform = _require_keys(
            platform, field=f"{field}.voltage_platform",
            required={
                "voltage_platform_id", "brand_id", "canonical_voltage",
                "voltage_value", "voltage_unit", "identity_version",
            },
            allowed={
                "voltage_platform_id", "brand_id", "canonical_voltage",
                "voltage_value", "voltage_unit", "identity_version",
            },
        )
        _matches(
            PLATFORM_ID_RE, platform["voltage_platform_id"],
            f"{field}.voltage_platform.voltage_platform_id",
        )
        if platform["brand_id"] != brand_id:
            raise ProductMasterError(f"{field} platform brand_id mismatch")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?V", str(platform["canonical_voltage"])):
            raise ProductMasterError(f"{field}.canonical_voltage is invalid")
        if platform["voltage_unit"] != "V" or platform["identity_version"] != "brand-voltage/v1":
            raise ProductMasterError(f"{field} platform contract is invalid")
        try:
            voltage_value = Decimal(str(platform["voltage_value"]))
            canonical_value = Decimal(str(platform["canonical_voltage"])[:-1])
        except Exception as exc:
            raise ProductMasterError(f"{field}.voltage_value is invalid") from exc
        if voltage_value <= 0 or voltage_value != canonical_value:
            raise ProductMasterError(f"{field}.voltage_value mismatch")
        if row["energy_type"] != "DC":
            raise ProductMasterError(f"{field} non-DC model cannot have a voltage platform")
        if null_reason is not None:
            raise ProductMasterError(f"{field} populated platform cannot have a null reason")
    bundle = row["bundle"]
    if row["product_kind"] == "single_product":
        if bundle is not None:
            raise ProductMasterError(f"{field} single_product bundle must be null")
    else:
        bundle = _require_keys(
            bundle, field=f"{field}.bundle",
            required={"scope", "primary_model_id", "members"},
            allowed={"scope", "primary_model_id", "members"},
        )
        if bundle["scope"] not in {"same_category", "cross_category"}:
            raise ProductMasterError(f"{field}.bundle.scope is invalid")
        primary_model_id = bundle["primary_model_id"]
        if primary_model_id is not None:
            _matches(MODEL_ID_RE, primary_model_id, f"{field}.bundle.primary_model_id")
        if not isinstance(bundle["members"], list):
            raise ProductMasterError(f"{field}.bundle.members must be an array")
        for member_index, member_value in enumerate(bundle["members"]):
            member_field = f"{field}.bundle.members[{member_index}]"
            member = _require_keys(
                member_value, field=member_field,
                required={
                    "role", "member_model_id", "member_label",
                    "resolution_status", "quantity", "evidence",
                },
                allowed={
                    "role", "member_model_id", "member_label",
                    "resolution_status", "quantity", "evidence",
                },
            )
            if member["role"] not in {
                "primary_product", "included_product", "battery", "charger",
                "accessory", "other",
            }:
                raise ProductMasterError(f"{member_field}.role is invalid")
            if member["resolution_status"] not in {"resolved", "unresolved"}:
                raise ProductMasterError(f"{member_field}.resolution_status is invalid")
            if member["member_label"] is not None and not isinstance(
                member["member_label"], str
            ):
                raise ProductMasterError(f"{member_field}.member_label is invalid")
            member_model_id = member["member_model_id"]
            if member["resolution_status"] == "resolved":
                _matches(MODEL_ID_RE, member_model_id, f"{member_field}.member_model_id")
            elif member_model_id is not None:
                raise ProductMasterError(
                    f"{member_field} unresolved member_model_id must be null"
                )
            elif not str(member["member_label"] or "").strip():
                raise ProductMasterError(
                    f"{member_field} unresolved member_label is required"
                )
            if not isinstance(member["quantity"], int) or member["quantity"] < 1:
                raise ProductMasterError(f"{member_field}.quantity is invalid")
            evidence = _require_keys(
                member["evidence"], field=f"{member_field}.evidence",
                required={"source_url", "source_type", "source_snapshot_hash", "observed_at"},
                allowed={"source_url", "source_type", "source_snapshot_hash", "observed_at"},
            )
            if any(value is not None and not isinstance(value, str) for value in evidence.values()):
                raise ProductMasterError(f"{member_field}.evidence is invalid")
            _date_time_or_none(
                evidence["observed_at"], f"{member_field}.evidence.observed_at"
            )
    eligibility = _require_keys(
        row["analysis_eligibility"], field=f"{field}.analysis_eligibility",
        required={"eligible", "reason", "policy_version"},
        allowed={"eligible", "reason", "policy_version"},
    )
    if not isinstance(eligibility["eligible"], bool):
        raise ProductMasterError(f"{field}.analysis_eligibility.eligible is invalid")
    if eligibility["reason"] not in {
        None, "cross_category_bundle", "bundle_primary_unresolved",
        "bundle_primary_category_mismatch",
    }:
        raise ProductMasterError(f"{field}.analysis_eligibility.reason is invalid")
    if eligibility["policy_version"] != ANALYSIS_POLICY_VERSION:
        raise ProductMasterError(f"{field}.analysis_eligibility policy mismatch")
    if eligibility["eligible"] != (eligibility["reason"] is None):
        raise ProductMasterError(f"{field}.analysis_eligibility eligible/reason mismatch")
    if row["product_kind"] == "single_product" and eligibility != {
        "eligible": True,
        "reason": None,
        "policy_version": ANALYSIS_POLICY_VERSION,
    }:
        raise ProductMasterError(f"{field} single_product must be analysis eligible")
    if row["review_status"] not in {"approved", "published"}:
        raise ProductMasterError(f"{field} is not approved/published")
    _date_time_or_none(row["reviewed_at"], f"{field}.reviewed_at")
    is_cross_category = category["code"] == "cross_category_bundle"
    bundle_is_cross_category = (
        isinstance(row["bundle"], dict)
        and row["bundle"].get("scope") == "cross_category"
    )
    if is_cross_category != bundle_is_cross_category:
        raise ProductMasterError(f"{field} cross-category identity mismatch")
    if is_cross_category:
        bundle = row["bundle"] if isinstance(row["bundle"], dict) else {}
        if (
            row["product_kind"] != "bundle"
            or category["category_id"] != "cat_cross_category_bundle"
            or category["name"] != "Cross-category Bundle"
            or bundle.get("scope") != "cross_category"
            or eligibility != {
                "eligible": False,
                "reason": "cross_category_bundle",
                "policy_version": ANALYSIS_POLICY_VERSION,
            }
        ):
            raise ProductMasterError(f"{field} cross-category eligibility mismatch")
    expected_hash = "sha256:" + _sha256(
        {key: value for key, value in row.items() if key != "record_snapshot_hash"}
    )
    _matches(RECORD_HASH_RE, row["record_snapshot_hash"], f"{field}.record_snapshot_hash")
    if row["record_snapshot_hash"] != expected_hash:
        raise ProductMasterError(f"{field}.record_snapshot_hash mismatch")
    return row


@dataclass(frozen=True)
class ProductMasterProjection:
    payload: dict[str, Any]
    path: Path

    @property
    def snapshot_id(self) -> str:
        return str(self.payload["snapshot_id"])

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload["records"])

    @property
    def records_by_model_id(self) -> dict[str, dict[str, Any]]:
        return {str(row["model_id"]): row for row in self.records}

    def platform_catalog(
        self, records: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in records if records is not None else self.records:
            platform = row.get("voltage_platform")
            if not isinstance(platform, dict):
                continue
            platform_id = str(platform["voltage_platform_id"])
            item = {
                "voltage_platform_id": platform_id,
                "brand_id": str(platform["brand_id"]),
                "brand_name": str(row["brand"]["canonical_name"]),
                "canonical_voltage": str(platform["canonical_voltage"]),
                "display_name": (
                    f"{row['brand']['canonical_name']} "
                    f"{platform['canonical_voltage']}"
                ),
            }
            existing = grouped.get(platform_id)
            if existing is not None and existing != item:
                raise ProductMasterError("voltage platform identity collision")
            grouped[platform_id] = item
        return sorted(grouped.values(), key=lambda item: (
            item["brand_name"].casefold(), item["canonical_voltage"],
            item["voltage_platform_id"],
        ))


def load_product_master_file(path: Path) -> ProductMasterProjection:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductMasterError(f"cannot read product master projection: {exc}") from exc
    top_keys = {
        "schema_version", "snapshot_id", "snapshot_generated_at",
        "hash_algorithm", "authority", "scope", "ordering", "pagination",
        "records",
    }
    root = _require_keys(payload, field="product_master", required=top_keys, allowed=top_keys)
    if root["schema_version"] != SCHEMA_VERSION:
        raise ProductMasterError("product master schema_version mismatch")
    if root["hash_algorithm"] != HASH_ALGORITHM or root["authority"] != "chengmu":
        raise ProductMasterError("product master authority/hash contract mismatch")
    _date_time_or_none(
        root["snapshot_generated_at"], "product_master.snapshot_generated_at"
    )
    _matches(SNAPSHOT_ID_RE, root["snapshot_id"], "product_master.snapshot_id")
    scope = _require_keys(
        root["scope"], field="product_master.scope",
        required={"review_status", "category_code", "model_ids", "voltage_platform_ids"},
        allowed={"review_status", "category_code", "model_ids", "voltage_platform_ids"},
    )
    if scope["review_status"] != ["approved", "published"]:
        raise ProductMasterError("product master must be approved/published-only")
    if scope["category_code"] is not None and not isinstance(scope["category_code"], str):
        raise ProductMasterError("product master category_code is invalid")
    if not isinstance(scope["model_ids"], list) or not isinstance(scope["voltage_platform_ids"], list):
        raise ProductMasterError("product master scope arrays are invalid")
    if len(scope["model_ids"]) != len(set(scope["model_ids"])) or any(
        MODEL_ID_RE.fullmatch(str(value)) is None for value in scope["model_ids"]
    ):
        raise ProductMasterError("product master scope.model_ids is invalid")
    if len(scope["voltage_platform_ids"]) != len(set(scope["voltage_platform_ids"])) or any(
        PLATFORM_ID_RE.fullmatch(str(value)) is None
        for value in scope["voltage_platform_ids"]
    ):
        raise ProductMasterError("product master scope.voltage_platform_ids is invalid")
    if root["ordering"] != (
        "category.code, brand.canonical_name(casefold), "
        "model_number(casefold), model_id"
    ):
        raise ProductMasterError("product master ordering contract mismatch")
    pagination = _require_keys(
        root["pagination"], field="product_master.pagination",
        required={"mode", "total_records"}, allowed={"mode", "total_records"},
    )
    if pagination["mode"] != "complete":
        raise ProductMasterError("product master pagination must be complete")
    if (
        isinstance(pagination["total_records"], bool)
        or not isinstance(pagination["total_records"], int)
        or pagination["total_records"] < 0
    ):
        raise ProductMasterError("product master total_records is invalid")
    if not isinstance(root["records"], list):
        raise ProductMasterError("product master records must be an array")
    records = [_validate_record(row, index) for index, row in enumerate(root["records"])]
    if pagination["total_records"] != len(records):
        raise ProductMasterError("product master total_records mismatch")
    model_ids = [str(row["model_id"]) for row in records]
    if len(model_ids) != len(set(model_ids)):
        raise ProductMasterError("product master contains duplicate model_id")
    if scope["category_code"] is not None and any(
        row["category"]["code"] != scope["category_code"] for row in records
    ):
        raise ProductMasterError("product master category filter mismatch")
    if scope["model_ids"] and any(
        row["model_id"] not in scope["model_ids"] for row in records
    ):
        raise ProductMasterError("product master model filter mismatch")
    if scope["voltage_platform_ids"] and any(
        not isinstance(row["voltage_platform"], dict)
        or row["voltage_platform"]["voltage_platform_id"]
        not in scope["voltage_platform_ids"]
        for row in records
    ):
        raise ProductMasterError("product master voltage platform filter mismatch")
    expected_order = sorted(records, key=lambda row: (
        str(row["category"]["code"]),
        str(row["brand"]["canonical_name"]).casefold(),
        str(row["model_number"]).casefold(),
        str(row["model_id"]),
    ))
    if records != expected_order:
        raise ProductMasterError("product master records are not in contract order")
    expected_snapshot_id = "pms_" + _sha256({
        "schema_version": root["schema_version"],
        "authority": root["authority"],
        "scope": root["scope"],
        "records": root["records"],
    })
    if root["snapshot_id"] != expected_snapshot_id:
        raise ProductMasterError("product master snapshot_id mismatch")
    return ProductMasterProjection(root, path)


def validated_product_master_file_identity(
    path: Path,
    *,
    require_read_only: bool = False,
    require_complete_authority_scope: bool = False,
) -> dict[str, Any]:
    """Validate a physical Chengmu projection and return its stable identity.

    Automated snapshot refreshes use this stricter file boundary.  The
    canonical payload validator proves the schema, authority, record hashes
    and content-addressed snapshot ID, while the physical digest detects a
    different byte representation of the same logical projection.
    """

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ProductMasterError(
            f"product master projection is missing: {resolved}",
            code="product_master_projection_missing",
        )
    if require_read_only and resolved.stat().st_mode & 0o222:
        raise ProductMasterError(
            "product master projection must be mounted read-only",
            code="product_master_projection_not_read_only",
        )
    before = resolved.stat()
    before_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    projection = load_product_master_file(resolved)
    if require_complete_authority_scope:
        scope = projection.payload["scope"]
        if (
            scope["category_code"] is not None
            or scope["model_ids"]
            or scope["voltage_platform_ids"]
        ):
            raise ProductMasterError(
                "automatic refresh requires an unfiltered authority-complete "
                "product master projection",
                code="product_master_projection_not_authority_complete",
                field="scope",
            )
    after = resolved.stat()
    after_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before_sha,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after_sha,
    ):
        raise ProductMasterError(
            "product master projection changed during validation",
            code="product_master_projection_changed",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": projection.snapshot_id,
        "hash_algorithm": HASH_ALGORITHM,
        "authority": "chengmu",
        "sha256": f"sha256:{after_sha}",
        "records": len(projection.records),
    }


def load_product_master(snapshot_root: Path) -> ProductMasterProjection | None:
    path = snapshot_root / PROJECTION_FILENAME
    return load_product_master_file(path) if path.is_file() else None


def validate_task_product_master_binding(
    projection: ProductMasterProjection,
    job: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Validate one Connector task binding against the frozen projection.

    Connector persists a model-scoped Chengmu snapshot reference on every
    newly bound job.  Reporting freezes a separate complete projection.  The
    two snapshot IDs may therefore differ, but the immutable record identity
    (model ID, entity version and record hash) must agree exactly.
    """
    supplied = {field: job.get(field) for field in TASK_BINDING_FIELDS}
    populated = {
        field for field, value in supplied.items()
        if value is not None and str(value).strip() != ""
    }
    record_json = job.get("product_master_record_json")
    if not populated and (record_json is None or record_json == ""):
        return None
    if populated != set(TASK_BINDING_FIELDS):
        missing = sorted(set(TASK_BINDING_FIELDS) - populated)
        raise ProductMasterError(
            "任务产品库绑定字段不完整：" + ", ".join(missing),
            code="product_master_task_binding_invalid",
        )
    schema_version = str(supplied["product_master_schema_version"]).strip()
    if schema_version != SCHEMA_VERSION:
        raise ProductMasterError(
            "任务产品库 schema_version 与报告契约不一致",
            code="product_master_task_binding_invalid",
        )
    source_snapshot_id = _matches(
        SNAPSHOT_ID_RE,
        str(supplied["product_master_snapshot_id"]).strip(),
        "task.product_master_snapshot_id",
    )
    model_id = _matches(
        MODEL_ID_RE,
        str(supplied["product_master_model_id"]).strip(),
        "task.product_master_model_id",
    )
    entity_version = supplied["product_master_entity_version"]
    if isinstance(entity_version, bool) or not isinstance(entity_version, int) or entity_version < 1:
        raise ProductMasterError(
            "任务产品库 entity_version 非法",
            code="product_master_task_binding_invalid",
        )
    record_hash = _matches(
        RECORD_HASH_RE,
        str(supplied["product_master_record_snapshot_hash"]).strip(),
        "task.product_master_record_snapshot_hash",
    )
    projection_record = projection.records_by_model_id.get(model_id)
    if projection_record is None:
        raise ProductMasterError(
            f"model_id={model_id} 不在报告固定主数据投影",
            code="master_identity_not_found",
            field="product_master_model_id",
        )
    if (
        projection_record["entity_version"] != entity_version
        or projection_record["record_snapshot_hash"] != record_hash
    ):
        raise ProductMasterError(
            f"job_id={job.get('id')} 的产品库记录版本已漂移，请重新绑定任务并重建合格快照",
            code="product_master_binding_drift",
        )
    if record_json is not None and record_json != "":
        try:
            task_record = (
                record_json
                if isinstance(record_json, dict)
                else json.loads(str(record_json), parse_float=Decimal)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductMasterError(
                "任务 product_master_record_json 不是合法 JSON",
                code="product_master_task_binding_invalid",
            ) from exc
        task_record = _validate_record(task_record, 0)
        if (
            task_record["model_id"] != model_id
            or task_record["entity_version"] != entity_version
            or task_record["record_snapshot_hash"] != record_hash
            or task_record != projection_record
        ):
            raise ProductMasterError(
                f"job_id={job.get('id')} 的产品库记录证据与固定投影不一致",
                code="product_master_binding_drift",
            )
    audit = {
        "job_id": str(job.get("id") or ""),
        "product_master_schema_version": schema_version,
        "product_master_snapshot_id": source_snapshot_id,
        "product_master_model_id": model_id,
        "product_master_entity_version": entity_version,
        "product_master_record_snapshot_hash": record_hash,
    }
    return projection_record, audit


def resolve_voltage_platform_scope(
    projection: ProductMasterProjection | None,
    scope: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind selected jobs to immutable Chengmu model/platform references."""
    requested_platforms = sorted(set(scope.get("voltage_platform_ids") or []))
    if not requested_platforms:
        return scope
    if projection is None:
        raise ProductMasterError(
            "电压平台筛选需要 retail-product-master/v2 正式投影",
            code="product_master_unavailable",
        )
    known_platforms = {
        item["voltage_platform_id"]: item
        for item in projection.platform_catalog()
    }
    unknown = sorted(set(requested_platforms) - set(known_platforms))
    if unknown:
        raise ProductMasterError(
            "未知 voltage_platform_id：" + ", ".join(unknown),
            code="master_identity_not_found",
            field="voltage_platform_id",
        )
    requested_jobs = set(scope.get("job_ids") or [])
    selected_jobs = [job for job in jobs if str(job.get("id") or "") in requested_jobs]
    if len(selected_jobs) != len(requested_jobs):
        raise ProductMasterError("电压平台范围包含无法绑定的 job_id")
    model_refs: list[dict[str, Any]] = []
    for job in selected_jobs:
        binding = validate_task_product_master_binding(projection, job)
        if binding is None:
            raise ProductMasterError(
                f"job_id={job.get('id')} 缺少数据采集 product_master_* 不可变绑定",
                code="product_master_task_unbound",
            )
        record, binding_audit = binding
        model_id = str(record["model_id"])
        eligibility = record["analysis_eligibility"]
        if not eligibility["eligible"]:
            raise ProductMasterError(
                f"model_id={model_id} 不具备量化报告资格：{eligibility['reason']}"
            )
        category_id = str(scope.get("category_id") or "")
        if record["category"]["code"] != category_id:
            raise ProductMasterError(
                f"model_id={model_id} 主数据品类与报告 category_id 不一致"
            )
        platform = record.get("voltage_platform")
        platform_id = (
            str(platform.get("voltage_platform_id"))
            if isinstance(platform, dict)
            else ""
        )
        if platform_id not in requested_platforms:
            raise ProductMasterError(
                f"job_id={job.get('id')} 不属于所选 voltage_platform_ids"
            )
        model_refs.append(binding_audit)
    selected_platforms = [known_platforms[value] for value in requested_platforms]
    return {
        **scope,
        "voltage_platform_ids": requested_platforms,
        "voltage_platforms": selected_platforms,
        "product_master": {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": projection.snapshot_id,
            "hash_algorithm": HASH_ALGORITHM,
            "authority": "chengmu",
            "analysis_policy_version": ANALYSIS_POLICY_VERSION,
        },
        "model_refs": sorted(model_refs, key=lambda item: item["job_id"]),
    }
