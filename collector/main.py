import asyncio
import csv
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .product_form import product_form, family_category, CATEGORY_FORMS, key as form_key
from .category_vocabulary import canonical_category_name
from .external_import import (
    enrich_import_bundle_metadata,
    ExternalImportError,
    external_import_format_requirements,
    IMPORT_VALIDATION_SCHEMA_VERSION,
    METADATA_AUTOFILL_CHANNELS,
    validate_import_bundle,
    write_import_artifacts,
)
from .external_cache_export import (
    build_external_cache_export_plan,
    release_external_cache_export_plan,
    stream_external_cache_export,
)
from .generic_import import (
    CONFIRM_SCHEMA_VERSION,
    GenericImportError,
    GovernedImportRegistry,
    PLAN_REQUEST_SCHEMA_VERSION,
    open_private_binary_writer,
)
from .runner import (
    CHANNELS,
    COLLECTION_CHANNELS,
    RetryableChannelError,
    TractorSupplyAccessError,
    Runner,
    infer_market_region,
    validate_channel_market_region,
)
from .product_master import ProductMasterClient, ProductMasterError
from .product_identity import (
    build_product_observation,
    build_task_energy,
    build_voltage_platform,
    extract_voltage,
    normalize_energy_type,
    normalize_voltage,
)
from .product_metadata import (
    normalize_model,
    normalize_product_metadata,
    require_governed_metadata,
)
from .master_data import (
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    valid_admin_proxy_token_by_runtime,
    valid_production_trusted_actors,
)
from .source_product_identity import source_product_identity_from_url
from .task_master_data import TaskCatalogError
from .source_metadata_client import SourceMetadataClient, merge_source_metadata
from .reviewed_product_metadata import reviewed_product_metadata
from .store import (
    CancelRequestedError,
    EXTERNAL_IMPORT_ACTION,
    EXTERNAL_IMPORT_CHANNELS,
    JOBS_PAGE_DEFAULT_LIMIT,
    RETRY_JOB_PRIORITY,
    Store,
    canonical_target_url,
)

store = Store()
from .task_view import TaskView
task_views = TaskView(store)
runner = Runner(store)
product_master = ProductMasterClient(timeout=3)
metadata_client = runner.metadata_client
generic_import_registry = GovernedImportRegistry(store.data_dir)


async def worker_loop(channel):
    while True:
        job = store.next_job(channel)
        if not job:
            await asyncio.sleep(1)
            continue
        try:
            result = await runner.run(
                job["id"],
                job["action"],
                job["channel"],
                job.get("target_url"),
                market_region=job.get("market_region"),
                source_product_id=job.get("source_product_id"),
                expected_category=job.get("category"),
                expected_model=job.get("model"),
            )
            report = next((item for item in result.values() if isinstance(item, dict) and item.get("product")), {})
            product = report.get("product") or {}
            # Metadata was filled before collection; reconcile the final typed
            # product result without promoting review IDs or URL hints to facts.
            runner.enrich_product_metadata(job["id"], product)
            is_partial = any(
                isinstance(item, dict)
                and item.get("status") == "PARTIAL_SUCCESS"
                for item in result.values()
            )
            if is_partial:
                partial_report = next(
                    item for item in result.values()
                    if isinstance(item, dict)
                    and item.get("status") == "PARTIAL_SUCCESS"
                )
                store.finish_partial(
                    job["id"],
                    result,
                    (partial_report.get("notes") or {}).get("stop_reason"),
                )
            else:
                store.finish(job["id"], result)
        except CancelRequestedError:
            cleanup_run(job["id"])
            store.delete_cancelled(job["id"])
        except RetryableChannelError as exc:
            if store.is_cancel_requested(job["id"]):
                cleanup_run(job["id"])
                store.delete_cancelled(job["id"])
            else:
                max_requeues = max(
                    0,
                    int(
                        os.getenv(
                            "RETRYABLE_CHANNEL_MAX_REQUEUES",
                            "1",
                        )
                    ),
                )
                prior_requeues = store.count_events(
                    job["id"],
                    "source_requeued",
                )
                if prior_requeues >= max_requeues:
                    store.fail(
                        job["id"],
                        RuntimeError(
                            f"{exc}; source access remained blocked after "
                            f"{prior_requeues} bounded requeue(s)"
                        ),
                    )
                else:
                    store.requeue(
                        job["id"],
                        f"{channel}: source access paused; automatic retry in {exc.retry_after}s",
                        str(exc),
                        retry_after=exc.retry_after,
                        failure_class=getattr(exc, "failure_class", "transport_error"),
                    )
        except TractorSupplyAccessError as exc:
            if getattr(exc, "cooldown_seconds", 0) > 0:
                store.pause_channel(
                    channel,
                    exc.cooldown_seconds,
                    getattr(exc, "failure_class", "access_denied"),
                    str(exc),
                )
            store.fail(job["id"], exc)
        except Exception as exc:
            if store.is_cancel_requested(job["id"]):
                cleanup_run(job["id"])
                store.delete_cancelled(job["id"])
            else:
                store.fail(job["id"], exc)


def cleanup_run(job_id, channel=None):
    """Remove only the active attempt worktree; keep archived attempt evidence."""
    if not channel:
        item = store.get_job(job_id) or {}
        channel = item.get("channel")
    if not channel:
        return
    run_dir = store.data_dir / "runs" / job_id / channel
    if run_dir.is_dir():
        shutil.rmtree(run_dir)


def promote_usable_failed_runs():
    """Recover validated non-empty artifacts from legacy failed jobs."""
    promoted = []
    for item in store.list_failed_jobs():
        if not item.get("channel"):
            continue
        run_dir = store.data_dir / "runs" / item["id"] / item["channel"]
        report_path = run_dir / "validation_report.json"
        csv_path = run_dir / "reviews.csv"
        raw_path = run_dir / "reviews.raw.jsonl"
        if not (report_path.is_file() and csv_path.is_file() and raw_path.is_file()):
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            eligible = int(report.get("eligible_reviews", report.get("unique_reviews")) or 0)
            source_unique = int(
                report.get("source_unique_reviews", report.get("unique_reviews"))
                or 0
            )
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = sum(1 for _ in csv.DictReader(handle))
            with raw_path.open("r", encoding="utf-8") as handle:
                raw_rows = sum(1 for line in handle if line.strip())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            source_unique <= 0
            or csv_rows != eligible
            or raw_rows != source_unique
        ):
            continue
        report["status"] = "PARTIAL_SUCCESS"
        notes = report.setdefault("notes", {})
        notes.setdefault(
            "stop_reason",
            item.get("error") or "legacy run stopped before source reconciliation",
        )
        notes["historical_status_recovered"] = True
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        store.finish_partial(
            item["id"],
            {item["channel"]: report},
            notes["stop_reason"],
        )
        promoted.append(item["id"])
    return promoted


@asynccontextmanager
async def lifespan(app):
    # Probe the optional product-master enrichment boundary for observability.
    # Collection workers and health remain available when that authority is
    # missing or offline; every new task preserves its source observation.
    product_master.readiness()
    # Startup may apply narrowly reviewed compatibility state, but it must not
    # infer or persist product metadata into historical NULL business fields.
    # Unknown product values remain a read-time UI/API presentation concern.
    for cancelled_job_id in store.delete_stale_cancelled():
        cleanup_run(cancelled_job_id)
    promote_usable_failed_runs()
    for item in store.list_all_jobs():
        report = next(
            (
                value
                for value in (item.get("result") or {}).values()
                if isinstance(value, dict) and value.get("product")
            ),
            {},
        )
        product = report.get("product") or {}
        evidence_url = item.get("target_url") or product.get("url") or ""
        if (
            evidence_url
            and (item.get("market_region") or "Other") == "Other"
        ):
            inferred_region = infer_market_region(evidence_url)
            if inferred_region != "Other":
                store.update_job(
                    item["id"], market_region=inferred_region
                )
                item["market_region"] = inferred_region
    tasks = [
        asyncio.create_task(worker_loop(channel))
        for channel in COLLECTION_CHANNELS
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="VoxQuarry Data Collection", lifespan=lifespan)
MAX_EXTERNAL_IMPORT_BYTES = 250 * 1024 * 1024
MAX_BULK_JOB_ITEMS = 100
BULK_PREVIEW_SCHEMA_VERSION = "voxquarry.job-bulk-preview/v1"
BULK_CREATE_REQUEST_SCHEMA_VERSION = "voxquarry.job-bulk-create-request/v1"
BULK_CREATE_RESULT_SCHEMA_VERSION = "voxquarry.job-bulk-create-result/v1"


class JobRequest(BaseModel):
    action: str = "scrape"
    channel: str
    product_url: str | None = None
    brand: str | None = None
    model: str | None = None
    source_product_id: str | None = None
    category: str | None = None
    product_form_factor: str | None = None
    voltage: str | None = None
    energy_type: str | None = None
    market_region: str | None = None
    acquisition_batch_id: str | None = None
    product_master_model_id: str | None = None
    product_master_snapshot_id: str | None = None
    product_master_record_snapshot_hash: str | None = None
    confirm_duplicate: bool = False
    max_pages: int = Field(default=3, ge=1, le=10)
    max_reviews: int = Field(default=100, ge=1, le=500)
    timeout_seconds: int = Field(default=120, ge=10, le=300)


class BulkJobItem(JobRequest):
    line_no: int


class BulkJobPreviewRequest(BaseModel):
    schema_version: str = BULK_PREVIEW_SCHEMA_VERSION
    text: str


class BulkJobCreateRequest(BaseModel):
    schema_version: str = BULK_CREATE_REQUEST_SCHEMA_VERSION
    acquisition_batch_id: str | None = None
    items: list[BulkJobItem]


class RetryRequest(BaseModel):
    confirm_duplicate: bool = False
    acquisition_batch_id: str | None = None


class JobUpdate(BaseModel):
    brand: str | None = None
    model: str | None = None
    source_product_id: str | None = None
    category: str | None = None
    product_form_factor: str | None = None
    market_region: str | None = None
    priority: int | None = None
    voltage: str | None = None
    energy_type: str | None = None


class JobSummaryRequest(BaseModel):
    schema_version: str = "voxquarry.job-filter-summary-request/v1"
    job_ids: list[str]


class GenericImportCacheRef(BaseModel):
    item_id: str
    cache_id: str
    package_sha256: str


class GenericImportPlanRequest(BaseModel):
    schema_version: str = PLAN_REQUEST_SCHEMA_VERSION
    request_id: str
    cache_refs: list[GenericImportCacheRef]


class GenericImportConfirmRequest(BaseModel):
    schema_version: str = CONFIRM_SCHEMA_VERSION
    execution_request_id: str
    confirmation_id: str
    confirmation_scope_sha256: str
    item_ids: list[str]
    confirmed: bool
    second_confirmation: bool = False


class ResolveRequest(BaseModel):
    url: str


class SearchRequest(BaseModel):
    brand: str
    model: str
    channels: list[str] = []


def acquisition_batch_id(value=None):
    value = str(value or "").strip()
    if not value:
        return f"acq_{uuid.uuid4().hex[:24]}"
    if not re.fullmatch(r"acq_[0-9a-f]{24}", value):
        raise ValueError(
            "acquisition_batch_id 必须是 acq_ 开头的 24 位小写十六进制标识"
        )
    return value


class FeedbackRequest(BaseModel):
    author: str = "匿名"
    tag: str = "其他"
    content: str


class MasterDataRequest(BaseModel):
    name: str


class MasterDataStatusRequest(BaseModel):
    replacement_id: str | None = None


class MasterDataValidateRequest(BaseModel):
    type: str
    code: str | None = None
    name: str | None = None
    exclude_id: str | None = None


