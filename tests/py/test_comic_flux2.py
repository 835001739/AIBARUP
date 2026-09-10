"""M17 · FLUX.2 Klein 结构控制工作流测试。

覆盖：
1. 节点拓扑稳定（节点 ID、class_type、关键连线）；
2. Roundtrip 无损（API → UI → API，比对 class_type + inputs）；
3. 三档用例：纯文生图 / 仅外观参考 / 外观+结构控制；
4. ensure_flux2_workflow 能落盘 ComfyUI workflows 目录；
5. 与 comic.workflow_ui.api_to_ui_graph 的契约（前端按钮能 loadGraphData）。

info_lookup 用本地缓存的 /tmp/oi.json（ComfyUI v0.30.0），不依赖在线 ComfyUI。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# 1) 让测试能在不依赖 aibar.live 服务的情况下从仓库根导入
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _info():
    """本地缓存的 ComfyUI object_info（启动 ComfyUI 后通过 curl 一次即可缓存）。"""
    cache = Path("/tmp/oi.json")
    if not cache.exists():
        pytest.skip("ComfyUI object_info 缓存缺失 (/tmp/oi.json)；先运行 ComfyUI 并 curl /object_info")
    with cache.open() as f:
        return json.load(f)


def _strip(graph: dict[str, Any]) -> dict[str, Any]:
    """去 _meta，仅留 class_type + inputs 用于比对。

    None 与缺失视为等价：API 工作流可省略 optional 字段（默认值由 ComfyUI 补），
    UI→API 反序列化时若把 ``None`` 写回 inputs（如 ``CLIPLoader.device``），
    不应算 roundtrip 失败。
    """
    out: dict[str, Any] = {}
    for k, v in graph.items():
        if isinstance(v, dict) and "class_type" in v:
            ins = {kk: vv for kk, vv in (v.get("inputs") or {}).items() if vv is not None}
            out[str(k)] = {"class_type": v["class_type"], "inputs": ins}
    return out


def _node_ids(graph: dict[str, Any]) -> set[str]:
    return {str(k) for k in graph.keys()}


# ---------- 1. 节点拓扑稳定性 ---------------------------------------------------

def test_build_flux2_t2i_minimal_nodes():
    """纯文生图：无参考图 → 14 个节点。"""
    from comic.flux2 import build_flux2_workflow

    g = build_flux2_workflow(positive="a cat")
    ids = _node_ids(g)
    # 必须节点
    for nid, ct in [
        ("1", "UNETLoader"), ("2", "CLIPLoader"), ("3", "VAELoader"),
        ("4", "CLIPTextEncode"), ("5", "CLIPTextEncode"),
        ("14", "EmptyFlux2LatentImage"), ("15", "Flux2Scheduler"),
        ("16", "CFGGuider"), ("17", "KSamplerSelect"), ("18", "RandomNoise"),
        ("19", "SamplerCustomAdvanced"), ("20", "VAEDecode"), ("21", "SaveImage"),
    ]:
        assert nid in ids, f"missing node {nid}"
        assert g[nid]["class_type"] == ct, f"node {nid} class_type={g[nid]['class_type']} != {ct}"
    # 不该有：参考图/控制图链
    assert "6" not in ids and "10" not in ids, "无参考图时不应有 6/10"
    assert "8" not in ids and "12" not in ids, "无参考图时不应有 8/12 ReferenceLatent"
    assert len(ids) == 13, f"纯文生图节点数应为 13，实际 {len(ids)}: {sorted(ids)}"


def test_build_flux2_with_reference():
    """仅外观参考图：16 个节点，ReferenceLatent 链接通。"""
    from comic.flux2 import build_flux2_workflow

    g = build_flux2_workflow(reference_image="ref.png", positive="a dog")
    ids = _node_ids(g)
    # 外观链必须有
    assert g["6"]["class_type"] == "LoadImage"
    assert g["7"]["class_type"] == "VAEEncode"
    assert g["8"]["class_type"] == "ReferenceLatent"
    assert g["9"]["class_type"] == "ReferenceLatent"
    # 结构链不应有
    assert "10" not in ids and "11" not in ids and "12" not in ids and "13" not in ids
    assert len(ids) == 17
    # ReferenceLatent 必须挂到 4/5（正负 conditioning）
    assert g["8"]["inputs"]["conditioning"] == ["4", 0]
    assert g["9"]["inputs"]["conditioning"] == ["5", 0]
    # ReferenceLatent 必须吃 VAEEncode(7) 的 latent
    assert g["8"]["inputs"]["latent"] == ["7", 0]


def test_build_flux2_full_control():
    """外观 + 结构控制：21 个节点，两条 ReferenceLatent 链正交。"""
    from comic.flux2 import build_flux2_workflow

    g = build_flux2_workflow(
        reference_image="ref.png",
        control_image="ctrl.png",
        positive="a dog, on a beach",
        negative="",
    )
    ids = _node_ids(g)
    assert len(ids) == 21
    # 结构链挂在外形链之后：12→8, 13→9（参考图先、外观锁后）
    assert g["12"]["inputs"]["conditioning"] == ["8", 0]
    assert g["13"]["inputs"]["conditioning"] == ["9", 0]
    assert g["12"]["inputs"]["latent"] == ["11", 0]
    # CFGGuider 接结构链输出
    assert g["16"]["inputs"]["positive"] == ["12", 0]
    assert g["16"]["inputs"]["negative"] == ["13", 0]


def test_build_flux2_clip_loader_type_is_flux2():
    """CLIPLoader 必须 type=flux2（这是 Klein 走 Qwen3-4B 的关键）。"""
    from comic.flux2 import build_flux2_workflow

    g = build_flux2_workflow(reference_image="r.png", positive="x")
    assert g["2"]["inputs"]["type"] == "flux2"


def test_build_flux2_default_models():
    """默认模型文件名与本机实际模型一致（防止默认值漂移）。"""
    from comic.flux2 import (
        DEFAULT_DIFFUSION, DEFAULT_TEXT_ENCODER, DEFAULT_VAE,
    )

    comfyui_root = Path("/Users/qinsu/AIproject/ComfyUI")
    assert (comfyui_root / "models" / "diffusion_models" / DEFAULT_DIFFUSION).exists()
    assert (comfyui_root / "models" / "text_encoders" / DEFAULT_TEXT_ENCODER).exists()
    assert (comfyui_root / "models" / "vae" / DEFAULT_VAE).exists()


def test_build_flux2_sampler_cfg():
    """蒸馏版默认 4 步 / CFG=1.0。"""
    from comic.flux2 import (
        DEFAULT_STEPS_DISTILLED, DEFAULT_CFG_DISTILLED, build_flux2_workflow,
    )

    assert DEFAULT_STEPS_DISTILLED == 4
    assert DEFAULT_CFG_DISTILLED == 1.0
    g = build_flux2_workflow(positive="x")
    assert g["15"]["inputs"]["steps"] == 4  # Flux2Scheduler
    assert g["16"]["inputs"]["cfg"] == 1.0  # CFGGuider


# ---------- 2. Roundtrip API→UI→API 无损 ---------------------------------------

def _do_roundtrip(graph: dict[str, Any]) -> tuple[bool, str]:
    """执行 API→UI→API 比对（宽松版：None 等价缺失）。

    API 工作流可省略 optional 字段（默认值由 ComfyUI 补），UI→API 反序列化时
    若把 ``None`` 写回 inputs（如 ``CLIPLoader.device``），不应算 roundtrip 失败。
    """
    from comic.workflow_ui import api_to_ui_graph
    from sync.workflow_convert import convert_ui_to_api

    info = _info()
    ui = api_to_ui_graph(graph, info_lookup=lambda: info)
    api2 = convert_ui_to_api(ui, info_lookup=lambda: info)
    a1, a2 = _strip(graph), _strip(api2)
    if a1 == a2:
        return True, ""
    # 找出差异
    diffs = []
    for k in sorted(set(a1) | set(a2)):
        if a1.get(k) != a2.get(k):
            diffs.append({"node": k, "orig": a1.get(k), "back": a2.get(k)})
    return False, "roundtrip_mismatch:%s" % json.dumps(diffs, ensure_ascii=False)[:300]


@pytest.mark.parametrize("case", [
    ("t2i", {}),
    ("ref", {"reference_image": "ref.png"}),
    ("full", {"reference_image": "ref.png", "control_image": "ctrl.png"}),
])
def test_flux2_workflow_roundtrip(case):
    """API → UI → API 无损，确保 AIBAR-Bridge loadGraphData 不掉配置。"""
    from comic.flux2 import build_flux2_workflow

    name, extra = case
    graph = build_flux2_workflow(positive="a test prompt", **extra)
    ok, err = _do_roundtrip(graph)
    assert ok, f"{name} roundtrip failed: {err}"


# ---------- 3. UI 图基本形状（前端能 loadGraphData） ----------------------------

def test_flux2_ui_graph_has_required_fields():
    """UI 图含 last_node_id / last_link_id / nodes / links / version。"""
    from comic.flux2 import build_flux2_workflow
    from comic.workflow_ui import api_to_ui_graph

    g = build_flux2_workflow(reference_image="r.png", control_image="c.png",
                              positive="a cat on a roof")
    info = _info()
    ui = api_to_ui_graph(g, info_lookup=lambda: info)
    for k in ("last_node_id", "last_link_id", "nodes", "links", "version"):
        assert k in ui
    # 节点数与 API 一致
    assert len(ui["nodes"]) == 21
    # 每个节点含 id/type/pos/size/inputs/outputs/widgets_values
    n0 = ui["nodes"][0]
    for k in ("id", "type", "pos", "size", "inputs", "outputs", "widgets_values"):
        assert k in n0, f"UI node missing {k}: {n0.get('title')}"
    # ReferenceLatent 节点必须有 link=None（输入是显式连线，不是控件）
    ref_nodes = [n for n in ui["nodes"] if n["type"] == "ReferenceLatent"]
    assert len(ref_nodes) == 4
    for n in ref_nodes:
        # condition 输入是连线 → link 字段为非 None
        cond_in = next((i for i in n["inputs"] if i["name"] == "conditioning"), None)
        assert cond_in is not None and cond_in["link"] is not None, \
            f"ReferenceLatent.conditioning 应连线: {n['title']}"


def test_flux2_ui_links_target_slot_correct():
    """links 元组 target_slot 必须等于输入在 inputs 数组中的下标（AIBAR-Bridge 硬契约）。"""
    from comic.flux2 import build_flux2_workflow
    from comic.workflow_ui import api_to_ui_graph

    g = build_flux2_workflow(reference_image="r.png", control_image="c.png",
                              positive="x")
    ui = api_to_ui_graph(g, info_lookup=lambda: _info())
    # 用 ui["nodes"] 的 inputs 数组下标核对每条 link
    for link in ui["links"]:
        lid, oid, oslot, tid, tslot, ltype = link
        target = next(n for n in ui["nodes"] if n["id"] == tid)
        assert 0 <= tslot < len(target["inputs"]), \
            f"link {lid} target_slot {tslot} OOR, node {tid} has {len(target['inputs'])} inputs"
        assert target["inputs"][tslot]["name"] != "_", \
            f"link {lid} target_slot {tslot} 指向空槽位"


# ---------- 4. 落盘 ensure_flux2_workflow --------------------------------------

def test_ensure_flux2_workflow_writes_file(tmp_path, monkeypatch):
    """ensure_flux2_workflow 把工作流写到 ComfyUI workflows 目录，幂等覆盖。"""
    from comic import flux2
    from sync import paths as sync_paths

    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)
    monkeypatch.setattr(sync_paths, "input_dir", lambda: tmp_path / "no_input")

    name = flux2.ensure_flux2_workflow(
        reference_image="r.png",
        positive="a cat",
    )
    assert name == flux2.FLUX2_WORKFLOW_FILENAME
    target = fake_wf / name
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "21" in data
    assert data["21"]["class_type"] == "SaveImage"
    # 幂等：再写一次不抛
    name2 = flux2.ensure_flux2_workflow(reference_image="r.png")
    assert name2 == name


def test_ensure_flux2_workflow_auto_picks_reference(tmp_path, monkeypatch):
    """**防回归**：默认参数下，落盘必须是带参考链的完整版（17+ 节点），不能裸 13 节点。"""
    from comic import flux2
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    (fake_input / "sample_ref.png").write_bytes(b"x" * 32)

    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)

    flux2.ensure_flux2_workflow()  # 不传任何图像 → 自动扫描 input

    data = json.loads((fake_wf / flux2.FLUX2_WORKFLOW_FILENAME).read_text(encoding="utf-8"))
    assert len(data) == 17, f"默认落盘必须 17 节点（含外观参考链），实际 {len(data)}"
    assert data["8"]["class_type"] == "ReferenceLatent"


# ---------------------------------------------------------------- M17.3 · 姿势参考 + 人物一致性
def test_build_flux2_pose_minimal_only_character():
    """仅 1 张人物参考 + 无姿势：17 节点（13 主干 + 4 人物链），1 条人物链（100/101/102/103）。"""
    from comic import flux2
    g = flux2.build_flux2_pose_workflow(character_images=["a.png"], positive="x", seed=1)
    types = {n["class_type"] for n in g.values()}
    assert "LoadImage" in types and "VAEEncode" in types and "ReferenceLatent" in types
    # 人物链只有 1 张 → 4 个节点 (100/101/102/103)
    assert "100" in g and g["100"]["class_type"] == "LoadImage"
    assert "103" in g and g["103"]["class_type"] == "ReferenceLatent"
    # 姿势链 104 不应存在
    assert "104" not in g
    # Guider 必须用 102(+) / 103(-) 而不是 CLIPTextEncode
    assert g["16"]["inputs"]["positive"] == ["102", 0]
    assert g["16"]["inputs"]["negative"] == ["103", 0]


def test_build_flux2_pose_three_characters_and_pose():
    """3 张人物 + 1 张姿势：人物链 100/104/108，姿势链 112；Guider 接 114/115。"""
    from comic import flux2
    g = flux2.build_flux2_pose_workflow(
        character_images=["c1.png", "c2.png", "c3.png"],
        pose_image="p.png",
        positive="x", seed=2,
    )
    # 4 条链 × 4 节点 = 16 + 13 主干 = 29
    assert len(g) == 29
    for i, expect_load in enumerate(["100", "104", "108", "112"]):
        assert g[expect_load]["class_type"] == "LoadImage", f"ref {i}"
    # 人物链标题必须带序号
    assert (g["102"].get("_meta") or {}).get("title") == "人物参考1(+)"
    assert (g["110"].get("_meta") or {}).get("title") == "人物参考3(+)"
    # 姿势链在最后
    assert (g["114"].get("_meta") or {}).get("title") == "姿势参考(+)"
    # 链序：人物1 → 人物2 → 人物3 → 姿势
    assert g["104"]["class_type"] == "LoadImage"  # 人物2
    assert g["102"]["inputs"]["conditioning"] == ["4", 0]  # 人物1 接 CLIPTextEncode(+)
    assert g["106"]["inputs"]["conditioning"] == ["102", 0]  # 人物2 接 人物1+
    assert g["110"]["inputs"]["conditioning"] == ["106", 0]  # 人物3 接 人物2+
    assert g["114"]["inputs"]["conditioning"] == ["110", 0]  # 姿势接 人物3+
    # 链序语义：人物先立住 → 姿势后改动作


def test_build_flux2_pose_caps_at_max_character_refs():
    """超 MAX_CHARACTER_REFS 的张数应被截断，不超 3。"""
    from comic import flux2
    g = flux2.build_flux2_pose_workflow(
        character_images=["a", "b", "c", "d", "e", "f"],  # 6 张
        pose_image="p.png",
        positive="x", seed=3,
    )
    # 3 张人物 + 1 张姿势 = 16 + 13 主干 = 29
    assert len(g) == 29
    # 第 4/5/6 张没占节点
    assert "116" not in g and "120" not in g and "124" not in g


def test_build_flux2_pose_no_pose_no_pose_chain():
    """无姿势图：只画人物链，姿势链节点全无。"""
    from comic import flux2
    g = flux2.build_flux2_pose_workflow(character_images=["a.png"], positive="x", seed=4)
    # 17 节点（1 张人物 + 13 主干）
    assert len(g) == 17
    # 链后没有追加的 LoadImage
    assert all("100" not in k or g[k]["class_type"] == "LoadImage" for k in g)


def test_build_flux2_pose_chain_helper_reused_with_legacy():
    """`_add_reference` 是 build_flux2_workflow / build_flux2_pose_workflow 的共同实现。"""
    from comic import flux2
    # 同样的 image，legacy 和 pose 都能正确挂上
    legacy = flux2.build_flux2_workflow(reference_image="x.png", control_image="y.png", positive="z", seed=5)
    pose = flux2.build_flux2_pose_workflow(character_images=["x.png"], pose_image="y.png", positive="z", seed=5)
    # legacy ReferenceLatent 节点：8/9(外观), 12/13(结构)  → 共 4
    # pose  ReferenceLatent 节点：102/103(人物), 114/115(姿势) → 共 4
    legacy_refs = [k for k, v in legacy.items() if v["class_type"] == "ReferenceLatent"]
    pose_refs = [k for k, v in pose.items() if v["class_type"] == "ReferenceLatent"]
    assert len(legacy_refs) == 4 and len(pose_refs) == 4
    # 节点类型集合必须一致
    assert {legacy[k]["class_type"] for k in legacy} == {pose[k]["class_type"] for k in pose}


def test_flux2_pose_roundtrip_full():
    """姿势工作流 roundtrip：API→UI→API 无损。"""
    from comic import flux2
    from comic import workflow_ui
    api = flux2.build_flux2_pose_workflow(
        character_images=["c1.png", "c2.png", "c3.png"], pose_image="p.png",
        positive="p", negative="n", seed=6,
    )
    r = workflow_ui.validate_roundtrip(api)
    assert r["ok"], r["error"]


def test_ensure_flux2_pose_workflow_writes_file(tmp_path, monkeypatch):
    """ensure_flux2_pose_workflow 落盘到 aibar_flux2_pose_consistent.json，幂等。"""
    from comic import flux2
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    (fake_input / "char.png").write_bytes(b"x" * 16)
    (fake_input / "pose.png").write_bytes(b"y" * 16)
    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)

    name = flux2.ensure_flux2_pose_workflow(
        character_images=["char.png"], pose_image="pose.png", positive="x", seed=10
    )
    assert name == flux2.FLUX2_POSE_WORKFLOW_FILENAME
    target = fake_wf / name
    data = json.loads(target.read_text(encoding="utf-8"))
    # 1 张人物 + 1 张姿势 + 13 主干 = 21
    assert len(data) == 21
    # 再次调用应幂等（同名覆盖）
    name2 = flux2.ensure_flux2_pose_workflow(character_images=["char.png"], pose_image="pose.png", positive="x", seed=10)
    assert name2 == name


def test_ensure_flux2_pose_workflow_auto_picks(tmp_path, monkeypatch):
    """无图像时：input/ 里 1 张当人物，2 张时第 2 张当姿势。"""
    from comic import flux2
    from sync import paths as sync_paths

    fake_input = tmp_path / "input"
    fake_input.mkdir()
    (fake_input / "a.png").write_bytes(b"a")
    (fake_input / "b.png").write_bytes(b"b")
    fake_wf = tmp_path / "workflows"
    fake_wf.mkdir()
    monkeypatch.setattr(sync_paths, "input_dir", lambda: fake_input)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: fake_wf)

    flux2.ensure_flux2_pose_workflow()  # 不传任何图
    data = json.loads((fake_wf / flux2.FLUX2_POSE_WORKFLOW_FILENAME).read_text(encoding="utf-8"))
    # 自动 1 人物 + 1 姿势 + 13 主干 = 21
    assert len(data) == 21
    # 人物是 a.png，姿势是 b.png
    assert data["100"]["inputs"]["image"] == "a.png"
    assert data["104"]["inputs"]["image"] == "b.png"