"""M15 · 视频转序列帧 / GIF。

为什么需要这个模块
------------------
生成类 AI 的产出正在从「单张图」走向「短视频」，而**视频是没法直接二次编辑的**——
想把一段 AI 生成的动画拆成关键帧做逐帧修改、或者导出成能贴进 PPT / 聊天窗口的
GIF，就得先把视频拆开。

本模块只做三件事，每件事都对应一个明确的产物：

1. **导入**视频（上传文件，或指定本机已有路径）——落盘 + 用 ffprobe 取元信息；
2. **抽序列帧**——按给定 fps / 宽度 / 时间区间，把视频拆成一批有序 JPG；
3. **转 GIF**——**从已抽出的帧**合成，而不是从原视频重新抽。

第 3 点是个刻意的设计：GIF 从帧序列合成，才能**保证用户预览看到的序列帧就是 GIF
里播放的内容**。从原视频另抽一遍，哪怕参数一样，抽到的帧也可能错开一两帧，
预览和产物对不上——这种不一致最难排查。

幂等与安全
----------
- **抽帧是幂等的**：重复抽同一参数会先清空帧目录再写，不会新旧文件混在一起；
- **路径一律走 ``sync.paths.safe_join``**，帧文件名由后端按序号生成，不接受用户
  传入的路径；
- **外部命令一律带超时**（``SUBPROCESS_TIMEOUT``），ffmpeg 卡死不会拖挂服务；
- 所有异常都记日志并转成中文 ``AIBARError``，**不让 ffprobe/ffmpeg 的英文报错
  直接泄漏给用户**（沿用 M12 的 U3 约定）。

依赖：系统需装有 ``ffmpeg`` / ``ffprobe``（本模块会自动探测常见安装位置）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import math
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.fileops import purge_stale, rmtree_quiet, unlink_quiet
from core.logging_setup import get_logger, safe_log

_LOGGER = get_logger("aibar.video.service")

# ---------------------------------------------------------------- 常量

# data/video_frames/<clip_id>/frame_0001.jpg
VIDEO_OUTPUT_SUBDIR = "video_frames"
# data/video_sources/<clip_id>_<hash>.<ext>  （保留原始视频，便于改参数重抽）
VIDEO_SOURCE_SUBDIR = "video_sources"

FRAME_NAME_FMT = "frame_%04d.jpg"
FRAME_GLOB = "frame_*.jpg"
GIF_FILENAME = "clip.gif"
PALETTE_FILENAME = "palette.png"

# 去背景变体：原帧是 JPG（无 alpha），抠图结果必须存 PNG，因此单独放子目录。
# 放在 frames_dir 之下而非平级，好处是 delete_clip 的 rmtree 天然连带清理。
NOBG_SUBDIR = "nobg"
NOBG_NAME_FMT = "frame_%04d.png"
NOBG_GLOB = "frame_*.png"
# 序列帧拼图
SHEET_FILENAME = "sheet.png"
SHEET_NOBG_FILENAME = "sheet_nobg.png"
# 帧变体：original=抽帧原图，nobg=去掉背景后的透明图
VARIANTS = ("original", "nobg")

# 去背景默认参数：绿幕 + 中等容差（JPG/高压缩编码会让纯色背景带噪点偏色，
# 实测 0.25 在高压缩小视频上抠不净、0.3 全透，真实绿幕拍摄素材也可用）
DEFAULT_BG_COLOR = "#00FF00"
DEFAULT_BG_SIMILARITY = 0.3
DEFAULT_BG_BLEND = 0.05
# colorkey 的 similarity / blend 合法区间是 0.01–1
MIN_BG_SIMILARITY, MAX_BG_SIMILARITY = 0.01, 1.0
MIN_BG_BLEND, MAX_BG_BLEND = 0.0, 1.0

# 拼图默认参数
DEFAULT_SHEET_COLS = 5
MIN_SHEET_COLS, MAX_SHEET_COLS = 1, 50
MIN_SHEET_PADDING, MAX_SHEET_PADDING = 0, 50

# 允许上传的视频后缀（小写比较）
ALLOWED_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".gif", ".mpg", ".mpeg")
# 上传大小上限
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# 默认抽帧参数：8fps / 480 宽 / 最多 300 帧，是 GIF 场景的经验甜点
DEFAULT_FPS = 8
DEFAULT_MAX_FRAMES = 300
DEFAULT_SCALE_WIDTH = 480

# 参数边界（拒绝会把机器跑死的输入）
MIN_FPS, MAX_FPS = 1, 30
MIN_SCALE_WIDTH, MAX_SCALE_WIDTH = 64, 1920
MAX_FRAMES_HARD = 1000

# ffmpeg / ffprobe 超时（秒）
SUBPROCESS_TIMEOUT = 180

# ffmpeg 二进制的兜底探测位置（PATH 里找不到时按序尝试）
_FFMPEG_CANDIDATES = (
    "/usr/local/ffmpeg/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)
_FFPROBE_CANDIDATES = (
    "/usr/local/ffmpeg/bin/ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
)


# ---------------------------------------------------------------- 外部命令


def _bin(name: str, candidates: tuple[str, ...]) -> str:
    """定位 ffmpeg / ffprobe 可执行文件。

    Python 子进程拿到的 ``PATH`` 未必和用户 shell 一样（尤其 GUI / launchd 拉起的
    服务），所以先查 ``which``，查不到再按常见安装位置兜底。全部找不到时抛
    ``AIBARError``——这个错误用户能看懂，也知道该去装什么。
    """
    found = shutil.which(name)
    if found:
        return found
    for cand in candidates:
        p = Path(cand)
        try:
            if p.is_file() and p.stat().st_mode & 0o111:
                return str(p)
        except OSError:
            continue
    raise AIBARError("ffmpeg_missing", f"未找到 {name}：请先安装 ffmpeg（ brew install ffmpeg ），或把它加进 PATH")


def _run(cmd: list[str], tag: str) -> subprocess.CompletedProcess:
    """执行外部命令并统一错误处理。**永不抛出 None**，失败一律转 AIBARError。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        safe_log(_LOGGER, 30, f"video_{tag}_timeout", seconds=SUBPROCESS_TIMEOUT)
        raise AIBARError("ffmpeg_timeout", f"{tag} 超时（>{SUBPROCESS_TIMEOUT}s）：视频太长或参数过大，请调小帧数 / 缩短区间")
    except FileNotFoundError as exc:
        safe_log(_LOGGER, 30, f"video_{tag}_missing", error_type=type(exc).__name__)
        raise AIBARError("ffmpeg_missing", f"{tag} 执行失败：找不到命令 {cmd[0]}")
    except Exception as exc:  # 兜底：任何子进程异常都不能变成 500
        safe_log(_LOGGER, 30, f"video_{tag}_failed", error_type=type(exc).__name__)
        raise AIBARError("ffmpeg_failed", f"{tag} 执行失败：{type(exc).__name__}")

    if proc.returncode != 0:
        # 只取最后一段 stderr：ffmpeg 的报错在末尾，前面的版本横幅没用
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        safe_log(_LOGGER, 30, f"video_{tag}_nonzero", returncode=proc.returncode, detail=detail[0][:200])
        raise AIBARError("ffmpeg_failed", f"{tag} 失败：{detail[0][:200] or '未知错误'}")
    return proc


