# -*- coding: utf-8 -*-
"""单价表加载与费用折算。

口径（来自 02/03 号）：
- 只做"按公开单价折算的 API 成本"，不是实际支付金额（免费额度可能把实际支付抵为 0）。
- `usage` 缺失、或单价未填 → 返回 None（**未知不记 0**）。
- 单价必须带 `source_url` 与 `fetched_at`；本文件不做任何联网取值。
"""
from __future__ import annotations

import json
import os

DEFAULT_PRICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices", "prices_v1.json")


def load_prices(path=None):
    path = path or DEFAULT_PRICES_PATH
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "models" not in data:
        raise ValueError("单价表结构不合法：顶层必须是对象且含 models")
    return data


def model_price(prices, model):
    """取某模型的单价条目；未登记返回 None（调用方据此记未知）。"""
    models = prices.get("models") or {}
    entry = models.get(model)
    return entry if isinstance(entry, dict) else None


def price_is_filled(price):
    """单价条目是否已具备可折算的完整字段。"""
    if not isinstance(price, dict):
        return False
    for key in ("input_price_per_1m", "output_price_per_1m"):
        value = price.get(key)
        if type(value) not in (int, float):
            return False
    return True


def missing_price_reason(price):
    if price is None:
        return "单价表中未登记该模型"
    missing = [k for k in ("input_price_per_1m", "output_price_per_1m") if type(price.get(k)) not in (int, float)]
    if missing:
        return "单价字段未填：" + ",".join(missing)
    return None


def compute_cost(price, usage):
    """按单价折算费用；任一输入不完整都返回 None。"""
    if not price_is_filled(price):
        return None
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(key)) is not int:
            return None

    input_rate = price["input_price_per_1m"]
    output_rate = price["output_price_per_1m"]
    cached_rate = price.get("cached_input_price_per_1m")
    if type(cached_rate) not in (int, float):
        cached_rate = input_rate

    prompt_tokens = usage["prompt_tokens"]
    cached_tokens = usage.get("cached_tokens") or 0
    if type(cached_tokens) is not int or cached_tokens < 0:
        cached_tokens = 0
    cached_tokens = min(cached_tokens, prompt_tokens)
    fresh_tokens = prompt_tokens - cached_tokens

    cost = (
        fresh_tokens * input_rate
        + cached_tokens * cached_rate
        + usage["completion_tokens"] * output_rate
    ) / 1_000_000
    return round(cost, 10)


def price_identity(price):
    """用于报告里的"单价版本"披露。"""
    if not isinstance(price, dict):
        return None
    return {
        "price_date": price.get("price_date"),
        "fetched_at": price.get("fetched_at"),
        "source_url": price.get("source_url"),
        "input_price_per_1m": price.get("input_price_per_1m"),
        "output_price_per_1m": price.get("output_price_per_1m"),
        "cached_input_price_per_1m": price.get("cached_input_price_per_1m"),
    }
