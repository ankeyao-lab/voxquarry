"""Build a read-only, content-addressed task-energy evidence proposal.

The builder has deliberately no database argument and no apply mode.  It binds
the exact incomplete subset of one governed Pressure Washer audit to official
or separately approved evidence payloads.  Operations must independently
verify the resulting artifact before any later, separately authorized apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .product_identity import (
    TASK_ENERGY_SCHEMA_VERSION,
    build_product_observation,
    build_task_energy,
    build_voltage_platform,
    normalize_voltage,
    validate_task_energy,
)


AUDIT_SCHEMA_VERSION = "voxquarry.pressure-washer-energy-governance-readonly-audit/v2"
EVIDENCE_SCHEMA_VERSION = "voxquarry.task-energy-authority-evidence/v3"
ATTESTATION_SCHEMA_VERSION = "voxquarry.task-energy-authority-attestation/v2"
PROPOSAL_SCHEMA_VERSION = "voxquarry.task-energy-evidence-proposal/v3"
RECEIPT_SCHEMA_VERSION = "voxquarry.task-energy-evidence-proposal-receipt/v3"
PROPOSAL_RULE_VERSION = "exact-allowlist-product-scoped-authority-context/3.0.0"

AUDIT_KEYS = {
    "candidate_commit", "counts", "decisions", "generated_at_utc", "policy",
    "production_write_count", "schema_version", "source_environment",
}
DECISION_KEYS = {
    "brand", "category", "channel", "collection_method", "conflicts",
    "evidence_hash", "exclusion_reason", "explicit_markers",
    "governance_readiness", "job_id", "model", "observed_voltage_values",
    "proposed_energy_type", "proposed_voltage", "selected_product_id",
    "selected_title_sha256", "selection_manifest_bytes",
    "selection_manifest_sha256", "source_site", "target_identity_sha256",
}
EVIDENCE_KEYS = {"production_write_count", "records", "schema_version"}
RECORD_KEYS = {
    "authority_attestation",
    "as_of_utc", "authority_evidence_type", "authority_payload_bytes",
    "authority_payload_path", "authority_payload_sha256",
    "authority_source_url", "baseline_evidence_hash", "captured_at_utc",
    "channel", "decision", "excluded_reason", "job_id",
    "proposed_energy_type", "proposed_voltage", "selected_product_id",
    "selected_title_sha256", "selection_manifest_sha256",
    "target_identity_sha256",
}
ATTESTATION_KEYS = {
    "approval", "canonical_voltage", "channel", "energy_type", "product_context",
    "job_id", "payload_sha256", "schema_version", "selected_product_id",
    "source_url", "target_identity_sha256",
}
APPROVAL_KEYS = {"content_sha256", "issuer", "pointer", "signature_sha256"}
PRODUCT_CONTEXT_KEYS = {
    "energy_observed", "identity_field", "identity_value", "kind",
    "source_url_field", "source_url_value", "specification_field",
    "specification_sha256", "voltage_observed",
}
PRODUCT_CONTEXT_KINDS = {
    "html_head_product/v1": {
        "identity_field": "canonical_url_product_id",
        "source_url_field": "canonical_url",
        "specification_field": "meta_description",
    },
    "json_ld_product/v1": {
        "identity_field": "sku",
        "source_url_field": "@id",
        "specification_field": "description",
    },
    "bv_catalog_product/v1": {
        "identity_field": "productId",
        "source_url_field": "productPageURL",
        "specification_field": "productDescription",
    },
}
SHA_FIELDS = {
    "baseline_evidence_hash", "authority_payload_sha256",
    "selected_title_sha256", "selection_manifest_sha256",
    "target_identity_sha256",
}
ALLOWED_EVIDENCE_TYPES = {
    "official_product_spec", "approved_authority_record", "identity_unproven",
    "access_blocked", "authority_conflict", "authority_not_available",
}
PROPOSAL_EVIDENCE_TYPES = {"official_product_spec", "approved_authority_record"}
EXCLUDED_EVIDENCE_TYPES = {
    "identity_unproven", "access_blocked", "authority_conflict",
    "authority_not_available",
}
EXCLUDED_REASONS = {
    "official_identity_not_proven", "official_access_blocked",
    "authority_energy_conflict", "authority_voltage_conflict",
    "authority_energy_not_available", "authority_voltage_not_available",
}
CHANNEL_HOSTS = {
    "walmart": ("walmart.com",),
    "ego": ("egopowerplus.com", "egopowerplus.co.uk"),
    "einhell": ("einhell.com", "einhell.co.uk", "einhell.de", "einhell.fr", "einhell.it"),
    "homedepot": ("homedepot.com",),
    "lowes": ("lowes.com",),
    "tractorsupply": ("tractorsupply.com",),
}

# Empty by design.  A future approved-authority source must be registered in a
# reviewed code change with its issuer and pointer boundary.  Merely labelling
# caller input as approved_authority_record can never elevate it to evidence.
APPROVED_AUTHORITY_ISSUERS = {}
ENERGY_FACT_TOKENS = {
    "battery": {"battery", "batteries", "batterie"},
    "corded": {"corded", "corded electric", "mains electric"},
    "fuel": {"fuel", "gas", "gasoline", "petrol"},
    "other": {"other"},
}


@dataclass(frozen=True)
class AuditBinding:
    audit_sha256: str
    expected_scope_jobs: int
    expected_ready: int
    expected_incomplete: int
    expected_energy_required: int
    expected_voltage_required: int


PRODUCTION_AUDIT_BINDING = AuditBinding(
    audit_sha256="644b9a9f1ceebe797fc6efce170c69363c3baf6d9173884748d1981d3c3c032e",
    expected_scope_jobs=260,
    expected_ready=225,
    expected_incomplete=35,
    expected_energy_required=18,
    expected_voltage_required=17,
)


class ProposalError(RuntimeError):
    """The proposal input is incomplete, inconsistent, or outside governance."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_json_bytes(path: Path, raw: bytes):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProposalError(f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalError(f"unreadable proposal input: {path}") from exc


