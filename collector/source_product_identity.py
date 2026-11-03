"""Versioned, auditable source-product identity discovery state.

Task creation never trusts an operator-supplied source identifier.  Identifiers
that are structurally part of an official canonical URL can be recorded at
preview/create time; all others stay pending until the channel worker reads an
official PDP or public source API.
"""

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit


SOURCE_PRODUCT_IDENTITY_SCHEMA_VERSION = "voxquarry.source-product-identity/v1"
SOURCE_PRODUCT_IDENTITY_STATUSES = {"resolved", "pending", "unavailable"}


def _canonical_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_source_product_identity(value):
    if not isinstance(value, dict):
        raise ValueError("source product identity must be an object")
    expected = {
        "schema_version", "status", "source_product_id",
        "evidence_source", "evidence_detail",
    }
    if set(value) != expected:
        raise ValueError("source product identity fields do not match v1")
    if value.get("schema_version") != SOURCE_PRODUCT_IDENTITY_SCHEMA_VERSION:
        raise ValueError("source product identity schema version is unsupported")
    status = value.get("status")
    product_id = value.get("source_product_id")
    if status not in SOURCE_PRODUCT_IDENTITY_STATUSES:
        raise ValueError("source product identity status is invalid")
    if status == "resolved":
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValueError("resolved source product identity requires an id")
    elif product_id is not None:
        raise ValueError("unresolved source product identity cannot contain an id")
    if not isinstance(value.get("evidence_source"), str) or not value["evidence_source"]:
        raise ValueError("source product identity evidence source is required")
    if not isinstance(value.get("evidence_detail"), str) or not value["evidence_detail"]:
        raise ValueError("source product identity evidence detail is required")
    return dict(value)


def source_product_identity_hash(value):
    return _canonical_hash(validate_source_product_identity(value))


def pending_source_product_identity():
    return {
        "schema_version": SOURCE_PRODUCT_IDENTITY_SCHEMA_VERSION,
        "status": "pending",
        "source_product_id": None,
        "evidence_source": "worker_official_product_page",
        "evidence_detail": "source_product_id_not_deterministic_from_url",
    }


def resolved_source_product_identity(product_id, evidence_source, evidence_detail):
    return validate_source_product_identity({
        "schema_version": SOURCE_PRODUCT_IDENTITY_SCHEMA_VERSION,
        "status": "resolved",
        "source_product_id": str(product_id).strip(),
        "evidence_source": str(evidence_source),
        "evidence_detail": str(evidence_detail),
    })


def source_product_identity_from_url(channel, url):
    """Return only identifiers proved by an official canonical URL shape."""
    parts = [unquote(item) for item in urlsplit(str(url or "")).path.split("/") if item]
    product_id = None
    detail = None
    if channel == "einhell":
        match = re.search(r"/p/(\d{6,15})(?:[-/]|$)", urlsplit(str(url)).path, re.I)
        if match:
            product_id = match.group(1)
            detail = "einhell_official_pdp_item_number"
    elif channel == "milwaukee":
        folded = [item.casefold() for item in parts]
        if "details" in folded and folded.index("details") + 2 == len(parts) - 1:
            candidate = parts[-1]
            if re.fullmatch(
                r"(?:\d{2}-\d{2}-\d{4}|\d{4}-[A-Za-z0-9]{1,4}|"
                r"(?=[A-Za-z0-9-]*\d)[A-Za-z]{2,}[A-Za-z0-9]*-[A-Za-z0-9]+)",
                candidate,
            ):
                product_id = candidate.upper()
                detail = "milwaukee_official_pdp_model"
    elif channel == "dewalt":
        candidate = next(
            (item for item in parts if re.fullmatch(r"DC[A-Z0-9-]{4,}", item, re.I)),
            None,
        )
        if candidate:
            product_id = candidate.upper()
            detail = "dewalt_official_pdp_model"
    elif channel == "amazon_reviews_2023":
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)", str(url), re.I)
        if match:
            product_id = match.group(1).upper()
            detail = "amazon_parent_asin"
    elif channel == "homedepot":
        match = re.search(r"/(\d{8,})(?:[/?#]|$)", str(url))
        if match:
            product_id = match.group(1)
            detail = "home_depot_item_id"
    if product_id:
        return resolved_source_product_identity(
            product_id, "canonical_product_url", detail,
        )
    return pending_source_product_identity()
