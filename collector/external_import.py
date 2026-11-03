"""Fail-closed validation for user-supplied public-review exports.

The import path is intentionally separate from source collection.  It accepts
one product per file, never accepts browser state or credentials, recomputes
all VoxQuarry eligibility fields, and only publishes a complete, internally
reconciled artifact set.
"""

import copy
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .store import EXTERNAL_IMPORT_CHANNELS, canonical_target_url
from .product_form import family_category, product_form, CATEGORY_FORMS, key as form_key
from .product_metadata import (
    normalize_product_metadata,
    require_governed_metadata,
)


IMPORT_SCHEMA_VERSION = "voxquarry.review-import/v2"
LEGACY_IMPORT_SCHEMA_VERSION = "voxquarry.review-import/v1"
SUPPORTED_IMPORT_SCHEMA_VERSIONS = {
    LEGACY_IMPORT_SCHEMA_VERSION,
    IMPORT_SCHEMA_VERSION,
}
IMPORT_VALIDATION_SCHEMA_VERSION = "voxquarry.external-review-import-validation/v1"
DECLARED_INCOMPLETE_STOP_REASON = "external_import_declared_incomplete"
IMPORT_CHANNELS = EXTERNAL_IMPORT_CHANNELS
LEGACY_IMPORT_CHANNELS = {"homedepot", "amazon_live", "walmart"}
# Source metadata autofill has its own narrower adapter contract. Accepting a
# completed fallback package must not start unsupported source discovery.
METADATA_AUTOFILL_CHANNELS = LEGACY_IMPORT_CHANNELS
BROWSER_FALLBACK_HOSTS = {
    "ace": "acehardware.com",
    "tractorsupply": "tractorsupply.com",
    "harborfreight": "harborfreight.com",
    "husqvarna": "husqvarna.com",
    "stihl": "stihlusa.com",
}
IMPORT_METHODS = {
    "licensed_export",
    "platform_data_export",
    "manual_public_export",
    "approved_data_provider",
    "browser_public_capture",
}
FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "private_key",
    "session_token",
}
REQUIRED_REVIEW_FIELDS = {
    "review_id",
    "product_id",
    "source_product_id",
    "product_url",
    "submission_time",
    "title",
    "user_nickname",
    "review_text",
    "rating",
    "verified_purchaser",
    "is_recommended",
    "where_purchase",
    "age",
    "review_locale",
    "is_syndicated",
    "is_translated",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "import_id",
    "channel",
    "task_policy",
    "target_url",
    "market_region",
    "product",
    "completeness",
    "reviews",
}
LEGACY_PRODUCT_FIELDS = {"id", "title", "brand", "model", "category"}
PRODUCT_FIELDS = LEGACY_PRODUCT_FIELDS | {"energy_type", "voltage"}
OPTIONAL_PRODUCT_FIELDS = {"source_metadata_evidence", "product_form_factor", "product_form_source_category"}
METADATA_AUTOFILL_FIELDS = {"brand", "model", "category", "energy_type", "voltage", "market_region", "product_form_factor"}
SOURCE_METADATA_SCHEMA = "voxquarry.source-product-metadata/v1"
SOURCE_METADATA_EVIDENCE_FIELDS = {
    "schema_version", "channel", "source_url", "final_url", "source_sha256",
    "identity", "fields", "gaps", "battery_count", "battery_ecosystem_voltage",
    "system_voltages", "raw_voltage_fields", "autofill_fields", "original_upload_sha256",
}
COMPLETENESS_FIELDS = {
    "is_complete",
    "source_total",
    "source_total_basis",
    "collected_at",
    "method",
    "evidence_url",
    "syndication_origin_complete",
    "note",
}
DERIVED_REVIEW_FIELDS = {
    "display_site",
    "source_site",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
}
CSV_FIELDS = [
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "display_site",
    "source_site",
    "source_product_id",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "is_syndicated",
    "is_translated",
    "submission_time",
    "title",
    "is_recommended",
    "user_nickname",
    "review_text",
    "rating",
    "verified_purchaser",
    "where_purchase",
    "age",
    "review_locale",
    "market_region",
    "market_region_evidence",
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
]
REVIEW_WORD_RE = re.compile(
    r"[^\W_]+(?:['’.-][^\W_]+)*",
    re.UNICODE,
)


class ExternalImportError(ValueError):
    """The uploaded bundle does not satisfy the governed import contract."""


def external_import_format_requirements():
    """Return a machine-readable, credential-free summary of the v2 contract."""
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "file_policy": "one UTF-8 JSON file per product task; maximum 250 MiB",
        "task_match": (
            "channel + canonical target_url must match the exported stable task; "
            "no task ID is added to review rows"
        ),
        "required_top_level_fields": sorted(TOP_LEVEL_FIELDS),
        "required_product_fields": sorted(PRODUCT_FIELDS),
        "optional_product_fields": sorted(OPTIONAL_PRODUCT_FIELDS),
        "legacy_read_compatibility": LEGACY_IMPORT_SCHEMA_VERSION,
        "required_completeness_fields": sorted(COMPLETENESS_FIELDS),
        "required_review_fields": sorted(REQUIRED_REVIEW_FIELDS),
        "allowed_collection_methods": sorted(IMPORT_METHODS),
        "allowed_channels": sorted(IMPORT_CHANNELS),
        "metadata_autofill_channels": sorted(METADATA_AUTOFILL_CHANNELS),
        "allowed_market_regions": [
            "North America",
            "Europe",
            "Australia",
            "Other",
        ],
        "review_business_key": ["channel", "source_product_id", "review_id"],
        "validation_rules": [
            "complete exports require completeness.source_total to equal the number of review records",
            (
                "declared incomplete exports require is_complete=false, a non-empty "
                "review array, and a strictly larger declared source_total; they publish "
                "PARTIAL_SUCCESS with stop_reason=external_import_declared_incomplete"
            ),
            "product, URL, source product ID, channel, and market region must reconcile",
            (
                "every concrete product requires canonical category and concrete "
                "energy_type; battery products also require source-proven brand and voltage"
            ),
            "review IDs/business keys must be unique and ratings must be between 1 and 5",
            "syndication origin evidence must be complete; EGO-official syndication is excluded",
            (
                "Home Depot and Amazon brand/category must match VoxQuarry master data; "
                "Walmart also requires source-proven brand/category and exact URL item identity"
            ),
            "VoxQuarry eligibility and word-count fields are recomputed during import",
        ],
        "forbidden_input": sorted(FORBIDDEN_KEYS | DERIVED_REVIEW_FIELDS),
        "review_record_template": {
            "review_id": "REQUIRED_SOURCE_REVIEW_ID",
            "product_id": "MUST_MATCH_PRODUCT_ID",
            "source_product_id": "MUST_MATCH_PRODUCT_ID",
            "product_url": "MUST_MATCH_EXPORTED_TARGET_URL",
            "submission_time": "2026-08-10T00:00:00Z",
            "title": "",
            "user_nickname": "",
            "review_text": "",
            "rating": 5,
            "verified_purchaser": None,
            "is_recommended": None,
            "where_purchase": "",
            "age": "",
            "review_locale": "en_US",
            "is_syndicated": False,
            "is_translated": False,
            "origin_source_name": "",
            "origin_source_url": "",
            "origin_source_site": "",
        },
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _count_review_words(value):
    return len(REVIEW_WORD_RE.findall(" ".join(str(value or "").split())))


