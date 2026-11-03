from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from reporting.category_profile_specs import CATEGORY_PROFILE_SPECS
from reporting.spreader_profile_specs import WHEELED_SPREADER_SPEC
from reporting.category_vocabulary import (
    category_definition,
    category_definition_by_id,
    category_id_for,
)
from reporting.source import canonical_json
from reporting.json_stream import canonical_json_sha256
from reporting.smart_method import (
    SMART_METHOD_DEFINITIONS,
    SMART_METHOD_VERSION,
    smart_method_definition,
)


SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)", re.MULTILINE)
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

EXTRACTOR_VERSION = "ope-context/3.4.4"
NARRATIVE_VERSION = "senior-pm/3.4.0"
EVIDENCE_SELECTION_VERSION = "evidence-diversity/4.1.0"

SOURCE_AGE_BUCKETS = {
    "17orunder": "≤17",
    "18to24": "18–24",
    "25to34": "25–34",
    "35to44": "35–44",
    "45to54": "45–54",
    "55to64": "55–64",
    "65orover": "65+",
}


def _rx(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.IGNORECASE)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _sample_interpretation(
    *,
    review_entities: int,
    written_entities: int,
    average_rating: float | None,
    positive_rate: float | None,
    negative_rate: float | None,
    channel_count: int,
    model_count: int,
    market_count: int,
) -> str:
    coverage = _ratio(written_entities, review_entities)
    coverage_read = (
        "正文覆盖充分，后续语义分析与评分总体接近同一分母"
        if coverage >= 0.8
        else "正文覆盖中等，语义结论只代表有正文的评论"
        if coverage >= 0.5
        else "正文覆盖偏低，评分总体与语义样本需要严格分开解读"
    )
    source_read = (
        f"样本覆盖 {channel_count} 个渠道和 {model_count} 个型号"
        if channel_count > 1 or model_count > 1
        else "样本由单一渠道和型号主导"
    )
    if market_count > 1:
        source_read += (
            f"，并覆盖 {market_count} 个来源市场；比较前需核对各单元规模与时间覆盖"
        )
    elif market_count == 1:
        source_read += "，但只有一个来源市场，不具备跨地区比较条件"
    else:
        source_read += "，且缺少来源市场证据，不能进行地区解读"
    rating_read = (
        f"总体口碑为 {average_rating:.2f} 分，好评率 "
        f"{(positive_rate or 0) * 100:.1f}%，差评率 "
        f"{(negative_rate or 0) * 100:.1f}%"
        if average_rating is not None
        else "当前范围缺少可用评分，不能解读总体口碑"
    )
    return f"{rating_read}；{coverage_read}；{source_read}。"


def _entity_ref(fact: dict[str, Any]) -> str:
    return str(fact.get("review_entity_id") or fact["review_occurrence_id"])


def _evidence_entity_ref(row: dict[str, Any]) -> str:
    return str(
        row.get("review_entity_id")
        or row.get("review_id")
        or row["evidence_id"]
    )


def _sentences(text: str) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for match in SENTENCE_RE.finditer(text):
        sentence = " ".join(match.group(0).split()).strip()
        if sentence:
            rows.append((match.start(), match.end(), sentence))
    return rows


@dataclass(frozen=True)
class LabelRule:
    label: str
    display: str
    expression: str


@dataclass(frozen=True)
class DimensionRule:
    dimension_id: str
    section: str
    title: str
    question: str
    rules: tuple[LabelRule, ...]


def _label(label: str, display: str, expression: str) -> LabelRule:
    return LabelRule(label, display, expression)


OCCUPATION_DIMENSION = DimensionRule(
    "occupation",
    "profile",
    "明确职业 / 工作背景",
    "评论者是否以第一人称明确自述了职业、专业背景或经营身份？",
    (
        _label(
            "landscape_tree_professional",
            "园林、树艺或草坪服务专业人员",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional|commercial) )?"
            r"(?:landscaper|gardener|arborist|lawn ?care professional)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional|commercial) )?"
            r"(?:landscaper|gardener|arborist|lawn ?care professional)\b|"
            r"\bmy (?:job|work|background|experience) as (?:a |an )?"
            r"(?:landscaper|gardener|arborist)\b|"
            r"\b(?:i|we) (?:own|owned|run|ran|operate|operated|have|had|"
            r"started|manage|managed) (?:a |an |the |my |our )?"
            r"(?:(?:small|commercial|startup) )?"
            r"(?:landscaping|lawn ?care|lawncare|tree care|mowing|"
            r"grounds maintenance) (?:business|company|service|crew)\b|"
            r"\b(?:my|our) (?:(?:small|commercial|startup) )?"
            r"(?:landscaping|lawn ?care|lawncare|tree care|mowing|"
            r"grounds maintenance) (?:business|company|crew)\b|"
            r"\b(?:owner\s*/\s*operator|owner[- ]operator) of (?:a )?"
            r"(?:landscaping|lawn ?care|tree care|mowing) "
            r"(?:business|company)\b|"
            r"\b(?:i|we) (?:work|worked) (?:as|in) (?:a )?"
            r"(?:landscaper|gardener|arborist|landscaping|lawn ?care)\b|"
            r"\b(?:i|we) (?:do|did) (?:landscaping|lawn ?care|tree work) "
            r"(?:professionally|for a living)\b|"
            r"\bmoonlighting as (?:a )?landscaper\b",
        ),
        _label(
            "contractor_trades",
            "承包商或专业技工",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional|licensed|"
            r"experienced|regular) )?"
            r"(?:contractor|handyman|carpenter|electrician|plumber|mechanic|"
            r"roofer|builder|tradesman|construction worker)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional|licensed|"
            r"experienced) )?(?:contractor|handyman|carpenter|electrician|"
            r"plumber|mechanic|roofer|builder|tradesman)\b|"
            r"\b(?:my|our) (?:contracting|construction|handyman) "
            r"(?:business|company|crew)\b|"
            r"\b(?:i|we) (?:own|run|operate|have|started) (?:a |an )?"
            r"(?:contracting|construction|handyman) (?:business|company)\b|"
            r"\b(?:work|worked) (?:as|in) (?:a )?"
            r"(?:contractor|construction|the trades)\b",
        ),
        _label(
            "facilities_grounds",
            "设施、物业或场地维护人员",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional) )?"
            r"(?:groundskeeper|custodian|janitor|facilities manager|"
            r"facility manager|maintenance worker|property manager)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional) )?"
            r"(?:groundskeeper|custodian|janitor|facilities manager|"
            r"facility manager|maintenance worker|property manager)\b|"
            r"\b(?:i )?(?:work|worked) (?:in|for|as).{0,24}"
            r"(?:facilities|grounds|property maintenance)\b|"
            r"\b(?:our|my) (?:grounds )?maintenance "
            r"(?:department|crew|business)\b",
        ),
        _label(
            "agriculture_operator",
            "农场、牧场或果园经营者",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional) )?"
            r"(?:farmer|rancher|orchardist|forester|logger)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional) )?"
            r"(?:farmer|rancher|orchardist|forester|logger)\b|"
            r"\b(?:my|our) (?:farm|ranch|orchard) (?:business|operation)\b|"
            r"\bwork (?:on|at).{0,18}(?:farm|ranch|orchard)\b",
        ),
        _label(
            "cleaning_detailing_professional",
            "专业清洁、保洁或车辆美容人员",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )(?:(?:former|retired|professional|commercial) )?"
            r"(?:cleaner|housekeeper|detailer|janitorial professional)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional|commercial) )?"
            r"(?:cleaner|housekeeper|detailer|janitorial professional)\b|"
            r"\b(?:my|our) (?:cleaning|detailing|pressure washing) "
            r"(?:business|company|service)\b|"
            r"\b(?:i|we) (?:own|run|operate|have|started) (?:a |an )?"
            r"(?:cleaning|detailing|pressure washing) "
            r"(?:business|company|service)\b",
        ),
        _label(
            "snow_removal_professional",
            "专业清雪人员",
            r"\b(?:my|our) snow removal (?:business|company|service|crew)\b|"
            r"\b(?:i|we) (?:remove|plow|clear) snow (?:for|at) "
            r"(?:clients|customers|work)\b|"
            r"\bprofessional snow removal\b",
        ),
        _label(
            "engineering_technical_professional",
            "工程、技术或产品专业人员",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional|licensed) )?"
            r"(?:engineer|architect|scientist|software developer|"
            r"software engineer|product manager|technician)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional|licensed) )?"
            r"(?:engineer|architect|scientist|software developer|"
            r"software engineer|product manager|technician)\b|"
            r"\bmy (?:job|work|background|experience|complaint|view|opinion) "
            r"as (?:a |an )?(?:engineer|architect|scientist|"
            r"software developer|software engineer|product manager|technician)\b|"
            r"\bi (?:do|did) (?:it|engineering|technical) work "
            r"for a living\b",
        ),
        _label(
            "health_education_public_service",
            "医疗、教育或公共服务专业人员",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|professional|licensed) )?"
            r"(?:nurse|doctor|physician|therapist|teacher|professor|"
            r"firefighter|fireman|police officer|first responder)\b|"
            r"^as (?:a |an )(?:(?:former|retired|professional|licensed) )?"
            r"(?:nurse|doctor|physician|therapist|teacher|professor|"
            r"firefighter|fireman|police officer|first responder)\b",
        ),
        _label(
            "business_owner_manager",
            "企业主 / 经营管理者",
            r"(?<!like )\b(?:i am|i['’]m|i was|i used to be) "
            r"(?:a |an )?(?:(?:former|retired|small) )?"
            r"(?:business owner|company owner|shop owner|operations manager)\b|"
            r"^as (?:a |an )(?:(?:former|retired|small) )?"
            r"(?:business owner|company owner|shop owner|operations manager)\b",
        ),
    ),
)

OPERATING_RESPONSIBILITY_DIMENSION = DimensionRule(
    "operating_responsibility",
    "profile",
    "物业 / 场地责任",
    "评论者明确负责维护什么性质的物业或场地？",
    (
        _label(
            "residential_self_maintainer",
            "明确承担住宅 / 庭院日常维护",
            r"\b(?:i|we) (?:personally )?"
            r"(?:mow|cut|trim|edge|blow|clear|clean|maintain|vacuum|"
            r"pressure wash|take care of|remove snow from) "
            r"(?:my|our) (?:own )?(?:yard|lawn|grass|driveway|walkway|"
            r"sidewalk|patio|deck|home|house|property|garden|hedges?|trees?)\b|"
            r"\b(?:i|we) (?:use|bought|purchased|got) "
            r"(?:it|this|one|the [a-z -]+).{0,45}\b(?:to|for) "
            r"(?:mow|cut|trim|edge|blow|clear|clean|maintain|vacuum|"
            r"pressure wash|take care of|remove snow from).{0,45}"
            r"\b(?:my|our) (?:own )?(?:yard|lawn|grass|driveway|walkway|"
            r"sidewalk|patio|deck|home|house|property|garden|hedges?|trees?)\b|"
            r"\b(?:i|we) (?:do|handle) (?:my|our) own "
            r"(?:yard work|lawn care|landscaping|snow removal|cleaning)\b",
        ),
        _label(
            "homeowner_owner_operator",
            "自有住宅 / 物业维护者",
            r"\bhome[- ]?owner\b|\bI own (?:my|a|the) "
            r"(?:home|house|property)\b|\bour own (?:home|house|property)\b",
        ),
        _label(
            "landlord_property_manager",
            "房东 / 物业管理者",
            r"\blandlord\b|\bproperty manager\b|\bmanage (?:a |our |my )?"
            r"(?:rental|properties|apartment)\b|\bmy rental propert(?:y|ies)\b",
        ),
        _label(
            "commercial_service_operator",
            "工作 / 商业服务用途",
            r"\bfor (?:my|our) (?:clients?|customers?)\b|"
            r"\b(?:use|using|used|cut|clean|mow|trim|clear|service|maintain|"
            r"dry|wash|detail|blow)"
            r".{0,50}\bfor (?:a |my |our )?(?:client|customer)\b|"
            r"\b(?:cut|clean|mow|trim|clear|service|maintain|dry|wash|detail|"
            r"blow).{0,40}\b(?:my|our) clients?['’s]*\b|"
            r"\b(?:client|customer) (?:jobs?|properties|yards?|sites?)\b|"
            r"\bat (?:a |the |my |our )?(?:client|customer)['’]s "
            r"(?:property|home|yard|site|business)\b|"
            r"\buse (?:it|this).{0,35}"
            r"(?:for|in) (?:my|our) business\b|"
            r"\b(?:use|using|used) (?:it|this|the [a-z -]+) "
            r"(?:on|for) (?:my|our) jobs?\b",
        ),
        _label(
            "institutional_site_caretaker",
            "机构 / 公共场地维护者",
            r"\b(?:church|school|campus|municipal|city|park|hoa) "
            r"(?:grounds|property|site|maintenance)\b",
        ),
        _label(
            "farm_ranch_property",
            "农场 / 牧场 / 果园维护者",
            r"\b(?:my|our) (?:farm|ranch|orchard)\b",
        ),
        _label(
            "renter_tenant",
            "租户 / 承租物业维护者",
            r"\b(?:i am|i['’]m|as) (?:a )?(?:renter|tenant)\b|"
            r"\bmy (?:rental|rented) (?:home|house|property|yard)\b",
        ),
        _label(
            "diy_maintainer",
            "明确自述的 DIY 维护者",
            r"\b(?:i am|i['’]m|as) (?:a )?(?:diyer|do[- ]it[- ]yourselfer)\b|"
            r"\bavid diy(?:er)?\b",
        ),
    ),
)

PURCHASE_RELATIONSHIP_DIMENSION = DimensionRule(
    "purchase_relationship",
    "relationship",
    "购买与共用关系",
    "谁购买、谁使用、是否在家庭或团队中共用？",
    (
        _label(
            "purchased_for_other",
            "为家庭成员购买",
            r"\b(?:bought|purchased|ordered|got) (?:it|this|one|the [a-z -]+)"
            r" for my (?:husband|wife|son|daughter|father|mother|dad|mom|parents?)\b|"
            r"\bgift(?:ed)? (?:it|this|one) to my "
            r"(?:husband|wife|son|daughter|father|mother|dad|mom|parents?)\b",
        ),
        _label(
            "received_as_gift",
            "作为礼物收到",
            r"\b(?:received|got) (?:it|this|one) as (?:a )?gift\b|"
            r"\b(?:husband|wife|son|daughter|father|mother|dad|mom) "
            r"(?:bought|gave|gifted) (?:me )?(?:it|this|one)\b",
        ),
        _label(
            "household_shared",
            "家庭成员共同使用",
            r"\b(?:my (?:husband|wife|son|daughter) and i|"
            r"(?:we|both) (?:use|share|operate) (?:it|this|the [a-z -]+)|"
            r"(?:whole|entire) family (?:uses?|shares?))\b",
        ),
        _label(
            "work_team_shared",
            "工作团队共同使用",
            r"\b(?:our|the) (?:crew|team|employees?|staff) "
            r"(?:uses?|shares?|operates?)\b|"
            r"\bshared (?:among|between) (?:the |our )?(?:crew|team|staff)\b",
        ),
    ),
)


DIMENSIONS: tuple[DimensionRule, ...] = (
    OCCUPATION_DIMENSION,
    OPERATING_RESPONSIBILITY_DIMENSION,
    PURCHASE_RELATIONSHIP_DIMENSION,
    DimensionRule(
        "life_stage_ability",
        "profile",
        "年龄、体能与健康",
        "是否明确表达年龄或影响操作的身体条件？",
        (
            _label(
                "senior",
                "年长 / 银发用户",
                r"\bsenior\b|\belderly\b|\bolder (?:person|adult|woman|man)\b|"
                r"\bin my (?:60s|70s|80s|nineties)\b",
            ),
            _label(
                "limited_strength",
                "力量 / 握力受限",
                r"\blimited strength\b|\bnot strong enough\b|\bweak (?:hands|arms|grip)\b|"
                r"\bstrength issues?\b",
            ),
            _label(
                "mobility_limit",
                "行动能力受限",
                r"\blimited mobility\b|\bwheelchair\b|\bwalking difficulty\b|"
                r"\bcan['’]t walk\b|\bdifficulty walking\b",
            ),
            _label(
                "joint_back_condition",
                "关节 / 腰背 / 肩部问题",
                r"\barthritis\b|\bbad back\b|\bback pain\b|\bshoulder pain\b|"
                r"\bknee (?:pain|problem)\b|\bjoint pain\b|\bdisab(?:led|ility)\b",
            ),
            _label(
                "retired",
                "明确退休",
                r"\b(?:i am|i['’]m|im) (?:now )?retired\b|"
                r"\bas (?:a |an )?retiree\b|"
                r"\bsince (?:i )?retir(?:ed|ing)\b|"
                r"\bafter (?:my )?retirement\b",
            ),
        ),
    ),
    DimensionRule(
        "experience_path",
        "profile",
        "经验与换机路径",
        "用户是新手、旧机换新，还是燃油转电动？",
        (
            _label(
                "gas_switcher",
                "燃油转电动",
                r"\bfrom gas\b|\breplac(?:e|ed|ement for) (?:my )?gas\b|"
                r"\bno more gas\b|\bswitched from (?:a )?gas\b|\bgas mower\b",
            ),
            _label(
                "previous_mower_owner",
                "旧机 / 前代用户",
                r"\bmy old mower\b|\bprevious mower\b|\breplaced my\b|"
                r"\bupgrade(?:d)? from\b|\bcompared to my old\b|\bused to have\b",
            ),
            _label(
                "long_time_user",
                "长期割草 / 品牌用户",
                r"\blong[- ]time (?:user|owner)\b|\bfor (?:over )?\d{1,2} years\b|"
                r"\byears of experience\b|\bbeen mowing for\b",
            ),
            _label(
                "battery_ecosystem_owner",
                "既有品牌电池生态用户",
                r"\balready (?:own|owned|had|have).{0,60}(?:ryobi|ego|batter)\b|"
                r"\bsame batter(?:y|ies)\b|\bone\+ (?:system|platform|line)\b|"
                r"\b(?:40v|56v) (?:system|platform|line)\b|"
                r"\b(?:ryobi|ego) collection\b|\bpower\s*\+ (?:system|platform|line|tools?)\b",
            ),
            _label(
                "first_electric_mower",
                "首台电动割草机",
                r"\bfirst (?:battery|electric|cordless) mower\b|"
                r"\bnew to (?:battery|electric|cordless) (?:mowers?|tools?)\b",
            ),
        ),
    ),
    DimensionRule(
        "property_context",
        "profile",
        "物业与作业责任",
        "设备用于单一家庭草坪、多处物业还是商用场地？",
        (
            _label(
                "single_home_yard",
                "家庭前 / 后院",
                r"\bmy (?:front|back) yard\b|\bmy (?:yard|lawn)\b|\bour (?:yard|lawn)\b",
            ),
            _label(
                "multiple_properties",
                "多处物业",
                r"\bmultiple (?:yards|properties|lawns)\b|\b(?:two|three|\d+) properties\b",
            ),
            _label(
                "commercial_site",
                "商用 / 公共场地",
                r"\bcommercial (?:property|site|use)\b|\bclient(?:s|['’]s)? (?:yard|lawn)\b|"
                r"\bchurch (?:yard|lawn)\b|\bschool grounds\b",
            ),
            _label(
                "rental_property",
                "出租物业",
                r"\brental (?:property|house|home)\b|\btenant(?:s)?\b",
            ),
        ),
    ),
    DimensionRule(
        "geography",
        "scenario",
        "地理与气候地区",
        "评论中是否明确写出作业所在地区？",
        (
            _label(
                "southeast",
                "美国东南 / 墨西哥湾沿岸",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:florida|georgia|alabama|mississippi|louisiana|south carolina|"
                r"north carolina|tennessee|arkansas)\b",
            ),
            _label(
                "northeast",
                "美国东北",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:new york|new jersey|pennsylvania|massachusetts|connecticut|"
                r"maine|vermont|new hampshire|rhode island)\b",
            ),
            _label(
                "midwest",
                "美国中西部",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:ohio|michigan|indiana|illinois|wisconsin|minnesota|iowa|"
                r"missouri|kansas|nebraska|north dakota|south dakota)\b",
            ),
            _label(
                "southwest",
                "美国西南 / 德州",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:texas|arizona|new mexico|nevada|oklahoma)\b",
            ),
            _label(
                "west",
                "美国西部 / 太平洋沿岸",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:california|oregon|washington|colorado|utah|idaho|montana|wyoming)\b",
            ),
            _label(
                "other_explicit_region",
                "其他明确地区",
                r"\b(?:live|living|here|yard|lawn|home|mowing) (?:in|near) "
                r"(?:canada|ontario|quebec|british columbia|hawaii|alaska)\b",
            ),
        ),
    ),
    DimensionRule(
        "grass_species",
        "scenario",
        "草种",
        "评论中是否明确写出草种？",
        (
            _label("bermuda", "Bermuda 百慕大草", r"\bbermuda grass\b|\bbermuda\b"),
            _label(
                "st_augustine",
                "St. Augustine 圣奥古斯丁草",
                r"\bst\.?\s*augustine(?: grass)?\b",
            ),
            _label("zoysia", "Zoysia 结缕草", r"\bzoysia(?: grass)?\b"),
            _label("fescue", "Fescue 羊茅", r"\bfescue(?: grass)?\b"),
            _label(
                "bluegrass",
                "Kentucky bluegrass",
                r"\bkentucky bluegrass\b|\bbluegrass\b",
            ),
            _label("ryegrass", "Ryegrass 黑麦草", r"\bryegrass\b|\brye grass\b"),
            _label("centipede", "Centipede 假俭草", r"\bcentipede grass\b"),
            _label("bahia", "Bahia 百喜草", r"\bbahia grass\b|\bbahiagrass\b"),
            _label("buffalo", "Buffalo 水牛草", r"\bbuffalo grass\b"),
        ),
    ),
    DimensionRule(
        "terrain",
        "scenario",
        "地形与障碍",
        "草坪是平地、坡地、坑洼还是障碍密集？",
        (
            _label("flat", "平坦地面", r"\bflat (?:yard|lawn|ground|terrain)\b"),
            _label(
                "slope_hill",
                "坡地 / 山丘",
                r"\bhill(?:s|side)?\b|\bslope(?:s|d)?\b|\bincline\b|\bsteep\b",
            ),
            _label(
                "rough_uneven",
                "崎岖 / 坑洼地面",
                r"\brough (?:yard|lawn|terrain|ground)\b|\buneven ground\b|"
                r"\bbumpy\b|\bholes? in (?:the )?(?:yard|lawn)\b",
            ),
            _label(
                "obstacles_tight",
                "树木 / 花坛 / 狭窄障碍",
                r"\baround (?:trees?|flower beds?|obstacles?|fence)\b|"
                r"\btight (?:spots?|spaces?|corners?)\b|\bmany trees\b",
            ),
        ),
    ),
    DimensionRule(
        "grass_condition",
        "scenario",
        "草况与负载",
        "割草时面对何种草况与负载？",
        (
            _label(
                "thick",
                "厚草 / 浓密草",
                r"\bthick grass\b|\bdense grass\b|\bheavy grass\b",
            ),
            _label(
                "tall_overgrown",
                "高草 / 过度生长",
                r"\btall grass\b|\bovergrown\b|\bhigh grass\b",
            ),
            _label(
                "wet_damp",
                "湿草 / 雨后",
                r"\bwet grass\b|\bdamp grass\b|\bafter (?:the )?rain\b",
            ),
            _label(
                "dry_dusty",
                "干燥 / 扬尘",
                r"\bdry grass\b|\bdusty\b|\bdry conditions?\b",
            ),
            _label(
                "leaves_debris",
                "落叶 / 碎屑混合",
                r"\bleaves\b|\bleaf cover\b|\btwigs?\b|\bdebris\b",
            ),
            _label("weeds", "杂草", r"\bweeds?\b|\bweedy\b"),
        ),
    ),
    DimensionRule(
        "mowing_cadence",
        "scenario",
        "割草频率",
        "用户多久割一次草？",
        (
            _label(
                "multiple_weekly",
                "每周多次",
                r"\b(?:twice|three times|\d+ times) (?:a|per) week\b",
            ),
            _label(
                "weekly",
                "每周 / 每周末",
                r"\bonce (?:a|per) week\b|\bevery week(?:end)?\b|\bweekly\b",
            ),
            _label(
                "biweekly",
                "每两周",
                r"\bevery (?:other|two) weeks?\b|\bbiweekly\b",
            ),
            _label(
                "irregular_overgrown",
                "低频 / 长草后集中处理",
                r"\bhadn['’]t mowed\b|\bweeks? since (?:i )?mowed\b|"
                r"\blet (?:the )?(?:grass|lawn) grow\b",
            ),
        ),
    ),
    DimensionRule(
        "time_of_day",
        "scenario",
        "割草时段",
        "评论中是否明确写出割草时段？",
        (
            _label("morning", "早晨", r"\b(?:mow|mowing|cut).{0,35}\bmorning\b|\bmorning mow\b"),
            _label("evening", "傍晚 / 夜间", r"\b(?:mow|mowing|cut).{0,35}\b(?:evening|night)\b|\bevening mow\b"),
            _label("weekend", "周末", r"\bweekend\b|\bsaturday\b|\bsunday\b"),
        ),
    ),
    DimensionRule(
        "charging_workflow",
        "scenario",
        "充电与换电流程",
        "用户如何组织充电、换电与多电池作业？",
        (
            _label(
                "single_charge_finish",
                "一次充电完成",
                r"\b(?:finish|finished|mow|mowed|cut).{0,80}\b(?:single|one|a) charge\b|"
                r"\bon one charge\b|\bwithout recharg(?:e|ing)\b",
            ),
            _label(
                "swap_batteries",
                "中途换电",
                r"\bswap(?:ped|ping)? (?:out )?(?:the )?batter(?:y|ies)\b|"
                r"\bchange(?:d|ing)? batter(?:y|ies)\b|\bsecond battery\b",
            ),
            _label(
                "recharge_mid_job",
                "中途充电 / 等待",
                r"\b(?:stop|wait|break).{0,60}\bcharg(?:e|ed|ing)\b|"
                r"\brecharg(?:e|ed|ing).{0,60}\b(?:finish|mow|job|yard)\b",
            ),
            _label(
                "multi_battery",
                "多电池协同",
                r"\b(?:two|three|four|2|3|4) batter(?:y|ies)\b|"
                r"\bdual batter(?:y|ies)\b|\bboth batteries\b",
            ),
            _label(
                "multi_charger",
                "多口 / 多充电器",
                r"\bdual charger\b|\bmulti[- ]port charger\b|\btwo chargers?\b|"
                r"\bcharge both\b|\bsimultaneous(?:ly)? charg",
            ),
        ),
    ),
    DimensionRule(
        "job_completion",
        "scenario",
        "整院完成度",
        "当前电量能否一次完成计划中的草坪？",
        (
            _label(
                "completed_with_margin",
                "完成且有余量",
                r"\b(?:finished|mowed|cut) (?:the|my|our) (?:whole|entire) "
                r"(?:yard|lawn).{0,80}\b(?:left|remaining|bars?|charge)\b|"
                r"\bstill ha(?:d|ve).{0,30}\b(?:charge|bars?|battery)\b",
            ),
            _label(
                "completed",
                "一次完成",
                r"\b(?:finished|mowed|cut) (?:the|my|our) (?:whole|entire) "
                r"(?:yard|lawn)\b|\b(?:finish|finished) (?:the )?(?:job|yard|lawn)\b",
            ),
            _label(
                "not_completed",
                "无法一次完成",
                r"\bcouldn['’]t finish (?:the )?(?:yard|lawn|job)\b|"
                r"\bcan['’]t finish (?:the )?(?:yard|lawn|job)\b|"
                r"\bnot enough (?:battery|charge|runtime).{0,50}\b(?:yard|lawn|finish)\b|"
                r"\b(?:battery|charge).{0,60}\b(?:died|ran out).{0,40}\b(?:before|halfway)\b",
            ),
        ),
    ),
    DimensionRule(
        "clipping_mode",
        "scenario",
        "草屑处理方式",
        "用户选择集草、碎草覆盖还是侧排？",
        (
            _label("bagging", "集草", r"\bbag(?:ged|ging|ger)?\b|\bgrass bag\b"),
            _label("mulching", "碎草覆盖", r"\bmulch(?:ed|ing)?\b"),
            _label("side_discharge", "侧排", r"\bside discharge\b|\bdischarge chute\b"),
            _label("rear_discharge", "后排", r"\brear discharge\b"),
        ),
    ),
    DimensionRule(
        "storage",
        "scenario",
        "收纳与运输",
        "设备如何折叠、直立、存放或搬运？",
        (
            _label(
                "fold_vertical",
                "折叠 / 直立收纳",
                r"\bfold(?:s|ed|ing)?\b|\bvertical stor(?:e|age)\b|\bstor(?:e|ed) upright\b",
            ),
            _label("garage", "车库存放", r"\bgarage\b"),
            _label("shed", "工具棚存放", r"\bshed\b"),
            _label(
                "outside",
                "室外覆盖存放",
                r"\bstor(?:e|ed) (?:it )?outside\b|\boutside under (?:a )?cover\b",
            ),
            _label(
                "transport_vehicle",
                "车辆运输 / 上下车",
                r"\bload(?:ed|ing)? (?:it|the mower) (?:into|in|onto)\b|"
                r"\btransport(?:ed|ing)?\b|\btruck bed\b|\btrunk\b",
            ),
        ),
    ),
    DimensionRule(
        "weather",
        "scenario",
        "天气与温度",
        "哪些天气或温度条件进入了评论？",
        (
            _label("heat", "高温 / 炎热", r"\bhot weather\b|\bhot day\b|\bheat\b|\bhigh temperatures?\b"),
            _label("cold", "寒冷 / 低温", r"\bcold weather\b|\bcold day\b|\bfreezing\b|\blow temperatures?\b"),
            _label("rain", "下雨 / 雨后", r"\brain(?:ed|ing|y)?\b|\bafter (?:the )?rain\b"),
            _label("humidity", "潮湿 / 高湿", r"\bhumid(?:ity)?\b|\bdamp conditions?\b"),
        ),
    ),
)