CHANNEL_HOSTS = {
    "lowes": ("lowes.com",),
    "homedepot": ("homedepot.com",),
    "ace": ("acehardware.com",),
    # EGO's North American and UK storefronts are both official product
    # sources.  The channel worker uses the reviewed PDP's own public
    # Bazaarvoice configuration, so regional hosts safely share one queue.
    "ego": ("egopowerplus.com", "egopowerplus.co.uk"),
    # RYOBI runs region-specific official storefronts.  Keep all recognised
    # subdomains in the same official channel so EU links use the same
    # server-side Bazaarvoice adapter and product metadata flow as North America.
    "ryobi": ("ryobitools.com", "ryobitools.eu"),
    "milwaukee": ("milwaukeetool.com",),
    "dewalt": ("dewalt.com",),
    "einhell": (
        "einhell.de", "einhell.co", "einhell.it", "einhell.fr",
        # UK PDPs publish an independent, public en_GB Bazaarvoice deployment.
        # The worker derives the product ID and requires PDP evidence to match.
        "einhell.co.uk",
    ),
    "harborfreight": ("harborfreight.com",),
    "tractorsupply": ("tractorsupply.com",),
    "skil": ("skil.com",),
    "amazon_reviews_2023": ("amazon.com",),
    "walmart": ("walmart.com",),
    "stihl": ("stihlusa.com",),
    "husqvarna": ("husqvarna.com",),
    "toro": ("toro.com",),
    "johndeere": ("deere.com",),
    "exmark": ("exmark.com",),
    "greenworks": ("greenworkstools.com",),
}
CHANNEL_BRANDS = {
    "ego": "EGO",
    "ryobi": "RYOBI",
    "milwaukee": "Milwaukee",
    "dewalt": "DEWALT",
    "einhell": "Einhell",
    "skil": "SKIL",
    "stihl": "STIHL",
    "husqvarna": "Husqvarna",
    "toro": "Toro",
    "johndeere": "John Deere",
    "exmark": "Exmark",
    "greenworks": "Greenworks",
}
CATEGORY_MARKERS = (
    ("Vacuum Accessory", ("vacuum attachment", "vacuum-attachment", "vacuum accessory", "vacuum-accessory", "vt2534")),
    ("Light Bulb", ("light bulb", "light-bulb", "bulb finder", "bulb-finder", "vintage bulb", "vintage-bulb")),
    ("Upright Vacuum", ("upright vacuum", "upright-vacuum")),
    ("Cordless Stick Vacuum", ("pblsv",)),
    ("Cordless Handheld Vacuum", ("handheld vacuum", "handheld-vacuum", "hand vacuum", "hand-vacuum", "dustbuster", "ionvac", "portable vacuum", "portable-vacuum", "dcv501", "33287201884")),
    ("Cordless Stick Vacuum", ("stick vacuum", "stick-vacuum", "stick vac", "stick-vac", "readyclean cordless", "readyclean-cordless", "pcl720", "5001795075")),
    ("Wet/Dry Vacuum", ("wet/dry vacuum", "wet-dry-vacuum", "wet dry vacuum", "wet/dry vac", "wet-dry-vac", "wet dry vac", "shop vacuum", "shop-vacuum", "shop vac", "shop-vac", "vac powerhead", "vac-powerhead", "quiet vac", "quiet-vac", "33287200375")),
    ("Hard Floor Cleaner", ("hard floor cleaner", "hard-floor-cleaner")),
    ("Spot Cleaner", ("spot cleaner", "spot-cleaner", "stain remover", "stain-remover")),
    ("Window Vacuum", ("window vacuum", "window-vacuum", "window vac", "window-vac")),
    ("Pool Vacuum", ("pool vacuum", "pool-vacuum", "pool vac", "pool-vac")),
    ("Ash Vacuum", ("ash vacuum", "ash-vacuum", "ash vac", "ash-vac")),
    ("Power Scrubber", ("power scrubber", "power-scrubber", "telescoping scrubber", "telescoping-scrubber")),
    ("Push Sweeper", ("push sweeper", "push-sweeper", "floor sweeper", "floor-sweeper")),
    ("Radio/Speaker", ("bluetooth speaker", "bluetooth-speaker", "cordless speaker", "cordless-speaker", "cordless radio", "cordless-radio")),
    ("LED Lighting", ("work light", "work-light", "task light", "task-light", "area light", "area-light", "project light", "project-light", "led light", "led-light")),
    ("Misting Fan", ("misting fan", "misting-fan", "misting stand fan", "misting-stand-fan")),
    ("Snow Shovel", ("snow shovel", "snow-shovel")),
    ("Snow Blower", ("snow blower", "snow-blower", "snow thrower", "snow-thrower")),
    ("Wheeled Blower", ("wheeled blower", "wheeled-blower")),
    ("Robot Mower", ("robot mower", "robot-mower", "robotic mower", "robotic-mower")),
    ("Riding Lawnmower", ("riding mower", "riding-mower", "ride-on mower", "ride on mower")),
    ("Tiller/Cultivator", ("tiller", "rototiller", "rotary tiller")),
    ("Cultivator", ("cultivator",)),
    ("Hedge Trimmer", ("hedge trimmer", "hedge-trimmer")),
    ("Edger", ("edger", "lawn edger", "lawn-edger")),
    ("Fan", ("jobsite-fan", "jobsite fan", "brushless-fan", "brushless fan", "cordless fan", "fan tool", "fan kit", "stand fan", "floor fan", "pedestal fan", "box fan", "misting fan", "clamp fan")),
    ("Heater", ("cordless heater", "cordless-heater", "propane heater", "propane-heater")),
    ("Generator", ("inverter generator", "inverter-generator", "portable generator", "portable-generator")),
    ("Inflator", ("inflator", "air-inflator", "air inflator", "tire inflator", "tire-inflator", "fvif51", "33287208968")),
    ("Air Compressor", ("air compressor", "air-compressor")),
    ("Pump", ("water pump", "water-pump", "transfer pump", "transfer-pump", "stick pump", "stick-pump", "dirt water pump", "clear water pump")),
    ("Patio Cleaner", ("patio cleaner", "patio-cleaner", "outdoor cleaner", "outdoor-cleaner")),
    ("Walk behind Lawnmower", ("mower", "lawn-mower", "lawn mower", "walk mower")),
    ("Leaf Blower/Vacuum", ("blower", "leaf-blower", "leaf blower", "handheld blower")),
    ("String Trimmer", ("trimmer", "string-trimmer", "string trimmer")),
    ("Chain Saw", ("chainsaw", "chain-saw", "chain saw")),
    (
        "Power Cleaner",
        (
            "ezclean", "dcpw550", "dcpw1000", "ry120350", "ry121850",
            "ry124052", "cordless power washers", "cordless-power-washers",
            "ry18pw22", "ry18pwx41", "4140135", "hypresso 18 24 1",
            "hypresso-18-24-1",
        ),
    ),
    (
        "Pressure Washer",
        (
            "pressure-washer", "pressure washer", "high pressure cleaner",
            "high-pressure-cleaner", "hydro cleaner", "hydro-cleaner",
            "hpw2105", "hpw3204", "4140160", "hypresso 36 105",
            "hypresso-36-105",
        ),
    ),
)
BRAND_MARKERS = (
    "EGO", "RYOBI", "Milwaukee", "DEWALT", "Kobalt", "Greenworks",
    "Toro", "Craftsman", "WORX", "STIHL", "BLACK+DECKER", "RIDGID",
    "Dyson", "Kenmore", "BISSELL", "Tzumi", "Lasko", "Westinghouse",
    "EcoSmart", "Feit Electric", "Commercial Electric", "Bucket Head",
    "Clorox", "30 Seconds", "GE", "Unbranded",
)
# Keep the generic RY prefix from mistaking the brand word "RYOBI" for a
# product model before it reaches a real identifier such as RY40HPLM01K2.
MODEL_PATTERN = re.compile(r"\b(?:LM[A-Z0-9-]{3,}|RY(?!OBI\b)[A-Z0-9-]{3,}|DC[A-Z0-9-]{3,}|ST[A-Z0-9-]{3,}|CS[A-Z0-9-]{3,}|PE[A-Z0-9-]{3,}|KMW[A-Z0-9-]{3,}|LMB[A-Z0-9-]{3,}|LB[A-Z0-9-]{3,}|HT[A-Z0-9-]{3,}|PH[A-Z0-9-]{3,}|Z6[A-Z0-9-]{3,}|\d{4}-\d{2})\b", re.I)
KOBALT_MODEL_PATTERN = re.compile(r"\bKLB\s?\d{4}[A-Z]?[- ]\d{2}\b", re.I)
PRODUCT_ID_PATTERN = re.compile(r"/(\d{8,})(?:[?#]|$)")

# A small, evidence-backed identity map makes selected pasted product links
# useful without making a network request. These values are the product's own
# public item/model fields, not guessed from a review title.
URL_PRODUCT_METADATA = {
    (
        "greenworks",
        "60v-3000-psi-2-0-gpm-electric-pressure-washer-tool-only-1",
    ): {
        "brand": "Greenworks",
        "model": "PW3000HYB",
        "category": "Pressure Washer",
        "voltage": "60V",
        "energy_type": "other",
        "product_title": (
            "60V/AC Hybrid 3000-PSI 2.0 GPM Electric Pressure Washer "
            "(Tool Only)"
        ),
    },
    (
        "greenworks",
        "60v-1800-psi-1-1-gpm-hybrid-ac-dc-pressure-washer",
    ): {
        "brand": "Greenworks",
        "model": "PW18HYB",
        "category": "Pressure Washer",
        "voltage": "60V",
        "energy_type": "other",
        "product_title": (
            "60V Hybrid 1800-PSI 1.1 GPM Electric Pressure Washer"
        ),
    },
    (
        "greenworks",
        "renewed-60v-hybrid-1800-psi-1-1-gpm-cold-water-pressure-washer-tool-only",
    ): {
        "brand": "Greenworks",
        "model": "PW18HYB",
        "category": "Pressure Washer",
        "voltage": "60V",
        "energy_type": "other",
        "product_title": (
            "OPEN BOX 60V Hybrid 1800 PSI 1.1 GPM Cold Water Pressure "
            "Washer (Tool Only)"
        ),
    },
    ("lowes", "1003171264"): {
        "brand": "EGO", "model": "LM2135SP", "category": "Walk-Behind Mower",
        "product_title": "EGO POWER+ 800 Series Select Cut with Touch Drive 21-in Self-Propelled Mower LM2135SP",
    },
    ("lowes", "5016893937"): {
        "brand": "EGO", "model": "LM2114SP-L", "category": "Walk-Behind Mower",
        "product_title": "EGO POWER+ 21-in Self-Propelled Mower LM2114SP-L",
    },
    ("lowes", "5016445291"): {
        "brand": "Kobalt", "model": "KLB 2024A-03", "category": "Leaf Blower",
        "product_title": "Kobalt QUIET TECH 24-volt 540-CFM 120-MPH Handheld Leaf Blower KLB 2024A-03",
    },
    ("ryobi", "46396056580"): {
        "brand": "RYOBI", "model": "RY40HPLM06B", "category": "Walk-Behind Mower",
        "product_title": "RYOBI 40V HP BRUSHLESS 20-inch PUSH MOWER RY40HPLM06B",
        "url": "https://www.ryobitools.com/products/46396056580",
    },
    ("ryobi", "46396054999"): {
        "brand": "RYOBI", "model": "RY40HPLM06K", "category": "Walk-Behind Mower",
        "product_title": "RYOBI 40V HP BRUSHLESS 20-inch PUSH MOWER KIT RY40HPLM06K",
        "url": "https://www.ryobitools.com/products/46396054999",
    },
}
GOOGLE_SEARCH_URL = "https://www.google.com/search"
OFFICIAL_SITEMAPS = {
    "ego": "https://egopowerplus.com/media/sitemap/sitemap_ego.xml",
    "ryobi": ("https://www.ryobitools.com/sitemap.xml", "https://uk.ryobitools.eu/sitemap.xml"),
    "skil": "https://www.skil.com/collections/outdoor-power-equipment/products.json?limit=250",
}
SEARCH_PATHS = {
    "lowes": "/pd/", "homedepot": "/p/", "ace": "/departments/",
    "ego": "/", "ryobi": "/products/", "milwaukee": "/products/details/",
    "dewalt": "/en-us/product/", "greenworks": "/products/",
}


def clean_text(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def product_id_from_url(url):
    parsed = urlsplit(str(url or ""))
    host = parsed.netloc.casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host == "greenworkstools.com"
        and len(parts) == 2
        and parts[0].casefold() == "products"
    ):
        return parts[1].casefold()
    match = PRODUCT_ID_PATTERN.search(url)
    return match.group(1) if match else ""


def infer_model(text):
    manufacturer = re.search(r"\b(?:mfr|model)\s*#?\s*([A-Z][A-Z0-9-]{4,})\b", text, re.I)
    if manufacturer:
        return manufacturer.group(1).upper()
    kobalt = KOBALT_MODEL_PATTERN.search(text)
    if kobalt:
        return re.sub(r"^(KLB)\s*(\d)", r"\1 \2", kobalt.group(0).upper())
    matches = MODEL_PATTERN.findall(text)
    # URLs can contain a model-family segment followed by the sellable SKU;
    # retain the most specific (longest) identifier, e.g. RY18PW22A-0.
    return max(matches, key=len).upper() if matches else ""


def resolve_product(url, hint_text=""):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(422, "请输入完整的 http(s) 商品链接")
    host = parsed.netloc.lower().removeprefix("www.")
    channel = next((name for name, domains in CHANNEL_HOSTS.items() if any(host == domain or host.endswith(f".{domain}") for domain in domains)), None)
    if not channel:
        raise HTTPException(
            422,
            "暂不支持该网站，请粘贴 Lowe's、Home Depot、Ace、EGO、RYOBI、"
            "Milwaukee、DEWALT、Walmart、STIHL、Husqvarna、Toro、"
            "Greenworks、John Deere 或 Exmark 商品链接",
        )
    known = URL_PRODUCT_METADATA.get((channel, product_id_from_url(url)), {})
    if not known:
        known = reviewed_product_metadata(channel, url) or {}
    # Pasted-link resolution is an interactive metadata hint, not collection.
    # Keep it deterministic and local: retailer PDP requests can block for tens
    # of seconds or return anti-bot pages, and task creation validates the same
    # domain/channel identity again without needing remote HTML.
    raw_path_text = unquote(f"{parsed.path} {parsed.query}")
    decoded = raw_path_text.replace("-", " ").replace("_", " ")
    source_text = f"{decoded} {hint_text}".strip()
    lower = source_text.lower()
    brand = known.get("brand") or CHANNEL_BRANDS.get(channel)
    if not brand:
        brand = next((item for item in BRAND_MARKERS if item.lower() in lower), None)
    inferred_category = known.get("category") or next(
        (
            name
            for name, markers in CATEGORY_MARKERS
            if any(marker in lower for marker in markers)
        ),
        None,
    )
    category = canonical_category_name(
        inferred_category or "Unknown Category"
    )
    # Keep the raw URL path for model extraction: European RYOBI bare-tool
    # suffixes (for example RY18PW22A-0) are semantically meaningful.
    model = known.get("model") or infer_model(f"{raw_path_text} {hint_text}") or infer_model(source_text)
    title = clean_text(hint_text) or known.get("product_title") or " ".join(part for part in decoded.split("/") if part).strip()[:160]
    voltage = extract_voltage(
        known.get("voltage"), raw_path_text, hint_text, title,
    )
    energy_type = known.get("energy_type") or "unknown"
    if energy_type == "unknown":
        if (
            re.search(r"\bhybrid\b", lower)
            and re.search(r"\bac\b", lower)
            and re.search(r"\bdc\b", lower)
        ):
            energy_type = "other"
        elif re.search(r"\b(?:gas|gasoline|petrol|diesel)\b", lower):
            energy_type = "fuel"
        elif re.search(r"\b(?:cordless|battery(?:[- ]powered)?|dc)\b", lower):
            energy_type = "battery"
        elif re.search(r"\b(?:corded|power cord|ac powered)\b", lower):
            energy_type = "corded"
    metadata = normalize_product_metadata(
        target_url=url,
        brand=brand,
        model=model,
        category=category,
        voltage=voltage,
        energy_type=energy_type,
        market_region=infer_market_region(url),
        product_title=title,
    )
    return {
        "channel": channel,
        "brand": metadata["brand"] or "",
        "model": metadata["model"] or "",
        "category": metadata["category"] or "",
        "voltage": metadata["voltage"],
        "energy_type": metadata["energy_type"],
        "market_region": metadata["market_region"],
        "product_title": title,
        "confidence": (
            "high"
            if brand and category != "Unknown Category" and model
            else "medium"
            if brand or category != "Unknown Category"
            else "low"
        ),
    }


