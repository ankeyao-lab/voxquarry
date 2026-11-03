from __future__ import annotations

from typing import Any


CATEGORIES = [
    {"id": "cat_walk_behind_mower", "code": "walk_behind_mower", "name": "Walk-behind Mower", "name_zh": "手推式割草机", "source_categories": ["Lawn Mower"], "description": "Mower product records with common specifications.", "enabled": True},
    {
        "id": "cat_wet_dry_vacuum",
        "code": "wet_dry_vacuum",
        "name": "Wet/Dry Vacuum",
        "name_zh": "干湿两用吸尘器",
        "source_categories": ["Wet/Dry Vacuum"],
        "description": "按容量、吸力、气流、过滤和供电方式维护吸尘器竞品。",
    },
    {
        "id": "cat_light",
        "code": "light",
        "name": "Light",
        "name_zh": "工作灯",
        "source_categories": ["LED Lighting"],
        "description": "按光通量、照射形态、续航和电压平台维护工作灯竞品。",
        "enabled": False,
    },
    {
        "id": "cat_fan",
        "code": "fan",
        "name": "Fan",
        "name_zh": "风扇",
        "source_categories": ["Fan"],
        "description": "按风量、风速、摆动、雾化和电压平台维护风扇竞品。",
        "enabled": False,
    },
    {
        "id": "cat_pressure_washer",
        "code": "pressure_washer",
        "name": "Pressure Washer",
        "name_zh": "高压清洗机",
        "source_categories": ["Pressure Washer"],
        "description": "按压力与同工况流量、供电、套装和渠道价格比较高压清洗机。",
        "enabled": True,
    },
    {
        "id": "cat_cross_category_bundle",
        "code": "cross_category_bundle",
        "name": "Cross-category Bundle",
        "name_zh": "跨品类组套",
        "source_categories": ["Cross-category Bundle"],
        "description": "跨两个或以上品类的组套主数据；下游单品分析必须明确拒绝。",
        "enabled": False,
    },
]

# Separate planning profiles. Existing collection identities require explicit
# source mappings; matching display names alone never merges product records.
for code, name, zh, description in [
    ("misting_fan", "Misting Fan", "喷雾风扇", "比较风量、喷雾、供水、便携与续航。"),
    ("handheld_vacuum", "Handheld Vacuum", "手持吸尘器", "比较清洁性能、容积、重量与配件。"),
    ("handheld_spreader", "Handheld Spreader", "手持撒播器", "比较容量、撒播范围、适用物料与操控。"),
    ("backpack_sprayer", "Backpack Sprayer", "背负式喷雾器", "比较容量、压力、流量、背负与维护。"),
    ("handheld_sprayer", "Handheld Sprayer", "手持喷雾器", "比较容量、压力、重量、喷嘴与供电。"),
]:
    CATEGORIES.append({"id": "cat_" + code, "code": code, "name": name,
                       "name_zh": zh, "source_categories": [name], "description": description})