NUMERIC_DIMENSION_IDS: tuple[str, ...] = (
    "explicit_age",
    "yard_size",
    "cut_height",
    "task_duration",
    "battery_spec",
    "battery_capacity",
    "battery_count",
)


ASPECT_RULES: tuple[LabelRule, ...] = (
    _label("cut_quality", "切割质量", r"\bcut(?:ting)? quality\b|\bclean cut\b|\buneven cut\b|\bscalp|\bmiss(?:ed|es) spots\b|\bcut the grass\b"),
    _label("power", "动力与负载能力", r"\bpower(?:ful)?\b|\btorque\b|\bthick grass\b|\btall grass\b|\bstall(?:s|ed|ing)?\b"),
    _label("runtime", "续航与整院完成", r"\brun\s*time\b|\bbattery life\b|\blast(?:s|ed)? \d+ (?:minutes?|hours?)\b|\bfinish (?:the )?(?:yard|lawn)\b"),
    _label("charging", "充电与换电", r"\bcharg(?:e|ed|er|ing)\b|\bswap(?:ped|ping)? batter"),
    _label("self_propel", "自走与速度控制", r"\bself[- ]?propel|\bdrive speed\b|\bpace\b|\bspeed control\b"),
    _label(
        "handling",
        "重量、转向与操控",
        r"\bweight\b|\bheavy\b|\blightweight\b|\bmaneuver|"
        r"\bturning radius\b|\bturn(?:s|ed|ing)? (?:the )?mower\b|"
        r"\bturn(?:s|ed|ing)? (?:around|corners?)\b|\bpush(?:ing)?\b",
    ),
    _label("noise", "噪音与振动", r"\bnoise\b|\bnoisy\b|\bquiet\b|\bvibrat"),
    _label("bagging", "集草体验", r"\bbag(?:ged|ging|ger)?\b|\bgrass bag\b"),
    _label("mulching_discharge", "碎草与排草", r"\bmulch(?:ed|ing)?\b|\bside discharge\b|\bdischarge chute\b"),
    _label("height_adjustment", "割高与调节", r"\bcut(?:ting)? height\b|\bheight adjust|\bheight setting\b|\bdeck height\b"),
    _label("assembly", "装配与开箱", r"\bassembl|\bout of the box\b|\bsetup\b|\bput together\b"),
    _label(
        "storage",
        "折叠、收纳与运输",
        r"\bfold(?:s|ed|ing)?\b|\bstor(?:ed|age|ing)\b|"
        r"\bstore (?:it|the mower|this mower|upright|vertically)\b|"
        r"\btransport",
    ),
    _label("battery_charger", "电池与充电器可靠性", r"\bbatter(?:y|ies)\b|\bcharger\b"),
    _label("reliability", "可靠性与耐久", r"\breliab|\bdurab|\bstopped working\b|\bquit working\b|\bbroke\b|\bbroken\b|\bfailed\b|\bdead\b"),
    _label("starting", "启动与开机", r"\bstart(?:s|ed|ing)?\b|\bwon['’]t start\b|\bpush button\b"),
    _label("maintenance_gasfree", "免燃油与低维护", r"\bno (?:more )?gas\b|\bno oil\b|\bmaintenance\b|\bcarburetor\b|\bspark plug\b|\bfumes\b"),
    _label("value", "价格与价值", r"\bprice\b|\bcost\b|\bvalue\b|\bexpensive\b|\bcheap\b|\bworth\b"),
    _label("safety", "安全", r"\bsafe(?:ty)?\b|\bunsafe\b|\bdanger(?:ous)?\b|\binjur|\bfire\b|\bsmoke\b"),
    _label("service", "售后与保修", r"\bcustomer service\b|\bsupport\b|\bwarrant(?:y|ies)\b|\breturn(?:ed|ing)?\b|\breplacement\b"),
    _label(
        "ecosystem",
        "电池生态与兼容",
        r"\bsame batter(?:y|ies)\b|\bcompatible\b|\bone\+\b|"
        r"\b(?:40v|56v) (?:system|line|platform)\b|"
        r"\bpower\s*\+ (?:system|line|platform|tools?)\b|"
        r"\balready (?:have|own|had).{0,40}batter",
    ),
    _label("smart_connectivity", "智能与连接", r"\bsmart\b|\bapp\b|\bbluetooth\b|\bconnect(?:ed|ivity)\b|\bautomatic\b|\bsensor\b|\bfirmware\b"),
)


def _dimension_from_mower(dimension_id: str) -> DimensionRule:
    return next(
        dimension
        for dimension in DIMENSIONS
        if dimension.dimension_id == dimension_id
    )


BLOWER_DIMENSIONS: tuple[DimensionRule, ...] = (
    OCCUPATION_DIMENSION,
    OPERATING_RESPONSIBILITY_DIMENSION,
    PURCHASE_RELATIONSHIP_DIMENSION,
    _dimension_from_mower("life_stage_ability"),
    DimensionRule(
        "experience_path",
        "profile",
        "经验与换机路径",
        "用户是首次购买、旧机换新、燃油转电动还是生态复用？",
        (
            _label(
                "gas_switcher",
                "燃油吹风机转电动",
                r"\bfrom (?:a )?gas (?:blower|unit)\b|"
                r"\breplac(?:e|ed|ing).{0,35}\bgas (?:blower|unit)\b|"
                r"\bno more gas\b|\bswitched from gas\b",
            ),
            _label(
                "corded_switcher",
                "有线转无线",
                r"\bfrom (?:a )?corded (?:blower|unit)\b|"
                r"\breplac(?:e|ed|ing).{0,35}\bcorded\b|"
                r"\bno more (?:cord|extension cord)\b",
            ),
            _label(
                "previous_blower_owner",
                "旧机 / 前代用户",
                r"\bmy old blower\b|\bprevious blower\b|"
                r"\bupgrade(?:d)? from\b|\bcompared to my old\b|"
                r"\bused to have\b",
            ),
            _label(
                "battery_ecosystem_owner",
                "既有品牌电池生态用户",
                r"\balready (?:own|owned|had|have).{0,60}(?:ryobi|ego|batter)\b|"
                r"\bsame batter(?:y|ies)\b|\b(?:40v|56v) "
                r"(?:system|platform|line)\b|\bpower\s*\+ "
                r"(?:system|platform|line|tools?)\b",
            ),
            _label(
                "first_battery_blower",
                "首台电池吹风机",
                r"\bfirst (?:battery|electric|cordless) blower\b|"
                r"\bnew to (?:battery|electric|cordless) (?:blowers?|tools?)\b",
            ),
        ),
    ),
    DimensionRule(
        "property_context",
        "profile",
        "物业与清理责任",
        "设备用于单一家庭物业、多处物业还是商用场地？",
        (
            _label(
                "single_home_yard",
                "家庭前 / 后院",
                r"\bmy (?:front|back) yard\b|\bmy "
                r"(?:yard|driveway|property)\b|\bour "
                r"(?:yard|driveway|property)\b",
            ),
            _label(
                "multiple_properties",
                "多处物业",
                r"\bmultiple (?:yards|properties|sites)\b|"
                r"\b(?:two|three|\d+) properties\b",
            ),
            _label(
                "commercial_site",
                "商用 / 公共场地",
                r"\bcommercial (?:property|site|use)\b|"
                r"\bclient(?:s|['’]s)? (?:yard|property|site)\b|"
                r"\bschool grounds\b|\bparking lot\b",
            ),
            _label(
                "rental_property",
                "出租物业",
                r"\brental (?:property|house|home)\b|\btenant(?:s)?\b",
            ),
        ),
    ),
    _dimension_from_mower("geography"),
    DimensionRule(
        "work_area",
        "scenario",
        "作业区域",
        "吹风机主要清理哪些区域？",
        (
            _label(
                "lawn_yard",
                "草坪 / 院子",
                r"\b(?:lawn|yard|grass)\b",
            ),
            _label(
                "driveway",
                "车道",
                r"\bdriveway\b|\bparking (?:area|lot)\b",
            ),
            _label(
                "sidewalk_path",
                "人行道 / 小径",
                r"\bsidewalk\b|\bwalkway\b|\bpath(?:way)?\b",
            ),
            _label(
                "deck_patio",
                "露台 / 平台",
                r"\bdeck\b|\bpatio\b|\bporch\b",
            ),
            _label(
                "garage_workshop",
                "车库 / 工坊",
                r"\bgarage\b|\bshop floor\b|\bworkshop\b",
            ),
            _label(
                "gutter_roof",
                "排水沟 / 屋顶",
                r"\bgutter(?:s)?\b|\broof\b",
            ),
            _label(
                "commercial_site",
                "商用 / 公共场地",
                r"\bcommercial (?:property|site|use)\b|"
                r"\bclient(?:s|['’]s)? (?:yard|property)\b|"
                r"\bschool grounds\b|\bparking lot\b",
            ),
        ),
    ),
    DimensionRule(
        "debris_type",
        "scenario",
        "清理物类型",
        "用户实际要移动什么？",
        (
            _label(
                "dry_leaves",
                "干叶 / 松散落叶",
                r"\bdry leaves?\b|\blight leaves?\b",
            ),
            _label(
                "wet_leaves",
                "湿叶 / 成堆落叶",
                r"\bwet leaves?\b|\bdamp leaves?\b|\bpiles? of leaves?\b",
            ),
            _label(
                "grass_clippings",
                "草屑",
                r"\bgrass clippings?\b|\bcut grass\b",
            ),
            _label(
                "pine_needles",
                "松针",
                r"\bpine needles?\b|\bpine straw\b",
            ),
            _label(
                "acorns_twigs",
                "橡果 / 枝条",
                r"\bacorns?\b|\btwigs?\b|\bsmall branches?\b",
            ),
            _label(
                "dust_sawdust",
                "灰尘 / 木屑",
                r"\bdust\b|\bsawdust\b|\bshop debris\b",
            ),
            _label(
                "gravel_stones",
                "砂石 / 小石子",
                r"\bgravel\b|\bsmall (?:rocks?|stones?)\b|\bpebbles?\b",
            ),
            _label(
                "light_snow",
                "轻雪",
                r"\blight snow\b|\bpowdery snow\b|\bsnow off\b",
            ),
        ),
    ),
    DimensionRule(
        "debris_condition",
        "scenario",
        "负载与干湿状态",
        "清理物是轻载、厚堆、湿重还是粘附状态？",
        (
            _label(
                "light_loose",
                "轻量 / 松散",
                r"\blight (?:debris|leaves|clippings)\b|"
                r"\bloose (?:debris|leaves)\b",
            ),
            _label(
                "heavy_pile",
                "厚堆 / 大量",
                r"\bheavy (?:debris|leaves)\b|\bthick (?:pile|layer)\b|"
                r"\blarge piles?\b|\btons? of leaves\b",
            ),
            _label(
                "wet_damp",
                "湿重 / 雨后",
                r"\bwet (?:leaves|debris|grass)\b|"
                r"\bdamp (?:leaves|debris)\b|\bafter (?:the )?rain\b",
            ),
            _label(
                "stuck_matted",
                "粘附 / 板结",
                r"\bstuck (?:to|on)\b|\bmatted (?:leaves|grass)\b|"
                r"\bpacked (?:leaves|debris)\b",
            ),
        ),
    ),
    DimensionRule(
        "cleanup_season",
        "scenario",
        "季节与任务节奏",
        "吹风任务发生在秋季集中清理还是全年日常维护？",
        (
            _label(
                "fall_cleanup",
                "秋季落叶集中清理",
                r"\bfall cleanup\b|\bautumn\b|\bleaf season\b",
            ),
            _label(
                "spring_cleanup",
                "春季清理",
                r"\bspring cleanup\b|\bspring cleaning\b",
            ),
            _label(
                "after_mowing",
                "割草后清理",
                r"\bafter (?:i |we )?mow(?:ing|ed)?\b|"
                r"\bafter cutting (?:the )?grass\b",
            ),
            _label(
                "year_round",
                "全年日常维护",
                r"\byear[- ]round\b|\ball year\b|\bevery week(?:end)?\b",
            ),
        ),
    ),
    DimensionRule(
        "airflow_workflow",
        "scenario",
        "风量与操作流程",
        "用户如何在低速、变速和 Turbo 之间控制风量？",
        (
            _label(
                "variable_speed",
                "连续变速",
                r"\bvariable speed\b|\bspeed control\b|\btrigger control\b",
            ),
            _label(
                "low_speed_precision",
                "低速精细清理",
                r"\blow speed\b|\blower setting\b|\bdelicate areas?\b|"
                r"\bflower beds?\b",
            ),
            _label(
                "turbo_boost",
                "Turbo / Boost 强吹",
                r"\bturbo\b|\bboost (?:button|mode)\b|\bmax power\b",
            ),
            _label(
                "cruise_lock",
                "定速 / 锁定",
                r"\bcruise control\b|\bspeed lock\b|\block(?:ed)? trigger\b",
            ),
        ),
    ),
    DimensionRule(
        "form_factor",
        "scenario",
        "形态与附件",
        "用户使用手持、背负、吸吹或不同喷嘴组合？",
        (
            _label("handheld", "手持式", r"\bhandheld\b|\bhand held\b"),
            _label("backpack", "背负式", r"\bbackpack blower\b|\bback pack\b"),
            _label(
                "vacuum_mulcher",
                "吸叶 / 碎叶",
                r"\bleaf vacuum\b|\bvacuum mode\b|\bmulcher\b|\bmulching leaves\b",
            ),
            _label(
                "concentrator_nozzle",
                "聚风 / 扁嘴",
                r"\bconcentrator\b|\bflat nozzle\b|\btapered nozzle\b",
            ),
            _label(
                "extension_gutter",
                "延长管 / 排水沟附件",
                r"\bextension tube\b|\bgutter attachment\b|\bgutter kit\b",
            ),
        ),
    ),
    DimensionRule(
        "charging_workflow",
        "scenario",
        "充电与换电流程",
        "用户如何组织充电、换电与多电池连续清理？",
        (
            _label(
                "single_charge_finish",
                "一次充电完成",
                r"\b(?:finish|finished|blow|blew|clean).{0,80}"
                r"\b(?:single|one|a) charge\b|\bon one charge\b|"
                r"\bwithout recharg(?:e|ing)\b",
            ),
            _label(
                "swap_batteries",
                "中途换电",
                r"\bswap(?:ped|ping)? (?:out )?(?:the )?batter(?:y|ies)\b|"
                r"\bsecond battery\b|\bchange(?:d|ing)? batter(?:y|ies)\b",
            ),
            _label(
                "recharge_mid_job",
                "中途充电 / 等待",
                r"\b(?:stop|wait|break).{0,60}\bcharg(?:e|ed|ing)\b|"
                r"\brecharg(?:e|ed|ing).{0,60}\b(?:finish|job|yard|cleanup)\b",
            ),
            _label(
                "multi_battery",
                "多电池协同",
                r"\b(?:two|three|four|2|3|4) batter(?:y|ies)\b|"
                r"\bdual batter(?:y|ies)\b|\bboth batteries\b",
            ),
        ),
    ),
    DimensionRule(
        "job_completion",
        "scenario",
        "清理任务完成度",
        "当前电量能否一次完成计划中的清理任务？",
        (
            _label(
                "completed_with_margin",
                "完成且有余量",
                r"\b(?:finished|cleaned|blew) (?:the|my|our) "
                r"(?:whole|entire) (?:yard|driveway|property).{0,80}"
                r"\b(?:left|remaining|bars?|charge)\b|"
                r"\bstill ha(?:d|ve).{0,30}\b(?:charge|bars?|battery)\b",
            ),
            _label(
                "completed",
                "一次完成",
                r"\b(?:finished|cleaned|blew) (?:the|my|our) "
                r"(?:whole|entire) (?:yard|driveway|property)\b|"
                r"\b(?:finish|finished) (?:the )?(?:job|yard|cleanup)\b",
            ),
            _label(
                "not_completed",
                "无法一次完成",
                r"\bcouldn['’]t finish (?:the )?(?:yard|job|cleanup)\b|"
                r"\bcan['’]t finish (?:the )?(?:yard|job|cleanup)\b|"
                r"\b(?:battery|charge).{0,60}\b(?:died|ran out)"
                r".{0,40}\b(?:before|halfway)\b",
            ),
        ),
    ),
    DimensionRule(
        "storage",
        "scenario",
        "收纳与运输",
        "吹风机如何悬挂、存放或运输？",
        (
            _label(
                "hang_wall",
                "壁挂 / 悬挂",
                r"\bhang(?:s|ing)? (?:it|the blower|on the wall)\b|"
                r"\bwall mount(?:ed)?\b",
            ),
            _label("garage", "车库存放", r"\bgarage\b"),
            _label("shed", "工具棚存放", r"\bshed\b"),
            _label(
                "outside",
                "室外覆盖存放",
                r"\bstor(?:e|ed) (?:it )?outside\b|"
                r"\boutside under (?:a )?cover\b",
            ),
            _label(
                "transport_vehicle",
                "车辆运输 / 商用携带",
                r"\bload(?:ed|ing)? (?:it|the blower) "
                r"(?:into|in|onto)\b|\btransport(?:ed|ing)?\b|"
                r"\btruck bed\b|\btrunk\b|\btrailer\b",
            ),
        ),
    ),
    DimensionRule(
        "time_of_day",
        "scenario",
        "清理时段",
        "评论中是否明确写出吹扫或清理时段？",
        (
            _label(
                "morning",
                "早晨",
                r"\b(?:blow|blowing|clean|cleanup).{0,35}\bmorning\b|"
                r"\bmorning (?:cleanup|blowing)\b",
            ),
            _label(
                "evening",
                "傍晚 / 夜间",
                r"\b(?:blow|blowing|clean|cleanup).{0,35}"
                r"\b(?:evening|night)\b|"
                r"\b(?:evening|night) (?:cleanup|blowing)\b",
            ),
            _label(
                "weekend",
                "周末",
                r"\bweekend\b|\bsaturday\b|\bsunday\b",
            ),
        ),
    ),
    _dimension_from_mower("weather"),
)

