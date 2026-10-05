# -*- coding: utf-8 -*-
"""列出当前 key 可用的模型 id（只读，零推理费用）。

为什么需要它：选型清单上的模型名是**展示名**，调用时要填**平台的真实 model id**
（通常是 `厂商/模型` 形式）。填错会直接 400，所以需要用一次 /v1/models 拿准确字符串。

用法（Key 只从环境变量读，脚本不会打印它）：

    $env:SILICONFLOW_API_KEY = 'sk-xxxxxxxx'
    py -3 tools\\list_models.py
    py -3 tools\\list_models.py --filter Qwen,GLM,DeepSeek --show 200

可选：
    --filter a,b     只看名称含这些关键词的模型
    --show N         最多显示多少条（默认 40；配合过滤用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.siliconflow.cn/v1"
KEY_ENV = "SILICONFLOW_API_KEY"
WATCH = ("Qwen3.5-27B", "Qwen3.5-35B-A3B", "GLM-5.3", "GLM-4.5-Air", "DeepSeek-V4-Pro", "DeepSeek-V3.2")


def get(url, key, timeout=30.0):
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    # 显式禁用代理：本机代理会把请求打成 ConnectionReset
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="列出硅基流动可用模型 id（只读）")
    parser.add_argument("--filter", default="")
    parser.add_argument("--show", type=int, default=40)
    parser.add_argument("--no-balance", action="store_true", help="跳过余额探测")
    args = parser.parse_args()

    key = (os.environ.get(KEY_ENV) or "").strip()
    if not key:
        print(f"[拒绝] 环境变量 {KEY_ENV} 为空，未发送请求。")
        print(f'       先执行：$env:{KEY_ENV} = "sk-你的key"')
        return 2

    try:
        data = get(BASE + "/models", key)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[失败] HTTP {exc.code}：{body}")
        print("       401/403 通常表示 Key 无效或无权限；请检查环境变量是否带换行或空格。")
        return 3
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}")
        return 3

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print("[失败] /v1/models 返回结构不符合预期。")
        print(json.dumps(data, ensure_ascii=False)[:500])
        return 4

    ids = sorted(str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id"))
    print(f"[ok] 该 Key 可用模型总数：{len(ids)}")

    keywords = [k.strip() for k in args.filter.split(",") if k.strip()]
    shown = [i for i in ids if not keywords or any(k.lower() in i.lower() for k in keywords)]
    if keywords:
        print(f"[filter] 命中 {len(shown)} 个（关键词：{','.join(keywords)}）")
    for model_id in shown[: args.show]:
        print("  " + model_id)
    if len(shown) > args.show:
        print(f"  …… 还有 {len(shown) - args.show} 个未显示，可用 --show 或 --filter 收窄")

    print("\n[关注项] 本线候选模型是否在列表里：")
    for name in WATCH:
        hits = [i for i in ids if name.lower() in i.lower()]
        if hits:
            print(f"  ✓ {name}")
            for hit in hits:
                print(f"      {hit}")
        else:
            print(f"  ✗ {name}（未在该 Key 的可用列表中）")

    if not args.no_balance:
        print("\n[余额探测] 尝试读取账户信息（接口可用性以平台文档为准）：")
        try:
            info = get(BASE + "/user/info", key)
            print(json.dumps(info, ensure_ascii=False, indent=2)[:800])
        except Exception as exc:
            print(f"  该接口不可用或不返回余额（{type(exc).__name__}），请到控制台查看余额。")

    print("\n把上面「✓」的完整 id 发回来，我写进 prices/prices_v1.json。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
