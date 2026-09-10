"""M12 漫画工作室测试：服务层 CRUD + 出图入队编排 + REST 接口 + 工作流注入。

隔离策略（HARNESS §7）：数据落在 ``tmp_path`` 下，monkeypatch ``Config`` 后重置
``core.db`` 的线程本地连接，不污染仓库里的 ``data/``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db
from core.db import query_scalar
from core.errors import AIBARError

from comic import routes as comic_routes
from comic import runner as comic_runner
from comic import service as comic_service


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与运行期目录重定向到临时目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "comic_outputs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")

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
    """只注册 comic 蓝图，不依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(comic_routes.bp)
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------- 漫画（概览）


def test_create_and_get_project(env):
    p = comic_service.create_project({"name": "测试漫画", "description": "demo", "status": "production"})
    assert p["id"] > 0
    assert p["status"] == "production"
    got = comic_service.get_project(p["id"])
    assert got["name"] == "测试漫画"
    assert got["description"] == "demo"


def test_create_project_requires_name(env):
    with pytest.raises(AIBARError):
        comic_service.create_project({"description": "无名称"})


def test_update_and_delete_project(env):
    p = comic_service.create_project({"name": "A"})
    updated = comic_service.update_project(p["id"], {"name": "B", "status": "done"})
    assert updated["name"] == "B"
    assert updated["status"] == "done"
    comic_service.delete_project(p["id"])
    with pytest.raises(AIBARError):
        comic_service.get_project(p["id"])
    # 级联删除：章节也应被清掉
    assert query_scalar("SELECT COUNT(*) FROM comic_chapters WHERE project_id=?", (p["id"],)) == 0


def test_list_projects_aggregates_counts(env):
    p = comic_service.create_project({"name": "聚合"})
    comic_service.create_chapter(p["id"], {"title": "第1章"})
    items = comic_service.list_projects()
    assert items[0]["chapter_count"] == 1


# ---------------------------------------------------------------- 章节管理


def test_chapter_crud(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "第1章", "order_idx": 2})
    assert c["order_idx"] == 2
    comic_service.update_chapter(c["id"], {"title": "改名", "summary": "梗概"})
    got = comic_service.get_chapter(c["id"])
    assert got["title"] == "改名"
    assert got["summary"] == "梗概"
    comic_service.delete_chapter(c["id"])
    with pytest.raises(AIBARError):
        comic_service.get_chapter(c["id"])


def test_chapter_requires_project(env):
    with pytest.raises(AIBARError):
        comic_service.create_chapter(99999, {"title": "幽灵章节"})


def test_get_single_chapter_via_api(client):
    """章节下钻页 renderChapter 依赖 GET /api/comic/chapters/<id>；缺此接口会 405。"""
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "第1章", "summary": "梗概"})
    resp = client.get(f"/api/comic/chapters/{c['id']}")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["id"] == c["id"]
    assert data["title"] == "第1章"
    assert data["project_id"] == p["id"]


def test_get_single_chapter_404(client):
    resp = client.get("/api/comic/chapters/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------- 分镜（预制提示词 + 出图工作流）


def test_page_crud(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {
        "title": "分镜1",
        "prompt_text": "一位少女",
        "negative_text": "模糊",
        "workflow_filename": "wf.json",
        "seed": 12345,
        "order_idx": 1,
    })
    assert pg["prompt_text"] == "一位少女"
    assert pg["seed"] == 12345
    assert pg["status"] == "pending"
    comic_service.update_page(pg["id"], {"prompt_text": "改后的提示词", "seed": ""})
    got = comic_service.get_page(pg["id"])
    assert got["prompt_text"] == "改后的提示词"
    assert got["seed"] is None


def test_delete_chapter_cascades_pages_and_jobs(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {"title": "P1"})
    comic_service.enqueue_page(pg["id"])
    comic_service.delete_chapter(c["id"])
    assert query_scalar("SELECT COUNT(*) FROM comic_pages WHERE chapter_id=?", (c["id"],)) == 0
    assert query_scalar("SELECT COUNT(*) FROM comic_jobs WHERE chapter_id=?", (c["id"],)) == 0


# ---------------------------------------------------------------- 出图队列编排


def test_enqueue_sets_page_queued(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {"title": "P1"})
    res = comic_service.enqueue_page(pg["id"])
    assert res["status"] == "queued"
    assert query_scalar("SELECT status FROM comic_pages WHERE id=?", (pg["id"],)) == "queued"
    job = comic_service.list_jobs(project_id=p["id"])[0]
    assert job["page_id"] == pg["id"]
    assert job["status"] == "queued"


def test_enqueue_rejects_generating(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {"title": "P1"})
    comic_service.enqueue_page(pg["id"])
    # 模拟进行中
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (pg["id"],))
    with pytest.raises(AIBARError):
        comic_service.enqueue_page(pg["id"])


