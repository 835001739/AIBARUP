"""M12 分镜工作流（storyboard）测试：规则拆分 + 生成入队 + 路由。

覆盖：
- 规则分集拆分（标记 / 内联编号 / 整段）；
- generate_storyboard 建「集=章节、关键分镜=分镜页」并自动入队；
- AI 拆分在 provider 未启用时回退规则；
- POST /api/comic/projects/<id>/storyboard 路由契约；
- worldview / plot_summary 列读写。
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


# ---------------------------------------------------------------- 规则拆分


def test_rules_split_by_markers():
    text = "第1集 少年离家\n起程的清晨，小镇还在沉睡。\n\n第2集 初遇\n在森林边缘遇见神秘的引路人。\n\n第3集 觉醒\n力量第一次苏醒。"
    eps = storyboard._split_text_into_episodes(text)
    assert len(eps) == 3
    assert eps[0]["title"] == "第1集"
    assert "起程" in eps[0]["scene"]
    assert eps[1]["title"] == "第2集"
    assert eps[2]["title"] == "第3集"


def test_rules_split_inline_numbering():
    text = "1. 雨夜的车站，女孩独自等待。 2. 陌生的来信改变了命运。 3. 两人踏上旅途。"
    eps = storyboard._split_text_into_episodes(text)
    assert len(eps) == 3
    assert eps[0]["scene"]
    assert eps[1]["scene"]


def test_rules_split_whole_block_single_episode():
    text = "在一个被遗忘的王国，少女为了寻找失踪的哥哥而踏上旅途。"
    eps = storyboard._split_text_into_episodes(text)
    assert len(eps) == 1
    assert eps[0]["title"] == "第1集"
    assert eps[0]["scene"] == text


def test_rules_split_caps_at_max():
    text = "\n\n".join("第%d集 场景%d" % (i, i) for i in range(1, 40))
    eps = storyboard._split_text_into_episodes(text)
    assert len(eps) == storyboard.MAX_EPISODES


def test_rules_split_empty():
    assert storyboard._split_text_into_episodes("") == []


# ---------------------------------------------------------------- 生成入队


def _make_project():
    return comic_service.create_project({
        "name": "分镜测试漫画",
        "worldview": "王国设定：魔法与机械并存。",
        "plot_summary": "第1集 启程\n少年离开家乡。\n\n第2集 初遇\n遇见引路人。",
    })


def test_generate_storyboard_creates_chapters_and_pages(env):
    p = _make_project()
    result = storyboard.generate_storyboard(p["id"])
    assert result["episodes"] == 2
    assert result["chapters"] == 2
    assert result["pages"] == 2
    assert result["enqueued"] == 2
    assert result["provider"] == "rules"

    chapters = comic_service.list_chapters(p["id"])
    assert len(chapters) == 2
    for ch in chapters:
        pages = comic_service.list_pages(ch["id"])
        assert len(pages) >= 1
        assert pages[0]["status"] == "queued"
        assert pages[0]["prompt_text"]


def test_generate_storyboard_without_plot_summary_raises(env):
    p = comic_service.create_project({"name": "无剧情"})
    with pytest.raises(Exception):
        storyboard.generate_storyboard(p["id"])


def test_generate_storyboard_saves_worldview_first(env):
    p = comic_service.create_project({"name": "先保存再生成"})
    result = storyboard.generate_storyboard(
        p["id"],
        worldview="新世界观",
        plot_summary="第1集 开端\n故事开始。",
    )
    assert result["episodes"] == 1
    updated = comic_service.get_project(p["id"])
    assert updated["worldview"] == "新世界观"
    assert updated["plot_summary"] == "第1集 开端\n故事开始。"


def test_storyboard_ai_fallback_to_rules_when_disabled(env):
    p = _make_project()
    plan = storyboard.plan_episodes(p["worldview"], p["plot_summary"], provider="openai_compatible")
    assert plan["provider"] == "rules"
    assert any("回退" in w for w in plan["warnings"])


def test_generate_storyboard_pages_inherit_default_workflow(env):
    p = comic_service.create_project({
        "name": "继承工作流",
        "default_workflow": "my_wf.json",
        "plot_summary": "第1集 A\n画面一。\n\n第2集 B\n画面二。",
    })
    storyboard.generate_storyboard(p["id"])
    for ch in comic_service.list_chapters(p["id"]):
        pages = comic_service.list_pages(ch["id"])
        assert pages[0]["workflow_filename"] == "my_wf.json"


# ---------------------------------------------------------------- 路由契约


def test_storyboard_route(client):
    p = comic_service.create_project({
        "name": "路由测试",
        "plot_summary": "第1集 A\n画面一。\n\n第2集 B\n画面二。",
    })
    resp = client.post("/api/comic/projects/%d/storyboard" % p["id"], json={})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["episodes"] == 2
    assert data["enqueued"] == 2


def test_storyboard_route_requires_plot_summary(client):
    p = comic_service.create_project({"name": "无剧情路由"})
    resp = client.post("/api/comic/projects/%d/storyboard" % p["id"], json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_input"


def test_project_columns_roundtrip(client):
    p = comic_service.create_project({
        "name": "列读写", "worldview": "W", "plot_summary": "S",
    })
    got = comic_service.get_project(p["id"])
    assert "worldview" in got and got["worldview"] == "W"
    assert "plot_summary" in got and got["plot_summary"] == "S"
    comic_service.update_project(p["id"], {"worldview": "W2", "plot_summary": "S2"})
    got2 = comic_service.get_project(p["id"])
    assert got2["worldview"] == "W2" and got2["plot_summary"] == "S2"


# ---------------------------------------------------------------- 出图数量（每章页数）

def test_generate_storyboard_pages_per_chapter_multi_page(env):
    p = _make_project()  # 2 集
    result = storyboard.generate_storyboard(p["id"], pages_per_chapter=3)
    assert result["episodes"] == 2
    assert result["chapters"] == 2
    assert result["pages_per_chapter"] == 3
    assert result["pages"] == 6
    assert result["enqueued"] == 6
    for ch in comic_service.list_chapters(p["id"]):
        pages = comic_service.list_pages(ch["id"])
        assert len(pages) == 3
        prompts = [pg["prompt_text"] for pg in pages]
        assert all(prompts)
        assert len(set(prompts)) == 3  # 每页提示词互不相同
        assert pages[0]["title"].endswith("第1页")
        assert pages[2]["title"].endswith("第3页")


def test_generate_storyboard_default_pages_per_chapter(env):
    p = _make_project()
    result = storyboard.generate_storyboard(p["id"])  # 默认取项目设置 1
    assert result["pages_per_chapter"] == 1
    assert result["pages"] == 2


def test_pages_per_chapter_column_roundtrip(client):
    p = comic_service.create_project({"name": "列读写2", "pages_per_chapter": 5})
    assert comic_service.get_project(p["id"])["pages_per_chapter"] == 5
    comic_service.update_project(p["id"], {"pages_per_chapter": 8})
    assert comic_service.get_project(p["id"])["pages_per_chapter"] == 8
    # 越界被收敛到 [1,12]
    comic_service.update_project(p["id"], {"pages_per_chapter": 999})
    assert comic_service.get_project(p["id"])["pages_per_chapter"] == 12
    comic_service.update_project(p["id"], {"pages_per_chapter": 0})
    assert comic_service.get_project(p["id"])["pages_per_chapter"] == 1


def test_storyboard_route_pages_per_chapter(client):
    p = comic_service.create_project({
        "name": "路由测试2",
        "plot_summary": "第1集 A\n画面一。\n\n第2集 B\n画面二。",
    })
    resp = client.post(
        "/api/comic/projects/%d/storyboard" % p["id"], json={"pages_per_chapter": 2}
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["pages_per_chapter"] == 2
    assert data["pages"] == 4
    assert data["enqueued"] == 4


def test_split_scene_into_beats_more_sentences_than_n():
    beats = storyboard._split_scene_into_beats(
        "少年离开家乡。在森林遇见引路人。两人踏上旅途。", 2
    )
    assert len(beats) == 2
    assert all(b for b in beats)


def test_split_scene_into_beats_fewer_sentences_pads_with_framing():
    beats = storyboard._split_scene_into_beats("少年离开家乡。", 3)
    assert len(beats) == 3
    assert "少年离开家乡。" in beats[0]
    assert "（" in beats[1] and "（" in beats[2]  # 末句 + 镜头景别派生
    assert beats[1] != beats[2]


def test_split_scene_into_beats_clamps_n():
    beats = storyboard._split_scene_into_beats("一。二。三。", 99)
    assert len(beats) == storyboard.MAX_PAGES_PER_CHAPTER
