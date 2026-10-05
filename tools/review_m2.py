# -*- coding: utf-8 -*-
"""M2 人工评定工具：导出核对材料 → 校验评定 → 派生汇总（11 号 §5 + 13 号 R1/R2）。

三个子命令：
  export  从批次目录导出隐藏组别/模型/费用/延迟的核对材料（确定性打乱，
          映射单独保存在本地；承认回答风格可能让评定者猜到模型，不声称完全盲法）。
  collect 读取人工评定 JSON，逐条验证身份（review_id / batch / request / test /
          答案 SHA-256 / 题集与判据身份 / 重复），并留下「输入评定 + 规范化结果 +
          映射」三者哈希；不合格即拒绝，不产出 validated。
  derive  先复核身份链（冻结题集与判据、**实际答案字节**、validated 未被手改、
          mapping 未被替换），再用共用只读对账（m2_reconcile）合成三组逐题表与指标。

13 号 R1/R2 的最小修正落在本文件：
- 三子命令共用 `resolve_frozen()` 与 `answer_index()`：题集/判据身份与映射、
  实际答案字节都在每一步核对；`--questions/--criteria` 指到不同文件时直接拒绝。
- `derive` 复核 `validated_reviews.json` 的规范化哈希与 mapping 哈希，手改或换映射即拒绝；
  逐事实点数量与类型在派生时仍校验（必须与冻结题集 summary_facts 数量一致）。
- collect/derive 共用 mapping→原始请求/计划/实际答案校验；收集前的错映射同样拒绝，
  已收集评定在派生时再次核对批次、请求、题号与答案身份（15 号 R1 收尾）。
- 费用一律来自 m2_reconcile（父占用 + 子 start/end），与批次汇总同一条路径；
  未闭合/查不到子事件的尝试计未知，不因"没有请求行"而漏账。
- 逐题表按计划补齐：未执行/中断/未知同样成行；人工状态区分 absent / pending / filled，
  单列待核项，不用"存在一条记录"冒充"人工已完成"。

退出码：0 成功 / 1 参数或身份校验失败 / 2 批次结构不完整。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import m2_reconcile  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

GROUPS = ("low", "high", "route")
QA_HUMAN_ITEMS = ("facts_correct", "no_unsupported_claim", "citation_supports")
SUMMARY_HUMAN_ITEMS = ("facts_covered_by_human", "no_added_content_by_human")
VALID_ROLES = ("user", "codebuddy", "codex")

DEFAULT_QUESTIONS = os.path.join(ROOT, "eval", "test_questions_v2.json")
DEFAULT_CRITERIA = os.path.join(ROOT, "eval", "criteria_m2_v1.json")

MANIFEST_NAME = "batch_manifest.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(obj):
    """规范化哈希：键序固定、无多余空白，用于核验"已收集评定未被手改"。"""
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# --------------------------------------------------------------- 共用身份链

def resolve_frozen(batch_dir, questions_arg, criteria_arg):
    """核对冻结题集/判据身份：必须与批次清单登记的哈希一致，否则拒绝。

    返回 (manifest, questions_path, criteria_path, questions_by_id, criteria) 或 (None, ...)。
    """
    manifest_path = os.path.join(batch_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        print("[拒绝] 批次缺少 batch_manifest.json", file=sys.stderr)
        return None, None, None, None, None
    manifest = load_json(manifest_path)
    questions_path = os.path.abspath(questions_arg or DEFAULT_QUESTIONS)
    criteria_path = os.path.abspath(criteria_arg or DEFAULT_CRITERIA)
    for label, path, key in (("题集", questions_path, "questions"), ("判据", criteria_path, "criteria")):
        if not os.path.exists(path):
            print(f"[拒绝] {label}文件不存在：{path}", file=sys.stderr)
            return None, None, None, None, None
        if sha256_file(path) != (manifest.get(key) or {}).get("sha256"):
            print(f"[拒绝] {label}与批次清单冻结身份不一致（拒绝换用其他版本）：{path}", file=sys.stderr)
            return None, None, None, None, None
    doc = load_json(questions_path)
    questions = {q["test_id"]: q for q in doc.get("questions") or []}
    criteria = load_json(criteria_path)
    return manifest, questions_path, criteria_path, questions, criteria


def answer_index(batch_dir, manifest):
    """实际答案字节核对：子运行 requests.jsonl 声明的 answer_sha256 必须等于答案文件字节哈希。

    返回 (index, problems)：problems 非空即拒绝（答案被改、被清空或缺失）。
    """
    index = {}
    problems = []
    plan_pairs = [(p["group"], p["test_id"]) for p in manifest.get("plan") or []]
    raw_dir = os.path.join(batch_dir, "raw")
    if not os.path.isdir(raw_dir):
        return index, problems
    for group in GROUPS:
        group_dir = os.path.join(raw_dir, group)
        if not os.path.isdir(group_dir):
            continue
        for test_id in sorted(os.listdir(group_dir)):
            sub = os.path.join(group_dir, test_id)
            for req in read_jsonl(os.path.join(sub, "requests.jsonl")):
                request_id = req.get("request_id")
                if plan_pairs.count((group, test_id)) != 1 or req.get("group") != group or \
                   req.get("test_id") != test_id or request_id != f"{group}-{test_id}":
                    problems.append(f"{request_id}: 原始请求身份与子目录或唯一计划项不一致")
                    continue
                if request_id in index:
                    problems.append(f"{request_id}: 原始请求身份重复，不能唯一匹配")
                    continue
                answer_path = os.path.join(sub, "answers", f"{test_id}.txt")
                if not os.path.exists(answer_path):
                    problems.append(f"{req.get('request_id')}: 答案文件缺失（{answer_path}）")
                    continue
                actual = sha256_file(answer_path)
                declared = req.get("answer_sha256")
                if actual != declared:
                    problems.append(f"{req.get('request_id')}: 实际答案字节与请求记录不一致"
                                    f"（记录 {str(declared)[:12]}…，实际 {actual[:12]}…）")
                    continue
                index[request_id] = {
                    "group": group, "test_id": test_id, "answer_sha256": actual,
                    "answer_path": answer_path, "request": req,
                }
    return index, problems


def load_mapping(batch_dir):
    path = os.path.join(batch_dir, "review", "mapping.json")
    if not os.path.exists(path):
        return None, None, None
    doc = load_json(path)
    items = doc.get("items") if isinstance(doc, dict) else None
    mapping = {m.get("review_id"): m for m in items if isinstance(m, dict)} if isinstance(items, list) else {}
    return mapping, doc, sha256_file(path)


def mapping_problems(manifest, mapping_doc, index):
    """收集与派生共用：映射必须连接到当前批次的唯一原始请求和实际答案。"""
    if not isinstance(mapping_doc, dict) or not isinstance(mapping_doc.get("items"), list):
        return ["mapping 必须包含 items 数组"]
    problems = []
    if mapping_doc.get("batch_id") != manifest["batch_id"]:
        problems.append("mapping 的 batch_id 与当前批次不符")
    seen_reviews, seen_requests = set(), set()
    for m in mapping_doc["items"]:
        if not isinstance(m, dict):
            problems.append("mapping 项必须是对象")
            continue
        rid, request_id = m.get("review_id"), m.get("request_id")
        if not isinstance(rid, str) or not rid or rid in seen_reviews:
            problems.append(f"{rid}: review_id 缺失或重复")
            continue
        seen_reviews.add(rid)
        if not isinstance(request_id, str) or not request_id or request_id in seen_requests:
            problems.append(f"{rid}: request_id 缺失或重复")
            continue
        seen_requests.add(request_id)
        if m.get("batch_id") != manifest["batch_id"]:
            problems.append(f"{rid}: mapping 项的 batch_id 与当前批次不符")
        actual = index.get(request_id)
        if actual is None or actual["request"].get("run_status") != "ok":
            problems.append(f"{rid}: mapping 未对应实际成功请求 {request_id}")
            continue
        for key in ("group", "test_id", "answer_sha256"):
            if m.get(key) != actual[key]:
                problems.append(f"{rid}: mapping 的 {key} 与原始请求/实际答案不一致")
    return problems


def review_identity_problems(entry, mapping_row, manifest):
    problems = []
    for key in ("batch_id", "request_id", "test_id", "answer_sha256"):
        if entry.get(key) != mapping_row.get(key):
            problems.append(f"{key} 与已核验映射不符")
    for key, frozen in (("questions_sha256", "questions"), ("criteria_sha256", "criteria")):
        if entry.get(key) != manifest[frozen]["sha256"]:
            problems.append(f"{key} 缺失或与冻结身份不符")
    return problems


def refuse_mapping(problems):
    if not problems:
        return False
    print("[拒绝] mapping 与原始请求/实际答案身份核对失败：", file=sys.stderr)
    for problem in problems[:5]:
        print("  - " + problem, file=sys.stderr)
    return True


# --------------------------------------------------------------- export

def cmd_export(args):
    manifest, _qp, _cp, questions, _criteria = resolve_frozen(args.batch, args.questions, args.criteria)
    if manifest is None:
        return 1
    index, problems = answer_index(args.batch, manifest)
    if problems:
        print("[拒绝] 实际答案身份核对失败，拒绝导出：", file=sys.stderr)
        for problem in problems[:5]:
            print("  - " + problem, file=sys.stderr)
        return 1

    rows = [v for v in index.values() if v["request"].get("run_status") == "ok"]
    rows.sort(key=lambda r: (r["answer_sha256"] or "", r["group"], r["test_id"]))

    sheet_items, mapping_items = [], []
    for i, row in enumerate(rows, 1):
        q = questions.get(row["test_id"]) or {}
        review_id = f"R{i:03d}"
        gold = q.get("task_type_gold")
        items = list(QA_HUMAN_ITEMS) if gold == "qa_grounded" else (list(SUMMARY_HUMAN_ITEMS) if gold == "summary" else [])
        with open(row["answer_path"], "rb") as fh:
            answer_text = fh.read().decode("utf-8")
        sheet_items.append({
            "review_id": review_id,
            "question": q.get("question"),
            "materials": q.get("materials"),
            "task_type_gold": gold,
            "human_items": items,
            "summary_facts": q.get("summary_facts") if gold == "summary" else None,
            "key_facts": q.get("key_facts") if gold == "qa_grounded" else None,
            "criteria_note": "机检已由评分器完成；请只评人工项。资料外问题（expect_refusal）的回答若含拒答短语即符合机检，人工项仍按事实一致性评。",
            "answer": answer_text,
        })
        mapping_items.append({
            "review_id": review_id, "group": row["group"], "test_id": row["test_id"],
            "request_id": row["request"].get("request_id"),
            "answer_sha256": row["answer_sha256"], "batch_id": manifest["batch_id"],
        })

    review_dir = os.path.join(args.batch, "review")
    os.makedirs(review_dir, exist_ok=True)
    with open(os.path.join(review_dir, "review_sheet.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "batch_id": manifest["batch_id"],
            "questions_sha256": manifest["questions"]["sha256"],
            "criteria_sha256": manifest["criteria"]["sha256"],
            "exported_at": now_iso(),
            "blindness_note": "隐藏组别/模型/费用/延迟与路由理由；顺序按答案哈希确定性打乱。回答风格可能泄露模型来源，不声称完全盲法。",
            "items": sheet_items,
        }, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(review_dir, "mapping.json"), "w", encoding="utf-8") as fh:
        json.dump({"batch_id": manifest["batch_id"],
                   "note": "本地保存，派生时用于身份校验；不要随核对材料一起发出。",
                   "items": mapping_items}, fh, ensure_ascii=False, indent=2)
    print(f"[export] {len(sheet_items)} 条核对材料 → {review_dir}")
    return 0


# --------------------------------------------------------------- collect

def normalize_review(entry, mapping_row):
    """规范化一条评定：只保留受校验的字段，键序固定（供哈希核验）。"""
    return {
        "review_id": entry.get("review_id"),
        "batch_id": entry.get("batch_id"),
        "request_id": entry.get("request_id"),
        "test_id": entry.get("test_id"),
        "answer_sha256": entry.get("answer_sha256"),
        "questions_sha256": entry.get("questions_sha256"),
        "criteria_sha256": entry.get("criteria_sha256"),
        "human_items": dict(sorted((entry.get("human_items") or {}).items())),
        "facts_covered_points": entry.get("facts_covered_points"),
        "reason": entry.get("reason"),
        "reviewer": entry.get("reviewer"),
        "reviewer_role": entry.get("reviewer_role"),
        "date": entry.get("date"),
    }


def cmd_collect(args):
    manifest, _qp, _cp, questions, _criteria = resolve_frozen(args.batch, args.questions, args.criteria)
    if manifest is None:
        return 1
    index, problems = answer_index(args.batch, manifest)
    if problems:
        print("[拒绝] 实际答案身份核对失败，拒绝收集：", file=sys.stderr)
        for problem in problems[:5]:
            print("  - " + problem, file=sys.stderr)
        return 1
    mapping, _mapping_doc, mapping_sha = load_mapping(args.batch)
    if mapping is None:
        print("[拒绝] 缺少 review/mapping.json，请先 export", file=sys.stderr)
        return 1
    if refuse_mapping(mapping_problems(manifest, _mapping_doc, index)):
        return 1

    reviews = load_json(args.reviews)
    if not isinstance(reviews, list):
        print("[拒绝] 评定文件必须是数组", file=sys.stderr)
        return 1
    input_sha = sha256_file(os.path.abspath(args.reviews))

    seen_requests = set()
    validated, rejected = [], []
    for i, entry in enumerate(reviews):
        rid = entry.get("review_id")
        problems = []
        m = mapping.get(rid)
        if m is None:
            problems.append("review_id 不在映射表")
        else:
            if m["request_id"] in seen_requests:
                problems.append("同一请求重复评定")
            problems.extend(review_identity_problems(entry, m, manifest))
            if entry.get("reviewer_role") not in VALID_ROLES:
                problems.append("reviewer_role 非法")
            if not entry.get("reviewer") or not entry.get("date"):
                problems.append("缺 reviewer 或 date")
            for item, value in (entry.get("human_items") or {}).items():
                if value is not None and type(value) is not bool:
                    problems.append(f"人工项 {item} 非 bool/null")
            q = questions.get(m["test_id"]) or {}
            if q.get("task_type_gold") == "summary":
                expected = len(q.get("summary_facts") or [])
                points = entry.get("facts_covered_points")
                if not isinstance(points, list) or len(points) != expected:
                    problems.append(f"facts_covered_points 必须与冻结题集的 {expected} 个事实点一一对应")
                elif any(p is not None and type(p) is not bool for p in points):
                    problems.append("facts_covered_points 只能 bool/null")
        if problems:
            rejected.append({"index": i, "review_id": rid, "problems": problems})
            continue
        seen_requests.add(m["request_id"])
        validated.append(normalize_review(entry, m))

    if rejected:
        print(f"[拒绝] {len(rejected)} 条评定身份校验未通过，拒绝产出 validated：", file=sys.stderr)
        for r in rejected:
            print(f"  - {r['review_id']}: {'；'.join(r['problems'])}", file=sys.stderr)
        out_path = os.path.join(args.batch, "review", "rejected_reviews.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(rejected, fh, ensure_ascii=False, indent=2)
        return 1

    validated.sort(key=lambda e: e["review_id"])
    out_path = os.path.join(args.batch, "review", "validated_reviews.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({
            "collected_at": now_iso(),
            "count": len(validated),
            "batch_id": manifest["batch_id"],
            "mapping_sha256": mapping_sha,
            "input_reviews_sha256": input_sha,
            "normalized_sha256": canonical_sha256(validated),
            "note": "改评定必须重新 collect；直接编辑本文件会被 derive 的规范化哈希核验拒绝。",
            "reviews": validated,
        }, fh, ensure_ascii=False, indent=2)
    print(f"[collect] {len(validated)} 条评定通过身份校验 → {out_path}")
    return 0


# --------------------------------------------------------------- derive

def human_state(question, entry):
    """区分 absent / pending / filled，并列待核项。"""
    gold = question.get("task_type_gold")
    if gold == "extract":
        return "filled" if entry is not None else "absent", []
    if entry is None:
        return "absent", list(QA_HUMAN_ITEMS if gold == "qa_grounded" else SUMMARY_HUMAN_ITEMS)
    items = entry.get("human_items") or {}
    pending = []
    if gold == "qa_grounded":
        for name in QA_HUMAN_ITEMS:
            if items.get(name) is None:
                pending.append(name)
    elif gold == "summary":
        if items.get("no_added_content_by_human") is None:
            pending.append("no_added_content_by_human")
        points = entry.get("facts_covered_points") or []
        facts = question.get("summary_facts") or []
        for idx, _fact in enumerate(facts):
            value = points[idx] if idx < len(points) else None
            if value is None:
                pending.append(f"facts_covered_by_human[{idx + 1}]")
    return ("filled" if not pending else "pending"), pending


def human_verdict(question, entry):
    gold = question.get("task_type_gold")
    if gold == "extract":
        return None
    if entry is None:
        return False
    items = entry.get("human_items") or {}
    if gold == "qa_grounded":
        return all(items.get(name) is True for name in QA_HUMAN_ITEMS)
    if gold == "summary":
        points = entry.get("facts_covered_points") or []
        facts_ok = bool(points) and all(p is True for p in points)
        return facts_ok and items.get("no_added_content_by_human") is True
    return False


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def cmd_derive(args):
    manifest, _qp, _cp, questions, _criteria = resolve_frozen(args.batch, args.questions, args.criteria)
    if manifest is None:
        return 1
    index, problems = answer_index(args.batch, manifest)
    if problems:
        print("[拒绝] 实际答案身份已变化，拒绝派生：", file=sys.stderr)
        for problem in problems[:5]:
            print("  - " + problem, file=sys.stderr)
        return 1
    mapping, _mapping_doc, mapping_sha = load_mapping(args.batch)
    if mapping is None:
        print("[拒绝] 缺少 review/mapping.json", file=sys.stderr)
        return 1
    if refuse_mapping(mapping_problems(manifest, _mapping_doc, index)):
        return 1
    validated_path = os.path.join(args.batch, "review", "validated_reviews.json")
    if not os.path.exists(validated_path):
        print("[拒绝] 缺少 validated_reviews.json，请先 collect", file=sys.stderr)
        return 1
    doc = load_json(validated_path)
    reviews = doc.get("reviews") or []
    if doc.get("batch_id") != manifest["batch_id"]:
        print("[拒绝] 已收集评定的 batch_id 与当前批次不符", file=sys.stderr)
        return 1
    if doc.get("mapping_sha256") != mapping_sha:
        print("[拒绝] mapping.json 在收集之后被改动，拒绝派生（请重新 export/collect）", file=sys.stderr)
        return 1
    if doc.get("normalized_sha256") != canonical_sha256(reviews):
        print("[拒绝] validated_reviews.json 内容与收集时不一致（被手改），拒绝派生（请重新 collect）", file=sys.stderr)
        return 1
    for entry in reviews:
        m = mapping.get(entry.get("review_id"))
        if m is None or review_identity_problems(entry, m, manifest):
            print("[拒绝] 已收集评定与原始请求/实际答案身份链不一致，拒绝派生", file=sys.stderr)
            return 1
        q = questions.get((m or {}).get("test_id")) or {}
        if q.get("task_type_gold") == "summary":
            expected = len(q.get("summary_facts") or [])
            if len(entry.get("facts_covered_points") or []) != expected:
                print(f"[拒绝] {entry.get('review_id')} 的事实点数量与冻结题集（{expected} 个）不符，拒绝派生",
                      file=sys.stderr)
                return 1

    by_request = {}
    for entry in reviews:
        m = mapping.get(entry.get("review_id"))
        if m:
            by_request[m["request_id"]] = entry

    # 共用只读对账：费用与逐题表都来自 m2_reconcile（与批次汇总同一条路径）
    account = m2_reconcile.reconcile(args.batch)
    table = m2_reconcile.table_skeleton(account, questions)

    for row in account["rows"]:
        cell = table[row["group"]][row["test_id"]]
        request = (index.get(row["request_id"]) or {}).get("request") or {}
        quality = request.get("quality") or {}
        cell["router_reason"] = (request.get("router") or {}).get("reason")
        attempts = request.get("attempts") or []
        cell["chosen_model"] = attempts[0].get("model") if attempts else None
        cell["mechanized_pass"] = quality.get("mechanized_pass")
        question = questions.get(row["test_id"]) or {}
        entry = by_request.get(row["request_id"])
        status, pending = human_state(question, entry)
        cell["human_status"] = status
        cell["human_filled"] = status == "filled"
        cell["human_pending_items"] = pending
        cell["human_verdict"] = human_verdict(question, entry)
        if question.get("task_type_gold") == "extract":
            cell["final_pass"] = bool(quality.get("overall_pass")) and row["state"] == "answered"
        else:
            cell["final_pass"] = bool(quality.get("mechanized_pass")) and cell["human_status"] == "filled" \
                and cell["human_verdict"] is True and row["state"] == "answered"

    planned_ids = manifest.get("planned_questions") or []

    def group_metrics(group):
        rows = table[group]
        planned = [tid for tid in planned_ids if tid in rows]
        answered = [tid for tid in planned if rows[tid]["state"] == "answered"]
        passed = [tid for tid in planned if rows[tid]["final_pass"]]
        by_cat = {}
        for cat in ("extract", "qa_grounded", "summary"):
            cat_ids = [tid for tid in planned if (questions.get(tid) or {}).get("task_type_gold") == cat]
            by_cat[cat] = {
                "denominator": len(cat_ids),
                "passed": len([tid for tid in cat_ids if rows[tid]["final_pass"]]),
                "answered": len([tid for tid in cat_ids if rows[tid]["state"] == "answered"]),
                "human_filled": len([tid for tid in cat_ids if rows[tid]["human_status"] == "filled"]),
                "human_pending": len([tid for tid in cat_ids if rows[tid]["human_status"] == "pending"]),
            }
        latencies = [rows[tid]["latency_ms_total"] for tid in answered if isinstance(rows[tid]["latency_ms_total"], (int, float))]
        acct = account["per_group"].get(group, {})
        return {
            "main_pass_rate": {"passed": len(passed), "denominator": len(planned),
                               "rate": round(len(passed) / len(planned), 6) if planned else None},
            "answered_count": len(answered),
            "content_pass_rate": {
                "passed": len([tid for tid in answered if rows[tid]["final_pass"]]),
                "denominator": len(answered),
                "rate": round(len([tid for tid in answered if rows[tid]["final_pass"]]) / len(answered), 6) if answered else None,
                "note": "回答后的内容达标率，分母为实际取得回答数；不替代主指标",
            },
            "by_category": by_cat,
            "human_completion": {
                "filled": len([tid for tid in planned if rows[tid]["human_status"] == "filled"]),
                "pending": len([tid for tid in planned if rows[tid]["human_status"] == "pending"]),
                "absent": len([tid for tid in planned if rows[tid]["human_status"] == "absent"]),
                "note": "filled = 全部适用人工项已填写；pending = 有评定但仍有 null 待核项；absent = 无评定记录",
            },
            "pending_items": {tid: rows[tid]["human_pending_items"] for tid in planned if rows[tid]["human_pending_items"]},
            "item_states": {state: len([tid for tid in planned if rows[tid]["state"] == state])
                            for state in sorted({rows[tid]["state"] for tid in planned})},
            "latency_p50_ms": median(latencies),
            "latency_samples": len(latencies),
            "cost": {
                "known_cost_cny": acct.get("known_cost_cny"),
                "unknown_attempt_count": acct.get("unknown_attempt_count"),
                "total_cost_cny": acct.get("total_cost_cny"),
            },
        }

    metrics = {g: group_metrics(g) for g in GROUPS}
    answered_sets = [
        {tid for tid in planned_ids if table[g].get(tid, {}).get("state") == "answered"} for g in GROUPS
    ]
    common = set.intersection(*answered_sets) if all(answered_sets) else set()

    paired_rows = []
    for tid in sorted(common):
        paired_rows.append({
            "test_id": tid,
            "task_type_gold": (questions.get(tid) or {}).get("task_type_gold"),
            "pass": {g: bool(table[g][tid]["final_pass"]) for g in GROUPS},
            "human_status": {g: table[g][tid]["human_status"] for g in GROUPS},
        })

    derived = {
        "batch_id": manifest["batch_id"],
        "derived_at": now_iso(),
        "planned_questions": planned_ids,
        "review_count": len(reviews),
        "review_identity": {
            "mapping_sha256": mapping_sha,
            "validated_normalized_sha256": doc.get("normalized_sha256"),
            "input_reviews_sha256": doc.get("input_reviews_sha256"),
            "note": "派生前已复核实际答案字节、冻结题集/判据身份与收集结果哈希",
        },
        "metrics": metrics,
        "cost_total": {
            "known_cost_cny": account["totals"]["known_cost_cny"],
            "unknown_attempt_count": account["totals"]["unknown_attempt_count"],
            "total_cost_cny": account["totals"]["total_cost_cny"],
            "attempts_counted": account["totals"]["attempts_counted"],
            "occurred_cost_note": account["totals"]["occurred_cost_note"],
            "complete_experiment_cost_cny": account["totals"]["complete_experiment_cost_cny"],
            "complete_experiment_note": account["totals"]["complete_experiment_note"],
            "note": "来自父占用与子 start/end 的只读对账；任一尝试费用未知则总费用为 null；不得用 null or 0 计算节省率",
        },
        "item_accounting": [
            {k: row[k] for k in ("seq", "test_id", "group", "state", "attempts_counted",
                                 "known_cost_cny", "unknown_attempt_count", "has_request_row", "run_status")}
            for row in account["rows"]
        ],
        "paired": {"common_ids": sorted(common), "samples": len(common), "rows": paired_rows,
                   "not_common": [tid for tid in planned_ids if tid not in common],
                   "note": "三组都取得回答的共同题号；人工未填完的按未达标计，不提前发布最终质量结论"},
        "blindness_note": "人工评定隐藏组别；回答风格可能泄露模型来源，不声称完全盲法。",
    }

    derived_dir = os.path.join(args.batch, "derived")
    os.makedirs(derived_dir, exist_ok=True)
    out_path = os.path.join(derived_dir, "m2_derived.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(derived, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(derived_dir, "per_question_table.json"), "w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False, indent=2)
    print(f"[derive] 派生汇总 → {out_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="M2 人工评定与派生汇总")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="导出隐藏组别的核对材料")
    p_export.add_argument("--batch", required=True)
    p_export.add_argument("--questions", default=None)
    p_export.add_argument("--criteria", default=None)
    p_export.set_defaults(func=cmd_export)

    p_collect = sub.add_parser("collect", help="校验人工评定并落盘")
    p_collect.add_argument("--batch", required=True)
    p_collect.add_argument("--reviews", required=True)
    p_collect.add_argument("--questions", default=None)
    p_collect.add_argument("--criteria", default=None)
    p_collect.set_defaults(func=cmd_collect)

    p_derive = sub.add_parser("derive", help="生成三组逐题表与派生指标")
    p_derive.add_argument("--batch", required=True)
    p_derive.add_argument("--questions", default=None)
    p_derive.add_argument("--criteria", default=None)
    p_derive.set_defaults(func=cmd_derive)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
