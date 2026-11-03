"""Versioned, presentation-only brand assets.

Only verified official domains are listed.  Missing brands deliberately fall back
to a text wordmark; the reporting service never invents a trademark.
"""

from __future__ import annotations

from typing import Any


BRAND_ASSET_REGISTRY_VERSION = "brand-assets/1.1.0"

_OFFICIAL_DOMAINS = {
    "ego": "egopowerplus.com",
    "ryobi": "ryobitools.com",
    "skil": "skil.com",
    "dewalt": "dewalt.com",
    "craftsman": "craftsman.com",
    "greenworks": "greenworkstools.com",
    "milwaukee": "milwaukeetool.com",
    "husqvarna": "husqvarna.com",
    "toro": "toro.com",
    "echo": "echo-usa.com",
    "worx": "worx.com",
    "kobalt": "lowes.com/kobalt",
    "einhell": "einhell.com",
    "sun joe": "snowjoe.com",
    "snow joe": "snowjoe.com",
    "black+decker": "blackanddecker.com",
    "black & decker": "blackanddecker.com",
    "makita": "makitatools.com",
    "stihl": "stihlusa.com",
    "westinghouse": "westinghouseoutdoorpower.com",
    "yardmax": "yardmax.com",
    "murray": "murray.com",
    "flex": "flexpowertools.com",
    "porter-cable": "portercable.com",
    "ridgid": "ridgid.com",
    "shopvac": "shopvac.com",
    "vacmaster": "vacmaster.com",
    "vacmaster pro": "vacmaster.com",
}

# A few official sites do not expose a usable favicon at the domain root.
# These URLs come from the official page's own brand/icon metadata. They are
# presentation-only: a failed image always falls back to the verified brand
# name and never affects report generation.
_OFFICIAL_LOGOS = {
    # These public references are presentation-only registry entries.
    # They remain remote official-site/CDN references: they are not copied into
    # Git and do not claim a governed brand identity or an asset licence.
    "craftsman": "https://www.craftsman.com/favicons/favicon-craftsman.png",
    "dewalt": "https://www.dewalt.com/favicons/favicon-dewalt.png",
    "einhell": "https://www.einhell.com/_assets/369fecbaddba51f6af024f761a766400/Assets/img/logo.png",
    "flex": "https://www.flexpowertools.com/media/favicon/websites/23/favicon-32x32.png",
    "greenworks": "https://www.greenworkstools.com/cdn/shop/files/greenworks_favicon_refactored.svg?crop=center&height=96&width=96",
    "milwaukee": "https://www.milwaukeetool.com/img/Foundation/Theming/MilwaukeeTool/global/favicon-192.png",
    "porter-cable": "https://www.portercable.com/sites/g/files/xnuzat3261/files/porter-cable-logo%402x.png",
    "ridgid": "https://www.ridgid.com/favicon.ico",
    "ryobi": "https://cdn.shopify.com/oxygen-v2/34633/24758/51400/4157011/assets/favicon-BiIQjobQ.ico",
    "shopvac": "https://cdn.shopify.com/s/files/1/0565/9643/1025/files/2022_ShopVac_Favicon_128x128_57afd1fa-c3a1-48a3-a4e8-7424baa618f2_180x.png?v=1654104679",
    "vacmaster": "https://cdn11.bigcommerce.com/s-ypbqzwo9/product_images/favicon.png?t=1648758418",
    "vacmaster pro": "https://cdn11.bigcommerce.com/s-ypbqzwo9/product_images/favicon.png?t=1648758418",
}


def brand_asset(brand: str) -> dict[str, Any]:
    """Return a real official-domain asset or an explicit text fallback."""

    label = " ".join(str(brand or "").split()).strip()
    domain = _OFFICIAL_DOMAINS.get(label.casefold())
    if not domain:
        return {
            "asset_version": BRAND_ASSET_REGISTRY_VERSION,
            "logo_url": "",
            "logo_source": "text_wordmark_fallback",
            "official_website": "",
        }
    website = f"https://{domain}"
    return {
        "asset_version": BRAND_ASSET_REGISTRY_VERSION,
        "logo_url": _OFFICIAL_LOGOS.get(label.casefold()) or f"{website}/favicon.ico",
        "logo_source": (
            "official_page_brand_asset"
            if label.casefold() in _OFFICIAL_LOGOS
            else "official_domain_favicon"
        ),
        "official_website": website,
    }
