# -*- coding: utf-8 -*-
"""记账：发送前登记、结束后补结果、未知传播、append-only 落盘。

依据 03 号 §1.3：
- "日志里没有"**不等于**"调用没发生"。请求可能已发出、随后超时或在写完成记录前中断。
- 因此先写 `start` 事件（含尝试序号与开始状态），成功/失败后再写 `end` 事件；
  只有 start 没有 end 的尝试，一律按 `status="unknown"`、费用未知处理。
- 只要有一次在线尝试未知，请求级 `total_cost_cny` 就是 None（另给已知部分与未知计数）。
"""
from __future__ import annotations

import json
import os

import pricing


class Ledger:
    def __init__(self, run_dir, prices=None, unit_price_version=None):
        self.run_dir = run_dir
        self.events_path = os.path.join(run_dir, "attempt_events.jsonl")
        self.requests_path = os.path.join(run_dir, "requests.jsonl")
        self.summary_path = os.path.join(run_dir, "summary.json")
        self.prices = prices
        self.unit_price_version = unit_price_version or (prices or {}).get("version")
        os.makedirs(run_dir, exist_ok=True)
        self._next_attempt = {}
        self._starts = []

    # ---------- 落盘 ----------

    def _append(self, path, record):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    # ---------- 尝试生命周期 ----------

    def start_attempt(self, request_id, model, slot, purpose="generate", offline_eval=False, ts=None):
        attempt_no = self._next_attempt.get(request_id, 0) + 1
        self._next_attempt[request_id] = attempt_no
        record = {
            "kind": "start",
            "request_id": request_id,
            "attempt_no": attempt_no,
            "model": model,
            "slot": slot,
            "purpose": purpose,
            "offline_eval": offline_eval,
            "ts": ts,
        }
        self._append(self.events_path, record)
        self._starts.append(record)
        return attempt_no

    def finish_attempt(
        self,
        request_id,
        attempt_no,
        status,
        usage=None,
        latency_ms=None,
        model=None,
        slot=None,
        purpose=None,
        offline_eval=False,
        ts=None,
    ):
        price = pricing.model_price(self.prices, model) if (self.prices and model) else None
        usage_known = isinstance(usage, dict) and all(
            type(usage.get(k)) is int for k in ("prompt_tokens", "completion_tokens")
        )
        cost = pricing.compute_cost(price, usage) if usage_known else None
        record = {
            "kind": "end",
            "request_id": request_id,
            "attempt_no": attempt_no,
            "status": status,
            "usage": usage if usage_known else None,
            "usage_known": bool(usage_known),
            "cost_cny": cost,
            "latency_ms": latency_ms,
            "model": model,
            "slot": slot,
            "purpose": purpose,
            "offline_eval": offline_eval,
            "unit_price_version": self.unit_price_version,
            "price_missing_reason": None if cost is not None else pricing.missing_price_reason(price),
            "ts": ts,
        }
        self._append(self.events_path, record)
        return record

    # ---------- 读取与汇总 ----------

    def events(self):
        if not os.path.exists(self.events_path):
            return []
        out = []
        with open(self.events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def sealed_attempts(self):
        """把 start/end 事件合并成尝试列表；缺 end 的尝试按未知处理。"""
        starts = {}
        ends = {}
        for event in self.events():
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
                sealed.append(
                    {
                        "request_id": start.get("request_id"),
                        "attempt_no": start.get("attempt_no"),
                        "model": start.get("model"),
                        "slot": start.get("slot"),
                        "purpose": start.get("purpose"),
                        "offline_eval": start.get("offline_eval", False),
                        "status": "unknown",
                        "usage": None,
                        "usage_known": False,
                        "cost_cny": None,
                        "latency_ms": None,
                        "sealed": False,
                    }
                )
                continue
            sealed.append(
                {
                    "request_id": end.get("request_id"),
                    "attempt_no": end.get("attempt_no"),
                    "model": end.get("model") or start.get("model"),
                    "slot": end.get("slot") or start.get("slot"),
                    "purpose": end.get("purpose") or start.get("purpose"),
                    "offline_eval": end.get("offline_eval", start.get("offline_eval", False)),
                    "status": end.get("status"),
                    "usage": end.get("usage"),
                    "usage_known": end.get("usage_known", False),
                    "cost_cny": end.get("cost_cny"),
                    "latency_ms": end.get("latency_ms"),
                    "sealed": True,
                }
            )
        return sealed

    def summarize(self):
        sealed = self.sealed_attempts()
        online = [a for a in sealed if not a["offline_eval"]]
        offline = [a for a in sealed if a["offline_eval"]]
        known_cost = round(sum(a["cost_cny"] for a in online if isinstance(a["cost_cny"], (int, float))), 10)
        unknown = [a for a in online if a["cost_cny"] is None]
        total = None if unknown else known_cost
        return {
            "attempts_total": len(sealed),
            "online_attempts": len(online),
            "offline_attempts": len(offline),
            "known_cost_cny": known_cost,
            "unknown_attempt_count": len(unknown),
            "total_cost_cny": total,
            "unsealed_attempts": len([a for a in sealed if not a["sealed"]]),
            "price_unfilled": any(a["cost_cny"] is None and a["sealed"] for a in online),
            "unit_price_version": self.unit_price_version,
        }

    def append_request(self, request):
        self._append(self.requests_path, request)

    def load_requests(self):
        if not os.path.exists(self.requests_path):
            return []
        out = []
        with open(self.requests_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def write_summary(self, extra=None):
        summary = self.summarize()
        if extra:
            summary.update(extra)
        with open(self.summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return summary
