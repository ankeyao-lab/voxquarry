"""Inclusive, versioned product research material; never disguises review status."""
from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit

from .store import RetailStore, content_hash, utc_now

REVIEWED = {"approved", "published"}
WARNING = "包含未审核的产品库资料。相关比较和判断属于待验证信息，不能表述为已确认事实；请在回答及导出成果中标注具体产品和未审核范围。"


def remember_actor(store: RetailStore, actor: dict[str, str]) -> None:
    with store.connect() as conn:
        conn.execute("""INSERT INTO review_actors VALUES (?, ?, ?, ?)
            ON CONFLICT(principal_id) DO UPDATE SET display_name=excluded.display_name,
            employee_id=excluded.employee_id, updated_at=excluded.updated_at
            WHERE review_actors.display_name IS NOT excluded.display_name
               OR review_actors.employee_id IS NOT excluded.employee_id""",
            (actor["principal_id"], actor["display_name"], actor.get("employee_id"), utc_now()))


def actor_label(store: RetailStore, principal: str | None) -> dict[str, Any] | None:
    if not principal:
        return None
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM review_actors WHERE principal_id=?", (principal,)).fetchone()
    return dict(row) if row else {"principal_id": principal, "display_name": principal,
                                 "employee_id": None, "identity_status": "historical_unlinked"}


def annotate_product(store: RetailStore, product: dict[str, Any]) -> dict[str, Any]:
    product = copy.deepcopy(product)
    product["reviewer"] = actor_label(store, product.get("reviewed_by")) if product["review_status"] in REVIEWED else None
    product["contains_unreviewed"] = product["review_status"] not in REVIEWED
    product["review_notice"] = WARNING if product["contains_unreviewed"] else "当前版本已审核；来源时效与资料覆盖仍需按研究问题判断。"
    for decision in product.get("review_decisions", []):
        decision["reviewer_identity"] = actor_label(store, decision.get("reviewer"))
    return product


def research_context(store: RetailStore, *, category: str, model_ids: list[str] | None = None,
                     review_scope: str = "all", market_region: str = "") -> dict[str, Any]:
    if not store.category(category):
        raise ValueError("未知品类")
    if review_scope not in {"all", "reviewed"}:
        raise ValueError("review_scope 必须为 all 或 reviewed")
    products = store.list_products(category, market_region=market_region)
    ids = set(model_ids or [])
    if ids - {p["id"] for p in products}:
        raise ValueError("所选产品不存在或不属于当前品类/地区；请重新选择")
    products = [p for p in products if p["review_status"] != "retired"
                and (not ids or p["id"] in ids)
                and (review_scope == "all" or p["review_status"] in REVIEWED)]
    records = []
    for product in sorted(products, key=lambda p: p["id"]):
        p = annotate_product(store, product)
        fields = ("brand", "brand_id", "model_number", "product_name", "category_id", "category_code",
                  "entity_version", "energy_type", "voltage_platform", "product_kind", "bundle", "analysis_eligibility",
                  "notes", "markets", "listings", "specs", "assets", "reports", "review_count", "review_count_status",
                  "review_status", "reviewed_at", "reviewer", "contains_unreviewed", "review_notice",
                  "source_url", "source_type", "source_snapshot_hash", "observed_at", "enrichment_gaps")
        record = {key: p.get(key) for key in fields}
        record["model_id"] = p["id"]
        record["record_hash"] = content_hash(record)
        records.append(record)
    unreviewed = [r["model_id"] for r in records if r["contains_unreviewed"]]
    context = {"schema_version": "retail-research-context/v1", "category": category,
               "market_region": market_region, "review_scope": review_scope,
               "records": records, "record_count": len(records), "unreviewed_count": len(unreviewed),
               "unreviewed_model_ids": unreviewed, "contains_unreviewed": bool(unreviewed),
               "notice": WARNING if unreviewed else "当前资料包仅含已审核版本。",
               "usage": "读取时的资料快照；后续修改或审核不会追溯改变本资料包。"}
    context["snapshot_id"] = content_hash(context)
    return context


def context_markdown(context: dict[str, Any]) -> str:
    lines = [f"# 产品库产品资料 · {context['category']}", "", context["notice"], "",
             f"版本：{context['snapshot_id']}", context["usage"], ""]
    for p in context["records"]:
        reviewer = (p.get("reviewer") or {}).get("display_name")
        lines += [f"## {p['brand']} {p['model_number']}",
                  f"Model ID：{p['model_id']} · 内容版本：{p['entity_version']}",
                  f"审核：{'未审核' if p['contains_unreviewed'] else '已审核'}"
                  + (f" · {reviewer} · {p.get('reviewed_at')}" if reviewer else ""),
                  p["review_notice"], f"名称：{p.get('product_name') or '待补充'}",
                  f"来源：{p.get('source_url') or '待补充'}", f"备注：{p.get('notes') or '无'}", ""]
        for spec in (p.get("specs") or {}).values():
            value = spec.get("normalized_value")
            if value not in (None, ""):
                lines.append(f"- {spec['label']}：{value} {spec.get('normalized_unit') or ''}；来源：{spec.get('source_url') or '待补充'}")
        for listing in p.get("listings") or []:
            lines.append(f"- 渠道：{listing.get('retailer')} · {listing.get('market_region')} · {listing.get('online_url')}")
            for price in listing.get("prices") or []:
                lines.append(f"  - 价格：{price.get('normalized_amount')} {price.get('currency_code')}；截至 {price.get('observed_at')}")
        lines += [f"评论覆盖：{p.get('review_count_status')}；评论数：{p.get('review_count') if p.get('review_count') is not None else '待接入'}", ""]
    return "\n".join(lines)


def manual_evidence(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Record the entered evidence, not a fictitious downloaded web-page hash."""
    payload = copy.deepcopy(payload)
    note = str(payload.pop("evidence_note", "") or "").strip()
    source = str(payload.get("source_url") or "").strip()
    if not note:
        return payload
    if urlsplit(source).scheme not in {"https", "http"} or not urlsplit(source).hostname:
        raise ValueError("记录依据时请提供有效的来源网页 URL")
    evidence = {"source_url": source, "source_type": "manual_evidence_record",
                "observed_at": utc_now(), "source_snapshot_hash": content_hash({
                    "kind": "manual_evidence_record", "actor": actor, "note": note, "payload": payload})}
    payload.update(evidence)
    payload["notes"] = str(payload.get("notes") or "") + f"\n人工核对依据：{note}"
    for spec in (payload.get("specs") or {}).values():
        if not spec.get("source_snapshot_hash"):
            spec.update(evidence)
    for listing in payload.get("listings") or []:
        if not listing.get("source_snapshot_hash"):
            listing.update({**evidence, "source_url": listing.get("online_url") or source})
        for price in listing.get("prices") or []:
            if not price.get("source_snapshot_hash"):
                price.update({**evidence, "source_url": listing.get("online_url") or source})
    return payload
