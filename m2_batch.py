# -*- coding: utf-8 -*-
"""M2 批处理入口：三组（low/high/route）× 30 题、按题轮换、统一整批调用上限。

11 号 §4 的最小实现 + 18 号（真实执行零调用准备）的受控真实委托：
- 复用现有 runner.main() 逐题逐组执行，不复制第二套请求/记账实现；
  每个「题号 × 组别」一个独立子运行目录（raw/<group>/<test_id>/）。
- 轮换规则（11 号表格）：
    题号 1、4、7……  → low → high → route
    题号 2、5、8……  → high → route → low
    题号 3、6、9……  → route → low → high
- batch_manifest.json 必须在首次 HTTP 之前落盘，且不可事后覆盖；
  内容含 30 题身份、90 项顺序、题集/判据/价格/代码哈希、模型槽位、
  生成参数、目标地址与整批上限。
- 统一 --max-calls 管整批：发送前先在父层落盘占用（batch_attempts.jsonl），
  再委托 runner；失败、超时、坏响应、状态未知都占额度，不退回、不重试。
- 每个子运行前检查冻结输入/代码仍与 manifest 一致（漂移即停止）。
- 默认拒绝重用任何已有批次或子运行目录；中断后只允许只读对账。

**stub 模式**：与 11 号 §4 相同，只允许回环地址；不做真实停线判断（保留桩回归语义）。

**real 模式（18 号）**：本入口自己**不判定是否获准**，只做受控委托 ——
- 必须同时满足：① `--confirm-real`；② `--max-calls` 与本地批准记录一致；
  ③ `--approval-record` 指的记录 `status=APPROVED`，且其 `scope` 与本次实际冻结的
  题集/判据/价格/代码身份（含本入口自身）/模型/生成参数/上限/组别/目标地址/输出目录/
  凭据环境变量名**逐项相同**；④ 该记录不在本批次输出目录内（产物不能自我授权）。
  缺任一条件在**首次发送前**拒绝（退出 2），不写清单、不建批次目录。
- 批准身份**只读取一次字节**（20 号 R1）：记录内容与哈希来自同一份字节；清单与后续漂移检查
  沿用门禁已经认可的题集/判据/价格/代码身份，不在门禁之后重新读取磁盘作为基准。门禁通过后、
  写清单之前与首次委托之前都会再与现场逐项复核，现场不再匹配即停止（退出 4、零委托），
  **绝不把变化后的文件重新采纳为冻结基准**。
- 两个预算字段必须是精确 `int`/`float`、有限、正数，且停线不高于意向金额（20 号 R2）；
  `NaN`/`Infinity` 在门禁与草案写入前即被拒绝，运行中停线数值不可用时按"不继续发送"处理。
- 凭据**只判断指定环境变量存在且非空**（不取值、不打印、不落盘）；实际读取与使用由
  runner 完成。批准记录与所有日志只出现环境变量名。
- `child_argv_for()` 是唯一构造委托参数的地方（纯函数，可离线断言）；每个子运行的实际
  argv 在调用前落盘 `batch_delegation.jsonl`（不含凭据值），作为"参数确实传入"的证据。
- 真实运行停止线（在每个子运行结束后、下一次发送前判断，见 `post_child_stop_reason`）：
  子异常/子状态未知、冻结身份漂移、平台认证·权限·参数·限流错误（HTTP 400/401/403/404/422/429）、
  出现任何未知费用、已知折算费用达到批准停线、占用数达到 `--max-calls`。
  超时与不确定发送占额度、不自动重试/退回/追加；普通 5xx 记 failed 占额度，
  但因 usage 缺失会成为"未知费用"从而按批准口径停线（这一点与 stub 语义不同，见 18 号说明）。

退出码：0 完成（含额度用尽正常停止）/ 1 参数非法 / 2 门禁拒绝（真实模式缺确认、缺批准记录、
       scope 不匹配、缺凭据）/ 3 输出目录不可复用 / 4 清单或漂移校验失败（含门禁通过后现场身份
       复核不一致：零委托、不写清单）/ 5 子运行异常终止 / 6 真实运行停线后留下的不完整批次
       （未知费用、费用达线、平台错误）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import m2_reconcile
import pricing
import runner as runner_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))

# 参与漂移检查的代码身份（11 号 §4：含批处理入口、对账模块与评定工具；18 号加入批准记录工具）
CODE_FILES = (
    "m2_batch.py",
    "m2_reconcile.py",
    "runner.py",
    "router.py",
    "grade.py",
    "ledger.py",
    "contract.py",
    "pricing.py",
    "metrics.py",
    os.path.join("tools", "review_m2.py"),
    os.path.join("tools", "approval_m2.py"),
)

ROTATIONS = (
    ("low", "high", "route"),   # 题号 1、4、7……
    ("high", "route", "low"),   # 题号 2、5、8……
    ("route", "low", "high"),   # 题号 3、6、9……
)

GROUPS = ("low", "high", "route")

BATCH_ARTIFACTS = ("batch_manifest.json", "batch_attempts.jsonl", "batch_delegation.jsonl", "raw")

APPROVAL_VERSION = "m2_real_approval_v1"
DEFAULT_API_KEY_ENV = "ROUTER_API_KEY"
DELEGATION_NAME = "batch_delegation.jsonl"
# 需要在下一次发送前停止的平台错误（runner 记 failure_kind=platform、notes="HTTP <code>"）：
# 认证/权限、参数或账号配置、限流；普通 5xx 不在其中（记 failed、占额度，由未知费用停线兜底）。
PLATFORM_STOP_HTTP_CODES = (400, 401, 403, 404, 422, 429)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path):
    return {"name": os.path.basename(path), "sha256": sha256_file(path)}


def code_identities():
    out = {}
    for rel in CODE_FILES:
        path = os.path.join(ROOT, rel)
        if os.path.exists(path):
            out[rel.replace("\\", "/")] = sha256_file(path)
    return out


def is_loopback(base_url):
    try:
        host = urlparse(base_url or "").hostname
    except Exception:
        return False
    return bool(host) and (host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127."))


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_json_bytes(path):
    """读取一次字节并从中解析：内容与哈希来自**同一份字节**（20 号 R1）。

    用于批准记录：若先 `load_json` 再另行 `sha256_file`，两次读取之间文件被替换/撤回时，
    门禁用的是旧内容、清单记的却是新哈希，之后的漂移检查无法发现。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def dump_json_strict(path, obj):
    """写 JSON 产物：禁止 NaN/Infinity 等非标准常量（20 号 R2）。返回 None 或错误说明。

    先序列化、成功后才打开文件：拒绝时**不留下空文件或半截文件**，
    避免"看起来像 JSON 但不是 JSON"的产物混进批次目录。
    """
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    except ValueError as exc:
        return f"{type(exc).__name__}: {exc}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return None


