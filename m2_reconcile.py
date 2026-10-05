# -*- coding: utf-8 -*-
"""M2 只读对账（13 号 R2 / R2b）：父层占用 + 子运行 sealed 尝试 + 已形成请求行。

统一口径（供 m2_batch.py 与 tools/review_m2.py 共用，不复制两份逻辑）：
- 以**父层主循环的序号**为权威：每个计划项（seq / 题号 / 组别）最多计一次尝试。
- 尝试的费用与状态取子运行 `attempt_events.jsonl` 的 start/end 合并结果
  （复用 ledger.sealed_attempts 的既有语义：缺 end 的尝试一律按 unknown、费用未知）。
- **请求行存在与否不能决定"调用是否计账"**：子 ledger 结束了就保留它的费用；
  未闭合、或父层已占用但子层查不到事件的尝试，一律计未知。
- 逐题表按**计划**补齐：未执行 / 中断 / 发送状态未知 / 失败 都显式成行，
  不因为"没有请求行"就从表里消失。

不写任何文件：本模块只读，供批次汇总与人工派生共同调用。
"""
from __future__ import annotations

import json
import os

RAW_DIRNAME = "raw"
ATTEMPTS_NAME = "batch_attempts.jsonl"
MANIFEST_NAME = "batch_manifest.json"


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


def sealed_attempts_from_events(events):
    """把 start/end 事件合并成尝试列表；缺 end 的按 unknown、费用未知（同 ledger 语义）。"""
    starts, ends = {}, {}
    for event in events:
        key = (event.get("request_id"), event.get("attempt_no"))
        if event.get("kind") == "start":
            starts[key] = event
        elif event.get("kind") == "end":
            ends[key] = event
    sealed = []
    for key in sorted(starts, key=lambda k: (str(k[0]), k[1] or 0)):
        start = starts[key]
        end = ends.get(key)
        if end is None:
            sealed.append({
                "request_id": start.get("request_id"), "attempt_no": start.get("attempt_no"),
                "model": start.get("model"), "slot": start.get("slot"),
                "status": "unknown", "usage": None, "usage_known": False,
                "cost_cny": None, "latency_ms": None, "sealed": False,
            })
        else:
            sealed.append({
                "request_id": end.get("request_id"), "attempt_no": end.get("attempt_no"),
                "model": end.get("model") or start.get("model"), "slot": end.get("slot") or start.get("slot"),
                "status": end.get("status"), "usage": end.get("usage"),
                "usage_known": end.get("usage_known", False), "cost_cny": end.get("cost_cny"),
                "latency_ms": end.get("latency_ms"), "sealed": True,
            })
    return sealed


def item_dir(batch_dir, group, test_id):
    return os.path.join(batch_dir, RAW_DIRNAME, group, test_id)


