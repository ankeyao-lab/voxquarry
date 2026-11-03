"""Public identity and completeness gates for official review providers.

The helpers in this module never authenticate, solve a challenge, attach a
cookie, or infer a review identity from a marketing model.  They consume only
the identity published by the official PDP or its public loader/API.
"""

from dataclasses import dataclass
import html
import re
from urllib.parse import parse_qs, urlsplit


CHALLENGE_MARKERS = (
    "access denied",
    "captcha",
    "just a moment",
    "unusual traffic",
    "verify you are human",
)


@dataclass(frozen=True)
class OfficialIdentity:
    product_id: str
    evidence: str


class OfficialSourceError(RuntimeError):
    def __init__(self, message, failure_class="source_contract_violation"):
        super().__init__(message)
        self.failure_class = failure_class


def guard_public_response(channel, response, purpose="official source"):
    """Classify public HTTP failures without trying another identity/access path."""
    status = int(getattr(response, "status_code", 0) or 0)
    if status in {401, 403}:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned access denied (HTTP {status})",
            "access_denied",
        )
    if status == 429:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned rate limited (HTTP 429)",
            "rate_limited",
        )
    if status == 412:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned an anti-bot precondition challenge (HTTP 412)",
            "anti_bot_challenge",
        )
    if status >= 500:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned upstream HTTP {status}",
            "upstream_5xx",
        )
    if status >= 400:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned HTTP {status}",
            "source_http_error",
        )
    body = str(getattr(response, "text", "") or "")
    folded = body.casefold()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_match.group(1)).casefold() if title_match else ""
    # Real PDP/API bodies can legitimately contain challenge-related words in
    # policy text, scripts or user reviews.  A challenge is classified only
    # from its document title or a small interstitial body, never a substring
    # anywhere in a full product response.
    looks_html = "<html" in folded or "<title" in folded
    challenge = any(marker in title for marker in CHALLENGE_MARKERS) or (
        looks_html
        and len(body.encode("utf-8")) < 50000
        and any(marker in folded for marker in CHALLENGE_MARKERS)
    )
    if challenge:
        raise OfficialSourceError(
            f"{channel}: {purpose} returned an anti-bot challenge",
            "anti_bot_challenge",
        )
    return response


def _unique_bv_marker(channel, source):
    values = {
        value.strip()
        for value in re.findall(
            r'data-bv-product-id=["\']([^"\']+)', str(source or ""), re.I
        )
        if value.strip()
    }
    if len(values) != 1:
        label = "ambiguous" if values else "missing"
        raise OfficialSourceError(
            f"{channel}: official PDP product identity was {label}",
            "product_identity_mismatch",
        )
    return next(iter(values))


def _greenworks_shopify_bv_identity(source):
    """Bind Greenworks' Bazaarvoice marker to the current Shopify product.

    Recommendation cards on a Greenworks PDP carry many Bazaarvoice product
    IDs, so uniqueness across every page marker is not a valid identity rule.
    The current Shopify resource ID must instead be present in the official
    PDP's Bazaarvoice ``data-bv-productid`` marker (the app uses the legacy
    attribute spelling without the second hyphen).
    """
    decoded = html.unescape(str(source or "")).replace("\\/", "/")
    shopify_ids = {
        value.strip()
        for value in re.findall(
            r'\bvar\s+meta\s*=\s*\{\s*"product"\s*:\s*\{\s*"id"\s*:\s*(\d+)',
            decoded,
            re.I,
        )
        if value.strip()
    }
    if len(shopify_ids) != 1:
        label = "ambiguous" if shopify_ids else "missing"
        raise OfficialSourceError(
            f"greenworks: current Shopify product identity was {label}",
            "product_identity_mismatch",
        )
    product_id = next(iter(shopify_ids))
    reviewed_ids = {
        value.strip()
        for value in re.findall(
            r'data-bv-productid=["\']([^"\']+)', decoded, re.I
        )
        if value.strip()
    }
    if product_id not in reviewed_ids:
        raise OfficialSourceError(
            "greenworks: Shopify product identity did not match the "
            "Bazaarvoice product marker",
            "product_identity_mismatch",
        )
    return OfficialIdentity(
        product_id,
        "shopify_product_id_bazaarvoice_marker",
    )


def _stihl_selected_variant(url, source):
    fragment = urlsplit(str(url or "")).fragment
    selected = parse_qs(fragment.split("?", 1)[1] if "?" in fragment else fragment).get("aID", [])
    if not selected:
        values = {
            value.strip()
            for value in re.findall(
                r'data-bv-product-id=["\']([^"\']+)',
                str(source or ""),
                re.I,
            )
            if value.strip()
        }
        if len(values) != 1:
            label = "ambiguous" if values else "missing"
            raise OfficialSourceError(
                f"stihl: current STIHL product identity was {label}",
                "product_identity_mismatch",
            )
        product_id = next(iter(values))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", product_id):
            raise OfficialSourceError(
                "stihl: current STIHL product identity was invalid",
                "product_identity_mismatch",
            )
        return OfficialIdentity(product_id, "pdp_current_product_bv_id")
    if len(selected) != 1 or not re.fullmatch(r"\d{8,15}", selected[0]):
        raise OfficialSourceError(
            "stihl: selected STIHL variant aID was missing or ambiguous",
            "product_identity_mismatch",
        )
    product_id = selected[0]
    normalized_source = str(source or "").replace("\\u003d", "=")
    # The PDP publishes every variant as a URL+SKU pair.  Require the exact
    # fragment aID and matching SKU; the server-rendered default marker may be
    # a different variant and is intentionally ignored.
    url_evidence = re.search(
        rf'["\'][^"\']*[?&]aID={re.escape(product_id)}["\']',
        normalized_source,
        re.I,
    )
    sku_evidence = re.search(
        rf'["\']sku["\']\s*:\s*["\']{re.escape(product_id)}["\']',
        normalized_source,
        re.I,
    )
    if not url_evidence or not sku_evidence:
        raise OfficialSourceError(
            "stihl: selected STIHL variant was not reconciled to PDP URL and SKU evidence",
            "product_identity_mismatch",
        )
    return OfficialIdentity(product_id, "pdp_selected_variant_aid")


