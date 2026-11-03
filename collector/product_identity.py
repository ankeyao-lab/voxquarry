import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation


PRODUCT_OBSERVATION_SCHEMA_VERSION = "voxquarry.product-observation/v1"
VOLTAGE_PLATFORM_SCHEMA_VERSION = "voxquarry.voltage-platform/v1"
TASK_ENERGY_SCHEMA_VERSION = "voxquarry.task-energy/v1"
TASK_ENERGY_TYPES = {"corded", "battery", "fuel", "other", "unknown"}


def _clean_optional(value):
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if not cleaned or cleaned.casefold() in {
        "unknown", "unknown category", "待补充", "待确认", "未知",
    }:
        return None
    return cleaned


def normalize_voltage(value):
    """Normalize explicit source voltage without inferring platform equivalence."""
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    parts = re.split(r"\s*/\s*", cleaned)
    if len(parts) > 2:
        return None
    normalized_parts = []
    for part in parts:
        match = re.fullmatch(
            r"(?i)\s*(\d+(?:\.\d+)?)\s*(?:v|volt|volts)\s*", part,
        )
        if not match:
            return None
        try:
            number = Decimal(match.group(1))
        except InvalidOperation:
            return None
        if not number.is_finite() or number <= 0 or number > 1000:
            return None
        normalized = format(number.normalize(), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        value = f"{normalized}V"
        if value not in normalized_parts:
            normalized_parts.append(value)
    return "/".join(normalized_parts)


def extract_voltage(*values):
    """Return one explicit voltage only; conflicting observations remain unknown."""
    observed = []
    for value in values:
        normalized_value = normalize_voltage(value)
        if normalized_value:
            if normalized_value not in observed:
                observed.append(normalized_value)
            continue
        for match in re.finditer(
            r"(?i)(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*[- ]?\s*(?:v|volt|volts)\b",
            str(value or ""),
        ):
            normalized = normalize_voltage(f"{match.group(1)}V")
            if normalized and normalized not in observed:
                observed.append(normalized)
    if len(observed) == 1:
        return observed[0]
    return None


def normalize_energy_type(value):
    """Normalize governed energy codes without guessing from voltage alone."""
    token = re.sub(
        r"[^a-z0-9]+", "_", str(value or "").casefold()
    ).strip("_")
    if token in {"", "unknown", "unbound", "pending", "未知"}:
        return "unknown"
    aliases = {
        "battery": "battery",
        "battery_powered": "battery",
        "cordless": "battery",
        "cordless_electric": "battery",
        "dc": "battery",
        "corded": "corded",
        "corded_electric": "corded",
        "ac": "corded",
        "fuel": "fuel",
        "gas": "fuel",
        "gasoline": "fuel",
        "petrol": "fuel",
        "diesel": "fuel",
        "other": "other",
        "manual": "other",
        "pneumatic": "other",
    }
    if token not in aliases:
        raise ValueError(
            "energy_type must be corded, battery, fuel, other, or unknown"
        )
    return aliases[token]


def build_product_observation(
    *, brand=None, model=None, category=None, voltage=None,
    evidence_source="request", voltage_raw=None, energy_type=None,
):
    raw_voltage = _clean_optional(voltage_raw if voltage_raw is not None else voltage)
    return {
        "schema_version": PRODUCT_OBSERVATION_SCHEMA_VERSION,
        "brand": _clean_optional(brand),
        "model": _clean_optional(model),
        "category": _clean_optional(category),
        "energy_type": _clean_optional(energy_type),
        "voltage_raw": raw_voltage,
        "normalized_voltage": normalize_voltage(voltage),
        "evidence_source": _clean_optional(evidence_source) or "unknown",
    }


def normalize_brand_token(value):
    """Return the governed Wotu brand token used in platform business keys."""
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    normalized = unicodedata.normalize("NFKC", cleaned).casefold()
    pieces = []
    pending_separator = False
    for char in normalized:
        if char.isalnum():
            if pending_separator and pieces:
                pieces.append("-")
            pieces.append(char)
            pending_separator = False
        else:
            pending_separator = True
    return "".join(pieces).strip("-") or None


def _is_explicit_non_dc(value):
    try:
        # ``other`` is intentionally not synonymous with non-DC. Hybrid
        # products can expose an AC operating mode while still belonging to a
        # governed battery-voltage ecosystem (for example Greenworks 60V).
        return normalize_energy_type(value) in {"corded", "fuel"}
    except ValueError:
        return False


def build_voltage_platform(observation, authority_binding=None):
    observation = dict(observation or {})
    brand = _clean_optional(observation.get("brand"))
    voltage = normalize_voltage(observation.get("normalized_voltage"))
    brand_token = normalize_brand_token(brand)
    authority_record = (
        authority_binding.get("record")
        if isinstance(authority_binding, dict)
        and isinstance(authority_binding.get("record"), dict)
        else None
    )
    authority_platform = (
        authority_record.get("voltage_platform")
        if isinstance(authority_record, dict)
        and isinstance(authority_record.get("voltage_platform"), dict)
        else None
    )
    authority_voltage = normalize_voltage(
        (authority_platform or {}).get("canonical_voltage")
    )
    observed_energy_type = normalize_energy_type(observation.get("energy_type"))
    authority_energy_type = (
        normalize_energy_type(authority_record.get("energy_type"))
        if authority_record
        else "unknown"
    )
    effective_energy_type = (
        authority_energy_type
        if observed_energy_type == "unknown" and authority_energy_type != "unknown"
        else observed_energy_type
    )
    evidence_source = _clean_optional(observation.get("evidence_source"))
    null_reason = None
    status = "known"
    quality_flags = []
    if _is_explicit_non_dc(effective_energy_type):
        status = "non_dc"
        null_reason = "not_dc"
        voltage = None
    elif not brand_token:
        status = "unknown"
        null_reason = "brand_unknown"
    elif not voltage:
        status = "unknown"
        null_reason = "voltage_unknown"
    elif authority_voltage and authority_voltage != voltage:
        # Source observation remains the platform fact. The authority mismatch
        # is preserved for governance rather than silently overriding it.
        quality_flags.append("authority_voltage_conflict")
    if status == "known":
        platform_key = f"{brand_token}|{voltage}"
        display_name = f"{brand} {voltage}"
    elif status == "non_dc":
        platform_key = None
        display_name = "非 DC"
    else:
        platform_key = None
        display_name = None
    return {
        "schema_version": VOLTAGE_PLATFORM_SCHEMA_VERSION,
        "status": status,
        "platform_key": platform_key,
        "brand": brand,
        "normalized_voltage": voltage,
        "display_name": display_name,
        "evidence_source": evidence_source,
        "null_reason": null_reason,
        "authority_calibrated": bool(
            status == "known" and authority_voltage == voltage
        ),
        "quality_flags": quality_flags,
    }


def build_task_energy(observation, voltage_platform=None):
    """Build the single task-level energy fact and collection readiness gate.

    Missing metadata never blocks task creation.  It does prevent a newly
    governed scrape task from being claimed until the energy fact is complete.
    Legacy tasks with NULL contract columns remain executable by Store policy.
    """
    observation = dict(observation or {})
    platform = dict(voltage_platform or {})
    energy_type = normalize_energy_type(observation.get("energy_type"))
    evidence_source = _clean_optional(observation.get("evidence_source")) or "unknown"
    canonical_voltage = None
    readiness_status = "ready"
    unknown_reason = None
    if energy_type == "unknown":
        readiness_status = "metadata_incomplete"
        unknown_reason = "energy_type_required"
    elif energy_type in {"battery", "other"}:
        # A voltage is canonical only when it belongs to a source-proven
        # product platform. This preserves main's stricter battery rule while
        # allowing hybrid ``other`` products such as Greenworks 60V.
        if platform.get("status") == "known":
            canonical_voltage = normalize_voltage(platform.get("normalized_voltage"))
        if energy_type == "battery" and not canonical_voltage:
            readiness_status = "metadata_incomplete"
            unknown_reason = "battery_voltage_required"
    return {
        "schema_version": TASK_ENERGY_SCHEMA_VERSION,
        "energy_type": energy_type,
        "readiness_status": readiness_status,
        "evidence_source": evidence_source,
        "canonical_voltage": canonical_voltage,
        "unknown_reason": unknown_reason,
    }


def validate_task_energy(value):
    energy = dict(value or {})
    required = {
        "schema_version", "energy_type", "readiness_status",
        "evidence_source", "canonical_voltage", "unknown_reason",
    }
    if set(energy) != required:
        raise ValueError("task energy contract fields are incomplete")
    if energy.get("schema_version") != TASK_ENERGY_SCHEMA_VERSION:
        raise ValueError("unsupported task energy schema")
    energy_type = normalize_energy_type(energy.get("energy_type"))
    if energy_type != energy.get("energy_type"):
        raise ValueError("task energy type is not canonical")
    expected_status = "ready"
    expected_reason = None
    expected_voltage = None
    if energy_type == "unknown":
        expected_status = "metadata_incomplete"
        expected_reason = "energy_type_required"
    elif energy_type in {"battery", "other"}:
        expected_voltage = normalize_voltage(energy.get("canonical_voltage"))
        if energy_type == "battery" and not expected_voltage:
            expected_status = "metadata_incomplete"
            expected_reason = "battery_voltage_required"
    if energy.get("readiness_status") != expected_status:
        raise ValueError("task energy readiness does not match metadata")
    if energy.get("unknown_reason") != expected_reason:
        raise ValueError("task energy unknown reason does not match metadata")
    if energy.get("canonical_voltage") != expected_voltage:
        raise ValueError("task energy voltage does not match energy type")
    if not _clean_optional(energy.get("evidence_source")):
        raise ValueError("task energy evidence source is required")
    return energy


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observation_hash(observation):
    return "sha256:" + hashlib.sha256(
        canonical_json(observation).encode("utf-8")
    ).hexdigest()