def _normalize_market_region(value):
    return {
        "north america": "North America",
        "europe": "Europe",
        "australia": "Australia",
        "other": "Other",
    }.get(str(value or "").strip().casefold(), "Other")


def _infer_market_region(url):
    host = urlsplit(str(url or "")).netloc.casefold().removeprefix("www.")
    if host.endswith((".com.au", ".au")):
        return "Australia"
    if host.endswith(
        (
            ".co.uk",
            ".uk",
            ".eu",
            ".de",
            ".fr",
            ".it",
            ".es",
            ".nl",
            ".se",
            ".pl",
        )
    ):
        return "Europe"
    if host in set(BROWSER_FALLBACK_HOSTS.values()):
        # The scoped manufacturer adapter is the US site. A .com host alone
        # must not silently label another Husqvarna market as North America.
        if host == "husqvarna.com" and not urlsplit(str(url)).path.startswith("/us/"):
            return "Other"
        return "North America"
    if host.endswith(("homedepot.com", "amazon.com", "amazon.ca", "walmart.com")):
        return "North America"
    return "Other"


def _nonempty(value, field):
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ExternalImportError(f"{field} must be non-empty")
    return cleaned


def _iso_datetime(value, field):
    cleaned = _nonempty(value, field)
    candidate = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ExternalImportError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ExternalImportError(f"{field} must include a timezone")
    return cleaned


def _submission_datetime(value, field, channel):
    """Validate source time precision without inventing a timezone.

    Walmart's governed public export exposes calendar dates for reviews rather
    than instants.  Preserve that exact precision; every other adapter keeps
    the existing timezone-aware timestamp requirement.
    """
    cleaned = _nonempty(value, field)
    if channel == "walmart" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        try:
            datetime.strptime(cleaned, "%Y-%m-%d")
        except ValueError as exc:
            raise ExternalImportError(f"{field} must be a valid ISO date") from exc
        return cleaned
    return _iso_datetime(cleaned, field)


def _walk_forbidden(value, path="bundle"):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_KEYS:
                raise ExternalImportError(
                    f"{path}.{key} is forbidden; credentials/browser state cannot be imported"
                )
            _walk_forbidden(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_forbidden(item, f"{path}[{index}]")


def _channel_host(channel, target_url):
    host = urlsplit(target_url).netloc.casefold().removeprefix("www.")
    if channel == "homedepot" and not (
        host == "homedepot.com" or host.endswith(".homedepot.com")
    ):
        raise ExternalImportError("target_url host does not match homedepot")
    if channel == "amazon_live" and not re.fullmatch(
        r"(?:www\.)?amazon\.(?:com|ca|co\.uk|de|fr|it|es|nl|se|pl|com\.au)",
        urlsplit(target_url).netloc.casefold(),
    ):
        raise ExternalImportError("target_url host does not match amazon_live")
    if channel == "walmart" and not (
        host == "walmart.com" or host.endswith(".walmart.com")
    ):
        raise ExternalImportError("target_url host does not match walmart")
    if channel in BROWSER_FALLBACK_HOSTS:
        parsed = urlsplit(target_url)
        if (host != BROWSER_FALLBACK_HOSTS[channel]
                or parsed.scheme not in {"http", "https"}
                or parsed.username is not None or parsed.password is not None):
            raise ExternalImportError(f"target_url host does not match {channel}")
        paths = {
            "ace": r"/(?:p/)?(?:[^/]+/)*\d+/?$",
            "tractorsupply": r"/tsc/product/[^/]+/?$",
            "harborfreight": r"/[^/]+-\d+\.html$",
            "husqvarna": r"/us/[^/]+/[^/]+/?$",
            "stihl": r"/(?:products/[^/]+/[^/]+/[^/]+|en/p/[^/]+-\d+)/?$",
        }
        if not re.fullmatch(paths[channel], parsed.path):
            raise ExternalImportError(f"{channel} target_url must identify an official product page")
    return host


def _walmart_product_id(target_url):
    """Return the exact public Walmart item ID carried by a product URL."""
    parsed = urlsplit(str(target_url or ""))
    match = re.search(r"/ip/(?:[^/?#]+/)?(\d+)(?:/|$)", parsed.path, re.I)
    return match.group(1) if match else ""


def _metadata_product_id(channel, target_url):
    patterns = {
        "homedepot": r"/p/(?:[^/]+/)?(\d+)(?:/|$)",
        "walmart": r"/ip/(?:[^/]+/)?(\d+)(?:/|$)",
        "amazon_live": r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)",
    }
    if channel not in patterns:
        return ""
    match = re.search(patterns[channel], urlsplit(str(target_url or "")).path, re.I)
    return match.group(1).casefold() if match else ""


