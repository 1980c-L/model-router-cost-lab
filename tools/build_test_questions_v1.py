# -*- coding: utf-8 -*-
"""构建 M2 测试集：从历史版本迁移并生成 T29/T30 长材料，然后做题集自检。

- 默认 `--source eval/test_questions_v1.json` → `--out eval/test_questions_v2.json`；
  原 v1 文件保持不动（已有桩产物绑定它的字节身份）。
- v1→v2 迁移：仅修 T08（13 号 R4——原题面要求顶层 JSON 数组，与 grade_extract 的
  "顶层对象 + answer_key 顶层字段"契约矛盾，正确答案必被拒），并把版本号升为 test_v2。
  其余 29 题的题面/材料/判据/答案一律原样继承。
- 长材料（T29/T30）由本脚本确定性生成（无随机数），重复运行输出逐字节一致。
- 自检覆盖：题集完整性、路由预期槽位（router_rules_v2）、判据正反例演练、T08 契约。
- 本脚本不联网、不发起任何模型请求；判据演练使用 grade 模块的离线判定。

用法（在 model-router-cost-lab 目录下）：
  py -3 tools\\build_test_questions_v1.py                 # v1 → v2
  py -3 tools\\build_test_questions_v1.py --source ... --out ... --version ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import grade  # noqa: E402
import router  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SOURCE = os.path.join(ROOT, "eval", "test_questions_v1.json")
DEFAULT_OUT = os.path.join(ROOT, "eval", "test_questions_v2.json")
CRITERIA_PATH = os.path.join(ROOT, "eval", "criteria_m2_v1.json")

QUESTIONS_PATH = DEFAULT_OUT  # 自检与报告使用的目标题集（main 中按参数覆盖）

# v1→v2 迁移规则（只列真正需要改的题；其余原样继承）
T08_FIX = {
    "question": "材料里有两位校区的器材采购记录。请抽取【南校区】那一条，输出 JSON，字段：campus、item、qty、budget_owner。其中 qty 用数字类型。",
    "constraints": {
        "require_json": True,
        "required_fields": ["campus", "item", "qty", "budget_owner"],
        "field_types": {"qty": "number"},
        "material_count": 1,
    },
    "answer_key": {
        "campus": "南校区",
        "item": "篮球记分牌",
        "qty": 6,
        "budget_owner": "学生活动科",
    },
    "notes": "单对象四字段：不触发 R5/R2，预期默认档 low。干扰项：北校区记录。v2 修正（13 号 R4）：原 v1 要求顶层 JSON 数组，与 grade_extract 的顶层对象契约矛盾，正确答案必被拒；现改为指定单一校区的顶层对象任务，判据与题面一致。",
}

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


# --------------------------------------------------------------- T29 长材料

T29_SEASON = ["冬季", "冬季", "春季", "春季", "春季", "夏季", "夏季", "夏季", "秋季", "秋季", "秋季", "冬季"]
T29_WATER = [3180, 3145, 3060, 3010, 2955, 2890, 2860, 2835, 2820, 2790, 2770, 2760]
T29_PREV_WATER = 3210  # 上年 12 月
T29_ELEC = [21.4, 20.8, 19.6, 18.9, 19.2, 22.5, 34.8, 33.9, 26.4, 21.8, 20.3, 21.0]
T29_PREV_ELEC = 22.0
T29_AC = [12, 11, 10, 9, 12, 30, 48, 47, 33, 18, 13, 12]
T29_LAMP = [9, 7, 11, 8, 12, 14, 13, 12, 10, 8, 7, 6]
T29_GATE = [1, 0, 2, 1, 2, 3, 2, 1, 1, 0, 2, 1]
T29_DIS = [4, 4, 5, 5, 6, 6, 6, 6, 5, 5, 4, 4]
T29_GREEN = [16, 15, 15, 14, 14, 13, 13, 12, 13, 14, 15, 15]
T29_PLANTS = [0, 0, 40, 20, 0, 0, 0, 0, 60, 30, 10, 0]
T29_SPECIAL = [
    "完成年度水电表具集中核验：共校验总表 6 块、楼层分表 118 块，误差均在允许范围内；同步更新表具台账与远传点位图。",
    "春节假期执行低谷巡检方案：留守值班 2 人，重点保障配电房与二次供水泵房；假期内报修响应未超 4 小时，无投诉。",
    "完成二次供水泵房滤芯更换与水箱人孔密封检查；春季绿化返青浇灌开始，绿化用水占比回升，已同步调整月度用水计划。",
    "按年度计划实施全园停水检修 8 小时，更换主管网阀门 3 只，提前三天张贴公告并在业主群同步；恢复供水当日水质送检合格。",
    "开展节水宣传周：发放节水提示卡 600 张，组织两场社区宣讲；本月用水量环比继续下降，节水措施初见成效。",
    "完成雨季前天台排水疏通：清理落水口 42 处，检查雨水井 36 座；地下车库集水坑潜水泵试运行 8 台，全部正常。",
    "防台防汛专项：加固室外广告牌 6 处，检查地下车库挡水板 8 套；台风预警期间增加一次夜间专项巡查，无积水事件。",
    "持续高温应对：绿化浇灌调整为夜间时段以减少蒸发损耗；配电房加装临时通风扇 4 台，红外测温频次加密至每周一次。",
    "开学季人流上升：公共区域照明开启时长延长 1 小时；快递柜与门禁用电分表读数纳入月报，方便后续分项跟踪。",
    "按年度计划实施第二次全园停水检修 6 小时，完成蓄水池清洗与消毒，水质送检全部合格；本次检修未收到有效投诉。",
    "完成秋季绿化补种与落叶清运专项；雨污管网完成年度第二次全面巡查，发现并修复一处轻微渗漏点。",
    "年终设施盘点启动：水电表底数冻结登记，备用钥匙与应急物资清点造册；全年维修工单已全部归档。",
]
T29_HINT = [
    "本月气温较低，空调用电处于全年低位，预计下月维持。",
    "假期用电回落明显，动力用电与上月基本持平。",
    "气温回升，空调用电开始爬坡，照明用电稳定。",
    "检修月用水量受停水影响略有波动，属计划内。",
    "宣传周后用水环比继续下降，趋势良好。",
    "入夏用电抬升，空调占比升至三成。",
    "空调用电达全年峰值，已按需启动错峰提示。",
    "高温持续，空调占比维持高位，下月预期回落。",
    "气温下降，空调用电明显回落。",
    "检修与蓄水池清洗完成，用水量处于年内低位。",
    "照明用电随日照缩短略有上升，符合季节规律。",
    "年终用电平稳，与上年同期基本持平。",
]
T29_FOCUS = [
    "下月关注春节低谷期的保温防冻，低温对户外管道的影响每日复核。",
    "下月关注返工人流回升后的用水恢复与公共照明时长调整。",
    "下月关注春季绿化浇灌量与雨季前的管网复查安排。",
    "下月关注停水检修后的水质复测与新阀门的运行状态跟踪。",
    "下月关注用水下降趋势是否延续，评估宣传周长期效果。",
    "下月关注汛期前集水坑与排水设施专项复查的落实。",
    "下月关注台风季值守排班与应急物资的补充到位。",
    "下月关注高温持续期的空调用电走势与错峰执行情况。",
    "下月关注用电回落节奏与开学季设备设施的复查。",
    "下月关注蓄水池清洗后的水质跟踪与年度工作收尾。",
    "下月关注冬季防冻准备与落叶清运的收尾进度。",
    "下月关注年度盘点数据核对与来年运行计划的编制。",
]


def t29_material():
    sections = []
    for i in range(12):
        m = i + 1
        if m == 1:
            wchg = f"下降 {round((T29_PREV_WATER - T29_WATER[0]) / T29_PREV_WATER * 100, 1)}%"
            echg = f"下降 {round((T29_PREV_ELEC - T29_ELEC[0]) / T29_PREV_ELEC * 100, 1)}%"
        else:
            wp = T29_WATER[i - 1]
            ep = T29_ELEC[i - 1]
            wchg = ("下降" if T29_WATER[i] <= wp else "上升") + f" {round(abs(wp - T29_WATER[i]) / wp * 100, 1)}%"
            echg = ("下降" if T29_ELEC[i] <= ep else "上升") + f" {round(abs(ep - T29_ELEC[i]) / ep * 100, 1)}%"
        ac = T29_AC[i]
        light_pct = 22
        pump_pct = 26
        rest = 100 - ac - light_pct - pump_pct
        sections.append(
            f"{m} 月园区运行月报（{T29_SEASON[i]}）\n"
            f"一、水电数据。全园用水量 {T29_WATER[i]} 吨，环比{wchg}；用电量 {T29_ELEC[i]} 万千瓦时，环比{echg}。"
            f"分项用电占比：空调 {ac}%、照明 {light_pct}%、动力与水泵 {pump_pct}%、其他 {rest}%。\n"
            f"二、设施巡检。二次供水泵房巡检 4 次，余氯与浊度全部达标；雨污管网巡查 2 次，无堵塞；"
            f"配电房红外测温 2 次，无过热点；电梯机房温湿度记录完整，异动为零。\n"
            f"三、维修闭环。楼道与路灯报修 {T29_LAMP[i]} 起，全部在 24 小时内闭环；电梯维保按期完成，困人事件 0 次；"
            f"门禁与道闸故障 {T29_GATE[i]} 起，均当日恢复。\n"
            f"四、专项工作。{T29_SPECIAL[i]}\n"
            f"五、安全。消防设施检查 2 次，全部正常；微型消防站器材抽查合格；本月无停水事故、无消防事故、无人员伤害事件。\n"
            f"六、环境。垃圾房消杀 {T29_DIS[i]} 次；绿化用水占比 {T29_GREEN[i]}%；补种绿植 {T29_PLANTS[i]} 株；雨污井盖防坠网抽查 10 处。\n"
            f"七、能耗提示。{T29_HINT[i]} 本月综合控制室值班 {28 + m} 班，交接班记录完整，异常工单 {T29_LAMP[i] + T29_GATE[i]} 张。\n"
            f"八、下月关注。{T29_FOCUS[i]}"
        )
    return "\n\n".join(sections)


# --------------------------------------------------------------- T30 长材料

T30_QUARTER = ["一", "一", "一", "二", "二", "二", "三", "三", "三", "四", "四", "四"]
T30_TEMP_ANOM = [2, 1, 3, 2, 4, 5, 6, 5, 3, 2, 3, 1]
T30_COMP_ANOM = [1, 0, 1, 1, 2, 2, 3, 2, 1, 1, 2, 0]
T30_BELT_ANOM = [0, 1, 0, 1, 1, 2, 2, 1, 0, 1, 0, 1]
T30_OTHER_ANOM = [0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 0]
T30_HANDLING = [
    "全部异常当班处理完毕：温控探头漂移 2 只已现场校准，空压机排水器堵塞 1 处已清理，复测正常。",
    "本月异常较少：仅 1 起传送带轻微跑偏，张紧后恢复；温控系统无异常。",
    "3 月 14 日完成空压机进气阀更换（详见备件记录）；其余温控异常 3 起均为传感器积尘，清洁后消除。",
    "温控异常集中在新风段：已调整设定区间并加装防尘滤网；空压机与传送带各 1 起异常当日闭环。",
    "联系厂家远程诊断 1 次，更新温控固件；异常工单全部闭环，未影响生产节拍。",
    "高温期加密巡检：温控异常 5 起全部当日闭环；空压机与传送带异常均已处理。",
    "温控异常达全年峰值 6 起，主要成因为传感器积尘，已批量清洁并调整巡检周期。",
    "延续高温运行：完成空调主机备用机切换演练 1 次；温控异常 5 起全部闭环。",
    "异常回落：传送带跑偏 1 起已张紧校正；温控异常 3 起当日处理。",
    "配电柜一处接线端子温度偏高，已紧固并复测正常；温控异常 2 起闭环。",
    "11 月 9 日更换 3 号线传送带 2 根（详见备件记录）；温控异常 3 起闭环。",
    "全年巡检收尾：异常工单全部闭环，未遗留未处理项。",
]
T30_PARTS = [
    "本月无备件更换；库存备件账物相符。",
    "本月无备件更换；温控传感器备货补充 4 只。",
    "更换空压机进气阀 1 台（3 月 14 日，停机窗口 3 小时）。",
    "本月无备件更换；传送带备用辊筒库存充足。",
    "本月无备件更换。",
    "本月无备件更换；空调滤网批量更换属计划保养。",
    "温控传感器批量清洁，无整件更换。",
    "本月无备件更换。",
    "本月无备件更换。",
    "本月无备件更换。",
    "更换 3 号线传送带 2 根（11 月 9 日，停机窗口 2 小时）。",
    "本月无备件更换；全年备件消耗台账已复核。",
]
T30_MAINT = [
    "年度润滑计划启动：完成传送带托辊润滑 1 轮。",
    "按计划完成空压机季度保养与安全阀校验。",
    "完成温控系统春季标定：探头校准 22 只。",
    "完成配电柜春季清扫紧固 1 轮。",
    "完成传动部件季度检查与张紧度调整。",
    "入夏前完成空调主机冷凝器清洗。",
    "高温期专项：电机绝缘检测 1 轮。",
    "完成空压机三季度保养。",
    "秋季标定：温控探头校准 22 只。",
    "完成配电柜秋季清扫紧固 1 轮。",
    "完成传动部件季度检查。",
    "年度保养计划全部完成，设备档案更新归档。",
]
T30_NOTES = [
    "摘记：本月低温时段温控系统响应偏慢，现场检查确认与新风机预热逻辑有关，已在控制端调整启动顺序；空压机排水器在连续运行后出现一次堵塞，清理后加装前置过滤杯；传送带托辊润滑后运行噪音明显下降；配电柜测温无异常。",
    "摘记：本月为全年异常最少月份，仅一起传送带跑偏；按计划完成空压机季度保养，安全阀校验合格；温控系统处于稳定区间，未做参数调整；月度备件盘点账物相符。",
    "摘记：本月完成温控系统春季标定，22 只探头全部校准，其中 3 只漂移超差已更换；空压机进气阀更换后压力波动消除；传送带与配电柜巡检正常，无遗留问题。",
    "摘记：本月温控异常集中在新风段，与滤网积尘相关，已全部清洁并缩短更换周期；配电柜春季清扫紧固完成；空压机与传送带异常各一起，均当班闭环。",
    "摘记：本月联系厂家远程诊断一次，更新温控固件后误报消失；空调主机冷凝器清洗按入夏前计划完成；异常工单全部闭环，未影响生产节拍。",
    "摘记：高温期加密巡检后温控异常 5 起全部当日闭环，成因以传感器积尘为主；空压机连续运行温度偏高，已加强机房通风；传送带张紧度按季度计划调整完毕。",
    "摘记：本月温控异常达全年峰值 6 起，批量清洁传感器并将巡检周期由每周加密到每半周；完成电机绝缘检测一轮，结果合格；无计划外停机。",
    "摘记：完成空调主机备用机切换演练一次，切换过程平稳；温控异常 5 起闭环；空压机三季度保养完成，油路无渗漏；配电柜红外测温正常。",
    "摘记：气温回落后异常明显减少；传送带跑偏一起已张紧校正；温控探头秋季标定计划已排定；配电柜无异常发现。",
    "摘记：配电柜一处接线端子温度偏高，紧固后复测正常，已纳入下月复查清单；温控异常两起闭环；传动部件季度检查完成。",
    "摘记：更换 3 号线传送带两根，停机窗口两小时，恢复后运行平稳；温控异常三起闭环；年度保养计划接近完成。",
    "摘记：全年巡检收尾，异常工单全部闭环；设备档案与巡检记录抽查无缺漏；全年整机停机为零；来年一季度保养计划已排定。",
]


def t30_material():
    sections = []
    for i in range(12):
        m = i + 1
        anom_total = T30_TEMP_ANOM[i] + T30_COMP_ANOM[i] + T30_BELT_ANOM[i] + T30_OTHER_ANOM[i]
        top_note = "，为当月占比最高的异常类别" if T30_TEMP_ANOM[i] >= max(T30_COMP_ANOM[i], T30_BELT_ANOM[i], T30_OTHER_ANOM[i]) and T30_TEMP_ANOM[i] > 0 else ""
        sections.append(
            f"{m} 月设备巡检记录汇编（{T30_QUARTER[i]}季度）\n"
            f"一、巡检覆盖。本月按计划完成：温控系统巡检 8 次、空压机 4 次、传送带 4 次、配电柜 2 次，覆盖率 100%。"
            f"巡检路线覆盖主车间、辅房与室外机组，固定巡检点 46 个，临时抽检点按月轮换 6 个。\n"
            f"二、异常统计。本月各类异常合计 {anom_total} 起：温控类 {T30_TEMP_ANOM[i]} 起{top_note}，"
            f"空压机类 {T30_COMP_ANOM[i]} 起，传送带类 {T30_BELT_ANOM[i]} 起，其他 {T30_OTHER_ANOM[i]} 起。\n"
            f"三、处理情况。{T30_HANDLING[i]}\n"
            f"四、备件与更换。{T30_PARTS[i]}\n"
            f"五、停机情况。本月整机停机 0 次，计划外停机 0 次；部件更换均在计划停机窗口内完成。\n"
            f"六、保养。{T30_MAINT[i]}\n"
            f"七、归档。本月巡检表单已归档 18 份，现场照片 96 张；巡检记录与工单编号一一对应，抽查无缺漏。\n"
            f"八、巡检摘记。{T30_NOTES[i]}\n"
            f"九、附表。本月设备点检项目合计 128 项，一次性合格 {125 - (i % 4)} 项；温湿度连续记录 31 天无断点；"
            f"能耗抄表 4 次无缺漏；备件出入库台账已复核，账物相符。"
        )
    return "\n\n".join(sections)


# --------------------------------------------------------------- 参考答案（判据演练用）

REF_T21_GOOD = (
    "本年度公共自行车骑行总量 820 万人次，同比增长 12%；中心城区站点 210 个，覆盖率 85%。"
    "年度报修 1.4 万次，平均修复 26 小时。明年计划新增 30 个站点并试点电动助力车。"
)
REF_T21_MISSING_FACT = "本年度骑行量有所增长，站点覆盖中心城区，报修处理及时，明年还会继续增设站点。"
REF_T21_OVERLONG = REF_T21_GOOD * 4
REF_T29_GOOD = "全年用水量总体下降，用电高峰在 7-8 月；年内按计划完成两次停水检修，全年无事故。"
REF_T11_GOOD = (
    "系统会自动执行一次缓存预热任务；若预热后仍低于 60%，则向值班群发送告警并冻结配置变更窗口。"
)
REF_T19_REFUSAL = "资料中未找到相关依据。"
REF_T19_NON_REFUSAL = "出厂序列号是 SRV-2024-0031。"

EXPECTED_SLOTS = {
    "T01": "low", "T02": "low", "T03": "low", "T04": "low",
    "T05": "high", "T06": "high", "T07": "high",
    "T08": "low", "T09": "high", "T10": "high",
    "T11": "low", "T12": "low", "T13": "low", "T14": "low",
    "T15": "high", "T16": "high", "T17": "high", "T18": "high",
    "T19": "low", "T20": "low",
    "T21": "low", "T22": "low", "T23": "low", "T24": "low",
    "T25": "high", "T26": "high", "T27": "high", "T28": "high",
    "T29": "high", "T30": "high",
}


def _sha256_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_request_text(question):
    parts = [question.get("question", "")]
    materials = question.get("materials") or []
    if materials:
        parts.append("【资料】")
        for index, material in enumerate(materials, 1):
            parts.append(f"[材料{index}]\n{material}")
    return "\n\n".join(parts)


def main():
    global QUESTIONS_PATH
    parser = argparse.ArgumentParser(description="构建 M2 测试集（v1→v2 迁移 + 长材料 + 自检）")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="来源题集（历史版本，保持不动）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出题集")
    parser.add_argument("--version", default="test_v2", help="输出题集的 version 字段")
    args = parser.parse_args()
    QUESTIONS_PATH = os.path.abspath(args.out)
    source_path = os.path.abspath(args.source)
    if os.path.abspath(QUESTIONS_PATH) == source_path:
        print("[拒绝] --out 不能等于 --source：历史题集字节必须保留", file=sys.stderr)
        return 1

    with open(source_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    questions = data["questions"]
    by_id = {q["test_id"]: q for q in questions}

    # v1→v2 迁移：只改 T08（13 号 R4），其余题原样继承
    by_id["T08"].update(json.loads(json.dumps(T08_FIX)))
    data["version"] = args.version
    data["note"] = (
        "M2 测试集：与开发集完全分离的合成题集，30 题、三类各 10。全部材料自造（source=synthetic），"
        "不得与 dev 集混用。判据参数（answer_key / coverage_keywords / max_chars / expect_refusal / summary_facts）"
        "随本文件一起冻结；真实运行前不得按结果调整。路由规则固定为 router_rules_v2，每题 notes 记录预期路由与设计意图，"
        "notes 不进入路由输入。v2（2026-09-27，13 号 R4）：仅把 T08 由「顶层 JSON 数组」改为「抽取指定校区的顶层对象」，"
        "使题面与 grade_extract 判据契约一致（原 v1 正确答案必被拒）；其余 29 题逐字节继承 v1。"
    )

    # 注入长材料（幂等：重复运行生成相同字节）
    by_id["T29"]["materials"] = [t29_material()]
    by_id["T30"]["materials"] = [t30_material()]

    with open(QUESTIONS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[build] 已由 {os.path.basename(source_path)} 构建 {os.path.basename(QUESTIONS_PATH)}（version={args.version}）")

    # ---------- 题集完整性 ----------
    check("题集版本与来源标记", data.get("version") == args.version and data.get("source") == "synthetic")
    check("题数为 30", len(questions) == 30, f"实际 {len(questions)}")
    ids = [q["test_id"] for q in questions]
    check("题号唯一且为 T01–T30", len(set(ids)) == 30 and sorted(ids) == [f"T{i:02d}" for i in range(1, 31)])
    golds = {}
    for q in questions:
        golds[q["task_type_gold"]] = golds.get(q["task_type_gold"], 0) + 1
    check("三类各 10 题", golds == {"extract": 10, "qa_grounded": 10, "summary": 10}, str(golds))
    check("长材料占位符已全部替换", "<<LONG" not in json.dumps(data, ensure_ascii=False))

    problems = []
    for q in questions:
        tid = q["test_id"]
        if not q.get("question"):
            problems.append(f"{tid} 缺题面")
        if not q.get("materials"):
            problems.append(f"{tid} 缺材料")
        if q["task_type_gold"] == "extract":
            c = q.get("constraints") or {}
            if not c.get("require_json"):
                problems.append(f"{tid} 抽取题缺 require_json")
            if not c.get("required_fields"):
                problems.append(f"{tid} 缺 required_fields")
            if not isinstance(q.get("answer_key"), dict):
                problems.append(f"{tid} 缺 answer_key")
        elif q["task_type_gold"] == "qa_grounded":
            if not isinstance(q.get("key_facts"), list) or not q["key_facts"]:
                problems.append(f"{tid} 缺 key_facts")
            if "expect_refusal" not in q:
                problems.append(f"{tid} 缺 expect_refusal")
        else:
            c = q.get("constraints") or {}
            crit = q.get("criteria") or {}
            if not c.get("max_chars") or not crit.get("max_chars"):
                problems.append(f"{tid} 缺 max_chars")
            if not isinstance(crit.get("coverage_keywords"), list) or not crit["coverage_keywords"]:
                problems.append(f"{tid} 缺 coverage_keywords")
            if not isinstance(crit.get("forbidden_keywords"), list):
                problems.append(f"{tid} 缺 forbidden_keywords")
            facts = q.get("summary_facts")
            if not isinstance(facts, list) or not (3 <= len(facts) <= 5):
                problems.append(f"{tid} summary_facts 应为 3–5 条")
            if "总长度不超过" not in q["question"] or "去除首尾空白" not in q["question"]:
                problems.append(f"{tid} 题面缺显式长度上限与计数说明")
    check("逐题最低要求齐备", not problems, "；".join(problems))

    # 与开发集分离（材料文本不重叠）
    with open(os.path.join(ROOT, "eval", "dev_questions.json"), "r", encoding="utf-8") as fh:
        dev = json.load(fh)
    dev_materials = {m for q in dev["questions"] for m in (q.get("materials") or [])}
    overlap = [q["test_id"] for q in questions if set(q.get("materials") or []) & dev_materials]
    check("材料与开发集无重叠", not overlap, str(overlap))

    # ---------- 路由预期（router_rules_v2，gold 不进入决策） ----------
    with open(CRITERIA_PATH, "r", encoding="utf-8") as fh:
        criteria = json.load(fh)

    route_problems = []
    pred_problems = []
    for q in questions:
        request_text = build_request_text(q)
        decision = router.decide(request_text, q.get("constraints"), None, instruction_text=q.get("question"))
        if decision["chosen_slot"] != EXPECTED_SLOTS[q["test_id"]]:
            route_problems.append(f"{q['test_id']}: 期望 {EXPECTED_SLOTS[q['test_id']]}，实际 {decision['chosen_slot']}（{decision['matched_rule']}）")
        if decision["task_type_pred"] != q["task_type_gold"]:
            pred_problems.append(f"{q['test_id']}: gold={q['task_type_gold']} pred={decision['task_type_pred']}")
    check("30 题路由槽位与预期一致", not route_problems, "；".join(route_problems))
    check("30 题任务类型识别与 gold 一致", not pred_problems, "；".join(pred_problems))

    # 长输入确实触发 R1
    for tid in ("T29", "T30"):
        q = by_id[tid]
        text = build_request_text(q)
        check(f"{tid} 输入长度 > 6000（R1）", len(text) > 6000, f"实际 {len(text)}")

    # ---------- 判据正反例演练（criteria_m2_v1 + 修订后 grade） ----------
    def drill(tid, ref, expect_mech):
        result = grade.grade(by_id[tid]["task_type_gold"], ref, by_id[tid], criteria)
        ok = result["mechanized_pass"] is bool(expect_mech)
        check(f"{tid} 参考答案演练（mechanized={result['mechanized_pass']}，期望 {expect_mech}）", ok, result.get("reason"))
        return result

    # T08 契约（13 号 R4）：题面与判据一致，正确参考答案能过、错误答案不能过
    t08 = by_id["T08"]
    check("T08 题面不再要求顶层数组", "数组" not in t08["question"] and "顶层" not in t08["question"])
    r = grade.grade("extract", json.dumps(t08["answer_key"], ensure_ascii=False), t08, criteria)
    check("T08 正确参考答案通过冻结判据", r["mechanized_pass"] is True, r.get("reason"))
    r = grade.grade("extract", json.dumps({"campus": "南校区", "item": "篮球记分牌", "qty": "6", "budget_owner": "后勤保障科"}, ensure_ascii=False), t08, criteria)
    check("T08 错误答案（类型错 + 值错）不通过", r["mechanized_pass"] is False)
    r = grade.grade("extract", json.dumps(t08["answer_key"]["items"] if "items" in t08["answer_key"] else [], ensure_ascii=False), t08, criteria)
    check("T08 旧 v1 数组形答案不再被接受（题面已改）", r["mechanized_pass"] is False)

    r = drill("T21", REF_T21_GOOD, True)
    check("T21 正确参考答案：人工项含 facts_covered_by_human",
          "facts_covered_by_human" in r["human_pending"] and "no_added_content_by_human" in r["human_pending"],
          str(r["human_pending"]))
    drill("T21", REF_T21_MISSING_FACT, False)
    drill("T21", REF_T21_OVERLONG, False)
    drill("T29", REF_T29_GOOD, True)
    drill("T11", REF_T11_GOOD, True)
    drill("T19", REF_T19_REFUSAL, True)
    drill("T19", REF_T19_NON_REFUSAL, False)

    # criteria_v1 历史行为不受影响（dev_v3 D08 演练）
    with open(os.path.join(ROOT, "eval", "criteria_v1.json"), "r", encoding="utf-8") as fh:
        criteria_v1 = json.load(fh)
    d08 = {q["test_id"]: q for q in dev["questions"]}["D08"]
    ref_d08 = "检索评测常用 Hit@K 与 MRR。Hit@K 看证据片段是否进入前 K 条，分母是计分题数；MRR 看首个相关结果的位置。两者只描述检索位置，不是答案准确率。"
    r = grade.grade("summary", ref_d08, d08, criteria_v1)
    check("criteria_v1 行为不变：D08 人工项仅 no_added_content_by_human",
          r["mechanized_pass"] is True and r["human_pending"] == ["no_added_content_by_human"], str(r["human_pending"]))

    passed = sum(1 for c in CHECKS if c["ok"])
    print(f"\n==== 题集构建与自检：{passed}/{len(CHECKS)} PASS ====")
    report_name = "_" + os.path.splitext(os.path.basename(QUESTIONS_PATH))[0] + "_build_check.json"
    report_path = os.path.join(ROOT, "runs", report_name)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"passed": passed, "total": len(CHECKS), "checks": CHECKS,
                   "source": os.path.basename(source_path),
                   "out": os.path.basename(QUESTIONS_PATH),
                   "out_sha256": _sha256_file(QUESTIONS_PATH),
                   "questions_sha256_note": "题集最终字节以交付时的 source_manifest 为准"}, fh, ensure_ascii=False, indent=2)
    print(f"[report] {report_path}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
