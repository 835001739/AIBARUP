"""M9.2 上传校验、受控缓存与过期清理。

设计要点（PRD M9.2 / M9.7 / M9.9）：
- 只相信**真实内容**：用 ``core.imagemeta.validate_image_file`` 校验，不只看扩展名与 MIME；
- 上传副本用**内容哈希命名**存入 ``Config.UPLOAD_DIR``，对外只暴露
  受控相对引用 ``uploads/<hash>.png``，**绝不保存或返回用户原始绝对路径**；
- 图片来源支持两种：``image_id``（图库记录，解析到 ``Config.STATIC_DIR`` 下的相对副本）
  与 ``upload_id``（受控缓存）；
- 清理：默认 24h 过期；**仍被未删除反推历史引用的文件必须保留**；清理失败只记告警。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from config import Config
from core.db import query_all
from core.errors import AIBARError
from core.imagemeta import extract_prompt_metadata, image_basic_info, validate_image_file
from core.logging_setup import get_logger

_LOGGER = get_logger("aibar.reverse.uploads")

CHUNK_SIZE = 65536
UPLOAD_REF_PREFIX = "uploads"
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,5}$")
_TMP_PREFIX = ".tmp_"

ERROR_STATUS = {
    "empty_file": 400,
    "file_too_large": 413,
    "invalid_image": 415,
    "animated_image": 415,
    "bad_extension": 415,
}

ERROR_MESSAGES = {
    "empty_file": "上传文件为空，请重新选择图片",
    "file_too_large": "图片体积超过上限，请压缩后重试",
    "invalid_image": "文件不是有效的图片或已损坏",
    "animated_image": "不支持动画图片，请使用静态图片",
    "bad_extension": "不支持的图片格式，仅支持 png / jpg / jpeg / webp",
    "image_not_found": "图库中找不到该图片，请重新选择",
    "image_unreadable": "该图片的本地副本不可读，请重新同步图库或直接上传图片",
    "upload_not_found": "上传缓存已过期，请重新上传图片",
}

#: 只有这些异常表示"这张图本身有问题"，可以放心归成 415。
#: Pillow 的 ``UnidentifiedImageError`` 继承自 ``OSError``，截断/损坏也多表现为
#: ``OSError`` / ``ValueError``，所以这一组覆盖了常见的解码失败。
#:
#: 其余异常（``AttributeError`` / ``TypeError`` / 数据库错误…）是**程序缺陷**，
#: 必须按 500 报出去并留下类型线索。此前统一降级成 415，
#: 结果就是"磁盘写满"和"代码有 bug"都被说成"文件不是有效的图片"。
_EXPECTED_IMAGE_ERRORS = (OSError, ValueError, EOFError)


def raise_upload_error(code: str) -> None:
    """按错误码抛出统一业务异常。"""
    raise AIBARError(code, ERROR_MESSAGES.get(code, "图片校验失败"), ERROR_STATUS.get(code, 400))


# ---------------------------------------------------------------- 内部工具


def _ensure_upload_dir() -> Path:
    directory = Path(Config.UPLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_suffix(filename: str) -> str:
    """只保留安全后缀；不合法后缀统一替换为 ``.bin``，后续按 bad_extension 拒绝。"""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix and _SAFE_SUFFIX_RE.match(suffix):
        return suffix
    return ".bin"


def _display_name(filename: str) -> str:
    """展示名：只取 basename，去掉任何路径信息。"""
    name = Path(str(filename or "")).name.strip()
    return (name or "")[:180]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_id_of(path: Path) -> str:
    return path.stem


def upload_ref_of(upload_id: str, suffix: str = ".png") -> str:
    """受控相对引用（写库用），例如 ``uploads/<hash>.png``。"""
    return f"{UPLOAD_REF_PREFIX}/{upload_id}{suffix}"


def _ref_to_upload_id(ref: str) -> str:
    return Path(str(ref or "")).stem


# ---------------------------------------------------------------- 上传落盘


def save_upload(stream: Any, filename: str = "") -> dict[str, Any]:
    """保存并校验一张上传图片，返回受控信息（不含绝对路径）。

    Args:
        stream: 类文件对象（``werkzeug.FileStorage`` 或已打开的二进制流）。
        filename: 原始文件名，只用于取后缀与展示名。

    Raises:
        AIBARError: 空文件 / 超限 / 坏扩展名 / 损坏 / 动画图。
    """
    max_bytes = max(1, int(Config.REVERSE_UPLOAD_MAX_MB)) * 1024 * 1024
    directory = _ensure_upload_dir()
    suffix = _safe_suffix(filename)
    tmp_path = directory / f"{_TMP_PREFIX}{uuid.uuid4().hex}{suffix}"

    total = 0
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                if isinstance(chunk, str):  # 防御：文本流不接受
                    chunk = chunk.encode("utf-8")
                total += len(chunk)
                if total > max_bytes:
                    raise AIBARError(
                        "file_too_large",
                        f"{ERROR_MESSAGES['file_too_large']}（上限 {Config.REVERSE_UPLOAD_MAX_MB}MB）",
                        ERROR_STATUS["file_too_large"],
                    )
                fh.write(chunk)
    except AIBARError:
        _unlink(tmp_path)
        raise
    except Exception as exc:
        # 这一阶段还没看过图片内容，写失败只可能是磁盘/权限等环境问题，
        # 归成"图片损坏"会骗人，也掩盖真实故障。
        _unlink(tmp_path)
        _LOGGER.error("reverse_upload_write_failed error_type=%s", type(exc).__name__)
        raise AIBARError("internal_error", "上传写入失败，请稍后重试", 500) from exc

    # 我们**有责任清理**的那一份：起初是临时文件，os.replace 后变成 dest。
    # 只认 tmp_path 会在 replace 之后漏删 dest（孤儿文件永久残留）；
    # 命中去重时 dest 是别人已上传的同一份内容，失败也不能删 —— 置 None。
    owned: Path | None = tmp_path
    try:
        ok, code = validate_image_file(
            tmp_path, tuple(Config.REVERSE_ALLOWED_EXT), max_bytes
        )
        if not ok:
            raise_upload_error(code or "invalid_image")

        content_hash = _file_sha256(tmp_path)
        dest = directory / f"{content_hash}{suffix}"
        if dest.exists():
            # 内容已存在：复用同一份，本次没写任何新文件
            _unlink(tmp_path)
            owned = None
        else:
            os.replace(tmp_path, dest)
            owned = dest

        info = image_basic_info(dest)
        if not info:
            raise_upload_error("invalid_image")

        has_metadata = bool(extract_prompt_metadata(dest).get("has_metadata"))
        payload = {
            "upload_id": content_hash,
            "preview_url": preview_url(content_hash),
            "filename": _display_name(filename) or dest.name,
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "size_bytes": int(info.get("size_bytes") or 0),
            "content_hash": content_hash,
            "has_metadata": has_metadata,
        }
        _LOGGER.info(
            "reverse_upload_saved upload_id=%s size_bytes=%s width=%s height=%s has_metadata=%s",
            content_hash,
            payload["size_bytes"],
            payload["width"],
            payload["height"],
            int(has_metadata),
        )
        return payload
    except AIBARError:
        _discard(owned)
        raise
    except _EXPECTED_IMAGE_ERRORS as exc:
        _discard(owned)
        # 不记日志：用户传坏图是常态，记了只会淹没真正的故障
        raise AIBARError("invalid_image", ERROR_MESSAGES["invalid_image"], 415) from exc
    except Exception as exc:
        _discard(owned)
        _LOGGER.error("reverse_upload_internal_error error_type=%s", type(exc).__name__)
        raise AIBARError("internal_error", "上传处理失败，请稍后重试", 500) from exc


def _unlink(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _discard(path: Path | None) -> None:
    """删除"本次上传自己写出来"的文件；``None`` 表示没有需要负责的文件。"""
    if path is not None:
        _unlink(path)


# ---------------------------------------------------------------- 查询


def preview_url(upload_id: str) -> str:
    """上传副本的受控预览地址。"""
    return f"/api/prompt-reverse/uploads/{upload_id}/preview"


def resolve_path(upload_id: str) -> Path | None:
    """解析 upload_id 到受控缓存文件；不存在返回 ``None``（内部使用，不外泄）。"""
    if not upload_id or not UPLOAD_ID_RE.match(str(upload_id)):
        return None
    directory = Path(Config.UPLOAD_DIR)
    for suffix in Config.REVERSE_ALLOWED_EXT:
        candidate = directory / f"{upload_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def info(upload_id: str) -> dict[str, Any] | None:
    """读取上传副本信息；不存在返回 ``None``。"""
    path = resolve_path(upload_id)
    if path is None:
        return None
    basic = image_basic_info(path)
    if not basic:
        return None
    return {
        "upload_id": upload_id,
        "preview_url": preview_url(upload_id),
        "filename": path.name,
        "width": int(basic.get("width") or 0),
        "height": int(basic.get("height") or 0),
        "size_bytes": int(basic.get("size_bytes") or 0),
        "content_hash": upload_id,
        "has_metadata": bool(extract_prompt_metadata(path).get("has_metadata")),
    }


def open_bytes(upload_id: str) -> bytes | None:
    """读取受控缓存的字节内容（供 Provider 使用）。"""
    path = resolve_path(upload_id)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------- 图库来源


def resolve_image(image_id: str) -> dict[str, Any]:
    """解析图库图片，返回受控信息（内部带绝对路径，仅供 Provider 使用）。

    Raises:
        AIBARError: ``image_not_found`` / ``image_unreadable``。
    """
    from core.db import query_one

    if not image_id:
        raise_upload_error("image_not_found")
    row = query_one(
        "SELECT id, filename, gallery_path, width, height, size_bytes FROM images WHERE id=?",
        (str(image_id),),
    )
    if row is None:
        raise_upload_error("image_not_found")

    relative = (row["gallery_path"] or "").strip().lstrip("/")
    if not relative:
        raise_upload_error("image_unreadable")

    static_root = Path(Config.STATIC_DIR)
    try:
        target = (static_root / relative).resolve()
        target.relative_to(static_root.resolve())  # 防止越界读取
    except (ValueError, OSError):
        raise_upload_error("image_unreadable")

    if not target.is_file() or not os.access(target, os.R_OK):
        raise_upload_error("image_unreadable")

    return {
        "image_id": str(row["id"]),
        "upload_id": None,
        "content_hash": str(row["id"]),
        "filename": str(row["filename"] or target.name),
        "width": int(row["width"] or 0),
        "height": int(row["height"] or 0),
        "size_bytes": int(row["size_bytes"] or 0),
        "has_metadata": bool(extract_prompt_metadata(target).get("has_metadata")),
        "path": target,
    }


def resolve_upload(upload_id: str) -> dict[str, Any]:
    """解析上传缓存，返回受控信息（内部带绝对路径）。"""
    payload = info(upload_id)
    if payload is None:
        raise_upload_error("upload_not_found")
    path = resolve_path(upload_id)
    if path is None:
        raise_upload_error("upload_not_found")
    return {
        "image_id": None,
        "upload_id": upload_id,
        "content_hash": upload_id,
        "filename": payload["filename"],
        "width": payload["width"],
        "height": payload["height"],
        "size_bytes": payload["size_bytes"],
        "has_metadata": payload["has_metadata"],
        "path": path,
    }


def resolve_ref(image_id: str | None = None, upload_id: str | None = None) -> dict[str, Any]:
    """统一解析图片来源，二选一。

    Returns:
        ``{image_id, upload_id, content_hash, filename, width, height, size_bytes,
        has_metadata, path}``；``path`` 仅供 Provider 内部读取，绝不对外返回或写库。
    """
    if image_id and upload_id:
        raise AIBARError("invalid_input", "image_id 与 upload_id 只能选择其一")
    if image_id:
        return resolve_image(image_id)
    if upload_id:
        return resolve_upload(upload_id)
    raise AIBARError("invalid_input", "需要提供 image_id 或 upload_id")


def public_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """去掉内部绝对路径，生成可安全返回前端与写库的引用。"""
    return {
        "image_id": ref.get("image_id"),
        "upload_id": ref.get("upload_id"),
        "content_hash": ref.get("content_hash") or "",
        "filename": ref.get("filename") or "",
        "width": int(ref.get("width") or 0),
        "height": int(ref.get("height") or 0),
        "size_bytes": int(ref.get("size_bytes") or 0),
        "has_metadata": bool(ref.get("has_metadata")),
    }


def upload_ref_for(ref: dict[str, Any]) -> str | None:
    """生成写入 ``prompt_reverse_jobs.upload_ref`` 的受控相对引用。"""
    if not ref.get("upload_id"):
        return None
    path = ref.get("path")
    suffix = Path(str(path)).suffix.lower() if path else ""
    if suffix not in Config.REVERSE_ALLOWED_EXT:
        suffix = ".png"
    return upload_ref_of(str(ref["upload_id"]), suffix)


# ---------------------------------------------------------------- 清理


def referenced_upload_ids() -> set[str]:
    """仍被未删除反推历史引用的 upload_id 集合。

    查询失败时**必须向外抛**，不能返回空集：清理流程拿它当"无人引用"的白名单，
    空集会让人误以为所有缓存都没人用，于是把还挂在历史记录上的图片一并删掉
    —— 那是静默的数据丢失，比清理失败严重得多。
    """
    rows = query_all(
        "SELECT upload_ref FROM prompt_reverse_jobs "
        "WHERE upload_ref IS NOT NULL AND upload_ref != ''"
    )
    return {_ref_to_upload_id(row["upload_ref"]) for row in rows if row["upload_ref"]}


def cleanup_expired(ttl_hours: int | None = None) -> dict[str, int]:
    """清理超过 TTL 的上传缓存；被历史引用的文件保留。

    任何单个文件失败只记告警，不影响整体清理与其他模块。
    """
    ttl = Config.REVERSE_UPLOAD_TTL_HOURS if ttl_hours is None else int(ttl_hours)
    cutoff = time.time() - max(0, ttl) * 3600
    return _sweep(cutoff=cutoff, keep=referenced_upload_ids())


def cleanup_unreferenced(extra_keep: set[str] | None = None) -> dict[str, int]:
    """删除没有任何反推历史引用的上传缓存（历史删除后调用）。

    仍被**未删除**历史引用的文件一律保留，避免删掉一条历史就清空别人的缓存。
    """
    keep = referenced_upload_ids()
    keep.update(extra_keep or set())
    return _sweep(cutoff=None, keep=keep)


def _sweep(cutoff: float | None, keep: set[str]) -> dict[str, int]:
    directory = Path(Config.UPLOAD_DIR)
    stats = {"scanned": 0, "expired": 0, "removed": 0, "kept_referenced": 0, "failed": 0}
    if not directory.is_dir():
        return stats
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        stats["failed"] += 1
        return stats

    for path in entries:
        if not path.is_file():
            continue
        stats["scanned"] += 1
        is_temp = path.name.startswith(_TMP_PREFIX)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            stats["failed"] += 1
            continue
        if cutoff is not None and mtime >= cutoff and not is_temp:
            continue
        stats["expired"] += 1
        if not is_temp and _upload_id_of(path) in keep:
            stats["kept_referenced"] += 1
            continue
        try:
            path.unlink()
            stats["removed"] += 1
        except OSError:
            stats["failed"] += 1
    if stats["failed"]:
        _LOGGER.warning(
            "reverse_upload_cleanup_partial removed=%s failed=%s",
            stats["removed"],
            stats["failed"],
        )
    else:
        _LOGGER.info("reverse_upload_cleanup removed=%s kept=%s", stats["removed"], stats["kept_referenced"])
    return stats


__all__ = [
    "ERROR_MESSAGES",
    "ERROR_STATUS",
    "cleanup_expired",
    "cleanup_unreferenced",
    "info",
    "open_bytes",
    "preview_url",
    "public_ref",
    "referenced_upload_ids",
    "resolve_image",
    "resolve_path",
    "resolve_ref",
    "resolve_upload",
    "save_upload",
    "upload_ref_for",
    "upload_ref_of",
    "raise_upload_error",
]
