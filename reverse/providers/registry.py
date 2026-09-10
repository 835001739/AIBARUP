"""M10.4 / M11.4 Provider 中心：注册、健康缓存、自动选择链、分层测试与默认偏好。

设计要点：
- ``list_providers()`` 只返回契约约定的字段，**不返回密钥与完整 endpoint**；
- 健康状态做**内存 TTL 缓存**（默认 60s）；``refresh()`` 必须绕过缓存；
  检测只做轻量探测，**绝不启动真实图片分析或下载模型**；
- ``auto_chain()`` 是**固定决策链**，只对「当前 Provider 不可用」降级；
  推理错误 / OOM 属于运行时失败，必须由上层告知用户并选择重试，
  **不得**连续自动切换并加载下一个大模型；
- ``set_default()`` 只把 ``provider_key`` 写入 ``prompt_provider_preferences``，不存运行状态。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core.db import execute, now as db_now, query_all, tx
from core.errors import AIBARError
from core.logging_setup import get_logger

from . import base
from .base import STATUS_ERROR, STATUS_READY, skipped_layer, status_payload
from .comfyui_blip import ComfyUIBlipProvider
from .joycaption import JoyCaptionProvider
from .metadata import MetadataProvider
from .openai_compatible import (
    consent_state,
    create_openai_compatible_providers,
    grant_consent,
    has_consent,
    revoke_consent,
)
from .qwen3vl import create_qwen3vl_providers

_LOGGER = get_logger("aibar.reverse.providers.registry")

# "auto" 作为默认偏好哨兵：没有显式默认 Provider 时启用自动链
AUTO_KEY = "auto"

# 健康状态缓存 TTL（秒）。测试可通过 monkeypatch 调整该常量。
HEALTH_TTL_SECONDS = 60.0

# 列表展示顺序：内置元数据 → 高级 VLM → 基础视觉 → 本地端点 → 外部服务
DEFAULT_ORDER = (
    "metadata",
    "comfyui_qwen3vl_8b",
    "comfyui_qwen3vl_4b",
    "comfyui_joycaption_beta",
    "comfyui_blip",
    "local_vision",
    "openai_compatible_vision",
)

# 自动决策链中除「内嵌元数据」与「用户默认」之外的固定顺序（PRD M11.4）
AUTO_CHAIN_FIXED = (
    "comfyui_qwen3vl_8b",
    "comfyui_qwen3vl_4b",
    "comfyui_joycaption_beta",
    "comfyui_blip",
)

# 分层测试的固定三层（PRD M10.4）
LAYER_NAMES = ("service", "model", "inference")

_registry: dict[str, base.VisionProvider] = {}
_registry_lock = threading.RLock()
_health_cache: dict[str, dict[str, Any]] = {}
_health_lock = threading.RLock()


# ---------------------------------------------------------------- 注册


def _default_providers() -> list[base.VisionProvider]:
    providers: list[base.VisionProvider] = [MetadataProvider()]
    providers.extend(create_qwen3vl_providers())
    providers.append(JoyCaptionProvider())
    providers.append(ComfyUIBlipProvider())
    providers.extend(create_openai_compatible_providers())
    return providers


def _ensure_registry() -> dict[str, base.VisionProvider]:
    """惰性构造默认注册表；测试可通过 ``register_provider`` 覆盖。"""
    with _registry_lock:
        if not _registry:
            for provider in _default_providers():
                _registry[provider.key] = provider
        return _registry


def register_provider(provider: base.VisionProvider) -> None:
    """注册（或覆盖）一个 Provider，并使其健康缓存失效。"""
    with _registry_lock:
        _registry[provider.key] = provider
    with _health_lock:
        _health_cache.pop(provider.key, None)


def reset_registry() -> None:
    """清空注册表与缓存（仅测试与异常恢复使用）。"""
    with _registry_lock:
        _registry.clear()
    reset_health_cache()


def all_providers() -> list[base.VisionProvider]:
    """按展示顺序返回全部 Provider。"""
    registry = _ensure_registry()
    ordered = [registry[key] for key in DEFAULT_ORDER if key in registry]
    ordered.extend(
        provider for key, provider in sorted(registry.items()) if key not in DEFAULT_ORDER
    )
    return ordered


def provider_keys() -> list[str]:
    return [provider.key for provider in all_providers()]


def get_provider(key: str) -> base.VisionProvider:
    registry = _ensure_registry()
    provider = registry.get(str(key or "").strip())
    if provider is None:
        raise AIBARError("provider_not_found", "未知的视觉 Provider", 404)
    return provider


# ---------------------------------------------------------------- 健康探测与缓存


def reset_health_cache() -> None:
    with _health_lock:
        _health_cache.clear()


def _probe(provider: base.VisionProvider) -> dict[str, Any]:
    """执行一次健康探测；任何异常都归一化为状态字典，绝不向上抛出。"""
    started = time.perf_counter()
    try:
        payload = provider.probe()
    except AIBARError as exc:
        status, reason_code, message = base.classify_error(exc)
        payload = status_payload(status, reason_code, message, model=provider.model)
    except Exception:  # 探测失败不得影响 Provider 中心整体可用
        payload = status_payload(
            STATUS_ERROR, "internal_error", "Provider 检测失败", model=provider.model
        )
    if not isinstance(payload, dict):
        payload = status_payload(STATUS_ERROR, "internal_error", model=provider.model)

    payload.setdefault("status", STATUS_ERROR)
    payload["status_label"] = base.status_label(str(payload.get("status") or ""))
    payload["available"] = payload.get("status") == STATUS_READY
    payload["reason_code"] = str(payload.get("reason_code") or "")
    payload["reason"] = str(payload.get("reason") or "") or base.reason_text(
        payload["reason_code"]
    )
    payload["model"] = str(payload.get("model") or provider.model)
    payload["latency_ms"] = int(payload.get("latency_ms") or int((time.perf_counter() - started) * 1000))
    payload["last_checked_at"] = db_now()
    return payload


def provider_health(key: str, force: bool = False) -> dict[str, Any]:
    """读取 Provider 健康状态（默认走 TTL 缓存）。"""
    provider = get_provider(key)
    if not force:
        with _health_lock:
            cached = _health_cache.get(provider.key)
        if cached is not None and time.monotonic() - cached["at"] < HEALTH_TTL_SECONDS:
            return dict(cached["payload"])

    payload = _probe(provider)
    with _health_lock:
        _health_cache[provider.key] = {"payload": dict(payload), "at": time.monotonic()}
    _LOGGER.info(
        "reverse_provider_probe key=%s status=%s reason_code=%s latency_ms=%s",
        provider.key,
        payload["status"],
        payload["reason_code"],
        payload["latency_ms"],
    )
    return dict(payload)


# ---------------------------------------------------------------- 列表


def list_providers(force: bool = False) -> dict[str, Any]:
    """Provider 列表（不返回密钥与完整 endpoint）。"""
    default_key = default_provider_key()
    items: list[dict[str, Any]] = []
    for provider in all_providers():
        health = provider_health(provider.key, force=force)
        items.append(
            {
                "key": provider.key,
                "label": provider.label,
                "description": provider.description,
                "local": bool(provider.local),
                "quality_tier": provider.quality_tier,
                "status": health["status"],
                "status_label": health["status_label"],
                "available": bool(health["available"]),
                "reason_code": health["reason_code"],
                "reason": health["reason"],
                "model": health["model"],
                "quantization": provider.quantization,
                "requires_consent": bool(provider.requires_consent),
                "advanced_vlm": bool(provider.advanced_vlm),
                "is_default": default_key == provider.key,
                "capabilities": dict(provider.capabilities),
                "last_checked_at": health["last_checked_at"],
            }
        )
    return {
        "items": items,
        "default_provider": default_key,
        "auto_enabled": default_key == AUTO_KEY,
    }


def refresh() -> dict[str, Any]:
    """绕过缓存重新探测全部 Provider（检测不启动真实分析或下载模型）。"""
    reset_health_cache()
    return list_providers(force=True)


# ---------------------------------------------------------------- 分层测试


def _error_layers(exc: Exception) -> list[dict[str, Any]]:
    status, reason_code, _message = base.classify_error(exc)
    layers = [
        {
            "name": "service",
            "status": status,
            "available": False,
            "latency_ms": 0,
            "reason_code": reason_code,
        }
    ]
    layers.append(skipped_layer("model"))
    layers.append(skipped_layer("inference"))
    return layers


def test_provider(key: str) -> dict[str, Any]:
    """分层测试：服务可达 → 模型或节点可用 → 最小视觉请求成功。"""
    provider = get_provider(key)
    try:
        layers = provider.test_layers()
    except AIBARError as exc:
        layers = _error_layers(exc)
    except Exception:  # 测试接口不得抛出未捕获异常
        layers = _error_layers(Exception("internal_error"))

    normalized: list[dict[str, Any]] = []
    if isinstance(layers, (list, tuple)):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            status = str(layer.get("status") or STATUS_ERROR)
            reason_code = str(layer.get("reason_code") or "")
            normalized.append(
                {
                    "name": str(layer.get("name") or ""),
                    "status": status,
                    "status_label": base.status_label(status),
                    "available": bool(layer.get("available")),
                    "latency_ms": int(layer.get("latency_ms") or 0),
                    "reason_code": reason_code,
                    "reason": base.reason_text(reason_code) if reason_code else "",
                }
            )
    # 补齐三层，保证前端拿到固定结构
    present = {layer["name"] for layer in normalized}
    for name in LAYER_NAMES:
        if name not in present:
            normalized.append(skipped_layer(name))

    executed = [layer for layer in normalized if layer["status"] != "pending"]
    ok = bool(executed) and all(layer["available"] for layer in executed)
    return {
        "key": provider.key,
        "ok": ok,
        "layers": normalized,
        "last_checked_at": db_now(),
    }


# ---------------------------------------------------------------- 默认偏好


def default_provider_key() -> str:
    """当前默认 Provider；没有显式选择时返回 ``"auto"``。"""
    try:
        rows = query_all(
            "SELECT provider_key FROM prompt_provider_preferences WHERE is_default=1"
        )
    except Exception:
        return AUTO_KEY
    keys = {str(row["provider_key"] or "").strip() for row in rows}
    keys.discard("")
    if not keys:
        return AUTO_KEY
    # 多个残留行时按展示顺序取最靠前的一个，保证结果确定
    for key in DEFAULT_ORDER:
        if key in keys:
            return key
    return sorted(keys)[0]


def set_default(key: str) -> dict[str, Any]:
    """设置默认 Provider；``"auto"`` 表示恢复自动链。

    只写入 ``provider_key``，**不保存任何运行状态**。
    """
    wanted = str(key or "").strip() or AUTO_KEY
    if wanted != AUTO_KEY:
        get_provider(wanted)

    with tx():
        execute(
            "UPDATE prompt_provider_preferences SET is_default=0, updated_at=? WHERE is_default=1",
            (db_now(),),
        )
        execute(
            "INSERT INTO prompt_provider_preferences (provider_key, is_default, updated_at) "
            "VALUES (?, 1, ?) ON CONFLICT(provider_key) DO UPDATE SET "
            "is_default=excluded.is_default, updated_at=excluded.updated_at",
            (wanted, db_now()),
        )
    _LOGGER.info("reverse_provider_default key=%s", wanted)
    return {
        "default_provider": default_provider_key(),
        "auto_enabled": wanted == AUTO_KEY,
    }


# ---------------------------------------------------------------- 外发确认


def _fingerprint_of(provider: base.VisionProvider) -> str:
    getter = getattr(provider, "fingerprint", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def consent_status(key: str) -> dict[str, Any]:
    """外部 Provider 的外发确认状态（只含服务指纹，不含密钥与 endpoint）。"""
    provider = get_provider(key)
    fingerprint = _fingerprint_of(provider)
    if not provider.requires_consent:
        return {
            "key": provider.key,
            "requires_consent": False,
            "consented": True,
            "fingerprint": fingerprint,
            "consented_at": None,
        }
    state = consent_state(provider.key, fingerprint)
    return {
        "key": provider.key,
        "requires_consent": True,
        "consented": bool(state.get("consented")),
        "fingerprint": fingerprint,
        "consented_at": state.get("consented_at"),
    }


def set_consent(key: str, consented: bool) -> dict[str, Any]:
    """记录或撤销外发确认；授权状态会改变可用状态，因此同步失效健康缓存。"""
    provider = get_provider(key)
    if not provider.requires_consent:
        raise AIBARError("invalid_input", "该 Provider 不需要外发确认")
    fingerprint = _fingerprint_of(provider)
    if consented:
        grant_consent(provider.key, fingerprint)
    else:
        revoke_consent(provider.key)
    _LOGGER.info("reverse_provider_consent key=%s consented=%s", provider.key, int(bool(consented)))
    reset_health_cache()
    return consent_status(provider.key)


# ---------------------------------------------------------------- 自动决策链


def _is_authorized(provider: base.VisionProvider) -> bool:
    if not provider.requires_consent:
        return True
    return has_consent(provider.key, _fingerprint_of(provider))


def auto_chain() -> list[str]:
    """固定决策链（PRD M11.4）。

    顺序：内嵌元数据 → 用户默认 Provider → Qwen3-VL 8B → Qwen3-VL 4B
    → JoyCaption → BLIP → 已授权外部 Provider。

    调用方**只能**在「当前 Provider 不可用」时沿链向下取下一个；
    推理错误 / OOM 必须停下来告知用户。
    """
    registry = _ensure_registry()
    keys: list[str] = []

    def push(key: str) -> None:
        if key and key in registry and key not in keys:
            keys.append(key)

    push("metadata")
    push(default_provider_key())
    for key in AUTO_CHAIN_FIXED:
        push(key)
    for provider in all_providers():
        if provider.local:
            continue
        if _is_authorized(provider):
            push(provider.key)
    return keys


def chain_status(keys: list[str] | None = None) -> list[dict[str, Any]]:
    """返回决策链上每个 Provider 的当前状态，供上层判断是否降级。"""
    chain = list(keys) if keys else auto_chain()
    return [{"key": key, **provider_health(key)} for key in chain]


__all__ = [
    "AUTO_CHAIN_FIXED",
    "AUTO_KEY",
    "DEFAULT_ORDER",
    "HEALTH_TTL_SECONDS",
    "LAYER_NAMES",
    "all_providers",
    "auto_chain",
    "chain_status",
    "consent_status",
    "default_provider_key",
    "get_provider",
    "list_providers",
    "provider_health",
    "provider_keys",
    "refresh",
    "register_provider",
    "reset_health_cache",
    "reset_registry",
    "set_consent",
    "set_default",
    "test_provider",
]
