"""OpenAI-compatible 本地 GPT 连接层。

AIBAR 只依赖一个事实标准：``POST {base}/chat/completions`` 与
``GET {base}/models``。Ollama、LM Studio、llama.cpp server、vLLM 等本地
服务都兼容这套接口，因此不需要为每个运行时写适配器。

这里集中解决「本地模型连不上」最常见的四类原因：

1. **端点写错**：用户常写 ``http://127.0.0.1:11434``（Ollama 根地址）或
   直接贴 ``/v1/chat/completions``，原实现无脑再拼一次路径导致 404。
   这里统一归一化到 ``.../v1``。
2. **模型名没填**：本地服务通常只加载一个模型，允许从 ``/v1/models``
   自动取第一个；显式配置的模型优先。
3. **代理劫持回环请求**：``HTTP_PROXY`` 存在时 requests 会把
   ``127.0.0.1`` 也发给代理。config 已补 ``NO_PROXY``，这里再对回环
   地址做一次兜底。
4. **错误不可读**：把连接拒绝 / 404 / 401 / 429 / 超时 / 空返回分别
   映射成带中文修复建议的错误码，而不是笼统的「连接失败」。

隐私：API Key 只用于请求头，绝不进入返回值、异常文本或日志；外部
（非回环）端点只回报「已配置」，不回报完整地址。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

from config import CFG

DEFAULT_TIMEOUT = 60
CONNECT_TIMEOUT = 5
MODEL_CACHE_SECONDS = 30
MAX_MODELS_IN_STATUS = 30

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

_ENDPOINT_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/completions",
    "/completions",
    "/v1/models",
    "/models",
    "/api/chat",
    "/api/generate",
)


class LLMError(Exception):
    """本地 GPT 调用失败，携带可展示的错误码、中文原因与修复建议。"""

    def __init__(self, code: str, message: str, *, status: int = 0, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status or 0)
        self.hint = hint


def _str(value: Any) -> str:
    return str(value or "").strip()


def _timeout() -> tuple[float, float]:
    """连接超时固定 5s，读取超时用配置值（本地大模型首次加载可能很慢）。"""
    read = max(1, int(CFG.AI_PROVIDER_TIMEOUT or DEFAULT_TIMEOUT))
    return (float(CONNECT_TIMEOUT), float(read))


def normalize_base_url(raw: Any) -> str:
    """把用户填写的各种端点归一化成 ``scheme://host[:port][/path]``。

    规则（按顺序）：
    - 没有 scheme 时补 ``http://``；
    - 去掉末尾斜杠、query、fragment；
    - 剥掉常见的完整接口后缀（``/chat/completions``、``/models`` 等）；
    - 空路径、``/api`` 映射到 ``/v1``（Ollama 原生路径）；
    - 其余路径保持原样，避免破坏 vLLM 的 ``/openai/v1`` 这类自定义前缀。

    Raises:
        LLMError: 端点不是 http/https 或无法解析。
    """
    text = _str(raw)
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text

    try:
        parsed = urlsplit(text)
    except ValueError:
        raise LLMError(
            "invalid_endpoint",
            "AI 服务端点格式不正确",
            hint="示例：http://127.0.0.1:11434/v1",
        )

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMError(
            "invalid_endpoint",
            "AI 服务端点必须是 http(s) 地址",
            hint="示例：http://127.0.0.1:11434/v1",
        )

    path = re.sub(r"/+$", "", parsed.path or "")
    changed = True
    while changed:
        changed = False
        for suffix in _ENDPOINT_SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                changed = True
                break

    if path in ("", "/"):
        path = "/v1"
    elif path == "/api" or path.endswith("/api"):
        path = path[: -len("/api")] + "/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _host_is_loopback(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.") or host.endswith(".localhost")


def endpoint_info(raw: Any = None) -> dict[str, Any]:
    """返回脱敏后的端点信息，供状态接口与排障脚本使用。"""
    base = normalize_base_url(raw if raw is not None else CFG.AI_PROVIDER_ENDPOINT)
    if not base:
        return {
            "configured": False,
            "loopback": False,
            "label": "",
            "host": "",
            "port": 0,
            "path": "",
        }

    parsed = urlsplit(base)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loopback = _host_is_loopback(host)
    label = f"{host}:{port}{parsed.path}" if loopback else "外部服务（地址已隐藏）"
    return {
        "configured": True,
        "loopback": loopback,
        "label": label,
        "host": host,
        "port": port,
        "path": parsed.path,
    }


def is_enabled() -> bool:
    """功能开关是否打开。"""
    return bool(CFG.AI_PROVIDER_ENABLED)


def is_configured() -> bool:
    """开关打开且端点有效；模型可留空由 ``/v1/models`` 自动发现。"""
    if not is_enabled():
        return False
    if not _str(CFG.AI_PROVIDER_ENDPOINT):
        return False
    try:
        normalize_base_url(CFG.AI_PROVIDER_ENDPOINT)
    except LLMError:
        return False
    if _str(CFG.AI_PROVIDER_MODEL):
        return True
    return bool(CFG.AI_PROVIDER_AUTO_MODEL)


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = _str(CFG.AI_PROVIDER_API_KEY)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _prefer_no_proxy(base: str) -> None:
    """把回环主机补进 NO_PROXY，避免外层代理把本机请求转走。

    config 在导入时已做过一次；这里再补是因为用户可能在运行期修改了
    代理环境变量。只改进程环境，不影响系统设置。
    """
    info = endpoint_info(base)
    if not info.get("loopback"):
        return
    import os

    host = info["host"]
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        parts = [part.strip() for part in current.split(",") if part.strip()]
        if host not in parts:
            parts.append(host)
        if "localhost" not in parts:
            parts.append("localhost")
        if "127.0.0.1" not in parts:
            parts.append("127.0.0.1")
        os.environ[key] = ",".join(parts)


def _error_body(response: Any) -> str:
    """尽量从服务端 JSON 里取出可读错误，但不回显任何请求内容。"""
    try:
        data = response.json()
    except (ValueError, AttributeError):
        return ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return _str(error.get("message") or error.get("code"))
        if isinstance(error, str):
            return _str(error)
        return _str(data.get("message"))
    return ""


def _raise_for_http(response: Any, *, url: str) -> None:
    """把 HTTP 错误映射成带修复建议的 ``LLMError``。"""
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 400:
        return

    detail = _error_body(response)
    lowered = detail.lower()

    if status in (401, 403):
        raise LLMError(
            "auth_failed",
            f"AI 服务拒绝访问（HTTP {status}）",
            status=status,
            hint="该服务要求 API Key；本地服务通常可以留空 AI_PROVIDER_API_KEY。",
        )
    if status == 404:
        if "model" in lowered:
            raise LLMError(
                "model_not_found",
                "AI 服务已连通，但模型名不存在",
                status=status,
                hint="把 AI_PROVIDER_MODEL 改成 /v1/models 返回的 id，或留空自动选择。",
            )
        raise LLMError(
            "endpoint_not_found",
            "AI 服务路径不存在（HTTP 404）",
            status=status,
            hint="端点通常应以 /v1 结尾，例如 http://127.0.0.1:11434/v1。",
        )
    if status == 429:
        raise LLMError(
            "rate_limited",
            "AI 服务触发限流（HTTP 429）",
            status=status,
            hint="稍后重试，或降低并发。",
        )
    if status >= 500:
        raise LLMError(
            "server_error",
            f"AI 服务内部错误（HTTP {status}）",
            status=status,
            hint="查看本地模型服务日志；模型可能加载失败或显存不足。",
        )
    raise LLMError(
        "http_error",
        f"AI 服务返回 HTTP {status}",
        status=status,
        hint=detail[:300] or "检查模型名与请求参数。",
    )


def _post_chat(url: str, payload: dict[str, Any], timeout: Any) -> Any:
    try:
        return requests.post(url, headers=_headers(), json=payload, timeout=timeout)
    except requests.exceptions.ProxyError:
        raise LLMError(
            "proxy_error",
            "请求被 HTTP 代理拦截",
            hint="把 127.0.0.1 与 localhost 加入 NO_PROXY，或临时关闭 HTTP_PROXY。",
        )
    except requests.exceptions.Timeout:
        raise LLMError(
            "timeout",
            "连接本地模型超时",
            hint="首次加载大模型可能需要更久；调大 AI_PROVIDER_TIMEOUT 后重试。",
        )
    except requests.exceptions.ConnectionError:
        raise LLMError(
            "connection_refused",
            "本地模型服务没有响应",
            hint="确认服务已启动、端口正确：Ollama 默认 11434，LM Studio 默认 1234。",
        )
    except requests.exceptions.RequestException as exc:
        raise LLMError(
            "request_failed",
            "调用本地模型失败：%s" % type(exc).__name__,
            hint="检查端点、网络与模型服务日志。",
        )


def _get_models(url: str, timeout: Any) -> Any:
    try:
        return requests.get(url, headers=_headers(), timeout=timeout)
    except requests.exceptions.ProxyError:
        raise LLMError(
            "proxy_error",
            "请求被 HTTP 代理拦截",
            hint="把 127.0.0.1 与 localhost 加入 NO_PROXY，或临时关闭 HTTP_PROXY。",
        )
    except requests.exceptions.Timeout:
        raise LLMError("timeout", "读取模型列表超时", hint="确认本地服务没有卡死。")
    except requests.exceptions.ConnectionError:
        raise LLMError(
            "connection_refused",
            "本地模型服务没有响应",
            hint="确认服务已启动、端口正确：Ollama 默认 11434，LM Studio 默认 1234。",
        )
    except requests.exceptions.RequestException as exc:
        raise LLMError(
            "request_failed",
            "读取模型列表失败：%s" % type(exc).__name__,
            hint="检查端点与本地服务日志。",
        )


def _models_from_payload(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    raw = data.get("data")
    if raw is None:
        raw = data.get("models")
    if not isinstance(raw, list):
        return []
    models: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = _str(item.get("id") or item.get("name"))
        else:
            name = ""
        if name and name not in models:
            models.append(name)
    return models


def list_models(base_url: str | None = None, timeout: Any = None) -> list[str]:
    """读取 ``GET {base}/models``；失败时抛 ``LLMError``。"""
    base = normalize_base_url(base_url if base_url is not None else CFG.AI_PROVIDER_ENDPOINT)
    if not base:
        raise LLMError("endpoint_missing", "还没有配置 AI 服务端点")
    _prefer_no_proxy(base)
    response = _get_models(base + "/models", timeout or _timeout())
    _raise_for_http(response, url=base + "/models")
    try:
        data = response.json()
    except (ValueError, AttributeError):
        raise LLMError(
            "bad_response",
            "模型列表不是合法 JSON",
            hint="确认端点指向 OpenAI-compatible 的 /v1。",
        )
    return _models_from_payload(data)


_model_cache: dict[str, tuple[float, list[str]]] = {}


def resolve_model(base_url: str | None = None) -> str:
    """返回要调用的模型名；未配置时从 ``/v1/models`` 取第一个并短时缓存。"""
    explicit = _str(CFG.AI_PROVIDER_MODEL)
    if explicit:
        return explicit
    if not CFG.AI_PROVIDER_AUTO_MODEL:
        raise LLMError(
            "model_missing",
            "还没有配置模型名",
            hint="在 .env 填 AI_PROVIDER_MODEL，或把 AI_PROVIDER_AUTO_MODEL 设为 true。",
        )

    base = normalize_base_url(base_url if base_url is not None else CFG.AI_PROVIDER_ENDPOINT)
    now = time.monotonic()
    cached = _model_cache.get(base)
    if cached and now - cached[0] < MODEL_CACHE_SECONDS and cached[1]:
        return cached[1][0]

    models = list_models(base)
    if not models:
        raise LLMError(
            "model_missing",
            "本地服务没有返回任何模型",
            hint="先在 Ollama/LM Studio 中下载并加载一个模型。",
        )
    _model_cache[base] = (now, models)
    return models[0]


def _content_text(data: Any) -> str:
    """兼容字符串 content 与新版 parts 数组 content。"""
    if not isinstance(data, dict):
        raise LLMError("bad_response", "AI 服务返回不是 JSON 对象")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(
            "bad_response",
            "AI 服务返回缺少 choices",
            hint="确认端点提供的是 Chat Completions 接口。",
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if content is None and isinstance(choice, dict):
        content = choice.get("text")

    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text_part = part.get("text")
                if isinstance(text_part, str):
                    parts.append(text_part)
        text = "".join(parts).strip()
    else:
        text = ""

    if not text:
        raise LLMError(
            "empty_content",
            "AI 服务返回了空内容",
            hint="调大 AI_PROVIDER_MAX_TOKENS，或换一个非推理模型。",
        )
    return text


def chat(
    messages: Iterable[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0,
    max_tokens: int | None = None,
    timeout: Any = None,
) -> str:
    """调用 Chat Completions，返回纯文本内容。

    Args:
        messages: OpenAI chat 消息数组，至少一条。
        model: 覆盖配置里的模型名；省略时走显式配置或自动发现。
        temperature: 默认 0，便于复现。
        max_tokens: 输出上限；默认读 ``AI_PROVIDER_MAX_TOKENS``。
        timeout: 覆盖默认超时。

    Raises:
        LLMError: 未配置、连接失败、HTTP 错误、返回结构异常或空内容。
    """
    if not is_enabled():
        raise LLMError(
            "provider_disabled",
            "本地 GPT 未启用",
            hint="在 .env 设置 AI_PROVIDER_ENABLED=true 后重启服务。",
        )

    base = normalize_base_url(CFG.AI_PROVIDER_ENDPOINT)
    if not base:
        raise LLMError(
            "endpoint_missing",
            "还没有配置本地 GPT 端点",
            hint="在 .env 设置 AI_PROVIDER_ENDPOINT，例如 http://127.0.0.1:11434/v1。",
        )

    message_list = [dict(item) for item in messages if isinstance(item, dict)]
    if not message_list:
        raise LLMError("invalid_input", "messages 不能为空")

    _prefer_no_proxy(base)
    chosen_model = model or resolve_model(base)
    payload: dict[str, Any] = {
        "model": chosen_model,
        "messages": message_list,
        "temperature": float(temperature or 0),
        "stream": False,
    }
    token_limit = int(max_tokens if max_tokens is not None else CFG.AI_PROVIDER_MAX_TOKENS or 0)
    if token_limit > 0:
        payload["max_tokens"] = token_limit

    response = _post_chat(base + "/chat/completions", payload, timeout or _timeout())
    _raise_for_http(response, url=base + "/chat/completions")
    try:
        data = response.json()
    except (ValueError, AttributeError):
        raise LLMError(
            "bad_response",
            "AI 服务返回不是合法 JSON",
            hint="确认端点指向 OpenAI-compatible 的 /v1。",
        )
    return _content_text(data)


def _base_status() -> dict[str, Any]:
    endpoint_error = ""
    try:
        info = endpoint_info(CFG.AI_PROVIDER_ENDPOINT)
    except LLMError as exc:
        info = {
            "configured": False,
            "loopback": False,
            "label": "",
            "host": "",
            "port": 0,
            "path": "",
        }
        endpoint_error = exc.message
    explicit_model = _str(CFG.AI_PROVIDER_MODEL)
    return {
        "enabled": is_enabled(),
        "configured": bool(info.get("configured")) and is_configured(),
        "endpoint": info,
        "endpoint_error": endpoint_error,
        "model": explicit_model,
        "model_source": "config" if explicit_model else ("auto" if CFG.AI_PROVIDER_AUTO_MODEL else ""),
        "auto_model": bool(CFG.AI_PROVIDER_AUTO_MODEL),
        "api_key_configured": bool(_str(CFG.AI_PROVIDER_API_KEY)),
        "timeout_seconds": max(1, int(CFG.AI_PROVIDER_TIMEOUT or DEFAULT_TIMEOUT)),
        "max_tokens": max(0, int(CFG.AI_PROVIDER_MAX_TOKENS or 0)),
    }


def provider_status(probe: bool = False) -> dict[str, Any]:
    """返回可展示的连接状态；``probe=True`` 时额外探测 ``/v1/models``。

    无论成功失败都返回 200 结构，不抛异常：状态页本身必须永远可用。
    """
    status = _base_status()
    status.update(
        {
            "reachable": False,
            "models": [],
            "model_available": False,
            "latency_ms": 0,
            "error_code": "",
            "error_message": "",
            "hint": "",
        }
    )

    if not status["enabled"]:
        status["error_code"] = "provider_disabled"
        status["error_message"] = "本地 GPT 未启用"
        status["hint"] = "在 .env 设置 AI_PROVIDER_ENABLED=true 后重启 AIBAR。"
        return status
    if status.get("endpoint_error"):
        status["error_code"] = "invalid_endpoint"
        status["error_message"] = status["endpoint_error"]
        status["hint"] = "示例：http://127.0.0.1:11434/v1"
        return status
    if not status["configured"]:
        status["error_code"] = "endpoint_missing"
        status["error_message"] = "还没有配置本地 GPT 端点"
        status["hint"] = "在 .env 设置 AI_PROVIDER_ENDPOINT，例如 http://127.0.0.1:11434/v1。"
        return status

    if not probe:
        return status

    started = time.perf_counter()
    try:
        models = list_models(timeout=(CONNECT_TIMEOUT, 10))
    except LLMError as exc:
        status["latency_ms"] = int((time.perf_counter() - started) * 1000)
        status["error_code"] = exc.code
        status["error_message"] = exc.message
        status["hint"] = exc.hint
        return status

    status["latency_ms"] = int((time.perf_counter() - started) * 1000)
    status["reachable"] = True
    status["models"] = models[:MAX_MODELS_IN_STATUS]
    model = _str(CFG.AI_PROVIDER_MODEL)
    if model:
        status["model_available"] = model in models
        if not status["model_available"]:
            status["hint"] = (
                "服务已连通，但没有找到模型 %s；可用模型：%s"
                % (model, "、".join(models[:8]) or "（空）")
            )
    elif models:
        status["model"] = models[0]
        status["model_available"] = True
    else:
        status["error_code"] = "model_missing"
        status["error_message"] = "本地服务没有返回任何模型"
        status["hint"] = "先在 Ollama/LM Studio 中下载并加载一个模型。"
    return status


def provider_test() -> dict[str, Any]:
    """完整自检：列出模型 + 发一条极短对话，验证推理链路。"""
    status = provider_status(probe=True)
    status.update({"chat_ok": False, "reply_len": 0, "chat_latency_ms": 0})
    if not status.get("reachable") or not status.get("model_available"):
        return status

    started = time.perf_counter()
    try:
        # 复用探测阶段已经解析到的模型名，避免把 /v1/models 再请求一遍。
        reply = chat(
            [
                {"role": "system", "content": "只回复 OK，不要解释。"},
                {"role": "user", "content": "ping"},
            ],
            model=status.get("model") or None,
            max_tokens=16,
        )
    except LLMError as exc:
        status["chat_latency_ms"] = int((time.perf_counter() - started) * 1000)
        status["error_code"] = exc.code
        status["error_message"] = exc.message
        status["hint"] = exc.hint
        return status

    status["chat_latency_ms"] = int((time.perf_counter() - started) * 1000)
    status["chat_ok"] = True
    status["reply_len"] = len(reply)
    status["error_code"] = ""
    status["error_message"] = ""
    status["hint"] = ""
    return status


__all__ = [
    "LLMError",
    "chat",
    "endpoint_info",
    "is_configured",
    "is_enabled",
    "list_models",
    "normalize_base_url",
    "provider_status",
    "provider_test",
    "resolve_model",
]