# ---------------------------------------------------------------- 元信息探测


def _parse_fps(rate: Any) -> float:
    """把 ffprobe 的 ``r_frame_rate``（如 ``30000/1001``、``30/1``）转浮点数。

    除零 / 非法格式一律返回 0.0——fps 只是展示与默认值参考，不该因为它炸掉导入。
    """
    try:
        s = str(rate or "")
        if "/" in s:
            num, den = s.split("/", 1)
            num_f, den_f = float(num), float(den)
            if den_f == 0:
                return 0.0
            return round(num_f / den_f, 3)
        return round(float(s), 3)
    except Exception:
        return 0.0


def probe_video(path: Path) -> dict[str, Any]:
    """用 ffprobe 读取视频元信息：时长 / 宽高 / 帧率。

    Returns:
        ``{"duration", "width", "height", "fps"}``；探测失败时字段为 0（**不抛**）——
        元信息缺失只影响展示，不该阻断导入。
    """
    result = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0}
    try:
        ffprobe = _bin("ffprobe", _FFPROBE_CANDIDATES)
    except AIBARError:
        return result

    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return result
        data = json.loads(proc.stdout or "{}")
    except Exception as exc:
        safe_log(_LOGGER, 30, "video_probe_failed", error_type=type(exc).__name__)
        return result

    try:
        result["duration"] = round(float((data.get("format") or {}).get("duration") or 0), 3)
    except Exception:
        pass

    for stream in data.get("streams") or []:
        # 只看视频流（codec_type == "video"），音频/字幕流没有宽高
        if stream.get("codec_type") != "video":
            continue
        try:
            result["width"] = int(stream.get("width") or 0)
            result["height"] = int(stream.get("height") or 0)
        except Exception:
            pass
        result["fps"] = _parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate"))
        break
    return result


# ---------------------------------------------------------------- 目录


def _sources_dir() -> Path:
    return Path(Config.DATA_DIR) / VIDEO_SOURCE_SUBDIR


def clips_dir() -> Path:
    """序列帧与 GIF 的根目录：``data/video_frames``。"""
    return Path(Config.DATA_DIR) / VIDEO_OUTPUT_SUBDIR


def frames_dir(clip_id: int) -> Path:
    """某个视频的帧目录：``data/video_frames/<clip_id>``。"""
    return clips_dir() / str(int(clip_id))


# ---------------------------------------------------------------- 帧变体（original / nobg）


def _norm_variant(variant: Any) -> str:
    """归一化帧变体名：只认 original / nobg，其它一律回落 original。

    为什么要兜底而不是报错：variant 来自 URL 查询参数，用户手改 URL 很常见，
    回落比抛 400 友好，且符合「未知变体当原图」的直觉。
    """
    return "nobg" if str(variant or "").strip().lower() == "nobg" else "original"


def frames_dir_for(clip_id: int, variant: str = "original") -> Path:
    """某个变体的帧目录（nobg 是原帧目录的子目录）。"""
    d = frames_dir(clip_id)
    return d / NOBG_SUBDIR if _norm_variant(variant) == "nobg" else d


def glob_for(variant: str = "original") -> str:
    """变体对应的帧通配名（原图 JPG / 去背景 PNG）。"""
    return NOBG_GLOB if _norm_variant(variant) == "nobg" else FRAME_GLOB


def name_fmt_for(variant: str = "original") -> str:
    """变体对应的 ffmpeg 输入文件名格式。"""
    return NOBG_NAME_FMT if _norm_variant(variant) == "nobg" else FRAME_NAME_FMT


def sheet_file(clip_id: int, variant: str = "original") -> Path:
    """拼图产物路径。按变体分名，两种拼图可共存（原图版 / 透明版）。"""
    name = SHEET_NOBG_FILENAME if _norm_variant(variant) == "nobg" else SHEET_FILENAME
    return frames_dir(clip_id) / name


def gif_file_for(clip_id: int, variant: str = "original") -> Path:
    """GIF 产物路径。同样按变体分名，与拼图保持一致的命名习惯。"""
    name = "clip_nobg.gif" if _norm_variant(variant) == "nobg" else GIF_FILENAME
    return frames_dir(clip_id) / name


# ---------------------------------------------------------------- 参数校验


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        iv = int(value)
    except Exception:
        return default
    return max(lo, min(hi, iv))


def _opt_float(value: Any) -> float | None:
    """解析可选的秒数；空值 / 非法值返回 None（表示不裁剪）。"""
    if value is None or value == "":
        return None
    try:
        fv = float(value)
    except Exception:
        return None
    if fv < 0:
        return None
    return fv


# ---------------------------------------------------------------- 导入


