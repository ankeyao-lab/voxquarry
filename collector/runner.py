"""Channel adapters and evidence-first runner.

Every adapter returns raw review records plus the total reported by the source. A
run is never labelled complete unless the unique review IDs equal that total.
"""
import asyncio
import csv
import hashlib
import html
import json
import os
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit

import requests
from patchright.async_api import async_playwright

from .official_sources import (
    OfficialSourceError,
    exmark_public_config,
    exmark_product_identity,
    guard_public_response,
    official_bv_identity,
    validate_complete_pages,
    walmart_review_identity,
)
from .source_product_identity import resolved_source_product_identity
from .source_metadata_client import SourceMetadataClient, collected_product_context, merge_source_metadata
from .source_product_metadata import SourceProductMetadataError, infer_source_market_region

CHANNELS = {
    "lowes",
    "homedepot",
    "ace",
    "ego",
    "ryobi",
    "milwaukee",
    "dewalt",
    "einhell",
    "harborfreight",
    "tractorsupply",
    "amazon_reviews_2023",
    "skil",
    "walmart",
    "stihl",
    "husqvarna",
    "toro",
    "johndeere",
    "exmark",
    "greenworks",
}
# All public adapters are selectable. Walmart and Home Depot use bounded samples.
COLLECTION_CHANNELS = CHANNELS
PAGE_SIZE = 100
LOWES_PAGE_SIZE = 100
LOWES_API_WINDOW_SIZE = 10000
LOWES_BV_OVERFLOW_THRESHOLD = LOWES_API_WINDOW_SIZE * 2
LOWES_BV_DEPLOYMENT_URL = (
    "https://apps.bazaarvoice.com/deployments/lowes/onl1/production/en_US/bv.js"
)
TRACTOR_SUPPLY_BV_DEPLOYMENT_URL = (
    "https://apps.bazaarvoice.com/deployments/tractorsupply/"
    "new_pdp/production/en_US/bv.js"
)
EINHELL_GOVERNED_BV_DEPLOYMENTS = {
    # einhell.co exposes the reviewed item number and aggregate count on the
    # official PDP, but its consent-managed placeholder does not publish a
    # deployment URL. The official Einhell public client contains the same
    # ProductIds; .co displays the all-locale aggregate, not de_DE alone.
    "einhell.co": {
        "deployment_url": (
            "https://apps.bazaarvoice.com/deployments/einhell-de/"
            "main_site/production/de_DE/bv.js"
        ),
        "all_content_locales": True,
    },
}
# These storefronts expose a public Bazaarvoice deployment. Official brand
# sites expose or resolve the reviewed PDP ID before the first review request.
# Reading the published config avoids fragile browser event capture and keeps
# collection product-level without asking users to type an internal ID.
PUBLIC_BV_CHANNELS = {"milwaukee", "dewalt", "ego", "einhell", "tractorsupply"}
SKIL_BV_DEPLOYMENT_URL = (
    "https://apps.bazaarvoice.com/deployments/skiltools/main_site/production/en_US/bv.js"
)
# SKIL's public PDPs carry the reviewed product identity in a data marker.  A
# governed deployment is used only after that marker is reconciled; the
# collector never guesses from a URL slug or a historical workbook model.
PUBLIC_BV_CHANNELS.update({"skil", "ryobi"})
OFFICIAL_STRICT_BV_CHANNELS = {
    "stihl", "husqvarna", "toro", "johndeere", "greenworks",
}
PUBLIC_BV_CHANNELS.update(OFFICIAL_STRICT_BV_CHANNELS)
OFFICIAL_BV_DEPLOYMENTS = {
    "stihl": (
        "https://apps.bazaarvoice.com/deployments/stihl/"
        "main_site/production/en_US/bv.js"
    ),
    "greenworks": (
        "https://apps.bazaarvoice.com/deployments/greenworkstools/"
        "main_site/production/en_US/bv.js"
    ),
}
EXMARK_PAGE_SIZE = 25
WALMART_PAGE_SIZE = 10

REVIEW_CSV_FIELDS = [
    "channel",
    "product_id",
    "product_title",
    "product_url",
    "review_id",
    "display_site",
    "source_site",
    "origin_source_name",
    "origin_source_url",
    "origin_source_site",
    "is_ego_official_origin",
    "source_product_id",
    "market_region",
    "market_region_evidence",
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
    "review_word_count",
    "is_valid_text_review",
    "is_eligible",
    "exclusion_reason",
]
MARKET_REGIONS = {"North America", "Europe", "Australia", "Other"}
AMAZON_ARCHIVE_THROUGH = "2023-09-30"
REVIEW_WORD_RE = re.compile(
    r"[^\W_]+(?:['’.-][^\W_]+)*",
    re.UNICODE,
)

CATEGORY_URLS = {
    "lowes": "https://www.lowes.com/pl/outdoor-tools-equipment/leaf-blowers-accessories/leaf-blowers/4294612766",
    "homedepot": "https://www.homedepot.com/b/Outdoors-Outdoor-Power-Equipment-Leaf-Blowers/N-5yc1vZbxav",
    "ace": "https://www.acehardware.com/departments/lawn-and-garden/outdoor-power-equipment/leaf-blowers",
}
SITEMAPS = {
    "ego": "https://egopowerplus.com/media/sitemap/sitemap_ego.xml",
    "ryobi": "https://www.ryobitools.com/sitemap.xml",
    "skil": "https://www.skil.com/collections/outdoor-power-equipment/products.json?limit=250",
}

HD_URL = "https://apionline.homedepot.com/federation-gateway/graphql?opname=reviews"
HD_QUERY = """query reviews($itemId:String!,$sortBy:String,$startIndex:Int,$pagesize:String,$recfirstpage:String,$filters:ReviewsFilterInput,$searchTerm:String,$excludeFamily:Boolean){reviews(itemId:$itemId,sortBy:$sortBy,startIndex:$startIndex,pagesize:$pagesize,recfirstpage:$recfirstpage,filters:$filters,searchTerm:$searchTerm,excludeFamily:$excludeFamily){Results{Id ProductId SubmissionTime Title IsRecommended UserNickname UserLocation ReviewText Rating} TotalResults}}"""
HD_SLICE_START_LIMIT = 600
HD_BASE_VIEWS = (("newest", None), ("oldest", None))
HD_EXPANSION_VIEWS = (
    ("mosthelpfull", None),
    ("photoreview", None),
    ("newest", True),
    ("oldest", True),
    ("mosthelpfull", True),
    ("photoreview", True),
    ("newest", False),
    ("oldest", False),
    ("mosthelpfull", False),
    ("photoreview", False),
)
HARBOR_FREIGHT_GRAPHQL_URL = "https://www.harborfreight.com/pwa-graphql"
HARBOR_FREIGHT_PAGE_SIZE = 20
HARBOR_FREIGHT_REVIEWS_QUERY = """
query ProductReviews($sku: String!, $q: String, $filters: ProductReviewFilterInput) {
  productReviews(sku: $sku, q: $q, filters: $filters) {
    offset
    total
    limit
    reviews {
      dimensions { dimensionId dimensionLabel label }
      incentivized
      dateCreated
      purchaseDate
      id
      title
      text
      rating
      upVotes
      downVotes
      user { externalId nickName }
      media {
        photo { id type thumbnailUrl normalUrl caption }
        video { id type thumbnailUrl html caption }
      }
      responses {
        dateCreated
        text
        user { externalId nickName }
      }
    }
  }
}
"""


class RetryableChannelError(RuntimeError):
    """A source-access outage that should pause one channel without failing jobs."""

    def __init__(self, message, retry_after=900):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class TractorSupplyAccessError(RuntimeError):
    """A fail-closed Tractor Supply access or identity classification."""

    failure_class = "source_access_error"
    cooldown_seconds = 21600

    def __init__(self, message, *, failure_class=None, cooldown_seconds=None):
        super().__init__(message)
        if failure_class:
            self.failure_class = str(failure_class)
        if cooldown_seconds is not None:
            self.cooldown_seconds = max(0, int(cooldown_seconds))


class TractorSupplyTransportError(RetryableChannelError):
    """A retryable TSC transport failure with a persisted channel cooldown."""

    failure_class = "transport_error"

    def __init__(self, message, *, retry_after=900, failure_class=None):
        super().__init__(message, retry_after=retry_after)
        if failure_class:
            self.failure_class = str(failure_class)


class LowesAccessError(RuntimeError):
    """Lowe's or its configured transport did not return the public API payload."""


class LowesProductIdentityError(LowesAccessError):
    """The official Lowe's response did not prove the requested PDP identity."""


class HomeDepotAccessError(RuntimeError):
    """Home Depot did not return its public review payload."""

    failure_class = "source_access_error"


class HomeDepotClassifiedError(HomeDepotAccessError):
    """A sanitized, auditable Home Depot source-access classification."""

    failure_class = "source_access_error"
    safe_message = "Home Depot source access did not return verified review data"

    def __init__(self, _unsafe_detail=None, *, attempt_count=None):
        message = f"{self.failure_class}: {self.safe_message}"
        if attempt_count is not None:
            message += f" after {max(1, int(attempt_count))} attempts"
        super().__init__(message)
        self.attempt_count = attempt_count


class HomeDepotProductEvidenceError(HomeDepotClassifiedError):
    failure_class = "product_evidence_missing"
    safe_message = "Home Depot response omitted verified target-product evidence"


class HomeDepotChallengeError(HomeDepotClassifiedError):
    failure_class = "anti_bot_challenge"
    safe_message = "Home Depot returned an anti-bot challenge or error page"


class HomeDepotAccessDeniedError(HomeDepotClassifiedError):
    failure_class = "access_denied"
    safe_message = "Home Depot denied the current public-source access path"


class HomeDepotUpstreamError(RetryableChannelError):
    """Firecrawl transport is unavailable; pause Home Depot without failing."""


def now():
    return datetime.now(timezone.utc).isoformat()


def clean_url(url):
    return urlunsplit((*urlsplit(url)[:3], "", ""))


def lowes_product_id_from_url(url):
    """Extract an official Lowe's PDP item ID without accepting stray digits.

    Lowe's has seven-digit legacy item IDs as well as newer longer IDs.  The
    URL provides only a candidate identity: seven-digit IDs are accepted by
    collection only after the first-party RNR response exposes the same
    ``reviewStatistics.omniItemId``.
    """
    parsed = urlsplit(str(url or ""))
    host = parsed.netloc.casefold().split("@")[-1].split(":", 1)[0]
    if host != "lowes.com" and not host.endswith(".lowes.com"):
        return ""
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].casefold() != "pd":
        return ""
    candidate = parts[-1]
    return candidate if re.fullmatch(r"\d{7,}", candidate) else ""


