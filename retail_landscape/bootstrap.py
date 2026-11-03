from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .field_catalog import CATEGORIES, fields_for
from .store import RetailStore, stable_id


RETAILERS = {
    "lowes": "Lowe's",
    "homedepot": "Home Depot",
    "ace": "Ace Hardware",
    "amazon_reviews_2023": "Amazon",
    "tractorsupply": "Tractor Supply",
    "ryobi": "RYOBI Official",
    "ego": "EGO Official",
    "einhell": "Einhell Official",
    "milwaukee": "Milwaukee Official",
    "dewalt": "DEWALT Official",
}

BRAND_CANONICAL = {
    "craftsman": "CRAFTSMAN",
    "dewalt": "DEWALT",
    "ridgid": "RIDGID",
    "rigid": "RIDGID",
    "ryobi": "RYOBI",
    "ego": "EGO",
    "greenworks": "Greenworks",
    "milwaukee": "Milwaukee",
    "einhell": "Einhell",
}


def normalize_brand(value: Any) -> str:
    brand = str(value or "").strip()
    return BRAND_CANONICAL.get(brand.casefold(), brand)

TASK_MODEL_OVERRIDES = {}  # No deployment-specific product seeds.

DC_PLATFORM_OVERRIDES = {}  # No deployment-specific product seeds.

# Small, source-backed enrichment set for task-linked Wet/Dry Vacuum models.
# Values are candidate facts and remain pending_review. Do not add a value here
# unless the cited manufacturer or retailer page states it explicitly.
VERIFIED_WDV_FACTS = {}  # No deployment-specific product seeds.


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def observed_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def workbook_source_ref(source_hash: str, sheet: str, model: str, field: str = "model") -> str:
    """Return a portable evidence locator without exposing a machine-local file path."""
    digest = source_hash.removeprefix("sha256:")
    return (
        f"urn:voxquarry:workbook:{digest}"
        f"?sheet={quote(sheet, safe='')}&model={quote(model, safe='')}&field={quote(field, safe='')}"
    )


def source_category_map() -> dict[str, str]:
    return {
        source: item["code"]
        for item in CATEGORIES
        if item.get("enabled", True)
        for source in item["source_categories"]
    }


def explicit_model_from_url(url: str) -> str | None:
    matches = re.findall(r"(?<![A-Za-z0-9])([A-Z]{1,6}\d{2,6}[A-Z0-9]{1,6})(?![A-Za-z0-9])", url)
    candidates = [item for item in matches if len(item) <= 16]
    return candidates[-1] if candidates else None


MODEL_DESCRIPTION_TOKENS = {
    "ACCESSORIES", "BATTERY", "CORDLESS", "FILTER", "GALLON", "GALLONS",
    "HOSE", "INCLUDED", "POWERHEAD", "QUIET", "SHOP", "STEALTHSONIC",
    "VAC", "VACUUM", "WET", "WITH",
}


def trustworthy_snapshot_model(value: str) -> str | None:
    """Accept an upstream model only when it still resembles a model identifier.

    Sanitized connector snapshots occasionally contain a retailer title slug in
    ``model``.  Keeping suspicious values pending is safer than turning a product
    description into a published product identity.
    """
    candidate = value.strip()
    if not candidate or len(candidate) > 32 or candidate.count(" ") > 4:
        return None
    tokens = {token for token in re.split(r"[^A-Za-z]+", candidate.upper()) if token}
    if len(tokens & MODEL_DESCRIPTION_TOKENS) >= 2:
        return None
    if not re.search(r"\d", candidate) and "-" in candidate and len(tokens) >= 3:
        return None
    return candidate


def explicit_voltage_from_url(url: str) -> str | None:
    values = []
    for value in re.findall(r"(?<!\d)(12|18|20|24|36|40|54|56|60|80|120)(?:V|v|[-_ ]volt)", url):
        normalized = f"{value}V"
        if normalized not in values:
            values.append(normalized)
    return " / ".join(values) if values else None


