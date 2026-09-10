"""图片元数据与校验工具（M3 / M9 共用）。

- 读取 PNG 内嵌文本块（ComfyUI 写入的 ``prompt`` / ``workflow``）；
- 读取基础图片信息（宽高、格式、字节数）；
- 校验上传图片的**真实内容**，不只相信扩展名与 MIME。

隐私约束：本模块只返回相对路径与元数据，不向外暴露本机绝对路径。
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .logging_setup import get_logger
from .textutil import sanitize_model_text

_LOGGER = get_logger("aibar.imagemeta")

# ComfyUI / A1111 常见的 PNG 文本键
_PROMPT_KEYS = ("prompt", "parameters", "Comment", "Description")
_WORKFLOW_KEYS = ("workflow",)


#: 单次向内核要多少字节。64KB 对顺序读足够大，又不会为找几个文本块
#: 把 20~50MB 的原图整个读进内存。
_CHUNK_READ_SIZE = 65536

#: 单个 PNG 数据块的长度上限。规范允许到 2^31-1，但真实的文本块远小于此；
#: 设上限是为了让损坏/恶意声明的长度直接判废，而不是让流式读取去分配几 GB。
_MAX_CHUNK_LENGTH = 64 * 1024 * 1024


#: 我们真正想要的数据块类型
_TEXT_CHUNK_TYPES = frozenset((b"tEXt", b"iTXt", b"zTXt"))


def _read_exactly(stream: Any, count: int) -> bytes | None:
    """从流里读满 ``count`` 字节；不足（文件被截断）或超上限时返回 ``None``。"""
    if count > _MAX_CHUNK_LENGTH:
        return None
    pieces: list[bytes] = []
    remaining = count
    while remaining > 0:
        block = stream.read(min(_CHUNK_READ_SIZE, remaining))
        if not block:
            return None
        pieces.append(block)
        remaining -= len(block)
    return b"".join(pieces)


def _skip_bytes(stream: Any, count: int) -> bool:
    """跳过 ``count`` 字节，**不把它们读进内存**。

    可定位的流直接挪文件指针（不产生任何 I/O），否则按块读完丢弃。
    返回 False 表示长度异常或文件被截断。
    """
    if count > _MAX_CHUNK_LENGTH:
        return False
    try:
        if stream.seekable():
            stream.seek(count, 1)
            return True
    except (OSError, ValueError):
        pass
    remaining = count
    while remaining > 0:
        block = stream.read(min(_CHUNK_READ_SIZE, remaining))
        if not block:
            return False
        remaining -= len(block)
    return True


def _iter_png_chunks(stream: Any, wanted: frozenset[bytes] = _TEXT_CHUNK_TYPES):
    """按 PNG 规范顺序产出 ``(块类型, 数据体)``，读到 ``IEND`` 或文件末尾停止。

    **只缓冲 ``wanted`` 里的块**，其余块（绝大多数是几十 MB 的图块 IDAT）直接
    跳过，连内存都不进 —— 否则一张图里最大的那一块就足以让"流式读取"白做。
    ``IEND`` 总是被产出（数据体为空），调用方据此收工。
    """
    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
        return
    while True:
        header = stream.read(8)
        if len(header) < 8:
            return
        (length,) = struct.unpack(">I", header[:4])
        ctype = header[4:8]
        keep = ctype in wanted or ctype == b"IEND"
        if keep:
            body = _read_exactly(stream, length)
            if body is None:
                return
        elif not _skip_bytes(stream, length):
            return
        if len(stream.read(4)) < 4:  # CRC
            return
        if keep:
            yield ctype, body
        if ctype == b"IEND":
            return


def read_png_text_chunks(path: str | Path) -> dict[str, str]:
    """解析 PNG 的 tEXt/iTXt/zTXt 文本块，返回键值字典。

    不依赖 Pillow 的 info（不同版本行为不一致），直接按规范解析，失败时静默返回空。

    **顺序流式读取，内存有界**：只为找几个文本块就把整张图
    ``read_bytes()`` 读进内存是浪费 —— 图库里的 ComfyUI 出图实测最大 24.5MB、
    合计 5.4GB，而它们的 tEXt 块紧跟 IHDR（抽样 60 张，全部落在文件前 1.85%）。
    这里按 :data:`_CHUNK_READ_SIZE` 顺序读、命中 ``IEND`` 即停，通常读完第一块
    就够了。**不依赖"文本块在前"这个经验**：它只影响提前收工，不影响结果——
    真遇到文本块在后的图，会一直读到它，与整读等价。
    """
    chunks: dict[str, str] = {}
    try:
        with open(path, "rb") as stream:
            for ctype, body in _iter_png_chunks(stream):
                if ctype == b"IEND":
                    break
                try:
                    if ctype == b"zTXt":
                        sep = body.index(b"\x00")
                        key = body[:sep].decode("utf-8", "ignore")
                        value = zlib.decompress(body[sep + 2 :]).decode("utf-8", "ignore")
                    else:
                        sep = body.index(b"\x00")
                        key = body[:sep].decode("utf-8", "ignore")
                        value = body[sep + 1 :].decode("utf-8", "ignore")
                    if key:
                        chunks[key] = value
                except (ValueError, zlib.error, UnicodeDecodeError):
                    continue
    except (OSError, struct.error, IndexError):
        return chunks
    return chunks


def _comfy_prompt_to_text(raw: str) -> tuple[str, str]:
    """把 ComfyUI 的 workflow API JSON 转成正向/负向提示词文本。"""
    positive, negative = "", ""
    try:
        graph = json.loads(raw)
    except (ValueError, TypeError):
        return positive, negative
    nodes: dict[str, Any]
    if isinstance(graph, dict) and "prompt" in graph and isinstance(graph["prompt"], dict):
        nodes = graph["prompt"]
    elif isinstance(graph, dict):
        nodes = graph
    else:
        return positive, negative
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs") or {}
        text = inputs.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if "CLIPTextEncode" not in class_type:
            continue
        if inputs.get("t5xxl") is not None:
            continue
        # 负向提示词通常由 KSampler 的 negative 输入或节点标题判定
        title = str(node.get("_meta", {}).get("title", "")).lower()
        if "negative" in title or "负面" in title:
            negative = text.strip()
        elif not positive:
            positive = text.strip()
    return positive, negative


def extract_prompt_metadata(path: str | Path) -> dict[str, Any]:
    """从图片中提取提示词与工作流元数据。

    返回 ``{"has_metadata", "positive", "negative", "params", "workflow_json"}``。
    """
    result: dict[str, Any] = {
        "has_metadata": False,
        "positive": "",
        "negative": "",
        "params": "",
        "workflow_json": "",
    }
    p = Path(path)
    if not p.exists():
        return result
    chunks: dict[str, str] = {}
    try:
        with Image.open(p) as img:
            raw_info = img.info or {}
            for key in _PROMPT_KEYS + _WORKFLOW_KEYS:
                value = raw_info.get(key)
                if isinstance(value, str) and value:
                    chunks[key] = value
                elif isinstance(value, bytes):
                    chunks[key] = value.decode("utf-8", "ignore")
    except (UnidentifiedImageError, OSError):
        pass
    if p.suffix.lower() == ".png":
        for key, value in read_png_text_chunks(p).items():
            chunks.setdefault(key, value)

    for key in _PROMPT_KEYS:
        raw = chunks.get(key)
        if raw and raw.strip().startswith("{"):
            pos, neg = _comfy_prompt_to_text(raw)
            if pos or neg:
                result["positive"] = sanitize_model_text(pos)
                result["negative"] = sanitize_model_text(neg)
                result["workflow_json"] = raw
                result["has_metadata"] = True
                break
    if not result["has_metadata"]:
        for key in _PROMPT_KEYS:
            raw = chunks.get(key)
            if raw and raw.strip():
                text = sanitize_model_text(raw)
                result["params"] = text
                head = text.split("\n")[0]
                neg_marker = "Negative prompt:"
                if neg_marker in text:
                    positive_part, _, tail = text.partition(neg_marker)
                    result["positive"] = positive_part.strip()
                    result["negative"] = tail.split("\n")[0].strip()
                else:
                    result["positive"] = head.strip()
                result["has_metadata"] = True
                break
    for key in _WORKFLOW_KEYS:
        if chunks.get(key):
            result["workflow_json"] = result["workflow_json"] or chunks[key]
            result["has_metadata"] = True
    return result


def image_basic_info(path: str | Path) -> dict[str, Any]:
    """读取图片基础信息；无法识别时返回空字典。"""
    p = Path(path)
    try:
        with Image.open(p) as img:
            return {
                "width": img.width,
                "height": img.height,
                "format": (img.format or "").upper(),
                "size_bytes": p.stat().st_size,
            }
    except (UnidentifiedImageError, OSError, ValueError):
        return {}


def is_animated(path: str | Path) -> bool:
    """判断是否为动画图片（GIF/WebP 多帧），动画图不接受反推。"""
    try:
        with Image.open(path) as img:
            return getattr(img, "n_frames", 1) > 1
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def validate_image_file(path: str | Path, allowed_ext: tuple[str, ...], max_bytes: int) -> tuple[bool, str]:
    """校验上传图片的**真实内容**。

    返回 ``(是否通过, 错误码)``。错误码取值：``empty_file`` / ``file_too_large`` /
    ``invalid_image`` / ``animated_image`` / ``bad_extension``。
    """
    p = Path(path)
    try:
        # 一次 stat 拿全部信息：此前 exists() + stat() + stat() 连打三次系统调用
        stat_result = p.stat()
    except OSError:
        return False, "empty_file"
    if stat_result.st_size == 0:
        return False, "empty_file"
    if stat_result.st_size > max_bytes:
        return False, "file_too_large"
    if p.suffix.lower() not in allowed_ext:
        return False, "bad_extension"
    try:
        with Image.open(p) as img:
            img.verify()
    except Exception as exc:
        # 这里是**判官**而不是业务代码：Pillow 抛任何异常都只说明"这不是一张
        # 能用的图"，正是本函数要判定的结论，不是故障。
        # 实测漏网的：PNG 缺 IDAT 时 Pillow 抛 IndexError（不在常见类型里），
        # 此前会穿透到 uploads 变成 500。
        _LOGGER.info("image_verify_rejected error_type=%s", type(exc).__name__)
        return False, "invalid_image"
    if is_animated(p):
        return False, "animated_image"
    return True, ""


__all__ = [
    "read_png_text_chunks",
    "extract_prompt_metadata",
    "image_basic_info",
    "is_animated",
    "validate_image_file",
]
