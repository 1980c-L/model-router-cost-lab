# -*- coding: utf-8 -*-
"""构建 M2 测试集 v3：抽取题输出契约校准（26 号 §3），零调用、确定性、可重跑。

原则（26 号 §3）：
- **正确标准答案自身必须通过**冻结判据；
- **受约束字段的规则必须写在模型实际收到的题面里**（类型/单位/粒度/原文字符串），
  不允许"隐藏答案键或隐含类型"；
- 不把隐藏答案键或示例答案附到题面；
- 只改新版本题集，不动 `test_questions_v2.json` / `criteria_m2_v1.json` 的字节，
  也不改 `grade.py` / `runner.py` / `router.py`。

v2→v3 修订（仅 7 题）：
  T07 approved_by：删除与答案键 null 冲突的 `field_types.string` 要求（25 号 P2 判据内部冲突）
  T01 area_sqm / T02 headcount / T03 stock_qty：题面声明"数字类型 + 单位含义"，
      constraints.field_types 与 answer_key 同步为数字（原先只有答案键里的 "240 平方米" 这类
      带单位字符串，题面与类型声明都没有说，属隐含契约）
  T04 host：题面声明"只填被访人姓名，不附加部门"；答案键取材料中的姓名（不把未要求的部门
      省略解释为事实错误）
  T09 event_name / venue / online_viewers：声明"照抄材料原文"的字段范围、观看量数字类型与
      单位换算规则、嘉宾数数字类型
  T10 root_cause：声明"只填材料给出的原因短语，不附加否定性补充判断"

用法（在 model-router-cost-lab 目录下）：
  py -3 tools\\build_test_questions_v3.py
  py -3 tools\\build_test_questions_v3.py --source eval\\test_questions_v2.json --out eval\\test_questions_v3.json --version test_v3
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import grade  # noqa: E402
import router  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SOURCE = os.path.join(ROOT, "eval", "test_questions_v2.json")
DEFAULT_OUT = os.path.join(ROOT, "eval", "test_questions_v3.json")
DEFAULT_CRITERIA = os.path.join(ROOT, "eval", "criteria_m2_v2.json")
V2_CRITERIA = os.path.join(ROOT, "eval", "criteria_m2_v1.json")

# v2 的期望槽位（15 low / 15 high；用于核对未改动题的路由不被题面校准带偏）
V2_EXPECTED_SLOTS = {
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

# ---------------------------------------------------------------- v2→v3 契约修订（仅这 7 题）
CONTRACT_FIXES = {
    "T01": {
        "question": "请从材料中抽取【3 号库房】这一条台账记录，输出 JSON，字段：warehouse_id、keeper、area_sqm、last_audit。"
                    "其中 area_sqm 用数字类型，单位为平方米（只填数字，不要带单位文字）。",
        "constraints": {
            "require_json": True,
            "required_fields": ["warehouse_id", "keeper", "area_sqm", "last_audit"],
            "field_types": {"area_sqm": "number"},
            "material_count": 1,
        },
        "answer_key": {"warehouse_id": "3 号库房", "keeper": "周敏", "area_sqm": 240, "last_audit": "2026-08-30"},
        "notes": "单对象四字段，预期默认档 low；材料含 4 号库房（郑强/180 平方米）干扰项。"
                 "v3 契约校准：area_sqm 明确为数字类型、单位平方米，题面/field_types/answer_key 三处一致"
                 "（v2 只在答案键写 \"240 平方米\"，题面与类型声明均未说，属隐含契约）。",
    },
    "T02": {
        "question": "请从材料中抽取【新员工入职培训】这一项的登记信息，输出 JSON，字段：course_name、trainer、room、headcount。"
                    "其中 headcount 用数字类型，单位为人（只填数字，不要带单位文字）。",
        "constraints": {
            "require_json": True,
            "required_fields": ["course_name", "trainer", "room", "headcount"],
            "field_types": {"headcount": "number"},
            "material_count": 1,
        },
        "answer_key": {"course_name": "新员工入职培训", "trainer": "刘倩", "room": "B-201", "headcount": 32},
        "notes": "单对象四字段，预期默认档 low；材料含消防演练培训干扰项。"
                 "v3 契约校准：headcount 明确为数字类型、单位人（v2 答案键为 \"32 人\" 但无类型声明）。",
    },
    "T03": {
        "question": "请从材料中抽取【物料卡 M-108】的信息，输出 JSON，字段：item_code、item_name、unit、stock_qty。"
                    "其中 stock_qty 用数字类型（只填数字）；unit 填材料中的计量单位文字。",
        "constraints": {
            "require_json": True,
            "required_fields": ["item_code", "item_name", "unit", "stock_qty"],
            "field_types": {"stock_qty": "number"},
            "material_count": 1,
        },
        "answer_key": {"item_code": "M-108", "item_name": "耐高温手套", "unit": "双", "stock_qty": 156},
        "notes": "单对象四字段，预期默认档 low；材料含 M-109 干扰项。"
                 "v3 契约校准：stock_qty 明确为数字类型（v2 答案键为字符串 \"156\" 且无类型声明）；"
                 "unit 明确保留材料中的计量单位文字。",
    },
    "T04": {
        "question": "请从材料中抽取访客【陈晓】的来访登记，输出 JSON，字段：visitor_name、host、visit_date、badge_no。"
                    "其中 host 只填被访人姓名，不要附加部门或其他括号说明。",
        "constraints": {
            "require_json": True,
            "required_fields": ["visitor_name", "host", "visit_date", "badge_no"],
            "material_count": 1,
        },
        "answer_key": {"visitor_name": "陈晓", "host": "高翔", "visit_date": "2026-09-15", "badge_no": "V-0623"},
        "notes": "单对象四字段，预期默认档 low；材料含林芳/苏磊干扰项。"
                 "v3 契约校准：host 定义为\"被访人姓名（不含部门）\"并在题面声明，答案键取 高翔；"
                 "不把省略未被要求的部门当作事实错误（26 号 §3）。",
    },
    "T07": {
        "question": "请从材料中抽取【实验 EXP-12】的登记卡，输出 JSON，字段：exp_id、subject、sample_size、duration_days、result、approved_by。"
                    "其中 sample_size、duration_days 用数字类型；材料中未给出的字段，在 JSON 中用 null 表示，不要省略字段。",
        "constraints": {
            "require_json": True,
            "required_fields": ["exp_id", "subject", "sample_size", "duration_days", "result", "approved_by"],
            "field_types": {"sample_size": "number", "duration_days": "number"},
            "material_count": 1,
        },
        "answer_key": {"exp_id": "EXP-12", "subject": "催化剂载体耐久性", "sample_size": 40,
                       "duration_days": 21, "result": "初步通过稳定性测试", "approved_by": None},
        "notes": "六字段，预期档 high。v3 契约校准（25 号 P2）：删除 approved_by 的 string 类型要求——"
                 "原 v2 同时要求\"必填\"、类型 string、答案键 null，三者互斥，正确标准答案必被拒；"
                 "现保留必填与答案键 null，缺字段/空串/字符串 \"null\"/虚构姓名仍被拒。"
                 "同时按 26 号 §3\"规则写在题面\"补上 sample_size/duration_days 的数字类型声明"
                 "（v2 声明了类型但题面没说）。",
    },
    "T09": {
        "question": "请从三段材料中抽取【发布会】的关键信息，输出 JSON，字段：event_name、date、venue、online_viewers、speaker_count。"
                    "其中 event_name 与 venue 照抄材料中出现的原文（不要删减、改写或补充）；date 照抄材料日期；"
                    "online_viewers 用数字类型，单位为人次；材料以\"万人次\"记数时，将该数值乘以 10000，输出整数人次；"
                    "speaker_count 用数字类型。",
        "constraints": {
            "require_json": True,
            "required_fields": ["event_name", "date", "venue", "online_viewers", "speaker_count"],
            "field_types": {"online_viewers": "number", "speaker_count": "number"},
            "material_count": 3,
        },
        "answer_key": {"event_name": "公司秋季发布会", "date": "2026-10-20",
                       "venue": "滨江国际会展中心 3 号厅", "online_viewers": 23000, "speaker_count": 9},
        "notes": "三段材料，预期档 high（三个材料单元）。v3 契约校准：event_name/venue 明确\"照抄原文\""
                 "（材料原文即\"公司秋季发布会\"，v2 答案键写\"秋季发布会\"与原文不一致）；"
                 "online_viewers 明确数字类型与万人次换算规则（v2 为字符串 \"2.3 万人次\" 且无类型声明）；"
                 "speaker_count 声明数字类型。",
    },
    "T10": {
        "question": "请从三段材料中抽取告警 ALM-77 的处理信息，输出 JSON，字段：alarm_id、service、root_cause、owner、resolved。"
                    "其中 root_cause 只填材料给出的原因短语本身，不要附加\"非代码缺陷\"之类的补充判断；resolved 用 boolean 类型。",
        "constraints": {
            "require_json": True,
            "required_fields": ["alarm_id", "service", "root_cause", "owner", "resolved"],
            "field_types": {"resolved": "boolean"},
            "material_count": 3,
        },
        "answer_key": {"alarm_id": "ALM-77", "service": "订单网关",
                       "root_cause": "连接池耗尽导致超时", "owner": "韩磊", "resolved": True},
        "notes": "三段材料，预期档 high。v3 契约校准：root_cause 明确\"只填原因短语\"，把"
                 "材料二中的附加否定说明（非代码缺陷）排除在字段之外，规则写在题面而非隐藏。",
    },
}

QA_SUMMARY_KEYS = ("key_facts", "expect_refusal", "criteria", "summary_facts")

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


def build_request_text(question):
    parts = [question.get("question", "")]
    materials = question.get("materials") or []
    if materials:
        parts.append("【资料】")
        for index, material in enumerate(materials, 1):
            parts.append(f"[材料{index}]\n{material}")
    return "\n\n".join(parts)


def build_criteria_v2():
    """criteria_m2_v2：规则继承 m2_v1（阈值/拒答短语/人工项逐字继承），补抽取契约原则与版本指向。"""
    with open(V2_CRITERIA, "r", encoding="utf-8") as fh:
        v1 = json.load(fh)
    out = copy.deepcopy(v1)
    out["version"] = "criteria_m2_v2"
    out["frozen_at"] = "2026-09-28"
    out["inherits"] = "criteria_m2_v1"
    out["questions_version"] = "test_v3"
    out["note"] = (
        "M2 判据 v2：机检规则（coverage_threshold、refusal_phrase、summary_human_items、"
        "extract/qa/summary 的整体达标合成）逐字继承 criteria_m2_v1，**不改任何阈值或人工项**；"
        "本次只补记抽取题的输出契约原则，并把版本指向 test_questions_v3.json。"
        "criteria_m2_v1.json 与 test_questions_v2.json 的字节保持不变，供旧批次诊断复核。"
    )
    out["extract_contract_rules"] = [
        "受类型或单位约束的字段，必须在**模型实际收到的题面**里声明，并与 constraints.field_types、"
        "answer_key 三处一致；不得靠答案键或评分器隐含类型/单位要求。",
        "评分器保持严格比较（JSON 可解析、必填字段、声明类型、字段值精确一致，数字按 1e-9 容差、"
        "布尔与数字分开）：不做语义归一，等价改写不自动通过；要放宽只能改题面契约并升版本重跑。",
        "正确标准答案自身必须通过冻结判据（金答案自洽）。",
        "本轮（首轮真实批次）的 41 次调用与旧判据结果保留为诊断证据，不因 v3 校准而改写。",
    ]
    out["notes"] = [
        "机检与人工结果分别保留，人工不直接覆盖机检失败（11 号 §3）。",
        "人工完成前 overall_pass 一律为 false，不得用机检结果代替人工结论。",
        "题目的判据参数（answer_key / coverage_keywords / max_chars / expect_refusal / summary_facts）"
        "随题集 test_questions_v3.json 一起冻结；真实运行后不得临时放宽。",
        "关键词覆盖沿用 coverage_threshold=0.6，逐题固定关键词（继承 criteria_m2_v1）。",
        "v2 判据的抽取契约原则见 extract_contract_rules；旧判据 criteria_m2_v1 的历史行为不变。",
    ]
    return out


def main():
    parser = argparse.ArgumentParser(description="构建 M2 题集 v3（抽取题契约校准，零调用）")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--version", default="test_v3")
    parser.add_argument("--criteria-out", default=DEFAULT_CRITERIA)
    args = parser.parse_args()

    source_path = os.path.abspath(args.source)
    out_path = os.path.abspath(args.out)
    if out_path == source_path:
        print("[拒绝] --out 不能等于 --source：旧题集字节必须保留", file=sys.stderr)
        return 1

    with open(source_path, "r", encoding="utf-8") as fh:
        source_data = json.load(fh)
    v2_questions = copy.deepcopy(source_data["questions"])
    by_id = {q["test_id"]: q for q in v2_questions}

    # ---------- 构建 v3 ----------
    data = copy.deepcopy(source_data)
    data["version"] = args.version
    data["note"] = (
        "M2 测试集 v3（2026-09-28）：30 题、三类各 10，材料仍为合成材料，与 dev 集分离。"
        "v3 只做**抽取题输出契约校准**（T01/T02/T03/T04/T07/T09/T10 的题面、field_types、answer_key、notes），"
        "其余 23 题的题面/材料/判据参数逐字节继承 v2；校准依据 25 号独立复审 P2（T07 判据内部冲突）"
        "与 26 号工作单（类型/单位/粒度/原文字符串必须写在题面里）。判据见 criteria_m2_v2.json；"
        "路由规则仍为 router_rules_v2。notes 不进入路由输入。"
    )
    v3_by_id = {q["test_id"]: q for q in data["questions"]}
    for tid, patch in CONTRACT_FIXES.items():
        for key in ("question", "constraints", "answer_key", "notes"):
            v3_by_id[tid][key] = copy.deepcopy(patch[key])

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    criteria = build_criteria_v2()
    with open(os.path.abspath(args.criteria_out), "w", encoding="utf-8") as fh:
        json.dump(criteria, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[build] 由 {os.path.basename(source_path)} 构建 {os.path.basename(out_path)}（version={args.version}）")
    print(f"[build] 判据 {os.path.basename(args.criteria_out)}（继承 criteria_m2_v1 阈值）")

    questions = data["questions"]

    # ---------- 1 题集完整性 ----------
    check("题集版本与来源标记", data.get("version") == args.version and data.get("source") == "synthetic")
    check("题数为 30、题号 T01–T30 唯一", len(questions) == 30
          and sorted(q["test_id"] for q in questions) == [f"T{i:02d}" for i in range(1, 31)])
    golds = {}
    for q in questions:
        golds[q["task_type_gold"]] = golds.get(q["task_type_gold"], 0) + 1
    check("三类各 10 题", golds == {"extract": 10, "qa_grounded": 10, "summary": 10}, str(golds))

    # 只有声明的 7 题变化，其余逐字节继承（比较除 version/note 外的全部内容）
    changed = []
    for q in questions:
        tid = q["test_id"]
        if json.dumps(q, ensure_ascii=False, sort_keys=True) != json.dumps(by_id[tid], ensure_ascii=False, sort_keys=True):
            changed.append(tid)
    check("只有声明的 7 题发生变化，其余 23 题逐字节继承 v2",
          changed == sorted(CONTRACT_FIXES), f"实际变化：{changed}")

    # 题目级最低要求仍齐备（沿用 v2 构建脚本的清单）
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
    check("逐题最低要求齐备（v2 清单不变）", not problems, "；".join(problems))

    # ---------- 2 抽取题契约一致性 ----------
    extract = [q for q in questions if q["task_type_gold"] == "extract"]
    key_problems, type_problems, decl_problems = [], [], []
    decl_table = []
    for q in extract:
        tid = q["test_id"]
        c = q.get("constraints") or {}
        required = set(c.get("required_fields") or [])
        key = q.get("answer_key") or {}
        if set(key) != required:
            key_problems.append(f"{tid} answer_key 字段与 required_fields 不一致：{sorted(set(key) ^ required)}")
        for field, expected_type in (c.get("field_types") or {}).items():
            if field not in key:
                type_problems.append(f"{tid}.{field} 声明了类型但不在答案键里")
                continue
            value = key[field]
            ok = (
                (expected_type == "number" and type(value) in (int, float) and type(value) is not bool)
                or (expected_type == "string" and isinstance(value, str))
                or (expected_type == "boolean" and type(value) is bool)
            )
            if not ok:
                type_problems.append(f"{tid}.{field} 声明 {expected_type}，答案键类型是 {type(value).__name__}")
            # 声明了数字类型的字段，题面必须显式出现"数字类型"
            if expected_type == "number" and "数字类型" not in q["question"]:
                decl_problems.append(f"{tid}.{field} 声明 number，但题面未出现\"数字类型\"")
            decl_table.append({"question": tid, "field": field, "declared_type": expected_type,
                               "answer_key_type": type(value).__name__, "answer_key_value": value})
        if tid == "T07":
            if "null" not in q["question"]:
                decl_problems.append("T07 题面未声明缺失字段用 null")
            if "approved_by" in (c.get("field_types") or {}):
                decl_problems.append("T07 approved_by 仍被声明类型（与答案键 null 冲突）")
    check("抽取题 answer_key 与 required_fields 完全一致", not key_problems, "；".join(key_problems))
    check("声明的 field_types 与答案键取值类型一致", not type_problems, "；".join(type_problems))
    check("声明了数字类型的字段，题面确实写明\"数字类型\"", not decl_problems, "；".join(decl_problems))

    t09 = v3_by_id["T09"]
    check("T09 指令含通用换算规则，未直接给出当前观看量答案",
          "乘以 10000" in t09["question"]
          and str(t09["answer_key"]["online_viewers"]) not in t09["question"]
          and "2.3 万" not in t09["question"])

    # ---------- 3 金答案自洽（正确标准答案自身必须通过冻结判据） ----------
    gold_fail = []
    gold_detail = []
    for q in extract:
        content = json.dumps(q["answer_key"], ensure_ascii=False)
        r = grade.grade("extract", content, q, criteria)
        d = r["detail"]
        ok = (r["mechanized_pass"] is True and d["json_parsable"] and not d["missing_fields"]
              and not d["mismatched_fields"] and not d["type_mismatched"])
        if not ok:
            gold_fail.append(f"{q['test_id']}: {r.get('reason')}")
        gold_detail.append({"question": q["test_id"], "mechanized_pass": r["mechanized_pass"],
                            "format_pass": r["format_pass"], "content_pass": r["content_pass"],
                            "reason": r.get("reason")})
    check("10 道抽取题的金答案全部通过 v2 判据（format+content 均通过）", not gold_fail, "；".join(gold_fail))

    # ---------- 4 T07 聚焦 ----------
    t07 = v3_by_id["T07"]
    good = {"exp_id": "EXP-12", "subject": "催化剂载体耐久性", "sample_size": 40,
            "duration_days": 21, "result": "初步通过稳定性测试", "approved_by": None}
    cases = [
        ("正确 null 通过", good, True),
        ("缺 approved_by 字段被拒", {k: v for k, v in good.items() if k != "approved_by"}, False),
        ("空字符串被拒", dict(good, approved_by=""), False),
        ("字符串 \"null\" 被拒", dict(good, approved_by="null"), False),
        ("虚构审批人被拒", dict(good, approved_by="王强"), False),
    ]
    t07_bad = []
    for name, payload, expect in cases:
        r = grade.grade("extract", json.dumps(payload, ensure_ascii=False), t07, criteria)
        if r["mechanized_pass"] is not expect:
            t07_bad.append(f"{name}（期望 {expect}，实际 {r['mechanized_pass']}）")
    check("T07 聚焦：正确 null 通过，缺字段/空串/字符串 null/虚构审批人一律拒绝", not t07_bad, "；".join(t07_bad))

    # ---------- 5 代表性负例 ----------
    negatives = [
        ("T01", {"warehouse_id": "4 号库房", "keeper": "郑强", "area_sqm": 180, "last_audit": "2026-09-02"}, "串入干扰实体"),
        ("T01", dict((v3_by_id["T01"]["answer_key"]), area_sqm=241), "错数值"),
        ("T04", dict(v3_by_id["T04"]["answer_key"], host="高翔（设计部）"), "附加未被要求的部门"),
        ("T06", {k: v for k, v in v3_by_id["T06"]["answer_key"].items() if k != "channel"}, "漏必填字段"),
        ("T06", dict(v3_by_id["T06"]["answer_key"], paid=1), "boolean/number 混用"),
        ("T05", dict(v3_by_id["T05"]["answer_key"], cpu_percent="62.5"), "明确类型错误（数字给字符串）"),
        ("T09", dict(v3_by_id["T09"]["answer_key"], online_viewers="2.3 万人次"), "单位未按题面换算"),
        ("T10", dict(v3_by_id["T10"]["answer_key"], root_cause="连接池耗尽导致超时，非代码缺陷"), "附加未允许的补充说明"),
        ("T03", dict(v3_by_id["T03"]["answer_key"], stock_qty="156"), "数字字段给字符串"),
        ("T07", dict(good, sample_size="40"), "数字字段给字符串（T07）"),
    ]
    neg_bad = []
    for tid, payload, why in negatives:
        r = grade.grade("extract", json.dumps(payload, ensure_ascii=False), v3_by_id[tid], criteria)
        if r["mechanized_pass"] is not False:
            neg_bad.append(f"{tid} {why} 未被拒绝")
    check("代表性负例（干扰实体/错值/漏字段/类型错/布尔数字混用/未按声明换算）全部被拒", not neg_bad, "；".join(neg_bad))

    # ---------- 6 路由与任务类型 ----------
    v2_slots, v3_slots = {}, {}
    for tid, q in by_id.items():
        v2_slots[tid] = router.decide(build_request_text(q), q.get("constraints"), None,
                                      instruction_text=q.get("question"))["chosen_slot"]
    pred_problems, slot_problems = [], []
    for q in questions:
        tid = q["test_id"]
        decision = router.decide(build_request_text(q), q.get("constraints"), None, instruction_text=q.get("question"))
        v3_slots[tid] = decision["chosen_slot"]
        if decision["task_type_pred"] != q["task_type_gold"]:
            pred_problems.append(f"{tid}: gold={q['task_type_gold']} pred={decision['task_type_pred']}")
        if tid not in CONTRACT_FIXES and decision["chosen_slot"] != V2_EXPECTED_SLOTS[tid]:
            slot_problems.append(f"{tid}: v2 期望 {V2_EXPECTED_SLOTS[tid]}，v3 实际 {decision['chosen_slot']}")
    check("未改动的 23 题路由槽位与 v2 期望一致", not slot_problems, "；".join(slot_problems))
    check("30 题任务类型识别与 gold 一致", not pred_problems, "；".join(pred_problems))

    def dist(slots):
        return {"low": sum(1 for v in slots.values() if v == "low"),
                "high": sum(1 for v in slots.values() if v == "high")}

    route_changed = [{"question": tid, "v2": v2_slots[tid], "v3": v3_slots[tid]}
                     for tid in CONTRACT_FIXES if v2_slots[tid] != v3_slots[tid]]
    for tid in ("T29", "T30"):
        check(f"{tid} 长材料输入仍 > 6000（R1）", len(build_request_text(v3_by_id[tid])) > 6000)

    # ---------- 7 报告 ----------
    out_sha = sha256_file(out_path)
    criteria_out_path = os.path.abspath(args.criteria_out)
    passed = sum(1 for c in CHECKS if c["ok"])
    report = {
        "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": os.path.basename(source_path), "source_sha256": sha256_file(source_path),
        "out": os.path.basename(out_path), "out_sha256": out_sha,
        "criteria_out": os.path.basename(criteria_out_path), "criteria_out_sha256": sha256_file(criteria_out_path),
        "changed_questions": sorted(CONTRACT_FIXES),
        "declaration_table": decl_table,
        "gold_answers": gold_detail,
        "route_slots_v2": dist(v2_slots), "route_slots_v3": dist(v3_slots),
        "route_slot_changed_questions": route_changed,
        "negatives": [{"question": t, "why": w} for t, _p, w in negatives],
        "passed": passed, "total": len(CHECKS), "checks": CHECKS,
    }
    report_path = os.path.join(ROOT, "runs", "_test_questions_v3_build_check.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n==== v3 构建与契约自检：{passed}/{len(CHECKS)} PASS ====")
    print(f"[info] 路由分布 v2={dist(v2_slots)} v3={dist(v3_slots)}；槽位变化的改动题：{route_changed}")
    print(f"[report] {report_path}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
