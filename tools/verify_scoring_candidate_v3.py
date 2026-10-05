# -*- coding: utf-8 -*-
"""评分版本候选（criteria_m2_v3 + test_v4）离线验证：零网络、零账号 API、零真实模型调用。

覆盖（对齐 31 号 §5、33 号 R1/R2/R3 与 35 号 §2/§3 对候选的验证要求）：
  A 身份：候选文件、旧件字节、现场运行源码
  B 继承性：阈值 / 拒答短语 / 摘要人工项 / 三类合成里未被改动的部分是否逐字继承
  C 正向：摘要金答案与「等价改写」可过（含逐点命中明细）
  C2 表内一致（33 号 R2）：每个事实点的**每一种已冻结写法**都能命中
  C3 独立构造（35 号 §2）：**不复用生成函数**，按题面承诺手写 26 条时间写法，
     双向核对「表 == 承诺」并逐条断言命中；避免生成函数漏一整类写法而闭包自检仍全过
  D 负向：缺复合事实、超长、禁止词、非自愿捐赠仍不过；并披露「单点缺失仍过」是冻结阈值口径
  E 变体不得吃掉数值变形：含数字的点做数值变形后该点必须未命中
  E2 数字边界（33 号 R1）：**紧邻**前缀 / 千分位 / 小数 / 符号 / 后缀都不得把错误数值计为命中；
     并单列「符号与数字之间有空格」这类保证范围之外的写法（35 号 §3 要求明示，不写成无条件保证）
  F 抽取题：金答案过；错数字 / 错单位 / 漏字段 / 类型错 / 附加未允许说明不过；空白差异按题面契约仍不过
  G 拒答：固定短语过、语义拒答（真实 high T19 回答）不过、编造序列号不过
  H 旧路径行为等价：与冻结基线 grade.py（批次清单里的 ee436f2d…）在所有输入上逐条同结果
  I 现场重放：用现行判据重放 v3-02 批次 70 条回答，mechanized_pass 与 reconcile_v3.json 记录逐条一致
  J 现场影响：候选口径对 10 条摘要回答的诊断重放（不回填旧结论）
  K 分工边界：合成负例经**现行 review_m2 人工函数 + AND 合成**后必须最终失败

用法（在 model-router-cost-lab 目录下）：
  py -3 tools\\verify_scoring_candidate_v3.py --report <本轮证据目录>\\verify_report.json

参数默认值的两类区别（37 号 T1 后明确写出，避免「默认值悄悄指向旧轮」）：
- **必须与本轮现场一致**：`--build-report`。默认取 `--report` **同目录**下的 `build_report.json`，
  即「本轮证据目录里的构建报告」；同目录没有该文件时直接退出 2 并要求显式传入，不会回退到任何旧轮路径。
- **冻结的历史锚**（跨轮不变，可安全默认）：`--baseline-grade`（批次冻结实现 runs\\_iso_m1_r2\\grade.py）、
  `--reconcile`（30 号证据的 reconcile_v3.json）、`--rev1-grade`（32 号证据的修订 1 实现）、
  `--rev2-questions`（34 号证据的修订 2 题集字节）。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

# 验证要 import 交付目录里的 `grade.py.after`（无 .py 后缀）；禁止写字节码缓存，
# 避免在交付证据目录里留下 __pycache__。
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import grade  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LAB = ROOT
RUN = LAB / "fixtures" / "m2-v3-02"
ROUND30 = LAB / "fixtures"
DEFAULT_BASELINE = LAB / "fixtures" / "grade_frozen.py"
DEFAULT_RECONCILE = ROUND30 / "reconcile_v3.json"
MANIFEST = RUN / "batch_manifest.json"

SUMMARY_IDS = [f"T{i:02d}" for i in range(21, 31)]
EXTRACT_IDS = [f"T{i:02d}" for i in range(1, 11)]

# 含数字的事实点：数值变形（只改数值，不改写法）后该点必须不命中
NUMERIC_MUTATIONS = {
    "total_ridership": ("820 万人次", "860 万人次"),
    "station_count": ("210 个", "260 个"),
    "repair_hours": ("26 小时", "36 小时"),
    "max_two_hours": ("2 小时", "3 小时"),
    "suspend_15_days": ("15 天", "16 天"),
    "upper_limit_8": ("8 册", "5 册"),
    "loan_days_21": ("21 天", "30 天"),
    "booths_22": ("22 个摊位", "23 个摊位"),
    "raised_9136": ("9136 元", "9137 元"),
}

# 33 号 R1：在这些**紧邻**写法下，正确数值被"包"进一个错误数值里，必须一律不算命中
INJECTIONS = [
    ("紧邻前缀多一位数字", lambda v: "1" + v),
    ("紧邻千分位逗号（半角）", lambda v: "1," + v),
    ("紧邻千分位逗号（全角）", lambda v: "1，" + v),
    ("紧邻小数点", lambda v: "0." + v),
    ("紧邻负号", lambda v: "-" + v),
    ("紧邻全角正号", lambda v: "＋" + v),
    ("紧邻正负号", lambda v: "±" + v),
]

# 边界助手自身的单元检查（合成变体，验证后缀那一侧）
HELPER_CASES = [
    ("22", "共 22 个摊位", True, "正常出现"),
    ("22", "第 122 号", False, "后缀多一位数字"),
    ("22", "22.5", False, "后缀小数点"),
    ("8", "8册", True, "数字与量词之间无空格"),
    ("8", "18 册", False, "紧邻前缀多一位数字"),
    ("820 万人次", "总量，820 万人次", True, "全角逗号是标点、不是千分位"),
    ("820 万人次", "1，820 万人次", False, "全角逗号紧跟在数字后 = 千分位"),
    ("9-12点", "19-12点", False, "紧邻前缀多一位数字"),
    ("9 点到 12 点", "时间-9 点到 12 点", False, "紧邻符号（已披露的取舍）"),
]

# 35 号 §3：紧邻规则**覆盖不到**的写法，明示为边界而不是隐藏缺口（这些必须仍然命中）
DISCLOSED_BOUNDARY_CASES = [
    ("820 万人次", "- 820 万人次", "符号与数字之间有空格，不在紧邻规则范围内"),
    ("820 万人次", "± 820 万人次", "同上"),
    ("210 个", "1 210 个", "空格千分位写法，不在紧邻规则范围内"),
]

# 35 号 §2：按**题面承诺**手写的 T23 时间写法（26 条），故意不复用 build 脚本的 time_writings()，
# 以独立核对「题面承诺 ↔ 变体表 ↔ 正向测试」。骨架 ①`N1点{C}N2点`（C=到/至/-，三个空格位自由，24 条）
# 与骨架 ②短写 `N1-N2点`（2 条）。
EXPECTED_TIME_WRITINGS = [
    "9 点到 12 点", "9 点到 12点", "9 点到12 点", "9 点到12点",
    "9点到 12 点", "9点到 12点", "9点到12 点", "9点到12点",
    "9 点至 12 点", "9 点至 12点", "9 点至12 点", "9 点至12点",
    "9点至 12 点", "9点至 12点", "9点至12 点", "9点至12点",
    "9 点- 12 点", "9 点- 12点", "9 点-12 点", "9 点-12点",
    "9点- 12 点", "9点- 12点", "9点-12 点", "9点-12点",
    "9-12 点", "9-12点",
]

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_module(path, name):
    """按源码加载模块；路径后缀不必是 .py（交付副本可能是 grade.py.after）。"""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def answer_text(group, tid):
    path = RUN / "raw" / group / tid / "answers" / f"{tid}.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def points_of(question):
    return (question.get("criteria") or {}).get("coverage_points") or []


def gold_summary(question, which=0):
    """金答案：冻结 summary_facts 拼接，再补上未覆盖的点（按 which 选择变体）。"""
    text = "；".join(question.get("summary_facts") or [])
    for point in points_of(question):
        variants = point.get("variants") or []
        if not any(v in text for v in variants):
            text += "；另涉：" + variants[which]
    return text


def drop_points(question, text, point_ids):
    """把指定事实点的所有变体从文本里删掉（模拟该事实完全没写）。"""
    for point in points_of(question):
        if point["id"] in point_ids:
            for variant in point["variants"]:
                text = text.replace(variant, "")
    return text


def diff_paths(old, new, prefix=""):
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


def main():
    parser = argparse.ArgumentParser(description="评分版本候选离线验证（零调用）")
    parser.add_argument("--report", required=True)
    parser.add_argument("--baseline-grade", default=str(DEFAULT_BASELINE))
    parser.add_argument("--reconcile", default=str(DEFAULT_RECONCILE))
    parser.add_argument("--build-report", default=None,
                        help="构建报告路径；缺省取 --report 同目录下的 build_report.json（本轮候选报告）")
    parser.add_argument("--rev1-grade",
                        default=str(LAB / "fixtures" / "grade_rev1.py"))
    parser.add_argument("--rev2-questions",
                        default=str(LAB / "fixtures" / "test_questions_v4_rev2.json"))
    args = parser.parse_args()

    # 37 号 T1：构建报告必须与本轮现场候选一致 —— 默认取 --report 同目录的 build_report.json，
    # 不做任何跨轮回退；同目录没有就明确拒绝（退出 2），避免按说明运行却拿到旧报告报 34/35。
    build_report_path = (Path(args.build_report) if args.build_report
                         else Path(args.report).resolve().parent / "build_report.json")
    if not build_report_path.exists():
        print(f"[拒绝] 找不到构建报告：{build_report_path}", file=sys.stderr)
        print("[拒绝] 默认只认 --report 同目录下的 build_report.json（本轮候选报告）；"
              "报告在别处时请显式传 --build-report", file=sys.stderr)
        return 2

    v4_doc = load_json(LAB / "eval" / "test_questions_v4.json")
    v3_doc = load_json(LAB / "eval" / "test_questions_v3.json")
    c3 = load_json(LAB / "eval" / "criteria_m2_v3.json")
    c2 = load_json(LAB / "eval" / "criteria_m2_v2.json")
    v4 = {q["test_id"]: q for q in v4_doc["questions"]}
    v3 = {q["test_id"]: q for q in v3_doc["questions"]}
    manifest = load_json(MANIFEST)
    build_report = load_json(build_report_path)
    baseline = load_module(args.baseline_grade, "grade_baseline")
    # 33 号 R1 的前后对照：候选修订 1（25347273…）用的是 `v in text`，修订 2 才是数字边界。
    rev1_grade = load_module(args.rev1_grade, "grade_rev1") if Path(args.rev1_grade).exists() else None

    out = {
        "checked_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "scoring_candidate_v3_verify",
        "effective_args": {
            "report": str(Path(args.report).resolve()),
            "build_report": str(build_report_path.resolve()),
            "build_report_sha256": sha256_file(build_report_path),
            "build_report_is_sibling_of_report": build_report_path.resolve().parent
                                                  == Path(args.report).resolve().parent,
            "build_report_claimed_candidate": {
                "test_questions_v4.json": build_report.get("out_sha256"),
                "criteria_m2_v3.json": build_report.get("criteria_out_sha256"),
            },
            "baseline_grade": str(args.baseline_grade),
            "rev1_grade": str(args.rev1_grade),
            "rev2_questions": str(args.rev2_questions),
            "reconcile": str(args.reconcile),
        },
        "identities": {},
        "summary_gold": [], "summary_details": [], "summary_negative": [],
        "numeric_variants": [], "extract_cases": [], "qa_cases": [],
        "baseline_equivalence": {}, "replay": {},
    }

    # ---------------------------------------------------------------- A 身份
    identities = {
        "eval/test_questions_v4.json": sha256_file(LAB / "eval" / "test_questions_v4.json"),
        "eval/criteria_m2_v3.json": sha256_file(LAB / "eval" / "criteria_m2_v3.json"),
        "eval/test_questions_v3.json": sha256_file(LAB / "eval" / "test_questions_v3.json"),
        "eval/criteria_m2_v2.json": sha256_file(LAB / "eval" / "criteria_m2_v2.json"),
        "eval/test_questions_v2.json": sha256_file(LAB / "eval" / "test_questions_v2.json"),
        "eval/criteria_m2_v1.json": sha256_file(LAB / "eval" / "criteria_m2_v1.json"),
        "eval/criteria_v1.json": sha256_file(LAB / "eval" / "criteria_v1.json"),
        "grade.py": sha256_file(LAB / "grade.py"),
        "grade.py.baseline": sha256_file(Path(args.baseline_grade)),
        "prices/prices_v1.json": sha256_file(LAB / "prices" / "prices_v1.json"),
    }
    out["identities"] = identities
    check("候选文件哈希与构建报告一致",
          identities["eval/test_questions_v4.json"] == build_report["out_sha256"]
          and identities["eval/criteria_m2_v3.json"] == build_report["criteria_out_sha256"],
          {"build_report_used": str(build_report_path),
           "live_test_questions_v4": identities["eval/test_questions_v4.json"][:16],
           "report_test_questions_v4": (build_report.get("out_sha256") or "")[:16],
           "live_criteria_m2_v3": identities["eval/criteria_m2_v3.json"][:16],
           "report_criteria_m2_v3": (build_report.get("criteria_out_sha256") or "")[:16],
           "hint": "报告与现场候选不一致：若该报告属旧轮，请显式传 --build-report 指向本轮构建报告"})
    protected_changed = {p: (before, sha256_file(p))
                         for p, before in build_report["protected_before"].items()
                         if not Path(p).exists() or sha256_file(p) != before}
    check("旧版本题集与判据字节未变（v1/v2/v3 题集 + criteria_v1/m2_v1/m2_v2，对照构建报告）",
          not protected_changed, protected_changed)
    check("基线 grade.py 就是本批冻结清单里的 ee436f2d…（行为对照用的旧实现）",
          identities["grade.py.baseline"] == manifest["code"]["grade.py"],
          {"baseline": identities["grade.py.baseline"][:16], "manifest": manifest["code"]["grade.py"][:16]})
    code_paths = [rel for rel in manifest["code"] if rel != "grade.py"]
    src_changed = []
    for rel in code_paths:
        live = sha256_file(LAB / rel)
        identities[rel] = live
        if live != manifest["code"][rel]:
            src_changed.append(rel)
    check("除 grade.py 外的 10 件运行源码现场哈希与冻结清单相同", not src_changed, src_changed)
    check("grade.py 相对冻结实现只增加版本化扩展（哈希已变，属需复审的变更）",
          identities["grade.py"] != identities["grade.py.baseline"],
          {"current": identities["grade.py"][:16], "frozen": identities["grade.py.baseline"][:16]})

    # ---------------------------------------------------------------- B 继承性
    inherited_exact = {
        "coverage_threshold": c2["coverage_threshold"],
        "refusal_phrase": c2["refusal_phrase"],
        "summary_human_items": c2["summary_human_items"],
    }
    check("阈值 / 拒答短语 / 摘要人工项逐字继承 criteria_m2_v2",
          all(c3[k] == val for k, val in inherited_exact.items()),
          {k: (c3[k], val) for k, val in inherited_exact.items() if c3[k] != val})
    rule_kept = [k for k in ("extract", "qa_grounded", "unknown") if c3["overall_pass_rule"][k] == c2["overall_pass_rule"][k]]
    check("整体达标合成规则中 extract / qa_grounded / unknown 三条逐字继承（只改 summary 一条）",
          rule_kept == ["extract", "qa_grounded", "unknown"],
          rule_kept)
    human_changed = sorted(diff_paths(c2["human_items"], c3["human_items"]))
    out["human_items_changed"] = human_changed
    check("人工项里只有 facts_covered_by_human / no_unsupported_claim 两处文案被细化（需复审）",
          human_changed == ["facts_covered_by_human", "no_unsupported_claim"], human_changed)

    # ---------------------------------------------------------------- C 摘要金答案与等价改写
    gold_fail, flip_fail, detail_rows = [], [], []
    for tid in SUMMARY_IDS:
        q = v4[tid]
        gold = gold_summary(q, 0)
        r_gold = grade.grade("summary", gold, q, c3)
        if not r_gold["mechanized_pass"]:
            gold_fail.append(f"{tid}:{r_gold.get('reason')}")
        flip = gold
        flip_used = []
        for point in points_of(q):
            variants = point["variants"]
            if len(variants) > 1 and variants[0] in flip:
                flip = flip.replace(variants[0], variants[-1])
                flip_used.append(f"{point['id']}:{variants[0]}→{variants[-1]}")
        r_flip = grade.grade("summary", flip, q, c3)
        if not r_flip["mechanized_pass"]:
            flip_fail.append(f"{tid}:{r_flip.get('reason')}")
        detail_rows.append({"test_id": tid, "gold_chars": len(gold), "max_chars": q["criteria"]["max_chars"],
                            "gold_ratio": r_gold["detail"]["coverage_ratio"],
                            "flip_ratio": r_flip["detail"]["coverage_ratio"],
                            "flip_substitutions": flip_used,
                            "points": r_gold["detail"].get("coverage_points_detail"),
                            "match_rule": r_gold["detail"].get("coverage_match_rule")})
    out["summary_gold"] = [{"test_id": tid, "text": gold_summary(v4[tid], 0)} for tid in SUMMARY_IDS]
    out["summary_details"] = detail_rows
    check("10 道摘要题金答案在候选判据下全部通过", not gold_fail, "；".join(gold_fail))
    check("10 道摘要题的「等价改写」（把主变体换成末变体）全部通过", not flip_fail, "；".join(flip_fail))
    check("等价改写确实命中了末变体（不是靠其他点凑覆盖率）",
          all(row["flip_ratio"] == 1.0 for row in detail_rows),
          [row["test_id"] for row in detail_rows if row["flip_ratio"] != 1.0])

    # ---------------------------------------------------------------- C2 写法闭包（33 号 R2）
    writing_rows, writing_bad = [], []
    for tid in SUMMARY_IDS:
        q = v4[tid]
        gold = gold_summary(q, 0)
        for point in points_of(q):
            base = point["variants"][0]
            if base not in gold:
                writing_bad.append(f"{tid}.{point['id']}：金答案不含主变体")
                continue
            for variant in point["variants"]:
                text = gold.replace(base, variant)
                r = grade.grade("summary", text, q, c3)
                entry = next((d for d in r["detail"]["coverage_points_detail"]
                              if d["point"] == point["id"]), None)
                hit = bool(entry and entry["hit"])
                ratio = r["detail"]["coverage_ratio"]
                writing_rows.append({"test_id": tid, "point": point["id"], "writing": variant,
                                     "hit": hit, "ratio": ratio})
                if not (hit and ratio == 1.0):
                    writing_bad.append(f"{tid}.{point['id']}「{variant}」")
    out["allowed_writings"] = writing_rows
    check("每个事实点的每一种已冻结写法都能命中该点、且金答案覆盖率保持 1.0（表内一致）",
          not writing_bad, "；".join(writing_bad))
    t23_writings = [row for row in writing_rows
                    if row["test_id"] == "T23" and row["point"] == "time_window"]

    # ---------------------------------------------------------------- C3 独立构造（35 号 §2）
    time_point = next(p for p in points_of(v4["T23"]) if p["id"] == "time_window")
    table_time = set(time_point["variants"])
    expected_time = set(EXPECTED_TIME_WRITINGS)
    gold_t23 = gold_summary(v4["T23"], 0)
    base_time = time_point["variants"][0]
    independent_rows, independent_bad = [], []
    for writing in EXPECTED_TIME_WRITINGS:
        r = grade.grade("summary", gold_t23.replace(base_time, writing), v4["T23"], c3)
        entry = next((d for d in r["detail"]["coverage_points_detail"]
                      if d["point"] == "time_window"), None)
        hit = bool(entry and entry["hit"])
        independent_rows.append({"writing": writing, "in_table": writing in table_time,
                                 "hit": hit, "ratio": r["detail"]["coverage_ratio"]})
        if not (hit and r["detail"]["coverage_ratio"] == 1.0):
            independent_bad.append(writing)
    out["independent_time_writings"] = independent_rows
    out["time_writings_missing_from_table"] = sorted(expected_time - table_time)
    out["time_writings_extra_in_table"] = sorted(table_time - expected_time)
    check("独立构造（不用生成函数）：按题面承诺手写的 26 条时间写法逐条命中、覆盖率 1.0",
          len(EXPECTED_TIME_WRITINGS) == 26 and not independent_bad,
          independent_bad)
    check("双向一致：题面承诺的写法全部在表内，且表内不得多出题面未承诺的时间写法",
          not (expected_time - table_time) and not (table_time - expected_time),
          {"missing": out["time_writings_missing_from_table"],
           "extra": out["time_writings_extra_in_table"]})
    check("T23 时间写法首位仍是冻结事实的原始写法（coverage_keywords[0] 不受影响）",
          base_time == "9 点到 12 点"
          and v4["T23"]["criteria"]["coverage_keywords"][0] == base_time,
          base_time)
    check("表内时间写法条数与独立承诺同为 26 条（与 35 号推算一致）",
          len(t23_writings) == 26 and len(table_time) == 26,
          {"table_rows": len(t23_writings), "table_set": len(table_time)})

    # 修订 2 ↔ 修订 3 的前后对照：用 34 号证据里的修订 2 题集字节复现 35 号 §2 的漏计
    named_by_35 = {"9点-12点", "9 点-12 点", "9 点-12点", "9点-12 点"}
    if Path(args.rev2_questions).exists():
        rev2_doc = load_json(args.rev2_questions)
        rev2_time_point = next(p for p in points_of({q["test_id"]: q for q in rev2_doc["questions"]}["T23"])
                               if p["id"] == "time_window")
        rev2_table = set(rev2_time_point["variants"])
        missing_in_rev2 = [w for w in EXPECTED_TIME_WRITINGS if w not in rev2_table]
        out["rev2_time_gap"] = {
            "rev2_questions_sha256": sha256_file(Path(args.rev2_questions)),
            "rev2_count": len(rev2_table), "rev3_count": len(table_time),
            "missing_in_rev2": missing_in_rev2,
            "now_in_table_and_hit": [row["writing"] for row in independent_rows
                                     if row["writing"] in missing_in_rev2 and row["hit"] and row["in_table"]],
        }
        check("R2 前后对照：修订 2 表内缺 8 条完整连字符写法（含 35 号点名的 4 条），修订 3 全部补齐并命中",
              len(missing_in_rev2) == 8 and named_by_35 <= set(missing_in_rev2)
              and all(w in table_time for w in missing_in_rev2)
              and all(row["hit"] for row in independent_rows if row["writing"] in missing_in_rev2),
              out["rev2_time_gap"])

    fact_numbers, example_numbers = set(), set()
    for tid in SUMMARY_IDS:
        for fact in v4[tid].get("summary_facts") or []:
            fact_numbers |= set(re.findall(r"\d+", fact))
        appended = v4[tid]["question"][len(v3[tid]["question"]):]
        example_numbers |= set(re.findall(r"\d+", appended))
    out["prompt_example_numbers"] = sorted(example_numbers)
    check("题面追加句里出现的示例数值与所有冻结事实数值不重合（示例不泄漏答案）",
          not (example_numbers & fact_numbers), sorted(example_numbers & fact_numbers))

    # ---------------------------------------------------------------- D 摘要负例
    negatives = []
    for tid in SUMMARY_IDS:
        q = v4[tid]
        gold = gold_summary(q, 0)
        ids = [p["id"] for p in points_of(q)]
        cases = [
            ("缺 2 个事实点", drop_points(q, gold, ids[:2]), False, "覆盖率 0.5 < 0.6"),
            ("缺 1 个事实点", drop_points(q, gold, ids[:1]), True, "覆盖率 0.75 ≥ 0.6（冻结阈值口径，本候选未改）"),
            ("超长", gold + "补" * (q["criteria"]["max_chars"] + 10), False, "长度超上限"),
            ("命中禁止词", gold + (q["criteria"]["forbidden_keywords"] or [""])[0], False, "禁止内容"),
        ]
        if tid == "T23":
            cases.append(("非自愿捐赠", gold.replace("三成自愿捐入", "强制捐入三成"), False, "T23 新增禁止词"))
        for name, text, expect_pass, why in cases:
            r = grade.grade("summary", text, q, c3)
            got = bool(r["mechanized_pass"])
            negatives.append({"test_id": tid, "case": name, "expect_pass": expect_pass, "got_pass": got,
                              "reason": r.get("reason"), "why": why,
                              "forbidden_hit": r["detail"].get("forbidden_hit")})
    out["summary_negative"] = negatives
    bad = [f"{n['test_id']}/{n['case']}" for n in negatives if n["got_pass"] != n["expect_pass"]]
    check("摘要负例全部符合预期（缺复合事实 / 超长 / 禁止词 / 非自愿捐赠不过；单点缺失仍过并已披露）",
          not bad, bad)

    # ---------------------------------------------------------------- E 变体 vs 数值变形
    numeric = []
    for tid in SUMMARY_IDS:
        q = v4[tid]
        gold = gold_summary(q, 0)
        for point in points_of(q):
            mut = NUMERIC_MUTATIONS.get(point["id"])
            if not mut:
                continue
            old_v, new_v = mut
            mutated = gold.replace(old_v, new_v)
            r = grade.grade("summary", mutated, q, c3)
            hit = next(d for d in r["detail"]["coverage_points_detail"] if d["point"] == point["id"])
            numeric.append({"test_id": tid, "point": point["id"], "mutation": f"{old_v}→{new_v}",
                            "point_hit_after_mutation": hit["hit"], "ratio": r["detail"]["coverage_ratio"],
                            "mechanized_pass": r["mechanized_pass"]})
    out["numeric_variants"] = numeric
    check("含数字的事实点做数值变形后该点必须不命中（变体不吃错数字）",
          all(not n["point_hit_after_mutation"] for n in numeric), numeric)
    check("数值变形后的单项结果仍按冻结阈值计（4 点题 1 点变形 → 0.75 仍过；已披露，不是本候选引入）",
          all(n["mechanized_pass"] is True for n in numeric), numeric)

    # ---------------------------------------------------------------- E2 数字边界（33 号 R1）
    injections, injection_bad = [], []
    for tid in SUMMARY_IDS:
        q = v4[tid]
        gold = gold_summary(q, 0)
        for point in points_of(q):
            base = point["variants"][0]
            if not base or not base[0].isdigit():
                continue
            for name, mutate in INJECTIONS:
                mutated_writing = mutate(base)
                mutated_text = gold.replace(base, mutated_writing)
                r = grade.grade("summary", mutated_text, q, c3)
                entry = next((d for d in r["detail"]["coverage_points_detail"]
                              if d["point"] == point["id"]), None)
                hit = bool(entry and entry["hit"])
                rev1_hit, rev1_ratio = None, None
                if rev1_grade is not None:
                    rev1 = rev1_grade.grade("summary", mutated_text, q, c3)
                    rev1_entry = next((d for d in rev1["detail"]["coverage_points_detail"]
                                       if d["point"] == point["id"]), None)
                    rev1_hit = bool(rev1_entry and rev1_entry["hit"])
                    rev1_ratio = rev1["detail"]["coverage_ratio"]
                injections.append({"test_id": tid, "point": point["id"], "case": name,
                                   "wrong_writing": mutated_writing, "point_hit": hit,
                                   "coverage_ratio": r["detail"]["coverage_ratio"],
                                   "boundary_rejected": (entry or {}).get("boundary_rejected"),
                                   "rev1_point_hit": rev1_hit, "rev1_coverage_ratio": rev1_ratio})
                if hit:
                    injection_bad.append(f"{tid}.{point['id']}/{name}")
    out["numeric_boundary_injections"] = injections
    check(f"33 号 R1 反例（**紧邻**写法）：把正确数值包进错误数值"
          f"（{len(INJECTIONS)} 类 × 全部含数字事实点）后该点一律不命中",
          not injection_bad, injection_bad)
    if rev1_grade is not None:
        rev1_wrong_hits = [row for row in injections if row["rev1_point_hit"]]
        check(f"R1 前后对照：候选修订 1（25347273…）把这 {len(injections)} 个错误数值全部计为命中，"
              f"修订 2/3 为 0",
              len(rev1_wrong_hits) == len(injections) and not injection_bad,
              {"rev1_wrong_hits": len(rev1_wrong_hits), "total": len(injections)})

    helper_rows = []
    for variant, text, expect, why in HELPER_CASES:
        got = grade._variant_hit(variant, text) is not None
        helper_rows.append({"variant": variant, "text": text, "expect_hit": expect,
                            "got_hit": got, "why": why})
    out["numeric_boundary_helper"] = helper_rows
    check("数字边界助手的单元检查（后缀 / 小数 / 紧邻千分位与全角标点的区分）",
          all(row["expect_hit"] == row["got_hit"] for row in helper_rows),
          [row for row in helper_rows if row["expect_hit"] != row["got_hit"]])

    # 35 号 §3：保证范围之外的写法明示出来（它们**应当**仍命中），避免把 R1 写成无条件保证
    boundary_rows = []
    for variant, text, why in DISCLOSED_BOUNDARY_CASES:
        got = grade._variant_hit(variant, text) is not None
        boundary_rows.append({"variant": variant, "text": text, "still_hits": got, "why": why})
    out["numeric_boundary_disclosed_outside"] = boundary_rows
    check("已披露的保证范围之外写法（符号/千分位与数字之间有空格的）仍会命中——明示边界，不是回归",
          all(row["still_hits"] for row in boundary_rows),
          [row for row in boundary_rows if not row["still_hits"]])
    not_covered = c3["coverage_match_rule"]["numeric_boundary"].get("not_covered", "")
    out["criteria_not_covered_text"] = not_covered
    check("判据里确实写明该不覆盖项（不写成「所有数值变形必然不命中」）",
          "- 820 万人次" in not_covered and "± 820 万人次" in not_covered,
          not_covered[:120])

    # ---------------------------------------------------------------- F 抽取题
    extract_cases = []
    gold_bad = []
    for tid in EXTRACT_IDS:
        q = v4[tid]
        r = grade.grade("extract", json.dumps(q["answer_key"], ensure_ascii=False), q, c3)
        extract_cases.append({"test_id": tid, "case": "金答案", "expect_pass": True,
                              "got_pass": bool(r["mechanized_pass"]), "reason": r.get("reason")})
        if not r["mechanized_pass"]:
            gold_bad.append(tid)
    extra = [
        ("T01", {"warehouse_id": "3号库房"}, "内部空格被删（题面要求照抄原文含空格）", False),
        ("T01", {"area_sqm": 241}, "错数字", False),
        ("T04", {"host": "高翔（设计部）"}, "附加未被要求的部门", False),
        ("T05", {"cpu_percent": "62.5"}, "数字字段给字符串", False),
        ("T06", {"paid": 1}, "boolean/number 混用", False),
        ("T07", {"approved_by": None, "sample_size": "40"}, "数字字段给字符串", False),
        ("T09", {"online_viewers": "2.3 万人次"}, "未按题面换算（错单位）", False),
        ("T10", {"root_cause": "连接池耗尽导致超时，非代码缺陷"}, "附加未允许说明", False),
    ]
    for tid, patch, why, expect in extra:
        q = v4[tid]
        payload = dict(q["answer_key"])
        payload.update(patch)
        r = grade.grade("extract", json.dumps(payload, ensure_ascii=False), q, c3)
        extract_cases.append({"test_id": tid, "case": why, "expect_pass": expect,
                              "got_pass": bool(r["mechanized_pass"]), "reason": r.get("reason")})
    out["extract_cases"] = extract_cases
    bad = [f"{c['test_id']}/{c['case']}" for c in extract_cases if c["got_pass"] != c["expect_pass"]]
    check("抽取题：10 道金答案全过；错数字 / 错单位 / 漏字段 / 类型错 / 附加说明 / 删空格全部不过",
          not gold_bad and not bad, {"gold_bad": gold_bad, "case_bad": bad})

    # ---------------------------------------------------------------- G 拒答
    qa_cases = []
    t19, t20 = v4["T19"], v4["T20"]
    real19 = answer_text("high", "T19") or ""
    cases = [
        ("T19", "固定短语", "资料中未找到相关依据。", True),
        ("T19", "语义正确但未用固定短语（本批真实回答）", real19, False),
        ("T19", "编造序列号", "出厂序列号是 SRV-2024-0031。", False),
        ("T20", "固定短语", "资料中未找到相关依据。", True),
        ("T20", "编造入职日期", "该员工于 2026-03-01 入职。", False),
    ]
    for tid, name, text, expect in cases:
        r = grade.grade("qa_grounded", text, v4[tid], c3)
        qa_cases.append({"test_id": tid, "case": name, "expect_pass": expect,
                         "got_pass": bool(r["mechanized_pass"]), "reason": r.get("reason"),
                         "detail": r["detail"]})
    out["qa_cases"] = qa_cases
    bad = [f"{c['test_id']}/{c['case']}" for c in qa_cases if c["got_pass"] != c["expect_pass"]]
    check("拒答口径：固定短语过；语义拒答（真实 high T19）与编造序列号/日期不过（口径已写明为输出契约遵循）",
          not bad, bad)

    # ---------------------------------------------------------------- H 旧路径行为等价
    inputs = []
    for tid, q in v3.items():
        gold_text = "；".join(q.get("summary_facts") or []) if q["task_type_gold"] == "summary" else ""
        real = None
        for group in ("low", "high", "route"):
            text = answer_text(group, tid)
            if text:
                real = text
                break
        if q["task_type_gold"] == "extract":
            base = json.dumps(q["answer_key"], ensure_ascii=False)
            inputs += [("extract", base, q), ("extract", base.replace(" ", ""), q),
                       ("extract", "", q), ("extract", "```json\n" + base + "\n```", q)]
        elif q["task_type_gold"] == "qa_grounded":
            inputs += [("qa_grounded", "资料中未找到相关依据。", q),
                       ("qa_grounded", "资料中未找到相关依据", q),
                       ("qa_grounded", "根据资料，答案是 24 小时。", q), ("qa_grounded", "", q)]
        else:
            inputs += [("summary", gold_text, q),
                       ("summary", gold_text + (q["criteria"]["forbidden_keywords"] or [""])[0], q),
                       ("summary", "", q), ("summary", gold_text + "补" * 400, q)]
        if real:
            inputs.append((q["task_type_gold"], real, q))
    mismatches = []
    for task_type, content, q in inputs:
        a = grade.grade(task_type, content, q, c2)
        b = baseline.grade(task_type, content, q, c2)
        if json.dumps(a, ensure_ascii=False, sort_keys=True) != json.dumps(b, ensure_ascii=False, sort_keys=True):
            mismatches.append({"task": task_type, "question": q["test_id"], "chars": len(content)})
    out["baseline_equivalence"] = {"inputs": len(inputs), "mismatches": mismatches}
    check("旧路径（criteria_m2_v2 题集输入）下，现行 grade.py 与冻结基线逐条同结果（含 detail）",
          not mismatches, f"{len(inputs)} 条输入，{len(mismatches)} 条不一致")

    # 变更确实生效：同一份「等价改写」在旧实现下不过、在新实现下过
    demo = []
    for tid in ("T21", "T22", "T23", "T24"):
        q4 = v4[tid]
        text = gold_summary(q4, 0)
        for point in points_of(q4):
            variants = point["variants"]
            if len(variants) > 1 and variants[0] in text:
                text = text.replace(variants[0], variants[-1])
        r_new = grade.grade("summary", text, q4, c3)
        r_old = baseline.grade("summary", text, q4, c3)  # 旧实现读不到 coverage_points → 退回 coverage_keywords
        demo.append({"test_id": tid, "new_pass": bool(r_new["mechanized_pass"]),
                     "old_impl_pass": bool(r_old["mechanized_pass"]),
                     "old_impl_ratio": r_old["detail"]["coverage_ratio"],
                     "new_ratio": r_new["detail"]["coverage_ratio"]})
    out["candidate_effect"] = demo
    check("候选确实解决了「表示差异被当漏事实」：同一份等价改写，旧实现不过、新实现过",
          all(d["new_pass"] and not d["old_impl_pass"] for d in demo), demo)

    # ---------------------------------------------------------------- I 现场重放
    recon = load_json(args.reconcile)
    replay_bad, replayed = [], 0
    for item in recon["items"]:
        if item["run_status"] != "ok":
            continue
        tid, group = item["test_id"], item["group"]
        text = answer_text(group, tid)
        if text is None:
            replay_bad.append(f"{group}-{tid} 缺回答文件")
            continue
        r = grade.grade(item["task_type_gold"], text, v3[tid], c2)
        replayed += 1
        if bool(r["mechanized_pass"]) != bool(item["mechanized_pass"]):
            replay_bad.append({"group": group, "test_id": tid, "replayed": bool(r["mechanized_pass"]),
                               "recorded": bool(item["mechanized_pass"]), "reason": r.get("reason")})
    out["replay"] = {"replayed": replayed, "mismatches": replay_bad}
    check("现场重放：70 条回答的 mechanized_pass 与 reconcile_v3.json 逐条一致",
          replayed == 70 and not replay_bad, {"replayed": replayed, "bad": replay_bad})

    # ---------------------------------------------------------------- J 现场影响（诊断，不回填旧结论）
    impact = {"summary": [], "extract": [], "summary_frozen_pass": 0, "summary_candidate_pass": 0}
    regress = []
    for tid in SUMMARY_IDS:
        rows = []
        for group in ("low", "high", "route"):
            text = answer_text(group, tid)
            if text is None:
                continue
            frozen = grade.grade("summary", text, v3[tid], c2)
            cand = grade.grade("summary", text, v4[tid], c3)
            rows.append({"group": group, "frozen_pass": bool(frozen["mechanized_pass"]),
                         "candidate_pass": bool(cand["mechanized_pass"]),
                         "frozen_ratio": frozen["detail"]["coverage_ratio"],
                         "candidate_ratio": cand["detail"]["coverage_ratio"],
                         "candidate_missing": cand["detail"]["coverage_missing"],
                         "forbidden_hit": cand["detail"]["forbidden_hit"]})
            if frozen["mechanized_pass"] and not cand["mechanized_pass"]:
                regress.append(f"{group}-{tid}")
        impact["summary_frozen_pass"] += sum(1 for r in rows if r["frozen_pass"])
        impact["summary_candidate_pass"] += sum(1 for r in rows if r["candidate_pass"])
        impact["summary"].append({"test_id": tid, "rows": rows})
    for tid in EXTRACT_IDS:
        for group in ("low", "high", "route"):
            text = answer_text(group, tid)
            if text is None:
                continue
            frozen = grade.grade("extract", text, v3[tid], c2)
            cand = grade.grade("extract", text, v4[tid], c3)
            if bool(frozen["mechanized_pass"]) != bool(cand["mechanized_pass"]):
                impact["extract"].append({"group": group, "test_id": tid,
                                          "frozen_pass": bool(frozen["mechanized_pass"]),
                                          "candidate_pass": bool(cand["mechanized_pass"])})
    out["on_batch_impact"] = impact
    check("候选不会把本轮已通过的摘要项判成不通过（单调性，不含回填结论）", not regress, regress)
    check("抽取题在本轮 30 条回答上候选与冻结判据结论逐条相同（该分支未变）",
          not impact["extract"], impact["extract"])

    # ---------------------------------------------------------------- K 人机分工（合成负例）
    # 用**现行 tools/review_m2.py 的人工函数 + final_pass AND 合成**跑合成负例，
    # 证明「机检过但最终失败」这条分工是有效的；人工值为合成测试数据，不是用户结论。
    review_m2 = load_module(ROOT / "tools" / "review_m2.py", "review_m2")

    def compose(group, tid, content, human_items, facts_covered=None, row_state="answered"):
        question = v4[tid]
        quality = grade.grade(question["task_type_gold"], content, question, c3)
        entry = {"human_items": human_items}
        if facts_covered is not None:
            entry["facts_covered_points"] = facts_covered
        state, pending = review_m2.human_state(question, entry)
        verdict = review_m2.human_verdict(question, entry)
        final_pass = (bool(quality["mechanized_pass"]) and state == "filled"
                      and verdict is True and row_state == "answered")
        return {
            "group": group, "test_id": tid, "task_type": question["task_type_gold"],
            "machine_pass": bool(quality["mechanized_pass"]),
            "reason": quality.get("reason"),
            "coverage_ratio": quality["detail"].get("coverage_ratio"),
            "forbidden_hit": quality["detail"].get("forbidden_hit"),
            "human_status": state, "human_pending": pending,
            "human_verdict": verdict, "row_state": row_state,
            "final_pass": final_pass,
        }

    t21_gold, t23_gold = gold_summary(v4["T21"], 0), gold_summary(v4["T23"], 0)
    composition = [
        ("正向对照：金答案 + 人工全通过",
         compose("low", "T23", t23_gold, {"no_added_content_by_human": True}, [True] * 5),
         True, True),
        ("缺一个复合事实（机检仍过 0.75，人工逐点判失败）",
         compose("high", "T23", drop_points(v4["T23"], t23_gold, ["venue"]),
                 {"no_added_content_by_human": True}, [True, False, True, True, True]),
         True, False),
        ("固定拒答短语后接编造序列号",
         compose("high", "T19", "资料中未找到相关依据，但出厂序列号就是 SRV-2024-0031。",
                 {"facts_correct": True, "no_unsupported_claim": False, "citation_supports": True}),
         True, False),
        ("固定拒答短语后接编造入职日期",
         compose("route", "T20", "资料中未找到相关依据，但该员工于 2026-03-01 入职。",
                 {"facts_correct": True, "no_unsupported_claim": False, "citation_supports": True}),
         True, False),
        ("错误数值摘要（R1 后该点机检不命中；冻结阈值 0.6 下整题机检仍过 0.75，人工判该点失败）",
         compose("route", "T21", t21_gold.replace("820 万人次", "1820 万人次"),
                 {"no_added_content_by_human": True}, [False, True, True, True]),
         True, False),
        ("非自愿捐赠但避开禁止词（机检过、人工失败）",
         compose("low", "T23", t23_gold.replace("三成自愿捐入", "三成须捐入"),
                 {"no_added_content_by_human": False}, [True, True, True, True, False]),
         True, False),
    ]
    out["human_composition"] = [
        dict(case=case, expect_machine=expect_machine, expect_final=expect_final, **payload)
        for case, payload, expect_machine, expect_final in composition]
    mismatch = [row["case"] for row in out["human_composition"]
                if row["machine_pass"] != row["expect_machine"]
                or row["final_pass"] != row["expect_final"]]
    check("人机分工：正向对照最终通过；5 例合成负例无一被最终合成放行",
          not mismatch, mismatch)
    machine_pass_final_fail = [row["case"] for row in out["human_composition"]
                               if row["machine_pass"] and not row["final_pass"]]
    check("至少 3 例「机检通过但最终失败」，证明人工项与 AND 合成没有被绕过",
          len(machine_pass_final_fail) >= 3, machine_pass_final_fail)

    # ---------------------------------------------------------------- 报告
    out["checks"] = CHECKS
    out["passed"] = sum(1 for c in CHECKS if c["ok"])
    out["total"] = len(CHECKS)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n==== 评分版本候选离线验证：{out['passed']}/{out['total']} PASS ====")
    print("[build-report used]", build_report_path)
    print("[report]", args.report)
    return 0 if out["passed"] == out["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
