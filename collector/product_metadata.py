"""Shared product-metadata normalization for every task creation pipeline."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .category_vocabulary import (
    canonical_category_name,
    reclassify_expansion_category,
    reclassify_power_cleaner_category,
)
from .product_form import product_form, family_category
from .product_identity import extract_voltage, normalize_energy_type, normalize_voltage


PRODUCT_METADATA_RULE_VERSION = "voxquarry.product-metadata-resolution/v2"
ALLOWED_ENERGY_TYPES = {"corded", "battery", "fuel", "other"}
ALLOWED_MARKET_REGIONS = {"North America", "Europe", "Australia", "Other"}
CANONICAL_BRAND_DISPLAY = {
    "ego": "EGO",
}
from .official_product_types import PASSIVE_TYPES

NON_POWERED_CATEGORIES = {
    *PASSIVE_TYPES,
    "Vacuum Accessory", "OPE Accessory", "Pressure Washer Accessory",
    "Outdoor Cleaner", "Cooler", "Hose Reel", "Patio Cleaner",
    "Push Sweeper", "Seeder", "Door Hardware",
}
CATEGORY_ENERGY_DEFAULTS = {
    "Battery Pack": "battery",
    "Charger": "corded",
    "Cordless Handheld Vacuum": "battery",
    "Cordless Stick Vacuum": "battery",
    "Light Bulb": "corded",
    "Upright Vacuum": "corded",
    "Generator": "fuel",
    **{category: "other" for category in NON_POWERED_CATEGORIES},
}
LEGACY_PRESSURE_WASHER_ENERGY = {
    "gas pressure washer": "fuel",
    "electric pressure washer": "corded",
    "cordless pressure washer": "battery",
    "power washer": "unknown",
    "高压清洗机/洗车机": "unknown",
}


def _clean(value):
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if not cleaned or cleaned.casefold() in {
        "unknown", "unknown category", "none", "null", "未知", "待补充", "待确认",
    }:
        return None
    return cleaned


def normalize_model(value):
    """Keep source model numbers and reject retailer-title slugs."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    cleaned = re.sub(
        r"^(?:(?:mfr\.?|manufacturer)\s+)?model\s*"
        r"(?:number|no\.?|#)\s*[:#-]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        return None
    if len(cleaned) > 80:
        return None
    words = re.findall(r"[A-Za-z0-9]+", cleaned)
    if len(words) > 8 or (cleaned.count("-") >= 6 and len(words) >= 7):
        return None
    return cleaned


