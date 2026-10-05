# -*- coding: utf-8 -*-
"""本地 LLM 桩：OpenAI 兼容返回 + usage + 可控故障 + 请求台账。绝不联网。

用途：开发集调试规则与格式、验证记账链路。**桩结果不能证明任何真实模型的质量或节省效果。**
故障模式通过请求头 `X-Stub-Mode` 指定：ok / error / timeout / format_invalid。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LEDGER_PATH = None
REQUEST_COUNT = {"n": 0}
DEFAULT_MODE = "ok"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stub_content(stub_kind):
    if stub_kind == "ok_json":
        # 刻意返回**多行** JSON：用于验证"答案按字节保存后哈希与文件一致"（07 号 §2.2）
        return json.dumps(
            {"_stub": True, "note": "本地桩固定 JSON，字段不会与标准答案一致", "lines": [1, 2, 3]},
            ensure_ascii=False,
            indent=2,
        )
    return "【桩回答】这是本地桩的固定输出，未经过任何模型。"


class Handler(BaseHTTPRequestHandler):
    server_version = "StubLLM/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "requests": REQUEST_COUNT["n"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        mode = self.headers.get("X-Stub-Mode") or DEFAULT_MODE
        model = body.get("model") or "stub-model"
        messages = body.get("messages") or []
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        stub_kind = (body.get("metadata") or {}).get("stub_kind")
        # 记录思考模式相关字段，供验收断言"参数确实透传"
        self._extra = {
            "enable_thinking": body.get("enable_thinking"),
            "thinking_budget": body.get("thinking_budget"),
        }

        if mode == "timeout":
            time.sleep(5)
        if mode == "error":
            REQUEST_COUNT["n"] += 1
            self._record(mode, model, None, prompt_chars)
            self._send(500, {"error": {"message": "stub platform error", "type": "stub_error"}})
            return

        if mode == "bad_json":
            REQUEST_COUNT["n"] += 1
            self._record(mode, model, None, prompt_chars)
            body_bytes = b"not-a-json-response"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        content = _stub_content(stub_kind)
        if mode == "format_invalid":
            content = "这不是 JSON，也没有结构。{ 桩的格式错误样本"

        completion_chars = len(content)
        usage = {
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, completion_chars // 4),
            "total_tokens": max(1, prompt_chars // 4) + max(1, completion_chars // 4),
            "cached_tokens": 0,
        }
        REQUEST_COUNT["n"] += 1
        self._record(mode, model, usage, prompt_chars)
        self._send(
            200,
            {
                "id": f"stub-{REQUEST_COUNT['n']}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
                "_stub": {"mode": mode, "usage_is_estimated": True},
            },
        )

    def _record(self, mode, model, usage, prompt_chars):
        if not LEDGER_PATH:
            return
        extra = getattr(self, "_extra", {}) or {}
        record = {
            "ts": _now(),
            "mode": mode,
            "model": model,
            "usage": usage,
            "prompt_chars": prompt_chars,
            "enable_thinking": extra.get("enable_thinking"),
            "thinking_budget": extra.get("thinking_budget"),
            "seq": REQUEST_COUNT["n"],
        }
        with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main():
    global LEDGER_PATH, DEFAULT_MODE
    parser = argparse.ArgumentParser(description="本地 LLM 桩（绝不联网）")
    parser.add_argument("--port", type=int, default=5291)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ledger", default=None, help="请求台账 JSONL 路径")
    parser.add_argument(
        "--default-mode",
        default="ok",
        choices=("ok", "error", "timeout", "format_invalid", "bad_json"),
        help="默认返回模式；请求头 X-Stub-Mode 可覆盖",
    )
    args = parser.parse_args()

    LEDGER_PATH = args.ledger
    DEFAULT_MODE = args.default_mode
    if LEDGER_PATH:
        os.makedirs(os.path.dirname(os.path.abspath(LEDGER_PATH)), exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[stub] listening on http://{args.host}:{args.port} (ledger={LEDGER_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(f"[stub] stopped after {REQUEST_COUNT['n']} requests", flush=True)


if __name__ == "__main__":
    main()