COMMON_FIELDS = [
    ("brand", "品牌", "text", "draft_required", None, ["Brand", "• Brand:"]),
    ("model_number", "型号", "text", "draft_required", None, ["Model", "Model #", "Model No.", "Model Number", "• Model Number:"]),
    ("product_name", "产品名称", "text", "recommended", None, ["Product:"]),
    ("positioning", "产品定位", "text", "optional", None, ["• Positioning:"]),
    ("platform_series", "平台 / 系列", "text", "recommended", None, ["• Platform or Series"]),
    ("energy_type", "能源形式", "enum", "publish_required", None, ["AC", "DC", "AC/DC", "• AC/DC", "Hybrid?", "Hybrid"]),
    ("voltage_platform", "电压平台", "text", "publish_conditional", "V", ["Voltage", "Volts", "• Volts"]),
    ("launch_date", "上市日期", "date", "recommended", None, ["Launch Date", "• Launch Time"]),
    ("launch_region", "上市地区", "text", "publish_required", None, []),
    ("retail_price", "零售价", "number", "recommended", None, ["Price", "Price ($)", "Retail price (USD)"]),
    ("bt_msrp", "裸机 MSRP", "number", "optional", None, ["• BT MSRP:"]),
    ("kit_msrp", "套装 MSRP", "number", "optional", None, ["• KIT MSRP:"]),
    ("rating_count", "评价数量", "integer", "optional", None, ["Rating Num", "rating number", "Rating Numbers", "Ratings (09/29/22)", "Rating Number(THD)"]),
    ("retailer", "零售商", "text", "publish_required", None, ["Retailer", "retailer", "Retailer (highlighted)", "Retailer online/in store (highlighted)"]),
    ("online_url", "线上零售链接", "url", "publish_required", None, ["Resource"]),
    ("warranty", "保修说明", "text", "optional", None, ["Warranty", "• Warranty"]),
    ("warranty_years", "保修年限", "number", "optional", "year", ["Warranty (years)"]),
    ("weight", "重量", "number", "optional", "lb", ["Weight", "Weight (lbs)", "Weight (lbs.)", "Weight(Ib)"]),
    ("motor_type", "电机类型", "text", "optional", None, ["Motor Type", "• Motor Type", "Motor"]),
    ("runtime_claimed", "宣称续航", "duration", "optional", "min", ["Runtime", "Run time Claim", "Run time (Claim)", "Run time (claim)", "• Claimed running time"]),
    ("included_accessories", "随附附件", "text", "optional", None, ["Include", "In-Kit Accessories", "In-kit Accessories"]),
    ("additional_features", "其他产品特征", "text", "optional", None, ["Additional Product Detail/Feature Set", "• Additional Product Detail/Feature Set", "OTHERS"]),
]


FAN_FIELDS = [
    ("usp_1", "USP 1", "text", None, ["• USP1"]),
    ("usp_2", "USP 2", "text", None, ["• USP2"]),
    ("usp_3", "USP 3", "text", None, ["• USP3"]),
    ("usp_4", "USP 4", "text", None, ["• USP4"]),
    ("usp_5", "USP 5", "text", None, ["• USP5"]),
    ("battery_amp_hours", "单电池容量", "number", "Ah", ["• Amp Hours"]),
    ("total_battery_capacity", "总电池容量", "number", "Wh", ["• Total Battery Capacity"]),
    ("fan_blade_size", "扇叶尺寸", "number", "in", ["• Fan Blade Size"]),
    ("max_power", "最大功率", "number", "W", ["• Max Power (w)"]),
    ("pivoting_angle", "俯仰角度", "number", "degree", ["• Pivoting Angle (degree)"]),
    ("auto_oscillation", "自动摆动", "boolean", None, ["• Auto oscillation"]),
    ("iot", "IoT", "boolean", None, ["• IoT"]),
    ("type_c", "Type-C", "boolean", None, ["• Type-C"]),
    ("handle", "提手", "text", None, ["• Handle"]),
    ("wheels", "滚轮", "boolean", None, ["• Wheels"]),
    ("misting_function", "雾化功能", "boolean", None, ["• Misting Function（Yes/No）"]),
    ("max_air_volume", "最大风量", "number", "CFM", ["• Max air volume (CFM)"]),
    ("max_air_speed_mph", "最大风速", "number", "MPH", ["• Max air speed (MPH)"]),
    ("max_air_speed_kmh", "最大风速", "number", "km/h", ["• Max air speed (km/h)"]),
    ("air_speed_settings", "风速档位", "integer", None, ["• Air speed setting"]),
    ("max_water_volume_gph", "最大水量", "number", "GPH", ["• Max water volume (GPH)"]),
    ("max_water_volume_lh", "最大水量", "number", "L/h", ["• Max water volume (L/h)"]),
    ("mist_settings", "雾化档位", "integer", None, ["• Mist Setting"]),
    ("nozzle_count", "喷嘴数量", "integer", None, ["• Number of nozzle"]),
    ("water_pump", "水泵", "boolean", None, ["• Water pump"]),
    ("garden_hose_adapter", "花园水管接口", "boolean", None, ["• Garden hose adapter (yes/no)"]),
    ("bucket_top", "桶顶适配", "boolean", None, ["• Bucket top"]),
    ("weight_without_battery", "不含电池重量", "number", "lb", ["• Weight w/o battery"]),
    ("weight_with_battery", "含电池重量", "number", "lb", ["• Weight w/ battery"]),
    ("noise", "噪音", "number", "dBA", ["• Noise"]),
    ("weather_resistant", "防护等级", "text", None, ["• Weather Resistant"]),
    ("in_store_2024", "2024 零售状态", "text", None, ["• 2024 In Store or Not:"]),
    ("in_store_2025", "2025 零售状态", "text", None, ["• 2025 In Store or Not:"]),
]


