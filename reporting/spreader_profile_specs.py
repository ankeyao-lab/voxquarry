"""Mention rules for an explicitly selected wheeled Spreader population.

These rules describe review evidence, never infer product form or drive type.
The shared Spreader/handheld profile and its stable IDs remain unchanged.
"""
from reporting.category_refresh_specs import EXTRA_PROFILE_SPECS, rows


_BASE = EXTRA_PROFILE_SPECS["Spreader"]
WHEELED_SPREADER_SPEC = {
    **_BASE,
    "profile_id": "spreader-wheeled/1.0.0",
    "category_label_en": "Wheeled spreader",
    "hero_title": "还原轮式撒播的装料、校准、行走与收尾",
    "workflows": (*_BASE["workflows"], *rows(
        r"turn|转弯、倒退与调头|\bturn\w*\b|\brevers\w*\b|\bmanoeuv\w*\b|\bmaneuv\w*\b|\bwenden\b" "\n"
        r"start_stop|行进启停与关料|\bshut.?off\b|\bgate\b|\bstop\w*\b|\banhalten\b" "\n"
        r"transport|搬运、装车与收纳|\btransport\w*\b|\btruck\b|\bfold\w*\b|\bstor\w*\b"
    )),
    "aspects": (
        *(row for row in _BASE["aspects"] if row[0] not in {"grip", "control", "metering"}),
        *rows(
            r"metering|下料量、校准与行速匹配|\b(?:application|spread|feed|flow) rate\b|\bdial\b|\bsettings?\b|\bcalibrat\w*\b|\bwalk\w* speed\b|\bstreumenge\b|\bdosier\w*\b" "\n"
            r"wheel_mobility|轮胎与地面通过|\bwheels?\b|\btires?\b|\btyres?\b|\bruts?\b|\braeder\b|\bräder\b|\breifen\b" "\n"
            r"drive_control|自走速度、牵引与制动|\bself.?propell\w*\b|\btraction\b|\bdrive\b|\bbrak\w*\b|\bhydrostatic\b|\bantrieb\w*\b" "\n"
            r"steering|转弯与操控|\bsteer\w*\b|\bturn\w*\b|\bmanoeuv\w*\b|\bmaneuv\w*\b|\blenk\w*\b" "\n"
            r"stability|坡地与满载稳定性|\bslope\w*\b|\bhills?\b|\btip\w* over\b|\bstability\b|\bhanglage\b|\bumkipp\w*\b" "\n"
            r"gate_control|闸门启停与停机漏料|\bshut.?off\b|\bgates?\b|\bleak\w*\b|\bspill\w*\b|\bschieber\b" "\n"
            r"transmission|轮轴、齿轮与传动可靠性|\baxles?\b|\bgear\w*\b|\btransmission\b|\bdrive belt\b|\bgetriebe\b|\bachse\b" "\n"
            r"edge_control|边缘挡料与撒幅边界|\bedge.?guard\b|\bedge (?:control|guard)\b|\bdeflector\b|\bborder\b|\brandbegrenz\w*\b" "\n"
            r"transport|扶手折叠、搬运与收纳|\bhandle\w*\b|\bfold\w*\b|\btransport\w*\b|\bstor\w*\b|\bschubbügel\b" "\n"
            r"ergonomics|扶手握持与操控疲劳|\bhandlebars?\b|\bpush ?bars?\b|\bhand fatigue\b|\bhandle grips?\b|\bschubbügel\b" "\n"
            r"engine|燃油发动机起动与维护|\bengine\b|\bfuel\b|\bpetrol\b|\bcarburetor\b|\bcarburettor\b|\bbenzin\w*\b"
        ),
    ),
}
