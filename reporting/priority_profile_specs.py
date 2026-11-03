"""Focused enrichment for the five latest collected product populations."""
from reporting.category_refresh_specs import rows


def enrich_priority_profiles(specs: dict) -> None:
    changes = {
        "Cordless Handheld Vacuum": {
            "profile_id": "handheld-vacuum/1.1.0", "category_label_en": "Handheld vacuum",
            "tasks": rows(r"workbench|工作台与设备缝隙|\bworkbench\b|\bkeyboard\b|\bdesk\b" "\n" r"family|儿童餐后与日常碎屑|\bkids?\b|\bchildren\b|\bbreakfast\b" "\n" r"travel|房车与旅行|\brv\b|\bcamper\b|\btravel\b"),
            "workflows": rows(r"boost|强档与常规档切换|\bboost\b|\bturbo\b|\bhigh mode\b|\blow mode\b" "\n" r"brush_hair|刷头缠毛清理|\bbrush roll\b|\btangl\w*\b" "\n" r"filter_dry|滤网洗后晾干|\bdry (?:the )?filter\b|\bfilter.*dry\b"),
            "aspects": rows(r"pet_pickup|宠物毛发拾取|\bpet hair\b|\bdog hair\b|\bcat hair\b|\bfur\b" "\n" r"large_debris|大颗粒入口与堵塞|\bcereal\b|\bkibble\b|\bclog\w*\b|\blarge debris\b" "\n" r"trigger|扳机保持与手部疲劳|\btrigger\b|\bhold (?:the )?button\b|\bcramp\w*\b" "\n" r"dust_leak|排气返尘与密封|\bblows? (?:out )?dust\b|\bdust leak\b|\bseal\b" "\n" r"charging_storage|充电座与收纳|\bdock\b|\bcharging stand\b|\bwall mount\b" "\n" r"emptying_hygiene|清空卫生与扬尘|\bempty\w*\b|\bdirty hands\b|\btouch (?:the )?dirt\b" "\n" r"battery_replace|电池更换与平台兼容|\breplaceable battery\b|\bremovable battery\b|\bswap\w* batter\w*\b"),
        },
        "Misting Fan": {
            "profile_id": "misting-fan/1.1.0",
            "tasks": rows(r"camping|露营与房车|\bcamp\w*\b|\brv\b" "\n" r"animals|宠物与畜舍降温|\bpets?\b|\bdogs?\b|\bhorses?\b|\bkennel\b|\bbarn\b" "\n" r"event|聚会与活动|\bparty\b|\bparties\b|\bevent\b|\btailgat\w*\b"),
            "materials": rows(r"hard_water|硬水与矿物沉积|\bhard water\b|\bmineral\w*\b|\bcalcium\b" "\n" r"tank_water|储水箱供水|\bwater tank\b|\breservoir\b"),
            "workflows": rows(r"fan_only|仅送风模式|\bfan.only\b|\bwithout (?:the )?mist\b|\bmist off\b" "\n" r"tank_refill|水箱补水与清洗|\brefill\w*\b|\bclean\w* (?:the )?tank\b" "\n" r"remote_control|遥控与摆头|\bremote\b|\boscillat\w*\b" "\n" r"descale|喷嘴除垢|\bdescal\w*\b|\bvinegar\b|\bclean\w* (?:the )?nozzles?\b"),
            "aspects": rows(r"water_runtime|耗水速度与补水间隔|\bwater consumption\b|\buses? (?:a lot of|too much) water\b|\brun\w* out of water\b|\brefill\w*\b" "\n" r"pump_prime|水泵启动与吸水|\bpump\b|\bprim\w*\b|\bsiphon\b" "\n" r"humidity_effect|湿度对降温的影响|\bhumid\w*\b|\bdry climate\b" "\n" r"oscillation|摆头、俯仰与覆盖|\boscillat\w*\b|\btilt\w*\b|\bpivot\b" "\n" r"remote|遥控与操作|\bremote\b|\bbuttons?\b|\bcontrols?\b" "\n" r"noise|风机与水泵噪音|\bnoise\b|\bnoisy\b|\bquiet\b|\bloud\b" "\n" r"stability|支架、脚座与抗倾倒|\bstand\b|\btripod\b|\btip\w* over\b|\bstable\b" "\n" r"runtime_power|续航与供电切换|\brun ?time\b|\bbattery life\b|\bpower cord\b|\bac power\b"),
        },
        "Sprayer": {
            "profile_id": "sprayer/1.1.0",
            "tasks": rows(r"garden|花园、果园与温室|\borchard\b|\bgreenhouse\b|\bplants?\b|\bflower\w*\b" "\n" r"large_land|农场与大面积管理|\bacres?\b|\bfarm\b|\bpasture\b" "\n" r"cleaning|清洁与消毒|\bdisinfect\w*\b|\bsanitiz\w*\b|\bbleach\b" "\n" r"mosquito|蚊虫与雾化防治|\bmosquito\w*\b|\bfogg\w*\b"),
            "materials": rows(r"lawn_turf|草坪与地被|\blawn\b|\bturf\b|\bgrass\b" "\n" r"trees|树木与高处叶面|\btrees?\b|\bleaves\b|\bfoliage\b"),
            "workflows": rows(r"prime|启动、排气与引水|\bprim\w*\b|\bair lock\b|\bstart\w*\b" "\n" r"carry_fit|背带调整与搬运|\bstraps?\b|\bharness\b|\bcarry\b" "\n" r"swap_tank|罐体与药液切换|\bswap\w* tanks?\b|\binterchangeable\b" "\n" r"drain_store|排空、越冬与存放|\bdrain\w*\b|\bwinter\b|\bstor\w*\b"),
            "aspects": rows(r"harness|背带、腰带与背部舒适|\bstraps?\b|\bharness\b|\bwaist belt\b|\bback pain\b|\bpadded\b" "\n" r"hand_control|握持、扳机与锁定|\btrigger\b|\bgrip\b|\btrigger lock\b|\bhand cramps?\b" "\n" r"loaded_weight|装液重量与搬运疲劳|\bheavy\b|\bweight\b|\bcarry\b|\bfatigue\b" "\n" r"tank_capacity|容量、刻度与加注口|\bcapacity\b|\bgallons?\b|\bliters?\b|\bmarkings?\b|\bfiller\b" "\n" r"reach|喷杆长度与喷射距离|\bwand\b|\breach\b|\bspray distance\b|\bextension\b" "\n" r"pump_prime|水泵、引水与起动|\bpump\b|\bprim\w*\b|\bwon['’]?t start\b|\bair lock\b" "\n" r"seal_gasket|密封圈与止回件|\bo.?rings?\b|\bgasket\w*\b|\bcheck valve\b" "\n" r"runtime_volume|续航与单次处理量|\brun ?time\b|\bbattery life\b|\btanks? (?:per|on)\b" "\n" r"chemical_change|残液、药液切换与清洗|\bresidu\w*\b|\bcontaminat\w*\b|\bflush\w*\b"),
        },
    }
    for name, change in changes.items():
        original = specs[name]
        for key, value in change.items():
            if key in {"tasks", "materials", "workflows", "aspects"}:
                original[key] = (*original[key], *value)
            else:
                original[key] = value
    # A bare family noun is not evidence about pressure or coverage.
    specs["Sprayer"]["aspects"] = tuple(
        (key, label, r"\bpressure\b|\bcoverage\b|\bspray (?:force|power)\b") if key == "spray_pressure" else (key, label, pattern)
        for key, label, pattern in specs["Sprayer"]["aspects"]
    )
