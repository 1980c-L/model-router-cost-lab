# -*- coding: utf-8 -*-
"""运行器：一题一次运行 → 路由决策 → 调用（桩/真实）→ 逐次记账 → 机检判据 → 落盘。

05 号窄修后的关键约束：
- **先占额度再发送**：尝试在发送前登记并占用一次调用额度；成功、失败、超时、
  响应解析错误都不退回额度。`actual_attempts` 记登记次数，`successful_calls` 另记成功次数。
- **拒绝复用输出目录**：目录里已有运行产物（attempt_events / requests / summary）时直接拒绝，
  因为复用会覆盖尝试身份、并让旧的未闭合（unknown）尝试被新的 end 事件盖掉。
- **评测按题集冻结类别**：判据用 `task_type_gold` 选择；缺失或非法在发送前拒绝。
  `task_type_pred` 只用于误分类分析，**评测不跟随预测**，路由也仍然拿不到 gold。
- **门禁**：真实模式拒绝占位模型字符串、缺价格、缺价格来源或日期的状态；
  桩模式强制回环地址。
- **答案留存**：每题完整原始回答落盘并记哈希；每轮写 run manifest 绑定
  题集 / 判据 / 评分与路由代码 / 价格身份 / 生成参数。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import contract
import grade
import ledger as ledger_mod
import metrics
import pricing
import router

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STUB_URL = "http://127.0.0.1:5291"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
GRADABLE_TYPES = ("extract", "qa_grounded", "summary")
PLACEHOLDER_MARKERS = ("PLACEHOLDER", "placeholder", "TODO", "待填", "FILL_ME")
RUN_ARTIFACTS = ("attempt_events.jsonl", "requests.jsonl", "summary.json")
MANIFEST_NAME = "run_manifest.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path):
    return {"name": os.path.basename(path), "sha256": sha256_file(path)}


def is_loopback(base_url):
    try:
        host = urlparse(base_url or "").hostname
    except Exception:
        return True
    if not host:
        return True
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------- 门禁

def guard_mode(args, base_url, prices, model_low, model_high):
    """模式门禁：全部为离线可测的纯检查，不发送任何请求。"""
    problems = []
    if args.target == "stub":
        if not is_loopback(base_url):
            problems.append("桩模式只允许回环地址：--base-url 必须指向 127.0.0.1 / localhost")
        return problems, ""

    if not args.confirm_real:
        problems.append("缺少 --confirm-real")
    if not args.max_calls or args.max_calls <= 0:
        problems.append("缺少 --max-calls（真实模式必须给出硬上限）")
    if is_loopback(base_url):
        problems.append("真实模式的 --base-url 不能是回环地址")
    api_key = (os.environ.get(args.api_key_env) or "").strip()
    if not api_key:
        problems.append(f"环境变量 {args.api_key_env} 为空")
    if "\n" in api_key or "\r" in api_key:
        problems.append("凭据含换行（会导致 HTTP 头校验失败）")

    for slot_name, model in (("low", model_low), ("high", model_high)):
        if not model:
            problems.append(f"{slot_name} 槽位未确定模型")
            continue
        if any(marker in str(model) for marker in PLACEHOLDER_MARKERS):
            problems.append(f"{slot_name} 槽位仍是占位字符串：{model}")
            continue
        price = pricing.model_price(prices, model)
        reason = pricing.missing_price_reason(price)
        if reason:
            problems.append(f"{slot_name} 槽位价格前提未完成：{reason}")
            continue
        if not price.get("source_url"):
            problems.append(f"{slot_name} 槽位缺价格来源 source_url")
        if not (price.get("price_date") or price.get("fetched_at")):
            problems.append(f"{slot_name} 槽位缺价格日期（price_date / fetched_at）")
    return problems, api_key


def guard_out_dir(out_dir):
    """始终拒绝复用已有运行产物的目录（05 号 §2.2、07 号 §2.1）。

    不复用是硬规则：复用会覆盖尝试身份、让旧的未闭合（unknown）尝试被新的 end 盖掉，
    并把端到端达标率算成 200%。**不提供任何开关**——显式选择复用也不能让错账成为有效实验结果。
    """
    if not os.path.isdir(out_dir):
        return []
    present = [name for name in RUN_ARTIFACTS if os.path.exists(os.path.join(out_dir, name))]
    if present:
        return [f"输出目录已有运行产物（{'、'.join(present)}），拒绝复用；请换新目录"]
    return []


# --------------------------------------------------------------- 请求构造

def build_request_text(question):
    parts = [question.get("question", "")]
    materials = question.get("materials") or []
    if materials:
        parts.append("【资料】")
        for index, material in enumerate(materials, 1):
            parts.append(f"[材料{index}]\n{material}")
    return "\n\n".join(parts)


def build_system_prompt(question, task_type_pred):
    base = "你是被评测的助手，请直接回答用户问题。若资料中确实没有依据，请明确说明“资料中未找到相关依据”。"
    if task_type_pred == "extract":
        fields = (question.get("constraints") or {}).get("required_fields") or []
        if fields:
            base += " 必须输出严格 JSON（不要代码块、不要额外文字），字段：" + ",".join(fields) + "。"
    return base


def normalize_usage(raw):
    """把平台返回的 usage 映射成契约字段，并返回 (usage, meta)。

    硅基流动的实际形状（07 号 §2.4，官方文档示例）：
      - `usage.prompt_tokens_details.cached_tokens`（首选）
      - `usage.cached_tokens`（兼容）
      - `usage.prompt_cache_hit_tokens`（兼容）
    只读 `usage.cached_tokens` 会把缓存命中漏成 0，从而**把费用算高**（示例中偏高 80%）。
    因此这里：按明示优先序取值、记录取值来源、冲突与越界都留痕、并保留原始 usage 便于核对。
    """
    if not isinstance(raw, dict):
        return None, None
    if not all(type(raw.get(k)) is int for k in ("prompt_tokens", "completion_tokens")):
        return None, None

    candidates = []
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
        candidates.append(("prompt_tokens_details.cached_tokens", details["cached_tokens"]))
    if type(raw.get("cached_tokens")) is int:
        candidates.append(("cached_tokens", raw["cached_tokens"]))
    if type(raw.get("prompt_cache_hit_tokens")) is int:
        candidates.append(("prompt_cache_hit_tokens", raw["prompt_cache_hit_tokens"]))

    notes = []
    if candidates:
        source, cached = candidates[0]
        if len({value for _name, value in candidates}) > 1:
            notes.append(
                "缓存字段冲突，按优先序取 "
                + source
                + "："
                + json.dumps({name: value for name, value in candidates}, ensure_ascii=False)
            )
    else:
        source, cached = "absent", 0
        notes.append("平台未返回缓存命中量，按 0 处理")

    prompt_tokens = raw["prompt_tokens"]
    if cached < 0:
        notes.append(f"缓存量为负（{cached}），按 0 处理")
        cached = 0
    if cached > prompt_tokens:
        notes.append(f"缓存量超过输入量（{cached} > {prompt_tokens}），已裁剪")
        cached = prompt_tokens

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": raw["completion_tokens"],
        "cached_tokens": cached,
    }
    meta = {
        "cached_source": source,
        "notes": notes,
        "raw_usage": raw,
        "cached_tokens_used": cached,
    }
    return usage, meta


def call_model(
    base_url,
    api_key,
    model,
    messages,
    timeout,
    max_tokens,
    temperature,
    stub_kind,
    enable_thinking=None,
    thinking_budget=None,
):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # 硅基流动文档：max_tokens 不含思维链，思考长度由 thinking_budget 控制。
    # 混合思考模型不显式关闭时，抽取类推理链可能极长（实测 180s 仍未返回）。
    if enable_thinking is not None:
        payload["enable_thinking"] = bool(enable_thinking)
    if thinking_budget is not None:
        payload["thinking_budget"] = int(thinking_budget)
    if stub_kind:
        payload["metadata"] = {"stub_kind": stub_kind}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    # 显式禁用代理：本机代理会把请求打成 ConnectionReset
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.time()
    with opener.open(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    latency_ms = int((time.time() - started) * 1000)
    content = body["choices"][0]["message"]["content"]
    usage, usage_meta = normalize_usage(body.get("usage"))
    return content, usage, latency_ms, usage_meta


def attempts_for_request(sealed, request_id, unit_price_version):
    out = []
    for item in sealed:
        if item.get("request_id") != request_id:
            continue
        out.append(
            contract.new_attempt(
                attempt_no=item["attempt_no"],
                model=item.get("model"),
                slot=item.get("slot") or "low",
                purpose=item.get("purpose") or "generate",
                status=item.get("status") or "unknown",
                usage=item.get("usage"),
                usage_known=bool(item.get("usage_known")),
                unit_price_version=unit_price_version,
                cost_cny=item.get("cost_cny"),
                latency_ms=item.get("latency_ms"),
                offline_eval=bool(item.get("offline_eval")),
            )
        )
    return out


# --------------------------------------------------------------- 主流程

def main(argv=None):
    parser = argparse.ArgumentParser(description="模型路由与成本质量评测 · 运行器")
    parser.add_argument("--questions", required=True)
    parser.add_argument("--ids", default="", help="逗号分隔的 test_id 子集；为空则全跑")
    parser.add_argument("--slot", required=True, choices=list(contract.GROUPS))
    parser.add_argument("--target", default="stub", choices=("stub", "real"))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model-low", default=None)
    parser.add_argument("--model-high", default=None)
    parser.add_argument("--prices", default=pricing.DEFAULT_PRICES_PATH)
    parser.add_argument("--criteria", default=os.path.join(ROOT, "eval", "criteria_v1.json"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-calls", type=int, default=0, help="硬上限；真实模式必填")
    parser.add_argument("--confirm-real", action="store_true")
    parser.add_argument("--api-key-env", default="ROUTER_API_KEY")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--user-task-type", default=None, help="用户显式指定任务模式时使用（来源记为 user）")
    parser.add_argument(
        "--enable-thinking",
        choices=("default", "true", "false"),
        default="default",
        help="思考模式开关；default = 不发送该字段。注意：硅基流动文档说明 max_tokens 不含思维链",
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="思维链最大 token 数（硅基流动文档：128–32768）；不传则平台默认",
    )
    args = parser.parse_args(argv)

    prices = pricing.load_prices(args.prices)
    slots = prices.get("slots") or {}
    model_low = args.model_low or slots.get("low")
    model_high = args.model_high or slots.get("high")
    base_url = args.base_url or (DEFAULT_STUB_URL if args.target == "stub" else None)

    if not base_url:
        print("[拒绝] 必须给出 --base-url", file=sys.stderr)
        return 2

    problems, api_key = guard_mode(args, base_url, prices, model_low, model_high)
    if problems:
        print(f"[拒绝] {args.target} 模式门禁未通过，未发送任何请求：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 3

    existing = guard_out_dir(args.out)
    if existing:
        print("[拒绝] 输出目录不可复用，未发送任何请求：", file=sys.stderr)
        for item in existing:
            print("  - " + item, file=sys.stderr)
        return 5

    questions = load_json(args.questions)
    all_questions = questions.get("questions") if isinstance(questions, dict) else questions
    ids = [item.strip() for item in args.ids.split(",") if item.strip()]
    selected = [q for q in all_questions if not ids or q.get("test_id") in ids]
    if ids:
        missing = [i for i in ids if i not in {q.get("test_id") for q in selected}]
        if missing:
            print("[拒绝] 题号不存在：" + ",".join(missing), file=sys.stderr)
            return 2
    criteria = load_json(args.criteria)

    # 评测类别必须随题集冻结，且必须在发送任何请求之前确定（05 号 §2.3）
    invalid = [
        (q.get("test_id"), q.get("task_type_gold"))
        for q in selected
        if q.get("task_type_gold") not in GRADABLE_TYPES
    ]
    if invalid:
        print("[拒绝] 存在缺失或非法的评测类别（task_type_gold），未发送任何请求：", file=sys.stderr)
        for test_id, gold in invalid:
            print(f"  - {test_id}: task_type_gold={gold!r}（必须是 {GRADABLE_TYPES} 之一）", file=sys.stderr)
        return 2

    max_calls = args.max_calls or 0
    enable_thinking_value = None if args.enable_thinking == "default" else (args.enable_thinking == "true")
    os.makedirs(args.out, exist_ok=True)
    manifest = {
        "manifest_version": "run_manifest_v1",
        "created_at": now_iso(),
        "run_id": os.path.basename(os.path.abspath(args.out)),
        "group": args.slot,
        "target": args.target,
        "questions": file_identity(args.questions),
        "criteria": file_identity(args.criteria),
        "criteria_version": criteria.get("version"),
        "prices": file_identity(args.prices),
        "prices_version": prices.get("version"),
        "code": {
            "grade.py": file_identity(os.path.join(ROOT, "grade.py")),
            "router.py": file_identity(os.path.join(ROOT, "router.py")),
            "contract.py": file_identity(os.path.join(ROOT, "contract.py")),
            "ledger.py": file_identity(os.path.join(ROOT, "ledger.py")),
        },
        "router_rules_version": router.RULES_VERSION,
        "models": {"low": model_low, "high": model_high},
        "generation": {
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "timeout_seconds": args.timeout,
            "enable_thinking": args.enable_thinking,
            "thinking_budget": args.thinking_budget,
        },
        "planned_ids": [q.get("test_id") for q in selected],
        "planned_count": len(selected),
        "max_calls": max_calls or None,
        "system_prompt_sha256": {},
        "note": "本清单在发送任何请求之前写出；逐题记录通过 manifest_ref 关联。",
    }

    # ---------- 预处理：在发送任何请求之前，固定逐题身份与提示 ----------
    # 07 号 §2.3：run manifest 必须是**事前**凭据，不能等整轮结束后才落盘。
    prepared = []
    for question in selected:
        test_id = question.get("test_id")
        grade_type = question["task_type_gold"]           # 评测用冻结类别
        request_text = build_request_text(question)
        constraints = question.get("constraints") or {}
        materials = question.get("materials") or []
        instruction = question.get("question") or ""

        if args.slot == "route":
            decision = router.decide(request_text, constraints, args.user_task_type, instruction_text=instruction)
            chosen_slot = decision["chosen_slot"]
        else:
            features = router.extract_features(request_text, constraints, instruction_text=instruction)
            task_type_pred, task_type_source = router.predict_task_type(features, args.user_task_type)
            chosen_slot = args.slot
            decision = {
                "rule_version": router.RULES_VERSION,
                "matched_rule": None,
                "reason": f"该组固定使用 {args.slot} 槽位（对照组，不经过路由）",
                "chosen_slot": chosen_slot,
                "default_applied": False,
                "hit_rules": [],
                "conflict_resolution": None,
                "features": None,
                "task_type_pred": task_type_pred,
                "task_type_pred_source": task_type_source,
            }
        task_type_pred = decision["task_type_pred"]
        prepared.append(
            {
                "question": question,
                "test_id": test_id,
                "grade_type": grade_type,
                "request_text": request_text,
                "constraints": constraints,
                "materials": materials,
                "decision": decision,
                "chosen_slot": chosen_slot,
                "task_type_pred": task_type_pred,
                "system_prompt": build_system_prompt(question, task_type_pred),
            }
        )

    manifest["system_prompt_sha256"] = {
        item["test_id"]: {
            "task_type_pred": item["task_type_pred"],
            "sha256": sha256_text(item["system_prompt"]),
        }
        for item in prepared
    }
    manifest["frozen_at"] = now_iso()
    manifest_path = os.path.join(args.out, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    manifest_sha = sha256_file(manifest_path)
    print(f"[manifest] 已在发送任何请求之前落盘（sha256={manifest_sha[:16]}…）", flush=True)

    ledger = ledger_mod.Ledger(args.out, prices=prices)
    answers_dir = os.path.join(args.out, "answers")
    os.makedirs(answers_dir, exist_ok=True)

    planned = len(prepared)
    registered = 0        # 已登记并占用额度的尝试数（含失败）
    succeeded = 0         # 成功拿到可用回答的次数
    executed = 0
    print(f"[run] group={args.slot} target={args.target} planned={planned} out={args.out}", flush=True)

    for item in prepared:
        if max_calls and registered >= max_calls:
            print(f"[stop] 已达调用上限 {max_calls}（失败也占额度），剩余题不执行", flush=True)
            break

        question = item["question"]
        test_id = item["test_id"]
        grade_type = item["grade_type"]
        request_text = item["request_text"]
        constraints = item["constraints"]
        materials = item["materials"]
        decision = item["decision"]
        chosen_slot = item["chosen_slot"]
        task_type_pred = item["task_type_pred"]
        model = model_low if chosen_slot == "low" else model_high
        stub_kind = "ok_json" if task_type_pred == "extract" else "ok_text"
        request_id = f"{args.slot}-{test_id}"

        messages = [
            {"role": "system", "content": item["system_prompt"]},
            {"role": "user", "content": request_text},
        ]

        # 先占额度、再发送（05 号 §2.1）
        attempt_no = ledger.start_attempt(request_id, model, chosen_slot, purpose="generate", ts=now_iso())
        registered += 1
        started = time.time()
        content, usage, latency_ms, usage_meta = None, None, None, None
        status = "ok"
        failure_kind = None
        note = ""
        try:
            content, usage, latency_ms, usage_meta = call_model(
                base_url, api_key, model, messages, args.timeout, args.max_tokens, args.temperature, stub_kind,
                enable_thinking=enable_thinking_value, thinking_budget=args.thinking_budget,
            )
            succeeded += 1
        except socket.timeout:
            status, failure_kind, note = "error", "timeout", "请求超时"
        except urllib.error.HTTPError as exc:
            status, failure_kind, note = "error", "platform", f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            status = "error"
            failure_kind = "timeout" if "timed out" in reason.lower() else "network"
            note = reason
        except Exception as exc:  # 解析类异常也算失败，不退回额度、不补跑
            status, failure_kind, note = "error", "unknown", f"{type(exc).__name__}: {exc}"
        latency_total_ms = int((time.time() - started) * 1000)

        ledger.finish_attempt(
            request_id,
            attempt_no,
            status=status,
            usage=usage,
            latency_ms=latency_ms if latency_ms is not None else latency_total_ms,
            model=model,
            slot=chosen_slot,
            purpose="generate",
            offline_eval=False,
            ts=now_iso(),
        )

        # 完整回答落盘（05 号 §2.5 / 07 号 §2.2）：
        # 写入的是**原始 UTF-8 字节**，并按同一份字节算哈希 ——
        # 否则 Windows 文本模式会把 LF 换成 CRLF，多行答案的哈希与文件字节不一致。
        answer_path = os.path.join(answers_dir, f"{test_id}.txt")
        answer_bytes = (content if content is not None else "").encode("utf-8")
        with open(answer_path, "wb") as fh:
            fh.write(answer_bytes)
        answer_ref = os.path.relpath(answer_path, args.out).replace("\\", "/")
        answer_sha = hashlib.sha256(answer_bytes).hexdigest()

        sealed = ledger.sealed_attempts()
        attempts = attempts_for_request(sealed, request_id, prices.get("version"))
        online = [a for a in attempts if not a["offline_eval"]]
        unknown = len([a for a in online if a["cost_cny"] is None])
        known = round(sum(a["cost_cny"] for a in online if isinstance(a["cost_cny"], (int, float))), 10)
        total = None if unknown else known

        if status == "ok":
            quality = grade.grade(grade_type, content, question, criteria)
            run_status = "ok"
        else:
            quality = {
                "overall_pass": False,
                "mechanized_pass": False,
                "needs_human": False,
                "reason": f"运行失败：{failure_kind}",
                "detail": {},
            }
            run_status = "failed"

        request = contract.new_request(
            request_id=request_id,
            ts=now_iso(),
            group=args.slot,
            task_type_pred=task_type_pred,
            task_type_pred_source=decision["task_type_pred_source"],
            test_id=test_id,
            input_ref={
                "text_sha256": sha256_text(request_text),
                "evidence_sha256": sha256_text("\n\n".join(materials)),
                "question_ref": f"{os.path.basename(args.questions)}#{test_id}",
            },
            attempts=attempts,
            run_status=run_status,
            total_cost_cny=total,
            known_cost_cny=known,
            unknown_attempt_count=unknown,
            latency_ms_total=latency_total_ms,
            quality=quality,
            task_type_gold=grade_type,
            failure_kind=failure_kind,
            notes=note,
            final_output_ref=answer_ref,
            router=decision,
        )
        request["answer_sha256"] = answer_sha
        request["manifest_ref"] = f"{MANIFEST_NAME}"
        request["manifest_sha256"] = manifest_sha
        request["usage_meta"] = usage_meta
        errors = contract.validate_request(request)
        if errors:
            print(f"[契约错误] {request_id}: {errors}", file=sys.stderr)
            return 4
        ledger.append_request(request)
        executed += 1
        print(
            f"  {test_id} gold={grade_type} pred={task_type_pred} slot={chosen_slot} "
            f"status={run_status} pass={quality.get('overall_pass')} err={failure_kind}",
            flush=True,
        )

    # 清单是**事前**凭据：不在结束时覆盖它。最终计数只写 summary（07 号 §2.3）。
    events = ledger.events()
    start_events = len([e for e in events if e.get("kind") == "start"])
    requests = ledger.load_requests()
    summary = ledger.write_summary(
        {
            "group": args.slot,
            "target": args.target,
            "planned_count": planned,
            "executed_count": executed,
            "actual_attempts": registered,
            "successful_calls": succeeded,
            "start_events": start_events,
            "attempts_match_events": registered == start_events,
            "max_calls": max_calls or None,
            "complete": executed == planned,
            "manifest_ref": MANIFEST_NAME,
            "manifest_sha256": manifest_sha,
            "metrics": {
                "end_to_end": metrics.end_to_end_pass_rate(requests, planned),
                "content": metrics.content_pass_rate(requests),
                "failure": metrics.run_failure_rate(requests, planned),
                "cost": metrics.cost_summary(requests),
                "paired": metrics.paired_compare({args.slot: requests}),
            },
            "prices": {
                "version": prices.get("version"),
                "slots": slots,
                "models_filled": {
                    "low": pricing.price_is_filled(pricing.model_price(prices, model_low)),
                    "high": pricing.price_is_filled(pricing.model_price(prices, model_high)),
                },
            },
        }
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
