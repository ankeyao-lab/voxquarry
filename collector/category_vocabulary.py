"""Canonical English category vocabulary for collection task metadata."""

from __future__ import annotations


MASTER_CATEGORY_NAMES = (
    "Battery Pack",
    "Charger",
    "Power Station/Inverter",
    "LED Lighting",
    "Light Bulb",
    "Radio/Speaker",
    "Fan",
    "Misting Fan",
    "Heater",
    "Generator",
    "Inflator",
    "Air Compressor",
    "Hard Floor Cleaner",
    "Cordless Stick Vacuum",
    "Handheld Vacuum",
    "Upright Vacuum",
    "Wet/Dry Vacuum",
    "Spot Cleaner",
    "Window Vacuum",
    "Pool Vacuum",
    "Ash Vacuum",
    "Vacuum Accessory",
    "OPE Accessory",
    "Pressure Washer Accessory",
    "OPE Combo Kit",
    "Outdoor Cleaner",
    "Bug Zapper",
    "Door Hardware",
    "Drill",
    "Mixer",
    "Impact Wrench",
    "Power Scrubber",
    "Push Sweeper",
    "Chain Saw",
    "Edger",
    "Grass Shrub/Shear",
    "Hedge Trimmer",
    "Leaf Blower/Vacuum",
    "Multi Head",
    "Power Cleaner",
    "Pressure Washer",
    "String Trimmer",
    "Auger",
    "Cooler",
    "Cultivator",
    "Log Splitter",
    "Mulcher",
    "Patio Cleaner",
    "Power Unit",
    "Pruner",
    "Pump",
    "Raker",
    "Scarifier",
    "Seeder",
    "Shredder",
    "Snow Shovel",
    "Sprayer",
    "Spreader",
    "Weed Remover",
    "Unknown Category",
    "Riding Lawnmower",
    "Robot Mower",
    "Snow Blower",
    "Tiller/Cultivator",
    "Walk behind Lawnmower",
    "Wheeled Blower",
)

# Existing collection records used a smaller, product-oriented vocabulary.
# Keep those values as aliases so the migration is deterministic and historical
# URLs resolve to the user-maintained OPE master taxonomy.
LEGACY_CATEGORY_NAMES = {
    "LED 灯具": "LED Lighting",
    "便携式电源": "Power Station/Inverter",
    "Portable Power Station": "Power Station/Inverter",
    "充气泵": "Inflator",
    "Inflator/Compressor": "Inflator",
    "Inflators and Compressors": "Inflator",
    "割草机": "Walk behind Lawnmower",
    "Walk-Behind Mower": "Walk behind Lawnmower",
    "Lawn Mower": "Walk behind Lawnmower",
    "吹风机": "Leaf Blower/Vacuum",
    "Leaf Blower": "Leaf Blower/Vacuum",
    "多功能系统": "Multi Head",
    "Multi-Head System": "Multi Head",
    "打草机": "String Trimmer",
    "扫雪机": "Snow Blower",
    "扫雪铲": "Snow Shovel",
    "无线庭院清洁器": "Patio Cleaner",
    "Cordless Outdoor Cleaner": "Patio Cleaner",
    "无线手持吸尘器": "Handheld Vacuum",
    "Cordless Handheld Vacuum": "Handheld Vacuum",
    "杆锯": "Pruner",
    "Pole Saw": "Pruner",
    "湿干两用吸尘器": "Wet/Dry Vacuum",
    "生活方式": "Unknown Category",
    "Lifestyle Tools": "Unknown Category",
    "直立吸尘器": "Upright Vacuum",
    "绿篱机": "Hedge Trimmer",
    "链锯": "Chain Saw",
    "Chainsaw": "Chain Saw",
    "风扇": "Fan",
    "Jobsite Fan": "Fan",
    "骑乘割草机": "Riding Lawnmower",
    "Riding Mower": "Riding Lawnmower",
    "Cordless Power Washer": "Power Cleaner",
    "高压清洗机": "Pressure Washer",
    "Non-Mowing Equipment": "Unknown Category",
}

DEFAULT_CATEGORY_NAMES = MASTER_CATEGORY_NAMES


POWER_CLEANER_IDENTITY_RULE_VERSION = "power-cleaner-identity/1.0.0"

# Retail names are not globally consistent: North American PDPs use "Power
# Cleaner", RYOBI Europe uses "Cordless Power Washer", and Einhell calls its
# compact 24-bar gun a "Cordless Pressure Washer".  These reviewed product
# identities describe the portable gun-style family without treating the
# ambiguous phrase "power washer" or the word "cordless" as sufficient proof.
POWER_CLEANER_COMPACT_IDENTITY_MARKERS = (
    "dcpw550",
    "dcpw1000",
    "ry120350",
    "ry121850",
    "ry124052",
    "ry18pw22",
    "ry18pwx41",
    "hypresso18241",
    "4140135",
    "ezclean",
    "cordlesspowerwashers",
)


