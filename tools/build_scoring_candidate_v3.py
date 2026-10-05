# -*- coding: utf-8 -*-
"""构建 M2 评分版本候选：criteria_m2_v3 + 题集 test_v4（零调用、确定性、可重跑）。

依据 31 号独立复审 §5「仅准备一个新评分版本候选」：
1. 抽取题：**字段精度与空白规则写进模型实际收到的题面**（照抄原文含空格），评分器保持严格比较；
2. 摘要题：把「数字与量词之间的空格、时间区间的不同写法、题面已允许的简写」等**表示差异**
   预先固定为每个事实点的**字面变体组**（`coverage_points`），不再当作漏事实；
   不做空白归一、不做语义判断、不新增 AI 裁判；
3. 拒答：机检明确选「固定短语遵循」（语义拒答只作另列观察），并写明评测目的；
4. 问答：写清「允许明确标注的推测/建议」，把边界写进人工项规则（需用户确认后生效）。

红线（本脚本自身保证）：
- 只写 `eval/test_questions_v4.json` 与 `eval/criteria_m2_v3.json` 两个**新版本**文件；
- `test_questions_v2/v3.json`、`criteria_m2_v1/v2.json`、`criteria_v1.json` 的字节保持不变（脚本前后比对）；
- 不改 `runner.py` / `router.py` / `m2_batch.py` / 计账；`grade.py` 的改动只在新字段 `coverage_points`
  出现时生效（旧题集无该字段 → 行为不变），由 `tools/verify_scoring_candidate_v3.py` 复核；
- 不把当前批次模型回答的任何句子写进题面或判据。

用法（在 model-router-cost-lab 目录下）：
  py -3 tools\\build_scoring_candidate_v3.py
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import grade  # noqa: E402
import router  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SOURCE = os.path.join(ROOT, "eval", "test_questions_v3.json")
DEFAULT_OUT = os.path.join(ROOT, "eval", "test_questions_v4.json")
DEFAULT_CRITERIA_OUT = os.path.join(ROOT, "eval", "criteria_m2_v3.json")
PROTECTED = (
    os.path.join(ROOT, "eval", "test_questions_v1.json"),
    os.path.join(ROOT, "eval", "test_questions_v2.json"),
    os.path.join(ROOT, "eval", "test_questions_v3.json"),
    os.path.join(ROOT, "eval", "criteria_v1.json"),
    os.path.join(ROOT, "eval", "criteria_m2_v1.json"),
    os.path.join(ROOT, "eval", "criteria_m2_v2.json"),
)

# v3 时点的路由槽位（由 tools/build_test_questions_v3.py 冻结；此处只作漂移检测）
V3_EXPECTED_SLOTS = {
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

EXTRACT_IDS = [f"T{i:02d}" for i in range(1, 11)]
SUMMARY_IDS = [f"T{i:02d}" for i in range(21, 31)]

# 抽取题统一写进题面的精度/空白契约（不含任何路由线索词，也不含答案）
EXTRACT_CLAUSE = "字符串字段的值请与材料原文逐字相同（包括原文中的空格与标点），不要增删空格；数字字段只填数字。"

# 摘要题统一写进题面的事实覆盖口径（不含路由线索词）。
# 修订 2（33 号 R2）：示例改用与任何冻结事实点数值都不重合的时间值（18/20），
# 避免把 T23 的真实答案「9 点到 12 点」写进题面；构建自检会机械核对这一点。
SUMMARY_CLAUSE = (
    "事实点核对按回答表达出的内容判断：数字与量词之间的空格可有可无、"
    "时间区间可用「到」「-」「至」连接（如写成 18-20 点或 18 点至 20 点）不计为事实缺失。"
)

# 题面声明规则的机械闭包：数字与量词之间的空格可有可无
_NUMBER_UNIT_GAP = re.compile(r"(?<=\d)\s*(?=[\u4e00-\u9fff])")


def allowed_writings(seed):
    """按「数字与量词之间的空格可有可无」生成写法闭包（种子恒在首位）。"""
    out = [seed]
    for form in (_NUMBER_UNIT_GAP.sub("", seed), _NUMBER_UNIT_GAP.sub(" ", seed)):
        if form not in out:
            out.append(form)
    return out


def time_writings(start, end):
    """时间区间的允许写法闭包（题面声明：连接符可用「到」「-」「至」；数字与量词之间、
    连接符与其后数字之间的空格可有可无）。

    骨架 ①`N1点{C}N2点`（C 为「到」「至」「-」，三个空格位自由组合，8 条/连接符 = 24 条）
    与骨架 ②`N1-N2点`（省略首个「点」的短写，数字与量词之间的空格自由，2 条）→ 共 26 条。

    修订 3（依据 35 号 §2）：修订 2 只对「到」「至」生成完整骨架，对「-」只留短写，
    比模型实际收到的题面更窄，漏掉 `9点-12点` 等 4 类写法；现补齐为三种连接符同走完整骨架、
    短写同时保留。首位仍是冻结事实的原始写法（`9 点到 12 点`）。
    """
    out = []
    for connective in ("到", "至", "-"):
        for gap_before_unit in (" ", ""):
            for gap_after_connective in (" ", ""):
                for gap_after_number in (" ", ""):
                    out.append(f"{start}{gap_before_unit}点{connective}"
                               f"{gap_after_connective}{end}{gap_after_number}点")
    for gap_after_number in (" ", ""):
        out.append(f"{start}-{end}{gap_after_number}点")
    return out


def expand_variants(point):
    """事实点声明 → 变体表（确定性；时间区间走专用闭包，其余走空格闭包）。"""
    if point.get("time_range"):
        return time_writings(*point["time_range"])
    out = []
    for seed in point["variants"]:
        for form in allowed_writings(seed):
            if form not in out:
                out.append(form)
    return out

# 摘要题事实点：每个点的**声明**（预先冻结；只加有表示差异证据的变体）。
# 修订 2：变体表由 expand_variants() 按上述两条规则机械生成闭包，本表只写种子/区间，不手写闭包。
COVERAGE_POINTS = {
    "T21": [
        {"id": "total_ridership", "fact": "年度骑行总量 820 万人次",
         "variants": ["820 万人次", "820万人次"]},
        {"id": "station_count", "fact": "中心城区站点 210 个",
         "variants": ["210 个", "210个"]},
        {"id": "repair_hours", "fact": "平均修复时长 26 小时",
         "variants": ["26 小时", "26小时"]},
        {"id": "e_bike_pilot", "fact": "明年在 10 个站点试点电动助力车",
         "variants": ["电动助力车"]},
    ],
    "T22": [
        {"id": "online_booking", "fact": "改为线上系统预约、线下不再受理",
         "variants": ["线上系统", "线上系统预约", "线上预约"]},
        {"id": "max_two_hours", "fact": "单次最长 2 小时",
         "variants": ["2 小时", "2小时"]},
        {"id": "suspend_15_days", "fact": "暂停预约权限 15 天",
         "variants": ["15 天", "15天"]},
        {"id": "whitelist", "fact": "重要会议可走白名单加急通道",
         "variants": ["白名单"]},
    ],
    "T23": [
        {"id": "time_window", "fact": "周六上午 9 点到 12 点",
         "time_range": ["9", "12"], "variants": ["9 点到 12 点"]},
        {"id": "venue", "fact": "中心广场风雨连廊",
         "variants": ["中心广场"]},
        {"id": "one_per_household", "fact": "摊位先到先得、每户限领一个",
         "variants": ["每户限领", "每户限领一个", "每户限一", "每户限一个"]},
        {"id": "donation_share", "fact": "收入三成自愿捐入互助基金",
         "variants": ["三成"]},
    ],
    "T24": [
        {"id": "upper_limit_8", "fact": "外借上限提高到 8 册",
         "variants": ["8 册", "8册"]},
        {"id": "loan_days_21", "fact": "借期缩短为 21 天",
         "variants": ["21 天", "21天"]},
        {"id": "online_renew", "fact": "可在线续借一次、延长 14 天",
         "variants": ["续借"]},
        {"id": "suspend_by_days", "fact": "逾期改为按天暂停外借权限",
         "variants": ["暂停"]},
    ],
    "T25": [
        {"id": "tech_debt", "fact": "一季度重心在偿还技术债", "variants": ["技术债"]},
        {"id": "search_revamp", "fact": "二季度发布搜索改版", "variants": ["搜索改版"]},
        {"id": "compliance", "fact": "三季度因合规检查暂停两个迭代", "variants": ["合规"]},
        {"id": "two_week_iteration", "fact": "四季度恢复双周迭代", "variants": ["双周迭代"]},
    ],
    "T26": [
        {"id": "knowledge_base", "fact": "把常见问题整理成自助知识库", "variants": ["知识库"]},
        {"id": "tags", "fact": "会话小结模板强制回填分类标签", "variants": ["标签"]},
        {"id": "retro", "fact": "每月按标签复盘高频问题", "variants": ["复盘"]},
        {"id": "sixty_percent", "fact": "自助知识库解决六成咨询", "variants": ["六成"]},
    ],
    "T27": [
        {"id": "sensor_light", "fact": "照明全部换成感应灯", "variants": ["感应灯"]},
        {"id": "curtain", "fact": "冷柜夜间加装帘布", "variants": ["帘布"]},
        {"id": "remote", "fact": "空调由总部远程设定", "variants": ["远程"]},
        {"id": "publicity", "fact": "节能数据每月公示", "variants": ["公示"]},
    ],
    "T28": [
        {"id": "booths_22", "fact": "共设 22 个摊位", "variants": ["22 个摊位", "22个摊位"]},
        {"id": "raised_9136", "fact": "两小时共筹得善款 9136 元", "variants": ["9136 元", "9136元"]},
        {"id": "pairing", "fact": "41 名同学报名长期结对助学", "variants": ["结对"]},
        {"id": "publicity", "fact": "善款明细当晚公示", "variants": ["公示"]},
    ],
    "T29": [
        {"id": "water", "fact": "全年用水量总体呈下降趋势", "variants": ["用水"]},
        {"id": "power", "fact": "用电高峰出现在夏季", "variants": ["用电"]},
        {"id": "maintenance", "fact": "全年按计划完成两次全园停水检修", "variants": ["检修"]},
        {"id": "no_accident", "fact": "全年无停水与消防事故", "variants": ["无事故"]},
    ],
    "T30": [
        {"id": "temperature_control", "fact": "温控类异常出现最多", "variants": ["温控"]},
        {"id": "abnormal", "fact": "异常工单全部闭环", "variants": ["异常"]},
        {"id": "replacement", "fact": "3 月与 11 月各更换过一次部件", "variants": ["更换"]},
        {"id": "normal", "fact": "其余设备运行正常", "variants": ["正常"]},
    ],
}

# T23 的「自愿」是冻结事实（三成自愿捐入）；把「非自愿」的表述预先固定为禁止内容。
FORBIDDEN_ADDITIONS = {
    "T23": ["强制捐赠", "强制捐入", "一律捐入", "必须捐入"],
}

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def diff_paths(old, new, prefix=""):
    """递归列出两个 JSON 结构之间发生变化的字段路径（用于把变更范围钉死在声明内）。"""
    out = set()
    if isinstance(old, dict) and isinstance(new, dict):
        for key in set(old) | set(new):
            out |= diff_paths(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else str(key))
        return out
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.add(prefix)
        else:
            for index, (a, b) in enumerate(zip(old, new)):
                out |= diff_paths(a, b, f"{prefix}[{index}]")
        return out
    if old != new:
        out.add(prefix)
    return out


def build_request_text(question):
    parts = [question.get("question", "")]
    materials = question.get("materials") or []
    if materials:
        parts.append("【资料】")
        for index, material in enumerate(materials, 1):
            parts.append(f"[材料{index}]\n{material}")
    return "\n\n".join(parts)


def slots_of(questions):
    out = {}
    for q in questions:
        decision = router.decide(build_request_text(q), q.get("constraints"), None,
                                 instruction_text=q.get("question"))
        out[q["test_id"]] = decision["chosen_slot"]
    return out


def gold_summary_text(question):
    """金答案（摘要）：冻结 summary_facts 拼接，再补上未被覆盖的事实点变体。

    只用冻结事实与冻结变体构造，**不使用任何模型回答的句子**。
    """
    text = "；".join(question.get("summary_facts") or [])
    missing = []
    for point in (question.get("criteria") or {}).get("coverage_points") or []:
        if not any(v in text for v in point.get("variants") or []):
            missing.append((point.get("variants") or [""])[0])
    if missing:
        text += "；另涉：" + "、".join(missing)
    return text


def writings_counts():
    """从**生成集合**取数，供判据与题集的说明文字引用（35 号 §3：不再保留手写旧数字）。"""
    total = sum(len(expand_variants(point))
                for points in COVERAGE_POINTS.values() for point in points)
    return {"time_window": len(time_writings("9", "12")), "all": total}


def build_criteria_v3():
    counts = writings_counts()
    v2 = load_json(os.path.join(ROOT, "eval", "criteria_m2_v2.json"))
    out = copy.deepcopy(v2)
    out["version"] = "criteria_m2_v3"
    out["status"] = "candidate"
    out["revision"] = 3
    out["frozen_at"] = "2026-09-29"
    out["revised_at"] = "2026-10-03"
    out["inherits"] = "criteria_m2_v2"
    out["questions_version"] = "test_v4"
    out["note"] = (
        "M2 判据 v3（候选，2026-09-29 首版 / 2026-10-03 修订 2，2026-10-03 修订 3，"
        "依据 31 号 §5、33 号 R1/R2、35 号 §2/§3）："
        "①抽取题把字段精度与空白规则写进题面，评分器保持严格字面比较；②摘要题改用 coverage_points"
        "（每个事实点一组**预先冻结的字面变体**），只把数字量词空格、时间区间写法、题面已允许的简写等"
        "表示差异计入，不做空白归一、不做语义判断、不新增 AI 裁判；③拒答机检明确选「固定短语遵循」并写明评测目的；"
        "④写清问答对「明确标注的推测/建议」的边界。"
        "**修订 2**（33 号）：(a) 变体命中增加数字边界（coverage_points_v2），`1820 万人次` 不再命中 `820 万人次`；"
        "(b) 时间区间允许写法闭包逐项冻结并逐点验证，题面示例与冻结事实数值解耦。"
        "**修订 3**（35 号）：(a) 时间区间三种连接符（到／至／-）同走完整骨架 `N1点{C}N2点`，"
        f"短写 `N1-N2点` 同时保留，时间闭包 {counts['time_window']} 条、全部写法 {counts['all']} 条；"
        "(b) 说明文字的计数一律取自生成集合（不再手写）；(c) 数值边界的保证范围收窄为**紧邻字符**并明示不覆盖项。"
        "coverage_threshold（0.6）、summary_human_items、拒答短语本身逐字继承 m2_v2；"
        "criteria_m2_v2/v1 与 test_questions_v3/v2 的字节保持不变。本文件是**候选**：采用前需用户裁决，"
        "并在下一轮真实执行前重新冻结实验设计与批准。"
    )
    out["coverage_match_rule"] = {
        "rule_version": "coverage_points_v2",
        "scope": "摘要题 criteria.coverage_points（每点一组预先冻结的字面变体）",
        "hit_rule": "任一变体以**合法数字边界**出现在回答里即算该点命中；不删空白、不做全半角/繁简/大小写/近义归一，不调用模型判断",
        "numeric_boundary": {
            "rule_version": "numeric_boundary_v1",
            "why": "33 号 R1：`v in text` 会把 `1820 万人次` 计为命中 `820 万人次`。"
                   "本候选把「错误数值不得算命中」收窄为**紧邻字符**规则并兑现。",
            "rule": "变体首字符是数字或小数点时，紧邻左侧不得是数字、小数点或数值符号（+-−±＋－）；"
                    "若紧邻左侧是千分位逗号（,／，），还要求逗号左边紧跟数字；"
                    "变体末尾是数字或小数点时，紧邻右侧不得是数字或小数点。"
                    "判定只看**紧邻**一个字符的位置，不做数值解析、不做空白归一。",
            "covers": "只覆盖**紧邻**写法：前缀多一位（1820）、紧邻符号（-820／＋820／±820）、"
                      "小数（0.820）、紧邻千分位（1,820／1，820）、以及变体末尾数字的右侧续写（后缀）",
            "not_covered": "符号与数字之间有空格的写法**不在覆盖范围**：`- 820 万人次`、`± 820 万人次` 仍会命中 `820 万人次`；"
                           "同理也不识别任意错误数值（如把 `820` 写成 `八百二十`）。"
                           "这两类不由机检保证，仍由人工项 facts_covered_by_human / no_added_content_by_human 把关。",
            "disclosed_tradeoff": "ASCII 连字符若紧邻在数字左侧（如把时间写成「时间-9 点到 12 点」）也会被当作数值符号而阻断。"
                                  f"这是为覆盖 `-820` 类紧邻符号写法所做的一致取舍；"
                                  f"正向 {counts['time_window']} 种时间写法与全部 {counts['all']} 条变体已逐条验证未误伤。",
            "scope": "只作用于声明 coverage_points 的分支；coverage_keywords 旧路径不判边界，保持旧行为。",
        },
        "allowed_writings_rule": "①数字与量词之间的空格可有可无；②时间区间用「到」「至」「-」任一连接符连接时"
                                 "写作 `N1点{C}N2点`（「数字与量词之间」「连接符与后一个数字之间」的空格可有可无，"
                                 "三种连接符 × 三个空格位 = 24 条）；③短写 `N1-N2点`（省略首个「点」，2 条）同时保留。"
                                 "两种骨架合计即时间闭包的全部写法。"
                                 "变体表由本规则**机械生成闭包**（tools/build_scoring_candidate_v3.py 的 "
                                 "allowed_writings / time_writings），不手写追加、不用当前批次回答反向拼凑；"
                                 "生成条数由生成函数返回，说明文字不手写数字（35 号 §3）。",
        "allowed_writings_boundary": "闭包只覆盖上面声明的空格位。未声明为自由的排版——例如「9 点 到 12 点」"
                                     "（「点」与连接符之间有空格）——不在变体表内，出现时按漏事实记；"
                                     "如要覆盖，需先把该空格位写进声明并重新生成闭包。"
                                     "另：题面示例 `18-20 点` 只是格式示例，取值与任何冻结事实无关。",
        "threshold_unchanged": "coverage_threshold 仍为 0.6；4 点题缺 1 点仍算覆盖达标、缺 2 点不达标——"
                               "这是冻结阈值的既有口径，本候选未改阈值，也未新增事实点",
        "compatibility": "未声明 coverage_points 的题集/判据仍走 coverage_keywords 字面子串旧路径",
        "consistency": "test_v4 中每个点的 coverage_keywords 必须等于该点 variants[0]（单一口径，保留旧字段仅供旧工具读取）；"
                       "variants 必须等于声明规则的闭包（构建自检逐点核对）",
    }
    out["string_compare"] = {
        "rule_version": "extract_string_precision_v1",
        "strip_outer_whitespace": True,
        "inner_whitespace": "strict",
        "rule": "字符串字段去掉首尾空白后精确比较；内部空格与标点保持严格（题面已声明「与材料原文逐字相同」）；"
                "不做全半角、繁简、近义归一",
        "why": "31 号 §5：抽取题的字段精度与空白规则必须在**模型实际收到的题面**里明确。"
               "本候选选择「写清规则 + 保持严格比较」，而不是在评分器里无差别删除字符串空白",
        "consequence": "high T01 的「3号库房」在新契约下仍记未过（题面已要求照抄原文含空格）；"
                       "该题在本轮按诊断证据保留，不回填旧结论",
    }
    out["refusal_rule"] = {
        "mode": "fixed_phrase",
        "phrase": out["refusal_phrase"],
        "purpose": "机检本项评测的是**输出契约遵循**：系统提示（runner.build_system_prompt）已明确要求"
                   "「若资料中确实没有依据，请明确说明“资料中未找到相关依据”」。语义正确但没有使用该固定短语的拒答"
                   "按未过记，不折算成通过。",
        "not_semantic_judgment": True,
        "why_not_semantic_mode": "「正确拒答语义」需要判断模型是否真的拒绝编造，机检在不引入 AI 裁判的前提下无法判定；"
                                 "31 号 §5 要求在两种口径中明确选一个，本候选选「固定短语遵循」并写明评测目的。",
        "variants_policy": "不使用由当前批次回答反向拼凑的短语变体（避免把被测答案写进判据）；"
                           "若将来要放宽，只能按新版本预先列举并重新冻结",
        "semantic_observation": "语义正确拒答（本轮 high T19）作为复审/人工的另列观察保留，不覆盖机检结论",
    }
    out["qa_rule"] = {
        "speculation_and_advice": "allowed_with_explicit_label",
        "allowed": "以「建议 / 推测 / 可能 / 需进一步核实 / 材料未涉及」等**显式标注**给出的材料外建议或推断",
        "not_allowed": "把材料外内容表述成材料事实；把材料的限定说法扩大（例：材料写「无异常批次」，"
                       "回答写成「无异常因素」）；与材料事实冲突的数字、结论或单位",
        "grading": "机检不变（非空 + 期望拒答时须含冻结短语）；以上边界属人工项 no_unsupported_claim 的判定规则",
        "pending_user_confirmation": True,
        "confirmed_by_user": False,
        "boundary_cases_reference": "31 号 §3 的 5 条边界项（high/route T15、high/route T17、high T19）由用户逐条确认",
    }
    out["overall_pass_rule"] = dict(out["overall_pass_rule"])
    out["overall_pass_rule"]["summary"] = (
        "mechanized_pass（长度不超上限 AND 事实点覆盖比例 >= coverage_threshold "
        "（按 coverage_points 的字面变体组判定）AND 无禁止词）"
        "AND 人工两项 facts_covered_by_human / no_added_content_by_human"
    )
    out["human_items"] = dict(out["human_items"])
    out["human_items"]["facts_covered_by_human"] = (
        "摘要对题集预冻结 summary_facts 的逐事实点语义覆盖：人工逐点记 true/false，全部必须事实点正确覆盖才通过；"
        "合法同义改写可以满足（机检已用预冻结变体覆盖常见表示差异），关键词出现不能替代人工判断"
    )
    out["human_items"]["no_unsupported_claim"] = (
        "是否把材料不支持的内容当作材料事实陈述。按 qa_rule：显式标注的建议/推测不算失败；"
        "扩大材料限定说法（如「无异常批次」→「无异常因素」）算失败"
    )
    out["notes"] = [
        "机检与人工结果分别保留，人工不直接覆盖机检失败（11 号 §3）。",
        "人工完成前 overall_pass 一律为 false，不得用机检结果代替人工结论。",
        "题目的判据参数（answer_key / coverage_points / max_chars / expect_refusal / summary_facts）"
        "随题集 test_questions_v4.json 一起冻结；真实运行后不得临时放宽。",
        "关键词/事实点覆盖沿用 coverage_threshold=0.6（继承 criteria_m2_v1）。",
        "v2 抽取契约原则见 extract_contract_rules；v3 新增 string_compare / refusal_rule / qa_rule / coverage_match_rule。",
        "本判据为候选：采用与再跑真实对照须由用户单独决定并重新批准。",
    ]
    out["extract_contract_rules"] = list(v2.get("extract_contract_rules") or []) + [
        "v3：字符串字段的精度与空白规则必须写进模型实际收到的题面（test_v4 已在 10 道抽取题统一声明"
        "「与材料原文逐字相同（包括原文中的空格与标点）」）；评分器仍保持严格比较，不做空白归一。",
    ]
    return out


def main():
    parser = argparse.ArgumentParser(description="构建 M2 评分版本候选（criteria_m2_v3 + test_v4，零调用）")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--criteria-out", default=DEFAULT_CRITERIA_OUT)
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    source_path, out_path = os.path.abspath(args.source), os.path.abspath(args.out)
    criteria_out = os.path.abspath(args.criteria_out)
    for path in (out_path, criteria_out):
        if path in PROTECTED or path == source_path:
            print(f"[拒绝] 目标 {path} 属受保护旧件或等于源文件：旧版本字节必须保留", file=sys.stderr)
            return 1

    protected_before = {p: sha256_file(p) for p in PROTECTED if os.path.exists(p)}
    source_data = load_json(source_path)
    v3_by_id = {q["test_id"]: q for q in source_data["questions"]}
    v3_slots = slots_of(source_data["questions"])

    # ---------- 构建 v4 ----------
    data = copy.deepcopy(source_data)
    data["version"] = "test_v4"
    data["note"] = (
        "M2 测试集 v4（候选，2026-09-29 首版 / 2026-10-03 修订 2；依据 31 号 §5 与 33 号 R1/R2）：30 题仍为合成材料。"
        "v4 做四件事：①10 道抽取题的题面统一补上字段精度与空白契约（与材料原文逐字相同，含空格与标点）；"
        "②10 道摘要题的题面统一补上事实覆盖口径，并把 criteria.coverage_keywords 升级为 coverage_points"
        "（每个事实点一组预先冻结的字面变体，coverage_keywords 保留为各点主变体以兼容旧工具）；"
        "③T23 增加 4 条「非自愿捐赠」禁止词（材料原文为「三成自愿捐入」）；"
        "④**修订 2/3**：摘要题的变体表改为按题面声明的两条表示差异规则（数字与量词之间的空格可有可无；"
        "时间区间用「到」「-」「至」连接，三种连接符同走完整骨架并保留短写）**机械生成闭包**，"
        f"T23 时间区间 {writings_counts()['time_window']} 条写法全部收录、全部事实点 {writings_counts()['all']} 条；"
        "同时把题面示例值换成与任何冻结事实数值都不重合的时间（18-20 点），避免把真实答案写进题面。"
        "其余 20 题的题面、材料、answer_key、max_chars、expect_refusal、summary_facts、"
        "forbidden_keywords 逐字节继承 v3。判据见 criteria_m2_v3.json；路由规则仍为 router_rules_v2，"
        "notes 不进入路由输入。"
    )
    v4_by_id = {q["test_id"]: q for q in data["questions"]}
    for tid in EXTRACT_IDS:
        v4_by_id[tid]["question"] = v4_by_id[tid]["question"] + EXTRACT_CLAUSE
    for tid in SUMMARY_IDS:
        q = v4_by_id[tid]
        q["question"] = q["question"] + SUMMARY_CLAUSE
        points = copy.deepcopy(COVERAGE_POINTS[tid])
        for point in points:
            point["variants"] = expand_variants(point)
        q["criteria"]["coverage_points"] = points
        q["criteria"]["coverage_keywords"] = [p["variants"][0] for p in points]
        if tid in FORBIDDEN_ADDITIONS:
            q["criteria"]["forbidden_keywords"] = list(q["criteria"]["forbidden_keywords"]) + FORBIDDEN_ADDITIONS[tid]

    criteria = build_criteria_v3()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(criteria_out, "w", encoding="utf-8") as fh:
        json.dump(criteria, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[build] {os.path.basename(source_path)} → {os.path.basename(out_path)}（version=test_v4）")
    print(f"[build] 判据 {os.path.basename(criteria_out)}（继承 criteria_m2_v2 阈值与拒答短语）")

    questions = data["questions"]

    # ---------- 1 结构完整性 ----------
    check("题数 30、题号 T01–T30 唯一、三类各 10",
          len(questions) == 30
          and sorted(q["test_id"] for q in questions) == [f"T{i:02d}" for i in range(1, 31)]
          and all(sum(1 for q in questions if q["task_type_gold"] == c) == 10
                  for c in ("extract", "qa_grounded", "summary")))

    # ---------- 2 变更范围：逐字段 diff，只允许声明的路径 ----------
    index_of = {q["test_id"]: i for i, q in enumerate(questions)}
    allowed_paths = {f"{index_of[t]}.question" for t in EXTRACT_IDS + SUMMARY_IDS}
    allowed_paths |= {f"{index_of[t]}.criteria.coverage_points" for t in SUMMARY_IDS}
    allowed_paths |= {f"{index_of[t]}.criteria.coverage_keywords" for t in SUMMARY_IDS}
    allowed_paths.add(f"{index_of['T23']}.criteria.forbidden_keywords")
    v3_by_index = {i: source_data["questions"][i] for i in range(len(source_data["questions"]))}
    changed_paths = sorted(set(diff_paths(v3_by_index, {i: questions[i] for i in range(len(questions))})) - allowed_paths)
    check("除声明变更外无其他字段改动（仅题面追加句 / 摘要点 / T23 禁止词）",
          not changed_paths, f"越界变化：{changed_paths}")
    question_text_ok = True
    for tid in EXTRACT_IDS:
        question_text_ok &= v4_by_id[tid]["question"] == v3_by_id[tid]["question"] + EXTRACT_CLAUSE
    for tid in SUMMARY_IDS:
        question_text_ok &= v4_by_id[tid]["question"] == v3_by_id[tid]["question"] + SUMMARY_CLAUSE
    check("题面变更恰为「原文 + 统一契约句」，未夹带其他文字", question_text_ok)
    check("10 道抽取题题面含精度/空白契约句",
          all(EXTRACT_CLAUSE in v4_by_id[t]["question"] for t in EXTRACT_IDS))
    check("10 道摘要题题面含事实覆盖口径句",
          all(SUMMARY_CLAUSE in v4_by_id[t]["question"] for t in SUMMARY_IDS))

    # ---------- 3 摘要点结构与一致性 ----------
    struct_problems, kw_problems = [], []
    for tid in SUMMARY_IDS:
        crit = v4_by_id[tid]["criteria"]
        points = crit.get("coverage_points") or []
        if not (3 <= len(points) <= 5):
            struct_problems.append(f"{tid} 事实点数 {len(points)} 不在 3–5")
        if len({p["id"] for p in points}) != len(points):
            struct_problems.append(f"{tid} 点 id 重复")
        for p in points:
            if not isinstance(p.get("variants"), list) or not p["variants"]:
                struct_problems.append(f"{tid}.{p.get('id')} 变体为空")
            if any(not isinstance(v, str) or not v for v in p["variants"]):
                struct_problems.append(f"{tid}.{p.get('id')} 变体非字符串")
        if crit.get("coverage_keywords") != [p["variants"][0] for p in points]:
            kw_problems.append(tid)
        if crit.get("max_chars") != v3_by_id[tid]["criteria"]["max_chars"]:
            struct_problems.append(f"{tid} max_chars 被改动")
    check("摘要题 coverage_points 结构合法（3–5 点、id 唯一、变体非空字符串）", not struct_problems,
          "；".join(struct_problems))
    check("摘要题 coverage_keywords 恰为各点主变体（单一口径，旧字段兼容）", not kw_problems, "；".join(kw_problems))
    closure_problems, variant_notes = [], []
    for tid in SUMMARY_IDS:
        for point in v4_by_id[tid]["criteria"]["coverage_points"]:
            expected = expand_variants(point)
            if point["variants"] != expected:
                closure_problems.append(f"{tid}.{point['id']}")
            if len(point["variants"]) > 1:
                variant_notes.append(f"{tid}.{point['id']}={len(point['variants'])}")
    check("每个事实点的变体表恰为题面声明规则的机械闭包（无手写追加、无遗漏）",
          not closure_problems, "；".join(closure_problems))
    grown_points = [f"{tid}.{point['id']}"
                    for tid in SUMMARY_IDS for point in v4_by_id[tid]["criteria"]["coverage_points"]
                    if not point.get("time_range")
                    and not any(ch.isdigit() for ch in point["variants"][0])
                    and point["variants"] != next(p["variants"] for p in COVERAGE_POINTS[tid]
                                                  if p["id"] == point["id"])]
    check("不含数字的事实点（「线上系统」「每户限领」「三成」等）只保留人写声明的变体、未被自动追加写法",
          not grown_points, "；".join(grown_points))
    t23_time = v4_by_id["T23"]["criteria"]["coverage_points"][0]["variants"]
    check("T23 时间区间写法闭包与声明规则逐字一致（含 33 号两条与 35 号四条漏计写法）",
          t23_time == time_writings("9", "12") and len(t23_time) == 26
          and {"9-12 点", "9 点至 12 点"} <= set(t23_time)
          and {"9点-12点", "9 点-12 点", "9 点-12点", "9点-12 点"} <= set(t23_time),
          t23_time)
    tradeoff_text = criteria["coverage_match_rule"]["numeric_boundary"]["disclosed_tradeoff"]
    stale_counts = [token for token in ("10 条写法", "10 种时间写法", "18 条写法", "71 条变体")
                    if token in data["note"] or token in tradeoff_text or token in criteria["note"]]
    check("说明文字里的计数取自生成集合、无残留旧数字（35 号 §3）",
          f"时间区间 {len(t23_time)} 条写法全部收录" in data["note"]
          and f"{len(t23_time)} 种时间写法" in tradeoff_text
          and f"时间闭包 {len(t23_time)} 条" in criteria["note"]
          and not stale_counts,
          {"stale": stale_counts})

    # 题面示例不得等于任何冻结事实点的数值（33 号 R2：避免把 T23 的真实答案写进题面）
    fact_numbers, clause_numbers = set(), set(re.findall(r"\d+", SUMMARY_CLAUSE))
    for tid in SUMMARY_IDS:
        for fact in v4_by_id[tid].get("summary_facts") or []:
            fact_numbers |= set(re.findall(r"\d+", fact))
    check("题面统一契约句不含任何冻结事实点的数值（示例与真实答案解耦）",
          not (clause_numbers & fact_numbers), sorted(clause_numbers & fact_numbers))
    check("T23 禁止词新增 4 条且原两条保留",
          v4_by_id["T23"]["criteria"]["forbidden_keywords"]
          == v3_by_id["T23"]["criteria"]["forbidden_keywords"] + FORBIDDEN_ADDITIONS["T23"],
          v4_by_id["T23"]["criteria"]["forbidden_keywords"])
    check("其余摘要题禁止词逐字继承 v3",
          all(v4_by_id[t]["criteria"]["forbidden_keywords"] == v3_by_id[t]["criteria"]["forbidden_keywords"]
              for t in SUMMARY_IDS if t != "T23"))

    # ---------- 4 抽取题契约 ----------
    key_problems, type_problems, decl_problems = [], [], []
    for tid in EXTRACT_IDS:
        q = v4_by_id[tid]
        c = q.get("constraints") or {}
        required = set(c.get("required_fields") or [])
        key = q.get("answer_key") or {}
        if set(key) != required:
            key_problems.append(f"{tid} 答案键字段与 required_fields 不一致")
        for field, expected_type in (c.get("field_types") or {}).items():
            value = key.get(field)
            ok = ((expected_type == "number" and type(value) in (int, float) and type(value) is not bool)
                  or (expected_type == "string" and isinstance(value, str))
                  or (expected_type == "boolean" and type(value) is bool))
            if not ok:
                type_problems.append(f"{tid}.{field} 声明 {expected_type} 与答案键类型不符")
            if expected_type == "number" and "数字类型" not in q["question"]:
                decl_problems.append(f"{tid}.{field} 声明 number 但题面未写「数字类型」")
    check("10 道抽取题答案键与 required_fields 一致", not key_problems, "；".join(key_problems))
    check("10 道抽取题声明类型与答案键取值类型一致", not type_problems, "；".join(type_problems))
    check("声明数字类型的字段题面确实写明「数字类型」", not decl_problems, "；".join(decl_problems))
    check("抽取题的答案键、字段类型、材料逐字节继承 v3",
          all(json.dumps(v4_by_id[t]["answer_key"], ensure_ascii=False, sort_keys=True)
              == json.dumps(v3_by_id[t]["answer_key"], ensure_ascii=False, sort_keys=True)
              and v4_by_id[t]["materials"] == v3_by_id[t]["materials"]
              and v4_by_id[t]["constraints"] == v3_by_id[t]["constraints"] for t in EXTRACT_IDS))

    # ---------- 5 金答案自洽（v3 判据） ----------
    gold_fail = []
    for tid in EXTRACT_IDS:
        r = grade.grade("extract", json.dumps(v4_by_id[tid]["answer_key"], ensure_ascii=False),
                        v4_by_id[tid], criteria)
        if not r["mechanized_pass"]:
            gold_fail.append(f"{tid}: {r.get('reason')}")
    check("10 道抽取题金答案在 criteria_m2_v3 下全部通过", not gold_fail, "；".join(gold_fail))

    summary_fail = []
    for tid in SUMMARY_IDS:
        text = gold_summary_text(v4_by_id[tid])
        r = grade.grade("summary", text, v4_by_id[tid], criteria)
        if not r["mechanized_pass"]:
            summary_fail.append(f"{tid}: {r.get('reason')} / 缺 {r['detail'].get('coverage_missing')} / "
                                f"{len(text)} 字符")
    check("10 道摘要题金答案（冻结事实拼接）在 criteria_m2_v3 下全部通过", not summary_fail, "；".join(summary_fail))

    qa_fail = []
    for tid in (f"T{i:02d}" for i in range(11, 21)):
        q = v4_by_id[tid]
        text = "资料中未找到相关依据。" if q.get("expect_refusal") else "；".join(
            f.get("fact", "") for f in (q.get("key_facts") or []))
        if not grade.grade("qa_grounded", text, q, criteria)["mechanized_pass"]:
            qa_fail.append(tid)
    check("10 道问答题金答案（拒答用固定短语 / 非拒答用事实句）在 criteria_m2_v3 下全部通过",
          not qa_fail, "；".join(qa_fail))

    # ---------- 6 路由与任务类型 ----------
    v4_slots = slots_of(questions)
    drift = [t for t in V3_EXPECTED_SLOTS if v3_slots[t] != V3_EXPECTED_SLOTS[t]]
    slot_changes = [{"question": t, "v3": v3_slots[t], "v4": v4_slots[t]}
                    for t in sorted(v4_slots) if v4_slots[t] != v3_slots[t]]
    pred_bad = []
    for q in questions:
        decision = router.decide(build_request_text(q), q.get("constraints"), None, instruction_text=q["question"])
        if decision["task_type_pred"] != q["task_type_gold"]:
            pred_bad.append(f"{q['test_id']} pred={decision['task_type_pred']} gold={q['task_type_gold']}")
    check("v3 现场槽位与冻结表一致（无历史漂移）", not drift, "；".join(drift))
    check("v4 全部 30 题槽位与 v3 逐题相同（题面追加句未改变路由）", not slot_changes, slot_changes)
    check("v4 任务类型识别与 gold 一致", not pred_bad, "；".join(pred_bad))
    check("T29/T30 长材料仍 > 6000 字符（R1）",
          len(build_request_text(v4_by_id["T29"])) > 6000 and len(build_request_text(v4_by_id["T30"])) > 6000,
          (len(build_request_text(v4_by_id["T29"])), len(build_request_text(v4_by_id["T30"]))))

    # ---------- 7 旧件保护 ----------
    protected_after = {p: sha256_file(p) for p in PROTECTED if os.path.exists(p)}
    check("旧版本文件（v1/v2/v3 题集与 v1/v2/criteria_v1 判据）字节未变",
          protected_before == protected_after,
          [p for p in protected_before if protected_before[p] != protected_after.get(p)])

    # ---------- 报告 ----------
    out_sha, criteria_sha = sha256_file(out_path), sha256_file(criteria_out)
    passed = sum(1 for c in CHECKS if c["ok"])
    report = {
        "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "scoring_candidate_v3",
        "source": os.path.basename(source_path), "source_sha256": sha256_file(source_path),
        "out": os.path.basename(out_path), "out_sha256": out_sha, "out_bytes": os.path.getsize(out_path),
        "criteria_out": os.path.basename(criteria_out), "criteria_out_sha256": criteria_sha,
        "criteria_out_bytes": os.path.getsize(criteria_out),
        "protected_before": protected_before, "protected_after": protected_after,
        "extract_clause": EXTRACT_CLAUSE, "summary_clause": SUMMARY_CLAUSE,
        "coverage_points": COVERAGE_POINTS, "forbidden_additions": FORBIDDEN_ADDITIONS,
        "route_slots_v3": v3_slots, "route_slots_v4": v4_slots,
        "changed_question_text": EXTRACT_IDS + SUMMARY_IDS,
        "passed": passed, "total": len(CHECKS), "checks": CHECKS,
    }
    report_path = args.report or os.path.join(ROOT, "runs", "_scoring_candidate_v3_build_check.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n==== 评分版本候选构建与自检：{passed}/{len(CHECKS)} PASS ====")
    print(f"[info] test_v4 {out_sha[:16]}（{os.path.getsize(out_path)} B）；criteria_m2_v3 {criteria_sha[:16]}"
          f"（{os.path.getsize(criteria_out)} B）")
    print(f"[report] {report_path}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