def reconcile(batch_dir):
    """按计划返回逐项对账行与总账。只读，不写盘。"""
    manifest = load_json(os.path.join(batch_dir, MANIFEST_NAME))
    plan = manifest.get("plan") or []
    attempts = read_jsonl(os.path.join(batch_dir, ATTEMPTS_NAME))
    starts = {a["seq"]: a for a in attempts if a.get("kind") == "start" and "seq" in a}
    ends = {a["seq"]: a for a in attempts if a.get("kind") == "end" and "seq" in a}

    rows = []
    for item in plan:
        seq, test_id, group = item["seq"], item["test_id"], item["group"]
        start, end = starts.get(seq), ends.get(seq)
        sub = item_dir(batch_dir, group, test_id)
        sealed = sealed_attempts_from_events(read_jsonl(os.path.join(sub, "attempt_events.jsonl")))
        requests = read_jsonl(os.path.join(sub, "requests.jsonl"))
        request = requests[0] if requests else None

        known = 0.0
        unknown = 0
        for attempt in sealed:
            if attempt.get("status") == "unknown" or attempt.get("cost_cny") is None:
                unknown += 1
            else:
                known += attempt["cost_cny"]
        send_unknown = bool(start) and not sealed
        if send_unknown:
            # 父层已占用，但子层没有任何尝试事件 → 保守占一次，状态未知
            unknown += 1

        if start is None:
            state = "not_executed"
        elif send_unknown:
            state = "send_unknown"
        elif end is None:
            state = "interrupted"
        elif not sealed:
            state = "unknown_result"
        elif any(a.get("status") == "unknown" for a in sealed):
            state = "unclosed_attempt"
        elif request is not None and request.get("run_status") == "failed":
            state = "failed"
        elif request is not None:
            state = "answered"
        else:
            # 子层有结束事件与费用，但请求行未生成（例如答案写盘失败）
            state = "result_missing"

        latencies = [a["latency_ms"] for a in sealed if isinstance(a.get("latency_ms"), (int, float))]
        rows.append({
            "seq": seq, "test_id": test_id, "group": group, "state": state,
            "parent_start": bool(start), "parent_end": bool(end),
            "attempts_counted": len(sealed) + (1 if send_unknown else 0),
            "known_cost_cny": round(known, 10),
            "unknown_attempt_count": unknown,
            "latency_ms_total": (request or {}).get("latency_ms_total") if request else (max(latencies) if latencies else None),
            "request_id": (request or {}).get("request_id"),
            "run_status": (request or {}).get("run_status"),
            "failure_kind": (request or {}).get("failure_kind"),
            "has_request_row": request is not None,
            "attempts": sealed,
        })

    known_total = round(sum(r["known_cost_cny"] for r in rows), 10)
    unknown_total = sum(r["unknown_attempt_count"] for r in rows)
    not_executed = [r for r in rows if r["state"] == "not_executed"]

    per_group = {}
    for group in sorted({r["group"] for r in rows}):
        group_rows = [r for r in rows if r["group"] == group]
        g_known = round(sum(r["known_cost_cny"] for r in group_rows), 10)
        g_unknown = sum(r["unknown_attempt_count"] for r in group_rows)
        per_group[group] = {
            "planned": len(group_rows),
            "executed": len([r for r in group_rows if r["state"] != "not_executed"]),
            "answered": len([r for r in group_rows if r["state"] == "answered"]),
            "failed": len([r for r in group_rows if r["state"] == "failed"]),
            "interrupted": len([r for r in group_rows if r["state"] in ("interrupted", "send_unknown", "unclosed_attempt")]),
            "known_cost_cny": g_known,
            "unknown_attempt_count": g_unknown,
            "total_cost_cny": None if g_unknown else g_known,
        }

    return {
        "batch_id": manifest.get("batch_id"),
        "manifest": manifest,
        "rows": rows,
        "per_group": per_group,
        "totals": {
            "planned_items": len(rows),
            "executed_items": len(rows) - len(not_executed),
            "remaining_items": len(not_executed),
            "attempts_counted": sum(r["attempts_counted"] for r in rows),
            "known_cost_cny": known_total,
            "unknown_attempt_count": unknown_total,
            "total_cost_cny": None if unknown_total else known_total,
            "occurred_cost_note": "本批已发生费用（含失败与未知尝试；已知部分不完整时总费用为 null）",
            "complete_experiment_cost_cny": None if not_executed else (None if unknown_total else known_total),
            "complete_experiment_note": "存在未执行计划项时，完整实验费用不成立（不能与完整组成本直接比较或计算节省率）",
        },
    }


def table_skeleton(reconcile_result, questions):
    """按计划题号 × 三组补齐逐题表骨架（未执行/中断同样成行）。"""
    table = {group: {} for group in ("low", "high", "route")}
    for row in reconcile_result["rows"]:
        question = questions.get(row["test_id"], {})
        table.setdefault(row["group"], {})[row["test_id"]] = {
            "task_type_gold": question.get("task_type_gold"),
            "state": row["state"],
            "run_status": row["run_status"],
            "has_request_row": row["has_request_row"],
            "attempts_counted": row["attempts_counted"],
            "known_cost_cny": row["known_cost_cny"],
            "unknown_attempt_count": row["unknown_attempt_count"],
            "latency_ms_total": row["latency_ms_total"],
            "mechanized_pass": None,
            "human_status": "absent",
            "human_filled": False,
            "human_pending_items": [],
            "human_verdict": None,
            "final_pass": False,
            "router_reason": None,
            "chosen_model": None,
        }
    return table
