"""Bounded public-page sampling. No challenge solving or transport fallback."""
import asyncio
import json
import time
from pathlib import Path

SCHEMA = "voxquarry.bounded-collection/v1"
DEFAULTS = {"max_pages": 3, "max_reviews": 100, "timeout_seconds": 120}
STOP_REASONS = {"page_limit", "review_limit", "time_limit", "source_blocked", "source_error", "source_drift", "duplicate_page", "empty_page"}


def save_limits(store, job_id, limits):
    path = store.data_dir / "collection-limits"
    path.mkdir(exist_ok=True)
    (path / (job_id + ".json")).write_text(json.dumps(limits))


def load_limits(runner, job_id):
    path = runner.store.data_dir / "collection-limits" / (str(job_id) + ".json") if runner.store else None
    values = json.loads(path.read_text()) if path and path.is_file() else DEFAULTS
    bounds = {"max_pages": (1, 10), "max_reviews": (1, 500), "timeout_seconds": (10, 300)}
    return {key: max(low, min(high, int(values.get(key, DEFAULTS[key])))) for key, (low, high) in bounds.items()}


async def collect_sample(runner, product, job_id, channel, fetch, normalize):
    limits = load_limits(runner, job_id)
    deadline = time.monotonic() + limits["timeout_seconds"]
    records, seen, totals = [], set(), []
    reason, pages = "page_limit", 0
    notes = {"collection_schema_version": SCHEMA, "method": "bounded_public_pages", "limits": limits,
             "requested_sample_complete": False, "source_reconciled_strict": False}
    for page_number in range(1, limits["max_pages"] + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "time_limit"; break
        try:
            rows, total = await asyncio.wait_for(fetch(page_number), timeout=remaining)
            if type(total) is not int or total < 0 or not isinstance(rows, list):
                raise ValueError("Invalid source population")
            if totals and total != totals[0]:
                totals.append(total); reason = "source_drift"; break
            totals.append(total)
            pages += 1
            if not rows and len(records) < total:
                reason = "empty_page"; break
            for row in rows:
                record = normalize(row)
                rid = str(record.get("review_id") or "")
                if not rid or rid in seen:
                    reason = "duplicate_page"
                    raise ValueError("Repeated or missing review identity")
                if len(records) >= limits["max_reviews"]:
                    break
                seen.add(rid); records.append(record)
            if len(records) > total:
                reason = "source_drift"; break
            notes.update(pages_collected=pages, stop_reason="page_limit")
            if runner.store and job_id and records and total >= len(records):
                checkpoint = runner.store.data_dir / "runs" / job_id / channel
                checkpoint.mkdir(parents=True, exist_ok=True)
                runner.persist_and_validate(checkpoint, channel, product, records, total, notes)
            if len(records) == total:
                reason = "source_complete"; break
            if len(records) >= limits["max_reviews"]:
                reason = "review_limit"; break
        except asyncio.TimeoutError:
            reason = "time_limit"; break
        except Exception as exc:
            if not records:
                # Sanitized classification only. Do not retain challenge bodies.
                raise RuntimeError(f"{channel}: public source unavailable ({type(exc).__name__})") from None
            if reason != "duplicate_page":
                reason = "source_blocked" if any(x in type(exc).__name__.lower() for x in ("challenge", "denied", "access")) else "source_error"
            break
        if page_number < limits["max_pages"]:
            await asyncio.sleep(min(1.0, max(0, deadline-time.monotonic())))
    total = max(totals) if totals else 0
    if total < len(records):
        raise RuntimeError("Source population contradicts collected records")
    notes.update(stop_reason=reason, pages_collected=pages, source_reconciled_strict=reason == "source_complete",
        requested_sample_complete=reason in {"source_complete", "review_limit", "page_limit"}, empty_source_confirmed=total == 0 and pages > 0)
    return records, total, notes


async def walmart(runner, product, job_id):
    from .runner import norm_walmart_record
    cached = product.pop("_walmart_first_page", None)
    async def fetch(number):
        page = cached if number == 1 and cached else await asyncio.to_thread(runner.walmart_reviews_page, str(product["id"]), number)
        if str(page["lookup_id"]) != str(product["source_product_id"]):
            raise ValueError("Product identity changed")
        return page["rows"], page["source_total"]
    return await collect_sample(runner, product, job_id, "walmart", fetch, lambda row: norm_walmart_record(product, row))


async def homedepot(runner, product, job_id):
    from .runner import norm_record, HomeDepotChallengeError, HomeDepotAccessDeniedError
    driver, browser, context = await runner.browser()
    try:
        page = await context.new_page()
        response = await page.goto(product["url"], wait_until="domcontentloaded", timeout=30000)
        if response and response.status in {401, 403}:
            raise HomeDepotAccessDeniedError()
        title = (await page.title()).lower()
        if any(word in title for word in ("captcha", "challenge", "access denied", "error page")):
            raise HomeDepotChallengeError()
        async def fetch(number):
            return await runner.hd_page(page, str(product["id"]), 1+(number-1)*100, "newest", None)
        return await collect_sample(runner, product, job_id, "homedepot", fetch, lambda row: norm_record("homedepot", product, row))
    finally:
        await context.close(); await browser.close(); await driver.stop()
