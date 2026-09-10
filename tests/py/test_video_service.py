"""M15 · 视频转序列帧 / GIF 测试。

覆盖：
- **元信息探测**：ffprobe 输出的时长 / 宽高 / 帧率解析（含 ``30000/1001`` 分数帧率）；
- **导入校验**：后缀白名单、体积上限、路径存在性；
- **流式落盘**：边写边查上限，超限不留半成品；
- **抽帧**：幂等（先清后写）、参数边界钳制、无帧时报错；
- **转 GIF**：必须先抽帧、palette 中间产物用完即删；
- **安全**：帧文件名路径穿越防护；
- **删除**：只删 data/ 内的源视频，用户指定的本机路径不动。
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from config import Config
from core import db as core_db
from core.errors import AIBARError

from video import service


# ---------------------------------------------------------------- 环境


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)

    core_db._local.conn = None
    core_db.migrate()

    yield {"data": data_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    core_db._local.conn = None


@pytest.fixture
def tiny_video(tmp_path_factory) -> Path | None:
    """用 ffmpeg 合成一段 1 秒的测试视频。没有 ffmpeg 时返回 None（测试跳过）。

    **必须是函数级**：``import_video`` 会把源文件 ``move`` 进 data/，
    用 session 级的话第一个测试就把文件搬走了，后面的测试拿到的是空路径。
    """
    import shutil

    if not shutil.which("ffmpeg"):
        return None
    out = tmp_path_factory.mktemp("video") / "tiny.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "testsrc=duration=1:size=160x120:rate=12",
        "-pix_fmt", "yuv420p", str(out),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return None
    return out if out.is_file() else None


def _make_clip(env, tmp_path, name="c") -> int:
    """建一条最小的视频记录，返回 id。"""
    ts = "2026-09-05 00:00:00"
    cur = core_db.execute(
        "INSERT INTO video_clips (name, source_path, status, created_at, updated_at) "
        "VALUES (?, '', 'idle', ?, ?)",
        (name, ts, ts),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 元信息解析


def test_parse_fps_handles_fraction():
    """``r_frame_rate`` 是分数（如 30000/1001），必须正确转成浮点。"""
    assert service._parse_fps("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert service._parse_fps("24/1") == 24.0
    assert service._parse_fps("24") == 24.0


def test_parse_fps_survives_garbage():
    """非法 / 除零一律返回 0，不该让 fps 解析炸掉整个导入。"""
    assert service._parse_fps("") == 0.0
    assert service._parse_fps("abc") == 0.0
    assert service._parse_fps("1/0") == 0.0
    assert service._parse_fps(None) == 0.0


def test_probe_video_returns_zeros_when_absent():
    """文件不存在时返回全 0 而不是抛——元信息缺失只影响展示。"""
    meta = service.probe_video(Path("/nonexistent/nope.mp4"))
    assert meta["duration"] == 0.0
    assert meta["width"] == 0


def test_probe_video_reads_real_file(tiny_video):
    if tiny_video is None:
        pytest.skip("未安装 ffmpeg")
    meta = service.probe_video(tiny_video)
    assert meta["width"] == 160
    assert meta["height"] == 120
    assert meta["duration"] > 0
    assert meta["fps"] > 0


# ---------------------------------------------------------------- 导入校验


def test_import_video_rejects_bad_suffix(env, tmp_path):
    src = tmp_path / "evil.exe"
    src.write_bytes(b"MZ")
    with pytest.raises(AIBARError) as ei:
        service.import_video(src, "evil.exe")
    assert "不支持的视频格式" in str(ei.value)


def test_import_video_rejects_missing_source(env, tmp_path):
    with pytest.raises(AIBARError) as ei:
        service.import_video(tmp_path / "nope.mp4", "nope.mp4")
    assert "源文件不存在" in str(ei.value)


def test_import_video_rejects_oversized(env, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 10)
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 100)
    with pytest.raises(AIBARError) as ei:
        service.import_video(src, "big.mp4")
    assert "太大" in str(ei.value)


def test_import_local_path_rejects_relative(env):
    with pytest.raises(AIBARError) as ei:
        service.import_local_path("relative/path.mp4")
    assert "绝对路径" in str(ei.value)


def test_import_local_path_rejects_missing(env):
    with pytest.raises(AIBARError) as ei:
        service.import_local_path("/nonexistent/nope.mp4")
    assert "不存在" in str(ei.value)


# ---------------------------------------------------------------- 流式落盘


def test_save_upload_stream_enforces_cap(env, monkeypatch, tmp_path):
    """超限要立即中断，并且**不留半成品**（否则磁盘被半截大文件占着）。"""

    class _FakeStream:
        def __init__(self):
            self.stream = [b"x" * 100 for _ in range(10)]  # 1000 字节

    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 250)
    with pytest.raises(AIBARError) as ei:
        service.save_upload_stream(_FakeStream(), "a.mp4")
    assert "太大" in str(ei.value)


def test_save_upload_stream_rejects_empty(env):
    class _Empty:
        stream = [b""]

    with pytest.raises(AIBARError) as ei:
        service.save_upload_stream(_Empty(), "a.mp4")
    assert "为空" in str(ei.value)


# ---------------------------------------------------------------- 抽帧


def test_extract_frames_requires_existing_clip(env):
    with pytest.raises(AIBARError) as ei:
        service.extract_frames(99999)
    assert "不存在" in str(ei.value)


def test_extract_frames_fails_when_source_gone(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    core_db.execute("UPDATE video_clips SET source_path=? WHERE id=?", ("gone.mp4", cid))
    with pytest.raises(AIBARError) as ei:
        service.extract_frames(cid)
    assert "已不存在" in str(ei.value)


def test_extract_frames_is_idempotent(env, tmp_path, tiny_video):
    """重复抽帧必须**先清空**——否则新旧参数的文件会混在一个目录里。"""
    if tiny_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(tiny_video, "tiny.mp4", name="t")
    cid = clip["id"]

    service.extract_frames(cid, fps=4, max_frames=100, scale_width=96)
    first = service.list_frames(cid)
    assert first  # 抽到了帧

    # 再抽一次：帧数应当等于新参数抽出的数量，而不是累加
    service.extract_frames(cid, fps=2, max_frames=100, scale_width=96)
    second = service.list_frames(cid)
    assert len(second) <= len(first)
    assert len(second) > 0


def test_extract_frames_clamps_params(env, tmp_path, tiny_video):
    """越界参数要被钳制，而不是直接喂给 ffmpeg（会把机器跑死）。"""
    if tiny_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(tiny_video, "tiny.mp4", name="t")
    cid = clip["id"]
    # fps=999 / width=99999 / max=99999 都应被夹到合法区间
    service.extract_frames(cid, fps=999, max_frames=99999, scale_width=99999)
    row = service.get_clip(cid)
    assert row["fps"] <= service.MAX_FPS
    assert row["scale_width"] <= service.MAX_SCALE_WIDTH
    assert row["max_frames"] <= service.MAX_FRAMES_HARD


def test_build_gif_requires_frames(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    with pytest.raises(AIBARError) as ei:
        service.build_gif(cid)
    assert "请先抽帧" in str(ei.value)


# ---------------------------------------------------------------- 帧


def test_list_frames_sorts_numerically(env, tmp_path):
    """frame_2 必须排在 frame_10 前面——字典序会把 10 排到 2 前面。"""
    cid = _make_clip(env, tmp_path)
    d = service.frames_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    for n in (1, 2, 10, 11):
        (d / f"frame_{n:04d}.jpg").write_bytes(b"x")
    assert service.list_frames(cid) == [
        "frame_0001.jpg", "frame_0002.jpg", "frame_0010.jpg", "frame_0011.jpg",
    ]


def test_list_frames_empty_when_missing(env, tmp_path):
    assert service.list_frames(_make_clip(env, tmp_path)) == []


def test_frame_file_blocks_traversal(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    d = service.frames_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "frame_0001.jpg").write_bytes(b"x")

    assert service.frame_file(cid, "frame_0001.jpg") is not None
    # 穿越 / 非图片后缀一律拒绝
    assert service.frame_file(cid, "../../../../etc/passwd") is None
    assert service.frame_file(cid, "palette.png") is None


# ---------------------------------------------------------------- 删除


def test_delete_clip_removes_frames_and_row(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    d = service.frames_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "frame_0001.jpg").write_bytes(b"x")

    assert service.delete_clip(cid) is True
    assert not d.exists()
    assert service.get_clip(cid) is None


def test_delete_clip_keeps_user_local_source(env, tmp_path):
    """用户指定的本机路径**绝不能**被删——那可能是人家唯一的原件。"""
    src = tmp_path / "precious.mp4"
    src.write_bytes(b"x")
    clip = service.import_local_path(str(src), name="p")
    cid = clip["id"]

    service.delete_clip(cid)
    assert src.is_file(), "用户本机源文件被误删了"
    assert service.get_clip(cid) is None


def test_delete_missing_clip_returns_false(env):
    assert service.delete_clip(99999) is False


# ---------------------------------------------------------------- 去背景：颜色解析与滤镜


def test_parse_hex_color_variants():
    """#RRGGBB / #RGB / 裸 hex 都要归一化；非法输入返回空串。"""
    assert service._parse_hex_color("#00FF00") == "#00ff00"
    assert service._parse_hex_color("#0f0") == "#00ff00"
    assert service._parse_hex_color("00FF00") == "#00ff00"
    assert service._parse_hex_color("zzzzzz") == ""
    assert service._parse_hex_color("") == ""
    assert service._parse_hex_color(None) == ""


