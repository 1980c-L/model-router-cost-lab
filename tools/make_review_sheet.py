# -*- coding: utf-8 -*-
"""生成人工核对表（Markdown）：把逐题资料、问题、完整答案与待核项排在一起。

用法：
    py -3 tools\\make_review_sheet.py runs\\m1-official-low runs\\m1-official-high --out M1人工核对表.md

只读产物，不联网。人工核完后把结果回填到该表即可（criteria_v1 规定：人工未填前一律记未达标）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HUMAN_LABELS = {
    "facts_correct": "关键事实与资料一致",
    "no_unsupported_claim": "没有资料不支持的说法",
    "citation_supports": "给出的依据支持结论",
    "no_added_content_by_human": "摘要没有新增材料里没有的内容",
    "task_type_unrecognized": "任务类型未识别（无法判定）",
}


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成人工核对表")
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--questions", default=os.path.join("eval", "dev_questions.json"))
    parser.add_argument("--out", default="M1人工核对表.md")
    args = parser.parse_args(argv)

    questions = {q["test_id"]: q for q in load_json(args.questions).get("questions", [])}
    groups = []
    for directory in args.run_dirs:
        summary_path = os.path.join(directory, "summary.json")
        if not os.path.exists(summary_path):
            continue
        summary = load_json(summary_path)
        groups.append(
            {
                "dir": directory,
                "group": summary.get("group") or os.path.basename(directory),
                "models": (summary.get("prices") or {}).get("slots", {}),
                "requests": load_jsonl(os.path.join(directory, "requests.jsonl")),
            }
        )

    lines = [
        "# M1 正式对照 · 人工核对表",
        "",
        "> 机检（JSON 可解析 / 必填字段 / 字段值 / 长度 / 覆盖关键词 / 禁止词）已完成；",
        "> 下表的**待核项**必须人工勾选后，该题才算整体达标（`criteria_v1.json` 规定人工未填前一律记未达标）。",
        "> 核对方式：逐条读「回答」，勾选待核项；有疑问的记录在行末备注即可。",
        "",
    ]

    order = [r["test_id"] for r in groups[0]["requests"]] if groups else []
    for test_id in order:
        question = questions.get(test_id, {})
        lines.append(f"## {test_id} · {question.get('task_type_gold', '?')}")
        lines.append("")
        lines.append(f"**问题**：{question.get('question', '(未找到题目)')}")
        lines.append("")
        materials = question.get("materials") or []
        if materials:
            lines.append("**资料**：")
            lines.append("")
            for index, material in enumerate(materials, 1):
                lines.append(f"- [材料{index}] {material}")
            lines.append("")
        if question.get("answer_key"):
            lines.append(f"**标准答案**：`{json.dumps(question['answer_key'], ensure_ascii=False)}`")
            lines.append("")
        if question.get("criteria"):
            lines.append(f"**题目判据参数**：`{json.dumps(question['criteria'], ensure_ascii=False)}`")
            lines.append("")

        for group in groups:
            record = next((r for r in group["requests"] if r["test_id"] == test_id), None)
            if record is None:
                continue
            model = (group["models"] or {}).get(record.get("router", {}).get("chosen_slot") or group["group"], "?")
            quality = record.get("quality") or {}
            lines.append(f"### {group['group']} 组 · {model}")
            lines.append("")
            lines.append(
                f"- 机检：`mechanized_pass={quality.get('mechanized_pass')}`"
                + (f"，`reason={quality.get('reason')}`" if quality.get("reason") else "")
            )
            lines.append(
                f"- 耗时 {record.get('latency_ms_total')} ms · "
                f"折算费用 ¥{((record.get('attempts') or [{}])[0].get('cost_cny'))}"
            )
            answer_path = os.path.join(group["dir"], record.get("final_output_ref") or "")
            answer = ""
            if os.path.exists(answer_path):
                with open(answer_path, "r", encoding="utf-8") as fh:
                    answer = fh.read()
            lines.append("")
            lines.append("**回答**：")
            lines.append("")
            lines.append("```text")
            lines.append(answer.rstrip("\n"))
            lines.append("```")
            lines.append("")
            pending = quality.get("human_pending") or []
            if pending:
                lines.append("**待核项**：")
                lines.append("")
                for item in pending:
                    lines.append(f"- [ ] {item} —— {HUMAN_LABELS.get(item, item)}")
            else:
                lines.append("**待核项**：无（该题机检即可判定）")
            lines.append("")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[ok] 已生成：{args.out}")
    print(f"     组数 {len(groups)}，题目 {len(order)}，共 {sum(len(g['requests']) for g in groups)} 条回答")
    return 0


if __name__ == "__main__":
    sys.exit(main())