def energy_and_voltage(brand: str, model: str, url: str) -> tuple[str, str | None]:
    explicit = explicit_voltage_from_url(url)
    upper_model = model.upper()
    if explicit:
        if explicit.split(" / ")[0] == "120V":
            return "AC", None
        return "DC", explicit.split(" / ")[0]
    if upper_model in DC_PLATFORM_OVERRIDES:
        return "DC", DC_PLATFORM_OVERRIDES[upper_model]
    if upper_model.startswith(("PCL", "PWV", "R18", "RV18")):
        return "DC", "18V"
    return "AC", None


def snapshot_rows(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return payload["jobs"], payload.get("generated_at")
    raise ValueError("任务快照必须是任务数组或包含 jobs 数组的脱敏快照")


def bootstrap_snapshot(store: RetailStore, snapshot_path: Path) -> dict[str, int]:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows, generated_at = snapshot_rows(payload)
    mapping = source_category_map()
    snapshot_hash = file_hash(snapshot_path)
    seen_products: set[str] = set()
    seen_listings: set[str] = set()
    with store.connect() as conn:
        for row in rows:
            category_code = mapping.get(str(row.get("category")))
            if not category_code:
                continue
            category = store.category(category_code)
            brand = normalize_brand(row.get("brand"))
            model = TASK_MODEL_OVERRIDES.get(str(row.get("id"))) or trustworthy_snapshot_model(str(row.get("model") or ""))
            url = str(row.get("target_url") or "").strip()
            market = str(row.get("market_region") or "Unknown").strip()
            channel = str(row.get("channel") or "").strip()
            if not (brand and url and category):
                continue
            model = model or explicit_model_from_url(url) or f"待确认 · {row['id']}"
            energy_type, voltage = energy_and_voltage(brand, model, url)
            product_title = next(
                (
                    str(row.get(key)).strip()
                    for key in ("product_name", "product_title", "title")
                    if row.get(key) not in (None, "")
                ),
                None,
            )
            # A connector task snapshot is evidence, not a human approval decision.
            # Even an explicit model must enter review before it can be published.
            product_status = "pending_review"
            product_id = stable_id("pvr", category_code, brand, model)
            listing_id = stable_id("lst", product_id, market, RETAILERS.get(channel, channel), url)
            now = generated_at or observed_at(snapshot_path)
            conn.execute(
                """
                INSERT INTO products(
                  id, category_id, brand, model_number, product_name, energy_type,
                  voltage_platform, review_status,
                  source_type, source_snapshot_hash, source_url, observed_at,
                  reviewed_by, reviewed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'voxquarry_task_snapshot', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  brand=excluded.brand,
                  source_snapshot_hash=excluded.source_snapshot_hash,
                  source_url=excluded.source_url, observed_at=excluded.observed_at,
                  energy_type=COALESCE(products.energy_type, excluded.energy_type),
                  voltage_platform=COALESCE(products.voltage_platform, excluded.voltage_platform),
                  updated_at=excluded.updated_at
                """,
                (
                    product_id, category["id"], brand, model, product_title,
                    energy_type, voltage, product_status, snapshot_hash, url, now,
                    None, None, now, now,
                ),
            )
            conn.execute(
                """
                INSERT INTO product_listings(
                  id, product_id, market_region, retailer, channel, online_url,
                  target_key, retail_status, review_status, source_type, source_snapshot_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 'connector_task_listing', ?, ?)
                ON CONFLICT(product_id, market_region, retailer, online_url) DO UPDATE SET
                  target_key=excluded.target_key, retail_status=excluded.retail_status,
                  source_type=excluded.source_type,
                  source_snapshot_hash=excluded.source_snapshot_hash, observed_at=excluded.observed_at
                """,
                (
                    listing_id, product_id, market, RETAILERS.get(channel, channel or "Unknown"),
                    channel, url, row.get("target_key"), product_status, snapshot_hash, now,
                ),
            )
            region = conn.execute(
                "SELECT id FROM market_regions WHERE lower(name)=lower(?)",
                (market,),
            ).fetchone()
            if region:
                conn.execute(
                    """
                    INSERT INTO product_market_regions(
                      product_id, market_region_id, review_status, source_url,
                      source_snapshot_hash, observed_at
                    ) VALUES (?, ?, 'pending_review', ?, ?, ?)
                    ON CONFLICT(product_id, market_region_id) DO UPDATE SET
                      source_url=excluded.source_url,
                      source_snapshot_hash=excluded.source_snapshot_hash,
                      observed_at=excluded.observed_at
                    """,
                    (product_id, region["id"], url, snapshot_hash, now),
                )
            retailer_name = RETAILERS.get(channel, channel or "Unknown")
            retailer_row = conn.execute(
                "SELECT id FROM retailers WHERE lower(name)=lower(?)",
                (retailer_name,),
            ).fetchone()
            if retailer_row:
                conn.execute(
                    "INSERT OR IGNORE INTO product_retailers(product_id, retailer_id, review_status) VALUES (?, ?, 'pending_review')",
                    (product_id, retailer_row["id"]),
                )
                conn.execute(
                    "UPDATE product_listings SET retailer_id=? WHERE id=?",
                    (retailer_row["id"], listing_id),
                )
            conn.execute(
                """
                INSERT INTO review_task_links(
                  product_listing_id, task_id, task_status, active_result_status, last_attempt_status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(product_listing_id, task_id) DO UPDATE SET
                  task_status=excluded.task_status, active_result_status=excluded.active_result_status,
                  last_attempt_status=excluded.last_attempt_status
                """,
                (
                    listing_id, row["id"], row.get("status", "unknown"),
                    row.get("active_result_status"), row.get("last_attempt_status"),
                ),
            )
            seen_products.add(product_id)
            seen_listings.add(listing_id)
    store._backfill_product_master_identities()
    return {"products": len(seen_products), "listings": len(seen_listings)}


def bootstrap_review_results(store: RetailStore, snapshot_path: Path | None) -> dict[str, int]:
    """Attach sanitized connector result totals to existing task links.

    The handoff contains counts and hashes only. It never contains review rows
    or raw result JSON, and it cannot create products, listings, or tasks.
    """
    if not snapshot_path or not snapshot_path.is_file():
        return {"updated": 0, "skipped": 0, "unmatched": 0}
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = payload.get("records") or payload.get("results") or payload.get("jobs") or []
    generated_at = payload.get("generated_at") or observed_at(snapshot_path)
    snapshot_hash = file_hash(snapshot_path)
    updated = skipped = unmatched = 0
    with store.connect() as conn:
        for row in rows:
            task_id = str(row.get("task_id") or row.get("id") or "").strip()
            if not task_id:
                skipped += 1
                continue
            try:
                unique_reviews = int(row["unique_reviews"]) if row.get("unique_reviews") is not None else None
                source_total = int(row["source_total"]) if row.get("source_total") is not None else None
            except (TypeError, ValueError):
                skipped += 1
                continue
            if (unique_reviews is not None and unique_reviews < 0) or (source_total is not None and source_total < 0):
                skipped += 1
                continue
            result_status = str(row.get("result_status") or row.get("status") or "").strip().upper() or None
            observed = row.get("result_observed_at") or row.get("finished_at") or generated_at
            digest = row.get("result_snapshot_hash") or snapshot_hash
            cursor = conn.execute(
                """
                UPDATE review_task_links
                SET unique_reviews=?, source_total=?, result_status=?,
                    result_observed_at=?, result_snapshot_hash=?
                WHERE task_id=?
                """,
                (unique_reviews, source_total, result_status, observed, digest, task_id),
            )
            if cursor.rowcount:
                updated += cursor.rowcount
            else:
                unmatched += 1
    return {"updated": updated, "skipped": skipped, "unmatched": unmatched}


def _alias_map(category_code: str) -> dict[str, str]:
    return {
        alias.strip(): field["code"]
        for field in fields_for(category_code)
        for alias in field["source_aliases"]
    }


def excel_energy_voltage(value: Any, model: str) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        upper = model.upper()
        if upper in DC_PLATFORM_OVERRIDES:
            return "DC", DC_PLATFORM_OVERRIDES[upper]
        if upper.startswith(("PCL", "PWV", "R18", "RV18", "M18")):
            return "DC", "18V"
        return None, None
    if "AC" in text.upper() and "DC" not in text.upper():
        return "AC", None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)", text)
    if not match:
        return None, None
    number = float(match.group(1))
    if number >= 100:
        return "AC", None
    return "DC", f"{int(number) if number.is_integer() else number:g}V"