def save_upload_stream(file_storage: Any, filename: str) -> Path:
    """把上传流写成临时文件（**边写边查上限**），返回临时文件路径。

    绝不把整个文件读进内存：视频可能有几百 MB。超限立即中断并清掉半成品，
    不留半个文件占磁盘。
    """
    suffix = Path(filename or "").suffix.lower()
    fd, tmp_str = tempfile.mkstemp(prefix="aibar_upload_", suffix=suffix or ".tmp")
    tmp = Path(tmp_str)
    try:
        written = 0
        with os.fdopen(fd, "wb") as fh:
            for chunk in file_storage.stream:
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise AIBARError(
                        "invalid_input",
                        "视频太大：超过 %d MB 上限" % (MAX_UPLOAD_BYTES // 1024 // 1024),
                    )
                fh.write(chunk)
        if written == 0:
            raise AIBARError("invalid_input", "上传内容为空")
        return tmp
    except Exception:
        unlink_quiet(tmp)
        raise


def import_video(source: Path, filename: str, name: str = "") -> dict[str, Any]:
    """登记一个视频文件并建库。

    收**源文件路径**而不是 bytes —— 上传的文件可能有几百 MB，全读进内存既慢又
    容易 OOM（项目里 M12 的 ``_download_output_bytes`` 就为这个专门做了流式 +
    大小上限）。调用方负责把上传流写成临时文件，这里只做「搬进 data/ + 探测」。

    Args:
        source: 已存在的源文件（通常是上传落地的临时文件）。
        filename: 原始文件名（只用于取后缀与默认展示名，不参与落盘路径构造）。
        name: 展示名；留空则用文件名主干。

    Returns:
        视频行（含探测到的元信息）。

    Raises:
        AIBARError: 后缀不允许 / 源文件不存在 / 写盘失败。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise AIBARError(
            "invalid_input",
            "不支持的视频格式：%s（支持 %s）" % (suffix or "未知", " / ".join(ALLOWED_VIDEO_SUFFIXES)),
        )
    src = Path(source)
    if not src.is_file():
        raise AIBARError("invalid_input", "源文件不存在：%s" % src)
    try:
        if src.stat().st_size > MAX_UPLOAD_BYTES:
            raise AIBARError(
                "invalid_input",
                "视频太大：%.1f MB（上限 %d MB）"
                % (src.stat().st_size / 1024 / 1024, MAX_UPLOAD_BYTES // 1024 / 1024),
            )
    except OSError as exc:
        raise AIBARError("invalid_input", "读取源文件失败：%s" % type(exc).__name__)

    base = (Path(filename or "clip").stem or "clip")[:60]
    ts = now()
    cur = db.execute(
        "INSERT INTO video_clips (name, source_path, status, created_at, updated_at) "
        "VALUES (?, '', 'idle', ?, ?)",
        (name or base, ts, ts),
    )
    try:
        clip_id = int(cur.lastrowid)
    except Exception as exc:
        raise AIBARError("db_error", "创建视频记录失败：%s" % type(exc).__name__)

    src_dir = _sources_dir()
    dest = src_dir / f"{clip_id}_{base}{suffix}"
    try:
        src_dir.mkdir(parents=True, exist_ok=True)
        # 同源盘内用 move 省一次拷贝；跨盘 / 临时文件用 copy 兜底
        try:
            shutil.move(str(src), str(dest))
        except OSError:
            shutil.copyfile(str(src), str(dest))
    except OSError as exc:
        # 写盘失败要清掉刚建的库记录，否则留下一条指向不存在文件的脏数据
        try:
            db.execute("DELETE FROM video_clips WHERE id=?", (clip_id,))
        except Exception:
            pass
        safe_log(_LOGGER, 30, "video_import_write_failed", error_type=type(exc).__name__)
        raise AIBARError("write_failed", "保存视频失败：%s" % type(exc).__name__)

    # 相对 DATA_DIR 存储（与 comic 的 image_path 一致，便于整体迁移 data/）
    try:
        rel = str(dest.relative_to(Path(Config.DATA_DIR)))
    except ValueError:
        rel = str(dest)

    meta = probe_video(dest)
    if not name:
        db.execute("UPDATE video_clips SET name=? WHERE id=?", (base, clip_id))
    db.execute(
        "UPDATE video_clips SET source_path=?, duration=?, width=?, height=?, src_fps=?, updated_at=? "
        "WHERE id=?",
        (rel, meta["duration"], meta["width"], meta["height"], meta["fps"], now(), clip_id),
    )
    size = dest.stat().st_size if dest.is_file() else 0
    safe_log(_LOGGER, 20, "video_imported", clip_id=clip_id, bytes=size, **meta)
    return get_clip(clip_id) or {}


def import_local_path(abs_path: str, name: str = "") -> dict[str, Any]:
    """导入本机已有的视频文件（不复制，只登记路径）。

    比上传省一次拷贝，适合本机已有的大视频。路径必须是**绝对路径且文件存在**。
    """
    p = Path(abs_path or "").expanduser()
    if not p.is_absolute():
        raise AIBARError("invalid_input", "请提供绝对路径")
    if not p.is_file():
        raise AIBARError("not_found", "文件不存在：%s" % p)
    if p.suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
        raise AIBARError("invalid_input", "不支持的视频格式：%s" % p.suffix)

    display = name or p.stem[:60]
    ts = now()
    cur = db.execute(
        "INSERT INTO video_clips (name, source_path, status, created_at, updated_at) "
        "VALUES (?, ?, 'idle', ?, ?)",
        (display, str(p), ts, ts),
    )
    clip_id = int(cur.lastrowid)
    meta = probe_video(p)
    db.execute(
        "UPDATE video_clips SET duration=?, width=?, height=?, src_fps=? WHERE id=?",
        (meta["duration"], meta["width"], meta["height"], meta["fps"], clip_id),
    )
    return get_clip(clip_id) or {}


# ---------------------------------------------------------------- 读取


def _row_to_clip(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    d = dict(row) if not isinstance(row, dict) else dict(row)
    cid = int(d.get("id") or 0)
    d["frame_count_actual"] = len(list_frames(cid))
    d["has_gif"] = bool(d.get("gif_path")) and (Path(Config.DATA_DIR) / str(d.get("gif_path") or "")).is_file()
    return d


def get_clip(clip_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM video_clips WHERE id=?", (int(clip_id),))
    return _row_to_clip(row)


def list_clips(keyword: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """视频列表（按更新时间倒序）。"""
    lim = _clamp_int(limit, 50, 1, 500)
    off = _clamp_int(offset, 0, 0, 100000)
    kw = (keyword or "").strip()
    where, params = "", []
    if kw:
        where = " WHERE name LIKE ?"
        params.append(f"%{kw}%")

    # db.query_one 返回 sqlite3.Row（不是 dict），只能下标取值——用 .get() 会
    # 报 AttributeError。这里沿用 actors.py 的范式：空行时兜一个 dict 字面量。
    total = db.query_one("SELECT COUNT(*) AS c FROM video_clips" + where, params)
    rows = db.rows_to_dicts(db.query_all(
        "SELECT * FROM video_clips" + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [lim, off],
    ))
    return {
        "total": int((total or {"c": 0})["c"] or 0),
        "items": [_row_to_clip(r) for r in rows],
    }


def list_frames(clip_id: int, variant: str = "original") -> list[str]:
    """列出某视频某变体的帧文件名（**按序号排序**）。

    Args:
        variant: ``original``（抽帧原图 JPG）或 ``nobg``（去背景透明图 PNG）。

    为什么不建帧表：视频帧是「一次生成、整体播放」的批量产物，不需要逐帧改动作 /
    重生成 / 排序。列目录天然有序、零维护。真需要单帧操作时再补表不迟。
    """
    d = frames_dir_for(clip_id, variant)
    try:
        if not d.is_dir():
            return []
        names = [p.name for p in d.glob(glob_for(variant)) if p.is_file()]
    except OSError:
        return []

    def _key(n: str) -> tuple[int, str]:
        # frame_0001.jpg → 1；抽不到数字时排最后，保证顺序稳定
        digits = "".join(ch for ch in n if ch.isdigit())
        return (int(digits) if digits else 10**9, n)

    return sorted(names, key=_key)


# ---------------------------------------------------------------- 衍生产物清理


def _purge_derived(out_dir: Path, include_nobg: bool = True) -> None:
    """清掉基于「当前这批帧」算出来的衍生产物。

    帧一旦重抽（数量 / 尺寸 / 时间点变了），去背景帧、拼图、GIF 全部失效。
    留着它们的危害不是占空间，而是**前端仍显示「已生成」**——用户看到的是
    与新帧错位、甚至张冠李戴的旧图，而且很难意识到要重跑。

    删除一律走 fileops 收口：safe-delete 钩子对整目录删除有批量保护，
    直接 rmtree 会以 SystemExit 干断请求线程（2026-09-05 实测事故）。
    """
    if include_nobg:
        rmtree_quiet(out_dir / NOBG_SUBDIR)
    for name in (SHEET_FILENAME, SHEET_NOBG_FILENAME, GIF_FILENAME, "clip_nobg.gif", PALETTE_FILENAME):
        unlink_quiet(out_dir / name)


# ---------------------------------------------------------------- 抽帧


def extract_frames(
    clip_id: int,
    fps: int | None = None,
    max_frames: int | None = None,
    scale_width: int | None = None,
    start_sec: Any = None,
    end_sec: Any = None,
) -> dict[str, Any]:
    """把视频拆成一批有序 JPG。**先清空帧目录再写**，保证不留旧参数的文件。

    Returns:
        ``{"clip_id", "frames", "fps", "scale_width", "files": [文件名...]}``
    """
    clip = get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    src_raw = str(clip.get("source_path") or "")
    src = Path(src_raw)
    if not src.is_absolute():
        src = Path(Config.DATA_DIR) / src_raw
    if not src.is_file():
        raise AIBARError("not_found", "源视频文件已不存在：%s" % src)

    use_fps = _clamp_int(fps, clip.get("fps") or DEFAULT_FPS, MIN_FPS, MAX_FPS)
    use_max = _clamp_int(max_frames, clip.get("max_frames") or DEFAULT_MAX_FRAMES, 1, MAX_FRAMES_HARD)
    use_w = _clamp_int(scale_width, clip.get("scale_width") or DEFAULT_SCALE_WIDTH, MIN_SCALE_WIDTH, MAX_SCALE_WIDTH)
    start = _opt_float(start_sec)
    end = _opt_float(end_sec)

    out_dir = frames_dir(clip_id)
    try:
        # 幂等：清掉上一次的产物，避免新旧参数的文件混在一个目录里。
        # 清帧走 fileops 收口（几百次 unlink 不能有 SystemExit 穿透）
        if out_dir.is_dir():
            purge_stale(out_dir, FRAME_GLOB)
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
        # 帧换了 → 基于旧帧的衍生产物（去背景帧 / 拼图 / GIF）全部失效，一并清掉
        _purge_derived(out_dir)
    except OSError as exc:
        raise AIBARError("write_failed", "准备帧目录失败：%s" % type(exc).__name__)

    ffmpeg = _bin("ffmpeg", _FFMPEG_CANDIDATES)
    cmd = [ffmpeg, "-y"]
    # -ss 放在 -i 之前 = 输入定位（快）；只在需要裁剪时才加
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if start is not None and end is not None and end > start:
        cmd += ["-t", str(round(end - start, 3))]
    elif end:
        cmd += ["-t", str(round(end, 3))]

    # lanczos 缩放比默认双线性锐利；-frames:v 硬上限防止长视频爆盘
    vf = f"fps={use_fps},scale={use_w}:-1:flags=lanczos"
    cmd += ["-vf", vf, "-frames:v", str(use_max), "-q:v", "2", str(out_dir / FRAME_NAME_FMT)]

    db.execute("UPDATE video_clips SET status='extracting', last_error='' WHERE id=?", (clip_id,))
    try:
        _run(cmd, "extract_frames")
    except AIBARError as exc:
        db.execute(
            "UPDATE video_clips SET status='failed', last_error=?, updated_at=? WHERE id=?",
            (str(exc), now(), clip_id),
        )
        raise

    files = list_frames(clip_id)
    if not files:
        db.execute(
            "UPDATE video_clips SET status='failed', last_error=?, updated_at=? WHERE id=?",
            ("未抽出任何帧：视频可能损坏或格式不支持", now(), clip_id),
        )
        raise AIBARError("ffmpeg_failed", "未抽出任何帧：视频可能损坏或格式不支持")

    db.execute(
        "UPDATE video_clips SET fps=?, max_frames=?, scale_width=?, start_sec=?, end_sec=?, "
        "frames_dir=?, frame_count=?, status='ready', last_error='', "
        "has_nobg=0, has_sheet=0, gif_path='', gif_bytes=0, updated_at=? WHERE id=?",
        (
            use_fps, use_max, use_w, start, end,
            f"{VIDEO_OUTPUT_SUBDIR}/{clip_id}", len(files), now(), clip_id,
        ),
    )
    safe_log(_LOGGER, 20, "video_frames_extracted", clip_id=clip_id, frames=len(files), fps=use_fps, width=use_w)
    return {
        "clip_id": int(clip_id),
        "frames": len(files),
        "fps": use_fps,
        "scale_width": use_w,
        "files": files,
    }


# ---------------------------------------------------------------- 转 GIF


def build_gif(clip_id: int, fps: int | None = None, variant: str = "original") -> dict[str, Any]:
    """从**已抽出的帧**合成 GIF（两遍 palette 法，颜色最准）。

    刻意不从原视频重抽：GIF 从帧序列合成，才能保证预览到的序列帧就是 GIF 里
    播放的内容。另抽一遍哪怕参数相同也可能错开帧，预览与产物对不上最难排查。

    Args:
        variant: ``original`` 用原帧；``nobg`` 用去背景帧，并**保留透明**——
            GIF 只有 1-bit 透明，靠 ``reserve_transparent`` + ``alpha_threshold`` 实现。
    """
    clip = get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    v = _norm_variant(variant)
    files = list_frames(clip_id, v)
    if not files:
        raise AIBARError("invalid_input", "还没有序列帧：请先抽帧再转 GIF")

    out_dir = frames_dir_for(clip_id, v)
    use_fps = _clamp_int(fps, clip.get("fps") or DEFAULT_FPS, MIN_FPS, MAX_FPS)

    ffmpeg = _bin("ffmpeg", _FFMPEG_CANDIDATES)
    # 调色板统一放帧目录根：它是中间产物，放进变体目录容易被人误当成一帧
    palette = frames_dir(clip_id) / PALETTE_FILENAME
    gif_path = gif_file_for(clip_id, v)
    src_fmt = str(out_dir / name_fmt_for(v))

    # 去背景变体要保住透明：生成调色板时预留一个透明色，合成时给 alpha 阈值
    gen_vf = "palettegen=max_colors=256" + (":reserve_transparent=1" if v == "nobg" else "")
    use_vf = "[0:v][1:v]paletteuse=dither=bayer:bayer_scale=5" + (
        ":alpha_threshold=128" if v == "nobg" else "")

    # 第一遍：生成调色板（GIF 只有 256 色，直接转会严重色带）
    _run(
        [ffmpeg, "-y", "-framerate", str(use_fps), "-i", src_fmt,
         "-vf", gen_vf, "-frames:v", "1", str(palette)],
        "palettegen",
    )
    # 第二遍：用调色板合成（bayer 抖动在渐变区域表现最自然）
    _run(
        [ffmpeg, "-y", "-framerate", str(use_fps), "-i", src_fmt,
         "-i", str(palette),
         "-filter_complex", use_vf,
         str(gif_path)],
        "gif_encode",
    )

    # 调色板是中间产物，用完即删（留着只会让用户以为是产出之一）。
    # 走 unlink_quiet 收口：safe-delete 的批量保护是**回合级累计**的，回合内
    # 删过 50 个以上对象后，裸 unlink 会以 SystemExit 穿透 except OSError，
    # 直接干断请求线程（2026-09-05 实测：转 GIF 接口返回空响应）。
    unlink_quiet(palette)

    if not gif_path.is_file():
        raise AIBARError("ffmpeg_failed", "GIF 生成失败：未产出文件")

    try:
        rel = str(gif_path.relative_to(Path(Config.DATA_DIR)))
    except ValueError:
        rel = str(gif_path)
    size = gif_path.stat().st_size

    db.execute(
        "UPDATE video_clips SET gif_path=?, gif_bytes=?, updated_at=? WHERE id=?",
        (rel, size, now(), clip_id),
    )
    safe_log(_LOGGER, 20, "video_gif_built", clip_id=clip_id, bytes=size, fps=use_fps, variant=v)
    return {"clip_id": int(clip_id), "gif_path": rel, "bytes": size, "fps": use_fps,
            "frames": len(files), "variant": v}


# ---------------------------------------------------------------- 去背景（按颜色抠图）


def _parse_hex_color(value: Any) -> str:
    """把 ``#RRGGBB`` / ``#RGB`` / ``RRGGBB`` 归一化成 ``#rrggbb``；非法返回空串。"""
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return ""
    try:
        int(s, 16)
    except ValueError:
        return ""
    return "#" + s.lower()


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        fv = float(value)
    except Exception:
        return default
    return max(lo, min(hi, fv))


def _alpha_expr(kr: int, kg: int, kb: int, s2: int, e2: int, with_blend: bool) -> str:
    """单个钥匙色对应的 alpha 表达式（纯四则 + if(lt())，避免 sqrt/pow/st/ld）。

    alpha=0 → 透明（抠掉），alpha=255 → 不透明（保留）。
    """
    d2 = (
        "(r(X,Y)-%d)*(r(X,Y)-%d)+(g(X,Y)-%d)*(g(X,Y)-%d)+(b(X,Y)-%d)*(b(X,Y)-%d)"
        % (kr, kr, kg, kg, kb, kb)
    )
    if with_blend:
        # 平方距离域里做线性过渡：视觉上与 colorkey 的线性域过渡差异可忽略
        return (
            "if(lt(%(d)s,%(s)d),0,if(lt(%(d)s,%(e)d),255*(%(d)s-%(s)d)/(%(e)d-%(s)d),255))"
            % {"d": d2, "s": s2, "e": e2}
        )
    return "if(lt(%(d)s,%(s)d),0,255)" % {"d": d2, "s": s2}


def _colorkey_geq_filter(
    color: str, similarity: float, blend: float,
    color2: str = "", similarity2: Any = None, blend2: Any = None,
) -> str:
    """构造「按颜色抠 alpha」的 geq 滤镜表达式，支持**双色**抠图。

    为什么不用 ``colorkey``：本机这枚 ffmpeg（N-97810 / 2020）的 colorkey 对
    ``0xRRGGBB`` / ``#RRGGBB`` 的颜色解析有 bug——实测 key 色被解析成完全无关
    的颜色，similarity 调到 0.4 都抠不动，只有颜色名（如 ``green``）可靠。
    但取色器给的是任意 hex。改用 ``geq`` 把 key 色当**数字**内嵌进表达式，
    彻底绕开颜色解析；距离用平方和——同一枚 ffmpeg 的 eval 里 ``sqrt`` /
    ``pow`` 也不可靠（表达式静默失效，alpha 恒 255），纯四则运算最稳。

    similarity / blend 与 colorkey 同尺度（0-1 的归一化 RGB 距离，分母 √3×255），
    用户的调参直觉不变：similarity 越大抠得越多，blend 是边缘过渡带宽。

    双色：第二把钥匙色 ``color2`` 非空时，最终 alpha 取两把钥匙各自 alpha 的
    **最小值**（close-to-either → 透明）。``min`` 在本机 ffmpeg eval 里可用
    （已实测），比嵌套 if 合成更直观。
    """
    key = color.lstrip("#")
    kr, kg, kb = int(key[0:2], 16), int(key[2:4], 16), int(key[4:6], 16)
    norm = math.sqrt(3.0) * 255.0   # 与 colorkey 相同的归一化分母（对角线长度）
    s2 = int(round((similarity * norm) ** 2))          # 全透明阈值的平方距离
    e2 = int(round(((similarity + blend) * norm) ** 2))  # 完全不透明阈值的平方距离
    a1 = _alpha_expr(kr, kg, kb, s2, e2, blend > 0 and e2 > s2)

    if color2:
        key2 = color2.lstrip("#")
        kr2, kg2, kb2 = int(key2[0:2], 16), int(key2[2:4], 16), int(key2[4:6], 16)
        sim2 = similarity if similarity2 is None else similarity2
        bl2 = blend if blend2 is None else blend2
        s2b = int(round((sim2 * norm) ** 2))
        e2b = int(round(((sim2 + bl2) * norm) ** 2))
        a2 = _alpha_expr(kr2, kg2, kb2, s2b, e2b, bl2 > 0 and e2b > s2b)
        # 近任意一把钥匙 → 透明：min 取更透明（更小）的那侧
        alpha = "min(%s,%s)" % (a1, a2)
    else:
        alpha = a1
    return "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='%s'" % alpha


# ---------------------------------------------------------------- 去背景：连通抠图（flood / 区域生长）

# 去背景模式：colorkey=按颜色整图抠（默认）；flood=只抠与选点连通的背景
BG_MODE_COLORKEY = "colorkey"
BG_MODE_FLOOD = "flood"


def _frame_index(name: str) -> int:
    """从帧文件名里抠出序号（frame_0001.jpg → 1）。"""
    m = re.search(r"(\d+)", str(name))
    return int(m.group(1)) if m else 1


def _parse_seed(value: Any, w: int, h: int) -> tuple[int, int]:
    """解析种子点字符串 ``"x,y"``（source 帧像素坐标）。

    非法 / 缺省回落左上角 ``(0,0)``；坐标越界则夹到 ``[0, w-1]`` / ``[0, h-1]``。
    """
    sx, sy = 0, 0
    s = str(value or "").strip()
    if s:
        parts = s.split(",")
        if len(parts) == 2:
            try:
                sx = int(float(parts[0]))
                sy = int(float(parts[1]))
            except ValueError:
                sx, sy = 0, 0
    sx = max(0, min(sx, w - 1))
    sy = max(0, min(sy, h - 1))
    return sx, sy


def _flood_mask(px: list, w: int, h: int, seed_x: int, seed_y: int, tol2: int) -> bytearray:
    """4-连通**区域生长**：从种子点出发，只把「颜色接近**种子色**」且**连通**的像素标为背景（1）。

    关键语义（对应「只抠与选点连通背景、不连通同色保留」）：

    - 以**种子色为基准做全局容差**（不是与相邻像素比）。某像素要被抠掉，必须同时满足：
      ① 颜色与种子色足够接近（``<= tol``）；② 通过 4-连通路径连到种子点。
    - 因此「画面里另一块同色背景、但被主体隔开」→ 条件②不满足 → **保留**（不连通同色区域保留）。
    - **不会跨背景色不一致处扩散**：若背景本身有渐变/偏色，远离种子色的那部分不满足①，自动停住，
      不会顺着渐变一路长穿整张图（这正是「局部容差」区域生长会出的 bug——渐变每步都很小，
      会一路长到主体里）。

    Args:
        px: 扁平 RGB 元组列表（``img.getdata()``）。
        w, h: 帧宽高。
        seed_x, seed_y: 种子点（已夹到合法范围）。
        tol2: 种子色容差的平方（欧氏 RGB 距离阈值平方，与 colorkey 同尺度）。
    Returns:
        ``bytearray(w*h)``，1=背景（要抠掉），0=保留。
    """
    n = w * h
    bg = bytearray(n)            # 结果：1=背景
    visited = bytearray(n)       # BFS 访问标记
    if not (0 <= seed_x < w and 0 <= seed_y < h):
        return bg
    base = seed_y * w + seed_x
    sr, sg, sb = px[base]
    dq = deque()
    dq.append(base)
    visited[base] = 1
    while dq:
        idx = dq.popleft()
        cx, cy = idx % w, idx // w
        # 4-邻域；越界用 -1 哨兵（下方统一跳过）
        for ni in (
            idx - 1 if cx > 0 else -1,
            idx + 1 if cx < w - 1 else -1,
            idx - w if cy > 0 else -1,
            idx + w if cy < h - 1 else -1,
        ):
            if ni < 0 or visited[ni]:
                continue
            r, g, b = px[ni]
            # 与「种子色」比（全局基准），而非与当前扩展像素比
            if (r - sr) ** 2 + (g - sg) ** 2 + (b - sb) ** 2 <= tol2:
                visited[ni] = 1
                dq.append(ni)
    for i in range(n):
        bg[i] = visited[i]
    return bg


def _img_rgb_list(img) -> list:
    """把 RGB 图像压平成 ``[(r,g,b), ...]`` 列表（用 tobytes 取，避开 getdata 弃用告警）。"""
    raw = img.tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


def _remove_background_flood(
    clip_id: int, seed: Any, similarity: Any, blend: Any
) -> dict[str, Any]:
    """连通抠图：从种子点区域生长，只抠与种子**连通**的背景，不连通同色区域保留。

    逐帧用 PIL 读原帧 → 算 mask → 合成 RGBA 透明 PNG 落 ``nobg``。无 numpy 依赖，
    纯 Python + PIL 实现（典型 M15 序列帧数量下足够快）。

    Returns:
        ``{"clip_id", "frames", "mode", "seed", "seed_color", "similarity", "blend"}``
    """
    files = list_frames(clip_id)
    if not files:
        raise AIBARError("invalid_input", "还没有序列帧：请先抽帧再去背景")

    use_sim = _clamp_float(similarity, DEFAULT_BG_SIMILARITY, MIN_BG_SIMILARITY, MAX_BG_SIMILARITY)
    use_blend = _clamp_float(blend, DEFAULT_BG_BLEND, MIN_BG_BLEND, MAX_BG_BLEND)
    norm = math.sqrt(3.0) * 255.0
    tol2 = int(round((use_sim * norm) ** 2))

    out_dir = frames_dir(clip_id)
    nobg_dir = frames_dir_for(clip_id, "nobg")
    rmtree_quiet(nobg_dir)
    try:
        nobg_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AIBARError("write_failed", "准备去背景目录失败：%s" % type(exc).__name__)

    # 先读首帧确定尺寸 + 解析种子 + 取种子色（用于展示/复用），再统一应用到全部帧
    first = Image.open(out_dir / files[0]).convert("RGB")
    fw, fh = first.size
    sx, sy = _parse_seed(seed, fw, fh)
    fps = _img_rgb_list(first)
    sr, sg, sb = fps[sy * fw + sx]
    seed_color = "#%02x%02x%02x" % (sr, sg, sb)
    seed_str = "%d,%d" % (sx, sy)

    made = 0
    for fname in files:
        img = Image.open(out_dir / fname).convert("RGB")
        w, h = img.size
        px = _img_rgb_list(img)
        xs = min(sx, w - 1)
        ys = min(sy, h - 1)
        bg = _flood_mask(px, w, h, xs, ys, tol2)
        n = w * h
        if use_blend > 0:
            # 边缘羽化：把二值 alpha 做高斯模糊，抠边更柔
            a = Image.frombytes("L", (w, h), bytes(255 if bg[i] == 0 else 0 for i in range(n)))
            a = a.filter(ImageFilter.GaussianBlur(max(0.5, use_blend * 6.0)))
            ab = a.tobytes()
        else:
            ab = bytes(255 if bg[i] == 0 else 0 for i in range(n))
        rgb = img.tobytes()
        rgba = bytearray(n * 4)
        for i in range(n):
            rgba[i * 4] = rgb[i * 3]
            rgba[i * 4 + 1] = rgb[i * 3 + 1]
            rgba[i * 4 + 2] = rgb[i * 3 + 2]
            rgba[i * 4 + 3] = ab[i]
        out_name = NOBG_NAME_FMT % _frame_index(fname)
        Image.frombytes("RGBA", (w, h), bytes(rgba)).save(nobg_dir / out_name, "PNG")
        made += 1

    return {
        "clip_id": int(clip_id), "frames": made, "mode": BG_MODE_FLOOD,
        "seed": seed_str, "seed_color": seed_color,
        "similarity": use_sim, "blend": use_blend,
    }


def remove_background(
    clip_id: int,
    color: Any = None,
    similarity: Any = None,
    blend: Any = None,
    color2: Any = None,
    similarity2: Any = None,
    blend2: Any = None,
    mode: Any = None,
    seed: Any = None,
) -> dict[str, Any]:
    """按颜色（可双色）抠掉帧背景，产出**带 alpha 的 PNG** 到 ``nobg`` 变体目录。

    抠图走 ``_colorkey_geq_filter``（geq 平方距离，见其 docstring——本机 ffmpeg
    的 colorkey 颜色解析有 bug）。JPG 原帧有压缩噪点，纯色背景其实并不纯，
    所以 similarity（容差）要给够、blend 做边缘羽化，否则抠完会留一圈硬边。
    传入第二把钥匙色 ``color2`` 即可双色抠图（两色各自的容差/羽化可独立给）。

    原帧**不做任何改动**——去背景是可逆的，随时能 clear 回原图。

    Returns:
        ``{"clip_id", "frames", "color", "similarity", "blend",
          "color2", "similarity2", "blend2"}``
    """
    clip = get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    # 连通抠图模式：从种子点区域生长，只抠与种子连通的背景（不连通同色区域保留）。
    # 双色抠图（color2）仅在 colorkey 模式有意义，flood 模式忽略它。
    use_mode = BG_MODE_FLOOD if str(mode or "").strip().lower() == BG_MODE_FLOOD else BG_MODE_COLORKEY
    if use_mode == BG_MODE_FLOOD:
        res = _remove_background_flood(clip_id, seed, similarity, blend)
        db.execute(
            "UPDATE video_clips SET has_nobg=1, bg_mode='flood', bg_seed=?, bg_color=?, "
            "bg_similarity=?, bg_blend=?, bg_color2='', bg_similarity2=NULL, bg_blend2=NULL, "
            "status='ready', last_error='', updated_at=? WHERE id=?",
            (res["seed"], res["seed_color"], res["similarity"], res["blend"], now(), clip_id),
        )
        safe_log(_LOGGER, 20, "video_bg_flood", clip_id=clip_id, frames=res["frames"],
                 seed=res["seed"], similarity=res["similarity"])
        return res

    # 颜色：显式传了却格式不对必须报错（静默回落成绿色最坑人）；没传就用上次的值。
    # 放在抽帧检查之前——坏输入要在参数边界就 fail-fast，不要等跑完流程才暴露。
    if color not in (None, "", "auto"):
        use_color = _parse_hex_color(color)
        if not use_color:
            raise AIBARError("invalid_input", "颜色格式不对：%s（应为 #RRGGBB，如 #00FF00）" % color)
    else:
        use_color = _parse_hex_color(clip.get("bg_color")) or DEFAULT_BG_COLOR

    # 第二把钥匙：空 / 未传 → 单色抠图；传了就校验格式
    use_color2 = ""
    if color2 not in (None, "", "auto"):
        parsed2 = _parse_hex_color(color2)
        if not parsed2:
            raise AIBARError("invalid_input", "第二颜色格式不对：%s（应为 #RRGGBB）" % color2)
        use_color2 = parsed2

    files = list_frames(clip_id)
    if not files:
        raise AIBARError("invalid_input", "还没有序列帧：请先抽帧再去背景")

    use_sim = _clamp_float(similarity, DEFAULT_BG_SIMILARITY, MIN_BG_SIMILARITY, MAX_BG_SIMILARITY)
    use_blend = _clamp_float(blend, DEFAULT_BG_BLEND, MIN_BG_BLEND, MAX_BG_BLEND)
    if use_color2:
        use_sim2 = _clamp_float(similarity2, use_sim, MIN_BG_SIMILARITY, MAX_BG_SIMILARITY)
        use_blend2 = _clamp_float(blend2, use_blend, MIN_BG_BLEND, MAX_BG_BLEND)
    else:
        use_sim2 = None
        use_blend2 = None

    out_dir = frames_dir(clip_id)
    nobg_dir = frames_dir_for(clip_id, "nobg")
    # 幂等：重跑前清空，否则新旧参数的帧混在一起（帧数不同会半透明半不透明）。
    # 删整目录走 rmtree_quiet：safe-delete 对整目录删除有批量保护，裸 rmtree
    # 会 SystemExit 干断请求线程（2026-09-05 去背景接口断连的根因）。
    rmtree_quiet(nobg_dir)
    try:
        nobg_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AIBARError("write_failed", "准备去背景目录失败：%s" % type(exc).__name__)

    ffmpeg = _bin("ffmpeg", _FFMPEG_CANDIDATES)
    cmd = [
        ffmpeg, "-y",
        "-i", str(out_dir / FRAME_NAME_FMT),
        "-vf", _colorkey_geq_filter(use_color, use_sim, use_blend,
                                    use_color2, use_sim2, use_blend2),
        "-pix_fmt", "rgba",   # 必须显式：否则 alpha 可能被编码器丢掉，等于白抠一场
        "-c:v", "png",
        str(nobg_dir / NOBG_NAME_FMT),
    ]
    db.execute("UPDATE video_clips SET status='extracting', last_error='' WHERE id=?", (clip_id,))
    try:
        _run(cmd, "remove_background")
    except AIBARError as exc:
        db.execute(
            "UPDATE video_clips SET status='failed', last_error=?, updated_at=? WHERE id=?",
            (str(exc), now(), clip_id),
        )
        raise

    made = list_frames(clip_id, "nobg")
    if not made:
        db.execute(
            "UPDATE video_clips SET status='failed', last_error=?, updated_at=? WHERE id=?",
            ("去背景未产出任何帧", now(), clip_id),
        )
        raise AIBARError("ffmpeg_failed", "去背景未产出任何帧")

    db.execute(
        "UPDATE video_clips SET has_nobg=1, bg_mode='colorkey', bg_seed='', "
        "bg_color=?, bg_similarity=?, bg_blend=?, "
        "bg_color2=?, bg_similarity2=?, bg_blend2=?, "
        "status='ready', last_error='', updated_at=? WHERE id=?",
        (use_color, use_sim, use_blend, use_color2, use_sim2, use_blend2, now(), clip_id),
    )
    safe_log(_LOGGER, 20, "video_bg_removed", clip_id=clip_id, frames=len(made),
             color=use_color, color2=use_color2, similarity=use_sim)
    return {"clip_id": int(clip_id), "frames": len(made),
            "mode": BG_MODE_COLORKEY, "seed": "",
            "color": use_color, "similarity": use_sim, "blend": use_blend,
            "color2": use_color2, "similarity2": use_sim2, "blend2": use_blend2}


def clear_background(clip_id: int) -> dict[str, Any]:
    """删掉去背景变体（**保留原帧**），回到「只有原始序列帧」的状态。"""
    clip = get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    # 删整目录走 rmtree_quiet（见 remove_background 里的批量保护说明）
    rmtree_quiet(frames_dir_for(clip_id, "nobg"))

    # 只清「去背景」这一支的产物，原始帧的 GIF / 拼图必须留着
    for name in (SHEET_NOBG_FILENAME, "clip_nobg.gif"):
        unlink_quiet(frames_dir(clip_id) / name)

    # 库里的 gif_path 若正指向被删掉的透明 GIF，要一并清空，否则下载按钮 404
    gif_rel = str(clip.get("gif_path") or "")
    set_gif = "" if gif_rel.endswith("clip_nobg.gif") else gif_rel
    # 连抠图参数一并复位：回到「没做过去背景」的状态，下次 remove-bg 从默认起步
    db.execute(
        "UPDATE video_clips SET has_nobg=0, gif_path=?, "
        "bg_mode='colorkey', bg_seed='', "
        "bg_color='', bg_similarity=NULL, bg_blend=NULL, "
        "bg_color2='', bg_similarity2=NULL, bg_blend2=NULL, "
        "updated_at=? WHERE id=?",
        (set_gif, now(), clip_id),
    )
    return {"clip_id": int(clip_id), "has_nobg": 0}


# ---------------------------------------------------------------- 序列帧拼图


def build_sheet(
    clip_id: int,
    cols: Any = None,
    padding: Any = None,
    variant: str = "original",
) -> dict[str, Any]:
    """把序列帧拼成一张网格大图（sprite sheet），用于素材总览或导出给别的工具。

    用 ffmpeg ``tile``：一个进程拼完，比「逐帧解码后再拼」快得多，也不引入 Pillow
    之类的新依赖——抽帧、转 GIF 都已经靠 ffmpeg 了，保持一致。

    Returns:
        ``{"clip_id", "cols", "rows", "frames", "bytes", "variant", "path"}``
    """
    clip = get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    v = _norm_variant(variant)
    files = list_frames(clip_id, v)
    if not files:
        raise AIBARError("invalid_input", "还没有序列帧：请先抽帧再拼图")

    use_cols = _clamp_int(cols, DEFAULT_SHEET_COLS, MIN_SHEET_COLS, MAX_SHEET_COLS)
    use_pad = _clamp_int(padding, 0, MIN_SHEET_PADDING, MAX_SHEET_PADDING)
    rows = max(1, int(math.ceil(len(files) / float(use_cols))))

    src_dir = frames_dir_for(clip_id, v)
    out = sheet_file(clip_id, v)
    ffmpeg = _bin("ffmpeg", _FFMPEG_CANDIDATES)
    # tile=CxR：C 列 R 行；padding 是帧与帧之间的留白，margin 是整图外边框
    vf = "tile=%dx%d:padding=%d:margin=%d" % (use_cols, rows, use_pad, use_pad)
    cmd = [
        ffmpeg, "-y",
        "-i", str(src_dir / name_fmt_for(v)),
        "-vf", vf,
        "-frames:v", "1",
    ]
    if v == "nobg":
        # 透明变体必须显式指定，否则留白会被填成黑或白，透明就白抠了
        cmd += ["-pix_fmt", "rgba"]
    cmd += [str(out)]

    try:
        _run(cmd, "build_sheet")
    except AIBARError as exc:
        db.execute(
            "UPDATE video_clips SET status='failed', last_error=?, updated_at=? WHERE id=?",
            (str(exc), now(), clip_id),
        )
        raise

    if not out.is_file():
        raise AIBARError("ffmpeg_failed", "拼图生成失败：未产出文件")

    size = out.stat().st_size
    db.execute(
        "UPDATE video_clips SET has_sheet=1, sheet_cols=?, status='ready', last_error='', updated_at=? WHERE id=?",
        (use_cols, now(), clip_id),
    )
    safe_log(_LOGGER, 20, "video_sheet_built", clip_id=clip_id, cols=use_cols,
             rows=rows, frames=len(files), variant=v)
    try:
        rel = str(out.relative_to(Path(Config.DATA_DIR)))
    except ValueError:
        rel = str(out)
    return {
        "clip_id": int(clip_id), "cols": use_cols, "rows": rows,
        "frames": len(files), "bytes": size, "variant": v, "path": rel,
    }


# ---------------------------------------------------------------- 删除


def delete_clip(clip_id: int) -> bool:
    """删除视频记录 + 源视频（仅当在 data/ 内）+ 帧目录与 GIF。"""
    clip = get_clip(clip_id)
    if not clip:
        return False

    # 源视频：只删 data/ 内的（上传的）；用户指定的本机路径**绝不删**
    src_raw = str(clip.get("source_path") or "")
    src = Path(src_raw)
    if not src.is_absolute():
        src = Path(Config.DATA_DIR) / src_raw
    try:
        data_root = Path(Config.DATA_DIR).resolve()
        if src.is_absolute() and src.resolve().is_relative_to(data_root):
            if src.is_file():
                unlink_quiet(src)
    except Exception as exc:
        safe_log(_LOGGER, 30, "video_src_delete_failed", error_type=type(exc).__name__)

    d = frames_dir(clip_id)
    rmtree_quiet(d)   # 帧目录整树删除走收口，见 _purge_derived 的说明

    cur = db.execute("DELETE FROM video_clips WHERE id=?", (int(clip_id),))
    try:
        return int(cur.rowcount or 0) > 0
    except Exception:
        return True


# ---------------------------------------------------------------- 产物路径（供路由安全读取）


def frame_file(clip_id: int, filename: str, variant: str = "original") -> Path | None:
    """取某一帧的绝对路径；文件名含路径穿越时返回 None。

    variant 决定从原帧目录还是 nobg 目录取，与 list_frames 保持一致。
    """
    from sync.paths import safe_join

    target = safe_join(frames_dir_for(clip_id, variant), filename)
    if target is None or not target.is_file():
        return None
    # 只允许取帧图，不接受 palette / 其它文件
    if target.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        return None
    return target


def gif_file(clip_id: int) -> Path | None:
    """取 GIF 绝对路径（相对路径存库，这里拼回绝对）。"""
    clip = get_clip(clip_id)
    if not clip:
        return None
    rel = str(clip.get("gif_path") or "")
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = Path(Config.DATA_DIR) / rel
    return p if p.is_file() else None


def sheet_output(clip_id: int, variant: str = "original") -> Path | None:
    """取拼图产物绝对路径（还没拼过则返回 None）。"""
    p = sheet_file(clip_id, variant)
    return p if p.is_file() else None


__all__ = [
    "VIDEO_OUTPUT_SUBDIR",
    "VIDEO_SOURCE_SUBDIR",
    "GIF_FILENAME",
    "ALLOWED_VIDEO_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "DEFAULT_FPS",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_SCALE_WIDTH",
    "DEFAULT_BG_COLOR",
    "DEFAULT_BG_SIMILARITY",
    "DEFAULT_BG_BLEND",
    "DEFAULT_SHEET_COLS",
    "VARIANTS",
    "clips_dir",
    "frames_dir",
    "frames_dir_for",
    "glob_for",
    "probe_video",
    "import_video",
    "import_local_path",
    "get_clip",
    "list_clips",
    "list_frames",
    "extract_frames",
    "build_gif",
    "remove_background",
    "clear_background",
    "build_sheet",
    "sheet_file",
    "sheet_output",
    "delete_clip",
    "frame_file",
    "gif_file",
]