VACUUM_FIELDS = [
    ("amps", "电流", "number", "A", ["AMPS", "Amps"]),
    ("dust_capacity", "尘桶容量", "number", "gal", ["Dust capacity (gallons)", "Size (Gallon)", "Size"]),
    ("peak_horsepower", "峰值马力", "number", "HP", ["Peak horse power (HP)"]),
    ("air_flow", "风量", "number", "CFM", ["Air flow (cfm)", "CFM"]),
    ("sealed_pressure", "密封压力 / 水升", "number", "in.H2O", ["Sealed pressure (in.H2O)", "Sealed Pressure", "Suction Power Lift(in.H2O)", "Suction Power Lift", "Water lift (in)"]),
    ("air_watts", "空气瓦特", "number", "AW", ["Air watts", "air watts"]),
    ("power", "功率", "number", "W", ["Power (W)"]),
    ("hose_accessory_diameter", "软管及附件直径", "number", "in", ["Hose and accessory diameter (in.)", "Hose size"]),
    ("hose_length", "软管长度", "number", "ft", ["Hose length (ft.)"]),
    ("power_cord_length", "电源线长度", "number", "ft", ["Power Cord length (ft)"]),
    ("wet_dry", "干湿两用", "boolean", None, ["Wet/dry"]),
    ("filtration_type", "过滤类型", "text", None, ["Filtration Type"]),
    ("filter_clean_system", "滤芯清洁系统", "text", None, ["Filter clean system"]),
    ("blower_function", "吹风功能", "boolean", None, ["Blower function"]),
    ("blower_speed", "吹风速度", "number", "MPH", ["Speed (mph)"]),
    ("noise", "噪音", "number", "dB", ["Noise", "Noise(dB)", "Noise (dB)"]),
    ("wall_mount", "壁挂", "boolean", None, ["Wall mount", "wall mount"]),
    ("tank_drain", "桶体排水", "boolean", None, ["Tank Drain", "Tank drain"]),
    ("battery_ports", "电池端口数", "integer", None, ["Battery Port"]),
]


