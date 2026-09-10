"""M14+ · 姿势可控出图（ControlNet + IPAdapter）测试。

覆盖：
- **姿势库渲染**：23 个预置姿势全部渲染成骨架 PNG，颜色与 OpenPose 标准配色一致；
- **渲染幂等**：相同 keypoints 两次渲染字节完全相同（可缓存 / 可去重）；
- **自定义姿势** ``add_custom_pose``：落盘 + 加入 POSES 字典 + 返回 ``file`` 路径；
- **工作流节点拓扑**：15 节点，model 与 conditioning 两条正交链汇入 KSampler；
- **work_skip 选项**：reference_image / pose_image 留空时跳过对应链路（无参考图=不锁脸、不控姿势）；
- **节点类型完整性**：IPAdapter、ControlNet、CLIP Vision、KSampler 等都到位；
- **节点 ID 与链接方向**：``5`` 接收 ``4.IMAGE``（参考图）、``11`` 接收 ``6.IMAGE``（骨架）；
- **AIBAR runner 兼容**：用 AIBAR 的 ``convert_text`` 处理不应丢节点；
- **ensure_pose_library 幂等性**：第二次调用 skipped=全部（按内容哈希命名 + 内容相同）；
- **落盘范式与目录**：用 ``sync.paths`` 而非硬编码路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------- 预置姿势


def test_all_poses_have_18_keypoints():
    """所有预置姿势必须含 18 个点（OpenPose 标配），缺一个 ControlNet 张量对不齐。"""
    from comic import pose

    for key, pose_def in pose.POSES.items():
        pts = pose_def.get("points") or {}
        assert set(pts.keys()) == set(pose.KEYPOINT_NAMES), \
            f"姿势 {key} 关键点不全：缺 {set(pose.KEYPOINT_NAMES) - set(pts.keys())}"


def test_pose_to_keypoints_returns_18_points():
    from comic import pose

    kps = pose.pose_to_keypoints(pose.POSES["stand"])
    assert len(kps) == 18
    for x, y in kps:
        assert 0.0 <= x <= 1.0
        assert 0.0 <= y <= 1.0


def test_pose_to_keypoints_fills_missing_with_zero_zero():
    from comic import pose

    partial = {"points": {"nose": (0.5, 0.2), "neck": (0.5, 0.3)}}
    kps = pose.pose_to_keypoints(partial)
    assert len(kps) == 18
    assert kps[14] == [0.0, 0.0]
    assert kps[17] == [0.0, 0.0]


def test_list_poses_returns_all_keys():
    from comic import pose

    items = pose.list_poses()
    assert len(items) == len(pose.POSES)
    assert {it["key"] for it in items} == set(pose.POSES.keys())
    assert all("label" in it and "desc" in it for it in items)


# ---------------------------------------------------------------- 骨架渲染


def test_render_pose_skeleton_returns_png_bytes():
    from comic import pose

    kps = pose.pose_to_keypoints(pose.POSES["stand"])
    png = pose.render_pose_skeleton(kps, width=384, height=512)
    assert isinstance(png, bytes)
    assert len(png) > 0
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_pose_skeleton_is_deterministic():
    """相同 keypoints 多次渲染字节相同（可作为缓存 / 去重依据）。"""
    from comic import pose

    kps = pose.pose_to_keypoints(pose.POSES["walk"])
    a = pose.render_pose_skeleton(kps, width=256, height=340)
    b = pose.render_pose_skeleton(kps, width=256, height=340)
    assert a == b


def test_render_pose_skeleton_handles_missing_points():
    """缺失点（``[0,0]``）必须被静默忽略，不能让 PIL 报错或画出原点。"""
    from comic import pose

    partial = [[0.0, 0.0]] * 18
    partial[1] = [0.5, 0.3]
    partial[2] = [0.4, 0.3]
    partial[3] = [0.4, 0.4]
    png = pose.render_pose_skeleton(partial)
    assert isinstance(png, bytes)


def test_render_pose_skeleton_returns_none_for_short_input():
    """输入少于 18 点直接拒绝（避免下游张量维度错位）。"""
    from comic import pose

    assert pose.render_pose_skeleton([[0.5, 0.5]] * 10) is None


# ---------------------------------------------------------------- 工作流节点拓扑


def test_workflow_has_15_nodes_with_full_chain():
    from comic import pose

    g = pose.build_pose_workflow(
        reference_image="ref.png", pose_image="pose.png",
        positive="x", negative="y", seed=1,
    )
    assert len(g) == 15


def test_workflow_skip_chain_when_no_reference():
    """无参考图：跳过整条 IPAdapter 锁脸链（2/3/4/5），保留 ControlNet 控姿势链。"""
    from comic import pose

    g = pose.build_pose_workflow(reference_image="", pose_image="pose.png", positive="x")
    nodes = set(g.keys())
    for nid in ("2", "3", "4", "5"):
        assert nid not in nodes, f"无参考图时不应建节点 {nid}"
    for nid in ("6", "7", "8", "11"):
        assert nid in nodes, f"有骨架图时应建节点 {nid}"


def test_workflow_skip_chain_when_no_pose():
    """无骨架图：跳过整条 ControlNet 链，保留 IPAdapter 锁脸链。"""
    from comic import pose

    g = pose.build_pose_workflow(reference_image="ref.png", pose_image="", positive="x")
    for nid in ("6", "7", "8", "11"):
        assert nid not in g, f"无骨架图时不应建节点 {nid}"
    for nid in ("2", "3", "4", "5"):
        assert nid in g, f"有参考图时应建节点 {nid}"


def test_workflow_minimal_works_without_any_input():
    """极端：两个都没，只剩公共节点——能跑出图但姿势/人物一致性都不保证。"""
    from comic import pose

    g = pose.build_pose_workflow(reference_image="", pose_image="", positive="x")
    assert set(g.keys()) == {"1", "9", "10", "12", "13", "14", "15"}
    assert g["13"]["class_type"] == "KSampler"


def test_workflow_node_topology_full_chain():
    from comic import pose

    g = pose.build_pose_workflow(
        reference_image="ref.png", pose_image="pose.png", positive="x", negative="y",
    )
    expected = {
        "1": "CheckpointLoaderSimple", "12": "EmptyLatentImage",
        "2": "CLIPVisionLoader", "3": "IPAdapterUnifiedLoaderFaceID",
        "4": "LoadImage", "5": "IPAdapterFaceID",
        "6": "LoadImage", "7": "ControlNetLoader", "8": "SetUnionControlNetType",
        "11": "ControlNetApplyAdvanced",
        "9": "CLIPTextEncode", "10": "CLIPTextEncode",
        "13": "KSampler", "14": "VAEDecode", "15": "SaveImage",
    }
    for nid, ct in expected.items():
        assert g[nid]["class_type"] == ct, f"节点 {nid} 应是 {ct}，实际 {g[nid]['class_type']}"


def test_workflow_links_connect_to_ksampler():
    """KSampler.model 收 IPAdapterFaceID 的 MODEL 输出（锁脸链路末节点），不是 CheckpointLoader。"""
    from comic import pose

    g = pose.build_pose_workflow(
        reference_image="ref.png", pose_image="pose.png", positive="x", negative="y",
    )
    assert g["13"]["inputs"]["model"] == ["5", 0]
    assert g["13"]["inputs"]["latent_image"] == ["12", 0]
    assert g["14"]["inputs"]["samples"] == ["13", 0]
    assert g["15"]["inputs"]["images"] == ["14", 0]


def test_workflow_positive_passes_through_controlnet():
    from comic import pose

    g = pose.build_pose_workflow(
        reference_image="ref.png", pose_image="pose.png", positive="x", negative="y",
    )
    assert g["13"]["inputs"]["positive"] == ["11", 0]
    assert g["13"]["inputs"]["negative"] == ["11", 1]


def test_workflow_aibar_runner_compatible():
    """``sync.workflow_convert.convert_text`` 必须能无损处理我们的工作流。"""
    from comic import pose
    from sync import workflow_convert

    g = pose.build_pose_workflow(
        reference_image="ref.png", pose_image="pose.png", positive="x", negative="y",
    )
    converted = workflow_convert.convert_text(json.dumps(g))
    assert set(converted.keys()) == set(g.keys())
    for nid, node in g.items():
        assert converted[nid].get("class_type") == node.get("class_type")


def test_workflow_text_nodes_have_chinese_titles():
    """AIBAR runner 靠 ``_meta.title`` 含「负面」识别负向节点；必须把标题写明。"""
    from comic import pose

    g = pose.build_pose_workflow(positive="x", negative="y")
    assert "负面" in g["10"]["_meta"]["title"]
    assert "负面" not in g["9"]["_meta"]["title"]


# ---------------------------------------------------------------- ensure_pose_library / ensure_pose_workflow 落盘


def test_ensure_pose_library_writes_files(tmp_path, monkeypatch):
    """落盘到 ``sync.paths.input_dir()/aibar_poses/``，文件名带内容哈希。"""
    from comic import pose
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()

    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    result = pose.ensure_pose_library()

    assert result["dir"] == str(fake_input)
    assert result["written"] >= len(pose.POSES)
    target_dir = fake_input / pose.POSE_SUBDIR
    assert target_dir.is_dir()
    files = list(target_dir.glob("pose_*.png"))
    assert len(files) >= len(pose.POSES)


def test_ensure_pose_library_idempotent(tmp_path, monkeypatch):
    """第二次调用同一个姿势的渲染应当全部 skipped（内容哈希 + 字节比对）。"""
    from comic import pose
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)

    pose.ensure_pose_library()
    result = pose.ensure_pose_library()

    assert result["written"] == 0
    assert result["skipped"] >= len(pose.POSES)


def test_ensure_pose_workflow_writes_to_workflows_dir(tmp_path, monkeypatch):
    from comic import pose
    from sync import paths as sync_paths

    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)
    monkeypatch.setattr(sync_paths, "input_dir", lambda: tmp_path / "no_input")

    name = pose.ensure_pose_workflow(
        reference_image="ref.png", pose_image="pose.png",
        positive="1girl", negative="blurry",
    )

    assert name == pose.POSE_WORKFLOW_FILENAME
    saved = fake_wf / name
    assert saved.is_file()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert "1" in data and data["1"]["class_type"] == "CheckpointLoaderSimple"


def test_ensure_pose_workflow_auto_fills_chain_when_images_exist(tmp_path, monkeypatch):
    """**关键防回归**：默认参数下，落盘文件必须是 15 节点完整链路，不能是 7 节点裸版。

    7 节点裸版 = 没有 IPAdapter/ControlNet 链 = AIBAR runner 跑出来的图
    无脸锁无姿势控 = 用户看到的「指定动作生成图片」完全失效。
    """
    from comic import pose
    from sync import paths as sync_paths

    # 模拟 input 目录：有一张角色定妆图 + 一张预置骨架
    fake_input = tmp_path / "input"
    fake_input.mkdir()
    (fake_input / "aibar_poses").mkdir()
    (fake_input / "aibar_poses" / "actor_3_ref.png").write_bytes(b"REF")
    (fake_input / "aibar_poses" / "pose_walk_abc.png").write_bytes(b"POSE")

    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()

    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)

    # 不传任何图像 → 触发自动扫描
    pose.ensure_pose_workflow()

    data = json.loads((fake_wf / pose.POSE_WORKFLOW_FILENAME).read_text(encoding="utf-8"))
    assert len(data) == 15, f"默认落盘必须 15 节点，实际 {len(data)}"
    assert data["5"]["class_type"] == "IPAdapterFaceID"
    assert data["11"]["class_type"] == "ControlNetApplyAdvanced"
    # 链路上图像必须来自自动扫描结果
    assert data["4"]["inputs"]["image"] == "aibar_poses/actor_3_ref.png"
    assert "aibar_poses/pose_walk" in data["6"]["inputs"]["image"]


def test_ensure_pose_workflow_handles_missing_dirs(monkeypatch):
    """workflows_dir 不可用时返回 None（不抛异常）。"""
    from comic import pose
    from sync import paths as sync_paths

    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: None)
    assert pose.ensure_pose_workflow() is None


def test_ensure_pose_workflow_auto_fill_false_keeps_pure_openpose(tmp_path, monkeypatch):
    """**防回归**：落「纯 openpose 版」必须显式 ``auto_fill=False``。

    踩过的坑：只留空 ``reference_image`` 是不够的——自动扫描会把 input 里第一张
    ``actor_*.png`` 补进来，落出来的仍是 15 节点锁脸版，纯 openpose 版根本拿不到。
    """
    from comic import pose
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    (fake_input / "aibar_poses").mkdir()
    (fake_input / "aibar_poses" / "actor_3_ref.png").write_bytes(b"REF")
    (fake_input / "aibar_poses" / "pose_walk_abc.png").write_bytes(b"POSE")

    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)

    # ① 不关 auto_fill：留空的参考图被补上 → 仍是 15 节点锁脸版（坑）
    pose.ensure_pose_workflow(
        pose_image="aibar_poses/pose_walk_abc.png", filename=pose.POSE_OPENPOSE_FILENAME
    )
    got = json.loads((fake_wf / pose.POSE_OPENPOSE_FILENAME).read_text(encoding="utf-8"))
    assert len(got) == 15, "auto_fill 默认开启时会被补成锁脸版"

    # ② 关掉 auto_fill：才是 11 节点纯 openpose 版，不锁脸
    name = pose.ensure_pose_workflow(
        pose_image="aibar_poses/pose_walk_abc.png",
        filename=pose.POSE_OPENPOSE_FILENAME,
        auto_fill=False,
    )
    assert name == pose.POSE_OPENPOSE_FILENAME
    data = json.loads((fake_wf / name).read_text(encoding="utf-8"))
    assert len(data) == 11, f"纯 openpose 版必须 11 节点，实际 {len(data)}"

    types = [v["class_type"] for v in data.values()]
    assert "IPAdapterFaceID" not in types, "纯 openpose 版不能有锁脸链"
    assert "IPAdapterUnifiedLoaderFaceID" not in types
    assert "CLIPVisionLoader" not in types
    # 控姿链必须完整且类型正确
    assert types.count("ControlNetLoader") == 1
    ctype = [v for v in data.values() if v["class_type"] == "SetUnionControlNetType"][0]
    assert ctype["inputs"]["type"] == "openpose"


def test_pose_openpose_filename_differs_from_default():
    """两个文件名必须不同，否则纯 openpose 版会覆盖锁脸版。"""
    from comic import pose

    assert pose.POSE_OPENPOSE_FILENAME != pose.POSE_WORKFLOW_FILENAME


# ---------------------------------------------------------------- 自定义姿势


def test_add_custom_pose_writes_file_and_registers(tmp_path, monkeypatch):
    from comic import pose
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)

    kps = pose.pose_to_keypoints(pose.POSES["stand"])
    kps[2] = [0.3, 0.28]

    result = pose.add_custom_pose("my_pose", kps, label="我的姿势", desc="测试")

    assert result["ok"] is True
    assert "my_pose" in pose.POSES
    assert pose.POSES["my_pose"]["label"] == "我的姿势"
    target = Path(result["path"])
    assert target.is_file()

    pose.POSES.pop("my_pose", None)
    target.unlink(missing_ok=True)


# ---------------------------------------------------------------- 与 runner 注入的对齐


def test_ksampler_seed_field_exists_for_runner_injection():
    """AIBAR runner 会注入 ``seed``；KSampler 必须有这个字段（即使默认 0）。"""
    from comic import pose

    g = pose.build_pose_workflow(positive="x", seed=12345)
    assert g["13"]["inputs"]["seed"] == 12345


def test_clip_text_nodes_are_discoverable_for_prompt_injection():
    """AIBAR runner 通过 ``class_type`` 含 ``CLIPTextEncode`` 来发现文本节点。"""
    from comic import pose

    g = pose.build_pose_workflow(positive="正", negative="反")
    text_nodes = [
        nid for nid, n in g.items()
        if "CLIPTextEncode" in n.get("class_type", "")
    ]
    assert len(text_nodes) >= 2


# ---------------------------------------------------------------- 路由：姿势工作流生成入口（2026-09-09）

@pytest.fixture
def pose_client(tmp_path, monkeypatch):
    """只注册 comic 蓝图的 Flask 测试客户端，input/ 与 workflows/ 指向临时目录。"""
    from flask import Flask

    from comic import routes as comic_routes
    from sync import paths as sync_paths

    inp = tmp_path / "input"
    (inp / "aibar_poses").mkdir(parents=True)
    wf = tmp_path / "workflows"
    wf.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: inp)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: wf)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(comic_routes.bp)
    with app.test_client() as c:
        c._aibar_input = inp  # type: ignore[attr-defined]
        c._aibar_workflows = wf  # type: ignore[attr-defined]
        yield c


def test_poses_assets_lists_references_and_poses(pose_client):
    """素材接口按命名约定把参考图与骨架图分开。"""
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "actor_ref.png").write_bytes(b"R")
    (sub / "pose_walk_abc.png").write_bytes(b"P")
    (sub / "pose_run_def.png").write_bytes(b"P")
    (sub / "ignored.txt").write_text("x")

    r = pose_client.get("/api/comic/poses/assets")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert [x["name"] for x in data["references"]] == ["actor_ref.png"]
    assert data["references"][0]["value"] == f"{pose.POSE_SUBDIR}/actor_ref.png"
    assert sorted(x["name"] for x in data["poses"]) == ["pose_run_def.png", "pose_walk_abc.png"]
    # 非 png 不进列表
    assert not any("ignored" in x["name"] for x in data["references"] + data["poses"])


def test_poses_assets_empty_when_dir_missing(pose_client, monkeypatch):
    """目录不可用时返回空数组而不是 500（页面只是少几个下拉选项）。"""
    from sync import paths as sync_paths

    monkeypatch.setattr(sync_paths, "input_dir", lambda: None)
    r = pose_client.get("/api/comic/poses/assets")
    assert r.status_code == 200
    assert r.get_json()["data"] == {"references": [], "poses": []}


def test_build_workflow_consistent_variant_is_15_nodes(pose_client):
    """锁脸 + 控姿变体：15 节点，落 POSE_WORKFLOW_FILENAME。"""
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "actor_ref.png").write_bytes(b"R")
    (sub / "pose_walk_abc.png").write_bytes(b"P")

    r = pose_client.post("/api/comic/poses/build-workflow", json={
        "variant": "consistent",
        "reference_image": f"{pose.POSE_SUBDIR}/actor_ref.png",
        "pose_image": f"{pose.POSE_SUBDIR}/pose_walk_abc.png",
        "positive": "1girl", "negative": "blurry",
    })
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["variant"] == "consistent"
    assert data["filename"] == pose.POSE_WORKFLOW_FILENAME
    assert data["nodes"] == 15, f"锁脸版必须 15 节点，实际 {data['nodes']}"

    saved = json.loads((pose_client._aibar_workflows / data["filename"]).read_text(encoding="utf-8"))
    assert saved["5"]["class_type"] == "IPAdapterFaceID"
    assert saved["6"]["inputs"]["image"] == f"{pose.POSE_SUBDIR}/pose_walk_abc.png"


def test_build_workflow_openpose_variant_is_11_nodes(pose_client):
    """**关键防回归**：纯 openpose 变体必须 11 节点，且**不能**有 IPAdapter 锁脸链。

    只要这里退化成 15 节点，就说明 auto_fill 又被打开了（留空的参考图被扫回来）。
    """
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "actor_ref.png").write_bytes(b"R")   # 故意放一张，验证不会被自动补上
    (sub / "pose_walk_abc.png").write_bytes(b"P")

    r = pose_client.post("/api/comic/poses/build-workflow", json={
        "variant": "openpose",
        "pose_image": f"{pose.POSE_SUBDIR}/pose_walk_abc.png",
    })
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["variant"] == "openpose"
    assert data["filename"] == pose.POSE_OPENPOSE_FILENAME
    assert data["nodes"] == 11, f"纯 openpose 版必须 11 节点，实际 {data['nodes']}"

    saved = json.loads((pose_client._aibar_workflows / data["filename"]).read_text(encoding="utf-8"))
    types = [v["class_type"] for v in saved.values()]
    assert "IPAdapterFaceID" not in types
    ctype = [v for v in saved.values() if v["class_type"] == "SetUnionControlNetType"][0]
    assert ctype["inputs"]["type"] == "openpose"


def test_build_workflow_two_variants_do_not_overwrite(pose_client):
    """两个变体落不同文件，互不影响。"""
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "actor_ref.png").write_bytes(b"R")
    (sub / "pose_walk_abc.png").write_bytes(b"P")

    for variant in ("consistent", "openpose"):
        pose_client.post("/api/comic/poses/build-workflow", json={"variant": variant})

    wf = pose_client._aibar_workflows
    assert (wf / pose.POSE_WORKFLOW_FILENAME).is_file()
    assert (wf / pose.POSE_OPENPOSE_FILENAME).is_file()
    a = json.loads((wf / pose.POSE_WORKFLOW_FILENAME).read_text(encoding="utf-8"))
    b = json.loads((wf / pose.POSE_OPENPOSE_FILENAME).read_text(encoding="utf-8"))
    assert len(a) == 15 and len(b) == 11


def test_build_workflow_rejects_unknown_variant(pose_client):
    r = pose_client.post("/api/comic/poses/build-workflow", json={"variant": "flux2"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_input"


def test_build_workflow_openpose_without_pose_image_still_has_control_chain(pose_client):
    """**关键防回归**：纯 openpose 变体即使没选骨架，也必须带控姿链（11 节点）。

    踩过的坑：``auto_fill=False`` 会同时关掉「补参考图」和「补骨架」，两张都空时
    ``build_pose_workflow`` 落出 **7 节点裸版**（既无 IPAdapter 也无 ControlNet）——
    用户点的是「生成 openpose 工作流」，拿到一个不能控姿的文件毫无意义。
    因此路由层在 openpose 变体下会自动挑第一张 pose_*.png。
    """
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "pose_walk_abc.png").write_bytes(b"P")

    r = pose_client.post("/api/comic/poses/build-workflow", json={"variant": "openpose"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["nodes"] == 11, f"没选骨架也必须是 11 节点，实际 {data['nodes']}"

    saved = json.loads((pose_client._aibar_workflows / data["filename"]).read_text(encoding="utf-8"))
    types = [v["class_type"] for v in saved.values()]
    assert "ControlNetApplyAdvanced" in types
    assert "IPAdapterFaceID" not in types
    # 自动挑的骨架必须是列表里那张
    assert saved["6"]["inputs"]["image"] == f"{pose.POSE_SUBDIR}/pose_walk_abc.png"


def test_build_workflow_openpose_without_any_pose_falls_back(pose_client):
    """一张骨架都没有时不会 500，只是退化（此时用户需要先去生成骨架库）。"""
    from comic import pose

    r = pose_client.post("/api/comic/poses/build-workflow", json={"variant": "openpose"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["filename"] == pose.POSE_OPENPOSE_FILENAME
    # 无骨架可挑 → 退化成 7 节点裸版，接口照常返回（前端会显示节点数，用户能看出来）
    assert data["nodes"] == 7


def test_build_workflow_openpose_without_pose_image_still_has_control_chain(pose_client):
    """**关键防回归**：纯 openpose 变体即使没选骨架，也必须带控姿链（11 节点）。

    踩过的坑：``auto_fill=False`` 会同时关掉「补参考图」和「补骨架」，两张都空时
    ``build_pose_workflow`` 落出 **7 节点裸版**（既无 IPAdapter 也无 ControlNet）——
    用户点的是「生成 openpose 工作流」，拿到一个不能控姿的文件毫无意义。
    因此路由层在 openpose 变体下会自动挑第一张 pose_*.png。
    """
    from comic import pose

    sub = pose_client._aibar_input / pose.POSE_SUBDIR
    (sub / "pose_walk_abc.png").write_bytes(b"P")

    r = pose_client.post("/api/comic/poses/build-workflow", json={"variant": "openpose"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["nodes"] == 11, f"没选骨架也必须是 11 节点，实际 {data['nodes']}"

    saved = json.loads((pose_client._aibar_workflows / data["filename"]).read_text(encoding="utf-8"))
    types = [v["class_type"] for v in saved.values()]
    assert "ControlNetApplyAdvanced" in types
    assert "IPAdapterFaceID" not in types
    # 自动挑的骨架必须是列表里那张
    assert saved["6"]["inputs"]["image"] == f"{pose.POSE_SUBDIR}/pose_walk_abc.png"


def test_build_workflow_openpose_without_any_pose_falls_back(pose_client):
    """一张骨架都没有时不会 500，只是退化（此时用户需要先去生成骨架库）。"""
    from comic import pose

    r = pose_client.post("/api/comic/poses/build-workflow", json={"variant": "openpose"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["filename"] == pose.POSE_OPENPOSE_FILENAME
    # 无骨架可挑 → 退化成 7 节点裸版，接口照常返回（前端会显示节点数，用户能看出来）
    assert data["nodes"] == 7