def canonical_result_url(url):
    parsed = urlsplit(html.unescape(url))
    if parsed.path == "/url":
        query = parse_qs(parsed.query)
        return (query.get("q") or query.get("url") or [""])[0]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def resolve_task_product(url, source=None):
    hints = resolve_product(url)
    if source is None:
        source = metadata_client.get(hints['channel'], url)
    error = source.get('source_metadata_error') or {}
    if error.get('code') in {'source_identity_mismatch', 'source_metadata_conflict'}:
        raise HTTPException(422, error)
    return merge_source_metadata(hints, source)


def google_product_links(query, channel, limit=6):
    """Return product links from Google's public result page.

    This is deliberately a discovery aid, not a review-data source.  Each link
    is subsequently submitted to its channel adapter, which records source
    totals and collection limitations independently.
    """
    response = requests.get(
        GOOGLE_SEARCH_URL, params={"q": query, "num": str(max(10, limit * 2)), "hl": "en"},
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36"},
        timeout=20,
    )
    response.raise_for_status()
    expected_hosts = CHANNEL_HOSTS[channel]
    expected_path = SEARCH_PATHS[channel]
    items, seen = [], set()
    for anchor in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', response.text, re.S | re.I):
        url = canonical_result_url(anchor.group(1))
        parsed = urlsplit(url)
        host = parsed.netloc.lower().removeprefix("www.")
        if not parsed.scheme.startswith("http") or not any(host == domain or host.endswith(f".{domain}") for domain in expected_hosts):
            continue
        if expected_path != "/" and expected_path not in parsed.path:
            continue
        if "/reviews/" in parsed.path:
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if url in seen:
            continue
        title = clean_text(anchor.group(2))
        if not title:
            continue
        seen.add(url)
        items.append({"url": url, "title": title})
        if len(items) >= limit:
            break
    return items


def official_sitemap_links(channel, model, limit=6):
    """Find exact model URLs from an official brand sitemap, without scraping a UI."""
    roots = OFFICIAL_SITEMAPS.get(channel)
    if not roots or not model:
        return []
    if isinstance(roots, str):
        roots = (roots,)
    urls = []
    for root in roots:
        response = requests.get(root, timeout=25)
        response.raise_for_status()
        root_urls = re.findall(r"<loc>(.*?)</loc>", response.text, re.I)
        nested = [url for url in root_urls if "sitemap" in url.lower()][:80]
        if nested:
            root_urls = []
            for url in nested:
                try:
                    root_urls.extend(re.findall(r"<loc>(.*?)</loc>", requests.get(url, timeout=20).text, re.I))
                except requests.RequestException:
                    continue
        urls.extend(root_urls)
    needle = model.lower().replace("-", "")
    return [urlunsplit((*urlsplit(url)[:3], "", "")) for url in urls if needle in url.lower().replace("-", "")][:limit]


def known_product_links(brand, model, selected):
    needle = model.lower().replace("-", "").replace(" ", "")
    items = []
    for (channel, product_id), metadata in URL_PRODUCT_METADATA.items():
        if channel not in selected:
            continue
        if brand and metadata["brand"].lower() != brand.lower():
            continue
        if needle and needle != metadata["model"].lower().replace("-", "").replace(" ", ""):
            continue
        items.append({
            "channel": channel,
            "url": metadata.get("url") or f"https://www.lowes.com/pd/product/{product_id}",
            "product_title": metadata["product_title"],
            "brand": metadata["brand"], "model": metadata["model"], "category": metadata["category"],
            "product_type": "single", "reported_total": None,
            "discovery_source": "verified product identity map",
        })
    return items


def historical_product_links(brand, model, selected):
    """Reuse an already verified product URL when the requested model exists in this platform's evidence store."""
    needle = model.lower().replace("-", "").replace(" ", "")
    items = []
    for job in store.iter_jobs():
        if job.get("channel") not in selected or not job.get("target_url"):
            continue
        report = next((value for value in (job.get("result") or {}).values() if isinstance(value, dict) and value.get("product")), {})
        product = report.get("product") or {}
        inferred = resolve_product(job["target_url"], product.get("title") or "")
        candidate_brand = job.get("brand") or inferred["brand"]
        candidate_model = job.get("model") or inferred["model"]
        if brand and (candidate_brand or "").lower() != brand.lower():
            continue
        if needle and needle != (candidate_model or "").lower().replace("-", "").replace(" ", ""):
            continue
        items.append({
            "channel": job["channel"], "url": job["target_url"],
            "product_title": product.get("title") or inferred["product_title"],
            "brand": candidate_brand, "model": candidate_model,
            "category": job.get("category") or inferred["category"],
            "product_type": "single", "reported_total": product.get("total"),
            "discovery_source": "platform verified product history",
        })
    return items


def search_products(brand, model, channels):
    brand, model = brand.strip(), model.strip()
    if not brand and not model:
        raise HTTPException(422, "请至少填写品牌或产品型号")
    selected = channels or list(CHANNELS)
    invalid = [channel for channel in selected if channel not in CHANNELS]
    if invalid:
        raise HTTPException(422, f"不支持的渠道：{', '.join(invalid)}")
    candidates = known_product_links(brand, model, selected) + historical_product_links(brand, model, selected)
    errors = []
    for channel in selected:
        if channel in OFFICIAL_SITEMAPS:
            try:
                for url in official_sitemap_links(channel, model):
                    inferred = resolve_product(url, f"{brand} {model}")
                    candidates.append({
                        "channel": channel, "url": url,
                        "product_title": f"{inferred['brand'] or brand} {inferred['model'] or model}",
                        "brand": inferred["brand"] or brand, "model": inferred["model"] or model,
                        "category": inferred["category"], "product_type": "single",
                        "reported_total": None, "discovery_source": "official sitemap",
                    })
            except requests.RequestException as exc:
                errors.append(f"{channel}: 官网目录暂不可用（{str(exc)[:80]}）")
        domain = CHANNEL_HOSTS[channel][0]
        query = f'site:{domain}{SEARCH_PATHS[channel]} "{brand}" "{model}"'
        # The official sitemap is authoritative.  Avoid the public-search
        # fallback for that channel when it already yielded an exact product.
        if any(item["channel"] == channel for item in candidates):
            continue
        try:
            links = google_product_links(query, channel)
        except requests.RequestException as exc:
            errors.append(f"{channel}: 搜索暂不可用（{str(exc)[:80]}）")
            continue
        for item in links:
            inferred = resolve_product(item["url"], f"{brand} {model} {item['title']}")
            # Exact model matches are preferred.  Brand-only search is allowed
            # and returns the real channel product pages that the index found.
            if model and model.lower().replace("-", "") not in f"{item['url']} {item['title']}".lower().replace("-", ""):
                continue
            candidates.append({
                "channel": channel, "url": item["url"], "product_title": item["title"],
                "brand": inferred["brand"] or brand, "model": inferred["model"] or model,
                "category": inferred["category"],
                "product_type": "bundle" if re.search(r"\b(bundle|combo|kit)\b", item["title"], re.I) else "single",
                "reported_total": None, "discovery_source": "Google public product index",
            })
    deduped, seen = [], set()
    for candidate in candidates:
        candidate.setdefault(
            "market_region",
            infer_market_region(candidate.get("url") or ""),
        )
        key = f"{candidate['channel']}:{product_id_from_url(candidate['url']) or candidate['url']}"
        if key not in seen:
            seen.add(key); deduped.append(candidate)
    message = f"已找到 {len(deduped)} 个真实商品候选项，请选择后创建采集任务。"
    if not deduped and errors:
        message = "未找到可用候选项；" + "；".join(errors)
    elif errors:
        message += " 部分渠道暂不可用：" + "；".join(errors)
    return {"candidates": deduped, "message": message, "manual_url_channels": []}


@app.get("/health")
def health(response: Response):
    product_master_readiness = product_master.readiness()
    response.status_code = 200
    return {
        "ok": True,
        "channels": list(CHANNELS),
        "collection_channels": list(COLLECTION_CHANNELS),
        "external_import_channels": sorted(EXTERNAL_IMPORT_CHANNELS),
        "product_master": {
            **product_master_readiness,
            "role": "optional_enrichment",
            "admission_policy": "collection-independent/product-master-optional",
        },
    }


def _product_observation(req, inferred, *, evidence_source="request_or_url"):
    def selected(name):
        requested = getattr(req, name, None)
        return requested if requested is not None else inferred.get(name)

    metadata = normalize_product_metadata(
        target_url=getattr(req, "product_url", None) or "",
        brand=selected("brand"),
        model=selected("model"),
        category=selected("category"),
        voltage=selected("voltage"),
        energy_type=selected("energy_type"),
        market_region=selected("market_region"),
        product_title=inferred.get("product_title"),
        product_form_factor=selected("product_form_factor"),
        product_form_evidence_source="task_form_manual" if getattr(req, "product_form_factor", None) else "source_product_page",
    )
    from .source_metadata_client import preserve_source_gaps
    manual = [name for name in ('brand', 'model', 'category', 'energy_type', 'voltage')
              if getattr(req, name, None) is not None and getattr(req, name) != inferred.get(name)]
    preserve_source_gaps(metadata, inferred, manual)
    observation = build_product_observation(
        brand=metadata["brand"],
        model=metadata["model"],
        category=metadata["category"],
        voltage=metadata["voltage"],
        voltage_raw=metadata["voltage"],
        energy_type=metadata["energy_type"],
        evidence_source=evidence_source,
    )
    observation["product_form"] = metadata["product_form"]
    if inferred.get('source_metadata_evidence'):
        observation['source_metadata_evidence'] = inferred['source_metadata_evidence']
        observation['evidence_source'] = 'source_product_page'
    if inferred.get('source_metadata_error'):
        observation['source_metadata_error'] = inferred['source_metadata_error']
    # A browser bulk request contains the previewed fields. Equal values are
    # still source facts, not manual confirmation of a different identity.
    if manual:
        observation['manual_fields'] = manual
    return observation


def _optional_product_context(
    observation, enrichment, req=None, *, require_complete=False,
):
    enrichment = dict(enrichment or {})
    binding = enrichment.get("binding")
    record = (
        binding.get("record")
        if isinstance(binding, dict)
        and isinstance(binding.get("record"), dict)
        else None
    )
    quality_flags = list(enrichment.get("quality_flags") or [])
    source_evidence = observation.get('source_metadata_evidence') or {}
    quality_flags.extend('source_metadata_missing_' + field for field in source_evidence.get('gaps', []))
    if observation.get('source_metadata_error'):
        quality_flags.append(observation['source_metadata_error']['code'])
    if req is not None and binding:
        expected = {
            "product_master_model_id": binding.get("model_id"),
            "product_master_snapshot_id": binding.get("snapshot_id"),
            "product_master_record_snapshot_hash": binding.get("record_snapshot_hash"),
        }
        for field, current in expected.items():
            supplied = getattr(req, field, None)
            if supplied and supplied != current:
                quality_flags.append(f"{field}_changed")
    energy_observation = dict(observation)
    if record:
        # An old master record cannot silently overwrite a freshly proved PDP.
        record_fields = {
            'brand': (record.get('brand') or {}).get('canonical_name'),
            'model': record.get('model_number'),
            'category': (record.get('category') or {}).get('name'),
            'energy_type': normalize_energy_type(record.get('energy_type')),
            'voltage': (record.get('voltage_platform') or {}).get('canonical_voltage'),
        }
        for field, authority in record_fields.items():
            proof = source_evidence.get('fields', {}).get(field) or {}
            actual = proof.get('value')
            if actual and authority and authority != 'unknown' and str(actual).strip().casefold() != str(authority).strip().casefold():
                raise ValueError(f'商品页的{field}与已维护产品记录不一致，请核对精确商品')
        energy_observation.update({
            "brand": (record.get("brand") or {}).get("canonical_name"),
            "model": record.get("model_number"),
            "category": (record.get("category") or {}).get("name"),
        })
        if normalize_energy_type(observation.get("energy_type")) == "unknown":
            energy_observation["energy_type"] = normalize_energy_type(
                record.get("energy_type")
            )
        authority_voltage = (
            (record.get("voltage_platform") or {}).get("canonical_voltage")
            if isinstance(record.get("voltage_platform"), dict)
            else None
        )
        if not energy_observation.get("normalized_voltage") and authority_voltage:
            energy_observation["voltage_raw"] = authority_voltage
            energy_observation["normalized_voltage"] = authority_voltage
        energy_observation["evidence_source"] = "product_master_exact_match"
    platform = build_voltage_platform(energy_observation, binding)
    task_energy = build_task_energy(energy_observation, platform)
    quality_flags.extend(platform.get("quality_flags") or [])
    if record:
        brand = record["brand"]["canonical_name"]
        model = record["model_number"]
        category = record["category"]["name"]
    else:
        brand = observation.get("brand")
        model = observation.get("model")
        category = observation.get("category")
    result = {
        "brand": brand,
        "model": model,
        "category": category,
        "binding": binding or {},
        "enrichment_status": enrichment.get("status") or "unknown",
        "quality_flags": sorted(set(quality_flags)),
        "observation": energy_observation,
        "voltage_platform": platform,
        "task_energy": task_energy,
    }
    if require_complete:
        require_governed_metadata({
            "brand": brand,
            "model": model,
            "category": category,
            "market_region": getattr(req, "market_region", None) if req else None,
            "energy_type": task_energy["energy_type"],
            "voltage": task_energy["canonical_voltage"],
        })
    return result


