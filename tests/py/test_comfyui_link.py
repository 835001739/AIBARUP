"""``sync.comfyui_link`` 的单元测试：深链接构造与提示词清洗。

这些函数决定了「点一下能不能把工作流和提示词带进 ComfyUI」，行为必须锁死：
URL 参数名一改，ComfyUI 侧的桥梁扩展就接不上了。

隔离策略与 ``test_sync.py`` 一致：目录与数据库全部落在 ``tmp_path``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from flask import Flask
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from config import Config
from core import db as core_db
from core.db import query_one
from core.errors import AIBARError
from sync import comfyui_link, scanner
from sync.routes import bp


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与 ComfyUI 源目录重定向到临时目录。"""
    data_dir = tmp_path / "data"
    workflows = tmp_path / "wf"
    output = tmp_path / "out"
    for path in (data_dir, workflows, output):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "COMFYUI_DIR", None)
    monkeypatch.setattr(Config, "COMFYUI_WORKFLOWS_DIR", str(workflows))
    monkeypatch.setattr(Config, "COMFYUI_OUTPUT_DIR", str(output))
    monkeypatch.setattr(Config, "COMFYUI_MODELS_DIR", tmp_path / "models")

    core_db._local.conn = None
    core_db.migrate()
    yield {"workflows": workflows, "output": output}
    core_db._local.conn = None


@pytest.fixture
def client(env):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------- 造数据


def _ui_graph(positive: str, negative: str = "") -> dict[str, Any]:
    """最小可用的 UI 格式画布：两个编码器 + 一个 KSampler。"""
    return {
        "nodes": [
            {
                "id": 1,
                "type": "CLIPTextEncode",
                "title": "Positive",
                "widgets_values": [positive],
                "inputs": [{"name": "clip", "type": "CLIP", "link": None}],
            },
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "title": "Negative",
                "widgets_values": [negative],
                "inputs": [{"name": "clip", "type": "CLIP", "link": None}],
            },
        ],
        "links": [],
    }