def append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


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


def file_is_inside(directory, path):
    """path 是否位于 directory 内（用于拒绝"批次产物自我授权"）。"""
    try:
        target = os.path.normcase(os.path.abspath(directory))
        candidate = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    return candidate == target or candidate.startswith(target + os.sep)


# ------------------------------------------------- 真实模式：批准记录与门禁

def approval_scope(questions_path, criteria_path, prices_path, out_dir, max_calls, base_url,
                   models, generation, credential_env, plan_count, groups=GROUPS):
    """真实执行范围：批准记录里必须逐项写明，门禁与执行共用同一构造函数（避免两套口径）。

    只包含可从现场事实推导的字段；预算与"不重试/未知即停"等条款由记录自身声明，
    分别用 approval_form_problems 与预算检查判断。
    """
    return {
        "questions": {"name": os.path.basename(questions_path), "sha256": sha256_file(questions_path)},
        "criteria": {"name": os.path.basename(criteria_path), "sha256": sha256_file(criteria_path)},
        "prices": {"name": os.path.basename(prices_path), "sha256": sha256_file(prices_path)},
        "code": code_identities(),
        "models": {"low": models.get("low"), "high": models.get("high")},
        "generation": dict(generation),
        "max_calls": max_calls,
        "plan_count": plan_count,
        "groups": list(groups),
        "base_url": base_url,
        "output_dir": os.path.abspath(out_dir),
        "credential_env": credential_env,
    }


def is_finite_number(value):
    """精确 int/float 且有限（20 号 R2）：`bool` 不算数字，`NaN`/`±Infinity` 一律不算。

    不能用 `value <= 0` 代替：`NaN` 与任何数比较都为假，会静默通过形式校验。
    """
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return False


def budget_problems(intention, stop):
    """预算字段校验（门禁与草案生成共用同一套口径，20 号 R2）。

    规则：两个字段都必须是有限的正数（精确 int/float），且停线不高于意向金额。
    """
    problems = []
    if not is_finite_number(intention) or intention <= 0:
        problems.append(f"budget.intention_cny 必须是有限的十进制正数（不接受 NaN/Infinity）：{intention!r}")
    if not is_finite_number(stop) or stop <= 0:
        problems.append(f"budget.known_cost_stop_cny 必须是有限的十进制正数（不接受 NaN/Infinity）：{stop!r}")
    elif is_finite_number(intention) and stop > intention:
        problems.append(f"budget.known_cost_stop_cny（{stop}）不能大于 intention_cny（{intention}）")
    return problems


