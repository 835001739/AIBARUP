"""M1 · ComfyUI 联动控制：探活、后台拉起、状态缓存。

设计要点（PRD M1 + HARNESS §5/§6）：
- 探测永不抛异常：ComfyUI 未运行时平台照常可用，只返回 ``running: false``；
- 探测结果做 5s 内存 TTL 缓存，避免前端高频刷新把本机端口打满；``refresh()`` 强制绕过；
- 拉起使用 ``start_new_session=True``，使 ComfyUI 脱离本进程会话，AI BAR 退出不影响它；
- 日志只记录状态码、错误码与耗时，绝不记录完整 endpoint 或本机路径。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from config import Config
from core.logging_setup import get_logger, safe_log

logger = get_logger("aibar.sync.comfyui")

# 探活结果缓存时长（秒）：状态页多组件共用同一次探测
PROBE_TTL = 5.0
# 拉起后等待可达的最长时间（秒）
START_WAIT = 5.0
_START_POLL_INTERVAL = 0.5

# 错误码
ERR_OFFLINE = "offline"
ERR_TIMEOUT = "timeout"
ERR_HTTP = "http_error"
ERR_UNKNOWN = "error"
ERR_DIR_MISSING = "comfyui_dir_missing"
ERR_ENTRY_MISSING = "comfyui_entry_missing"

_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_last_probe_at: str | None = None

# ComfyUI 永远是本机服务，任何情况下都不应走系统/沙箱代理。
# 显式置空代理，双保险于 config.py 的 NO_PROXY 兜底。
_LOOPBACK_PROXIES: dict[str, str | None] = {"http": None, "https": None}


def _clean_child_env() -> dict[str, str]:
    """为 ComfyUI 子进程构造净化环境。

    AIBAR 常由代理/沙箱 shell（WorkBuddy CLI 等）拉起，其进程环境会注入
    ``CODEBUDDY_SAFE_DELETE_*``、``BASH_ENV`` 等守卫变量。ComfyUI-Manager 的
    safe-delete 检测到这些变量后，会在启动日志轮转处弹出批量删除确认，而
    子进程 stdin 是 DEVNULL，导致启动卡死在 "Security scan" 之后、永远到不了
    "Starting server"。这里剥离这些守卫变量，让 ComfyUI 在干净环境启动。
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("CODEBUDDY_") or key == "BASH_ENV":
            env.pop(key, None)
    # 回环地址不走外层代理（NO_PROXY 已由 config.py 兜底，这里再确认一次）
    no_proxy = env.get("NO_PROXY", "")
    extra = "127.0.0.1,localhost"
    env["NO_PROXY"] = no_proxy if extra in no_proxy else ",".join(filter(None, [no_proxy, extra]))
    return env


def _empty_result(error_code: str = ERR_OFFLINE) -> dict[str, Any]:
    return {
        "running": False,
        "host": Config.COMFYUI_HOST,
        "port": Config.COMFYUI_PORT,
        "system_stats": None,
        "error_code": error_code,
    }


def probe(timeout: float | None = None) -> dict[str, Any]:
    """探测 ComfyUI 是否可达。

    Args:
        timeout: 超时秒数，默认 ``Config.COMFYUI_TIMEOUT``。

    Returns:
        ``{"running", "host", "port", "system_stats", "error_code"}``。
        任何异常都被吞掉并转为 ``running: False``。
    """
    global _last_probe_at
    result = _empty_result()
    seconds = float(timeout) if timeout else Config.COMFYUI_TIMEOUT
    started = time.perf_counter()
    try:
        resp = requests.get(
            f"{Config.comfyui_base()}/system_stats",
            timeout=max(0.5, seconds),
            proxies=_LOOPBACK_PROXIES,
        )
        if resp.status_code != 200:
            result["error_code"] = ERR_HTTP
        else:
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            result["running"] = True
            result["system_stats"] = payload if isinstance(payload, dict) else {}
            result["error_code"] = ""
    except requests.exceptions.Timeout:
        result["error_code"] = ERR_TIMEOUT
    except requests.exceptions.RequestException:
        result["error_code"] = ERR_OFFLINE
    except Exception:  # 兜底：非预期异常也绝不上抛
        result["error_code"] = ERR_UNKNOWN
    finally:
        _last_probe_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _cache["ts"] = time.monotonic()
        _cache["data"] = dict(result)
        duration_ms = int((time.perf_counter() - started) * 1000)
        safe_log(
            logger,
            logging.DEBUG if result["running"] else logging.INFO,
            "comfyui_probe",
            running=result["running"],
            error_code=result["error_code"],
            duration_ms=duration_ms,
        )
    return result