def reclassify_power_cleaner_category(
    current_category: object,
    target_url: object,
    model: object = "",
    product_title: object = "",
) -> str:
    """Split reviewed portable Power Cleaners from Pressure Washer tasks.

    The current category is a fail-closed guard.  This function never moves a
    task from another category and never relies on review text, PSI alone, the
    word ``cleaner`` alone, or the broad phrase ``power washer``.
    """

    current = canonical_category_name(current_category)
    if current != "Pressure Washer":
        return current
    text = " ".join(
        str(value or "").casefold()
        for value in (target_url, model, product_title)
    )
    compact = "".join(character for character in text if character.isalnum())
    if any(marker in compact for marker in POWER_CLEANER_COMPACT_IDENTITY_MARKERS):
        return "Power Cleaner"
    return current


# E24 expansion research uses several regional merchandising labels for the
# same product family.  These rules recover a stable category for legacy
# ``Unknown Category`` tasks without using the brand or review status as a
# proxy.  More specific accessory, bulb and form-factor rules must precede
# broader vacuum/lighting markers.
UNKNOWN_CATEGORY_RULES = (
    (
        "Pressure Washer Accessory",
        (
            "surface cleaner for pressure washer",
            "surface-cleaner-for-pressure-washer",
            "surface cleaner for pressure washers",
            "surface-cleaner-for-pressure-washers",
        ),
    ),
    (
        "OPE Combo Kit",
        (
            "string trimmer and leaf blower combo",
            "string-trimmer-and-leaf-blower-combo",
            "string trimmer and blower combo",
            "string-trimmer-and-blower-combo",
            "blower stringtrimmer combo",
            "blower-stringtrimmer-combo",
            "string trimmer and 460 cfm blower combo",
            "string trimmer and 530 cfm blower combo",
            "string trimmer and 420 cfm leaf blower combo",
            "string-trimmer-and-460-cfm-blower-combo",
            "string-trimmer-and-530-cfm-blower-combo",
            "string-trimmer-and-420-cfm-leaf-blower-combo",
        ),
    ),
    (
        "Outdoor Cleaner",
        (
            "outdoor cleaner concentrate",
            "outdoor-cleaner-concentrate",
            "outdoor ready to spray cleaner",
            "outdoor-ready-to-spray-cleaner",
            "outdoor multi surface rts cleaner",
            "outdoor-multi-surface-rts-cleaner",
            "outdoor bleach cleaner",
            "outdoor-bleach-cleaner",
        ),
    ),
    (
        "OPE Accessory",
        (
            "blower hip belt",
            "blower-hip-belt",
            "blower tapered and flat nozzle attachment",
            "blower-tapered-and-flat-nozzle-attachment",
            "blower gutter attachment",
            "blower-gutter-attachment",
            "blower angled flare attachment",
            "blower-angled-flare-attachment",
            "chainsaw case",
            "chainsaw-case",
            "saw chain",
            "saw-chain",
            "guide bar",
            "guide-bar",
            "mower cover",
            "mower-cover",
            "mower blade",
            "mower-blade",
            "pole saw attachment",
            "pole-saw-attachment",
        ),
    ),
    (
        "Bug Zapper",
        ("bug zapper", "bug-zapper"),
    ),
    (
        "Drill",
        ("flip drill", "flip-drill"),
    ),
    (
        "Mixer",
        ("mud mixer", "mud-mixer"),
    ),
    (
        "Impact Wrench",
        ("impact wrench", "impact-wrench"),
    ),
    (
        "Vacuum Accessory",
        (
            "vacuum attachment",
            "vacuum-attachment",
            "vacuum accessory",
            "vacuum-accessory",
            "vt2534",
        ),
    ),
    (
        "Light Bulb",
        (
            "light bulb",
            "light-bulb",
            "bulb finder",
            "bulb-finder",
            "vintage bulb",
            "vintage-bulb",
            "a19 led",
            "a19-led",
            "b11 led",
            "b11-led",
            "br30 led",
            "br30-led",
            "br40 led",
            "br40-led",
            "st19 led",
            "st19-led",
        ),
    ),
    (
        "Upright Vacuum",
        (
            "upright vacuum",
            "upright-vacuum",
            "nv366",
        ),
    ),
    (
        "Cordless Stick Vacuum",
        (
            "pblsv",
        ),
    ),
    (
        "Cordless Handheld Vacuum",
        (
            "handheld vacuum",
            "handheld-vacuum",
            "hand vacuum",
            "hand-vacuum",
            "dustbuster",
            "ionvac",
            "portable vacuum",
            "portable-vacuum",
            "dcv501",
            "chv1410",
            "pcl700",
            "pcl702",
            "pcl704",
            "pcl705",
            "33287201884",
        ),
    ),
    (
        "Cordless Stick Vacuum",
        (
            "stick vacuum",
            "stick-vacuum",
            "stick vac",
            "stick-vac",
            "readyclean cordless",
            "readyclean-cordless",
            "pcl720",
            "5001795075",
        ),
    ),
    (
        "Wet/Dry Vacuum",
        (
            "wet dry vacuum",
            "wet-dry-vacuum",
            "wet/dry vacuum",
            "wet dry vac",
            "wet-dry-vac",
            "wet/dry vac",
            "shop vacuum",
            "shop-vacuum",
            "shop vac",
            "shop-vac",
            "vac powerhead",
            "vac-powerhead",
            "quiet vac",
            "quiet-vac",
            "pcl733",
            "pcl734",
            "wdv0904",
            "33287200375",
        ),
    ),
    (
        "Inflator",
        (
            "inflator",
            "air-inflator",
            "air inflator",
            "fvif51",
            "33287208968",
        ),
    ),
    (
        "Misting Fan",
        (
            "misting fan",
            "misting-fan",
            "misting stand fan",
            "misting-stand-fan",
        ),
    ),
    (
        "Fan",
        (
            "jobsite fan",
            "jobsite-fan",
            "cordless fan",
            "cordless-fan",
            "brushless fan",
            "brushless-fan",
        ),
    ),
    (
        "LED Lighting",
        (
            "work light",
            "work-light",
            "task light",
            "task-light",
            "area light",
            "area-light",
            "project light",
            "project-light",
            "ultra bright ho fixture",
            "ultra-bright-ho-fixture",
            "led light",
            "led-light",
            "lt0300",
            "lt1000",
        ),
    ),
    (
        "Radio/Speaker",
        (
            "bluetooth speaker",
            "bluetooth-speaker",
            "cordless speaker",
            "cordless-speaker",
            "cordless radio",
            "cordless-radio",
            "sk1800",
        ),
    ),
    (
        "Power Cleaner",
        (
            "dcpw550",
            "dcpw1000",
            "ry120350",
            "ry121850",
            "ry124052",
            "ry18pw22",
            "ry18pwx41",
            "hypresso-18-24-1",
            "4140135",
            "ezclean",
            "cordless-power-washers",
            "bcpw350c1",
            "20v-power-cleaner",
        ),
    ),
    (
        "Pruner",
        (
            "pole saw with battery",
            "pole-saw-with-battery",
            "40v brushless 10 inch pole saw",
            "40v-brushless-10-in-pole-saw",
            "20v polesaw",
            "20v-polesaw",
        ),
    ),
    (
        "Walk behind Lawnmower",
        (
            "cordless lawn mower",
            "cordless-lawn-mower",
            "push mower kit",
            "push-mower-kit",
            "self propelled mower kit",
            "self-propelled-mower-kit",
            "lawnmower-pm",
            "lawnmower pm",
        ),
    ),
)