def _check_metadata_evidence(payload, evidence):
    """Validate coherent declared PDP provenance, never certify uploaded claims.

    The server's source parser produces this versioned shape. Imported evidence
    remains a declaration anchored to a source digest, not an authentication
    token or a substitute for the existing governed import checks.
    """
    if not isinstance(evidence, dict):
        raise ExternalImportError("product.source_metadata_evidence must be an object")
    _walk_forbidden(evidence, "product.source_metadata_evidence")
    if set(evidence) - SOURCE_METADATA_EVIDENCE_FIELDS:
        raise ExternalImportError("source_metadata_evidence has unsupported fields")
    if evidence.get("schema_version") != SOURCE_METADATA_SCHEMA:
        raise ExternalImportError(f"source_metadata_evidence.schema_version must be {SOURCE_METADATA_SCHEMA}")
    if len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) > 32768:
        raise ExternalImportError("source_metadata_evidence exceeds 32 KiB")
    channel, target_url = payload.get("channel"), payload.get("target_url")
    if not isinstance(channel, str) or channel not in METADATA_AUTOFILL_CHANNELS or evidence.get("channel") != channel:
        raise ExternalImportError("source_metadata_evidence channel does not match import")
    source_id = _metadata_product_id(channel, target_url)
    product_id = str((payload.get("product") or {}).get("id") or "").strip().casefold()
    if not source_id or product_id != source_id:
        raise ExternalImportError("source_metadata_evidence requires exact target URL and product.id identity")
    target_host = (urlsplit(str(target_url or "")).hostname or "").casefold().removeprefix("www.")
    for key in ("source_url", "final_url"):
        url = evidence.get(key)
        if not isinstance(url, str):
            raise ExternalImportError(f"source_metadata_evidence.{key} must be HTTP(S)")
        try:
            parsed = urlsplit(url)
            unsafe = (parsed.scheme not in {"http", "https"} or parsed.username is not None
                      or parsed.password is not None or parsed.port not in {None, 80, 443})
        except ValueError as exc:
            raise ExternalImportError(f"source_metadata_evidence.{key} is invalid") from exc
        if unsafe:
            raise ExternalImportError(f"source_metadata_evidence.{key} must be credential-free HTTP(S)")
        _channel_host(channel, url)
        if ((parsed.hostname or "").casefold().removeprefix("www.") != target_host
                or _metadata_product_id(channel, url) != source_id):
            raise ExternalImportError(f"source_metadata_evidence.{key} does not match product identity")
    if canonical_target_url(evidence["source_url"]) != canonical_target_url(target_url):
        raise ExternalImportError("source_metadata_evidence.source_url does not match target_url")
    identity = evidence.get("identity")
    if (not isinstance(identity, dict)
            or set(identity) - {"method", "canonical_url", "product_id", "variant_id"}
            or not isinstance(identity.get("method"), str)
            or identity.get("method") not in {"jsonld_exact_product", "canonical_and_primary_heading"}
            or str(identity.get("product_id") or "").casefold() != source_id
            or identity.get("variant_id") is not None):
        raise ExternalImportError("source_metadata_evidence requires exact public product identity")
    canonical_url = identity.get("canonical_url")
    if canonical_url is not None:
        if not isinstance(canonical_url, str):
            raise ExternalImportError("source_metadata_evidence canonical URL must be a string")
        try:
            parsed_canonical = urlsplit(canonical_url)
            unsafe_canonical = (parsed_canonical.scheme not in {"http", "https"}
                                or parsed_canonical.username is not None or parsed_canonical.password is not None
                                or parsed_canonical.port not in {None, 80, 443})
        except ValueError as exc:
            raise ExternalImportError("source_metadata_evidence canonical URL is invalid") from exc
        if unsafe_canonical:
            raise ExternalImportError("source_metadata_evidence canonical URL must be credential-free HTTP(S)")
        _channel_host(channel, canonical_url)
        if (_metadata_product_id(channel, canonical_url) != source_id
                or (urlsplit(canonical_url).hostname or "").casefold().removeprefix("www.") != target_host):
            raise ExternalImportError("source_metadata_evidence canonical URL does not match product identity")
    for key in ("source_sha256", "original_upload_sha256"):
        if key in evidence or key == "source_sha256":
            if not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get(key) or "")):
                raise ExternalImportError(f"source_metadata_evidence.{key} must be a SHA-256 digest")
    fields = evidence.get("fields")
    if not isinstance(fields, dict) or set(fields) - METADATA_AUTOFILL_FIELDS - {"product_title"}:
        raise ExternalImportError("source_metadata_evidence.fields must contain known product fields")
    for key, claim in fields.items():
        if (not isinstance(claim, dict) or set(claim) != {"source", "raw", "value"}
                or not isinstance(claim.get("source"), str) or not claim["source"].strip()
                or not isinstance(claim.get("raw"), (str, int, float))
                or not isinstance(claim.get("value"), str) or not claim["value"].strip()):
            raise ExternalImportError(f"source_metadata_evidence.fields.{key} has an invalid source claim")
        if key == "model" and claim["value"].strip().casefold() == source_id:
            raise ExternalImportError("source product ID cannot be used as manufacturer model")
    if (not isinstance(evidence.get("gaps", []), list)
            or any(not isinstance(field, str) or field not in METADATA_AUTOFILL_FIELDS | {"product_title"}
                   for field in evidence.get("gaps", []))):
        raise ExternalImportError("source_metadata_evidence.gaps must name product fields")
    if "battery_count" in evidence and (type(evidence["battery_count"]) is not int or evidence["battery_count"] != 2):
        raise ExternalImportError("source_metadata_evidence.battery_count must describe the supported two-battery evidence")
    if "battery_ecosystem_voltage" in evidence and not isinstance(evidence["battery_ecosystem_voltage"], str):
        raise ExternalImportError("source_metadata_evidence.battery_ecosystem_voltage must be a string")
    if "system_voltages" in evidence and (not isinstance(evidence["system_voltages"], list)
            or any(not isinstance(value, str) for value in evidence["system_voltages"])):
        raise ExternalImportError("source_metadata_evidence.system_voltages must be a string list")
    if "raw_voltage_fields" in evidence:
        raw_fields = evidence["raw_voltage_fields"]
        if (not isinstance(raw_fields, list) or any(not isinstance(value, dict)
                or set(value) != {"value", "source"} or not all(isinstance(item, str) for item in value.values())
                for value in raw_fields)):
            raise ExternalImportError("source_metadata_evidence.raw_voltage_fields must contain labelled string values")
    return copy.deepcopy(evidence)


