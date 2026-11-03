from __future__ import annotations

import gzip
import hashlib
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from reporting.config import ATOMIC_SCRIPT_REGISTRY_VERSION
from reporting.source import canonical_json, sha256_text


SENTENCE_PATTERN = re.compile(r"[^.!?\n]+(?:[.!?]+|$)", re.MULTILINE)


@dataclass(frozen=True)
class AtomicScript:
    script_id: str
    name: str
    purpose: str
    applications: tuple[str, ...]
    patterns: tuple[tuple[str, str], ...]


def _p(label: str, expression: str) -> tuple[str, str]:
    return label, expression


SCRIPT_REGISTRY: tuple[AtomicScript, ...] = (
    AtomicScript(
        "E01",
        "产品属性提及",
        "识别评论明确提到的产品表现维度。",
        ("属性表现", "竞品 Pros / Cons", "Kano 候选"),
        (
            _p("cut_quality", r"\bcut(?:ting)? quality\b|\bclean cut\b|\buneven cut\b"),
            _p("power", r"\bpower(?:ful)?\b|\btorque\b|\bhorsepower\b"),
            _p(
                "runtime",
                r"\brun\s*time\b|\bbattery life\b|"
                r"\b(?:battery|charge|runtime) lasts?\b|"
                r"\blast(?:s|ed)? \d+(?:\.\d+)? (?:minutes?|hours?|mins?|hrs?)\b",
            ),
            _p("charging", r"\bcharg(?:e|ed|er|ing)\b"),
            _p("handling", r"\bweight\b|\bheavy\b|\blightweight\b|\bmaneuver"),
            _p("noise", r"\bnoise\b|\bnoisy\b|\bquiet\b|\bvibrat"),
            _p("self_propel", r"\bself[- ]?propel|\bdrive speed\b|\bpace\b"),
            _p("bagging_mulching", r"\bbag(?:ger|ging)?\b|\bmulch|\bside discharge\b"),
            _p("assembly_storage", r"\bassembl|\bfold(?:ing)?\b|\bstor(?:e|age)\b"),
            _p("value", r"\bprice\b|\bcost\b|\bvalue\b|\bexpensive\b|\bcheap\b"),
        ),
    ),
    AtomicScript(
        "E02",
        "部件与系统提及",
        "识别被评价、故障或需求指向的具体部件。",
        ("故障定位", "服务分析", "需求归因"),
        (
            _p("battery", r"\bbatter(?:y|ies)\b"),
            _p("charger", r"\bcharger\b|\bcharging station\b"),
            _p("blade", r"\bblade(?:s)?\b"),
            _p("motor", r"\bmotor\b|\bengine\b"),
            _p("wheel", r"\bwheel(?:s)?\b|\btire(?:s)?\b"),
            _p("handle", r"\bhandle\b|\bhandlebar\b"),
            _p("bag", r"\bgrass bag\b|\bbagger\b|\bbag\b"),
            _p("deck", r"\bdeck\b|\bmower housing\b"),
            _p("drive", r"\bdrive\b|\bself[- ]?propel"),
            _p("control", r"\bbutton\b|\bswitch\b|\blever\b|\bcontrol\b"),
        ),
    ),
    AtomicScript(
        "E03",
        "局部评价极性",
        "提取明确的正向或负向评价词，不用星级替代句级表达。",
        ("Pros / Cons", "属性口碑", "证据检索"),
        (
            _p("positive", r"\blove(?:d)?\b|\bgreat\b|\bexcellent\b|\bperfect\b|\bimpress(?:ed|ive)\b|\bworks? well\b|\beasy\b|\bquiet\b"),
            _p("negative", r"\bhate(?:d)?\b|\bpoor\b|\bterrible\b|\bawful\b|\bdisappoint(?:ed|ing)\b|\buseless\b|\bdifficult\b|\bproblem\b"),
        ),
    ),
    AtomicScript(
        "E04",
        "否定关系",
        "标记 no/not/never/without 等否定，供下游关系判断使用。",
        ("情感校正", "故障排除", "需求理解"),
        (
            _p("negation", r"\bno\b|\bnot\b|\bnever\b|\bneither\b|\bwithout\b|\bdoesn['’]t\b|\bdidn['’]t\b|\bwon['’]t\b|\bcan['’]t\b|\bcannot\b"),
        ),
    ),
    AtomicScript(
        "E05",
        "强度、不确定与转折",
        "提取程度副词、缓和词与转折连接词。",
        ("表述强度", "混合评价", "证据排序"),
        (
            _p("intensifier", r"\bvery\b|\breally\b|\bextremely\b|\bsuper\b|\btoo\b|\bso\b"),
            _p("hedge", r"\bmaybe\b|\bperhaps\b|\bsomewhat\b|\bslightly\b|\bseems?\b|\bprobably\b"),
            _p("contrast", r"\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\byet\b|\bexcept\b"),
        ),
    ),
    AtomicScript(
        "E06",
        "故障与症状",
        "识别明确故障、失效和异常症状。",
        ("痛点分析", "质量监控", "产品机会"),
        (
            _p("not_start", r"\bwon['’]t start\b|\bwouldn['’]t start\b|\bdoesn['’]t start\b|\bnot start(?:ing)?\b"),
            _p("stopped_working", r"\bstopped working\b|\bquit working\b|\bdead\b|\bfailed\b"),
            _p("stalling", r"\bstall(?:s|ed|ing)?\b|\bshuts? off\b|\bcut(?:s)? out\b"),
            _p("breakage", r"\bbroke\b|\bbroken\b|\bcrack(?:ed)?\b|\bsnapped\b"),
            _p("battery_failure", r"\bwon['’]t charge\b|\bnot charg(?:e|ing)\b|\bbattery (?:died|dead|failed)\b"),
            _p("clogging", r"\bclog(?:s|ged|ging)?\b|\bjam(?:s|med|ming)?\b"),
            _p("uneven_result", r"\buneven\b|\bmiss(?:ed|es) spots\b|\bscalps?\b"),
        ),
    ),
    AtomicScript(
        "E07",
        "原因与触发条件",
        "识别用户明确陈述的原因、时间触发或工况触发。",
        ("故障链", "工况分析", "机会诊断"),
        (
            _p("causal_marker", r"\bbecause\b|\bdue to\b|\bcaused by\b|\bas a result of\b"),
            _p("temporal_trigger", r"\bwhen\b|\bwhenever\b|\bafter\b|\bbefore\b|\bonce\b"),
            _p("condition_trigger", r"\bunder\b|\bduring\b|\bwhile\b|\bif\b"),
        ),
    ),
    AtomicScript(
        "E08",
        "后果与影响",
        "识别故障或问题造成的任务、时间、成本和安全后果。",
        ("故障严重度", "用户损失", "产品机会"),
        (
            _p("task_blocked", r"\bcouldn['’]t (?:finish|mow|use|complete)\b|\bcan['’]t (?:finish|mow|use|complete)\b|\bunusable\b"),
            _p("extra_work", r"\bhad to (?:redo|stop|wait|push|restart|return|replace)\b|\bextra (?:work|time|pass)\b|\btook twice\b"),
            _p("cost_impact", r"\bwasted money\b|\badditional cost\b|\bcost me\b|\bpay for\b"),
            _p("safety_impact", r"\binjur(?:y|ed)\b|\bdanger(?:ous)?\b|\bunsafe\b|\bfire\b"),
        ),
    ),
    AtomicScript(
        "E09",
        "规避、维修与补救",
        "识别用户为继续使用而采取的明确动作。",
        ("可用性问题", "维修路径", "服务机会"),
        (
            _p("restart_reset", r"\brestart(?:ed|ing)?\b|\breset(?:ting)?\b|\bturn(?:ed)? it off\b"),
            _p("clean_clear", r"\bclean(?:ed|ing)?\b|\bclear(?:ed|ing)?\b|\bunclog"),
            _p("repair_fix", r"\brepair(?:ed|ing)?\b|\bfix(?:ed|ing)?\b|\btighten(?:ed|ing)?\b"),
            _p("replace_exchange", r"\breplac(?:e|ed|ing)\b|\bexchange(?:d)?\b|\bswap(?:ped)?\b"),
            _p("return_refund", r"\breturn(?:ed|ing)?\b|\brefund(?:ed)?\b"),
        ),
    ),
    AtomicScript(
        "E10",
        "服务、保修与退货",
        "识别售后接触、保修、退换货及服务结果。",
        ("售后体验", "质量成本", "渠道差异"),
        (
            _p("customer_service", r"\bcustomer service\b|\bsupport (?:team|line)\b|\bcalled support\b"),
            _p("warranty", r"\bwarrant(?:y|ies)\b|\bwarranty claim\b"),
            _p("return", r"\breturn(?:ed|ing)?\b|\bsent it back\b"),
            _p("refund", r"\brefund(?:ed)?\b|\bmoney back\b"),
            _p("replacement", r"\breplacement\b|\breplaced under warranty\b"),
        ),
    ),
    AtomicScript(
        "E11",
        "数值与单位",
        "提取显式数量、面积、时长、距离、电气和价格表达。",
        ("续航分析", "草坪规模", "价格与规格"),
        (
            _p("time", r"\b\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?)\b"),
            _p("area", r"\b(?:\d+/\d+|\d+(?:\.\d+)?)\s*(?:acres?|sq\.?\s*ft\.?|square feet)\b|\b(?:quarter|half|one)[ -]acre\b"),
            _p("length", r"\b\d+(?:\.\d+)?\s*(?:inches?|in\.?|feet|ft\.?|yards?)\b"),
            _p("electric", r"\b\d+(?:\.\d+)?\s*(?:v|volt(?:s)?|ah|amp hours?|wh|watt hours?)\b"),
            _p("money", r"\$\s*\d+(?:[,.]\d+)?|\b\d+(?:[,.]\d+)?\s*dollars?\b"),
            _p("count", r"\b\d+\s*(?:times?|passes?|batter(?:y|ies))\b"),
        ),
    ),
    AtomicScript(
        "E12",
        "耐久、复发与生命周期",
        "识别使用时长、重复故障和寿命判断。",
        ("耐久质量", "新旧型号", "故障复发"),
        (
            _p("ownership_duration", r"\b(?:after|for|within)\s+\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?)\b"),
            _p("recurrence", r"\bagain\b|\brepeatedly\b|\bkeeps? (?:happening|stopping|failing)\b|\bevery time\b"),
            _p("longevity_positive", r"\bstill works?\b|\bheld up\b|\bgoing strong\b|\byears? later\b"),
            _p("early_failure", r"\bonly lasted\b|\bfailed after\b|\bdied after\b|\bfirst use\b"),
        ),
    ),
    AtomicScript(
        "E13",
        "JTBD 任务",
        "识别用户明确要完成的作业任务。",
        ("使用场景", "需求分析", "任务链"),
        (
            _p("mow_cut", r"\bmow(?:ed|ing|s)?\b|\bcut(?:ting)? (?:the )?grass\b"),
            _p("mulch", r"\bmulch(?:ed|ing)?\b"),
            _p("bag_collect", r"\bbag(?:ged|ging)?\b|\bcollect(?:ed|ing)? (?:grass|clippings|leaves)\b"),
            _p("trim_edge", r"\btrim(?:med|ming)?\b|\bedg(?:e|ed|ing)\b"),
            _p("store_transport", r"\bstor(?:e|ed|ing)\b|\btransport(?:ed|ing)?\b|\bload(?:ed|ing)?\b"),
            _p("charge_manage", r"\bcharg(?:e|ed|ing)\b|\bswap(?:ped|ping)? batter"),
        ),
    ),
    AtomicScript(
        "E14",
        "使用环境与工况",
        "识别草坪、地形、草况和天气等明确工况。",
        ("场景分层", "性能边界", "竞品公平比较"),
        (
            _p("yard_size", r"\b(?:small|large|back|front) (?:yard|lawn)\b|\bbackyard\b|\bfront yard\b"),
            _p("thick_tall_grass", r"\bthick grass\b|\btall grass\b|\bovergrown\b|\bheavy grass\b"),
            _p("wet_condition", r"\bwet grass\b|\bdamp\b|\bafter (?:the )?rain\b"),
            _p("slope_hill", r"\bhill(?:s|side)?\b|\bslope(?:s|d)?\b|\bincline\b"),
            _p("rough_terrain", r"\brough\b|\buneven ground\b|\bbumpy\b|\broots?\b"),
            _p("weather", r"\bhot weather\b|\bcold weather\b|\bheat\b|\btemperature\b"),
        ),
    ),
    AtomicScript(
        "E15",
        "频率、规模与时长",
        "识别使用频率、作业规模和持续时间。",
        ("重度用户", "续航需求", "使用强度"),
        (
            _p("frequency", r"\b(?:once|twice|\d+ times?) (?:a|per) (?:day|week|month)\b|\bevery (?:day|week|weekend)\b|\bdaily\b|\bweekly\b"),
            _p("duration", r"\b(?:for|takes?|took)\s+\d+(?:\.\d+)?\s*(?:minutes?|hours?|mins?|hrs?)\b"),
            _p("area_scale", r"\b(?:\d+/\d+|\d+(?:\.\d+)?)\s*(?:acres?|sq\.?\s*ft\.?|square feet)\b|\b(?:quarter|half|one)[ -]acre\b"),
            _p("multiple_properties", r"\bmultiple (?:yards|properties|lawns)\b|\bcommercial use\b"),
        ),
    ),
    AtomicScript(
        "E16",
        "用户身份与角色",
        "只提取评论中明确自述的用户角色。",
        ("用户特征", "专业/家用分层", "场景解释"),
        (
            _p("homeowner", r"\bhomeowner\b|\bmy (?:home|house|yard|lawn)\b"),
            _p("professional", r"\blandscaper\b|\bprofessional\b|\bcontractor\b|\bcommercial (?:user|use)\b"),
            _p("first_time_user", r"\bfirst[- ]time (?:owner|buyer|user)\b|\bnew to (?:mowing|lawn care)\b"),
            _p("family_role", r"\bmy (?:husband|wife|son|daughter|father|mother)\b|\bfor my (?:parents?|family)\b"),
        ),
    ),
    AtomicScript(
        "E17",
        "明确年龄",
        "只提取用户明确表达的年龄，不推断缺失人口属性。",
        ("年龄覆盖", "适老证据", "用户特征"),
        (
            _p("explicit_age", r"\b(?:i am|i['’]m|as a)\s+(?:a\s+)?(\d{2})\s*(?:years? old|yo)?\b|\b(\d{2})[- ]year[- ]old\b|\bin my (?:[2-8]0s|nineties)\b"),
        ),
    ),
    AtomicScript(
        "E18",
        "能力与身体限制",
        "识别用户明确说明的体力、行动或健康限制。",
        ("无障碍体验", "操控需求", "适老产品"),
        (
            _p("senior", r"\bsenior\b|\belderly\b|\bolder (?:person|adult|woman|man)\b"),
            _p("mobility", r"\blimited mobility\b|\bwheelchair\b|\bwalk(?:ing)? difficulty\b"),
            _p("strength", r"\bnot strong enough\b|\blimited strength\b|\bweak (?:hands|arms|grip)\b"),
            _p("health_condition", r"\barthritis\b|\bbad back\b|\bback pain\b|\bshoulder pain\b|\bdisab(?:led|ility)\b"),
        ),
    ),
    AtomicScript(
        "E19",
        "经验、持有与换机",
        "识别使用经验、旧机持有和技术路线切换。",
        ("换机路径", "新老用户", "竞品迁移"),
        (
            _p("years_experience", r"\b(?:mow|mowing) for \d+ years\b|\byears of experience\b|\blong[- ]time user\b"),
            _p("previous_owner", r"\bmy old mower\b|\bprevious mower\b|\bowned (?:a|an|this)\b|\bused to have\b"),
            _p("brand_switch", r"\bswitched from\b|\breplaced my\b|\bupgrade(?:d)? from\b|\bcompared to my old\b"),
            _p("gas_to_battery", r"\bfrom gas\b|\breplace(?:d|ment for) (?:my )?gas\b|\bno more gas\b"),
        ),
    ),
    AtomicScript(
        "E20",
        "明确需求、请求与愿望",
        "识别用户明确提出的 need/wish/should 等表达。",
        ("需求池", "改进建议", "机会发现"),
        (
            _p("need", r"\bi need\b|\bwe need\b|\b(?:it|this|the mower) needs to\b"),
            _p("wish", r"\bi wish\b|\bwish it\b|\bwould like\b|\bi['’]d like\b"),
            _p("should", r"\bshould (?:have|be|include|come|make|add)\b"),
            _p("request", r"\bplease (?:add|make|include|improve|fix)\b|\bwould be better if\b"),
        ),
    ),
    AtomicScript(
        "E21",
        "预期差距",
        "识别预期、承诺与实际体验之间的明确落差。",
        ("满意度驱动", "承诺验证", "新品改进"),
        (
            _p("did_not_meet", r"\bdidn['’]t meet (?:my )?expectations?\b|\bnot what i expected\b"),
            _p("expected_but", r"\bexpected\b[^.!?\n]{0,80}\bbut\b|\bthought\b[^.!?\n]{0,80}\bbut\b"),
            _p("should_have", r"\bshould have\b|\bwas supposed to\b|\badvertised (?:as|to)\b"),
            _p("less_than_claimed", r"\bless than (?:advertised|claimed|expected)\b|\bnot as .* as\b"),
        ),
    ),
    AtomicScript(
        "E22",
        "比较关系",
        "提取明确的优劣、相似和替代比较表达。",
        ("竞品分析", "新旧型号对比", "换机分析"),
        (
            _p("better", r"\bbetter than\b|\bimprovement over\b|\boutperform(?:s|ed)?\b"),
            _p("worse", r"\bworse than\b|\bnot as good as\b|\bunderperform(?:s|ed)?\b"),
            _p("similar", r"\bsame as\b|\bsimilar to\b|\bcomparable to\b"),
            _p("generic_comparison", r"\bcompared (?:with|to)\b|\bversus\b|\bvs\.?\b|\bthan my (?:old|previous)\b"),
        ),
    ),
    AtomicScript(
        "E23",
        "购买驱动",
        "识别用户明确说明的购买、选择或替换原因。",
        ("购买决策", "竞品赢单", "价值主张"),
        (
            _p("bought_because", r"\bbought (?:it|this|the .{0,30}) because\b|\bpurchased .* because\b|\bchose .* because\b"),
            _p("price_driver", r"\bon sale\b|\bdiscount\b|\bprice was\b|\baffordable\b"),
            _p("brand_driver", r"\btrust(?:ed)? the brand\b|\bbrand reputation\b|\balready (?:own|had)\b"),
            _p("ecosystem_driver", r"\balready had (?:the )?batter(?:y|ies)\b|\bsame battery\b|\btool ecosystem\b|\bplatform\b"),
            _p("feature_driver", r"\b(?:bought|purchased|chose) .{0,60}\bfor (?:the|its) (?:self[- ]propel|runtime|power|features?)\b"),
        ),
    ),
    AtomicScript(
        "E24",
        "惊喜与愉悦",
        "识别超出预期、强烈喜爱和高光体验。",
        ("Delighter 候选", "传播卖点", "体验亮点"),
        (
            _p("exceeded_expectations", r"\bexceeded (?:my )?expectations?\b|\bbetter than expected\b"),
            _p("delight", r"\bpleasantly surprised\b|\bblown away\b|\bgame changer\b|\bwhat a difference\b"),
            _p("favorite", r"\bmy favorite\b|\bbest (?:mower|purchase|tool)\b|\babsolutely love\b"),
            _p("impressed", r"\bvery impressed\b|\bhighly impressed\b|\bamazing\b|\bfantastic\b"),
        ),
    ),
    AtomicScript(
        "E25",
        "生态与兼容性",
        "识别电池、附件、平台和现有设备的兼容关系。",
        ("生态价值", "附件机会", "购买驱动"),
        (
            _p("battery_compatibility", r"\bsame batter(?:y|ies)\b|\bbattery (?:works|compatible|interchangeable)\b|\buses? my .* battery\b"),
            _p("platform", r"\b(?:18v|40v|56v|one\+|power\+) (?:system|platform|line|ecosystem)\b|\btool ecosystem\b"),
            _p("accessory_compatibility", r"\bcompatible with\b|\bworks with\b|\bfits? (?:the|my)\b|\battachment(?:s)?\b"),
            _p("incompatibility", r"\bnot compatible\b|\bdoesn['’]t fit\b|\bproprietary\b|\bneed(?:s)? an adapter\b"),
        ),
    ),
    AtomicScript(
        "E26",
        "安全、信任与隐私",
        "识别物理安全、产品信任和数字隐私表达。",
        ("安全风险", "信任护栏", "智能化风险"),
        (
            _p("unsafe", r"\bunsafe\b|\bdanger(?:ous)?\b|\bsafety issue\b|\balmost (?:hit|cut|injured)\b"),
            _p("fire_electric", r"\bfire\b|\bsmoke\b|\bspark(?:ed|ing|s)\b|\boverheat(?:ed|ing)?\b"),
            _p("trust", r"\btrust\b|\bunreliable\b|\breliable\b|\bdependable\b"),
            _p("privacy", r"\bprivacy\b|\btracking\b|\bpersonal data\b|\bdata collection\b"),
        ),
    ),
    AtomicScript(
        "E27",
        "智能与数字能力",
        "识别连接、感知、自动化、诊断和通知等明确数字能力。",
        ("智能化方法论", "数字服务", "能力缺口"),
        (
            _p("connectivity", r"\bapp\b|\bbluetooth\b|\bwi[- ]?fi\b|\bconnect(?:ed|ivity)?\b"),
            _p("sensor", r"\bsensor(?:s)?\b|\bdetect(?:s|ed|ion)?\b|\bsens(?:es|ed|ing)\b"),
            _p("automation", r"\bautomatic(?:ally)?\b|\bauto adjust\b|\bself[- ]adjust"),
            _p("diagnostics", r"\bdiagnostic(?:s)?\b|\berror code\b|\btroubleshoot(?:ing)?\b|\bmaintenance reminder\b"),
            _p("notification", r"\bnotification\b|\balert(?:s|ed)?\b|\bstatus update\b"),
            _p("smart_control", r"\bremote control\b|\bschedul(?:e|ed|ing) (?:the|a|mow|run|start)\b|\bsmart feature\b"),
        ),
    ),
    AtomicScript(
        "E28",
        "推荐、复购与留存",
        "识别明确推荐、复购、保留、退货或转品牌意向。",
        ("NPS 证据", "留存风险", "品牌忠诚"),
        (
            _p("recommend", r"\brecommend(?:ed|ing)?\b|\bwould suggest\b"),
            _p("repurchase", r"\bbuy (?:it|this|one) again\b|\bpurchase again\b|\banother one\b"),
            _p("keep", r"\bkeep(?:ing)? it\b|\bglad i bought\b|\bno regrets\b"),
            _p("return", r"\breturn(?:ed|ing)? it\b|\bsend(?:ing)? it back\b"),
            _p("switch_away", r"\bnever buy .* again\b|\bswitch(?:ed|ing)? (?:to|away)\b|\blost a customer\b"),
        ),
    ),
)


