"""M12 · 漫画工作室模块装配与队列 worker 单例。

依赖方向（HARNESS）：``comic`` 是高于 ``sync``/``promptlib`` 的业务包，
编排出图时复用 ``sync.workflow_convert``（UI→API 转换）与
``reverse.comfyui_client``（ComfyUI HTTP）这两个既有能力，不另起一套。
"""

from __future__ import annotations

from .runner import ComicWorker

_worker: ComicWorker | None = None


def get_worker() -> ComicWorker:
    """返回进程内唯一的出图队列 worker（懒创建）。"""
    global _worker
    if _worker is None:
        _worker = ComicWorker()
    return _worker


def start_worker() -> bool:
    """在后台拉起出图队列 worker；失败只记告警，不阻断启动。"""
    try:
        get_worker().start()
        return True
    except Exception:
        return False
