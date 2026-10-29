"""M16 · 视频转绘 service 单测（隔离 DB，不依赖 ComfyUI / ffmpeg）。

覆盖：建任务（参考图落库 + URL 暴露）、行映射（源帧/骨架/生成/去背景 URL）、
read_job_file 的 source / reference / sheet 种类、build_nobg / build_sheet（纯 PIL）、删除清理。

prepare / pose / generate 需要 ffmpeg + ComfyUI，放到 E2E 脚本（/tmp/vp_e2e.py）覆盖。
"""

from __future__ import annotations

import io
import pytest
from pathlib import Path
from PIL import Image

from config import Config
from core import db
from core.db import now
from videopaint import service as svc
import video.service as video_service


pytestmark = pytest.mark.usefixtures("isolated_db")


def _make_clip(tmp_path: Path) -> int:
    # import_local_path 只登记路径、不抽帧，用一个合法后缀的占位文件即可
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"")
    clip = video_service.import_local_path(str(fake), "ut-clip")
    return clip["id"]


def _seed_frames(job_id: int, gen_dir: Path, count: int) -> None:
    gen_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        Image.new("RGB", (16, 16), (i * 10 % 255, i * 40 % 255, i * 70 % 255)).save(gen_dir / (svc.GEN_NAME_FMT % i))
        rel = str((gen_dir / (svc.GEN_NAME_FMT % i)).relative_to(Path(Config.DATA_DIR)))
        db.execute(
            "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, image_path, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(job_id), i, "f", rel, "done", now(), now()),
        )


def test_create_job_copies_reference_and_exposes_urls(isolated_db, tmp_path):
    clip_id = _make_clip(tmp_path)
    ref = tmp_path / "ref.png"
    Image.new("RGB", (32, 32), (10, 20, 30)).save(ref)

    job = svc.create_job({
        "clip_id": clip_id, "name": "ut", "reference_path": str(ref),
        "prompt": "p", "pose_mode": "dwpose",
    })
    assert job["id"]
    assert job["reference_rel"], "参考图应复制到本任务目录"
    assert job["reference_url"].startswith("/api/videopaint/jobs/")
    assert job["status"] == "idle"

    data, mime = svc.read_job_file(job["id"], "reference", 0)
    assert data and mime == "image/png"


def test_create_job_without_reference_has_empty_url(isolated_db, tmp_path):
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "ut2"})
    assert job["reference_rel"] == ""
    assert job["reference_url"] == ""


def test_row_to_frame_maps_all_urls_and_source(isolated_db, tmp_path):
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "ut3"})
    from videopaint.service import job_gen_dir, job_poses_dir
    # 造三个阶段的产物文件，让 URL 非空
    gdir = job_gen_dir(job["id"]); gdir.mkdir(parents=True, exist_ok=True)
    pdir = job_poses_dir(job["id"]); pdir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(gdir / (svc.GEN_NAME_FMT % 1))
    Image.new("RGB", (8, 8), (4, 5, 6)).save(pdir / (svc.POSE_NAME_FMT % 1))
    ndir = svc.job_nobg_dir(job["id"]); ndir.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (8, 8), (7, 8, 9, 0)).save(ndir / (svc.NOBG_NAME_FMT % 1))
    # 源帧（动作拆解前抽帧）也要造，供 read_job_file("source", 1) 读取
    fdir = svc.job_frames_dir(job["id"]); fdir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (255, 0, 0)).save(fdir / (svc.FRAME_NAME_FMT % 1))

    rel_gen = str((gdir / (svc.GEN_NAME_FMT % 1)).relative_to(Path(Config.DATA_DIR)))
    rel_pose = str((pdir / (svc.POSE_NAME_FMT % 1)).relative_to(Path(Config.DATA_DIR)))
    rel_nobg = str((ndir / (svc.NOBG_NAME_FMT % 1)).relative_to(Path(Config.DATA_DIR)))
    db.execute(
        "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, pose_path, image_path, nobg_path, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (int(job["id"]), 1, "f", rel_pose, rel_gen, rel_nobg, "done", now(), now()),
    )
    fr = svc.get_frame(job["id"], 1)
    assert fr["pose_path_url"].endswith("/file/pose/1")
    assert fr["image_path_url"].endswith("/file/image/1")
    assert fr["nobg_path_url"].endswith("/file/nobg/1")
    assert fr["frame_url"].endswith("/file/source/1")
    # 源帧可服务
    data, mime = svc.read_job_file(job["id"], "source", 1)
    assert data and mime == "image/png"


def test_build_nobg_and_sheet(isolated_db, tmp_path):
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "ut4"})
    gen_dir = svc.job_gen_dir(job["id"])
    _seed_frames(job["id"], gen_dir, 2)

    svc.build_nobg(job["id"], mode="flood", seed="0,0", similarity=0.4, blend=0.0)
    assert svc.get_job(job["id"])["has_nobg"] == 1

    sheet = svc.build_sheet(job["id"], cols=2, padding=2, variant="original")
    assert sheet["frames"] == 2
    data, mime = svc.read_job_file(job["id"], "sheet", 0)
    assert data and mime == "image/png"

    # 去背景变体拼图
    sheet2 = svc.build_sheet(job["id"], cols=2, padding=2, variant="nobg")
    assert sheet2["variant"] == "nobg"
    data2, _ = svc.read_job_file(job["id"], "sheet", 1)
    assert data2