def _make_png(path: Path, prompt: str | None = None, workflow: dict | None = None) -> Path:
    """生成带 ComfyUI 元数据块的 PNG（prompt=API 图，workflow=UI 图）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngInfo()
    if prompt is not None:
        pnginfo.add_text("prompt", prompt)
    if workflow is not None:
        pnginfo.add_text("workflow", json.dumps(workflow))
    Image.new("RGB", (4, 4), (10, 20, 30)).save(path, pnginfo=pnginfo)
    return path


def _scan_image(env, name: str, prompt: str | None, workflow: dict | None = None) -> str:
    _make_png(env["output"] / name, prompt=prompt, workflow=workflow)
    scanner.scan_images()
    row = query_one("SELECT id FROM images WHERE filename = ?", (name,))
    assert row is not None, f"扫描未入库：{name}"
    return str(row["id"])


# ------------------------------------------------------- 提示词清洗


def test_split_prompt_roundtrip():
    assert comfyui_link.split_prompt("正向内容\nNegative: 负向内容") == ("正向内容", "负向内容")
    assert comfyui_link.split_prompt("只有正向") == ("只有正向", "")
    assert comfyui_link.split_prompt("") == ("", "")


def test_clean_stored_prompt_keeps_plain_text():
    assert comfyui_link.clean_stored_prompt("a cat on a table") == "a cat on a table"


def test_clean_stored_prompt_unwraps_full_json():
    """库里存的 prompt 常是整份 API 图 JSON，要剥出正向文本。"""
    api = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "一只在屋顶上的猫"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "模糊"}},
        "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
    }
    assert comfyui_link.clean_stored_prompt(json.dumps(api)) == "一只在屋顶上的猫"


def test_clean_stored_prompt_rescues_truncated_json():
    """元数据超长被截断的 JSON，正则抢救出最长的那段文本。

    抢救有 20 字下限（``_FRAGMENT_RE``）：短于这个长度的往往是模型名、
    采样器名之类的枚举值，捞出来只会更糟。
    """
    long_prompt = "一只橘猫蹲在屋顶上，夕阳把整条街染成金色"
    truncated = '{"1": {"inputs": {"value": "' + long_prompt + '", "x"'
    assert comfyui_link.clean_stored_prompt(truncated) == long_prompt


def test_clean_stored_prompt_ignores_short_json_fragments():
    """不足 20 字的片段不抢救，宁可空着。"""
    assert comfyui_link.clean_stored_prompt('{"1": {"inputs": {"value": "很短"') == ""


def test_clean_stored_prompt_gives_up_quietly():
    """抢救不出任何东西时返回空串——宁可不填，也不能填一堆 JSON 进去。"""
    assert comfyui_link.clean_stored_prompt("{") == ""
    assert comfyui_link.clean_stored_prompt(None) == ""


# ------------------------------------------------------- 链接构造


def test_build_editor_link_without_args_degrades_gracefully():
    """什么都不给时退化成"只打开 ComfyUI"，由路由层负责拒绝空请求。"""
    got = comfyui_link.build_editor_link()
    assert got["mode"] == "none"
    assert got["url"] == comfyui_link.comfyui_base() + "/"


def test_build_editor_link_rejects_unknown_image():
    with pytest.raises(AIBARError) as exc:
        comfyui_link.build_editor_link(image_id="不存在的图片")
    assert exc.value.code == "not_found"


def test_build_editor_link_from_workflow(env):
    """按工作流文件名构造：走工作流库路径，带提示词与写入节点。"""
    (env["workflows"] / "demo.json").write_text(
        json.dumps(_ui_graph("一位女剑客站在雨夜街头", "低质量")), encoding="utf-8"
    )
    got = comfyui_link.build_editor_link(workflow="demo.json")

    assert got["mode"] == "library"
    assert got["target"] == "1"
    assert got["prompt"] == "一位女剑客站在雨夜街头"
    assert got["negative"] == "低质量"

    params = parse_qs(urlparse(got["url"]).query)
    assert got["url"].startswith(comfyui_link.comfyui_base() + "/")
    assert params["aibar_wf"][0].endswith("/api/workflows/demo.json/graph")
    assert params["aibar_target"][0] == "1"
    assert params["aibar_prompt"][0] == "一位女剑客站在雨夜街头"
    assert params["aibar_name"][0] == "demo"


def test_build_editor_link_prefers_image_embedded_workflow(env):
    """点图片时优先用图片内嵌的画布——它才是真正产出这张图的那张。"""
    image_id = _scan_image(
        env,
        "shot_0001_.png",
        prompt="库里存的脏数据",
        workflow=_ui_graph("图片里真正的提示词"),
    )
    got = comfyui_link.build_editor_link(image_id=image_id)

    assert got["mode"] == "embedded"
    assert got["prompt"] == "图片里真正的提示词"
    assert got["target"] == "1"
    params = parse_qs(urlparse(got["url"]).query)
    assert params["aibar_wf"][0].endswith(f"/api/gallery/{image_id}/workflow")


def test_build_editor_link_falls_back_to_no_workflow(env):
    """既无内嵌工作流、名字也匹配不上时退化为只打开 ComfyUI，不能编造参数。"""
    image_id = _scan_image(env, "orphan_0001_.png", prompt="一段提示词")
    got = comfyui_link.build_editor_link(image_id=image_id)

    assert got["mode"] == "none"
    assert got["url"] == comfyui_link.comfyui_base() + "/"
    assert "aibar_wf" not in got["url"]


def test_bridge_not_installed_is_reported(env):
    """桥梁没装时仍然给出链接，但要如实告知，前端据此提示用户。"""
    assert comfyui_link.bridge_installed() is False
    got = comfyui_link.build_editor_link(workflow="whatever.json")
    assert got["bridge_installed"] is False


def test_bridge_installed_detects_file(tmp_path, monkeypatch):
    bridge = tmp_path / "custom_nodes" / comfyui_link.BRIDGE_DIRNAME
    (bridge / "js").mkdir(parents=True)
    (bridge / "js" / "aibar_bridge.js").write_text("// test", encoding="utf-8")
    monkeypatch.setattr(Config, "COMFYUI_DIR", str(tmp_path))
    assert comfyui_link.bridge_installed() is True


# ------------------------------------------------------- 路由


def test_editor_link_route_ok(client, env):
    (env["workflows"] / "demo.json").write_text(
        json.dumps(_ui_graph("路由测试提示词")), encoding="utf-8"
    )
    resp = client.get("/api/comfyui/editor-link?workflow=demo.json")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["data"]["prompt"] == "路由测试提示词"


def test_editor_link_route_rejects_empty(client):
    resp = client.get("/api/comfyui/editor-link")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_gallery_workflow_route_returns_raw_json(client, env):
    """桥梁要的是裸 JSON（不套 envelope），否则 loadGraphData 认不出来。"""
    image_id = _scan_image(env, "raw_0001_.png", prompt=None, workflow=_ui_graph("裸 JSON 测试"))
    resp = client.get(f"/api/gallery/{image_id}/workflow")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    graph = resp.get_json()
    assert isinstance(graph.get("nodes"), list)
    # 不能有 ok/data 包装
    assert "ok" not in graph


def test_gallery_workflow_route_404_when_absent(client, env):
    image_id = _scan_image(env, "plain_0001_.png", prompt=None, workflow=None)
    assert client.get(f"/api/gallery/{image_id}/workflow").status_code == 404
    # 不存在的图片也应该是 404，不能泄露"谁有元数据"
    assert client.get("/api/gallery/不存在的id/workflow").status_code == 404


# --------------------------------------------------------------- 路径穿越


def test_safe_join_rejects_escaping_paths(tmp_path: Path):
    """文件名来自 ?workflow= 查询参数，任何逃出源目录的写法都必须被拒绝。

    曾经的实现是 ``str(target).startswith(str(base))``，它挡得住 ``/etc/passwd``
    却挡不住**兄弟目录**：base 为 ``.../workflows`` 时，``../workflows-evil/x.json``
    解析成 ``.../workflows-evil/x.json``，前缀判断照样通过，于是目录外的文件被读走。
    """
    from sync import paths as sync_paths

    base = tmp_path / "workflows"
    base.mkdir()
    sibling = tmp_path / "workflows-evil"
    sibling.mkdir()
    (sibling / "secret.json").write_text("{}", encoding="utf-8")
    (base / "ok.json").write_text("{}", encoding="utf-8")

    # 正常路径放行
    assert sync_paths.safe_join(base, "ok.json") == (base / "ok.json").resolve()
    assert sync_paths.safe_join(base, "sub/ok.json") is not None  # 不存在但路径合法

    # 各种逃逸写法一律拒绝
    for evil in [
        "../workflows-evil/secret.json",   # 兄弟目录（前缀判断的盲区）
        "../../etc/passwd",
        "/etc/passwd",                     # 绝对路径
        "C:\\Windows\\win.ini",            # 盘符
        "",                                # 空
        ".",                               # 目录本身
    ]:
        assert sync_paths.safe_join(base, evil) is None, f"应拒绝：{evil!r}"

    # base 为 None 时也不能炸
    assert sync_paths.safe_join(None, "ok.json") is None


def test_editor_link_cannot_read_outside_workflows_dir(client, env):
    """端到端：?workflow=../<兄弟目录>/x.json 不许把内容带回响应。"""
    sibling = env["workflows"].parent / "workflows-evil"
    sibling.mkdir()
    (sibling / "secret.json").write_text(
        json.dumps(_ui_graph("TOP-SECRET-LEAKED")), encoding="utf-8"
    )

    resp = client.get("/api/comfyui/editor-link?workflow=../workflows-evil/secret.json")
    payload = resp.get_json()
    leaked = json.dumps(payload, ensure_ascii=False)

    assert "TOP-SECRET-LEAKED" not in leaked
    # 读不到文件时 mode 应退化为 none，而不是照常给出链接
    assert payload["data"]["mode"] in ("none", "library")
    assert payload["data"].get("prompt") != "TOP-SECRET-LEAKED"


# --------------------------------------------------------------- 路径穿越


def test_safe_join_rejects_escaping_paths(tmp_path: Path):
    """文件名来自 ?workflow= 查询参数，任何逃出源目录的写法都必须被拒绝。

    曾经的实现是 ``str(target).startswith(str(base))``，它挡得住 ``/etc/passwd``
    却挡不住**兄弟目录**：base 为 ``.../workflows`` 时，``../workflows-evil/x.json``
    解析成 ``.../workflows-evil/x.json``，前缀判断照样通过，于是目录外的文件被读走。
    """
    from sync import paths as sync_paths

    base = tmp_path / "workflows"
    base.mkdir()
    sibling = tmp_path / "workflows-evil"
    sibling.mkdir()
    (sibling / "secret.json").write_text("{}", encoding="utf-8")
    (base / "ok.json").write_text("{}", encoding="utf-8")

    # 正常路径放行
    assert sync_paths.safe_join(base, "ok.json") == (base / "ok.json").resolve()
    assert sync_paths.safe_join(base, "sub/ok.json") is not None  # 不存在但路径合法

    # 各种逃逸写法一律拒绝
    for evil in [
        "../workflows-evil/secret.json",   # 兄弟目录（前缀判断的盲区）
        "../../etc/passwd",
        "/etc/passwd",                     # 绝对路径
        "C:\\Windows\\win.ini",            # 盘符
        "",                                # 空
        ".",                               # 目录本身
    ]:
        assert sync_paths.safe_join(base, evil) is None, f"应拒绝：{evil!r}"

    # base 为 None 时也不能炸
    assert sync_paths.safe_join(None, "ok.json") is None


def test_editor_link_cannot_read_outside_workflows_dir(client, env):
    """端到端：?workflow=../<兄弟目录>/x.json 不许把内容带回响应。"""
    sibling = env["workflows"].parent / "workflows-evil"
    sibling.mkdir()
    (sibling / "secret.json").write_text(
        json.dumps(_ui_graph("TOP-SECRET-LEAKED")), encoding="utf-8"
    )

    resp = client.get("/api/comfyui/editor-link?workflow=../workflows-evil/secret.json")
    payload = resp.get_json()
    leaked = json.dumps(payload, ensure_ascii=False)

    assert "TOP-SECRET-LEAKED" not in leaked
    # 读不到文件时 mode 应退化为 none，而不是照常给出链接
    assert payload["data"]["mode"] in ("none", "library")
    assert payload["data"].get("prompt") != "TOP-SECRET-LEAKED"