COMPILED_REGISTRY: tuple[tuple[AtomicScript, tuple[tuple[str, re.Pattern[str]], ...]], ...] = tuple(
    (
        script,
        tuple(
            (label, re.compile(expression, re.IGNORECASE))
            for label, expression in script.patterns
        ),
    )
    for script in SCRIPT_REGISTRY
)


def registry_manifest() -> list[dict[str, Any]]:
    return [
        {
            "script_id": script.script_id,
            "name": script.name,
            "purpose": script.purpose,
            "applications": list(script.applications),
            "rule_count": len(script.patterns),
            "version": ATOMIC_SCRIPT_REGISTRY_VERSION,
            "method": "deterministic-regex",
            "llm_used": False,
        }
        for script in SCRIPT_REGISTRY
    ]


def _sentences(text: str) -> Iterable[tuple[int, int, str]]:
    for match in SENTENCE_PATTERN.finditer(text):
        sentence = match.group(0).strip()
        if sentence:
            leading = len(match.group(0)) - len(match.group(0).lstrip())
            start = match.start() + leading
            yield start, start + len(sentence), sentence


def _fact_text(fact: dict[str, Any]) -> str:
    title = str(fact.get("review_title") or "").strip()
    body = str(fact.get("review_text") or "").strip()
    return ". ".join(value for value in (title, body) if value)


