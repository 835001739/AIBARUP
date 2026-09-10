"""ComfyUI HTTP API 客户端（M10 / M11 共用）。

对外只暴露四个能力：``object_info`` / ``upload_image`` / ``submit_prompt`` / ``wait_history``。
硬性约束（HARNESS §6 + PRD M10.2）：
- 每个请求都带超时，任何网络/解析异常都被捕获；
- **禁止抛出未捕获异常**：失败时返回 ``None``/``False``，或抛出带稳定错误码的
  ``ComfyUIError``（它是 ``AIBARError`` 的子类，路由层可转成统一响应）；
- 只把**受控文件名**传给 ComfyUI，绝不传递用户提交的任意本机路径。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

from config import Config
from core.errors import AIBARError
from core.logging_setup import get_logger

_LOGGER = get_logger("aibar.reverse.comfyui")

PROBE_TIMEOUT = 3.0
SUBMIT_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 180.0
HISTORY_TIMEOUT = 10.0
POLL_INTERVAL = 1.0
POLL_MAX_INTERVAL = 3.0

# 稳定错误码（Provider 会据此映射状态，不得统一显示为“未配置 Provider”）
ERR_OFFLINE = "comfyui_offline"
ERR_TIMEOUT = "comfyui_timeout"
ERR_SUBMIT = "submit_failed"
ERR_WORKFLOW = "workflow_failed"
ERR_CANCELLED = "cancelled"
ERR_UPLOAD = "upload_failed"
ERR_NODE_MISSING = "missing_node"

# 错误信息关键字 -> 面向用户的错误码，用于把一次失败归类到可操作的修复动作
_FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    ("out of memory", "out_of_memory"),
    ("oom", "out_of_memory"),
    ("mps backend", "out_of_memory"),
    ("not enough memory", "out_of_memory"),
    ("no such file", "missing_model"),
    ("model not found", "missing_model"),
    ("does not exist", "missing_model"),
    ("mmproj", "missing_mmproj"),
    ("clip vision", "missing_mmproj"),
    ("invalid prompt", "missing_node"),
    ("node type", "missing_node"),
    ("not installed", "missing_node"),
    ("cannot import", "incompatible_runtime"),
    ("llama_cpp", "incompatible_runtime"),
    ("metal", "incompatible_runtime"),
)


class ComfyUIError(AIBARError):
    """ComfyUI 相关业务异常。``code`` 为稳定英文错误码。"""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(code, message, status)


def base_url() -> str:
    return Config.comfyui_base()


def _url(path: str) -> str:
    return f"{base_url()}{path}"


def _default_timeout() -> float:
    try:
        value = float(Config.COMFYUI_TIMEOUT)
    except (TypeError, ValueError):
        value = PROBE_TIMEOUT
    return value if value > 0 else PROBE_TIMEOUT


def classify_failure(message: str, fallback: str = ERR_WORKFLOW) -> str:
    """把 ComfyUI 的错误文本归类为稳定错误码（只做关键字匹配，不泄漏细节）。"""
    text = (message or "").lower()
    for hint, code in _FAILURE_HINTS:
        if hint in text:
            return code
    return fallback


# ---------------------------------------------------------------- 基础请求


def _get(path: str, timeout: float) -> tuple[bool, Any]:
    try:
        response = requests.get(_url(path), timeout=timeout)
    except requests.RequestException:
        return False, None
    except Exception:  # 兜底：任何非预期异常都不能冒泡
        return False, None
    if response.status_code != 200:
        return False, None
    try:
        return True, response.json()
    except ValueError:
        return False, None


def is_reachable(timeout: float | None = None) -> tuple[bool, int]:
    """探测 ComfyUI 是否可达，返回 ``(是否可达, 耗时毫秒)``。"""
    # 用轻量的 /system_stats 探活，避免每次健康检查都拉取上 MB 的节点清单
    started = time.perf_counter()
    ok, _ = _get("/system_stats", timeout or _default_timeout())
    latency = int((time.perf_counter() - started) * 1000)
    return ok, latency


def object_info(node_class: str | None = None, timeout: float | None = None) -> dict | None:
    """读取节点契约。

    Args:
        node_class: 节点 class name；``None`` 表示拉取全部。

    Returns:
        节点信息字典（``{"input": {...}, "output": [...], ...}``）或全部节点字典；
        节点不存在、服务不可达或解析失败返回 ``None``。
    """
    path = "/object_info" if not node_class else f"/object_info/{node_class}"
    if timeout is None:
        timeout = 15.0 if not node_class else max(_default_timeout(), 5.0)
    ok, payload = _get(path, timeout)
    if not ok or not isinstance(payload, dict):
        return None
    if node_class:
        # ComfyUI 对单节点请求直接返回该节点契约；少数版本包一层
        if "input" in payload:
            return payload
        if len(payload) == 1:
            return next(iter(payload.values()))
        return None
    return payload


def upload_image(
    path: str,
    name: str,
    *,
    subfolder: str = "",
    overwrite: bool = True,
    timeout: float = UPLOAD_TIMEOUT,
) -> bool:
    """把受控副本上传到 ComfyUI input 目录。

    Args:
        path: 受控缓存文件的绝对路径（仅内部使用，不进入日志）。
        name: 传到 ComfyUI 的**受控文件名**，只能来自内容哈希，不接受用户路径。

    Returns:
        是否上传成功。
    """
    try:
        with open(path, "rb") as fh:
            files = {"image": (name, fh.read(), "application/octet-stream")}
    except OSError as exc:
        _LOGGER.warning("comfyui_upload_read_failed reason=%s", type(exc).__name__)
        raise ComfyUIError(ERR_UPLOAD, "读取受控图片副本失败") from exc
    data = {
        "overwrite": "true" if overwrite else "false",
        "subfolder": subfolder,
    }
    try:
        response = requests.post(
            _url("/upload/image"), files=files, data=data, timeout=timeout
        )
    except requests.RequestException as exc:
        _LOGGER.warning("comfyui_upload_failed reason=%s", type(exc).__name__)
        raise ComfyUIError(ERR_OFFLINE, "无法连接 ComfyUI，请确认它正在运行") from exc
    except Exception as exc:  # 兜底
        _LOGGER.warning("comfyui_upload_error reason=%s", type(exc).__name__)
        raise ComfyUIError(ERR_UPLOAD, "上传图片到 ComfyUI 失败") from exc
    if response.status_code != 200:
        raise ComfyUIError(ERR_UPLOAD, "上传图片到 ComfyUI 失败")
    return True


def _iter_node_errors(errs):
    """把单个节点的错误信息规范化为可读字符串列表。"""
    if isinstance(errs, list):
        for e in errs:
            if isinstance(e, dict):
                text = e.get("message") or e.get("details") or e.get("type") or ""
                if text:
                    yield str(text)
            elif isinstance(e, str) and e.strip():
                yield e
    elif isinstance(errs, dict):
        text = errs.get("message") or errs.get("details") or errs.get("type") or ""
        if text:
            yield str(text)
    elif isinstance(errs, str) and errs.strip():
        yield errs


def _format_comfyui_reason(body):
    """把 ComfyUI 的 /prompt 拒绝响应整理成面向用户、可定位的原因。

    优先展示 ``extra_info.node_errors``（精确指出哪个节点缺什么），其次是
    ``error.message`` / ``error.details``；不泄漏任何本地路径。
    """
    if not isinstance(body, dict):
        return "ComfyUI 返回了无法解析的拒绝响应（非 JSON）"
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    etype = error.get("type") or body.get("type") or "prompt_rejected"
    message = (error.get("message") if isinstance(error.get("message"), str) else "") or ""
    details = (error.get("details") if isinstance(error.get("details"), str) else "") or ""
    node_errors = {}
    extra = error.get("extra_info") if isinstance(error.get("extra_info"), dict) else {}
    ne = extra.get("node_errors") if isinstance(extra.get("node_errors"), dict) else None
    if not ne and isinstance(body.get("node_errors"), dict):
        ne = body.get("node_errors")
    if isinstance(ne, dict):
        node_errors = ne

    parts = [f"ComfyUI 拒绝了本次提交（{etype}）"]
    if message:
        parts.append(message.strip())
    if node_errors:
        shown = 0
        for node_id, errs in node_errors.items():
            for entry in _iter_node_errors(errs):
                parts.append(f"节点 {node_id}：{entry}")
                shown += 1
                if shown >= 5:
                    parts.append("…（其余节点省略）")
                    break
            if shown >= 5:
                break
    elif details:
        parts.append(str(details).strip())
    return "；".join(p for p in parts if p)


def submit_prompt(graph: dict, client_id: str, timeout: float = SUBMIT_TIMEOUT) -> str:
    """提交最小工作流，返回 ``prompt_id``。

    Raises:
        ComfyUIError: 服务不可达（``comfyui_offline``）、节点缺失或校验失败。
    """
    payload = {"prompt": graph, "client_id": client_id}
    try:
        response = requests.post(_url("/prompt"), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ComfyUIError(ERR_OFFLINE, "无法连接 ComfyUI，请确认它正在运行") from exc
    except Exception as exc:
        raise ComfyUIError(ERR_SUBMIT, "提交 ComfyUI 任务失败") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200 or not isinstance(body, dict):
        reason = _format_comfyui_reason(body)
        raise ComfyUIError(classify_failure(reason, ERR_SUBMIT), reason)
    prompt_id = body.get("prompt_id")
    if not prompt_id:
        reason = _format_comfyui_reason(body)
        raise ComfyUIError(classify_failure(reason, ERR_SUBMIT), reason)
    return str(prompt_id)


def history(prompt_id: str, timeout: float = HISTORY_TIMEOUT) -> dict | None:
    """读取一次任务的历史输出；任务不存在或尚未产出返回 ``None``。"""
    ok, payload = _get(f"/history/{prompt_id}", timeout)
    if not ok or not isinstance(payload, dict):
        return None
    entry = payload.get(prompt_id)
    if not isinstance(entry, dict):
        # 极少数版本返回单键包装
        for value in payload.values():
            if isinstance(value, dict) and "outputs" in value:
                entry = value
                break
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    if not outputs and not status:
        return None
    return {"prompt_id": prompt_id, "outputs": outputs, "status": status}


def _status_error(status: dict) -> str | None:
    """从 history.status.messages 中提取执行错误文本。"""
    messages = status.get("messages")
    if not isinstance(messages, list):
        return None
    for item in messages:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        kind, payload = item[0], item[1]
        if not isinstance(kind, str) or "error" not in kind.lower():
            continue
        if isinstance(payload, dict):
            detail = str(
                payload.get("exception_message")
                or payload.get("message")
                or payload.get("error")
                or ""
            )
            return detail or kind
        return str(payload)
    return None


def wait_history(
    prompt_id: str,
    timeout: float = 180.0,
    interval: float = POLL_INTERVAL,
    cancel: Callable[[], bool] | None = None,
) -> dict | None:
    """轮询任务历史直到完成、取消或超时。

    Args:
        prompt_id: 任务 ID。
        timeout: 总超时秒数。
        interval: 轮询间隔秒数（内部会做上限保护）。
        cancel: 取消回调，返回 True 时立即中止等待。

    Returns:
        历史条目 ``{"prompt_id", "outputs", "status"}``；超时返回 ``None``。

    Raises:
        ComfyUIError: 任务被取消（``cancelled``）或执行失败（``workflow_failed`` 等）。
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    sleep_for = min(max(0.3, float(interval)), POLL_MAX_INTERVAL)
    while True:
        if cancel is not None:
            try:
                if cancel():
                    raise ComfyUIError(ERR_CANCELLED, "反推任务已取消")
            except ComfyUIError:
                raise
            except Exception:
                pass
        entry = history(prompt_id)
        if entry is not None:
            status = entry.get("status") or {}
            if status.get("completed") or entry.get("outputs"):
                detail = _status_error(status)
                if detail:
                    raise ComfyUIError(
                        classify_failure(detail), f"ComfyUI 工作流执行失败：{classify_failure(detail)}"
                    )
                return entry
        if time.monotonic() >= deadline:
            _LOGGER.warning("comfyui_wait_timeout prompt_id=%s", prompt_id)
            raise ComfyUIError(ERR_TIMEOUT, "ComfyUI 反推任务超时，请重试或降低分析精度")
        time.sleep(sleep_for)