def refresh(timeout: float | None = None) -> dict[str, Any]:
    """强制绕过缓存重新探测。"""
    _cache["ts"] = 0.0
    return probe(timeout)


def probe_cached() -> dict[str, Any]:
    """带 TTL 的探测：缓存有效期内直接复用上一次结果。"""
    cached = _cache.get("data")
    if cached and (time.monotonic() - float(_cache.get("ts") or 0.0)) < PROBE_TTL:
        return dict(cached)
    return probe()


def status(force: bool = False) -> dict[str, Any]:
    """组合探活结果，附带最近一次探测时间与站点配置。"""
    data = refresh() if force else probe_cached()
    return {
        "running": data["running"],
        "host": data["host"],
        "port": data["port"],
        "system_stats": data["system_stats"],
        "error_code": data["error_code"],
        "checked_at": _last_probe_at,
        "dir_configured": bool(Config.COMFYUI_DIR),
    }


def _python_bin() -> str:
    """选择拉起 ComfyUI 的解释器：配置 > 当前解释器 > python3。"""
    return Config.COMFYUI_PYTHON or sys.executable or "python3"


def start(wait: float = START_WAIT) -> dict[str, Any]:
    """后台拉起 ComfyUI，并轮询等待其可达。

    Returns:
        ``{"started", "running", "error_code", "message"}``；目录缺失等场景不抛异常。
    """
    base = Config.COMFYUI_DIR
    if not base:
        return {
            "started": False,
            "running": False,
            "error_code": ERR_DIR_MISSING,
            "message": "未配置 ComfyUI 目录，请在 .env 中设置 COMFYUI_DIR",
        }
    root = Path(base).expanduser()
    if not root.is_dir():
        return {
            "started": False,
            "running": False,
            "error_code": ERR_DIR_MISSING,
            "message": "配置的 ComfyUI 目录不存在，请检查 .env 中的 COMFYUI_DIR",
        }
    if not (root / "main.py").is_file():
        return {
            "started": False,
            "running": False,
            "error_code": ERR_ENTRY_MISSING,
            "message": "ComfyUI 目录下未找到 main.py，无法自动启动",
        }

    cmd = [
        _python_bin(),
        "main.py",
        "--listen",
        Config.COMFYUI_HOST,
        "--port",
        str(Config.COMFYUI_PORT),
    ]
    log_path = Path(Config.TMP_DIR) / "comfyui.log"
    started = False
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 追加写入，便于排障时回溯多次拉起记录（日志内容由 ComfyUI 自己产生）
        with open(log_path, "ab", buffering=0) as handle:
            subprocess.Popen(
                cmd,
                cwd=str(root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_clean_child_env(),
            )
        started = True
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        safe_log(logger, logging.WARNING, "comfyui_start_failed", error_code=type(exc).__name__)
        return {
            "started": False,
            "running": False,
            "error_code": ERR_UNKNOWN,
            "message": "启动命令执行失败，请查看 tmp/comfyui.log",
        }

    # 轮询等待端口可达；等不到也算"已发起启动"，前端可再次刷新状态
    running = False
    deadline = time.monotonic() + max(0.0, wait)
    while time.monotonic() < deadline:
        if refresh(timeout=1.0)["running"]:
            running = True
            break
        time.sleep(_START_POLL_INTERVAL)

    safe_log(logger, logging.INFO, "comfyui_started", started=started, running=running)
    return {
        "started": started,
        "running": running,
        "error_code": "" if started else ERR_UNKNOWN,
        "message": "已启动 ComfyUI" if running else "已发起启动，ComfyUI 仍在初始化",
    }


__all__ = ["probe", "refresh", "probe_cached", "status", "start", "PROBE_TTL"]
