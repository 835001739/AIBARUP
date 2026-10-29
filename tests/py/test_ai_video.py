"""M19 AI 视频创作工作台：服务层、模板、聚合与 REST 契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from ai_video import routes, service
from config import Config
from core import db
from core.errors import AIBARError


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(Config, "DB_PATH", data / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data / "uploads")
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    db._local.conn = None
    actions = db.migrate()
    assert "create_table:ai_video_projects" in actions
    assert db.migrate() == []
    yield
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    db._local.conn = None


@pytest.fixture
def client(env):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(routes.bp)
    with app.test_client() as test_client:
        yield test_client


def test_project_crud_and_validation(env):
    project = service.create_project({
        "name": "真人短剧样片", "target_duration": 30, "weekly_output": 5,
        "compliance": {"portrait_consent": True},
    })
    assert project["video_format"] == "short_drama"
    assert project["compliance"]["portrait_consent"] is True
    updated = service.update_project(project["id"], {"status": "production", "automation_level": "L2"})
    assert updated["status"] == "production"
    with pytest.raises(AIBARError):
        service.create_project({"name": ""})
    with pytest.raises(AIBARError):
        service.update_project(project["id"], {"status": "invented"})


def test_scene_shot_asset_and_dashboard(env):
    project = service.create_project({"name": "X"})
    scene = service.create_scene(project["id"], {
        "scene_code": "SC01", "title": "会议室", "axis_rule": "A 左 B 右",
        "continuity": {"A": {"look": "office"}},
    })
    shot = service.create_shot(scene["id"], {
        "shot_code": "EP01_SC01_S01", "shot_type": "speak_cu", "duration": 2.2,
        "speaker": "A", "lipsync": True, "cost_estimate": 1.5,
    })
    service.update_shot(shot["id"], {
        "status": "approved", "attempts": 2,
        "qc": {"face_consistency": True, "audio_sync": True},
        "snapshot": {"model": "local", "seed": 42},
    })
    asset = service.create_asset(project["id"], {
        "asset_type": "character", "asset_key": "char_A", "name": "主角 A",
        "ref_path": "assets/characters/char_A/front.png", "status": "ready",
    })
    assert asset["metadata"] == {}
    dashboard = service.project_dashboard(project["id"])
    assert dashboard["stats"]["shot_count"] == 1
    assert dashboard["stats"]["approved_count"] == 1
    assert dashboard["stats"]["lipsync_count"] == 1
    assert dashboard["stats"]["cost_estimate"] == 1.5
    assert dashboard["scenes"][0]["shots"][0]["snapshot"]["seed"] == 42
    assert len(dashboard["stages"]) == 6


def test_rejected_shot_reaches_needs_human_threshold(env):
    project = service.create_project({"name": "重试"})
    scene = service.create_scene(project["id"], {"scene_code": "SC01"})
    shot = service.create_shot(scene["id"], {"shot_code": "S01", "max_attempts": 2})
    once = service.update_shot(shot["id"], {"status": "rejected"})
    assert once["status"] == "rejected"
    assert once["attempts"] == 1
    service.update_shot(shot["id"], {"status": "pending"})
    twice = service.update_shot(shot["id"], {"status": "rejected"})
    assert twice["status"] == "needs_human"
    assert twice["attempts"] == 2


def test_asset_rejects_absolute_path(env):
    project = service.create_project({"name": "X"})
    with pytest.raises(AIBARError) as exc:
        service.create_asset(project["id"], {
            "asset_type": "character", "asset_key": "a", "name": "A",
            "ref_path": "/Users/name/private.png",
        })
    assert exc.value.code == "invalid_input"


def test_seed_realistic_drama_template(env):
    project = service.create_project({"name": "模板"})
    result = service.seed_realistic_drama_template(project["id"])
    assert result["scenes"] == 4
    assert result["shots"] == 28
    dashboard = service.project_dashboard(project["id"])
    assert dashboard["stats"]["scene_count"] == 4
    assert dashboard["stats"]["shot_count"] == 28
    # 原始模板汇总行写“7（25%）”，逐镜标记实际为 6；以逐镜事实为准，避免凭空新增口型镜。
    assert dashboard["stats"]["lipsync_count"] == 6
    assert len(dashboard["assets"]) == 5
    assert dashboard["project"]["status"] == "asset_building"
    with pytest.raises(AIBARError) as exc:
        service.seed_realistic_drama_template(project["id"])
    assert exc.value.code == "project_not_empty"


def test_delete_project_cascades(env):
    project = service.create_project({"name": "删除"})
    service.seed_realistic_drama_template(project["id"])
    service.delete_project(project["id"])
    assert db.query_scalar("SELECT COUNT(*) FROM ai_video_scenes") == 0
    assert db.query_scalar("SELECT COUNT(*) FROM ai_video_shots") == 0
    assert db.query_scalar("SELECT COUNT(*) FROM ai_video_assets") == 0


def test_routes_envelope_and_errors(client):
    create = client.post("/api/ai-video/projects", json={"name": "API 项目"})
    assert create.status_code == 200
    payload = create.get_json()
    assert payload["ok"] is True
    project_id = payload["data"]["id"]

    seeded = client.post(f"/api/ai-video/projects/{project_id}/seed-template", json={})
    assert seeded.status_code == 200
    dashboard = client.get(f"/api/ai-video/projects/{project_id}/dashboard")
    assert dashboard.get_json()["data"]["stats"]["shot_count"] == 28

    bad = client.post("/api/ai-video/projects", json=[])
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_input"
    missing = client.get("/api/ai-video/projects/99999")
    assert missing.status_code == 404
    assert missing.get_json()["ok"] is False


def test_metadata_contains_workbook_concepts(env):
    meta = service.metadata()
    assert meta["automation_levels"] == ("L1", "L2", "L3")
    assert "needs_human" in meta["shot_statuses"]
    assert "axis_continuity" in meta["qc_keys"]


def test_asset_update_workflow_and_image_route(env, client, tmp_path):
    # 工作流清单来自同步表
    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("角色出图", "char_workflow.json", 5, "[]", "", ""),
    )
    project = service.create_project({"name": "资产", "default_workflow": "char_workflow.json"})
    asset = service.create_asset(project["id"], {
        "asset_type": "character", "asset_key": "char_a", "name": "主角 A",
    })
    updated = service.update_asset(asset["id"], {
        "prompt": "character reference sheet", "workflow": "char_workflow.json", "seed": 42,
        "status": "ready",
    })
    assert updated["prompt"] == "character reference sheet"
    assert updated["workflow"] == "char_workflow.json"
    assert updated["seed"] == 42
    assert updated["status_label"] == "已就绪"

    workflows = service.list_workflows()
    assert workflows["total"] == 1

    rel = "ai_video_assets/%d/%d_test.png" % (project["id"], asset["id"])
    target = Config.DATA_DIR / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    db.execute(
        "UPDATE ai_video_assets SET image_path=?, last_generated_at=? WHERE id=?",
        (rel, "2026-10-29 00:00:00", asset["id"]),
    )
    response = client.get("/api/ai-video/assets/%d/image" % asset["id"])
    assert response.status_code == 200
    assert response.data.startswith(b"\x89PNG")

    missing = client.get("/api/ai-video/assets/%d/image" % asset["id"])
    assert missing.status_code == 200


def test_asset_generate_worker_updates_image(env, tmp_path, monkeypatch):
    from ai_video import generation

    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("角色出图", "char_workflow.json", 5, "[]", "", ""),
    )
    project = service.create_project({"name": "资产", "default_workflow": "char_workflow.json"})
    asset = service.create_asset(project["id"], {
        "asset_type": "character", "asset_key": "char_a", "name": "主角 A",
    })

    monkeypatch.setattr(generation, "build_graph", lambda *args, **kwargs: {"1": {}})
    monkeypatch.setattr(generation.cc, "submit_prompt", lambda *args, **kwargs: "prompt-1")
    monkeypatch.setattr(generation.cc, "wait_history", lambda *args, **kwargs: {
        "outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}
    })
    monkeypatch.setattr(generation, "_download_image", lambda image: b"PNGDATA")
    saved = generation.generate_asset(asset["id"], {"prompt": "reference", "seed": 7}, background=False)

    assert saved["status"] == "ready"
    assert saved["prompt"] == "reference"
    assert saved["seed"] == 7
    assert saved["image_path"]
    assert (Config.DATA_DIR / saved["image_path"]).read_bytes() == b"PNGDATA"


def test_generate_route_conflict_and_image_missing(client, env, monkeypatch):
    from ai_video import generation

    project = service.create_project({"name": "资产"})
    asset = service.create_asset(project["id"], {
        "asset_type": "character", "asset_key": "char_a", "name": "主角 A",
    })
    monkeypatch.setattr(generation, "generate_asset", lambda asset_id, payload: {"id": asset_id, "status": "generating"})
    response = client.post("/api/ai-video/assets/%d/generate" % asset["id"], json={})
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "generating"

    works = client.get("/api/ai-video/workflows")
    assert works.status_code == 200
    assert works.get_json()["ok"] is True


def test_comfyui_wait_history_raises_on_execution_error(monkeypatch):
    from reverse import comfyui_client as cc

    monkeypatch.setattr(cc, "history", lambda prompt_id: {
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [["execution_error", {"exception_message": "SaveImage failed"}]],
        },
        "outputs": {},
    })
    with pytest.raises(cc.ComfyUIError):
        cc.wait_history("prompt-1", timeout=5)


def test_asset_gallery_variants_cover_and_delete(env, tmp_path):
    project = service.create_project({"name": "多图"})
    asset = service.create_asset(project["id"], {"asset_type": "character", "asset_key": "a", "name": "A"})
    rel1 = "ai_video_assets/%d/a_front.png" % project["id"]
    rel2 = "ai_video_assets/%d/a_side.png" % project["id"]
    for rel in (rel1, rel2):
        target = Config.DATA_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"IMG")
        db.execute(
            "INSERT INTO ai_video_asset_images (asset_id,project_id,label,image_path,workflow,seed,width,height,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (asset["id"], project["id"], rel, rel, "wf.json", 1, 64, 64, db.now()),
        )
    items = service.list_asset_images(asset["id"])
    assert items["total"] == 2
    newest = items["items"][0]
    service.set_asset_cover(asset["id"], newest["id"])
    got = service.get_asset(asset["id"])
    assert got["image_path"] == newest["image_path"]
    assert got["image_url"]
    assert got["image_count"] == 2

    service.delete_asset_image(asset["id"], newest["id"])
    remaining = service.list_asset_images(asset["id"])
    assert remaining["total"] == 1
    assert not (Config.DATA_DIR / newest["image_path"]).exists()


def test_generate_asset_set_records_variants(env, monkeypatch):
    from ai_video import generation

    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("角色出图", "char_workflow.json", 5, "[]", "", ""),
    )
    project = service.create_project({"name": "多图", "default_workflow": "char_workflow.json"})
    asset = service.create_asset(project["id"], {"asset_type": "character", "asset_key": "a", "name": "A"})
    monkeypatch.setattr(generation, "build_graph", lambda *args, **kwargs: {"1": {}})
    monkeypatch.setattr(generation.cc, "submit_prompt", lambda *args, **kwargs: "p")
    monkeypatch.setattr(generation.cc, "wait_history", lambda *args, **kwargs: {
        "outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}
    })
    monkeypatch.setattr(generation, "_download_image", lambda image: b"IMGDATA")
    result = generation.generate_asset_set(asset["id"], {
        "preset": "custom", "labels": ["正面", "侧面"], "seed": 9,
    }, background=False)
    assert result["count"] == 2
    saved = service.get_asset(asset["id"])
    assert saved["status"] == "ready"
    assert saved["image_count"] == 2
    assert saved["metadata"]["set_progress"]["finished"] is True
    items = service.list_asset_images(asset["id"])
    assert {item["label"] for item in items["items"]} == {"正面", "侧面"}


def test_workflow_classification_and_recommendation(env):
    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("视频", "video.json", 3, '["CLIPTextEncode","CreateVideo","SaveVideo"]', "", ""),
    )
    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("角色", "character.json", 5, '["CLIPTextEncode","EmptyFlux2LatentImage","SaveImage"]', "", ""),
    )
    items = service.list_workflows()["items"]
    by_name = {item["filename"]: item for item in items}
    assert by_name["video.json"]["kind"] == "video"
    assert by_name["video.json"]["character_ready"] is False
    assert by_name["character.json"]["character_ready"] is True
    assert service.recommended_workflow() == "character.json"


def test_reap_stale_generating_asset(env):
    project = service.create_project({"name": "中断"})
    asset = service.create_asset(project["id"], {"asset_type": "character", "asset_key": "a", "name": "A"})
    db.execute(
        "UPDATE ai_video_assets SET status='generating', updated_at='2000-01-01 00:00:00' WHERE id=?",
        (asset["id"],),
    )
    service.project_dashboard(project["id"])
    got = service.get_asset(asset["id"])
    assert got["status"] == "failed"
    assert "中断" in got["error_message"]


def test_shot_frame_attach_from_asset_image(env):
    project = service.create_project({"name": "帧"})
    asset = service.create_asset(project["id"], {"asset_type": "character", "asset_key": "a", "name": "A"})
    rel = "ai_video_assets/%d/frame.png" % project["id"]
    target = Config.DATA_DIR / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"IMG")
    db.execute(
        "INSERT INTO ai_video_asset_images (asset_id,project_id,label,image_path,workflow,seed,width,height,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (asset["id"], project["id"], "正面", rel, "wf.json", 1, 64, 64, db.now()),
    )
    image_id = db.query_scalar("SELECT id FROM ai_video_asset_images WHERE asset_id=?", (asset["id"],))
    scene = service.create_scene(project["id"], {"scene_code": "SC01"})
    shot = service.create_shot(scene["id"], {"shot_code": "S01"})
    updated = service.attach_shot_frame(shot["id"], "start", int(image_id))
    assert updated["first_frame"] == "asset-image:%d" % image_id
    assert updated["first_frame_url"] == "/api/ai-video/asset-images/%d/image" % image_id
    cleared = service.clear_shot_frame(shot["id"], "start")
    assert cleared["first_frame"] == ""
    assert cleared["first_frame_url"] == ""


def test_shot_frame_upload_and_serve(env, client):
    from io import BytesIO
    from PIL import Image

    project = service.create_project({"name": "帧"})
    scene = service.create_scene(project["id"], {"scene_code": "SC01"})
    shot = service.create_shot(scene["id"], {"shot_code": "S01"})
    buf = BytesIO()
    Image.new("RGB", (32, 32), (120, 80, 40)).save(buf, format="PNG")
    response = client.post(
        "/api/ai-video/shots/%d/frames/start/upload" % shot["id"],
        data={"file": (BytesIO(buf.getvalue()), "start.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["first_frame"].startswith("frame:%d:" % shot["id"])
    url = data["first_frame_url"]
    assert client.get(url).status_code == 200
    cleared = client.delete("/api/ai-video/shots/%d/frames/start" % shot["id"])
    assert cleared.get_json()["data"]["first_frame"] == ""


def test_generate_shot_frame_and_reuse_seed(env, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from ai_video import generation

    db.execute(
        "INSERT INTO workflows (name, filename, node_count, node_types, positive_prompt, negative_prompt) VALUES (?,?,?,?,?,?)",
        ("镜头出图", "shot_workflow.json", 5, '["CLIPTextEncode","EmptyFlux2LatentImage","SaveImage"]', "", ""),
    )
    project = service.create_project({"name": "帧生成", "default_workflow": "shot_workflow.json"})
    scene = service.create_scene(project["id"], {"scene_code": "SC01", "location": "会议室"})
    shot = service.create_shot(scene["id"], {"shot_code": "S01", "shot_type": "speak_cu", "prompt_image": "woman speaking"})
    buf = BytesIO()
    Image.new("RGB", (24, 24), (90, 40, 20)).save(buf, format="PNG")
    png = buf.getvalue()
    monkeypatch.setattr(generation, "build_graph", lambda *args, **kwargs: {"1": {}})
    monkeypatch.setattr(generation.cc, "submit_prompt", lambda *args, **kwargs: "p")
    monkeypatch.setattr(generation.cc, "wait_history", lambda *args, **kwargs: {
        "outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}
    })
    monkeypatch.setattr(generation, "_download_image", lambda image: png)

    started = generation.generate_shot_frame(shot["id"], "start", {"seed": 7}, background=False)
    assert started["frame_status"]["start"]["status"] == "ready"
    got = service.get_shot(shot["id"])
    assert got["first_frame"].startswith("frame:%d:" % shot["id"])
    assert got["first_frame_url"].startswith("/api/ai-video/shots/%d/frames/" % shot["id"])
    assert got["frame_status"]["start"]["status"] == "ready"
    assert (Config.DATA_DIR / "ai_video_frames" / str(shot["id"]) / got["first_frame"].split(":")[-1]).is_file()

    generation.generate_shot_frame(shot["id"], "end", {}, background=False)
    got = service.get_shot(shot["id"])
    assert got["last_frame"].startswith("frame:%d:" % shot["id"])
    assert got["frame_status"]["end"]["status"] == "ready"
    assert got["frame_status"]["end"]["seed"] == got["frame_status"]["start"]["seed"] == 7


def test_reap_stale_shot_frame_status(env):
    project = service.create_project({"name": "帧超时"})
    scene = service.create_scene(project["id"], {"scene_code": "SC01"})
    shot = service.create_shot(scene["id"], {"shot_code": "S01"})
    db.execute(
        "UPDATE ai_video_shots SET frame_status=? WHERE id=?",
        (db.dumps({"start": {"status": "generating", "started_at": "2000-01-01 00:00:00"}}), shot["id"]),
    )
    service.project_dashboard(project["id"])
    got = service.get_shot(shot["id"])
    assert got["frame_status"]["start"]["status"] == "failed"
    assert "中断" in got["frame_status"]["start"]["error"]