def collect_text_outputs(entry: dict, node_id: str | None = None) -> list[tuple[str, str, str]]:
    """从历史输出中收集文本，返回 ``[(node_id, output_key, text), ...]``。

    只做结构提取，不做语义猜测；Provider 再按自己的契约挑选需要的输出。
    """
    results: list[tuple[str, str, str]] = []
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    if not isinstance(outputs, dict):
        return results
    for key, value in outputs.items():
        if node_id and str(key) != str(node_id):
            continue
        if not isinstance(value, dict):
            continue
        for out_name, out_value in value.items():
            texts: list[str] = []
            if isinstance(out_value, str):
                texts = [out_value]
            elif isinstance(out_value, list):
                for item in out_value:
                    if isinstance(item, str):
                        texts.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        texts.append(item["text"])
            elif isinstance(out_value, dict) and isinstance(out_value.get("text"), str):
                texts = [out_value["text"]]
            for text in texts:
                if text.strip():
                    results.append((str(key), str(out_name), text))
    return results


__all__ = [
    "ComfyUIError",
    "ERR_CANCELLED",
    "ERR_NODE_MISSING",
    "ERR_OFFLINE",
    "ERR_SUBMIT",
    "ERR_TIMEOUT",
    "ERR_UPLOAD",
    "ERR_WORKFLOW",
    "POLL_INTERVAL",
    "base_url",
    "classify_failure",
    "collect_text_outputs",
    "history",
    "is_reachable",
    "object_info",
    "submit_prompt",
    "upload_image",
    "wait_history",
]