def clean_html_text(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def count_review_words(value):
    """Count Unicode lexical tokens after normalising review whitespace."""
    text = " ".join(str(value or "").split())
    return len(REVIEW_WORD_RE.findall(text))


def normalize_market_region(value):
    aliases = {
        "north america": "North America",
        "na": "North America",
        "北美": "North America",
        "europe": "Europe",
        "eu": "Europe",
        "欧洲": "Europe",
        "australia": "Australia",
        "au": "Australia",
        "澳洲": "Australia",
        "other": "Other",
        "其他": "Other",
    }
    return aliases.get(str(value or "").strip().casefold(), "Other")


def validate_market_region(value):
    normalized = normalize_market_region(value)
    if normalized == "Other" and str(value or "").strip().casefold() not in {
        "other", "其他",
    }:
        raise ValueError(
            "market_region must be North America, Europe, Australia, or Other"
        )
    return normalized


def validate_channel_market_region(channel, value):
    normalized = validate_market_region(value)
    if (
        channel == "amazon_reviews_2023"
        and normalized != "North America"
    ):
        raise ValueError(
            "Amazon Reviews 2023 market_region must be North America"
        )
    if channel == "tractorsupply" and normalized != "North America":
        raise ValueError(
            "Tractor Supply market_region must be North America"
        )
    return normalized


def infer_market_region(url):
    host = urlsplit(str(url or "")).netloc.casefold().removeprefix("www.")
    if not host:
        return "Other"
    for channel, domains in {
        'husqvarna': ('husqvarna.com',), 'toro': ('toro.com',), 'johndeere': ('deere.com',),
    }.items():
        if any(host == domain or host.endswith('.' + domain) for domain in domains):
            return infer_source_market_region(channel, url) or 'Other'
    if host.endswith((".com.au", ".au")):
        return "Australia"
    if host in {"screwfix.com", "www.screwfix.com", "einhell.co"} or host.endswith(
        (
            ".co.uk", ".uk", ".eu", ".de", ".fr", ".it", ".es", ".nl",
            ".be", ".at", ".se", ".no", ".dk", ".fi", ".pl", ".ie",
        )
    ):
        return "Europe"
    if host.endswith(
        (
            "lowes.com", "homedepot.com", "acehardware.com",
            "harborfreight.com", "tractorsupply.com", "skil.com", "amazon.com",
            "amazon.ca", "canadiantire.ca", "egopowerplus.com",
            "ryobitools.com", "milwaukeetool.com", "dewalt.com",
            "walmart.com", "stihlusa.com", "husqvarna.com", "toro.com",
            "deere.com", "exmark.com",
            "greenworkstools.com",
        )
    ):
        return "North America"
    return "Other"


def review_origin(raw):
    source = raw.get("SyndicationSource") or raw.get("syndicationSource") or {}
    if isinstance(source, dict):
        name = (
            source.get("Name")
            or source.get("name")
            or source.get("SourceName")
            or source.get("sourceName")
            or ""
        )
        url = (
            source.get("ContentLink")
            or source.get("contentLink")
            or source.get("Url")
            or source.get("url")
            or ""
        )
    else:
        name, url = str(source or ""), ""
    name = (
        name
        or raw.get("SyndicationSourceName")
        or raw.get("syndicationSourceName")
        or ""
    )
    url = (
        url
        or raw.get("SyndicationSourceUrl")
        or raw.get("syndicationSourceUrl")
        or ""
    )
    host = urlsplit(str(url)).netloc.casefold().removeprefix("www.")
    is_ego = bool(
        re.search(r"(^|\.)egopowerplus\.(?:com|co\.uk)$", host)
        or re.search(r"\bego(?:\s*power\s*\+?)?\b", str(name), re.I)
    )
    if host:
        source_site = host
    elif is_ego:
        source_site = "egopowerplus.com"
    else:
        source_site = re.sub(
            r"[^a-z0-9]+", "-", str(name).casefold()
        ).strip("-")
    return {
        "origin_source_name": str(name or ""),
        "origin_source_url": str(url or ""),
        "origin_source_site": source_site,
        "is_ego_official_origin": is_ego,
    }


def apply_review_contract(channel, product, raw, record):
    origin = review_origin(raw)
    display_site = product.get("source_site", channel)
    review_text = record.get("review_text") or ""
    word_count = count_review_words(review_text)
    excluded_ego_origin = channel != "ego" and origin["is_ego_official_origin"]
    source_site = origin["origin_source_site"] or display_site
    return {
        **record,
        "display_site": display_site,
        "source_site": source_site,
        **origin,
        "review_locale": (
            raw.get("ContentLocale")
            or raw.get("contentLocale")
            or raw.get("Locale")
            or raw.get("locale")
            or product.get("review_locale")
            or ""
        ),
        "market_region": normalize_market_region(product.get("market_region")),
        "market_region_evidence": "source_market",
        "review_word_count": word_count,
        "is_valid_text_review": bool(str(review_text).strip()),
        "is_eligible": not excluded_ego_origin,
        "exclusion_reason": (
            "syndicated_from_ego_official" if excluded_ego_origin else ""
        ),
    }


def parse_public_bv_loader(source):
    """Parse the public configuration embedded in Bazaarvoice's legacy loader."""
    passkey = re.search(r'passkey:"([^"]+)"', source)
    display_code = re.search(r'displaycode:"([^"]+)"', source, re.I)
    locale_block = re.search(r"contentLocales:\[([^]]*)\]", source)
    locales = re.findall(r'"([^"]+)"', locale_block.group(1)) if locale_block else []
    if not passkey or not locales:
        raise RuntimeError("Bazaarvoice public loader did not expose its API configuration")
    return {
        "passkey": passkey.group(1),
        "display_code": display_code.group(1) if display_code else "",
        "content_locales": locales,
    }


def parse_public_bv_deployment(source):
    """Parse old and current public loader routing without persisting keys."""
    legacy = re.search(r'legacyScoutUrl:"([^"]+)"', source)
    api_config = re.search(
        r'configUrl:"([^"]+/api-config\.js)"', source, re.I
    )
    client = re.search(r'client:"([^"]+)"', source)
    site = re.search(r'site:"([^"]+)"', source)
    locale = re.search(r'locale:"([^"]+)"', source)
    display_code = re.search(r'displayCode:"([^"]+)"', source)
    if not legacy:
        raise RuntimeError(
            "Bazaarvoice deployment did not expose a public compatibility loader"
        )
    return {
        "loader_url": legacy.group(1),
        "api_config_url": api_config.group(1) if api_config else "",
        "client": client.group(1) if client else "",
        "site": site.group(1) if site else "",
        "locale": locale.group(1) if locale else "",
        "display_code": display_code.group(1) if display_code else "",
        "runtime": "config_url_v2" if api_config else "legacy_loader",
    }


def parse_public_bv_api_config(source):
    """Read only non-secret deployment identity from current api-config.js."""
    match = re.search(r'BV\["api"\]\.configure\((\{.*?\})\);', source, re.S)
    if not match:
        raise RuntimeError("Bazaarvoice api-config did not expose public identity")
    try:
        config = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bazaarvoice api-config identity was invalid") from exc
    if not isinstance(config, dict) or not config.get("client") or not config.get("site"):
        raise RuntimeError("Bazaarvoice api-config identity was incomplete")
    return {
        "client": str(config["client"]),
        "site": str(config["site"]),
        "api_access_allowed": bool(
            (config.get("features") or {}).get("apiAccessAllowed")
        ),
    }


def official_bv_product_id(channel, url, source):
    """Resolve the reviewed PDP's own Bazaarvoice product identifier."""
    if channel == "ego":
        from .ego_official import is_us_product_url, primary_product
        if is_us_product_url(url):
            return primary_product(url, source)["marker_id"] or ""
    if channel == "ryobi":
        # Recommendation tiles also expose IDs. Only the primary summary is
        # identity evidence on the US storefront; never take the first marker.
        tags = re.findall(r"<[^>]*data-bv-show=[\"']rating_summary[\"'][^>]*>", source, re.I)
        identities = {m.group(1).strip() for tag in tags
                      if (m := re.search(r"data-bv-product-id=[\"']([^\"']+)", tag, re.I)) and m.group(1).strip()}
        return next(iter(identities)) if len(identities) == 1 else ""
    if channel == "milwaukee":
        # New official model shapes (e.g. MT315) need an exact primary Product
        # URL and manufacturer part number. Arbitrary route slugs remain invalid.
        records = []
        for raw in re.findall(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", source, re.I | re.S):
            try:
                from .source_product_metadata import _walk_json, _url_key
                records.extend(_walk_json(json.loads(raw)))
            except (ValueError, TypeError):
                continue
        identities = {str(row.get('mpn') or '').strip() for row in records
                      if isinstance(row.get('url'), str) and _url_key(row['url']) == _url_key(url)
                      and str(row.get('mpn') or '').strip().casefold() == urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].casefold()
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{2,63}', str(row.get('mpn') or '').strip())}
        if len(identities) == 1:
            return next(iter(identities))
    page_product = re.search(r'data-bv-product-id=["\']([^"\']+)', source, re.I)
    if page_product:
        return page_product.group(1)
    if channel == "tractorsupply":
        candidates = set(
            re.findall(
                r'["\'](?:productAgilityId|productAgiltyId)["\']\s*:\s*["\'](\d{6,15})["\']',
                source,
                re.I,
            )
        )
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise RuntimeError(
                "tractorsupply: official PDP exposed ambiguous product ids"
            )
    parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    if channel == "milwaukee":
        # Milwaukee's exact PDP route is /products/details/<slug>/<model>.
        # Its public catalog uses several documented model shapes, including
        # tool/kit suffixes, MX FUEL IDs and three-part accessory numbers.  The
        # route is product identity evidence; category paths and arbitrary
        # slug tokens are not.
        try:
            details_position = [part.casefold() for part in parts].index(
                "details"
            )
        except ValueError:
            details_position = -1
        if details_position >= 0 and details_position + 2 == len(parts) - 1:
            candidate = parts[-1]
            if re.fullmatch(
                r"(?:\d{2}-\d{2}-\d{4}|\d{4}-[A-Za-z0-9]{1,4}|(?=[A-Za-z0-9-]*\d)[A-Za-z]{2,}[A-Za-z0-9]*-[A-Za-z0-9]+)",
                candidate,
            ):
                return candidate.upper()
    if channel == "dewalt":
        match = next((part for part in parts if re.fullmatch(r"DC[A-Z0-9-]{4,}", part, re.I)), None)
        if match:
            return match.upper()
    if channel == "einhell":
        match = re.search(r"/p/(\d{6,15})(?:[-/]|$)", urlsplit(url).path, re.I)
        if match:
            return match.group(1)
    return ""


def skil_bare_tool_family_key(product_id):
    """Return the stable bare-tool family portion of a SKIL SKU."""
    value = str(product_id or "").strip().casefold()
    match = re.fullmatch(r"(.+)-\d{2}", value)
    return match.group(1) if match else value


def skil_product_in_bare_tool_scope(source_product_id, observed_product_id):
    """Accept kit suffix variants of one bare tool, not unrelated bundles."""
    source = str(source_product_id or "").strip().casefold()
    observed = str(observed_product_id or "").strip().casefold()
    return bool(observed) and (
        observed == source
        or skil_bare_tool_family_key(observed)
        == skil_bare_tool_family_key(source)
    )


def milwaukee_product_in_official_family_scope(
    source_product_id,
    observed_product_id,
    payload,
):
    """Require public Bazaarvoice family evidence for a Milwaukee variant.

    Milwaukee's official ProductId filter intentionally returns reviews for
    bare-tool and kit ProductIds in one pool.  The public response names that
    relationship in ``Includes.Products[*].FamilyIds``.  Do not infer a family
    from a model prefix, title, brand, or URL: a different ProductId is accepted
    only when the source explicitly places both products in a shared family.
    """
    source = str(source_product_id or "").strip().casefold()
    observed = str(observed_product_id or "").strip().casefold()
    if not source or not observed:
        return False
    if observed == source:
        return True
    includes = payload.get("Includes") or payload.get("includes") or {}
    products = includes.get("Products") or includes.get("products") or {}
    if isinstance(products, list):
        candidates = products
    elif isinstance(products, dict):
        candidates = list(products.values())
    else:
        return False
    family_sets = {}
    for product in candidates:
        if not isinstance(product, dict):
            continue
        product_id = str(
            product.get("Id") or product.get("id") or ""
        ).strip().casefold()
        if product_id not in {source, observed}:
            continue
        family_ids = product.get("FamilyIds") or product.get("familyIds")
        if not isinstance(family_ids, list):
            return False
        family_sets[product_id] = {
            str(value or "").strip().casefold() for value in family_ids
            if str(value or "").strip()
        }
    return bool(
        family_sets.get(source)
        and family_sets.get(observed)
        and family_sets[source] & family_sets[observed]
    )


def norm_record(channel, product, raw):
    c = raw.get("ContextDataValues") or raw.get("contextDataValues") or {}
    def get_ctx(name):
        item = c.get(name) or {}
        return item.get("Value") or item.get("value") or ""
    badges = raw.get("Badges") or raw.get("badges") or {}
    rid = raw.get("Id") or raw.get("id") or ""
    record = {
        "channel": channel, "product_id": product["id"], "product_title": product["title"],
        "product_url": product["url"], "review_id": str(rid),
        "source_site": product.get("source_site", channel),
        "source_product_id": raw.get("ProductId") or raw.get("productId") or product.get("source_product_id", product["id"]),
        "is_syndicated": raw.get("IsSyndicated") if "IsSyndicated" in raw else raw.get("isSyndicated", ""),
        "submission_time": raw.get("SubmissionTime") or raw.get("submissionTime") or "",
        "title": raw.get("Title") or raw.get("title") or "",
        "is_recommended": raw.get("IsRecommended") if "IsRecommended" in raw else raw.get("isRecommended", ""),
        "user_nickname": raw.get("UserNickname") or raw.get("userNickname") or "",
        "review_text": raw.get("ReviewText") or raw.get("reviewText") or "",
        "rating": raw.get("Rating") or raw.get("rating") or "",
        "verified_purchaser": get_ctx("VerifiedPurchaser") or ("True" if "verifiedPurchaser" in badges else ""),
        "where_purchase": get_ctx("WherePurchase"), "age": get_ctx("Age"),
        "is_translated": (
            raw.get("IsTranslated")
            if "IsTranslated" in raw
            else raw.get("isTranslated", "")
        ),
        "raw": raw,
    }
    return apply_review_contract(channel, product, raw, record)


def norm_exmark_record(product, raw):
    details = raw.get("details") or {}
    metrics = raw.get("metrics") or {}
    badges = raw.get("badges") or {}
    created = details.get("created_date")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(
            created / 1000.0, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    adapted = {
        "Id": str(raw.get("review_id") or raw.get("ugc_id") or ""),
        "ProductId": product["source_product_id"],
        "SubmissionTime": created or "",
        "Title": details.get("headline") or "",
        "ReviewText": details.get("comments") or "",
        "Rating": metrics.get("rating") or "",
        "UserNickname": details.get("nickname") or "",
        "IsRecommended": details.get("bottom_line") or "",
        "ContentLocale": details.get("locale") or "en_US",
        "Badges": (
            {"verifiedPurchaser": {}}
            if badges.get("is_verified_buyer")
            else {}
        ),
        "_powerreviews": raw,
    }
    return norm_record("exmark", product, adapted)


def walmart_item_id_from_url(url):
    path = urlsplit(str(url or "")).path.rstrip("/")
    match = re.search(r"/(?:ip/(?:[^/]+/)?|reviews/product/)(\d{8,15})$", path)
    return match.group(1) if match else ""


def parse_walmart_next_data(source, expected_item_id, expected_page):
    marker = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        str(source or ""),
        re.I | re.S,
    )
    if not marker:
        raise OfficialSourceError(
            "walmart: official review page omitted __NEXT_DATA__",
            "product_identity_missing",
        )
    try:
        root = json.loads(marker.group(1))
        data = root["props"]["pageProps"]["initialData"]["data"]
        product = data["product"]
        reviews = data["reviews"]
        pagination = reviews["pagination"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OfficialSourceError(
            "walmart: official review page data was invalid",
            "source_contract_violation",
        ) from exc
    identity = walmart_review_identity(
        expected_item_id,
        {"item": {
            "canonicalUrl": product.get("canonicalUrl"),
            "lookupId": reviews.get("lookupId"),
        }},
    )
    source_total = reviews.get("reviewsWithTextCount")
    if source_total != pagination.get("total"):
        raise OfficialSourceError(
            "walmart: review total disagreed with pagination total",
            "source_total_drift",
        )
    rows = reviews.get("customerReviews")
    if not isinstance(rows, list):
        raise OfficialSourceError(
            "walmart: official review rows were missing",
            "source_contract_violation",
        )
    current_span = str(pagination.get("currentSpan") or "")
    expected_start = (int(expected_page) - 1) * WALMART_PAGE_SIZE + 1
    if int(source_total or 0) and not current_span.startswith(f"{expected_start}-"):
        raise OfficialSourceError(
            "walmart: official review page span did not match requested page",
            "product_identity_mismatch",
        )
    for raw in rows:
        if not str(raw.get("reviewId") or "").strip() or not str(
            raw.get("reviewText") or ""
        ).strip():
            raise OfficialSourceError(
                "walmart: official written review row was incomplete",
                "source_contract_violation",
            )
        item_id = str(raw.get("itemId") or "")
        if item_id and item_id != str(expected_item_id):
            raise OfficialSourceError(
                "walmart: official review row belonged to a different item",
                "product_identity_mismatch",
            )
    return {
        "item_id": str(expected_item_id),
        "lookup_id": identity.product_id,
        "title": str(product.get("name") or "").strip(),
        "source_total": int(source_total or 0),
        "rating_total": int(reviews.get("totalReviewCount") or 0),
        "page_number": int(expected_page),
        "rows": rows,
    }


def norm_walmart_record(product, raw):
    source = raw.get("syndicationSource") or {}
    adapted = {
        "Id": raw.get("reviewId"),
        "ProductId": product["source_product_id"],
        "SubmissionTime": raw.get("reviewSubmissionTime") or "",
        "Title": raw.get("reviewTitle") or "",
        "ReviewText": raw.get("reviewText") or "",
        "Rating": raw.get("rating") or "",
        "UserNickname": raw.get("userNickname") or "",
        "IsRecommended": raw.get("recommended", ""),
        "SyndicationSource": source,
        "ContentLocale": raw.get("translatedLocale") or raw.get("originalLocale") or "en_US",
        "_walmart": raw,
    }
    return norm_record("walmart", product, adapted)


def norm_ryobi_eu_record(product, raw):
    """Normalize the first-party RYOBI Europe review response.

    The EU storefront exposes written reviews through its public
    ``/api/productreviews/get`` endpoint.  Its field names differ from the
    Bazaarvoice schema used by the North American RYOBI site.
    """
    record = {
        "channel": "ryobi", "product_id": product["id"], "product_title": product["title"],
        "product_url": product["url"], "review_id": str(raw.get("guid") or raw.get("id") or ""),
        "source_site": urlsplit(product["url"]).netloc,
        "source_product_id": str(raw.get("productAgiltyId") or product["id"]),
        "is_syndicated": "",
        "submission_time": raw.get("prettyDate") or "",
        "title": raw.get("title") or "",
        "is_recommended": raw.get("recommendProduct") or "",
        "user_nickname": raw.get("firstName") or "",
        "review_text": raw.get("message") or "",
        "rating": raw.get("rating") or "",
        "verified_purchaser": "True" if raw.get("isVerified") else "",
        "where_purchase": "", "age": "",
        "is_translated": bool(raw.get("isTranslation")),
        "raw": raw,
    }
    return apply_review_contract("ryobi", product, raw, record)


def norm_harborfreight_record(product, raw):
    user = raw.get("user") or {}
    record = {
        "channel": "harborfreight",
        "product_id": product["id"],
        "product_title": product["title"],
        "product_url": product["url"],
        "review_id": str(raw.get("id") or ""),
        "source_site": "harborfreight.com",
        "source_product_id": product["id"],
        "is_syndicated": False,
        "submission_time": raw.get("dateCreated") or "",
        "title": raw.get("title") or "",
        "is_recommended": "",
        "user_nickname": user.get("nickName") or "",
        "review_text": raw.get("text") or "",
        "rating": raw.get("rating") or "",
        "verified_purchaser": "",
        "where_purchase": "",
        "age": "",
        "is_translated": False,
        "is_incentivized": bool(raw.get("incentivized")),
        "purchase_time": raw.get("purchaseDate") or "",
        "helpful_up_votes": raw.get("upVotes") or 0,
        "helpful_down_votes": raw.get("downVotes") or 0,
        "raw": raw,
    }
    return apply_review_contract("harborfreight", product, raw, record)


def norm_amazon_reviews_2023_record(product, raw):
    if not str(raw.get("asin") or "").strip():
        raise ValueError("Amazon archive review is missing asin")
    if not str(raw.get("user_id") or "").strip():
        raise ValueError("Amazon archive review is missing user_id")
    timestamp = raw.get("timestamp")
    if isinstance(timestamp, bool):
        raise ValueError("Amazon archive review has invalid timestamp")
    try:
        if int(timestamp) <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Amazon archive review has invalid timestamp"
        ) from exc
    rating = raw.get("rating")
    if isinstance(rating, bool):
        raise ValueError("Amazon archive review has invalid rating")
    try:
        numeric_rating = float(rating)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Amazon archive review has invalid rating"
        ) from exc
    if not 1 <= numeric_rating <= 5:
        raise ValueError("Amazon archive review rating is outside 1-5")

    normalized_text = " ".join(str(raw.get("text") or "").split()).casefold()
    identity = "|".join(
        (
            str(raw.get("user_id") or ""),
            str(raw.get("timestamp") or ""),
            str(raw.get("asin") or ""),
            hashlib.sha256(normalized_text.encode()).hexdigest(),
        )
    )
    review_id = hashlib.sha256(identity.encode()).hexdigest()
    try:
        submission_time = datetime.fromtimestamp(
            float(timestamp) / 1000,
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OverflowError):
        submission_time = str(timestamp or "")
    record = {
        "channel": "amazon_reviews_2023",
        "product_id": product["id"],
        "product_title": product["title"],
        "product_url": product["url"],
        "review_id": review_id,
        "source_site": "amazon.com",
        "source_product_id": product["id"],
        "review_asin": str(raw.get("asin") or ""),
        "is_syndicated": "",
        "submission_time": submission_time,
        "title": raw.get("title") or "",
        "is_recommended": "",
        "user_nickname": "",
        "review_text": raw.get("text") or "",
        "rating": raw.get("rating") or "",
        "verified_purchaser": raw.get("verified_purchase", ""),
        "where_purchase": "",
        "age": "",
        "is_translated": False,
        "helpful_up_votes": raw.get("helpful_vote") or 0,
        "archive_source_revision": product.get("source_revision") or "",
        "archive_through": product.get("archive_through") or "",
        "provenance_limitations": (
            "static archive has no review-level syndication-origin field"
        ),
        "raw": raw,
    }
    return apply_review_contract(
        "amazon_reviews_2023", product, raw, record
    )


class BvTemplate:
    def __init__(self, kind, url, headers, all_content_locales=False):
        self.kind, self.url = kind, url
        self.all_content_locales = all_content_locales
        self.headers = {k: v for k, v in headers.items() if k.lower() in {
            "accept", "origin", "referer", "user-agent", "bv-bfd-token", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}}
        self.parts = urlsplit(url)
        self.pairs = parse_qsl(self.parts.query, keep_blank_values=True)

    def product_id(self):
        for k, v in self.pairs:
            if k.lower() == "filter" and "productid" in v.lower():
                return v.split(":")[-1]
        decoded = unquote(self.url)
        patterns = (r"productid(?:=|:|%3a)([A-Za-z0-9_-]+)",
                    r"filter\.q\d+=(?:id|productid)(?:%3a|:)(?:eq%3a|eq:)([A-Za-z0-9_-]+)")
        for pattern in patterns:
            match = re.search(pattern, decoded, re.I)
            if match:
                return match.group(1)
        return None

    def page_url(self, product_id, offset):
        if self.kind == "bfd":
            pairs = []
            for k, v in self.pairs:
                low = k.lower()
                if low in {"offset", "limit", "sort"} or (low == "filter" and "isratingsonly" in v.lower()):
                    continue
                if low == "filter" and "productid" in v.lower():
                    v = f"productid:eq:{product_id}"
                pairs.append((k, v))
            pairs += [("sort", "submissiontime:desc"), ("offset", str(offset)), ("limit", str(PAGE_SIZE))]
            return urlunsplit((self.parts.scheme, self.parts.netloc, self.parts.path, urlencode(pairs, safe=":,*"), ""))
        passkey = next((v for k, v in self.pairs if k.lower() == "passkey"), None)
        api_version = next((v for k, v in self.pairs if k.lower() == "apiversion"), "5.5")
        display_code = next((v for k, v in self.pairs if k.lower() == "displaycode"), None)
        locale = next(
            (
                v.split(":", 1)[1]
                for k, v in self.pairs
                if k.lower() == "filter" and v.lower().startswith("contentlocale:")
            ),
            next((v for k, v in self.pairs if "locale" in k.lower()), None),
        )
        pairs = [("apiversion", api_version), ("passkey", passkey)]
        if display_code:
            pairs.append(("displaycode", display_code))
        pairs.append(("Filter", f"ProductId:{product_id}"))
        if not self.all_content_locales:
            pairs.append(("Filter", f"ContentLocale:{locale or 'en_US'}"))
        pairs += [("Sort", "SubmissionTime:desc"), ("Limit", str(PAGE_SIZE)),
                  ("Offset", str(offset)), ("Include", "Products"), ("Stats", "Reviews")]
        return "https://api.bazaarvoice.com/data/reviews.json?" + urlencode(pairs, safe=":,")

    def parse(self, data):
        if self.kind == "bfd":
            data = data.get("response") or {}
        return data.get("Results") or [], data.get("TotalResults") or 0


class Runner:
    def __init__(self, store):
        self.store = store
        self._metadata_client = SourceMetadataClient(getattr(store, "data_dir", Path(os.getenv("DATA_DIR", "data"))))
        self.session = requests.Session()
        self.official_session_factory = requests.Session
        self.session.headers["user-agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136 Safari/537.36"
        # Firecrawl calls run in a worker thread so one slow Lowe's request
        # cannot block the other channel workers. Keep their HTTP session
        # isolated because requests.Session is not a cross-thread contract.
        self.lowes_session = requests.Session()
        self.lowes_session.headers.update(self.session.headers)

    @property
    def metadata_client(self):
        # Offline adapter probes may construct Runner without __init__.
        if not hasattr(self, '_metadata_client'):
            self._metadata_client = SourceMetadataClient(getattr(getattr(self, 'store', None), 'data_dir', Path(os.getenv('DATA_DIR', 'data'))))
        return self._metadata_client

    def official_public_get(self, url, **kwargs):
        """Issue one stateless official-source request with no cookie jar."""
        headers = {
            "user-agent": self.session.headers.get("user-agent", ""),
            "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            **dict(kwargs.pop("headers", {}) or {}),
        }
        session = self.official_session_factory()
        session.trust_env = False
        if hasattr(session, "headers"):
            session.headers.clear()
        try:
            return session.get(url, headers=headers, **kwargs)
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                close()

    async def run(
        self,
        job_id,
        action,
        channel,
        target_url=None,
        market_region=None,
        source_product_id=None,
        expected_category=None,
        expected_model=None,
    ):
        channels = CHANNELS if channel == "all" else {channel}
        out = {}
        for name in sorted(channels):
            self.store.update(job_id, f"{name}: discovering candidates")
            run_dir = self.store.data_dir / "runs" / job_id / name
            if target_url:
                product = await self.product_summary(
                    name,
                    target_url,
                    job_id=job_id,
                    expected_product_id=(
                        source_product_id
                        if name in {"tractorsupply", "einhell", "skil"}
                        else None
                    ),
                    expected_category=expected_category if name == "tractorsupply" else None,
                    expected_model=expected_model if name == "tractorsupply" else None,
                )
                if not product:
                    raise RuntimeError(f"{name}: unable to read the requested product")
                self.persist_source_product_identity(job_id, product)
                candidates = [product]
            else:
                product, candidates = await self.discover_product(name)
            product["market_region"] = (
                validate_channel_market_region(name, market_region)
                if name == "amazon_reviews_2023"
                else validate_channel_market_region(name, market_region or infer_market_region(target_url or product.get("url")))
            )
            if action == 'scrape':
                self.enrich_product_metadata(job_id, product)
            public_product = lambda item: {
                key: value for key, value in item.items() if not key.startswith("_")
            }
            # Do not leave an empty active-root directory when product
            # discovery fails before any attempt artifact exists.  Once a
            # selection is proven, the directory becomes owned by this
            # attempt and failed retries can archive it explicitly.
            run_dir.mkdir(parents=True, exist_ok=True)
            self.write_json(
                run_dir / "selection_manifest.json",
                {
                    "created_at": now(),
                    "channel": name,
                    "selected": public_product(product),
                    "candidates": [public_product(item) for item in candidates],
                },
            )
            if action == "discover":
                out[name] = {"selected": product, "candidate_count": len(candidates), "artifact": f"{name}/selection_manifest.json"}
                continue
            self.store.update(job_id, f"{name}: collecting reviews for {product['id']}")
            records, source_total, notes = await self.collect(name, product, job_id)
            report = self.persist_and_validate(run_dir, name, product, records, source_total, notes)
            if report.get("status") != "PARTIAL_SUCCESS":
                self.ensure_report_complete(name, report)
            out[name] = report
            self.store.update(job_id, f"{name}: {report['status']} ({report['unique_reviews']}/{source_total})")
        return out

    def persist_source_product_identity(self, job_id, product):
        product_id = str(
            product.get("source_product_id") or product.get("id") or ""
        ).strip()
        if not product_id:
            raise RuntimeError("official source did not expose a product identity")
        bv_evidence = (
            product.get("bv_evidence")
            or product.get("official_source_evidence")
            or {}
        )
        evidence_source = (
            bv_evidence.get("product_identity_evidence")
            or product.get("product_identity_evidence")
            or product.get("collection_method")
            or "official_source_response"
        )
        normalized_source = {
            "pdp_data_bv_product_id": "pdp_public_product_marker",
            "pdp_current_product_bv_id": "pdp_public_product_marker",
            "official_page_public_marker": "pdp_public_product_marker",
            "pdp_item_number": "pdp_public_product_marker",
            "pdp_canonical_url": "pdp_public_product_marker",
            "shopify_product_id_bazaarvoice_marker": (
                "pdp_public_product_marker"
            ),
        }.get(evidence_source, evidence_source)
        identity = resolved_source_product_identity(
            product_id,
            normalized_source,
            f"{product.get('source_site') or 'official_source'}:{evidence_source}",
        )
        return self.store.record_source_product_identity(job_id, identity)

    def enrich_product_metadata(self, job_id, product):
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError('采集任务不存在')
        context = collected_product_context(job, product)
        metadata = context.pop('metadata')
        source_verified = context.pop('source_verified')
        self.store.enrich_metadata(
            job_id, metadata['brand'], metadata['model'], metadata['category'],
            source_verified=source_verified, market_region=metadata['market_region'], **context,
        )
        return context

    @staticmethod
    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def ensure_report_complete(channel, report):
        """Never present a partial source result as a completed collection."""
        if report.get("status") in {"COMPLETE", "COMPLETE_FALLBACK_SOURCE"}:
            return
        message = (
            f"{channel} pagination did not reconcile: "
            f"{report.get('unique_reviews', 0)}/{report.get('source_total', 0)}"
        )
        if channel == "lowes":
            raise RetryableChannelError(
                f"{message}; checkpoint retained and task kept queued",
                retry_after=int(os.getenv("LOWES_INCOMPLETE_RETRY_SECONDS", "60")),
            )
        raise RuntimeError(message)

    async def browser(self):
        p = await async_playwright().start()
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"}
        browser = await p.chromium.launch(
            headless=headless,
            executable_path=os.getenv("BROWSER_EXECUTABLE") or None,
            args=[],
        )
        context_options = {
            "viewport": {"width": 1440, "height": 1200},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if headless:
            # Keep UA and Chromium client hints on the same major version.  The
            # old fixed Chrome/136 UA with Chromium/150 was an Akamai signal.
            context_options["user_agent"] = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
            )
        context = await browser.new_context(**context_options)
        return p, browser, context

    async def candidate_links(self, channel):
        """Collect product links from public category pages. This is evidence, not a top-seller shortcut."""
        if channel in {"walmart", "exmark"}:
            raise RuntimeError(f"{channel}: explicit product URL is required")
        if channel in {"skil", "ryobi"}:
            return await self.sitemap_links(channel)
        if channel in PUBLIC_BV_CHANNELS:
            raise RuntimeError(f"{channel}: explicit product URL is required")
        if channel in {"ego", "ryobi"}:
            return await self.sitemap_links(channel)
        if channel == "ace":
            # Ace renders its category catalog as embedded JSON rather than product anchors.
            # A productCode is sufficient for its canonical category/product URL.
            response = self.session.get(CATEGORY_URLS[channel], timeout=45)
            response.raise_for_status()
            codes = list(dict.fromkeys(re.findall(r'"productCode":"([A-Za-z0-9]+)"', response.text)))
            return [f"{CATEGORY_URLS[channel]}/{code}" for code in codes]
        base = CATEGORY_URLS[channel]
        p, browser, context = await self.browser()
        try:
            page = await context.new_page()
            await page.goto(base, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(5000)
            title = await page.title()
            if "denied" in title.lower() or "just a moment" in title.lower():
                raise RuntimeError(f"{channel} category access challenge: {title}")
            for _ in range(12):
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(600)
            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        finally:
            await context.close(); await browser.close(); await p.stop()
        if channel == "lowes":
            links = [h for h in hrefs if "/pd/" in h and "leaf" not in h.lower() or "/pd/" in h]
        elif channel == "homedepot":
            links = [h for h in hrefs if "/p/" in h and re.search(r"/\d{8,}(?:[?#]|$)", h)]
        else:
            links = [h for h in hrefs if "/departments/" in h and re.search(r"/\d{5,}(?:[?#]|$)", h)]
        return list(dict.fromkeys(clean_url(h) for h in links))

    async def sitemap_links(self, channel):
        if channel == "skil":
            response = self.session.get(SITEMAPS[channel], timeout=45)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("skil: official Shopify product index was not JSON") from exc
            links = []
            for product in payload.get("products") or []:
                handle = str(product.get("handle") or "").strip()
                if handle:
                    links.append(clean_url(f"https://www.skil.com/products/{handle}"))
            return list(dict.fromkeys(links))
        root = self.session.get(SITEMAPS[channel], timeout=30)
        root.raise_for_status()
        urls = re.findall(r"<loc>(.*?)</loc>", root.text)
        nested = [u for u in urls if "sitemap" in u][:80]
        if nested:
            urls = []
            for u in nested:
                try:
                    text = self.session.get(u, timeout=30).text
                    urls += re.findall(r"<loc>(.*?)</loc>", text)
                except requests.RequestException:
                    continue
        if channel == "ego":
            # Only leaf-blower product detail URLs: omit snow blowers, tubes, straps,
            # replacement parts and trimmer bundles that happen to contain "blower".
            product = re.compile(r"/(?:blower-(?:480|530|575|580|600|650)cfm|blower-(?:615|765|670)cfm-lb\d+|commercial-800cfm-backpack-blower-lbpx8000|power-880-cfm-blower-lb8800|leaf-vacuum-mulcher-500-cfm-blower-lv5005)/?$", re.I)
            return [clean_url(u) for u in urls if product.search(urlsplit(u).path)]
        return [clean_url(u) for u in urls if re.search(r"blower|leaf[-_ ]?blower", u, re.I)]

    async def discover_product(self, channel):
        links = await self.candidate_links(channel)
        if not links:
            raise RuntimeError(f"{channel}: no blower product links found")
        # Some category pages expose only the first viewport. The service records this limitation
        # rather than claiming a complete catalog; sitemap channels are exhaustive when available.
        candidates = []
        for index, url in enumerate(links[:120]):
            try:
                product = await self.product_summary(channel, url)
                if product and product["total"] is not None:
                    candidates.append(product)
            except Exception:
                continue
            if index % 10 == 0:
                await asyncio.sleep(0.2)
        if not candidates:
            raise RuntimeError(f"{channel}: no review totals could be read from {len(links)} candidates")
        candidates.sort(key=lambda x: x["total"], reverse=True)
        selected = {k: v for k, v in candidates[0].items() if k != "total"}
        selected["reported_total"] = candidates[0]["total"]
        return selected, candidates

    def tractor_supply_public_get(self, url, label, **kwargs):
        """Read one TSC/BV public endpoint and retain a sanitized failure class."""
        try:
            response = self.official_public_get(url, **kwargs)
        except requests.Timeout as exc:
            raise TractorSupplyTransportError(
                f"tractorsupply: {label} timed out",
                retry_after=int(os.getenv("TRACTOR_SUPPLY_RETRY_AFTER_SECONDS", "900")),
            ) from exc
        except requests.RequestException as exc:
            raise TractorSupplyTransportError(
                f"tractorsupply: {label} transport failed",
                retry_after=int(os.getenv("TRACTOR_SUPPLY_RETRY_AFTER_SECONDS", "900")),
            ) from exc
        if response.status_code in {401, 403}:
            raise TractorSupplyAccessError(
                f"tractorsupply: {label} access denied",
                failure_class="access_denied",
                cooldown_seconds=int(
                    os.getenv("TRACTOR_SUPPLY_ACCESS_COOLDOWN_SECONDS", "21600")
                ),
            )
        if response.status_code == 429:
            raise TractorSupplyTransportError(
                f"tractorsupply: {label} rate limited",
                retry_after=int(os.getenv("TRACTOR_SUPPLY_RETRY_AFTER_SECONDS", "900")),
                failure_class="access_denied",
            )
        if response.status_code >= 500:
            raise TractorSupplyTransportError(
                f"tractorsupply: {label} upstream unavailable",
                retry_after=int(os.getenv("TRACTOR_SUPPLY_RETRY_AFTER_SECONDS", "900")),
                failure_class="upstream_5xx",
            )
        try:
            guard_public_response("tractorsupply", response, label)
        except OfficialSourceError as exc:
            failure_class = str(
                getattr(exc, "failure_class", "source_contract_violation")
            )
            raise TractorSupplyAccessError(
                str(exc),
                failure_class=failure_class,
                cooldown_seconds=(
                    int(
                        os.getenv(
                            "TRACTOR_SUPPLY_ACCESS_COOLDOWN_SECONDS",
                            "21600",
                        )
                    )
                    if failure_class
                    in {"access_denied", "anti_bot_challenge"}
                    else 0
                ),
            ) from exc
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TractorSupplyTransportError(
                f"tractorsupply: {label} returned an unusable response",
                retry_after=int(os.getenv("TRACTOR_SUPPLY_RETRY_AFTER_SECONDS", "900")),
            ) from exc
        return response

    @staticmethod
    def tractor_supply_item_number(url):
        match = re.search(r"-(\d{6,8})/?$", urlsplit(url).path)
        return match.group(1) if match else ""

    @staticmethod
    def tractor_supply_catalog_path(url, item_number=""):
        path = unquote(urlsplit(str(url or "")).path).rstrip("/")
        if item_number and path.endswith(f"-{item_number}"):
            path = path[: -(len(item_number) + 1)]
        return re.sub(r"/+", "/", path).casefold()

    def tractor_supply_catalog_identity(
        self,
        url,
        config,
        *,
        expected_product_id="",
    ):
        """Verify a candidate against the official BV product catalog."""
        item_number = self.tractor_supply_item_number(url)
        candidate = str(expected_product_id or "")
        candidate_source = "stored_verified_source_product_id"
        if not candidate:
            if not item_number:
                raise TractorSupplyAccessError(
                    "tractorsupply: target URL omitted a verifiable item number",
                    failure_class="product_evidence_missing",
                    cooldown_seconds=0,
                )
            # This is a lookup candidate only. It is never accepted unless the
            # official Products API returns the exact ID and canonical PDP URL.
            candidate = f"{item_number}99"
            candidate_source = "url_item_candidate_verified_by_official_catalog"
        if not re.fullmatch(r"\d{6,15}", candidate):
            raise TractorSupplyAccessError(
                "tractorsupply: stored source identity was not a numeric BV ProductId",
                failure_class="product_identity_mismatch",
                cooldown_seconds=0,
            )
        params = [
            ("apiversion", "5.5"),
            ("passkey", config["passkey"]),
            ("Filter", f"Id:eq:{candidate}"),
            ("Stats", "Reviews"),
            ("Limit", "2"),
        ]
        response = self.tractor_supply_public_get(
            "https://api.bazaarvoice.com/data/products.json?"
            + urlencode(params, safe=":"),
            "official product catalog",
            timeout=30,
        )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TractorSupplyAccessError(
                "tractorsupply: official product catalog returned invalid JSON",
                failure_class="source_contract_violation",
                cooldown_seconds=0,
            ) from exc
        if payload.get("Errors"):
            raise TractorSupplyAccessError(
                "tractorsupply: official product catalog returned source errors",
                failure_class="source_contract_violation",
                cooldown_seconds=0,
            )
        products = payload.get("Results")
        if not isinstance(products, list) or len(products) != 1:
            raise TractorSupplyAccessError(
                "tractorsupply: official product catalog did not prove one exact product",
                failure_class="product_evidence_missing",
                cooldown_seconds=0,
            )
        product = products[0]
        product_id = str(product.get("Id") or "")
        product_url = str(product.get("ProductPageUrl") or "")
        host = urlsplit(product_url).netloc.casefold().removeprefix("www.")
        if (
            product_id != candidate
            or host != "tractorsupply.com"
            or self.tractor_supply_catalog_path(product_url)
            != self.tractor_supply_catalog_path(url, item_number)
        ):
            raise TractorSupplyAccessError(
                "tractorsupply: official product catalog identity did not match target URL",
                failure_class="product_identity_mismatch",
                cooldown_seconds=0,
            )
        title = clean_html_text(product.get("Name") or "")
        if not title:
            raise TractorSupplyAccessError(
                "tractorsupply: official product catalog omitted the product title",
                failure_class="product_evidence_missing",
                cooldown_seconds=0,
            )
        return {
            "product_id": product_id,
            "title": title,
            "product_url": product_url,
            "item_number": item_number,
            "identity_evidence": candidate_source,
        }

    @staticmethod
    def validate_tractor_supply_category(expected_category, title, url):
        """Reject a proven Power Cleaner from a Pressure Washer task; never guess."""
        category = str(expected_category or "").strip().casefold()
        evidence = f"{title} {unquote(urlsplit(url).path)}".casefold()
        power_cleaner = any(
            marker in evidence
            for marker in ("power cleaner", "ezclean", "hydroshot")
        )
        if category == "power cleaner" or power_cleaner:
            raise TractorSupplyAccessError(
                "tractorsupply: official product evidence identifies an out-of-scope Power Cleaner",
                failure_class="category_out_of_scope",
                cooldown_seconds=0,
            )
        if not category:
            return (
                "official_evidence_pressure_washer_unbound_task"
                if "pressure washer" in evidence
                else "unbound"
            )
        if category != "pressure washer":
            return "not_applicable"
        if "pressure washer" not in evidence:
            raise TractorSupplyAccessError(
                "tractorsupply: official product evidence did not prove Pressure Washer category",
                failure_class="product_evidence_missing",
                cooldown_seconds=0,
            )
        return "official_title_and_url_pressure_washer"

    @staticmethod
    def validate_tractor_supply_model(expected_model, title, url):
        """Require an operator model to be present in official catalog evidence."""
        if not str(expected_model or "").strip():
            return "unbound"
        expected = re.sub(r"[^A-Z0-9]+", "", str(expected_model).upper())
        observed = re.sub(
            r"[^A-Z0-9]+",
            "",
            f"{title} {unquote(urlsplit(url).path)}".upper(),
        )
        if len(expected) < 3 or expected not in observed:
            raise TractorSupplyAccessError(
                "tractorsupply: official product evidence did not match stored model",
                failure_class="model_evidence_mismatch",
                cooldown_seconds=0,
            )
        return "official_catalog_title_or_url_model"

    def discover_public_bv(
        self,
        channel,
        url,
        expected_product_id="",
        expected_category=None,
        expected_model=None,
    ):
        """Build a stable API template from an official page's public BV loader.

        Milwaukee, DEWALT, and EGO publish both values in PDP source. Tractor
        Supply uses its reviewed product-card identifier plus the retailer's
        fixed public deployment. Reading that configuration directly is more
        reliable than waiting for consent-managed browser JavaScript to emit a
        review request, while retaining the same first-party source.
        """
        all_content_locales = False
        deployment_discovery = "official_page_source"
        product_identity_evidence = "official_page_public_marker"
        tsc_pdp_error = None
        tsc_catalog_identity = None
        if channel == "tractorsupply":
            source = ""
            page_product_id = ""
            try:
                page_response = self.tractor_supply_public_get(
                    url, "official PDP", timeout=30
                )
                source = page_response.text
                page_product_id = official_bv_product_id(channel, url, source) or ""
                if expected_product_id and page_product_id and str(expected_product_id) != str(page_product_id):
                    raise TractorSupplyAccessError(
                        "tractorsupply: official PDP product evidence did not match stored identity",
                        failure_class="product_identity_mismatch",
                        cooldown_seconds=0,
                    )
            except (TractorSupplyTransportError, TractorSupplyAccessError) as exc:
                if page_product_id:
                    raise
                tsc_pdp_error = exc
            deployment_url = TRACTOR_SUPPLY_BV_DEPLOYMENT_URL
            deployment_discovery = "governed_official_channel_mapping"
            product_identity_evidence = "pending_official_catalog_reconciliation"
        elif channel == "skil":
            if expected_product_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,31}", str(expected_product_id)):
                raise ValueError("skil source_product_id must be a stable official SKU/ProductId")
            page_response = self.session.get(url, timeout=30)
            page_response.raise_for_status()
            source = page_response.text
            page_product_id = official_bv_product_id(channel, url, source)
            if not page_product_id:
                raise RuntimeError("skil: official PDP did not expose data-bv-product-id")
            if expected_product_id and str(page_product_id) != str(expected_product_id):
                raise RuntimeError("skil: official PDP data-bv-product-id did not match source_product_id")
            expected_product_id = page_product_id
            deployment_url = SKIL_BV_DEPLOYMENT_URL
            deployment_discovery = "governed_public_channel_mapping"
            product_identity_evidence = "pdp_data_bv_product_id"
        elif channel in OFFICIAL_STRICT_BV_CHANNELS:
            page_response = self.official_public_get(url, timeout=30)
            guard_public_response(channel, page_response, "official PDP")
            source = page_response.text
            identity = official_bv_identity(channel, url, source)
            if expected_product_id and str(expected_product_id) != identity.product_id:
                raise OfficialSourceError(
                    f"{channel}: official PDP identity changed",
                    "product_identity_mismatch",
                )
            expected_product_id = identity.product_id
            deployment_url = OFFICIAL_BV_DEPLOYMENTS.get(channel, "")
            if deployment_url:
                deployment_discovery = "governed_public_channel_mapping"
            else:
                deployment = re.search(
                    r"(?:https?:)?//apps\.bazaarvoice\.com/deployments/"
                    r"[^\"'\\\s<>]+/bv\.js",
                    source,
                    re.I,
                )
                if not deployment:
                    raise OfficialSourceError(
                        f"{channel}: official PDP omitted public Bazaarvoice loader",
                        "product_identity_missing",
                    )
                deployment_url = deployment.group(0)
                if deployment_url.startswith("//"):
                    deployment_url = f"https:{deployment_url}"
            product_identity_evidence = identity.evidence
        else:
            page_response = self.session.get(url, timeout=30)
            page_response.raise_for_status()
            source = page_response.text
            deployment = re.search(
                r"(?:https?:)?//apps\.bazaarvoice\.com/deployments/"
                r"[^\"'\\\s<>]+/bv\.js",
                source,
                re.I,
            )
            if deployment:
                deployment_url = deployment.group(0)
                if deployment_url.startswith("//"):
                    deployment_url = f"https:{deployment_url}"
                # Einhell UK publishes a public deployment in the consent-gated
                # script tag.  Do not trust an operator-supplied item number:
                # its official PDP must expose the same reviewed ProductId.
                # Applying this check to all direct Einhell hosts avoids a
                # silent cross-product collection if a PDP template changes.
                if channel in {"einhell", "skil"} and expected_product_id:
                    page_product_id = official_bv_product_id(channel, url, source)
                    if str(page_product_id) != str(expected_product_id):
                        raise RuntimeError(
                            "einhell: official page product evidence did not "
                            "match source_product_id"
                        )
            else:
                source_host = urlsplit(url).netloc.lower().removeprefix("www.")
                governed = (
                    EINHELL_GOVERNED_BV_DEPLOYMENTS.get(source_host)
                    if channel == "einhell"
                    else None
                )
                if not governed:
                    raise RuntimeError(
                        f"{channel}: official page did not expose its "
                        "Bazaarvoice deployment"
                    )
                expected_product_id = (
                    expected_product_id
                    or official_bv_product_id(channel, url, source)
                )
                if not expected_product_id:
                    raise RuntimeError(
                        "einhell: official PDP did not expose a product identity"
                    )
                if not re.fullmatch(r"\d{6,15}", str(expected_product_id)):
                    raise ValueError(
                        "einhell source_product_id must be a 6-15 digit "
                        "official item number"
                    )
                escaped_product_id = re.escape(str(expected_product_id))
                path_matches = re.search(
                    rf"/p/{escaped_product_id}(?:[-/]|$)",
                    urlsplit(url).path,
                    re.I,
                )
                item_number_matches = re.search(
                    rf'class=["\'][^"\']*\bdata-point-itemno\b[^"\']*["\']'
                    rf"[^>]*>\s*{escaped_product_id}\s*<",
                    source,
                    re.I,
                )
                canonical_matches = re.search(
                    rf'<link\b[^>]*\brel=["\']canonical["\'][^>]*'
                    rf'\bhref=["\'][^"\']*/p/{escaped_product_id}(?:[-/])',
                    source,
                    re.I,
                )
                if not path_matches or not (
                    item_number_matches or canonical_matches
                ):
                    raise RuntimeError(
                        "einhell: official page product evidence did not match "
                        "source_product_id"
                    )
                deployment_url = governed["deployment_url"]
                all_content_locales = bool(
                    governed.get("all_content_locales")
                )
                deployment_discovery = "governed_official_host_mapping"
                product_identity_evidence = (
                    "pdp_item_number"
                    if item_number_matches
                    else "pdp_canonical_url"
                )
        deployment_source = (
            self.tractor_supply_public_get(
                deployment_url, "public Bazaarvoice deployment", timeout=30
            )
            if channel == "tractorsupply"
            else
            self.official_public_get(deployment_url, timeout=30)
            if channel in OFFICIAL_STRICT_BV_CHANNELS
            else self.session.get(deployment_url, timeout=30)
        )
        if channel in OFFICIAL_STRICT_BV_CHANNELS:
            guard_public_response(channel, deployment_source, "public Bazaarvoice deployment")
        else:
            deployment_source.raise_for_status()
        deployment = parse_public_bv_deployment(deployment_source.text)
        loader_url = deployment["loader_url"]
        if loader_url.startswith("//"):
            loader_url = f"https:{loader_url}"
        if channel == "tractorsupply" and deployment["api_config_url"]:
            api_config_response = self.tractor_supply_public_get(
                deployment["api_config_url"], "public Bazaarvoice api-config", timeout=30
            )
            public_identity = parse_public_bv_api_config(api_config_response.text)
            if (
                public_identity["client"].casefold() != "tractorsupply"
                or public_identity["site"].casefold() != "new_pdp"
                or not public_identity["api_access_allowed"]
            ):
                raise TractorSupplyAccessError(
                    "tractorsupply: public Bazaarvoice runtime identity changed",
                    failure_class="source_contract_violation",
                    cooldown_seconds=0,
                )
        loader_response = (
            self.tractor_supply_public_get(
                loader_url, "public Bazaarvoice compatibility loader", timeout=30
            )
            if channel == "tractorsupply"
            else
            self.official_public_get(loader_url, timeout=30)
            if channel in OFFICIAL_STRICT_BV_CHANNELS
            else self.session.get(loader_url, timeout=30)
        )
        if channel in OFFICIAL_STRICT_BV_CHANNELS:
            guard_public_response(channel, loader_response, "public Bazaarvoice loader")
        else:
            loader_response.raise_for_status()
        config = parse_public_bv_loader(loader_response.text)
        if channel == "tractorsupply":
            if page_product_id:
                expected_product_id = page_product_id
                product_identity_evidence = "pdp_public_product_marker"
                title_match = re.search(
                    r"<title[^>]*>(.*?)</title>", source, re.I | re.S
                )
                tsc_title = (
                    clean_html_text(title_match.group(1))
                    if title_match
                    else self.tractor_supply_product_title(url, page_product_id)
                )
            else:
                try:
                    tsc_catalog_identity = self.tractor_supply_catalog_identity(
                        url,
                        config,
                        expected_product_id=expected_product_id,
                    )
                except TractorSupplyAccessError:
                    if tsc_pdp_error is not None:
                        raise tsc_pdp_error
                    raise
                expected_product_id = tsc_catalog_identity["product_id"]
                tsc_title = tsc_catalog_identity["title"]
                product_identity_evidence = tsc_catalog_identity["identity_evidence"]
            category_evidence = self.validate_tractor_supply_category(
                expected_category, tsc_title, url
            )
            model_evidence = self.validate_tractor_supply_model(
                expected_model, tsc_title, url
            )
        page_product_id = (
            official_bv_identity(channel, url, source).product_id
            if channel in OFFICIAL_STRICT_BV_CHANNELS
            else official_bv_product_id(channel, url, source)
        )
        ego_catalog_evidence = None
        if channel == "ego":
            from .ego_official import is_us_product_url, primary_product, verify_catalog_product
            if is_us_product_url(url):
                primary = primary_product(url, source)
                if not page_product_id:
                    if not deployment_url.startswith("https://apps.bazaarvoice.com/deployments/egopowerplus/"):
                        raise OfficialSourceError("ego: official review deployment identity changed", "product_identity_mismatch")
                    catalog_response = self.session.get(
                        "https://api.bazaarvoice.com/data/products.json",
                        params={"apiversion": "5.5", "passkey": config["passkey"],
                                "Filter": "Id:" + primary["sku"]},
                        headers={"referer": url}, timeout=30,
                    )
                    guard_public_response("ego", catalog_response, "official product catalog")
                    catalog_payload = catalog_response.json()
                    catalog_rows = catalog_payload.get("Results") or []
                    if len(catalog_rows) == 1:
                        catalog_url = catalog_rows[0].get("ProductPageUrl")
                        if catalog_url and is_us_product_url(catalog_url) and clean_url(catalog_url).rstrip("/").casefold() != clean_url(url).rstrip("/").casefold():
                            linked_page = self.session.get(catalog_url, timeout=30)
                            if linked_page.status_code != 404:
                                guard_public_response("ego", linked_page, "official catalog product page")
                            primary["catalog_url_verification"] = {
                                "url": catalog_url, "final_url": linked_page.url,
                                "status": linked_page.status_code,
                            }
                    ego_catalog_evidence = verify_catalog_product(primary, catalog_payload)
                    page_product_id = ego_catalog_evidence["product_id"]
                    product_identity_evidence = "official_primary_sku_exact_catalog_record"
                else:
                    product_identity_evidence = "primary_manufacturer_sku_bound_bv_marker"
                if expected_product_id and str(expected_product_id).casefold() != page_product_id.casefold():
                    raise OfficialSourceError("ego: primary product identity changed", "product_identity_mismatch")
                expected_product_id = page_product_id
        if channel == "milwaukee":
            if not page_product_id:
                raise RuntimeError(
                    "milwaukee: official page product id was not present"
                )
            if (
                expected_product_id
                and str(expected_product_id).casefold()
                != str(page_product_id).casefold()
            ):
                raise RuntimeError(
                    "milwaukee: official page product evidence did not "
                    "match source_product_id"
                )
            product_id = page_product_id
        else:
            product_id = expected_product_id or page_product_id
        if not product_id:
            raise RuntimeError(f"{channel}: official page product id was not present")
        pairs = [
            ("apiversion", "5.5"),
            ("passkey", config["passkey"]),
            ("Filter", f"ProductId:{product_id}"),
        ]
        if not all_content_locales:
            pairs.append(
                (
                    "Filter",
                    f"ContentLocale:{','.join(config['content_locales'])}",
                )
            )
        if config["display_code"]:
            pairs.append(("displaycode", config["display_code"]))
        template_url = "https://api.bazaarvoice.com/data/reviews.json?" + urlencode(pairs, safe=":,")
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", source, re.I | re.S
        )
        if channel == "tractorsupply":
            title = tsc_title
        elif title_match:
            title = clean_html_text(title_match.group(1))
        elif channel == "tractorsupply":
            title = self.tractor_supply_product_title(url, product_id)
        else:
            title = product_id
        evidence = {
            "schema_version": "voxquarry.official-review-source/v1",
            "provider": "bazaarvoice",
            "access_policy": "public_official_loader_api_no_session",
            "completeness_policy": "stable_total_exact_unique_ids",
            "method": "official public Bazaarvoice loader",
            "deployment_discovery": deployment_discovery,
            "product_identity_evidence": product_identity_evidence,
            "deployment_url": deployment_url,
            "loader_url": loader_url,
            "content_locales": config["content_locales"],
            "content_locale_scope": (
                "all_public_locales"
                if all_content_locales
                else "deployment_content_locales"
            ),
            "display_code": config["display_code"],
        }
        if ego_catalog_evidence:
            evidence["catalog_identity"] = ego_catalog_evidence
        if channel == "tractorsupply":
            evidence.update(
                {
                    "deployment_runtime": deployment["runtime"],
                    "api_config_verified": bool(deployment["api_config_url"]),
                    "item_number": self.tractor_supply_item_number(url),
                    "category_evidence": category_evidence,
                    "model_evidence": model_evidence,
                    "pdp_access_class": (
                        getattr(tsc_pdp_error, "failure_class", "")
                        if tsc_pdp_error
                        else ""
                    ),
                }
            )
        if source:
            try:
                self.metadata_client.remember(channel, url, source)
            except SourceProductMetadataError as exc:
                # A review marker can coexist with absent specifications, but
                # must not hide an observed wrong product or conflicting facts.
                if exc.code != "source_metadata_missing":
                    raise
        return (
            BvTemplate(
                "classic",
                template_url,
                {"referer": url},
                all_content_locales=all_content_locales,
            ),
            title,
            product_id,
            evidence,
        )

    @staticmethod
    def tractor_supply_product_title(url, product_id):
        parts = [
            unquote(part)
            for part in urlsplit(str(url or "")).path.split("/")
            if part
        ]
        slug = ""
        if "product" in parts:
            position = parts.index("product")
            if position + 1 < len(parts):
                slug = parts[position + 1]
        slug = re.sub(r"-\d+$", "", slug)
        return (
            clean_html_text(slug.replace("-", " "))
            or f"Tractor Supply product {product_id}"
        )

    @staticmethod
    def lowes_api_url(
        product_id,
        offset,
        sort_by="newestFirst",
        max_results=LOWES_PAGE_SIZE,
    ):
        if sort_by not in {"newestFirst", "oldestFirst"}:
            raise ValueError(f"unsupported Lowe's review sort: {sort_by}")
        query = urlencode({
            "sortBy": sort_by,
            "offset": int(offset),
            "maxResults": int(max_results),
        })
        return f"https://www.lowes.com/rnr/r/get-by-product/{product_id}?{query}"

    @staticmethod
    def lowes_product_title(url, product_id):
        parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
        try:
            position = parts.index(product_id)
        except ValueError:
            position = len(parts)
        slug = parts[position - 1] if position > 0 else product_id
        if slug.lower() in {"product", "pd"}:
            return f"Lowe's product {product_id}"
        return clean_html_text(slug.replace("-", " ")) or f"Lowe's product {product_id}"

    @staticmethod
    def validate_lowes_api_payload(payload, product_id, requested_offset):
        if not isinstance(payload, dict):
            raise LowesAccessError("Lowe's reviews API returned a non-object payload")
        if payload.get("hasErrors"):
            raise LowesAccessError("Lowe's reviews API reported an error")
        results = payload.get("results")
        total = payload.get("totalResults")
        if not isinstance(results, list) or not isinstance(total, int):
            raise LowesAccessError("Lowe's reviews API payload is missing results or totalResults")
        response_offset = int(payload.get("offset") or 0)
        if response_offset != int(requested_offset):
            raise LowesAccessError(
                f"Lowe's reviews API returned offset {response_offset}, expected {requested_offset}"
            )
        requested_product_id = str(product_id)
        if int(requested_offset) == 0 and re.fullmatch(r"\d{7}", requested_product_id):
            statistics = payload.get("reviewStatistics")
            response_product_id = (
                str(statistics.get("omniItemId") or "")
                if isinstance(statistics, dict)
                else ""
            )
            if not response_product_id:
                raise LowesProductIdentityError(
                    "Lowe's reviews API did not expose official identity "
                    "evidence for the seven-digit PDP"
                )
            if response_product_id != requested_product_id:
                raise LowesProductIdentityError(
                    "Lowe's reviews API returned a different product identity"
                )
        # Lowe's deliberately merges reviews across variants. The endpoint ID
        # remains the requested PDP, while individual records may expose the
        # canonical source SKU in productId. Preserve that source field during
        # normalization instead of rejecting a valid syndicated record.
        return payload

    def lowes_direct_api_page(
        self,
        product_id,
        offset,
        product_url,
        sort_by="newestFirst",
        max_results=LOWES_PAGE_SIZE,
    ):
        response = self.lowes_session.get(
            self.lowes_api_url(product_id, offset, sort_by, max_results),
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": product_url,
            },
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise LowesAccessError(f"Lowe's direct reviews API returned HTTP {response.status_code}")
        response.raise_for_status()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LowesAccessError("Lowe's direct reviews API did not return JSON") from exc
        return self.validate_lowes_api_payload(payload, product_id, offset)

    def firecrawl_lowes_page(
        self,
        product_id,
        offset,
        sort_by="newestFirst",
        max_results=LOWES_PAGE_SIZE,
    ):
        api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        if not api_key:
            raise LowesAccessError("FIRECRAWL_API_KEY is not configured")
        response = self.lowes_session.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "url": self.lowes_api_url(
                    product_id,
                    offset,
                    sort_by,
                    max_results,
                ),
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "proxy": "enhanced",
                "maxAge": int(os.getenv("FIRECRAWL_LOWES_CACHE_AGE_MS", "300000")),
                "timeout": 120000,
            },
            timeout=150,
        )
        if response.status_code in (401, 402, 403):
            raise LowesAccessError(
                f"Firecrawl credential, billing, or access check returned HTTP {response.status_code}"
            )
        response.raise_for_status()
        envelope = response.json()
        if not envelope.get("success"):
            raise LowesAccessError(
                "Firecrawl did not complete the Lowe's public reviews API request"
            )
        raw_html = (envelope.get("data") or {}).get("rawHtml") or ""
        if "access denied" in raw_html.lower():
            raise LowesAccessError("Firecrawl received Lowe's Access Denied document")
        try:
            payload = json.loads(raw_html)
        except json.JSONDecodeError as exc:
            raise LowesAccessError("Firecrawl did not return Lowe's reviews API JSON") from exc
        return self.validate_lowes_api_payload(payload, product_id, offset)

    def firecrawl_remaining_credits(self):
        api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            response = self.lowes_session.get(
                "https://api.firecrawl.dev/v2/team/credit-usage",
                headers={"authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            response.raise_for_status()
            return int((response.json().get("data") or {}).get("remainingCredits") or 0)
        except (requests.RequestException, ValueError, TypeError):
            # Credit telemetry must not make an otherwise bounded task fail.
            # The per-job ceiling and configured reserve remain enforced when
            # the provider's usage endpoint is temporarily unavailable.
            return None

    async def lowes_api_page(
        self,
        product_id,
        offset,
        product_url,
        sort_by="newestFirst",
        max_results=LOWES_PAGE_SIZE,
        preferred_method=None,
        job_id=None,
    ):
        allow_firecrawl = self.research_firecrawl_allowed(job_id)
        direct_error = None
        if not allow_firecrawl or preferred_method != "firecrawl_enhanced_proxy":
            try:
                payload = await asyncio.to_thread(
                    self.lowes_direct_api_page,
                    product_id,
                    offset,
                    product_url,
                    sort_by,
                    max_results,
                )
                return payload, "direct"
            except LowesProductIdentityError:
                raise
            except Exception as exc:
                direct_error = str(exc)
        if not allow_firecrawl:
            raise LowesAccessError(
                f"Lowe's direct API unavailable; research policy forbids Firecrawl: {direct_error}"
            )
        last_error = None
        for attempt in range(1, 3):
            try:
                payload = await asyncio.to_thread(
                    self.firecrawl_lowes_page,
                    product_id,
                    offset,
                    sort_by,
                    max_results,
                )
                return payload, "firecrawl_enhanced_proxy"
            except LowesProductIdentityError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(2)
        details = f"direct={direct_error}; firecrawl={last_error}"
        raise LowesAccessError(f"Lowe's reviews API unavailable: {details}")

    def research_firecrawl_allowed(self, job_id):
        if not job_id or not self.store:
            return True
        policy = self.store.research_attempt_policy(job_id)
        return policy is None or bool(policy["firecrawl_allowed"])

    def lowes_checkpoint_path(self, job_id):
        if not job_id or not self.store:
            return None
        return self.store.data_dir / "runs" / job_id / "lowes" / "lowes-api-pages.jsonl"

    def load_lowes_checkpoint(self, job_id, product_id):
        path = self.lowes_checkpoint_path(job_id)
        pages = {}
        if not path or not path.is_file():
            return pages
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(item.get("product_id")) != str(product_id):
                continue
            sort_by = item.get("sort_by") or "newestFirst"
            offset = item.get("offset")
            payload = item.get("payload")
            if (
                sort_by in {"newestFirst", "oldestFirst"}
                and isinstance(offset, int)
                and isinstance(payload, dict)
            ):
                pages[(sort_by, offset)] = item
        return pages

    def append_lowes_checkpoint(
        self,
        job_id,
        product_id,
        sort_by,
        offset,
        payload,
        access_method,
    ):
        path = self.lowes_checkpoint_path(job_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "captured_at": now(),
            "product_id": str(product_id),
            "sort_by": sort_by,
            "offset": int(offset),
            "access_method": access_method,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def lowes_bv_template(self):
        """Discover Lowe's public Bazaarvoice API configuration at runtime."""
        deployment = self.session.get(LOWES_BV_DEPLOYMENT_URL, timeout=30)
        deployment.raise_for_status()
        legacy = re.search(r'legacyScoutUrl:"([^"]+)"', deployment.text)
        if not legacy:
            raise LowesAccessError(
                "Lowe's public Bazaarvoice deployment did not expose its loader"
            )
        loader_url = legacy.group(1)
        loader = self.session.get(loader_url, timeout=30)
        loader.raise_for_status()
        config = parse_public_bv_loader(loader.text)
        pairs = [
            ("apiversion", "5.5"),
            ("passkey", config["passkey"]),
            ("Filter", "ProductId:placeholder"),
            ("Filter", f"ContentLocale:{','.join(config['content_locales'])}"),
        ]
        if config["display_code"]:
            pairs.append(("displaycode", config["display_code"]))
        template_url = (
            "https://api.bazaarvoice.com/data/reviews.json?"
            + urlencode(pairs, safe=":,")
        )
        evidence = {
            "deployment_url": LOWES_BV_DEPLOYMENT_URL,
            "loader_url": loader_url,
            "display_code": config["display_code"],
            "content_locales": config["content_locales"],
        }
        return BvTemplate("classic", template_url, {}), evidence

    def lowes_bv_checkpoint_path(self, job_id):
        if not job_id or not self.store:
            return None
        return (
            self.store.data_dir
            / "runs"
            / job_id
            / "lowes"
            / "lowes-bazaarvoice-pages.jsonl"
        )

    def load_lowes_bv_checkpoint(self, job_id, product_id, source_total):
        path = self.lowes_bv_checkpoint_path(job_id)
        pages = {}
        if not path or not path.is_file():
            return pages
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                str(item.get("product_id")) != str(product_id)
                or int(item.get("source_total") or -1) != int(source_total)
            ):
                continue
            offset = item.get("offset")
            payload = item.get("payload")
            if isinstance(offset, int) and isinstance(payload, dict):
                pages[offset] = item
        return pages

    def append_lowes_bv_checkpoint(
        self,
        job_id,
        product_id,
        source_total,
        offset,
        payload,
    ):
        path = self.lowes_bv_checkpoint_path(job_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "captured_at": now(),
            "product_id": str(product_id),
            "source_total": int(source_total),
            "offset": int(offset),
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def harborfreight_page(self, sku, offset):
        response = self.session.post(
            HARBOR_FREIGHT_GRAPHQL_URL,
            json={
                "operationName": "ProductReviews",
                "variables": {
                    "sku": str(sku),
                    "q": None,
                    "filters": {
                        "offset": int(offset),
                        "limit": HARBOR_FREIGHT_PAGE_SIZE,
                    },
                },
                "query": HARBOR_FREIGHT_REVIEWS_QUERY,
            },
            timeout=45,
        )
        if response.status_code in {403, 429}:
            raise RetryableChannelError(
                "Harbor Freight public review access is rate-limited or "
                "requires an interactive challenge; no bypass attempted",
                retry_after=int(
                    os.getenv("HARBOR_FREIGHT_RETRY_AFTER_SECONDS", "1800")
                ),
            )
        if response.status_code in {500, 502, 503, 504}:
            raise RetryableChannelError(
                "Harbor Freight public review endpoint is temporarily unavailable",
                retry_after=int(
                    os.getenv("HARBOR_FREIGHT_RETRY_AFTER_SECONDS", "1800")
                ),
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(
                "Harbor Freight review API returned GraphQL errors"
            )
        page = (payload.get("data") or {}).get("productReviews")
        if not isinstance(page, dict) or not isinstance(
            page.get("reviews"), list
        ):
            raise RuntimeError(
                "Harbor Freight review API returned no product review page"
            )
        if int(page.get("offset") or 0) != int(offset):
            raise RuntimeError(
                "Harbor Freight review API returned an unexpected offset"
            )
        return page

    def harborfreight_checkpoint_path(self, job_id):
        if not job_id or not self.store:
            return None
        return (
            self.store.data_dir
            / "runs"
            / job_id
            / "harborfreight"
            / "graphql-pages.jsonl"
        )

    def load_harborfreight_checkpoint(self, job_id, sku):
        path = self.harborfreight_checkpoint_path(job_id)
        pages = {}
        if not path or not path.is_file():
            return pages
        for line in path.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(item.get("sku")) != str(sku):
                continue
            offset = item.get("offset")
            page = item.get("page")
            if isinstance(offset, int) and isinstance(page, dict):
                pages[offset] = page
        return pages

    def append_harborfreight_checkpoint(self, job_id, sku, offset, page):
        path = self.harborfreight_checkpoint_path(job_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "captured_at": now(),
            "sku": str(sku),
            "offset": int(offset),
            "page": page,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def amazon_reviews_2023_cache_paths(self, parent_asin):
        cache = (
            self.store.data_dir
            / "archive_cache"
            / "amazon_reviews_2023"
        )
        return (
            cache / "products" / f"{parent_asin}.json",
            cache / "reviews" / f"{parent_asin}.jsonl",
        )

    def exmark_reviews_page(self, page_id, offset, config):
        url = (
            f"https://display.powerreviews.com/m/{config['merchant_id']}/"
            f"l/en_US/product/{page_id}/reviews"
        )
        response = self.official_public_get(
            url,
            params={
                "apikey": config["api_key"],
                "_noconfig": "true",
                "paging.from": int(offset),
                "paging.size": EXMARK_PAGE_SIZE,
                "sort": "Newest",
            },
            headers={"referer": "https://www.exmark.com/"},
            timeout=30,
        )
        guard_public_response("exmark", response, "public PowerReviews API")
        try:
            payload = response.json()
            results = payload["results"]
            result = results[0]
            returned_page_id = str(result["page_id"])
            total = int(
                (result.get("rollup") or {}).get("review_count")
                or (payload.get("paging") or {}).get("total_results")
            )
            rows = result["reviews"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OfficialSourceError(
                "exmark: public PowerReviews response was incomplete",
                "source_contract_violation",
            ) from exc
        if len(results) != 1 or returned_page_id != str(page_id):
            raise OfficialSourceError(
                "exmark: PowerReviews returned a different page_id",
                "product_identity_mismatch",
            )
        if not isinstance(rows, list):
            raise OfficialSourceError(
                "exmark: PowerReviews review rows were missing",
                "source_contract_violation",
            )
        return rows, total

    def walmart_reviews_page(self, item_id, page_number):
        url = f"https://www.walmart.com/reviews/product/{item_id}"
        response = self.official_public_get(
            url,
            params={"sort": "relevancy", "page": int(page_number)},
            timeout=30,
        )
        guard_public_response("walmart", response, "official review page")
        return parse_walmart_next_data(response.text, item_id, page_number)

    async def product_summary(self, channel, url, **kwargs):
        product = await self._review_product_summary(channel, url, **kwargs)
        if not product:
            return product
        source = await asyncio.to_thread(self.metadata_client.get, channel, url)
        error = source.get('source_metadata_error') or {}
        if error.get('code') in {'source_identity_mismatch', 'source_metadata_conflict'}:
            raise ValueError(error['message'])
        merged = merge_source_metadata(product, source)
        if source.get('product_title'):
            merged['title'] = source['product_title']
        return merged

    async def _review_product_summary(
        self,
        channel,
        url,
        job_id=None,
        expected_product_id=None,
        expected_category=None,
        expected_model=None,
    ):
        if channel == "exmark":
            model = [
                part for part in urlsplit(url).path.split("/") if part
            ][-1].upper()
            page_response = self.official_public_get(url, timeout=30)
            guard_public_response("exmark", page_response, "official PDP")
            public_config = exmark_public_config(page_response.text)
            model_response = self.official_public_get(
                "https://www.exmark.com/api/mc-proxy/models/get-mower-details",
                params={"modelNumber": model},
                timeout=30,
            )
            guard_public_response("exmark", model_response, "official model API")
            try:
                model_payload = model_response.json()
            except ValueError as exc:
                raise OfficialSourceError(
                    "exmark: official model API returned invalid JSON",
                    "source_contract_violation",
                ) from exc
            identity = exmark_product_identity(url, model_payload)
            verified_model = str(model_payload["model"]["ModelNumber"]).upper()
            rows, total = self.exmark_reviews_page(
                identity.product_id, 0, public_config
            )
            title_match = re.search(r"<title[^>]*>(.*?)</title>", page_response.text, re.I | re.S)
            return {
                "id": identity.product_id,
                "source_product_id": identity.product_id,
                "model": verified_model,
                "source_typed_fields": ["model"],
                "source_typed_field_evidence": {
                    "model": {
                        "value": verified_model,
                        "method": "official_model_api_model_number",
                        "source_url": "https://www.exmark.com/api/mc-proxy/models/get-mower-details",
                        "field": "model.ModelNumber",
                        "source_product_id": identity.product_id,
                    },
                },
                "url": clean_url(url),
                "title": clean_html_text(title_match.group(1)) if title_match else model,
                "total": total,
                "collection_method": "official_public_powerreviews_api",
                "source_site": "exmark.com",
                "market_region": "North America",
                "review_locale": "en_US",
                "product_identity_evidence": identity.evidence,
                "powerreviews_merchant_id": public_config["merchant_id"],
                "powerreviews_merchant_group_id": public_config["merchant_group_id"],
                "official_source_evidence": {
                    "schema_version": "voxquarry.official-review-source/v1",
                    "provider": "powerreviews",
                    "access_policy": "public_official_loader_api_no_session",
                    "completeness_policy": "stable_total_exact_unique_ids",
                    "product_identity_evidence": identity.evidence,
                },
                "_powerreviews_config": public_config,
            }
        if channel == "walmart":
            item_id = walmart_item_id_from_url(url)
            if not item_id:
                raise ValueError(
                    "walmart: exact /ip/.../<item_id> product URL is required"
                )
            first = self.walmart_reviews_page(item_id, 1)
            return {
                "id": item_id,
                "source_product_id": first["lookup_id"],
                "url": clean_url(url),
                "title": first["title"] or item_id,
                "total": first["source_total"],
                "rating_total": first["rating_total"],
                "collection_method": "walmart_official_review_pages_public_ssr",
                "source_site": "walmart.com",
                "market_region": "North America",
                "review_locale": "en_US",
                "product_identity_evidence": "official_review_page_lookup_id",
                "official_source_evidence": {
                    "schema_version": "voxquarry.official-review-source/v1",
                    "provider": "walmart_public_ssr",
                    "access_policy": "public_official_page_no_session",
                    "completeness_policy": "stable_total_exact_unique_ids",
                    "product_identity_evidence": "official_review_page_lookup_id",
                },
                "_walmart_first_page": first,
            }
        if channel == "ryobi" and urlsplit(url).netloc.lower().removeprefix("www.").endswith("ryobitools.eu"):
            return await self.ryobi_eu_summary(url)
        if channel in PUBLIC_BV_CHANNELS:
            template, title, product_id, evidence = self.discover_public_bv(
                channel,
                url,
                expected_product_id or "",
                expected_category=expected_category,
                expected_model=expected_model,
            )
            response = (
                self.tractor_supply_public_get(
                    template.page_url(product_id, 0),
                    "public Bazaarvoice reviews API",
                    headers=template.headers,
                    timeout=30,
                )
                if channel == "tractorsupply"
                else
                self.official_public_get
                if channel in OFFICIAL_STRICT_BV_CHANNELS
                else self.session.get
            )
            if channel != "tractorsupply":
                response = response(
                    template.page_url(product_id, 0),
                    headers=template.headers,
                    timeout=30,
                )
            if channel in OFFICIAL_STRICT_BV_CHANNELS:
                guard_public_response(channel, response, "public Bazaarvoice API")
            else:
                response.raise_for_status()
            payload = response.json()
            if channel == "ego":
                from .ego_official import validate_review_payload
                validate_review_payload(payload)
            rows, total = template.parse(payload)
            if payload.get("Errors"):
                raise RuntimeError(
                    f"{channel}: Bazaarvoice returned source errors"
                )
            returned_ids = {
                str(item.get("ProductId") or item.get("productId") or "")
                for item in rows
            }
            if channel == "skil":
                in_scope = {
                    value for value in returned_ids
                    if skil_product_in_bare_tool_scope(product_id, value)
                }
                if total and not in_scope:
                    raise RuntimeError(
                        f"{channel}: Bazaarvoice returned no review for the "
                        "requested bare-tool family"
                    )
            elif channel in {
                "milwaukee", "toro", "stihl", "husqvarna", "greenworks", "ego", "ryobi",
            }:
                if any(
                    not milwaukee_product_in_official_family_scope(
                        product_id,
                        value,
                        payload,
                    )
                    for value in returned_ids
                ):
                    raise RuntimeError(
                        f"{channel}: Bazaarvoice returned reviews for a "
                        "different product id"
                    )
            elif any(str(value) != str(product_id) for value in returned_ids):
                if channel == "tractorsupply":
                    raise TractorSupplyAccessError(
                        "tractorsupply: Bazaarvoice returned reviews for a "
                        "different product id",
                        failure_class="product_identity_mismatch",
                        cooldown_seconds=0,
                    )
                raise RuntimeError(
                    f"{channel}: Bazaarvoice returned reviews for a "
                    "different product id"
                )
            product = {
                "id": product_id,
                "source_product_id": product_id,
                "url": url,
                "title": title,
                "total": total,
                "collection_method": "official_public_bazaarvoice_loader",
                "source_site": urlsplit(url).netloc.lower().removeprefix("www."),
                "market_region": "North America" if channel == "skil" else infer_market_region(url),
                "bv_evidence": evidence,
            }
            if channel == "tractorsupply":
                product["_expected_category"] = expected_category
                product["_expected_model"] = expected_model
            return product
        if channel == "lowes":
            pid = lowes_product_id_from_url(url)
            if not pid:
                return None
            checkpoint = self.load_lowes_checkpoint(job_id, pid)
            cached = checkpoint.get(("newestFirst", 0))
            try:
                if cached:
                    first_page = self.validate_lowes_api_payload(
                        cached["payload"],
                        pid,
                        0,
                    )
                    access_method = cached.get("access_method") or "firecrawl_enhanced_proxy"
                else:
                    first_page, access_method = await self.lowes_api_page(pid, 0, url, job_id=job_id)
            except LowesProductIdentityError:
                raise
            except Exception as exc:
                raise RetryableChannelError(
                    f"Lowe's source access is temporarily unavailable; task kept queued: {exc}",
                    retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                ) from exc
            total = int(first_page["totalResults"])
            if total > LOWES_BV_OVERFLOW_THRESHOLD:
                try:
                    template, evidence = await asyncio.to_thread(
                        self.lowes_bv_template
                    )
                    bv_response = await asyncio.to_thread(
                        self.session.get,
                        template.page_url(pid, 0),
                        headers=template.headers,
                        timeout=30,
                    )
                    bv_response.raise_for_status()
                    _, bv_total = template.parse(bv_response.json())
                    if int(bv_total) < total:
                        raise LowesAccessError(
                            "Lowe's public Bazaarvoice total is lower than its RNR total"
                        )
                except Exception as exc:
                    raise RetryableChannelError(
                        "Lowe's large product needs its public Bazaarvoice overflow "
                        f"source, which is temporarily unavailable: {exc}",
                        retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                    ) from exc
                return {
                    "id": pid,
                    "url": url,
                    "title": self.lowes_product_title(url, pid),
                    "total": int(bv_total),
                    "rnr_total_at_discovery": total,
                    "source_site": "lowes.com",
                    "collection_method": "official_lowes_bazaarvoice_overflow",
                    "access_method": "direct_public_bazaarvoice",
                    "page_size": PAGE_SIZE,
                    "estimated_firecrawl_credits": 0,
                    "bv_evidence": evidence,
                    "_lowes_bv_template": template,
                    "_lowes_bv_first_page": bv_response.json(),
                }
            page_count = max(1, (total + LOWES_PAGE_SIZE - 1) // LOWES_PAGE_SIZE)
            estimated_credits = page_count * 5 if access_method == "firecrawl_enhanced_proxy" and self.research_firecrawl_allowed(job_id) else 0
            max_credits = int(os.getenv("FIRECRAWL_LOWES_MAX_CREDITS_PER_JOB", "2000"))
            if estimated_credits > max_credits:
                raise RetryableChannelError(
                    f"Lowe's task needs about {estimated_credits} Firecrawl credits, "
                    f"above the configured per-job limit {max_credits}; task kept queued",
                    retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "21600")),
                )
            if access_method == "firecrawl_enhanced_proxy" and self.research_firecrawl_allowed(job_id):
                remaining = await asyncio.to_thread(self.firecrawl_remaining_credits)
                reserve = int(os.getenv("FIRECRAWL_LOWES_CREDIT_RESERVE", "500"))
                if remaining is not None and remaining < estimated_credits + reserve:
                    raise RetryableChannelError(
                        f"Lowe's task needs about {estimated_credits} Firecrawl credits; "
                        f"{remaining} remain and {reserve} are reserved; task kept queued",
                        retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "21600")),
                    )
            return {
                "id": pid,
                "url": url,
                "title": self.lowes_product_title(url, pid),
                "total": total,
                "source_site": "lowes.com",
                "collection_method": "official_lowes_rnr_api",
                "access_method": access_method,
                "page_size": LOWES_PAGE_SIZE,
                "estimated_firecrawl_credits": estimated_credits,
                "_lowes_first_page": first_page,
            }
        if channel == "homedepot":
            m = re.search(r"/(\d{8,})(?:[?#]|$)", url)
            if not m: return None
            item = m.group(1)
            parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
            try:
                position = parts.index(item)
            except ValueError:
                position = len(parts)
            slug = parts[position - 1] if position > 0 else item
            title = clean_html_text(slug.replace("-", " ")) or f"Home Depot product {item}"
            # Do not spend a disposable browser session just to probe the
            # total. Collection immediately opens the reviewed PDP and has a
            # verified enhanced-browser fallback when Akamai rejects the
            # server's fresh Chrome context.
            return {
                "id": item,
                "url": url,
                "title": title,
                "total": None,
                "source_site": "homedepot.com",
                "collection_method": "homedepot_official_review_pages",
            }
        if channel == "harborfreight":
            match = re.search(r"-(\d{4,})\.html(?:[?#]|$)", url, re.I)
            if not match:
                return None
            sku = match.group(1)
            checkpoint = self.load_harborfreight_checkpoint(job_id, sku)
            page = checkpoint.get(0)
            if page is None:
                page = await asyncio.to_thread(
                    self.harborfreight_page, sku, 0
                )
                self.append_harborfreight_checkpoint(job_id, sku, 0, page)
            slug = urlsplit(url).path.rsplit("/", 1)[-1]
            slug = re.sub(
                rf"-{re.escape(sku)}\.html$", "", slug, flags=re.I
            )
            return {
                "id": sku,
                "url": url,
                "title": clean_html_text(slug.replace("-", " "))
                or f"Harbor Freight product {sku}",
                "total": int(page.get("total") or 0),
                "source_site": "harborfreight.com",
                "review_locale": "en_US",
                "collection_method": "harborfreight_public_graphql",
                "page_size": HARBOR_FREIGHT_PAGE_SIZE,
                "_harborfreight_first_page": page,
            }
        if channel == "amazon_reviews_2023":
            match = re.search(
                r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)", url, re.I
            )
            if not match:
                return None
            parent_asin = match.group(1).upper()
            metadata_path, reviews_path = (
                self.amazon_reviews_2023_cache_paths(parent_asin)
            )
            if not metadata_path.is_file() or not reviews_path.is_file():
                raise RetryableChannelError(
                    "Amazon Reviews 2023 cache is not prepared for "
                    f"{parent_asin}; run the reviewed archive cache builder",
                    retry_after=int(
                        os.getenv(
                            "AMAZON_ARCHIVE_RETRY_AFTER_SECONDS", "21600"
                        )
                    ),
                )
            try:
                metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Amazon archive metadata is invalid for {parent_asin}"
                ) from exc
            source_product_id = str(
                metadata.get("source_product_id")
                or metadata.get("parent_asin")
                or ""
            ).upper()
            if source_product_id != parent_asin:
                raise RuntimeError(
                    "Amazon archive cache product identity does not match URL"
                )
            archive_match_field = str(
                metadata.get("archive_match_field") or "parent_asin"
            )
            if archive_match_field not in {"parent_asin", "asin"}:
                raise RuntimeError(
                    "Amazon archive cache match field is invalid"
                )
            if metadata.get("market_region") != "North America":
                raise RuntimeError(
                    "Amazon archive cache market_region must be "
                    "North America"
                )
            if metadata.get("archive_through") != AMAZON_ARCHIVE_THROUGH:
                raise RuntimeError(
                    "Amazon archive cache boundary must be "
                    f"{AMAZON_ARCHIVE_THROUGH}"
                )
            title = str(metadata.get("title") or "").strip()
            if re.search(r"\bEGO(?:\s+POWER\s*\+?)?\b", title, re.I):
                raise RuntimeError(
                    "EGO products are excluded from the Amazon archive "
                    "because review-level syndication origin is unavailable"
                )
            return {
                "id": parent_asin,
                "url": f"https://www.amazon.com/dp/{parent_asin}",
                "title": title or f"Amazon product {parent_asin}",
                "total": int(metadata.get("source_total") or 0),
                "source_site": "amazon.com",
                "review_locale": "en_US",
                "market_region": "North America",
                "source_dataset": metadata.get("source_dataset"),
                "source_subset": metadata.get("source_subset"),
                "source_revision": metadata.get("source_revision"),
                "archive_through": metadata.get("archive_through"),
                "archive_matched_records": metadata.get(
                    "matched_archive_records",
                    metadata.get("source_total"),
                ),
                "archive_invalid_records_excluded": metadata.get(
                    "invalid_records_excluded",
                    0,
                ),
                "archive_invalid_reason_histogram": metadata.get(
                    "invalid_reason_histogram",
                    {},
                ),
                "archive_match_field": archive_match_field,
                "archive_parent_asins": metadata.get(
                    "archive_parent_asins",
                    {},
                ),
                "collection_method": "amazon_reviews_2023_cache",
                "_amazon_reviews_path": str(reviews_path),
            }
        # Bazaarvoice hosts: template discovery provides both total and canonical product id.
        template, title = await self.discover_bv_retry(url)
        pid = template.product_id() or self.url_id(url)
        if not pid: return None
        resp = self.session.get(template.page_url(pid, 0), headers=template.headers, timeout=30); resp.raise_for_status()
        _, total = template.parse(resp.json())
        return {"id": pid, "url": url, "title": title, "total": total, "bv_kind": template.kind}

    def ryobi_eu_context(self, url):
        """Read the public product component identifiers for a EU RYOBI URL."""
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        source = response.text
        agility = re.search(r'"agilityId":(\d+)', source)
        review_ids = re.search(r'"reviewAgilityIds":(\[[^]]+\])', source)
        if not agility or not review_ids:
            raise RuntimeError("RYOBI Europe review component metadata was not present")
        try:
            ids = json.loads(review_ids.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError("RYOBI Europe review identifiers could not be parsed") from exc
        path = urlsplit(url).path
        if not path.endswith("/"):
            path += "/"
        culture = re.search(r'"documentCulture":"([^"]+)"', source)
        return {
            "agility_id": agility.group(1), "review_agility_ids": [str(value) for value in ids],
            "path": path, "culture": culture.group(1) if culture else "en-GB",
            "api_base": urlunsplit((urlsplit(url).scheme, urlsplit(url).netloc, "", "", "")).rstrip("/"),
        }

    async def ryobi_eu_page(self, context, page):
        params = {
            "cultureCode": context["culture"], "showAllCulturesReviews": "true", "page": str(page),
            "agilityId": context["agility_id"], "reviewAgilityIds": ",".join(context["review_agility_ids"]),
            "url": context["path"], "orderBy": "0", "ratingFilter": "3", "automaticallyTranslate": "true",
        }
        endpoint = f"{context['api_base']}/api/productreviews/get"
        for attempt in range(1, 7):
            response = self.session.get(endpoint, params=params, timeout=30)
            if response.status_code == 429:
                # The public endpoint has a rolling request limit.  Respect a
                # server-provided retry interval when present, otherwise back
                # off conservatively without blocking other channel workers.
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 0
                delay = max(delay, min(90, 10 * attempt))
                await asyncio.sleep(delay + random.uniform(0.5, 2.0))
                continue
            if response.status_code in (500, 502, 503, 504) and attempt < 6:
                await asyncio.sleep(min(45, 3 * attempt) + random.uniform(0.2, 1.0))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get("data"), list):
                raise RuntimeError("RYOBI Europe review API returned no review list")
            return payload
        raise RuntimeError(f"RYOBI Europe review API remained rate limited on page {page} after retries")

    async def ryobi_eu_summary(self, url):
        context = self.ryobi_eu_context(url)
        payload = await self.ryobi_eu_page(context, 1)
        written_total = int(payload.get("filteredCount") or 0)
        rating_total = int(payload.get("totalCount") or 0)
        if written_total <= 0:
            raise RuntimeError("RYOBI Europe review API reported no written reviews")
        return {
            "id": context["agility_id"], "url": url, "title": payload.get("productTitle") or "RYOBI Europe product",
            "total": written_total, "rating_total": rating_total, "review_context": context,
            "collection_method": "ryobi_eu_public_review_api", "source_site": "uk.ryobitools.eu",
            "review_locale": context["culture"],
        }

    @staticmethod
    def url_id(url):
        m = re.search(r"/(\d{5,})(?:[?#]|$)", url)
        return m.group(1) if m else None

    async def discover_bv(self, url):
        captured = {}
        p, browser, context = await self.browser()
        try:
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            extra_headers = {}
            def extra_info(event):
                # Chromium surfaces protected headers, including Bv-Bfd-Token, in the
                # CDP ExtraInfo event instead of Playwright's request headers.
                for key, value in (event.get("headers") or {}).items():
                    if key.lower() == "bv-bfd-token":
                        extra_headers[key] = value
            cdp.on("Network.requestWillBeSentExtraInfo", extra_info)
            async def inspect_request(req):
                # Request.headers can omit browser-managed headers. Bazaarvoice BFD requires
                # Bv-Bfd-Token, so use all_headers() before persisting the reusable template.
                try:
                    headers = await req.all_headers()
                except Exception:
                    return
                u = req.url.lower()
                if "bfd" not in captured and "reviews.json" in u and "reviews_n_stats" in u:
                    captured["bfd"] = (req.url, headers)
                if "classic" not in captured and "bazaarvoice.com/data/" in u and "passkey=" in u:
                    captured["classic"] = (req.url, headers)
            page.on("request", lambda req: asyncio.create_task(inspect_request(req)))
            await page.goto(url, wait_until="domcontentloaded", timeout=90000); await page.wait_for_timeout(3500)
            for _ in range(10):
                await page.mouse.wheel(0, 2200); await page.wait_for_timeout(700)
                if captured: break
            title = await page.title()
            if "bfd" in captured:
                captured["bfd"] = (captured["bfd"][0], {**captured["bfd"][1], **extra_headers})
        finally:
            await context.close(); await browser.close(); await p.stop()
        if "bfd" in captured: return BvTemplate("bfd", *captured["bfd"]), title
        if "classic" in captured: return BvTemplate("classic", *captured["classic"]), title
        raise RuntimeError("Bazaarvoice request not observed")

    async def discover_bv_retry(self, url, attempts=3):
        last = None
        for attempt in range(1, attempts + 1):
            try:
                return await self.discover_bv(url)
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(2 * attempt)
        raise RuntimeError(f"Bazaarvoice request not observed after {attempts} sessions: {last}")

    async def collect(self, channel, product, job_id=None):
        if channel == "lowes": return await self.collect_lowes(product, job_id)
        if channel == "homedepot": return await self.collect_hd(product, job_id)
        if channel == "walmart": return await self.collect_walmart(product, job_id)
        if channel == "exmark": return await self.collect_exmark(product, job_id)
        if channel == "harborfreight":
            return await self.collect_harborfreight(product, job_id)
        if channel == "amazon_reviews_2023":
            return await self.collect_amazon_reviews_2023(product, job_id)
        if channel == "ryobi" and product.get("collection_method") == "ryobi_eu_public_review_api":
            return await self.collect_ryobi_eu(product, job_id)
        return await self.collect_bv(channel, product, job_id)

    async def collect_exmark(self, product, job_id=None):
        page_id = str(product["source_product_id"])
        public_config = product.pop("_powerreviews_config", None)
        if not isinstance(public_config, dict):
            page_response = self.official_public_get(product["url"], timeout=30)
            guard_public_response("exmark", page_response, "official PDP")
            public_config = exmark_public_config(page_response.text)
        records, page_lengths, totals = [], [], []
        seen = set()
        offset = 0
        while True:
            batch, total = self.exmark_reviews_page(
                page_id, offset, public_config
            )
            page_lengths.append(len(batch))
            totals.append(total)
            if not batch and offset < total:
                validate_complete_pages(page_lengths, totals)
            for raw in batch:
                rid = str(raw.get("review_id") or raw.get("ugc_id") or "")
                details = raw.get("details") or {}
                if (
                    not rid
                    or str(details.get("product_page_id") or page_id) != page_id
                ):
                    raise OfficialSourceError(
                        "exmark: PowerReviews row identity did not match page_id",
                        "product_identity_mismatch",
                    )
                if rid in seen:
                    raise OfficialSourceError(
                        "exmark: PowerReviews repeated a review id across pages",
                        "duplicate_review_identity",
                    )
                seen.add(rid)
                records.append(norm_exmark_record(product, raw))
            offset += len(batch)
            if job_id and self.store:
                self.store.update(job_id, f"exmark: {len(records)}/{total} records")
            if offset >= total:
                break
            if not batch:
                break
            await asyncio.sleep(0.35 + random.random() * 0.25)
        source_total = validate_complete_pages(page_lengths, totals)
        return records, source_total, {
            "method": "official public PowerReviews API",
            "source_site": "exmark.com",
            "source_product_id": page_id,
            "product_identity_evidence": product.get("product_identity_evidence"),
            "merchant_id": public_config["merchant_id"],
            "merchant_group_id": public_config["merchant_group_id"],
            "page_size": EXMARK_PAGE_SIZE,
            "source_reconciled_strict": True,
            "empty_source_confirmed": source_total == 0,
        }

    async def collect_walmart(self, product, job_id=None):
        from .bounded import walmart
        return await walmart(self, product, job_id)

    async def collect_amazon_reviews_2023(self, product, job_id=None):
        reviews_path = Path(
            product.pop("_amazon_reviews_path", "")
            or self.amazon_reviews_2023_cache_paths(product["id"])[1]
        )
        if not reviews_path.is_file():
            raise RetryableChannelError(
                f"Amazon Reviews 2023 cache disappeared for {product['id']}",
                retry_after=int(
                    os.getenv(
                        "AMAZON_ARCHIVE_RETRY_AFTER_SECONDS", "21600"
                    )
                ),
            )
        records = []
        with reviews_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "Amazon archive cache contains invalid JSON at "
                        f"line {line_number}"
                    ) from exc
                match_field = product.get(
                    "archive_match_field",
                    "parent_asin",
                )
                if str(raw.get(match_field) or "").upper() != product["id"]:
                    raise RuntimeError(
                        "Amazon archive review belongs to a different "
                        f"{match_field}"
                    )
                records.append(
                    norm_amazon_reviews_2023_record(product, raw)
                )
                if (
                    job_id
                    and self.store
                    and len(records) % 1000 == 0
                ):
                    self.store.update(
                        job_id,
                        "amazon_reviews_2023: "
                        f"{len(records)}/{product['total']} cached records",
                    )
        return records, int(product.get("total") or 0), {
            "method": "official McAuley-Lab Amazon Reviews 2023 archive cache",
            "source_site": "amazon.com",
            "source_dataset": product.get("source_dataset"),
            "source_subset": product.get("source_subset"),
            "source_revision": product.get("source_revision"),
            "archive_through": product.get("archive_through"),
            "archive_matched_records": product.get(
                "archive_matched_records",
                product.get("total"),
            ),
            "archive_invalid_records_excluded": product.get(
                "archive_invalid_records_excluded",
                0,
            ),
            "archive_invalid_reason_histogram": product.get(
                "archive_invalid_reason_histogram",
                {},
            ),
            "archive_match_field": product.get(
                "archive_match_field",
                "parent_asin",
            ),
            "archive_parent_asins": product.get(
                "archive_parent_asins",
                {},
            ),
            "static_archive": True,
            "syndication_origin_available": False,
            "ego_policy": (
                "all EGO-titled products excluded because the archive lacks "
                "review-level syndication origin"
            ),
        }

    async def collect_harborfreight(self, product, job_id=None):
        sku = product["id"]
        checkpoint = self.load_harborfreight_checkpoint(job_id, sku)
        first_page = product.pop("_harborfreight_first_page", None)
        if first_page is not None:
            checkpoint.setdefault(0, first_page)
        records = []
        seen = set()
        observed_totals = []
        offset = 0
        cached_pages = 0
        fetched_pages = 0
        while True:
            page = checkpoint.get(offset)
            if page is not None:
                cached_pages += 1
            else:
                page = await asyncio.to_thread(
                    self.harborfreight_page, sku, offset
                )
                self.append_harborfreight_checkpoint(
                    job_id, sku, offset, page
                )
                fetched_pages += 1
            total = int(page.get("total") or 0)
            observed_totals.append(total)
            batch = page.get("reviews") or []
            for raw in batch:
                review_id = str(raw.get("id") or "")
                if review_id and review_id not in seen:
                    seen.add(review_id)
                    records.append(
                        norm_harborfreight_record(product, raw)
                    )
            expected = max(observed_totals)
            if job_id and self.store:
                self.store.update(
                    job_id,
                    f"harborfreight: {len(records)}/{expected} records",
                )
            if len(records) >= expected:
                break
            if not batch:
                raise RuntimeError(
                    "Harbor Freight pagination stopped before source total"
                )
            offset += len(batch)
            delay = max(
                0.0,
                float(
                    os.getenv(
                        "HARBOR_FREIGHT_PAGE_DELAY_SECONDS", "1.25"
                    )
                ),
            )
            if delay:
                await asyncio.sleep(delay + random.uniform(0.0, 0.25))
        source_total = max(observed_totals) if observed_totals else 0
        return records, source_total, {
            "method": "Harbor Freight public first-party product review GraphQL",
            "source_site": "harborfreight.com",
            "page_size": HARBOR_FREIGHT_PAGE_SIZE,
            "cached_pages": cached_pages,
            "fetched_pages": fetched_pages,
            "pagination_exhausted": len(records) == source_total,
            "source_total_min": min(observed_totals)
            if observed_totals
            else 0,
            "source_total_max": source_total,
            "access_policy": (
                "bounded public requests; stop and retry on rate limit or "
                "interactive challenge"
            ),
        }

    async def collect_ryobi_eu(self, product, job_id=None):
        records, seen, page = [], set(), 1
        expected = 0
        while True:
            payload = await self.ryobi_eu_page(product["review_context"], page)
            expected = int(payload.get("filteredCount") or expected)
            batch = payload.get("data") or []
            for raw in batch:
                review_id = str(raw.get("guid") or raw.get("id") or "")
                if review_id and review_id not in seen:
                    seen.add(review_id)
                    records.append(norm_ryobi_eu_record(product, raw))
            if job_id and self.store:
                self.store.update(job_id, f"ryobi-eu: {len(records)}/{expected} written review records")
            if not payload.get("canLoadMore"):
                break
            if not batch:
                raise RuntimeError(f"RYOBI Europe review API stopped before its declared total on page {page}")
            page += 1
            # Keep below the source's rolling-rate threshold during long runs.
            await asyncio.sleep(0.48 + random.uniform(0.02, 0.12))
        return records, expected, {
            "method": "public first-party RYOBI Europe productreviews API",
            "source_site": "uk.ryobitools.eu",
            "rating_total": product.get("rating_total"),
            "written_review_total": expected,
            "ratings_without_written_review": max(0, int(product.get("rating_total") or 0) - expected),
            "pagination_exhausted_at_page": page,
        }

    async def collect_lowes(self, product, job_id=None):
        if product.get("collection_method") == "official_lowes_bazaarvoice_overflow":
            return await self.collect_lowes_bv(product, job_id)
        if product.get("collection_method") != "official_lowes_rnr_api":
            raise RuntimeError("unsupported Lowe's collection method")
        return await self.collect_lowes_api(product, job_id)

    async def collect_lowes_bv(self, product, job_id=None):
        """Collect a >20k Lowe's product from its public Bazaarvoice deployment."""
        product_id = product["id"]
        expected_total = int(product["total"])
        template = product.pop("_lowes_bv_template", None)
        first_page = product.pop("_lowes_bv_first_page", None)
        if template is None:
            template, evidence = await asyncio.to_thread(self.lowes_bv_template)
            product["bv_evidence"] = evidence
        checkpoint = self.load_lowes_bv_checkpoint(
            job_id,
            product_id,
            expected_total,
        )
        records, seen = [], set()
        offset = 0
        cached_pages = 0
        direct_pages = 0
        while offset < expected_total or offset == 0:
            cached = checkpoint.get(offset)
            if cached:
                payload = cached["payload"]
                cached_pages += 1
            elif offset == 0 and first_page:
                payload = first_page
                self.append_lowes_bv_checkpoint(
                    job_id,
                    product_id,
                    expected_total,
                    offset,
                    payload,
                )
                checkpoint[offset] = {"payload": payload}
            else:
                try:
                    response = await asyncio.to_thread(
                        self.session.get,
                        template.page_url(product_id, offset),
                        headers=template.headers,
                        timeout=30,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    raise RetryableChannelError(
                        f"Lowe's public Bazaarvoice offset {offset} is temporarily "
                        f"unavailable; checkpoint retained: {exc}",
                        retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                    ) from exc
                self.append_lowes_bv_checkpoint(
                    job_id,
                    product_id,
                    expected_total,
                    offset,
                    payload,
                )
                checkpoint[offset] = {"payload": payload}
            direct_pages += 1
            batch, page_total = template.parse(payload)
            if int(page_total) != expected_total:
                raise RetryableChannelError(
                    "Lowe's public Bazaarvoice total changed during collection "
                    f"from {expected_total} to {page_total}; a fresh snapshot is required",
                    retry_after=int(os.getenv("LOWES_INCOMPLETE_RETRY_SECONDS", "60")),
                )
            if not batch and offset < expected_total:
                raise RetryableChannelError(
                    f"Lowe's public Bazaarvoice returned an empty page at offset "
                    f"{offset}/{expected_total}; checkpoint retained",
                    retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                )
            for raw in batch:
                review_id = str(raw.get("Id") or "")
                if review_id and review_id not in seen:
                    seen.add(review_id)
                    records.append(norm_record("lowes", product, raw))
            if job_id and self.store:
                self.store.update(
                    job_id,
                    f"lowes: {len(records)}/{expected_total} records via public "
                    f"Bazaarvoice; offset {offset}",
                )
            offset += len(batch)
            if offset >= expected_total:
                break
            await asyncio.sleep(
                float(os.getenv("LOWES_PAGE_DELAY_SECONDS", "0.15"))
            )
        return records, expected_total, {
            "method": "Lowe's official public Bazaarvoice deployment",
            "source_site": "lowes.com",
            "access_method": "direct_public_bazaarvoice",
            "page_size": PAGE_SIZE,
            "pagination_exhausted_at_offset": offset,
            "checkpoint_pages_reused": cached_pages,
            "direct_pages": direct_pages,
            "rnr_total_at_discovery": product.get("rnr_total_at_discovery"),
            "bazaarvoice_total_at_discovery": expected_total,
            "source_total_note": (
                "The public Bazaarvoice deployment is the authoritative overflow "
                "source for products beyond the RNR API's two 10,000-row windows; "
                "its crawl-time total may be newer than the RNR summary."
            ),
            "bv_evidence": product.get("bv_evidence", {}),
        }

    async def collect_lowes_api(self, product, job_id=None):
        """Collect Lowe's RNR API through resumable newest/oldest windows."""
        product_id = product["id"]
        preferred_method = product.get("access_method")
        first_page = product.pop("_lowes_first_page", None)
        checkpoint = self.load_lowes_checkpoint(job_id, product_id)
        records, seen, source_product_ids = [], set(), set()
        reported_total = int(product.get("total") or 0)
        cached_pages = 0
        firecrawl_pages = 0
        direct_pages = 0
        window_stats = {}
        sort_modes = (
            ["newestFirst", "oldestFirst"]
            if reported_total > LOWES_API_WINDOW_SIZE
            else ["newestFirst"]
        )
        for sort_by in sort_modes:
            offset = 0
            window_pages = 0
            window_unique_start = len(records)
            while offset < min(reported_total, LOWES_API_WINDOW_SIZE) or offset == 0:
                remaining = max(1, reported_total - len(records))
                request_limit = min(
                    LOWES_PAGE_SIZE,
                    LOWES_API_WINDOW_SIZE - offset,
                    remaining,
                )
                checkpoint_key = (sort_by, offset)
                cached = checkpoint.get(checkpoint_key)
                if cached:
                    payload = self.validate_lowes_api_payload(
                        cached["payload"],
                        product_id,
                        offset,
                    )
                    access_method = cached.get("access_method") or preferred_method
                    cached_pages += 1
                elif sort_by == "newestFirst" and offset == 0 and first_page:
                    payload = self.validate_lowes_api_payload(
                        first_page,
                        product_id,
                        0,
                    )
                    access_method = preferred_method or "direct"
                    self.append_lowes_checkpoint(
                        job_id,
                        product_id,
                        sort_by,
                        offset,
                        payload,
                        access_method,
                    )
                    checkpoint[checkpoint_key] = {
                        "payload": payload,
                        "access_method": access_method,
                    }
                else:
                    try:
                        payload, access_method = await self.lowes_api_page(
                            product_id,
                            offset,
                            product["url"],
                            sort_by=sort_by,
                            max_results=request_limit,
                            preferred_method=preferred_method,
                            job_id=job_id,
                        )
                    except Exception as exc:
                        raise RetryableChannelError(
                            f"Lowe's API {sort_by} offset {offset} is temporarily "
                            f"unavailable; checkpoint retained and task kept queued: {exc}",
                            retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                        ) from exc
                    self.append_lowes_checkpoint(
                        job_id,
                        product_id,
                        sort_by,
                        offset,
                        payload,
                        access_method,
                    )
                    checkpoint[checkpoint_key] = {
                        "payload": payload,
                        "access_method": access_method,
                    }
                if access_method == "firecrawl_enhanced_proxy":
                    firecrawl_pages += 1
                else:
                    direct_pages += 1
                window_pages += 1
                batch = payload["results"]
                reported_total = max(reported_total, int(payload["totalResults"]))
                if not batch and len(records) < reported_total:
                    raise RetryableChannelError(
                        f"Lowe's API returned an empty {sort_by} page at offset "
                        f"{offset} before the declared total {reported_total}; "
                        "checkpoint retained",
                        retry_after=int(os.getenv("LOWES_RETRY_AFTER_SECONDS", "900")),
                    )
                for raw in batch:
                    review_id = str(raw.get("id") or raw.get("Id") or "")
                    source_product_id = str(
                        raw.get("productId") or raw.get("ProductId") or ""
                    )
                    if source_product_id:
                        source_product_ids.add(source_product_id)
                    if review_id and review_id not in seen:
                        seen.add(review_id)
                        records.append(norm_record("lowes", product, raw))
                if job_id and self.store:
                    self.store.update(
                        job_id,
                        f"lowes: {len(records)}/{reported_total} records via official "
                        f"API; {sort_by} offset {offset}",
                    )
                # Offset zero can return only 10 rows even when 100 were
                # requested. Advance by the actual batch size so no range is
                # skipped. A large product switches to oldestFirst after this
                # sort window reaches the API's 10,000-record boundary.
                offset += len(batch)
                if len(records) >= reported_total:
                    break
                if offset >= LOWES_API_WINDOW_SIZE:
                    break
                await asyncio.sleep(
                    float(os.getenv("LOWES_PAGE_DELAY_SECONDS", "0.15"))
                )
            window_stats[sort_by] = {
                "pages": window_pages,
                "exhausted_at_offset": offset,
                "unique_reviews_added": len(records) - window_unique_start,
            }
            if len(records) >= reported_total:
                break
        return records, reported_total, {
            "method": "Lowe's official first-party RNR reviews API",
            "source_site": "lowes.com",
            "access_method": preferred_method,
            "page_size": LOWES_PAGE_SIZE,
            "api_window_size": LOWES_API_WINDOW_SIZE,
            "sort_windows": list(window_stats),
            "pagination_windows": window_stats,
            "checkpoint_pages_reused": cached_pages,
            "firecrawl_enhanced_proxy_pages": firecrawl_pages,
            "direct_pages": direct_pages,
            "source_product_ids": sorted(source_product_ids),
            "estimated_firecrawl_credits": product.get("estimated_firecrawl_credits", 0),
            "empty_source_confirmed": reported_total == 0,
            "transport_note": (
                "Firecrawl enhanced proxy transports public Lowe's API responses "
                "when the server datacenter IP is denied; no login or CAPTCHA is solved."
            ) if firecrawl_pages else "Direct Lowe's API access",
        }

    @staticmethod
    def validate_hd_payload(status, body):
        """Reject Akamai's HTTP 206 generic envelope and silent empty shapes."""
        raw_signals = (
            body
            if isinstance(body, str)
            else json.dumps(body, ensure_ascii=False, default=str)
        ).lower()
        if any(
            marker in raw_signals
            for marker in (
                "captcha",
                "challenge",
                "verify you are human",
                "unusual traffic",
                "error page",
            )
        ):
            raise HomeDepotChallengeError()
        if int(status or 0) in {401, 403} or "access denied" in raw_signals:
            raise HomeDepotAccessDeniedError()
        try:
            data = json.loads(body) if isinstance(body, str) else body
        except (TypeError, json.JSONDecodeError) as exc:
            raise HomeDepotAccessError(
                f"Home Depot reviews API returned non-JSON HTTP {status}"
            ) from exc
        errors = data.get("errors") or data.get("error") if isinstance(data, dict) else None
        reviews = (data.get("data") or {}).get("reviews") if isinstance(data, dict) else None
        if int(status) != 200 or not isinstance(reviews, dict):
            detail = errors or body
            raise HomeDepotAccessError(
                f"Home Depot reviews API returned HTTP {status} without reviews: "
                f"{str(detail)[:240]}"
            )
        results = reviews.get("Results")
        total = reviews.get("TotalResults")
        if results is not None and not isinstance(results, list):
            raise HomeDepotAccessError("Home Depot reviews API Results is not a list")
        if not isinstance(total, int):
            raise HomeDepotAccessError("Home Depot reviews API omitted TotalResults")
        return results or [], total

    async def hd_page(
        self,
        page,
        item_id,
        start,
        sort,
        filters,
        product_url=None,
    ):
        payload = {
            "operationName": "reviews",
            "variables": {
                "itemId": item_id,
                "sortBy": sort,
                "startIndex": start,
                "pagesize": "100",
                "recfirstpage": "30",
                "filters": filters,
                "searchTerm": None,
                "excludeFamily": False,
            },
            "query": HD_QUERY,
        }
        js = """async ({url,payload})=>{try{const r=await fetch(url,{method:'POST',credentials:'include',headers:{'content-type':'application/json','x-experience-name':'fusion-gm-pip-desktop','x-debug':'false','x-hd-dc':'origin','x-current-url':location.pathname},body:JSON.stringify(payload)});return {status:r.status,body:await r.text()}}catch(error){return {status:0,body:String(error)}}}"""
        attempts = int(os.getenv("HOMEDEPOT_PAGE_ATTEMPTS", "4"))
        last = None
        for attempt in range(1, attempts + 1):
            try:
                response = await page.evaluate(
                    js,
                    {"url": HD_URL, "payload": payload},
                )
                return self.validate_hd_payload(
                    response.get("status", 0),
                    response.get("body", ""),
                )
            except (HomeDepotChallengeError, HomeDepotAccessDeniedError):
                raise
            except Exception as exc:
                last = exc
                if attempt >= attempts:
                    break
                await page.wait_for_timeout(
                    1000 * min(10, attempt * 2) + random.randint(100, 600)
                )
                if product_url and attempt >= 2:
                    try:
                        await page.goto(
                            product_url,
                            wait_until="domcontentloaded",
                            timeout=90000,
                        )
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass
        if isinstance(last, HomeDepotClassifiedError):
            raise type(last)(attempt_count=attempts) from last
        raise HomeDepotAccessError(
            f"Home Depot review page failed after {attempts} attempts: {last}"
        ) from last

    async def hd_direct_view(self, page, product, sort, filters):
        rows, start, reported, requests_made = [], 1, None, 0
        while start <= HD_SLICE_START_LIMIT:
            batch, current_total = await self.hd_page(
                page,
                product["id"],
                start,
                sort,
                filters,
                product_url=product["url"],
            )
            if reported is None:
                reported = current_total
            elif current_total != reported:
                raise HomeDepotAccessError(
                    f"Home Depot total changed within one view: "
                    f"{reported} -> {current_total}"
                )
            requests_made += 1
            if not batch:
                break
            rows.extend(batch)
            if start + len(batch) > current_total:
                break
            next_start = start + len(batch)
            # The first 100-row request returns only 10 rows, which would
            # otherwise make the next request after 411 start at 511.  Home
            # Depot accepts a final overlapping window at 501, covering source
            # positions 501-600 before its hard window limit.
            if (
                next_start > 501
                and start < 501
                and current_total > 500
            ):
                start = 501
            else:
                start = next_start
            await page.wait_for_timeout(450 + random.randint(0, 250))
        return rows, int(reported or 0), {
            "sort": sort,
            "filters": filters,
            "requests": requests_made,
            "last_start_index": start,
        }

    @staticmethod
    def hd_view_filters(star, verified):
        filters = {"starRatings": [star]}
        if verified is not None:
            filters["isVerifiedPurchase"] = verified
        return filters

    @staticmethod
    def hd_search_terms(product, records, star):
        """Prioritize bounded, source-backed terms for middle-window recovery."""
        stopwords = {
            "about", "after", "again", "also", "and", "are", "because",
            "been", "before", "but", "can", "could", "did", "does", "for",
            "from", "get", "got", "had", "has", "have", "home", "into",
            "its", "just", "more", "not", "now", "one", "our", "out",
            "product", "reviews", "that", "the", "their", "them", "then",
            "there", "these", "they", "this", "too", "use", "used", "very",
            "was", "were", "what", "when", "which", "will", "with",
            "would", "you", "your",
        }
        token_pattern = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
        title_terms = []
        for token in token_pattern.findall(product.get("title") or ""):
            term = token.lower().strip("-'")
            if term not in stopwords and term not in title_terms:
                title_terms.append(term)
        counts = Counter()
        for record in records:
            try:
                record_star = int(record.get("rating") or 0)
            except (TypeError, ValueError):
                continue
            if record_star != int(star):
                continue
            body = "{} {}".format(
                record.get("title") or "",
                record.get("review_text") or "",
            )
            counts.update(
                {
                    token.lower().strip("-'")
                    for token in token_pattern.findall(body)
                    if token.lower().strip("-'") not in stopwords
                }
            )
        matched_title_terms = [
            term for term in title_terms if counts.get(term, 0) > 0
        ]
        unmatched_title_terms = [
            term for term in title_terms if counts.get(term, 0) == 0
        ]
        frequency_terms = [
            term
            for term, _ in counts.most_common()
            if term not in matched_title_terms
        ]
        title_order = {
            term: index for index, term in enumerate(title_terms)
        }
        matched_title_terms.sort(
            key=lambda term: (
                -counts.get(term, 0),
                title_order[term],
            )
        )
        broad_recovery_terms = [
            "a", "i", "it", "is", "to", "of", "in",
            "on", "my", "we", "an", "or", "no", "so",
        ]
        limit = int(os.getenv("HOMEDEPOT_SEARCH_TERM_LIMIT", "64"))
        # Product words usually expose a useful slice first. A broad,
        # one-character term is deliberately next: Home Depot search results
        # remain source-filtered, and this view reaches otherwise inaccessible
        # middle windows much faster than exhausting low-frequency words.
        priority_title_terms = matched_title_terms[:2]
        remaining_title_terms = matched_title_terms[2:]
        candidates = (
            priority_title_terms
            + [
                term
                for term in broad_recovery_terms[:1]
                if term not in priority_title_terms
            ]
            + frequency_terms[:8]
            + [
                term
                for term in broad_recovery_terms[1:]
                if term not in matched_title_terms
                and term not in frequency_terms[:8]
            ]
            + frequency_terms[8:40]
            + remaining_title_terms
            + unmatched_title_terms
        )
        return candidates[:max(0, limit)]

    @staticmethod
    def merge_hd_rows(product, rows, records, seen, star_seen):
        for raw in rows:
            review_id = str(raw.get("Id") or "")
            if not review_id:
                continue
            star_seen.add(review_id)
            if review_id in seen:
                continue
            seen.add(review_id)
            records.append(norm_record("homedepot", product, raw))

    async def collect_hd_direct(self, product, job_id=None):
        records, seen, rating_total = [], set(), 0
        star_review_totals, view_stats = {}, []
        p, browser, context = await self.browser()
        try:
            page = await context.new_page()
            await page.goto(
                product["url"],
                wait_until="domcontentloaded",
                timeout=90000,
            )
            await page.wait_for_timeout(3500)
            _, rating_total = await self.hd_page(
                page,
                product["id"],
                1,
                "newest",
                None,
                product_url=product["url"],
            )
            title = await page.title()
            if title and "error page" not in title.lower():
                product["title"] = title
            for star in range(1, 6):
                star_seen = set()
                for sort, verified in HD_BASE_VIEWS:
                    filters = self.hd_view_filters(star, verified)
                    rows, reported, stats = await self.hd_direct_view(
                        page,
                        product,
                        sort,
                        filters,
                    )
                    star_review_totals.setdefault(star, reported)
                    if reported != star_review_totals[star]:
                        raise HomeDepotAccessError(
                            f"Home Depot {star}-star total changed between views"
                        )
                    self.merge_hd_rows(product, rows, records, seen, star_seen)
                    view_stats.append(stats)
                    if job_id and self.store:
                        self.store.update(
                            job_id,
                            f"homedepot: {len(records)} unique records; "
                            f"{star}-star/{sort}",
                        )
                if len(star_seen) < star_review_totals[star]:
                    for sort, verified in HD_EXPANSION_VIEWS:
                        filters = self.hd_view_filters(star, verified)
                        rows, _, stats = await self.hd_direct_view(
                            page,
                            product,
                            sort,
                            filters,
                        )
                        self.merge_hd_rows(
                            product,
                            rows,
                            records,
                            seen,
                            star_seen,
                        )
                        view_stats.append(stats)
                        if job_id and self.store:
                            self.store.update(
                                job_id,
                                f"homedepot: {len(records)} unique records; "
                                f"{star}-star/{sort} expanded",
                            )
                        if len(star_seen) >= star_review_totals[star]:
                            break
        finally:
            await context.close()
            await browser.close()
            await p.stop()
        review_total = sum(star_review_totals.values())
        if len(records) != review_total:
            raise HomeDepotAccessError(
                f"Home Depot direct slice windows returned "
                f"{len(records)}/{review_total} unique records"
            )
        return records, review_total, {
            "method": "Home Depot PDP GraphQL with disjoint star slices",
            "access_method": "server_chrome",
            "rating_total": rating_total,
            "review_record_total": review_total,
            "aggregate_ratings_without_review_record": max(
                0,
                rating_total - review_total,
            ),
            "star_review_totals": star_review_totals,
            "slice_views": view_stats,
            "offset_limit": HD_SLICE_START_LIMIT,
            "empty_source_confirmed": False,
        }

    @staticmethod
    def hd_reviews_page_url(product_url, page_number):
        parts = urlsplit(product_url)
        path = parts.path.rstrip("/")
        if "/p/reviews/" not in path:
            path = path.replace("/p/", "/p/reviews/", 1)
        else:
            path = re.sub(r"/\d+$", "", path)
        return urlunsplit(
            (
                parts.scheme or "https",
                parts.netloc,
                f"{path}/{int(page_number)}",
                "",
                "",
            )
        )

    @staticmethod
    def parse_hd_reviews_page(raw_html, item_id, page_number):
        marker = "window.__APOLLO_STATE__="
        start = raw_html.find(marker)
        if start < 0:
            raise HomeDepotAccessError(
                "Home Depot reviews page omitted Apollo state"
            )
        try:
            state, _ = json.JSONDecoder().raw_decode(
                raw_html[start + len(marker):].lstrip()
            )
        except json.JSONDecodeError as exc:
            raise HomeDepotAccessError(
                "Home Depot reviews page Apollo state was invalid"
            ) from exc
        expected_start = (int(page_number) - 1) * 30 + 1
        candidates = []
        query_state = state.get("ROOT_QUERY")
        query_items = []
        if isinstance(query_state, dict):
            query_items.extend(query_state.items())
        # Older PDP builds placed field-keyed query values at the top level.
        # Accept both normalized Apollo-cache layouts.
        query_items.extend(state.items())
        for key, value in query_items:
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            if not key.startswith("reviews(") or not key.endswith(")"):
                continue
            try:
                variables = json.loads(key[len("reviews("):-1])
            except json.JSONDecodeError:
                continue
            variable_item_id = (
                variables.get("itemId")
                or variables.get("itemID")
                or variables.get("productId")
            )
            variable_start = variables.get("startIndex")
            if variable_start is None:
                variable_start = variables.get("start")
            variable_page_size = (
                variables.get("pagesize")
                or variables.get("pageSize")
                or variables.get("page_size")
                or 30
            )
            try:
                start_index = int(variable_start)
                page_size = int(variable_page_size)
            except (TypeError, ValueError):
                continue
            # Home Depot alternates between one- and zero-based query state in
            # SSR builds. The returned row count remains the final guard.
            if (
                str(variable_item_id) != str(item_id)
                or start_index not in {expected_start, expected_start - 1}
                or page_size != 30
            ):
                continue
            candidates.append((variables, value, start_index))
        if not candidates:
            raise HomeDepotAccessError(
                f"Home Depot reviews page {page_number} omitted its "
                f"{expected_start} review window"
            )
        variables, reviews, response_start = candidates[0]
        rows = reviews.get("Results")
        total = reviews.get("TotalResults")
        if not isinstance(rows, list) or not isinstance(total, int):
            raise HomeDepotAccessError(
                f"Home Depot reviews page {page_number} returned an "
                f"invalid review window"
            )
        one_based_start = response_start + 1 if response_start == expected_start - 1 else response_start
        expected_rows = max(0, min(30, total - one_based_start + 1))
        if len(rows) != expected_rows:
            raise HomeDepotAccessError(
                f"Home Depot reviews page {page_number} returned "
                f"{len(rows)}/{expected_rows} rows"
            )
        product_title = ""
        product = state.get(f"base-catalog-{item_id}") or {}
        identifiers = product.get("identifiers") or {}
        if identifiers.get("productLabel"):
            product_title = identifiers["productLabel"]
        return {
            "page": int(page_number),
            "start_index": expected_start,
            "rows": rows,
            "total": total,
            "sort": variables.get("sortBy"),
            "title": product_title,
        }

    def firecrawl_hd_reviews_page(
        self,
        product_url,
        item_id,
        page_number,
        proxy="basic",
    ):
        api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        if not api_key:
            raise HomeDepotAccessError(
                "FIRECRAWL_API_KEY is not configured for Home Depot fallback"
            )
        review_url = self.hd_reviews_page_url(product_url, page_number)
        response = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "url": review_url,
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "proxy": proxy,
                "maxAge": int(
                    os.getenv("FIRECRAWL_HD_CACHE_AGE_MS", "0")
                ),
                "timeout": 120000,
                "location": {
                    "country": "US",
                    "languages": ["en-US"],
                },
            },
            timeout=int(os.getenv("FIRECRAWL_HD_TIMEOUT_SECONDS", "150")),
        )
        if response.status_code in (401, 402, 403):
            raise HomeDepotAccessError(
                f"Firecrawl credential, billing, or access check returned "
                f"HTTP {response.status_code}"
            )
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            retry_after = int(
                os.getenv("HOMEDEPOT_UPSTREAM_RETRY_SECONDS", "300")
            )
            try:
                retry_after = int(response.headers.get("Retry-After", retry_after))
            except (TypeError, ValueError):
                pass
            raise HomeDepotUpstreamError(
                "Firecrawl transport for Home Depot is temporarily unavailable "
                f"(HTTP {response.status_code})",
                retry_after=retry_after,
            )
        response.raise_for_status()
        envelope = response.json()
        if not envelope.get("success"):
            raise HomeDepotAccessError(
                f"Firecrawl did not complete Home Depot reviews page "
                f"{page_number}"
            )
        data = envelope.get("data") or {}
        metadata = data.get("metadata") or {}
        title = str(metadata.get("title") or "")
        raw_html = data.get("rawHtml") or ""
        page_signals = f"{title}\n{raw_html}".lower()
        if any(
            marker in page_signals
            for marker in (
                "captcha",
                "challenge",
                "verify you are human",
                "unusual traffic",
            )
        ):
            raise HomeDepotChallengeError()
        if "access denied" in page_signals:
            raise HomeDepotAccessDeniedError()
        if "error page" in page_signals:
            raise HomeDepotChallengeError()
        if str(item_id) not in raw_html:
            raise HomeDepotProductEvidenceError()
        result = self.parse_hd_reviews_page(
            raw_html,
            item_id,
            page_number,
        )
        result["url"] = review_url
        result["proxy"] = proxy
        result["estimated_credits"] = 1 if proxy == "basic" else 5
        return result

    async def hd_firecrawl_reviews_page(
        self,
        product,
        page_number,
    ):
        attempt_limit = max(
            1, int(os.getenv("HOMEDEPOT_PAGE_ATTEMPTS", "4"))
        )
        attempts = tuple("basic" for _ in range(max(0, attempt_limit - 1))) + ("enhanced",)
        last = None
        estimated_credits = 0
        for index, proxy in enumerate(attempts, start=1):
            estimated_credits += 1 if proxy == "basic" else 5
            try:
                result = await asyncio.to_thread(
                    self.firecrawl_hd_reviews_page,
                    product["url"],
                    product["id"],
                    page_number,
                    proxy,
                )
                result["attempts"] = index
                result["estimated_credits"] = estimated_credits
                return result
            except (HomeDepotChallengeError, HomeDepotAccessDeniedError) as exc:
                exc.estimated_credits = estimated_credits
                raise
            except Exception as exc:
                last = exc
                if index < len(attempts):
                    base = max(
                        0.0,
                        float(os.getenv("HOMEDEPOT_UPSTREAM_BACKOFF_SECONDS", "5")),
                    )
                    await asyncio.sleep(min(60.0, base * (2 ** (index - 1))))
        if isinstance(last, HomeDepotUpstreamError):
            raise HomeDepotUpstreamError(
                f"Home Depot reviews page {page_number} failed after "
                f"{len(attempts)} attempts: {last}",
                retry_after=last.retry_after,
            ) from last
        if isinstance(last, HomeDepotClassifiedError):
            error = type(last)(attempt_count=len(attempts))
            error.estimated_credits = estimated_credits
            raise error from last
        error = HomeDepotAccessError(
            f"Home Depot reviews page {page_number} failed after "
            f"{len(attempts)} attempts: {last}"
        )
        error.estimated_credits = estimated_credits
        raise error from last

    def load_hd_page_checkpoint(self, job_id, product_id):
        if not job_id or not self.store:
            return {}
        path = (
            self.store.data_dir / "runs" / job_id / "homedepot"
            / "hd-page-checkpoint.jsonl"
        )
        pages = {}
        if not path.is_file():
            return pages
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                str(item.get("product_id")) == str(product_id)
                and isinstance(item.get("rows"), list)
                and int(item.get("page") or 0) > 0
            ):
                pages[int(item["page"])] = item
        return pages

    def append_hd_page_checkpoint(self, job_id, product_id, result):
        if not job_id or not self.store:
            return
        path = (
            self.store.data_dir / "runs" / job_id / "homedepot"
            / "hd-page-checkpoint.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "product_id": str(product_id),
            "page": int(result["page"]),
            "total": int(result["total"]),
            "rows": result["rows"],
            "url": result.get("url"),
            "proxy": result.get("proxy"),
            "attempts": int(result.get("attempts") or 1),
            "estimated_credits": int(result.get("estimated_credits") or 0),
            "collected_at": now(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def collect_hd_review_pages(self, product, job_id=None):
        """Collect canonical 30-row SSR windows with bounded time and credits."""
        started = time.monotonic()
        max_seconds = max(
            60, int(os.getenv("HOMEDEPOT_MAX_RUNTIME_SECONDS", "1200"))
        )
        max_credits = max(
            1, int(os.getenv("FIRECRAWL_HD_MAX_CREDITS_PER_JOB", "300"))
        )
        reserve = max(
            0, int(os.getenv("FIRECRAWL_HD_CREDIT_RESERVE", "500"))
        )
        drift_tolerance = max(
            0, int(os.getenv("HOMEDEPOT_TOTAL_DRIFT_TOLERANCE", "50"))
        )
        remaining = await asyncio.to_thread(self.firecrawl_remaining_credits)
        if remaining is not None:
            max_credits = min(max_credits, max(0, remaining - reserve))
        if max_credits <= 0:
            raise HomeDepotAccessError(
                f"Home Depot collection stopped before spending reserved credits; "
                f"{remaining} remain and {reserve} are reserved"
            )

        checkpoint = self.load_hd_page_checkpoint(job_id, product["id"])
        records, seen = [], set()
        source_total = None
        source_total_initial = None
        source_total_final = None
        observed_totals = []
        pages_reused = 0
        pages_requested = 0
        credits_used = 0
        stop_reason = ""
        checkpoint_invalidated = False

        def merge_page(item):
            nonlocal source_total, source_total_initial, source_total_final
            current_total = int(item["total"])
            if source_total_initial is None:
                source_total_initial = current_total
            observed_totals.append(current_total)
            source_total_final = current_total
            # Keep the largest observed source count even when this page is
            # outside tolerance so a partial result never claims a smaller,
            # stale denominator.
            source_total = max(observed_totals)
            if max(observed_totals) - min(observed_totals) > drift_tolerance:
                raise HomeDepotAccessError(
                    "Home Depot source total drift exceeded "
                    f"{drift_tolerance}: {min(observed_totals)} -> {max(observed_totals)}"
                )
            # Collect through the largest observed total. A one-to-fifty row
            # storefront update does not invalidate the useful snapshot.
            for raw in item["rows"]:
                review_id = str(raw.get("Id") or "")
                if review_id and review_id not in seen:
                    seen.add(review_id)
                    records.append(norm_record("homedepot", product, raw))

        try:
            for page_number in sorted(checkpoint):
                merge_page(checkpoint[page_number])
                pages_reused += 1
        except HomeDepotAccessError:
            # A prior execution belongs to another source epoch. Ignore it;
            # later writes supersede its page entries in the checkpoint file.
            checkpoint_invalidated = bool(checkpoint)
            checkpoint = {}
            records, seen, observed_totals = [], set(), []
            source_total = source_total_initial = source_total_final = None
            pages_reused = 0

        page_number = 1
        while source_total is None or page_number <= max(
            1, (source_total + 29) // 30
        ):
            if page_number in checkpoint:
                page_number += 1
                continue
            elapsed = time.monotonic() - started
            if elapsed >= max_seconds:
                stop_reason = (
                    f"reached {max_seconds}s Home Depot runtime safety limit"
                )
                break
            if credits_used >= max_credits:
                stop_reason = (
                    f"reached {max_credits}-credit Home Depot per-job safety limit"
                )
                break
            try:
                result = await self.hd_firecrawl_reviews_page(
                    product, page_number
                )
            except HomeDepotClassifiedError as exc:
                credits_used += int(getattr(exc, "estimated_credits", 0))
                if not records:
                    raise
                stop_reason = str(exc)
                break
            except HomeDepotAccessError as exc:
                credits_used += int(getattr(exc, "estimated_credits", 0))
                stop_reason = str(exc)
                break
            pages_requested += 1
            credits_used += int(result.get("estimated_credits") or 0)
            if result.get("title"):
                product["title"] = result["title"]
            try:
                merge_page(result)
            except HomeDepotAccessError as exc:
                stop_reason = str(exc)
                break
            self.append_hd_page_checkpoint(job_id, product["id"], result)
            if job_id and self.store:
                self.store.update(
                    job_id,
                    f"homedepot: {len(records)}/{source_total} records; "
                    f"official review page {page_number}",
                )
            page_number += 1
            if credits_used >= max_credits:
                stop_reason = (
                    f"reached {max_credits}-credit Home Depot per-job safety limit"
                )
                break
            await asyncio.sleep(
                float(os.getenv("HOMEDEPOT_PAGE_DELAY_SECONDS", "0.15"))
            )

        source_total = int(source_total or 0)
        if not records and source_total == 0 and not stop_reason:
            empty_source_confirmed = True
        else:
            empty_source_confirmed = False
        if stop_reason and not records:
            raise HomeDepotAccessError(stop_reason)
        if len(records) > source_total:
            raise HomeDepotAccessError(
                f"Home Depot official review pages returned "
                f"{len(records)}/{source_total} rows at an invalid product grain"
            )
        complete = len(records) == source_total and (
            source_total > 0 or empty_source_confirmed
        )
        if not complete and not stop_reason:
            stop_reason = (
                f"official review pages returned {len(records)}/{source_total} "
                "unique records"
            )
        return records, source_total, {
            "method": "Home Depot official server-rendered review pages",
            "access_method": "firecrawl_raw_html",
            "page_size": 30,
            "checkpoint_pages_reused": pages_reused,
            "checkpoint_invalidated": checkpoint_invalidated,
            "pages_requested": pages_requested,
            "estimated_firecrawl_credits": credits_used,
            "credit_limit": max_credits,
            "runtime_limit_seconds": max_seconds,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stop_reason": stop_reason,
            "source_total_initial": source_total_initial,
            "source_total_final": source_total_final,
            "source_total_min": min(observed_totals) if observed_totals else None,
            "source_total_max": max(observed_totals) if observed_totals else None,
            "source_total_drift": (
                max(observed_totals) - min(observed_totals)
                if observed_totals else 0
            ),
            "source_total_drift_tolerance": drift_tolerance,
            "empty_source_confirmed": empty_source_confirmed,
        }

    @staticmethod
    def hd_firecrawl_slice_script(
        item_id,
        sort,
        filters,
        start_index=1,
        exclude_family=False,
        search_term=None,
    ):
        """Build one bounded in-browser GraphQL slice.

        Home Depot rejects the same public GraphQL request from datacenter
        HTTP clients, while accepting it from a browser session established on
        the product's official reviews route.  The API also caps a single
        sorted/filter view, so callers combine several independently bounded
        views and reconcile unique IDs against the source totals.
        """
        config = json.dumps(
            {
                "itemId": str(item_id),
                "sort": sort,
                "filters": filters,
                "startIndex": int(start_index),
                "excludeFamily": bool(exclude_family),
                "searchTerm": search_term,
                "startLimit": HD_SLICE_START_LIMIT,
                "attempts": int(os.getenv("HOMEDEPOT_PAGE_ATTEMPTS", "4")),
            },
            separators=(",", ":"),
        )
        endpoint = json.dumps(HD_URL)
        query = json.dumps(HD_QUERY)
        return """
(async () => {
  const config = %s;
  const endpoint = %s;
  const query = %s;
  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const rows = [];
  const reviewIds = new Set();
  const familyIds = new Set();
  let total = null;
  let start = config.startIndex;
  let requests = 0;
  let lastStatus = 0;
  while (start <= config.startLimit) {
    let reviews = null;
    let lastError = "";
    for (let attempt = 1; attempt <= config.attempts; attempt += 1) {
      const payload = {
        operationName: "reviews",
        variables: {
          itemId: config.itemId,
          sortBy: config.sort,
          startIndex: start,
          pagesize: "100",
          recfirstpage: "30",
          filters: config.filters,
          searchTerm: config.searchTerm,
          excludeFamily: config.excludeFamily
        },
        query
      };
      try {
        const response = await fetch(endpoint, {
          method: "POST",
          credentials: "include",
          headers: {
            "content-type": "application/json",
            "x-experience-name": "fusion-gm-pip-desktop",
            "x-debug": "false",
            "x-hd-dc": "origin",
            "x-current-url": location.pathname
          },
          body: JSON.stringify(payload)
        });
        lastStatus = response.status;
        const body = await response.text();
        let envelope = null;
        try {
          envelope = JSON.parse(body);
        } catch (error) {
          lastError = "non-JSON HTTP " + response.status;
        }
        const candidate = envelope && envelope.data && envelope.data.reviews;
        if (
          response.status === 200 &&
          candidate &&
          Array.isArray(candidate.Results) &&
          Number.isInteger(candidate.TotalResults)
        ) {
          reviews = candidate;
          break;
        }
        if (!lastError) {
          const sourceErrors = envelope && (envelope.errors || envelope.error);
          lastError = (
            "HTTP " + response.status + " without reviews" +
            (sourceErrors ? ": " + JSON.stringify(sourceErrors).slice(0, 500) : "")
          );
        }
      } catch (error) {
        lastError = String(error);
      }
      if (attempt < config.attempts) {
        await sleep(700 * attempt + Math.floor(Math.random() * 350));
      }
    }
    if (!reviews) {
      if (rows.length >= 600 && start >= 600) {
        return JSON.stringify({
          ok: true,
          total: total === null ? 0 : total,
          rows,
          familyIds: Array.from(familyIds),
          requests,
          lastStartIndex: start,
          windowExhausted: true,
          exhaustionReason: lastError || "source window limit"
        });
      }
      return JSON.stringify({
        ok: false,
        error: lastError || "reviews request failed",
        status: lastStatus,
        total,
        rows,
        familyIds: Array.from(familyIds),
        nextStart: start,
        start,
        requests
      });
    }
    const currentTotal = reviews.TotalResults;
    if (total === null) {
      total = currentTotal;
    } else if (currentTotal !== total) {
      return JSON.stringify({
        ok: false,
        error: "total changed within slice",
        previousTotal: total,
        currentTotal,
        total,
        rows,
        familyIds: Array.from(familyIds),
        nextStart: start,
        start,
        requests
      });
    }
    const batch = reviews.Results;
    const products = (
      reviews.Includes &&
      reviews.Includes.Products
    ) || {};
    const productEntries = [];
    if (products.store) {
      productEntries.push(products.store);
    }
    if (Array.isArray(products.items)) {
      productEntries.push(...products.items);
    }
    for (const entry of productEntries) {
      if (entry && entry.Id) {
        familyIds.add(String(entry.Id));
      }
      for (const familyId of (entry && entry.FamilyIds) || []) {
        if (familyId) {
          familyIds.add(String(familyId));
        }
      }
    }
    requests += 1;
    for (const raw of batch) {
      if (!raw.Id || reviewIds.has(String(raw.Id))) {
        continue;
      }
      reviewIds.add(String(raw.Id));
      if (raw.ProductId) {
        familyIds.add(String(raw.ProductId));
      }
      rows.push({
        Id: raw.Id,
        ProductId: raw.ProductId,
        SubmissionTime: raw.SubmissionTime,
        Title: raw.Title,
        IsRecommended: raw.IsRecommended,
        UserNickname: raw.UserNickname,
        UserLocation: raw.UserLocation,
        ReviewText: raw.ReviewText,
        Rating: raw.Rating,
        ContextDataValues: raw.ContextDataValues
      });
    }
    if (batch.length === 0 || start + batch.length > total) {
      break;
    }
    const nextStart = start + batch.length;
    if (nextStart > 501 && start < 501 && total > 500) {
      start = 501;
    } else {
      start = nextStart;
    }
    await sleep(220 + Math.floor(Math.random() * 180));
  }
  return JSON.stringify({
    ok: true,
    total: total === null ? 0 : total,
    rows,
    familyIds: Array.from(familyIds),
    requests,
    lastStartIndex: start
  });
})()
""" % (config, endpoint, query)

    def firecrawl_hd_slice(
        self,
        product_url,
        item_id,
        sort,
        filters,
        bootstrap_page=2,
        start_index=1,
        exclude_family=False,
        search_term=None,
        timeout_seconds=None,
    ):
        api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        if not api_key:
            raise HomeDepotAccessError(
                "FIRECRAWL_API_KEY is not configured for Home Depot fallback"
            )
        review_url = self.hd_reviews_page_url(product_url, bootstrap_page)
        request_timeout = int(
            timeout_seconds
            or os.getenv("FIRECRAWL_HD_TIMEOUT_SECONDS", "180")
        )
        response = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "url": review_url,
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "proxy": "enhanced",
                "maxAge": int(
                    os.getenv("FIRECRAWL_HD_CACHE_AGE_MS", "0")
                ),
                "timeout": min(150000, request_timeout * 1000),
                "location": {
                    "country": "US",
                    "languages": ["en-US"],
                },
                "actions": [
                    {
                        "type": "executeJavascript",
                        "script": self.hd_firecrawl_slice_script(
                            item_id,
                            sort,
                            filters,
                            start_index,
                            exclude_family,
                            search_term,
                        ),
                    }
                ],
            },
            timeout=request_timeout,
        )
        if response.status_code in (401, 402, 403):
            raise HomeDepotAccessError(
                f"Firecrawl credential, billing, or access check returned "
                f"HTTP {response.status_code}"
            )
        response.raise_for_status()
        envelope = response.json()
        if not envelope.get("success"):
            raise HomeDepotAccessError(
                "Firecrawl did not complete the Home Depot browser slice"
            )
        data = envelope.get("data") or {}
        action_data = data.get("actions") or {}
        returns = action_data.get("javascriptReturns") or []
        if not returns:
            raise HomeDepotAccessError(
                "Firecrawl Home Depot browser slice omitted its JavaScript result"
            )
        result = returns[0]
        if isinstance(result, dict) and "value" in result:
            result = result["value"]
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise HomeDepotAccessError(
                    "Firecrawl Home Depot browser slice returned invalid JSON"
                ) from exc
        if not isinstance(result, dict):
            raise HomeDepotAccessError(
                "Home Depot browser slice returned an invalid action result: "
                f"{str(result)[:300]}"
            )
        rows = result.get("rows") or []
        total = result.get("total")
        if not isinstance(rows, list):
            raise HomeDepotAccessError(
                "Home Depot browser slice returned an invalid review payload"
            )
        if result.get("ok") and not isinstance(total, int):
            raise HomeDepotAccessError(
                "Home Depot browser slice omitted its source total"
            )
        if total is not None and not isinstance(total, int):
            raise HomeDepotAccessError(
                "Home Depot browser slice returned an invalid source total"
            )
        seen = set()
        for raw in rows:
            review_id = str(raw.get("Id") or "")
            if not review_id:
                raise HomeDepotAccessError(
                    "Home Depot browser slice returned a review without an ID"
                )
            if review_id in seen:
                raise HomeDepotAccessError(
                    "Home Depot browser slice returned duplicate review IDs"
                )
            seen.add(review_id)
        return {
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or ""),
            "rows": rows,
            "total": total,
            "next_start": int(
                result.get("nextStart")
                or result.get("lastStartIndex")
                or start_index
            ),
            "sort": sort,
            "filters": filters,
            "family_ids": [
                str(item)
                for item in (result.get("familyIds") or [])
                if str(item)
            ],
            "exclude_family": bool(exclude_family),
            "search_term": search_term,
            "requests": int(result.get("requests") or 0),
            "last_start_index": int(result.get("lastStartIndex") or 0),
            "window_exhausted": bool(result.get("windowExhausted")),
            "url": review_url,
            "proxy": "enhanced",
            "estimated_credits": 5,
        }

    async def hd_firecrawl_slice(
        self,
        product,
        sort,
        filters,
        bootstrap_page,
        exclude_family=False,
        search_term=None,
        max_sessions=None,
    ):
        attempts = int(
            max_sessions
            or os.getenv("FIRECRAWL_HD_SLICE_ATTEMPTS", "5")
        )
        request_timeout = int(
            os.getenv(
                (
                    "FIRECRAWL_HD_TIMEOUT_SECONDS"
                    if max_sessions is None
                    else "FIRECRAWL_HD_OPTIONAL_TIMEOUT_SECONDS"
                ),
                "180" if max_sessions is None else "100",
            )
        )
        last = None
        next_start = 1
        rows, seen = [], set()
        family_ids = set()
        reported_total = None
        requests_made = 0
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(
                    self.firecrawl_hd_slice,
                    product["url"],
                    product["id"],
                    sort,
                    filters,
                    bootstrap_page,
                    next_start,
                    exclude_family,
                    search_term,
                    request_timeout,
                )
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(attempt * 2)
                continue
            current_total = result.get("total")
            if current_total is not None:
                if reported_total is None:
                    reported_total = current_total
                elif current_total != reported_total:
                    raise HomeDepotAccessError(
                        f"Home Depot {sort} slice total changed between "
                        f"browser sessions: {reported_total} -> {current_total}"
                    )
            for raw in result["rows"]:
                review_id = str(raw.get("Id") or "")
                if review_id not in seen:
                    seen.add(review_id)
                    rows.append(raw)
            family_ids.update(result.get("family_ids") or [])
            requests_made += int(result.get("requests") or 0)
            if result["ok"]:
                result["rows"] = rows
                result["total"] = int(reported_total or 0)
                result["requests"] = requests_made
                result["family_ids"] = sorted(family_ids)
                result["attempts"] = attempt
                result["estimated_credits"] = attempt * 5
                return result
            last = HomeDepotAccessError(
                result["error"] or "browser slice session failed"
            )
            resumed_at = int(result.get("next_start") or next_start)
            if resumed_at < next_start:
                raise HomeDepotAccessError(
                    "Home Depot browser slice returned a backwards checkpoint"
                )
            if (
                resumed_at >= 600
                and len(rows) >= 600
                and reported_total is not None
            ):
                result["ok"] = True
                result["rows"] = rows
                result["total"] = reported_total
                result["requests"] = requests_made
                result["family_ids"] = sorted(family_ids)
                result["last_start_index"] = resumed_at
                result["window_exhausted"] = True
                result["attempts"] = attempt
                result["estimated_credits"] = attempt * 5
                return result
            next_start = resumed_at
            if attempt < attempts:
                await asyncio.sleep(attempt * 2)
        raise HomeDepotAccessError(
            f"Home Depot {sort} browser slice stopped at index {next_start} "
            f"after {attempts} sessions: {last}"
        ) from last

    async def collect_hd_firecrawl(self, product, job_id=None):
        remaining = await asyncio.to_thread(self.firecrawl_remaining_credits)
        reserve = int(os.getenv("FIRECRAWL_HD_CREDIT_RESERVE", "500"))
        if remaining is not None and remaining <= reserve:
            raise HomeDepotAccessError(
                f"Home Depot fallback needs Firecrawl credits; {remaining} "
                f"remain and {reserve} are reserved"
            )
        first = await self.hd_firecrawl_reviews_page(product, 1)
        rating_total = int(first["total"])
        if first.get("title"):
            product["title"] = first["title"]
        bootstrap_page = 2 if rating_total > 30 else 1
        max_credits = int(
            os.getenv("FIRECRAWL_HD_MAX_CREDITS_PER_JOB", "750")
        )
        minimum_credits = 1 + len(HD_BASE_VIEWS) * 5 * 5
        if max_credits < minimum_credits:
            raise HomeDepotAccessError(
                f"Home Depot fallback needs at least {minimum_credits} "
                f"credits for base slices; per-job limit is {max_credits}"
            )
        if remaining is not None and remaining < reserve + minimum_credits:
            raise HomeDepotAccessError(
                f"Home Depot fallback needs at least {minimum_credits} "
                f"credits; {remaining} remain and {reserve} are reserved"
            )
        records, seen = [], set()
        star_review_totals, slice_stats = {}, []
        known_family_ids = {str(product["id"])}
        family_partition_totals = {}
        search_terms_used = {}
        optional_slice_errors = []
        credits_used = int(first.get("estimated_credits") or 0)

        async def load_slice(
            star,
            sort,
            verified,
            stage,
            target_id=None,
            exclude_family=False,
            search_term=None,
        ):
            nonlocal credits_used
            if credits_used + 5 > max_credits:
                raise HomeDepotAccessError(
                    f"Home Depot fallback reached its {max_credits}-credit "
                    "per-job safety limit"
                )
            slice_product = product
            if target_id is not None and str(target_id) != str(product["id"]):
                slice_product = dict(product)
                slice_product["id"] = str(target_id)
            result = await self.hd_firecrawl_slice(
                slice_product,
                sort,
                self.hd_view_filters(star, verified),
                bootstrap_page,
                exclude_family=exclude_family,
                search_term=search_term,
                max_sessions=(
                    None
                    if stage in {
                        "base_primary",
                        "base_secondary",
                        "search_priority",
                    }
                    else int(
                        os.getenv(
                            "FIRECRAWL_HD_OPTIONAL_SLICE_ATTEMPTS",
                            "1",
                        )
                    )
                ),
            )
            known_family_ids.update(result.get("family_ids") or [])
            credits_used += int(result.get("estimated_credits") or 0)
            if credits_used > max_credits:
                raise HomeDepotAccessError(
                    f"Home Depot fallback used {credits_used} estimated "
                    f"credits, above the per-job limit {max_credits}"
                )
            slice_stats.append(
                {
                    "star": star,
                    "stage": stage,
                    "item_id": str(target_id or product["id"]),
                    "sort": sort,
                    "verified": verified,
                    "exclude_family": exclude_family,
                    "search_term": search_term,
                    "reported_total": result["total"],
                    "rows": len(result["rows"]),
                    "requests": result["requests"],
                    "last_start_index": result["last_start_index"],
                    "window_exhausted": result["window_exhausted"],
                    "attempts": result["attempts"],
                }
            )
            return result

        async def load_family_partitions(star, star_seen):
            """Use source product IDs before broader search-term expansion."""
            processed_family_ids = set()
            while True:
                pending_family_ids = sorted(
                    known_family_ids - processed_family_ids
                )
                if not pending_family_ids:
                    return
                family_id = pending_family_ids[0]
                processed_family_ids.add(family_id)
                family_seen = set()
                family_total = None
                for sort, _ in HD_BASE_VIEWS:
                    try:
                        result = await load_slice(
                            star,
                            sort,
                            None,
                            "family",
                            target_id=family_id,
                            exclude_family=True,
                        )
                    except HomeDepotAccessError as exc:
                        if "credit" in str(exc).lower():
                            raise
                        optional_slice_errors.append(
                            {
                                "star": star,
                                "stage": "family",
                                "item_id": family_id,
                                "sort": sort,
                                "error": str(exc)[:500],
                            }
                        )
                        break
                    current_total = int(result["total"])
                    if family_total is None:
                        family_total = current_total
                        family_partition_totals.setdefault(
                            star,
                            {},
                        )[family_id] = current_total
                    elif current_total != family_total:
                        raise HomeDepotAccessError(
                            f"Home Depot family {family_id} total "
                            "changed between views"
                        )
                    family_seen.update(
                        str(raw.get("Id") or "")
                        for raw in result["rows"]
                        if raw.get("Id")
                    )
                    self.merge_hd_rows(
                        product,
                        result["rows"],
                        records,
                        seen,
                        star_seen,
                    )
                    if job_id and self.store:
                        self.store.update(
                            job_id,
                            f"homedepot: {len(records)} unique records; "
                            f"{star}-star family {family_id}/{sort}",
                        )
                    if (
                        len(star_seen) >= star_review_totals[star]
                        or len(family_seen) >= int(family_total or 0)
                    ):
                        break
                if len(star_seen) >= star_review_totals[star]:
                    return

        for star in range(1, 6):
            star_seen = set()
            for base_index, (sort, verified) in enumerate(HD_BASE_VIEWS):
                try:
                    result = await load_slice(
                        star,
                        sort,
                        verified,
                        (
                            "base_primary"
                            if base_index == 0
                            else "base_secondary"
                        ),
                    )
                except HomeDepotAccessError as exc:
                    if base_index == 0 or "credit" in str(exc).lower():
                        raise
                    optional_slice_errors.append(
                        {
                            "star": star,
                            "stage": "base_secondary",
                            "sort": sort,
                            "error": str(exc)[:500],
                        }
                    )
                    break
                reported = int(result["total"])
                if star not in star_review_totals:
                    star_review_totals[star] = reported
                elif reported != star_review_totals[star]:
                    raise HomeDepotAccessError(
                        f"Home Depot {star}-star total changed between "
                        f"base views: {star_review_totals[star]} -> {reported}"
                    )
                self.merge_hd_rows(
                    product,
                    result["rows"],
                    records,
                    seen,
                    star_seen,
                )
                if job_id and self.store:
                    self.store.update(
                        job_id,
                        f"homedepot: {len(records)} unique records; "
                        f"{star}-star/{sort} via enhanced browser",
                    )
                if len(star_seen) >= reported:
                    break

            if len(star_seen) < star_review_totals[star]:
                await load_family_partitions(star, star_seen)

            if len(star_seen) < star_review_totals[star]:
                search_terms_used[star] = []
                for term in self.hd_search_terms(product, records, star):
                    term_seen = set()
                    term_total = None
                    search_terms_used[star].append(term)
                    for sort, _ in HD_BASE_VIEWS:
                        try:
                            result = await load_slice(
                                star,
                                sort,
                                None,
                                (
                                "search_priority"
                                    if term == "a"
                                    else "search_term"
                                ),
                                search_term=term,
                            )
                        except HomeDepotAccessError as exc:
                            if "credit" in str(exc).lower():
                                raise
                            optional_slice_errors.append(
                                {
                                    "star": star,
                                    "stage": "search_term",
                                    "search_term": term,
                                    "sort": sort,
                                    "error": str(exc)[:500],
                                }
                            )
                            break
                        current_total = int(result["total"])
                        if term_total is None:
                            term_total = current_total
                        elif current_total != term_total:
                            raise HomeDepotAccessError(
                                f"Home Depot search term {term!r} total "
                                "changed between views"
                            )
                        term_seen.update(
                            str(raw.get("Id") or "")
                            for raw in result["rows"]
                            if raw.get("Id")
                        )
                        self.merge_hd_rows(
                            product,
                            result["rows"],
                            records,
                            seen,
                            star_seen,
                        )
                        if job_id and self.store:
                            self.store.update(
                                job_id,
                                f"homedepot: {len(records)} unique records; "
                                f"{star}-star search {term!r}/{sort}",
                            )
                        if (
                            len(star_seen) >= star_review_totals[star]
                            or len(term_seen) >= int(term_total or 0)
                        ):
                            break
                    if len(star_seen) >= star_review_totals[star]:
                        break

            if len(star_seen) < star_review_totals[star]:
                for sort, verified in HD_EXPANSION_VIEWS:
                    try:
                        result = await load_slice(
                            star,
                            sort,
                            verified,
                            "expanded",
                        )
                    except HomeDepotAccessError as exc:
                        if "credit" in str(exc).lower():
                            raise
                        optional_slice_errors.append(
                            {
                                "star": star,
                                "stage": "expanded",
                                "sort": sort,
                                "verified": verified,
                                "error": str(exc)[:500],
                            }
                        )
                        continue
                    if (
                        verified is None
                        and int(result["total"]) != star_review_totals[star]
                    ):
                        raise HomeDepotAccessError(
                            f"Home Depot {star}-star total changed between "
                            "unverified-filter views"
                        )
                    self.merge_hd_rows(
                        product,
                        result["rows"],
                        records,
                        seen,
                        star_seen,
                    )
                    if job_id and self.store:
                        self.store.update(
                            job_id,
                            f"homedepot: {len(records)} unique records; "
                            f"{star}-star/{sort} expanded",
                        )
                    if len(star_seen) >= star_review_totals[star]:
                        break
            if len(star_seen) != star_review_totals[star]:
                raise HomeDepotAccessError(
                    f"Home Depot enhanced slice windows returned "
                    f"{len(star_seen)}/{star_review_totals[star]} unique "
                    f"{star}-star records"
                )

        review_total = sum(star_review_totals.values())
        if len(records) != review_total:
            raise HomeDepotAccessError(
                f"Home Depot enhanced slice windows returned "
                f"{len(records)}/{review_total} unique records"
            )
        verification = await self.hd_firecrawl_reviews_page(product, 1)
        if (
            int(verification["total"]) != rating_total
            or {
                str(item.get("Id") or "")
                for item in verification["rows"]
            }
            != {
                str(item.get("Id") or "")
                for item in first["rows"]
            }
        ):
            raise HomeDepotAccessError(
                "Home Depot reviews changed during collection; "
                "retry required for a consistent snapshot"
            )
        credits_used += int(verification.get("estimated_credits") or 0)
        return records, review_total, {
            "method": (
                "Home Depot official reviews route plus browser GraphQL "
                "star/sort slices"
            ),
            "access_method": "firecrawl_enhanced_browser_slices",
            "rating_total": rating_total,
            "review_record_total": review_total,
            "aggregate_ratings_without_review_record": max(
                0,
                rating_total - review_total,
            ),
            "star_review_totals": star_review_totals,
            "family_ids": sorted(known_family_ids),
            "family_partition_totals": family_partition_totals,
            "search_terms_used": search_terms_used,
            "optional_slice_errors": optional_slice_errors,
            "slice_views": slice_stats,
            "offset_limit": HD_SLICE_START_LIMIT,
            "bootstrap_page": bootstrap_page,
            "verification_page_reloaded": True,
            "estimated_firecrawl_credits": credits_used,
            "empty_source_confirmed": (
                review_total == 0 and rating_total == 0
            ),
        }

    async def collect_hd(self, product, job_id=None):
        from .bounded import homedepot
        return await homedepot(self, product, job_id)

    async def collect_bv(self, channel, product, job_id=None):
        source_url = product.get("source_url", product["url"])
        source_product_id = product.get("source_product_id", product["id"])
        if product.get("collection_method") == "official_public_bazaarvoice_loader":
            template, _, discovered_product_id, discovery_evidence = self.discover_public_bv(
                channel,
                source_url,
                source_product_id,
                expected_category=product.get("_expected_category"),
                expected_model=product.get("_expected_model"),
            )
            if discovered_product_id != source_product_id:
                raise RuntimeError(
                    f"{channel}: Bazaarvoice product id changed from "
                    f"{source_product_id} to {discovered_product_id}"
                )
        else:
            template, _ = await self.discover_bv_retry(source_url)
            discovery_evidence = {}
        records, seen, offset, total = [], set(), 0, 0
        accepted_product_ids = set()
        excluded_product_ids = set()
        observed_totals = []
        while True:
            response = (
                self.tractor_supply_public_get(
                    template.page_url(source_product_id, offset),
                    "public Bazaarvoice reviews API",
                    headers=template.headers,
                    timeout=30,
                )
                if channel == "tractorsupply"
                else
                self.official_public_get
                if channel in OFFICIAL_STRICT_BV_CHANNELS
                else self.session.get
            )
            if channel != "tractorsupply":
                response = response(
                    template.page_url(source_product_id, offset),
                    headers=template.headers,
                    timeout=30,
                )
            if channel in OFFICIAL_STRICT_BV_CHANNELS:
                guard_public_response(channel, response, "public Bazaarvoice API")
            if channel == "ego":
                guard_public_response(channel, response, "public Bazaarvoice API")
            if response.status_code in (401, 403):
                template, _ = await self.discover_bv_retry(source_url)
                continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("Errors"):
                raise RuntimeError(
                    f"{channel}: Bazaarvoice returned source errors"
                )
            if channel == "ego":
                from .ego_official import validate_review_payload
                validate_review_payload(payload)
            batch, total = template.parse(payload)
            observed_totals.append(int(total or 0))
            if (
                channel == "tractorsupply"
                and any(value != observed_totals[0] for value in observed_totals)
            ):
                raise TractorSupplyAccessError(
                    "tractorsupply: Bazaarvoice source total drift detected",
                    failure_class="total_drift_exceeded",
                    cooldown_seconds=0,
                )
            if (
                channel in OFFICIAL_STRICT_BV_CHANNELS
                and any(value != observed_totals[0] for value in observed_totals)
            ):
                raise OfficialSourceError(
                    f"{channel}: Bazaarvoice source total drift detected",
                    "source_total_drift",
                )
            if channel == "skil" and observed_totals:
                baseline = observed_totals[0]
                if abs(int(total or 0) - baseline) > 50:
                    raise RuntimeError(
                        f"skil: Bazaarvoice total drift exceeded bounded window ({baseline} -> {total})"
                    )
            if channel == "skil":
                for item in batch:
                    item_product_id = str(
                        item.get("ProductId") or item.get("productId") or ""
                    )
                    if skil_product_in_bare_tool_scope(
                        source_product_id, item_product_id
                    ):
                        accepted_product_ids.add(item_product_id)
                    else:
                        excluded_product_ids.add(item_product_id)
                mismatched = set()
            elif channel in {
                "milwaukee", "toro", "stihl", "husqvarna", "greenworks", "ego", "ryobi",
            }:
                mismatched = set()
                for item in batch:
                    item_product_id = str(
                        item.get("ProductId")
                        or item.get("productId")
                        or ""
                    )
                    if milwaukee_product_in_official_family_scope(
                        source_product_id,
                        item_product_id,
                        payload,
                    ):
                        accepted_product_ids.add(item_product_id)
                    else:
                        mismatched.add(item_product_id)
            else:
                mismatched = {
                    str(item.get("ProductId") or item.get("productId") or "")
                    for item in batch
                    if str(item.get("ProductId") or item.get("productId") or "")
                    != str(source_product_id)
                }
            if mismatched:
                error_class = (
                    TractorSupplyAccessError
                    if channel == "tractorsupply"
                    else RuntimeError
                )
                raise error_class(
                    f"{channel}: Bazaarvoice returned reviews for a "
                    "different product id",
                    **(
                        {
                            "failure_class": "product_identity_mismatch",
                            "cooldown_seconds": 0,
                        }
                        if channel == "tractorsupply"
                        else {}
                    ),
                )
            if channel == "tractorsupply" and not batch and len(records) < int(total or 0):
                raise TractorSupplyAccessError(
                    "tractorsupply: Bazaarvoice returned a premature empty page",
                    failure_class="pagination_window_missing",
                    cooldown_seconds=0,
                )
            if (
                channel in OFFICIAL_STRICT_BV_CHANNELS
                and not batch
                and len(records) < int(total or 0)
            ):
                raise OfficialSourceError(
                    f"{channel}: Bazaarvoice returned a premature empty page",
                    "premature_empty_page",
                )
            if not batch:
                break
            for raw in batch:
                rid = str(raw.get("Id", ""))
                if channel in OFFICIAL_STRICT_BV_CHANNELS and not rid:
                    raise OfficialSourceError(
                        f"{channel}: Bazaarvoice returned a review without an id",
                        "duplicate_review_identity",
                    )
                if channel in OFFICIAL_STRICT_BV_CHANNELS and rid in seen:
                    raise OfficialSourceError(
                        f"{channel}: Bazaarvoice repeated a review id across pages",
                        "duplicate_review_identity",
                    )
                if channel == "tractorsupply" and rid and rid in seen:
                    raise TractorSupplyAccessError(
                        "tractorsupply: Bazaarvoice repeated a review across pages",
                        failure_class="pagination_duplicate",
                        cooldown_seconds=0,
                    )
                if rid and rid not in seen:
                    seen.add(rid)
                    record = norm_record(channel, product, raw)
                    if channel == "skil":
                        item_product_id = str(
                            raw.get("ProductId") or raw.get("productId") or ""
                        )
                        if not skil_product_in_bare_tool_scope(
                            source_product_id, item_product_id
                        ):
                            record["is_eligible"] = False
                            record["exclusion_reason"] = (
                                "product_family_out_of_scope"
                            )
                    records.append(record)
            offset += PAGE_SIZE
            if job_id and self.store:
                self.store.update(job_id, f"{channel}: {len(records)}/{total} records")
            if offset >= total: break
            await asyncio.sleep(0.7 + random.random() * 0.4)
        if channel == "tractorsupply" and len(records) != int((observed_totals or [total])[0]):
            raise TractorSupplyAccessError(
                "tractorsupply: Bazaarvoice unique review count did not reconcile",
                failure_class="validation_mismatch",
                cooldown_seconds=0,
            )
        if channel == "skil" and total and not accepted_product_ids:
            raise RuntimeError(
                "skil: Bazaarvoice returned no review for the requested "
                "bare-tool family"
            )
        if channel in OFFICIAL_STRICT_BV_CHANNELS:
            validate_complete_pages(
                [
                    min(PAGE_SIZE, max(0, observed_totals[0] - page_offset))
                    for page_offset in range(0, len(records), PAGE_SIZE)
                ] if records else [0],
                observed_totals or [total],
            )
        notes = {
            "bazaarvoice": template.kind,
            "source_site": product.get("source_site", urlsplit(source_url).netloc),
            "source_product_id": source_product_id,
            "scope": (
                "SKIL bare-tool family; same family-prefix kit variants included "
                "and unrelated ProductIds retained as out-of-scope evidence"
                if channel == "skil"
                else (
                    "Official product family; a variant ProductId is included "
                    "only when public Bazaarvoice metadata places the variant "
                    "and requested PDP ProductId in a shared FamilyId"
                    if channel in {"milwaukee", "greenworks", "ego", "ryobi"}
                    else "official page product/family as configured by the source site's Bazaarvoice deployment"
                )
            ),
            **discovery_evidence,
        }
        if channel == "skil":
            notes["accepted_product_ids"] = sorted(accepted_product_ids)
            notes["excluded_product_ids"] = sorted(excluded_product_ids)
            notes["total_observed_min"] = min(observed_totals or [total])
            notes["total_observed_max"] = max(observed_totals or [total])
            notes["total_drift_bounded"] = True
            notes["empty_source_confirmed"] = bool(total == 0 and not records)
        elif channel in {
            "milwaukee", "toro", "stihl", "husqvarna", "greenworks", "ego", "ryobi",
        }:
            notes["accepted_product_ids"] = sorted(accepted_product_ids)
            notes["product_family_evidence"] = (
                "bazaarvoice_includes_products_shared_familyids_with_target_product"
            )
            notes["empty_source_confirmed"] = bool(
                total == 0 and not records
                and (channel != "ego" or product.get("total") == 0)
            )
            if channel in {"toro", "stihl", "husqvarna", "greenworks"}:
                notes["source_reconciled_strict"] = True
                notes["total_observed_min"] = min(observed_totals or [total])
                notes["total_observed_max"] = max(observed_totals or [total])
        elif channel == "tractorsupply":
            notes["source_reconciled_strict"] = True
            notes["total_observed_min"] = min(observed_totals or [total])
            notes["total_observed_max"] = max(observed_totals or [total])
            notes["empty_source_confirmed"] = bool(total == 0 and not records)
        elif channel in OFFICIAL_STRICT_BV_CHANNELS:
            notes["source_reconciled_strict"] = True
            notes["total_observed_min"] = min(observed_totals or [total])
            notes["total_observed_max"] = max(observed_totals or [total])
            notes["empty_source_confirmed"] = bool(total == 0 and not records)
        return records, total, notes

    def persist_and_validate(self, run_dir, channel, product, records, source_total, notes):
        source_unique = {r["review_id"]: r for r in records if r["review_id"]}
        source_rows = list(source_unique.values())
        rows = [r for r in source_rows if r.get("is_eligible", True)]
        raw_path, csv_path = run_dir / "reviews.raw.jsonl", run_dir / "reviews.csv"
        with raw_path.open("w", encoding="utf-8") as f:
            for row in source_rows:
                f.write(json.dumps(row["raw"], ensure_ascii=False) + "\n")
        schema_row = rows[0] if rows else (source_rows[0] if source_rows else None)
        fields = (
            [k for k in schema_row if k != "raw"]
            if schema_row
            else REVIEW_CSV_FIELDS
        )
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
            writer.writerows([{k:v for k,v in row.items() if k != "raw"} for row in rows])
        ratings = Counter(str(row["rating"]) for row in rows)
        source_reconciled = len(source_rows) == source_total and (
            source_total > 0 or notes.get("empty_source_confirmed") is True
        )
        bounded = notes.get("collection_schema_version") == "voxquarry.bounded-collection/v1"
        if bounded:
            source_reconciled = source_reconciled and notes.get("source_reconciled_strict") is True
        if source_reconciled:
            status = "COMPLETE_FALLBACK_SOURCE" if product.get("collection_method") == "manufacturer_syndication_fallback" else "COMPLETE"
        elif rows and (not bounded or len(source_rows) < source_total):
            status = "PARTIAL_SUCCESS"
        else:
            status = "INCOMPLETE_SOURCE_LIMIT"
        exclusions = Counter(
            row.get("exclusion_reason") or "unspecified"
            for row in source_rows
            if not row.get("is_eligible", True)
        )
        valid_text_reviews = sum(
            bool(row.get("is_valid_text_review")) for row in rows
        )
        short_text_reviews = sum(
            0 < int(row.get("review_word_count") or 0) < 5
            for row in rows
        )
        report = {"channel": channel, "status": status, "product": product, "source_total": source_total,
                  "source_unique_reviews": len(source_rows), "source_reconciled": source_reconciled,
                  "unique_reviews": len(rows), "eligible_reviews": len(rows),
                  "excluded_reviews": len(source_rows)-len(rows),
                  "excluded_ego_origin_reviews": exclusions.get("syndicated_from_ego_official", 0),
                  "exclusion_reason_histogram": exclusions,
                  "valid_text_reviews": valid_text_reviews,
                  "short_text_reviews": short_text_reviews,
                  "market_region": normalize_market_region(product.get("market_region")),
                  "duplicates_removed": len(records)-len(source_rows), "rating_histogram": ratings,
                  "ratings_only": sum(not str(r["review_text"]).strip() for r in rows), "notes": notes,
                  "artifacts": {"csv": f"{channel}/reviews.csv", "raw_jsonl": f"{channel}/reviews.raw.jsonl"}, "completed_at": now()}
        if "rating_total" in notes:
            report["source_rating_total"] = notes["rating_total"]
            # Some official sources distinguish aggregate star ratings from
            # written review records under a source-specific field name.
            # Preserve that distinction without making report generation depend
            # on every adapter using one exact internal key.
            report["aggregate_ratings_without_review_record"] = notes.get(
                "aggregate_ratings_without_review_record",
                notes.get("ratings_without_written_review", 0),
            )
        if notes.get("collection_schema_version") == "voxquarry.bounded-collection/v1":
            report.update(validation_schema_version="voxquarry.bounded-collection/v1",
                source_records_collected=len(source_rows), source_missing_records=source_total-len(source_rows),
                stop_reason=notes.get("stop_reason"), requested_sample_complete=notes.get("requested_sample_complete", False))
        self.write_json(run_dir / "validation_report.json", report)
        report["artifacts"]["report"] = f"{channel}/validation_report.json"
        return report