def first_numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def retailer_ids_from_text(conn: Any, value: Any) -> list[str]:
    text = str(value or "").casefold()
    aliases = {
        "home depot": "Home Depot", "thd": "Home Depot", "lowe": "Lowe's",
        "amazon": "Amazon", "ace": "Ace Hardware", "tractor": "Tractor Supply",
    }
    names = {name for token, name in aliases.items() if token in text}
    result = []
    for name in names:
        row = conn.execute("SELECT id FROM retailers WHERE lower(name)=lower(?)", (name,)).fetchone()
        if row:
            result.append(str(row["id"]))
    return result


def _import_matrix(
    store: RetailStore,
    workbook_path: Path,
    category_code: str,
    sheets: list[str] | None = None,
) -> dict[str, int]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for Excel staging import") from exc
    source_hash = file_hash(workbook_path)
    source_time = observed_at(workbook_path)
    aliases = _alias_map(category_code)
    wb = load_workbook(workbook_path, read_only=True, data_only=False)
    created: set[str] = set()
    facts = 0
    for ws in wb.worksheets:
        if sheets and ws.title not in sheets:
            continue
        rows = list(ws.iter_rows(values_only=True))
        by_label = {
            str(row[0]).replace("\n", " ").strip(): row
            for row in rows
            if row and row[0] not in (None, "")
        }
        brand_row = next((by_label[key] for key in by_label if key in {"Brand", "• Brand:"}), None)
        model_row = next((by_label[key] for key in by_label if key in {"Model", "Model No.", "Model #", "• Model Number:"}), None)
        voltage_row = next((by_label[key] for key in by_label if key in {"Voltage", "Volts", "• Volts"}), None)
        retailer_row = next((by_label[key] for key in by_label if key.casefold().startswith("retailer")), None)
        if not brand_row or not model_row:
            continue
        category = store.category(category_code)
        for col in range(1, min(len(brand_row), len(model_row))):
            brand = normalize_brand(brand_row[col])
            model = str(model_row[col] or "").strip()
            if not brand or not model:
                continue
            product_id = stable_id("pvr", category_code, brand, model)
            energy_type, voltage = excel_energy_voltage(
                voltage_row[col] if voltage_row and col < len(voltage_row) else None,
                model,
            )
            # Excel workbooks provide model/spec facts but no authoritative
            # ecommerce title. Keep product_name empty until a cited official
            # or retailer title is captured; never manufacture brand + model.
            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO products(
                      id, category_id, brand, model_number, product_name, energy_type,
                      voltage_platform, review_status,
                      source_type, source_snapshot_hash, source_url, observed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', 'excel_staging', ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      energy_type=COALESCE(products.energy_type, excluded.energy_type),
                      voltage_platform=COALESCE(products.voltage_platform, excluded.voltage_platform),
                      source_snapshot_hash=excluded.source_snapshot_hash,
                      source_url=COALESCE(products.source_url, excluded.source_url),
                      observed_at=excluded.observed_at, updated_at=excluded.updated_at
                    """,
                    (
                        product_id, category["id"], brand, model, None,
                        energy_type, voltage, source_hash,
                        workbook_source_ref(source_hash, ws.title, model),
                        source_time, source_time, source_time,
                    ),
                )
                if retailer_row and col < len(retailer_row):
                    for retailer_id in retailer_ids_from_text(conn, retailer_row[col]):
                        conn.execute(
                            "INSERT OR IGNORE INTO product_retailers(product_id, retailer_id, review_status) VALUES (?, ?, 'pending_review')",
                            (product_id, retailer_id),
                        )
                definitions = {
                    row["code"]: dict(row)
                    for row in conn.execute("SELECT * FROM field_definitions WHERE category_id=?", (category["id"],)).fetchall()
                }
                for label, row in by_label.items():
                    code = aliases.get(label)
                    if not code or col >= len(row) or row[col] in (None, ""):
                        continue
                    if code in {"brand", "model_number", "energy_type", "voltage_platform", "retailer", "online_url", "launch_region"}:
                        continue
                    value = row[col]
                    spec_id = stable_id("spv", product_id, definitions[code]["id"])
                    conn.execute(
                        """
                        INSERT INTO spec_values(
                          id, product_id, field_definition_id, raw_value, normalized_value,
                          normalized_unit, value_state, review_status, source_url, source_type,
                          source_snapshot_hash, observed_at, numeric_value, unit_id
                        ) VALUES (?, ?, ?, ?, ?, ?, 'known', 'pending_review', ?, 'excel_staging', ?, ?, ?, ?)
                        ON CONFLICT(product_id, field_definition_id) DO UPDATE SET
                          raw_value=excluded.raw_value, normalized_value=excluded.normalized_value,
                          normalized_unit=excluded.normalized_unit, review_status='pending_review',
                          source_url=excluded.source_url, source_type=excluded.source_type,
                          source_snapshot_hash=excluded.source_snapshot_hash, observed_at=excluded.observed_at,
                          numeric_value=excluded.numeric_value, unit_id=excluded.unit_id
                        """,
                        (
                            spec_id, product_id, definitions[code]["id"], str(value), str(value),
                            definitions[code]["canonical_unit"],
                            workbook_source_ref(source_hash, ws.title, model, label),
                            source_hash, source_time,
                            first_numeric(value) if definitions[code].get("is_matrix") else None,
                            definitions[code].get("unit_id"),
                        ),
                    )
                    facts += 1
            created.add(product_id)
    wb.close()
    store._backfill_product_master_identities()
    return {"products": len(created), "candidate_facts": facts}


def bootstrap_excel(
    store: RetailStore,
    *,
    fan_workbook: Path | None = None,
    vacuum_workbook: Path | None = None,
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    if fan_workbook and fan_workbook.exists():
        result["fan"] = _import_matrix(store, fan_workbook, "fan")
    if vacuum_workbook and vacuum_workbook.exists():
        result["wet_dry_vacuum"] = _import_matrix(
            store,
            vacuum_workbook,
            "wet_dry_vacuum",
            ["Shopvac", "Vacmaster", "Milwaukee", "Dewalt", "Rigid", "Craftsman", "Ryobi", "Greenworks", "Flex", "Stanley"],
        )
    return result


def bootstrap_reports(store: RetailStore, report_root: Path) -> int:
    aliases = {
        "fan": {"风扇", "Fan"},
        "wet_dry_vacuum": {"干湿两用吸尘器", "Wet/Dry Vacuum"},
        "light": {"工作灯", "LED Lighting", "Light"},
    }
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for path in report_root.glob("rpt_*.json") if report_root.is_dir() else []:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        categories = set((report.get("scope") or {}).get("categories") or [])
        for category_code, names in aliases.items():
            if not categories & names:
                continue
            key = (category_code, report["report_id"])
            chosen[key] = {**report, "_path": str(path)}
    with store.connect() as conn:
        for (category_code, _), report in chosen.items():
            category = store.category(category_code)
            conn.execute(
                """
                INSERT OR REPLACE INTO report_links(
                  category_id, report_id, title, report_path, generated_at, market_region
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    category["id"], report["report_id"], report.get("title") or report["report_id"],
                    report["_path"], report.get("generated_at"),
                    (report.get("scope") or {}).get("market_region"),
                ),
            )
    return len(chosen)


