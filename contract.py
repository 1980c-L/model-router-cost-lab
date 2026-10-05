# -*- coding: utf-8 -*-
"""数据契约 v1：请求级与尝试级字段定义与校验。

三条硬约束（来自 03 号复审，写在这里作为实现依据）：
1. 尝试级逐次记账；`usage` 未知时 `cost_cny` 必须是 None，不得记 0。
2. 请求级 `total_cost_cny` 只要有一次"在线尝试"费用未知或未闭合，就整体为 None。
3. 失败的题不从主指标分母里删除（分母 = 计划题数），失败原因单独归类。
"""
from __future__ import annotations

import math

GROUPS = ("low", "high", "route")
TASK_TYPES = ("extract", "qa_grounded", "summary", "unknown")
TASK_TYPE_SOURCES = ("rule", "user", "none")
RUN_STATUS = ("ok", "failed")
FAILURE_KINDS = ("network", "platform", "format", "timeout", "unknown")
ATTEMPT_PURPOSES = ("generate", "route_classify", "selfcheck", "upgrade_gen", "judge")
ATTEMPT_STATUS = ("pending", "ok", "error", "refused", "format_invalid", "unknown")
ATTEMPT_EVENT_KINDS = ("start", "end")

REQUIRED_REQUEST_FIELDS = (
    "request_id",
    "ts",
    "group",
    "task_type_pred",
    "task_type_pred_source",
    "test_id",
    "input_ref",
    "attempts",
    "run_status",
    "total_cost_cny",
    "known_cost_cny",
    "unknown_attempt_count",
    "latency_ms_total",
    "quality",
)

REQUIRED_ATTEMPT_FIELDS = (
    "attempt_no",
    "model",
    "slot",
    "purpose",
    "status",
    "usage",
    "usage_known",
    "unit_price_version",
    "cost_cny",
    "latency_ms",
    "offline_eval",
)

REQUEST_OPTIONAL_DEFAULTS = {
    "task_type_gold": None,
    "failure_kind": None,
    "notes": "",
    "final_output_ref": None,
}

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens")


def _is_int(value) -> bool:
    """精确整数判定：排除 bool（RAG 线踩过 `isinstance(True, int)` 的坑）。"""
    return type(value) is int


def _is_number_or_none(value) -> bool:
    if value is None:
        return True
    return type(value) in (int, float)


def _is_finite_nonneg_or_none(value) -> bool:
    """金额与耗时：必须是有限、非负的精确数字（bool 不算），或 null。"""
    if value is None:
        return True
    if type(value) not in (int, float):
        return False
    return math.isfinite(value) and value >= 0


MONEY_TOLERANCE = 1e-9


