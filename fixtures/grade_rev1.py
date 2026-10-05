# -*- coding: utf-8 -*-
"""机检判据（可机检的部分）。

口径（来自 02 号 §4 与 03 号 §1.5）：
- `mechanized_pass`：本文件能自动判定的部分。
- `human_required`：需要人工核的项；**人工完成前不得算整体达标**（`overall_pass` 为 False）。
- 三类任务的整体达标合成条件写在 `eval/criteria_v1.json`，本文件按它实现。
- 判据与题目参数必须在跑之前冻结；改动必须升版本号并重跑三组。

判据版本相关：
- `criteria_m2_v3`（候选，2026-09-29）起，摘要题可声明 `coverage_points`
  （`[{"id": ..., "variants": [...]}]`）：命中判据是**预先冻结的字面变体**之一作为子串出现，
  不做空白归一、不做语义判断；未声明 `coverage_points` 的题集/判据仍走
  `coverage_keywords` 旧路径，行为与本改动前逐字节一致。
"""
from __future__ import annotations

import json
import re

# 05 号 §2.6：题面与系统提示要求"严格 JSON、不要代码块、不要额外文字"，
# 因此**计分路径不做代码块剥离、不抽内部 JSON**；宽松解析只留给人工分析。
LENIENT_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _result(mechanized_pass, detail, human_pending=None, reason="", format_pass=None, content_pass=None):
    human_pending = human_pending or []
    return {
        "overall_pass": bool(mechanized_pass and not human_pending),
        "mechanized_pass": bool(mechanized_pass),
        "format_pass": format_pass,
        "content_pass": content_pass,
        "human_required": bool(human_pending),
        "human_filled": False,
        "human_pending": human_pending,
        "reason": reason,
        "detail": detail,
    }