BLOWER_NUMERIC_DIMENSION_IDS: tuple[str, ...] = (
    "explicit_age",
    "yard_size",
    "task_duration",
    "battery_spec",
    "battery_capacity",
    "battery_count",
)

BLOWER_ASPECT_RULES: tuple[LabelRule, ...] = (
    _label(
        "blowing_power",
        "吹力与重载能力",
        r"\bblowing power\b|\bair power\b|\bpowerful\b|\bunderpowered\b|"
        r"\bmove(?:s|d)? (?:wet )?leaves\b|\bwet leaves\b|\bcfm\b|\bmph\b",
    ),
    _label(
        "airflow_control",
        "风量控制与精细度",
        r"\bvariable speed\b|\bspeed control\b|\btrigger\b|\bturbo\b|"
        r"\bboost\b|\bcruise control\b|\btoo much power\b",
    ),
    _label(
        "runtime",
        "续航与任务完成",
        r"\brun\s*time\b|\bbattery life\b|\blast(?:s|ed)? "
        r"\d+ (?:minutes?|hours?)\b|\bfinish (?:the )?(?:yard|job|driveway)\b",
    ),
    _label(
        "charging",
        "充电与换电",
        r"\bcharg(?:e|ed|er|ing)\b|\bswap(?:ped|ping)? batter",
    ),
    _label(
        "weight_balance",
        "重量、平衡与疲劳",
        r"\bweight\b|\bheavy\b|\blightweight\b|\bbalance(?:d)?\b|"
        r"\barm (?:tired|fatigue|sore)\b|\bfatigu(?:e|ing)\b",
    ),
    _label(
        "ergonomics",
        "握持、背带与操控",
        r"\bergonomic\b|\bhandle\b|\bgrip\b|\bstrap\b|\bharness\b|"
        r"\bone[- ]handed\b|\bcomfortable\b",
    ),
    _label(
        "noise",
        "噪音与振动",
        r"\bnoise\b|\bnoisy\b|\bquiet\b|\bvibrat",
    ),
    _label(
        "nozzle_attachment",
        "喷嘴、管路与附件",
        r"\bnozzle\b|\btube\b|\battachment\b|\bgutter kit\b|"
        r"\bconcentrator\b|\bvacuum mode\b|\bmulcher\b",
    ),
    _label(
        "wet_debris",
        "湿叶与重碎屑表现",
        r"\bwet leaves?\b|\bdamp leaves?\b|\bheavy debris\b|"
        r"\bpine needles?\b|\bacorns?\b|\bstuck leaves?\b",
    ),
    _label(
        "dust_control",
        "扬尘与精细清理",
        r"\bdust\b|\bdusty\b|\bblows? (?:dust|mulch|gravel) everywhere\b|"
        r"\bflower beds?\b|\bdelicate\b",
    ),
    _label(
        "assembly",
        "装配与开箱",
        r"\bassembl|\bout of the box\b|\bsetup\b|\bput together\b",
    ),
    _label(
        "storage",
        "收纳与运输",
        r"\bstor(?:ed|age|ing)\b|\bhang(?:s|ing)?\b|\bgarage\b|"
        r"\btransport\b",
    ),
    _label(
        "battery_charger",
        "电池与充电器可靠性",
        r"\bbatter(?:y|ies)\b|\bcharger\b",
    ),
    _label(
        "reliability",
        "可靠性与耐久",
        r"\breliab|\bdurab|\bstopped working\b|\bquit working\b|"
        r"\bbroke\b|\bbroken\b|\bfailed\b|\bdead\b",
    ),
    _label(
        "starting",
        "启动与开机",
        r"\b(?:won['’]t|wouldn['’]t|doesn['’]t|didn['’]t|can['’]t|cannot|"
        r"would not|does not|did not|will not) start\b|"
        r"\b(?:hard|difficult|easy) to start\b|"
        r"\bstopped starting\b|\bpush[- ]button start\b|"
        r"\b(?:blower|unit|tool|machine|motor).{0,45}"
        r"\bstart(?:s|ed|ing)?\b|"
        r"\bstart(?:s|ed|ing)?\b.{0,45}"
        r"\b(?:blower|unit|tool|machine|motor)\b",
    ),
    _label(
        "maintenance_gasfree",
        "免燃油与低维护",
        r"\bno (?:more )?gas\b|\bno oil\b|\bmaintenance\b|"
        r"\bcarburetor\b|\bfumes\b",
    ),
    _label(
        "value",
        "价格与价值",
        r"\bprice\b|\bcost\b|\bvalue\b|\bexpensive\b|\bcheap\b|\bworth\b",
    ),
    _label(
        "safety",
        "安全",
        r"\bsafe(?:ty)?\b|\bunsafe\b|\bdanger(?:ous)?\b|\binjur|"
        r"\bfire\b|\bsmoke\b|\beye protection\b|\bhearing protection\b",
    ),
    _label(
        "service",
        "售后与保修",
        r"\bcustomer service\b|\bsupport\b|\bwarrant(?:y|ies)\b|"
        r"\breturn(?:ed|ing)?\b|\breplacement\b",
    ),
    _label(
        "ecosystem",
        "电池生态与兼容",
        r"\bsame batter(?:y|ies)\b|\bcompatible\b|\bone\+\b|"
        r"\b(?:40v|56v) (?:system|line|platform)\b|"
        r"\bpower\s*\+ (?:system|line|platform|tools?)\b|"
        r"\balready (?:have|own|had).{0,40}batter",
    ),
    _label(
        "smart_connectivity",
        "智能与连接",
        r"\bsmart\b|\bapp\b|\bbluetooth\b|\bconnect(?:ed|ivity)\b|"
        r"\bautomatic\b|\bsensor\b|\bfirmware\b",
    ),
)


def _rules_by_id(
    rules: Iterable[LabelRule], *labels: str
) -> tuple[LabelRule, ...]:
    by_id = {rule.label: rule for rule in rules}
    return tuple(by_id[label] for label in labels if label in by_id)


def _dimensions_by_id(
    dimensions: Iterable[DimensionRule], *dimension_ids: str
) -> tuple[DimensionRule, ...]:
    by_id = {dimension.dimension_id: dimension for dimension in dimensions}
    return tuple(
        by_id[dimension_id]
        for dimension_id in dimension_ids
        if dimension_id in by_id
    )


_GENERIC_EXPERIENCE_DIMENSION = DimensionRule(
    "experience_path",
    "profile",
    "经验与换机路径",
    "用户是首次进入该品类、旧设备换新、燃油 / 有线转电动，还是复用既有电池生态？",
    (
        _label(
            "gas_switcher",
            "燃油设备转电动",
            r"\bfrom (?:a )?gas (?:unit|tool|machine|equipment|pressure washer)\b|"
            r"\breplac(?:e|ed|ing).{0,35}\bgas (?:unit|tool|machine|equipment|pressure washer)\b|"
            r"\bno more gas\b|\bswitched from gas\b",
        ),
        _label(
            "corded_switcher",
            "有线转无线",
            r"\bfrom (?:a )?corded (?:unit|tool|machine|equipment)\b|"
            r"\breplac(?:e|ed|ing).{0,35}\bcorded\b|"
            r"\bno more (?:cord|extension cord)\b",
        ),
        _label(
            "previous_product_owner",
            "旧设备 / 前代用户",
            r"\bmy old (?:unit|tool|machine|equipment|pressure washer)\b|"
            r"\bprevious (?:unit|tool|machine|model|pressure washer)\b|"
            r"\bupgrade(?:d)? from\b|\bcompared to my old\b|"
            r"\bused to have\b",
        ),
        _label(
            "battery_ecosystem_owner",
            "既有品牌电池生态用户",
            r"\balready (?:own|owned|had|have).{0,60}(?:ryobi|ego|batter)\b|"
            r"\bsame batter(?:y|ies)\b|\b(?:18v|20v|40v|56v) "
            r"(?:system|platform|line)\b|\bpower\s*\+ "
            r"(?:system|platform|line|tools?)\b",
        ),
        _label(
            "first_time_owner",
            "首次进入该品类 / 电池平台",
            r"\bfirst (?:battery|electric|cordless) "
            r"(?:unit|tool|machine|pressure washer)\b|"
            r"\bnew to (?:battery|electric|cordless) "
            r"(?:equipment|tools?|pressure washers?)\b",
        ),
    ),
)

_GENERIC_CHARGING_DIMENSION = DimensionRule(
    "charging_workflow",
    "scenario",
    "充电与换电流程",
    "用户如何组织充电、换电与多电池连续作业？",
    (
        _label(
            "single_charge_finish",
            "一次充电完成",
            r"\b(?:finish|finished|complete|completed|use|used).{0,80}"
            r"\b(?:single|one|a) charge\b|\bon one charge\b|"
            r"\bwithout recharg(?:e|ing)\b",
        ),
        _label(
            "swap_batteries",
            "中途换电",
            r"\bswap(?:ped|ping)? (?:out )?(?:the )?batter(?:y|ies)\b|"
            r"\bsecond battery\b|\bchange(?:d|ing)? batter(?:y|ies)\b",
        ),
        _label(
            "recharge_mid_job",
            "中途充电 / 等待",
            r"\b(?:stop|wait|break).{0,60}\bcharg(?:e|ed|ing)\b|"
            r"\brecharg(?:e|ed|ing).{0,60}\b(?:finish|job|task|work)\b",
        ),
        _label(
            "multi_battery",
            "多电池协同",
            r"\b(?:two|three|four|2|3|4) batter(?:y|ies)\b|"
            r"\bdual batter(?:y|ies)\b|\bboth batteries\b",
        ),
    ),
)

_GENERIC_JOB_COMPLETION_DIMENSION = DimensionRule(
    "job_completion",
    "scenario",
    "作业完成度",
    "当前能源、供给与性能能否一次完成计划任务？",
    (
        _label(
            "completed_with_margin",
            "完成且有余量",
            r"\b(?:finished|completed) (?:the|my|our) "
            r"(?:whole|entire) (?:job|task|work|project).{0,80}"
            r"\b(?:left|remaining|bars?|charge)\b|"
            r"\bstill ha(?:d|ve).{0,30}\b(?:charge|bars?|battery)\b",
        ),
        _label(
            "completed",
            "一次完成",
            r"\b(?:finish|finished|complete|completed) "
            r"(?:the )?(?:job|task|work|project)\b",
        ),
        _label(
            "not_completed",
            "无法一次完成",
            r"\b(?:couldn['’]t|can['’]t|cannot) "
            r"(?:finish|complete) (?:the )?(?:job|task|work|project)\b|"
            r"\b(?:battery|charge).{0,60}\b(?:died|ran out)"
            r".{0,40}\b(?:before|halfway)\b",
        ),
    ),
)

_GENERIC_STORAGE_DIMENSION = DimensionRule(
    "storage",
    "scenario",
    "收纳与运输",
    "设备如何存放、搬运或运输？",
    (
        _label(
            "hang_wall",
            "壁挂 / 悬挂",
            r"\bhang(?:s|ing)? (?:it|the unit|the tool|on the wall)\b|"
            r"\bwall mount(?:ed)?\b",
        ),
        _label("garage", "车库存放", r"\bgarage\b"),
        _label("shed", "工具棚存放", r"\bshed\b"),
        _label(
            "outside",
            "室外覆盖存放",
            r"\bstor(?:e|ed) (?:it )?outside\b|"
            r"\boutside under (?:a )?cover\b",
        ),
        _label(
            "transport_vehicle",
            "车辆运输 / 商用携带",
            r"\bload(?:ed|ing)? (?:it|the unit|the tool|the machine) "
            r"(?:into|in|onto)\b|\btransport(?:ed|ing)?\b|"
            r"\btruck bed\b|\btrunk\b|\btrailer\b",
        ),
    ),
)

_GENERIC_TIME_OF_DAY_DIMENSION = DimensionRule(
    "time_of_day",
    "scenario",
    "作业时段",
    "评论中是否明确写出作业时段？",
    (
        _label(
            "morning",
            "早晨",
            r"\b(?:use|using|work|working|clean|cleaning).{0,35}\bmorning\b|"
            r"\bmorning (?:work|task|cleaning)\b",
        ),
        _label(
            "evening",
            "傍晚 / 夜间",
            r"\b(?:use|using|work|working|clean|cleaning).{0,35}"
            r"\b(?:evening|night)\b|"
            r"\b(?:evening|night) (?:work|task|cleaning)\b",
        ),
        _label("weekend", "周末", r"\bweekend\b|\bsaturday\b|\bsunday\b"),
    ),
)

_GENERIC_COMMON_DIMENSIONS = (
    *_dimensions_by_id(
        BLOWER_DIMENSIONS,
        "occupation",
        "operating_responsibility",
        "purchase_relationship",
        "life_stage_ability",
        "property_context",
        "geography",
        "weather",
    ),
    _GENERIC_EXPERIENCE_DIMENSION,
    _GENERIC_CHARGING_DIMENSION,
    _GENERIC_JOB_COMPLETION_DIMENSION,
    _GENERIC_STORAGE_DIMENSION,
    _GENERIC_TIME_OF_DAY_DIMENSION,
)

_GENERIC_COMMON_ASPECTS = _rules_by_id(
    BLOWER_ASPECT_RULES,
    "runtime",
    "charging",
    "weight_balance",
    "ergonomics",
    "noise",
    "assembly",
    "storage",
    "battery_charger",
    "reliability",
    "starting",
    "maintenance_gasfree",
    "value",
    "safety",
    "service",
    "ecosystem",
    "smart_connectivity",
)

_GENERIC_NUMERIC_DIMENSION_IDS: tuple[str, ...] = (
    "explicit_age",
    "yard_size",
    "task_duration",
    "battery_spec",
    "battery_capacity",
    "battery_count",
)


def _profile_dimension(
    dimension_id: str,
    section: str,
    title: str,
    question: str,
    rows: object,
) -> DimensionRule:
    return DimensionRule(
        dimension_id,
        section,
        title,
        question,
        tuple(
            _label(str(label), str(display), str(expression))
            for label, display, expression in rows  # type: ignore[misc]
        ),
    )


def _generic_profile_dimensions(
    spec: dict[str, object],
) -> tuple[DimensionRule, ...]:
    return (
        *_GENERIC_COMMON_DIMENSIONS,
        _profile_dimension(
            "task_context",
            "scenario",
            "核心任务场景",
            "用户明确要完成什么任务？",
            spec["tasks"],
        ),
        _profile_dimension(
            "material_context",
            "scenario",
            "处理对象与负载",
            "设备面对什么对象、材料或负载？",
            spec["materials"],
        ),
        _profile_dimension(
            "workflow_context",
            "scenario",
            "关键操作流程",
            "用户如何完成准备、操作、切换和收尾？",
            spec["workflows"],
        ),
    )


def _generic_profile_aspects(
    spec: dict[str, object],
) -> tuple[LabelRule, ...]:
    specific = tuple(
        _label(str(label), str(display), str(expression))
        for label, display, expression in spec["aspects"]  # type: ignore[misc]
    )
    specific_ids = {rule.label for rule in specific}
    return specific + tuple(
        rule
        for rule in _GENERIC_COMMON_ASPECTS
        if rule.label not in specific_ids
    )


@dataclass(frozen=True)
class ProductCategoryProfile:
    category_id: str
    profile_id: str
    category_label: str
    category_label_en: str
    hero_title: str
    dimensions: tuple[DimensionRule, ...]
    numeric_dimension_ids: tuple[str, ...]
    aspect_rules: tuple[LabelRule, ...]


def _validate_profile_presentation(
    profile: ProductCategoryProfile,
    forbidden_terms: object,
) -> None:
    """Fail closed when authored copy leaks another category vocabulary."""

    presentation = "\n".join(
        (
            profile.category_label,
            profile.category_label_en,
            profile.hero_title,
            *(dimension.title for dimension in profile.dimensions),
            *(dimension.question for dimension in profile.dimensions),
            *(
                rule.display
                for dimension in profile.dimensions
                for rule in dimension.rules
            ),
            *(rule.display for rule in profile.aspect_rules),
        )
    )
    leaked = [
        str(term)
        for term in forbidden_terms  # type: ignore[union-attr]
        if str(term) and str(term) in presentation
    ]
    if leaked:
        raise RuntimeError(
            f"{profile.profile_id} contains forbidden presentation terms: "
            + ", ".join(leaked)
        )


CATEGORY_PROFILES: dict[str, ProductCategoryProfile] = {
    "walk_behind_mower": ProductCategoryProfile(
        category_id="walk_behind_mower",
        profile_id="mower/1.2.0",
        category_label="Walk-Behind Mower",
        category_label_en="Walk-behind mower",
        hero_title="还原一场完整割草任务",
        dimensions=DIMENSIONS,
        numeric_dimension_ids=NUMERIC_DIMENSION_IDS,
        aspect_rules=ASPECT_RULES,
    ),
    "leaf_blower": ProductCategoryProfile(
        category_id="leaf_blower",
        profile_id="blower/1.2.0",
        category_label="Leaf Blower",
        category_label_en="Leaf blower",
        hero_title="还原一场完整清理任务",
        dimensions=BLOWER_DIMENSIONS,
        numeric_dimension_ids=BLOWER_NUMERIC_DIMENSION_IDS,
        aspect_rules=BLOWER_ASPECT_RULES,
    ),
}


for _category_alias, _profile_spec in CATEGORY_PROFILE_SPECS.items():
    _category_definition = category_definition(_category_alias)
    if _category_definition is None:
        raise RuntimeError(
            f"unknown category profile specification: {_category_alias}"
        )
    if str(_profile_spec["profile_id"]) != _category_definition.profile_id:
        raise RuntimeError(
            f"profile id mismatch for {_category_definition.category_id}: "
            f"{_profile_spec['profile_id']} != "
            f"{_category_definition.profile_id}"
        )
    _profile = ProductCategoryProfile(
            category_id=_category_definition.category_id,
            profile_id=_category_definition.profile_id,
            category_label=_category_definition.display_name,
            category_label_en=str(_profile_spec["category_label_en"]),
            hero_title=str(_profile_spec["hero_title"]),
            dimensions=_generic_profile_dimensions(_profile_spec),
            numeric_dimension_ids=_GENERIC_NUMERIC_DIMENSION_IDS,
            aspect_rules=_generic_profile_aspects(_profile_spec),
    )
    _validate_profile_presentation(
        _profile,
        _profile_spec.get("forbidden_presentation_terms", ()),
    )
    CATEGORY_PROFILES[_category_definition.category_id] = _profile


_WHEELED_SPREADER_PROFILE = ProductCategoryProfile(
    category_id="spreader",
    profile_id=WHEELED_SPREADER_SPEC["profile_id"],
    category_label="Spreader",
    category_label_en=WHEELED_SPREADER_SPEC["category_label_en"],
    hero_title=WHEELED_SPREADER_SPEC["hero_title"],
    dimensions=_generic_profile_dimensions(WHEELED_SPREADER_SPEC),
    numeric_dimension_ids=_GENERIC_NUMERIC_DIMENSION_IDS,
    aspect_rules=_generic_profile_aspects(WHEELED_SPREADER_SPEC),
)


def category_profile(
    category: str, *, product_form_factors: Iterable[str] | None = None,
) -> ProductCategoryProfile | None:
    """Resolve the family, optionally using an explicitly selected form."""
    category_id = category_id_for(category)
    if category_id is None:
        definition = category_definition_by_id(category)
        category_id = definition.category_id if definition else None
    if category_id == "spreader" and set(product_form_factors or ()) == {"wheeled"}:
        return _WHEELED_SPREADER_PROFILE
    return CATEGORY_PROFILES.get(category_id or "")


POSITIVE_RE = _rx(
    r"\blove(?:d)?\b|\bgreat\b|\bexcellent\b|\bperfect\b|\bamazing\b|"
    r"\bfantastic\b|\bimpress(?:ed|ive)\b|\bworks? well\b|\beasy\b|"
    r"\bquiet\b|\blightweight\b|\bpowerful\b|\bsmooth\b|\bconvenient\b|"
    r"\bplenty (?:of )?(?:power|runtime|charge)\b|\bbetter than expected\b|"
    r"\bgame changer\b|\bhighly recommend\b"
)
NEGATIVE_RE = _rx(
    r"\bhate(?:d)?\b|\bpoor\b|\bterrible\b|\bawful\b|\bdisappoint(?:ed|ing)\b|"
    r"\buseless\b|\bdifficult\b|\bproblem\b|\bheavy\b|\bnoisy\b|\bweak\b|"
    r"\bunderpowered\b|\bshort runtime\b|\bdied\b|\bdead\b|\bfailed\b|"
    r"\bstopped working\b|\bquit working\b|\bbroke\b|\bbroken\b|\buneven\b|"
    r"\bclog(?:s|ged|ging)?\b|\btoo (?:fast|slow|heavy|loud)\b|"
    r"\bhard to\b|\bwon['’]t\b|\bwouldn['’]t\b|\bcan['’]t\b|\bcannot\b"
)
NEGATED_POSITIVE_RE = _rx(
    r"\b(?:not|isn['’]t|wasn['’]t|doesn['’]t)\s+(?:very\s+)?"
    r"(?:easy|good|great|enough|quiet|powerful|reliable|working|worth)\b"
)
NEGATED_NEGATIVE_RE = _rx(
    r"\b(?:not|isn['’]t|wasn['’]t)\s+(?:too\s+)?"
    r"(?:heavy|loud|difficult|bad|noisy)\b|\bno problem\b|"
    r"\bnever (?:failed|stalled|stopped|broke)\b|"
    r"\b(?:hasn['’]t|haven['’]t|hadn['’]t) (?:failed|stalled|stopped|broken)\b"
)

