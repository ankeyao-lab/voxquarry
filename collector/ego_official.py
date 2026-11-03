"""Bind EGO's public review catalog to its primary manufacturer SKU.

Recommendation cards are never product identity. A missing review widget is
not evidence of zero reviews; the official deployment's catalog must contain
the exact primary SKU before its public review API can be used.
"""

import html
import re
from urllib.parse import urlsplit

from .official_sources import OfficialSourceError


def is_us_product_url(url):
    return urlsplit(str(url)).hostname in {"egopowerplus.com", "www.egopowerplus.com"}


def _url_key(url):
    parts = urlsplit(str(url))
    return parts.netloc.casefold().removeprefix("www."), parts.path.rstrip("/").casefold()


def primary_product(url, source):
    if not is_us_product_url(url):
        raise OfficialSourceError("ego: unsupported primary product host", "product_identity_mismatch")
    source = str(source or "")
    skus = {
        html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        for value in re.findall(
            r'<span\b[^>]*class=["\'][^"\']*\bproduct-meta___sku-details\b[^"\']*["\'][^>]*>(.*?)</span>',
            source, re.I | re.S,
        )
    } - {""}
    if len(skus) != 1:
        label = "missing" if not skus else "ambiguous"
        raise OfficialSourceError(f"ego: primary manufacturer SKU was {label}", "product_identity_missing")
    sku = next(iter(skus))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,79}", sku):
        raise OfficialSourceError("ego: primary manufacturer SKU was invalid", "product_identity_mismatch")
    bare = set()
    for tag in re.findall(r'<[^>]+class=["\'][^"\']*ego-product-gallery__main[^"\']*["\'][^>]*>', source, re.I):
        match = re.search(r'data-bare-tool-sku=["\']([^"\']+)', tag, re.I)
        if match:
            bare.add(html.unescape(match.group(1)).strip())
    if len(bare) > 1:
        raise OfficialSourceError("ego: primary bare-tool SKU was ambiguous", "product_identity_mismatch")
    markers = {
        html.unescape(value).strip().casefold(): html.unescape(value).strip()
        for value in re.findall(r'data-bv-product-id=["\']([^"\']+)', source, re.I)
    }
    marker = next((markers[v.casefold()] for v in [sku, *sorted(bare)] if v.casefold() in markers), None)
    title = re.search(r"<h1\b[^>]*>(.*?)</h1>", source, re.I | re.S)
    title = html.unescape(re.sub(r"<[^>]+>", " ", title.group(1))).strip() if title else ""
    if not title:
        raise OfficialSourceError("ego: primary product title was missing", "product_identity_missing")
    return {"url": url, "sku": sku, "bare_sku": next(iter(bare), None), "title": title, "marker_id": marker}


def verify_catalog_product(primary, payload):
    if payload.get("Errors") or payload.get("HasErrors"):
        raise OfficialSourceError("ego: official product catalog returned source errors")
    results = payload.get("Results")
    if not isinstance(results, list) or not results:
        raise OfficialSourceError("ego: primary SKU was not present in official review catalog", "product_identity_missing")
    if len(results) != 1 or str(results[0].get("Id", "")).casefold() != primary["sku"].casefold():
        raise OfficialSourceError("ego: catalog product did not match primary SKU", "product_identity_mismatch")
    product = results[0]
    name = str(product.get("Name") or "").strip()
    page_url = product.get("ProductPageUrl")
    url_check = primary.get("catalog_url_verification") or {}
    if page_url and _url_key(page_url) != _url_key(primary["url"]):
        same_check = url_check.get("url") == page_url
        redirected = (same_check and url_check.get("status") == 200
                      and _url_key(url_check.get("final_url")) == _url_key(primary["url"]))
        # A dead historical URL does not invalidate an exact manufacturer SKU
        # and an exact product name. A live different page still fails closed.
        name_key = lambda value: "".join(re.findall(r"[a-z0-9]+", str(value).casefold()))
        obsolete = (same_check and url_check.get("status") == 404
                    and name_key(name) == name_key(primary["title"]))
        if not redirected and not obsolete:
            raise OfficialSourceError("ego: catalog product URL did not match primary page", "product_identity_mismatch")
    # A manufacturer SKU is unique within this verified EGO deployment. Some
    # genuine accessory catalog records omit Name and ProductPageUrl. The
    # primary manufacturer SKU and exact official catalog ID establish their
    # identity; retain display metadata gaps rather than guessing their names.
    return {"product_id": str(product["Id"]), "manufacturer_sku": primary["sku"],
            "catalog_name": name or None, "catalog_product_url": page_url,
            "catalog_metadata_gaps": [key for key, value in
                                      (("Name", name), ("ProductPageUrl", page_url)) if not value],
            "catalog_url_status": url_check.get("status"),
            "method": "official_primary_sku_exact_catalog_record"}


def validate_review_payload(payload):
    if payload.get("Errors") or payload.get("HasErrors"):
        raise OfficialSourceError("ego: Bazaarvoice returned source errors")
    total = payload.get("TotalResults")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise OfficialSourceError("ego: official review total was missing or invalid", "source_total_missing")
    rows = payload.get("Results")
    if not isinstance(rows, list) or (total == 0 and rows):
        raise OfficialSourceError("ego: official review result was invalid", "source_incomplete")
