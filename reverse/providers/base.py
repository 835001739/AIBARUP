"""Provider 抽象基类、状态枚举、节点契约工具与高级 VLM 全局队列。

设计要点（PRD M9.5 / M10.3 / M11.3 / M11.4）：
- 状态枚举**固定**为 10 个取值，前端据此显示中文原因与下一步操作；
- 健康检查**不得**启动真实图片分析或下载模型（M10.4）；
- 高级 VLM（Qwen3-VL / JoyCaption）全局同时最多运行 **1** 个任务，
  其余请求返回排队状态，绝不并行加载多个大模型；
- 节点名不得硬编码：通过 ``/object_info`` 动态发现真实 class name 与输入枚举。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import Config
from core.db import now as db_now
from core.errors import AIBARError
from core.textutil import classify_dimension, has_content_value, profile_keys, split_segments

from .. import comfyui_client, schema
from ..comfyui_client import ComfyUIError

# ---------------------------------------------------------------- 状态枚举（固定）

STATUS_READY = "ready"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_OFFLINE = "offline"
STATUS_MISSING_NODE = "missing_node"
STATUS_MISSING_MODEL = "missing_model"
STATUS_MISSING_MMPROJ = "missing_mmproj"
STATUS_INCOMPATIBLE_RUNTIME = "incompatible_runtime"
STATUS_OUT_OF_MEMORY = "out_of_memory"
STATUS_UNAUTHORIZED = "unauthorized"
STATUS_ERROR = "error"

STATUSES: tuple[str, ...] = (
    STATUS_READY,
    STATUS_UNCONFIGURED,
    STATUS_OFFLINE,
    STATUS_MISSING_NODE,
    STATUS_MISSING_MODEL,
    STATUS_MISSING_MMPROJ,
    STATUS_INCOMPATIBLE_RUNTIME,
    STATUS_OUT_OF_MEMORY,
    STATUS_UNAUTHORIZED,
    STATUS_ERROR,
)

STATUS_LABELS: dict[str, str] = {
    STATUS_READY: "可用",
    STATUS_UNCONFIGURED: "未配置",
    STATUS_OFFLINE: "服务未运行",
    STATUS_MISSING_NODE: "缺少节点",
    STATUS_MISSING_MODEL: "缺少模型",
    STATUS_MISSING_MMPROJ: "缺少视觉投影文件",
    STATUS_INCOMPATIBLE_RUNTIME: "运行时不兼容",
    STATUS_OUT_OF_MEMORY: "显存/内存不足",
    STATUS_UNAUTHORIZED: "未确认外发",
    STATUS_ERROR: "异常",
}

# reason_code -> 中文说明（不展示原始异常堆栈）
REASON_LABELS: dict[str, str] = {
    "comfyui_offline": "ComfyUI 未运行或无法访问，请先启动 ComfyUI",
    "missing_node": "ComfyUI 缺少对应自定义节点，请安装节点后重新检测",
    "missing_model": "模型文件缺失或不完整，请重新安装模型",
    "missing_mmproj": "视觉投影文件（mmproj）缺失，请重新下载",
    "incompatible_runtime": "运行时依赖不兼容，请检查 Metal 版推理运行时",
    "out_of_memory": "内存不足，请关闭其他大模型后重试",
    "unauthorized": "尚未确认将图片发送到外部服务",
    "workflow_failed": "ComfyUI 工作流执行失败，请重试或降低精度",
    "comfyui_timeout": "分析超时，请重试或降低分析精度",
    "timeout": "分析超时，请重试或降低分析精度",
    "provider_offline": "无法连接视觉服务，请检查服务地址与网络",
    "cancelled": "任务已取消",
    "provider_busy": "已有高级视觉任务在运行，请等待其完成后重试",
    "not_configured": "尚未配置该 Provider",
    "no_endpoint": "未配置服务地址",
    "no_api_key": "未配置 API Key（只从环境变量读取）",
    "bad_response": "Provider 返回内容无法解析",
    "submit_failed": "ComfyUI 拒绝了该工作流",
    "upload_failed": "上传受控图片副本到 ComfyUI 失败",
    "internal_error": "Provider 内部错误",
}

# ---------------------------------------------------------------- 精度

PRECISION_FAST = "fast"
PRECISION_STANDARD = "standard"
PRECISION_FINE = "fine"
PRECISIONS: tuple[str, ...] = (PRECISION_FAST, PRECISION_STANDARD, PRECISION_FINE)
DEFAULT_PRECISION = PRECISION_STANDARD
PRECISION_LABELS = {
    PRECISION_FAST: "快速",
    PRECISION_STANDARD: "标准",
    PRECISION_FINE: "精细",
}

DEFAULT_TIMEOUT_SECONDS = 240

# ComfyUI 错误码 -> Provider 状态
_COMFY_STATUS_BY_CODE: dict[str, str] = {
    "comfyui_offline": STATUS_OFFLINE,
    "provider_offline": STATUS_OFFLINE,
    "missing_node": STATUS_MISSING_NODE,
    "missing_model": STATUS_MISSING_MODEL,
    "missing_mmproj": STATUS_MISSING_MMPROJ,
    "out_of_memory": STATUS_OUT_OF_MEMORY,
    "incompatible_runtime": STATUS_INCOMPATIBLE_RUNTIME,
    "unauthorized": STATUS_UNAUTHORIZED,
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "未知状态")


def reason_text(reason_code: str, fallback: str = "") -> str:
    return REASON_LABELS.get(reason_code, fallback or "请重新检测或查看安装指引")


def normalize_precision(precision: str | None) -> str:
    if precision in PRECISIONS:
        return precision  # type: ignore[return-value]
    return DEFAULT_PRECISION


def is_status(value: str) -> bool:
    return value in STATUSES


# ---------------------------------------------------------------- 高级 VLM 队列
#
# PRD M11.4：全局同时最多运行 1 个高级 VLM 任务，其他请求显示排队状态，
# 切换 Provider 前先释放上一个，避免同时驻留多个大模型。

_vlm_lock = threading.RLock()
_vlm_state: dict[str, Any] = {"provider": None, "job": None, "started_at": None, "queued": 0}
VLM_MAX_PARALLEL = 1


def acquire_vlm_slot(provider_key: str, job: Any = None) -> bool:
    """尝试占用高级 VLM 执行槽；被占用时返回 False（调用方返回排队状态）。"""
    with _vlm_lock:
        if _vlm_state["provider"] is not None:
            _vlm_state["queued"] = int(_vlm_state["queued"]) + 1
            return False
        _vlm_state["provider"] = provider_key
        _vlm_state["job"] = job
        _vlm_state["started_at"] = time.time()
        return True


def release_vlm_slot(provider_key: str | None = None) -> None:
    """释放执行槽；``provider_key`` 不匹配时不误释放别人的槽位。"""
    with _vlm_lock:
        if provider_key is not None and _vlm_state["provider"] not in (None, provider_key):
            return
        _vlm_state["provider"] = None
        _vlm_state["job"] = None
        _vlm_state["started_at"] = None


def vlm_queue_state() -> dict[str, Any]:
    """返回队列状态快照（供 UI 显示排队提示）。"""
    with _vlm_lock:
        holder = _vlm_state["provider"]
        return {
            "busy": holder is not None,
            "provider": holder,
            "job_id": _vlm_state["job"],
            "max_parallel": VLM_MAX_PARALLEL,
            "queued": int(_vlm_state["queued"]),
            "running_ms": int((time.time() - _vlm_state["started_at"]) * 1000)
            if _vlm_state["started_at"]
            else 0,
        }


def reset_vlm_queue() -> None:
    """仅供测试与异常恢复使用。"""
    with _vlm_lock:
        _vlm_state.update({"provider": None, "job": None, "started_at": None, "queued": 0})


@contextmanager
def vlm_slot(provider_key: str, job: Any = None):
    """高级 VLM 执行槽上下文；被占用时抛出 ``provider_busy``。"""
    if not acquire_vlm_slot(provider_key, job):
        raise AIBARError("provider_busy", reason_text("provider_busy"), 503)
    try:
        yield
    finally:
        release_vlm_slot(provider_key)


def new_client_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- 安装清单（manifest）
#
# PRD M11.2 / M11.3：节点契约由 ``/object_info`` 实测得到并写回
# ``resources/provider_manifest.json``，作为下次发现的兜底；清单不记录密钥与用户绝对路径。

MANIFEST_FILENAME = "provider_manifest.json"
_MANIFEST_CACHE: dict | None = None
_RUNTIME_PROBE_CACHE: dict[str, tuple[float, bool, str]] = {}
RUNTIME_PROBE_TTL = 300.0


def manifest_path() -> Path:
    return Path(Config.RESOURCES_DIR) / MANIFEST_FILENAME


def load_manifest(force: bool = False) -> dict:
    """读取可复现安装清单；任何读取失败都返回空字典，不影响 Provider 运行。"""
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is not None and not force:
        return _MANIFEST_CACHE
    data: dict = {}
    try:
        with open(manifest_path(), "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    _MANIFEST_CACHE = data
    return data


def manifest_entry(provider_key: str) -> dict:
    providers = load_manifest().get("providers")
    if isinstance(providers, dict):
        entry = providers.get(provider_key)
        if isinstance(entry, dict):
            return entry
    return {}


def save_node_contract(provider_key: str, contract: dict) -> bool:
    """把实测节点契约写回清单；内容无变化时不做磁盘写入。"""
    path = manifest_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False
    except (OSError, ValueError):
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    entry = providers.get(provider_key)
    if not isinstance(entry, dict):
        return False
    old = entry.get("node_contract")
    if isinstance(old, dict) and old.get("node_class") == contract.get("node_class") and old.get("inputs") == contract.get("inputs"):
        return False
    entry["node_contract"] = contract
    entry["verified_at"] = db_now()
    global _MANIFEST_CACHE
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    _MANIFEST_CACHE = data
    return True


def discover_node_class(
    object_info_all: dict,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    require_image_input: bool = True,
) -> str | None:
    """在全部节点清单里按关键字发现候选 class name（不依赖硬编码节点名）。"""
    best: str | None = None
    best_score = 0
    for name, info in (object_info_all or {}).items():
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        low = name.lower()
        if any(token.lower() in low for token in exclude):
            continue
        score = sum(1 for token in include if token.lower() in low)
        if not score:
            continue
        if require_image_input and find_input_of_type(info, "IMAGE") is None:
            continue
        if score > best_score:
            best, best_score = name, score
    return best


def probe_runtime(python: str, timeout: float = 30.0) -> tuple[bool, str]:
    """检查 ComfyUI Python 中的 Metal 多模态运行时（llama-cpp-python）是否可用。

    只在配置了 ``COMFYUI_PYTHON`` 时执行；结果做 5 分钟缓存，避免频繁起子进程。
    """
    key = str(python or "")
    if not key:
        return True, "未配置 ComfyUI Python，跳过运行时检查"
    cached = _RUNTIME_PROBE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < RUNTIME_PROBE_TTL:
        return cached[1], cached[2]
    code = "import importlib;m=importlib.import_module('llama_cpp');print(getattr(m,'__version__','ok'))"
    try:
        result = subprocess.run(
            [key, "-c", code], capture_output=True, timeout=timeout, text=True
        )
    except (OSError, subprocess.SubprocessError):
        outcome = (False, "无法执行 ComfyUI Python 解释器")
        _RUNTIME_PROBE_CACHE[key] = (time.monotonic(),) + outcome
        return outcome
    ok = result.returncode == 0
    detail = "" if ok else "llama-cpp-python 不可用，请安装 Metal 构建"
    _RUNTIME_PROBE_CACHE[key] = (time.monotonic(), ok, detail)
    return ok, detail


def reset_runtime_probe_cache() -> None:
    _RUNTIME_PROBE_CACHE.clear()


def reset_manifest_cache() -> None:
    global _MANIFEST_CACHE
    _MANIFEST_CACHE = None


# ---------------------------------------------------------------- 节点契约工具


def node_input_spec(info: dict | None) -> dict[str, Any]:
    """合并 required + optional 输入定义。"""
    if not isinstance(info, dict):
        return {}
    inputs = info.get("input")
    if not isinstance(inputs, dict):
        return {}
    spec: dict[str, Any] = {}
    for bucket in ("required", "optional"):
        section = inputs.get(bucket)
        if isinstance(section, dict):
            spec.update(section)
    return spec


def node_output_names(info: dict | None) -> list[str]:
    if not isinstance(info, dict):
        return []
    names = info.get("output_name")
    if isinstance(names, list):
        return [str(n) for n in names]
    outputs = info.get("output")
    return [str(n) for n in outputs] if isinstance(outputs, list) else []


def node_output_types(info: dict | None) -> list[str]:
    if not isinstance(info, dict):
        return []
    outputs = info.get("output")
    return [str(o) for o in outputs] if isinstance(outputs, list) else []


def enum_options(entry: Any) -> list[Any]:
    """取输入项的枚举候选；不是枚举返回空列表。"""
    if isinstance(entry, list) and entry and isinstance(entry[0], list):
        return list(entry[0])
    return []


def _match_key(spec: dict[str, Any], key: str) -> str | None:
    if key in spec:
        return key
    lowered = key.lower()
    for name in spec:
        if str(name).lower() == lowered:
            return name
    return None


def pick_enum(entry: Any, *preferred: str) -> Any | None:
    """从枚举候选中按偏好顺序挑选；都没命中返回第一个候选。"""
    options = enum_options(entry)
    if not options:
        return None
    texts = [str(o) for o in options]
    for want in preferred:
        if not want:
            continue
        for text in texts:
            if text == want:
                return options[texts.index(text)]
        low = str(want).lower()
        for index, text in enumerate(texts):
            if low == text.lower():
                return options[index]
        for index, text in enumerate(texts):
            if low in text.lower():
                return options[index]
    return options[0]


def find_option(entry: Any, *preferred: str) -> Any | None:
    """在枚举候选中严格匹配；未命中返回 ``None``。

    与 :func:`pick_enum` 的区别：**不回退到第一个候选**，
    这样“节点里没有我们期望的模型”才不会被误判为可用。
    """
    options = enum_options(entry)
    if not options:
        return None
    texts = [str(o) for o in options]
    for want in preferred:
        if not want:
            continue
        for index, text in enumerate(texts):
            if text.lower() == str(want).lower():
                return options[index]
    for want in preferred:
        if not want:
            continue
        low = str(want).lower()
        for index, text in enumerate(texts):
            if low in text.lower():
                return options[index]
    return None


def _coerce_scalar(type_name: str, value: Any) -> Any:
    try:
        if type_name in ("INT",):
            return int(value)
        if type_name in ("FLOAT",):
            return float(value)
        if type_name in ("BOOLEAN",):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
            return bool(value)
        if type_name in ("STRING",):
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return None
    return value


def build_inputs(info: dict | None, desired: dict[str, Any]) -> dict[str, Any]:
    """按实测节点契约构造输入。

    只写入节点真实存在的输入项；枚举值按偏好挑选；类型不匹配时做保守修复。
    **绝不伪造节点不存在的输入**（ComfyUI 会因此拒绝整个工作流）。
    """
    spec = node_input_spec(info)
    built: dict[str, Any] = {}
    for key, value in (desired or {}).items():
        target = _match_key(spec, key)
        if target is None:
            continue
        entry = spec[target]
        options = enum_options(entry)
        if options:
            # 允许传入偏好列表：按顺序匹配，命中第一个即止
            if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                preferences = [str(v) for v in value]
            elif isinstance(value, str):
                preferences = [value]
            else:
                preferences = []
            chosen = pick_enum(entry, *preferences)
            if chosen is None and isinstance(value, int) and not isinstance(value, bool):
                if 0 <= value < len(options):
                    chosen = options[value]
            if chosen is None:
                continue
            built[target] = chosen
            continue
        type_name = ""
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            type_name = entry[0].upper()
        if type_name in ("INT", "FLOAT", "BOOLEAN", "STRING"):
            coerced = _coerce_scalar(type_name, value)
            if coerced is None:
                continue
            built[target] = coerced
        else:
            # IMAGE / MASK / LATENT / 其它：原样传入（通常是上游连线 [node_id, idx]）
            built[target] = value
    return built


def find_input_of_type(info: dict | None, type_name: str) -> str | None:
    """找到指定类型的输入名（例如 ``IMAGE``）。"""
    spec = node_input_spec(info)
    wanted = type_name.upper()
    for name, entry in spec.items():
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            if entry[0].upper() == wanted:
                return name
    return None


def link(node_id: str | int, index: int = 0) -> list[Any]:
    """构造 ComfyUI 的节点连线引用。"""
    return [str(node_id), int(index)]


def graph(
    *nodes: tuple[str | int, str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """构造最小工作流图。每个元素为 ``(node_id, class_type, inputs)``。"""
    return {
        str(node_id): {"class_type": class_type, "inputs": dict(inputs)}
        for node_id, class_type, inputs in nodes
    }


# ---------------------------------------------------------------- 错误归类


def classify_error(exc: Exception) -> tuple[str, str, str]:
    """把异常归类为 ``(status, reason_code, message)``。"""
    if isinstance(exc, ComfyUIError):
        code = str(exc.code or "")
        status = _COMFY_STATUS_BY_CODE.get(code, STATUS_ERROR)
        return status, code or "workflow_failed", str(exc.message)
    if isinstance(exc, AIBARError):
        code = str(exc.code or "")
        status = _COMFY_STATUS_BY_CODE.get(code, STATUS_ERROR)
        return status, code or "internal_error", str(exc.message)
    return STATUS_ERROR, "internal_error", "视觉 Provider 执行失败"


def status_payload(
    status: str,
    reason_code: str = "",
    reason: str = "",
    *,
    model: str = "",
    latency_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一的状态字典。``available`` 只在 ready 时为 True。"""
    payload = {
        "status": status,
        "status_label": status_label(status),
        "available": status == STATUS_READY,
        "reason_code": reason_code,
        "reason": reason or reason_text(reason_code) if reason_code else "",
        "model": model,
        "latency_ms": int(latency_ms),
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------- 基类


class VisionProvider:
    """视觉 Provider 抽象基类。

    子类至少实现 ``probe()`` 与 ``analyze_image()``；``probe()`` 只允许做
    轻量探测（服务可达 / 节点存在 / 模型文件完整），**不得**启动真实图片分析或下载模型。
    """

    key: str = ""
    label: str = ""
    description: str = ""
    local: bool = True
    quality_tier: str = "basic"
    model: str = ""
    quantization: str = ""
    requires_consent: bool = False
    advanced_vlm: bool = False  # 是否占用全局高级 VLM 单任务队列
    capabilities: dict[str, Any] = {"precision": ["fast", "standard", "fine"]}
    precision_map: dict[str, dict[str, Any]] = {}

    # ---- 描述 ----

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "local": self.local,
            "quality_tier": self.quality_tier,
            "model": self.model,
            "quantization": self.quantization,
            "requires_consent": self.requires_consent,
            "advanced_vlm": self.advanced_vlm,
            "capabilities": dict(self.capabilities),
        }

    # ---- 精度 ----

    def map_precision(self, precision: str | None) -> dict[str, Any]:
        return dict(self.precision_map.get(normalize_precision(precision), {}))

    # ---- 健康探测（子类实现）----

    def probe(self) -> dict[str, Any]:
        return status_payload(STATUS_READY, model=self.model)

    # ---- 分层测试（子类可覆盖）----

    def test_layers(self) -> list[dict[str, Any]]:
        """分层测试：服务可达 → 模型或节点可用 → 最小视觉请求。

        前一层不通时后续层标记为 ``pending``，不谎报成独立失败；
        最小视觉请求会真实发送图片，基类**不自动发起**，由前端显式触发。
        """
        started = time.perf_counter()
        state = self.probe()
        latency = int((time.perf_counter() - started) * 1000)
        if not state.get("available"):
            return [_layer("service", state, latency), skipped_layer("model"), skipped_layer("inference")]
        return [
            _layer("service", state, latency),
            _layer("model", state, 0),
            skipped_layer("inference"),
        ]

    # ---- 分析（子类实现）----

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    # ---- 工具 ----

    def _timeout(self, options: dict[str, Any] | None) -> float:
        raw = (options or {}).get("timeout")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT_SECONDS
        return min(max(30.0, value), 1800.0)

    def _cancel_flag(self, options: dict[str, Any] | None):
        cancel = (options or {}).get("cancel")
        return cancel if callable(cancel) else None