PRESSURE_WASHER_FIELDS = [
    ("battery_amp_hours", "单电池容量", "number", "Ah", ["Individual battery Ah", "Individual Battery Amp Hours"]),
    ("batteries_included", "随附电池数", "integer", None, ["# of batteries included"]),
    ("total_watt_hours", "随附总电量", "number", "Wh", ["Total watt-hours included", "Total Watt hours included"]),
    ("battery_ports", "工具电池端口数", "integer", None, ["# of battery ports/tool"]),
    ("charger_wattage", "充电器功率", "number", "W", ["Charger Wattage", "Charger Wattage (W)"]),
    ("charge_time", "充电时间", "duration", "min", ["Charge time (claimed)", "Charge time (hours)", "Charge time (minutes)"]),
    ("charging_ports", "充电端口数", "integer", None, ["# of chargers/charging ports", "# of charging ports"]),
    ("battery_draw", "电池放电", "number", "A", ["Battery Draw"]),
    ("motor_amps", "电机电流", "number", "A", ["Amps (Motor)", "Amps (motor)", "Amps"]),
    ("max_psi", "最大压力", "number", "PSI", ["Max PSI"]),
    ("max_gpm", "最大流量", "number", "GPM", ["Max GPM"]),
    ("noise", "噪音", "number", "dB", ["dB"]),
    ("pressure_settings", "压力档位数", "integer", None, ["# Pressure settings"]),
    ("remote_speed_control", "远程速度控制", "boolean", None, ["Remote Speed Control"]),
    ("hp_hose_length", "高压软管长度", "number", "ft", ["Length of HP hose (ft)", "Length of HP hose (Ft)", "Length of hose"]),
    ("siphon_hose_length", "虹吸 / 抽水管长度", "number", "ft", ["Length of siphon hose (ft)", "Length of siphon hose", "Length of extraction hose (ft)"]),
    ("bucket_included", "随附水桶", "boolean", None, ["Bucket included"]),
    ("bucket_size", "水桶容量", "number", "gal", ["Bucket size (gal)"]),
    ("hose_connect", "水管直连", "boolean", None, ["Hose connect?", "Hose Connect"]),
    ("wheel_size", "轮径", "number", "in", ["Wheel Diameter", "Wheel size"]),
    ("handle_style", "手柄形式", "text", None, ["Handle Style"]),
    ("handle_material", "手柄材质", "text", None, ["Handle Material"]),
    ("wand_material", "喷杆材质", "text", None, ["Metal or Plastic Wand"]),
    ("detergent_system", "清洁剂系统", "text", None, ["Detergent system"]),
    ("nozzle_count", "喷嘴总数", "integer", None, ["# of total nozzles", "# of nozzles", "# of nozzles (spray tips)"]),
    ("nozzle_angle_count", "喷嘴角度数", "integer", None, ["# of nozzle angles"]),
    ("adjustable_nozzle", "可调喷嘴", "boolean", None, ["1 adjustable nozzle", "1 adjustable Nozzle"]),
    ("nozzle_type", "喷嘴类型", "text", None, ["Nozzle Type"]),
    ("turbo_nozzle", "Turbo 喷嘴", "boolean", None, ["Turbo Nozzle", "Turbo"]),
    ("soap_nozzle", "皂液喷嘴", "boolean", None, ["Soap Nozzle"]),
    ("nozzle_0", "0° 喷嘴", "boolean", None, ["0°"]),
    ("nozzle_15", "15° 喷嘴", "boolean", None, ["15° Nozzle", "15°"]),
    ("nozzle_20", "20° 喷嘴", "boolean", None, ["20°"]),
    ("nozzle_25", "25° 喷嘴", "boolean", None, ["25° Nozzle", "25°"]),
    ("nozzle_30", "30° 喷嘴", "boolean", None, ["30°"]),
    ("nozzle_40", "40° 喷嘴", "boolean", None, ["40° Nozzle", "40°"]),
    ("foam_cannon", "泡沫壶", "boolean", None, ["Foam Cannon", "Foam Cannon included", "Foam Cannon Included", "Foam Cannon (in-kit)"]),
    ("quick_connect", "喷嘴快接", "boolean", None, ["Quick Connect? (Nozzle)", "Quick Connect (nozzle)"]),
    ("quick_connect_integration", "各连接点快接", "text", None, ["Quick Connect integration (each connection)"]),
    ("external_accessories", "外部附件", "text", None, ["External Accessories"]),
    ("surface_cleaner", "地面清洗盘", "boolean", None, ["Surface Cleaner"]),
    ("surface_cleaner_style", "地面清洗盘形式", "text", None, ["Surface Cleaner Style"]),
    ("brass_foam_cannon", "黄铜泡沫壶", "boolean", None, ["Brass Foam Cannon"]),
    ("low_pressure_nozzle", "低压喷嘴", "boolean", None, ["Low Pressure Nozzle"]),
    ("telescopic_wand", "伸缩喷杆", "boolean", None, ["Telescopic Wand"]),
    ("telescopic_wand_extension", "伸缩喷杆长度", "number", "ft", ["Telecopic Wand Extention (ft.)"]),
    ("storage_features", "收纳功能", "text", None, ["Storage Feature Set"]),
    ("hose_storage", "软管收纳", "text", None, ["Hose Storage Method", "HP Hose Storage?", "HP Hose Storage"]),
    ("nozzle_storage", "喷嘴收纳", "text", None, ["Nozzle Storage Location", "NozzleStorage Method", "Nozzle Storage Method"]),
    ("ac_cord_storage", "电源线收纳", "text", None, ["AC Cord Storage Method"]),
    ("pwma_certification", "PWMA 认证", "boolean", None, ["PWMA Certification?", "PWMA Certification"]),
]


