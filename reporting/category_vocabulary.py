"""Stable category identities shared by reporting contracts and profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final
import unicodedata
from reporting.category_refresh_specs import EXTRA_PROFILE_SPECS


CATEGORY_RESOLUTION_SCHEMA_VERSION: Final[str] = (
    "voxquarry.reporting-category-resolution/v1"
)


@dataclass(frozen=True)
class CategoryDefinition:
    category_id: str
    display_name: str
    aliases: tuple[str, ...]
    profile_id: str
    profile_status: str = "ready"
    illustration_format: str = "webp"

    def public_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "illustration_asset": (
                "images/report-categories/"
                f"{self.category_id}.{self.illustration_format}?v=1"
            ),
        }


CATEGORY_DEFINITIONS: Final[tuple[CategoryDefinition, ...]] = (
    CategoryDefinition(
        "air_compressor",
        "Air Compressor",
        (
            "Portable Air Compressor",
            "Cordless Air Compressor",
            "Jobsite Air Compressor",
        ),
        "air-compressor/1.0.0",
    ),
    CategoryDefinition(
        "auger", "Auger", ("Earth Auger", "Post Hole Auger"), "auger/1.0.0"
    ),
    CategoryDefinition(
        "battery_pack",
        "Battery Pack",
        (
            "Tool Battery",
            "Cordless Tool Battery",
            "Battery",
            "Battery Pack/Power Tool Battery",
        ),
        "battery-pack/1.0.0",
    ),
    CategoryDefinition(
        "charger",
        "Charger",
        (
            "Battery Charger",
            "Rapid Charger",
            "Fast Charger",
            "Multi-Port Charger",
        ),
        "charger/1.0.0",
    ),
    CategoryDefinition(
        "chainsaw", "Chainsaw", ("链锯", "Chain Saw"), "chainsaw/1.2.0"
    ),
    CategoryDefinition(
        "cultivator",
        "Cultivator",
        ("Garden Cultivator", "Power Cultivator"),
        "cultivator/1.0.0",
    ),
    CategoryDefinition(
        "cordless_handheld_vacuum",
        "Handheld Vacuum",
        ("无线手持吸尘器", "Cordless Handheld Vacuum", "Cordless Hand Vacuum"),
        "handheld-vacuum/1.1.0",
    ),
    CategoryDefinition(
        "cordless_stick_vacuum",
        "Cordless Stick Vacuum",
        (
            "Cordless Stick Vac",
            "Stick Vacuum",
            "Stick Vac",
            "Cordless Handstick Vacuum",
            "Handstick Vacuum Cleaner",
        ),
        "cordless-stick-vacuum/1.0.0",
    ),
    CategoryDefinition(
        "cordless_outdoor_cleaner",
        "Cordless Outdoor Cleaner",
        (
            "无线庭院清洁器",
            "Outdoor Cleaner",
        ),
        "outdoor-cleaner/1.0.0",
        "legacy_hidden",
    ),
    CategoryDefinition(
        "cooler",
        "Cooler",
        (
            "Portable Cooler",
            "Powered Cooler",
            "Electric Cooler",
            "Jobsite Cooler",
            "Cooler/Warmer",
        ),
        "cooler/1.0.0",
    ),
    CategoryDefinition(
        "edger", "Edger", ("Lawn Edger", "Power Edger"), "edger/1.0.0"
    ),
    CategoryDefinition(
        "fan",
        "Fan",
        (
            "Portable Fan",
            "Cordless Portable Fan",
            "Jobsite Fan",
            "Job Site Fan",
            "Cordless Fan",
            "风扇",
            "jobsite_fan",
        ),
        "fan/1.0.0",
    ),
    CategoryDefinition(
        "generator",
        "Generator",
        ("Portable Generator", "Inverter Generator"),
        "generator/1.0.0",
    ),
    CategoryDefinition(
        "grass_shrub_shear",
        "Grass Shrub/Shear",
        ("Grass Shear", "Shrub Shear", "Grass Shrub Shear"),
        "grass-shrub-shear/1.0.0",
    ),
    CategoryDefinition(
        "hard_floor_cleaner",
        "Hard Floor Cleaner",
        (
            "Hard Floor Washer",
            "Floor Washer",
            "Floor Cleaner",
            "Wet Dry Floor Cleaner",
            "Wet/Dry Floor Cleaner",
        ),
        "hard-floor-cleaner/1.0.0",
    ),
    CategoryDefinition(
        "hedge_trimmer",
        "Hedge Trimmer",
        ("绿篱机", "Hedge Cutter"),
        "hedge-trimmer/1.2.0",
    ),
    CategoryDefinition(
        "inflator",
        "Inflator",
        ("充气泵", "Air Inflator", "Tire Inflator", "Compressor"),
        "inflator/1.0.0",
    ),
    CategoryDefinition(
        "heater",
        "Heater",
        ("Portable Heater", "Jobsite Heater"),
        "heater/1.0.0",
    ),
    CategoryDefinition(
        "led_lighting",
        "LED Lighting",
        ("LED 灯具", "LED Light", "Jobsite Lighting", "Work Light"),
        "led-lighting/1.0.0",
    ),
    CategoryDefinition(
        "light_bulb",
        "Light Bulb",
        ("LED Bulb", "Smart Bulb"),
        "light-bulb/1.0.0",
    ),
    CategoryDefinition(
        "leaf_blower",
        "Leaf Blower",
        ("吹风机", "Blower", "Leaf Blower/Vacuum"),
        "blower/1.2.0",
    ),
    CategoryDefinition(
        "log_splitter",
        "Log Splitter",
        ("Wood Splitter", "Electric Log Splitter"),
        "log-splitter/1.0.0",
    ),
    CategoryDefinition(
        "lifestyle_tools",
        "Lifestyle Tools",
        ("生活方式", "Lifestyle and Adjacent Tools"),
        "lifestyle-mixed/1.2.0",
        "legacy_hidden",
    ),
    CategoryDefinition(
        "multi_head_system",
        "Multi-Head System",
        ("多功能系统", "Multi Head", "Multi Head System", "Power Head System"),
        "multi-head/1.2.0",
    ),
    CategoryDefinition(
        "misting_fan",
        "Misting Fan",
        ("Mist Fan", "Portable Misting Fan"),
        "misting-fan/1.1.0",
    ),
    CategoryDefinition(
        "mulcher",
        "Mulcher",
        ("Leaf Mulcher", "Garden Mulcher"),
        "mulcher/1.0.0",
    ),
    CategoryDefinition(
        "pole_saw", "Pole Saw", ("杆锯",), "pole-saw/1.2.0"
    ),
    CategoryDefinition(
        "pool_vacuum",
        "Pool Vacuum",
        ("Cordless Pool Vacuum", "Pool Cleaner Vacuum"),
        "pool-vacuum/1.0.0",
    ),
    CategoryDefinition(
        "patio_cleaner",
        "Patio Cleaner",
        (
            "Patio Surface Cleaner",
            "Cordless Patio Cleaner",
            "Patio Scrubber",
        ),
        "patio-cleaner/1.0.0",
    ),
    CategoryDefinition(
        "power_cleaner",
        "Power Cleaner",
        (
            "Cordless Power Washer",
            "Cordless Power Washers",
        ),
        "power-cleaner/1.0.0",
    ),
    CategoryDefinition(
        "power_scrubber",
        "Power Scrubber",
        (
            "Cordless Power Scrubber",
            "Telescoping Power Scrubber",
            "Electric Spin Scrubber",
            "Spin Scrubber",
        ),
        "power-scrubber/1.0.0",
    ),
    CategoryDefinition(
        "portable_power_station",
        "Portable Power Station",
        ("便携式电源", "Power Station/Inverter", "Portable Power", "Power Station"),
        "portable-power/1.2.0",
    ),
    CategoryDefinition(
        "pressure_washer",
        "Pressure Washer",
        (
            "高压清洗机",
            "Power Washer",
            "Gas Pressure Washer",
            "Electric Pressure Washer",
            "Cordless Pressure Washer",
        ),
        "pressure-washer/1.2.1",
    ),
    CategoryDefinition(
        "pruner",
        "Pruner",
        ("Cordless Pruner", "Pruning Shear"),
        "pruner/1.0.0",
    ),
    CategoryDefinition(
        "pump",
        "Pump",
        ("Water Pump", "Transfer Pump", "Utility Pump"),
        "pump/1.0.0",
    ),
    CategoryDefinition(
        "raker",
        "Raker",
        ("Power Rake", "Lawn Raker"),
        "raker/1.0.0",
    ),
    CategoryDefinition(
        "radio_speaker",
        "Radio/Speaker",
        (
            "Radio Speaker",
            "Radio",
            "Speaker",
            "Jobsite Radio",
            "Jobsite Speaker",
            "Jobsite Radio/Speaker",
            "Bluetooth Speaker",
            "Cordless Radio",
            "Cordless Speaker",
        ),
        "radio-speaker/1.0.0",
    ),
    CategoryDefinition(
        "riding_mower",
        "Riding Mower",
        ("骑乘割草机", "Riding Lawnmower", "Ride-On Mower", "Ride On Mower"),
        "riding-mower/1.2.0",
    ),
    CategoryDefinition(
        "snow_blower",
        "Snow Blower",
        ("扫雪机", "Snow Thrower"),
        "snow-blower/1.2.0",
    ),
    CategoryDefinition(
        "scarifier",
        "Scarifier",
        ("Lawn Scarifier", "Dethatcher"),
        "scarifier/1.0.0",
    ),
    CategoryDefinition(
        "seeder",
        "Seeder",
        ("Lawn Seeder", "Seed Spreader"),
        "seeder/1.0.0",
    ),
    CategoryDefinition(
        "shredder",
        "Shredder",
        ("Garden Shredder", "Wood Chipper"),
        "shredder/1.0.0",
    ),
    CategoryDefinition(
        "snow_shovel",
        "Snow Shovel",
        ("扫雪铲", "Power Snow Shovel", "Cordless Snow Shovel"),
        "snow-shovel/1.0.0",
    ),
    CategoryDefinition(
        "sprayer",
        "Sprayer",
        ("Backpack Sprayer", "Handheld Sprayer", "Garden Sprayer"),
        "sprayer/1.1.0",
    ),
    CategoryDefinition(
        "spot_cleaner",
        "Spot Cleaner",
        (
            "Portable Spot Cleaner",
            "Carpet Spot Cleaner",
            "Upholstery Spot Cleaner",
            "Spot Carpet Cleaner",
        ),
        "spot-cleaner/1.0.0",
    ),
    CategoryDefinition(
        "string_trimmer",
        "String Trimmer",
        ("打草机", "Grass Trimmer", "Weed Trimmer", "Weed Eater"),
        "string-trimmer/1.2.0",
    ),
    CategoryDefinition(
        "tiller_cultivator",
        "Tiller/Cultivator",
        ("Tiller Cultivator", "Garden Tiller"),
        "tiller-cultivator/1.0.0",
    ),
    CategoryDefinition(
        "upright_vacuum",
        "Upright Vacuum",
        ("直立吸尘器",),
        "upright-vacuum/1.2.0",
    ),
    CategoryDefinition(
        "vacuum_accessory",
        "Vacuum Accessory",
        ("Vacuum Attachment", "Shop Vacuum Accessory"),
        "vacuum-accessory/1.0.0",
    ),
    CategoryDefinition(
        "walk_behind_mower",
        "Walk-Behind Mower",
        (
            "割草机",
            "Walk Behind Mower",
            "Walk behind Lawnmower",
            "Push Mower",
            "Self-Propelled Mower",
            "Lawn Mower",
        ),
        "mower/1.2.0",
    ),
    CategoryDefinition(
        "weed_remover",
        "Weed Remover",
        ("Weed Puller", "Weeding Tool"),
        "weed-remover/1.0.0",
    ),
    CategoryDefinition(
        "wet_dry_vacuum",
        "Wet/Dry Vacuum",
        ("湿干两用吸尘器", "Wet Dry Vacuum", "Shop Vacuum", "Shop Vac"),
        "wet-dry-vacuum/1.0.0",
    ),
)


CATEGORY_DEFINITIONS = (*CATEGORY_DEFINITIONS, *(
    CategoryDefinition(str(spec["category_id"]), name,
                       ("Handheld Spreader",) if name == "Spreader" else (),
                       str(spec["profile_id"]))
    for name, spec in EXTRA_PROFILE_SPECS.items()
))

CATEGORY_BY_ID: Final[dict[str, CategoryDefinition]] = {
    definition.category_id: definition for definition in CATEGORY_DEFINITIONS
}


def _alias_key(value: object) -> str:
    cleaned = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).strip()
    return cleaned.casefold()


CATEGORY_BY_ALIAS: Final[dict[str, CategoryDefinition]] = {
    _alias_key(alias): definition
    for definition in CATEGORY_DEFINITIONS
    for alias in (
        definition.category_id,
        definition.display_name,
        *definition.aliases,
    )
}


def category_definition(value: object) -> CategoryDefinition | None:
    return CATEGORY_BY_ALIAS.get(_alias_key(value))


def category_definition_by_id(
    category_id: object,
) -> CategoryDefinition | None:
    return CATEGORY_BY_ID.get(str(category_id or "").strip())


def category_id_for(value: object) -> str | None:
    definition = category_definition(value)
    return definition.category_id if definition else None


def category_resolution(value: object) -> dict[str, object]:
    """Return the versioned audit record for a source category label."""

    source_category = str(value or "")
    normalized_source_category = _alias_key(value)
    definition = CATEGORY_BY_ALIAS.get(normalized_source_category)
    if definition is None:
        match_type = "unresolved"
    elif normalized_source_category == _alias_key(definition.category_id):
        match_type = "category_id"
    elif normalized_source_category == _alias_key(definition.display_name):
        match_type = "canonical_name"
    else:
        match_type = "controlled_alias"
    return {
        "schema_version": CATEGORY_RESOLUTION_SCHEMA_VERSION,
        "source_category": source_category,
        "normalized_source_category": normalized_source_category,
        "category_id": definition.category_id if definition else None,
        "canonical_category": definition.display_name if definition else None,
        "match_type": match_type,
    }


def canonical_category_name(value: object) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    definition = category_definition(cleaned)
    return definition.display_name if definition else cleaned


def category_catalog() -> list[dict[str, object]]:
    return [definition.public_dict() for definition in CATEGORY_DEFINITIONS]


# Backward-compatible exports for older immutable reports and callers.
LEGACY_CATEGORY_NAMES: Final[dict[str, str]] = {
    alias: definition.display_name
    for definition in CATEGORY_DEFINITIONS
    for alias in definition.aliases
    if any("\u4e00" <= character <= "\u9fff" for character in alias)
}
ENGLISH_TO_LEGACY: Final[dict[str, str]] = {
    english: legacy for legacy, english in LEGACY_CATEGORY_NAMES.items()
}