def _validated_autofill_evidence(payload):
    evidence = _check_metadata_evidence(payload, payload["product"]["source_metadata_evidence"])
    filled = evidence.get("autofill_fields")
    if (not isinstance(filled, list) or not filled or any(not isinstance(key, str) for key in filled)
            or len(filled) != len(set(filled)) or set(filled) - METADATA_AUTOFILL_FIELDS
            or set(evidence["fields"]) != set(filled)):
        raise ExternalImportError("source_metadata_evidence.autofill_fields must exactly match its field claims")
    for key in filled:
        value = payload.get(key) if key == "market_region" else payload["product"].get(key)
        claim = evidence["fields"][key]["value"]
        # A normalized manifest retains the original category claim verbatim.
        # Admit only its exact, separately recorded source-category mapping.
        mapped_category = (key == "category"
                           and payload["product"].get("product_form_source_category") == claim
                           and value == family_category(claim))
        if value != claim and not mapped_category:
            raise ExternalImportError(f"source_metadata_evidence.{key} conflicts with imported product field")
    return evidence


def enrich_import_bundle_metadata(payload, source):
    """Fill missing v2 product fields from one exact parsed PDP, without I/O.

    Reviews and existing user fields are preserved. Only fields actually filled
    are retained as source claims. The caller can freeze the returned bundle and
    its original upload digest; later validation never revisits the live PDP.
    """
    enriched = copy.deepcopy(payload)
    if (not isinstance(payload, dict) or payload.get("schema_version") != IMPORT_SCHEMA_VERSION
            or not isinstance(payload.get("channel"), str) or payload.get("channel") not in METADATA_AUTOFILL_CHANNELS):
        return enriched
    if not isinstance(payload.get("product"), dict):
        raise ExternalImportError("product must be an object")
    if "source_metadata_evidence" in payload["product"]:
        # A previously frozen import must not silently combine claims from a
        # newer page into the old page's single digest.
        _validated_autofill_evidence(payload)
        return enriched
    if not source:
        return enriched
    if not isinstance(source, dict):
        raise ExternalImportError("source metadata must be an object")
    error = source.get("source_metadata_error")
    if error:
        if isinstance(error, dict) and error.get("code") in {"source_identity_mismatch", "source_metadata_conflict"}:
            raise ExternalImportError("source metadata does not uniquely match the imported product")
        return enriched
    evidence = _check_metadata_evidence(payload, source.get("source_metadata_evidence"))
    for field, claim in evidence["fields"].items():
        if source.get(field) != claim["value"]:
            raise ExternalImportError(f"source metadata {field} conflicts with its source evidence")
    filled = []
    for field in sorted(METADATA_AUTOFILL_FIELDS):
        destination = enriched if field == "market_region" else enriched["product"]
        existing = destination.get(field)
        claim = evidence["fields"].get(field)
        if (existing is None or isinstance(existing, str) and not existing.strip()) and claim:
            destination[field] = claim["value"]
            filled.append(field)
    if filled:
        evidence["fields"] = {field: evidence["fields"][field] for field in filled}
        evidence["autofill_fields"] = filled
        enriched["product"]["source_metadata_evidence"] = evidence
        _validated_autofill_evidence(enriched)
    return enriched


def _is_ego_origin(name, url, site):
    evidence = " ".join((str(name or ""), str(url or ""), str(site or "")))
    return bool(
        re.search(r"(?:^|[./\s-])egopowerplus\.(?:com|co\.uk)(?:$|[/\s])", evidence, re.I)
        or re.search(r"\bego(?:\s*power\s*\+?)?\b", str(name or ""), re.I)
    )


