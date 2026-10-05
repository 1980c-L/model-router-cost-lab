# -*- coding: utf-8 -*-
"""规则路由 v0：有序规则表 + 冲突处理 + 默认档 + 规则版本 + 识别来源。

设计约束（来自 03 号复审 §1.1）：
- 必须写清"依据哪些输入、哪些规则先执行、没有命中走哪档"。
- 路由器**只吃请求文本与显式约束**；不读题号、不读标准答案、不读人工期望档位。
  为保证这一点，本模块的函数签名里根本不存在这些参数（verify_m1.py 另有源码级断言）。
- 区分两个字段：
    task_type_gold  —— 题集的人工标签，只用于评测侧，**不进入本模块**；
    task_type_pred  —— 本模块识别出的任务类型 + 来源（rule / user / none）。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

RULES_VERSION = "router_rules_v2"
# v2 变更（05 号 §3 规则观察）：R4 的长输出线索从"出现『至少』即算"缩窄为
# "必须与数量+单位共现（如『不少于 800 字』）"，修掉"请至少用 1 句话概括"被当成长输出的误判。

# 阈值只在开发集上调；改动必须升版本号，否则历史结果的规则版本会失真。
LONG_INPUT_CHARS = 6000
MULTI_MATERIAL_SECTIONS = 3
MIN_JSON_FIELDS = 6
MULTI_ITEM_THRESHOLD = 4

COMPARISON_CUES = (
    "比较", "对比", "区别", "差异", "优缺点", "为什么", "原因",
    "推导", "计算", "步骤", "设计", "分析", "权衡", "取舍", "评估",
)
LONG_OUTPUT_PATTERNS = (
    r"(?:不少于|至少)\s*\d+\s*(?:字|词|token)",
    r"\d+\s*字(?:以上|左右|起)",
    r"详细",
    r"展开",
    r"完整说明",
)
JSON_CUES = ("json", "字段", "结构化", "schema")
QA_CUES = ("根据资料", "根据材料", "根据文档", "文中", "资料中", "材料中", "依据资料")
SUMMARY_CUES = ("总结", "概括", "摘要", "提炼", "简述全文")
ITEM_RE = re.compile(r"(?:列举|列出|至少)\s*(\d+)\s*(?:项|条|个)?")

DEFAULT_SLOT = "low"

# 有序规则表：从上往下，**首个命中生效**（这就是冲突处理规则）。
RULES = (
    ("R1_long_input", "输入长度超过阈值（需要长上下文处理）", "high"),
    ("R2_multi_material", "材料段数 >= 阈值（跨段整合）", "high"),
    ("R3_comparison_or_multi_item", "含比较/推理线索，或要求列举 >= 4 项", "high"),
    ("R4_long_output", "要求长输出", "high"),
    ("R5_strict_json_many_fields", "要求严格 JSON 且字段数 >= 6", "high"),
)


@dataclass
class Features:
    input_chars: int
    material_sections: int
    comparison_cues: list
    require_json: bool
    required_fields: int
    require_long_output: bool
    items_requested: int
    has_qa_cues: bool
    has_summary_cues: bool


def extract_features(request_text, constraints=None, instruction_text=None):
    """从请求文本与显式约束中抽取特征。

    - 长度与材料段数用**全文**（题面 + 材料）；
    - 线索词（比较/推理、长输出要求、任务类型、JSON 要求）只在**题面**上匹配：
      材料里出现"评估"这类词，不代表任务需要多步推理。
      （M1 桩跑抓到的真实误判：D01 因材料含"风险等级评估"被送进 high。）
    - 不传 `instruction_text` 时退回全文匹配，仅供单元级调用。
    """
    constraints = constraints or {}
    text = request_text or ""
    instruction = text if instruction_text is None else (instruction_text or "")
    lowered = instruction.lower()

    # 材料段数：优先用调用方显式声明的 material_count，其次数文本里的 [材料N] 标记，
    # 最后才退回按空行分段（兜底）。避免"题面 + 资料标题 + 单条材料"被误判成多段材料。
    material_sections = constraints.get("material_count")
    if type(material_sections) is not int:
        material_sections = len(re.findall(r"\[材料\s*\d+\]", text))
    if material_sections <= 0:
        raw_sections = re.split(r"\n\s*(?:---+|===+)\s*\n|\n\s*\n", text)
        material_sections = len([s for s in raw_sections if s.strip()])

    required_fields = constraints.get("required_fields")
    if isinstance(required_fields, list):
        required_count = len(required_fields)
    else:
        required_count = 0

    items_match = ITEM_RE.search(instruction)
    items_requested = int(items_match.group(1)) if items_match and items_match.group(1) else 0

    features = Features(
        input_chars=len(text),
        material_sections=material_sections,
        comparison_cues=[cue for cue in COMPARISON_CUES if cue in instruction],
        require_json=bool(constraints.get("require_json")) or any(c in lowered for c in JSON_CUES),
        required_fields=required_count,
        require_long_output=any(re.search(pattern, instruction) for pattern in LONG_OUTPUT_PATTERNS),
        items_requested=items_requested,
        has_qa_cues=any(cue in instruction for cue in QA_CUES),
        has_summary_cues=any(cue in instruction for cue in SUMMARY_CUES),
    )
    return features


def _rule_hit(rule_id, features):
    if rule_id == "R1_long_input":
        return features.input_chars > LONG_INPUT_CHARS
    if rule_id == "R2_multi_material":
        return features.material_sections >= MULTI_MATERIAL_SECTIONS
    if rule_id == "R3_comparison_or_multi_item":
        return bool(features.comparison_cues) or features.items_requested >= MULTI_ITEM_THRESHOLD
    if rule_id == "R4_long_output":
        return features.require_long_output
    if rule_id == "R5_strict_json_many_fields":
        return features.require_json and features.required_fields >= MIN_JSON_FIELDS
    raise KeyError(f"未知规则：{rule_id}")


def predict_task_type(features, user_task_type=None):
    """识别任务类型与来源。用户显式指定时来源是 user，不包装成自动识别。"""
    if user_task_type:
        return user_task_type, "user"
    if features.require_json:
        return "extract", "rule"
    if features.has_qa_cues:
        return "qa_grounded", "rule"
    if features.has_summary_cues:
        return "summary", "rule"
    return "unknown", "rule"


def decide(request_text, constraints=None, user_task_type=None, instruction_text=None):
    """返回一次路由决策（可完整落进日志的解释卡）。"""
    features = extract_features(request_text, constraints, instruction_text)
    hit_ids = [rule_id for rule_id, _desc, _slot in RULES if _rule_hit(rule_id, features)]

    matched_rule = None
    chosen_slot = DEFAULT_SLOT
    reason = f"未命中任何高成本特征，走默认档 {DEFAULT_SLOT}"
    for rule_id, desc, slot in RULES:
        if rule_id in hit_ids:
            matched_rule = rule_id
            chosen_slot = slot
            reason = f"{rule_id}：{desc}"
            break

    task_type_pred, task_type_source = predict_task_type(features, user_task_type)

    return {
        "rule_version": RULES_VERSION,
        "matched_rule": matched_rule or f"DEFAULT_{DEFAULT_SLOT.upper()}",
        "reason": reason,
        "chosen_slot": chosen_slot,
        "default_applied": matched_rule is None,
        "hit_rules": hit_ids,
        "conflict_resolution": "按规则表顺序取首个命中（高优先级在前）",
        "features": asdict(features),
        "task_type_pred": task_type_pred,
        "task_type_pred_source": task_type_source,
    }