def bootstrap_verified_wdv_facts(store: RetailStore) -> dict[str, int]:
    category = store.category("wet_dry_vacuum")
    if not category:
        return {"products": 0, "facts": 0}
    observed = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    products = 0
    facts = 0
    with store.connect() as conn:
        definitions = {
            row["code"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM field_definitions WHERE category_id=? AND active=1",
                (category["id"],),
            ).fetchall()
        }
        for model, evidence in VERIFIED_WDV_FACTS.items():
            rows = conn.execute(
                "SELECT id FROM products WHERE category_id=? AND upper(model_number)=upper(?)",
                (category["id"], model),
            ).fetchall()
            if not rows:
                continue
            products += len(rows)
            for product in rows:
                for code, (number, unit_id) in evidence["facts"].items():
                    definition = definitions.get(code)
                    if not definition:
                        continue
                    unit = conn.execute("SELECT symbol FROM units WHERE id=?", (unit_id,)).fetchone()
                    spec_id = stable_id("spv", product["id"], definition["id"])
                    conn.execute(
                        """
                        INSERT INTO spec_values(
                          id, product_id, field_definition_id, raw_value, normalized_value,
                          normalized_unit, value_state, review_status, source_url,
                          observed_at, numeric_value, unit_id
                        ) VALUES (?, ?, ?, ?, ?, ?, 'known', 'pending_review', ?, ?, ?, ?)
                        ON CONFLICT(product_id, field_definition_id) DO UPDATE SET
                          raw_value=excluded.raw_value,
                          normalized_value=excluded.normalized_value,
                          normalized_unit=excluded.normalized_unit,
                          value_state='known', review_status='pending_review',
                          source_url=excluded.source_url, observed_at=excluded.observed_at,
                          numeric_value=excluded.numeric_value, unit_id=excluded.unit_id
                        """,
                        (
                            spec_id, product["id"], definition["id"], str(number), str(number),
                            unit["symbol"], evidence["source"], observed, float(number), unit_id,
                        ),
                    )
                    facts += 1
    return {"products": products, "facts": facts}