def test_delete_job_cleans_group_and_dir(isolated_db, tmp_path):
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "ut5"})
    job_dir = svc.job_dir(job["id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "reference.png").write_bytes(b"x")

    res = svc.delete_job(job["id"])
    assert res.get("ok") is True
    assert svc.get_job(job["id"]) is None
    assert not job_dir.exists()


def _pose_png(points, size=(400, 200)) -> bytes:
    image = Image.new("RGB", size, (0, 0, 0))
    for x, y, color in points:
        for xx in range(max(0, x - 3), min(size[0], x + 4)):
            for yy in range(max(0, y - 3), min(size[1], y + 4)):
                image.putpixel((xx, yy), color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_pose_normalization_preserves_wide_extremities_and_matches_target_canvas():
    raw = _pose_png([
        (5, 100, (255, 0, 0)),
        (200, 20, (0, 255, 0)),
        (394, 100, (0, 0, 255)),
    ])
    normalized = svc._normalize_pose_bytes(raw, 896, 1152)
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == (896, 1152)
        colors = list(image.getdata())
        # 横向伸展的左右端点都必须保留，不能像 ComfyUI center crop 那样切掉一侧。
        assert any(r > 100 and g < 80 and b < 80 for r, g, b in colors)
        assert any(b > 100 and r < 80 and g < 80 for r, g, b in colors)


def test_pose_normalization_rejects_empty_control_image():
    empty = _pose_png([])
    with pytest.raises(ValueError, match="骨架图为空"):
        svc._normalize_pose_bytes(empty, 896, 1152)


def test_pose_similarity_accepts_same_pose_and_rejects_different_pose():
    a = _pose_png([(50, 50, (255, 0, 0)), (100, 100, (0, 255, 0)), (150, 150, (0, 0, 255))], (200, 200))
    b = _pose_png([(50, 150, (255, 0, 0)), (100, 100, (0, 255, 0)), (150, 50, (0, 0, 255))], (200, 200))
    assert svc._pose_similarity(a, a) > 0.95
    assert svc._pose_similarity(a, b) < svc.STRICT_POSE_PASS_SCORE


def test_strict_pose_prompt_blocks_cover_and_multi_character_composition():
    positive, negative = svc._strict_pose_prompts({"prompt": "red hair", "negative": "lowres"})
    assert positive.startswith("solo, one person")
    assert "full body" in positive and positive.endswith("red hair")
    assert "multiple people" in negative
    assert "book cover" in negative and negative.endswith("lowres")


def test_dwpose_graph_enables_xinsir_stick_scaling():
    graph = svc._build_dwpose_graph("x.png", "DWPreprocessor", 512, xinsir_controlnet=True)
    assert graph["2"]["inputs"]["scale_stick_for_xinsr_cn"] == "enable"


def test_pose_control_graph_guard_rejects_disconnected_sampler():
    graph = svc.comic_pose.build_pose_workflow(pose_image="pose.png", controlnet_strength=1.15)
    svc._assert_pose_control_graph(graph, "pose.png")

    graph["13"]["inputs"]["positive"] = ["9", 0]
    with pytest.raises(Exception, match="骨架控制链未正确接入"):
        svc._assert_pose_control_graph(graph, "pose.png")


def test_generate_uses_zimage_pose_workflow(monkeypatch, tmp_path):
    """页面的批量 generate 必须提交 Z-Image 图，不能只新增一个编辑器按钮。"""
    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    pose_path = tmp_path / "poses" / "pose.png"
    pose_path.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (0, 255, 0)).save(pose_path)
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 40, 50)).save(out, format="PNG")

    job = {
        "id": 7, "group_id": 8, "prompt": "anime hero", "negative": "lowres",
        "width": 64, "height": 64, "base_seed": 100, "seed_step": 3,
        "controlnet_strength": 1.15, "pose_mode": "dwpose", "pose_resolution": 512,
        "controlnet": "",
    }
    frame = {
        "id": 9, "order_idx": 2, "status": "posed", "pose_path": "poses/pose.png",
        "shot_frame_id": 10,
    }
    captured = {}
    real_build = svc.videopaint_zimage.build_zimage_pose_workflow

    def spy_build(**kwargs):
        captured.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(svc, "get_job", lambda _job_id: job)
    monkeypatch.setattr(svc, "list_frames", lambda _job_id: [frame])
    monkeypatch.setattr(svc.comfyui_client, "is_reachable", lambda: (True, ""))
    monkeypatch.setattr(svc.comfyui_client, "upload_image", lambda *args, **kwargs: {})
    monkeypatch.setattr(svc.videopaint_zimage, "build_zimage_pose_workflow", spy_build)
    monkeypatch.setattr(
        svc.comic_pose, "build_pose_workflow",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应调用 SDXL 构图器")),
    )
    monkeypatch.setattr(svc.comic_runner, "run_graph_once", lambda *args, **kwargs: (out.getvalue(), "", ""))
    monkeypatch.setattr(svc, "_verify_generated_pose", lambda *args, **kwargs: (None, ""))
    monkeypatch.setattr(svc.db, "execute", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc.comic_groups, "_save_frame_image", lambda *args, **kwargs: "shots/z.png")
    monkeypatch.setattr(svc.comic_groups, "_sync_counts", lambda *_args: None)

    svc.generate(7)

    assert captured["pose_image"] == "aibar_vp_pose_7_2.png"
    assert captured["seed"] == 106
    assert captured["strength"] == 1.0  # 旧 SDXL 任务的 1.15 会映射到 Z-Image 合法上限
    assert captured["positive"].startswith("solo, one person")
