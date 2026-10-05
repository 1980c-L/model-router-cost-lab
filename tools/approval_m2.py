# -*- coding: utf-8 -*-
"""M2 真实执行的「本地批准记录」生成与自检（零调用：不联网、不读凭据值、不发送请求）。

用途：
- `draft`：按当前冻结的题集/判据/价格/代码身份/模型/生成参数/上限/输出目录/凭据环境变量名，
  生成一份 `status=PENDING_USER_APPROVAL` 的批准记录草案（即 19 号申请的可机读部分）。
- `check`：用与 `m2_batch.py` 真实模式**同一套**函数核对某份记录是否与本次执行逐项一致，
  并单列执行前置（--confirm-real / 凭据环境变量是否存在）。

边界（重要）：
- 本工具**不能**把记录改成 APPROVED，也不提供任何自动批准的开关。批准是用户的动作。
  用户明确批准后，才由人（或按用户批准语句行事的一方）填写 status=APPROVED、approved_by、
  approved_at，并在 approval_basis 里引用当时的批准语句；实施方不得自行填这两个字段。
- 只判断凭据环境变量是否存在，不取值、不打印、不落盘；凭据的实际读取与使用在 runner。
- draft 默认拒绝覆盖已有记录；需要重出时显式加 --force，旧记录会改名保留（不删除）。
- 两个预算字段（`--budget-intention-cny` / `--known-cost-stop-cny`）必须是有限的十进制正数，
  且停线不高于意向金额；`NaN`/`Infinity` 在写入草案之前即被拒绝（20 号 R2），
  写入使用 `allow_nan=False`，断不会产出含非标准 JSON 常量的记录。

退出码：0 通过 / 1 参数非法或记录与执行不一致 / 2 记录匹配但执行前置（确认、凭据）不满足。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import m2_batch  # noqa: E402
import pricing  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

STATUS_DRAFT = "PENDING_USER_APPROVAL"
PENDING_FIELDS = [
    "status：PENDING_USER_APPROVAL → APPROVED",
    "approved_by：批准人标识",
    "approved_at：批准时间（ISO 8601，含时区）",
    "approval_basis：用户批准语句的原文引用（不得由实施方代写）",
]


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        # allow_nan=False：记录里不允许出现 NaN/Infinity 这类非标准 JSON 常量（20 号 R2）
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def instance_dir(path):
    """改名隔离（不删除）：供 --force 重出草案时保留旧件。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1, 1000):
        target = f"{path}.prev-{stamp}-{index}"
        if not os.path.exists(target):
            os.rename(path, target)
            return target
    raise RuntimeError(f"无法隔离：{path}")


def build_scope(args):
    prices = load_json(args.prices)
    slots = prices.get("slots") or {}
    model_low = args.model_low or slots.get("low")
    model_high = args.model_high or slots.get("high")
    if not model_low or not model_high:
        raise SystemExit("[拒绝] 未确定 low/high 槽位模型（--model-low/--model-high 或价格表 slots）")
    questions_doc = load_json(args.questions)
    questions = questions_doc.get("questions") or []
    if not questions:
        raise SystemExit("[拒绝] 题集为空")
    plan = m2_batch.build_plan(questions)
    generation = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout,
        "enable_thinking": args.enable_thinking,
        "thinking_budget": args.thinking_budget,
    }
    scope = m2_batch.approval_scope(args.questions, args.criteria, args.prices, args.out, args.max_calls,
                                   args.base_url, {"low": model_low, "high": model_high}, generation,
                                   args.credential_env, len(plan))
    return scope, plan, questions_doc