def _entity_ref(fact: dict[str, Any]) -> str:
    return str(fact.get("review_entity_id") or fact["review_occurrence_id"])


def _match_script(
    text: str,
    compiled_patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for sentence_start, _, sentence in _sentences(text):
        for rule_index, (label, pattern) in enumerate(compiled_patterns, start=1):
            for match in pattern.finditer(sentence):
                start = sentence_start + match.start()
                end = sentence_start + match.end()
                key = (label, start, end)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "label": label,
                        "rule_id": f"R{rule_index:02d}",
                        "start": start,
                        "end": end,
                        "matched_text": match.group(0),
                        "sentence": sentence[:500],
                    }
                )
    matches.sort(key=lambda item: (item["start"], item["end"], item["label"]))
    return matches


class _LedgerWriter:
    """Stream a deterministic full execution ledger without retaining it."""

    def __init__(
        self,
        output_root: Path | None,
        artifact_relative_path: str,
    ) -> None:
        self.output_root = output_root
        self.artifact_relative_path = artifact_relative_path
        self.digest = hashlib.sha256()
        self.record_count = 0
        self.target: Path | None = None
        self.temporary: Path | None = None
        self.raw: Any = None
        self.compressed: gzip.GzipFile | None = None
        if output_root is None:
            return
        self.target = output_root / artifact_relative_path
        self.target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{self.target.stem}-",
            suffix=".jsonl.gz",
            dir=self.target.parent,
            delete=False,
        )
        self.temporary = Path(handle.name)
        handle.close()
        self.raw = self.temporary.open("wb")
        self.compressed = gzip.GzipFile(
            filename="",
            fileobj=self.raw,
            mode="wb",
            mtime=0,
        )

    def write(self, row: dict[str, Any]) -> None:
        serialized = canonical_json(row).encode("utf-8") + b"\n"
        self.digest.update(serialized)
        self.record_count += 1
        if self.compressed is not None:
            self.compressed.write(serialized)

    def close(self, *, commit: bool) -> None:
        if self.compressed is not None:
            self.compressed.close()
            self.compressed = None
        if self.raw is not None:
            self.raw.close()
            self.raw = None
        if (
            commit
            and self.temporary is not None
            and self.target is not None
        ):
            os.replace(self.temporary, self.target)
        if self.temporary is not None and self.temporary.exists():
            self.temporary.unlink()


