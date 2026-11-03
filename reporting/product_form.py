"""Versioned product form facts; never classify a product from review prose."""
from __future__ import annotations

import unicodedata
from typing import Any

FORM_SCHEMA_VERSION = "voxquarry.product-form/v1"
FORM_LABELS = {
    "handheld": "手持式", "backpack": "背负式", "wheeled": "轮式",
    "stick": "杆式", "upright": "直立式", "robot": "机器人式",
    "unknown": "形态未明确",
}
# Only explicit product-family labels are evidence. Fan and Misting Fan have
# separate identities and deliberately do not inherit any of these forms.
CATEGORY_FORMS = {
    "handheld sprayer": "handheld", "backpack sprayer": "backpack",
    "handheld spreader": "handheld", "handheld vacuum": "handheld",
    "cordless handheld vacuum": "handheld", "backpack vacuum": "backpack",
    "cordless stick vacuum": "stick", "stick vacuum": "stick",
    "upright vacuum": "upright", "robot vacuum": "robot",
    "wheeled blower": "wheeled",
}
FORM_CATEGORIES = {
    "sprayer", "spreader", "cordless_handheld_vacuum", "backpack_vacuum",
    "cordless_stick_vacuum", "upright_vacuum", "robot_vacuum", "leaf_blower",
}


def _key(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def product_form_fact(job: dict[str, Any]) -> dict[str, Any]:
    category = _key(job.get("category"))
    explicit = str(job.get("product_form_factor") or "").strip()
    evidence = str(job.get("product_form_evidence_source") or "").strip()
    expected = CATEGORY_FORMS.get(_key(job.get("product_form_source_category"))) or CATEGORY_FORMS.get(category)
    if job.get("product_form_schema_version") not in {None, "", FORM_SCHEMA_VERSION}:
        raise ValueError("unknown product form schema version")
    if explicit:
        if explicit not in FORM_LABELS:
            raise ValueError("unknown product_form_factor")
        if explicit != "unknown" and not evidence:
            raise ValueError("product form requires provenance")
        if expected and explicit != "unknown" and explicit != expected:
            raise ValueError("product form conflicts with explicit source category")
        if category in {"fan", "misting fan"} and explicit != "unknown":
            raise ValueError("Fan and Misting Fan do not use handheld/backpack taxonomy")
        value = explicit
        basis = evidence or "unspecified"
    else:
        value = expected or "unknown"
        basis = "explicit_source_category" if expected else "unspecified"
    return {"schema_version": FORM_SCHEMA_VERSION, "value": value,
            "label": FORM_LABELS[value], "status": "known" if value != "unknown" else "unknown",
            "evidence_source": basis, "source_category": str(job.get("product_form_source_category") or job.get("category") or "")}


def form_catalog(tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    present = {str(task.get("product_form_factor") or "unknown") for task in tasks}
    return [{"value": value, "label": label} for value, label in FORM_LABELS.items() if value in present]