def _money_equal(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if type(left) not in (int, float) or type(right) not in (int, float):
        return False
    return abs(float(left) - float(right)) <= MONEY_TOLERANCE


def validate_usage(usage) -> list:
    errors = []
    if usage is None:
        return errors
    if not isinstance(usage, dict):
        return ["usage 必须是对象或 null"]
    for key in USAGE_FIELDS:
        if key not in usage:
            errors.append(f"usage 缺字段 {key}")
            continue
        if not _is_int(usage[key]):
            errors.append(f"usage.{key} 必须是精确整数（bool 不算）")
        elif usage[key] < 0:
            errors.append(f"usage.{key} 不能为负")
    return errors


def validate_attempt(attempt) -> list:
    errors = []
    if not isinstance(attempt, dict):
        return ["attempt 必须是对象"]
    for field in REQUIRED_ATTEMPT_FIELDS:
        if field not in attempt:
            errors.append(f"attempt 缺字段 {field}")
    if errors:
        return errors

    if not _is_int(attempt["attempt_no"]) or attempt["attempt_no"] < 1:
        errors.append("attempt_no 必须是 >=1 的精确整数")
    if attempt["slot"] not in GROUPS:
        errors.append(f"slot 必须是 {GROUPS} 之一")
    if attempt["purpose"] not in ATTEMPT_PURPOSES:
        errors.append(f"purpose 必须是 {ATTEMPT_PURPOSES} 之一")
    if attempt["status"] not in ATTEMPT_STATUS:
        errors.append(f"status 必须是 {ATTEMPT_STATUS} 之一")
    if type(attempt["usage_known"]) is not bool:
        errors.append("usage_known 必须是 bool")
    if type(attempt["offline_eval"]) is not bool:
        errors.append("offline_eval 必须是 bool")
    if not _is_finite_nonneg_or_none(attempt["cost_cny"]):
        errors.append("cost_cny 必须是有限、非负的数字或 null")
    if not _is_finite_nonneg_or_none(attempt["latency_ms"]):
        errors.append("latency_ms 必须是有限、非负的数字或 null")
    errors.extend(validate_usage(attempt["usage"]))

    # 契约核心：用量未知 → 费用必须未知
    if attempt["usage_known"] is False and attempt["cost_cny"] is not None:
        errors.append("usage_known=false 时 cost_cny 必须为 null（未知不得记 0）")
    if attempt["usage"] is None and attempt["cost_cny"] is not None:
        errors.append("usage 为 null 时 cost_cny 必须为 null")
    return errors


def validate_request(request) -> list:
    errors = []
    if not isinstance(request, dict):
        return ["request 必须是对象"]
    for field in REQUIRED_REQUEST_FIELDS:
        if field not in request:
            errors.append(f"request 缺字段 {field}")
    if errors:
        return errors

    if request["group"] not in GROUPS:
        errors.append(f"group 必须是 {GROUPS} 之一")
    if request["task_type_pred"] not in TASK_TYPES:
        errors.append(f"task_type_pred 必须是 {TASK_TYPES} 之一")
    if request["task_type_pred_source"] not in TASK_TYPE_SOURCES:
        errors.append(f"task_type_pred_source 必须是 {TASK_TYPE_SOURCES} 之一")
    gold = request.get("task_type_gold")
    if gold is not None and gold not in TASK_TYPES:
        errors.append(f"task_type_gold 必须是 {TASK_TYPES} 之一或 null")
    if request["run_status"] not in RUN_STATUS:
        errors.append(f"run_status 必须是 {RUN_STATUS} 之一")
    if request["run_status"] == "failed":
        if request.get("failure_kind") not in FAILURE_KINDS:
            errors.append(f"run_status=failed 时 failure_kind 必须是 {FAILURE_KINDS} 之一")
    if not _is_int(request["unknown_attempt_count"]) or request["unknown_attempt_count"] < 0:
        errors.append("unknown_attempt_count 必须是 >=0 的精确整数")
    if not _is_number_or_none(request["total_cost_cny"]):
        errors.append("total_cost_cny 必须是数字或 null")
    if not _is_number_or_none(request["known_cost_cny"]):
        errors.append("known_cost_cny 必须是数字")
    if type(request["known_cost_cny"]) not in (int, float):
        errors.append("known_cost_cny 必须是数字（可为 0）")
    if request["unknown_attempt_count"] > 0 and request["total_cost_cny"] is not None:
        errors.append("存在未知尝试时 total_cost_cny 必须为 null")
    if not isinstance(request["input_ref"], dict) or "text_sha256" not in request["input_ref"]:
        errors.append("input_ref 必须含 text_sha256")
    if not isinstance(request["quality"], dict):
        errors.append("quality 必须是对象")

    attempts = request["attempts"]
    if not isinstance(attempts, list):
        errors.append("attempts 必须是数组")
    else:
        seen = set()
        for attempt in attempts:
            errors.extend(validate_attempt(attempt))
            if isinstance(attempt, dict) and _is_int(attempt.get("attempt_no")):
                if attempt["attempt_no"] in seen:
                    errors.append(f"attempt_no 重复：{attempt['attempt_no']}")
                seen.add(attempt["attempt_no"])
        errors.extend(_validate_derived_totals(request, attempts))
    return errors


def _validate_derived_totals(request, attempts):
    """请求级派生数字必须能由**在线**尝试重新推导出来（05 号 §2.7）。

    否则"字段齐全但数字自相矛盾"的记录会被当成"通过契约校验"。
    """
    errors = []
    online = [a for a in attempts if isinstance(a, dict) and a.get("offline_eval") is False]
    derived_unknown = 0
    derived_known = 0.0
    for attempt in online:
        cost = attempt.get("cost_cny")
        if attempt.get("status") == "unknown" or cost is None:
            derived_unknown += 1
            continue
        if not _is_finite_nonneg_or_none(cost):
            errors.append("在线尝试的 cost_cny 非法，无法推导汇总")
            continue
        derived_known += float(cost)

    derived_known = round(derived_known, 10)
    derived_total = None if derived_unknown else derived_known

    if not _is_int(request.get("unknown_attempt_count")) or request["unknown_attempt_count"] != derived_unknown:
        errors.append(
            "unknown_attempt_count 与在线尝试不一致：记录 "
            f"{request.get('unknown_attempt_count')}，推导 {derived_unknown}"
        )
    if not _money_equal(request.get("known_cost_cny"), derived_known):
        errors.append(f"known_cost_cny 与在线尝试之和不一致：记录 {request.get('known_cost_cny')}，推导 {derived_known}")
    if not _money_equal(request.get("total_cost_cny"), derived_total):
        errors.append(f"total_cost_cny 与推导值不一致：记录 {request.get('total_cost_cny')}，推导 {derived_total}")
    return errors


def new_attempt(
    attempt_no,
    model,
    slot,
    purpose="generate",
    status="pending",
    usage=None,
    usage_known=False,
    unit_price_version=None,
    cost_cny=None,
    latency_ms=None,
    offline_eval=False,
    extra=None,
):
    attempt = {
        "attempt_no": attempt_no,
        "model": model,
        "slot": slot,
        "purpose": purpose,
        "status": status,
        "usage": usage,
        "usage_known": usage_known,
        "unit_price_version": unit_price_version,
        "cost_cny": cost_cny,
        "latency_ms": latency_ms,
        "offline_eval": offline_eval,
    }
    if extra:
        attempt.update(extra)
    return attempt


def new_request(
    request_id,
    ts,
    group,
    task_type_pred,
    task_type_pred_source,
    test_id,
    input_ref,
    attempts,
    run_status,
    total_cost_cny,
    known_cost_cny,
    unknown_attempt_count,
    latency_ms_total,
    quality,
    task_type_gold=None,
    failure_kind=None,
    notes="",
    final_output_ref=None,
    router=None,
):
    request = {
        "request_id": request_id,
        "ts": ts,
        "group": group,
        "task_type_pred": task_type_pred,
        "task_type_pred_source": task_type_pred_source,
        "test_id": test_id,
        "input_ref": input_ref,
        "attempts": attempts,
        "run_status": run_status,
        "total_cost_cny": total_cost_cny,
        "known_cost_cny": known_cost_cny,
        "unknown_attempt_count": unknown_attempt_count,
        "latency_ms_total": latency_ms_total,
        "quality": quality,
        "task_type_gold": task_type_gold,
        "failure_kind": failure_kind,
        "notes": notes,
        "final_output_ref": final_output_ref,
        "router": router,
    }
    return request
