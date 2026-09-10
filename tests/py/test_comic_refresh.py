"""M12 漫画工作室「持续打磨」第二轮测试。

覆盖本轮新增的四项能力：
- **主角贯穿**：``characters.select_page_characters`` 让主角在每一页都注入锚点；
- **分镜页差异化**：``storyboard.split_scene_into_beats`` 不再把所有补位页堆在末句上；
- **重建模式**：``generate_storyboard(mode="rebuild")`` 先清旧章节，不叠加重复分集；
- **提示词重刷**：改完角色卡 / 换风格后用 ``refresh_project_prompts`` 一键重算全本提示词；
- 附带：进度 ETA、runner 产物清理与正负向节点识别。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import characters
from comic import routes as comic_routes
from comic import runner as comic_runner
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


# ---------------------------------------------------------------- 主角贯穿


def test_is_main_tolerates_various_types():
    """数据库存 0/1，历史数据可能是字符串，都要能正确解析。"""
    assert characters.is_main({"is_main": 1}) is True
    assert characters.is_main({"is_main": "1"}) is True
    assert characters.is_main({"is_main": "true"}) is True
    assert characters.is_main({"is_main": True}) is True
    assert characters.is_main({"is_main": 0}) is False
    assert characters.is_main({"is_main": "0"}) is False
    assert characters.is_main({}) is False
    assert characters.is_main(None) is False


def test_select_page_characters_includes_main_without_name_hit():
    """主角即使在这一页没被提到名字，也要注入锚点（这是「贯穿」的意义）。"""
    main = {"id": 1, "name": "阿岚", "is_main": 1}
    side = {"id": 2, "name": "苏叶", "is_main": 0}
    chars = [main, side]

    # 这一页只提到苏叶
    picked = characters.select_page_characters("苏叶走进了城门。", chars)
    assert [c["name"] for c in picked] == ["阿岚", "苏叶"]

    # 这一页谁都没提到：仍然要有主角
    picked2 = characters.select_page_characters("天空泛起鱼肚白。", chars)
    assert [c["name"] for c in picked2] == ["阿岚"]

    # 关掉贯穿后退回纯文本命中
    picked3 = characters.select_page_characters("天空泛起鱼肚白。", chars, include_main=False)
    assert picked3 == []


def test_select_page_characters_keeps_card_order_and_dedupes():
    """返回顺序恒为角色卡顺序，且主角 + 文本命中不会重复。"""
    chars = [
        {"id": 2, "name": "苏叶", "is_main": 0},
        {"id": 1, "name": "阿岚", "is_main": 1},
    ]
    picked = characters.select_page_characters("阿岚与苏叶同行。", chars)
    assert [c["id"] for c in picked] == [2, 1]


def test_first_created_character_becomes_main(env):
    """第一个建的角色默认主角，之后默认配角（service 层行为）。"""
    pid = comic_service.create_project({"name": "主角默认"})["id"]
    a = comic_service.create_character(pid, {"name": "阿岚"})
    b = comic_service.create_character(pid, {"name": "苏叶"})
    assert characters.is_main(a) is True
    assert characters.is_main(b) is False

    # 可以显式改：把苏叶提为主角、阿岚降为配角
    comic_service.update_character(b["id"], {"is_main": True})
    comic_service.update_character(a["id"], {"is_main": False})
    assert characters.is_main(comic_service.get_character(b["id"])) is True
    assert characters.is_main(comic_service.get_character(a["id"])) is False


# ---------------------------------------------------------------- 分镜页差异化


def test_split_beats_cycles_sentences_when_short():
    """剧情只有 2 句但要 6 页时，应循环取句而不是把 4 页都堆在末句上。"""
    scene = "少年离开家乡。他在森林里迷路了。"
    beats = storyboard.split_scene_into_beats(scene, 6)
    assert len(beats) == 6
    texts = [b["beat"] for b in beats]
    # 两句都出现了，且不是「后 5 页都等于末句」
    assert texts.count("少年离开家乡。") == 3
    assert texts.count("他在森林里迷路了。") == 3


def test_split_beats_assigns_distinct_shots():
    """同一集内的景别应当轮换，避免 6 页用同一个镜头。"""
    beats = storyboard.split_scene_into_beats("少年离开家乡。他在森林里迷路了。", 6)
    shots = [b["shot"] for b in beats]
    assert len(set(shots)) == 6  # 6 页 6 个不同景别
    assert all(s.startswith("（") for s in shots)


def test_split_beats_samples_first_and_last_when_enough():
    scene = "一。二。三。四。五。"
    beats = storyboard.split_scene_into_beats(scene, 3)
    texts = [b["beat"] for b in beats]
    assert texts[0] == "一。"
    assert texts[-1] == "五。"


def test_split_beats_never_exceeds_cap():
    beats = storyboard.split_scene_into_beats("少年离开家乡。", 99)
    assert len(beats) == storyboard.MAX_PAGES_PER_CHAPTER


# ---------------------------------------------------------------- 重建模式


def _mk_project(env, plot: str, ppc: int = 2):
    return comic_service.create_project(
        {
            "name": "重建演示",
            "worldview": "蒸汽朋克浮空城",
            "plot_summary": plot,
            "pages_per_chapter": ppc,
        }
    )["id"]


def test_generate_storyboard_append_keeps_existing(env):
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。")
    storyboard.generate_storyboard(pid)
    first = len(comic_service.list_chapters(pid))
    storyboard.generate_storyboard(pid)
    assert len(comic_service.list_chapters(pid)) == first * 2


def test_generate_storyboard_rebuild_replaces_chapters(env):
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。")
    storyboard.generate_storyboard(pid)
    after_first = len(comic_service.list_chapters(pid))

    res = storyboard.generate_storyboard(pid, mode="rebuild")
    assert res["mode"] == "rebuild"
    assert res["removed_chapters"] == after_first
    assert len(comic_service.list_chapters(pid)) == after_first  # 清掉旧的、建了同样的新集


def test_rebuild_does_not_leave_orphan_pages(env):
    """重建时旧页与旧任务要一起删掉，不能留下孤儿数据。"""
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。")
    storyboard.generate_storyboard(pid)
    old_page_ids = [
        p["id"] for ch in comic_service.list_chapters(pid)
        for p in comic_service.list_pages(ch["id"])
    ]
    assert old_page_ids

    storyboard.generate_storyboard(pid, mode="rebuild")
    remaining = {p["id"] for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])}
    assert remaining.isdisjoint(old_page_ids)

    orphan_jobs = core_db.query_all(
        "SELECT id FROM comic_jobs WHERE page_id IN (%s)" % ",".join("?" * len(old_page_ids)),
        tuple(old_page_ids),
    )
    assert orphan_jobs == []


def test_invalid_mode_rejected(env):
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。")
    with pytest.raises(Exception):
        storyboard.generate_storyboard(pid, mode="nonsense")


# ---------------------------------------------------------------- 提示词重刷


def test_pages_persist_base_prompt_and_shot(env):
    """每一页都要存下原始扩写结果与景别，否则没法在不重拆剧情的前提下重刷。"""
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。他走进森林。")
    storyboard.generate_storyboard(pid)
    pages = [p for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    assert pages
    for pg in pages:
        assert pg["base_prompt"], "base_prompt 必须落库"
        assert pg["shot_note"], "shot_note 必须落库"
        assert pg["prompt_text"] != pg["base_prompt"] or not pg["character_names"]


def test_refresh_prompts_picks_up_edited_character(env):
    """改角色外貌 → 重刷 → 全本提示词同步更新（这是本次打磨的核心闭环）。"""
    pid = _mk_project(env, "第1集 启程\n阿岚离开家乡。他走进森林。")
    storyboard.generate_storyboard(pid)

    char = comic_service.list_characters(pid)[0]
    comic_service.update_character(char["id"], {"appearance": "银色短发，红色眼睛"})

    before = [p["prompt_text"] for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    res = storyboard.refresh_project_prompts(pid, requeue="none")
    after = [p["prompt_text"] for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]

    assert res["updated"] == len(before)
    assert res["changed"] > 0
    assert res["enqueued"] == 0  # requeue=none 时不出图
    assert any("银色短发" in t for t in after)
    assert not any("银色短发" in t for t in before)


def test_refresh_prompts_keeps_scene_text(env):
    """重刷只重做「锚点 / 风格 / 种子」，不应把剧情描述改掉。"""
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。他走进森林深处。")
    storyboard.generate_storyboard(pid)
    pages_before = [p for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    bases_before = [p["base_prompt"] for p in pages_before]

    storyboard.refresh_project_prompts(pid, requeue="none")
    pages_after = [p for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]

    assert [p["base_prompt"] for p in pages_after] == bases_before
    # 每一页扩写出来的画面描述仍应出现在最终提示词里
    for before, after in zip(pages_before, pages_after):
        scene = before["base_prompt"]
        assert scene[:8] in after["prompt_text"]


def test_refresh_prompts_is_idempotent(env):
    """同样输入重刷两次应得到完全相同的结果（种子与提示词都稳定）。"""
    pid = _mk_project(env, "第1集 启程\n阿岚离开家乡。他走进森林。")
    storyboard.generate_storyboard(pid)
    storyboard.refresh_project_prompts(pid, requeue="none")
    snap1 = [(p["prompt_text"], p["seed"]) for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    res = storyboard.refresh_project_prompts(pid, requeue="none")
    snap2 = [(p["prompt_text"], p["seed"]) for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    assert snap1 == snap2
    assert res["changed"] == 0


def test_refresh_prompts_can_switch_style(env):
    pid = _mk_project(env, "第1集 启程\n阿岚离开家乡。他走进森林。")
    storyboard.generate_storyboard(pid)
    res = storyboard.refresh_project_prompts(pid, style="shinkai", requeue="none")
    assert res["style"] == "shinkai"
    assert comic_service.get_project(pid)["style_preset"] == "shinkai"
    prompts = [p["prompt_text"] for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    assert prompts and all(len(t) > 0 for t in prompts)


def test_refresh_prompts_requeues_failed_only(env):
    """默认只重跑失败的页，成功的页不该被反复重出。"""
    pid = _mk_project(env, "第1集 启程\n阿岚离开家乡。他走进森林。")
    storyboard.generate_storyboard(pid)
    pages = [p for ch in comic_service.list_chapters(pid) for p in comic_service.list_pages(ch["id"])]
    assert len(pages) >= 2

    comic_service.update_page(pages[0]["id"], {"error_message": "x"})
    core_db.execute("UPDATE comic_pages SET status='failed' WHERE id=?", (pages[0]["id"],))
    core_db.execute("UPDATE comic_pages SET status='done' WHERE id=?", (pages[1]["id"],))

    res = storyboard.refresh_project_prompts(pid, requeue="failed")
    assert res["enqueued"] == 1


def test_refresh_prompts_rejects_bad_requeue(env):
    pid = _mk_project(env, "第1集 启程\n少年离开家乡。")
    storyboard.generate_storyboard(pid)
    with pytest.raises(Exception):
        storyboard.refresh_project_prompts(pid, requeue="maybe")


def test_refresh_prompts_handles_pages_without_base(env):
    """历史页/手工新建页没有 base_prompt 时用当前提示词兜底，不会被清空。"""
    pid = comic_service.create_project({"name": "历史页"})["id"]
    ch = comic_service.create_chapter(pid, {"title": "第1集", "summary": "老页"})
    pg = comic_service.create_page(ch["id"], {"title": "页1", "prompt_text": "少年站在城墙上"})
    assert pg["base_prompt"] == ""

    res = storyboard.refresh_project_prompts(pid, requeue="none")
    assert res["updated"] == 1
    after = comic_service.get_page(pg["id"])
    assert after["prompt_text"], "兜底逻辑不应把已有提示词清空"
    assert after["base_prompt"] == "少年站在城墙上"


def test_refresh_prompts_route(client, env):
    pid = client.post(
        "/api/comic/projects",
        json={"name": "路由重刷", "plot_summary": "第1集 启程\n阿岚离开家乡。他走进森林。"},
    ).get_json()["data"]["id"]
    client.post("/api/comic/projects/%d/storyboard" % pid, json={"pages_per_chapter": 2})

    res = client.post(
        "/api/comic/projects/%d/refresh-prompts" % pid, json={"requeue": "none"}
    ).get_json()
    assert res["ok"] is True
    assert "updated" in res["data"] and "changed" in res["data"]


def test_storyboard_route_accepts_mode(client, env):
    pid = client.post(
        "/api/comic/projects",
        json={"name": "模式路由", "plot_summary": "第1集 启程\n少年离开家乡。"},
    ).get_json()["data"]["id"]
    client.post("/api/comic/projects/%d/storyboard" % pid, json={"pages_per_chapter": 1})
    res = client.post(
        "/api/comic/projects/%d/storyboard" % pid,
        json={"pages_per_chapter": 1, "mode": "rebuild"},
    ).get_json()
    assert res["ok"] is True
    assert res["data"]["mode"] == "rebuild"
    assert res["data"]["removed_chapters"] >= 1


# ---------------------------------------------------------------- 进度 ETA


def test_progress_exposes_eta_fields(env):
    pid = comic_service.create_project({"name": "ETA"})["id"]
    data = comic_service.project_progress(pid)
    assert "eta_seconds" in data and "avg_job_seconds" in data
    assert data["eta_seconds"] == 0  # 没有样本时不瞎猜


def test_avg_job_seconds_uses_finished_jobs(env):
    pid = comic_service.create_project({"name": "ETA2"})["id"]
    ch = comic_service.create_chapter(pid, {"title": "第1集"})
    pg = comic_service.create_page(ch["id"], {"title": "页1", "prompt_text": "x"})
    core_db.execute(
        "INSERT INTO comic_jobs (page_id, chapter_id, project_id, status, stage, created_at, finished_at) "
        "VALUES (?,?,?, 'done', 'done', '2026-01-01 10:00:00', '2026-01-01 10:00:30')",
        (pg["id"], ch["id"], pid),
    )
    assert comic_service._avg_job_seconds(pid) == 30.0
    data = comic_service.project_progress(pid)
    assert data["avg_job_seconds"] == 30


# ---------------------------------------------------------------- runner 加固


def test_save_output_removes_previous_file(env, monkeypatch):
    """反复重生成同一页不应让旧图堆积在磁盘上。"""
    # OUTPUT_ROOT 是 import 时就绑定的模块级常量，monkeypatch Config 不生效，得直接改它
    monkeypatch.setattr(comic_runner, "OUTPUT_ROOT", env["data"] / "comic_outputs")

    saved_first = comic_runner._save_output(1, 2, 77, b"AAA")
    root = comic_runner.output_root() / "1" / "2"
    assert len(list(root.glob("77_*.png"))) == 1

    saved_second = comic_runner._save_output(1, 2, 77, b"BBB")
    files = list(root.glob("77_*.png"))
    assert len(files) == 1, "同一页只应保留最新一张"
    assert files[0].name != Path(saved_first).name
    assert files[0].name == Path(saved_second).name

    # 其它页的文件不受影响
    comic_runner._save_output(1, 2, 78, b"CCC")
    assert len(list(root.glob("77_*.png"))) == 1
    assert len(list(root.glob("78_*.png"))) == 1


def test_inject_prompt_respects_negative_node_title():
    """节点标题写着 negative 时，即使它排第一也不能当正向节点写。"""
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old neg"},
              "_meta": {"title": "Negative Prompt"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old pos"},
              "_meta": {"title": "Positive"}},
    }
    comic_runner._inject_prompt(graph, "NEW POS", "NEW NEG", None)
    assert graph["2"]["inputs"]["text"] == "NEW POS"
    assert graph["1"]["inputs"]["text"] == "NEW NEG"


def test_inject_prompt_detects_negative_by_content():
    """没有标题时，靠既有文本里的负面词也能认出负向节点。"""
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a girl"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "ugly, blurry, watermark"}},
    }
    comic_runner._inject_prompt(graph, "NEW POS", "NEW NEG", None)
    assert graph["1"]["inputs"]["text"] == "NEW POS"
    assert graph["2"]["inputs"]["text"] == "NEW NEG"


def test_inject_prompt_sets_seed_on_both_key_names():
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "p"}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "4": {"class_type": "RandomNoise", "inputs": {"noise_seed": 2}},
    }
    comic_runner._inject_prompt(graph, "P", "", 12345)
    assert graph["3"]["inputs"]["seed"] == 12345
    assert graph["4"]["inputs"]["noise_seed"] == 12345


def test_inject_prompt_handles_single_text_node():
    """只有一个文本节点时不能因为没有负向节点就整个跳过正向注入。"""
    graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}}}
    comic_runner._inject_prompt(graph, "NEW POS", "NEW NEG", None)
    assert graph["1"]["inputs"]["text"] == "NEW POS"
