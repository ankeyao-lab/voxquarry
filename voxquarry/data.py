"""Portable ingestion and immutable snapshots. Bundled examples are synthetic."""
from argparse import Namespace
from pathlib import Path
import uuid


def ingest(store, runner, payload):
    rows = payload.get("reviews")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("reviews must contain 1–10,000 records")
    brand = str(payload.get("brand") or "Unspecified")[:120]
    model = str(payload.get("model") or "Imported product")[:120]
    category = str(payload.get("category") or "Lawn Mower")
    language = payload.get("language", "en")
    if language not in {"en", "zh-CN", "unknown"}:
        raise ValueError("language must be en, zh-CN or unknown")
    job_id = uuid.uuid4().hex[:12]
    synthetic = payload.get("synthetic") is True
    channel = "synthetic" if synthetic else "file_import"
    product = {"id": job_id, "source_product_id": job_id, "title": model,
               "url": "", "market_region": "North America", "language": language,
               "synthetic": synthetic}
    records, seen = [], set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Each review must be an object")
        rid = str(row.get("review_id") or f"import-{i+1}")[:128]
        if rid in seen:
            raise ValueError("Duplicate review_id: " + rid)
        seen.add(rid)
        rating = row.get("rating")
        if rating is not None and (type(rating) not in {int, float} or not 1 <= rating <= 5):
            raise ValueError("rating must be 1–5 or null")
        body = str(row.get("review_text") or "")
        if len(body) > 20000:
            raise ValueError("Review text exceeds 20,000 characters")
        records.append({"channel": channel, "product_id": job_id, "product_title": model,
            "product_url": "", "review_id": rid, "display_site": "example.test" if synthetic else "user_import",
            "source_site": "example.test" if synthetic else "user_import", "source_product_id": job_id,
            "market_region": "North America", "market_region_evidence": "import_declaration",
            "is_syndicated": False, "submission_time": str(row.get("submission_time") or "2026-01-01"),
            "title": str(row.get("title") or ""), "is_recommended": "", "user_nickname": "",
            "review_text": body, "rating": rating if rating is not None else "", "verified_purchaser": "",
            "where_purchase": "", "age": "", "review_word_count": len(body.split()),
            "is_eligible": True, "is_valid_text_review": bool(body.strip()), "exclusion_reason": "",
            "language": language, "raw": {"review_id": rid, "review_text": body, "rating": rating, "synthetic": synthetic}})
    store.create_job(job_id, "external_import", channel, brand=brand, model=model,
                     category=category, market_region="North America", source_product_id=job_id)
    run_dir = store.data_dir / "runs" / job_id / channel
    run_dir.mkdir(parents=True)
    report = runner.persist_and_validate(run_dir, channel, product, records, len(records),
        {"method": "synthetic_fixture" if synthetic else "user_file_import", "source_reconciled_strict": True,
         "language": language, "source_population": "provided_file", "site_coverage": "unknown"})
    store.finish(job_id, {channel: report})
    return {"job_id": job_id, "records": len(records), "language": language, "synthetic": synthetic}


def snapshot(source_root, snapshots):
    from scripts.build_reporting_snapshot_v2 import build_snapshot
    target = Path(snapshots) / uuid.uuid4().hex
    build_snapshot(Namespace(source_root=Path(source_root), output_root=target,
        connector_commit="voxquarry-0.1.0", reporting_commit="voxquarry-0.1.0", product_master_projection=None))
    return target


def demo_payload():
    phrases = [
        (5, "The mower is quiet and easy to push. Battery life is enough for my small yard."),
        (4, "I like the lightweight design and compact storage. The cut quality is excellent."),
        (2, "The battery runs out too quickly on tall grass. I need a second battery to finish."),
        (3, "The mower struggles with wet grass. The grass bag is hard to attach."),
        (5, "Easy assembly and simple controls. The self propelled speed is comfortable."),
        (4, "The mower feels durable and starts reliably. Charging takes longer than expected."),
        (1, "The handle broke after a few uses. Customer service did not resolve the issue."),
        (5, "A clean even cut with very little noise. It is easy to fold and store."),
    ]
    return {"brand": "Northstar Demo", "model": "M40 (synthetic)", "category": "Lawn Mower",
        "language": "en", "synthetic": True,
        "reviews": [{"review_id": f"synthetic-{i+1:03}", "rating": rating,
                     "review_text": body + f" Synthetic scenario {i+1}.", "submission_time": "2026-01-01"}
                    for i, (rating, body) in enumerate(phrases * 5)]}
