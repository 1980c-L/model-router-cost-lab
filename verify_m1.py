# -*- coding: utf-8 -*-
"""M1 离线验收（含 05 号七项窄修的反例）。

全程零真实模型调用：只使用本地桩。验收自己起一次性桩（用完即停），
并在临时目录生成单价表副本与临时题集，不触碰正式单价表与正式题集。

分组：
  A 契约（含派生字段一致性）    B 路由（含 R4 缩窄）    C 记账
  D 判据（含严格 JSON / 类型）  E 指标                  F 桩链路、上限、门禁、答案与清单、目录复用
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import contract  # noqa: E402
import grade  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import metrics  # noqa: E402
import pricing  # noqa: E402
import router  # noqa: E402
import runner  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PORT_OK = 5299
PORT_ERR = 5300
PORT_BADJSON = 5301
RUNS = os.path.join(ROOT, "runs")
VERIFY_DIR = os.path.join(RUNS, "_verify_m1")
DEV_QUESTIONS = os.path.join(ROOT, "eval", "dev_questions.json")
CRITERIA = os.path.join(ROOT, "eval", "criteria_v1.json")
PRICES = os.path.join(ROOT, "prices", "prices_v1.json")

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))


def child_env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- A 契约

def valid_attempt(attempt_no=1, **overrides):
    base = contract.new_attempt(
        attempt_no=attempt_no,
        model="slot-low-model",
        slot="low",
        purpose="generate",
        status="ok",
        usage={"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 0},
        usage_known=True,
        unit_price_version="prices_v1",
        cost_cny=0.0002,
        latency_ms=120,
        offline_eval=False,
    )
    base.update(overrides)
    return base


def valid_request(**overrides):
    base = contract.new_request(
        request_id="low-D01",
        ts="2026-09-26T12:00:00+08:00",
        group="low",
        task_type_pred="extract",
        task_type_pred_source="rule",
        test_id="D01",
        input_ref={"text_sha256": "a" * 64, "evidence_sha256": "b" * 64},
        attempts=[valid_attempt()],
        run_status="ok",
        total_cost_cny=0.0002,
        known_cost_cny=0.0002,
        unknown_attempt_count=0,
        latency_ms_total=120,
        quality={"overall_pass": True, "mechanized_pass": True},
    )
    base.update(overrides)
    return base


def part_contract():
    check("A1 合法请求通过校验", contract.validate_request(valid_request()) == [])

    bad = valid_request()
    bad.pop("test_id")
    check("A2 缺字段被拒", any("test_id" in e for e in contract.validate_request(bad)))

    bad = valid_request(
        attempts=[valid_attempt(usage=None, usage_known=False, cost_cny=0.0)],
        total_cost_cny=0.0,
        known_cost_cny=0.0,
    )
    check("A3 usage 未知却记了费用 → 拒", any("usage" in e for e in contract.validate_request(bad)))

    bad = valid_request(
        attempts=[valid_attempt(status="unknown", usage=None, usage_known=False, cost_cny=None)],
        total_cost_cny=0.0,
        known_cost_cny=0.0,
        unknown_attempt_count=1,
    )
    errors = contract.validate_request(bad)
    check("A4 有未知尝试却给出总费用 → 拒", any("total_cost_cny" in e for e in errors), errors)

    bad = valid_request(run_status="failed", failure_kind=None)
    check("A5 失败题无 failure_kind → 拒", any("failure_kind" in e for e in contract.validate_request(bad)))

    bad = valid_request(
        attempts=[valid_attempt(usage={"prompt_tokens": True, "completion_tokens": 50, "cached_tokens": 0})]
    )
    check("A6 usage 布尔值不算整数 → 拒", any("prompt_tokens" in e for e in contract.validate_request(bad)))

    bad = valid_request(attempts=[valid_attempt(1), valid_attempt(1)])
    check("A7 attempt_no 重复 → 拒", any("重复" in e for e in contract.validate_request(bad)))

    bad = valid_request(task_type_gold="not_a_type")
    check("A8 非法 gold 标签 → 拒", any("task_type_gold" in e for e in contract.validate_request(bad)))

    bad = valid_request(known_cost_cny=None)
    check("A9 known_cost_cny 为 null → 拒", any("known_cost_cny" in e for e in contract.validate_request(bad)))

    check("A10 合法 attempt 通过校验", contract.validate_attempt(valid_attempt()) == [])

    # 05 号 §2.7：派生字段必须能重新推导
    bad = valid_request(known_cost_cny=999, total_cost_cny=999)
    errors = contract.validate_request(bad)
    check("A11 汇总费用与尝试不一致 → 拒", any("known_cost_cny" in e or "total_cost_cny" in e for e in errors), errors)

    bad = valid_request(
        attempts=[valid_attempt(status="unknown", usage=None, usage_known=False, cost_cny=None)],
        total_cost_cny=None,
        known_cost_cny=0.0,
        unknown_attempt_count=0,
    )
    errors = contract.validate_request(bad)
    check("A12 未知次数与尝试不一致 → 拒", any("unknown_attempt_count" in e for e in errors), errors)

    bad = valid_request(
        attempts=[valid_attempt(cost_cny=-1.0)], known_cost_cny=-1.0, total_cost_cny=-1.0
    )
    check("A13 负费用 → 拒", any("cost_cny" in e for e in contract.validate_request(bad)))

    bad = valid_request(
        attempts=[valid_attempt(cost_cny=float("inf"))],
        known_cost_cny=0.0,
        total_cost_cny=0.0,
    )
    check("A14 非有限费用 → 拒", any("cost_cny" in e for e in contract.validate_request(bad)))


# ---------------------------------------------------------------- B 路由

def part_router():
    long_text = "资料。" * 3000
    decision = router.decide(long_text, {"material_count": 1})
    check("B1 长输入 → high（R1）", decision["chosen_slot"] == "high" and decision["matched_rule"] == "R1_long_input", decision["matched_rule"])

    decision = router.decide("请总结这三段材料。", {"material_count": 3})
    check("B2 材料数 >= 3 → high（R2）", decision["chosen_slot"] == "high" and decision["matched_rule"] == "R2_multi_material", decision["matched_rule"])

    decision = router.decide("请比较这两种方案的区别。", {"material_count": 1})
    check("B3 比较线索 → high（R3）", decision["matched_rule"] == "R3_comparison_or_multi_item", decision["matched_rule"])

    decision = router.decide("请至少列举 5 项要点。", {"material_count": 1})
    check("B4 要求列举 >= 4 项 → high（R3）", decision["matched_rule"] == "R3_comparison_or_multi_item", decision["matched_rule"])

    decision = router.decide("请不少于 800 字地说明。", {"material_count": 1})
    check("B5 长输出要求 → high（R4）", decision["matched_rule"] == "R4_long_output", decision["matched_rule"])

    decision = router.decide(
        "输出 JSON。",
        {"require_json": True, "required_fields": ["a", "b", "c", "d", "e", "f"], "material_count": 1},
    )
    check("B6 JSON + 字段 >= 6 → high（R5）", decision["matched_rule"] == "R5_strict_json_many_fields", decision["matched_rule"])

    decision = router.decide("请说明 k1 参数的含义。", {"material_count": 1})
    check(
        "B7 无高成本特征 → 默认 low",
        decision["chosen_slot"] == "low" and decision["default_applied"] is True,
        decision,
    )

    conflict = router.decide(long_text + " 请比较两者的区别。", {"material_count": 3})
    check(
        "B8 冲突处理：取规则表首个命中（R1 优先于 R2/R3）",
        conflict["matched_rule"] == "R1_long_input" and len(conflict["hit_rules"]) >= 2,
        conflict["hit_rules"],
    )

    text = "请根据资料说明 MRR 的含义。"
    a = router.decide(text, {"material_count": 1})
    b = router.decide(text, {"material_count": 1})
    check("B9 同输入决策稳定（可复现）", json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False))

    signature_text = str(inspect.signature(router.decide)) + str(inspect.signature(router.extract_features))
    check(
        "B10 路由接口不含 gold / expected / test_id 参数",
        not any(word in signature_text for word in ("gold", "expected", "test_id")),
        signature_text,
    )

    check("B11 决策带规则版本", router.decide("测试", {}).get("rule_version") == router.RULES_VERSION)

    user_decision = router.decide("随便问一句。", {"material_count": 1}, user_task_type="summary")
    check(
        "B12 用户显式指定 → 来源记为 user",
        user_decision["task_type_pred"] == "summary" and user_decision["task_type_pred_source"] == "user",
    )

    decoy_text = "请从材料中抽取信息。\n\n【资料】\n[材料1]\n当前风险等级评估为中。"
    decoy = router.decide(decoy_text, {"material_count": 1}, instruction_text="请从材料中抽取信息。")
    check(
        "B13 材料里的线索词不参与判定（题面无线索 → 默认 low）",
        decoy["chosen_slot"] == "low" and decoy["default_applied"] is True,
        decoy["matched_rule"],
    )

    cue = router.decide(
        "请比较两者的区别。\n\n【资料】\n[材料1]\n很短的资料。",
        {"material_count": 1},
        instruction_text="请比较两者的区别。",
    )
    check("B14 题面含线索词 → 命中 R3", cue["matched_rule"] == "R3_comparison_or_multi_item", cue["matched_rule"])

    fallback = router.decide("当前风险等级评估为中。", {"material_count": 1})
    check(
        "B15 不传题面时退回全文匹配（兼容单元级调用）",
        fallback["matched_rule"] == "R3_comparison_or_multi_item",
        fallback["matched_rule"],
    )

    # 05 号 §3 的规则观察：单独出现"至少"不得算长输出
    short = router.decide("请至少用 1 句话概括材料。", {"material_count": 1}, instruction_text="请至少用 1 句话概括材料。")
    check(
        "B16 『请至少用 1 句话概括』不命中 R4",
        short["matched_rule"] != "R4_long_output",
        short["matched_rule"],
    )

    check("B17 规则版本已升到 v2（线索词缩窄是有记录的行为变更）", router.RULES_VERSION == "router_rules_v2", router.RULES_VERSION)


# ---------------------------------------------------------------- C 记账

def make_temp_prices(path, slots, models):
    data = {
        "version": os.path.basename(path).replace(".json", ""),
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "fetched_at": "2026-09-26",
        "source_url": "https://example.invalid/pricing",
        "slots": slots,
        "models": models,
    }
    return write_json(path, data)


def part_ledger():
    run_dir = os.path.join(VERIFY_DIR, "ledger_case")
    shutil.rmtree(run_dir, ignore_errors=True)
    prices = pricing.load_prices(
        make_temp_prices(
            os.path.join(VERIFY_DIR, "prices_verify.json"),
            {"low": "model-cheap", "high": "model-pricey"},
            {
                "model-cheap": {"input_price_per_1m": 1.0, "output_price_per_1m": 2.0, "cached_input_price_per_1m": 0.5},
                "model-unknown": {"input_price_per_1m": None, "output_price_per_1m": None},
            },
        )
    )
    ledger = ledger_mod.Ledger(run_dir, prices=prices)

    no = ledger.start_attempt("low-D01", "model-cheap", "low")
    record = ledger.finish_attempt(
        "low-D01", no, status="ok", usage={"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 0},
        latency_ms=200, model="model-cheap", slot="low", purpose="generate",
    )
    check("C1 按单价折算费用正确", abs((record["cost_cny"] or 0) - 0.002) < 1e-9, record["cost_cny"])
    check("C1b 尝试记录带单价版本", record.get("unit_price_version") == prices.get("version"), record.get("unit_price_version"))

    hit = ledger.summarize()
    check("C2 全部已知 → 总费用等于已知之和", hit["total_cost_cny"] == 0.002 and hit["unknown_attempt_count"] == 0, hit)

    ledger.start_attempt("low-D02", "model-unknown", "low")
    ledger.finish_attempt(
        "low-D02", 1, status="ok", usage={"prompt_tokens": 10, "completion_tokens": 10, "cached_tokens": 0},
        latency_ms=50, model="model-unknown", slot="low", purpose="generate",
    )
    ledger.start_attempt("low-D03", "model-cheap", "low")  # 只有 start，没有 end
    summary = ledger.summarize()
    check("C3 单价未填 → 费用未知（不记 0）", summary["unknown_attempt_count"] >= 1, summary)
    check("C4 存在未知 → 总费用为 null，另给已知部分", summary["total_cost_cny"] is None and summary["known_cost_cny"] == 0.002, summary)
    check("C5 只有 start 的尝试 → 计为未闭合且未知", summary["unsealed_attempts"] >= 1, summary)

    sealed = ledger.sealed_attempts()
    target = [a for a in sealed if a["request_id"] == "low-D03"][0]
    check("C6 未闭合尝试的 status=unknown 且费用 null", target["status"] == "unknown" and target["cost_cny"] is None, target)

    events = ledger.events()
    kinds = [(e.get("kind"), e.get("attempt_no")) for e in events][:2]
    check("C7 事件流 append-only：start 在前 end 在后", kinds == [("start", 1), ("end", 1)], kinds)

    ledger.start_attempt("low-D01", "model-cheap", "low")
    check("C8 同一请求内 attempt_no 递增", ledger._next_attempt["low-D01"] == 2)


# ---------------------------------------------------------------- D 判据

def part_grade():
    criteria = load_json(CRITERIA)
    extract_q = {
        "constraints": {"required_fields": ["a", "b"]},
        "answer_key": {"a": "x", "b": "y"},
    }
    good = grade.grade("extract", '{"a": "x", "b": "y"}', extract_q, criteria)
    check("D1 extract 完全正确 → 机检通过且无人工项", good["mechanized_pass"] and good["overall_pass"] and not good["human_required"], good)

    missing = grade.grade("extract", '{"a": "x"}', extract_q, criteria)
    check("D2 extract 缺字段 → 格式不通过", missing["format_pass"] is False and "b" in missing["detail"]["missing_fields"], missing["detail"])

    mismatch = grade.grade("extract", '{"a": "x", "b": "z"}', extract_q, criteria)
    check(
        "D3 extract 格式对但内容错 → 分开记录",
        mismatch["format_pass"] is True and mismatch["content_pass"] is False,
        mismatch,
    )

    # 05 号 §2.6：D4 反转 —— 代码块不再被剥掉，必须判不通过
    fenced = grade.grade("extract", '```json\n{"a": "x", "b": "y"}\n```', extract_q, criteria)
    check(
        "D4 严格模式：代码块包裹 → 不通过（原宽松行为已废）",
        fenced["mechanized_pass"] is False and fenced["detail"]["json_parsable"] is False,
        fenced["detail"],
    )

    extra = grade.grade("extract", '{"a": "x", "b": "y"}\n以上就是结果。', extract_q, criteria)
    check("D4b 结尾多余文字 → 不通过", extra["mechanized_pass"] is False, extra["detail"])

    summary_q = {
        "criteria": {"max_chars": 20, "coverage_keywords": ["检索", "生成"], "forbidden_keywords": ["准确率提升"]},
    }
    too_long = grade.grade("summary", "检索与生成需要分别验证，这段刻意写得超过二十个字以便触发长度上限。", summary_q, criteria)
    check("D5 summary 超长 → 不达标", too_long["mechanized_pass"] is False and too_long["detail"]["length_pass"] is False, too_long["detail"])

    ok_summary = grade.grade("summary", "检索与生成要分开验证", summary_q, criteria)
    check(
        "D6 summary 机检过但仍需人工 → overall 为 false",
        ok_summary["mechanized_pass"] and ok_summary["human_required"] and ok_summary["overall_pass"] is False,
        ok_summary,
    )

    forbidden = grade.grade("summary", "检索与生成要分开验证", {"criteria": {"max_chars": 50, "coverage_keywords": [], "forbidden_keywords": ["分开"]}}, criteria)
    check("D7 summary 触发禁止词 → 不达标", forbidden["mechanized_pass"] is False, forbidden["detail"])

    qa_refusal_needed = grade.grade("qa_grounded", "根据资料，k1 控制饱和速度。", {"expect_refusal": True}, criteria)
    check("D8 期望拒答但没拒答 → 不达标", qa_refusal_needed["mechanized_pass"] is False, qa_refusal_needed["detail"])

    qa_ok = grade.grade("qa_grounded", "资料中未找到相关依据。", {"expect_refusal": True}, criteria)
    check("D9 期望拒答且拒答 → 机检通过但仍需人工", qa_ok["mechanized_pass"] and qa_ok["human_required"], qa_ok)

    unknown = grade.grade("unknown", "随便回答", {}, criteria)
    check("D10 未识别任务类型 → 不达标且标人工", unknown["overall_pass"] is False and unknown["human_required"] is True, unknown)

    # 05 号 §2.6 的第二个反例：bool 不得冒充数字
    numeric_q = {"constraints": {"required_fields": ["amount"]}, "answer_key": {"amount": 1}}
    bool_as_number = grade.grade("extract", '{"amount": true}', numeric_q, criteria)
    check(
        "D11 bool 冒充数字 → 内容不通过",
        bool_as_number["content_pass"] is False and bool_as_number["overall_pass"] is False,
        bool_as_number["detail"],
    )
    number_ok = grade.grade("extract", '{"amount": 1.0}', numeric_q, criteria)
    check("D12 数字按容差比较 → 1.0 与 1 视为一致", number_ok["mechanized_pass"] is True, number_ok["detail"])

    typed_q = {
        "constraints": {"required_fields": ["amount"], "field_types": {"amount": "string"}},
        "answer_key": {"amount": "12.5 万元"},
    }
    wrong_type = grade.grade("extract", '{"amount": 12.5}', typed_q, criteria)
    check(
        "D13 field_types 生效：声明为字符串却给数字 → 格式不通过",
        wrong_type["format_pass"] is False and wrong_type["detail"]["type_mismatched"],
        wrong_type["detail"],
    )
    check("D14 严格解析的 detail 标记", good["detail"].get("strict_parse") is True, good["detail"])
    check(
        "D15 宽松解析只留给人工分析（不计分路径）",
        grade.parse_lenient_for_analysis('```json\n{"a": 1}\n```') == {"a": 1}
        and grade.parse_strict_json('```json\n{"a": 1}\n```') is None,
    )


# ---------------------------------------------------------------- E 指标

def fake_request(test_id, group="low", status="ok", passed=True):
    quality = {"overall_pass": passed, "mechanized_pass": passed}
    return {"test_id": test_id, "group": group, "run_status": status, "quality": quality, "attempts": []}


def part_metrics():
    requests = [fake_request("T1"), fake_request("T2"), fake_request("T3", passed=False), fake_request("T4", status="failed", passed=False)]
    e2e = metrics.end_to_end_pass_rate(requests, 5)
    check("E1 主指标分母 = 计划题数（含失败与未执行）", e2e["denominator"] == 5 and e2e["rate"] == round(2 / 5, 6), e2e)

    content = metrics.content_pass_rate(requests)
    check("E2 内容达标率分母 = 已作答数", content["denominator"] == 3 and content["rate"] == round(2 / 3, 6), content)

    grouped = {
        "low": [fake_request("T1"), fake_request("T2", passed=False)],
        "high": [fake_request("T1"), fake_request("T2"), fake_request("T3")],
    }
    paired = metrics.paired_compare(grouped)
    check("E3 配对比较只用共同题", paired["common_ids"] == ["T1", "T2"] and paired["samples"] == 2, paired)

    failure = metrics.run_failure_rate([fake_request("T1"), {"test_id": "T2", "run_status": "failed", "failure_kind": "timeout", "quality": {}}], 4)
    check("E4 失败率按原因分类且分母为计划数", failure["rate"] == 0.25 and failure["by_kind"] == {"timeout": 1}, failure)

    cost = metrics.cost_summary([
        {"attempts": [{"offline_eval": False, "cost_cny": 0.001}]},
        {"attempts": [{"offline_eval": False, "cost_cny": None}]},
    ])
    check("E5 费用汇总：有未知则总额未知", cost["total_cost_cny"] is None and cost["known_cost_cny"] == 0.001, cost)


# ---------------------------------------------------------------- F 桩链路

def health_ok(port):
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/health", timeout=1
        ) as response:
            return response.status == 200
    except Exception:
        return False


def wait_health(port, tries=60):
    for _ in range(tries):
        if health_ok(port):
            return True
        time.sleep(0.2)
    return False


def stub_ledger_count(path):
    return len(load_jsonl(path))


def start_stub(port, ledger_path, default_mode="ok"):
    if ledger_path and os.path.exists(ledger_path):
        os.remove(ledger_path)
    proc = subprocess.Popen(
        [
            sys.executable, "tools/stub_llm_server.py",
            "--port", str(port), "--ledger", ledger_path, "--default-mode", default_mode,
        ],
        cwd=ROOT,
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    return proc


def stop_stub(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def run_runner(extra_args, out_dir, env=None):
    if out_dir:
        shutil.rmtree(out_dir, ignore_errors=True)
    return subprocess.run(
        [sys.executable, "runner.py"] + extra_args,
        cwd=ROOT,
        env=env or child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def route_args(port, out_dir, ids="D01,D02,D05,D07,D08,D09", max_calls=6, prices=None):
    args = [
        "--questions", DEV_QUESTIONS,
        "--ids", ids,
        "--slot", "route",
        "--target", "stub",
        "--base-url", f"http://127.0.0.1:{port}",
        "--max-calls", str(max_calls),
        "--out", out_dir,
    ]
    if prices:
        args += ["--prices", prices]
    return args


def part_stub_chain():
    ok_ledger = os.path.join(VERIFY_DIR, "stub_ok_requests.jsonl")
    stub = start_stub(PORT_OK, ok_ledger)
    try:
        check("F1 一次性桩可启动", wait_health(PORT_OK))

        out_dir = os.path.join(RUNS, "_verify_m1_route")
        proc = run_runner(route_args(PORT_OK, out_dir), out_dir)
        check("F2 route 组真实价格表桩运行退出码 0", proc.returncode == 0, proc.stderr[-400:])

        requests = load_jsonl(os.path.join(out_dir, "requests.jsonl"))
        check("F3 6 题各产生一条请求记录", len(requests) == 6, len(requests))
        check("F4 桩台账请求数 == 6（每题 1 次、0 重试）", stub_ledger_count(ok_ledger) == 6, stub_ledger_count(ok_ledger))
        check("F5 全部题目运行状态为 ok", all(r["run_status"] == "ok" for r in requests))
        check("F6 每题恰好 1 条尝试且 purpose=generate", all(len(r["attempts"]) == 1 and r["attempts"][0]["purpose"] == "generate" for r in requests))
        check("F7 每题尝试都记录了 usage", all(r["attempts"][0]["usage_known"] for r in requests))

        check(
            "F8 正式价格表已填 → 单次费用已知（不再全部未知）",
            all(r["attempts"][0]["cost_cny"] is not None for r in requests),
            [r["attempts"][0]["cost_cny"] for r in requests],
        )
        check(
            "F9 全部已知 → 总额等于已知之和且未知次数为 0",
            all(r["total_cost_cny"] is not None and r["unknown_attempt_count"] == 0 for r in requests),
            [(r["total_cost_cny"], r["unknown_attempt_count"]) for r in requests],
        )
        check("F9b 尝试记录带单价版本", all(r["attempts"][0]["unit_price_version"] == "prices_v1" for r in requests))

        check("F10 跑出来的请求全部通过契约校验", all(contract.validate_request(r) == [] for r in requests))
        check("F11 桩回答不达标（桩不能证明质量）", all(r["quality"]["overall_pass"] is False for r in requests))

        decisions = {r["test_id"]: r["router"]["chosen_slot"] for r in requests}
        expected = {"D01": "low", "D02": "high", "D05": "low", "D07": "low", "D08": "low", "D09": "high"}
        check("F12 路由决策与预期一致（D02/D09 走 high）", decisions == expected, decisions)

        summary = load_json(os.path.join(out_dir, "summary.json"))
        check(
            "F13 summary 主指标分母 = 计划题数",
            summary["metrics"]["end_to_end"]["denominator"] == 6 and summary["metrics"]["end_to_end"]["planned"] == 6,
            summary["metrics"]["end_to_end"],
        )
        check("F14 summary 标记本轮完整", summary["complete"] is True and summary["executed_count"] == 6, summary)
        check(
            "F15 登记尝试数 == start 事件数 == 桩台账数 == 6",
            summary["actual_attempts"] == 6 and summary["start_events"] == 6 and stub_ledger_count(ok_ledger) == 6,
            (summary["actual_attempts"], summary["start_events"], stub_ledger_count(ok_ledger)),
        )
        check("F15b 成功次数单列且与本次一致", summary["successful_calls"] == 6, summary["successful_calls"])

        # 未知传播（端到端）：high 槽位价格未填
        temp_prices = make_temp_prices(
            os.path.join(VERIFY_DIR, "prices_partial.json"),
            {"low": "model-cheap", "high": "model-unknown"},
            {
                "model-cheap": {"input_price_per_1m": 1.0, "output_price_per_1m": 2.0},
                "model-unknown": {"input_price_per_1m": None, "output_price_per_1m": None},
            },
        )
        out_unknown = os.path.join(RUNS, "_verify_m1_unknown")
        run_runner(route_args(PORT_OK, out_unknown, prices=temp_prices), out_unknown)
        reqs_unknown = {r["test_id"]: r for r in load_jsonl(os.path.join(out_unknown, "requests.jsonl"))}
        check(
            "F16 端到端未知传播：走到未填价格槽位的题总额为 null、未知次数 1",
            reqs_unknown["D02"]["total_cost_cny"] is None
            and reqs_unknown["D02"]["unknown_attempt_count"] == 1
            and reqs_unknown["D02"]["known_cost_cny"] == 0.0,
            reqs_unknown["D02"]["total_cost_cny"],
        )
        check(
            "F16b 同轮已知槽位的题仍给出费用",
            reqs_unknown["D01"]["total_cost_cny"] is not None and reqs_unknown["D01"]["unknown_attempt_count"] == 0,
            reqs_unknown["D01"]["total_cost_cny"],
        )

        # 正常路径上限截断
        out_capped = os.path.join(RUNS, "_verify_m1_capped")
        proc_cap = run_runner(route_args(PORT_OK, out_capped, ids="D01,D02,D05,D07,D08,D09", max_calls=2), out_capped)
        requests2 = load_jsonl(os.path.join(out_capped, "requests.jsonl"))
        summary2 = load_json(os.path.join(out_capped, "summary.json"))
        check("F17 达到调用上限即停（不自动补跑）", len(requests2) == 2 and proc_cap.returncode == 0, len(requests2))
        check("F18 截断轮标记为不完整", summary2["complete"] is False and summary2["executed_count"] == 2, summary2)
    finally:
        stop_stub(stub)

    # 失败也占额度：HTTP 500
    err_ledger = os.path.join(VERIFY_DIR, "stub_err_requests.jsonl")
    stub_err = start_stub(PORT_ERR, err_ledger, default_mode="error")
    try:
        check("F19 故障桩可启动（HTTP 500）", wait_health(PORT_ERR))
        out_err = os.path.join(RUNS, "_verify_m1_limits_error")
        proc = run_runner(route_args(PORT_ERR, out_err, ids="D01,D02,D05", max_calls=1), out_err)
        reqs = load_jsonl(os.path.join(out_err, "requests.jsonl"))
        summary = load_json(os.path.join(out_err, "summary.json"))
        check("F20 HTTP 500 轮：退出码 0（失败也如实记录）", proc.returncode == 0, proc.stderr[-300:])
        check(
            "F21 HTTP 500 轮：上限 1 → 只发送 1 次（失败占额度）",
            stub_ledger_count(err_ledger) == 1,
            stub_ledger_count(err_ledger),
        )
        check(
            "F22 HTTP 500 轮：汇总次数与 start 事件、台账一致（1/1/1）",
            summary["actual_attempts"] == 1 and summary["start_events"] == 1 and stub_ledger_count(err_ledger) == 1,
            (summary["actual_attempts"], summary["start_events"], stub_ledger_count(err_ledger)),
        )
        check("F23 HTTP 500 轮：成功次数为 0", summary["successful_calls"] == 0, summary["successful_calls"])
        check("F24 HTTP 500 轮：失败题记为 failed 且带原因", all(r["run_status"] == "failed" and r["failure_kind"] == "platform" for r in reqs), [r.get("failure_kind") for r in reqs])
        check(
            "F25 HTTP 500 轮：失败题仍占主指标分母，且不进内容达标率分母",
            summary["metrics"]["end_to_end"]["denominator"] == 3
            and summary["metrics"]["end_to_end"]["rate"] == 0.0
            and summary["metrics"]["content"]["denominator"] == 0,
            summary["metrics"]["end_to_end"],
        )
    finally:
        stop_stub(stub_err)

    # 失败也占额度：响应体不可解析
    bad_ledger = os.path.join(VERIFY_DIR, "stub_badjson_requests.jsonl")
    stub_bad = start_stub(PORT_BADJSON, bad_ledger, default_mode="bad_json")
    try:
        check("F26 故障桩可启动（200 但响应体不可解析）", wait_health(PORT_BADJSON))
        out_bad = os.path.join(RUNS, "_verify_m1_limits_badjson")
        run_runner(route_args(PORT_BADJSON, out_bad, ids="D01,D02,D05", max_calls=1), out_bad)
        summary_bad = load_json(os.path.join(out_bad, "summary.json"))
        check("F27 解析失败轮：只发送 1 次且尝试数为 1", summary_bad["actual_attempts"] == 1 and stub_ledger_count(bad_ledger) == 1, (summary_bad["actual_attempts"], stub_ledger_count(bad_ledger)))
    finally:
        stop_stub(stub_bad)


def part_dir_reuse_and_guards():
    # 目录复用拒绝（05 号 §2.2）
    ok_ledger = os.path.join(VERIFY_DIR, "stub_reuse_requests.jsonl")
    stub = start_stub(PORT_OK, ok_ledger)
    try:
        out_dir = os.path.join(RUNS, "_verify_m1_reuse")
        run_runner(route_args(PORT_OK, out_dir, ids="D01", max_calls=1), out_dir)
        first = stub_ledger_count(ok_ledger)
        proc = subprocess.run(
            [sys.executable, "runner.py"] + route_args(PORT_OK, out_dir, ids="D01", max_calls=1),
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
        )
        check("F28 复用已有运行目录 → 拒绝（退出码 5）", proc.returncode == 5, proc.returncode)
        check("F29 拒绝时未发出新请求", stub_ledger_count(ok_ledger) == first, (first, stub_ledger_count(ok_ledger)))

        # 桩模式非回环 URL
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "stub", "--base-url", "https://review.invalid", "--max-calls", "1",
                "--out", os.path.join(RUNS, "_verify_m1_nonloop"),
            ],
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
        )
        check("F30 桩模式非回环 URL → 拒绝（退出码 3）", proc.returncode == 3, proc.returncode)
        check("F30b 该拒绝发生在发送之前（台账未增）", stub_ledger_count(ok_ledger) == first, stub_ledger_count(ok_ledger))

        # 真实模式：占位模型
        placeholder_prices = make_temp_prices(
            os.path.join(VERIFY_DIR, "prices_placeholder.json"),
            {"low": "SLOT_LOW_PLACEHOLDER", "high": "SLOT_HIGH_PLACEHOLDER"},
            {
                "SLOT_LOW_PLACEHOLDER": {"input_price_per_1m": None, "output_price_per_1m": None},
                "SLOT_HIGH_PLACEHOLDER": {"input_price_per_1m": None, "output_price_per_1m": None},
            },
        )
        env_key = child_env()
        env_key["ROUTER_API_KEY"] = "sk-fake-for-guard-only"
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "real", "--confirm-real", "--base-url", "https://api.example.com",
                "--max-calls", "1", "--prices", placeholder_prices,
                "--out", os.path.join(RUNS, "_verify_m1_guard_placeholder"),
            ],
            cwd=ROOT, env=env_key, capture_output=True, text=True, encoding="utf-8",
        )
        check("F31 真实模式占位模型 → 拒绝（退出码 3）", proc.returncode == 3, proc.stdout[-200:] + proc.stderr[-200:])

        # 真实模式：缺价格来源
        no_source_prices = {
            "version": "prices_no_source",
            "currency": "CNY",
            "slots": {"low": "model-cheap", "high": "model-pricey"},
            "models": {
                "model-cheap": {"input_price_per_1m": 1.0, "output_price_per_1m": 2.0},
                "model-pricey": {"input_price_per_1m": 8.0, "output_price_per_1m": 28.0},
            },
        }
        no_source_path = write_json(os.path.join(VERIFY_DIR, "prices_no_source.json"), no_source_prices)
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "real", "--confirm-real", "--base-url", "https://api.example.com",
                "--max-calls", "1", "--prices", no_source_path,
                "--out", os.path.join(RUNS, "_verify_m1_guard_nosource"),
            ],
            cwd=ROOT, env=env_key, capture_output=True, text=True, encoding="utf-8",
        )
        check("F32 真实模式缺价格来源/日期 → 拒绝（退出码 3）", proc.returncode == 3, proc.stderr[-200:])

        # 真实模式：缺凭据
        env_no_key = child_env()
        env_no_key.pop("ROUTER_API_KEY", None)
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "real", "--confirm-real", "--base-url", "https://api.example.com",
                "--max-calls", "1",
                "--out", os.path.join(RUNS, "_verify_m1_guard_nokey"),
            ],
            cwd=ROOT, env=env_no_key, capture_output=True, text=True, encoding="utf-8",
        )
        check("F33 真实模式缺凭据 → 拒绝（退出码 3）", proc.returncode == 3, proc.stderr[-200:])
    finally:
        stop_stub(stub)


def part_misclassification_and_manifest():
    # 评测类别缺失/非法 → 发送前拒绝
    bad_questions = write_json(
        os.path.join(VERIFY_DIR, "questions_bad_gold.json"),
        {"version": "verify_bad_gold", "questions": [{"test_id": "X01", "question": "随便问。", "materials": []}]},
    )
    ok_ledger = os.path.join(VERIFY_DIR, "stub_misc_requests.jsonl")
    stub = start_stub(PORT_OK, ok_ledger)
    try:
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", bad_questions, "--ids", "X01", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}", "--max-calls", "1",
                "--out", os.path.join(RUNS, "_verify_m1_bad_gold"),
            ],
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
        )
        check("F34 评测类别缺失 → 拒绝（退出码 2）", proc.returncode == 2, proc.returncode)
        check("F35 拒绝时零请求", stub_ledger_count(ok_ledger) == 0, stub_ledger_count(ok_ledger))

        # 误分类不得改变评分标准
        misc_questions = write_json(
            os.path.join(VERIFY_DIR, "questions_misclass.json"),
            {
                "version": "verify_misclass",
                "questions": [
                    {
                        "test_id": "X01",
                        "task_type_gold": "qa_grounded",
                        "question": "根据资料说明 JSON 字段的作用。",
                        "materials": ["JSON 字段用于结构化表达数据。"],
                        "constraints": {"material_count": 1},
                        "expect_refusal": False,
                    }
                ],
            },
        )
        out_misc = os.path.join(RUNS, "_verify_m1_misclass")
        run_runner(
            [
                "--questions", misc_questions, "--ids", "X01", "--slot", "route",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}",
                "--max-calls", "1", "--out", out_misc,
            ],
            out_misc,
        )
        record = load_jsonl(os.path.join(out_misc, "requests.jsonl"))[0]
        check("F36 预测类别被误判为 extract（误分类确实发生）", record["task_type_pred"] == "extract", record["task_type_pred"])
        check(
            "F37 评分仍按冻结类别 qa_grounded：需人工、不得直接达标",
            record["task_type_gold"] == "qa_grounded"
            and record["quality"]["human_required"] is True
            and record["quality"]["overall_pass"] is False,
            record["quality"],
        )
        check(
            "F38 误分类只记录、不改变判据（gold 未进入路由）",
            record["router"]["task_type_pred"] == "extract" and record["router"]["task_type_pred_source"] == "rule",
            record["router"],
        )

        # 完整答案留存 + run manifest
        out_dir = os.path.join(RUNS, "_verify_m1_route")
        requests = load_jsonl(os.path.join(out_dir, "requests.jsonl"))
        answers_ok = True
        multiline_seen = False
        for item in requests:
            answer_path = os.path.join(out_dir, item["final_output_ref"])
            if not os.path.exists(answer_path):
                answers_ok = False
                break
            with open(answer_path, "rb") as fh:
                raw = fh.read()
            if b"\r\n" in raw:           # 出现 CRLF 说明文本模式改写发生过
                answers_ok = False
                break
            if b"\n" in raw:
                multiline_seen = True
            if hashlib.sha256(raw).hexdigest() != item["answer_sha256"]:
                answers_ok = False
                break
        check("F39 每题完整答案落盘且哈希自洽（按文件字节核对）", answers_ok)
        check("F39b 多行答案已覆盖：文件保留原始 LF、未被写成 CRLF", multiline_seen)

        manifest = load_json(os.path.join(out_dir, "run_manifest.json"))
        needed = ("questions", "criteria", "prices", "code", "router_rules_version", "generation", "system_prompt_sha256")
        check("F40 run manifest 记录题集/判据/价格/代码/规则版本/生成参数", all(k in manifest for k in needed), list(manifest.keys()))
        check(
            "F41 manifest 的题集与判据哈希与现场文件一致",
            manifest["questions"]["sha256"] == _sha256(DEV_QUESTIONS)
            and manifest["criteria"]["sha256"] == _sha256(CRITERIA)
            and manifest["prices"]["sha256"] == _sha256(PRICES),
        )
        check(
            "F42 manifest 逐题记录系统提示身份，且与计划题集一致",
            set(manifest["system_prompt_sha256"].keys()) == set(manifest["planned_ids"]) and len(manifest["planned_ids"]) == 6,
            manifest["planned_ids"],
        )
        check(
            "F43 manifest 是事前件：只含计划身份、不含结束统计",
            "planned_ids" in manifest
            and "executed_count" not in manifest
            and "actual_attempts" not in manifest
            and bool(manifest.get("frozen_at")),
            sorted(manifest.keys()),
        )
        summary = load_json(os.path.join(out_dir, "summary.json"))
        check("F44 summary 绑定 manifest 哈希", summary.get("manifest_sha256") == _sha256(os.path.join(out_dir, "run_manifest.json")), summary.get("manifest_ref"))
        check(
            "F44b 结束统计只写在 summary 里",
            summary.get("executed_count") == 6
            and summary.get("actual_attempts") == 6
            and summary.get("successful_calls") == 6,
            (summary.get("executed_count"), summary.get("actual_attempts"), summary.get("successful_calls")),
        )
    finally:
        stop_stub(stub)


def _sha256(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def part_thinking_flags():
    """思考模式开关与思考预算必须真的透传（真实超时的修法依据）。"""
    ok_ledger = os.path.join(VERIFY_DIR, "stub_thinking_requests.jsonl")
    stub = start_stub(PORT_OK, ok_ledger)
    try:
        out_dir = os.path.join(RUNS, "_verify_m1_thinking_off")
        run_runner(
            [
                "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}",
                "--max-calls", "1", "--enable-thinking", "false", "--thinking-budget", "1024",
                "--out", out_dir,
            ],
            out_dir,
        )
        records = load_jsonl(ok_ledger)
        check(
            "F45 关闭思考与思考预算确实透传到请求（桩台账可证）",
            bool(records) and records[-1].get("enable_thinking") is False and records[-1].get("thinking_budget") == 1024,
            records[-1] if records else None,
        )
        manifest = load_json(os.path.join(out_dir, "run_manifest.json"))
        check(
            "F46 run manifest 记录 enable_thinking / thinking_budget",
            manifest["generation"].get("enable_thinking") == "false"
            and manifest["generation"].get("thinking_budget") == 1024,
            manifest["generation"],
        )

        out_dir2 = os.path.join(RUNS, "_verify_m1_thinking_default")
        run_runner(
            [
                "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}",
                "--max-calls", "1", "--out", out_dir2,
            ],
            out_dir2,
        )
        records2 = load_jsonl(ok_ledger)
        check(
            "F47 默认不发送思考开关（不改变原有行为）",
            bool(records2) and records2[-1].get("enable_thinking") is None,
            records2[-1].get("enable_thinking") if records2 else None,
        )
    finally:
        stop_stub(stub)


def part_closing_edges():
    """07 号四处代码边界的反例。"""
    ok_ledger = os.path.join(VERIFY_DIR, "stub_edges_requests.jsonl")
    stub = start_stub(PORT_OK, ok_ledger)
    try:
        # 1) 复用开关必须已删除
        out_dir = os.path.join(RUNS, "_verify_m1_edges")
        proc = subprocess.run(
            [
                sys.executable, "runner.py", "--questions", DEV_QUESTIONS, "--ids", "D01", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}", "--max-calls", "1",
                "--allow-existing-run", "--out", out_dir,
            ],
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
        )
        check("F48 复用开关已删除：传 --allow-existing-run 被拒绝", proc.returncode != 0, proc.returncode)
        check("F48b 该拒绝发生在发送之前（零请求）", stub_ledger_count(ok_ledger) == 0, stub_ledger_count(ok_ledger))

        # 2) 清单在第一条请求之前落盘（输出顺序 + 事前件字段）
        out_manifest = os.path.join(RUNS, "_verify_m1_manifest_first")
        proc2 = run_runner(
            [
                "--questions", DEV_QUESTIONS, "--ids", "D01,D02,D05", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}",
                "--max-calls", "1", "--out", out_manifest,
            ],
            out_manifest,
        )
        stdout_lines = [line for line in (proc2.stdout or "").splitlines() if line.strip()]
        manifest_line = next((i for i, line in enumerate(stdout_lines) if line.startswith("[manifest]")), None)
        first_question_line = next((i for i, line in enumerate(stdout_lines) if line.strip().startswith("D0")), None)
        check(
            "F49 清单落盘发生在第一条请求之前（输出顺序可证）",
            manifest_line is not None and first_question_line is not None and manifest_line < first_question_line,
            (manifest_line, first_question_line),
        )
        manifest_first = load_json(os.path.join(out_manifest, "run_manifest.json"))
        check(
            "F49b 截断轮仍保留完整计划身份（计划 3 题、只执行 1 题、清单不被覆盖）",
            manifest_first.get("planned_count") == 3
            and len(manifest_first.get("system_prompt_sha256", {})) == 3
            and "executed_count" not in manifest_first,
            (manifest_first.get("planned_count"), len(manifest_first.get("system_prompt_sha256", {}))),
        )
        check(
            "F49c 每条请求记录同时绑定 manifest 名称与哈希",
            all(
                r.get("manifest_ref") == "run_manifest.json" and r.get("manifest_sha256")
                for r in load_jsonl(os.path.join(out_manifest, "requests.jsonl"))
            ),
        )

        # 3) 缓存用量字段映射（SiliconFlow 实际形状）
        usage_a, meta_a = runner.normalize_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 80}}
        )
        check("F50 嵌套字段 prompt_tokens_details.cached_tokens 被读取（不再漏成 0）", bool(usage_a) and usage_a["cached_tokens"] == 80, meta_a)
        usage_b, meta_b = runner.normalize_usage({"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 30})
        check("F50b 兼容字段 cached_tokens 被读取", bool(usage_b) and usage_b["cached_tokens"] == 30, meta_b)
        usage_c, meta_c = runner.normalize_usage({"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 55})
        check("F50c 兼容字段 prompt_cache_hit_tokens 被读取", bool(usage_c) and usage_c["cached_tokens"] == 55, meta_c)
        usage_d, meta_d = runner.normalize_usage({"prompt_tokens": 100, "completion_tokens": 10})
        check(
            "F50d 缺失缓存字段时记 0 但标注来源，不是无说明记零",
            bool(usage_d) and usage_d["cached_tokens"] == 0 and meta_d["cached_source"] == "absent",
            meta_d,
        )
        usage_e, meta_e = runner.normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 80},
                "cached_tokens": 10,
            }
        )
        check(
            "F50e 字段冲突时按明示优先序取值并留痕",
            bool(usage_e) and usage_e["cached_tokens"] == 80 and any("冲突" in n for n in meta_e["notes"]),
            meta_e,
        )
        usage_f, meta_f = runner.normalize_usage(
            {"prompt_tokens": 50, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 999}}
        )
        check(
            "F50f 缓存量超过输入量被裁剪并留痕",
            bool(usage_f) and usage_f["cached_tokens"] == 50 and any("裁剪" in n for n in meta_f["notes"]),
            meta_f,
        )
        check("F50g 原始 usage 被保留以便核对", bool(meta_a) and bool(meta_a.get("raw_usage")), meta_a)
        check(
            "F50h 示例代价：嵌套 80 缓存命中不会再被算成 0（费用不再偏高）",
            bool(usage_a) and usage_a["cached_tokens"] == 80,
        )

        # 4) 多行答案的字节哈希（端到端再确认一次）
        out_multi = os.path.join(RUNS, "_verify_m1_multiline")
        run_runner(
            [
                "--questions", DEV_QUESTIONS, "--ids", "D02", "--slot", "low",
                "--target", "stub", "--base-url", f"http://127.0.0.1:{PORT_OK}",
                "--max-calls", "1", "--out", out_multi,
            ],
            out_multi,
        )
        record = load_jsonl(os.path.join(out_multi, "requests.jsonl"))[0]
        with open(os.path.join(out_multi, record["final_output_ref"]), "rb") as fh:
            raw = fh.read()
        check(
            "F51 多行答案：文件字节哈希与记录一致，且行尾保持 LF",
            hashlib.sha256(raw).hexdigest() == record["answer_sha256"] and b"\r\n" not in raw and b"\n" in raw,
            (len(raw), b"\r\n" in raw),
        )
    finally:
        stop_stub(stub)


def main():
    for name in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
        if name.startswith("_verify_m1"):
            shutil.rmtree(os.path.join(RUNS, name), ignore_errors=True)
    shutil.rmtree(VERIFY_DIR, ignore_errors=True)
    os.makedirs(VERIFY_DIR, exist_ok=True)

    part_contract()
    part_router()
    part_ledger()
    part_grade()
    part_metrics()
    part_stub_chain()
    part_dir_reuse_and_guards()
    part_misclassification_and_manifest()
    part_thinking_flags()
    part_closing_edges()

    passed = len([c for c in CHECKS if c["ok"]])
    failed = [c for c in CHECKS if not c["ok"]]
    result = {
        "total": len(CHECKS),
        "passed": passed,
        "failed": len(failed),
        "checks": CHECKS,
        "criteria_sha256": _sha256(CRITERIA),
        "prices_sha256": _sha256(PRICES),
        "router_rules_version": router.RULES_VERSION,
        "note": "全程使用本地桩，零真实模型调用；临时单价表与临时题集只写在验收目录内。",
    }
    write_json(os.path.join(VERIFY_DIR, "verify_result.json"), result)

    print(f"\n==== M1 离线验收：{passed}/{len(CHECKS)} PASS，{len(failed)} FAIL ====")
    for item in failed:
        print(f"  FAIL {item['name']} -- {item['detail']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