def official_bv_identity(channel, url, source):
    if channel == "stihl":
        return _stihl_selected_variant(url, source)
    if channel == "greenworks":
        return _greenworks_shopify_bv_identity(source)
    if channel == "johndeere":
        decoded = html.unescape(str(source or ""))
        values = {
            value.strip()
            for value in re.findall(
                r'["\'](?:productId|baseCode)["\']\s*:\s*["\']([^"\']+)',
                decoded,
                re.I,
            )
            if value.strip()
        }
        # A Deere PDP may repeat the same baseCode in analytics, build and
        # Reviews components.  Repetition is safe; multiple distinct values
        # are not.
        if len(values) != 1:
            label = "ambiguous" if values else "missing"
            raise OfficialSourceError(
                f"johndeere: official PDP review identity was {label}",
                "product_identity_mismatch",
            )
        product_id = next(iter(values))
        return OfficialIdentity(product_id, "pdp_reviews_component_product_id")
    product_id = _unique_bv_marker(channel, source)
    evidence = {
        "husqvarna": "pdp_data_bv_product_id_not_article_sku",
        "toro": "pdp_data_bv_product_id",
        "johndeere": "pdp_public_review_product_id",
    }.get(channel, "pdp_data_bv_product_id")
    return OfficialIdentity(product_id, evidence)


def exmark_product_identity(url, model_payload):
    parts = [part for part in urlsplit(str(url or "")).path.split("/") if part]
    requested_model = parts[-1].upper() if parts else ""
    model = model_payload.get("model") if isinstance(model_payload, dict) else None
    if not isinstance(model, dict):
        raise OfficialSourceError(
            "exmark: official model API omitted product identity",
            "product_identity_missing",
        )
    returned_model = str(model.get("ModelNumber") or "").upper()
    if not requested_model or returned_model != requested_model:
        raise OfficialSourceError(
            "exmark: official model API returned a different model",
            "product_identity_mismatch",
        )
    page_id = str(model.get("PowerReviewId") or "").strip()
    if not re.fullmatch(r"\d+", page_id):
        raise OfficialSourceError(
            "exmark: official model API omitted PowerReviews page_id",
            "product_identity_missing",
        )
    return OfficialIdentity(page_id, "official_model_api_power_review_id")


def exmark_public_config(source):
    """Read the public widget configuration rendered by the official PDP."""
    source = str(source or "")
    fields = {
        "api_key": "powerReviewsPublicAPIKey",
        "merchant_id": "powerReviewsMerchantId",
        "merchant_group_id": "powerReviewsMerchantGroupId",
    }
    config = {}
    for output, marker in fields.items():
        values = {
            value.strip()
            for value in re.findall(
                rf'{marker}\s*:\s*["\']([^"\']+)', source, re.I
            )
            if value.strip()
        }
        if len(values) != 1:
            raise OfficialSourceError(
                f"exmark: official PDP public {marker} was missing or ambiguous",
                "product_identity_missing",
            )
        config[output] = next(iter(values))
    return config


def walmart_review_identity(item_id, product_payload):
    item = product_payload.get("item") if isinstance(product_payload, dict) else None
    if not isinstance(item, dict):
        raise OfficialSourceError(
            "walmart: official review page omitted product identity",
            "product_identity_missing",
        )
    canonical = str(item.get("canonicalUrl") or "")
    if not re.search(rf"/{re.escape(str(item_id))}(?:[/?#]|$)", canonical):
        raise OfficialSourceError(
            "walmart: official review page returned a different item",
            "product_identity_mismatch",
        )
    lookup_id = str(item.get("lookupId") or "").strip()
    if not lookup_id:
        raise OfficialSourceError(
            "walmart: official review page omitted lookupId",
            "product_identity_missing",
        )
    return OfficialIdentity(lookup_id, "official_review_page_lookup_id")


def validate_complete_pages(page_lengths, observed_totals):
    """Require one stable total and exactly that many records across pages."""
    if not observed_totals:
        raise OfficialSourceError("official source total was missing", "source_total_missing")
    totals = {int(value) for value in observed_totals}
    if len(totals) != 1:
        raise OfficialSourceError("official source total drift detected", "source_total_drift")
    total = next(iter(totals))
    collected = 0
    for index, length in enumerate(page_lengths):
        length = int(length)
        if length == 0 and collected < total:
            raise OfficialSourceError(
                f"official source returned premature empty page {index + 1}",
                "premature_empty_page",
            )
        collected += length
    if collected != total:
        raise OfficialSourceError(
            f"official source incomplete pagination: {collected}/{total}",
            "source_incomplete",
        )
    return total
