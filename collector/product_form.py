"""Product-family/form observation contract; never infer from review prose.

This collector contract is mirrored by reporting/product_form.py and covered
by a parity test. Neither projection replaces Chengmu's model authority.
"""
from __future__ import annotations
import unicodedata

SCHEMA_VERSION = "voxquarry.product-form/v1"
LABELS = {"handheld": "手持式", "backpack": "背负式", "wheeled": "轮式", "stick": "杆式", "upright": "直立式", "robot": "机器人式", "unknown": "形态未明确"}
CATEGORY_FORMS = {"handheld sprayer": "handheld", "backpack sprayer": "backpack", "handheld spreader": "handheld", "handheld vacuum": "handheld", "cordless handheld vacuum": "handheld", "backpack vacuum": "backpack", "cordless stick vacuum": "stick", "stick vacuum": "stick", "upright vacuum": "upright", "robot vacuum": "robot", "wheeled blower": "wheeled"}
CATEGORY_NAMES = {"handheld sprayer": "Sprayer", "backpack sprayer": "Sprayer", "handheld spreader": "Spreader", "cordless handheld vacuum": "Handheld Vacuum"}


def key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def family_category(category):
    return CATEGORY_NAMES.get(key(category), category)


def product_form(category, value=None, *, evidence_source=None, source_category=None):
    expected = CATEGORY_FORMS.get(key(source_category or category))
    if value is not None and value != "":
        if value not in LABELS:
            raise ValueError("unknown product_form_factor")
        if value != "unknown" and not evidence_source:
            raise ValueError("product form requires provenance")
        if expected and value not in {expected, "unknown"}:
            raise ValueError("product form conflicts with explicit source category")
    else:
        value = expected or "unknown"
        evidence_source = "explicit_source_category" if expected else "unspecified"
    if key(category) in {"fan", "misting fan"} and value != "unknown":
        raise ValueError("Fan and Misting Fan do not use handheld/backpack taxonomy")
    return {"schema_version": SCHEMA_VERSION, "value": value, "label": LABELS[value],
            "status": "unknown" if value == "unknown" else "known", "evidence_source": evidence_source or "unspecified",
            "source_category": str(source_category or category or "")}


def observation_form(category, observation=None, existing=None):
    supplied = (observation or {}).get("product_form")
    if supplied is not None:
        if not isinstance(supplied, dict) or supplied.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("invalid product form contract")
        form = product_form(category, supplied.get("value"), evidence_source=supplied.get("evidence_source"), source_category=supplied.get("source_category"))
    else:
        form = product_form(category)
    previous = dict(existing or {})
    if previous.get("product_form_factor") not in {None, "", "unknown"}:
        if form["value"] == "unknown":
            form = product_form(category, previous["product_form_factor"], evidence_source=previous.get("product_form_evidence_source"), source_category=previous.get("product_form_source_category"))
        elif previous["product_form_factor"] != form["value"]:
            raise ValueError("product form conflicts with existing product identity")
    return form


def form_columns(form):
    return {"product_form_schema_version": SCHEMA_VERSION, "product_form_factor": form["value"],
            "product_form_evidence_source": form["evidence_source"], "product_form_source_category": form["source_category"]}


def persist_form(conn, job_id, form):
    values = form_columns(form)
    conn.execute("UPDATE jobs SET " + ",".join(name + "=?" for name in values) + " WHERE id=?", (*values.values(), job_id))