BLOWER_OTHER_COMPONENT_RE = _rx(
    r"\bstring trimmer\b|\bweed (?:eater|whacker)\b|"
    r"\btrimmer (?:head|line|guard|shaft)\b|\bstring head\b|"
    r"\bline feed\b|\bspool\b|\bedger\b|\bchain ?saw\b"
)
BLOWER_COMPONENT_RE = _rx(r"\b(?:leaf )?blower\b")
DELIGHT_RE = _rx(
    r"\bpleasantly surprised\b|\bblown away\b|\bgame changer\b|"
    r"\bexceeded (?:my )?expectations?\b|\bbetter than expected\b|"
    r"\babsolutely love\b|\bmy favorite\b|\bbest (?:mower|purchase|tool)\b|"
    r"\bvery impressed\b|\bamazing\b|\bfantastic\b"
)
EXPECTATION_RE = _rx(
    r"\bshould have\b|\bwas supposed to\b|\badvertised (?:as|to)\b|"
    r"\bdidn['’]t meet (?:my )?expectations?\b|\bnot what i expected\b|"
    r"\bneed(?:s)? to\b|\bmust\b"
)
EXPLICIT_NEED_RE = _rx(
    r"\bi need\b|\bwe need\b|\bi wish\b|\bwould like\b|\bi['’]d like\b|"
    r"\bshould (?:have|be|include|come|make|add)\b|\bplease (?:add|make|include|improve|fix)\b|"
    r"\bwould be better if\b"
)


def _quote_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in TOKEN_RE.findall(value.casefold())
        if len(token) > 2
    )


def _near_duplicate_quote(
    quote: str,
    tokens: frozenset[str],
    selected_quotes: list[tuple[str, frozenset[str]]],
) -> bool:
    normalized = " ".join(quote.casefold().split())
    for other, other_tokens in selected_quotes:
        if normalized == other:
            return True
        shorter, longer = sorted((normalized, other), key=len)
        if len(shorter) >= 60 and shorter in longer:
            return True
        union = tokens | other_tokens
        if union and len(tokens & other_tokens) / len(union) >= 0.82:
            return True
    return False


class EvidenceStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add(
        self,
        fact: dict[str, Any],
        *,
        kind: str,
        label: str,
        sentence: str,
        matched_text: str,
        polarity: str | None = None,
    ) -> str:
        material = {
            "entity": _entity_ref(fact),
            "kind": kind,
            "label": label,
            "sentence": sentence,
            "matched_text": matched_text,
            "polarity": polarity,
        }
        evidence_id = (
            "ev_ope_"
            + hashlib.sha256(
                canonical_json(material).encode("utf-8")
            ).hexdigest()[:24]
        )
        self.rows[evidence_id] = {
            "evidence_id": evidence_id,
            "evidence_type": "review_quote",
            "is_original_review_quote": True,
            "review_entity_id": fact.get("review_entity_id"),
            "review_id": fact.get("review_id"),
            "channel": fact.get("channel"),
            "brand": fact.get("brand"),
            "model": fact.get("model"),
            **(
                {
                    "market_region": fact.get("market_region"),
                    "market_region_evidence": fact.get(
                        "market_region_evidence"
                    ),
                    "display_site": fact.get("display_site"),
                    "source_site": fact.get("source_site_normalized"),
                }
                if fact.get("market_region")
                else {}
            ),
            "rating": fact.get("rating"),
            "submission_date": fact.get("submission_date"),
            "kind": kind,
            "label": label,
            "polarity": polarity,
            "matched_text": matched_text,
            "quote": sentence,
            "source_url": fact.get("product_url"),
            "source_context": {
                key: value
                for key, value in {
                    "age": str(fact.get("age") or "").strip(),
                }.items()
                if value
            },
        }
        return evidence_id

    def add_source_field(
        self,
        fact: dict[str, Any],
        *,
        dimension_id: str,
        label: str,
        field: str,
        raw_value: str,
    ) -> str:
        material = {
            "entity": _entity_ref(fact),
            "kind": f"dimension:{dimension_id}",
            "label": label,
            "field": field,
            "raw_value": raw_value,
        }
        evidence_id = (
            "ev_ope_"
            + hashlib.sha256(
                canonical_json(material).encode("utf-8")
            ).hexdigest()[:24]
        )
        self.rows[evidence_id] = {
            "evidence_id": evidence_id,
            "evidence_type": "source_field",
            "is_original_review_quote": False,
            "review_entity_id": fact.get("review_entity_id"),
            "review_id": fact.get("review_id"),
            "channel": fact.get("channel"),
            "brand": fact.get("brand"),
            "model": fact.get("model"),
            **(
                {
                    "market_region": fact.get("market_region"),
                    "market_region_evidence": fact.get(
                        "market_region_evidence"
                    ),
                    "display_site": fact.get("display_site"),
                    "source_site": fact.get("source_site_normalized"),
                }
                if fact.get("market_region")
                else {}
            ),
            "rating": fact.get("rating"),
            "submission_date": fact.get("submission_date"),
            "kind": f"dimension:{dimension_id}",
            "label": label,
            "polarity": None,
            "matched_text": raw_value,
            "quote": f"{field}={raw_value}",
            "source_field": field,
            "source_value": raw_value,
            "source_url": fact.get("product_url"),
            "source_context": {
                key: value
                for key, value in {
                    "age": str(fact.get("age") or "").strip(),
                }.items()
                if value
            },
        }
        return evidence_id


class OpeExecutionLedger:
    """Lazy all-sample ledger backed by matched-only in-memory state."""

    def __init__(
        self,
        entities: Iterable[str],
        extractor_ids: Iterable[str],
        matches: dict[tuple[str, str], tuple[str, ...]],
    ) -> None:
        self.entities = tuple(sorted(entities))
        self.extractor_ids = tuple(extractor_ids)
        self.matches = matches

    def __len__(self) -> int:
        return len(self.entities) * len(self.extractor_ids)

    def __iter__(self) -> Iterable[dict[str, Any]]:
        for entity in self.entities:
            for extractor_id in self.extractor_ids:
                labels = list(
                    self.matches.get((entity, extractor_id), ())
                )
                yield {
                    "review_entity_ref": entity,
                    "extractor_id": extractor_id,
                    "status": "matched" if labels else "no_match",
                    "labels": labels,
                }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OpeExecutionLedger):
            return False
        return (
            self.entities == other.entities
            and self.extractor_ids == other.extractor_ids
            and self.matches == other.matches
        )