def test_regenerate_ignores_current_status(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {"title": "P1", "status": "done"})
    res = comic_service.regenerate_page(pg["id"])
    assert res["status"] == "queued"
    # 重新生成会再入队一个任务
    assert len(comic_service.list_jobs(project_id=p["id"])) == 1


def test_enqueue_chapter_and_project(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    comic_service.create_page(c["id"], {"title": "P1"})
    comic_service.create_page(c["id"], {"title": "P2"})
    res = comic_service.enqueue_chapter(c["id"])
    assert res["enqueued"] == 2
    res2 = comic_service.enqueue_project(p["id"])
    assert res2["enqueued"] == 2


def test_cancel_job_resets_page(env):
    p = comic_service.create_project({"name": "X"})
    c = comic_service.create_chapter(p["id"], {"title": "C"})
    pg = comic_service.create_page(c["id"], {"title": "P1"})
    comic_service.enqueue_page(pg["id"])
    job = comic_service.list_jobs(project_id=p["id"])[0]
    comic_service.cancel_job(job["id"])
    assert comic_service.list_jobs(project_id=p["id"])[0]["status"] == "cancelled"
    assert query_scalar("SELECT status FROM comic_pages WHERE id=?", (pg["id"],)) == "pending"


# ---------------------------------------------------------------- REST 接口


def test_rest_project_lifecycle(client):
    r = client.post("/api/comic/projects", json={"name": "REST漫画"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    pid = r.get_json()["data"]["id"]

    r = client.get("/api/comic/projects")
    assert r.get_json()["data"]["items"][0]["name"] == "REST漫画"

    r = client.patch(f"/api/comic/projects/{pid}", json={"status": "done"})
    assert r.get_json()["data"]["status"] == "done"

    r = client.delete(f"/api/comic/projects/{pid}")
    assert r.status_code == 200
    r = client.get(f"/api/comic/projects/{pid}")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_rest_invalid_body(client):
    r = client.post("/api/comic/projects", data="not-json", content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
    assert r.get_json()["error"]["code"] == "invalid_input"


def test_rest_full_hierarchy(client):
    p = client.post("/api/comic/projects", json={"name": "M"}).get_json()["data"]
    pid = p["id"]
    c = client.post(f"/api/comic/projects/{pid}/chapters", json={"title": "C1"}).get_json()["data"]
    cid = c["id"]
    pg = client.post(f"/api/comic/chapters/{cid}/pages", json={
        "title": "P1", "prompt_text": "正向", "negative_text": "负向", "workflow_filename": "wf.json"
    }).get_json()["data"]
    assert pg["prompt_text"] == "正向"

    r = client.post(f"/api/comic/pages/{pg['id']}/generate", json={})
    assert r.get_json()["data"]["status"] == "queued"

    r = client.get("/api/comic/jobs", query_string={"project_id": pid})
    assert len(r.get_json()["data"]["items"]) == 1


# ---------------------------------------------------------------- 工作流注入（runner）


def test_inject_prompt_into_clip_nodes():
    graph = {
        "10": {"class_type": "CLIPTextEncode", "inputs": {"text": "old_pos"}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "old_neg"}},
        "20": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }
    comic_runner._inject_prompt(graph, "新正向", "新负向", 777)
    assert graph["10"]["inputs"]["text"] == "新正向"
    assert graph["11"]["inputs"]["text"] == "新负向"
    assert graph["20"]["inputs"]["seed"] == 777


def test_inject_prompt_skips_seed_without_seed_node():
    graph = {"10": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}}}
    comic_runner._inject_prompt(graph, "正向", "", None)
    assert graph["10"]["inputs"]["text"] == "正向"


def test_first_image_extracts_output():
    entry = {
        "outputs": {
            "30": {"images": [{"filename": "a.png", "subfolder": "s", "type": "output"}]}
        }
    }
    img = comic_runner._first_image(entry)
    assert img["filename"] == "a.png"
    assert img["type"] == "output"


def test_build_api_graph_injects_prompt(monkeypatch, tmp_path):
    wf = tmp_path / "page.json"
    wf.write_text("{}")
    fake_graph = {
        "10": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }
    monkeypatch.setattr(comic_runner, "_workflow_file", lambda filename: wf)
    monkeypatch.setattr(comic_runner.workflow_convert, "convert_text", lambda raw: fake_graph)
    project = {"default_workflow": "default.json"}
    page = {"workflow_filename": "page.json", "prompt_text": "页面正向", "negative_text": "页面负向", "seed": None}
    out = comic_runner.build_api_graph(project, page)
    assert out["10"]["inputs"]["text"] == "页面正向"
    assert out["11"]["inputs"]["text"] == "页面负向"


def test_build_api_graph_falls_back_to_default_workflow(monkeypatch, tmp_path):
    wf = tmp_path / "default.json"
    wf.write_text("{}")
    fake_graph = {"10": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}
    monkeypatch.setattr(comic_runner, "_workflow_file", lambda filename: wf)
    monkeypatch.setattr(comic_runner.workflow_convert, "convert_text", lambda raw: fake_graph)
    project = {"default_workflow": "default.json"}
    page = {"workflow_filename": "", "prompt_text": "继承正向", "negative_text": "", "seed": None}
    out = comic_runner.build_api_graph(project, page)
    assert out["10"]["inputs"]["text"] == "继承正向"


def test_build_api_graph_missing_workflow_raises(monkeypatch):
    monkeypatch.setattr(comic_runner, "_workflow_file", lambda filename: None)
    with pytest.raises(AIBARError):
        comic_runner.build_api_graph({"default_workflow": "missing.json"}, {"workflow_filename": "", "prompt_text": "", "negative_text": "", "seed": None})


# ------------------------------------------ 落盘健壮性：清理旧图失败不能毁掉已出图
# 与 actors._save_actor_image 同构的隐患（2026-09-01）：分镜落盘原先同样是
# 「先删旧图再写新图」+ 只捕 (OSError, ValueError)。删除被运行环境拦截并以
# SystemExit 中断时，会穿透 generate_once 的全部 except Exception，
# 把已经出图成功（ComfyUI 排队 + 几十秒 GPU）的分镜整页打成 failed。


def _boom_unlink(*_a, **_kw):
    """模拟「删除被安全策略拦截」——抛 BaseException 子类而非 OSError。"""
    raise SystemExit(1)


def test_save_output_writes_new_file_even_when_purge_raises_systemexit(
    env, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        comic_runner, "output_root", lambda: Path(Config.DATA_DIR) / "comic_outputs"
    )
    monkeypatch.setattr(Path, "unlink", _boom_unlink)

    rel = comic_runner._save_output(1, 2, 3, b"\x89PNG-page-bytes")

    # 关键：新图必须照常落盘。旧图残留只是多占磁盘，丢图则要整条链路重跑。
    saved = Path(Config.DATA_DIR) / rel
    assert saved.is_file()
    assert saved.read_bytes() == b"\x89PNG-page-bytes"
    assert rel.startswith("comic_outputs/")
    assert saved.name.startswith("3_")


def test_save_output_purges_stale_but_keeps_current(env, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        comic_runner, "output_root", lambda: Path(Config.DATA_DIR) / "comic_outputs"
    )
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / "1" / "2"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "3_aaaaaaaaaaaaaaaa.png").write_bytes(b"old")
    (dest_dir / "4_bbbbbbbbbbbbbbbb.png").write_bytes(b"other-page")

    rel = comic_runner._save_output(1, 2, 3, b"\x89PNG-new")

    # glob 不保证顺序，用集合比较
    assert {p.name for p in dest_dir.glob("*.png")} == {
        Path(rel).name,        # 本次新图
        "4_bbbbbbbbbbbbbbbb.png",  # 别的页，不受影响
    }
    # 该页的旧图已被换掉，只留一张
    assert len(list(dest_dir.glob("3_*.png"))) == 1
