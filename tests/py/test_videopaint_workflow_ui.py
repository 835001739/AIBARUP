"""M16 · 视频转绘「工作流按钮」单测。

覆盖：
- comic.workflow_ui.api_to_ui_graph 把姿态 API 图无损转成 ComfyUI 编辑器 UI 图
  （以 sync.workflow_convert.convert_ui_to_api 往返自校验，节点布局对齐真实保存格式）；
- videopaint.service.build_frame_pose_ui_graph / frame_editor_link 的接线
  （mock ComfyUI 上传，验证 UI 图结构与深链接 URL）。

不依赖真实 ComfyUI：is_reachable / upload_image 均被 mock。
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from PIL import Image

from comic import flux2 as comic_flux2
from comic import pose as comic_pose
from comic import workflow_ui as workflow_ui
from config import Config
from core import db
from core.db import now
from videopaint import service as svc
import video.service as video_service


pytestmark = pytest.mark.usefixtures("isolated_db")


def _make_clip(tmp_path: Path) -> int:
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"")
    clip = video_service.import_local_path(str(fake), "ut-clip")
    return clip["id"]


def _seed_job_and_frame(tmp_path: Path) -> tuple[int, int]:
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "wf-ut", "prompt": "1girl", "negative": "lowres"})
    jid = job["id"]
    # 补上 generate 所需的参数
    db.execute(
        "UPDATE video_paint_jobs SET group_id=1, reference_name='aibar_vp_ref_%d.png', "
        "checkpoint='animagine_xl_3.1.safetensors', controlnet='xinsir_controlnet-union-sdxl-1.0.safetensors', "
        "width=896, height=1152, steps=28, cfg=6.5, controlnet_strength=0.9, "
        "ipadapter_weight=0.85, faceidv2_weight=0.85, base_seed=1000, seed_step=7 WHERE id=?" % jid,
        (int(jid),),
    )
    # 造一个真实骨架图文件，让 pose_path 指向它
    pdir = svc.job_poses_dir(jid)
    pdir.mkdir(parents=True, exist_ok=True)
    pose_file = pdir / (svc.POSE_NAME_FMT % 1)
    pose = Image.new("RGB", (16, 16), (0, 0, 0))
    pose.putpixel((8, 8), (255, 0, 0))
    pose.save(pose_file)
    pose_rel = str(pose_file.relative_to(Path(Config.DATA_DIR)))
    db.execute(
        "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, pose_path, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(jid), 1, "f", pose_rel, "posed", now(), now()),
    )
    return jid, 1


def test_api_to_ui_roundtrip_full():
    api = comic_pose.build_pose_workflow(
        reference_image="aibar_vp_ref_1.png",
        pose_image="aibar_vp_pose_1_0.png",
        positive="1girl", negative="lowres",
        seed=12345, steps=28, cfg=6.5,
    )
    r = workflow_ui.validate_roundtrip(api)
    assert r["ok"], r["error"]


def test_api_to_ui_roundtrip_minimal():
    api = comic_pose.build_pose_workflow(positive="x", negative="y", seed=1)
    r = workflow_ui.validate_roundtrip(api)
    assert r["ok"], r["error"]


def test_api_to_ui_node_layout_matches_comfyui():
    """UI 图结构需与 ComfyUI 真实保存格式一致（端口顺序 / widgets_values）。"""
    api = comic_pose.build_pose_workflow(
        reference_image="aibar_vp_ref_1.png",
        pose_image="aibar_vp_pose_1_0.png",
        positive="1girl", negative="lowres",
    )
    ui = workflow_ui.api_to_ui_graph(api)
    nodes = {n["id"]: n for n in ui["nodes"]}
    # KSampler：10 个端口（4 连线 + 6 控件），6 个 widgets_values
    ks = next(n for n in ui["nodes"] if n["type"] == "KSampler")
    assert len(ks["inputs"]) == 10
    assert sum(1 for i in ks["inputs"] if i.get("link") is not None) == 4
    assert len(ks["widgets_values"]) == 6
    # 每个节点都有合法的 inputs / outputs / widgets_values
    for n in ui["nodes"]:
        assert isinstance(n["inputs"], list)
        assert isinstance(n["outputs"], list)
        assert isinstance(n["widgets_values"], list)
    # 顶层字段齐备
    assert ui["last_node_id"] >= max(nodes)
    assert ui["version"] == 0.4
    assert isinstance(ui["links"], list)
    # 每条 link 元组：[id, origin_id, origin_slot, target_id, target_slot, type]
    for link in ui["links"]:
        assert len(link) == 6
        assert link[1] in nodes and link[3] in nodes


def test_build_frame_pose_ui_graph_uploads_and_builds(monkeypatch, tmp_path):
    jid, oidx = _seed_job_and_frame(tmp_path)
    uploaded = []

    def fake_reachable():
        return (True, 0)

    def fake_upload(path, name, overwrite=True):
        uploaded.append((Path(path).name, name))
        return True

    monkeypatch.setattr(svc.comfyui_client, "is_reachable", fake_reachable)
    monkeypatch.setattr(svc.comfyui_client, "upload_image", fake_upload)

    info = svc.build_frame_pose_ui_graph(jid, oidx)
    ui = info["ui"]
    assert info["pose_name"] == "aibar_vp_pose_%d_%d.png" % (jid, oidx)
    # 骨架图应按约定文件名上传到 ComfyUI input
    assert any(n == "aibar_vp_pose_%d_%d.png" % (jid, oidx) for _, n in uploaded)
    # UI 图可直接被往返还原
    r = workflow_ui.validate_roundtrip(comic_pose.build_pose_workflow(
        reference_image=info["ref_name"], pose_image=info["pose_name"],
        positive="1girl", negative="lowres",
        seed=info["seed"], steps=28, cfg=6.5,
    ))
    assert r["ok"], r["error"]
    assert any(n["type"] == "KSampler" for n in ui["nodes"])


def test_build_frame_pose_ui_graph_requires_pose(monkeypatch, tmp_path):
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "wf-ut2"})
    jid = job["id"]
    db.execute("UPDATE video_paint_jobs SET group_id=1 WHERE id=?", (int(jid),))
    # 帧没有 pose_path → 应报 invalid_input
    db.execute(
        "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (int(jid), 1, "f", "pending", now(), now()),
    )
    with pytest.raises(Exception):
        svc.build_frame_pose_ui_graph(jid, 1)


def test_frame_editor_link_url_shape(monkeypatch, tmp_path):
    jid, oidx = _seed_job_and_frame(tmp_path)
    monkeypatch.setattr(svc.comfyui_client, "is_reachable", lambda: (True, 0))
    monkeypatch.setattr(svc.comfyui_client, "upload_image", lambda *a, **k: True)

    link = svc.frame_editor_link(jid, oidx)
    assert link["mode"] == "graph"
    assert "aibar_wf=" in link["url"]
    # aibar_wf 参数值指向本任务某帧的 workflow-graph 直链（落在 AIBAR :8099）
    assert "/api/videopaint/jobs/%d/frames/%d/workflow-graph" % (jid, oidx) in link["url"]
    assert "aibar_name=" in link["url"]
    # 正向提示词应作为 aibar_prompt 传递
    assert "aibar_prompt=" in link["url"]
    assert link["target"] == "9"


# --------------------------------------------------------------------------- M17 · FLUX.2 Klein 并列按钮
_OI_CACHE = Path("/tmp/oi.json")  # 本机 ComfyUI v0.30.0 的 /object_info 快照（离线时用）


def _fake_object_info():
    """返回缓存的 object_info；没有缓存就退化成空 dict（相关断言自动跳过）。"""
    if not _OI_CACHE.exists():
        return {}
    try:
        return json.loads(_OI_CACHE.read_text())
    except Exception:
        return {}


def _patch_comfyui(monkeypatch):
    monkeypatch.setattr(svc.comfyui_client, "is_reachable", lambda: (True, 0))
    monkeypatch.setattr(svc.comfyui_client, "upload_image", lambda *a, **k: True)
    monkeypatch.setattr(svc.comic_workflow_ui, "_object_info_all", _fake_object_info)


def test_build_frame_flux2_ui_graph_uploads_and_builds(monkeypatch, tmp_path):
    jid, oidx = _seed_job_and_frame(tmp_path)
    uploaded = []

    monkeypatch.setattr(svc.comfyui_client, "is_reachable", lambda: (True, 0))

    def fake_upload(path, name, overwrite=True):
        uploaded.append((Path(path).name, name))
        return True

    monkeypatch.setattr(svc.comfyui_client, "upload_image", fake_upload)
    monkeypatch.setattr(svc.comic_workflow_ui, "_object_info_all", _fake_object_info)

    info = svc.build_frame_flux2_ui_graph(jid, oidx)
    # 骨架图（结构控制）与参考图（外观）都要在 ComfyUI input 里，否则画布上是红框
    assert info["pose_name"] == "aibar_vp_pose_%d_%d.png" % (jid, oidx)
    assert any(n == "aibar_vp_pose_%d_%d.png" % (jid, oidx) for _, n in uploaded)
    assert info["ref_name"] == "aibar_vp_ref_%d.png" % jid

    ui = info["ui"]
    types = [n["type"] for n in ui["nodes"]]
    # Klein 链路的关键节点：不是 SDXL 的 CheckpointLoader / ControlNetApply
    assert "UNETLoader" in types
    assert "EmptyFlux2LatentImage" in types
    assert "Flux2Scheduler" in types
    assert "SamplerCustomAdvanced" in types
    assert "CheckpointLoaderSimple" not in types
    assert "ControlNetApplyAdvanced" not in types
    # 4 个 ReferenceLatent = 参考图链(正/负) + 结构控制链(正/负)
    assert sum(1 for t in types if t == "ReferenceLatent") == 4
    # 蒸馏版走 CFGGuider（保留负向），不是 BasicGuider
    assert "CFGGuider" in types
    assert "BasicGuider" not in types


def test_build_frame_flux2_uses_distilled_sampling_params(monkeypatch, tmp_path):
    """Klein 蒸馏版必须 4 步 / cfg 1.0 —— 不能继承任务的 SDXL 参数（28 步 / cfg 6.5）。"""
    jid, oidx = _seed_job_and_frame(tmp_path)
    _patch_comfyui(monkeypatch)

    captured = {}
    real_build = svc.comic_flux2.build_flux2_workflow

    def spy_build(**kwargs):
        captured.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(svc.comic_flux2, "build_flux2_workflow", spy_build)
    info = svc.build_frame_flux2_ui_graph(jid, oidx)

    # 任务本身是 SDXL 参数（28 步 / cfg 6.5 / 896×1152），Klein 一律用蒸馏默认值
    assert captured["steps"] == 4, captured
    assert captured["cfg"] == 1.0, captured
    assert captured["reference_image"] == "aibar_vp_ref_%d.png" % jid
    assert captured["control_image"] == "aibar_vp_pose_%d_%d.png" % (jid, oidx)
    assert captured["diffusion"] == comic_flux2.DEFAULT_DIFFUSION
    assert captured["text_encoder"] == comic_flux2.DEFAULT_TEXT_ENCODER
    # 蒸馏版走 CFGGuider（保留负向），不是 BasicGuider
    types = [n["type"] for n in info["ui"]["nodes"]]
    assert "CFGGuider" in types
    assert "BasicGuider" not in types


def test_build_frame_flux2_ui_graph_requires_pose(monkeypatch, tmp_path):
    """没骨架图时两个按钮都该被后端挡住（invalid_input）。"""
    clip_id = _make_clip(tmp_path)
    job = svc.create_job({"clip_id": clip_id, "name": "wf-ut-flux2"})
    jid = job["id"]
    db.execute("UPDATE video_paint_jobs SET group_id=1 WHERE id=?", (int(jid),))
    db.execute(
        "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (int(jid), 1, "f", "pending", now(), now()),
    )
    _patch_comfyui(monkeypatch)
    with pytest.raises(Exception):
        svc.build_frame_flux2_ui_graph(jid, 1)


def test_frame_flux2_editor_link_url_shape(monkeypatch, tmp_path):
    jid, oidx = _seed_job_and_frame(tmp_path)
    _patch_comfyui(monkeypatch)

    link = svc.frame_flux2_editor_link(jid, oidx)
    assert link["mode"] == "graph"
    assert "aibar_wf=" in link["url"]
    assert "/api/videopaint/jobs/%d/frames/%d/flux2-workflow-graph" % (jid, oidx) in link["url"]
    assert "aibar_prompt=" in link["url"]
    assert "aibar_name=" in link["url"]
    # 正向提示词节点在 build_flux2_workflow 里是 id=4（不是姿态工作流的 9）
    assert link["target"] == "4"


def test_flux2_ui_graph_roundtrip(monkeypatch, tmp_path):
    """FLUX.2 UI 图必须能被 convert_ui_to_api 无损还原（桥梁 loadGraphData 的前提）。"""
    jid, oidx = _seed_job_and_frame(tmp_path)
    _patch_comfyui(monkeypatch)
    info = svc.build_frame_flux2_ui_graph(jid, oidx)
    if not _fake_object_info():
        pytest.skip("缺少 /tmp/oi.json 节点契约快照，跳过往返校验")
    r = workflow_ui.validate_roundtrip(svc.comic_flux2.build_flux2_workflow(
        reference_image=info["ref_name"],
        control_image=info["pose_name"],
        positive="1girl", negative="lowres",
        seed=info["seed"], steps=4, cfg=1.0,
    ))
    assert r["ok"], r["error"]
