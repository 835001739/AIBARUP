"""M12 漫画「功能扩展」测试。

覆盖本轮新增的两条链路：
- **产出图回流图库**（``sync.scanner.register_image`` + ``comic.service.sync_gallery``）：
  漫画产出写在 ``data/comic_outputs``，图库扫描只覆盖 ComfyUI 产出目录，
  不回流的话「对分镜反推提示词 / 图库深链」对漫画一律不可用；
- **单页重扩写**（``comic.storyboard.reexpand_page``）：对某一页重跑扩写模型，
  区别于项目级「全本重刷（不重新扩写）」。

另含图库补登记路由与幂等性。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask
from PIL import Image

from config import Config
from core import db as core_db

from comic import routes as comic_routes
from comic import runner
from comic import service as comic_service
from comic import storyboard
from sync import scanner


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    (data_dir / "comic_outputs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")
    monkeypatch.setenv("AI_PROVIDER_ENABLED", "false")

    # runner.OUTPUT_ROOT 是 import 期绑定的模块级常量，必须直接打桩
    monkeypatch.setattr(runner, "OUTPUT_ROOT", data_dir / "comic_outputs")

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


def _mk(pages: int = 2):
    p = comic_service.create_project({"name": "扩展", "default_workflow": "flux_dev.json"})
    ch = comic_service.create_chapter(p["id"], {"title": "第1集"})
    created = [
        comic_service.create_page(ch["id"], {"title": "页%d" % (i + 1), "prompt_text": "分镜%d" % (i + 1)})
        for i in range(pages)
    ]
    return p, ch, created


def _page_row(page_id: int) -> dict:
    row = core_db.row_to_dict(core_db.query_one("SELECT * FROM comic_pages WHERE id=?", (page_id,)))
    assert row is not None
    return row


def _attach_png(page_id: int, color: tuple[int, int, int] = (10, 20, 30)) -> str:
    """造一张**真实** PNG 产出图并回填 image_path（图库登记需要 PIL 能解析）。"""
    page = _page_row(page_id)
    tmp = Path(Config.DATA_DIR) / "_seed.png"
    Image.new("RGB", (4, 4), color).save(tmp)
    rel = runner._save_output(page["project_id"], page["chapter_id"], page_id, tmp.read_bytes())
    tmp.unlink()
    core_db.execute("UPDATE comic_pages SET image_path=? WHERE id=?", (rel, page_id))
    return rel


# ---------------------------------------------------------------- 产出图回流图库


def test_register_image_inserts_into_gallery(env):
    """产出目录之外的图也能登记进图库，并生成 gallery 副本。"""
    src = env["data"] / "outside.png"
    Image.new("RGB", (6, 6), (1, 2, 3)).save(src)

    image_id = scanner.register_image(src, prompt="一只猫", workflow_link="flux_dev.json")

    assert image_id
    row = core_db.row_to_dict(core_db.query_one("SELECT * FROM images WHERE id=?", (image_id,)))
    assert row is not None
    assert row["prompt"] == "一只猫"
    assert row["workflow_link"] == "flux_dev.json"
    assert row["width"] == 6 and row["height"] == 6
    gallery = Path(Config.GALLERY_DIR) / (row["gallery_path"] or "").replace("gallery/", "")
    assert gallery.exists()


def test_register_image_is_idempotent(env):
    """同一张图重复登记只应命中既有记录，不重复复制、不重复插行。"""
    src = env["data"] / "same.png"
    Image.new("RGB", (4, 4), (9, 9, 9)).save(src)

    first = scanner.register_image(src)
    second = scanner.register_image(src)

    assert first == second
    assert int(core_db.query_scalar("SELECT COUNT(*) FROM images", default=0)) == 1


def test_register_image_returns_none_for_non_image(env):
    """解析不了的文件不应抛异常，也不能污染图库。"""
    junk = env["data"] / "junk.png"
    junk.write_bytes(b"NOT_AN_IMAGE")

    assert scanner.register_image(junk) is None
    assert int(core_db.query_scalar("SELECT COUNT(*) FROM images", default=0)) == 0


def test_runner_registers_gallery_on_success(env):
    """出图成功后应自动登记图库，并把 image_id 写回分镜页。"""
    _p, _ch, pages = _mk(pages=1)
    page = pages[0]
    tmp = env["data"] / "_seed.png"
    Image.new("RGB", (4, 4), (5, 6, 7)).save(tmp)
    data = tmp.read_bytes()

    rel = runner._save_output(_p["id"], page["chapter_id"], page["id"], data)
    image_id = runner._register_gallery(rel, page)

    assert image_id
    assert core_db.query_one("SELECT 1 FROM images WHERE id=?", (image_id,)) is not None


def test_runner_gallery_failure_is_non_fatal(env, monkeypatch: pytest.MonkeyPatch):
    """图库登记失败不能把「图已出好」变成失败任务。"""
    _p, _ch, pages = _mk(pages=1)

    def boom(*_a, **_kw):
        raise OSError("gallery 不可写")

    monkeypatch.setattr(scanner, "register_image", boom)
    assert runner._register_gallery("comic_outputs/1/1/1_x.png", pages[0]) is None


def test_sync_gallery_backfills_legacy_pages(env):
    """改造前出好的图（无 image_id）应能被一次性补登记。"""
    p, _ch, pages = _mk(pages=2)
    for i, page in enumerate(pages):
        _attach_png(page["id"], (i + 1, i + 1, i + 1))

    result = comic_service.sync_gallery(p["id"])

    assert result["scanned"] == 2
    assert result["registered"] == 2
    assert result["failed"] == 0
    for page in pages:
        assert (_page_row(page["id"])).get("image_id")


def test_sync_gallery_skips_already_registered(env):
    """重复补登记不应重复写库（幂等）。"""
    p, _ch, pages = _mk(pages=1)
    _attach_png(pages[0]["id"])

    comic_service.sync_gallery(p["id"])
    again = comic_service.sync_gallery(p["id"])

    assert again["registered"] == 0
    assert again["skipped"] == 1


def test_sync_gallery_clears_dangling_image_path(env):
    """库里有路径、磁盘上没文件的页应被清掉 —— 否则前端一直渲染破图。"""
    p, _ch, pages = _mk(pages=1)
    rel = _attach_png(pages[0]["id"])
    (Path(Config.DATA_DIR) / rel).unlink()

    result = comic_service.sync_gallery(p["id"])

    assert result["missing"] == 1
    assert result["failed"] == 0
    row = _page_row(pages[0]["id"])
    assert not (row.get("image_path") or "")
    assert not row.get("image_id")


def test_sync_gallery_rejects_unknown_project(env):
    p, _ch, _pages = _mk(pages=1)
    with pytest.raises(Exception):
        comic_service.sync_gallery(p["id"] + 99999)


def test_sync_gallery_route(env, client):
    p, _ch, pages = _mk(pages=1)
    _attach_png(pages[0]["id"])

    resp = client.post("/api/comic/projects/%d/sync-gallery" % p["id"], json={})

    assert resp.status_code == 200
    assert resp.get_json()["data"]["registered"] == 1


def test_page_get_route(env, client):
    """前端补登记后要按 id 取回新 image_id，路由必须存在。"""
    _p, _ch, pages = _mk(pages=1)

    resp = client.get("/api/comic/pages/%d" % pages[0]["id"])

    assert resp.status_code == 200
    assert resp.get_json()["data"]["id"] == pages[0]["id"]


# ---------------------------------------------------------------- 单页重扩写


def test_reexpand_page_recomposes_without_beat(env):
    """没有剧情原文时跳过扩写，但仍按当前角色卡与风格重算（不臆造、不失败）。"""
    p, _ch, pages = _mk(pages=1)
    page = comic_service.update_page(
        pages[0]["id"], {"base_prompt": "清晨的车站，少女独自等待", "base_negative": "blurry"}
    )
    assert page["prompt_text"]

    result = storyboard.reexpand_page(page["id"])

    assert result["reexpanded"] is False
    assert result["provider"] == "skip"
    assert result["warnings"], "缺少剧情原文时应给出提示"
    row = _page_row(page["id"])
    assert "清晨的车站" in (row["prompt_text"] or "")


def test_reexpand_page_reruns_expand_when_beat_exists(env, monkeypatch: pytest.MonkeyPatch):
    """有剧情原文时应真的重跑一次扩写，并把新的扩写结果存回 base_prompt。"""
    p, _ch, pages = _mk(pages=1)
    page = comic_service.update_page(
        pages[0]["id"],
        {"beat_text": "雨夜的车站，少女独自等待", "base_prompt": "旧扩写", "base_negative": ""},
    )

    calls: list[str] = []

    def fake_expand(text: str, profile: str, intensity: str, provider: str = "") -> dict:
        calls.append(text)
        return {
            "expanded_positive": "%s，扩写后的画面" % text,
            "expanded_negative": "low quality",
            "provider": "ai",
            "warnings": [],
        }

    monkeypatch.setattr(storyboard, "_expand_prompt", fake_expand)

    result = storyboard.reexpand_page(page["id"])

    assert calls == ["雨夜的车站，少女独自等待"]
    assert result["reexpanded"] is True
    assert result["provider"] == "ai"
    row = _page_row(page["id"])
    assert row["base_prompt"] == "雨夜的车站，少女独自等待，扩写后的画面"
    assert row["base_negative"] == "low quality"
    assert "扩写后的画面" in (row["prompt_text"] or "")


def test_reexpand_page_keeps_beat_text(env, monkeypatch: pytest.MonkeyPatch):
    """重扩写不能把剧情原文弄丢 —— 否则第二次重扩写就退化了。"""
    _p, _ch, pages = _mk(pages=1)
    page = comic_service.update_page(
        pages[0]["id"], {"beat_text": "原剧情", "base_prompt": "旧扩写"}
    )

    monkeypatch.setattr(
        storyboard,
        "_expand_prompt",
        lambda t, pf, it, provider="": {
            "expanded_positive": "新扩写",
            "expanded_negative": "",
            "provider": "ai",
            "warnings": [],
        },
    )
    storyboard.reexpand_page(page["id"])

    assert (_page_row(page["id"])).get("beat_text") == "原剧情"
    # 第二次仍然走真扩写，而不是退化成 skip
    assert storyboard.reexpand_page(page["id"])["reexpanded"] is True


def test_reexpand_page_rejects_empty_page(env):
    """全空的页不该静默成功，应给出可读报错。"""
    _p, _ch, pages = _mk(pages=1)
    comic_service.update_page(
        pages[0]["id"], {"prompt_text": "", "base_prompt": "", "beat_text": ""}
    )

    with pytest.raises(Exception):
        storyboard.reexpand_page(pages[0]["id"])


def test_reexpand_route_accepts_options(env, client, monkeypatch: pytest.MonkeyPatch):
    p, _ch, pages = _mk(pages=1)
    comic_service.update_page(pages[0]["id"], {"beat_text": "雨夜车站", "base_prompt": "旧"})
    monkeypatch.setattr(
        storyboard,
        "_expand_prompt",
        lambda t, pf, it, provider="": {
            "expanded_positive": "新",
            "expanded_negative": "",
            "provider": "ai",
            "warnings": [],
        },
    )

    resp = client.post(
        "/api/comic/pages/%d/reexpand" % pages[0]["id"], json={"requeue": False}
    )

    assert resp.status_code == 200
    body = resp.get_json()["data"]
    assert body["reexpanded"] is True
    assert body["job_id"] is None


def test_reexpand_route_can_requeue(env, client):
    p, _ch, pages = _mk(pages=1)
    comic_service.update_page(pages[0]["id"], {"beat_text": "雨夜车站", "base_prompt": "旧"})

    resp = client.post(
        "/api/comic/pages/%d/reexpand" % pages[0]["id"], json={"requeue": True}
    )

    body = resp.get_json()["data"]
    assert body["job_id"]
    assert (_page_row(pages[0]["id"]))["status"] == "queued"


def test_generate_storyboard_persists_beat_text(env):
    """生成分镜时必须把剧情原文落库，否则后面没法对单页重扩写。"""
    p = comic_service.create_project(
        {"name": "带原文", "default_workflow": "flux_dev.json", "plot_summary": "少年离家，踏上旅途。", "worldview": "现代都市"}
    )

    storyboard.generate_storyboard(p["id"], pages_per_chapter=1)

    for ch in comic_service.list_chapters(p["id"]):
        for page in comic_service.list_pages(ch["id"]):
            assert (page.get("beat_text") or "").strip(), "分镜页缺少剧情原文"
