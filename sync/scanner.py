"""M2/M3 · 目录扫描、内容哈希去重与落库。

职责边界：本模块只做"扫目录 → 解析 → 去重 → 写库"，**不负责调度**（调度在 ``watcher.py``），
这样测试可以脱离后台线程直接验证落库结果。

关键策略：
- 工作流按 ``filename``（相对源目录）唯一，比较 ``mtime+size`` 跳过未变更文件；
- 图片按内容 SHA-256 作主键，重复内容不重复入库，也不重复复制；
- 扫描状态持久化到 ``data/state.json``，进程重启后仍能增量；
- 单文件失败只记日志计数，绝不中断整体循环（PRD M4）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from config import Config
from core import db
from core.imagemeta import image_basic_info
from core.logging_setup import get_logger, safe_log

from . import parser, paths

logger = get_logger("aibar.sync.scanner")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
WORKFLOW_EXT = ".json"
_HASH_CHUNK = 1 << 20
_STATE_FILE = "state.json"

# 默认空计数，避免在调用点重复拼字典
EMPTY_COUNTER = {"added": 0, "updated": 0, "skipped": 0, "failed": 0}


# ---------------------------------------------------------------- 扫描状态


def state_path() -> Path:
    """状态文件路径（``data/state.json``，随 Config.DATA_DIR 变化，便于测试隔离）。"""
    return Path(Config.DATA_DIR) / _STATE_FILE


def load_state() -> dict[str, Any]:
    """读取扫描状态；文件缺失或损坏时返回空状态，不抛异常。"""
    try:
        raw = state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("seeded", False)
            data.setdefault("workflows", {})
            data.setdefault("images", {})
            return data
    except (OSError, ValueError):
        pass
    return {"seeded": False, "workflows": {}, "images": {}}


def save_state(state: dict[str, Any]) -> None:
    """持久化扫描状态。写盘失败只记日志，不影响同步结果。"""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，避免中途崩溃留下半截 JSON
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        safe_log(logger, logging.WARNING, "state_save_failed", error_code=type(exc).__name__)


def is_seeded() -> bool:
    return bool(load_state().get("seeded"))


def mark_seeded() -> None:
    """标记首跑种子已完成（先读后写，避免覆盖并发写入的扫描指纹）。"""
    state = load_state()
    state["seeded"] = True
    save_state(state)


# ---------------------------------------------------------------- 基础工具


def sha256_file(path: str | Path, chunk: int = _HASH_CHUNK) -> str:
    """计算文件内容 SHA-256；读不到内容时返回空串由调用方判为失败。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_files(root: Path, exts: Iterable[str], recursive: bool = True) -> list[Path]:
    """列出目录下指定后缀的文件，排序保证每次扫描顺序稳定。"""
    wanted = {e.lower() for e in exts}
    found: list[Path] = []
    try:
        walker = root.rglob("*") if recursive else root.glob("*")
        for path in walker:
            try:
                if path.is_file() and path.suffix.lower() in wanted:
                    found.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(found, key=lambda p: str(p))


def _mtime_size(path: Path) -> list[float] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return [round(stat.st_mtime, 3), stat.st_size]


def _fmt_mtime(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _gallery_rel(digest: str, ext: str) -> str:
    """图库副本的相对 static 路径，例如 ``gallery/ab/abc...png``。"""
    return f"gallery/{digest[:2]}/{digest}{ext}"


def _copy_to_gallery(src: Path, digest: str) -> str:
    """复制图片到 static/gallery，返回相对 static 的路径。失败时抛 OSError 由调用方处理。"""
    ext = src.suffix.lower()
    rel = _gallery_rel(digest, ext)
    dst = Path(Config.GALLERY_DIR) / digest[:2] / f"{digest}{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return rel


def _count(table: str) -> int:
    """统计某表条数；查不动时返回 0，但**必须记一笔**（否则表坏了也看不出来）。

    这里选择不向外抛：统计只用于同步前后的数量对比，失败一次不该中断整轮同步。
    """
    try:
        return int(db.query_scalar(f"SELECT COUNT(*) FROM {table}", default=0) or 0)
    except Exception as exc:
        safe_log(logger, logging.WARNING, "scan_count_failed",
                 table=table, error_code=type(exc).__name__)
        return 0


# ---------------------------------------------------------------- M2 工作流


def _upsert_workflow(
    filename: str,
    source: Path,
    stat_sig: list[float],
    parsed: dict[str, Any],
) -> bool:
    """写入或更新一条工作流，返回 True 表示新增（用于计数）。"""
    row = db.query_one("SELECT id FROM workflows WHERE filename = ?", (filename,))
    node_types = db.dumps(parsed.get("node_types") or [])
    if row is None:
        db.execute(
            "INSERT INTO workflows (name, filename, source_path, node_count, node_types,"
            " positive_prompt, negative_prompt, synced_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                Path(filename).stem,
                filename,
                str(source),
                int(parsed.get("node_count") or 0),
                node_types,
                parsed.get("positive_prompt") or "",
                parsed.get("negative_prompt") or "",
                db.now(),
                db.now(),
            ),
        )
        return True
    db.execute(
        "UPDATE workflows SET name = ?, source_path = ?, node_count = ?, node_types = ?,"
        " positive_prompt = ?, negative_prompt = ?, updated_at = ? WHERE filename = ?",
        (
            Path(filename).stem,
            str(source),
            int(parsed.get("node_count") or 0),
            node_types,
            parsed.get("positive_prompt") or "",
            parsed.get("negative_prompt") or "",
            db.now(),
            filename,
        ),
    )
    return False