def approval_form_problems(doc):
    """批准记录自身的形式与条款检查（不涉及本次执行参数）。"""
    problems = []
    if not isinstance(doc, dict):
        return ["批准记录顶层必须是 JSON 对象"]
    if doc.get("approval_version") != APPROVAL_VERSION:
        problems.append(f"approval_version 不是 {APPROVAL_VERSION}：{doc.get('approval_version')!r}")
    if doc.get("status") != "APPROVED":
        problems.append(f"批准记录 status 不是 APPROVED：{doc.get('status')!r}（草案/未批准不能执行）")
    for key in ("approval_id", "approved_by", "approved_at"):
        value = doc.get(key)
        if not (isinstance(value, str) and value.strip()):
            problems.append(f"批准记录缺 {key}")
    terms = doc.get("terms")
    if not isinstance(terms, dict):
        problems.append("批准记录缺 terms 条款（不自动重试、未知费用即停等）")
    else:
        for key in ("no_auto_retry", "stop_on_unknown_cost", "stop_before_next_send"):
            if terms.get(key) is not True:
                problems.append(f"批准记录未确认条款 terms.{key}=true")
    budget = doc.get("budget")
    if not isinstance(budget, dict):
        problems.append("批准记录缺 budget（意向金额与已知费用停线）")
    else:
        problems.extend(budget_problems(budget.get("intention_cny"), budget.get("known_cost_stop_cny")))
    return problems


def approval_scope_problems(expected, recorded):
    """把批准记录的 scope 与实际执行逐项对拍；不一致逐键列出（便于人工核对）。"""
    problems = []
    if not isinstance(recorded, dict):
        return ["批准记录缺 scope 对象"]
    for key in sorted(set(expected) | set(recorded)):
        want = expected.get(key)
        got = recorded.get(key)
        if want != got:
            problems.append(
                f"批准记录 scope.{key} 与实际执行不一致：记录="
                + json.dumps(got, ensure_ascii=False, sort_keys=True)
                + " 实际=" + json.dumps(want, ensure_ascii=False, sort_keys=True)
            )
    return problems


def credential_problems(credential_env, env_lookup=None):
    """凭据**存在性**检查：只看环境变量是否非空，不取值使用、不打印、不落盘。"""
    env_lookup = env_lookup or os.environ.get
    if not credential_env:
        return ["未指定凭据环境变量名（--api-key-env）"]
    value = env_lookup(credential_env)
    if not (isinstance(value, str) and value.strip()):
        return [f"环境变量 {credential_env} 为空：凭据由 runner 读取，此处只判断存在性"]
    if "\n" in value or "\r" in value:
        return [f"环境变量 {credential_env} 含换行（会导致 HTTP 头校验失败）"]
    return []


def real_gate_problems(record_path, expected_scope, out_dir, confirm_real, credential_env, env_lookup=None):
    """真实模式门禁：返回 (problems, 批准记录, 记录字节哈希)。**任何请求之前**调用。

    记录内容与返回的哈希来自同一次字节读取（20 号 R1）：调用方必须用这个哈希写清单，
    不得在门禁之后再从磁盘重新计算。
    """
    problems = []
    doc = None
    record_sha = None
    path = os.path.abspath(record_path or "")
    if not record_path:
        problems.append("真实模式必须给出 --approval-record（本地批准记录）")
        return problems, doc, record_sha
    if file_is_inside(out_dir, path):
        problems.append("批准记录不能放在本批次输出目录内（批次产物不能自我授权）")
    if not os.path.exists(path):
        problems.append(f"批准记录不存在：{path}")
        return problems, doc, record_sha
    try:
        doc, record_sha = load_json_bytes(path)
    except Exception as exc:
        problems.append(f"批准记录不是合法 JSON：{type(exc).__name__}: {exc}")
        return problems, doc, record_sha

    if not confirm_real:
        problems.append("缺少 --confirm-real（真实模式必须显式确认）")
    problems.extend(approval_form_problems(doc))
    problems.extend(approval_scope_problems(expected_scope, doc.get("scope") if isinstance(doc, dict) else None))
    problems.extend(credential_problems(credential_env, env_lookup=env_lookup))
    return problems, doc, record_sha


# ------------------------------------------------- 真实模式：委托参数与停线