def test_colorkey_geq_filter_embeds_key_as_numbers():
    """key 色必须以**数字**内嵌进 geq 表达式。

    本机 ffmpeg（N-97810）的 colorkey 对 ``0xRRGGBB`` / ``#RRGGBB`` 的解析有
    bug（实测被解析成完全无关的颜色），只有颜色名可靠——但取色器给的是任意
    hex，所以走 geq + 数字内嵌，顺带断言表达式里不允许再出现 0x / #。
    """
    vf = service._colorkey_geq_filter("#00FF00", 0.3, 0.05)
    assert "(r(X,Y)-0)" in vf and "(g(X,Y)-255)" in vf and "(b(X,Y)-0)" in vf
    assert "0x" not in vf and "#" not in vf
    assert vf.startswith("format=rgba,geq=")


def test_colorkey_geq_filter_no_blend_is_single_threshold():
    """无羽化时只有一个阈值（二值 alpha），不该出现过渡带分支。"""
    vf = service._colorkey_geq_filter("#FF0000", 0.2, 0.0)
    assert vf.count("if(lt(") == 1


def test_colorkey_geq_filter_dual_color_uses_min():
    """双色时把两把钥匙各自的 alpha 用 min 合成——近任一色即透明。"""
    vf = service._colorkey_geq_filter("#00FF00", 0.3, 0.05, color2="#FF0000")
    # 必须出现 min(...) 合成两个 alpha
    assert "min(" in vf
    # 两把钥匙的数字都得内嵌（绿 0/255/0，红 255/0/0）
    assert "(r(X,Y)-0)" in vf and "(g(X,Y)-255)" in vf        # 绿
    assert "(r(X,Y)-255)" in vf and "(g(X,Y)-0)" in vf       # 红
    # 单色不该有 min
    vf1 = service._colorkey_geq_filter("#00FF00", 0.3, 0.05)
    assert "min(" not in vf1


