from __future__ import annotations

from typing import Any


SMART_METHOD_VERSION = "smart-opportunity-method/2.0.0"

# This is a classification vocabulary, not an eight-slot checklist.  A report
# may contain multiple opportunities of one type and omit unsupported types.
SMART_METHOD_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "code": "A1",
        "name": "能力补全",
        "core": "补人的反应、感知或判断上的盲区，不是替代体力，而是替代“没反应过来”的那一下。",
        "examples": [
            "脱链、障碍或下坡失控时的智能急停",
            "error 码智能解读",
            "电池 SOH 预测性维护",
        ],
        "value_anchor": "补足人的即时反应与判断能力",
        "priority": "standard",
    },
    {
        "code": "A2",
        "name": "摩擦消除",
        "core": "消灭一个具体的恨点，而不是新增卖点。",
        "examples": ["Power Load / Line IQ 自动上线与送线"],
        "value_anchor": "让一个反复发生的具体操作摩擦消失",
        "priority": "standard",
    },
    {
        "code": "A3",
        "name": "认知降负",
        "core": "消除不确定性本身就是价值，不需要用户为此执行额外操作。",
        "examples": [
            "精确剩余作业量提示",
            "电池健康与剩余寿命透明化",
        ],
        "value_anchor": "把黑箱状态变成可信、可理解的确定性",
        "priority": "standard",
    },
    {
        "code": "A4",
        "name": "门槛降低",
        "core": "让新手做出老师傅级的结果，是智能化机会的第一优先级。",
        "examples": [
            "工况、负载与热自适应控制栈",
            "厚湿草、硬木、雪质等负载下自动使用合适输出",
        ],
        "value_anchor": "跨品类复用控制栈，稳定任务结果",
        "priority": "first_priority",
    },
    {
        "code": "A5",
        "name": "哲学翻转",
        "core": "机器适应人，而不是人迁就机器。",
        "examples": ["压力感应自适应步速", "侧坡自动走直"],
        "value_anchor": "惊喜溢价",
        "priority": "standard",
    },
    {
        "code": "A6",
        "name": "身份表达",
        "core": "用户买的不是功能，而是使用这台机器时自己变成的那个人。",
        "examples": ["科技模式与 UI", "“割草机里的 Tesla”式体验"],
        "value_anchor": "品牌溢价",
        "priority": "standard",
    },
    {
        "code": "A7",
        "name": "环境编程",
        "core": "用户没注意，但状态已经变好了；智能在后台无感地适配环境。",
        "examples": ["雪质自动识别", "电池智能充电与存储保护"],
        "value_anchor": "无感改善结果与设备状态",
        "priority": "standard",
    },
    {
        "code": "A8",
        "name": "管家进化",
        "core": "沿工具→助手→管家的进化轴，形成跨任务、跨季节的持续服务。",
        "examples": ["学习使用习惯", "OTA 升级", "分区记忆"],
        "value_anchor": "超越单品功能的生态护城河",
        "priority": "standard",
    },
)

SMART_METHOD_BY_CODE = {
    definition["code"]: definition for definition in SMART_METHOD_DEFINITIONS
}


def smart_method_definition(code: str) -> dict[str, Any]:
    return SMART_METHOD_BY_CODE[code]