def child_argv_for(item, cfg, sub_out):
    """构造委托给 runner 的参数（唯一构造点；真实模式在此追加 --confirm-real 与凭据环境变量名）。"""
    argv = [
        "--questions", cfg["questions"],
        "--criteria", cfg["criteria"],
        "--target", cfg["target"],
        "--base-url", cfg["base_url"],
        "--prices", cfg["prices"],
        "--model-low", cfg["model_low"],
        "--model-high", cfg["model_high"],
        "--timeout", str(cfg["timeout"]),
        "--max-tokens", str(cfg["max_tokens"]),
        "--temperature", str(cfg["temperature"]),
        "--enable-thinking", cfg["enable_thinking"],
        "--ids", item["test_id"],
        "--slot", item["group"],
        "--out", sub_out,
        "--max-calls", "1",
    ]
    if cfg.get("thinking_budget") is not None:
        argv += ["--thinking-budget", str(cfg["thinking_budget"])]
    if cfg["target"] == "real":
        argv += ["--confirm-real", "--api-key-env", cfg["api_key_env"]]
    return argv


def child_request_row(sub_out):
    rows = read_jsonl(os.path.join(sub_out, "requests.jsonl"))
    return rows[0] if rows else None


def platform_failure_stop(row):
    """平台侧认证/权限/参数/限流错误 → 下一次发送前停止；普通 5xx 不在此列。"""
    if not isinstance(row, dict) or row.get("failure_kind") != "platform":
        return None
    notes = str(row.get("notes") or "")
    match = re.search(r"HTTP\s+(\d{3})", notes)
    code = int(match.group(1)) if match else None
    if code in PLATFORM_STOP_HTTP_CODES:
        kinds = {400: "参数/请求错误", 401: "认证失败", 403: "权限或账号配置错误",
                 404: "模型或路径不可用", 422: "请求不合法", 429: "限流"}
        return (f"平台错误 HTTP {code}（{kinds.get(code, '平台拒绝')}）：{notes}")
    return None


def post_child_stop_reason(account, child_row, stop_known_cny):
    """子运行结束后的真实停线判断（纯函数，便于离线逐条验证）。返回 (code, reason) 或 None。"""
    platform = platform_failure_stop(child_row)
    if platform:
        return ("platform_error", platform)
    totals = (account or {}).get("totals") or {}
    unknown = totals.get("unknown_attempt_count") or 0
    if unknown > 0:
        return ("unknown_cost",
                f"出现未知费用（unknown_attempt_count={unknown}），按批准口径在下一次发送前停止")
    known = totals.get("known_cost_cny") or 0
    if not is_finite_number(stop_known_cny) or stop_known_cny <= 0:
        # 20 号 R2：停线数值不可用（NaN/Infinity/缺失）时按"不继续发送"处理。
        # `known >= NaN` 恒为假会静默失效，因此这里失败关闭，而不是当作没有停线。
        return ("invalid_stop_line",
                f"批准停线数值不可用（{stop_known_cny!r}）：不判断费用、直接停止后续发送")
    if known >= stop_known_cny:
        return ("known_cost_stop_line",
                f"已知折算费用 ¥{known} 已达到批准停线 ¥{stop_known_cny}")
    return None


def build_plan(questions):
    """90 项执行计划：按题号轮换三组顺序。"""
    plan = []
    seq = 0
    ordered = sorted(questions, key=lambda q: q["test_id"])
    for question in ordered:
        index = int(question["test_id"][1:]) - 1  # T01..T30 → 0..29
        for group in ROTATIONS[index % 3]:
            seq += 1
            plan.append({"seq": seq, "test_id": question["test_id"], "group": group})
    return plan


def drift_check(manifest):
    """每个子运行前：冻结输入/代码/清单本身仍与开工时一致。"""
    problems = []
    for key in ("questions", "criteria", "prices"):
        path = manifest["_" + key + "_path"]
        if sha256_file(path) != manifest[key]["sha256"]:
            problems.append(f"{key} 文件与清单冻结身份不一致：{path}")
    for rel, expected in manifest["code"].items():
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path) or sha256_file(path) != expected:
            problems.append(f"代码文件漂移：{rel}")
    if sha256_file(manifest["_manifest_path"]) != manifest["_manifest_sha256"]:
        problems.append("batch_manifest.json 本身被修改过")
    # 18 号：真实模式的批准记录也是冻结身份，执行中被人改动即停止
    approval = manifest.get("approval")
    if isinstance(approval, dict) and manifest.get("_approval_path"):
        path = manifest["_approval_path"]
        if not os.path.exists(path) or sha256_file(path) != approval.get("sha256"):
            problems.append("批准记录在批次执行中被修改或删除")
    return problems