def _create_job(req: JobRequest, *, batch_id_override=None, research_operation=None):
    if req.action not in {"discover", "scrape"}:
        raise HTTPException(400, "action must be discover or scrape")
    if req.channel not in CHANNELS:
        raise HTTPException(400, f"unknown channel: {req.channel}")
    if req.action == "scrape" and not req.product_url:
        raise HTTPException(422, "scrape 任务需要商品链接")
    try:
        batch_id = acquisition_batch_id(
            batch_id_override
            if batch_id_override is not None
            else req.acquisition_batch_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    inferred = resolve_product(req.product_url) if req.product_url else {}
    if inferred and inferred["channel"] != req.channel:
        raise HTTPException(422, "所选渠道与链接域名不一致")
    duplicate = store.find_target(req.channel, req.product_url) if req.product_url else None
    if duplicate and research_operation is not None:
        from .bounded import save_limits
        save_limits(store, duplicate["id"], {k: getattr(req, k) for k in ("max_pages", "max_reviews", "timeout_seconds")})
        return store.queue_retry(
            duplicate["id"], RETRY_JOB_PRIORITY,
            acquisition_batch_id=batch_id, research_operation=research_operation,
        )
    if duplicate and duplicate["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(
            409,
            {
                "code": "duplicate_active",
                "message": "该链接已有正在等待或采集的任务，不能重复创建。",
                "existing_job_id": duplicate["id"],
                "existing_status": duplicate["status"],
                "target_key": canonical_target_url(req.product_url),
            },
        )
    if duplicate and not req.confirm_duplicate:
        raise HTTPException(
            409,
            {
                "code": "duplicate_target",
                "message": "该链接已有历史任务，是否确认在原任务上再次执行？",
                "existing_job_id": duplicate["id"],
                "existing_status": duplicate["status"],
                "target_key": canonical_target_url(req.product_url),
            },
        )
    if duplicate:
        from .bounded import save_limits
        save_limits(store, duplicate["id"], {k: getattr(req, k) for k in ("max_pages", "max_reviews", "timeout_seconds")})
        try:
            queued = store.queue_retry(
                duplicate["id"],
                RETRY_JOB_PRIORITY,
                action=req.action,
                acquisition_batch_id=batch_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": queued["id"],
            "status": "queued",
            "attempt_no": queued["attempt_count"],
            "reused_task": True,
            "acquisition_batch_id": batch_id,
            "execution_policy": (
                "bounded_public_collection" if req.channel in {"homedepot", "walmart"} else "server_collection"
            ),
            "energy_type": duplicate.get("energy_type"),
            "energy_readiness_status": duplicate.get("energy_readiness_status"),
            "metadata_incomplete": duplicate.get("energy_readiness_status") == "metadata_incomplete",
        }

    if req.product_url:
        inferred = resolve_task_product(req.product_url)

    # Source product identity is always system-derived. URL-deterministic
    # channels resolve it now; PDP/API-dependent channels remain pending until
    # the worker reads official evidence. A caller-supplied value is ignored.
    source_product_identity = source_product_identity_from_url(
        req.channel, req.product_url,
    )
    try:
        market_region = validate_channel_market_region(
            req.channel,
            (
                req.market_region
                if req.market_region is not None
                else inferred.get('market_region') or infer_market_region(req.product_url)
            ),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        observation = _product_observation(req, inferred)
        enrichment = (
            product_master.enrich_optional(
                brand=observation.get("brand"),
                model=observation.get("model"),
            )
            if req.action == "scrape"
            else {
                "status": "unknown",
                "binding": None,
                "quality_flags": ["product_identity_not_requested"],
            }
        )
        product_context = _optional_product_context(
            observation, enrichment, req=req, require_complete=req.action == "scrape",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    master_binding = product_context["binding"]
    brand = product_context["brand"]
    model = product_context["model"]
    category = product_context["category"]
    job_id = uuid.uuid4().hex[:12]
    stored_action = req.action
    from .bounded import save_limits
    save_limits(store, job_id, {k: getattr(req, k) for k in ("max_pages", "max_reviews", "timeout_seconds")})
    try:
        created = store.create_job(
            job_id,
            stored_action,
            req.channel,
            req.product_url,
            brand,
            model,
            category,
            market_region=market_region,
            source_product_identity=source_product_identity,
            acquisition_batch_id=batch_id,
            product_master_binding=master_binding,
            product_observation=product_context["observation"],
            product_enrichment_status=product_context["enrichment_status"],
            product_quality_flags=product_context["quality_flags"],
            voltage_platform=product_context["voltage_platform"],
            task_energy=product_context["task_energy"],
            research_operation=research_operation,
        )
        if research_operation is not None:
            if created.get("research_retry_job_id"):
                return store.queue_retry(
                    created["research_retry_job_id"], RETRY_JOB_PRIORITY,
                    acquisition_batch_id=batch_id,
                    research_operation=research_operation,
                )
            return created
    except TaskCatalogError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc
    except MasterDataConflict as exc:
        status = 503 if "recovery" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
    except MasterDataError as exc:
        raise HTTPException(
            422,
            {
                "code": "master_data_mismatch",
                "message": str(exc),
            },
        ) from exc
    except sqlite3.IntegrityError as exc:
        message = str(exc).casefold()
        target_conflict = (
            "unique constraint failed" in message
            and "jobs.channel" in message
            and "jobs.target_key" in message
        )
        duplicate = (
            store.find_target(req.channel, req.product_url)
            if target_conflict
            else None
        )
        if duplicate:
            raise _duplicate_target_http_error(
                duplicate, req.product_url,
            ) from exc
        raise
    return {
        "id": job_id,
        "status": "queued",
        "acquisition_batch_id": batch_id,
        "execution_policy": (
            "external_import_only"
            if stored_action == EXTERNAL_IMPORT_ACTION
            else "server_collection"
        ),
        "source_product_identity_status": source_product_identity["status"],
        "energy_type": product_context["task_energy"]["energy_type"],
        "energy_readiness_status": product_context["task_energy"]["readiness_status"],
        "metadata_incomplete": product_context["task_energy"]["readiness_status"] != "ready",
    }


@app.post("/api/jobs")
def create_job(req: JobRequest):
    return _create_job(req)


def _bulk_http_error(exc):
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or "request_rejected")
        message = str(detail.get("message") or code)
        extra = {
            key: value
            for key, value in detail.items()
            if key not in {"code", "message"}
        }
    else:
        code = "request_rejected"
        message = str(detail or "请求被拒绝")
        extra = {}
    return {
        "http_status": int(exc.status_code),
        "code": code,
        "message": message,
        **extra,
    }


def _duplicate_target_http_error(duplicate, target_url):
    active = duplicate.get("status") in {"queued", "running", "cancelling"}
    return HTTPException(409, {
        "code": "duplicate_active" if active else "duplicate_target",
        "message": (
            "该链接已有正在等待或采集的任务，不能重复创建。"
            if active
            else "该链接已有历史任务，请重新预览并确认后再执行。"
        ),
        "existing_job_id": duplicate["id"],
        "existing_status": duplicate["status"],
        "target_key": canonical_target_url(target_url),
    })


def _bulk_input_lines(text):
    lines = [
        (line_no, line.strip())
        for line_no, line in enumerate(str(text or "").splitlines(), start=1)
        if line.strip()
    ]
    if not lines:
        raise HTTPException(422, {
            "code": "bulk_input_empty",
            "message": "请粘贴至少一个商品链接，每行一个 URL。",
        })
    if len(lines) > MAX_BULK_JOB_ITEMS:
        raise HTTPException(422, {
            "code": "bulk_limit_exceeded",
            "message": f"单批最多支持 {MAX_BULK_JOB_ITEMS} 个非空链接。",
            "max_items": MAX_BULK_JOB_ITEMS,
            "received_items": len(lines),
        })
    return lines


def _validate_bulk_url(value):
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HTTPException(422, {
            "code": "invalid_product_url",
            "message": "必须提供不含账号凭据的完整 HTTP(S) 商品链接。",
        })
    return canonical_target_url(value)


def _product_master_projection(binding):
    record = binding["record"]
    return {
        "binding_status": "bound",
        "schema_version": binding["schema_version"],
        "snapshot_id": binding["snapshot_id"],
        "model_id": binding["model_id"],
        "entity_version": binding["entity_version"],
        "record_snapshot_hash": binding["record_snapshot_hash"],
        "brand_id": record["brand"]["brand_id"],
        "category_id": record["category"]["category_id"],
        "category_code": record["category"]["code"],
        "energy_type": record["energy_type"],
        "voltage_platform": record["voltage_platform"],
        "voltage_platform_null_reason": record["voltage_platform_null_reason"],
        "product_kind": record["product_kind"],
        "bundle_scope": (record.get("bundle") or {}).get("scope"),
        "analysis_eligibility": record["analysis_eligibility"],
    }


def _optional_enrichment_projection(product_context):
    binding = product_context.get("binding") or {}
    if binding:
        projection = _product_master_projection(binding)
    else:
        projection = {"binding_status": "unbound"}
    return {
        **projection,
        "association_status": product_context.get("enrichment_status") or "unknown",
        "quality_flags": product_context.get("quality_flags") or [],
    }


def _stored_product_master_projection(existing):
    if existing.get("product_master_binding_status") != "bound":
        return {
            "binding_status": (
                existing.get("product_master_binding_status") or "legacy_unbound"
            ),
            "association_status": (
                existing.get("product_enrichment_status") or "unknown"
            ),
            "quality_flags": existing.get("product_quality_flags") or [],
        }
    record = existing.get("product_master_record")
    if not isinstance(record, dict):
        return {
            "binding_status": "bound",
            "schema_version": existing.get("product_master_schema_version"),
            "snapshot_id": existing.get("product_master_snapshot_id"),
            "model_id": existing.get("product_master_model_id"),
            "entity_version": existing.get("product_master_entity_version"),
            "record_snapshot_hash": existing.get("product_master_record_snapshot_hash"),
        }
    projection = _product_master_projection({
        "schema_version": existing.get("product_master_schema_version"),
        "snapshot_id": existing.get("product_master_snapshot_id"),
        "model_id": existing.get("product_master_model_id"),
        "entity_version": existing.get("product_master_entity_version"),
        "record_snapshot_hash": existing.get("product_master_record_snapshot_hash"),
        "record": record,
    })
    projection.update({
        "association_status": (
            existing.get("product_enrichment_status") or "enriched"
        ),
        "quality_flags": existing.get("product_quality_flags") or [],
    })
    return projection


def _bulk_existing_preview_item(pending_item, existing):
    active = existing.get("status") in {"queued", "running", "cancelling"}
    product_master_projection = _stored_product_master_projection(existing)
    item = {
        "line_no": pending_item["line_no"],
        "input_url": pending_item["input_url"],
        "canonical_url": pending_item["canonical_url"],
        "status": "invalid" if active else "existing",
        "can_create": not active,
        "requires_duplicate_confirmation": not active,
        "request": {
            "action": "scrape",
            "channel": existing["channel"],
            "product_url": pending_item["canonical_url"],
            "brand": existing.get("brand"),
            "model": existing.get("model"),
            "source_product_id": existing.get("source_product_id"),
            "category": existing.get("category"),
            "market_region": existing.get("market_region"),
            "voltage": existing.get("energy_canonical_voltage"),
            "energy_type": existing.get("energy_type") or "unknown",
            "product_master_model_id": existing.get("product_master_model_id"),
            "product_master_snapshot_id": existing.get("product_master_snapshot_id"),
            "product_master_record_snapshot_hash": existing.get(
                "product_master_record_snapshot_hash"
            ),
        },
        "product_master": product_master_projection,
        "task_energy": (
            {
                "schema_version": existing.get("energy_schema_version"),
                "energy_type": existing.get("energy_type"),
                "readiness_status": existing.get("energy_readiness_status"),
                "evidence_source": existing.get("energy_evidence_source"),
                "canonical_voltage": existing.get("energy_canonical_voltage"),
                "unknown_reason": existing.get("energy_unknown_reason"),
            }
            if existing.get("energy_schema_version")
            else None
        ),
        "existing_job": {
            "id": existing["id"],
            "status": existing["status"],
            "active": active,
        },
    }
    if active:
        item["error"] = {
            "http_status": 409,
            "code": "duplicate_active",
            "message": "该链接已有正在等待或采集的任务，不能重复创建。",
            "existing_job_id": existing["id"],
        }
    return item


@app.post("/api/jobs/bulk/preview")
def preview_bulk_jobs(req: BulkJobPreviewRequest, response: Response):
    if req.schema_version != BULK_PREVIEW_SCHEMA_VERSION:
        raise HTTPException(422, {
            "code": "unsupported_bulk_preview_schema",
            "message": "不支持的批量链接预览契约版本。",
        })
    response.headers["Cache-Control"] = "no-store"
    lines = _bulk_input_lines(req.text)
    items = []
    seen = {}
    pending = []
    for line_no, input_url in lines:
        try:
            canonical_url = _validate_bulk_url(input_url)
        except HTTPException as exc:
            items.append({
                "line_no": line_no,
                "input_url": input_url,
                "canonical_url": None,
                "status": "invalid",
                "can_create": False,
                "error": _bulk_http_error(exc),
            })
            continue
        if canonical_url in seen:
            items.append({
                "line_no": line_no,
                "input_url": input_url,
                "canonical_url": canonical_url,
                "status": "duplicate_input",
                "can_create": False,
                "duplicate_of_line": seen[canonical_url],
                "error": {
                    "http_status": 409,
                    "code": "duplicate_input_url",
                    "message": f"与第 {seen[canonical_url]} 行是同一规范商品链接。",
                },
            })
            continue
        seen[canonical_url] = line_no
        try:
            inferred = resolve_product(canonical_url)
        except HTTPException as exc:
            items.append({
                "line_no": line_no,
                "input_url": input_url,
                "canonical_url": canonical_url,
                "status": "invalid",
                "can_create": False,
                "error": _bulk_http_error(exc),
            })
            continue
        pending_item = {
            "line_no": line_no,
            "input_url": input_url,
            "canonical_url": canonical_url,
            "inferred": inferred,
        }
        existing = store.find_target(inferred["channel"], canonical_url)
        if existing:
            items.append(_bulk_existing_preview_item(pending_item, existing))
            continue
        pending.append(pending_item)
    sources = metadata_client.get_many([
        (item['inferred']['channel'], item['canonical_url']) for item in pending
    ]) if pending else []
    ready_pending = []
    for item, source in zip(pending, sources):
        try:
            item['inferred'] = resolve_task_product(item['canonical_url'], source)
            ready_pending.append(item)
        except HTTPException as exc:
            items.append({**{key: item[key] for key in ('line_no', 'input_url', 'canonical_url')},
                          'status': 'invalid', 'can_create': False, 'error': _bulk_http_error(exc)})
    pending = ready_pending
    enrichments = product_master.enrich_optional_many([
        {
            "brand": item["inferred"].get("brand"),
            "model": item["inferred"].get("model"),
        }
        for item in pending
    ]) if pending else []
    catalog, catalog_error = None, None
    if pending:
        try:
            catalog = store.task_metadata_catalog()
        except TaskCatalogError as exc:
            catalog_error = exc
    for pending_item, enrichment in zip(pending, enrichments):
        line_no = pending_item["line_no"]
        input_url = pending_item["input_url"]
        canonical_url = pending_item["canonical_url"]
        try:
            if catalog_error:
                raise catalog_error
            observation = _product_observation(
                JobRequest(channel=pending_item['inferred']['channel'], product_url=canonical_url),
                pending_item['inferred'], evidence_source='product_url',
            )
            product_context = _optional_product_context(
                observation, enrichment, require_complete=True,
            )
            store.validate_task_metadata(
                'scrape', product_context['brand'],
                product_observation=product_context['observation'],
                voltage_platform=product_context['voltage_platform'],
                task_energy=product_context['task_energy'],
                catalog=catalog,
            )
        except ValueError as exc:
            items.append({
                "line_no": line_no,
                "input_url": input_url,
                "canonical_url": canonical_url,
                "status": "invalid",
                "can_create": False,
                "error": {
                    "http_status": getattr(exc, 'status_code', 422),
                    "code": getattr(exc, 'code', 'product_metadata_incomplete'),
                    "message": str(exc),
                },
            })
            continue
        resolved = {
            **pending_item["inferred"],
            "brand": product_context["brand"],
            "model": product_context["model"],
            "category": product_context["category"],
        }
        product_master_projection = _optional_enrichment_projection(
            product_context,
        )
        existing = store.find_target(resolved["channel"], canonical_url)
        active = bool(existing and existing.get("status") in {
            "queued", "running", "cancelling",
        })
        status = "existing" if existing else "ready"
        can_create = not active
        item = {
            "line_no": line_no,
            "input_url": input_url,
            "canonical_url": canonical_url,
            "status": status,
            "can_create": can_create,
            "requires_duplicate_confirmation": bool(existing and not active),
            "request": {
                "action": "scrape",
                "channel": resolved["channel"],
                "product_url": canonical_url,
                "brand": resolved["brand"],
                "model": resolved["model"],
                "source_product_id": None,
                "category": resolved["category"],
                "voltage": observation.get("normalized_voltage"),
                "energy_type": observation.get("energy_type"),
                "market_region": resolved["market_region"],
                "product_master_model_id": product_context["binding"].get("model_id"),
                "product_master_snapshot_id": product_context["binding"].get("snapshot_id"),
                "product_master_record_snapshot_hash": product_context["binding"].get("record_snapshot_hash"),
            },
            "product_master": product_master_projection,
            "product_observation": observation,
            "source_product_identity": source_product_identity_from_url(
                resolved["channel"], canonical_url,
            ),
            "voltage_platform": product_context["voltage_platform"],
            "task_energy": product_context["task_energy"],
            "existing_job": (
                {
                    "id": existing["id"],
                    "status": existing["status"],
                    "active": active,
                }
                if existing
                else None
            ),
        }
        if active:
            item["status"] = "invalid"
            item["error"] = {
                "http_status": 409,
                "code": "duplicate_active",
                "message": "该链接已有正在等待或采集的任务，不能重复创建。",
                "existing_job_id": existing["id"],
            }
        items.append(item)
    items.sort(key=lambda item: item["line_no"])
    counts = {
        "input": len(lines),
        "ready": sum(item["status"] == "ready" for item in items),
        "existing": sum(item["status"] == "existing" for item in items),
        "duplicates": sum(item["status"] == "duplicate_input" for item in items),
        "invalid": sum(item["status"] == "invalid" for item in items),
    }
    return {
        "schema_version": BULK_PREVIEW_SCHEMA_VERSION,
        "max_items": MAX_BULK_JOB_ITEMS,
        "counts": counts,
        "items": items,
    }


@app.post("/api/jobs/bulk")
def create_bulk_jobs(req: BulkJobCreateRequest, response: Response):
    if req.schema_version != BULK_CREATE_REQUEST_SCHEMA_VERSION:
        raise HTTPException(422, {
            "code": "unsupported_bulk_create_schema",
            "message": "不支持的批量任务创建契约版本。",
        })
    items = list(req.items or [])
    if not items:
        raise HTTPException(422, {
            "code": "bulk_input_empty",
            "message": "批量创建至少需要一个已预览链接。",
        })
    if len(items) > MAX_BULK_JOB_ITEMS:
        raise HTTPException(422, {
            "code": "bulk_limit_exceeded",
            "message": f"单批最多支持 {MAX_BULK_JOB_ITEMS} 个任务。",
            "max_items": MAX_BULK_JOB_ITEMS,
            "received_items": len(items),
        })
    line_numbers = [getattr(item, "line_no", None) for item in items]
    if (
        any(not isinstance(line_no, int) or line_no < 1 for line_no in line_numbers)
        or len(set(line_numbers)) != len(line_numbers)
    ):
        raise HTTPException(422, {
            "code": "bulk_line_identity_invalid",
            "message": "批量任务的 line_no 必须是唯一正整数。",
        })
    try:
        batch_id = acquisition_batch_id(req.acquisition_batch_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    results = []
    for item in items:
        input_url = str(item.product_url or "")
        canonical_url = None
        try:
            if item.action != "scrape":
                raise HTTPException(422, {
                    "code": "bulk_action_invalid",
                    "message": "批量链接任务只允许 action=scrape；discover 必须使用独立发现流程。",
                })
            canonical_url = _validate_bulk_url(input_url)
            item.product_url = canonical_url
            created = _create_job(item, batch_id_override=batch_id)
            reused = bool(created.get("reused_task"))
            results.append({
                "line_no": item.line_no,
                "input_url": input_url,
                "canonical_url": canonical_url,
                "status": "reused" if reused else "created",
                "job_id": created["id"],
                "attempt_no": created.get("attempt_no", 1),
                "execution_policy": created.get("execution_policy"),
                "energy_type": created.get("energy_type"),
                "energy_readiness_status": created.get("energy_readiness_status"),
                "metadata_incomplete": bool(created.get("metadata_incomplete")),
                "error": None,
            })
        except HTTPException as exc:
            results.append({
                "line_no": item.line_no,
                "input_url": input_url,
                "canonical_url": canonical_url,
                "status": "failed",
                "job_id": None,
                "attempt_no": None,
                "execution_policy": None,
                "error": _bulk_http_error(exc),
            })
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": BULK_CREATE_RESULT_SCHEMA_VERSION,
        "acquisition_batch_id": batch_id,
        "requested_count": len(results),
        "created_count": sum(item["status"] == "created" for item in results),
        "reused_count": sum(item["status"] == "reused" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


def _task_view_catalog():
    return (store.remote_master_data.snapshot() if store.remote_master_data
            else _shared_master_data().snapshot())


@app.get("/api/jobs/view")
def task_view(response: Response, page: int = 1, limit: int = 30, filters: str = "{}"):
    try:
        if len(filters) > 4096:
            raise ValueError("task filters too large")
        result = task_views.page(page, limit, json.loads(filters), _task_view_catalog())
    except ValueError as exc:
        raise HTTPException(409 if 'scope changed' in str(exc) else 422, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/jobs/queue-view")
def task_queue_view(response: Response):
    try:
        result = task_views.queue()
    except ValueError as exc:
        raise HTTPException(409, "task scope changed; retry") from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/jobs/view-summary")
def task_view_summary(response: Response, filters: str = "{}"):
    try:
        if len(filters) > 4096:
            raise ValueError("task filters too large")
        result = task_views.summary(json.loads(filters), _task_view_catalog())
    except ValueError as exc:
        raise HTTPException(409 if 'scope changed' in str(exc) else 422, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/jobs")
def jobs(
    response: Response,
    limit: int = JOBS_PAGE_DEFAULT_LIMIT,
    cursor: str | None = None,
):
    try:
        page = store.list_jobs_page(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return page


@app.post("/api/jobs/summary")
def job_summary(req: JobSummaryRequest, response: Response):
    if req.schema_version != "voxquarry.job-filter-summary-request/v1":
        raise HTTPException(422, "unsupported job filter summary request schema")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(job_id)) for job_id in req.job_ids):
        raise HTTPException(422, "job_ids contains an invalid stable task ID")
    try:
        result = store.summarize_job_scope(req.job_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/acquisition-batches/readiness")
def acquisition_batches_readiness(response: Response):
    """Read-only batch settlement contract for the reporting scheduler."""
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": "voxquarry.acquisition-batch-readiness/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settle_seconds": 600,
        "batches": store.acquisition_batch_readiness(settle_seconds=600),
    }


@app.get("/api/jobs/exports/homedepot-pending")
def export_homedepot_pending_jobs():
    """Export every cache-only Home Depot task and the governed import spec."""
    tasks = store.list_external_import_queue("homedepot")
    exported_at = datetime.now(timezone.utc).isoformat()
    requirements = external_import_format_requirements()

    def export_task(item):
        source_product_id = (
            item.get("source_product_id")
            or product_id_from_url(item.get("target_url") or "")
        )
        review_template = dict(requirements["review_record_template"])
        review_template.update(
            {
                "product_id": source_product_id,
                "source_product_id": source_product_id,
                "product_url": item["target_url"],
            }
        )
        return {
            "task_id": item["id"],
            "action": item["action"],
            "status": item["status"],
            "target_url": item["target_url"],
            "canonical_target_key": item["target_key"],
            "source_product_id": source_product_id,
            "brand": item.get("brand"),
            "model": item.get("model"),
            "category": item.get("category"),
            "energy_type": item.get("energy_type"),
            "voltage": item.get("energy_canonical_voltage"),
            "voltage_platform_key": item.get("voltage_platform_key"),
            "market_region": item.get("market_region"),
            "created_at": item.get("created_at"),
            "acquisition_batch_id": item.get("acquisition_batch_id"),
            "import_match_rule": (
                f"Upload one {requirements['schema_version']} file whose channel "
                "and canonical target_url match this task. A valid import "
                "completes this stable task ID in place."
            ),
            "template_note": (
                "Illustrative one-row structure only. Replace import_id, title, "
                "collection evidence, source_total, and reviews with the complete "
                "source export; never upload this template unchanged."
            ),
            "import_bundle_template": {
                "schema_version": requirements["schema_version"],
                "import_id": f"homedepot-{item['id']}-REPLACE-WITH-UNIQUE-ID",
                "channel": "homedepot",
                "task_policy": "create_or_append_attempt",
                "target_url": item["target_url"],
                "market_region": item.get("market_region"),
                "product": {
                    "id": source_product_id,
                    "title": "REQUIRED_PRODUCT_TITLE",
                    "brand": item.get("brand"),
                    "model": item.get("model"),
                    "category": item.get("category"),
                    "energy_type": item.get("energy_type") or "unknown",
                    "voltage": item.get("energy_canonical_voltage"),
                },
                "completeness": {
                    "is_complete": True,
                    "source_total": 1,
                    "source_total_basis": "user_review_records",
                    "collected_at": exported_at,
                    "method": "manual_public_export",
                    "evidence_url": item["target_url"],
                    "syndication_origin_complete": True,
                },
                "reviews": [review_template],
            },
        }

    payload = {
        "schema_version": "voxquarry.homedepot-external-queue/v2",
        "exported_at": exported_at,
        "channel": "homedepot",
        "execution_policy": "external_import_only",
        "server_collection_enabled": False,
        "task_count": len(tasks),
        "tasks": [export_task(item) for item in tasks],
        "import_format_requirements": requirements,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    date = exported_at[:10].replace("-", "")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="voxquarry-homedepot-pending-{date}.json"'
            ),
            "Cache-Control": "no-store",
        },
    )


def _external_cache_export_scope(channels, task_ids):
    selected_channels = [item.strip() for item in str(channels or "").split(",") if item.strip()]
    selected_ids = [item.strip() for item in str(task_ids or "").split(",") if item.strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in selected_ids):
        raise HTTPException(422, "task_ids contains an invalid stable task ID")
    try:
        return build_external_cache_export_plan(
            store,
            channels=selected_channels,
            task_ids=selected_ids or None,
        )
    except ValueError as exc:
        raise HTTPException(
            409,
            {"code": "external_cache_export_blocked", "message": str(exc)},
        ) from exc


@app.get("/api/jobs/exports/external-cache/preview")
def preview_external_cache_export(
    response: Response,
    channels: str = "homedepot,walmart",
    task_ids: str | None = None,
):
    """Return bounded counts before generating a potentially large export."""
    plan = _external_cache_export_scope(channels, task_ids)
    try:
        response.headers["Cache-Control"] = "no-store"
        return {
            "schema_version": "voxquarry.external-review-cache-export-preview/v2",
            "logical_content_sha256": plan["logical_content_sha256"],
            "summary": plan["summary"],
            "tasks": [
                {key: value for key, value in task.items() if not key.startswith("_")}
                for task in plan["tasks"]
            ],
        }
    finally:
        release_external_cache_export_plan(plan)


@app.get("/api/jobs/exports/external-cache")
def export_external_cache(
    channels: str = "homedepot,walmart",
    task_ids: str | None = None,
):
    """Stream one channel-grouped JSON without loading all reviews in memory."""
    plan = _external_cache_export_scope(channels, task_ids)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        stream_external_cache_export(plan),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="voxquarry-external-cache-{date}.json"'
            ),
            "Cache-Control": "no-store",
            "X-VoxQuarry-Logical-Content-SHA256": plan["logical_content_sha256"],
        },
    )