def validate_import_bundle(payload):
    """Validate and normalize one external review bundle.

    All review records are retained in ``source_rows``.  The 5-word rule is
    recomputed as an analytical flag rather than used as an import filter.
    Only the deterministic EGO-official syndication exclusion is omitted from
    ``eligible_rows``.
    """
    if not isinstance(payload, dict):
        raise ExternalImportError("bundle must be a JSON object")
    _walk_forbidden(payload)
    extra_top = sorted(set(payload) - TOP_LEVEL_FIELDS)
    if extra_top:
        raise ExternalImportError(
            f"bundle has unsupported field {extra_top[0]}"
        )
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_IMPORT_SCHEMA_VERSIONS:
        raise ExternalImportError(
            f"schema_version must be {IMPORT_SCHEMA_VERSION}; {LEGACY_IMPORT_SCHEMA_VERSION} is read-compatible"
        )
    import_id = _nonempty(payload.get("import_id"), "import_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}", import_id):
        raise ExternalImportError("import_id has an invalid format")
    channel = _nonempty(payload.get("channel"), "channel")
    if channel not in IMPORT_CHANNELS:
        raise ExternalImportError(
            "channel is not supported for governed external import; Amazon Reviews 2023 uses its archive pipeline"
        )
    if schema_version == LEGACY_IMPORT_SCHEMA_VERSION and channel not in LEGACY_IMPORT_CHANNELS:
        raise ExternalImportError("browser fallback channels require voxquarry.review-import/v2")
    if payload.get("task_policy") != "create_or_append_attempt":
        raise ExternalImportError(
            "task_policy must be create_or_append_attempt"
        )
    target_url = _nonempty(payload.get("target_url"), "target_url")
    display_site = _channel_host(channel, target_url)
    target_key = canonical_target_url(target_url)
    market_region = _normalize_market_region(payload.get("market_region"))
    if market_region == "Other" or market_region != payload.get("market_region"):
        raise ExternalImportError(
            "market_region must be one of North America, Europe, or Australia for these imports"
        )
    inferred_region = _infer_market_region(target_url)
    if market_region != inferred_region:
        raise ExternalImportError(
            f"market_region {market_region} conflicts with target_url evidence {inferred_region}"
        )

    product = payload.get("product")
    if not isinstance(product, dict):
        raise ExternalImportError("product must be an object")
    product_fields = (
        PRODUCT_FIELDS | OPTIONAL_PRODUCT_FIELDS
        if schema_version == IMPORT_SCHEMA_VERSION
        else LEGACY_PRODUCT_FIELDS
    )
    extra_product = sorted(set(product) - product_fields)
    if extra_product:
        raise ExternalImportError(
            f"product has unsupported field {extra_product[0]}"
        )
    product_id = _nonempty(product.get("id"), "product.id")
    if channel == "walmart":
        url_product_id = _walmart_product_id(target_url)
        if not url_product_id:
            raise ExternalImportError(
                "walmart target_url must expose an exact numeric /ip/ product identity"
            )
        if product_id != url_product_id:
            raise ExternalImportError(
                "walmart product.id does not match target_url item identity"
            )
    # Legacy Walmart v1 files remain readable with their historical optional
    # metadata semantics. Every v2 file is governed uniformly across channels.
    metadata_optional = (
        channel == "walmart" and schema_version == LEGACY_IMPORT_SCHEMA_VERSION
    )
    source_title = _nonempty(product.get("title"), "product.title")
    metadata_evidence = (
        _validated_autofill_evidence(payload)
        if schema_version == IMPORT_SCHEMA_VERSION and "source_metadata_evidence" in product
        else None
    )
    source_brand = (
        str(product.get("brand") or "").strip()
        if metadata_optional
        else _nonempty(product.get("brand"), "product.brand")
    )
    source_model = str(product.get("model") or "").strip()
    if schema_version == LEGACY_IMPORT_SCHEMA_VERSION and not metadata_optional:
        source_model = _nonempty(product.get("model"), "product.model")
    source_category = (
        str(product.get("category") or "").strip()
        if metadata_optional
        else _nonempty(product.get("category"), "product.category")
    )
    declared_form = product.get("product_form_factor")
    form_category = product.get("product_form_source_category") or source_category
    if family_category(form_category) != family_category(source_category):
        raise ExternalImportError("product_form_source_category conflicts with product category")
    category_form = CATEGORY_FORMS.get(form_key(source_category))
    source_form = CATEGORY_FORMS.get(form_key(form_category))
    if category_form and source_form and category_form != source_form:
        raise ExternalImportError("product_form_source_category conflicts with explicit product category")
    form_claim = (metadata_evidence or {}).get("fields", {}).get("product_form_factor")
    if declared_form not in {None, "", "unknown"} and not form_claim and not CATEGORY_FORMS.get(form_key(form_category)):
        raise ExternalImportError("product_form_factor requires exact source metadata evidence or an explicit legacy category")
    form_evidence = "source_product_page" if form_claim else "explicit_source_category" if declared_form not in {None, "", "unknown"} else "unspecified"
    try:
        task_product_metadata = normalize_product_metadata(
            target_url=target_url,
            brand=source_brand,
            model=source_model,
            category=source_category,
            market_region=market_region,
            energy_type=product.get("energy_type"),
            voltage=product.get("voltage"),
            product_title=source_title,
            product_form_factor=declared_form,
            product_form_evidence_source=form_evidence,
            product_form_source_category=form_category,
        )
    except ValueError as exc:
        raise ExternalImportError(str(exc)) from exc
    if schema_version == IMPORT_SCHEMA_VERSION:
        try:
            require_governed_metadata(task_product_metadata)
        except ValueError as exc:
            raise ExternalImportError(str(exc)) from exc
    if metadata_evidence:
        for field in metadata_evidence["autofill_fields"]:
            claim_value = metadata_evidence["fields"][field]["value"]
            if field == "category":
                claim_value = family_category(claim_value)
            if task_product_metadata.get(field) != claim_value:
                raise ExternalImportError(f"source metadata {field} conflicts with normalized import metadata")
        task_product_metadata["source_metadata_evidence"] = copy.deepcopy(metadata_evidence)
    artifact_brand = (
        task_product_metadata["brand"] or ""
        if schema_version == IMPORT_SCHEMA_VERSION
        else source_brand
    )
    artifact_model = (
        task_product_metadata["model"] or ""
        if schema_version == IMPORT_SCHEMA_VERSION
        else source_model
    )
    artifact_category = (
        task_product_metadata["category"]
        if schema_version == IMPORT_SCHEMA_VERSION
        else source_category
    )
    normalized_product = {
        "id": product_id,
        "title": source_title,
        "url": target_url,
        "brand": artifact_brand,
        "model": artifact_model,
        "category": artifact_category,
        "source_site": display_site,
        "market_region": market_region,
        "collection_method": "governed_external_import",
    }
    if schema_version == IMPORT_SCHEMA_VERSION:
        normalized_product.update({
            "energy_type": task_product_metadata["energy_type"],
            "voltage": task_product_metadata["voltage"],
            "product_form_factor": task_product_metadata["product_form_factor"],
            "product_form_source_category": task_product_metadata["product_form"]["source_category"],
            "product_form": task_product_metadata["product_form"],
        })
    if metadata_evidence:
        normalized_product["source_metadata_evidence"] = copy.deepcopy(metadata_evidence)

    completeness = payload.get("completeness")
    if not isinstance(completeness, dict):
        raise ExternalImportError("completeness must be an object")
    extra_completeness = sorted(set(completeness) - COMPLETENESS_FIELDS)
    if extra_completeness:
        raise ExternalImportError(
            f"completeness has unsupported field {extra_completeness[0]}"
        )
    note = completeness.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise ExternalImportError("completeness.note must be a string")
        if len(note.encode("utf-8")) > 4096:
            raise ExternalImportError("completeness.note must not exceed 4096 UTF-8 bytes")
    is_complete = completeness.get("is_complete")
    if not isinstance(is_complete, bool):
        raise ExternalImportError("completeness.is_complete must be true or false")
    if completeness.get("source_total_basis") != "user_review_records":
        raise ExternalImportError(
            "completeness.source_total_basis must be user_review_records"
        )
    if completeness.get("syndication_origin_complete") is not True:
        raise ExternalImportError(
            "completeness.syndication_origin_complete must be true"
        )
    method = _nonempty(completeness.get("method"), "completeness.method")
    if method not in IMPORT_METHODS:
        raise ExternalImportError(
            "completeness.method is not an approved import evidence method"
        )
    if method == "browser_public_capture" and schema_version != IMPORT_SCHEMA_VERSION:
        raise ExternalImportError("browser_public_capture requires voxquarry.review-import/v2")
    _iso_datetime(completeness.get("collected_at"), "completeness.collected_at")
    evidence_url = _nonempty(
        completeness.get("evidence_url"), "completeness.evidence_url"
    )
    if urlsplit(evidence_url).scheme not in {"http", "https"}:
        raise ExternalImportError("completeness.evidence_url must be HTTP(S)")
    if channel == "walmart" or channel in BROWSER_FALLBACK_HOSTS or method == "browser_public_capture":
        _channel_host(channel, evidence_url)
        if canonical_target_url(evidence_url) != target_key:
            raise ExternalImportError(
                f"{channel} completeness.evidence_url must match target_url"
            )
    source_total = completeness.get("source_total")
    if isinstance(source_total, bool) or not isinstance(source_total, int) or source_total <= 0:
        raise ExternalImportError("completeness.source_total must be a positive integer")

    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ExternalImportError("reviews must be a non-empty array")
    source_records_collected = len(reviews)
    if source_total < source_records_collected:
        raise ExternalImportError(
            f"source_total {source_total} cannot be smaller than reviews length {source_records_collected}"
        )
    if is_complete and source_total != source_records_collected:
        raise ExternalImportError(
            f"complete source_total {source_total} does not equal reviews length {source_records_collected}"
        )
    if not is_complete and source_total <= source_records_collected:
        raise ExternalImportError(
            "incomplete source_total must exceed reviews length with a positive provable gap"
        )
    source_missing_records = source_total - source_records_collected
    publication_status = "completed" if is_complete else "partial_success"
    stop_reason = "" if is_complete else DECLARED_INCOMPLETE_STOP_REASON

    source_rows = []
    identities = set()
    for index, raw in enumerate(reviews):
        field = f"reviews[{index}]"
        if not isinstance(raw, dict):
            raise ExternalImportError(f"{field} must be an object")
        missing = sorted(REQUIRED_REVIEW_FIELDS - set(raw))
        if missing:
            raise ExternalImportError(f"{field} missing required field {missing[0]}")
        conflicting = sorted(DERIVED_REVIEW_FIELDS & set(raw))
        if conflicting:
            raise ExternalImportError(
                f"{field}.{conflicting[0]} is derived by VoxQuarry and must not be supplied"
            )
        unsupported = sorted(
            set(raw) - REQUIRED_REVIEW_FIELDS - DERIVED_REVIEW_FIELDS
        )
        if unsupported:
            raise ExternalImportError(
                f"{field} has unsupported field {unsupported[0]}"
            )
        review_id = _nonempty(raw.get("review_id"), f"{field}.review_id")
        product_id = _nonempty(raw.get("product_id"), f"{field}.product_id")
        if product_id != normalized_product["id"]:
            raise ExternalImportError(
                f"{field}.product_id does not match product.id"
            )
        row_url = _nonempty(raw.get("product_url"), f"{field}.product_url")
        if canonical_target_url(row_url) != target_key:
            raise ExternalImportError(
                f"{field}.product_url does not match target_url"
            )
        source_product_id = _nonempty(
            raw.get("source_product_id"), f"{field}.source_product_id"
        )
        _submission_datetime(
            raw.get("submission_time"), f"{field}.submission_time", channel
        )
        locale = _nonempty(raw.get("review_locale"), f"{field}.review_locale")
        rating = raw.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not 1 <= float(rating) <= 5:
            raise ExternalImportError(f"{field}.rating must be numeric from 1 to 5")
        for boolean_name in (
            "verified_purchaser",
            "is_recommended",
            "is_syndicated",
            "is_translated",
        ):
            if raw.get(boolean_name) is not None and not isinstance(
                raw.get(boolean_name), bool
            ):
                raise ExternalImportError(
                    f"{field}.{boolean_name} must be true, false, or null"
                )
        origin_name = str(raw.get("origin_source_name") or "").strip()
        origin_url = str(raw.get("origin_source_url") or "").strip()
        origin_site = str(raw.get("origin_source_site") or "").strip().casefold()
        if raw.get("is_syndicated") is True and not origin_site:
            raise ExternalImportError(
                f"{field}.origin_source_site is required for syndicated reviews"
            )
        source_site = origin_site or display_site
        identity = (source_site, source_product_id, review_id)
        if identity in identities:
            raise ExternalImportError(
                f"duplicate review business key at {field}"
            )
        identities.add(identity)
        review_text = str(raw.get("review_text") or "")
        word_count = _count_review_words(review_text)
        excluded_ego = channel != "ego" and _is_ego_origin(
            origin_name, origin_url, origin_site
        )
        row = {
            "channel": channel,
            "product_id": product_id,
            "product_title": normalized_product["title"],
            "product_url": target_url,
            "review_id": review_id,
            "display_site": display_site,
            "source_site": source_site,
            "source_product_id": source_product_id,
            "origin_source_name": origin_name,
            "origin_source_url": origin_url,
            "origin_source_site": origin_site,
            "is_syndicated": raw.get("is_syndicated"),
            "is_translated": raw.get("is_translated"),
            "submission_time": raw.get("submission_time"),
            "title": str(raw.get("title") or ""),
            "is_recommended": raw.get("is_recommended"),
            "user_nickname": str(raw.get("user_nickname") or ""),
            "review_text": review_text,
            "rating": rating,
            "verified_purchaser": raw.get("verified_purchaser"),
            "where_purchase": str(raw.get("where_purchase") or ""),
            "age": str(raw.get("age") or ""),
            "review_locale": locale,
            "market_region": market_region,
            "market_region_evidence": "source_market",
            "review_word_count": word_count,
            "is_valid_text_review": bool(review_text.strip()),
            "is_eligible": not excluded_ego,
            "exclusion_reason": (
                "syndicated_from_ego_official" if excluded_ego else ""
            ),
            "raw": raw,
        }
        source_rows.append(row)

    return {
        "schema_version": schema_version,
        "import_id": import_id,
        "channel": channel,
        "task_policy": "create_or_append_attempt",
        "target_url": target_url,
        "target_key": target_key,
        "market_region": market_region,
        "product": normalized_product,
        "task_product_metadata": task_product_metadata,
        "completeness": dict(completeness),
        "source_total": source_total,
        "source_records_collected": source_records_collected,
        "source_missing_records": source_missing_records,
        "publication_status": publication_status,
        "stop_reason": stop_reason,
        "source_rows": source_rows,
        "eligible_rows": [row for row in source_rows if row["is_eligible"]],
    }