def bootstrap_canonical_brands(store: RetailStore) -> int:
    updated = 0
    renames: list[tuple[str, str]] = []
    with store.connect() as conn:
        rows = conn.execute("SELECT id, brand, brand_id FROM products").fetchall()
        for row in rows:
            canonical = normalize_brand(row["brand"])
            if canonical != row["brand"]:
                if row["brand_id"]:
                    renames.append((str(row["brand_id"]), canonical))
                else:
                    conn.execute("UPDATE products SET brand=?, updated_at=? WHERE id=?", (canonical, datetime.now(timezone.utc).replace(microsecond=0).isoformat(), row["id"]))
                updated += 1
    for brand_id, canonical in dict.fromkeys(renames):
        store.rename_brand(brand_id, canonical, actor="bootstrap_canonical_brands")
    store._backfill_product_master_identities()
    return updated


def build_preview(
    store: RetailStore,
    snapshot_path: Path,
    report_root: Path,
    fan_workbook: Path | None = None,
    vacuum_workbook: Path | None = None,
    review_results_snapshot: Path | None = None,
) -> dict[str, Any]:
    result = {
        "snapshot": bootstrap_snapshot(store, snapshot_path),
        "excel": bootstrap_excel(store, fan_workbook=fan_workbook, vacuum_workbook=vacuum_workbook),
    }
    result["verified_wdv"] = bootstrap_verified_wdv_facts(store)
    result["review_results"] = bootstrap_review_results(store, review_results_snapshot)
    result["canonical_brands"] = bootstrap_canonical_brands(store)
    result["reports"] = bootstrap_reports(store, report_root)
    result["summary"] = store.summary()
    return result