def _generic_import_context(request):
    actor_id = str(request.headers.get("x-voxquarry-actor-id") or "")
    run_id = str(request.headers.get("x-voxquarry-run-id") or "")
    if not actor_id or not run_id:
        raise HTTPException(
            401,
            {
                "code": "generic_import_identity_required",
                "message": "trusted actor and run headers are required",
            },
        )
    return actor_id, run_id


def _generic_import_http_error(exc):
    return HTTPException(
        exc.http_status,
        {"code": exc.code, "message": exc.message},
    )


def _http_error_projection(exc):
    detail = exc.detail
    if isinstance(detail, dict):
        return {
            "code": str(detail.get("code") or "generic_import_dry_run_blocked"),
            "message": str(detail.get("message") or "package was blocked"),
        }
    return {
        "code": "generic_import_dry_run_blocked",
        "message": str(detail or "package was blocked"),
    }


class _CachedImportRequest:
    """Minimal Request-compatible, bounded file stream for adapter reuse."""

    def __init__(self, path, package_sha256, *, dry_run):
        self.path = Path(path)
        self.headers = {
            "content-type": "application/json",
            "content-length": str(self.path.stat().st_size),
            "x-voxquarry-expected-upload-sha256": package_sha256,
        }
        self.query_params = {"dry_run": "true"} if dry_run else {}

    async def stream(self):
        with self.path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk


