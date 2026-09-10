"""M4 · 同步引擎：后台轮询线程、首跑种子、手动触发与自动开关。

设计要点（PRD M4）：
- 轮询而非 inotify：跨平台、实现简单，间隔默认 10s；
- 首跑种子：首次只同步最近 ``SEED_LIMIT`` 张图 + 全部工作流，之后全量增量，
  种子状态写入 ``data/state.json``，避免每次重启都全量搬；
- 线程安全：``threading.Lock`` 保证同一时刻只有一个同步在执行；
- 任何一次同步的异常都被吞掉并记日志，后台线程绝不因单次失败退出。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from config import Config
from core import db
from core.logging_setup import get_logger, safe_log

from . import scanner

logger = get_logger("aibar.sync.watcher")

# 轮询最小间隔保护：配置被误写成 0 时不要让线程空转烧 CPU
MIN_INTERVAL = 1


class SyncWatcher:
    """后台同步调度器（进程内单例由 ``get_watcher()`` 提供）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._auto = bool(Config.SYNC_AUTO)
        self._last_sync: str | None = None
        self._last_result: dict[str, Any] = {}
        self._last_error = ""

    # ------------------------------------------------------------ 状态

    def is_running(self) -> bool:
        """后台线程是否存活。"""
        return self._thread is not None and self._thread.is_alive()

    def is_auto(self) -> bool:
        return self._auto

    def set_auto(self, enabled: bool) -> bool:
        """切换自动同步（线程在本轮 sleep 结束后立即生效）。"""
        self._auto = bool(enabled)
        safe_log(logger, logging.INFO, "sync_auto_changed", auto=self._auto)
        db.log_sync(f"自动同步已{'开启' if self._auto else '关闭'}", "info", "sync")
        return self._auto

    def last_sync(self) -> str | None:
        """最近一次同步结束时间；未同步过则回退到日志表最新时间。"""
        if self._last_sync:
            return self._last_sync
        try:
            return db.query_scalar("SELECT MAX(ts) FROM sync_log WHERE type = 'sync'")
        except Exception:
            return None

    def last_result(self) -> dict[str, Any]:
        return dict(self._last_result)

    def last_error(self) -> str:
        return self._last_error

    # ------------------------------------------------------------ 同步

    def sync_once(self, force_all: bool = False) -> dict[str, Any]:
        """立即同步一次（阻塞直到完成）。

        Args:
            force_all: 忽略 mtime 指纹并跳过种子限制，强制全量重扫。
        """
        with self._lock:
            started = time.perf_counter()
            # 首跑只做种子，避免第一次打开页面时卡在上千张图上
            seed_limit: int | None = None
            if not force_all and not scanner.is_seeded():
                seed_limit = max(0, int(Config.SEED_LIMIT))
                safe_log(logger, logging.INFO, "sync_seed_start", limit=seed_limit)
            try:
                result = scanner.sync_all(force_all=force_all, seed_limit=seed_limit)
                self._last_error = ""
            except Exception as exc:
                # 整体失败也不能让调用方 500：记录错误码并返回上一次结果
                self._last_error = type(exc).__name__
                safe_log(logger, logging.ERROR, "sync_failed", error_code=type(exc).__name__)
                db.log_sync(f"同步执行失败：{type(exc).__name__}", "error", "sync")
                result = {
                    "workflows": 0,
                    "images": 0,
                    "added_workflows": 0,
                    "added_images": 0,
                    "skipped": 0,
                    "failed": 0,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            if seed_limit is not None:
                scanner.mark_seeded()
            self._last_result = result
            self._last_sync = db.now()
            db.log_sync(
                "同步完成：工作流 {w} 条 / 图片 {i} 条 / 新增 {aw}+{ai} / 失败 {f}".format(
                    w=result["workflows"],
                    i=result["images"],
                    aw=result["added_workflows"],
                    ai=result["added_images"],
                    f=result["failed"],
                ),
                "info" if not result["failed"] else "warning",
                "sync",
            )
            return result

    # ------------------------------------------------------------ 线程

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._auto:
                    self.sync_once()
            except Exception as exc:
                # 兜底：任何漏网异常都不允许终止后台线程
                safe_log(logger, logging.ERROR, "sync_loop_error", error_code=type(exc).__name__)
                db.log_sync(f"同步线程异常：{type(exc).__name__}", "error", "sync")
            # 用 Event.wait 代替 sleep，stop() 可以立刻唤醒而不必等满一个周期
            if self._stop.wait(max(MIN_INTERVAL, int(Config.SYNC_INTERVAL))):
                break

    def start(self, run_now: bool = False) -> bool:
        """启动后台线程；已运行时直接返回 False。"""
        if self.is_running():
            return False
        self._stop.clear()
        if run_now:
            try:
                self.sync_once()
            except Exception as exc:
                # 启动时的首次同步失败不该拖住线程启动，但也绝不能悄悄吞掉：
                # 此前这里只有一个 pass，同步坏了界面上什么迹象都没有。
                safe_log(
                    logger, logging.ERROR, "sync_start_once_failed", error_code=type(exc).__name__
                )
        self._thread = threading.Thread(
            target=self._loop, name="aibar-sync", daemon=True
        )
        self._thread.start()
        safe_log(logger, logging.INFO, "sync_thread_started", interval=Config.SYNC_INTERVAL)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """停止后台线程（daemon 线程，join 超时也不会阻塞进程退出）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        safe_log(logger, logging.INFO, "sync_thread_stopped")


_watcher_lock = threading.Lock()
_watcher: SyncWatcher | None = None


def get_watcher() -> SyncWatcher:
    """模块级单例：路由与 app 装配共用同一个调度器。"""
    global _watcher
    if _watcher is None:
        with _watcher_lock:
            if _watcher is None:
                _watcher = SyncWatcher()
    return _watcher


__all__ = ["SyncWatcher", "get_watcher"]