def normalize_brand(value):
    """Reject energy/category/channel labels accidentally stored as brands."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    if cleaned.casefold() in {
        "battery", "corded", "cordless", "electric", "fuel", "gas",
        "other", "power cleaner", "power washer", "pressure washer",
        "amazon", "home depot", "lowe's", "lowes", "walmart",
    }:
        return None
    return CANONICAL_BRAND_DISPLAY.get(cleaned.casefold(), cleaned)


def infer_market_region(url):
    # Some global .com storefronts expose market through /uk/, /en-us/, etc.
    # Keep this import lazy: the pure page parser reuses normalization helpers.
    from .source_product_metadata import CHANNEL_METADATA_SOURCES, infer_source_market_region
    host = urlsplit(str(url or "")).netloc.casefold().removeprefix("www.")
    for channel, policy in CHANNEL_METADATA_SOURCES.items():
        if any(host == domain or host.endswith('.' + domain) for domain in policy['hosts']):
            return infer_source_market_region(channel, url) or 'Other'
    if host.endswith((".com.au", ".au")):
        return "Australia"
    if host.endswith((
        ".co.uk", ".uk", ".eu", ".de", ".fr", ".it", ".es", ".nl",
        ".se", ".pl",
    )):
        return "Europe"
    if host.endswith((".com", ".ca", ".us")):
        return "North America"
    return "Other"


def canonicalize_pressure_washer_category(category):
    raw = _clean(category) or "Unknown Category"
    legacy_energy = LEGACY_PRESSURE_WASHER_ENERGY.get(raw.casefold())
    if legacy_energy is not None:
        return "Pressure Washer", legacy_energy
    return canonical_category_name(raw), None


def _portable_power_cleaner(category, target_url, model, title):
    resolved = reclassify_power_cleaner_category(
        category, target_url, model=model, product_title=title,
    )
    if resolved != "Pressure Washer":
        return resolved
    text = unquote(" ".join(str(value or "") for value in (target_url, title))).casefold()
    text = text.replace("-", " ").replace("_", " ")
    portable = bool(re.search(r"\b(?:portable|handheld|hand-held|spray gun)\b", text))
    cordless = bool(re.search(r"\b(?:cordless|battery(?:[- ]powered)?)\b", text))
    washer = bool(re.search(r"\b(?:pressure|power) washer\b", text))
    return "Power Cleaner" if portable and cordless and washer else resolved


def _correct_power_cleaner_category(category, target_url, model, title, energy_type):
    """Undo marketing-name collisions without collapsing portable cleaners."""
    canonical = canonical_category_name(category)
    if canonical != "Power Cleaner":
        return canonical
    text = unquote(" ".join(str(value or "") for value in (
        target_url, model, title,
    ))).casefold().replace("-", " ").replace("_", " ")
    compact = "".join(character for character in text if character.isalnum())
    reviewed_portable = any(marker in compact for marker in (
        "dcpw550", "dcpw1000", "ry120350", "ry121850", "ry124052",
        "ry18pw22", "ry18pwx41", "hypresso18241", "4140135", "ezclean",
        "cordlesspowerwashers",
    ))
    portable = bool(re.search(r"\b(?:portable|handheld|hand held|spray gun)\b", text))
    cordless = bool(re.search(r"\b(?:cordless|battery|power cell)\b", text))
    if reviewed_portable or (portable and cordless):
        return "Power Cleaner"
    if energy_type in {"fuel", "corded"} and re.search(
        r"\b(?:pressure|power) washer\b", text,
    ):
        return "Pressure Washer"
    return canonical


def infer_energy_type(*, category, explicit=None, text_values=()):
    """Resolve energy from explicit source evidence with hybrid precedence."""
    canonical_category, category_energy = canonicalize_pressure_washer_category(category)
    text = unquote(" ".join(str(value or "") for value in text_values)).casefold()
    text = text.replace("-", " ").replace("_", " ")
    hybrid = bool(re.search(r"\bhybrid\b", text)) and bool(
        re.search(r"\b(?:ac|corded|120v|mains)\b", text)
    ) and bool(re.search(r"\b(?:dc|battery|cordless|\d+\s*v)\b", text))
    greenworks_pressure_washer_hybrid = (
        canonical_category == "Pressure Washer"
        and bool(re.search(r"\bgreenworks\b", text))
        and bool(re.search(r"\bhybrid\b|\bpw(?:18|3000)hyb\b", text))
    )
    if hybrid or greenworks_pressure_washer_hybrid or re.search(
        r"\bac\s*[/&+-]\s*dc\b|\bdual[- ]source\b|\bliac\b", text,
    ):
        return "other"
    if re.search(
        r"\b(?:pcl691b|dcr025|dcr018|hybrid compressor pressito|"
        r"battery backup bulb)\b",
        text,
    ):
        return "other"
    normalized_explicit = normalize_energy_type(explicit)
    if normalized_explicit != "unknown":
        return normalized_explicit
    if category_energy and category_energy != "unknown":
        return category_energy
    if canonical_category in NON_POWERED_CATEGORIES:
        return "other"
    if re.search(r"\b(?:solar powered|solar flag pole|manual|hand operated)\b", text):
        return "other"
    if re.search(
        r"\b(?:gas|gasoline|petrol|diesel|propane|natural gas|"
        r"\d+\s*cc|engine powered|gas powered)\b",
        text,
    ):
        return "fuel"
    battery_platform = re.search(
        r"\b(?:m12|m18|mx fuel|one\+|power\+|pwr\s*core|pwrcore|"
        r"flexvolt|power x change|power x-change|egopowerplus)\b",
        text,
    )
    brand_family_battery = (
        bool(re.search(r"\bego\b", text))
        and bool(re.search(
            r"\b(?:lm|lb|st|tr|zt|snt|fn|lt)\d{3,4}[a-z0-9-]*\b",
            text,
        ))
    ) or (
        bool(re.search(r"\beinhell\b", text))
        and bool(re.search(r"\b(?:akku|li solo|li-solo|cordless)\b", text))
    ) or bool(re.search(
        r"\b(?:usb lithium|ap system|automower|mp100dz)\b", text,
    ))
    low_voltage = re.search(
        r"(?<![a-z0-9.])(?:4|12|18|20|21|24|36|40|48|56|60|72|80)"
        r"\s*[- ]?\s*(?:v|volt|volts)\b",
        text,
    )
    if re.search(r"\bdual power\b", text) and (
        battery_platform
        or re.search(r"\b(?:cordless|battery|dc powered)\b", text)
    ):
        return "other"
    if brand_family_battery or re.search(
        r"\b(?:cordless|battery(?:[- ]powered)?|power cell|dc powered)\b", text,
    ):
        return "battery"
    if battery_platform or (
        low_voltage
        and canonical_category not in {"Charger", "Light Bulb"}
    ):
        return "battery"
    if re.search(
        r"\b(?:corded|power cord|ac powered|electric powered|mains powered|"
        r"direct drive|plug in|electric|\d+(?:\.\d+)?\s*"
        r"(?:a|amp|amps|w|watt|watts))\b",
        text,
    ):
        return "corded"
    if canonical_category in {
        "Riding Lawnmower", "Walk behind Lawnmower", "Snow Blower",
        "Mulcher", "Shredder", "Tiller/Cultivator", "Log Splitter",
        "Leaf Blower/Vacuum",
    } and re.search(r"\b(?:\d+(?:\.\d+)?\s*hp|v twin|kohler engine)\b", text):
        return "fuel"
    if re.search(r"\b(?:tow|pull behind|weed puller|brush grubber)\b", text):
        return "other"
    if canonical_category == "Log Splitter" and "hydraulic log splitter" in text:
        return "other"
    if re.search(
        r"\b(?:150bt|power clear 721|power clear 821|big buddy portable heater|"
        r"fire sense .*patio heater|earthquake versa|200905)\b",
        text,
    ) or "exmark.com/mowers/zero turn/" in text:
        return "fuel"
    if canonical_category == "Log Splitter" and "40 ton log splitter" in text:
        return "fuel"
    if canonical_category == "Pool Vacuum" and "robotic pool vacuum" in text:
        return "corded"
    if re.search(r"\b(?:ac portable work light|touch lamp with)\b", text):
        return "corded"
    if canonical_category == "Sprayer" and re.search(
        r"\b(?:pump backpack sprayer|no leak pump|hand pump|chemical sprayer)\b", text,
    ):
        return "other"
    if canonical_category == "Sprayer":
        return "other"
    if canonical_category in {"Raker", "Snow Shovel", "Weed Remover"}:
        return "other"
    if canonical_category == "Pool Vacuum" and re.search(
        r"\b(?:auto pool cleaner|automatic pool cleaner)\b", text,
    ):
        return "other"
    if canonical_category in {
        "Air Compressor", "Fan", "Misting Fan", "LED Lighting", "Pump",
    }:
        return "corded"
    if canonical_category in {
        "Ash Vacuum", "Hard Floor Cleaner", "Spot Cleaner", "Upright Vacuum",
    }:
        return "corded"
    if canonical_category == "Wet/Dry Vacuum" and re.search(
        r"\b(?:peak hp|shop vac|wet dry vacuum|wet/dry vacuum)\b", text,
    ):
        return "corded"
    if canonical_category == "Pressure Washer" and re.search(
        r"\belectric (?:pressure|power) washer\b", text
    ):
        return "corded"
    default = CATEGORY_ENERGY_DEFAULTS.get(canonical_category)
    if default:
        return default
    return "unknown"


def normalize_product_metadata(
    *, target_url, category, brand=None, model=None, market_region=None,
    energy_type=None, voltage=None, product_title=None, product_form_factor=None,
    product_form_evidence_source=None, product_form_source_category=None,
):
    """Return one canonical product projection without network access."""
    canonical_category, _legacy_energy = canonicalize_pressure_washer_category(category)
    clean_model = normalize_model(model)
    canonical_category = reclassify_expansion_category(
        canonical_category, target_url, clean_model, product_title,
    )
    canonical_category = _portable_power_cleaner(
        canonical_category, target_url, clean_model, product_title,
    )
    evidence_text = (target_url, product_title, category, brand, model, voltage)
    canonical_energy = infer_energy_type(
        category=canonical_category,
        explicit=energy_type,
        text_values=evidence_text,
    )
    canonical_category = _correct_power_cleaner_category(
        canonical_category, target_url, clean_model, product_title,
        canonical_energy,
    )
    # A dedicated source field (including a reviewed composite platform such as
    # 12V/20V) outranks incidental charger/output/system voltages in titles.
    normalized_voltage = normalize_voltage(voltage) or extract_voltage(
        product_title, target_url, model,
    )
    if canonical_energy in {'battery', 'other'}:
        # An explicit battery arrangement is not a nominal/max conversion.
        # Only the current product title is in scope, not compatibility text,
        # recommendations, reviews, or an unrelated charger's input voltage.
        title = str(product_title or '')
        cells = [normalize_voltage(f'{m}V') for m in re.findall(
            r'(?i)(?<![\w.])2\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:v|volts?)\b', title)]
        cells += [normalize_voltage(f'{m}V') for m in re.findall(
            r'(?i)(?<![\w.])(\d+(?:\.\d+)?)\s*(?:v|volts?)\s*[x×]\s*2\b', title)]
        cells = {v for v in cells if v}
        if len(cells) == 1 and not re.search(r'(?i)compatible|replacement|charger\s+for', title):
            normalized_voltage = cells.pop()
    if canonical_energy == "battery" and not normalized_voltage:
        searchable = unquote(" ".join(str(value or "") for value in (
            target_url, product_title, model,
        ))).casefold().replace("_", " ")
        brand_key = (normalize_brand(brand) or "").casefold()
        candidates = set()
        if re.search(r"\bm12\b", searchable):
            candidates.add("12V")
        if re.search(r"\bm18\b", searchable):
            candidates.add("18V")
        if re.search(r"\bone\+\b|\bone plus\b", searchable):
            candidates.add("18V")
        if re.search(r"\bflexvolt\b", searchable):
            candidates.add("60V")
        if brand_key == "ego" and (
            "egopowerplus.com" in searchable
            or "egopowerplus.co.uk" in searchable
            or re.search(r"\bpower\+\b", searchable)
            or re.search(
                r"\b(?:hpw|lm|lb|st|tr|zt|snt|fn|lt)\d{3,4}[a-z0-9-]*\b",
                searchable,
            )
        ):
            candidates.add("56V")
        if brand_key == "einhell" and (
            re.search(r"\bpower x[- ]change\b", searchable)
            or re.search(
                r"\b(?:ge|te|tp|tc|gc|ce|ar|picobella|pressito|hypresso)"
                r"[- ]?[a-z]*\s*18(?:[/\s-]|$)",
                searchable,
            )
        ):
            candidates.add("18V")
        if brand_key == "einhell" and re.search(
            r"\b(?:brillianto|cleanexxo)\b", searchable,
        ):
            candidates.add("18V")
        if brand_key == "craftsman" and re.search(r"\bv20\b", searchable):
            candidates.add("20V")
        if brand_key == "ryobi" and re.search(r"\busb lithium\b", searchable):
            candidates.add("4V")
        if brand_key == "ryobi" and re.search(r"\bop40\d+", searchable):
            candidates.add("40V")
        if brand_key == "milwaukee" and re.search(r"\b48 11 18\d{2}[a-z]?\b", searchable):
            candidates.add("18V")
        if brand_key == "milwaukee" and re.search(
            r"\b48[- ]11[- ]18\d{2}[a-z]?\b", searchable,
        ):
            candidates.add("18V")
        if brand_key == "milwaukee" and re.search(r"\bmx fuel\b", searchable):
            candidates.add("72V")
        if brand_key == "skil" and re.search(r"\bpwrcore?\s*40\b", searchable):
            candidates.add("40V")
        if brand_key == "stihl" and "ap system" in searchable:
            candidates.add("36V")
        if brand_key == "makita" and "mp100dz" in searchable:
            candidates.add("12V")
        if brand_key == "ego" and "nexus" in searchable:
            candidates.add("56V")
        if len(candidates) == 1:
            normalized_voltage = candidates.pop()
    inferred_region = infer_market_region(target_url)
    supplied_region = _clean(market_region)
    region = inferred_region if inferred_region != "Other" else supplied_region or "Other"
    form = product_form(category, product_form_factor, evidence_source=product_form_evidence_source, source_category=product_form_source_category)
    canonical_category = family_category(canonical_category)
    return {
        "product_form": form,
        "product_form_factor": form["value"],
        "rule_version": PRODUCT_METADATA_RULE_VERSION,
        "brand": normalize_brand(brand),
        "model": clean_model,
        "category": canonical_category,
        "market_region": region,
        "energy_type": canonical_energy,
        "voltage": normalized_voltage,
        "product_title": _clean(product_title),
    }


def require_governed_metadata(metadata):
    """Fail closed for every newly created concrete product task.

    Model numbers remain nullable because some exact PDPs do not publish a
    manufacturer model.  Category and energy are product facts and may not use
    an unknown placeholder; battery products additionally need the brand and
    source-proven voltage that form the voltage-platform key.
    """
    value = dict(metadata or {})
    category = _clean(value.get("category"))
    if not category or category == "Unknown Category":
        raise ValueError("product task requires a canonical category before task creation")
    energy_type = value.get("energy_type")
    if energy_type not in ALLOWED_ENERGY_TYPES:
        raise ValueError(
            f"{category} requires source-proven energy_type before task creation"
        )
    market_region = value.get("market_region")
    if market_region is not None and market_region not in ALLOWED_MARKET_REGIONS:
        raise ValueError("product task has an unsupported market_region")
    if energy_type == "battery":
        if not value.get("voltage"):
            raise ValueError(
                f"{category} battery task requires source-proven voltage"
            )
        if not value.get("brand"):
            raise ValueError(
                f"{category} battery task requires source-proven brand"
            )
    return value