def add_common(parser):
    parser.add_argument("--questions", required=True)
    parser.add_argument("--criteria", required=True)
    parser.add_argument("--prices", default=pricing.DEFAULT_PRICES_PATH)
    parser.add_argument("--out", required=True, help="本次执行要用的批次输出目录（全路径或相对当前目录）")
    parser.add_argument("--max-calls", type=int, required=True, help="整批调用上限（必须与拟批准次数一致）")
    parser.add_argument("--base-url", required=True, help="真实地址，如 https://api.siliconflow.cn")
    parser.add_argument("--credential-env", default=m2_batch.DEFAULT_API_KEY_ENV)
    parser.add_argument("--model-low", default=None)
    parser.add_argument("--model-high", default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--enable-thinking", choices=("default", "true", "false"), default="default")
    parser.add_argument("--thinking-budget", type=int, default=None)


def cmd_draft(args):
    # 20 号 R2：预算字段必须在**写入之前**校验完（NaN/Infinity 的比较恒为假，不能只判 <= 0）
    budget_problems = m2_batch.budget_problems(args.budget_intention_cny, args.known_cost_stop_cny)
    if budget_problems:
        print("[拒绝] 预算参数不合法，未写出任何草案：", file=sys.stderr)
        for item in budget_problems:
            print("  - " + item, file=sys.stderr)
        return 1
    scope, plan, questions_doc = build_scope(args)
    if m2_batch.is_loopback(args.base_url):
        print("[拒绝] 草案用于真实执行，--base-url 不能是回环地址", file=sys.stderr)
        return 1
    path = os.path.abspath(args.approval_record)
    if m2_batch.file_is_inside(args.out, path):
        print("[拒绝] 批准记录不能放在批次输出目录内", file=sys.stderr)
        return 1
    if os.path.exists(path):
        if not args.force:
            print(f"[拒绝] 批准记录已存在：{path}；如需重出请加 --force（旧件改名保留）", file=sys.stderr)
            return 1
        print(f"[隔离] 旧记录改名保留：{os.path.basename(instance_dir(path))}")

    record = {
        "approval_version": m2_batch.APPROVAL_VERSION,
        "approval_id": os.path.basename(os.path.normpath(os.path.abspath(args.out))),
        "status": STATUS_DRAFT,
        "approved_by": None,
        "approved_at": None,
        "approval_basis": None,
        "prepared_at": now_iso(),
        "prepared_by": "实施方草案（CodeBuddy）；未经用户批准，不能作为执行依据",
        "terms": {
            "no_auto_retry": True,
            "stop_on_unknown_cost": True,
            "stop_before_next_send": True,
            "note": "失败/超时/状态未知都占额度；出现未知费用、达到已知费用停线或平台认证·限流错误时，"
                    "在下一次发送前停止，不自动补跑。",
        },
        "budget": {
            "intention_cny": args.budget_intention_cny,
            "known_cost_stop_cny": args.known_cost_stop_cny,
            "note": "意向金额与软件停线；不是平台支付硬限额。运行中最后一条请求与未知账可能使实际费用超出意向金额。",
        },
        "scope": scope,
        "pending_fields": list(PENDING_FIELDS),
        "draft_note": "本文件由 tools/approval_m2.py draft 生成；scope 与现场冻结身份逐项一致后，"
                      "才可由用户批准。执行日必须重新核对公开价格并另存快照，再用 --prices 指向该快照重新出草案。",
    }
    write_json(path, record)
    # 自检只覆盖"与批准无关"的形式与条款（版本、条款、预算）；status/approved_by/approved_at
    # 本来就是待用户填写的字段，用一份占位副本验证格式不会漏检。
    probe = dict(record)
    probe.update(status="APPROVED", approved_by="<待用户填写>", approved_at=now_iso())
    problems = m2_batch.approval_form_problems(probe)
    if problems:
        print("[异常] 生成的草案未通过自检（不应发生）：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1
    print(json.dumps({
        "record": path,
        "status": record["status"],
        "plan_count": scope["plan_count"],
        "max_calls": scope["max_calls"],
        "models": scope["models"],
        "known_cost_stop_cny": record["budget"]["known_cost_stop_cny"],
        "code_files": len(scope["code"]),
        "pending_fields": record["pending_fields"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args):
    scope, plan, _doc = build_scope(args)
    path = os.path.abspath(args.approval_record)
    problems = []
    if m2_batch.file_is_inside(args.out, path):
        problems.append("批准记录位于批次输出目录内（批次产物不能自我授权）")
    if not os.path.exists(path):
        print(f"[拒绝] 批准记录不存在：{path}", file=sys.stderr)
        return 1
    doc = load_json(path)
    problems.extend(m2_batch.approval_form_problems(doc))
    problems.extend(m2_batch.approval_scope_problems(scope, doc.get("scope") if isinstance(doc, dict) else None))
    if problems:
        print("[不一致] 记录与本次执行参数不匹配：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1

    preconditions = []
    if not args.confirm_real:
        preconditions.append("尚未给出 --confirm-real（执行时必须显式确认）")
    preconditions.extend(m2_batch.credential_problems(args.credential_env))
    report = {
        "record": path,
        "approval_id": doc.get("approval_id"),
        "approved_by": doc.get("approved_by"),
        "approved_at": doc.get("approved_at"),
        "scope_matches_current_files": True,
        "plan_count": scope["plan_count"],
        "max_calls": scope["max_calls"],
        "execution_preconditions_unmet": preconditions,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if preconditions else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="M2 真实执行批准记录草案与自检（零调用）")
    sub = parser.add_subparsers(dest="command", required=True)

    draft = sub.add_parser("draft", help="生成 PENDING_USER_APPROVAL 草案")
    add_common(draft)
    draft.add_argument("--approval-record", required=True, help="草案输出路径（不能在批次输出目录内）")
    draft.add_argument("--budget-intention-cny", type=float, required=True)
    draft.add_argument("--known-cost-stop-cny", type=float, required=True)
    draft.add_argument("--force", action="store_true", help="允许覆盖已有记录（旧件改名保留）")

    check = sub.add_parser("check", help="按 m2_batch 同一套门禁核对记录与本次执行")
    add_common(check)
    check.add_argument("--approval-record", required=True)
    check.add_argument("--confirm-real", action="store_true", help="执行时会给出的显式确认（仅用于前置自检）")

    args = parser.parse_args(argv)
    if args.max_calls <= 0:
        print("[拒绝] --max-calls 必须为正整数", file=sys.stderr)
        return 1
    return cmd_draft(args) if args.command == "draft" else cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
