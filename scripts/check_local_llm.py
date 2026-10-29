#!/usr/bin/env python3
"""本地 GPT 连接自检。

用途：不改代码、不重启 AIBAR，直接在终端确认「本地模型到底卡在哪」。
默认读取 .env 的 ``AI_PROVIDER_*``，探测 ``/v1/models`` 并发一条极短对话；
也可以用 ``--endpoint`` 临时覆盖，先把服务调通再写回 .env。

用法::

    .venv/bin/python scripts/check_local_llm.py
    .venv/bin/python scripts/check_local_llm.py --endpoint http://127.0.0.1:11434
    .venv/bin/python scripts/check_local_llm.py --endpoint http://127.0.0.1:1234/v1 --model qwen2.5-7b-instruct
    .venv/bin/python scripts/check_local_llm.py --json

返回码：0=模型已正常响应；1=仍未连通或推理失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 AIBAR 的本地 GPT 连接")
    parser.add_argument("--endpoint", default="", help="临时覆盖 AI_PROVIDER_ENDPOINT")
    parser.add_argument("--model", default="", help="临时覆盖 AI_PROVIDER_MODEL（留空自动发现）")
    parser.add_argument("--timeout", type=int, default=0, help="临时覆盖 AI_PROVIDER_TIMEOUT 秒数")
    parser.add_argument("--no-chat", action="store_true", help="只探测 /v1/models，不发对话")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 必须在导入 config 之前写入环境变量：Config 在导入时读取 .env 与环境。
    if args.endpoint:
        os.environ["AI_PROVIDER_ENDPOINT"] = args.endpoint
        os.environ["AI_PROVIDER_ENABLED"] = "true"
    if args.model:
        os.environ["AI_PROVIDER_MODEL"] = args.model
    if args.timeout:
        os.environ["AI_PROVIDER_TIMEOUT"] = str(args.timeout)

    from core import llm  # noqa: WPS433 - 延迟导入以应用上面的覆盖

    result = llm.provider_status(probe=True) if args.no_chat else llm.provider_test()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        ok = bool(result.get("chat_ok") if not args.no_chat else result.get("reachable"))
        return 0 if ok else 1

    status = result
    print("本地 GPT 连接自检")
    print("=" * 42)
    print("启用        :", "是" if status.get("enabled") else "否")
    print("端点        :", (status.get("endpoint") or {}).get("label") or "未配置")
    print("模型        :", status.get("model") or "（自动选择）")
    print("模型来源    :", status.get("model_source") or "未配置")
    print("API Key     :", "已配置" if status.get("api_key_configured") else "未配置（本地服务通常无需）")
    print("超时        :", status.get("timeout_seconds"), "秒")

    if status.get("models"):
        print("可用模型    :", "、".join(status["models"][:12]))
    if status.get("latency_ms"):
        print("模型列表耗时:", status["latency_ms"], "ms")
    if status.get("chat_latency_ms"):
        print("对话耗时    :", status["chat_latency_ms"], "ms")

    if status.get("chat_ok"):
        print("结论        : 通过，模型已正常返回内容。")
        print("下一步      : 在 .env 打开 AI_PROVIDER_ENABLED=true 并重启 AIBAR。")
        return 0

    print("结论        : 未通过")
    if status.get("error_message"):
        print("原因        :", status["error_message"])
    if status.get("hint"):
        print("建议        :", status["hint"])
    print()
    print("常见修复：")
    print("1. 确认本地服务已启动：Ollama 端口 11434，LM Studio 端口 1234。")
    print("2. 端点统一写成 http://127.0.0.1:<端口>/v1；写根地址也可以。")
    print("3. 模型名可用 `curl http://127.0.0.1:<端口>/v1/models` 查看。")
    print("4. 如果设置了 HTTP_PROXY，把 127.0.0.1 与 localhost 加入 NO_PROXY。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