def _csv_projection(rows):
    return [
        {
            key: "" if row.get(key) is None else str(row.get(key, ""))
            for key in CSV_FIELDS
        }
        for row in rows
    ]


def build_import_validation_report(validated, *, completed_at=None):
    """Build the canonical validation projection for one validated import."""
    source_rows = validated["source_rows"]
    eligible_rows = validated["eligible_rows"]
    exclusions = Counter(
        row["exclusion_reason"] for row in source_rows if not row["is_eligible"]
    )
    ratings = Counter(str(row["rating"]) for row in eligible_rows)
    channel = validated["channel"]
    is_complete = validated["publication_status"] == "completed"
    report = {
        "validation_schema_version": IMPORT_VALIDATION_SCHEMA_VERSION,
        "channel": channel,
        "status": "COMPLETE" if is_complete else "PARTIAL_SUCCESS",
        "product": validated["product"],
        "source_total": validated["source_total"],
        "source_records_collected": validated["source_records_collected"],
        "source_missing_records": validated["source_missing_records"],
        "source_unique_reviews": len(source_rows),
        "source_reconciled": is_complete,
        "unique_reviews": len(eligible_rows),
        "eligible_reviews": len(eligible_rows),
        "excluded_reviews": len(source_rows) - len(eligible_rows),
        "excluded_ego_origin_reviews": exclusions.get(
            "syndicated_from_ego_official", 0
        ),
        "exclusion_reason_histogram": dict(exclusions),
        "valid_text_reviews": sum(
            bool(row["is_valid_text_review"]) for row in eligible_rows
        ),
        "short_text_reviews": sum(
            0 < int(row["review_word_count"]) < 5 for row in eligible_rows
        ),
        "market_region": validated["market_region"],
        "duplicates_removed": 0,
        "rating_histogram": dict(ratings),
        "ratings_only": sum(
            not str(row["review_text"]).strip() for row in eligible_rows
        ),
        "stop_reason": validated["stop_reason"],
        "notes": {
            "method": "governed external review import",
            "import_schema_version": validated["schema_version"],
            "import_id": validated["import_id"],
            "source_total_basis": "user_review_records",
            "source_evidence_method": validated["completeness"]["method"],
            "source_evidence_url": validated["completeness"]["evidence_url"],
            "source_collected_at": validated["completeness"]["collected_at"],
            "syndication_origin_complete": True,
            "source_note": validated["completeness"].get("note"),
            "nonempty_text_is_valid_text_review": True,
            "partial_publication_requires_positive_declared_gap": True,
        },
        "artifacts": {
            "csv": f"{channel}/reviews.csv",
            "raw_jsonl": f"{channel}/reviews.raw.jsonl",
            "report": f"{channel}/validation_report.json",
        },
        "completed_at": completed_at or _now(),
    }
    return report