def adapt_model_json(
    data: dict,
    *,
    evidence: str,
    warnings: list[str] | None = None,
    negative_confidence: float = 0.65,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """把模型输出 JSON 适配为 M9.4 的 sections。

    只允许：类型修复、枚举映射（维度）、长度裁剪。
    维度无法识别时改用确定性关键词归类并降级置信度，**绝不伪造视觉细节**。
    """
    if not isinstance(data, dict):
        raise AIBARError("bad_response", "模型返回的 JSON 不是对象")

    sections_raw = data.get("sections")
    if not isinstance(sections_raw, list):
        mapping = {
            k: v for k, v in data.items() if k not in ("negative", "negative_prompt", "warnings")
        }
        if mapping and all(isinstance(v, (str, list)) for v in mapping.values()):
            sections_raw = [{"dimension": k, "text": v} for k, v in mapping.items()]
        else:
            raise AIBARError("bad_response", "模型返回缺少 sections 字段")

    collected: list[str] = list(warnings) if warnings else []
    sections: list[dict[str, Any]] = []

    for item in sections_raw:
        if isinstance(item, dict):
            text_value = (
                item.get("text")
                if item.get("text") is not None
                else item.get("描述", item.get("content", item.get("prompt")))
            )
            dimension_value = item.get("dimension", item.get("维度", item.get("category")))
            confidence_value = item.get("confidence", item.get("置信度", 0.7))
            uncertainty_value = item.get("uncertainty", item.get("不确定性", "")) or ""
            subcategory_value = item.get("subcategory", item.get("子类", "")) or ""
        elif isinstance(item, str):
            text_value, dimension_value, confidence_value = item, None, 0.7
            uncertainty_value, subcategory_value = "", ""
        else:
            continue

        text = schema.clean_text(text_value, schema.MAX_SECTION_TEXT).strip()
        if not text:
            continue

        dimension = schema.coerce_dimension(dimension_value)
        confidence = _unit_float(confidence_value, 0.7)
        uncertainty = schema.clean_text(uncertainty_value, schema.MAX_UNCERTAINTY)
        if dimension is None:
            fallback_dim, fallback_conf = classify_dimension(text, schema.MEDIA_TYPE)
            dimension = fallback_dim
            confidence = min(confidence, float(fallback_conf))
            uncertainty = uncertainty or "模型给出的维度无法识别，已按关键词归类"

        sections.append(
            schema.make_section(
                len(sections),
                dimension,
                text,
                schema.cap_confidence(confidence, schema.SOURCE_VISION),
                subcategory=str(subcategory_value or ""),
                uncertainty=uncertainty,
                evidence=evidence,
            )
        )
        if len(sections) >= schema.MAX_SECTIONS:
            break

    negative_value = data.get("negative_prompt", data.get("negative", ""))
    if isinstance(negative_value, list):
        negative_text = "，".join(str(x) for x in negative_value)
    else:
        negative_text = str(negative_value or "")
    for piece in split_segments(negative_text):
        if not has_content_value(piece):
            continue
        sections.append(
            schema.make_section(
                len(sections),
                "negative",
                piece,
                schema.cap_confidence(negative_confidence, schema.SOURCE_VISION),
                uncertainty="由模型给出的排除项",
                evidence=evidence,
            )
        )

    raw_warnings = data.get("warnings")
    if isinstance(raw_warnings, list):
        for item in raw_warnings:
            text = schema.clean_text(item, schema.MAX_WARNING_TEXT)
            if text and text not in collected:
                collected.append(text)
    return sections, negative_text, collected


def _unit_float(value: Any, default: float) -> float:
    """类型修复 + 范围裁剪（宽容适配阶段允许裁剪，严格校验阶段才拒绝）。"""
    if isinstance(value, bool):
        return default
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    if number != number:
        return default
    return max(0.0, min(1.0, number))


def skipped_layer(name: str) -> dict[str, Any]:
    """分层测试中“因前置层失败而未执行”的一层。"""
    return {"name": name, "status": "pending", "available": False, "latency_ms": 0, "reason_code": "not_run"}


def _layer(name: str, state: dict[str, Any], latency_ms: int) -> dict[str, Any]:
    """把状态字典转成分层测试的一层结果。"""
    return {
        "name": name,
        "status": state.get("status", STATUS_ERROR),
        "available": bool(state.get("available")),
        "latency_ms": int(latency_ms or state.get("latency_ms") or 0),
        "reason_code": state.get("reason_code", ""),
    }


def profile_or_default(profile: str | None) -> str:
    if profile in profile_keys():
        return profile  # type: ignore[return-value]
    return "generic"


__all__ = [
    "adapt_model_json",
    "DEFAULT_PRECISION",
    "DEFAULT_TIMEOUT_SECONDS",
    "PRECISION_FAST",
    "PRECISION_FINE",
    "PRECISION_STANDARD",
    "PRECISION_LABELS",
    "PRECISIONS",
    "STATUSES",
    "STATUS_ERROR",
    "STATUS_INCOMPATIBLE_RUNTIME",
    "STATUS_MISSING_MMPROJ",
    "STATUS_MISSING_MODEL",
    "STATUS_MISSING_NODE",
    "STATUS_OFFLINE",
    "STATUS_OUT_OF_MEMORY",
    "STATUS_READY",
    "STATUS_UNAUTHORIZED",
    "STATUS_UNCONFIGURED",
    "STATUS_LABELS",
    "REASON_LABELS",
    "VLM_MAX_PARALLEL",
    "VisionProvider",
    "acquire_vlm_slot",
    "build_inputs",
    "classify_error",
    "comfyui_client",
    "discover_node_class",
    "enum_options",
    "find_input_of_type",
    "find_option",
    "graph",
    "link",
    "load_manifest",
    "manifest_entry",
    "manifest_path",
    "new_client_id",
    "node_input_spec",
    "node_output_names",
    "node_output_types",
    "normalize_precision",
    "pick_enum",
    "probe_runtime",
    "profile_or_default",
    "reason_text",
    "release_vlm_slot",
    "reset_manifest_cache",
    "reset_runtime_probe_cache",
    "reset_vlm_queue",
    "save_node_contract",
    "skipped_layer",
    "status_label",
    "status_payload",
    "vlm_queue_state",
    "vlm_slot",
]
