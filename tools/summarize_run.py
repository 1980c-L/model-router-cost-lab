# -*- coding: utf-8 -*-
"""汇总一次或多次运行的产物：逐题结果 + 组级指标 + 两两配对比较。

用法：
    py -3 tools\\summarize_run.py runs\\m1-official-low runs\\m1-official-high

只读产物（requests.jsonl / summary.json），不联网、不改任何文件。
"""
from __future__ import annotations

import itertools
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


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


def attempt_cost(record):
    attempts = record.get("attempts") or []
    if not attempts:
        return None
    return attempts[0].get("cost_cny")


def main(dirs):
    groups = {}
    for directory in dirs:
        requests = load_jsonl(os.path.join(directory, "requests.jsonl"))
        summary_path = os.path.join(directory, "summary.json")
        if not os.path.exists(summary_path):
            print(f"[跳过] {directory} 没有 summary.json")
            continue
        with open(summary_path, "r", encoding="utf-8") as fh:
            summary = json.load(fh)
        group = summary.get("group") or os.path.basename(directory)
        groups[group] = {"dir": directory, "requests": requests, "summary": summary}

        print(f"=== {group}  ({directory}) ===")
        for record in requests:
            attempts = record.get("attempts") or [{}]
            usage = attempts[0].get("usage") or {}
            print(
                f"  {record.get('test_id'):<5} {record.get('run_status'):<7} "
                f"pass={str(record.get('quality', {}).get('overall_pass')):<5} "
                f"ms={record.get('latency_ms_total'):>7} out_tok={usage.get('completion_tokens')} "
                f"cost={attempt_cost(record)} pred={record.get('task_type_pred')} gold={record.get('task_type_gold')}"
            )
        metrics = summary.get("metrics", {})
        cost = metrics.get("cost", {})
        e2e = metrics.get("end_to_end", {})
        content = metrics.get("content", {})
        failure = metrics.get("failure", {})
        print(
            f"  -- actual_attempts={summary.get('actual_attempts')} successful_calls={summary.get('successful_calls')} "
            f"manifest={str(summary.get('manifest_sha256'))[:12]}…"
        )
        print(
            f"  -- 费用：已知 ¥{cost.get('known_cost_cny')} / 未知 {cost.get('unknown_attempt_count')} 次 / "
            f"总额 {cost.get('total_cost_cny')}"
        )
        print(
            f"  -- 端到端达标 {e2e.get('passed')}/{e2e.get('denominator')}（rate={e2e.get('rate')}）· "
            f"内容达标 {content.get('passed')}/{content.get('denominator')} · "
            f"失败 {failure.get('failures')} {failure.get('by_kind')}"
        )
        print()

    if len(groups) > 1:
        for name_a, name_b in itertools.combinations(groups, 2):
            ok_a = {r["test_id"]: r for r in groups[name_a]["requests"] if r.get("run_status") == "ok"}
            ok_b = {r["test_id"]: r for r in groups[name_b]["requests"] if r.get("run_status") == "ok"}
            common = sorted(set(ok_a) & set(ok_b))
            passed_a = len([t for t in common if ok_a[t].get("quality", {}).get("overall_pass")])
            passed_b = len([t for t in common if ok_b[t].get("quality", {}).get("overall_pass")])
            cost_a = round(sum((attempt_cost(ok_a[t]) or 0) for t in common), 6)
            cost_b = round(sum((attempt_cost(ok_b[t]) or 0) for t in common), 6)
            print(f"[配对] {name_a} vs {name_b}：共同题 {len(common)} 道 {common}")
            print(f"  {name_a}: 达标 {passed_a}/{len(common)}，折算费用 ¥{cost_a}")
            print(f"  {name_b}: 达标 {passed_b}/{len(common)}，折算费用 ¥{cost_b}")
            for test_id in common:
                print(
                    f"    {test_id}: {name_a}={ok_a[test_id].get('quality', {}).get('overall_pass')} / "
                    f"{name_b}={ok_b[test_id].get('quality', {}).get('overall_pass')}"
                )
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：py -3 tools\\summarize_run.py <run_dir> [run_dir2 ...]")
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
