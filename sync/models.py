"""M8 导航「模型」页：ComfyUI 模型目录扫描。

只做只读扫描：结果入 5 分钟内存缓存，目录不存在时返回空列表而不是报错，
保证 ComfyUI 未安装或未配置时模型页依然可用。
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from core.logging_setup import get_logger, safe_log

from . import paths

logger = get_logger("aibar.sync.models")

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
CACHE_TTL = 300.0
# 直接放在模型根目录下的文件没有子目录可归类，用统一占位值避免空字符串
ROOT_TYPE = "root"

_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def _item_id(rel: str) -> str:
    """用相对路径生成稳定短 ID：路径即身份，重名文件不会互相覆盖。"""
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _scan() -> dict[str, Any]:
    root = paths.models_dir()
    items: list[dict[str, Any]] = []
    if root is None:
        return {"items": [], "total": 0}

    try:
        files = sorted(
            (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MODEL_EXTS),
            key=lambda p: str(p).lower(),
        )
    except OSError as exc:
        safe_log(logger, logging.WARNING, "models_scan_failed", error_code=type(exc).__name__)
        return {"items": [], "total": 0}

    for path in files:
        try:
            rel = path.relative_to(root).as_posix()
            # 模型类型取所在子目录名（checkpoints / loras / controlnet ...）
            parts = rel.split("/")
            kind = parts[0] if len(parts) > 1 else ROOT_TYPE
            items.append(
                {
                    "id": _item_id(rel),
                    "name": path.name,
                    "type": kind,
                    "path": rel,
                    "size_bytes": path.stat().st_size,
                    "ext": path.suffix.lower().lstrip("."),
                }
            )
        except (OSError, ValueError):
            # 单个文件 stat 失败（权限/断链）只跳过该文件
            continue

    items.sort(key=lambda item: (item["type"], item["name"].lower()))
    safe_log(logger, logging.INFO, "models_scanned", total=len(items))
    return {"items": items, "total": len(items)}


def scan_models(refresh: bool = False) -> dict[str, Any]:
    """扫描模型目录。

    Args:
        refresh: True 时绕过 5 分钟缓存重新扫描。

    Returns:
        ``{"items": [{"id","name","type","path","size_bytes","ext"}], "total": n}``。
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if not refresh and cached and (now - float(_cache.get("ts") or 0.0)) < CACHE_TTL:
        return cached
    data = _scan()
    _cache["ts"] = now
    _cache["data"] = data
    return data


def invalidate_cache() -> None:
    """清空缓存（配置变更或模型目录刚被写入时调用）。"""
    _cache["ts"] = 0.0
    _cache["data"] = None


__all__ = ["MODEL_EXTS", "scan_models", "invalidate_cache"]