def parse_strict_json(content):
    """严格解析：整体输出去掉首尾空白后直接解析，不剥代码块、不抽内部 JSON。"""
    text = (content or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_lenient_for_analysis(content):
    """宽松解析：只用于人工分析，**不参与判定**。"""
    text = (content or "").strip()
    match = LENIENT_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        return None


def _strict_equal(got, expected) -> bool:
    """逐字段比较：布尔与数字分开、字符串去首尾空白后精确比较、数字按容差。

    修复 05 号 §2.6 的反例：标准答案 `amount=1`，回答 `amount=true` 不得算通过
    （Python 的 `True == 1.0` 会把它蒙过去）。
    """
    if isinstance(expected, bool) or isinstance(got, bool):
        return type(got) is bool and type(expected) is bool and got == expected
    if type(expected) in (int, float) and type(got) in (int, float):
        return abs(float(got) - float(expected)) <= 1e-9
    if isinstance(expected, str) and isinstance(got, str):
        return got.strip() == expected.strip()
    return type(got) is type(expected) and got == expected


def grade_extract(content, question, criteria):
    constraints = question.get("constraints") or {}
    required = constraints.get("required_fields") or []
    field_types = constraints.get("field_types") or {}
    answer_key = question.get("answer_key") or {}
    parsed = parse_strict_json(content)
    detail = {
        "json_parsable": parsed is not None,
        "strict_parse": True,
        "missing_fields": [],
        "mismatched_fields": [],
        "type_mismatched": [],
    }

    if parsed is None:
        detail["reason"] = "整体输出不是可解析的 JSON（严格模式不剥代码块、不抽内部 JSON）"
        return _result(False, detail, reason="JSON 不可解析（严格模式）", format_pass=False, content_pass=False)

    if not isinstance(parsed, dict):
        detail["reason"] = "JSON 顶层不是对象"
        return _result(False, detail, reason="JSON 顶层不是对象", format_pass=False, content_pass=False)

    missing = [field for field in required if field not in parsed]
    detail["missing_fields"] = missing

    type_mismatched = []
    if isinstance(field_types, dict):
        for field, expected_type in field_types.items():
            if field not in parsed:
                continue
            got = parsed[field]
            ok = (
                (expected_type == "number" and type(got) in (int, float) and type(got) is not bool)
                or (expected_type == "string" and isinstance(got, str))
                or (expected_type == "boolean" and type(got) is bool)
            )
            if not ok:
                type_mismatched.append(
                    {"field": field, "expected_type": expected_type, "got_type": type(got).__name__}
                )
    detail["type_mismatched"] = type_mismatched

    mismatched = []
    for field, expected in answer_key.items():
        got = parsed.get(field)
        if not _strict_equal(got, expected):
            mismatched.append({"field": field, "expected": expected, "got": got})
    detail["mismatched_fields"] = mismatched

    format_pass = (not missing) and (not type_mismatched)
    content_pass = not mismatched
    mechanized_pass = format_pass and content_pass
    reason = ""
    if missing:
        reason = "缺必填字段：" + ",".join(missing)
    elif type_mismatched:
        reason = "字段类型不符：" + ",".join(item["field"] for item in type_mismatched)
    elif mismatched:
        reason = "字段值不一致：" + ",".join(item["field"] for item in mismatched)
    detail["parsed"] = parsed
    return _result(mechanized_pass, detail, reason=reason, format_pass=format_pass, content_pass=content_pass)


def _match_coverage(text, params):
    """返回 (命中标识, 未命中标识, 覆盖率, 逐点明细或 None)。

    - 声明 `coverage_points` 时（criteria_m2_v3 起）：每个点给出**冻结的字面变体组**，
      任一变体作为子串出现即算该点命中；标识用点的 `id`。明细里只记录命中的那一个变体。
    - 未声明时沿用 `coverage_keywords` 旧路径（标识即关键词本身），行为不变。
    """
    points = params.get("coverage_points")
    if isinstance(points, list) and points:
        detail = []
        for point in points:
            point = point or {}
            variants = [v for v in (point.get("variants") or []) if isinstance(v, str)]
            matched = next((v for v in variants if v in text), None)
            detail.append({
                "point": point.get("id"),
                "hit": matched is not None,
                "matched": matched,
                "variants": variants,
            })
        hit = [d["point"] for d in detail if d["hit"]]
        missing = [d["point"] for d in detail if not d["hit"]]
        return hit, missing, round(len(hit) / len(detail), 4), detail

    keywords = params.get("coverage_keywords") or []
    hit = [word for word in keywords if word in text]
    ratio = round(len(hit) / len(keywords), 4) if keywords else 1.0
    return hit, [w for w in keywords if w not in hit], ratio, None


def grade_summary(content, question, criteria):
    text = (content or "").strip()
    params = question.get("criteria") or {}
    max_chars = params.get("max_chars") or (question.get("constraints") or {}).get("max_chars")
    forbidden_keywords = params.get("forbidden_keywords") or []
    threshold = criteria.get("coverage_threshold", 0.6)

    # 11 号 §3：M2 判据显式声明 summary_human_items 时才启用逐事实语义覆盖项；
    # criteria_v1 未声明，历史行为（仅 no_added_content_by_human）保持不变。
    human_items = criteria.get("summary_human_items")
    if not isinstance(human_items, list) or not human_items:
        human_items = ["no_added_content_by_human"]

    length_pass = True if not max_chars else len(text) <= max_chars
    hit, missing, coverage_ratio, points_detail = _match_coverage(text, params)
    coverage_pass = coverage_ratio >= threshold
    forbidden_hit = [word for word in forbidden_keywords if word in text]

    detail = {
        "chars": len(text),
        "max_chars": max_chars,
        "length_pass": length_pass,
        "coverage_hit": hit,
        "coverage_missing": missing,
        "coverage_ratio": coverage_ratio,
        "coverage_pass": coverage_pass,
        "forbidden_hit": forbidden_hit,
    }
    if points_detail is not None:
        detail["coverage_points_detail"] = points_detail
        detail["coverage_match_rule"] = criteria.get("coverage_match_rule", {}).get(
            "rule_version", "coverage_points_v1")
    mechanized_pass = length_pass and coverage_pass and not forbidden_hit
    reason = ""
    if not length_pass:
        reason = f"超长（{len(text)} > {max_chars}）"
    elif not coverage_pass:
        reason = f"覆盖面不足（{coverage_ratio} < {threshold}）"
    elif forbidden_hit:
        reason = "出现禁止内容：" + ",".join(forbidden_hit)
    return _result(
        mechanized_pass,
        detail,
        human_pending=human_items,
        reason=reason,
    )


def grade_qa(content, question, criteria):
    text = (content or "").strip()
    refusal_phrase = criteria.get("refusal_phrase", "资料中未找到相关依据")
    expect_refusal = bool(question.get("expect_refusal"))
    not_empty = len(text) > 0
    refusal_ok = (refusal_phrase in text) if expect_refusal else True
    detail = {
        "chars": len(text),
        "not_empty": not_empty,
        "expect_refusal": expect_refusal,
        "refusal_phrase_present": refusal_phrase in text,
        "refusal_ok": refusal_ok,
    }
    mechanized_pass = not_empty and refusal_ok
    reason = ""
    if not not_empty:
        reason = "空回答"
    elif not refusal_ok:
        reason = "期望拒答但未出现拒答短语"
    return _result(
        mechanized_pass,
        detail,
        human_pending=["facts_correct", "no_unsupported_claim", "citation_supports"],
        reason=reason,
    )


def grade(task_type, content, question, criteria):
    if task_type == "extract":
        return grade_extract(content, question, criteria)
    if task_type == "summary":
        return grade_summary(content, question, criteria)
    if task_type == "qa_grounded":
        return grade_qa(content, question, criteria)
    return _result(
        False,
        {"reason": "task_type_pred=unknown，无法选择判据"},
        human_pending=["task_type_unrecognized"],
        reason="任务类型未识别",
    )