# ``Non-Mowing Equipment`` was a catch-all rather than a product category.
# During migration, only products with an unambiguous target in the current
# production master data are moved to that target. Everything else becomes the
# explicit, non-reportable ``Unknown Category`` instead of being guessed into
# a semantically unrelated OPE category.
LEGACY_NON_MOWING_RULES = (
    (
        "Power Cleaner",
        (
            "dcpw550",
            "dcpw1000",
            "ry120350",
            "ry121850",
            "ry124052",
            "ry18pw22",
            "ry18pwx41",
            "hypresso-18-24-1",
            "4140135",
            "ezclean",
            "cordless-power-washers",
        ),
    ),
    (
        "Pressure Washer",
        (
            "pressure washer",
            "pressure-washer",
            "hpw2105",
            "hpw3204",
        ),
    ),
    ("Sprayer", ("sprayer", "bsp3500")),
    ("Auger", ("auger", "ig0800")),
    (
        "Fan",
        (
            " fan ",
            "-fan-",
            "/fan/",
            "fn1000",
            "fn1800",
            "dce512b",
            "0818-20",
            "0886-20",
            "pcf02",
            "pcl800",
            "pcl801",
            "fvg66",
        ),
    ),
    (
        "Power Scrubber",
        (
            "power scrubber",
            "power-scrubber",
            "fvg51k",
        ),
    ),
    (
        "Patio Cleaner",
        (
            "patio cleaner",
            "patio-cleaner",
            "outdoor cleaner",
            "outdoor-cleaner",
        ),
    ),
    ("Inflator", ("inflator", "air-inflator", "if1600")),
    (
        "Power Station/Inverter",
        ("power station", "power-station", "inverter"),
    ),
)