LIGHT_FIELDS = [
    ("light_output", "最大光通量", "number", "lm", []),
    ("brightness_settings", "亮度档位", "integer", None, []),
    ("color_temperature", "色温", "number", "K", []),
    ("beam_distance", "照射距离", "number", "m", []),
    ("head_pivot_angle", "灯头旋转角度", "number", "degree", []),
    ("runtime_high", "高档续航", "duration", "min", []),
    ("runtime_low", "低档续航", "duration", "min", []),
    ("ac_input", "AC 输入", "boolean", None, []),
    ("usb_output", "USB 输出", "boolean", None, []),
    ("tripod_compatible", "三脚架兼容", "boolean", None, []),
    ("mounting_options", "安装方式", "text", None, []),
    ("weather_resistant", "防护等级", "text", None, []),
]


def _field(
    category_code: str,
    definition: tuple,
    *,
    common: bool = False,
) -> dict[str, Any]:
    if common:
        code, label, value_type, requirement, unit, aliases = definition
        group = "基础信息"
    else:
        code, label, value_type, unit, aliases = definition
        requirement = "optional"
        group = "品类规格"
    return {
        "id": f"fld_{category_code}_{code}",
        "category_code": category_code,
        "code": code,
        "label": label,
        "group": group,
        "value_type": value_type,
        "requirement": requirement,
        "canonical_unit": unit,
        "is_matrix": group == "品类规格" and value_type in {"number", "integer", "duration"},
        "source_aliases": aliases,
    }


FIELD_PROFILES = {
    "walk_behind_mower": [
        ("cutting_width", "割幅", "number", "in", []),
        ("cutting_height", "切割高度", "text", "in", []),
        ("self_propelled", "自走", "boolean", None, []),
    ],
    "fan": FAN_FIELDS,
    "wet_dry_vacuum": VACUUM_FIELDS,
    "pressure_washer": PRESSURE_WASHER_FIELDS,
    "light": LIGHT_FIELDS,
    "cross_category_bundle": [],
    "misting_fan": FAN_FIELDS,
    "handheld_vacuum": [item for item in VACUUM_FIELDS if item[0] in {"air_watts", "sealed_pressure", "max_air_volume", "tank_capacity", "hose_length"}],
    "handheld_spreader": [
        ("hopper_capacity", "料斗容量", "number", "L", []),
        ("spread_width", "撒播幅宽", "number", "m", []),
        ("flow_settings", "流量档位", "integer", "count", []),
        ("compatible_materials", "适用物料", "text", None, []),
    ],
    "backpack_sprayer": [
        ("tank_capacity", "药箱容量", "number", "L", []),
        ("max_psi", "最大压力", "number", "PSI", []),
        ("flow_rate", "流量", "number", "L/min", []),
        ("harness", "背负系统", "text", None, []),
    ],
    "handheld_sprayer": [
        ("tank_capacity", "药箱容量", "number", "L", []),
        ("max_psi", "最大压力", "number", "PSI", []),
        ("flow_rate", "流量", "number", "L/min", []),
        ("nozzle_count", "喷嘴数量", "integer", "count", []),
    ],
}


def has_field_profile(category_code: str) -> bool:
    return category_code in FIELD_PROFILES


def fields_for(category_code: str) -> list[dict[str, Any]]:
    """Return a specialized field profile, or no fields for registry-only categories.

    Product category identity is governed by the persisted category registry.
    Absence from this module means only that Chengmu has no category-specific
    comparison profile; it must not make the category identity invalid.
    """
    if not has_field_profile(category_code):
        return []
    specific = FIELD_PROFILES[category_code]
    return [
        *[_field(category_code, item, common=True) for item in COMMON_FIELDS],
        *[_field(category_code, item) for item in specific],
    ]


def all_fields() -> list[dict[str, Any]]:
    return [field for category in CATEGORIES for field in fields_for(category["code"])]


def uncovered_aliases(category_code: str, aliases: set[str]) -> set[str]:
    covered = {
        alias.strip()
        for field in fields_for(category_code)
        for alias in field["source_aliases"]
    }
    structural = {
        "SPEC", "Product:", "USP", "Power", "Basic features", "Air", "Mist",
        "Other Important Features:", "Cordless Pressure Washer Landscape",
        "DC Power Cleaner Landscape", "AC (in-store) Pressure Washer Landscape",
        "Gas (in-store) Landscape", "Brand", "Model No.", "Model #", "Model Number",
    }
    return {value for value in aliases if value.strip() not in covered | structural}