def scan_workflows(force_all: bool = False) -> dict[str, int]:
    """扫描工作流目录并落库。

    Returns:
        ``{"added", "updated", "skipped", "failed"}``。
    """
    counter = dict(EMPTY_COUNTER)
    root = paths.workflows_dir()
    if root is None:
        return counter

    state = load_state()
    marks: dict[str, Any] = state.setdefault("workflows", {})
    changed = False

    for path in iter_files(root, (WORKFLOW_EXT,)):
        rel = path.relative_to(root).as_posix()
        try:
            sig = _mtime_size(path)
            if sig is None:
                counter["failed"] += 1
                continue
            # 增量：mtime 与 size 都没变就跳过解析，避免每次轮询重读上千个 JSON
            if not force_all and marks.get(rel) == sig:
                counter["skipped"] += 1
                continue
            parsed = parser.parse_workflow_text(path.read_text(encoding="utf-8", errors="ignore"))
            if _upsert_workflow(rel, path, sig, parsed):
                counter["added"] += 1
            else:
                counter["updated"] += 1
            marks[rel] = sig
            changed = True
        except Exception as exc:
            # 单文件失败被隔离：记录错误码，不中断整个目录的扫描
            counter["failed"] += 1
            safe_log(logger, logging.WARNING, "workflow_parse_failed", error_code=type(exc).__name__)
            db.log_sync(f"工作流解析失败：{rel}", "error", "sync")

    if changed:
        save_state(state)
    return counter


# ---------------------------------------------------------------- M3 图库


def _insert_image(
    path: Path,
    digest: str,
    gallery_rel: str,
    info: dict[str, Any],
    meta: dict[str, Any],
    created_at: str,
) -> None:
    db.execute(
        "INSERT INTO images (id, filename, source_path, gallery_path, width, height,"
        " size_bytes, prompt, workflow_link, created_at, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            digest,
            path.name,
            str(path),
            gallery_rel,
            info.get("width"),
            info.get("height"),
            info.get("size_bytes"),
            meta.get("prompt") or "",
            meta.get("workflow_link") or "",
            created_at,
            db.now(),
        ),
    )