def reclassify_legacy_non_mowing(
    target_url: object,
    model: object = "",
) -> str:
    """Return the deterministic replacement for a legacy catch-all task."""

    text = " ".join(
        str(value or "").replace("_", " ").casefold()
        for value in (target_url, model)
    )
    # Padding lets rules such as `` fan `` match a word at either edge while
    # avoiding false positives in unrelated identifiers.
    searchable = f" {text} "
    for category, markers in LEGACY_NON_MOWING_RULES:
        if any(marker.casefold() in searchable for marker in markers):
            return category
    return "Unknown Category"


def is_sprayer_product(*values) -> bool:
    """Recognize complete sprayers, excluding paint equipment and service parts."""
    import re
    from urllib.parse import unquote
    text = unquote(" ".join(str(value or "") for value in values)).casefold()
    text = text.replace("-", " ").replace("_", " ")
    return bool(re.search(r"\bsprayers?\b", text)) and not bool(re.search(
        r"\b(?:paint|airless|hvlp|replacement|accessor\w*|attachment\w*|"
        r"nozzles?|hoses?|wands?|repair|parts?|spray gun)\b", text,
    ))


def reclassify_unknown_category(
    target_url: object,
    model: object = "",
    product_title: object = "",
) -> str:
    """Return a reviewed expansion category or preserve explicit unknown."""

    if is_sprayer_product(target_url, model, product_title):
        return "Sprayer"
    text = " ".join(
        str(value or "").replace("_", " ").casefold()
        for value in (target_url, model, product_title)
    )
    searchable = f" {text} "
    for category, markers in UNKNOWN_CATEGORY_RULES:
        if any(marker.casefold() in searchable for marker in markers):
            return category
    return "Unknown Category"


def reclassify_expansion_category(
    current_category: object,
    target_url: object,
    model: object = "",
    product_title: object = "",
) -> str:
    """Normalize reviewed E24 expansion products from earlier broad buckets."""

    current = canonical_category_name(current_category)
    if current == "Unknown Category":
        return reclassify_unknown_category(target_url, model, product_title)

    text = " ".join(
        str(value or "").replace("_", " ").casefold()
        for value in (target_url, model, product_title)
    )
    accessory_markers = (
        "blower hip belt", "blower nozzle attachment", "blower gutter attachment",
        "blower angled flare attachment", "chainsaw case", "saw chain",
        "guide bar", "mower cover", "mower blade", "pole saw attachment",
        "string trimmer attachment", "hedge trimmer attachment",
        "edger attachment", "auger attachment", "scraper bar", "storage cover",
        "mulch plug",
    )
    if any(marker in text for marker in accessory_markers):
        return "OPE Accessory"
    if current == "Walk behind Lawnmower" and any(
        marker in text for marker in (
            "riding mower", "riding-mower", "zero turn", "zero-turn", "zt4214",
            "zt5200",
        )
    ):
        return "Riding Lawnmower"
    if "exmark.com/mowers/zero turn/" in text:
        return "Riding Lawnmower"
    if "privacy lever" in text or "door knob" in text or "door lock" in text:
        return "Door Hardware"
    if current in {"Leaf Blower/Vacuum", "Riding Lawnmower"} and all(
        marker in text for marker in ("mower", "string trimmer", "blower")
    ):
        return "OPE Combo Kit"
    if current == "Cordless Stick Vacuum" and "cleanview swivel upright" in text:
        return "Upright Vacuum"
    if current == "Pump" and any(
        marker in text
        for marker in (
            "inflator",
            "compressor",
            "if1600",
            "fvif51",
            "rpi18",
            "dcc020",
            "pcl001",
            "pcl031",
            "r87044",
            "2475-20",
            "33287208968",
        )
    ):
        return "Inflator"
    if current == "Fan" and any(
        marker in text for marker in ("misting fan", "misting-fan")
    ):
        return "Misting Fan"
    if current == "Patio Cleaner" and any(
        marker in text
        for marker in ("power scrubber", "power-scrubber", "fvg51k")
    ):
        return "Power Scrubber"
    if current == "Pressure Washer":
        return reclassify_power_cleaner_category(
            current,
            target_url,
            model,
        )
    return current or "Unknown Category"


def canonical_category_name(value: object) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if not cleaned:
        return ""
    for alias, canonical in LEGACY_CATEGORY_NAMES.items():
        if cleaned.casefold() == alias.casefold():
            return canonical
    return cleaned