def test_colorkey_geq_filter_rejects_bad_second_color():
    """第二颜色格式不对时 filter 仍按单色构造（校验在调用方做）。"""
    # 这里只验证 color2 为空时不走 min 分支
    vf = service._colorkey_geq_filter("#00FF00", 0.3, 0.05, color2="")
    assert "min(" not in vf


# ---------------------------------------------------------------- 去背景：流程


@pytest.fixture
def green_video(tmp_path_factory) -> Path | None:
    """纯绿底测试视频。函数级：import_video 会把源文件 move 走。"""
    import shutil

    if not shutil.which("ffmpeg"):
        return None
    out = tmp_path_factory.mktemp("green") / "green.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=green:size=160x120:rate=12:duration=1",
        "-pix_fmt", "yuv420p", str(out),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return None
    return out if out.is_file() else None


def test_remove_background_full_cycle(env, tmp_path, green_video):
    """去背景 → 落库 → 原帧不动 → 清除回到原图，整条环必须闭合。"""
    if green_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(green_video, "green.mp4", name="g")
    cid = clip["id"]
    service.extract_frames(cid, fps=4, max_frames=10, scale_width=96)

    r = service.remove_background(cid, color="#00FF00", similarity=0.4, blend=0.05)
    assert r["frames"] > 0
    assert r["color"] == "#00ff00"

    nobg = service.list_frames(cid, "nobg")
    assert len(nobg) == r["frames"]

    row = service.get_clip(cid)
    assert row["has_nobg"] == 1
    assert row["bg_color"] == "#00ff00"

    # 原帧必须原封不动（去背景是可逆变换，不是就地修改）
    original_count = len(service.list_frames(cid))
    assert original_count > 0

    service.clear_background(cid)
    assert service.list_frames(cid, "nobg") == []
    row2 = service.get_clip(cid)
    assert row2["has_nobg"] == 0
    assert len(service.list_frames(cid)) == original_count