def live_identity_problems(frozen, questions_path, criteria_path, prices_path,
                           approval_path=None, approval_sha=None):
    """门禁通过后、写清单与首次委托之前：现场必须仍与**门禁已认可的身份**逐项一致（20 号 R1）。

    规则是"用冻结值复核现场"，不是"再读一次现场当基准"：任何差异都停止，零委托。
    """
    problems = []
    for key, path in (("questions", questions_path), ("criteria", criteria_path), ("prices", prices_path)):
        expected = (frozen or {}).get(key) or {}
        if not os.path.exists(path) or sha256_file(path) != expected.get("sha256"):
            problems.append(f"{key} 文件在批准身份冻结后发生变化，与门禁认可的哈希不一致：{path}")
    for rel, expected in ((frozen or {}).get("code") or {}).items():
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path) or sha256_file(path) != expected:
            problems.append(f"代码文件在批准身份冻结后发生变化，与门禁认可的哈希不一致：{rel}")
    if approval_path:
        try:
            current_approval_sha = sha256_file(approval_path)
        except OSError:
            current_approval_sha = None
        if current_approval_sha != approval_sha:
            problems.append(f"批准记录在门禁读取之后被修改、撤回或无法读取：{approval_path}")
    return problems


def collect_group_requests(raw_dir, group):
    requests = []
    group_dir = os.path.join(raw_dir, group)
    if not os.path.isdir(group_dir):
        return requests
    for test_id in sorted(os.listdir(group_dir)):
        path = os.path.join(group_dir, test_id, "requests.jsonl")
        if os.path.exists(path):
            requests.extend(read_jsonl(path))
    return requests


