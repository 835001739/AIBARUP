"""M12 分镜工作流「出图风格」测试：风格预设 + 提示词规范 + 项目列读写 + 路由。

覆盖：
- ``comic/styles.py``：非法值收敛、正面/负面提示词规范、幂等、超长截断保留风格词；
- ``comic_projects.style_preset`` 列读写与非法值回退；
- ``generate_storyboard`` 把同一套风格词套用到**每一集的每一页**（风格统一）；
- ``GET /api/comic/style-presets`` 与 storyboard 路由的 ``style`` 透传。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import routes as comic_routes
from comic import service as comic_service
from comic import storyboard
from comic import styles


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "comic_outputs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")
    monkeypatch.setenv("AI_PROVIDER_ENABLED", "false")

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
def client(env):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(comic_routes.bp)
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------- styles 纯函数


def test_normalize_style_falls_back():
    assert styles.normalize_style(None) == styles.DEFAULT_STYLE
    assert styles.normalize_style("") == styles.DEFAULT_STYLE
    assert styles.normalize_style("不存在的风格") == styles.DEFAULT_STYLE
    assert styles.normalize_style("SHINKAI") == "shinkai"  # 大小写 + 空格容错
    assert styles.normalize_style(" garbage ", "cyberpunk") == "cyberpunk"  # 非法 → 回退传入值
    assert styles.normalize_style(None, "japanese_manga") == "japanese_manga"
    assert styles.normalize_style("x", "也不存在") == styles.DEFAULT_STYLE  # 回退值也非法时


def test_get_style_never_missing():
    for key in styles.STYLE_PRESETS:
        preset = styles.get_style(key)
        assert preset["key"] == key
        assert preset["label"]
    assert styles.get_style("???")["key"] == styles.DEFAULT_STYLE


def test_style_options_starts_with_default():
    opts = styles.style_options()
    assert opts
    assert opts[0]["key"] == styles.DEFAULT_STYLE
    assert len(opts) == len(styles.STYLE_PRESETS)
    assert all(o["key"] and o["label"] for o in opts)


def test_apply_style_appends_positive_and_negative():
    pos, neg = styles.apply_style("少年离开家乡", "blurry", "shinkai")
    assert pos.startswith("少年离开家乡")
    assert "shinkai" in pos
    assert "blurry" in neg
    assert "monochrome" in neg  # 风格自带的排除项


def test_apply_style_none_preset_keeps_prompt():
    pos, neg = styles.apply_style("少年离开家乡", "blurry", "none")
    assert pos == "少年离开家乡"
    assert neg == "blurry"
    pos2, neg2 = styles.apply_style("少年离开家乡", "", None)
    assert pos2 == "少年离开家乡"
    assert neg2 == ""


def test_apply_style_is_idempotent():
    once = styles.apply_style("少年离开家乡", "", "cyberpunk")
    twice = styles.apply_style(once[0], once[1], "cyberpunk")
    assert once == twice  # 重复套用不叠加


def test_apply_style_truncation_keeps_style_tail():
    body = "少" * 3000
    pos, _neg = styles.apply_style(body, "", "pixel_art", max_len=200)
    assert len(pos) <= 200
    assert pos.endswith(styles.STYLE_PRESETS["pixel_art"]["positive"])  # 风格词保留


def test_apply_style_all_presets_have_tokens_except_none():
    for key, preset in styles.STYLE_PRESETS.items():
        if key == styles.DEFAULT_STYLE:
            assert not preset["positive"] and not preset["negative"]
            continue
        assert preset["positive"] and preset["negative"]


# ---------------------------------------------------------------- 数据列读写


def test_style_preset_column_roundtrip(env):
    p = comic_service.create_project({"name": "风格列读写", "style_preset": "ink_wash"})
    assert comic_service.get_project(p["id"])["style_preset"] == "ink_wash"

    comic_service.update_project(p["id"], {"style_preset": "watercolor"})
    assert comic_service.get_project(p["id"])["style_preset"] == "watercolor"

    # 未传 style_preset 时保留原值
    comic_service.update_project(p["id"], {"description": "改描述"})
    assert comic_service.get_project(p["id"])["style_preset"] == "watercolor"

    # 非法值：保留原值（不会悄悄变成 none）
    comic_service.update_project(p["id"], {"style_preset": "not-a-style"})
    assert comic_service.get_project(p["id"])["style_preset"] == "watercolor"


def test_style_preset_default_and_invalid_on_create(env):
    assert comic_service.create_project({"name": "默认风格"})["style_preset"] == "none"
    assert comic_service.create_project(
        {"name": "非法风格", "style_preset": "???", }
    )["style_preset"] == "none"


# ---------------------------------------------------------------- 生成：每一页统一风格


def _make_project(**kwargs):
    payload = {
        "name": "风格测试漫画",
        "worldview": "王国设定：魔法与机械并存。",
        "plot_summary": "第1集 启程\n少年离开家乡。\n\n第2集 初遇\n遇见引路人。",
    }
    payload.update(kwargs)
    return comic_service.create_project(payload)


def test_generate_storyboard_applies_style_to_every_page(env):
    p = _make_project(style_preset="shinkai", pages_per_chapter=2)
    result = storyboard.generate_storyboard(p["id"])
    assert result["style"] == "shinkai"
    assert result["pages"] == 4  # 2 集 × 每章 2 页

    token = styles.STYLE_PRESETS["shinkai"]["positive"]
    neg_token = "monochrome"
    checked = 0
    for ch in comic_service.list_chapters(p["id"]):
        for pg in comic_service.list_pages(ch["id"]):
            assert token in pg["prompt_text"], "每一页都必须带统一风格词"
            assert neg_token in pg["negative_text"], "每一页都必须合并风格排除项"
            checked += 1
    assert checked == 4


def test_generate_storyboard_style_from_request_overrides_project(env):
    p = _make_project(style_preset="none", pages_per_chapter=1)
    result = storyboard.generate_storyboard(p["id"], style="cyberpunk")
    assert result["style"] == "cyberpunk"
    # 请求传入的风格会写回项目，后续生成沿用同一风格
    assert comic_service.get_project(p["id"])["style_preset"] == "cyberpunk"

    ch = comic_service.list_chapters(p["id"])[0]
    pg = comic_service.list_pages(ch["id"])[0]
    assert styles.STYLE_PRESETS["cyberpunk"]["positive"] in pg["prompt_text"]


def test_generate_storyboard_invalid_style_falls_back_to_project(env):
    p = _make_project(style_preset="watercolor")
    result = storyboard.generate_storyboard(p["id"], style="???")
    assert result["style"] == "watercolor"  # 非法值 → 回退项目设置
    # 非法值不得污染项目已保存的风格
    assert comic_service.get_project(p["id"])["style_preset"] == "watercolor"


def test_generate_storyboard_style_label_returned(env):
    p = _make_project(style_preset="japanese_manga")
    result = storyboard.generate_storyboard(p["id"])
    assert result["style_label"] == styles.STYLE_PRESETS["japanese_manga"]["label"]


# ---------------------------------------------------------------- 路由


def test_style_presets_route(client):
    resp = client.get("/api/comic/style-presets")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["default"] == "none"
    assert data["items"][0]["key"] == "none"
    assert any(i["key"] == "shinkai" for i in data["items"])


def test_storyboard_route_style_passthrough(client):
    p = _make_project()  # 项目默认 none
    resp = client.post(
        "/api/comic/projects/%d/storyboard" % p["id"],
        json={"style": "pixel_art", "pages_per_chapter": 2},
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["style"] == "pixel_art"
    assert data["pages_per_chapter"] == 2
    assert data["pages"] == 4

    ch = comic_service.list_chapters(p["id"])[0]
    for pg in comic_service.list_pages(ch["id"]):
        assert styles.STYLE_PRESETS["pixel_art"]["positive"] in pg["prompt_text"]


def test_storyboard_route_style_defaults_to_project(client):
    p = _make_project(style_preset="ink_wash")
    resp = client.post("/api/comic/projects/%d/storyboard" % p["id"], json={})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["style"] == "ink_wash"