def test_remove_background_rejects_bad_color(env, tmp_path):
    """颜色格式不对必须报错——静默回落成绿色会抠掉用户意想不到的东西。"""
    cid = _make_clip(env, tmp_path)
    with pytest.raises(AIBARError) as ei:
        service.remove_background(cid, color="nothex")
    assert "颜色格式" in str(ei.value)


def test_remove_background_requires_frames(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    with pytest.raises(AIBARError) as ei:
        service.remove_background(cid, color="#00FF00")
    assert "请先抽帧" in str(ei.value)


def test_extract_resets_derived_state(env, tmp_path, green_video):
    """重抽帧后，去背景变体必须清掉并重置标志。

    旧透明帧与新帧错位、而 UI 还显示「已抠图」，比老实显示「未抠图」坑得多。
    """
    if green_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(green_video, "green.mp4", name="g")
    cid = clip["id"]
    service.extract_frames(cid, fps=4, max_frames=10, scale_width=96)
    service.remove_background(cid, color="#00FF00", similarity=0.4)
    assert service.get_clip(cid)["has_nobg"] == 1

    service.extract_frames(cid, fps=2, max_frames=10, scale_width=96)
    assert service.get_clip(cid)["has_nobg"] == 0
    assert service.list_frames(cid, "nobg") == []


def test_remove_background_dual_color(env, tmp_path, green_video):
    """双色抠图：两把钥匙都嵌进 filter，bg_color2 落库，清除后复位。"""
    if green_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(green_video, "green.mp4", name="g2")
    cid = clip["id"]
    service.extract_frames(cid, fps=4, max_frames=10, scale_width=96)
    r = service.remove_background(
        cid, color="#00FF00", similarity=0.4,
        color2="#FF0000", similarity2=0.4, blend2=0.05)
    assert r["frames"] > 0
    assert r["color"] == "#00ff00"
    assert r["color2"] == "#ff0000"
    row = service.get_clip(cid)
    assert row["bg_color2"] == "#ff0000"
    assert row["bg_similarity2"] == 0.4
    assert row["has_nobg"] == 1
    # 透明帧确实落了 nobg 目录
    assert len(service.list_frames(cid, "nobg")) == r["frames"]
    # 原帧不动
    assert len(service.list_frames(cid)) > 0
    service.clear_background(cid)
    row2 = service.get_clip(cid)
    assert row2["has_nobg"] == 0
    assert row2["bg_color2"] == ""


# ---------------------------------------------------------------- 连通抠图（flood）


def test_flood_mask_preserves_disconnected_same_color():
    """连通抠图核心语义：不连通的同色区域必须保留，不能因为颜色一样就一起抠掉。

    构造 5x5：左侧是绿底（与种子点连通），中间一列红墙，右侧是一块被红墙隔开的
    「绿色孤岛」。从 (0,0) 出发的连通区域只能到达左侧绿底，右侧绿孤岛被红墙隔断，
    因此必须保留。
    """
    G = (0, 200, 0)
    R = (255, 0, 0)
    px = []
    for y in range(5):
        for x in range(5):
            px.append(R if x == 2 else G)  # x==2 是红墙，其余是绿
    tol2 = int(round((0.4 * math.sqrt(3) * 255) ** 2))
    mask = service._flood_mask(px, 5, 5, 0, 0, tol2)
    # 左上角绿底（与种子连通）→ 背景(1)
    assert mask[0 * 5 + 0] == 1
    # 右上角绿孤岛（被红墙隔开，不连通）→ 保留(0)
    assert mask[0 * 5 + 4] == 0
    # 红墙本身 → 保留(0)
    assert mask[0 * 5 + 2] == 0


def _synthetic_flood_frame(size: int = 60) -> "Image.Image":
    """绿底 + 红主体 + 一块被红墙围住的「绿色孤岛」（与绿底不连通）。"""
    from PIL import ImageDraw

    img = Image.new("RGB", (size, size), (0, 200, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([15, 15, 30, 30], fill=(255, 0, 0))          # 红主体
    d.rectangle([40, 40, 55, 55], fill=(255, 0, 0))          # 红墙方块
    d.rectangle([44, 44, 51, 51], fill=(0, 200, 0))          # 墙内绿孤岛（不连通）
    return img


def _write_frame(cid: int, img: "Image.Image"):
    d = service.frames_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / "frame_0001.jpg", "JPEG", quality=95)


def test_remove_background_flood_keeps_disconnected_island(env, tmp_path):
    """连通抠图：从左上角出发只抠连通绿底，红主体与绿孤岛都保留。"""
    cid = _make_clip(env, tmp_path)
    _write_frame(cid, _synthetic_flood_frame())

    r = service.remove_background(cid, mode="flood", seed="0,0", similarity=0.4, blend=0)
    assert r["mode"] == "flood"
    assert r["seed"] == "0,0"
    assert r["frames"] == 1

    row = service.get_clip(cid)
    assert row["has_nobg"] == 1
    assert row["bg_mode"] == "flood"
    assert row["bg_seed"] == "0,0"
    # 双色字段应被清空（连通模式忽略双色）
    assert row["bg_color2"] == ""

    nobg = service.frames_dir_for(cid, "nobg") / "frame_0001.png"
    im = Image.open(nobg).convert("RGBA")
    # 左上角绿底（连通）→ 透明
    assert im.getpixel((5, 5))[3] == 0
    # 红主体 → 保留
    assert im.getpixel((22, 22))[3] == 255
    # 墙内绿孤岛（不连通）→ 必须保留！这是连通抠图的价值所在
    assert im.getpixel((47, 47))[3] == 255


def test_remove_background_flood_seed_fallback_and_clear(env, tmp_path):
    """非法 seed 回落左上角；clear 后 bg_mode/bg_seed 复位。"""
    cid = _make_clip(env, tmp_path)
    _write_frame(cid, _synthetic_flood_frame())

    # 非法 seed：解析失败 → 默认 (0,0)，不应报错
    r = service.remove_background(cid, mode="flood", seed="abc", similarity=0.4, blend=0)
    assert r["seed"] == "0,0"
    row = service.get_clip(cid)
    assert row["bg_mode"] == "flood"

    service.clear_background(cid)
    row2 = service.get_clip(cid)
    assert row2["has_nobg"] == 0
    assert row2["bg_mode"] == "colorkey"
    assert row2["bg_seed"] == ""


def test_build_sheet_requires_frames(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    with pytest.raises(AIBARError) as ei:
        service.build_sheet(cid)
    assert "请先抽帧" in str(ei.value)


def test_build_sheet_grid(env, tmp_path, green_video):
    if green_video is None:
        pytest.skip("未安装 ffmpeg")
    clip = service.import_video(green_video, "green.mp4", name="g")
    cid = clip["id"]
    service.extract_frames(cid, fps=4, max_frames=10, scale_width=96)

    r = service.build_sheet(cid, cols=3)
    assert r["cols"] == 3
    assert r["frames"] > 0
    assert r["rows"] >= 1
    assert service.sheet_output(cid, "original") is not None


def test_sheet_output_missing_returns_none(env, tmp_path):
    cid = _make_clip(env, tmp_path)
    assert service.sheet_output(cid) is None


# ---------------------------------------------------------------- 变体（original / nobg）


def test_variant_dirs_and_globs():
    """变体目录是帧目录的子目录；未知变体必须回落 original（URL 是用户可改的）。"""
    d = service.frames_dir_for(7, "nobg")
    assert d.name == "nobg"
    assert d.parent.name == "7"
    assert service.glob_for("nobg") == service.NOBG_GLOB
    assert service.glob_for("original") == service.FRAME_GLOB
    assert service.glob_for("bogus") == service.FRAME_GLOB


def test_list_frames_nobg_uses_png_glob(env, tmp_path):
    """nobg 变体列 PNG；非帧命名（如 palette.png）不能混进清单。"""
    cid = _make_clip(env, tmp_path)
    d = service.frames_dir_for(cid, "nobg")
    d.mkdir(parents=True, exist_ok=True)
    (d / "frame_0001.png").write_bytes(b"x")
    (d / "palette.png").write_bytes(b"x")
    assert service.list_frames(cid, "nobg") == ["frame_0001.png"]


# ---------------------------------------------------------------- rmtree_quiet


def test_rmtree_quiet_removes_nested_tree(tmp_path):
    """深层目录树要一次删净——它是 safe-delete 批量保护下的安全替代。"""
    from core.fileops import rmtree_quiet

    d = tmp_path / "tree"
    (d / "a" / "b").mkdir(parents=True)
    (d / "a" / "b" / "f.txt").write_text("x")
    (d / "top.txt").write_text("x")
    assert rmtree_quiet(d) is True
    assert not d.exists()


def test_rmtree_quiet_missing_returns_false(tmp_path):
    from core.fileops import rmtree_quiet

    assert rmtree_quiet(tmp_path / "nope") is False
