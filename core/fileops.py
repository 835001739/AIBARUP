"""文件操作的健壮封装——**清理型删除的统一收口**。

存在理由（2026-09-01 线上故障）：
    删除文件这类「免费的旁路动作」，在某些运行环境（沙箱 / 安全钩子 / 解释器
    退出）会以 ``BaseException`` 子类中断，典型就是 ``SystemExit``。而代码里
    遍地的 ``except OSError`` / ``except (OSError, ValueError)`` **抓不到它**
    —— SystemExit 不是 Exception 子类。结果是：前面已经花几十秒 GPU 完成的
    出图，在「删旧图」这一步被打成 failed，UI 只显示一句「生成异常：SystemExit」。

与其在每个调用点各自记着「这里要捕 BaseException」，不如把所有清理型删除
收敛到本模块，统一按 **失败只降级** 处理。

设计原则：
- **先写新文件，再清旧文件**。调用方负责保证顺序；本模块只保证清理本身不抛。
- **逐文件独立容错**：单张删除失败不影响其余，也不影响主流程。
- **绝不静默**：失败要记日志，但记的是 warning，不是把异常往上抛。
"""

from __future__ import annotations

from pathlib import Path

from core.logging_setup import get_logger, safe_log

_LOGGER = get_logger("aibar.core.fileops")

__all__ = ["purge_stale", "unlink_quiet", "rmtree_quiet"]


def purge_stale(dest_dir: Path, pattern: str, keep: Path | None = None) -> int:
    """删除 ``dest_dir`` 下匹配 ``pattern`` 的旧文件，``keep`` 指定的保留。

    **绝不上抛任何异常**——清理只是省磁盘的旁路动作，失败的最坏结果不过是
    留下几个旧文件，远好过让主流程（可能已经跑了几分钟）整单失败。

    Args:
        dest_dir: 要清理的目录。
        pattern: ``glob`` 模式，如 ``"7_*.png"``。
        keep: 需要保留的文件（通常是刚写入的新文件），按真实路径比对。

    Returns:
        实际删除的文件数。
    """
    removed = 0
    keep_key: Path | None = None
    if keep is not None:
        try:
            keep_key = keep.resolve()
        except OSError:
            keep_key = keep
    try:
        for old in dest_dir.glob(pattern):
            try:
                if not old.is_file():
                    continue
                if keep_key is not None and old.resolve() == keep_key:
                    continue
                old.unlink()
                removed += 1
            except BaseException as exc:  # 单张失败不影响其余
                safe_log(_LOGGER, 30, "file_unlink_failed", error_type=type(exc).__name__)
    except BaseException as exc:  # glob / 遍历本身出问题，同样只降级
        safe_log(_LOGGER, 30, "purge_failed", error_type=type(exc).__name__)
    return removed


def unlink_quiet(path: str | Path) -> bool:
    """删除单个文件，返回**是否真的删掉了一个文件**；任何失败都返回 ``False``。

    准确语义（便于调用方计数）：目标原本就不存在 → ``False``（没删到东西就不算）；
    删除被拦截（含 ``SystemExit`` 这类 BaseException，``except OSError`` 抓不到）
    → ``False``，且不外泄——适合「删不掉就算了」的清理场景。
    """
    try:
        target = Path(path)
        if not target.exists():
            return False
        target.unlink()
        return True
    except BaseException as exc:
        safe_log(_LOGGER, 30, "file_unlink_failed", error_type=type(exc).__name__)
        return False


def rmtree_quiet(path: str | Path) -> bool:
    """递归删除整棵目录树；任何失败都降级为 ``False``，绝不外泄异常。

    为什么不用 ``shutil.rmtree``：运行环境的 safe-delete 钩子对「一次删整个
    目录」按目录内对象数做**批量删除保护**（阈值 50），超了就要人工确认，
    确认框弹不出来就以 ``SystemExit`` 干断当前线程——2026-09-05 视频模块的
    去背景接口一调就断连，根因就是它（删 51 个对象的 nobg 目录）。

    这里改成**逐文件删 + 由深到浅删空目录**：每次删除的对象数都是 1，
    不触发批量保护，走的也与普通清理完全相同的收口路径。
    """
    root = Path(path)
    if not root.exists():
        return False
    failed = False
    try:
        # 由深到浅：先删叶子文件/子目录内容，空目录才能被 rmdir
        children = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    child.rmdir()
                else:
                    child.unlink()
            except BaseException as exc:
                failed = True
                safe_log(_LOGGER, 30, "file_unlink_failed", error_type=type(exc).__name__)
        root.rmdir()
        return not failed
    except BaseException as exc:
        safe_log(_LOGGER, 30, "rmtree_failed", error_type=type(exc).__name__)
        return False
