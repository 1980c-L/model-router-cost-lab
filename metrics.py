# -*- coding: utf-8 -*-
"""指标计算：口径写死在这里，避免报告里各说各话。

依据 03 号 §1.2：
- 端到端达标率（主指标）：分母 = **计划题数**；失败的题记未达标，不从分母删除。
- 获得回答后的内容达标率（辅助）：分母 = 该组实际取得可用回答的题数，必须披露分母。
- 配对比较：只用各组都取得回答的**共同题目集**，并列明题号与样本数。
- 运行失败率：失败请求数 / 计划请求数，按原因分类。
"""
from __future__ import annotations


def _passes(quality):
    if not isinstance(quality, dict):
        return False
    if "overall_pass" in quality:
        return bool(quality["overall_pass"])
    return bool(quality.get("mechanized_pass"))


def end_to_end_pass_rate(requests, planned_count):
    """主指标：分母固定为计划题数。"""
    if planned_count <= 0:
        return {"planned": planned_count, "passed": 0, "rate": None, "denominator": 0}
    passed = len([r for r in requests if r.get("run_status") == "ok" and _passes(r.get("quality"))])
    return {
        "planned": planned_count,
        "executed": len(requests),
        "passed": passed,
        "denominator": planned_count,
        "rate": round(passed / planned_count, 6),
        "note": "失败与未执行的题按未达标计入分母",
    }


def content_pass_rate(requests):
    """辅助：分母是实际取得回答的题数。"""
    answered = [r for r in requests if r.get("run_status") == "ok"]
    if not answered:
        return {"answered": 0, "passed": 0, "denominator": 0, "rate": None}
    passed = len([r for r in answered if _passes(r.get("quality"))])
    return {
        "answered": len(answered),
        "passed": passed,
        "denominator": len(answered),
        "rate": round(passed / len(answered), 6),
        "note": "不替代主指标",
    }


def paired_compare(requests_by_group):
    """只用各组都取得回答的共同题目集做配对比较。"""
    groups = list(requests_by_group.keys())
    if not groups:
        return {"groups": [], "common_ids": [], "samples": 0, "table": {}}
    per_group_ids = {}
    for group, requests in requests_by_group.items():
        per_group_ids[group] = {
            r["test_id"] for r in requests if r.get("run_status") == "ok"
        }
    common = set.intersection(*per_group_ids.values()) if per_group_ids else set()
    table = {}
    for group, requests in requests_by_group.items():
        by_id = {r["test_id"]: r for r in requests if r.get("run_status") == "ok"}
        table[group] = {
            "common_answered": len(common),
            "common_passed": len([tid for tid in common if _passes(by_id[tid].get("quality"))]),
        }
    return {
        "groups": groups,
        "common_ids": sorted(common),
        "samples": len(common),
        "table": table,
    }


def run_failure_rate(requests, planned_count):
    failures = [r for r in requests if r.get("run_status") == "failed"]
    by_kind = {}
    for item in failures:
        kind = item.get("failure_kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    rate = round(len(failures) / planned_count, 6) if planned_count > 0 else None
    return {
        "planned": planned_count,
        "failures": len(failures),
        "rate": rate,
        "by_kind": by_kind,
        "note": "网络/平台故障不等于内容错误，单独归类",
    }


def cost_summary(requests):
    """费用汇总：任一在线尝试未知 → 总费用未知（另给已知部分）。"""
    known = 0.0
    unknown = 0
    for request in requests:
        for attempt in request.get("attempts") or []:
            if attempt.get("offline_eval"):
                continue
            cost = attempt.get("cost_cny")
            if isinstance(cost, (int, float)):
                known += cost
            else:
                unknown += 1
    known = round(known, 10)
    return {
        "known_cost_cny": known,
        "unknown_attempt_count": unknown,
        "total_cost_cny": None if unknown else known,
        "note": "按公开单价折算；未知不得记 0",
    }