@app.post("/api/imports/reviews/cache")
async def cache_external_review_import(request: Request, request_id: str):
    """Stream one local file into the actor-scoped governed cache after dry-run."""
    actor_id, run_id = _generic_import_context(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type not in {"application/json", "application/octet-stream"}:
        raise HTTPException(415, "请上传 JSON 格式的网评导入文件")
    try:
        staging_dir = await asyncio.to_thread(
            generic_import_registry.create_upload_staging
        )
    except GenericImportError as exc:
        raise _generic_import_http_error(exc) from exc
    upload_path = staging_dir / "upload.json"
    digest = hashlib.sha256()
    size = 0
    try:
        with open_private_binary_writer(upload_path) as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_EXTERNAL_IMPORT_BYTES:
                    raise HTTPException(413, "导入文件不能超过 250 MiB")
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(422, "导入文件不能为空")
        # Freeze auto-filled specifications into the cached package before its
        # dry-run hash is recorded. Confirmation never revisits a changing PDP.
        try:
            with upload_path.open('r', encoding='utf-8') as handle:
                payload = json.load(handle)
            product = payload.get('product') if isinstance(payload, dict) else None
            missing = isinstance(product, dict) and (
                any(product.get(field) in (None, '') for field in ('brand', 'category', 'energy_type'))
                or (product.get('energy_type') in (None, '', 'battery') and not product.get('voltage'))
                or not payload.get('market_region')
            )
            if (missing and not product.get('source_metadata_evidence')
                    and payload.get('schema_version') == 'voxquarry.review-import/v2'
                    and payload.get('channel') in METADATA_AUTOFILL_CHANNELS):
                source = await asyncio.to_thread(metadata_client.get, payload['channel'], payload.get('target_url') or '')
                enriched = enrich_import_bundle_metadata(payload, source)
                if enriched != payload:
                    enriched['product']['source_metadata_evidence']['original_upload_sha256'] = digest.hexdigest()
                    encoded = json.dumps(enriched, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                    if len(encoded) > MAX_EXTERNAL_IMPORT_BYTES:
                        raise HTTPException(413, '补全后的导入文件不能超过 250 MiB')
                    upload_path = staging_dir / 'enriched-upload.json'
                    with open_private_binary_writer(upload_path) as handle:
                        handle.write(encoded)
                    size = len(encoded)
                    digest = hashlib.sha256(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ExternalImportError, ValueError) as exc:
            raise HTTPException(422, {'code': 'external_import_product_metadata_invalid', 'message': str(exc)}) from exc
        package_sha256 = "sha256:" + digest.hexdigest()
        preview = await import_external_reviews(
            _CachedImportRequest(upload_path, package_sha256, dry_run=True)
        )
        try:
            cached = await asyncio.to_thread(
                generic_import_registry.register_file_cache,
                actor_id=actor_id,
                run_id=run_id,
                request_id=request_id,
                source_path=upload_path,
                package_sha256=package_sha256,
                package_bytes=size,
                preview=preview,
            )
        except GenericImportError as exc:
            raise _generic_import_http_error(exc) from exc
        cached.pop("package_path", None)
        return cached
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


@app.get("/api/imports/reviews/caches")
async def list_external_review_import_caches(request: Request, response: Response):
    actor_id, _ = _generic_import_context(request)
    try:
        caches = await asyncio.to_thread(generic_import_registry.list_caches, actor_id)
    except GenericImportError as exc:
        raise _generic_import_http_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": "voxquarry.generic-review-import-cache-list/v1",
        "cache_count": len(caches),
        "caches": caches,
    }


@app.post("/api/imports/reviews/plans")
async def plan_external_review_imports(
    request: Request,
    payload: GenericImportPlanRequest,
):
    actor_id, run_id = _generic_import_context(request)
    if payload.schema_version != PLAN_REQUEST_SCHEMA_VERSION:
        raise HTTPException(422, "unsupported generic import plan request schema")
    refs = [
        {
            "item_id": item.item_id,
            "cache_id": item.cache_id,
            "package_sha256": item.package_sha256,
        }
        for item in payload.cache_refs
    ]
    evaluations = {}
    for ref in refs:
        try:
            cached = await asyncio.to_thread(
                generic_import_registry.get_cache,
                actor_id,
                ref["cache_id"],
            )
            evaluations[ref["item_id"]] = await import_external_reviews(
                _CachedImportRequest(
                    cached["package_path"],
                    cached["package_sha256"],
                    dry_run=True,
                )
            )
        except GenericImportError:
            # The registry projects missing/hash/actor errors per item.
            continue
        except HTTPException as exc:
            evaluations[ref["item_id"]] = {
                "status": "blocked",
                "error": _http_error_projection(exc),
            }
    try:
        return await asyncio.to_thread(
            generic_import_registry.create_plan,
            actor_id=actor_id,
            run_id=run_id,
            request_id=payload.request_id,
            cache_refs=refs,
            evaluations=evaluations,
        )
    except GenericImportError as exc:
        raise _generic_import_http_error(exc) from exc


@app.post("/api/imports/reviews/plans/{plan_id}/confirm")
async def confirm_external_review_imports(
    plan_id: str,
    request: Request,
    payload: GenericImportConfirmRequest,
):
    actor_id, run_id = _generic_import_context(request)
    if payload.schema_version != CONFIRM_SCHEMA_VERSION:
        raise HTTPException(422, "unsupported generic import confirmation schema")
    try:
        claimed = await asyncio.to_thread(
            generic_import_registry.claim_confirmation,
            actor_id=actor_id,
            run_id=run_id,
            plan_id=plan_id,
            execution_request_id=payload.execution_request_id,
            confirmation_id=payload.confirmation_id,
            scope_sha256=payload.confirmation_scope_sha256,
            item_ids=payload.item_ids,
            confirmed=payload.confirmed,
            second_confirmation=payload.second_confirmation,
        )
    except GenericImportError as exc:
        raise _generic_import_http_error(exc) from exc
    if claimed.get("replayed_receipt"):
        return claimed["replayed_receipt"]
    plan = await asyncio.to_thread(generic_import_registry.get_plan, plan_id)
    by_item = {item["item_id"]: item for item in plan["items"]}
    for item_id in plan["selected_item_ids"]:
        if item_id in (plan.get("item_results") or {}):
            continue
        item = by_item[item_id]
        try:
            cached = await asyncio.to_thread(
                generic_import_registry.get_cache,
                actor_id,
                item["cache_id"],
            )
            published = await import_external_reviews(
                _CachedImportRequest(
                    cached["package_path"],
                    cached["package_sha256"],
                    dry_run=False,
                )
            )
            result = {
                "status": "published",
                "cache_id": item["cache_id"],
                "package_sha256": item["package_sha256"],
                "channel": published["channel"],
                "job_id": published["job_id"],
                "attempt_no": published["attempt_no"],
                "job_status": published["job_status"],
                "active_result_status": published["active_result_status"],
                "source_total": published["source_total"],
                "source_records_collected": published["source_records_collected"],
                "source_missing_records": published["source_missing_records"],
                "acquisition_batch_id": published["acquisition_batch_id"],
            }
        except (GenericImportError, HTTPException) as exc:
            if isinstance(exc, GenericImportError):
                error = {"code": exc.code, "message": exc.message}
            else:
                error = _http_error_projection(exc)
            prior = await asyncio.to_thread(
                store.find_external_import_publication,
                item.get("import_id"),
                item["package_sha256"].removeprefix("sha256:"),
            )
            if prior:
                result = {
                    "status": "already_published",
                    "cache_id": item["cache_id"],
                    "package_sha256": item["package_sha256"],
                    **prior,
                }
            else:
                result = {
                    "status": "blocked",
                    "cache_id": item["cache_id"],
                    "package_sha256": item["package_sha256"],
                    "error": error,
                }
        try:
            plan = await asyncio.to_thread(
                generic_import_registry.record_item_result,
                plan_id,
                item_id,
                result,
            )
        except GenericImportError as exc:
            raise _generic_import_http_error(exc) from exc
    try:
        return await asyncio.to_thread(generic_import_registry.finalize_receipt, plan_id)
    except GenericImportError as exc:
        raise _generic_import_http_error(exc) from exc


@app.post("/api/imports/reviews")
async def import_external_reviews(request: Request):
    """Validate and publish a complete or auditable partial review export.

    The uploaded body is a single JSON bundle.  It is streamed to a bounded
    staging file, never accepts browser credentials, and is removed after the
    validated standard artifacts have been published (or validation fails).
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type not in {"application/json", "application/octet-stream"}:
        raise HTTPException(415, "请上传 JSON 格式的网评导入文件")
    try:
        declared_size = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise HTTPException(400, "invalid content-length") from exc
    if declared_size > MAX_EXTERNAL_IMPORT_BYTES:
        raise HTTPException(413, "导入文件不能超过 250 MiB")

    staging_root = store.data_dir / "import-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix="review-import-", dir=staging_root)
    )
    upload_path = staging_dir / "upload.json"
    upload_sha = hashlib.sha256()
    size = 0
    try:
        with upload_path.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_EXTERNAL_IMPORT_BYTES:
                    raise HTTPException(413, "导入文件不能超过 250 MiB")
                upload_sha.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(422, "导入文件不能为空")
        expected_upload_sha = str(
            request.headers.get("x-voxquarry-expected-upload-sha256") or ""
        )
        if expected_upload_sha:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_upload_sha):
                raise HTTPException(422, "invalid expected upload sha256")
            if expected_upload_sha != "sha256:" + upload_sha.hexdigest():
                raise HTTPException(
                    409,
                    {
                        "code": "external_import_package_changed",
                        "message": "import package bytes changed after dry-run",
                    },
                )
        try:
            def load_and_validate():
                with upload_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                product = payload.get('product') if isinstance(payload, dict) else None
                missing = isinstance(product, dict) and (
                    any(product.get(field) in (None, '') for field in ('brand', 'category', 'energy_type'))
                    or (product.get('energy_type') == 'battery' and not product.get('voltage'))
                    or not payload.get('market_region')
                )
                # The production upload UI posts here directly. Freeze source
                # facts into this import's artifacts while retaining the hash
                # of the original uploaded bytes. Hash-bound confirmations and
                # already evidenced bundles must never revisit a changing PDP.
                if (missing and not expected_upload_sha
                        and not product.get('source_metadata_evidence')
                        and payload.get('schema_version') == 'voxquarry.review-import/v2'
                        and payload.get('channel') in METADATA_AUTOFILL_CHANNELS):
                    source = metadata_client.get(payload['channel'], payload.get('target_url') or '')
                    enriched = enrich_import_bundle_metadata(payload, source)
                    if enriched != payload:
                        enriched['product']['source_metadata_evidence']['original_upload_sha256'] = upload_sha.hexdigest()
                        payload = enriched
                return validate_import_bundle(payload)

            validated = await asyncio.to_thread(load_and_validate)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "导入文件必须是有效的 UTF-8 JSON") from exc
        except (ExternalImportError, ValueError) as exc:
            raise HTTPException(
                422,
                {
                    "code": "external_import_validation_failed",
                    "message": str(exc),
                },
            ) from exc

        task_metadata = dict(validated["task_product_metadata"])
        try:
            require_governed_metadata(task_metadata)
        except ValueError as exc:
            raise HTTPException(
                422,
                {
                    "code": "external_import_product_metadata_incomplete",
                    "message": str(exc),
                },
            ) from exc

        if validated["channel"] == "walmart":
            brand = task_metadata.get("brand") or None
            category = task_metadata.get("category") or None
        else:
            try:
                brand, category = await asyncio.to_thread(
                    lambda: (
                        store.canonical_master_value(
                            "brand", task_metadata["brand"]
                        ),
                        store.canonical_master_value(
                            "category", task_metadata["category"]
                        ),
                    )
                )
            except ValueError as exc:
                raise HTTPException(
                    422,
                    {
                        "code": "external_import_master_data_mismatch",
                        "message": str(exc),
                    },
                ) from exc
            if not brand or not category:
                raise HTTPException(
                    422,
                    {
                        "code": "external_import_master_data_mismatch",
                        "message": "product.brand 和 product.category 必须与现有主数据完全匹配",
                    },
                )

        existing = await asyncio.to_thread(
            store.find_target,
            validated["channel"],
            validated["target_url"],
        )
        model = task_metadata.get("model") or (existing or {}).get("model")
        observation = build_product_observation(
            brand=brand or (existing or {}).get("brand"),
            model=model,
            category=category,
            voltage=task_metadata.get("voltage"),
            voltage_raw=task_metadata.get("voltage"),
            evidence_source="governed_external_import",
            energy_type=task_metadata.get("energy_type"),
        )
        observation["product_form"] = task_metadata["product_form"]
        if task_metadata.get('source_metadata_evidence'):
            observation['source_metadata_evidence'] = task_metadata['source_metadata_evidence']
        enrichment = await asyncio.to_thread(
            product_master.enrich_optional,
            brand=observation.get("brand"),
            model=observation.get("model"),
        )
        product_context = _optional_product_context(
            observation, enrichment, require_complete=True,
        )
        master_binding = product_context["binding"]
        brand = product_context["brand"]
        model = product_context["model"]
        category = product_context["category"]
        query_params = getattr(request, "query_params", {})
        if str(query_params.get("dry_run", "")).casefold() in {"1", "true", "yes"}:
            return {
                "schema_version": "voxquarry.external-review-import-dry-run/v1",
                "status": "accepted",
                "import_id": validated["import_id"],
                "publishable_status": validated["publication_status"],
                "would_reuse_task": bool(existing),
                "existing_job_id": existing.get("id") if existing else None,
                "channel": validated["channel"],
                "target_key": validated["target_key"],
                "source_product_id": validated["product"]["id"],
                "source_total": validated["source_total"],
                "source_records_collected": validated["source_records_collected"],
                "source_missing_records": validated["source_missing_records"],
                "eligible_reviews": len(validated["eligible_rows"]),
                "upload_sha256": upload_sha.hexdigest(),
                "writes_performed": 0,
                "product_metadata": {
                    "brand": brand,
                    "model": model,
                    "category": category,
                    "market_region": validated["market_region"],
                    "energy_type": product_context["task_energy"]["energy_type"],
                    "voltage_platform": product_context["voltage_platform"],
                },
            }
        job_id = existing["id"] if existing else uuid.uuid4().hex[:12]
        import_batch_id = (
            existing.get("acquisition_batch_id")
            if existing
            and existing.get("status") == "queued"
            and existing.get("action") == EXTERNAL_IMPORT_ACTION
            and existing.get("acquisition_batch_id")
            else acquisition_batch_id()
        )
        artifact_stage = staging_dir / validated["channel"]
        report = await asyncio.to_thread(
            write_import_artifacts, artifact_stage, validated
        )
        try:
            published = await asyncio.to_thread(
                store.publish_external_import,
                job_id,
                channel=validated["channel"],
                target_url=validated["target_url"],
                brand=brand,
                model=model,
                category=category,
                market_region=validated["market_region"],
                source_product_id=validated["product"]["id"],
                result={validated["channel"]: report},
                staged_run_dir=artifact_stage,
                audit_detail={
                    "import_id": validated["import_id"],
                    "schema_version": validated["schema_version"],
                    "upload_sha256": upload_sha.hexdigest(),
                    "upload_bytes": size,
                    "source_total": validated["source_total"],
                    "source_records_collected": validated["source_records_collected"],
                    "source_missing_records": validated["source_missing_records"],
                    "publication_status": validated["publication_status"],
                    "eligible_reviews": len(validated["eligible_rows"]),
                },
                acquisition_batch_id=import_batch_id,
                product_master_binding=master_binding,
                product_observation=product_context["observation"],
                product_enrichment_status=product_context["enrichment_status"],
                product_quality_flags=product_context["quality_flags"],
                voltage_platform=product_context["voltage_platform"],
                task_energy=product_context["task_energy"],
            )
        except ValueError as exc:
            raise HTTPException(
                409,
                {
                    "code": "external_import_publish_conflict",
                    "message": str(exc),
                },
            ) from exc
        return {
            "status": validated["publication_status"],
            "job_status": published["status"],
            "active_result_status": published["active_result_status"],
            "job_id": published["id"],
            "attempt_no": published["attempt_count"],
            "reused_task": bool(existing),
            "channel": validated["channel"],
            "source_total": validated["source_total"],
            "source_records_collected": validated["source_records_collected"],
            "source_missing_records": validated["source_missing_records"],
            "eligible_reviews": len(validated["eligible_rows"]),
            "valid_text_reviews": report["valid_text_reviews"],
            "excluded_reviews": report["excluded_reviews"],
            "validation": {
                "schema_version": IMPORT_VALIDATION_SCHEMA_VERSION,
                "status": report["status"],
                "source_reconciled": report["source_reconciled"],
                "stop_reason": report["stop_reason"] or None,
            },
            "upload_sha256": upload_sha.hexdigest(),
            "acquisition_batch_id": import_batch_id,
        }
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


@app.delete("/api/jobs/failed")
def delete_failed_jobs():
    raise HTTPException(
        409,
        "失败任务及其尝试历史会保留用于重试和审计；请在原任务上重试。",
    )


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: str, req: RetryRequest):
    source = store.get_job(job_id)
    if not source:
        raise HTTPException(404, "job not found")
    if source["status"] not in {"failed", "partial_success"}:
        raise HTTPException(409, "仅失败或部分成功任务可以重试")
    if not source.get("target_url"):
        raise HTTPException(409, "该历史任务没有可复用的商品链接")
    # A failed attempt has no usable payload to overwrite, so retry it
    # immediately under the stable task ID.  Partial success still requires
    # confirmation because it already has a reportable artifact version.
    if source["status"] == "partial_success" and not req.confirm_duplicate:
        raise HTTPException(
            409,
            {
                "code": "retry_confirmation_required",
                "message": "当前任务已有部分成功数据；确认后会保留该版本，并在同一任务中创建新的采集尝试。",
                "existing_job_id": source["id"],
                "existing_status": source["status"],
            },
        )
    try:
        batch_id = acquisition_batch_id(req.acquisition_batch_id)
        queued = store.queue_retry(
            job_id,
            RETRY_JOB_PRIORITY,
            acquisition_batch_id=batch_id,
        )
    except MasterDataConflict as exc:
        status = 503 if "recovery" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "id": queued["id"],
        "status": "queued",
        "attempt_no": queued["attempt_count"],
        "reused_task": True,
        "acquisition_batch_id": batch_id,
    }


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    try:
        result = store.request_delete(job_id)
    except MasterDataConflict as exc:
        status = 503 if "recovery" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "job not found")
    if result["deleted"]:
        cleanup_run(job_id)
    return result


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    result = store.get_job(job_id)
    if not result:
        raise HTTPException(404, "job not found")
    result["attempts"] = store.list_attempts(job_id)
    result["events"] = store.list_events(job_id)
    return result


@app.get("/api/jobs/{job_id}/attempts")
def job_attempts(job_id: str):
    if not store.get_job(job_id):
        raise HTTPException(404, "job not found")
    return {"job_id": job_id, "attempts": store.list_attempts(job_id), "events": store.list_events(job_id)}


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: str, req: JobUpdate):
    model = (normalize_model(req.model) or "") if req.model is not None else None
    try:
        existing = store.get_job(job_id)
        if not existing:
            raise HTTPException(404, "job not found")
        if existing and existing.get("product_master_binding_status") == "bound":
            record = existing.get("product_master_record") or {}
            immutable_values = {
                "brand": (record.get("brand") or {}).get("canonical_name"),
                "model": record.get("model_number"),
                "category": (record.get("category") or {}).get("name"),
            }
            requested_values = {"brand": req.brand, "model": model, "category": req.category}
            changed = [
                field for field, value in requested_values.items()
                if value is not None and str(value).strip() != str(immutable_values[field] or "").strip()
            ]
            if changed:
                raise ValueError(
                    "已绑定产品库稳定产品身份，品牌、型号和品类不可在数据采集单独改写"
                )
        is_bound = bool(existing and existing.get("product_master_binding_status") == "bound")
        brand = (
            existing.get("brand") if is_bound and req.brand is not None
            else store.canonical_master_value("brand", req.brand) if req.brand is not None
            else None
        )
        category = (
            existing.get("category") if is_bound and req.category is not None
            else store.canonical_master_value("category", family_category(req.category)) if req.category is not None
            else None
        )
        market_region = (
            validate_channel_market_region(
                existing.get("channel") if existing else "",
                req.market_region,
            )
            if req.market_region is not None
            else None
        )
        if req.brand is not None and not brand:
            raise ValueError("请从主数据中选择品牌")
        if req.category is not None and not category:
            raise ValueError("请从主数据中选择品类")
        observation = None
        platform = None
        task_energy = None
        metadata_changed = any(
            value is not None and str(value).strip() != str(existing.get(field) or "").strip()
            for field, value in (("brand", brand), ("model", model), ("category", category))
        )
        energy_changed = (
            req.energy_type is not None
            and normalize_energy_type(req.energy_type) != normalize_energy_type(existing.get("energy_type"))
        )
        voltage_changed = (
            req.voltage is not None
            and normalize_voltage(req.voltage) != normalize_voltage(
                existing.get("energy_canonical_voltage") or existing.get("voltage_platform_voltage")
                or (existing.get("product_observation") or {}).get("normalized_voltage")
            )
        )
        if metadata_changed or energy_changed or voltage_changed:
            stored_observation = dict(existing.get("product_observation") or {})
            selected_energy = (
                req.energy_type
                if req.energy_type is not None
                else existing.get("energy_type") or stored_observation.get("energy_type")
            )
            selected_voltage = (
                req.voltage
                if req.voltage is not None
                else existing.get("energy_canonical_voltage")
                or existing.get("voltage_platform_voltage")
                or stored_observation.get("normalized_voltage")
            )
            # Explicit changes cannot erase already governed facts. Legacy
            # incomplete rows can still receive an unrelated model correction.
            if energy_changed or voltage_changed:
                if normalize_energy_type(selected_energy) == "unknown":
                    raise ValueError("请选择已确认的能源类型，不能将能源信息改为未知")
                if normalize_energy_type(selected_energy) == "battery" and not normalize_voltage(selected_voltage):
                    raise ValueError("电池任务必须填写有效电压")
            values = {
                "brand": brand if brand is not None else existing.get("brand"),
                "model": model if model is not None else existing.get("model"),
                "category": category if category is not None else existing.get("category"),
                "voltage": selected_voltage,
                "energy_type": normalize_energy_type(selected_energy),
            }
            if energy_changed or voltage_changed or brand is not None or category is not None:
                normalized = normalize_product_metadata(
                    target_url=existing["target_url"], **values,
                    market_region=market_region or existing.get("market_region"),
                )
                # Use the shared hybrid/energy rules, without silently changing
                # unrelated identity fields or filling a nullable model.
                values["energy_type"] = normalized["energy_type"]
                if existing.get("energy_readiness_status") == "ready" or energy_changed or voltage_changed:
                    require_governed_metadata(values)
            observation = build_product_observation(
                **values,
                voltage_raw=selected_voltage,
                evidence_source="user_confirmed",
            )
            binding = (
                {"record": existing.get("product_master_record")}
                if existing.get("product_master_binding_status") == "bound"
                and existing.get("product_master_record")
                else None
            )
            platform = build_voltage_platform(observation, binding)
            task_energy = build_task_energy(observation, platform)
            if (platform.get("platform_key"), platform.get("status")) == (
                existing.get("voltage_platform_key"), existing.get("voltage_platform_status")
            ):
                platform["evidence_source"] = existing.get("voltage_platform_evidence_source")
            if (task_energy["energy_type"], task_energy["canonical_voltage"]) == (
                existing.get("energy_type"), existing.get("energy_canonical_voltage")
            ):
                task_energy["evidence_source"] = existing.get("energy_evidence_source") or task_energy["evidence_source"]
            if not energy_changed and not voltage_changed and all(
                value is None or value == existing.get(field)
                for field, value in (("brand", brand), ("category", category))
            ):
                # A model correction only changes the observation. In particular,
                # it must not initialize legacy NULL energy or rewrite provenance.
                observation = {**observation, **stored_observation, "model": observation["model"], "evidence_source": "user_confirmed"}
                platform = None
                task_energy = None
        if req.product_form_factor is not None or (not is_bound and form_key(req.category) in CATEGORY_FORMS):
            if is_bound:
                raise ValueError("绑定产品库的产品形态必须在产品库修订，不能由任务覆盖")
            observation = dict(observation or existing.get("product_observation") or {})
            observation["product_form"] = product_form(category or existing.get("category"), req.product_form_factor,
                evidence_source="user_confirmed", source_category=req.category or existing.get("product_form_source_category") or existing.get("category"))
        result = store.update_job(
            job_id,
            brand,
            model,
            category,
            req.priority,
            market_region=market_region,
            product_observation=observation,
            voltage_platform=platform,
            task_energy=task_energy,
        )
    except MasterDataConflict as exc:
        status = 503 if "recovery" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "job not found")
    return result


@app.post("/api/jobs/{job_id}/promote")
def promote_job(job_id: str):
    try:
        result = store.promote(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "job not found")
    return result


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    resolved = resolve_product(req.url)
    observation = build_product_observation(
        brand=resolved.get("brand"),
        model=resolved.get("model"),
        category=resolved.get("category"),
        voltage=resolved.get("voltage"),
        voltage_raw=resolved.get("voltage"),
        evidence_source="product_url",
        energy_type=resolved.get("energy_type"),
    )
    context = _optional_product_context(
        observation,
        product_master.enrich_optional(
            brand=observation.get("brand"),
            model=observation.get("model"),
        ),
    )
    return {
        **resolved,
        "brand": context["brand"],
        "model": context["model"],
        "category": context["category"],
        "product_observation": observation,
        "product_master": _optional_enrichment_projection(context),
        "voltage_platform": context["voltage_platform"],
        "task_energy": context["task_energy"],
        "source_product_identity": source_product_identity_from_url(
            resolved["channel"], req.url,
        ),
    }


@app.get("/api/stats")
def stats(by: str = "channel"):
    try:
        groups = store.stats(by)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"by": by, "groups": groups}


@app.get("/api/master-data")
def master_data():
    try:
        return store.list_master_data()
    except MasterDataConflict as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/master-data/{kind}")
def create_master_data(kind: str, req: MasterDataRequest, request: Request):
    try:
        actor = (
            _master_data_actor(request)
            if (
                _legacy_master_writes_protected()
                or store.legacy_master_data_activated()
            )
            else "legacy-api"
        )
        return store.create_master_data(kind, req.name, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch("/api/master-data/{kind}/{item_id}")
def update_master_data(
    kind: str, item_id: int, req: MasterDataRequest, request: Request
):
    try:
        actor = (
            _master_data_actor(request)
            if (
                _legacy_master_writes_protected()
                or store.legacy_master_data_activated()
            )
            else "legacy-api"
        )
        result = store.update_master_data(kind, item_id, req.name, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "主数据不存在")
    return result


@app.delete("/api/master-data/{kind}/{item_id}")
def delete_master_data(kind: str, item_id: int, request: Request):
    try:
        actor = (
            _master_data_actor(request)
            if (
                _legacy_master_writes_protected()
                or store.legacy_master_data_activated()
            )
            else "legacy-api"
        )
        result = store.delete_master_data(kind, item_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result:
        raise HTTPException(404, "主数据不存在")
    return result


def _shared_master_data():
    catalog = store.shared_master_data
    if catalog is None:
        raise HTTPException(
            503,
            "shared master-data catalog unavailable; run the explicit init/import CLI",
        )
    return catalog


def _legacy_master_writes_protected() -> bool:
    return os.getenv(
        "MASTER_DATA_PROTECT_LEGACY_WRITES", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}


def _master_data_actor(request: Request, *, require_write: bool = True) -> str:
    writes_enabled = os.getenv(
        "MASTER_DATA_ADMIN_WRITES_ENABLED", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if require_write and not writes_enabled:
        raise HTTPException(503, "master-data admin writes are disabled")
    dev_mode = os.getenv("MASTER_DATA_DEV_MODE", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }
    if not dev_mode:
        configured_token = valid_admin_proxy_token_by_runtime()
        if configured_token is None:
            raise HTTPException(503, "master-data admin proxy authentication is not configured")
        supplied_token = request.headers.get("X-Master-Data-Proxy-Token", "")
        if not supplied_token:
            raise HTTPException(401, "trusted admin proxy token is required")
        if not hmac.compare_digest(supplied_token, configured_token):
            raise HTTPException(403, "trusted admin proxy token is invalid")
    actor = (request.headers.get("X-Master-Data-Actor") or "").strip()
    trusted = valid_production_trusted_actors()
    if actor:
        if actor.casefold() in {"anonymous", "匿名"}:
            raise HTTPException(401, "a trusted master-data actor is required")
        if dev_mode:
            dev_actor = os.getenv("MASTER_DATA_DEV_ACTOR", "").strip()
            if not dev_actor or actor != dev_actor:
                raise HTTPException(403, "master-data actor is not trusted")
            return actor
        if actor not in trusted:
            raise HTTPException(403, "master-data actor is not trusted")
        return actor
    dev_actor = os.getenv("MASTER_DATA_DEV_ACTOR", "").strip()
    if dev_mode and dev_actor and dev_actor.casefold() not in {"anonymous", "匿名"}:
        return dev_actor
    raise HTTPException(401, "X-Master-Data-Actor is required")


def _if_match_catalog_hash(request: Request) -> str:
    value = (request.headers.get("If-Match") or "").strip()
    if not value:
        raise HTTPException(428, "If-Match with the current catalog ETag is required")
    if value.startswith("W/"):
        raise HTTPException(412, "weak catalog ETags are not accepted for writes")
    return value.strip('"')


def _catalog_read_response(request: Request, response: Response, payload):
    content_hash = payload.get("catalog_content_hash")
    if not content_hash:
        raise HTTPException(500, "catalog response is missing snapshot hash")
    etag = f'"{content_hash}"'
    if (request.headers.get("If-None-Match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return payload


def _catalog_write_error(exc: Exception):
    if isinstance(exc, MasterDataNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, MasterDataConflict):
        status = 412 if str(exc).startswith("catalog ETag conflict") else 409
        raise HTTPException(status, str(exc)) from exc
    if isinstance(exc, MasterDataError):
        raise HTTPException(422, str(exc)) from exc
    raise exc


@app.get("/api/v2/master-data")
def shared_master_data_root(request: Request, response: Response):
    catalog = _shared_master_data()
    return _catalog_read_response(request, response, catalog.summary())


@app.get("/api/v2/master-data/snapshot")
def shared_master_data_snapshot(request: Request, response: Response):
    try:
        payload = (store.remote_master_data.snapshot() if store.remote_master_data
                   else _shared_master_data().snapshot())
        return _catalog_read_response(request, response, payload)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get('/api/v2/master-data/task-bindings')
def task_master_data_status(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return store.task_master_data_status()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/v2/master-data/summary")
def shared_master_data_summary(request: Request, response: Response):
    catalog = _shared_master_data()
    return _catalog_read_response(request, response, catalog.summary())


@app.post("/api/v2/master-data/validate")
def validate_shared_master_data_request(
    req: MasterDataValidateRequest,
    request: Request,
    response: Response,
):
    catalog = _shared_master_data()
    try:
        payload = catalog.validate_candidate(
            req.type,
            code=req.code,
            name=req.name,
            exclude_id=req.exclude_id,
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/v2/master-data/{entity_type}/resolve")
def resolve_shared_master_data(
    entity_type: str,
    value: str,
    request: Request,
    response: Response,
    source_system: str | None = None,
):
    catalog = _shared_master_data()
    try:
        payload = catalog.resolve(
            entity_type, value, source_system=source_system
        )
    except (MasterDataError, MasterDataNotFound) as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/v2/master-data/{entity_type}/validate")
def validate_shared_master_data(
    entity_type: str,
    request: Request,
    response: Response,
    code: str | None = None,
    name: str | None = None,
    exclude_id: str | None = None,
):
    catalog = _shared_master_data()
    try:
        payload = catalog.validate_candidate(
            entity_type, code=code, name=name, exclude_id=exclude_id
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/v2/master-data/{entity_type}")
def list_shared_master_data(
    entity_type: str,
    request: Request,
    response: Response,
    q: str = "",
    status: str = "active",
    page: int = 1,
    page_size: int = 50,
):
    catalog = _shared_master_data()
    try:
        payload = catalog.list(
            entity_type, q=q, status=status, page=page, page_size=page_size
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/v2/master-data/{entity_type}/{entity_id}")
def shared_master_data_detail(
    entity_type: str, entity_id: str, request: Request, response: Response
):
    catalog = _shared_master_data()
    try:
        payload = catalog.detail(entity_type, entity_id)
    except (MasterDataError, MasterDataNotFound) as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/admin/master-data/audit")
def shared_master_data_audit(
    request: Request,
    response: Response,
    type: str | None = None,
    entity_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    _master_data_actor(request, require_write=False)
    catalog = _shared_master_data()
    try:
        payload = catalog.audit(
            entity_type=type,
            entity_id=entity_id,
            page=page,
            page_size=page_size,
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    return _catalog_read_response(request, response, payload)


@app.get("/api/admin/master-data/integration-status")
def shared_master_data_integration_status(
    request: Request, response: Response
):
    _master_data_actor(request, require_write=False)
    response.headers["Cache-Control"] = "no-store"
    return _shared_master_data().integration_status()


@app.post("/api/admin/master-data/{entity_type}", status_code=201)
def create_shared_master_data(
    entity_type: str,
    payload: dict,
    request: Request,
    response: Response,
):
    catalog = _shared_master_data()
    try:
        result = catalog.create(
            entity_type,
            payload,
            actor=_master_data_actor(request),
            expected_catalog_hash=_if_match_catalog_hash(request),
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    response.headers["ETag"] = f'"{result["catalog_content_hash"]}"'
    return result


@app.patch("/api/admin/master-data/{entity_type}/{entity_id}")
def update_shared_master_data(
    entity_type: str,
    entity_id: str,
    payload: dict,
    request: Request,
    response: Response,
):
    catalog = _shared_master_data()
    try:
        result = catalog.update(
            entity_type,
            entity_id,
            payload,
            actor=_master_data_actor(request),
            expected_catalog_hash=_if_match_catalog_hash(request),
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    response.headers["ETag"] = f'"{result["catalog_content_hash"]}"'
    return result


@app.post("/api/admin/master-data/{entity_type}/{entity_id}/enable")
def enable_shared_master_data(
    entity_type: str,
    entity_id: str,
    request: Request,
    response: Response,
):
    catalog = _shared_master_data()
    try:
        result = catalog.set_active(
            entity_type,
            entity_id,
            True,
            actor=_master_data_actor(request),
            expected_catalog_hash=_if_match_catalog_hash(request),
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    response.headers["ETag"] = f'"{result["catalog_content_hash"]}"'
    return result


@app.post("/api/admin/master-data/{entity_type}/{entity_id}/disable")
def disable_shared_master_data(
    entity_type: str,
    entity_id: str,
    request: Request,
    response: Response,
    req: MasterDataStatusRequest | None = None,
):
    catalog = _shared_master_data()
    try:
        result = catalog.set_active(
            entity_type,
            entity_id,
            False,
            actor=_master_data_actor(request),
            expected_catalog_hash=_if_match_catalog_hash(request),
            replacement_id=req.replacement_id if req else None,
        )
    except MasterDataError as exc:
        _catalog_write_error(exc)
    response.headers["ETag"] = f'"{result["catalog_content_hash"]}"'
    return result


@app.post("/api/search")
def search(req: SearchRequest):
    return search_products(req.brand, req.model, req.channels)


@app.get("/api/feedback")
def feedback():
    return store.list_feedback()


@app.post("/api/feedback")
def create_feedback(req: FeedbackRequest):
    content = req.content.strip()
    if not content:
        raise HTTPException(422, "反馈内容不能为空")
    return store.create_feedback(uuid.uuid4().hex[:12], req.author.strip() or "匿名", req.tag.strip() or "其他", content)


@app.post("/api/feedback/{feedback_id}/like")
def like_feedback(feedback_id: str):
    result = store.like_feedback(feedback_id)
    if not result:
        raise HTTPException(404, "feedback not found")
    return result


@app.get("/api/artifacts/{job_id}/{channel}/{name}")
def artifact(job_id: str, channel: str, name: str):
    if "/" in channel or "/" in name or ".." in channel or ".." in name:
        raise HTTPException(400, "invalid artifact")
    path = store.data_dir / "runs" / job_id / channel / name
    if not path.is_file():
        raise HTTPException(404, "artifact not found")
    job = store.get_job(job_id) or {}
    report = next((value for value in (job.get("result") or {}).values() if isinstance(value, dict) and value.get("product")), {})
    product = report.get("product") or {}
    title = product.get("title") or job.get("model") or job.get("brand") or "product"
    date = (job.get("finished_at") or job.get("created_at") or "").split("T")[0].replace("-", "") or "unknown-date"
    channel_name = {
        "lowes": "Lowes",
        "homedepot": "HomeDepot",
        "ace": "Ace",
        "ego": "EGO",
        "ryobi": "RYOBI",
        "milwaukee": "Milwaukee",
        "dewalt": "DEWALT",
        "greenworks": "Greenworks",
    }.get(channel, channel)
    safe_title = re.sub(r"[\\\\/:*?\"<>|]+", "-", clean_text(title)).strip(" .-")[:110] or "product"
    suffix = {"reviews.csv": "", "reviews.raw.jsonl": "_原始评论", "validation_report.json": "_验证报告"}.get(name, "")
    return FileResponse(path, filename=f"{date}_{channel_name}_{safe_title}{suffix}{path.suffix}")


from .research_bridge import ResearchBridge, register_research_bridge
from .review_product_discovery import discover as discover_review_products

register_research_bridge(app, ResearchBridge(
    lambda: store, _create_job, resolve_task_product, JobRequest, COLLECTION_CHANNELS,
    discover=lambda brand, model, channels: discover_review_products(brand, model, channels,
        history=historical_product_links, search=google_product_links),
))


# Public presentation is served by voxquarry.server.