class OpeIndex:
    def __init__(
        self,
        facts: list[dict[str, Any]],
        *,
        profile_id: str = "mower/1.2.0",
        category_label: str = "割草机",
        dimensions: tuple[DimensionRule, ...] = DIMENSIONS,
        numeric_dimension_ids: tuple[str, ...] = NUMERIC_DIMENSION_IDS,
        aspect_rules: tuple[LabelRule, ...] = ASPECT_RULES,
    ) -> None:
        self.profile_id = profile_id
        self.category_label = category_label
        self.dimensions = dimensions
        self.numeric_dimension_ids = numeric_dimension_ids
        self.aspect_rules = aspect_rules
        self.facts = {
            _entity_ref(fact): fact for fact in facts if fact.get("has_text")
        }
        self.all_entities = set(self.facts)
        self.dimension_entities: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.dimension_evidence: dict[
            str, dict[str, dict[str, list[str]]]
        ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        self.aspect_entities: dict[str, set[str]] = defaultdict(set)
        self.aspect_positive: dict[str, set[str]] = defaultdict(set)
        self.aspect_negative: dict[str, set[str]] = defaultdict(set)
        self.aspect_delight: dict[str, set[str]] = defaultdict(set)
        self.aspect_expectation: dict[str, set[str]] = defaultdict(set)
        self.aspect_evidence: dict[
            str, dict[str, dict[str, list[str]]]
        ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        self.age_values: dict[str, int] = {}
        self.age_buckets: dict[str, str] = {}
        self.yard_acres: dict[str, float] = {}
        self.cut_heights: dict[str, float] = {}
        self.task_minutes: dict[str, float] = {}
        self.battery_volts: dict[str, set[float]] = defaultdict(set)
        self.battery_ah: dict[str, set[float]] = defaultdict(set)
        self.battery_counts: dict[str, set[int]] = defaultdict(set)
        self.evidence = EvidenceStore()
        self.ledger_matches: dict[
            tuple[str, str], tuple[str, ...]
        ] = {}
        self.ledger_rows = OpeExecutionLedger(
            self.all_entities,
            (
                *(dimension.dimension_id for dimension in self.dimensions),
                *self.numeric_dimension_ids,
                *(
                    f"aspect:{rule.label}"
                    for rule in self.aspect_rules
                ),
            ),
            self.ledger_matches,
        )
        self.evidence_groups: dict[str, dict[str, Any]] = {}
        self.evidence_group_by_representatives: dict[
            tuple[str, ...], list[str]
        ] = defaultdict(list)

    def _record_dimension(
        self,
        fact: dict[str, Any],
        dimension_id: str,
        label: str,
        sentence: str,
        matched_text: str,
    ) -> None:
        entity = _entity_ref(fact)
        self.dimension_entities[dimension_id][label].add(entity)
        evidence_id = self.evidence.add(
            fact,
            kind=f"dimension:{dimension_id}",
            label=label,
            sentence=sentence,
            matched_text=matched_text,
        )
        bucket = self.dimension_evidence[dimension_id][label][entity]
        if evidence_id not in bucket:
            bucket.append(evidence_id)

    def _record_source_dimension(
        self,
        fact: dict[str, Any],
        dimension_id: str,
        label: str,
        field: str,
        raw_value: str,
    ) -> None:
        entity = _entity_ref(fact)
        self.dimension_entities[dimension_id][label].add(entity)
        evidence_id = self.evidence.add_source_field(
            fact,
            dimension_id=dimension_id,
            label=label,
            field=field,
            raw_value=raw_value,
        )
        bucket = self.dimension_evidence[dimension_id][label][entity]
        if evidence_id not in bucket:
            bucket.append(evidence_id)

    def _record_aspect(
        self,
        fact: dict[str, Any],
        aspect: str,
        sentence: str,
        matched_text: str,
        polarity: str,
    ) -> None:
        entity = _entity_ref(fact)
        self.aspect_entities[aspect].add(entity)
        if polarity == "positive":
            self.aspect_positive[aspect].add(entity)
        elif polarity == "negative":
            self.aspect_negative[aspect].add(entity)
        if DELIGHT_RE.search(sentence):
            self.aspect_delight[aspect].add(entity)
        if EXPECTATION_RE.search(sentence):
            self.aspect_expectation[aspect].add(entity)
        evidence_id = self.evidence.add(
            fact,
            kind=f"aspect:{aspect}",
            label=aspect,
            sentence=sentence,
            matched_text=matched_text,
            polarity=polarity,
        )
        bucket = self.aspect_evidence[aspect][polarity][entity]
        if evidence_id not in bucket:
            bucket.append(evidence_id)

    def run(self) -> None:
        compiled_dimensions = {
            dimension.dimension_id: [
                (rule, _rx(rule.expression)) for rule in dimension.rules
            ]
            for dimension in self.dimensions
        }
        compiled_aspects = [
            (rule, _rx(rule.expression)) for rule in self.aspect_rules
        ]
        for entity in sorted(self.all_entities):
            fact = self.facts[entity]
            raw_age = str(fact.get("age") or "").strip()
            source_age_bucket = SOURCE_AGE_BUCKETS.get(raw_age.casefold())
            if source_age_bucket:
                self.age_buckets[entity] = source_age_bucket
                self._record_source_dimension(
                    fact,
                    "explicit_age",
                    source_age_bucket,
                    "age",
                    raw_age,
                )
            text = ". ".join(
                value.strip()
                for value in (
                    str(fact.get("review_title") or ""),
                    str(fact.get("review_text") or ""),
                )
                if value.strip()
            )
            sentences = _sentences(text)
            matched_dimensions: dict[str, set[str]] = defaultdict(set)
            matched_aspects: set[str] = set()
            for _start, _end, sentence in sentences:
                for dimension in self.dimensions:
                    for rule, pattern in compiled_dimensions[
                        dimension.dimension_id
                    ]:
                        match = pattern.search(sentence)
                        if match:
                            matched_dimensions[dimension.dimension_id].add(
                                rule.label
                            )
                            self._record_dimension(
                                fact,
                                dimension.dimension_id,
                                rule.label,
                                sentence,
                                match.group(0),
                            )
                sentence_polarity = _polarity(sentence)
                for rule, pattern in compiled_aspects:
                    match = pattern.search(sentence)
                    if not match:
                        continue
                    if (
                        self.profile_id.startswith("blower/")
                        and BLOWER_OTHER_COMPONENT_RE.search(sentence)
                        and not BLOWER_COMPONENT_RE.search(sentence)
                    ):
                        continue
                    matched_aspects.add(rule.label)
                    polarity = sentence_polarity
                    if polarity == "neutral" and fact.get("rating") is not None:
                        if fact["rating"] <= 2:
                            polarity = "rating_context_negative"
                        elif fact["rating"] >= 4:
                            polarity = "rating_context_positive"
                    self._record_aspect(
                        fact,
                        rule.label,
                        sentence,
                        match.group(0),
                        polarity,
                    )
            self._extract_numeric(fact, text)
            for dimension_id in (
                *(dimension.dimension_id for dimension in self.dimensions),
                *self.numeric_dimension_ids,
            ):
                labels = sorted(
                    matched_dimensions.get(dimension_id, set())
                    or {
                        label
                        for label, entities in self.dimension_entities.get(
                            dimension_id, {}
                        ).items()
                        if entity in entities
                    }
                )
                if labels:
                    self.ledger_matches[(entity, dimension_id)] = tuple(
                        labels
                    )
            for rule in self.aspect_rules:
                if rule.label in matched_aspects:
                    self.ledger_matches[
                        (entity, f"aspect:{rule.label}")
                    ] = (rule.label,)

    def _extract_numeric(self, fact: dict[str, Any], text: str) -> None:
        entity = _entity_ref(fact)
        for start, end, sentence in _sentences(text):
            del start, end
            for match in _rx(
                r"\b(?:i am|i['’]m|as a)\s+(?:a\s+)?(\d{2})\s*"
                r"(?:years? old|yo)?\b|\b(\d{2})[- ]year[- ]old\b"
            ).finditer(sentence):
                value = int(match.group(1) or match.group(2))
                if 18 <= value <= 99:
                    self.age_values.setdefault(entity, value)
                    self.age_buckets.setdefault(entity, _age_bucket(value))
                    self._record_dimension(
                        fact,
                        "explicit_age",
                        _age_bucket(value),
                        sentence,
                        match.group(0),
                    )
            for match in _rx(
                r"\b(\d+(?:\.\d+)?|\d+/\d+|quarter|half|one)[ -]"
                r"(acres?)\b|"
                r"\b(\d{3,6}(?:,\d{3})?)\s*(?:sq\.?\s*ft\.?|square feet)\b"
            ).finditer(sentence):
                if _rx(
                    r"\b(?:up to|advertised|rated|claims?|designed for|covers? up to)\b"
                ).search(sentence):
                    continue
                if not _rx(
                    r"\b(?:my|our|have|has|property|yard|lawn|mow|mowed|mowing|cut|lot)\b"
                ).search(sentence):
                    continue
                acres = _parse_area_match(match)
                if acres is not None and 0.01 <= acres <= 50:
                    current = self.yard_acres.get(entity)
                    if current is None or acres > current:
                        self.yard_acres[entity] = acres
                    self._record_dimension(
                        fact,
                        "yard_size",
                        _yard_bucket(acres),
                        sentence,
                        match.group(0),
                    )
            for match in _rx(
                r"\b(?:cut(?:ting)?|deck|mow(?:ing)?) (?:height|setting)?"
                r"[^.!?\n]{0,30}?(\d(?:\.\d+)?)\s*(?:inches?|in\.?)\b|"
                r"\b(\d(?:\.\d+)?)\s*(?:inches?|in\.?)"
                r"[^.!?\n]{0,30}(?:cut|height|setting)\b"
            ).finditer(sentence):
                value = float(match.group(1) or match.group(2))
                if 0.5 <= value <= 6:
                    self.cut_heights.setdefault(entity, value)
                    self._record_dimension(
                        fact,
                        "cut_height",
                        _cut_height_bucket(value),
                        sentence,
                        match.group(0),
                    )
            for match in _rx(
                r"\b(?:takes?|took|for|mow(?:ed|ing)? for)\s+"
                r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?)\b"
            ).finditer(sentence):
                value = float(match.group(1))
                if match.group(2).lower().startswith(("hour", "hr")):
                    value *= 60
                if 3 <= value <= 720:
                    self.task_minutes.setdefault(entity, value)
                    self._record_dimension(
                        fact,
                        "task_duration",
                        _duration_bucket(value),
                        sentence,
                        match.group(0),
                    )
            for match in _rx(r"\b(\d{2,3}(?:\.\d+)?)\s*(?:v|volts?)\b").finditer(
                sentence
            ):
                value = float(match.group(1))
                if 12 <= value <= 120:
                    self.battery_volts[entity].add(value)
                    self._record_dimension(
                        fact,
                        "battery_spec",
                        f"{value:g}V",
                        sentence,
                        match.group(0),
                    )
            for match in _rx(
                r"\b(\d+(?:\.\d+)?)\s*(?:ah|amp[- ]?hours?)\b"
            ).finditer(sentence):
                value = float(match.group(1))
                if 0.5 <= value <= 30:
                    self.battery_ah[entity].add(value)
                    self._record_dimension(
                        fact,
                        "battery_capacity",
                        f"{value:g}Ah",
                        sentence,
                        match.group(0),
                    )
            for match in _rx(
                r"\b(\d|two|three|four|five|six)\s+batter(?:y|ies)\b"
            ).finditer(sentence):
                raw = match.group(1).lower()
                value = {
                    "two": 2,
                    "three": 3,
                    "four": 4,
                    "five": 5,
                    "six": 6,
                }.get(raw, int(raw) if raw.isdigit() else 0)
                if 1 <= value <= 8:
                    self.battery_counts[entity].add(value)
                    self._record_dimension(
                        fact,
                        "battery_count",
                        f"{value} 块电池",
                        sentence,
                        match.group(0),
                    )

    def entities_for(
        self, dimension_id: str, labels: Iterable[str] | None = None
    ) -> set[str]:
        values = self.dimension_entities.get(dimension_id, {})
        if labels is None:
            return set().union(*values.values()) if values else set()
        return set().union(
            *(values.get(label, set()) for label in labels)
        )

    def evidence_for_entities(
        self,
        entities: set[str],
        *,
        dimension_ids: Iterable[str] = (),
        dimension_label_filters: dict[str, set[str]] | None = None,
        aspects: Iterable[str] = (),
        polarity: str | None = None,
        limit: int = 4,
        prefer_low: bool = False,
    ) -> list[str]:
        candidates: list[str] = []
        focus_rank: dict[str, int] = {}
        focus_order = 0
        for dimension_id in dimension_ids:
            for label, label_rows in self.dimension_evidence.get(
                dimension_id, {}
            ).items():
                allowed_labels = (
                    dimension_label_filters.get(dimension_id)
                    if dimension_label_filters
                    else None
                )
                if allowed_labels is not None and label not in allowed_labels:
                    continue
                for entity in sorted(entities):
                    for evidence_id in label_rows.get(entity, []):
                        candidates.append(evidence_id)
                        focus_rank.setdefault(evidence_id, focus_order)
            focus_order += 1
        for aspect in aspects:
            polarity_rows = self.aspect_evidence.get(aspect, {})
            requested = (
                [polarity]
                if polarity
                else [
                    "negative",
                    "positive",
                    "rating_context_negative",
                    "rating_context_positive",
                    "neutral",
                ]
            )
            for key in requested:
                for entity in sorted(entities):
                    for evidence_id in polarity_rows.get(key, {}).get(
                        entity, []
                    ):
                        candidates.append(evidence_id)
                        focus_rank.setdefault(evidence_id, focus_order)
            focus_order += 1
        candidates = list(dict.fromkeys(candidates))

        def rating_bucket(row: dict[str, Any]) -> str:
            rating = row.get("rating")
            if rating is None:
                return "unknown"
            if rating <= 2:
                return "low"
            if rating == 3:
                return "mid"
            return "high"

        def rating_order(evidence_id: str) -> tuple[int, str]:
            rating = self.evidence.rows[evidence_id].get("rating")
            numeric = int(rating) if rating is not None else 9
            return (
                numeric if prefer_low else -numeric,
                evidence_id,
            )

        # Build deterministic streams by analytical focus, then interleave
        # channel/model/rating strata. This prevents the first model, channel,
        # or sentence pattern from monopolizing the evidence drawer.
        focus_streams: dict[int, list[str]] = {}
        for rank in sorted(set(focus_rank.values())):
            grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
            for evidence_id in candidates:
                if focus_rank[evidence_id] != rank:
                    continue
                row = self.evidence.rows[evidence_id]
                grouped[
                    (
                        str(row.get("channel") or "unknown"),
                        str(row.get("model") or "unknown"),
                        rating_bucket(row),
                    )
                ].append(evidence_id)
            for values in grouped.values():
                values.sort(key=rating_order)
            stream: list[str] = []
            group_keys = sorted(
                grouped,
                key=lambda key: (
                    rating_order(grouped[key][0]),
                    key,
                ),
            )
            position = 0
            while True:
                added = False
                for key in group_keys:
                    values = grouped[key]
                    if position < len(values):
                        stream.append(values[position])
                        added = True
                if not added:
                    break
                position += 1
            focus_streams[rank] = stream

        diversified: list[str] = []
        position = 0
        while True:
            added = False
            for rank in sorted(focus_streams):
                stream = focus_streams[rank]
                if position < len(stream):
                    diversified.append(stream[position])
                    added = True
            if not added:
                break
            position += 1

        selected: list[str] = []
        selected_entities: set[str] = set()
        selected_quotes: list[tuple[str, frozenset[str]]] = []
        for evidence_id in diversified:
            row = self.evidence.rows[evidence_id]
            entity = _evidence_entity_ref(row)
            if entity in selected_entities:
                continue
            quote = str(row["quote"])
            tokens = _quote_tokens(quote)
            if _near_duplicate_quote(quote, tokens, selected_quotes):
                continue
            selected.append(evidence_id)
            selected_entities.add(entity)
            selected_quotes.append(
                (" ".join(quote.casefold().split()), tokens)
            )
            if len(selected) >= limit:
                break
        material = {
            "entities": sorted(entities),
            "dimension_ids": list(dimension_ids),
            "dimension_label_filters": {
                key: sorted(value)
                for key, value in sorted(
                    (dimension_label_filters or {}).items()
                )
            },
            "aspects": list(aspects),
            "polarity": polarity,
            "prefer_low": prefer_low,
        }
        group_id = (
            "evg_"
            + hashlib.sha256(
                canonical_json(material).encode("utf-8")
            ).hexdigest()[:24]
        )
        self.evidence_groups[group_id] = {
            "evidence_ids": diversified,
            "representative_evidence_ids": selected,
            "query": {
                key: value
                for key, value in material.items()
                if key != "entities"
            },
            "unique_review_entities": len(entities),
        }
        representative_key = tuple(selected)
        if group_id not in self.evidence_group_by_representatives[
            representative_key
        ]:
            self.evidence_group_by_representatives[
                representative_key
            ].append(group_id)
        return selected

    def register_evidence_group(
        self,
        representative_evidence_ids: Iterable[str],
        evidence_ids: Iterable[str],
        *,
        unique_review_entities: int,
        query: dict[str, Any] | None = None,
    ) -> str:
        representatives = list(
            dict.fromkeys(str(value) for value in representative_evidence_ids)
        )
        members = list(
            dict.fromkeys(str(value) for value in evidence_ids)
        )
        material = {
            "representative_evidence_ids": representatives,
            "evidence_ids": members,
            "query": query or {},
        }
        group_id = (
            "evg_"
            + hashlib.sha256(
                canonical_json(material).encode("utf-8")
            ).hexdigest()[:24]
        )
        self.evidence_groups[group_id] = {
            "evidence_ids": members,
            "representative_evidence_ids": representatives,
            "query": query or {},
            "unique_review_entities": unique_review_entities,
        }
        if group_id not in self.evidence_group_by_representatives[
            tuple(representatives)
        ]:
            self.evidence_group_by_representatives[
                tuple(representatives)
            ].append(group_id)
        return group_id

    def dedupe_evidence_ids(
        self,
        evidence_ids: Iterable[str],
        *,
        limit: int,
    ) -> list[str]:
        selected: list[str] = []
        selected_entities: set[str] = set()
        selected_quotes: list[tuple[str, frozenset[str]]] = []
        for evidence_id in evidence_ids:
            row = self.evidence.rows.get(evidence_id)
            if not row:
                continue
            entity = _evidence_entity_ref(row)
            if entity in selected_entities:
                continue
            quote = str(row["quote"])
            tokens = _quote_tokens(quote)
            if _near_duplicate_quote(quote, tokens, selected_quotes):
                continue
            selected.append(evidence_id)
            selected_entities.add(entity)
            selected_quotes.append(
                (" ".join(quote.casefold().split()), tokens)
            )
            if len(selected) >= limit:
                break
        return selected

    def stats(self, entities: set[str]) -> dict[str, Any]:
        ratings = [
            self.facts[entity]["rating"]
            for entity in entities
            if self.facts[entity].get("rating") is not None
        ]
        low = sum(rating <= 2 for rating in ratings)
        high = sum(rating >= 4 for rating in ratings)
        return {
            "review_entities": len(entities),
            "coverage": _ratio(len(entities), len(self.all_entities)),
            "rating_n": len(ratings),
            "average_rating": (
                round(statistics.fmean(ratings), 4) if ratings else None
            ),
            "high_star_entities": high,
            "high_star_rate": _ratio(high, len(ratings)),
            "low_star_entities": low,
            "low_star_rate": _ratio(low, len(ratings)),
            "quality_flag": "low_sample" if len(entities) < 20 else None,
        }


def _polarity(sentence: str) -> str:
    positive = len(POSITIVE_RE.findall(sentence))
    negative = len(NEGATIVE_RE.findall(sentence))
    negative += len(NEGATED_POSITIVE_RE.findall(sentence))
    positive += len(NEGATED_NEGATIVE_RE.findall(sentence))
    if positive > negative:
        return "positive"
    if negative > positive:
        return "negative"
    if positive and negative:
        return "mixed"
    return "neutral"


def _age_bucket(value: int) -> str:
    if value < 18:
        return "≤17"
    if value < 25:
        return "18–24"
    if value < 35:
        return "25–34"
    if value < 45:
        return "35–44"
    if value < 55:
        return "45–54"
    if value < 65:
        return "55–64"
    return "65+"


def _parse_number(raw: str) -> float:
    value = raw.casefold()
    if value == "quarter":
        return 0.25
    if value == "half":
        return 0.5
    if value == "one":
        return 1.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    return float(value.replace(",", ""))


def _parse_area_match(match: re.Match[str]) -> float | None:
    if match.group(1):
        return _parse_number(match.group(1))
    if match.group(3):
        return _parse_number(match.group(3)) / 43560
    return None


def _yard_bucket(acres: float) -> str:
    if acres < 0.25:
        return "< ¼ acre"
    if acres < 0.5:
        return "¼–½ acre"
    if acres < 1:
        return "½–1 acre"
    if acres < 2:
        return "1–2 acres"
    return "2+ acres"


def _cut_height_bucket(inches: float) -> str:
    if inches < 2:
        return "< 2 in"
    if inches < 3:
        return "2–<3 in"
    if inches < 4:
        return "3–<4 in"
    return "4+ in"


def _duration_bucket(minutes: float) -> str:
    if minutes < 30:
        return "< 30 min"
    if minutes < 60:
        return "30–<60 min"
    if minutes < 120:
        return "1–<2 hr"
    return "2+ hr"


def _dimension_meta(
    dimension_id: str,
    dimensions: tuple[DimensionRule, ...] = DIMENSIONS,
) -> tuple[str, str, str]:
    for dimension in dimensions:
        if dimension.dimension_id == dimension_id:
            return dimension.section, dimension.title, dimension.question
    extra = {
        "explicit_age": ("profile", "明确年龄", "评论中出现哪些明确年龄段？"),
        "yard_size": ("scenario", "院子大小", "明确面积如何分布？"),
        "cut_height": ("scenario", "偏好割高", "评论中明确写出的割高是多少？"),
        "task_duration": ("scenario", "单次作业时长", "明确写出的作业时长是多少？"),
        "battery_spec": ("scenario", "电池电压", "评论中出现哪些电压平台？"),
        "battery_capacity": ("scenario", "电池容量", "评论中出现哪些 Ah 容量？"),
        "battery_count": ("scenario", "配套电池数量", "评论中明确写出几块电池？"),
    }
    return extra[dimension_id]


def _build_dimension_census(index: OpeIndex) -> list[dict[str, Any]]:
    dimension_ids = [
        dimension.dimension_id for dimension in index.dimensions
    ] + list(index.numeric_dimension_ids)
    rows = []
    for dimension_id in dimension_ids:
        section, title, question = _dimension_meta(
            dimension_id, index.dimensions
        )
        labels = index.dimension_entities.get(dimension_id, {})
        entities = set().union(*labels.values()) if labels else set()
        values = []
        for label, label_entities in labels.items():
            display = label
            for dimension in index.dimensions:
                if dimension.dimension_id == dimension_id:
                    display = next(
                        (
                            rule.display
                            for rule in dimension.rules
                            if rule.label == label
                        ),
                        label,
                    )
                    break
            values.append(
                {
                    "value_id": label,
                    "label": display,
                    "review_entities": len(label_entities),
                    "coverage": _ratio(
                        len(label_entities), len(index.all_entities)
                    ),
                    "evidence_population": len(label_entities),
                    "evidence_ids": index.evidence_for_entities(
                        label_entities,
                        dimension_ids=(dimension_id,),
                        dimension_label_filters={
                            dimension_id: {label}
                        },
                        limit=12,
                    ),
                }
            )
        values.sort(
            key=lambda row: (-row["review_entities"], row["label"])
        )
        coverage = _ratio(len(entities), len(index.all_entities))
        unknown_entities = len(index.all_entities) - len(entities)
        rows.append(
            {
                "dimension_id": dimension_id,
                "section": section,
                "title": title,
                "question": question,
                "review_entities": len(entities),
                "coverage": coverage,
                "unknown_entities": unknown_entities,
                "unknown_rate": _ratio(
                    unknown_entities, len(index.all_entities)
                ),
                "coverage_grade": (
                    "usable"
                    if coverage >= 0.05
                    else "directional"
                    if coverage >= 0.01
                    else "sparse"
                    if entities
                    else "missing"
                ),
                "values": values,
                "boundary": (
                    "只统计评论中明确表达；未命中不等于该特征不存在。"
                ),
            }
        )
    return rows


def _aspect_rows(index: OpeIndex) -> list[dict[str, Any]]:
    display = {rule.label: rule.display for rule in index.aspect_rules}
    rows = []
    for aspect in sorted(index.aspect_entities):
        entities = index.aspect_entities[aspect]
        positive = index.aspect_positive[aspect]
        negative = index.aspect_negative[aspect]
        delight = index.aspect_delight[aspect]
        expectation = index.aspect_expectation[aspect]
        row = {
            "aspect_id": aspect,
            "label": display.get(aspect, aspect),
            **index.stats(entities),
            "explicit_positive_entities": len(positive),
            "explicit_positive_rate": _ratio(len(positive), len(entities)),
            "explicit_negative_entities": len(negative),
            "explicit_negative_rate": _ratio(len(negative), len(entities)),
            "delight_entities": len(delight),
            "expectation_gap_entities": len(expectation),
            "positive_evidence_population": len(positive),
            "negative_evidence_population": len(negative),
            "positive_evidence_ids": index.evidence_for_entities(
                positive,
                aspects=(aspect,),
                polarity="positive",
                limit=16,
            ),
            "negative_evidence_ids": index.evidence_for_entities(
                negative,
                aspects=(aspect,),
                polarity="negative",
                limit=16,
                prefer_low=True,
            ),
            "boundary": (
                "显式正负向来自同句词法；星级关联是评论级上下文，不表示因果。"
            ),
        }
        # Topic coverage includes neutral mentions, so its evidence path must
        # include every matched entity, independently of sentiment.
        row.update(
            evidence_population=len(entities),
            evidence_ids=index.evidence_for_entities(
                entities, aspects=(aspect,), limit=16,
            ),
        )
        rows.append(row)
    rows.sort(
        key=lambda row: (-row["review_entities"], row["aspect_id"])
    )
    return rows


def _persona_rows(
    index: OpeIndex,
    aspect_rows: list[dict[str, Any]],
    brand_label: str,
) -> list[dict[str, Any]]:
    return _generic_persona_rows(index, aspect_rows, brand_label)


def _blower_persona_rows(
    index: OpeIndex,
    aspect_rows: list[dict[str, Any]],
    brand_label: str,
) -> list[dict[str, Any]]:
    all_n = len(index.all_entities)
    overall_aspect_rate = {
        row["aspect_id"]: row["review_entities"] / all_n
        for row in aspect_rows
    }
    senior = index.entities_for("explicit_age", {"65+"}) | index.entities_for(
        "life_stage_ability"
    )
    large_cleanup = index.entities_for(
        "yard_size", {"½–1 acre", "1–2 acres", "2+ acres"}
    ) | index.entities_for(
        "work_area", {"commercial_site", "driveway", "lawn_yard"}
    )
    demanding_debris = index.entities_for(
        "debris_condition", {"heavy_pile", "wet_damp", "stuck_matted"}
    ) | index.entities_for(
        "debris_type", {"wet_leaves", "pine_needles", "acorns_twigs"}
    )
    persona_specs = [
        (
            "large_cleanup_planner",
            "大院 / 多区域的一次清完规划者",
            "明确面积 ≥½ acre，或同时清理草坪、车道和商用区域。",
            large_cleanup,
            ("yard_size", "work_area", "job_completion"),
            ("runtime", "charging", "weight_balance"),
            "核心任务是不中断地清完整个区域，并控制长时间手持的疲劳。",
        ),
        (
            "demanding_debris_operator",
            "湿叶 / 重碎屑的性能验证者",
            "明确面对湿叶、厚堆、板结、松针、橡果或重碎屑。",
            demanding_debris,
            ("debris_type", "debris_condition"),
            ("blowing_power", "wet_debris", "airflow_control"),
            "需要的是在真实负载下移动碎屑，而不是只看实验室 CFM / MPH。",
        ),
        (
            "gas_cord_switcher",
            "从燃油或有线切换的省事派",
            "明确从燃油或有线吹风机切换到电池产品。",
            index.entities_for(
                "experience_path", {"gas_switcher", "corded_switcher"}
            ),
            ("experience_path",),
            (
                "maintenance_gasfree",
                "starting",
                "noise",
                "runtime",
            ),
            "价值锚是随拿随用、摆脱燃油和电源线，同时不能牺牲清理能力。",
        ),
        (
            "senior_fatigue_sensitive",
            "银发 / 疲劳敏感的轻负担用户",
            "来源年龄档案为 65+，或明确表达力量、关节、腰背和长时间手持限制。",
            senior,
            ("explicit_age", "life_stage_ability"),
            ("weight_balance", "ergonomics", "noise", "starting"),
            "轻并不等于好用；整机平衡、握持、背带和扳机负担共同决定疲劳。",
        ),
        (
            "ecosystem_owner",
            f"{brand_label} 电池生态复用者",
            "明确拥有同平台电池、工具或把兼容性作为购买上下文。",
            index.entities_for(
                "experience_path", {"battery_ecosystem_owner"}
            ),
            ("experience_path", "charging_workflow"),
            ("ecosystem", "battery_charger", "charging", "value"),
            "复用降低购买门槛，但高功率吹风的电池容量和重量交换必须透明。",
        ),
    ]
    rows: list[dict[str, Any]] = []
    aspect_labels = {
        row["aspect_id"]: row["label"] for row in aspect_rows
    }
    for (
        persona_id,
        title,
        definition,
        entities,
        dimensions,
        focus_aspects,
        jtbd,
    ) in persona_specs:
        if not entities:
            continue
        lifts = []
        for aspect in focus_aspects:
            cohort_mentions = len(entities & index.aspect_entities[aspect])
            cohort_rate = cohort_mentions / len(entities)
            overall_rate = overall_aspect_rate.get(aspect, 0)
            lifts.append(
                {
                    "aspect_id": aspect,
                    "label": aspect_labels.get(aspect, aspect),
                    "review_entities": cohort_mentions,
                    "cohort_rate": round(cohort_rate, 6),
                    "overall_rate": round(overall_rate, 6),
                    "lift": (
                        round(cohort_rate / overall_rate, 3)
                        if overall_rate
                        else None
                    ),
                }
            )
        lifts.sort(
            key=lambda row: (
                -(row["lift"] or 0),
                -row["review_entities"],
            )
        )
        rows.append(
            {
                "persona_id": persona_id,
                "title": title,
                "definition": definition,
                "jtbd": jtbd,
                **index.stats(entities),
                "distinctive_aspects": lifts[:4],
                "evidence_ids": index.evidence_for_entities(
                    entities,
                    dimension_ids=dimensions,
                    limit=18,
                ),
                "evidence_population": len(entities),
                "boundary": (
                    "规则群体可重叠，是评论者自述片段，不是市场人口占比。"
                ),
            }
        )
    rows.sort(
        key=lambda row: (-row["review_entities"], row["persona_id"])
    )
    return rows


def _generic_persona_rows(
    index: OpeIndex,
    aspect_rows: list[dict[str, Any]],
    brand_label: str,
) -> list[dict[str, Any]]:
    """Build explicit audience cohorts without turning tasks into people.

    Occupation, responsibility, life stage/ability, and experience may describe
    who the reviewer is. Purchase relationships and task/material/workflow
    dimensions remain visible elsewhere but are never promoted to personas.
    """

    all_n = len(index.all_entities)
    overall_rate = {
        row["aspect_id"]: row["review_entities"] / all_n
        for row in aspect_rows
    } if all_n else {}
    aspect_labels = {
        row["aspect_id"]: row["label"] for row in aspect_rows
    }
    aspect_rank = [
        row["aspect_id"]
        for row in sorted(
            aspect_rows,
            key=lambda row: (-row["review_entities"], row["aspect_id"]),
        )
        if row["review_entities"]
    ]

    candidates: list[
        tuple[str, str, str, set[str], tuple[str, ...], str]
    ] = []

    product_work_occupations = {
        "landscape_tree_professional",
        "contractor_trades",
        "facilities_grounds",
        "agriculture_operator",
        "cleaning_detailing_professional",
        "snow_removal_professional",
    }
    professional = (
        index.entities_for("occupation", product_work_occupations)
        | index.entities_for(
            "operating_responsibility",
            {"commercial_service_operator"},
        )
    )
    if professional:
        candidates.append(
            (
                "explicit_professional_operator",
                "明确用于职业 / 商业服务的作业者",
                "评论者明确自述相关职业，或明确说明设备用于客户、商业服务或工作任务；工程师、教师等职业背景不会自动被视为商用。",
                professional,
                ("occupation", "operating_responsibility"),
                f"以可重复的效率、可靠性和服务保障完成 {index.category_label} 工作任务。",
            )
        )

    owner_operator = index.entities_for(
        "operating_responsibility",
        {
            "residential_self_maintainer",
            "homeowner_owner_operator",
        },
    )
    if owner_operator:
        candidates.append(
            (
                "residential_self_maintainer",
                "明确承担住宅 / 庭院维护的用户",
                "评论者明确说明自己执行割草、修边、清扫、清雪、清洗等住宅维护动作，或明确自述拥有住宅 / 物业；普通“my yard”不会自动归入。",
                owner_operator,
                ("operating_responsibility",),
                f"用可控的时间和身体负担独立完成自有物业的 {index.category_label} 任务。",
            )
        )

    property_caretaker = index.entities_for(
        "operating_responsibility",
        {
            "landlord_property_manager",
            "commercial_service_operator",
            "institutional_site_caretaker",
            "farm_ranch_property",
        },
    )
    if property_caretaker:
        candidates.append(
            (
                "multi_property_caretaker",
                "多物业 / 客户 / 机构场地维护者",
                "明确负责出租物业、客户场地、机构公共场地或农牧果园，而不是仅从任务地点推断身份。",
                property_caretaker,
                ("operating_responsibility", "occupation"),
                f"跨场地稳定完成 {index.category_label} 任务，并控制停机、切换和维护成本。",
            )
        )

    older_adults = (
        index.entities_for("explicit_age", {"65+"})
        | {
            entity
            for entity, value in index.age_values.items()
            if value >= 65
        }
        | index.entities_for(
            "life_stage_ability", {"senior", "retired"}
        )
    )
    if older_adults:
        candidates.append(
            (
                "explicit_older_adults",
                "明确 65+ / 退休用户",
                "来源年龄档案为 65+、正文明确年龄 ≥65，或明确自述退休；年龄本身不等于体能受限。",
                older_adults,
                ("explicit_age", "life_stage_ability"),
                f"在不预设身体能力的前提下，清晰完成 {index.category_label} 的启动、操控与维护流程。",
            )
        )

    physical_constraints = index.entities_for(
        "life_stage_ability",
        {
            "limited_strength",
            "mobility_limit",
            "joint_back_condition",
        },
    )
    if physical_constraints:
        candidates.append(
            (
                "explicit_physical_constraints",
                "明确体力 / 行动受限用户",
                "评论者明确提到力量、握力、行动、关节、腰背或肩部限制；不会仅凭年龄推断。",
                physical_constraints,
                ("life_stage_ability",),
                f"以更低握持、推拉、搬运和操作负担完成 {index.category_label} 任务。",
            )
        )

    novice = index.entities_for(
        "experience_path",
        {
            "first_electric_mower",
            "first_battery_blower",
            "first_time_owner",
        },
    )
    if novice:
        candidates.append(
            (
                "first_time_category_users",
                "明确首次进入该品类 / 电池平台的用户",
                "评论中明确出现 first、new to 等首次使用表达；不会根据购买时间推断新手。",
                novice,
                ("experience_path",),
                f"无需既有经验即可正确完成 {index.category_label} 的装配、启动、调节与收尾。",
            )
        )

    experienced = index.entities_for(
        "experience_path",
        {
            "gas_switcher",
            "corded_switcher",
            "previous_mower_owner",
            "previous_blower_owner",
            "previous_product_owner",
            "long_time_user",
        },
    )
    if experienced:
        candidates.append(
            (
                "experienced_switchers",
                "明确有旧机经验 / 动力平台迁移者",
                "评论者明确比较旧机、长期经验，或说明从燃油 / 有线产品迁移。",
                experienced,
                ("experience_path",),
                f"在保留既有作业能力的同时，降低 {index.category_label} 的启动、维护和能源管理负担。",
            )
        )

    rows: list[dict[str, Any]] = []
    for (
        persona_id,
        title,
        definition,
        entities,
        dimensions,
        jtbd,
    ) in candidates:
        if not entities:
            continue
        focus_aspects = tuple(aspect_rank[:8])
        lifts = []
        for aspect_id in focus_aspects:
            cohort_mentions = len(entities & index.aspect_entities[aspect_id])
            cohort_rate = cohort_mentions / len(entities)
            baseline = overall_rate.get(aspect_id, 0)
            lifts.append(
                {
                    "aspect_id": aspect_id,
                    "label": aspect_labels.get(aspect_id, aspect_id),
                    "review_entities": cohort_mentions,
                    "cohort_rate": round(cohort_rate, 6),
                    "overall_rate": round(baseline, 6),
                    "lift": (
                        round(cohort_rate / baseline, 3)
                        if baseline
                        else None
                    ),
                }
            )
        lifts.sort(
            key=lambda row: (
                -(row["lift"] or 0),
                -row["review_entities"],
                row["aspect_id"],
            )
        )
        rows.append(
            {
                "persona_id": persona_id,
                "title": title,
                "definition": definition,
                "jtbd": jtbd,
                **index.stats(entities),
                "distinctive_aspects": lifts[:4],
                "evidence_ids": index.evidence_for_entities(
                    entities,
                    dimension_ids=dimensions,
                    limit=18,
                ),
                "evidence_population": len(entities),
                "boundary": (
                    "只使用明确职业、责任、年龄 / 体能或经验信号；购买 / 共用关系和任务场景不作为人物身份。群体可重叠，不能外推市场人口占比。"
                ),
            }
        )
    rows.sort(
        key=lambda row: (-row["review_entities"], row["persona_id"])
    )
    return rows


def _kano_rows(
    index: OpeIndex, aspect_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not index.profile_id.startswith(("mower/", "blower/")):
        return _generic_kano_rows(index, aspect_rows)
    by_id = {row["aspect_id"]: row for row in aspect_rows}
    mower_specs = [
        (
            "must_be",
            "必备型 M",
            [
                ("reliability", "持续工作、不无故趴窝"),
                ("starting", "按流程可靠启动"),
                ("safety", "基本物理与电气安全"),
                ("battery_charger", "电池与充电器可正常工作"),
            ],
            "缺失会迅速制造低星与退换；满足本身通常不构成宣传记忆点。",
        ),
        (
            "performance",
            "期望型 O",
            [
                ("runtime", "续航与一次完成度"),
                ("cut_quality", "切割均匀与草面效果"),
                ("power", "厚草 / 高草负载能力"),
                ("self_propel", "自走速度与可控性"),
                ("charging", "充电与换电效率"),
                ("bagging", "集草效率与容量"),
                ("mulching_discharge", "碎草与排草效果"),
                ("value", "性能与价格的交换"),
            ],
            "表现越好，满意度越高；应按具体工况和目标值定义，不只写“更强”。",
        ),
        (
            "attractive",
            "魅力型 A（假设）",
            [
                ("maintenance_gasfree", "免燃油、免拉绳的生活方式转变"),
                ("noise", "安静到可以改变可用时段"),
                ("storage", "折叠 / 直立收纳释放车库空间"),
                ("ecosystem", "电池跨工具复用与平台便利"),
            ],
            "来自惊喜语言与积极记忆的候选；仍需标准 Kano 问卷验证。",
        ),
        (
            "unclear",
            "无差异 / 未定 I",
            [
                ("smart_connectivity", "App、连接与自动化"),
            ],
            "直接需求与样本不足，不能把技术可能性误写成用户价值。",
        ),
    ]
    blower_specs = [
        (
            "must_be",
            "必备型 M",
            [
                ("reliability", "持续工作、不无故趴窝"),
                ("starting", "按流程可靠启动"),
                ("safety", "基本物理与电气安全"),
                ("battery_charger", "电池与充电器可正常工作"),
            ],
            "缺失会迅速制造低星与退换；满足本身通常不构成宣传记忆点。",
        ),
        (
            "performance",
            "期望型 O",
            [
                ("blowing_power", "真实碎屑负载下的吹力"),
                ("runtime", "续航与一次任务完成度"),
                ("airflow_control", "低速精细与高功率切换"),
                ("weight_balance", "长时间手持的重量和平衡"),
                ("ergonomics", "握持、背带和扳机负担"),
                ("wet_debris", "湿叶与重碎屑处理"),
                ("noise", "噪音与振动"),
                ("value", "性能与价格的交换"),
            ],
            "表现越好，满意度越高；必须放在碎屑、面积和作业时长中定义。",
        ),
        (
            "attractive",
            "魅力型 A（假设）",
            [
                ("maintenance_gasfree", "免燃油、免拉绳和摆脱电源线"),
                ("ecosystem", "电池跨工具复用与平台便利"),
                ("nozzle_attachment", "快速切换喷嘴和扩展任务"),
                ("storage", "轻松悬挂、收纳与携带"),
            ],
            "来自惊喜语言与积极记忆的候选；仍需标准 Kano 问卷验证。",
        ),
        (
            "unclear",
            "无差异 / 未定 I",
            [("smart_connectivity", "App、连接与自动化")],
            "直接需求与样本不足，不能把技术可能性误写成用户价值。",
        ),
    ]
    specs = (
        blower_specs
        if index.profile_id.startswith("blower/")
        else mower_specs
    )
    rows = []
    for category_id, title, aspects, principle in specs:
        items = []
        for aspect_id, statement in aspects:
            row = by_id.get(aspect_id)
            if not row:
                continue
            entities = index.aspect_entities[aspect_id]
            if category_id == "must_be":
                support = (
                    index.aspect_negative[aspect_id]
                    | index.aspect_expectation[aspect_id]
                    | {
                        entity
                        for entity in entities
                        if index.facts[entity].get("rating") is not None
                        and index.facts[entity]["rating"] <= 2
                    }
                )
                polarity = "negative"
            elif category_id == "attractive":
                support = (
                    index.aspect_delight[aspect_id]
                    | index.aspect_positive[aspect_id]
                )
                polarity = "positive"
            elif category_id == "unclear":
                support = entities
                polarity = None
            else:
                support = entities
                polarity = None
            items.append(
                {
                    "aspect_id": aspect_id,
                    "label": row["label"],
                    "statement": statement,
                    "mention_entities": row["review_entities"],
                    "support_entities": len(support),
                    "evidence_population": len(support),
                    "average_rating": row["average_rating"],
                    "low_star_rate": row["low_star_rate"],
                    "delight_entities": row["delight_entities"],
                    "evidence_ids": index.evidence_for_entities(
                        support,
                        aspects=(aspect_id,),
                        polarity=polarity,
                        limit=12,
                        prefer_low=category_id == "must_be",
                    ),
                }
            )
        items.sort(
            key=lambda item: (
                -item["support_entities"],
                item["aspect_id"],
            )
        )
        rows.append(
            {
                "category_id": category_id,
                "title": title,
                "principle": principle,
                "items": items,
                "boundary": (
                    "这是基于网评语言和结果关联的 Kano 假设，不是功能失/得双问卷的正式分类。"
                ),
            }
        )
    return rows


def _generic_kano_rows(
    index: OpeIndex, aspect_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {row["aspect_id"]: row for row in aspect_rows}
    all_ids = set(by_id)
    must_ids = [
        aspect_id
        for aspect_id in (
            "reliability",
            "starting",
            "safety",
            "battery_charger",
        )
        if aspect_id in all_ids
    ]
    unclear_ids = [
        aspect_id
        for aspect_id in ("smart_connectivity",)
        if aspect_id in all_ids
    ]
    attractive_candidates = {
        "maintenance_gasfree",
        "ecosystem",
        "storage",
        "portability",
        "attachments",
        "nozzles_accessories",
        "display_controls",
    }
    attractive_ids = [
        row["aspect_id"]
        for row in sorted(
            aspect_rows,
            key=lambda row: (
                -row["delight_entities"],
                -row["explicit_positive_entities"],
                row["aspect_id"],
            ),
        )
        if row["aspect_id"] in attractive_candidates
        and row["aspect_id"] not in set(must_ids + unclear_ids)
    ][:4]
    assigned = set(must_ids + attractive_ids + unclear_ids)
    performance_ids = [
        row["aspect_id"]
        for row in sorted(
            aspect_rows,
            key=lambda row: (-row["review_entities"], row["aspect_id"]),
        )
        if row["aspect_id"] not in assigned and row["review_entities"]
    ][:8]
    specs = (
        (
            "must_be",
            "必备型 M",
            must_ids,
            "缺失会迅速制造低星、停机或安全风险；满足本身通常不形成差异化。",
        ),
        (
            "performance",
            "期望型 O",
            performance_ids,
            "表现越好通常满意度越高；需要放进该品类的任务和负载条件中定义。",
        ),
        (
            "attractive",
            "魅力型 A（假设）",
            attractive_ids,
            "来自明确惊喜或正向记忆的候选，仍需标准 Kano 双问题问卷验证。",
        ),
        (
            "unclear",
            "无差异 / 未定 I",
            unclear_ids,
            "直接需求或样本不足，不能把技术可能性误写成用户价值。",
        ),
    )
    rows: list[dict[str, Any]] = []
    for category_id, title, aspect_ids, principle in specs:
        items = []
        for aspect_id in aspect_ids:
            row = by_id[aspect_id]
            entities = index.aspect_entities[aspect_id]
            if category_id == "must_be":
                support = (
                    index.aspect_negative[aspect_id]
                    | index.aspect_expectation[aspect_id]
                    | {
                        entity
                        for entity in entities
                        if index.facts[entity].get("rating") is not None
                        and index.facts[entity]["rating"] <= 2
                    }
                )
                polarity = "negative"
            elif category_id == "attractive":
                support = (
                    index.aspect_delight[aspect_id]
                    | index.aspect_positive[aspect_id]
                )
                polarity = "positive"
            else:
                support = entities
                polarity = None
            items.append(
                {
                    "aspect_id": aspect_id,
                    "label": row["label"],
                    "statement": f"{index.category_label} 的 {row['label']}",
                    "mention_entities": row["review_entities"],
                    "support_entities": len(support),
                    "evidence_population": len(support),
                    "average_rating": row["average_rating"],
                    "low_star_rate": row["low_star_rate"],
                    "delight_entities": row["delight_entities"],
                    "evidence_ids": index.evidence_for_entities(
                        support,
                        aspects=(aspect_id,),
                        polarity=polarity,
                        limit=12,
                        prefer_low=category_id == "must_be",
                    ),
                }
            )
        rows.append(
            {
                "category_id": category_id,
                "title": title,
                "principle": principle,
                "items": items,
                "boundary": (
                    "这是基于网评语言和结果关联的 Kano 假设，不是功能失/得双问卷的正式分类。"
                ),
            }
        )
    return rows


def _opportunity_rows(index: OpeIndex) -> list[dict[str, Any]]:
    if index.profile_id.startswith("blower/"):
        return _blower_opportunity_rows(index)
    if not index.profile_id.startswith("mower/"):
        return _generic_opportunity_rows(index)
    need_entities = {
        entity
        for entity, fact in index.facts.items()
        if EXPLICIT_NEED_RE.search(str(fact.get("review_text") or ""))
    }
    negative_aspects = {
        aspect: set(index.aspect_negative[aspect])
        | {
            entity
            for entity in index.aspect_entities[aspect]
            if index.facts[entity].get("rating") is not None
            and index.facts[entity]["rating"] <= 2
        }
        for aspect in (rule.label for rule in index.aspect_rules)
    }
    large_yard = index.entities_for(
        "yard_size", {"½–1 acre", "1–2 acres", "2+ acres"}
    ) | index.entities_for("grass_condition")
    specs = [
        {
            "opportunity_id": "finishability",
            "title": "把“能否一次割完”变成可预期的整院能力",
            "problem": "续航、充电与草况被分开描述，用户真正任务是不中断完成整院。",
            "entities": (
                index.aspect_entities["runtime"]
                | index.aspect_entities["charging"]
                | index.entities_for("job_completion")
            )
            & (
                large_yard
                | index.entities_for("charging_workflow")
                | index.aspect_entities["battery_charger"]
            ),
            "negative": (
                negative_aspects["runtime"]
                | negative_aspects["charging"]
                | negative_aspects["battery_charger"]
            ),
            "dimensions": (
                "yard_size",
                "grass_condition",
                "charging_workflow",
                "job_completion",
            ),
            "aspects": ("runtime", "charging", "battery_charger"),
            "product_direction": "统一定义“典型草况 × 面积 × 电池组合”的完成能力，并让包装、选型和充电流程使用同一口径。",
            "validation": "按面积、草况、切高和电池组合做任务级台架与家庭实测；不要只测空载分钟数。",
        },
        {
            "opportunity_id": "adaptive_drive",
            "title": "让自走“跟得上人”，而不是让人追着机器",
            "problem": "速度、启停、转向和坡地控制与用户体能直接耦合。",
            "entities": index.aspect_entities["self_propel"]
            & (
                index.entities_for("life_stage_ability")
                | index.entities_for("explicit_age")
                | index.entities_for("terrain")
                | index.aspect_entities["handling"]
            ),
            "negative": negative_aspects["self_propel"]
            | negative_aspects["handling"],
            "dimensions": ("life_stage_ability", "explicit_age", "terrain"),
            "aspects": ("self_propel", "handling"),
            "product_direction": "把速度曲线、起步斜率、转弯阻力和坡地限速拆成可验证的操控需求。",
            "validation": "用银发、力量受限和坡地用户做步速匹配测试，记录误触、追赶、拖拽与转弯负担。",
        },
        {
            "opportunity_id": "demanding_cut",
            "title": "为厚草、湿草与高草建立可解释的性能边界",
            "problem": "轻载满意不代表高负载任务成功；切质、动力与停转需要按工况联读。",
            "entities": (
                index.aspect_entities["cut_quality"]
                | index.aspect_entities["power"]
                | index.aspect_entities["reliability"]
            )
            & index.entities_for("grass_condition"),
            "negative": negative_aspects["cut_quality"]
            | negative_aspects["power"]
            | negative_aspects["reliability"],
            "dimensions": ("grass_condition", "cut_height", "weather"),
            "aspects": ("cut_quality", "power", "reliability"),
            "product_direction": "围绕一次通过率、漏割、停转、草屑处理和能耗，定义工况分级而非单一“HP”卖点。",
            "validation": "建立草种、含水率、初始草高、目标割高与速度的测试矩阵。",
        },
        {
            "opportunity_id": "clipping_flow",
            "title": "把集草 / 碎草从附件功能变成连续作业流程",
            "problem": "装袋、满袋、堵塞、碎草与侧排共同决定停机次数和二次整理。",
            "entities": (
                index.aspect_entities["bagging"]
                | index.aspect_entities["mulching_discharge"]
            )
            & (
                index.entities_for("clipping_mode")
                | index.entities_for("grass_condition")
            ),
            "negative": negative_aspects["bagging"]
            | negative_aspects["mulching_discharge"],
            "dimensions": ("clipping_mode", "grass_condition"),
            "aspects": ("bagging", "mulching_discharge"),
            "product_direction": "用“每次倾倒间隔、堵塞清理次数、草屑均匀度”定义完整工作流。",
            "validation": "在湿草、高草、落叶和不同前进速度下对三种草屑模式做任务级测试。",
        },
        {
            "opportunity_id": "reliability_recovery",
            "title": "从“突然趴窝”到可定位、可恢复、可服务",
            "problem": "故障本身与无法判断原因、重复尝试和售后往返一起放大损失。",
            "entities": (
                index.aspect_entities["reliability"]
                | index.aspect_entities["starting"]
                | index.aspect_entities["battery_charger"]
            )
            & (
                negative_aspects["reliability"]
                | negative_aspects["starting"]
                | negative_aspects["battery_charger"]
                | index.aspect_entities["service"]
            ),
            "negative": negative_aspects["reliability"]
            | negative_aspects["starting"]
            | negative_aspects["battery_charger"],
            "dimensions": (),
            "aspects": (
                "reliability",
                "starting",
                "battery_charger",
                "service",
            ),
            "product_direction": "建立故障码—用户动作—恢复结果—服务工单的闭环，优先处理高损失且可复现的问题链。",
            "validation": "按型号、部件、购后时长与触发工况复核；接入真实保修数据前不把评论命中当故障率。",
        },
        {
            "opportunity_id": "storage_accessibility",
            "title": "让搬运、折叠与收纳不抵消电动化的省力价值",
            "problem": "重量、把手、折叠和垂直收纳共同决定从车库到草坪的第一段体验。",
            "entities": (
                index.aspect_entities["handling"]
                | index.aspect_entities["storage"]
                | index.aspect_entities["assembly"]
            )
            & (
                index.entities_for("storage")
                | index.entities_for("life_stage_ability")
                | index.entities_for("explicit_age")
            ),
            "negative": negative_aspects["handling"]
            | negative_aspects["storage"]
            | negative_aspects["assembly"],
            "dimensions": ("storage", "life_stage_ability", "explicit_age"),
            "aspects": ("handling", "storage", "assembly"),
            "product_direction": "以单人提拿、折叠动作数、把手受力和占地面积定义收纳可用性。",
            "validation": "对不同身高、握力和车库空间做端到端任务测试，而非只验证能否折叠。",
        },
    ]
    rows = []
    for spec in specs:
        entities = set(spec["entities"])
        negative = entities & set(spec["negative"])
        explicit_need = entities & need_entities
        stats = index.stats(entities)
        stats.update(
            {
                "negative_entities": len(negative),
                "negative_rate": _ratio(len(negative), len(entities)),
                "explicit_need_entities": len(explicit_need),
                "explicit_need_rate": _ratio(
                    len(explicit_need), len(entities)
                ),
            }
        )
        evidence_ids = index.evidence_for_entities(
            negative or entities,
            aspects=spec["aspects"],
            polarity="negative",
            limit=18,
            prefer_low=True,
        )
        if len(evidence_ids) < 12:
            evidence_ids.extend(
                evidence_id
                for evidence_id in index.evidence_for_entities(
                    negative or entities,
                    dimension_ids=spec["dimensions"],
                    aspects=spec["aspects"],
                    polarity="rating_context_negative",
                    limit=18,
                    prefer_low=True,
                )
                if evidence_id not in evidence_ids
            )
            evidence_ids = index.dedupe_evidence_ids(
                evidence_ids,
                limit=18,
            )
        rows.append(
            {
                key: value
                for key, value in spec.items()
                if key not in {"entities", "negative", "dimensions", "aspects"}
            }
            | stats
            | {
                "evidence_ids": evidence_ids,
                "evidence_population": len(negative or entities),
                "confidence": (
                    "high"
                    if len(entities) >= 200
                    else "moderate"
                    if len(entities) >= 50
                    else "directional"
                ),
                "boundary": (
                    "网评提供问题证据，不包含成本、战略与工程可行性；不作为路线图分数。"
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            -row["negative_entities"],
            -row["review_entities"],
        )
    )
    return rows


def _generic_opportunity_rows(index: OpeIndex) -> list[dict[str, Any]]:
    need_entities = {
        entity
        for entity, fact in index.facts.items()
        if EXPLICIT_NEED_RE.search(str(fact.get("review_text") or ""))
    }
    dimension_by_id = {
        dimension.dimension_id: dimension for dimension in index.dimensions
    }
    scenario_dimensions = tuple(
        dimension_id
        for dimension_id in (
            "task_context",
            "material_context",
            "workflow_context",
            "property_context",
            "weather",
        )
        if dimension_id in dimension_by_id
    )
    aspect_rows = []
    aspect_display = {
        rule.label: rule.display for rule in index.aspect_rules
    }
    for aspect_id in aspect_display:
        entities = set(index.aspect_entities[aspect_id])
        negative = set(index.aspect_negative[aspect_id]) | {
            entity
            for entity in entities
            if index.facts[entity].get("rating") is not None
            and index.facts[entity]["rating"] <= 2
        }
        if not entities:
            continue
        aspect_rows.append(
            (
                aspect_id,
                entities,
                negative,
                len(negative),
                len(entities),
            )
        )
    aspect_rows.sort(key=lambda row: (-row[3], -row[4], row[0]))

    rows: list[dict[str, Any]] = []
    for aspect_id, entities, negative, _negative_n, _mention_n in aspect_rows[:6]:
        context_id = None
        context_label = None
        context_entities: set[str] = set()
        best_overlap = 0
        for dimension_id in scenario_dimensions:
            dimension = dimension_by_id[dimension_id]
            for label, label_entities in index.dimension_entities.get(
                dimension_id, {}
            ).items():
                overlap = len(entities & label_entities)
                if overlap <= best_overlap:
                    continue
                best_overlap = overlap
                context_id = dimension_id
                context_entities = set(label_entities)
                context_label = next(
                    (
                        rule.display
                        for rule in dimension.rules
                        if rule.label == label
                    ),
                    label,
                )
        scoped_entities = (
            entities & context_entities if context_entities else entities
        )
        scoped_negative = (
            negative & scoped_entities if scoped_entities else negative
        )
        explicit_need = scoped_entities & need_entities
        stats = index.stats(scoped_entities)
        stats.update(
            {
                "negative_entities": len(scoped_negative),
                "negative_rate": _ratio(
                    len(scoped_negative), len(scoped_entities)
                ),
                "explicit_need_entities": len(explicit_need),
                "explicit_need_rate": _ratio(
                    len(explicit_need), len(scoped_entities)
                ),
            }
        )
        label = aspect_display[aspect_id]
        context_copy = (
            f"在“{context_label}”场景中" if context_label else ""
        )
        evidence_entities = scoped_negative or scoped_entities
        rows.append(
            {
                "opportunity_id": f"improve_{aspect_id}",
                "title": f"{context_copy}系统改善{label}",
                "problem": (
                    f"{context_copy}{label}出现明确负向或低星关联；"
                    "当前证据只能定位问题池，不能直接给出工程原因。"
                ),
                **stats,
                "product_direction": (
                    f"把{label}拆成任务结果、触发条件、恢复动作和可验证边界，"
                    f"并在 {index.category_label} 真实负载下定义验收口径。"
                ),
                "validation": (
                    f"按{context_label or '核心任务、对象与工作流'}分层复核原文，"
                    "再用台架、家庭任务和售后数据验证原因及优先级。"
                ),
                "evidence_ids": index.evidence_for_entities(
                    evidence_entities,
                    dimension_ids=(context_id,) if context_id else (),
                    aspects=(aspect_id,),
                    limit=18,
                    prefer_low=True,
                ),
                "evidence_population": len(evidence_entities),
                "confidence": (
                    "high"
                    if len(scoped_entities) >= 200
                    else "moderate"
                    if len(scoped_entities) >= 50
                    else "directional"
                ),
                "boundary": (
                    "网评提供问题证据，不包含成本、战略与工程可行性；不作为路线图分数。"
                ),
            }
        )
    return rows


def _blower_opportunity_rows(index: OpeIndex) -> list[dict[str, Any]]:
    need_entities = {
        entity
        for entity, fact in index.facts.items()
        if EXPLICIT_NEED_RE.search(str(fact.get("review_text") or ""))
    }

    def negative(*aspect_ids: str) -> set[str]:
        rows: set[str] = set()
        for aspect_id in aspect_ids:
            rows |= index.aspect_negative[aspect_id]
            rows |= {
                entity
                for entity in index.aspect_entities[aspect_id]
                if index.facts[entity].get("rating") is not None
                and index.facts[entity]["rating"] <= 2
            }
        return rows

    specs = [
        {
            "opportunity_id": "cleanup_finishability",
            "title": "把“能否一次清完”变成可预期的任务能力",
            "problem": "续航、风量、面积和碎屑负载被分开描述，用户真正任务是不中断地完成清理。",
            "entities": (
                index.aspect_entities["runtime"]
                | index.aspect_entities["charging"]
                | index.entities_for("job_completion")
            )
            & (
                index.entities_for("work_area")
                | index.entities_for("debris_condition")
                | index.entities_for("yard_size")
            ),
            "negative": negative("runtime", "charging", "battery_charger"),
            "dimensions": (
                "work_area",
                "debris_condition",
                "yard_size",
                "job_completion",
                "charging_workflow",
            ),
            "aspects": ("runtime", "charging", "battery_charger"),
            "product_direction": "用“区域 × 碎屑类型 / 干湿 × 风量档位 × 电池组合”定义任务完成能力。",
            "validation": "对典型区域和碎屑负载做整任务测试，同时记录 Turbo 占比、换电次数和剩余电量。",
        },
        {
            "opportunity_id": "heavy_debris_control",
            "title": "从标称风量走向湿叶与重碎屑的可控清理",
            "problem": "最大 CFM / MPH 不能说明湿叶、厚堆和粘附碎屑是否能被移动，也不能说明是否会把砂石和覆盖物吹散。",
            "entities": (
                index.aspect_entities["blowing_power"]
                | index.aspect_entities["wet_debris"]
                | index.aspect_entities["airflow_control"]
            )
            & (
                index.entities_for("debris_type")
                | index.entities_for("debris_condition")
            ),
            "negative": negative(
                "blowing_power", "wet_debris", "airflow_control"
            ),
            "dimensions": (
                "debris_type",
                "debris_condition",
                "airflow_workflow",
            ),
            "aspects": (
                "blowing_power",
                "wet_debris",
                "airflow_control",
            ),
            "product_direction": "建立低速精细—持续高风量—短时 Turbo 的任务曲线，并按碎屑类型给出可解释边界。",
            "validation": "在干叶、湿叶、松针、橡果、砂石边界和花坛场景测清理时间、二次返工与扰动。",
        },
        {
            "opportunity_id": "fatigue_balance",
            "title": "把整机重量改写为连续作业中的疲劳与平衡",
            "problem": "电池位置、喷管力矩、握把、扳机和背带共同决定手臂疲劳，单一重量参数无法解释。",
            "entities": (
                index.aspect_entities["weight_balance"]
                | index.aspect_entities["ergonomics"]
            )
            & (
                index.entities_for("life_stage_ability")
                | index.entities_for("task_duration")
                | index.entities_for("form_factor")
            ),
            "negative": negative("weight_balance", "ergonomics"),
            "dimensions": (
                "life_stage_ability",
                "task_duration",
                "form_factor",
            ),
            "aspects": ("weight_balance", "ergonomics"),
            "product_direction": "按不同电池、握姿和时长定义手腕力矩、换手频率、触发负担和主观疲劳。",
            "validation": "招募不同身高、握力和年龄用户完成 15/30/60 分钟任务，比较手持与背负方案。",
        },
        {
            "opportunity_id": "precision_airflow",
            "title": "让强吹、精细清理和扬尘控制成为一套连续操作",
            "problem": "同一用户既要移动湿叶，也要避免吹散覆盖物、砂石或制造扬尘。",
            "entities": (
                index.aspect_entities["airflow_control"]
                | index.aspect_entities["dust_control"]
                | index.aspect_entities["nozzle_attachment"]
            )
            & (
                index.entities_for("airflow_workflow")
                | index.entities_for("work_area")
            ),
            "negative": negative(
                "airflow_control", "dust_control", "nozzle_attachment"
            ),
            "dimensions": (
                "airflow_workflow",
                "work_area",
                "form_factor",
            ),
            "aspects": (
                "airflow_control",
                "dust_control",
                "nozzle_attachment",
            ),
            "product_direction": "优化低速可控区间、扳机映射、定速和喷嘴切换，减少二次整理与环境扰动。",
            "validation": "在花坛、碎石、车库灰尘和人行道上测目标物移动、非目标物扰动和完成时间。",
        },
        {
            "opportunity_id": "reliability_recovery",
            "title": "从突然停机到可定位、可恢复、可服务",
            "problem": "故障、电池 / 充电器异常和售后往返共同放大季节性清理任务损失。",
            "entities": (
                index.aspect_entities["reliability"]
                | index.aspect_entities["starting"]
                | index.aspect_entities["battery_charger"]
                | index.aspect_entities["service"]
            )
            & negative(
                "reliability", "starting", "battery_charger", "service"
            ),
            "negative": negative(
                "reliability", "starting", "battery_charger", "service"
            ),
            "dimensions": (),
            "aspects": (
                "reliability",
                "starting",
                "battery_charger",
                "service",
            ),
            "product_direction": "建立故障码—用户动作—恢复结果—服务工单闭环，优先处理高损失且可复现的问题链。",
            "validation": "按型号、购后时长、环境温度、进气 / 喷管状态和电池组合复核；评论命中不等于故障率。",
        },
        {
            "opportunity_id": "attachment_workflow",
            "title": "把喷嘴和扩展附件做成可理解的任务系统",
            "problem": "附件价值取决于何时用、如何切换和是否真正改善目标碎屑，而不是包装中附件数量。",
            "entities": index.aspect_entities["nozzle_attachment"]
            | index.entities_for("form_factor"),
            "negative": negative("nozzle_attachment"),
            "dimensions": ("form_factor", "debris_type", "work_area"),
            "aspects": ("nozzle_attachment", "blowing_power"),
            "product_direction": "用任务语言说明聚风嘴、扁嘴、延长管和排水沟附件的适用负载与切换路径。",
            "validation": "测附件装拆时间、误用率、清理时间和存放完整性，淘汰无明确任务收益的附件。",
        },
    ]
    rows: list[dict[str, Any]] = []
    for spec in specs:
        entities = set(spec["entities"])
        negative_entities = entities & set(spec["negative"])
        explicit_need = entities & need_entities
        stats = index.stats(entities)
        stats.update(
            {
                "negative_entities": len(negative_entities),
                "negative_rate": _ratio(
                    len(negative_entities), len(entities)
                ),
                "explicit_need_entities": len(explicit_need),
                "explicit_need_rate": _ratio(
                    len(explicit_need), len(entities)
                ),
            }
        )
        evidence_ids = index.evidence_for_entities(
            negative_entities or entities,
            dimension_ids=spec["dimensions"],
            aspects=spec["aspects"],
            limit=18,
            prefer_low=True,
        )
        rows.append(
            {
                key: value
                for key, value in spec.items()
                if key
                not in {"entities", "negative", "dimensions", "aspects"}
            }
            | stats
            | {
                "evidence_ids": evidence_ids,
                "evidence_population": len(
                    negative_entities or entities
                ),
                "confidence": (
                    "high"
                    if len(entities) >= 200
                    else "moderate"
                    if len(entities) >= 50
                    else "directional"
                ),
                "boundary": (
                    "网评提供问题证据，不包含成本、战略与工程可行性；不作为路线图分数。"
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            -row["negative_entities"],
            -row["review_entities"],
        )
    )
    return rows


def _smart_rows(
    index: OpeIndex, _opportunities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if index.profile_id.startswith("blower/"):
        return _blower_smart_rows(index)
    if not index.profile_id.startswith("mower/"):
        return _generic_smart_rows(index, _opportunities)
    explicit_smart = index.aspect_entities["smart_connectivity"]

    def problem_entities(*aspect_ids: str) -> set[str]:
        entities: set[str] = set()
        for aspect_id in aspect_ids:
            entities |= index.aspect_negative[aspect_id]
            entities |= {
                entity
                for entity in index.aspect_entities[aspect_id]
                if index.facts[entity].get("rating") is not None
                and index.facts[entity]["rating"] <= 2
            }
        return entities

    specs = [
        (
            "A1",
            "能力补全",
            "故障诊断 + 预测维护",
            "reliability_recovery",
            "从症状、部件状态和历史事件定位原因，失效前给出可执行预警。",
            "故障码、部件遥测、维修结果、保修标签",
            "错报会损伤信任；必须给出不确定性和人工服务入口。",
        ),
        (
            "A2",
            "摩擦消除",
            "割高与草屑处理切换摩擦消除",
            "clipping_flow",
            "记住并协助完成割高、集草、碎草和侧排之间反复发生的设置与切换。",
            "割高、草屑模式、堵塞 / 满袋状态、切换动作与任务结果",
            "自动操作必须可撤销；不能在护罩、刀片或草袋未安全就位时执行。",
        ),
        (
            "A3",
            "认知降负",
            "整院完成度预测",
            "finishability",
            "把面积、草况、割高、步速和电池状态转成“能否完成 / 还差多少”。",
            "面积、草高、负载、速度、实时能耗、SOH",
            "先显示区间与影响因素，避免给出伪精确的单点分钟数。",
        ),
        (
            "A4",
            "门槛降低",
            "草况自适应的动力与刀速控制",
            "demanding_cut",
            "在厚草、湿草和高草中自动调节输出，帮助普通用户获得稳定切质。",
            "负载、电流、刀速、行进速度、草况标签",
            "自适应不能掩盖物理能力边界，需明确何时建议减速或二次通过。",
        ),
        (
            "A5",
            "哲学翻转",
            "以人步速为中心的自走控制",
            "adaptive_drive",
            "机器学习用户步速和转向习惯，起步、转弯和坡地时主动保持可控。",
            "轮速、坡度、把手输入、用户调整历史",
            "安全优先；任何学习策略都必须有硬限速和即时人工覆盖。",
        ),
        (
            "A6",
            "身份表达",
            "“割草机里的 Tesla”式科技体验",
            None,
            "把安静、无油污、生态复用和专业完成感转成可分享的使用成就。",
            "任务完成记录、节省的燃油维护、用户自愿分享",
            "直接智能需求很少，不能为了“科技感”制造无价值连接。",
        ),
        (
            "A7",
            "环境编程",
            "电池存储与天气保护",
            "storage_accessibility",
            "结合温度、季节与存放状态，主动建议或执行适合的电池保护策略。",
            "环境温度、存储时长、充电状态、电池 SOH",
            "涉及充电安全的动作必须遵循硬件 BMS 规则，不由云端策略越权。",
        ),
        (
            "A8",
            "管家进化",
            "跨季节维护与个人设置记忆",
            "reliability_recovery",
            "记住常用割高、自走速度、地块与维护历史，下一次直接恢复合适设置。",
            "用户选择、地块档案、维护记录、设备生命周期",
            "默认本地最小化数据；用户能查看、编辑和删除记忆。",
        ),
    ]
    rows = []
    for code, method, title, source_id, value, data, guardrail in specs:
        evidence_queries = {
            "A1": (
                problem_entities(
                    "reliability",
                    "service",
                    "starting",
                    "battery_charger",
                ),
                (),
                ("reliability", "service", "starting", "battery_charger"),
            ),
            "A2": (
                problem_entities(
                    "height_adjustment", "bagging", "mulching_discharge"
                ),
                ("clipping_mode", "grass_condition", "cut_height"),
                ("height_adjustment", "bagging", "mulching_discharge"),
            ),
            "A3": (
                problem_entities("runtime")
                | index.entities_for("job_completion", {"not_completed"}),
                ("yard_size", "job_completion", "task_duration"),
                ("runtime",),
            ),
            "A4": (
                problem_entities("power", "cut_quality")
                & (
                    index.entities_for("grass_condition")
                    | index.entities_for("cut_height")
                ),
                ("grass_condition", "cut_height"),
                ("power", "cut_quality"),
            ),
            "A5": (
                problem_entities("self_propel", "handling"),
                ("terrain", "life_stage_ability"),
                ("self_propel", "handling"),
            ),
            "A6": (
                explicit_smart,
                (),
                ("smart_connectivity",),
            ),
            "A7": (
                problem_entities("storage", "battery_charger"),
                ("storage", "weather"),
                ("storage", "battery_charger"),
            ),
            "A8": (
                problem_entities(
                    "height_adjustment",
                    "starting",
                    "storage",
                ),
                ("cut_height", "storage"),
                ("height_adjustment", "starting"),
            ),
        }
        (
            source_entity_set,
            source_dimensions,
            source_aspects,
        ) = evidence_queries[code]
        source_entities = len(source_entity_set)
        if not source_entity_set:
            continue
        if code == "A6" and len(explicit_smart) < 3:
            continue
        source_evidence = index.evidence_for_entities(
            source_entity_set,
            dimension_ids=source_dimensions,
            aspects=source_aspects,
            limit=12,
            prefer_low=code in {"A1", "A2", "A3", "A4", "A5", "A7"},
        )
        explicit_overlap = (
            len(
                explicit_smart
                & {
                    evidence["review_entity_id"]
                    for evidence_id in source_evidence
                    if (
                        evidence := index.evidence.rows.get(evidence_id)
                    )
                    and evidence.get("review_entity_id")
                }
            )
            if source_evidence
            else 0
        )
        rows.append(
            {
                "code": code,
                "method": method,
                "title": title,
                "source_opportunity_id": source_id,
                "source_problem_entities": source_entities,
                "evidence_population": source_entities,
                "explicit_smart_entities": (
                    len(explicit_smart) if code == "A6" else explicit_overlap
                ),
                "claim_basis": (
                    "direct_signal"
                    if code == "A6"
                    else "problem_to_capability_hypothesis"
                ),
                "value_hypothesis": value,
                "required_data": data,
                "guardrail": guardrail,
                "evidence_ids": source_evidence[:12],
                "boundary": (
                    "智能化方向是从已量化问题推导的概念假设，不代表用户已直接要求该功能。"
                ),
            }
        )
    return rows


def _enrich_smart_rows(
    rows: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add explicit need/function fields and permit evidence-backed variants.

    A1–A8 are opportunity types rather than eight singleton slots.  The base
    category profile keeps one stable anchor per type; a second row is added
    only when a distinct product-opportunity evidence pool maps cleanly to the
    same type.
    """

    user_needs = {
        "A1": "设备或环境突变时，用户需要补足来不及反应、感知或判断的盲区。",
        "A2": "用户需要一个反复发生、令人厌烦的具体操作摩擦直接消失。",
        "A3": "用户需要把能否完成、还能用多久等黑箱状态变成可信的确定性。",
        "A4": "经验不足的用户也需要在复杂工况下获得接近熟练用户的稳定结果。",
        "A5": "用户需要机器适应自己的步速、动作和任务方式，而不是迁就机器。",
        "A6": "用户需要产品帮助表达专业、科技或理想自我，而不只是展示功能。",
        "A7": "用户不必持续关注，设备也应随环境变化让状态在后台自动变好。",
        "A8": "用户需要设备从工具成长为能跨任务、跨季节持续照料的管家。",
    }
    enriched: list[dict[str, Any]] = []
    used_opportunity_ids: set[str] = set()
    for row in rows:
        item = dict(row)
        code = str(item.get("code") or "")
        definition = smart_method_definition(code)
        item["method"] = definition["name"]
        item["methodology_version"] = SMART_METHOD_VERSION
        item["methodology_core"] = definition["core"]
        item["methodology_value_anchor"] = definition["value_anchor"]
        item["methodology_priority"] = definition["priority"]
        item.setdefault("user_need", user_needs.get(code, item.get("value_hypothesis")))
        item.setdefault("smart_function", item.get("value_hypothesis"))
        if item.get("source_opportunity_id"):
            used_opportunity_ids.add(str(item["source_opportunity_id"]))
        enriched.append(item)

    def opportunity_type(opportunity_id: str) -> str | None:
        lowered = opportunity_id.lower()
        mappings = (
            ("A1", ("reliability", "recovery", "starting", "service")),
            ("A2", ("workflow", "attachment", "clipping", "line_feed")),
            ("A3", ("finishability", "runtime", "charging", "health")),
            (
                "A4",
                (
                    "power",
                    "cut",
                    "airflow",
                    "suction",
                    "effectiveness",
                    "precision",
                ),
            ),
            (
                "A5",
                (
                    "adaptive_drive",
                    "weight",
                    "balance",
                    "handling",
                    "ergonomic",
                    "portability",
                    "reach",
                ),
            ),
            ("A7", ("safety", "storage", "weather", "protection")),
            ("A8", ("maintenance", "lifecycle", "memory")),
        )
        for code, needles in mappings:
            if any(needle in lowered for needle in needles):
                return code
        return None

    extra_codes: set[str] = set()
    for opportunity in opportunities:
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        code = opportunity_type(opportunity_id)
        if (
            not code
            or code in extra_codes
            or opportunity_id in used_opportunity_ids
            or int(opportunity.get("evidence_population") or 0) <= 0
        ):
            continue
        title = str(opportunity.get("title") or "任务机会").strip()
        product_direction = str(
            opportunity.get("product_direction") or ""
        ).strip()
        enriched.append(
            {
                "code": code,
                "method": smart_method_definition(code)["name"],
                "methodology_version": SMART_METHOD_VERSION,
                "methodology_core": smart_method_definition(code)["core"],
                "methodology_value_anchor": smart_method_definition(code)[
                    "value_anchor"
                ],
                "methodology_priority": smart_method_definition(code)[
                    "priority"
                ],
                "title": f"{title}的智能辅助",
                "source_opportunity_id": opportunity_id,
                "source_problem_entities": int(
                    opportunity.get("evidence_population")
                    or opportunity.get("negative_entities")
                    or opportunity.get("review_entities")
                    or 0
                ),
                "evidence_population": int(
                    opportunity.get("evidence_population") or 0
                ),
                "explicit_smart_entities": 0,
                "claim_basis": "problem_to_capability_hypothesis",
                "user_need": str(
                    opportunity.get("problem") or user_needs.get(code) or ""
                ),
                "smart_function": (
                    "把相关任务条件、设备状态、用户调整和完成结果数字化，"
                    f"基于证据给出可解释的建议、提醒或恢复步骤。{product_direction}"
                ),
                "value_hypothesis": (
                    "用可解释的状态感知和任务反馈降低该问题造成的中断与试错。"
                ),
                "required_data": "任务条件、设备状态、用户调整、完成结果与恢复记录",
                "guardrail": (
                    "必须显示依据与不确定性，允许人工覆盖；不能把评论相关性写成故障因果。"
                ),
                "evidence_ids": list(opportunity.get("evidence_ids") or [])[:12],
                "boundary": (
                    "该方向由独立产品问题证据池映射，不代表用户已直接要求智能功能。"
                ),
            }
        )
        extra_codes.add(code)

    counters: dict[str, int] = {}
    for row in enriched:
        code = str(row.get("code") or "")
        counters[code] = counters.get(code, 0) + 1
        row["opportunity_rank_within_type"] = counters[code]
    return enriched


def _generic_smart_rows(
    index: OpeIndex, opportunities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    explicit_smart = set(index.aspect_entities["smart_connectivity"])
    ranked_aspects = [
        (
            rule.label,
            set(index.aspect_negative[rule.label])
            | {
                entity
                for entity in index.aspect_entities[rule.label]
                if index.facts[entity].get("rating") is not None
                and index.facts[entity]["rating"] <= 2
            },
        )
        for rule in index.aspect_rules
        if rule.label != "smart_connectivity"
        and index.aspect_entities[rule.label]
    ]
    ranked_aspects.sort(
        key=lambda item: (
            -len(item[1]),
            -len(index.aspect_entities[item[0]]),
            item[0],
        )
    )
    aspect_display = {
        rule.label: rule.display for rule in index.aspect_rules
    }

    def pick(
        preferred: tuple[str, ...], _fallback: int
    ) -> tuple[str, set[str]]:
        by_id = dict(ranked_aspects)
        for aspect_id in preferred:
            if aspect_id in by_id:
                return aspect_id, set(by_id[aspect_id])
        # A method type may be empty.  Never recycle an unrelated high-volume
        # aspect merely to fill an A1–A8 slot.
        return preferred[0] if preferred else "smart_connectivity", set()

    diagnostics = pick(("reliability", "starting", "battery_charger"), 0)
    friction = pick(
        (
            "assembly",
            "setup",
            "height_adjustment",
            "line_feed",
            "nozzle_attachment",
            "attachment_change",
        ),
        1,
    )
    finishability = pick(("runtime", "charging"), 1)
    adaptive = pick(
        (
            "cutting_power",
            "clearing_power",
            "cleaning_power",
            "suction",
            "output_power",
            "task_effectiveness",
        ),
        2,
    )
    usability = pick(
        (
            "weight_balance",
            "ergonomics",
            "portability",
            "maneuverability",
            "reach_balance",
        ),
        3,
    )
    safety = pick(("safety", "kickback_safety", "safety_control"), 4)
    service = pick(("service", "storage", "assembly"), 5)
    specs = (
        (
            "A1",
            "能力补全",
            "故障诊断与恢复引导",
            diagnostics,
            "把症状、设备状态和历史事件转成可核验的原因候选与恢复步骤。",
            "故障码、部件状态、使用条件、恢复结果、服务标签",
            "必须显示不确定性；无法定位时直接进入人工服务。",
        ),
        (
            "A2",
            "摩擦消除",
            f"{aspect_display.get(friction[0], friction[0])}摩擦消除",
            friction,
            "让反复发生的准备、装配、切换或调节动作直接减少，而不是额外增加一个卖点。",
            "操作步骤、重复动作、设置状态、失败与恢复结果",
            "必须锁定一个可测量的具体摩擦；不能用新增提醒代替真正减少步骤。",
        ),
        (
            "A3",
            "认知降负",
            "剩余作业量与设备健康透明化",
            finishability,
            "把能否完成、还剩多少和设备健康从黑箱变成可信区间，不要求用户额外操作。",
            "任务对象、负载、SOC/SOH、实时功率、历史完成结果",
            "只能给区间和影响因素，不能把预测伪装成续航或寿命承诺。",
        ),
        (
            "A4",
            "门槛降低",
            f"{aspect_display.get(adaptive[0], adaptive[0])}自适应控制",
            adaptive,
            "按工况、负载与热状态自动使用合适输出，让新手也获得稳定结果。",
            "任务标签、负载、温度、设置、设备遥测与完成质量",
            "保留人工覆盖并明确产品物理能力与安全边界。",
        ),
        (
            "A5",
            "哲学翻转",
            f"{aspect_display.get(usability[0], usability[0])}人机自适应",
            usability,
            "让机器适应用户的步速、力量和动作，而不是要求用户迁就固定机器特性。",
            "操作时长、控制输入、设备姿态、用户调整与任务结果",
            "不得把体能信号写成健康诊断；必须有硬件限值和即时人工接管。",
        ),
        (
            "A6",
            "身份表达",
            "克制的完成成果与生态价值",
            ("smart_connectivity", explicit_smart),
            "只在用户自愿时呈现任务完成、维护节省与电池生态复用成果。",
            "本地任务记录、设备生态、用户自愿分享",
            "直接智能需求不足时不得为了科技感制造连接。",
        ),
        (
            "A7",
            "环境编程",
            f"{aspect_display.get(safety[0], safety[0])}环境后台适配",
            safety,
            "结合环境、负载和设备状态在后台调整保护策略，让状态无感地变好。",
            "环境、负载、保护状态、告警、人工确认",
            "软件提示不能替代说明书、防护装备和硬件安全互锁。",
        ),
        (
            "A8",
            "管家进化",
            "维护、收纳与设置记忆",
            service,
            "记住用户主动选择的设置与维护记录，在下次任务或季节切换时恢复。",
            "用户选择、维护记录、设备生命周期、存放环境",
            "默认本地最小化；用户可查看、编辑和删除记忆。",
        ),
    )
    opportunity_by_aspect = {
        str(row.get("opportunity_id") or "").removeprefix("improve_"): row
        for row in opportunities
    }
    rows: list[dict[str, Any]] = []
    for code, method, title, source, value, data, guardrail in specs:
        aspect_id, entities = source
        source_entities = set(entities)
        # A1–A8 are a classification vocabulary, not an eight-slot checklist.
        # Do not invent a card when this report range has no independently
        # observable problem pool. A6 is deliberately stricter: a vague
        # connection/theme mention is not enough to claim identity expression.
        if not source_entities:
            continue
        if code == "A6" and len(explicit_smart) < 3:
            continue
        opportunity = opportunity_by_aspect.get(aspect_id)
        evidence_ids = index.evidence_for_entities(
            source_entities,
            aspects=(aspect_id,),
            limit=12,
            prefer_low=code != "A6",
        )
        rows.append(
            {
                "code": code,
                "method": method,
                "title": title,
                "source_opportunity_id": (
                    opportunity.get("opportunity_id")
                    if opportunity
                    else None
                ),
                "source_problem_entities": len(source_entities),
                "evidence_population": len(source_entities),
                "explicit_smart_entities": (
                    len(explicit_smart)
                    if code == "A6"
                    else len(source_entities & explicit_smart)
                ),
                "claim_basis": (
                    "direct_signal"
                    if code == "A6"
                    else "problem_to_capability_hypothesis"
                ),
                "value_hypothesis": value,
                "required_data": data,
                "guardrail": guardrail,
                "evidence_ids": evidence_ids,
                "boundary": (
                    "智能化方向是从已量化问题推导的概念假设，不代表用户已直接要求该功能。"
                ),
            }
        )
    return rows


def _blower_smart_rows(index: OpeIndex) -> list[dict[str, Any]]:
    explicit_smart = index.aspect_entities["smart_connectivity"]

    def problem(*aspect_ids: str) -> set[str]:
        rows: set[str] = set()
        for aspect_id in aspect_ids:
            rows |= index.aspect_negative[aspect_id]
            rows |= {
                entity
                for entity in index.aspect_entities[aspect_id]
                if index.facts[entity].get("rating") is not None
                and index.facts[entity]["rating"] <= 2
            }
        return rows

    specs = [
        {
            "code": "A1",
            "method": "能力补全",
            "title": "故障诊断与预测维护",
            "source_opportunity_id": "reliability_recovery",
            "entities": problem(
                "reliability", "starting", "battery_charger", "service"
            ),
            "dimensions": (),
            "aspects": (
                "reliability",
                "starting",
                "battery_charger",
                "service",
            ),
            "value_hypothesis": "从停机、电池、充电器、进气和控制状态定位原因，并给出可执行恢复步骤。",
            "required_data": "故障码、电流温度、进气状态、电池 SOH、维修结果",
            "guardrail": "错报会损伤信任；必须给出不确定性和人工服务入口。",
        },
        {
            "code": "A2",
            "method": "摩擦消除",
            "title": "风量与喷嘴切换摩擦消除",
            "source_opportunity_id": "precision_control",
            "entities": problem("airflow_control", "nozzle_attachment"),
            "dimensions": ("airflow_workflow", "debris_type", "work_area"),
            "aspects": ("airflow_control", "nozzle_attachment"),
            "value_hypothesis": "减少档位、扳机锁定与喷嘴切换中的重复动作，让一个具体操作恨点消失。",
            "required_data": "控制输入、档位、喷嘴状态、切换次数与任务结果",
            "guardrail": "必须减少真实操作步骤；不能用更多提示或模式菜单制造新摩擦。",
        },
        {
            "code": "A3",
            "method": "认知降负",
            "title": "清理完成度预测",
            "source_opportunity_id": "cleanup_finishability",
            "entities": problem("runtime")
            | index.entities_for("job_completion", {"not_completed"}),
            "dimensions": (
                "work_area",
                "debris_condition",
                "task_duration",
                "job_completion",
            ),
            "aspects": ("runtime", "blowing_power"),
            "value_hypothesis": "把区域、碎屑干湿、风量档位和电池状态转成能否完成与预计余量。",
            "required_data": "区域、碎屑标签、档位、实时功率、SOC/SOH",
            "guardrail": "先显示区间与条件，避免伪精确的单点分钟数。",
        },
        {
            "code": "A4",
            "method": "门槛降低",
            "title": "碎屑识别与自适应风量",
            "source_opportunity_id": "heavy_debris_control",
            "entities": problem(
                "blowing_power", "wet_debris", "airflow_control"
            )
            & (
                index.entities_for("debris_type")
                | index.entities_for("debris_condition")
            ),
            "dimensions": (
                "debris_type",
                "debris_condition",
                "airflow_workflow",
            ),
            "aspects": (
                "blowing_power",
                "wet_debris",
                "airflow_control",
            ),
            "value_hypothesis": "识别干叶、湿叶和精细区域，自动建议或调节适合的风量曲线。",
            "required_data": "负载、电流、档位、喷嘴、碎屑与地面标签",
            "guardrail": "必须保留人工即时覆盖，不能自动吹散砂石或覆盖物。",
        },
        {
            "code": "A5",
            "method": "哲学翻转",
            "title": "以疲劳为中心的动力与交互控制",
            "source_opportunity_id": "fatigue_balance",
            "entities": problem("weight_balance", "ergonomics"),
            "dimensions": (
                "life_stage_ability",
                "task_duration",
                "form_factor",
            ),
            "aspects": ("weight_balance", "ergonomics", "airflow_control"),
            "value_hypothesis": "根据连续时长、握持和档位使用提醒换手、休息或切换更省力的控制方式。",
            "required_data": "档位时长、扳机输入、姿态 / 运动、用户自报疲劳",
            "guardrail": "不得把健康推断写成诊断；提醒必须可关闭且本地优先。",
        },
        {
            "code": "A6",
            "method": "身份表达",
            "title": "克制的电动工具完成感",
            "source_opportunity_id": None,
            "entities": explicit_smart,
            "dimensions": (),
            "aspects": ("smart_connectivity",),
            "value_hypothesis": "把安静、免燃油、生态复用和完成任务转成用户自愿查看或分享的成果。",
            "required_data": "任务完成记录、节省的燃油维护、用户自愿分享",
            "guardrail": "直接智能需求很少，不能为了科技感制造无价值连接。",
        },
        {
            "code": "A7",
            "method": "环境编程",
            "title": "扬尘、噪音与环境边界守护",
            "source_opportunity_id": "precision_airflow",
            "entities": problem("dust_control", "noise", "airflow_control"),
            "dimensions": ("work_area", "time_of_day", "weather"),
            "aspects": ("dust_control", "noise", "airflow_control"),
            "value_hypothesis": "在花坛、砂石、邻里时段和干燥环境中提示更合适的档位与喷嘴。",
            "required_data": "位置类别、环境颗粒、噪音、时间、档位与喷嘴",
            "guardrail": "环境判断只能辅助，不能替代护目镜、听力保护和现场观察。",
        },
        {
            "code": "A8",
            "method": "管家进化",
            "title": "跨季节维护与任务设置记忆",
            "source_opportunity_id": "reliability_recovery",
            "entities": problem(
                "reliability", "battery_charger", "nozzle_attachment"
            ),
            "dimensions": (
                "cleanup_season",
                "storage",
                "form_factor",
            ),
            "aspects": (
                "reliability",
                "battery_charger",
                "nozzle_attachment",
            ),
            "value_hypothesis": "记住常用区域、风量、喷嘴与维护历史，在季节切换时恢复合适设置。",
            "required_data": "用户选择、任务档案、附件、维护记录、设备生命周期",
            "guardrail": "默认本地最小化数据；用户能查看、编辑和删除记忆。",
        },
    ]
    rows = []
    for spec in specs:
        entities = set(spec["entities"])
        evidence_ids = index.evidence_for_entities(
            entities,
            dimension_ids=spec["dimensions"],
            aspects=spec["aspects"],
            limit=12,
            prefer_low=spec["code"] not in {"A6"},
        )
        explicit_overlap = len(entities & explicit_smart)
        rows.append(
            {
                key: value
                for key, value in spec.items()
                if key not in {"entities", "dimensions", "aspects"}
            }
            | {
                "source_problem_entities": len(entities),
                "evidence_population": len(entities),
                "explicit_smart_entities": (
                    len(explicit_smart)
                    if spec["code"] == "A6"
                    else explicit_overlap
                ),
                "claim_basis": (
                    "direct_signal"
                    if spec["code"] == "A6"
                    else "problem_to_capability_hypothesis"
                ),
                "evidence_ids": evidence_ids,
                "boundary": (
                    "智能化方向是从已量化问题推导的概念假设，不代表用户已直接要求该功能。"
                ),
            }
        )
    return rows


def _evidence_audit(report: dict[str, Any]) -> dict[str, Any]:
    referenced: list[str] = []
    group_sizes: list[int] = []
    groups: list[list[str]] = []
    evidence_group_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence":
                    continue
                if key.endswith("evidence_group_id") and item:
                    evidence_group_ids.add(str(item))
                if key.endswith("evidence_ids") and isinstance(item, list):
                    values = [str(entry) for entry in item]
                    referenced.extend(values)
                    group_sizes.append(len(values))
                    groups.append(values)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    referenced_ids = set(referenced)
    selected_rows = [
        row
        for row in report["evidence"]
        if row["evidence_id"] in referenced_ids
    ]
    normalized_quotes = {
        " ".join(str(row["quote"]).casefold().split())
        for row in selected_rows
        if row.get("is_original_review_quote")
    }
    original_rows = [
        row for row in selected_rows if row.get("is_original_review_quote")
    ]
    evidence_by_id = {
        row["evidence_id"]: row for row in report["evidence"]
    }
    internal_entity_duplicates = 0
    internal_quote_duplicates = 0
    for group in groups:
        rows = [
            evidence_by_id[evidence_id]
            for evidence_id in group
            if evidence_id in evidence_by_id
        ]
        entities = [_evidence_entity_ref(row) for row in rows]
        quotes = [
            " ".join(str(row["quote"]).casefold().split())
            for row in rows
        ]
        internal_entity_duplicates += len(entities) - len(set(entities))
        internal_quote_duplicates += len(quotes) - len(set(quotes))
    return {
        "selection_version": EVIDENCE_SELECTION_VERSION,
        "full_evidence_rows": len(report["evidence"]),
        "referenced_evidence_rows": len(selected_rows),
        "referenced_review_entities": len(
            {_evidence_entity_ref(row) for row in selected_rows}
        ),
        "original_review_quote_rows": len(original_rows),
        "source_field_evidence_rows": len(selected_rows) - len(original_rows),
        "unique_original_quote_texts": len(normalized_quotes),
        "exact_quote_uniqueness_rate": _ratio(
            len(normalized_quotes), len(original_rows)
        ),
        "evidence_groups": len(evidence_group_ids),
        "evidence_reference_slots": len(group_sizes),
        "groups_with_no_evidence": sum(size == 0 for size in group_sizes),
        "groups_with_at_least_12_examples": sum(
            size >= 12 for size in group_sizes
        ),
        "within_group_duplicate_entities": internal_entity_duplicates,
        "within_group_duplicate_exact_quotes": internal_quote_duplicates,
        "reused_reference_count": len(referenced) - len(referenced_ids),
        "selection_rules": [
            "同一证据入口内每条证据来自不同评论实体。",
            "过滤完全重复、长文本包含和 token Jaccard ≥ 0.82 的近似重复原文。",
            "在分析焦点之间轮转，并交错渠道、型号与高/中/低星样本。",
            "结构化来源字段与评论正文分开标记，不把字段值伪装成原文。",
        ],
    }


def _attach_evidence_groups(
    report: dict[str, Any],
    index: OpeIndex,
) -> dict[str, dict[str, Any]]:
    """Bind every representative list to its complete deterministic pool."""

    evidence_by_id = index.evidence.rows
    attached: dict[str, dict[str, Any]] = {}

    def bind(
        container: dict[str, Any],
        evidence_key: str,
        path: str,
    ) -> None:
        representatives = list(
            dict.fromkeys(
                str(value) for value in container.get(evidence_key) or []
            )
        )
        if not representatives:
            return
        candidate_ids = index.evidence_group_by_representatives.get(
            tuple(representatives), []
        )
        group_id = candidate_ids[0] if candidate_ids else None
        if group_id is None:
            representative_set = set(representatives)
            related = [
                value
                for value in index.evidence_groups.values()
                if representative_set
                & set(value["representative_evidence_ids"])
            ]
            if related:
                members = [
                    evidence_id
                    for group in related
                    for evidence_id in group["evidence_ids"]
                ]
                entities = {
                    _evidence_entity_ref(evidence_by_id[evidence_id])
                    for evidence_id in members
                    if evidence_id in evidence_by_id
                }
                group_id = index.register_evidence_group(
                    representatives,
                    members,
                    unique_review_entities=len(entities),
                    query={"combined_representative_groups": True},
                )
        if group_id is None:
            return
        raw = index.evidence_groups[group_id]
        members = [
            evidence_id
            for evidence_id in raw["evidence_ids"]
            if evidence_id in evidence_by_id
        ]
        entities = {
            _evidence_entity_ref(evidence_by_id[evidence_id])
            for evidence_id in members
        }
        prefix = (
            evidence_key[: -len("_evidence_ids")]
            if evidence_key.endswith("_evidence_ids")
            else ""
        )
        group_key = (
            f"{prefix}_evidence_group_id"
            if prefix
            else "evidence_group_id"
        )
        rows_key = (
            f"{prefix}_full_evidence_rows"
            if prefix
            else "full_evidence_rows"
        )
        entities_key = (
            f"{prefix}_full_evidence_entities"
            if prefix
            else "full_evidence_entities"
        )
        bound_group_id = (
            "evg_"
            + hashlib.sha256(
                canonical_json(
                    {
                        "source_group_id": group_id,
                        "reference_path": path,
                    }
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        container[group_key] = bound_group_id
        container[rows_key] = len(members)
        container[entities_key] = len(entities)
        label = str(
            container.get("title")
            or container.get("statement")
            or container.get("label")
            or container.get("question")
            or path
        )
        attached[bound_group_id] = {
            **raw,
            "path": path,
            "label": label,
        }

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                child_path = f"{path}.{key}" if path else key
                if key.endswith("evidence_ids") and isinstance(item, list):
                    bind(value, key, child_path)
                elif key != "evidence":
                    walk(item, child_path)
        elif isinstance(value, list):
            for index_value, item in enumerate(value):
                walk(item, f"{path}[{index_value}]")

    walk(report, "")
    report["evidence_group_catalog"] = [
        {
            "evidence_group_id": group_id,
            "path": group["path"],
            "label": group["label"],
            "representative_count": len(
                group["representative_evidence_ids"]
            ),
            "full_evidence_rows": len(group["evidence_ids"]),
            "full_evidence_entities": len(
                {
                    _evidence_entity_ref(evidence_by_id[evidence_id])
                    for evidence_id in group["evidence_ids"]
                    if evidence_id in evidence_by_id
                }
            ),
        }
        for group_id, group in sorted(attached.items())
    ]
    return attached


def build_ope_product_report(
    facts: list[dict[str, Any]],
    source_report: dict[str, Any],
    *,
    include_evidence_groups: bool = False,
) -> Any:
    scope_categories = [
        str(value)
        for value in source_report.get("scope", {}).get("categories", [])
        if str(value).strip()
    ]
    observed_categories = sorted(
        {
            str(fact.get("category") or "").strip()
            for fact in facts
            if str(fact.get("category") or "").strip()
        }
    )
    category_label = (
        scope_categories[0]
        if len(scope_categories) == 1
        else observed_categories[0]
        if len(observed_categories) == 1
        else ""
    )
    scope_category_id = str(
        source_report.get("scope", {}).get("category_id") or ""
    ).strip()
    forms = source_report.get("scope", {}).get("product_form_factors") or []
    profile = category_profile(scope_category_id or category_label, product_form_factors=forms)
    if profile is _WHEELED_SPREADER_PROFILE and any(
        fact.get("product_form_factor") != "wheeled" for fact in facts
    ):
        raise ValueError("wheeled Spreader facts conflict with product_form_factors scope")
    if profile is None:
        supported = "、".join(
            sorted(value.category_label for value in CATEGORY_PROFILES.values())
        )
        raise ValueError(
            f"品类“{category_label or '多品类'}”尚无完整场景 profile；"
            f"当前完整支持：{supported}"
        )
    index = OpeIndex(
        facts,
        profile_id=profile.profile_id,
        category_label=profile.category_label,
        dimensions=profile.dimensions,
        numeric_dimension_ids=profile.numeric_dimension_ids,
        aspect_rules=profile.aspect_rules,
    )
    index.run()
    scope_brands = [
        str(value) for value in source_report.get("scope", {}).get("brands", [])
    ]
    observed_brands = sorted(
        {
            str(fact.get("brand") or "").strip()
            for fact in facts
            if str(fact.get("brand") or "").strip()
        }
    )
    brand_label = (
        scope_brands[0]
        if len(scope_brands) == 1
        else observed_brands[0]
        if len(observed_brands) == 1
        else "多品牌"
    )
    dimension_census = _build_dimension_census(index)
    aspects = _aspect_rows(index)
    personas = _persona_rows(index, aspects, brand_label)
    kano = _kano_rows(index, aspects)
    opportunities = _opportunity_rows(index)
    smart = _enrich_smart_rows(
        _smart_rows(index, opportunities),
        (
            []
            if index.profile_id.startswith(("mower/", "blower/"))
            else opportunities
        ),
    )
    metrics = {
        metric["metric_id"]: metric
        for metric in source_report["metrics"]
    }
    channel_counts = Counter(
        str(fact.get("channel") or "unknown") for fact in index.facts.values()
    )
    model_counts = Counter(
        str(fact.get("model") or "unknown") for fact in index.facts.values()
    )
    market_region_counts = Counter(
        str(fact.get("market_region") or "")
        for fact in index.facts.values()
        if str(fact.get("market_region") or "").strip()
    )
    age_dimension = next(
        row
        for row in dimension_census
        if row["dimension_id"] == "explicit_age"
    )
    occupation_dimension = next(
        row
        for row in dimension_census
        if row["dimension_id"] == "occupation"
    )
    responsibility_dimension = next(
        row
        for row in dimension_census
        if row["dimension_id"] == "operating_responsibility"
    )
    relationship_dimension = next(
        row
        for row in dimension_census
        if row["dimension_id"] == "purchase_relationship"
    )
    sparse_dimensions = [
        row["title"]
        for row in dimension_census
        if row["coverage"] < 0.01
        and row["dimension_id"] != "explicit_age"
    ]
    report = {
        "schema_version": "product-insight/v1",
        "report_id": (
            "ope_"
            + hashlib.sha256(
                canonical_json(
                    {
                        "source_report_id": source_report["report_id"],
                        "extractor_version": EXTRACTOR_VERSION,
                        "category_profile_id": profile.profile_id,
                        "narrative_version": NARRATIVE_VERSION,
                        "evidence_selection_version": (
                            EVIDENCE_SELECTION_VERSION
                        ),
                    }
                ).encode("utf-8")
            ).hexdigest()[:24]
        ),
        "source_report_id": source_report["report_id"],
        "data_snapshot_id": source_report["data_snapshot_id"],
        "analysis_version": EXTRACTOR_VERSION,
        "narrative_version": NARRATIVE_VERSION,
        "evidence_selection_version": EVIDENCE_SELECTION_VERSION,
        "category_id": profile.category_id,
        "category_profile_id": profile.profile_id,
        "category_label": profile.category_label,
        "category_label_en": profile.category_label_en,
        "hero_title": profile.hero_title,
        "brand_label": brand_label,
        "scope": source_report["scope"],
        "sample": {
            "review_entities": metrics["average_rating"]["sample_size"],
            "written_review_entities": len(index.all_entities),
            "average_rating": metrics["average_rating"]["value"],
            "positive_rate": metrics["positive_rating_rate"]["value"],
            "negative_rate": metrics["negative_rating_rate"]["value"],
            "written_coverage": metrics["written_coverage"]["value"],
            "interpretation": _sample_interpretation(
                review_entities=metrics["average_rating"]["sample_size"],
                written_entities=len(index.all_entities),
                average_rating=metrics["average_rating"]["value"],
                positive_rate=metrics["positive_rating_rate"]["value"],
                negative_rate=metrics["negative_rating_rate"]["value"],
                channel_count=len(channel_counts),
                model_count=len(model_counts),
                market_count=len(market_region_counts),
            ),
            "channels": [
                {"channel": key, "written_review_entities": value}
                for key, value in sorted(
                    channel_counts.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "models": [
                {"model": key, "written_review_entities": value}
                for key, value in sorted(
                    model_counts.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            **(
                {
                    "market_regions": [
                        {
                            "market_region": key,
                            "eligible_valid_text_entities": value,
                            "share": (
                                round(value / len(index.all_entities), 4)
                                if index.all_entities
                                else None
                            ),
                        }
                        for key, value in sorted(
                            market_region_counts.items(),
                            key=lambda item: (-item[1], item[0]),
                        )
                    ],
                    "raw_source_records": source_report.get(
                        "samples", {}
                    ).get("raw_source_records"),
                    "excluded_ineligible_records": source_report.get(
                        "samples", {}
                    ).get("excluded_ineligible_records"),
                    "excluded_invalid_text_records": source_report.get(
                        "samples", {}
                    ).get("excluded_invalid_text_records"),
                    "non_text_records": source_report.get(
                        "samples", {}
                    ).get("non_text_records"),
                }
                if market_region_counts
                else {}
            ),
        },
        "audience_profile": {
            "written_review_entities": len(index.all_entities),
            "occupation_entities": occupation_dimension[
                "review_entities"
            ],
            "occupation_coverage": occupation_dimension["coverage"],
            "occupation_unknown_entities": occupation_dimension[
                "unknown_entities"
            ],
            "responsibility_entities": responsibility_dimension[
                "review_entities"
            ],
            "responsibility_coverage": responsibility_dimension[
                "coverage"
            ],
            "age_entities": age_dimension["review_entities"],
            "age_coverage": age_dimension["coverage"],
            "purchase_relationship_entities": relationship_dimension[
                "review_entities"
            ],
            "purchase_relationship_coverage": relationship_dimension[
                "coverage"
            ],
            "boundary": (
                "职业 / 工作身份、场地责任、年龄 / 体能和经验用于描述用户；"
                "家庭代买、赠礼与共用单列为关系信号，不作为用户画像。"
            ),
        },
        "dimension_census": dimension_census,
        "personas": personas,
        "aspect_experience": aspects,
        "kano": kano,
        "product_opportunities": opportunities,
        "smart_methodology": {
            "version": SMART_METHOD_VERSION,
            "definitions": [dict(row) for row in SMART_METHOD_DEFINITIONS],
            "usage_boundary": (
                "A1–A8 是机会分类词汇，不是必须填满的八宫格；"
                "同类可出现多条，证据不足的类型允许为空。每条均是“用户问题 → "
                "智能功能 → 所需数据 → 护栏”的待验证假设，不代表用户直接提出该功能。"
            ),
        },
        "smart_opportunities": smart,
        "evidence": list(index.evidence.rows.values()),
        "coverage": {
            "extractors": (
                len(index.dimensions)
                + len(index.numeric_dimension_ids)
                + len(index.aspect_rules)
            ),
            "entity_extractor_executions": len(index.ledger_rows),
            "expected_entity_extractor_executions": (
                len(index.all_entities)
                * (
                    len(index.dimensions)
                    + len(index.numeric_dimension_ids)
                    + len(index.aspect_rules)
                )
            ),
            "failed_executions": 0,
        },
        "method": {
            "core_metrics_generated_by": "deterministic source report",
            "context_extraction_generated_by": (
                "versioned regex, numeric parsers, and review-level set operations"
            ),
            "evidence_selection_generated_by": (
                "deterministic entity, quote, focus, channel, model, and rating diversity"
            ),
            "narrative_generated_by": (
                "Codex-authored product interpretation, bound to stored metrics and evidence"
            ),
            "runtime_llm_used": False,
            "narrative_origin": "codex_authored_template",
            "third_party_llm_api_used": False,
            "core_metrics_modified_by_llm": False,
        },
        "limitations": [
            "画像和场景只统计评论中明确表达的片段；未命中不能解释为特征不存在。",
            (
                f"年龄证据覆盖正文 {age_dimension['coverage']:.1%}；"
                "来源年龄档案与正文自述均为直接证据，但仍不能外推品牌总体购买人口。"
            ),
            (
                f"明确职业 / 工作身份只覆盖正文 "
                f"{occupation_dimension['coverage']:.1%}；"
                f"其余 {occupation_dimension['unknown_entities']:,} 条正文的职业未知，"
                "不能用 homeowner、家庭成员、任务地点或产品用途补写职业。"
            ),
            (
                "以下维度在当前正文中的明确表达不足 1%："
                + "、".join(sparse_dimensions)
                + "；报告保留缺口，不用模型补齐。"
            ),
            (
                "Pros / Cons 的显式极性来自句级词法；星级关联是评论级上下文，"
                "不是属性因果情感。"
            ),
            "Kano 是网评派生假设，不替代标准功能失 / 得双问题问卷。",
            f"本次范围只有 {brand_label}，不能生成真实竞品 GAP 或品牌胜负结论。",
            "产品机会不包含战略、成本、BOM、工程可行性或路线图承诺。",
            "智能化机会是从问题到能力的推导假设；直接智能功能需求仍然稀疏。",
            *(
                [
                    (
                        "组合套装评论中明确只描述打草机、割边机或链锯的句子已从"
                        "吹风机属性统计中排除；未点名具体工具的套装评论仍标记为"
                        "可能存在部件归因歧义。"
                    )
                ]
                if profile.profile_id.startswith("blower/")
                else []
            ),
        ],
    }
    evidence_groups = _attach_evidence_groups(report, index)
    report["evidence_audit"] = _evidence_audit(report)
    report["content_hash"] = "sha256:" + canonical_json_sha256(report)
    if include_evidence_groups:
        return report, index.ledger_rows, evidence_groups
    return report, index.ledger_rows
