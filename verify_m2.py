# -*- coding: utf-8 -*-
"""M2 离线验收（11 号 §7 + 13 号窄修反例 + 18 号真实模式零调用验证）：
全部使用一次性本地回环桩或本地替身 runner，不设置凭据环境变量、零真实调用。

内置捕获桩（区别于 tools/stub_llm_server）：
- 记录**完整请求 payload**（含 messages 与生成参数）到 JSONL 台账；
- 支持按请求序号注入故障：HTTP 500 / 200 但缺 usage / 挂起（中断对账）；
- 可监听清单文件逐请求记录「HTTP 到达时批次清单是否已存在」；
- 可选「参考答案模式」：按题面匹配题目并返回冻结标准答案，用于验证正向达标通道
  （桩答案仍是夹具，不是模型质量结果）。

覆盖场景：
  A 题集完整性与 T08 契约        B 90 项固定桩批次 + 实际输入一致
  C 参考答案正向通道与人工状态   D 路由隔离
  E 整批上限 / 失败占额度 / 未知费用
  F 漂移与复用拒绝               G 中断对账 + 未闭合费用 + 逐题表补全
  H 四种身份反例                 I 子运行异常后立即停止 + 事件级费用不漏
  J 固定分母与延迟               K 历史保护
  L 真实模式零调用准备（18 号）：批准记录门禁矩阵、CLI 拒绝、委托参数、候选生成参数
    进入实际 payload、真实停线判定、本地替身 runner 下的主循环与停线（零网络、零凭据）
  M 20 号 R1/R2 反例回归：批准身份连续性（读取后撤回 / 门禁后改题面 / 首项后撤回的正常
    恢复）与预算字段有限性（NaN/Infinity 在门禁、草案与主循环三处都被拒绝或失败关闭）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import contract  # noqa: E402
import m2_batch  # noqa: E402
import router  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PORT = 5310
RUNS = os.path.join(ROOT, "runs")
VDIR = os.path.join(RUNS, "_verify_m2")
FULL_BATCH = os.path.join(RUNS, "m2-stub-verify-v2")
REF_BATCH = os.path.join(RUNS, "m2-stub-ref-v2")
QUESTIONS = os.path.join(ROOT, "eval", "test_questions_v2.json")
CRITERIA = os.path.join(ROOT, "eval", "criteria_m2_v1.json")
PRICES = os.path.join(ROOT, "prices", "prices_v1.json")

CHECKS = []
HISTORY_SNAPSHOT = {}


def check(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


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


def child_env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("ROUTER_API_KEY", None)
    return env


def snapshot_history():
    out = {}
    if os.path.isdir(RUNS):
        for name in os.listdir(RUNS):
            path = os.path.join(RUNS, name)
            if name.startswith("_") or name.startswith("m2-stub"):
                continue
            if os.path.isfile(path):
                out[path] = sha256_file(path)
            elif os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        out[fpath] = sha256_file(fpath)
    for rel in ("eval/dev_questions.json", "eval/dev_questions_v2.json", "eval/test_questions_v1.json",
                "eval/criteria_v1.json", "prices/prices_v1.json", "M1人工核对表.md"):
        path = os.path.join(ROOT, rel)
        if os.path.exists(path):
            out[path] = sha256_file(path)
    return out


def verify_history_unchanged():
    problems = [p for p, expected in HISTORY_SNAPSHOT.items()
                if not os.path.exists(p) or sha256_file(p) != expected]
    check("历史保护：历史运行、两版 dev、v1 测试题集、旧判据/价格与人工表身份未变", not problems,
          f"{len(problems)} 处变化" + ("；" + "；".join(problems[:3]) if problems else ""))


# --------------------------------------------------------------- 捕获桩

def reference_answer(question):
    gold = question.get("task_type_gold")
    if gold == "extract":
        return json.dumps(question.get("answer_key") or {}, ensure_ascii=False)
    if gold == "qa_grounded":
        if question.get("expect_refusal"):
            return "资料中未找到相关依据。"
        return "；".join(f.get("fact", "") for f in (question.get("key_facts") or []))
    text = "；".join(question.get("summary_facts") or [])
    missing = [k for k in ((question.get("criteria") or {}).get("coverage_keywords") or []) if k not in text]
    if missing:
        text += "；另涉：" + "、".join(missing)
    return text


def make_stub(plan, ledger_path, watch_manifest=None, questions=None):
    state = {"seq": 0, "plan": plan, "ledger": ledger_path, "watch": watch_manifest}

    class Handler(BaseHTTPRequestHandler):
        server_version = "CaptureStub/2.0"

        def log_message(self, fmt, *args):
            return

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.startswith("/v1/chat/completions"):
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            state["seq"] += 1
            n = state["seq"]
            record = {"seq": n, "ts": datetime.now(timezone.utc).isoformat(), "payload": body,
                      "manifest_present_at_http": None}
            if state["watch"]:
                record["manifest_present_at_http"] = os.path.exists(state["watch"])
            with open(state["ledger"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

            if n in state["plan"].get("hang_at", []):
                time.sleep(600)
                return
            break_dir = state["plan"].get("break_answer_dir")
            if break_dir and n == 1:
                os.makedirs(break_dir, exist_ok=True)
            if n in state["plan"].get("fail_at", []):
                self._send(500, {"error": {"message": "stub platform error", "type": "stub_error"}})
                return

            stub_kind = (body.get("metadata") or {}).get("stub_kind")
            content = None
            if state["plan"].get("reference_answers") and questions:
                user_text = (body.get("messages") or [{}])[-1].get("content") or ""
                for q in questions.values():
                    if user_text.startswith(q.get("question") or ""):
                        content = reference_answer(q)
                        break
            if content is None:
                if stub_kind == "ok_json":
                    content = json.dumps({"_stub": True, "note": "capture stub fixed json"},
                                         ensure_ascii=False, indent=2)
                else:
                    content = "【桩回答】这是本地捕获桩的固定输出，未经过任何模型。"

            usage = None
            if n not in state["plan"].get("no_usage_at", []):
                prompt_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages") or [])
                usage = {"prompt_tokens": max(1, prompt_chars // 4), "completion_tokens": max(1, len(content) // 4),
                         "total_tokens": max(1, prompt_chars // 4) + max(1, len(content) // 4), "cached_tokens": 0}
            self._send(200, {"id": f"cap-{n}", "object": "chat.completion", "model": body.get("model"),
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                          "finish_reason": "stop"}], "usage": usage})

    return ThreadingHTTPServer(("127.0.0.1", PORT), Handler), state


def start_stub(plan, ledger_path, watch_manifest=None, questions=None):
    server, _state = make_stub(plan, ledger_path, watch_manifest, questions)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    return server


def _write_log(log_name, argv, result):
    logdir = os.path.join(VDIR, "logs")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, log_name + ".txt"), "w", encoding="utf-8") as fh:
        fh.write("argv: " + " ".join(str(a) for a in argv) + "\n")
        fh.write(f"exit: {result.returncode}\n\nSTDOUT\n{result.stdout}\n\nSTDERR\n{result.stderr}\n")


def run_m2_batch(out, questions, max_calls, extra=(), log_name=None):
    cmd = [sys.executable, os.path.join(ROOT, "m2_batch.py"),
           "--questions", questions, "--criteria", CRITERIA,
           "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT}",
           "--out", out, "--max-calls", str(max_calls)]
    result = subprocess.run(cmd + list(extra), cwd=ROOT, env=child_env(),
                            capture_output=True, text=True, encoding="utf-8", timeout=900)
    if log_name:
        _write_log(log_name, cmd + list(extra), result)
    return result


def review_tool(args, log_name=None):
    cmd = [sys.executable, os.path.join(ROOT, "tools", "review_m2.py")] + list(args)
    result = subprocess.run(cmd, cwd=ROOT, env=child_env(), capture_output=True, text=True,
                            encoding="utf-8", timeout=180)
    if log_name:
        _write_log(log_name, cmd, result)
    return result


def fresh(path):
    """把既有目录/文件改名隔离后重建。

    不删除任何东西：部分受管终端会给批量删除加闸门（会直接终止进程），
    改名既避免风险，也保留上一轮产物可追溯。
    """
    if not os.path.exists(path):
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i in range(1, 1000):
        target = f"{path}.prev-{stamp}-{i}"
        if not os.path.exists(target):
            os.rename(path, target)
            print(f"[fresh] 旧产物改名隔离（未删除）：{os.path.basename(target)}")
            return
    raise RuntimeError(f"无法隔离：{path}")


# --------------------------------------------------------------- A 题集与 T08

def scenario_a_integrity():
    doc = load_json(QUESTIONS)
    questions = doc["questions"]
    ids = [q["test_id"] for q in questions]
    check("题集完整性：30 个唯一题号且为 T01–T30", len(questions) == 30 and sorted(ids) == [f"T{i:02d}" for i in range(1, 31)])
    golds = {}
    for q in questions:
        golds[q["task_type_gold"]] = golds.get(q["task_type_gold"], 0) + 1
    check("题集完整性：三类各 10 题", golds == {"extract": 10, "qa_grounded": 10, "summary": 10}, str(golds))
    check("题集完整性：v2 版本标记", doc.get("version") == "test_v2")
    v1 = load_json(os.path.join(ROOT, "eval", "test_questions_v1.json"))
    v1_by = {q["test_id"]: q for q in v1["questions"]}
    diff = [q["test_id"] for q in questions if q != v1_by.get(q["test_id"])]
    check("题集完整性：v2 相对 v1 只改了 T08", diff == ["T08"], str(diff))
    dev = load_json(os.path.join(ROOT, "eval", "dev_questions.json"))
    dev_materials = {m for q in dev["questions"] for m in (q.get("materials") or [])}
    overlap = [q["test_id"] for q in questions if set(q.get("materials") or []) & dev_materials]
    check("题集完整性：与 dev 内容分离", not overlap, str(overlap))

    # T08 契约（13 号 R4）
    import grade
    criteria = load_json(CRITERIA)
    t08 = {q["test_id"]: q for q in questions}["T08"]
    r = grade.grade("extract", json.dumps(t08["answer_key"], ensure_ascii=False), t08, criteria)
    check("T08 契约：正确参考答案通过机检（原 v1 数组任务必被拒的问题已修）", r["mechanized_pass"] is True, r.get("reason"))
    r = grade.grade("extract", json.dumps({"campus": "北校区", "item": "舞台音响套装", "qty": 1, "budget_owner": "后勤保障科"}, ensure_ascii=False), t08, criteria)
    check("T08 契约：抽取错误对象（干扰项）不通过", r["mechanized_pass"] is False)
    return questions


# --------------------------------------------------------------- B 固定桩 90 项

def scenario_b_full_batch(questions):
    fresh(FULL_BATCH)
    ledger = os.path.join(VDIR, "full_capture.jsonl")
    fresh(ledger)
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": []}, ledger,
                        watch_manifest=os.path.join(FULL_BATCH, "batch_manifest.json"))
    try:
        result = run_m2_batch(FULL_BATCH, QUESTIONS, 90, log_name="full_batch_m2_batch")
        check("90 项固定桩批次：退出 0", result.returncode == 0, result.stderr[-300:] if result.returncode else "")
    finally:
        server.shutdown()
        server.server_close()

    manifest = load_json(os.path.join(FULL_BATCH, "batch_manifest.json"))
    attempts = read_jsonl(os.path.join(FULL_BATCH, "batch_attempts.jsonl"))
    starts = [a for a in attempts if a.get("kind") == "start"]
    ends = [a for a in attempts if a.get("kind") == "end"]
    check("90 项固定桩批次：90 start + 90 end", len(starts) == 90 and len(ends) == 90, f"start={len(starts)} end={len(ends)}")
    per_group = {g: set() for g in ("low", "high", "route")}
    for a in starts:
        per_group[a["group"]].add(a["test_id"])
    check("90 项固定桩批次：每组恰 30 题、每题三组齐全",
          all(len(v) == 30 for v in per_group.values()) and
          per_group["low"] == per_group["high"] == per_group["route"] == {q["test_id"] for q in questions})
    plan_ok = all(starts[i]["seq"] == i + 1 and starts[i]["test_id"] == manifest["plan"][i]["test_id"]
                  and starts[i]["group"] == manifest["plan"][i]["group"] for i in range(90))
    check("90 项固定桩批次：执行顺序与清单 90 项轮换逐项一致", plan_ok)

    captures = read_jsonl(ledger)
    check("90 项固定桩批次：桩捕获 90 条 HTTP（无应用缓存复用）", len(captures) == 90, f"实际 {len(captures)}")
    check("首次发送：第一条 HTTP 到达时批次清单已存在", all(c.get("manifest_present_at_http") for c in captures))

    by_id = {q["test_id"]: q for q in questions}
    groups_payloads = {}
    for i, cap in enumerate(captures):
        groups_payloads.setdefault(manifest["plan"][i]["test_id"], {})[manifest["plan"][i]["group"]] = cap["payload"]
    problems = []
    for tid, by_group in groups_payloads.items():
        base = by_group.get("low")
        if not base or set(by_group) != {"low", "high", "route"}:
            problems.append(f"{tid} 三组捕获不全")
            continue
        for g in ("high", "route"):
            if base.get("messages") != by_group[g].get("messages"):
                problems.append(f"{tid} {g} messages 与 low 不一致")
            for key in ("max_tokens", "temperature", "enable_thinking", "thinking_budget"):
                if base.get(key) != by_group[g].get(key):
                    problems.append(f"{tid} {g} 生成参数 {key} 不一致")
    check("实际输入一致：同题三组 messages 与生成参数一致（仅模型可不同）", not problems, "；".join(problems[:4]))

    leak = []
    for tid, by_group in groups_payloads.items():
        question = by_id[tid]
        parts = [question.get("question", "")]
        if question.get("materials"):
            parts.append("【资料】")
            for idx, mat in enumerate(question["materials"], 1):
                parts.append(f"[材料{idx}]\n{mat}")
        expected_user = "\n\n".join(parts)
        for g, payload in by_group.items():
            if (payload.get("messages") or [{}])[-1].get("content") != expected_user:
                leak.append(f"{tid} {g} user 内容与题面+材料不一致")
    check("实际输入一致：user 内容逐字等于题面+材料（隐藏判据未泄漏）", not leak, "；".join(leak[:4]))
    check("实际输入一致：全部摘要题长度要求进入 messages",
          all("总长度不超过" in (by_group["low"].get("messages") or [{}])[-1].get("content", "")
              for tid, by_group in groups_payloads.items() if by_id[tid]["task_type_gold"] == "summary"))


# --------------------------------------------------------------- C 正向通道（参考答案桩）

def scenario_c_positive_channel(questions):
    fresh(REF_BATCH)
    ledger = os.path.join(VDIR, "ref_capture.jsonl")
    fresh(ledger)
    by_id = {q["test_id"]: q for q in questions}
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": [], "reference_answers": True},
                        ledger, questions=by_id)
    try:
        result = run_m2_batch(REF_BATCH, QUESTIONS, 90, log_name="ref_batch_m2_batch")
        check("参考答案桩批次：退出 0（90 次回环 HTTP）", result.returncode == 0, result.stderr[-300:] if result.returncode else "")
    finally:
        server.shutdown()
        server.server_close()

    r = review_tool(["export", "--batch", REF_BATCH], log_name="positive_export")
    check("正向通道：export 通过身份核对", r.returncode == 0, r.stderr[-200:])
    sheet = load_json(os.path.join(REF_BATCH, "review", "review_sheet.json"))
    mapping = {m["review_id"]: m for m in load_json(os.path.join(REF_BATCH, "review", "mapping.json"))["items"]}
    manifest = load_json(os.path.join(REF_BATCH, "batch_manifest.json"))

    def entry_for(m, truth=True, facts=True, null_item=None):
        entry = {
            "review_id": m["review_id"], "batch_id": manifest["batch_id"],
            "request_id": m["request_id"], "test_id": m["test_id"],
            "answer_sha256": m["answer_sha256"],
            "questions_sha256": manifest["questions"]["sha256"],
            "criteria_sha256": manifest["criteria"]["sha256"],
            "human_items": {}, "reason": "verify 夹具", "reviewer": "verify", "reviewer_role": "codebuddy",
            "date": "2026-09-27",
        }
        gold = by_id[m["test_id"]]["task_type_gold"]
        if gold == "qa_grounded":
            entry["human_items"] = {k: truth for k in ("facts_correct", "no_unsupported_claim", "citation_supports")}
            if null_item:
                entry["human_items"][null_item] = None
        elif gold == "summary":
            entry["human_items"] = {"no_added_content_by_human": truth}
            entry["facts_covered_points"] = [facts] * len(by_id[m["test_id"]].get("summary_facts") or [])
        return entry

    def pick(tid, group="low"):
        return next(m for m in mapping.values() if m["test_id"] == tid and m["group"] == group)

    # 正向：qa / summary / extract 三类都要能进分子；另造一条 pending（有 null）验证状态区分
    reviews = [entry_for(pick("T11")), entry_for(pick("T21")), entry_for(pick("T01")),
               entry_for(pick("T15"), null_item="citation_supports")]
    path = os.path.join(VDIR, "ref_reviews_positive.json")
    dump_json(path, reviews)
    r = review_tool(["collect", "--batch", REF_BATCH, "--reviews", path], log_name="positive_collect")
    check("正向通道：合法评定通过 collect", r.returncode == 0, r.stderr[-200:])
    r = review_tool(["derive", "--batch", REF_BATCH], log_name="positive_derive")
    check("正向通道：derive 成功", r.returncode == 0, r.stderr[-200:])
    table = load_json(os.path.join(REF_BATCH, "derived", "per_question_table.json"))
    derived = load_json(os.path.join(REF_BATCH, "derived", "m2_derived.json"))
    check("正向通道：参考答案的抽取/问答/摘要三类都能进入达标分子",
          table["low"]["T01"]["final_pass"] and table["low"]["T11"]["final_pass"] and table["low"]["T21"]["final_pass"],
          f"T01={table['low']['T01']['final_pass']} T11={table['low']['T11']['final_pass']} T21={table['low']['T21']['final_pass']}")
    check("人工状态三分：有 null 待核项的题记 pending 且不达标",
          table["low"]["T15"]["human_status"] == "pending" and table["low"]["T15"]["final_pass"] is False
          and table["low"]["T15"]["human_pending_items"] == ["citation_supports"],
          f"status={table['low']['T15']['human_status']} pending={table['low']['T15']['human_pending_items']}")
    check("人工状态三分：未评定的题记 absent，已填完的记 filled",
          table["low"]["T12"]["human_status"] == "absent" and table["low"]["T11"]["human_status"] == "filled")
    check("派生汇总区分人工完成度（filled/pending/absent 计数可查）",
          derived["metrics"]["low"]["human_completion"]["filled"] >= 2
          and derived["metrics"]["low"]["human_completion"]["pending"] >= 1)
    return sheet, mapping, manifest


# --------------------------------------------------------------- D 路由隔离

def scenario_d_route_isolation(questions):
    by_id = {q["test_id"]: q for q in questions}
    q = by_id["T01"]

    def decide_of(question):
        return router.decide(m2_batch_run_text(question), question.get("constraints"), None,
                             instruction_text=question.get("question"))

    base = decide_of(q)
    variants = []
    v1 = json.loads(json.dumps(q)); v1["test_id"] = "Z99"; variants.append(v1)
    v2 = json.loads(json.dumps(q)); v2["task_type_gold"] = "summary"; variants.append(v2)
    v3 = json.loads(json.dumps(q)); v3["answer_key"] = {"x": "y"}; variants.append(v3)
    check("路由隔离：改题号/gold/answer_key 不改变路由决策",
          all(decide_of(v)["chosen_slot"] == base["chosen_slot"] and decide_of(v)["matched_rule"] == base["matched_rule"]
              for v in variants))
    import grade
    criteria = load_json(CRITERIA)
    r1 = grade.grade("extract", "{}", q, criteria)
    r2 = grade.grade("summary", "任意文本", q, criteria)
    check("路由隔离：gold 决定评分类别", r1["detail"].get("json_parsable") is not None and "chars" in r2["detail"])


def m2_batch_run_text(question):
    parts = [question.get("question", "")]
    if question.get("materials"):
        parts.append("【资料】")
        for index, material in enumerate(question["materials"], 1):
            parts.append(f"[材料{index}]\n{material}")
    return "\n\n".join(parts)


def make_small_questions(path):
    doc = load_json(QUESTIONS)
    keep = {"T01", "T02", "T11", "T15", "T21", "T25"}
    doc["questions"] = [q for q in doc["questions"] if q["test_id"] in keep]
    doc["version"] = "tmp_small_v2"
    dump_json(path, doc)
    return path


# --------------------------------------------------------------- E 上限/失败/未知

def scenario_e_cap_and_costs():
    small = make_small_questions(os.path.join(VDIR, "small_questions.json"))
    out = os.path.join(VDIR, "cap4_batch")
    fresh(out)
    ledger = os.path.join(VDIR, "cap4_capture.jsonl")
    fresh(ledger)
    server = start_stub({"fail_at": [2], "no_usage_at": [3], "hang_at": []}, ledger)
    try:
        result = run_m2_batch(out, small, 4, log_name="cap4_m2_batch")
        check("整批上限：cap=4 批次正常退出 0（额度用尽正常停止）", result.returncode == 0, result.stderr[-300:])
    finally:
        server.shutdown()
        server.server_close()

    attempts = read_jsonl(os.path.join(out, "batch_attempts.jsonl"))
    starts = [a for a in attempts if a.get("kind") == "start"]
    captures = read_jsonl(ledger)
    plan_count = load_json(os.path.join(out, "batch_manifest.json"))["plan_count"]
    check("整批上限：跨题跨组最多 4 次 HTTP，第 5 项无 start",
          len(starts) == 4 and len(captures) == 4 and plan_count == 18,
          f"start={len(starts)} http={len(captures)} plan={plan_count}")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("整批上限：汇总说明剩余计划项未执行",
          summary["remaining_items"] == 14 and summary["stop_reason"].startswith("已达整批调用上限"))
    reqs = read_jsonl(os.path.join(out, "raw", "high", "T01", "requests.jsonl"))
    check("失败占额度：注入 HTTP 500 的子运行记为 failed、无重试追加",
          len(reqs) == 1 and reqs[0]["run_status"] == "failed" and reqs[0]["failure_kind"] == "platform")
    route_reqs = read_jsonl(os.path.join(out, "raw", "route", "T01", "requests.jsonl"))
    check("未知费用：usage 缺失的尝试费用为 null、请求级总费用 null",
          route_reqs and route_reqs[0]["unknown_attempt_count"] == 1 and route_reqs[0]["total_cost_cny"] is None)
    check("未知费用：批次总费用 null、未知计数正确、不给出节省率结论",
          summary["cost_total"]["total_cost_cny"] is None and summary["cost_total"]["unknown_attempt_count"] == 2
          and "savings_rate" not in summary["cost_total"] and "saving_ratio" not in json.dumps(summary, ensure_ascii=False),
          f"unknown={summary['cost_total']['unknown_attempt_count']} total={summary['cost_total']['total_cost_cny']}")


# --------------------------------------------------------------- F 漂移与复用

def scenario_f_drift_and_reuse():
    ledger = os.path.join(VDIR, "reuse_capture.jsonl")
    fresh(ledger)
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": []}, ledger)
    try:
        small = os.path.join(VDIR, "small_questions.json")
        result = run_m2_batch(os.path.join(VDIR, "cap4_batch"), small, 4, log_name="reuse_rejected_m2_batch")
        check("重入拒绝：复用已有批次目录被拒绝（退出 3）", result.returncode == 3, f"exit={result.returncode}")
        check("重入拒绝：拒绝时零新增 HTTP", len(read_jsonl(ledger)) == 0)
    finally:
        server.shutdown()
        server.server_close()

    tmp_q = os.path.join(VDIR, "drift_q.json")
    tmp_p = os.path.join(VDIR, "drift_p.json")
    tmp_c = os.path.join(VDIR, "drift_code.py")
    shutil.copyfile(QUESTIONS, tmp_q)
    shutil.copyfile(PRICES, tmp_p)
    with open(tmp_c, "w", encoding="utf-8") as fh:
        fh.write("# code\n")
    fake = {
        "questions": {"sha256": sha256_file(tmp_q)}, "criteria": {"sha256": sha256_file(CRITERIA)},
        "prices": {"sha256": sha256_file(tmp_p)},
        "code": {"runs/_verify_m2/drift_code.py": sha256_file(tmp_c)},
        "_questions_path": tmp_q, "_criteria_path": CRITERIA, "_prices_path": tmp_p,
        "_manifest_path": tmp_c, "_manifest_sha256": sha256_file(tmp_c),
    }
    check("漂移检查：开工态无漂移", m2_batch.drift_check(fake) == [])
    with open(tmp_q, "a", encoding="utf-8") as fh:
        fh.write(" ")
    with open(tmp_c, "w", encoding="utf-8") as fh:
        fh.write("# changed\n")
    problems = m2_batch.drift_check(fake)
    check("漂移检查：题集与代码漂移被捕获（含清单本身）",
          any("questions" in p for p in problems) and any("drift_code" in p for p in problems), str(problems))


# --------------------------------------------------------------- G 中断 + 未闭合费用 + 表补全

def scenario_g_interrupt():
    small = os.path.join(VDIR, "small_questions.json")
    out = os.path.join(VDIR, "hang_batch")
    fresh(out)
    ledger = os.path.join(VDIR, "hang_capture.jsonl")
    fresh(ledger)
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": [2]}, ledger)
    proc = None
    try:
        cmd = [sys.executable, os.path.join(ROOT, "m2_batch.py"),
               "--questions", small, "--criteria", CRITERIA, "--target", "stub",
               "--base-url", f"http://127.0.0.1:{PORT}", "--out", out, "--max-calls", "2"]
        proc = subprocess.Popen(cmd, cwd=ROOT, env=child_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        time.sleep(6)
    finally:
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        server.shutdown()
        server.server_close()

    attempts = read_jsonl(os.path.join(out, "batch_attempts.jsonl"))
    starts = [a for a in attempts if a.get("kind") == "start"]
    ends = [a for a in attempts if a.get("kind") == "end"]
    check("中断对账：父层留下 2 条占用（第 2 条发送状态未知）", len(starts) == 2 and len(ends) == 1,
          f"start={len(starts)} end={len(ends)}")
    sub_events = read_jsonl(os.path.join(out, "raw", "high", "T01", "attempt_events.jsonl"))
    check("中断对账：被挂住的子运行留下未闭合 start、没有请求行",
          len([e for e in sub_events if e.get("kind") == "start"]) >= 1
          and len(read_jsonl(os.path.join(out, "raw", "high", "T01", "requests.jsonl"))) == 0)
    check("中断后重启：默认拒绝继续（退出 3）",
          run_m2_batch(out, small, 2, log_name="hang_restart_rejected").returncode == 3)

    # 未闭合批次：派生费用必须为 null 且表补齐全部计划行（13 号 R2）
    r = review_tool(["export", "--batch", out, "--questions", small], log_name="hang_export")
    check("中断批次：export 仍可用（未闭合不算身份失败）", r.returncode == 0, r.stderr[-200:])
    empty = os.path.join(VDIR, "hang_empty_reviews.json")
    dump_json(empty, [])
    check("中断批次：collect 空评定可收集",
          review_tool(["collect", "--batch", out, "--reviews", empty, "--questions", small],
                      log_name="hang_collect").returncode == 0)
    r = review_tool(["derive", "--batch", out, "--questions", small], log_name="hang_derive")
    derived = load_json(os.path.join(out, "derived", "m2_derived.json"))
    cost = derived["cost_total"]
    check("未闭合费用：未知 >= 1 且总费用为 null（缺请求行不决定计账）",
          cost["unknown_attempt_count"] >= 1 and cost["total_cost_cny"] is None, json.dumps(cost, ensure_ascii=False)[:200])
    table = load_json(os.path.join(out, "derived", "per_question_table.json"))
    planned = load_json(os.path.join(out, "batch_manifest.json"))["planned_questions"]
    check("不完整批次逐题表：每组表包含全部计划题号",
          all(set(table[g]) == set(planned) for g in table),
          f"planned={len(planned)} counts={ {g: len(table[g]) for g in table} }")
    check("不完整批次逐题表：未执行项显式标状态而非缺行",
          table["low"]["T02"]["state"] == "not_executed" and table["high"]["T01"]["state"] == "interrupted",
          f"low/T02={table['low']['T02']['state']} high/T01={table['high']['T01']['state']}")


# --------------------------------------------------------------- H 四种身份反例

def scenario_h_identity_refusals(questions):
    manifest = load_json(os.path.join(REF_BATCH, "batch_manifest.json"))
    mapping = {m["review_id"]: m for m in load_json(os.path.join(REF_BATCH, "review", "mapping.json"))["items"]}
    pick = lambda tid, group="low": next(m for m in mapping.values() if m["test_id"] == tid and m["group"] == group)

    def clone(name):
        out = os.path.join(VDIR, name)
        fresh(out)
        shutil.copytree(REF_BATCH, out)
        return out

    def entry_for(m, truth=True):
        gold = {q["test_id"]: q for q in questions}[m["test_id"]]["task_type_gold"]
        entry = {
            "review_id": m["review_id"], "batch_id": manifest["batch_id"],
            "request_id": m["request_id"], "test_id": m["test_id"],
            "answer_sha256": m["answer_sha256"],
            "questions_sha256": manifest["questions"]["sha256"],
            "criteria_sha256": manifest["criteria"]["sha256"],
            "human_items": {}, "reason": "verify 反例", "reviewer": "verify", "reviewer_role": "codebuddy",
            "date": "2026-09-27",
        }
        if gold == "qa_grounded":
            entry["human_items"] = {k: truth for k in ("facts_correct", "no_unsupported_claim", "citation_supports")}
        elif gold == "summary":
            entry["human_items"] = {"no_added_content_by_human": truth}
            entry["facts_covered_points"] = [truth] * len({q["test_id"]: q for q in questions}[m["test_id"]].get("summary_facts") or [])
        return entry

    # 反例 1：收集后改答案字节（写空）→ derive 必须拒绝
    b1 = clone("identity_answer_changed")
    good = os.path.join(VDIR, "identity_good_reviews.json")
    dump_json(good, [entry_for(pick("T11")), entry_for(pick("T21"))])
    assert review_tool(["collect", "--batch", b1, "--reviews", good], log_name="identity1_collect").returncode == 0
    with open(os.path.join(b1, "raw", "low", "T11", "answers", "T11.txt"), "wb") as fh:
        fh.write(b"")
    r = review_tool(["derive", "--batch", b1], log_name="identity1_derive_rejected")
    check("身份反例①：收集后答案字节被改 → derive 拒绝", r.returncode != 0, f"exit={r.returncode}")

    # 反例 2：换用不同题集（T21 增加第 5 个事实点）→ derive 必须拒绝
    b2 = clone("identity_questions_changed")
    assert review_tool(["collect", "--batch", b2, "--reviews", good], log_name="identity2_collect").returncode == 0
    qdoc = load_json(QUESTIONS)
    for q in qdoc["questions"]:
        if q["test_id"] == "T21":
            q["summary_facts"] = list(q["summary_facts"]) + ["新增夹具事实点"]
    changed = os.path.join(b2, "changed_questions.json")
    dump_json(changed, qdoc)
    r = review_tool(["derive", "--batch", b2, "--questions", changed], log_name="identity2_derive_rejected")
    check("身份反例②：换用不同题集（事实点 5 个）→ derive 拒绝", r.returncode != 0, f"exit={r.returncode}")

    # 反例 3：手改 validated_reviews（全 false 改成 true）→ derive 必须拒绝
    b3 = clone("identity_validated_edited")
    false_reviews = os.path.join(VDIR, "identity_false_reviews.json")
    dump_json(false_reviews, [entry_for(pick("T11"), truth=False)])
    assert review_tool(["collect", "--batch", b3, "--reviews", false_reviews], log_name="identity3_collect").returncode == 0
    vpath = os.path.join(b3, "review", "validated_reviews.json")
    vdoc = load_json(vpath)
    vdoc["reviews"][0]["human_items"] = {k: True for k in vdoc["reviews"][0]["human_items"]}
    dump_json(vpath, vdoc)
    r = review_tool(["derive", "--batch", b3], log_name="identity3_derive_rejected")
    check("身份反例③：手改 validated_reviews → derive 拒绝（要求重新 collect）", r.returncode != 0, f"exit={r.returncode}")

    # 反例 4：评定身份字段与映射矛盾 → collect 必须拒绝
    b4 = clone("identity_wrong_fields")
    bad = entry_for(pick("T11"))
    bad.update(batch_id="OTHER_BATCH", request_id="high-T30", test_id="T30")
    bad_path = os.path.join(VDIR, "identity_wrong_fields.json")
    dump_json(bad_path, [bad])
    r = review_tool(["collect", "--batch", b4, "--reviews", bad_path], log_name="identity4_collect_rejected")
    check("身份反例④：batch/request/test 与映射矛盾 → collect 拒绝", r.returncode != 0, f"exit={r.returncode}")

    # 反例 5（补充）：mapping 在收集后被替换 → derive 拒绝
    b5 = clone("identity_mapping_changed")
    assert review_tool(["collect", "--batch", b5, "--reviews", good], log_name="identity5_collect").returncode == 0
    mpath = os.path.join(b5, "review", "mapping.json")
    mdoc = load_json(mpath)
    mdoc["items"] = mdoc["items"][:-1]
    dump_json(mpath, mdoc)
    r = review_tool(["derive", "--batch", b5], log_name="identity5_derive_rejected")
    check("身份反例⑤：mapping 在收集后被改动 → derive 拒绝", r.returncode != 0, f"exit={r.returncode}")

    # 15 号 R1 收尾：收集前就错误的 mapping 不能成为被哈希绑定的可信依据。
    for key, wrong in (("answer_sha256", "0" * 64), ("test_id", "T12")):
        b6 = clone("identity_mapping_before_collect_" + key)
        mpath = os.path.join(b6, "review", "mapping.json")
        mdoc = load_json(mpath)
        row = next(m for m in mdoc["items"] if m["request_id"] == "low-T11")
        row[key] = wrong
        dump_json(mpath, mdoc)
        bad = entry_for(pick("T11"))
        bad[key] = wrong
        bad_path = os.path.join(b6, "wrong_mapping_reviews.json")
        dump_json(bad_path, [bad])
        r = review_tool(["collect", "--batch", b6, "--reviews", bad_path],
                        log_name="mapping_before_collect_" + key + "_collect")
        check(f"收集前错映射 {key}：collect 按原始身份拒绝",
              r.returncode == 1 and "mapping 与原始请求/实际答案" in r.stderr, f"exit={r.returncode}")
        r = review_tool(["derive", "--batch", b6], log_name="mapping_before_collect_" + key + "_derive")
        check(f"收集前错映射 {key}：derive 同样按原始身份拒绝",
              r.returncode == 1 and "mapping 与原始请求/实际答案" in r.stderr, f"exit={r.returncode}")


# --------------------------------------------------------------- I 子异常立即停止

def scenario_i_child_exception_stop():
    small = make_small_questions(os.path.join(VDIR, "small_questions.json"))
    out = os.path.join(VDIR, "child_break_batch")
    fresh(out)
    ledger = os.path.join(VDIR, "child_break_capture.jsonl")
    fresh(ledger)
    break_dir = os.path.join(out, "raw", "low", "T01", "answers", "T01.txt")
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": [], "break_answer_dir": break_dir}, ledger)
    try:
        result = run_m2_batch(out, small, 3, log_name="child_break_m2_batch")
    finally:
        server.shutdown()
        server.server_close()

    captures = read_jsonl(ledger)
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("子运行异常：只发出第一次 HTTP 后立即停止后续发送",
          len(captures) == 1 and result.returncode == 5, f"http={len(captures)} exit={result.returncode}")
    check("子运行异常：stop_reason 写明 child_error/child_unknown 与剩余计划项",
          ("child_error" in summary["stop_reason"] or "child_unknown" in summary["stop_reason"])
          and summary["remaining_items"] == 17 and summary["executed_items"] == 1,
          summary["stop_reason"][:120])

    event_cost = 0.0
    for root, _dirs, files in os.walk(os.path.join(out, "raw")):
        if "attempt_events.jsonl" in files:
            for e in read_jsonl(os.path.join(root, "attempt_events.jsonl")):
                if e.get("kind") == "end" and e.get("cost_cny") is not None:
                    event_cost += e["cost_cny"]
    check("子运行异常：事件级已知费用不丢（批次费用 == 子 ledger 结束事件之和）",
          round(event_cost, 10) == summary["cost_total"]["known_cost_cny"],
          f"ledger={round(event_cost, 10)} batch={summary['cost_total']['known_cost_cny']}")


# --------------------------------------------------------------- K 分母与统计

def scenario_k_denominators():
    check("固定分母与延迟：偶数样本 p50 取中间两项均值",
          m2_batch.median([1, 2, 3, 4]) == 2.5 and m2_batch.median([1, 2, 3]) == 2)
    summary = load_json(os.path.join(FULL_BATCH, "batch_summary.json"))
    check("固定分母与延迟：一组含失败/未执行仍分母 30",
          all(summary["per_group"][g]["end_to_end"]["denominator"] == 30 for g in ("low", "high", "route")))
    derived = load_json(os.path.join(REF_BATCH, "derived", "m2_derived.json"))
    check("固定分母与延迟：类别分母 10",
          all(derived["metrics"][g]["by_category"][c]["denominator"] == 10
              for g in ("low", "high", "route") for c in ("extract", "qa_grounded", "summary")))
    check("费用口径：已知/未知/总费用三者在派生结果里同时披露",
          all(k in derived["cost_total"] for k in ("known_cost_cny", "unknown_attempt_count", "total_cost_cny"))
          and derived["cost_total"]["occurred_cost_note"]
          and derived["cost_total"]["complete_experiment_note"])


# --------------------------------------------------------------- L 真实模式零调用准备（18 号）
# 说明：本场景只做离线验证 —— 门禁用纯函数 + 子进程 CLI 拒绝，委托参数用纯函数断言，
# 真实模式的主循环用**本地替身 runner**（不建立任何网络连接）跑通。
# 夹具批准记录是本地测试件，不是用户授权；测试进程不设置任何凭据环境变量。

FIX_DIR = os.path.join(VDIR, "real_mode")
SMALL_Q = os.path.join(VDIR, "small_questions.json")
REAL_BASE_URL = "https://api.siliconflow.cn"
FIXTURE_CREDENTIAL = "fixture-not-a-real-credential"


def fixture_scope(questions_path, out_dir, max_calls, base_url=REAL_BASE_URL,
                  credential_env="ROUTER_API_KEY", timeout=180.0, enable_thinking="false"):
    prices = load_json(PRICES)
    slots = prices.get("slots") or {}
    plan = m2_batch.build_plan(load_json(questions_path)["questions"])
    generation = {"temperature": 0.0, "max_tokens": 512, "timeout_seconds": timeout,
                  "enable_thinking": enable_thinking, "thinking_budget": None}
    scope = m2_batch.approval_scope(questions_path, CRITERIA, PRICES, out_dir, max_calls, base_url,
                                    {"low": slots.get("low"), "high": slots.get("high")}, generation,
                                    credential_env, len(plan))
    return scope, plan


def fixture_record(scope, **overrides):
    record = {
        "approval_version": m2_batch.APPROVAL_VERSION,
        "approval_id": "verify-fixture-18",
        "status": "APPROVED",
        "approved_by": "verify-fixture-not-a-real-approval",
        "approved_at": "2026-09-27T00:00:00+08:00",
        "approval_basis": "本地门禁测试夹具，不是用户授权",
        "terms": {"no_auto_retry": True, "stop_on_unknown_cost": True, "stop_before_next_send": True},
        "budget": {"intention_cny": 10.0, "known_cost_stop_cny": 8.0},
        "scope": json.loads(json.dumps(scope)),
    }
    record.update(overrides)
    return record


def fake_account(known=0.0, unknown=0):
    return {"totals": {"known_cost_cny": known, "unknown_attempt_count": unknown,
                       "total_cost_cny": None if unknown else known}}


class ShimRunner:
    """本地替身 runner：只记录 argv 并写最小子产物；不建立任何网络连接。"""

    def __init__(self, behavior="complete"):
        self.behavior = behavior
        self.calls = []

    @staticmethod
    def _pairs(argv):
        out = {}
        for index, token in enumerate(argv):
            if token.startswith("--"):
                out[token[2:]] = argv[index + 1] if index + 1 < len(argv) else None
        return out

    def main(self, argv):
        self.calls.append(list(argv))
        ns = self._pairs(argv)
        out_dir = ns["out"]
        os.makedirs(out_dir, exist_ok=True)
        if self.behavior == "child_error":
            return 5
        slot, test_id = ns["slot"], ns["ids"]
        request_id = f"{slot}-{test_id}"
        model = ns["model-low"] if slot != "high" else ns["model-high"]
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "cached_tokens": 0}
        if self.behavior == "http_401":
            status, usage, usage_known, cost = "error", None, False, None
            run_status, failure_kind, notes = "failed", "platform", "HTTP 401"
        elif self.behavior == "no_usage":
            status, usage, usage_known, cost = "ok", None, False, None
            run_status, failure_kind, notes = "ok", None, ""
        elif self.behavior == "expensive":
            status, usage_known, cost, run_status, failure_kind, notes = "ok", True, 4.0, "ok", None, ""
        else:
            status, usage_known, cost, run_status, failure_kind, notes = "ok", True, 0.00004, "ok", None, ""

        events = [
            {"kind": "start", "request_id": request_id, "attempt_no": 1, "model": model, "slot": slot,
             "ts": "2026-09-27T00:00:00+08:00"},
            {"kind": "end", "request_id": request_id, "attempt_no": 1, "model": model, "slot": slot,
             "status": status, "usage": usage, "usage_known": usage_known, "cost_cny": cost,
             "latency_ms": 12, "ts": "2026-09-27T00:00:01+08:00"},
        ]
        with open(os.path.join(out_dir, "attempt_events.jsonl"), "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.makedirs(os.path.join(out_dir, "answers"), exist_ok=True)
        with open(os.path.join(out_dir, "answers", f"{test_id}.txt"), "wb") as fh:
            fh.write(b"shim")
        attempt = contract.new_attempt(attempt_no=1, model=model, slot=slot, status=status, usage=usage,
                                       usage_known=usage_known, unit_price_version="prices_v1",
                                       cost_cny=cost, latency_ms=12, offline_eval=False)
        unknown = 0 if cost is not None else 1
        request = contract.new_request(
            request_id=request_id, ts="2026-09-27T00:00:01+08:00", group=slot, task_type_pred="extract",
            task_type_pred_source="rule", test_id=test_id, input_ref={"text_sha256": "0" * 64},
            attempts=[attempt], run_status=run_status, total_cost_cny=None if unknown else cost,
            known_cost_cny=0.0 if unknown else cost, unknown_attempt_count=unknown, latency_ms_total=12,
            quality={"overall_pass": run_status == "ok", "mechanized_pass": run_status == "ok",
                     "needs_human": False, "reason": "shim", "detail": {}},
            task_type_gold="extract", failure_kind=failure_kind, notes=notes,
            final_output_ref=f"answers/{test_id}.txt")
        request["answer_sha256"] = "0" * 64
        with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(request, ensure_ascii=False) + "\n")
        return 0


def run_real_cli(out, record, extra=(), base_url=REAL_BASE_URL, max_calls="18", log_name=None):
    cmd = [sys.executable, os.path.join(ROOT, "m2_batch.py"),
           "--questions", SMALL_Q, "--criteria", CRITERIA, "--prices", PRICES,
           "--target", "real", "--base-url", base_url, "--out", out, "--max-calls", max_calls,
           "--timeout", "180", "--enable-thinking", "false"]
    if record:
        cmd += ["--approval-record", record]
    result = subprocess.run(cmd + list(extra), cwd=ROOT, env=child_env(), capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    if log_name:
        _write_log(log_name, cmd + list(extra), result)
    return result


def run_real_batch_case(name, behavior, max_calls=18):
    """真实模式主循环 + 本地替身 runner（零网络）；夹具记录只放在测试目录。"""
    out = os.path.join(FIX_DIR, "batches", name)
    fresh(out)
    scope, _plan = fixture_scope(SMALL_Q, out, max_calls)
    record_path = os.path.join(FIX_DIR, name + "_approval.json")
    dump_json(record_path, fixture_record(scope))
    argv = ["--questions", SMALL_Q, "--criteria", CRITERIA, "--prices", PRICES, "--target", "real",
            "--confirm-real", "--approval-record", record_path, "--base-url", REAL_BASE_URL,
            "--out", out, "--max-calls", str(max_calls), "--timeout", "180", "--enable-thinking", "false"]
    shim = ShimRunner(behavior)
    original = m2_batch.runner_mod
    m2_batch.runner_mod = shim
    try:
        code = m2_batch.main(argv, env_lookup=lambda _name: FIXTURE_CREDENTIAL)
    finally:
        m2_batch.runner_mod = original
    return out, code, shim, record_path


def scenario_l_real_mode_prep():
    os.makedirs(FIX_DIR, exist_ok=True)
    make_small_questions(SMALL_Q)
    out_ok = os.path.join(FIX_DIR, "batch_ok")
    base_scope, plan = fixture_scope(SMALL_Q, out_ok, 18)

    counter = {"n": 0}

    def gate(name, mutate=None, fragment=None, expect_ok=False, confirm=True, env_value=FIXTURE_CREDENTIAL,
             out_dir=out_ok, drop_record=False, raw_text=None):
        record = fixture_record(base_scope)
        if mutate:
            mutate(record)
        counter["n"] += 1
        path = os.path.join(FIX_DIR, f"gate_case_{counter['n']:02d}.json")
        if drop_record:
            pass
        elif raw_text is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw_text)
        else:
            dump_json(path, record)
        problems, _doc, _sha = m2_batch.real_gate_problems(path, base_scope, out_dir, confirm, "ROUTER_API_KEY",
                                                           env_lookup=lambda _name: env_value)
        ok = (not problems) if expect_ok else bool(problems) and (fragment is None
                                                                  or any(fragment in p for p in problems))
        check(f"真实门禁：{name}", ok, "；".join(problems)[:300] if not ok else "")

    gate("完整批准记录（本地夹具）", expect_ok=True)
    gate("草案未批准", lambda r: r.update(status="PENDING_USER_APPROVAL"), "APPROVED")
    gate("缺 approved_by", lambda r: r.update(approved_by=None), "approved_by")
    gate("缺 approved_at", lambda r: r.update(approved_at=""), "approved_at")
    gate("缺 approval_id", lambda r: r.update(approval_id=None), "approval_id")
    gate("版本不支持", lambda r: r.update(approval_version="other_version"), "approval_version")
    gate("未确认不自动重试条款", lambda r: r["terms"].update(no_auto_retry=False), "no_auto_retry")
    gate("缺条款对象", lambda r: r.pop("terms"), "terms")
    gate("缺预算", lambda r: r.pop("budget"), "budget")
    gate("停线高于意向金额", lambda r: r["budget"].update(known_cost_stop_cny=12.0), "不能大于")
    gate("整批上限不一致（记录 89 / 实际 18）", lambda r: r["scope"].update(max_calls=89), "max_calls")
    gate("题集哈希不一致", lambda r: r["scope"]["questions"].update(sha256="0" * 64), "questions")
    gate("判据哈希不一致", lambda r: r["scope"]["criteria"].update(sha256="0" * 64), "criteria")
    gate("价格哈希不一致", lambda r: r["scope"]["prices"].update(sha256="0" * 64), "prices")
    gate("代码哈希不一致", lambda r: r["scope"]["code"].update({"m2_batch.py": "0" * 64}), "code")
    gate("生成参数不一致（超时 30 秒）",
         lambda r: r["scope"]["generation"].update(timeout_seconds=30.0), "generation")
    gate("生成参数不一致（思考开关 default）",
         lambda r: r["scope"]["generation"].update(enable_thinking="default"), "generation")
    gate("模型不一致", lambda r: r["scope"]["models"].update(low="Other/model"), "models")
    gate("目标地址不一致", lambda r: r["scope"].update(base_url="https://api.example.com"), "base_url")
    gate("输出目录不一致",
         lambda r: r["scope"].update(output_dir=os.path.join(FIX_DIR, "other_batch")), "output_dir")
    gate("凭据环境变量名不一致", lambda r: r["scope"].update(credential_env="OTHER_KEY"), "credential_env")
    gate("计划项数不一致（记录 90 / 实际 18）", lambda r: r["scope"].update(plan_count=90), "plan_count")
    gate("组别声明不一致", lambda r: r["scope"].update(groups=["low", "high"]), "groups")
    gate("缺 scope", lambda r: r.pop("scope"), "缺 scope")
    gate("记录放在输出目录内", out_dir=FIX_DIR, fragment="输出目录内")
    gate("记录文件不存在", drop_record=True, fragment="不存在")
    gate("记录不是合法 JSON", raw_text="{not json", fragment="合法 JSON")
    gate("缺 --confirm-real", confirm=False, fragment="confirm-real")
    gate("凭据环境变量为空", env_value=None, fragment="为空")
    gate("凭据含换行", env_value="fixture-x\n", fragment="换行")

    # ---------- CLI 级拒绝（子进程；child_env 已移除凭据类环境变量） ----------
    def cli_record(name, out_dir, base_url=REAL_BASE_URL, **overrides):
        """按该用例的输出目录生成"记录本身合法"的批准记录，让拒绝原因只来自被考察的那个条件。"""
        scope, _plan = fixture_scope(SMALL_Q, out_dir, 18, base_url=base_url)
        path = os.path.join(FIX_DIR, name + "_approval.json")
        dump_json(path, fixture_record(scope, **overrides))
        return path

    out = os.path.join(FIX_DIR, "cli_no_record_batch")
    fresh(out)
    r = run_real_cli(out, None, extra=("--confirm-real",), log_name="real_cli_no_record")
    check("真实 CLI：缺 --approval-record 拒绝且不创建批次目录",
          r.returncode == 2 and "approval-record" in r.stderr and not os.path.exists(out), r.stderr[-200:])

    out = os.path.join(FIX_DIR, "cli_no_confirm_batch")
    fresh(out)
    r = run_real_cli(out, cli_record("cli_no_confirm", out), log_name="real_cli_no_confirm")
    check("真实 CLI：缺 --confirm-real 拒绝（记录与其余参数都匹配）",
          r.returncode == 2 and "confirm-real" in r.stderr and "不一致" not in r.stderr, r.stderr[-200:])

    out = os.path.join(FIX_DIR, "cli_no_credential_batch")
    fresh(out)
    r = run_real_cli(out, cli_record("cli_no_credential", out), extra=("--confirm-real",),
                     log_name="real_cli_no_credential")
    check("真实 CLI：记录与参数全部通过、仅凭据环境变量为空 → 首次发送前拒绝、不建目录",
          r.returncode == 2 and "为空" in r.stderr and "不一致" not in r.stderr and not os.path.exists(out),
          r.stderr[-300:])

    out = os.path.join(FIX_DIR, "cli_loopback_batch")
    fresh(out)
    r = run_real_cli(out, None, extra=("--confirm-real",), base_url="http://127.0.0.1:5311",
                     log_name="real_cli_loopback")
    check("真实 CLI：真实模式指向回环地址被拒绝（在任何记录检查之前）",
          r.returncode == 2 and "回环" in r.stderr and "不一致" not in r.stderr, r.stderr[-200:])

    out = os.path.join(FIX_DIR, "cli_cap_mismatch_batch")
    fresh(out)
    r = run_real_cli(out, cli_record("cli_cap_mismatch", out), extra=("--confirm-real",), max_calls="90",
                     log_name="real_cli_cap_mismatch")
    check("真实 CLI：--max-calls 与批准记录不一致被拒绝",
          r.returncode == 2 and "max_calls" in r.stderr, r.stderr[-200:])

    out = os.path.join(FIX_DIR, "cli_draft_batch")
    fresh(out)
    r = run_real_cli(out, cli_record("cli_draft", out, status="PENDING_USER_APPROVAL", approved_by=None,
                                     approved_at=None), extra=("--confirm-real",), log_name="real_cli_draft")
    check("真实 CLI：PENDING_USER_APPROVAL 草案不能执行",
          r.returncode == 2 and "APPROVED" in r.stderr, r.stderr[-200:])

    out = os.path.join(FIX_DIR, "cli_inside_batch")
    fresh(out)
    os.makedirs(out, exist_ok=True)
    inside = os.path.join(out, "approval.json")
    dump_json(inside, fixture_record(fixture_scope(SMALL_Q, out, 18)[0]))
    r = run_real_cli(out, inside, extra=("--confirm-real",), log_name="real_cli_inside_out")
    check("真实 CLI：批准记录放在批次输出目录内被拒绝",
          r.returncode == 2 and "输出目录内" in r.stderr, r.stderr[-200:])

    # ---------- 委托参数（纯函数，唯一构造点） ----------
    cfg = {"questions": SMALL_Q, "criteria": CRITERIA, "target": "real", "base_url": REAL_BASE_URL,
           "prices": PRICES, "model_low": "Qwen/Qwen3.5-27B", "model_high": "zai-org/GLM-5.3",
           "timeout": 180.0, "max_tokens": 512, "temperature": 0.0, "enable_thinking": "false",
           "thinking_budget": None, "api_key_env": "ROUTER_API_KEY"}
    item = {"test_id": "T01", "group": "low"}
    expected_real = ["--questions", SMALL_Q, "--criteria", CRITERIA, "--target", "real",
                     "--base-url", REAL_BASE_URL, "--prices", PRICES,
                     "--model-low", "Qwen/Qwen3.5-27B", "--model-high", "zai-org/GLM-5.3",
                     "--timeout", "180.0", "--max-tokens", "512", "--temperature", "0.0",
                     "--enable-thinking", "false", "--ids", "T01", "--slot", "low",
                     "--out", os.path.join("runs", "x", "raw", "low", "T01"), "--max-calls", "1",
                     "--confirm-real", "--api-key-env", "ROUTER_API_KEY"]
    check("委托参数：real 路径逐项符合候选配置（含 real 确认与凭据环境变量名）",
          m2_batch.child_argv_for(item, cfg, os.path.join("runs", "x", "raw", "low", "T01")) == expected_real,
          json.dumps(m2_batch.child_argv_for(item, cfg, os.path.join("runs", "x", "raw", "low", "T01")),
                     ensure_ascii=False))
    stub_cfg = dict(cfg, target="stub", base_url=f"http://127.0.0.1:{PORT}")
    stub_argv = m2_batch.child_argv_for(item, stub_cfg, os.path.join("runs", "x", "raw", "low", "T01"))
    check("委托参数：stub 路径不带 real 确认与凭据环境变量名",
          "--confirm-real" not in stub_argv and "--api-key-env" not in stub_argv
          and stub_argv[stub_argv.index("--target") + 1] == "stub")
    check("委托参数：未指定 thinking_budget 时不发送该字段",
          "--thinking-budget" not in expected_real and "--thinking-budget" in m2_batch.child_argv_for(
              item, dict(cfg, thinking_budget=1024), os.path.join("runs", "x")))

    # ---------- 候选生成参数确实进入实际 payload（桩批次） ----------
    out = os.path.join(VDIR, "candidate_params_batch")
    fresh(out)
    ledger = os.path.join(VDIR, "candidate_params_capture.jsonl")
    fresh(ledger)
    server = start_stub({"fail_at": [], "no_usage_at": [], "hang_at": []}, ledger)
    try:
        result = run_m2_batch(out, SMALL_Q, 4, extra=("--timeout", "180", "--enable-thinking", "false"),
                              log_name="candidate_params_m2_batch")
        check("候选参数：桩批次退出 0（4 次回环 HTTP）", result.returncode == 0, result.stderr[-200:])
    finally:
        server.shutdown()
        server.server_close()
    payloads = [cap["payload"] for cap in read_jsonl(ledger)]
    check("候选参数：enable_thinking=false 确实进入实际 payload",
          len(payloads) == 4 and all(p.get("enable_thinking") is False for p in payloads),
          json.dumps(payloads[:1], ensure_ascii=False)[:200])
    check("候选参数：不发送 thinking_budget、max_tokens=512、temperature=0",
          all("thinking_budget" not in p and p.get("max_tokens") == 512 and p.get("temperature") == 0.0
              for p in payloads))
    manifest = load_json(os.path.join(out, "batch_manifest.json"))
    check("候选参数：客户端超时 180 秒与思考开关进入事前清单",
          manifest["generation"]["timeout_seconds"] == 180 and manifest["generation"]["enable_thinking"] == "false")

    # ---------- 真实停线判定（纯函数逐条） ----------
    check("真实停线：无异常时不停", m2_batch.post_child_stop_reason(fake_account(0.5), None, 8.0) is None)
    check("真实停线：出现未知费用即停",
          (m2_batch.post_child_stop_reason(fake_account(0.5, 1), None, 8.0) or ("", ""))[0] == "unknown_cost")
    check("真实停线：已知费用达到批准停线即停",
          (m2_batch.post_child_stop_reason(fake_account(8.0), None, 8.0) or ("", ""))[0] == "known_cost_stop_line")
    check("真实停线：已知费用未达停线继续", m2_batch.post_child_stop_reason(fake_account(7.99), None, 8.0) is None)
    check("真实停线：平台 401 按认证失败停止",
          "认证失败" in (m2_batch.platform_failure_stop({"failure_kind": "platform", "notes": "HTTP 401"}) or ""))
    check("真实停线：平台 429 按限流停止",
          "限流" in (m2_batch.platform_failure_stop({"failure_kind": "platform", "notes": "HTTP 429"}) or ""))
    check("真实停线：平台 403/400 也停止",
          bool(m2_batch.platform_failure_stop({"failure_kind": "platform", "notes": "HTTP 403"}))
          and bool(m2_batch.platform_failure_stop({"failure_kind": "platform", "notes": "HTTP 400"})))
    check("真实停线：普通 5xx 不算平台停线（记 failed 占额度，由未知费用兜底）",
          m2_batch.platform_failure_stop({"failure_kind": "platform", "notes": "HTTP 500"}) is None
          and m2_batch.post_child_stop_reason(fake_account(0.1), {"failure_kind": "platform", "notes": "HTTP 500"}, 8.0) is None)
    check("真实停线：超时本身不是平台停线（由未知费用兜底）",
          m2_batch.platform_failure_stop({"failure_kind": "timeout", "notes": "请求超时"}) is None
          and (m2_batch.post_child_stop_reason(fake_account(0.0, 1), {"failure_kind": "timeout"}, 8.0) or ("", ""))[0]
          == "unknown_cost")

    # ---------- 批准记录也参与漂移检查 ----------
    drift_record = os.path.join(FIX_DIR, "drift_record.json")
    dump_json(drift_record, fixture_record(base_scope))
    drift_manifest = {"questions": {"sha256": sha256_file(SMALL_Q)}, "criteria": {"sha256": sha256_file(CRITERIA)},
                      "prices": {"sha256": sha256_file(PRICES)}, "code": {},
                      "_questions_path": SMALL_Q, "_criteria_path": CRITERIA, "_prices_path": PRICES,
                      "_manifest_path": drift_record, "_manifest_sha256": sha256_file(drift_record),
                      "approval": {"sha256": sha256_file(drift_record)}, "_approval_path": drift_record}
    check("漂移检查：批准记录未变时无漂移", m2_batch.drift_check(drift_manifest) == [])
    dump_json(drift_record, fixture_record(base_scope, approved_by="changed-mid-run"))
    drift_problems = m2_batch.drift_check(drift_manifest)
    check("漂移检查：批准记录在批次执行中被改动会被捕获",
          any("批准记录" in p for p in drift_problems), str(drift_problems))

    # ---------- 真实模式主循环（本地替身 runner，零网络） ----------
    out, code, shim, record_path = run_real_batch_case("complete", "complete")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    manifest = load_json(os.path.join(out, "batch_manifest.json"))
    delegation = read_jsonl(os.path.join(out, "batch_delegation.jsonl"))
    check("真实委托（替身）：18 项全部完成、退出 0、stop_reason=completed",
          code == 0 and len(shim.calls) == 18 and summary["stop_reason"] == "completed",
          f"exit={code} calls={len(shim.calls)} stop={summary['stop_reason']}")
    check("真实委托（替身）：落盘委托 argv 与真正传给 runner 的 argv 逐条一致",
          len(delegation) == 18 and [d["argv"] for d in delegation] == shim.calls)
    check("真实委托（替身）：每次委托都带 real 确认与凭据环境变量名、且不含凭据值",
          all("--confirm-real" in a and "--api-key-env" in a and "ROUTER_API_KEY" in a for a in shim.calls)
          and not any(FIXTURE_CREDENTIAL in token for a in shim.calls for token in a))
    check("真实委托（替身）：清单记录批准身份、哈希与停线口径",
          manifest["approval"]["approved_by"] == "verify-fixture-not-a-real-approval"
          and manifest["approval"]["sha256"] == sha256_file(record_path)
          and manifest["stop_policy"]["mode"] == "real_approved_v1"
          and manifest["stop_policy"]["known_cost_stop_cny"] == 8.0
          and manifest["budget"]["known_cost_stop_cny"] == 8.0
          and manifest["max_calls"] == 18)
    leaked = []
    for root, _dirs, files in os.walk(out):
        for fname in files:
            with open(os.path.join(root, fname), "rb") as fh:
                if FIXTURE_CREDENTIAL.encode("utf-8") in fh.read():
                    leaked.append(fname)
    check("真实委托（替身）：凭据夹具值不落盘（批次目录全文扫描）", not leaked, str(leaked))

    out, code, shim, _rec = run_real_batch_case("child_error", "child_error")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("真实停线：子运行异常 → 仅 1 次委托、退出 5、注明剩余项",
          code == 5 and len(shim.calls) == 1 and "child_error" in summary["stop_reason"]
          and summary["remaining_items"] == 17, f"exit={code} stop={summary['stop_reason']}")

    out, code, shim, _rec = run_real_batch_case("http_401", "http_401")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("真实停线：平台 401 → 仅 1 次委托、退出 6、stop_code=platform_error",
          code == 6 and len(shim.calls) == 1 and summary["stop_code"] == "platform_error"
          and "401" in summary["stop_reason"], f"exit={code} stop={summary['stop_reason']}")

    out, code, shim, _rec = run_real_batch_case("no_usage", "no_usage")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("真实停线：usage 缺失（费用未知）→ 退出 6、stop_code=unknown_cost、总费用 null",
          code == 6 and len(shim.calls) == 1 and summary["stop_code"] == "unknown_cost"
          and summary["cost_total"]["total_cost_cny"] is None, f"exit={code} stop={summary['stop_reason']}")

    out, code, shim, _rec = run_real_batch_case("expensive", "expensive")
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("真实停线：已知折算费用达 ¥8 → 第 2 项后退出 6、stop_code=known_cost_stop_line",
          code == 6 and len(shim.calls) == 2 and summary["stop_code"] == "known_cost_stop_line"
          and summary["cost_total"]["known_cost_cny"] >= 8.0,
          f"exit={code} calls={len(shim.calls)} known={summary['cost_total']['known_cost_cny']}")


# --------------------------------------------------------------- M 20 号 R1/R2 反例回归
# R1 批准身份连续性、R2 预算字段有限性。三个主循环用例都在**本地替身 runner** 下运行：
# 零网络、零凭据；夹具批准记录只放在测试目录，不是用户授权。

def withdraw_record(record_path, scope):
    """把磁盘上的批准记录改成"已撤回"（status 非 APPROVED 且批准人字段清空）。"""
    dump_json(record_path, fixture_record(scope, status="PENDING_USER_APPROVAL",
                                          approved_by=None, approved_at=None))


def run_guarded_real_case(name, after_gate_mutation=None, questions_path=None, max_calls=18, shim_factory=None):
    """真实模式主循环：在**门禁返回之后、清单建立之前**执行一次磁盘改动（复现 R1 的时间顺序）。"""
    questions_path = questions_path or SMALL_Q
    out = os.path.join(FIX_DIR, "batches", name)
    fresh(out)
    scope, _plan = fixture_scope(questions_path, out, max_calls)
    record_path = os.path.join(FIX_DIR, name + "_approval.json")
    dump_json(record_path, fixture_record(scope))
    argv = ["--questions", questions_path, "--criteria", CRITERIA, "--prices", PRICES, "--target", "real",
            "--confirm-real", "--approval-record", record_path, "--base-url", REAL_BASE_URL,
            "--out", out, "--max-calls", str(max_calls), "--timeout", "180", "--enable-thinking", "false"]
    shim = shim_factory(record_path, scope) if shim_factory else ShimRunner("complete")
    original_runner = m2_batch.runner_mod
    original_gate = m2_batch.real_gate_problems

    def wrapper(*args, **kwargs):
        result = original_gate(*args, **kwargs)
        if after_gate_mutation:
            after_gate_mutation(record_path, scope)
        return result

    m2_batch.runner_mod = shim
    m2_batch.real_gate_problems = wrapper
    try:
        code = m2_batch.main(argv, env_lookup=lambda _name: FIXTURE_CREDENTIAL)
    finally:
        m2_batch.runner_mod = original_runner
        m2_batch.real_gate_problems = original_gate
    return out, code, shim, record_path, scope


def run_real_loop_bypassing_budget_check(out, record_path, max_calls=18):
    """故意让 `approval_form_problems` 放行：验证"门禁万一漏判，下游仍然失败关闭"。"""
    argv = ["--questions", SMALL_Q, "--criteria", CRITERIA, "--prices", PRICES, "--target", "real",
            "--confirm-real", "--approval-record", record_path, "--base-url", REAL_BASE_URL,
            "--out", out, "--max-calls", str(max_calls), "--timeout", "180", "--enable-thinking", "false"]
    shim = ShimRunner("complete")
    original_runner = m2_batch.runner_mod
    original_form = m2_batch.approval_form_problems
    m2_batch.runner_mod = shim
    m2_batch.approval_form_problems = lambda _doc: []
    try:
        code = m2_batch.main(argv, env_lookup=lambda _name: FIXTURE_CREDENTIAL)
    finally:
        m2_batch.runner_mod = original_runner
        m2_batch.approval_form_problems = original_form
    return code, shim


def scenario_m_r1_r2_regressions():
    os.makedirs(FIX_DIR, exist_ok=True)
    make_small_questions(SMALL_Q)
    out_ok = os.path.join(FIX_DIR, "batch_ok")
    base_scope, _plan = fixture_scope(SMALL_Q, out_ok, 18)

    # ---------------- R1 批准身份连续性 ----------------
    # ① 读取 APPROVED 记录后把磁盘记录改为 PENDING：门禁看到的是旧内容，
    #    验收要求"拒绝或停止、零委托"，绝不能以撤回后的文件哈希当清单基准。
    out, code, shim, _rec, _scope = run_guarded_real_case(
        "r1_withdrawn_after_read", lambda record_path, scope: withdraw_record(record_path, scope))
    check("R1①：读取批准记录后撤回 → 退出 4、零委托、不建批次目录、不写清单",
          code == 4 and len(shim.calls) == 0 and not os.path.exists(out),
          f"exit={code} 委托={len(shim.calls)} 目录存在={os.path.exists(out)}")

    def remove_approval(record_path, _scope):
        retired = record_path + ".withdrawn-fixture"
        fresh(retired)
        os.rename(record_path, retired)

    out, code, shim, _rec, _scope = run_guarded_real_case(
        "r1_missing_after_read", remove_approval)
    check("R1①b：批准记录读取后消失 → 退出 4、零委托、不建批次目录、不抛异常",
          code == 4 and len(shim.calls) == 0 and not os.path.exists(out),
          f"exit={code} 委托={len(shim.calls)} 目录存在={os.path.exists(out)}")

    # ② 门禁通过后修改题面：清单必须沿用门禁已认可的题集身份，不能采纳变化后的文件。
    changed_q = os.path.join(FIX_DIR, "r1_questions_changed_after_gate.json")
    shutil.copyfile(SMALL_Q, changed_q)

    def change_questions(_record_path, _scope):
        doc = load_json(changed_q)
        doc["questions"][0]["question"] = str(doc["questions"][0]["question"]) + "（复审夹具：门禁通过后改动题面）"
        dump_json(changed_q, doc)

    out, code, shim, _rec, _scope = run_guarded_real_case(
        "r1_questions_changed_after_gate", change_questions, questions_path=changed_q)
    check("R1②：门禁通过后题面被改 → 退出 4、零委托、不建批次目录（清单未采纳变化后的哈希）",
          code == 4 and len(shim.calls) == 0 and not os.path.exists(out),
          f"exit={code} 委托={len(shim.calls)} 目录存在={os.path.exists(out)}")

    # ③ 正常恢复用例：首项结束后撤回批准 → 仅 1 次委托，下一项发送前停止（退出 4）。
    class WithdrawAfterFirst(ShimRunner):
        def __init__(self, record_path, scope):
            super().__init__("complete")
            self.record_path = record_path
            self.scope = scope

        def main(self, argv):
            code = super().main(argv)
            if len(self.calls) == 1:
                withdraw_record(self.record_path, self.scope)
            return code

    out, code, shim, _rec, _scope = run_guarded_real_case(
        "r1_withdrawn_after_first_item",
        shim_factory=lambda record_path, scope: WithdrawAfterFirst(record_path, scope))
    summary = load_json(os.path.join(out, "batch_summary.json"))
    check("R1③（正常恢复）：首项结束后撤回批准 → 仅 1 次委托、下一项前停止、退出 4、stop_reason=drift_detected",
          code == 4 and len(shim.calls) == 1 and summary["stop_reason"] == "drift_detected"
          and summary["executed_items"] == 1,
          f"exit={code} 委托={len(shim.calls)} stop={summary['stop_reason']}")

    # ---------------- R2 预算字段有限性 ----------------
    matrix = [
        ("正常 10/8", 10.0, 8.0, False),
        ("停线等于意向金额", 10.0, 10.0, False),
        ("意向金额 NaN", float("nan"), 8.0, True),
        ("意向金额 +Inf", float("inf"), 8.0, True),
        ("意向金额 -Inf", float("-inf"), 8.0, True),
        ("意向金额 0", 0.0, 0.0, True),
        ("停线 NaN", 10.0, float("nan"), True),
        ("停线 +Inf", 10.0, float("inf"), True),
        ("停线 -Inf", 10.0, float("-inf"), True),
        ("停线 0", 10.0, 0.0, True),
        ("停线为字符串", 10.0, "8", True),
        ("停线为布尔", 10.0, True, True),
        ("停线缺失", 10.0, None, True),
        ("停线高于意向金额", 10.0, 12.0, True),
    ]
    bad = [name for name, intention, stop, expect_bad in matrix
           if bool(m2_batch.budget_problems(intention, stop)) != expect_bad]
    check("R2①：预算矩阵（NaN/±Inf/0/字符串/布尔/缺失/停线高于意向一律拒绝，正常 10/8 通过）", not bad, str(bad))

    nan_record = fixture_record(base_scope)
    nan_record["budget"] = {"intention_cny": 10.0, "known_cost_stop_cny": float("nan")}
    nan_path = os.path.join(FIX_DIR, "r2_nan_budget_record.json")
    with open(nan_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(nan_record, ensure_ascii=False, allow_nan=True))
    problems, _doc, _sha = m2_batch.real_gate_problems(nan_path, base_scope, out_ok, True, "ROUTER_API_KEY",
                                                       env_lookup=lambda _name: FIXTURE_CREDENTIAL)
    check("R2②：含 NaN 停线的批准记录在真实门禁被拒绝（首次发送前）",
          bool(problems) and any("有限" in p for p in problems), "；".join(problems)[:200])

    def run_draft(path, intention="10", stop="8", log_name=None):
        cmd = [sys.executable, os.path.join(ROOT, "tools", "approval_m2.py"), "draft",
               "--questions", SMALL_Q, "--criteria", CRITERIA, "--prices", PRICES,
               "--out", out_ok, "--max-calls", "18", "--base-url", REAL_BASE_URL,
               "--approval-record", path, "--budget-intention-cny", intention, "--known-cost-stop-cny", stop]
        result = subprocess.run(cmd, cwd=ROOT, env=child_env(), capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        if log_name:
            _write_log(log_name, cmd, result)
        return result

    nan_draft = os.path.join(FIX_DIR, "r2_nan_draft.json")
    result = run_draft(nan_draft, stop="nan", log_name="r2_draft_nan_stop")
    check("R2③：draft --known-cost-stop-cny nan → 退出 1、不写出草案文件（写入前校验）",
          result.returncode == 1 and not os.path.exists(nan_draft) and "有限" in result.stderr,
          f"exit={result.returncode} 存在={os.path.exists(nan_draft)}")

    inf_draft = os.path.join(FIX_DIR, "r2_inf_intention_draft.json")
    result = run_draft(inf_draft, intention="inf", log_name="r2_draft_inf_intention")
    check("R2③b：draft --budget-intention-cny inf → 退出 1、不写出草案文件",
          result.returncode == 1 and not os.path.exists(inf_draft),
          f"exit={result.returncode} 存在={os.path.exists(inf_draft)}")

    good_draft = os.path.join(FIX_DIR, "r2_good_draft.json")
    if os.path.exists(good_draft):
        fresh(good_draft)
    result = run_draft(good_draft, log_name="r2_draft_normal")
    text = open(good_draft, encoding="utf-8").read() if os.path.exists(good_draft) else ""
    check("R2④：正常 10/8 草案仍可生成（退出 0、PENDING_USER_APPROVAL 且产物无非标准常量）",
          result.returncode == 0 and '"status": "PENDING_USER_APPROVAL"' in text
          and "NaN" not in text and "Infinity" not in text,
          f"exit={result.returncode} 存在={os.path.exists(good_draft)}")

    check("R2⑤：停线数值不可用（NaN/None）时停线判定失败关闭，不返回 None",
          (m2_batch.post_child_stop_reason(fake_account(16.0), None, float("nan")) or ("", ""))[0] == "invalid_stop_line"
          and (m2_batch.post_child_stop_reason(fake_account(0.0), None, None) or ("", ""))[0] == "invalid_stop_line"
          and m2_batch.post_child_stop_reason(fake_account(7.99), None, 8.0) is None)

    # ⑥⑦ 属于"门禁万一漏判"的纵深防御：形式校验被绕过时，下游仍必须停。
    nan_loop_out = os.path.join(FIX_DIR, "batches", "r2_nan_budget_loop")
    fresh(nan_loop_out)
    scope_nan, _plan = fixture_scope(SMALL_Q, nan_loop_out, 18)
    record_nan_loop = os.path.join(FIX_DIR, "r2_nan_budget_loop_approval.json")
    record = fixture_record(scope_nan)
    record["budget"] = {"intention_cny": 10.0, "known_cost_stop_cny": float("nan")}
    with open(record_nan_loop, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, allow_nan=True))
    code, shim = run_real_loop_bypassing_budget_check(nan_loop_out, record_nan_loop)
    check("R2⑥：形式校验被绕过后，含 NaN 的批次清单仍拒绝落盘（退出 4、零委托、无清单文件）",
          code == 4 and len(shim.calls) == 0
          and not os.path.exists(os.path.join(nan_loop_out, "batch_manifest.json")),
          f"exit={code} 委托={len(shim.calls)}")

    null_loop_out = os.path.join(FIX_DIR, "batches", "r2_null_stop_line_loop")
    fresh(null_loop_out)
    scope_null, _plan = fixture_scope(SMALL_Q, null_loop_out, 18)
    record_null_loop = os.path.join(FIX_DIR, "r2_null_stop_line_loop_approval.json")
    record = fixture_record(scope_null)
    record["budget"] = {"intention_cny": 10.0, "known_cost_stop_cny": None}
    dump_json(record_null_loop, record)
    code, shim = run_real_loop_bypassing_budget_check(null_loop_out, record_null_loop)
    summary = load_json(os.path.join(null_loop_out, "batch_summary.json"))
    check("R2⑦：形式校验被绕过后，停线数值缺失仍在主循环失败关闭"
          "（退出 6、仅 1 次委托、stop_code=invalid_stop_line）",
          code == 6 and len(shim.calls) == 1 and summary["stop_code"] == "invalid_stop_line",
          f"exit={code} 委托={len(shim.calls)} stop_code={summary['stop_code']}")


def main():
    global HISTORY_SNAPSHOT
    os.makedirs(VDIR, exist_ok=True)
    HISTORY_SNAPSHOT = snapshot_history()

    questions = scenario_a_integrity()
    scenario_b_full_batch(questions)
    scenario_c_positive_channel(questions)
    scenario_d_route_isolation(questions)
    scenario_e_cap_and_costs()
    scenario_f_drift_and_reuse()
    scenario_g_interrupt()
    scenario_h_identity_refusals(questions)
    scenario_i_child_exception_stop()
    scenario_k_denominators()
    scenario_l_real_mode_prep()
    scenario_m_r1_r2_regressions()
    verify_history_unchanged()

    passed = sum(1 for c in CHECKS if c["ok"])
    print(f"\n==== M2 离线验收：{passed}/{len(CHECKS)} PASS ====")
    with open(os.path.join(VDIR, "verify_m2_result.json"), "w", encoding="utf-8") as fh:
        json.dump({"passed": passed, "total": len(CHECKS), "checks": CHECKS, "finished_at": now_iso()},
                  fh, ensure_ascii=False, indent=2)
    print(f"[report] {os.path.join(VDIR, 'verify_m2_result.json')}")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