def run_atomic_scripts(
    facts: list[dict[str, Any]],
    *,
    snapshot_id: str,
    output_root: Path | None = None,
    evidence_per_script: int = 3,
) -> dict[str, Any]:
    written = sorted(
        (fact for fact in facts if fact.get("has_text")),
        key=_entity_ref,
    )
    execution = {
        script.script_id: {
            "script_id": script.script_id,
            "name": script.name,
            "eligible": len(written),
            "matched": 0,
            "no_match": 0,
            "failed": 0,
            "atom_count": 0,
        }
        for script in SCRIPT_REGISTRY
    }
    label_counts: dict[tuple[str, str], int] = defaultdict(int)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    covered_entities = 0
    previous_entity: str | None = None
    entity_labels: set[tuple[str, str]] = set()
    entity_covered = False
    safe_snapshot = re.sub(r"[^A-Za-z0-9_.-]+", "-", snapshot_id)
    relative_path = (
        f"atomic-ledgers/{safe_snapshot}--"
        f"{ATOMIC_SCRIPT_REGISTRY_VERSION.replace('/', '-')}.jsonl.gz"
    )
    ledger = _LedgerWriter(output_root, relative_path)

    for fact in written:
        entity_ref = _entity_ref(fact)
        # Sorting above keeps every occurrence of one entity adjacent. Retain
        # only its labels, rather than millions of entity memberships across
        # all labels. Execution rows still include every input occurrence.
        if entity_ref != previous_entity:
            previous_entity = entity_ref
            entity_labels.clear()
            entity_covered = False
        text = _fact_text(fact)
        for script, compiled_patterns in COMPILED_REGISTRY:
            try:
                matches = _match_script(text, compiled_patterns)
                status = "matched" if matches else "no_match"
            except (TypeError, ValueError, re.error) as exc:
                matches = []
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
            else:
                error = None
            row = {
                "review_entity_ref": entity_ref,
                "script_id": script.script_id,
                "script_version": ATOMIC_SCRIPT_REGISTRY_VERSION,
                "status": status,
                "match_count": len(matches),
                "matches": matches,
            }
            if error:
                row["error"] = error
            ledger.write(row)
            execution[script.script_id][status] += 1
            execution[script.script_id]["atom_count"] += len(matches)
            if matches:
                if not entity_covered:
                    covered_entities += 1
                    entity_covered = True
                for match in matches:
                    key = (script.script_id, match["label"])
                    if key not in entity_labels:
                        entity_labels.add(key)
                        label_counts[key] += 1
                if len(evidence[script.script_id]) < evidence_per_script:
                    first = matches[0]
                    evidence_material = {
                        "entity_ref": entity_ref,
                        "script_id": script.script_id,
                        "start": first["start"],
                        "end": first["end"],
                    }
                    evidence_id = (
                        "ev_atomic_"
                        + sha256_text(canonical_json(evidence_material))[:24]
                    )
                    evidence[script.script_id].append(
                        {
                            "evidence_id": evidence_id,
                            "review_entity_id": fact.get("review_entity_id"),
                            "review_occurrence_id": fact.get(
                                "review_occurrence_id"
                            ),
                            "factor_namespace": "atomic_v2",
                            "factor_id": script.script_id,
                            "factor_label": script.name,
                            "snippet": first["sentence"],
                            "matched_text": first["matched_text"],
                            "span": [first["start"], first["end"]],
                            "channel": fact.get("channel"),
                            "brand": fact.get("brand"),
                            "model": fact.get("model"),
                            **(
                                {
                                    "market_region": fact.get(
                                        "market_region"
                                    ),
                                    "market_region_evidence": fact.get(
                                        "market_region_evidence"
                                    ),
                                    "display_site": fact.get("display_site"),
                                    "source_site": fact.get(
                                        "source_site_normalized"
                                    ),
                                }
                                if fact.get("market_region")
                                else {}
                            ),
                            "rating": fact.get("rating"),
                            "submission_date": fact.get("submission_date"),
                            "method": "deterministic regex with character span",
                            "extractor_version": ATOMIC_SCRIPT_REGISTRY_VERSION,
                        }
                    )

    ledger.close(commit=True)
    logical_hash = ledger.digest.hexdigest()

    execution_rows = []
    for script in SCRIPT_REGISTRY:
        row = execution[script.script_id]
        matched = row["matched"]
        row["coverage"] = round(matched / len(written), 6) if written else None
        row["evidence_ids"] = [
            item["evidence_id"] for item in evidence[script.script_id]
        ]
        row["version"] = ATOMIC_SCRIPT_REGISTRY_VERSION
        execution_rows.append(row)

    label_rows = [
        {
            "script_id": script_id,
            "script_name": execution[script_id]["name"],
            "label": label,
            "review_entities": count,
            "coverage": (
                round(count / len(written), 6) if written else None
            ),
            "evidence_ids": [
                item["evidence_id"] for item in evidence[script_id]
            ],
        }
        for (script_id, label), count in label_counts.items()
    ]
    label_rows.sort(
        key=lambda item: (
            item["script_id"],
            -item["review_entities"],
            item["label"],
        )
    )
    all_evidence = [
        item
        for script_id in sorted(evidence)
        for item in evidence[script_id]
    ]
    return {
        "registry_version": ATOMIC_SCRIPT_REGISTRY_VERSION,
        "script_count": len(SCRIPT_REGISTRY),
        "scripts": registry_manifest(),
        "execution": execution_rows,
        "labels": label_rows,
        "evidence": all_evidence,
        "written_entities": len(written),
        "covered_entities": covered_entities,
        "unmatched_entities": len(written) - covered_entities,
        "coverage_rate": (
            round(covered_entities / len(written), 6)
            if written
            else None
        ),
        "ledger": {
            "artifact_id": f"atomic_ledger_{logical_hash[:24]}",
            "relative_path": relative_path,
            "content_type": "application/x-ndjson+gzip",
            "record_count": ledger.record_count,
            "logical_content_sha256": f"sha256:{logical_hash}",
        },
    }