def scan_images(limit: int | None = None, force_all: bool = False) -> dict[str, int]:
    """扫描产出目录并落库。

    Args:
        limit: 只处理最近 ``limit`` 张（按 mtime 倒序），用于首跑种子；None 表示全量。
        force_all: 忽略 mtime 指纹，重新哈希全部文件。

    Returns:
        ``{"added", "updated", "skipped", "failed"}``。
    """
    counter = dict(EMPTY_COUNTER)
    root = paths.output_dir()
    if root is None:
        return counter

    files = iter_files(root, IMAGE_EXTS)
    if not files:
        return counter

    # 首跑种子按 mtime 倒序取最近 N 张，保证页面"立刻有图"
    if limit is not None and limit >= 0:
        files.sort(key=_mtime_or_zero, reverse=True)
        files = files[:limit]

    state = load_state()
    marks: dict[str, Any] = state.setdefault("images", {})
    changed = False

    for path in files:
        key = str(path)
        try:
            sig = _mtime_size(path)
            if sig is None:
                counter["failed"] += 1
                continue
            if not force_all and marks.get(key) == sig:
                counter["skipped"] += 1
                continue
            digest = sha256_file(path)
            if not digest:
                counter["failed"] += 1
                continue
            # 内容去重：哈希命中库里已有记录时只需登记指纹，不重复复制
            if db.query_one("SELECT 1 FROM images WHERE id = ?", (digest,)) is not None:
                marks[key] = sig
                changed = True
                counter["skipped"] += 1
                continue
            info = image_basic_info(path)
            if not info:
                counter["failed"] += 1
                db.log_sync(f"图片无法解析：{path.name}", "error", "sync")
                continue
            meta = parser.parse_image_metadata(path)
            gallery_rel = _copy_to_gallery(path, digest)
            created_at = _fmt_mtime(sig[0])
            _insert_image(path, digest, gallery_rel, info, meta, created_at)
            marks[key] = sig
            changed = True
            counter["added"] += 1
        except Exception as exc:
            counter["failed"] += 1
            safe_log(logger, logging.WARNING, "image_scan_failed", error_code=type(exc).__name__)
            db.log_sync(f"图片同步失败：{path.name}", "error", "sync")

    if changed:
        save_state(state)
    return counter


def _mtime_or_zero(path: Path) -> float:
    """排序用的 mtime；读取失败退化为 0，避免整个排序因单文件失败而中断。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def register_image(
    path: str | Path,
    prompt: str = "",
    workflow_link: str = "",
) -> str | None:
    """把一张**产出目录之外**的图登记进图库，返回 ``image_id``（内容 sha256）。

    ``scan_images`` 只扫 ComfyUI 产出目录，因此漫画分镜这类「自己写盘」的产出
    永远不会进入图库，进而使「以图为输入」的两条链路（提示词反推、ComfyUI 深链）
    对它完全不可用。本函数把这套登记逻辑开放出来，供其余产出方复用：
    同样的内容哈希、同样的 ``static/gallery`` 副本、同样的 ``images`` 行。

    Args:
        path: 图片的绝对路径。
        prompt: 该图对应的正向提示词（写入 ``images.prompt``）。
        workflow_link: 关联工作流（写入 ``images.workflow_link``）。

    Returns:
        成功返回 image_id；图已存在时直接返回既有 id（幂等）；
        解析失败返回 ``None`` —— 图库登记只是旁路，失败不应中断主流程。
    """
    p = Path(path)
    digest = sha256_file(p)
    if not digest:
        return None
    if db.query_one("SELECT 1 FROM images WHERE id = ?", (digest,)) is not None:
        return digest
    info = image_basic_info(p)
    if not info:
        return None
    gallery_rel = _copy_to_gallery(p, digest)
    _insert_image(
        p,
        digest,
        gallery_rel,
        info,
        {"prompt": prompt, "workflow_link": workflow_link},
        _fmt_mtime(_mtime_or_zero(p)),
    )
    return digest


# ---------------------------------------------------------------- 组合入口


def sync_all(force_all: bool = False, seed_limit: int | None = None) -> dict[str, Any]:
    """执行一次完整同步（工作流全量增量 + 图库增量/种子）。

    Returns:
        ``{"workflows", "images", "added_workflows", "added_images",
        "skipped", "failed", "duration_ms"}``。
    """
    started = time.perf_counter()
    wf = scan_workflows(force_all=force_all)
    img = scan_images(limit=seed_limit, force_all=force_all)
    duration_ms = int((time.perf_counter() - started) * 1000)

    result = {
        "workflows": _count("workflows"),
        "images": _count("images"),
        "added_workflows": wf["added"],
        "added_images": img["added"],
        "skipped": wf["skipped"] + img["skipped"],
        "failed": wf["failed"] + img["failed"],
        "duration_ms": duration_ms,
    }
    safe_log(
        logger,
        logging.INFO,
        "sync_done",
        workflows=result["workflows"],
        images=result["images"],
        added_workflows=result["added_workflows"],
        added_images=result["added_images"],
        failed=result["failed"],
        duration_ms=duration_ms,
    )
    return result


__all__ = [
    "IMAGE_EXTS",
    "load_state",
    "save_state",
    "is_seeded",
    "mark_seeded",
    "sha256_file",
    "iter_files",
    "scan_workflows",
    "scan_images",
    "register_image",
    "sync_all",
]