def build_import_selection_manifest(validated):
    """Build the canonical selection-manifest projection for one import."""
    return {
        "schema_version": validated["schema_version"],
        "import_id": validated["import_id"],
        "channel": validated["channel"],
        "task_policy": validated["task_policy"],
        "target_url": validated["target_url"],
        "target_key": validated["target_key"],
        "market_region": validated["market_region"],
        "product": validated["product"],
        "completeness": validated["completeness"],
        "review_count": len(validated["source_rows"]),
        "source_records_collected": validated["source_records_collected"],
        "source_missing_records": validated["source_missing_records"],
        "publication_status": validated["publication_status"],
        "stop_reason": validated["stop_reason"],
    }


def _read_artifact_object(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalImportError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ExternalImportError(f"{label} must be a JSON object")
    return value


def verify_import_artifacts(run_dir, *, allow_legacy_declared_incomplete=False):
    """Rebuild and compare every governed import artifact before publication.

    This deliberately derives the canonical import from manifest plus raw JSONL
    and then compares all output projections.  It therefore does not trust a
    CSV, validation report, or manifest merely because its aggregate counts
    happen to agree.
    """
    run_dir = Path(run_dir)
    required = {
        "reviews.csv",
        "reviews.raw.jsonl",
        "validation_report.json",
        "selection_manifest.json",
    }
    missing = sorted(name for name in required if not (run_dir / name).is_file())
    if missing:
        raise ExternalImportError("import artifacts are incomplete: " + ", ".join(missing))
    report = _read_artifact_object(run_dir / "validation_report.json", "validation report")
    manifest = _read_artifact_object(run_dir / "selection_manifest.json", "selection manifest")
    raw_rows = []
    try:
        with (run_dir / "reviews.raw.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    raise ExternalImportError("raw JSONL contains an empty line")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ExternalImportError("raw JSONL contains a non-object record")
                raw_rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalImportError("raw JSONL is unreadable") from exc
    try:
        payload = {
            key: manifest[key]
            for key in TOP_LEVEL_FIELDS
            if key != "reviews"
        }
    except KeyError as exc:
        raise ExternalImportError(f"selection manifest missing {exc.args[0]}") from exc
    product = manifest.get("product")
    if not isinstance(product, dict):
        raise ExternalImportError("selection manifest product is invalid")
    try:
        manifest_schema = manifest.get("schema_version")
        product_fields = (
            PRODUCT_FIELDS
            if manifest_schema == IMPORT_SCHEMA_VERSION
            else LEGACY_PRODUCT_FIELDS
        )
        payload["product"] = {key: product[key] for key in product_fields}
        if manifest_schema == IMPORT_SCHEMA_VERSION:
            payload['product'].update({key: product[key] for key in OPTIONAL_PRODUCT_FIELDS if key in product})
    except KeyError as exc:
        raise ExternalImportError(
            f"selection manifest product missing {exc.args[0]}"
        ) from exc
    payload["reviews"] = raw_rows
    validated = validate_import_bundle(payload)
    expected_manifest = build_import_selection_manifest(validated)
    completed_at = _iso_datetime(report.get("completed_at"), "validation.completed_at")
    expected_report = build_import_validation_report(
        validated,
        completed_at=completed_at,
    )
    form_fields = {"product_form_factor", "product_form_source_category", "product_form"}
    if manifest_schema == IMPORT_SCHEMA_VERSION and not (set(product) & form_fields):
        historical_product = report.get("product") or {}
        if set(historical_product) & form_fields:
            raise ExternalImportError("historical form fields disagree between artifacts")
        for projection in (expected_manifest["product"], expected_report["product"]):
            for field in form_fields:
                projection.pop(field, None)
            if family_category(product["category"]) == projection["category"]:
                projection["category"] = product["category"]
    # Early Home Depot v1 publications predate two additive validation-report
    # fields.  The manifest and raw evidence still rebuild the full governed
    # projection, so permit only those exact historical omissions at export
    # time; all present values and every business field remain byte-equivalent.
    if (
        allow_legacy_declared_incomplete
        and manifest_schema == LEGACY_IMPORT_SCHEMA_VERSION
    ):
        for field in (
            "source_records_collected",
            "source_missing_records",
            "publication_status",
            "stop_reason",
        ):
            if field not in manifest:
                expected_manifest.pop(field)
        if "validation_schema_version" not in report:
            expected_report.pop("validation_schema_version")
        for field in (
            "source_records_collected",
            "source_missing_records",
            "stop_reason",
        ):
            if field not in report:
                expected_report.pop(field)
        report_notes = report.get("notes")
        expected_notes = expected_report.get("notes")
        if (
            isinstance(report_notes, dict)
            and isinstance(expected_notes, dict)
            and "source_note" not in report_notes
            and expected_notes.get("source_note") is None
        ):
            expected_notes.pop("source_note")
        legacy_five_word_rule = (
            isinstance(report_notes, dict)
            and report_notes.get(
                "five_word_rule_is_analytical_flag_not_import_filter"
            )
            is True
        )
        if legacy_five_word_rule and isinstance(expected_notes, dict):
            expected_notes.pop("nonempty_text_is_valid_text_review", None)
            expected_notes.pop(
                "partial_publication_requires_positive_declared_gap", None
            )
            expected_notes[
                "five_word_rule_is_analytical_flag_not_import_filter"
            ] = True
            expected_report["valid_text_reviews"] = sum(
                int(row["review_word_count"]) >= 5
                for row in validated["eligible_rows"]
            )
    else:
        legacy_five_word_rule = False
    legacy_reason = "external_export_incomplete"
    if (
        allow_legacy_declared_incomplete
        and report.get("stop_reason") == legacy_reason
        and manifest.get("stop_reason") == legacy_reason
    ):
        expected_report.pop("validation_schema_version", None)
        expected_report["stop_reason"] = legacy_reason
        expected_manifest["stop_reason"] = legacy_reason
    if manifest != expected_manifest:
        raise ExternalImportError("selection manifest does not match raw import evidence")
    if report != expected_report:
        raise ExternalImportError("validation report does not match raw import evidence")
    try:
        with (run_dir / "reviews.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != CSV_FIELDS:
                raise ExternalImportError("CSV fields do not match the import contract")
            csv_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ExternalImportError("CSV is unreadable") from exc
    expected_csv_rows = _csv_projection(validated["eligible_rows"])
    if legacy_five_word_rule:
        for projected, row in zip(expected_csv_rows, validated["eligible_rows"]):
            projected["is_valid_text_review"] = str(
                int(row["review_word_count"]) >= 5
            )
    if csv_rows != expected_csv_rows:
        raise ExternalImportError("CSV does not match raw import evidence")
    return {
        "validated": validated,
        "report": report,
        "manifest": manifest,
        "source_total": validated["source_total"],
        "source_records_collected": validated["source_records_collected"],
        "source_missing_records": validated["source_missing_records"],
        "eligible_reviews": len(validated["eligible_rows"]),
        "raw_records": len(validated["source_rows"]),
        "csv_records": len(validated["eligible_rows"]),
    }


def write_import_artifacts(run_dir, validated):
    """Write one fully validated bundle in the standard connector layout."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    source_rows = validated["source_rows"]
    eligible_rows = validated["eligible_rows"]
    with (run_dir / "reviews.raw.jsonl").open("w", encoding="utf-8") as handle:
        for row in source_rows:
            handle.write(json.dumps(row["raw"], ensure_ascii=False) + "\n")
    with (run_dir / "reviews.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_projection(eligible_rows))
    report = build_import_validation_report(validated)
    (run_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "selection_manifest.json").write_text(
        json.dumps(
            build_import_selection_manifest(validated),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report