def median(values):
    """标准中位数：偶数样本取中间两项均值（11 号 §5）。"""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def main(argv=None, env_lookup=None):
    """env_lookup 仅为离线验证提供注入点；CLI 路径始终使用 os.environ。"""
    parser = argparse.ArgumentParser(description="M2 三组批处理（stub 默认；real 需本地批准记录）")
    parser.add_argument("--questions", required=True)
    parser.add_argument("--criteria", required=True)
    parser.add_argument("--target", default="stub", choices=("stub", "real"))
    parser.add_argument("--base-url", default="http://127.0.0.1:5291")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-calls", type=int, default=90, help="整批硬上限；占用含失败与状态未知")
    parser.add_argument("--model-low", default=None)
    parser.add_argument("--model-high", default=None)
    parser.add_argument("--prices", default=pricing.DEFAULT_PRICES_PATH)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--enable-thinking", choices=("default", "true", "false"), default="default")
    parser.add_argument("--thinking-budget", type=int, default=None)
    parser.add_argument("--approval-record", default=None, help="真实模式必需的本地批准记录（JSON）")
    parser.add_argument("--confirm-real", action="store_true", help="真实模式必须显式确认")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV,
                        help="凭据所在环境变量名；只传名字，不传值（值只由 runner 读取）")
    args = parser.parse_args(argv)

    # ---------- 基础门禁（全部在任何请求、任何落盘之前） ----------
    if args.max_calls <= 0:
        print("[拒绝] --max-calls 必须为正整数", file=sys.stderr)
        return 1
    if args.target == "stub" and not is_loopback(args.base_url):
        print("[拒绝] stub 模式只允许回环地址 --base-url", file=sys.stderr)
        return 2
    if args.target == "real" and is_loopback(args.base_url):
        print("[拒绝] 真实模式的 --base-url 不能是回环地址", file=sys.stderr)
        return 2

    if os.path.isdir(args.out):
        present = [name for name in BATCH_ARTIFACTS if os.path.exists(os.path.join(args.out, name))]
        if present:
            print(f"[拒绝] 批次目录已有产物（{'、'.join(present)}），拒绝复用；请换新目录", file=sys.stderr)
            return 3

    for label, path in (("题集", args.questions), ("判据", args.criteria), ("价格", args.prices)):
        if not os.path.exists(path):
            print(f"[拒绝] {label}文件不存在：{path}", file=sys.stderr)
            return 1

    prices = load_json(args.prices)
    slots = prices.get("slots") or {}
    model_low = args.model_low or slots.get("low")
    model_high = args.model_high or slots.get("high")
    if not model_low or not model_high:
        print("[拒绝] 未确定 low/high 槽位模型（--model-low/--model-high 或价格表 slots）", file=sys.stderr)
        return 1

    questions_doc = load_json(args.questions)
    questions = questions_doc.get("questions") or []
    if not questions:
        print("[拒绝] 题集为空", file=sys.stderr)
        return 1

    plan = build_plan(questions)
    generation = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout,
        "enable_thinking": args.enable_thinking,
        "thinking_budget": args.thinking_budget,
    }

    # ---------- 真实模式门禁：批准记录必须与本次执行逐项一致 ----------
    approval_doc = None
    approval_ref = None
    budget = None
    frozen_scope = None
    if args.target == "real":
        # 冻结身份：先算一次，门禁与清单/漂移检查共用同一份（20 号 R1：绝不再读一次磁盘当基准）
        frozen_scope = approval_scope(args.questions, args.criteria, args.prices, args.out, args.max_calls,
                                      args.base_url, {"low": model_low, "high": model_high}, generation,
                                      args.api_key_env, len(plan))
        problems, approval_doc, record_sha = real_gate_problems(
            args.approval_record, frozen_scope, args.out, args.confirm_real, args.api_key_env,
            env_lookup=env_lookup)
        if problems:
            print("[拒绝] 真实模式门禁未通过：未发送任何请求、未创建批次目录、未写清单", file=sys.stderr)
            for item in problems:
                print("  - " + item, file=sys.stderr)
            return 2
        approval_ref = {
            "path": os.path.abspath(args.approval_record),
            # 与门禁解析内容同一次读取得到的哈希（不是门禁之后重新计算）
            "sha256": record_sha,
            "approval_id": approval_doc.get("approval_id"),
            "approved_by": approval_doc.get("approved_by"),
            "approved_at": approval_doc.get("approved_at"),
        }
        budget = approval_doc.get("budget")

        # ---------- 门禁通过后、任何落盘或发送之前：现场必须仍与已认可身份一致 ----------
        live = live_identity_problems(frozen_scope, args.questions, args.criteria, args.prices,
                                      approval_ref["path"], record_sha)
        if live:
            print("[拒绝] 门禁通过后现场身份发生变化：未发送任何请求、未创建批次目录、未写清单",
                  file=sys.stderr)
            for item in live:
                print("  - " + item, file=sys.stderr)
            return 4

    # ---------- 停线口径：写进清单，供执行与复审核对 ----------
    if args.target == "real":
        stop_policy = {
            "mode": "real_approved_v1",
            "stop_on_child_error": True,
            "stop_on_drift": True,
            "stop_http_codes": list(PLATFORM_STOP_HTTP_CODES),
            "stop_on_unknown_cost": True,
            "stop_http_note": "平台认证/权限/参数/限流错误停止；普通 5xx 记 failed 占额度，其费用未知会触发未知费用停线",
            "known_cost_stop_cny": (budget or {}).get("known_cost_stop_cny"),
            "budget_intention_cny": (budget or {}).get("intention_cny"),
            "max_calls": args.max_calls,
            "no_auto_retry": True,
            "note": "任一停线条件在下一次发送前生效；超时与不确定发送占额度、不重试、不退回、不追加。",
        }
    else:
        stop_policy = {
            "mode": "stub",
            "max_calls": args.max_calls,
            "no_auto_retry": True,
            "note": "桩模式保留 11 号语义：只受整批上限、漂移与子异常约束，不做真实费用停线。",
        }

    # ---------- 批次清单：在任何子运行 / 任何 HTTP 之前落盘 ----------
    # 真实模式的输入/代码身份一律沿用门禁已经认可的那一份（20 号 R1）；
    # 桩模式没有批准记录，按现场读取即可（其身份随后由 drift_check 逐项守住）。
    identity = frozen_scope if frozen_scope is not None else {
        "questions": file_identity(args.questions),
        "criteria": file_identity(args.criteria),
        "prices": file_identity(args.prices),
        "code": code_identities(),
        "models": {"low": model_low, "high": model_high},
        "generation": generation,
    }
    os.makedirs(args.out, exist_ok=True)
    raw_dir = os.path.join(args.out, "raw")
    manifest = {
        "manifest_version": "batch_manifest_v1",
        "created_at": now_iso(),
        "batch_id": os.path.basename(os.path.abspath(args.out)),
        "target": args.target,
        "base_url": args.base_url,
        "max_calls": args.max_calls,
        "planned_questions": [q["test_id"] for q in sorted(questions, key=lambda q: q["test_id"])],
        "planned_count": len(questions),
        "plan": plan,
        "plan_count": len(plan),
        "rotation": {
            "题号 mod 3 == 1": "low → high → route",
            "题号 mod 3 == 2": "high → route → low",
            "题号 mod 3 == 0": "route → low → high",
        },
        "questions": identity["questions"],
        "questions_version": questions_doc.get("version"),
        "criteria": identity["criteria"],
        "prices": identity["prices"],
        "prices_version": prices.get("version"),
        "code": identity["code"],
        "models": identity["models"],
        "generation": identity["generation"],
        "approval": approval_ref,
        "budget": budget,
        "stop_policy": stop_policy,
        "note": "本清单在发送任何请求之前写出，且不可事后覆盖；每个子运行沿用 runner 的逐次记账与事前 run manifest。"
                "真实模式的输入/代码身份来自门禁同一次读取，不在门禁之后重新采纳现场文件。",
    }
    manifest_path = os.path.join(args.out, "batch_manifest.json")
    manifest_error = dump_json_strict(manifest_path, manifest)
    if manifest_error:
        print("[拒绝] 批次清单含非标准 JSON 常量（NaN/Infinity），拒绝落盘，未发送任何请求："
              + manifest_error, file=sys.stderr)
        return 4
    manifest_sha = sha256_file(manifest_path)
    manifest["_manifest_path"] = manifest_path
    manifest["_manifest_sha256"] = manifest_sha
    manifest["_questions_path"] = os.path.abspath(args.questions)
    manifest["_criteria_path"] = os.path.abspath(args.criteria)
    manifest["_prices_path"] = os.path.abspath(args.prices)
    manifest["_approval_path"] = approval_ref["path"] if approval_ref else None
    print(f"[manifest] 批次清单已落盘（sha256={manifest_sha[:16]}…，{len(plan)} 项计划，整批上限 {args.max_calls}）", flush=True)

    attempts_path = os.path.join(args.out, "batch_attempts.jsonl")
    delegation_path = os.path.join(args.out, DELEGATION_NAME)

    # ---------- 主循环：串行执行，占用即落盘 ----------
    occupied = 0
    executed = 0
    failures = 0
    stop_reason = "completed"
    real_stop_code = None
    child_cfg = {
        "questions": args.questions,
        "criteria": args.criteria,
        "target": args.target,
        "base_url": args.base_url,
        "prices": args.prices,
        "model_low": model_low,
        "model_high": model_high,
        "timeout": args.timeout,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "enable_thinking": args.enable_thinking,
        "thinking_budget": args.thinking_budget,
        "api_key_env": args.api_key_env,
    }

    for item in plan:
        if occupied >= args.max_calls:
            stop_reason = f"已达整批调用上限 {args.max_calls}，剩余计划项未执行"
            print(f"[stop] {stop_reason}", flush=True)
            break

        drift = drift_check(manifest)
        if drift:
            print("[拒绝] 冻结身份漂移，停止后续发送：", file=sys.stderr)
            for problem in drift:
                print("  - " + problem, file=sys.stderr)
            stop_reason = "drift_detected"
            break

        # 父层先占额度（发送前落盘），再委托 runner
        append_jsonl(attempts_path, {
            "kind": "start", "seq": item["seq"], "test_id": item["test_id"],
            "group": item["group"], "ts": now_iso(),
        })
        occupied += 1

        sub_out = os.path.join(raw_dir, item["group"], item["test_id"])
        child_argv = child_argv_for(item, child_cfg, sub_out)
        # 委托参数落盘（不含凭据值）：证明真实参数确实传给了 runner
        append_jsonl(delegation_path, {
            "kind": "delegate", "seq": item["seq"], "test_id": item["test_id"],
            "group": item["group"], "target": args.target, "argv": child_argv, "ts": now_iso(),
        })
        started = time.time()
        status = "ok"
        note = ""
        try:
            code = runner_mod.main(child_argv)
            if code != 0:
                status = "child_error"
                note = f"runner 退出码 {code}"
                failures += 1
        except Exception as exc:  # 子运行自身异常：占用不退回，状态未知时保守计一次
            status = "unknown"
            note = f"{type(exc).__name__}: {exc}"
            failures += 1
        wall_ms = int((time.time() - started) * 1000)

        append_jsonl(attempts_path, {
            "kind": "end", "seq": item["seq"], "test_id": item["test_id"],
            "group": item["group"], "status": status, "note": note,
            "runner_wall_ms": wall_ms, "ts": now_iso(),
        })
        executed += 1
        print(f"  [{item['seq']:02d}/{len(plan)}] {item['test_id']} {item['group']:5s} {status}"
              + (f" ({note})" if note else ""), flush=True)

        # 13 号 R3：子运行自身异常（非零退出或未处理异常）后立即停止后续发送。
        # 普通 HTTP 500 已由 runner 正常记为 failed（返回 0），不在此列、仍只占额度。
        if status in ("child_error", "unknown"):
            stop_reason = (f"child_error/child_unknown：seq={item['seq']} {item['test_id']} {item['group']}"
                           f"（{note}）；剩余计划项未执行")
            print(f"[stop] {stop_reason}", flush=True)
            break

        # 18 号：真实模式停线（费用未知 / 已知费用达线 / 平台认证·限流），只读对账后判断。
        # 20 号 R2：停线数值不可用（NaN/Infinity）时按"不继续发送"处理（fail-closed）。
        # stub 模式不进入这里，保留 11 号桩语义（失败占额度继续，供离线回归）。
        if args.target == "real":
            stop = post_child_stop_reason(m2_reconcile.reconcile(args.out), child_request_row(sub_out),
                                          (budget or {}).get("known_cost_stop_cny"))
            if stop:
                real_stop_code, stop_note = stop
                stop_reason = f"{real_stop_code}：{stop_note}；剩余计划项未执行"
                print(f"[stop] {stop_reason}", flush=True)
                break

    # ---------- 汇总（只读子运行产物，不改写任何原始文件） ----------
    # 费用统一走共用对账（父占用 + 子 sealed 尝试），与人工派生同一条路径（13 号 R2）
    groups = ("low", "high", "route")
    requests_by_group = {g: collect_group_requests(raw_dir, g) for g in groups}
    import metrics as metrics_mod

    account = m2_reconcile.reconcile(args.out)
    per_group = {}
    for g in groups:
        reqs = requests_by_group[g]
        latencies = [r["latency_ms_total"] for r in reqs if r.get("run_status") == "ok"]
        acct = account["per_group"].get(g, {"planned": 0, "executed": 0, "answered": 0, "failed": 0,
                                            "interrupted": 0, "known_cost_cny": 0.0,
                                            "unknown_attempt_count": 0, "total_cost_cny": 0.0})
        states = {}
        for row in account["rows"]:
            if row["group"] == g:
                states[row["state"]] = states.get(row["state"], 0) + 1
        per_group[g] = {
            "planned": len([q for q in questions]),
            "executed": acct["executed"],
            "answered": acct["answered"],
            "failed": acct["failed"],
            "interrupted_or_unknown": acct["interrupted"],
            "item_states": states,
            "end_to_end": metrics_mod.end_to_end_pass_rate(reqs, len(questions)),
            "content": metrics_mod.content_pass_rate(reqs),
            "cost": {
                "known_cost_cny": acct["known_cost_cny"],
                "unknown_attempt_count": acct["unknown_attempt_count"],
                "total_cost_cny": acct["total_cost_cny"],
                "note": "来自父占用与子 start/end 的只读对账；未闭合或查不到子事件的尝试计未知",
            },
            "latency_p50_ms": median(latencies),
            "latency_samples": len(latencies),
        }

    totals = account["totals"]
    remaining = len(plan) - executed

    summary = {
        "batch_id": manifest["batch_id"],
        "manifest_ref": "batch_manifest.json",
        "manifest_sha256": manifest_sha,
        "target": args.target,
        "plan_count": len(plan),
        "executed_items": executed,
        "occupied_attempts": occupied,
        "child_failures": failures,
        "remaining_items": remaining,
        "stop_reason": stop_reason,
        "stop_code": real_stop_code,
        "max_calls": args.max_calls,
        "approval": approval_ref,
        "budget": budget,
        "stop_policy": stop_policy,
        "delegation_ref": DELEGATION_NAME if os.path.exists(delegation_path) else None,
        "per_group": per_group,
        "paired": metrics_mod.paired_compare(requests_by_group),
        "cost_total": {
            "known_cost_cny": totals["known_cost_cny"],
            "unknown_attempt_count": totals["unknown_attempt_count"],
            "total_cost_cny": totals["total_cost_cny"],
            "attempts_counted": totals["attempts_counted"],
            "occurred_cost_note": totals["occurred_cost_note"],
            "complete_experiment_cost_cny": totals["complete_experiment_cost_cny"],
            "complete_experiment_note": totals["complete_experiment_note"],
            "note": "按公开单价折算；任一尝试费用未知则总费用为 null（未知不得记 0）",
        },
        "item_accounting": [
            {k: row[k] for k in ("seq", "test_id", "group", "state", "attempts_counted",
                                 "known_cost_cny", "unknown_attempt_count", "has_request_row", "run_status")}
            for row in account["rows"]
        ],
        "written_at": now_iso(),
    }
    summary_path = os.path.join(args.out, "batch_summary.json")
    # 同上：产物不允许非标准 JSON 常量（20 号 R2）
    summary_error = dump_json_strict(summary_path, summary)
    if summary_error:
        print("[异常] 批次汇总含非标准 JSON 常量（NaN/Infinity），未写出汇总：" + summary_error,
              file=sys.stderr)
        return 4
    print(json.dumps({k: summary[k] for k in ("plan_count", "executed_items", "occupied_attempts",
                                              "remaining_items", "stop_reason")}, ensure_ascii=False), flush=True)

    if stop_reason == "drift_detected":
        return 4
    if real_stop_code:
        return 6
    if failures:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