def _load_json(path: Path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProposalError(f"unreadable proposal input: {path}") from exc
    return _load_json_bytes(path, raw)


def _expect_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ProposalError(f"{label} fields do not match the governed contract")


def _expect_sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value):
        raise ProposalError(f"{label} must be a lowercase sha256 value")


def _canonical_sha(value):
    _expect_sha(value, "sha256")
    return value if value.startswith("sha256:") else "sha256:" + value


def _expect_time(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProposalError(f"{label} must be an explicit UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProposalError(f"{label} is invalid") from exc


def _safe_regular_file(root: Path, relative: str):
    if not isinstance(relative, str) or not relative or relative.startswith("."):
        raise ProposalError("authority payload path is invalid")
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1:
        raise ProposalError("authority payload path must be one plain file name")
    path = root / rel
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProposalError(f"authority payload is missing: {relative}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ProposalError(f"authority payload must be a regular non-symlink file: {relative}")
    return path


def _official_host(url, channel, evidence_type):
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProposalError("authority source URL must use credential-free HTTPS")
    host = parsed.hostname.casefold()
    if host.startswith("www."):
        host = host[4:]
    if evidence_type == "official_product_spec":
        allowed = CHANNEL_HOSTS.get(channel)
        if not allowed or not any(host == suffix or host.endswith("." + suffix) for suffix in allowed):
            raise ProposalError("official evidence host does not match the task channel")
    return host


def _payload_text(payload: Path):
    try:
        return payload.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProposalError("authority payload must be valid UTF-8 public evidence") from exc


def _normalized_product_url(value):
    parsed = urlparse(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProposalError("product context source URL must use credential-free HTTPS")
    host = parsed.hostname.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


class _ProductHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_title = False
        self.title = []
        self.canonical_urls = []
        self.descriptions = []
        self.script_type = None
        self.script_chunks = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        values = {str(key).casefold(): value for key, value in attrs}
        tag = tag.casefold()
        if tag == "head":
            self.in_head = True
        elif self.in_head and tag == "title":
            self.in_title = True
        elif self.in_head and tag == "link" and str(values.get("rel") or "").casefold() == "canonical":
            if values.get("href"):
                self.canonical_urls.append(values["href"])
        elif self.in_head and tag == "meta":
            name = str(values.get("name") or "").casefold()
            if name == "canonical" and values.get("content"):
                self.canonical_urls.append(values["content"])
            elif name == "description" and values.get("content"):
                self.descriptions.append(values["content"])
        elif tag == "script":
            self.script_type = str(values.get("type") or "").casefold()
            self.script_chunks = []

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "title":
            self.in_title = False
        elif tag == "head":
            self.in_head = False
        elif tag == "script" and self.script_type is not None:
            self.scripts.append((self.script_type, "".join(self.script_chunks)))
            self.script_type = None
            self.script_chunks = []

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.script_type is not None:
            self.script_chunks.append(data)


def _walk_json_products(value):
    if isinstance(value, dict):
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(item).casefold() == "product" for item in kinds):
            yield value
        for child in value.values():
            yield from _walk_json_products(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_products(child)


def _balanced_objects(array_text):
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(array_text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ProposalError("Bazaarvoice product context is malformed")
            if depth == 0 and start is not None:
                yield array_text[start:index + 1]
                start = None
        elif char == "]" and depth == 0:
            return
    if depth != 0 or in_string:
        raise ProposalError("Bazaarvoice product context is malformed")


def _extract_product_contexts(payload_text, kind):
    parser = _ProductHTMLParser()
    try:
        parser.feed(payload_text)
        parser.close()
    except Exception as exc:
        raise ProposalError("official product page HTML is malformed") from exc
    contexts = []
    if kind == "html_head_product/v1":
        descriptions = list(dict.fromkeys(item.strip() for item in parser.descriptions if item.strip()))
        canonicals = list(dict.fromkeys(item.strip() for item in parser.canonical_urls if item.strip()))
        for canonical in canonicals:
            for description in descriptions:
                contexts.append({
                    "identity": canonical,
                    "source_url": canonical,
                    "specification": description,
                })
    elif kind == "json_ld_product/v1":
        for script_type, script_text in parser.scripts:
            if script_type != "application/ld+json" or not script_text.strip():
                continue
            try:
                document = json.loads(script_text)
            except json.JSONDecodeError:
                continue
            for product in _walk_json_products(document):
                if all(isinstance(product.get(field), str) for field in ("sku", "@id", "description")):
                    contexts.append({
                        "identity": product["sku"],
                        "source_url": product["@id"],
                        "specification": product["description"],
                    })
    elif kind == "bv_catalog_product/v1":
        for _, script_text in parser.scripts:
            match = re.search(r"\bcatalogProducts\s*:\s*\[", script_text)
            if not match:
                continue
            for object_text in _balanced_objects(script_text[match.end():]):
                try:
                    product = json.loads(re.sub(r",\s*([}\]])", r"\1", object_text))
                except json.JSONDecodeError:
                    continue
                if all(isinstance(product.get(field), str) for field in (
                    "productId", "productPageURL", "productDescription",
                )):
                    contexts.append({
                        "identity": product["productId"],
                        "source_url": product["productPageURL"],
                        "specification": product["productDescription"],
                    })
    else:
        raise ProposalError("product context kind is not governed")
    return contexts


def _verify_product_context(payload_text, context, record):
    _expect_keys(context, PRODUCT_CONTEXT_KEYS, "authority product context")
    kind = context.get("kind")
    contract = PRODUCT_CONTEXT_KINDS.get(kind)
    if not contract:
        raise ProposalError("product context kind is not governed")
    for field in ("identity_field", "source_url_field", "specification_field"):
        if context.get(field) != contract[field]:
            raise ProposalError(f"product context {field} is not governed for {kind}")
    _expect_sha(context.get("specification_sha256"), "product context specification_sha256")
    selected = str(record["selected_product_id"])
    source_url = _normalized_product_url(record["authority_source_url"])
    matches = []
    for extracted in _extract_product_contexts(payload_text, kind):
        identity_matches = (
            selected.casefold() in _normalized_product_url(extracted["identity"]).casefold()
            if kind == "html_head_product/v1"
            else extracted["identity"].casefold() == selected.casefold()
        )
        try:
            url_matches = _normalized_product_url(extracted["source_url"]) == source_url
        except ProposalError:
            url_matches = False
        if identity_matches and url_matches:
            matches.append(extracted)
    if len(matches) != 1:
        raise ProposalError("official payload must expose exactly one matching product-scoped context")
    extracted = matches[0]
    if context.get("identity_value") != extracted["identity"]:
        raise ProposalError("product context identity value does not match parsed official context")
    if _normalized_product_url(context.get("source_url_value")) != source_url:
        raise ProposalError("product context source URL does not match parsed official context")
    specification = extracted["specification"]
    if _canonical_sha(context["specification_sha256"]) != _sha_bytes(specification.encode("utf-8")):
        raise ProposalError("product context specification hash does not match parsed official context")
    energy_observed = context.get("energy_observed")
    voltage_observed = context.get("voltage_observed")
    if not isinstance(energy_observed, str) or not energy_observed.strip():
        raise ProposalError("product context energy observation is invalid")
    if energy_observed not in specification:
        raise ProposalError("energy observation is outside the matching product specification context")
    if energy_observed.casefold() not in ENERGY_FACT_TOKENS.get(record["proposed_energy_type"], set()):
        raise ProposalError("product context energy does not match the proposed energy type")
    if record["proposed_energy_type"] == "battery":
        if not isinstance(voltage_observed, str) or voltage_observed not in specification:
            raise ProposalError("voltage observation is outside the matching product specification context")
        if normalize_voltage(voltage_observed) != normalize_voltage(record["proposed_voltage"]):
            raise ProposalError("product context voltage does not match the proposed voltage")
    elif voltage_observed is not None:
        raise ProposalError("non-battery product context cannot carry voltage")
    return {
        "energy_observed": energy_observed,
        "identity_field": context["identity_field"],
        "identity_value": extracted["identity"],
        "kind": kind,
        "source_url_field": context["source_url_field"],
        "source_url_value": extracted["source_url"],
        "specification_field": context["specification_field"],
        "specification_sha256": _sha_bytes(specification.encode("utf-8")),
        "voltage_observed": voltage_observed,
    }


def _verify_authority_attestation(record, payload: Path):
    attestation = record.get("authority_attestation")
    _expect_keys(attestation, ATTESTATION_KEYS, "authority attestation")
    if attestation.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        raise ProposalError("authority attestation schema is not governed")
    pairs = {
        "job_id": (attestation.get("job_id"), record.get("job_id")),
        "channel": (attestation.get("channel"), record.get("channel")),
        "selected_product_id": (
            attestation.get("selected_product_id"), record.get("selected_product_id")
        ),
        "target_identity_sha256": (
            attestation.get("target_identity_sha256"), record.get("target_identity_sha256")
        ),
        "payload_sha256": (
            attestation.get("payload_sha256"), record.get("authority_payload_sha256")
        ),
        "source_url": (attestation.get("source_url"), record.get("authority_source_url")),
        "energy_type": (
            attestation.get("energy_type"), record.get("proposed_energy_type")
        ),
        "canonical_voltage": (
            attestation.get("canonical_voltage"), record.get("proposed_voltage")
        ),
    }
    for field, (actual, expected) in pairs.items():
        if field.endswith("sha256"):
            _expect_sha(actual, f"authority attestation.{field}")
            equal = _canonical_sha(actual) == _canonical_sha(expected)
        else:
            equal = actual == expected
        if not equal:
            raise ProposalError(f"authority attestation {field} does not match its record")

    evidence_type = record["authority_evidence_type"]
    approval = attestation.get("approval")
    if evidence_type == "official_product_spec":
        if approval is not None:
            raise ProposalError("official product spec cannot self-declare an approval")
    elif evidence_type == "approved_authority_record":
        _expect_keys(approval, APPROVAL_KEYS, "authority approval")
        issuer = approval.get("issuer")
        pointer = approval.get("pointer")
        if APPROVED_AUTHORITY_ISSUERS.get(issuer) != pointer:
            raise ProposalError("authority record issuer/pointer is not approved")
        for field in ("content_sha256", "signature_sha256"):
            _expect_sha(approval.get(field), f"authority approval.{field}")
        if _canonical_sha(approval["content_sha256"]) != _canonical_sha(record["authority_payload_sha256"]):
            raise ProposalError("authority approval content SHA does not match the payload")
    else:
        raise ProposalError("positive attestation is only valid for positive evidence")

    text = _payload_text(payload)
    verified_context = _verify_product_context(text, attestation.get("product_context"), record)
    return {
        "approval": approval,
        "canonical_voltage": attestation["canonical_voltage"],
        "channel": attestation["channel"],
        "energy_type": attestation["energy_type"],
        "job_id": attestation["job_id"],
        "payload_sha256": _canonical_sha(attestation["payload_sha256"]),
        "product_context": verified_context,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "selected_product_id": attestation["selected_product_id"],
        "source_url": attestation["source_url"],
        "target_identity_sha256": _canonical_sha(attestation["target_identity_sha256"]),
    }


def _validate_audit(path: Path, binding: AuditBinding):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProposalError("energy audit is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != binding.audit_sha256:
        raise ProposalError("energy audit SHA does not match the approved binding")
    audit = _load_json_bytes(path, raw)
    _expect_keys(audit, AUDIT_KEYS, "energy audit")
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise ProposalError("energy audit schema is not governed")
    if audit.get("production_write_count") != 0:
        raise ProposalError("energy audit is not read-only")
    decisions = audit.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != binding.expected_scope_jobs:
        raise ProposalError("energy audit scope count does not match binding")
    seen = set()
    incomplete = []
    reasons = {"energy_type_required": 0, "battery_voltage_required": 0}
    for index, item in enumerate(decisions):
        _expect_keys(item, DECISION_KEYS, f"energy decision[{index}]")
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ProposalError("energy audit job IDs must be nonempty and unique")
        seen.add(job_id)
        for field in (
            "evidence_hash", "selected_title_sha256", "selection_manifest_sha256",
            "target_identity_sha256",
        ):
            _expect_sha(item.get(field), f"energy decision[{index}].{field}")
        readiness = item.get("governance_readiness")
        reason = item.get("exclusion_reason")
        if readiness == "metadata_incomplete":
            if reason not in reasons:
                raise ProposalError("metadata-incomplete energy reason is not governed")
            reasons[reason] += 1
            incomplete.append(item)
        elif readiness != "ready" or reason is not None:
            raise ProposalError("energy audit readiness semantics are invalid")
    if len(incomplete) != binding.expected_incomplete:
        raise ProposalError("energy audit incomplete count does not match binding")
    if len(decisions) - len(incomplete) != binding.expected_ready:
        raise ProposalError("energy audit ready count does not match binding")
    if reasons != {
        "energy_type_required": binding.expected_energy_required,
        "battery_voltage_required": binding.expected_voltage_required,
    }:
        raise ProposalError("energy audit exclusion counts do not match binding")
    return audit, sorted(incomplete, key=lambda item: item["job_id"])


def _audited_task_energy(item):
    energy_type = item["proposed_energy_type"]
    return {
        "schema_version": TASK_ENERGY_SCHEMA_VERSION,
        "energy_type": energy_type,
        "readiness_status": item["governance_readiness"],
        "evidence_source": f"{AUDIT_SCHEMA_VERSION}:{_canonical_sha(item['evidence_hash'])}",
        "canonical_voltage": item["proposed_voltage"],
        "unknown_reason": item["exclusion_reason"],
    }


def _validate_evidence(path: Path, payload_root: Path, incomplete):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProposalError("authority evidence is unreadable") from exc
    evidence = _load_json_bytes(path, raw)
    _expect_keys(evidence, EVIDENCE_KEYS, "authority evidence")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ProposalError("authority evidence schema is not governed")
    if evidence.get("production_write_count") != 0:
        raise ProposalError("authority evidence must declare zero production writes")
    records = evidence.get("records")
    if not isinstance(records, list):
        raise ProposalError("authority evidence records must be an array")
    baseline = {item["job_id"]: item for item in incomplete}
    record_ids = [record.get("job_id") for record in records if isinstance(record, dict)]
    if len(record_ids) != len(baseline) or len(set(record_ids)) != len(record_ids) or set(record_ids) != set(baseline):
        raise ProposalError("authority evidence must cover the exact allowlist once")
    output = []
    payload_files = {}
    for index, record in enumerate(sorted(records, key=lambda item: item.get("job_id", ""))):
        _expect_keys(record, RECORD_KEYS, f"authority record[{index}]")
        base = baseline[record["job_id"]]
        for field in SHA_FIELDS:
            _expect_sha(record.get(field), f"authority record[{index}].{field}")
        identity_pairs = {
            "channel": (record.get("channel"), base.get("channel")),
            "selected_product_id": (record.get("selected_product_id"), base.get("selected_product_id")),
            "baseline_evidence_hash": (record.get("baseline_evidence_hash"), base.get("evidence_hash")),
            "selected_title_sha256": (record.get("selected_title_sha256"), base.get("selected_title_sha256")),
            "selection_manifest_sha256": (record.get("selection_manifest_sha256"), base.get("selection_manifest_sha256")),
            "target_identity_sha256": (record.get("target_identity_sha256"), base.get("target_identity_sha256")),
        }
        if any(
            (
                _canonical_sha(actual) != _canonical_sha(expected)
                if key.endswith("hash") or key.endswith("sha256")
                else actual != expected
            )
            for key, (actual, expected) in identity_pairs.items()
        ):
            raise ProposalError(f"authority identity does not match audit for {record['job_id']}")
        evidence_type = record.get("authority_evidence_type")
        if evidence_type not in ALLOWED_EVIDENCE_TYPES:
            raise ProposalError("authority evidence type is not governed")
        _official_host(record.get("authority_source_url"), record["channel"], evidence_type)
        _expect_time(record.get("captured_at_utc"), "captured_at_utc")
        _expect_time(record.get("as_of_utc"), "as_of_utc")
        payload = _safe_regular_file(payload_root, record.get("authority_payload_path"))
        if payload.stat().st_size != record.get("authority_payload_bytes") or _sha_file(payload) != record.get("authority_payload_sha256"):
            raise ProposalError(f"authority payload hash/size mismatch: {payload.name}")
        if payload.name in payload_files:
            raise ProposalError("authority payload paths must be unique")
        payload_files[payload.name] = {
            "bytes": payload.stat().st_size,
            "sha256": record["authority_payload_sha256"],
        }
        action = record.get("decision")
        if action == "proposed":
            if evidence_type not in PROPOSAL_EVIDENCE_TYPES or record.get("excluded_reason") is not None:
                raise ProposalError("proposed energy requires positive authority evidence")
            verified_attestation = _verify_authority_attestation(record, payload)
            energy_type = record.get("proposed_energy_type")
            voltage = record.get("proposed_voltage")
            observation = build_product_observation(
                brand=base.get("brand"), model=base.get("model"),
                category=base.get("category"), energy_type=energy_type,
                voltage=voltage, voltage_raw=voltage,
                evidence_source=f"{EVIDENCE_SCHEMA_VERSION}:{record['authority_payload_sha256']}",
            )
            platform = build_voltage_platform(observation)
            new_energy = validate_task_energy(build_task_energy(observation, platform))
            if new_energy["energy_type"] == "unknown" or new_energy["readiness_status"] != "ready":
                raise ProposalError("proposed energy evidence does not produce a ready fact")
            if base["exclusion_reason"] == "battery_voltage_required":
                if new_energy["energy_type"] != "battery" or not normalize_voltage(voltage):
                    raise ProposalError("battery voltage evidence must preserve battery identity")
            if new_energy["energy_type"] == "battery" and platform.get("status") != "known":
                raise ProposalError("battery proposal requires same-product brand and voltage platform evidence")
            proposed_platform = platform
        elif action == "excluded_unknown":
            if evidence_type not in EXCLUDED_EVIDENCE_TYPES:
                raise ProposalError("excluded decision requires negative authority evidence")
            if record.get("excluded_reason") not in EXCLUDED_REASONS:
                raise ProposalError("excluded reason is not governed")
            if record.get("proposed_energy_type") is not None or record.get("proposed_voltage") is not None:
                raise ProposalError("excluded decision cannot propose energy metadata")
            if record.get("authority_attestation") is not None:
                raise ProposalError("excluded decision cannot carry a positive authority attestation")
            new_energy = None
            proposed_platform = None
            verified_attestation = None
        else:
            raise ProposalError("authority decision must be proposed or excluded_unknown")
        output.append({
            "apply_status": action,
            "as_of_utc": record["as_of_utc"],
            "audited_task_energy": _audited_task_energy(base),
            "authority": {
                "attestation": verified_attestation,
                "captured_at_utc": record["captured_at_utc"],
                "evidence_type": evidence_type,
                "payload_bytes": record["authority_payload_bytes"],
                "payload_path": f"authority-payloads/{payload.name}",
                "payload_sha256": _canonical_sha(record["authority_payload_sha256"]),
                "source_url": record["authority_source_url"],
            },
            "baseline": {
                "brand": base["brand"], "category": base["category"],
                "channel": base["channel"], "evidence_hash": _canonical_sha(base["evidence_hash"]),
                "job_id": base["job_id"], "model": base["model"],
                "selected_product_id": base["selected_product_id"],
                "selected_title_sha256": _canonical_sha(base["selected_title_sha256"]),
                "selection_manifest_sha256": _canonical_sha(base["selection_manifest_sha256"]),
                "target_identity_sha256": _canonical_sha(base["target_identity_sha256"]),
            },
            "excluded_reason": record["excluded_reason"],
            "new_task_energy": new_energy,
            "new_voltage_platform": proposed_platform,
        })
    return output, payload_files, _sha_bytes(raw)


def _write_private(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def build_proposal(
    audit_path, evidence_path, payload_root, output_root,
    *, binding=PRODUCTION_AUDIT_BINDING,
):
    audit_path = Path(audit_path)
    evidence_path = Path(evidence_path)
    payload_root = Path(payload_root)
    output_root = Path(output_root)
    audit, incomplete = _validate_audit(audit_path, binding)
    records, payload_descriptors, evidence_sha256 = _validate_evidence(
        evidence_path, payload_root, incomplete
    )
    counts = {
        "excluded_unknown": sum(item["apply_status"] == "excluded_unknown" for item in records),
        "proposed": sum(item["apply_status"] == "proposed" for item in records),
        "total": len(records),
    }
    proposal = {
        "audit": {
            "generated_at_utc": audit["generated_at_utc"],
            "schema_version": AUDIT_SCHEMA_VERSION,
            "sha256": "sha256:" + binding.audit_sha256,
        },
        "counts": counts,
        "policy": {
            "apply_included": False,
            "category_mutation_allowed": False,
            "exact_allowlist": True,
            "operations_review_required": True,
            "production_write_count": 0,
            "rule_version": PROPOSAL_RULE_VERSION,
            "title_only_inference_allowed": False,
        },
        "records": records,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
    }
    proposal_bytes = _canonical(proposal).encode("utf-8")
    logical = {
        "audit_sha256": "sha256:" + binding.audit_sha256,
        "authority_evidence_sha256": evidence_sha256,
        "generator_sha256": _sha_file(Path(__file__)),
        "payload_files": payload_descriptors,
        "proposal_sha256": _sha_bytes(proposal_bytes),
        "schema_version": PROPOSAL_SCHEMA_VERSION,
    }
    digest = hashlib.sha256(_canonical(logical).encode("utf-8")).hexdigest()
    artifact_id = f"task-energy-evidence-proposal-v3-{digest}"
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    final = output_root / artifact_id
    if final.exists():
        raise ProposalError("content-addressed proposal already exists")
    staging = Path(tempfile.mkdtemp(prefix=".task-energy-proposal-", dir=output_root))
    os.chmod(staging, 0o700)
    try:
        payload_dest = staging / "authority-payloads"
        payload_dest.mkdir(mode=0o700)
        for name, descriptor in payload_descriptors.items():
            source = _safe_regular_file(payload_root, name)
            destination = payload_dest / name
            _write_private(destination, source.read_bytes())
            if destination.stat().st_size != descriptor["bytes"] or _sha_file(destination) != descriptor["sha256"]:
                raise ProposalError("copied authority payload failed verification")
        _write_private(staging / "authority-evidence.json", evidence_path.read_bytes())
        if _sha_file(staging / "authority-evidence.json") != logical["authority_evidence_sha256"]:
            raise ProposalError("copied authority evidence failed verification")
        _write_private(staging / "proposal.json", proposal_bytes)
        receipt = {
            "artifact_id": artifact_id,
            "counts": counts,
            "logical": logical,
            "production_write_count": 0,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "proposal_ready_for_operations_review",
        }
        receipt_bytes = _canonical(receipt).encode("utf-8")
        _write_private(staging / "receipt.json", receipt_bytes)
        sums = {
            "authority-evidence.json": logical["authority_evidence_sha256"],
            "proposal.json": _sha_bytes(proposal_bytes),
            "receipt.json": _sha_bytes(receipt_bytes),
            **{f"authority-payloads/{name}": item["sha256"] for name, item in payload_descriptors.items()},
        }
        sums_bytes = "".join(
            f"{value.removeprefix('sha256:')}  {name}\n" for name, value in sorted(sums.items())
        ).encode("utf-8")
        _write_private(staging / "SHA256SUMS", sums_bytes)
        # Recheck the complete private staging tree immediately before publish.
        actual = {
            str(path.relative_to(staging)) for path in staging.rglob("*") if path.is_file()
        }
        if actual != set(sums) | {"SHA256SUMS"}:
            raise ProposalError("proposal staging tree is not closed")
        for name, expected in sums.items():
            if _sha_file(staging / name) != expected:
                raise ProposalError("proposal staging payload drifted")
        os.replace(staging, final)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return {
        "artifact_id": artifact_id,
        "artifact_path": str(final),
        "counts": counts,
        "production_write_count": 0,
    }
