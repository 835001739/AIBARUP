"""P1-8 / P1-13：PNG 文本块解析与图片校验。

背景（实测数据）：图库 2697 张 PNG，最大 24.5MB、合计 5.4GB。
``read_png_text_chunks`` 原本为了找几个 KB 的文本块，把整张图 ``read_bytes()``
读进内存 —— 一张图峰值 24MB，同步扫描时再乘上并发。

改成流式分块 + 跳过图块后：同一张图峰值 **8.6KB**、实际读入 **5.6KB**。

本文件守三件事：
1. **等价性**：与"整读"的旧实现逐字节比对，含文本块在 IDAT 之后等边角情形；
2. **有界性**：真的没把文件读完（用计数流精确测量读入字节数）；
3. **健壮性**：截断、超长声明、非 PNG 都静默返回，不抛异常。
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from config import Config
from core.imagemeta import (
    _MAX_CHUNK_LENGTH,
    _iter_png_chunks,
    _read_exactly,
    _skip_bytes,
    is_animated,
    read_png_text_chunks,
    validate_image_file,
)

# ---------------------------------------------------------------- PNG 构造


def _chunk(ctype: bytes, body: bytes) -> bytes:
    """按 PNG 规范封一个数据块（长度 + 类型 + 数据 + CRC）。"""
    return (
        struct.pack(">I", len(body))
        + ctype
        + body
        + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    )


def _ihdr() -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))


def _text(key: str, value: str) -> bytes:
    return _chunk(b"tEXt", key.encode("utf-8") + b"\x00" + value.encode("utf-8"))


def _ztext(key: str, value: str) -> bytes:
    return _chunk(b"zTXt", key.encode("utf-8") + b"\x00\x00" + zlib.compress(value.encode("utf-8")))


def _itext(key: str, value: str) -> bytes:
    # iTXt：键 \0 压缩标志 \0 压缩方法 \0 语言 \0 翻译键 \0 正文
    return _chunk(b"iTXt", key.encode("utf-8") + b"\x00\x00\x00\x00\x00" + value.encode("utf-8"))


def _png(*chunks: bytes) -> bytes:
    """拼一个合法 PNG：签名 + IHDR + 给定块 + IEND。"""
    return b"\x89PNG\r\n\x1a\n" + _ihdr() + b"".join(chunks) + _chunk(b"IEND", b"")


def _write(tmp_path: Path, data: bytes, name: str = "sample.png") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


class _CountingReader:
    """只统计**真正读出来的字节数**的流包装（seek 不计入）。"""

    def __init__(self, raw: io.BytesIO):
        self._raw = raw
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        block = self._raw.read(size)
        self.bytes_read += len(block)
        return block

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()


# ---------------------------------------------------------------- 基线：修复前的整读实现


def _legacy_read(path: Path) -> dict[str, str]:
    """``read_png_text_chunks`` 的修复前版本，整文件 ``read_bytes()``。"""
    chunks: dict[str, str] = {}
    try:
        data = Path(path).read_bytes()
    except OSError:
        return chunks
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return chunks
    pos = 8
    size = len(data)
    try:
        while pos + 8 <= size:
            (length,) = struct.unpack(">I", data[pos : pos + 4])
            ctype = data[pos + 4 : pos + 8]
            body = data[pos + 8 : pos + 8 + length]
            pos += 12 + length
            if ctype in (b"tEXt", b"iTXt", b"zTXt"):
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
            elif ctype == b"IEND":
                break
    except (struct.error, IndexError):
        pass
    return chunks


# ---------------------------------------------------------------- 功能正确性


def test_reads_text_before_idat(tmp_path: Path):
    path = _write(tmp_path, _png(_text("parameters", "a girl, rainy street")))
    assert read_png_text_chunks(path) == {"parameters": "a girl, rainy street"}


def test_reads_text_after_idat(tmp_path: Path):
    """文本块在图块之后也要能被找到 —— 不能依赖"文本块总在前面"这个经验。"""
    payload = _png(
        _chunk(b"IDAT", b"\x00" * 4096),
        _text("parameters", "trailing metadata"),
    )
    path = _write(tmp_path, payload)

    assert read_png_text_chunks(path) == {"parameters": "trailing metadata"}
    assert read_png_text_chunks(path) == _legacy_read(path)


def test_reads_zlib_compressed_chunk(tmp_path: Path):
    path = _write(tmp_path, _png(_ztext("workflow", '{"nodes": []}')))
    assert read_png_text_chunks(path) == {"workflow": '{"nodes": []}'}


def test_reads_itxt_chunk_like_legacy(tmp_path: Path):
    """iTXt 目前按 tEXt 的方式处理：取第一个 ``\\0`` 之后的全部字节。

    严格按规范，iTXt 还应在其后剥掉「压缩标志 / 压缩方法 / 语言 / 翻译键」
    三到四个字段。这是个已知的简化，**本次不改动它** —— 这里只锁定
    "与整读实现完全一致"，避免流式改造顺手改了语义。
    """
    path = _write(tmp_path, _png(_itext("Comment", "国际文本")))
    chunks = read_png_text_chunks(path)

    assert chunks == _legacy_read(path)
    assert "国际文本" in chunks["Comment"]


def test_reads_multiple_chunks_in_order(tmp_path: Path):
    path = _write(
        tmp_path,
        _png(_text("parameters", "first"), _text("Description", "second"), _ztext("workflow", "{}")),
    )
    chunks = read_png_text_chunks(path)
    assert chunks == {"parameters": "first", "Description": "second", "workflow": "{}"}


def test_ignores_text_chunk_without_separator(tmp_path: Path):
    """没有 \\0 分隔符的坏块跳过，不影响其他块。"""
    path = _write(tmp_path, _png(_chunk(b"tEXt", b"no-separator"), _text("parameters", "ok")))
    assert read_png_text_chunks(path) == {"parameters": "ok"}


def test_non_png_returns_empty(tmp_path: Path):
    path = _write(tmp_path, b"not an image at all")
    assert read_png_text_chunks(path) == {}


def test_empty_file_returns_empty(tmp_path: Path):
    path = _write(tmp_path, b"")
    assert read_png_text_chunks(path) == {}


def test_missing_file_returns_empty(tmp_path: Path):
    assert read_png_text_chunks(tmp_path / "nope.png") == {}


def test_truncated_chunk_header_returns_partial(tmp_path: Path):
    """块头被截断：已读到的块保留，不抛异常。"""
    path = _write(tmp_path, _png(_text("parameters", "kept")) + b"\x00\x00")
    assert read_png_text_chunks(path) == {"parameters": "kept"}


def test_truncated_chunk_body_returns_partial(tmp_path: Path):
    """声明 1000 字节却只有 10 字节：读到多少算多少。"""
    payload = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 1000) + b"tEXt" + b"key\x00value"
    path = _write(tmp_path, payload)
    assert read_png_text_chunks(path) == {}


def test_oversized_declared_length_does_not_allocate(tmp_path: Path):
    """声明一个 4GB 的块：不能真的去分配 4GB。"""
    huge = struct.pack(">I", 0xFFFFFFF0)
    path = _write(tmp_path, b"\x89PNG\r\n\x1a\n" + huge + b"IDAT" + b"\x00" * 16)

    assert read_png_text_chunks(path) == {}
    assert _read_exactly(io.BytesIO(b"x" * 16), _MAX_CHUNK_LENGTH + 1) is None
    assert _skip_bytes(io.BytesIO(b"x" * 16), _MAX_CHUNK_LENGTH + 1) is False


# ---------------------------------------------------------------- 有界性：真的没把文件读完


def test_does_not_read_whole_file_when_text_is_early(tmp_path: Path):
    """反证核心：文本块在前时，不该为了它去读后面几十 MB 的图块。"""
    big_idat = _chunk(b"IDAT", b"\x00" * (2 * 1024 * 1024))  # 2MB 图块
    data = _png(_text("parameters", "early"), big_idat)
    path = _write(tmp_path, data)

    assert read_png_text_chunks(path) == {"parameters": "early"}

    reader = _CountingReader(io.BytesIO(data))
    for _ in _iter_png_chunks(reader):
        pass
    assert reader.bytes_read < len(data) // 10, (
        f"读了 {reader.bytes_read} 字节 / 共 {len(data)} 字节 —— 图块没被跳过"
    )


def test_skipped_chunks_are_not_buffered(tmp_path: Path):
    """跳过的块连内存都不进（seek 而不是 read）。"""
    data = _png(_chunk(b"IDAT", b"\x00" * (1024 * 1024)), _text("parameters", "late"))
    reader = _CountingReader(io.BytesIO(data))
    chunks = list(_iter_png_chunks(reader))

    assert dict(chunks).get(b"tEXt") is not None or chunks
    # 1MB 图块若被整块读进内存，读入量会超过 1MB
    assert reader.bytes_read < 1024 * 1024, f"图块被读进内存了：{reader.bytes_read} 字节"


def test_streaming_matches_legacy_on_realistic_files(tmp_path: Path):
    """用 Pillow 生成带真实元数据的图，与旧整读实现逐张比对。"""
    for index, (color, parameters) in enumerate(
        (((255, 0, 0), "红色主体，硬光"), ((0, 0, 255), "蓝色主体，柔光"), ((0, 255, 0), None))
    ):
        image = Image.new("RGB", (16, 16), color)
        meta = None
        if parameters:
            from PIL.PngImagePlugin import PngInfo

            meta = PngInfo()
            meta.add_text("parameters", parameters)
        path = tmp_path / f"real{index}.png"
        image.save(path, format="PNG", pnginfo=meta)

        assert read_png_text_chunks(path) == _legacy_read(path), f"第 {index} 张不一致"


# ---------------------------------------------------------------- P1-13 校验：一次 stat 拿全部信息


def test_validate_rejects_empty_file(tmp_path: Path):
    path = _write(tmp_path, b"")
    assert validate_image_file(path, (".png",), 10 * 1024 * 1024) == (False, "empty_file")


def test_validate_rejects_oversized_file(tmp_path: Path):
    path = _write(tmp_path, _png(_text("parameters", "x")))
    assert validate_image_file(path, (".png",), 1) == (False, "file_too_large")


def test_validate_rejects_bad_extension(tmp_path: Path):
    path = _write(tmp_path, _png(), name="sample.gif")
    assert validate_image_file(path, (".png",), 10 * 1024 * 1024) == (False, "bad_extension")


def test_validate_rejects_broken_image(tmp_path: Path):
    path = _write(tmp_path, b"\x89PNG\r\n\x1a\n" + b"garbage")
    assert validate_image_file(path, (".png",), 10 * 1024 * 1024) == (False, "invalid_image")


def test_validate_accepts_valid_png(tmp_path: Path):
    """用 Pillow 生成的真图（带 IDAT），不是手工拼的骨架。"""
    path = tmp_path / "real.png"
    Image.new("RGB", (8, 8), (120, 80, 200)).save(path, format="PNG")

    ok, code = validate_image_file(path, (".png",), 10 * 1024 * 1024)
    assert ok is True and code == ""


def test_validate_rejects_png_without_idat(tmp_path: Path):
    """反证：缺少 IDAT 的畸形 PNG 会让 Pillow 抛 IndexError。

    它不在原先的捕获列表里，会一路穿透 —— 上传这种图得到的是 500
    "服务器内部错误"，而不是 415 "图片损坏"。校验函数的任何解析异常
    都只应得出"不能用"这一个结论。
    """
    path = _write(tmp_path, _png(_text("parameters", "no image data")))

    ok, code = validate_image_file(path, (".png",), 10 * 1024 * 1024)
    assert ok is False
    assert code == "invalid_image"


def test_validate_rejects_animated(tmp_path: Path):
    first = Image.new("RGB", (8, 8), (255, 0, 0))
    second = Image.new("RGB", (8, 8), (0, 0, 255))
    path = tmp_path / "anim.webp"
    first.save(path, format="WEBP", save_all=True, append_images=[second])
    assert is_animated(path) is True
    assert validate_image_file(path, (".webp",), 10 * 1024 * 1024) == (False, "animated_image")


def test_validate_handles_missing_file(tmp_path: Path):
    assert validate_image_file(tmp_path / "gone.png", (".png",), 1024) == (False, "empty_file")


# ---------------------------------------------------------------- 真实图库抽样（有数据才跑）


def test_matches_legacy_on_real_gallery_sample():
    """对真实图库抽样比对；图库为空时跳过。"""
    gallery = Path(Config.GALLERY_DIR)
    files = sorted(gallery.rglob("*.png"))[:20] if gallery.is_dir() else []
    if not files:
        pytest.skip("图库为空，跳过真实数据比对")

    for path in files:
        assert read_png_text_chunks(path) == _legacy_read(path), f"{path.name} 结果不一致"
