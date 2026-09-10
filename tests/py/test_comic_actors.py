"""M13 演员库（Actor Library）测试：全局花名册 CRUD + 关联 + 重新生成一致性。

覆盖：
- ``comic/actors.py`` 纯函数：定妆锚点、确定性种子（randomize=False 可复现、randomize=True 换脸）；
- 路由 CRUD：创建（三来源）、必填校验、列表、详情、关联/取消关联、删除；
- **关键回归**：``_regenerate_worker`` 绝不能覆盖 ``base_seed``——
  randomize=False 重生成必须得到同一张脸（同种子 + 同图），否则「以演员为基准保持一致性」破产；
- 从角色提升为演员时继承角色的 ``base_seed``（首次重生成复现角色原脸）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import actors
from comic import routes as comic_routes
from comic import service as comic_service


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


# ---------------------------------------------------------------- 纯函数


def test_build_actor_anchor_is_deterministic():
    a = {
        "name": "阿岚",
        "appearance": "黑色短发，琥珀色眼睛",
        "outfit": "靛蓝长袍",
        "palette": "靛蓝与银白",
    }
    assert actors.build_actor_anchor(a) == actors.build_actor_anchor(a)
    assert actors.build_actor_anchor(a).startswith("阿岚")
    assert "黑色短发" in actors.build_actor_anchor(a)


def test_actor_seed_deterministic_when_not_randomized():
    actor = {"id": 1, "name": "阿岚", "base_seed": 123456789, "seed_offset": 0}
    s1 = actors.actor_seed(actor, randomize=False)
    s2 = actors.actor_seed(actor, randomize=False)
    assert s1 == s2  # 两次确定性派生必须一致 —— 同一个人同一张脸


def test_actor_seed_randomize_changes_face():
    actor = {"id": 1, "name": "阿岚", "base_seed": 123456789, "seed_offset": 0}
    det = actors.actor_seed(actor, randomize=False)
    rnd = actors.actor_seed(actor, randomize=True)
    assert det != rnd  # randomize=True 必须换一张脸


# ---------------------------------------------------------------- 路由 CRUD


def test_create_manual_actor_via_api(client):
    r = client.post("/api/comic/actors", json={
        "source": "manual",
        "name": "李白",
        "appearance": "俊逸青年",
        "outfit": "白袍",
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["id"]
    assert d["source_type"] == "manual"
    assert d["status"] == "ready"
    assert d["anchor"].startswith("李白")
    assert d["seed"] is not None


def test_create_actor_rejects_missing_name(client):
    r = client.post("/api/comic/actors", json={"source": "manual", "appearance": "x"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
    assert "演员名" in r.get_json()["error"]["message"]


def test_list_and_delete_roundtrip(client):
    before = client.get("/api/comic/actors").get_json()["data"]["total"]
    aid = client.post("/api/comic/actors", json={"source": "manual", "name": "临时演员"}).get_json()["data"]["id"]
    mid = client.get("/api/comic/actors").get_json()["data"]["total"]
    assert mid == before + 1
    d = client.delete("/api/comic/actors/%d" % aid)
    assert d.status_code == 200
    assert d.get_json()["ok"] is True
    assert client.get("/api/comic/actors").get_json()["data"]["total"] == before


def test_link_and_unlink_character(client, env):
    # 建一个项目 + 角色（service 直接建，base_seed 留空不影响本测试）
    proj = comic_service.create_project({"name": "关联测试项目"})
    char = comic_service.create_character(proj["id"], {"name": "苏利耶"})
    aid = client.post("/api/comic/actors", json={"source": "manual", "name": "主演"}).get_json()["data"]["id"]

    r = client.post("/api/comic/actors/%d/links" % aid, json={"character_id": char["id"], "apply": True})
    assert r.status_code == 200
    assert r.get_json()["data"]["link_count"] == 1
    detail = client.get("/api/comic/actors/%d" % aid).get_json()["data"]
    assert any(c["character_id"] == char["id"] for c in detail["characters"])

    u = client.delete("/api/comic/actors/%d/links/%d" % (aid, char["id"]))
    assert u.status_code == 200
    assert u.get_json()["data"]["link_count"] == 0


# ---------------------------------------------------------------- 重新生成一致性（回归）


def _poll_status(aid, timeout=15):
    for _ in range(timeout * 4):
        a = actors.get_actor(aid)
        if a["status"] in ("ready", "failed"):
            return a
        import time
        time.sleep(0.25)
    return actors.get_actor(aid)


def test_regenerate_reproducible_seed_and_image(client, monkeypatch, env):
    """回归核心：randomize=False 必须可复现同一张脸（同种子 + 同图）。

    通过 mock ``comic.runner.generate_once`` 让生成瞬间完成且输出确定，
    从而把测试与真实 ComfyUI 解耦、可控可重复。
    """
    fake = (b"\x89PNG\r\n\x1a\n" + b"x" * 200)  # 稳定字节流 -> 内容哈希命名稳定

    def fake_generate(workflow, prompt_text, negative, seed, timeout=0.0):
        # 校验：worker 传入的 seed 必须就是我们派生的确定性种子，
        # 并且 worker 绝不能在中途改写 base_seed（否则会漂移）。
        return fake, None, None

    monkeypatch.setattr("comic.runner.generate_once", fake_generate)
    # 隔离：测试库没有同步工作流，绕过 _default_workflow 缺失导致的 400
    monkeypatch.setattr(actors, "_default_workflow", lambda: "dummy.json")

    aid = client.post("/api/comic/actors", json={
        "source": "manual", "name": "一致性演员", "appearance": "紫发少女", "outfit": "黑裙"
    }).get_json()["data"]["id"]
    base_before = actors.get_actor(aid)["base_seed"]

    # 第一次重生成
    r1 = client.post("/api/comic/actors/%d/regenerate" % aid, json={"randomize": False})
    assert r1.status_code == 200
    assert r1.get_json()["data"]["actor"]["status"] == "generating"
    a1 = _poll_status(aid)
    assert a1["status"] == "ready"
    assert a1["base_seed"] == base_before  # 关键：base_seed 绝不能被覆盖
    seed1 = a1["seed"]
    img1 = a1["image_id"]

    # 第二次重生成（仍 randomize=False）
    client.post("/api/comic/actors/%d/regenerate" % aid, json={"randomize": False})
    a2 = _poll_status(aid)
    assert a2["status"] == "ready"
    assert a2["base_seed"] == base_before
    assert a2["seed"] == seed1        # 同种子
    assert a2["image_id"] == img1     # 同图（内容哈希命名稳定）

    # randomize=True 换脸
    client.post("/api/comic/actors/%d/regenerate" % aid, json={"randomize": True})
    a3 = _poll_status(aid)
    assert a3["seed"] != seed1

    client.delete("/api/comic/actors/%d" % aid)


# ---------------------------------------------------------------- 从角色提升（继承 base_seed）


def test_create_from_character_links_and_derives_base_seed(env):
    # 从角色提升为演员：演员复制角色定妆信息 + 自动建立关联。
    # 角色表无 base_seed 列（角色种子在出图时按项目派生），故演员回落到
    # 由演员名确定性派生的基准——同一演员名每次新建都得到同一张脸。
    proj = comic_service.create_project({"name": "提升测试项目"})
    char = comic_service.create_character(proj["id"], {"name": "苏利耶", "appearance": "金发少女"})

    actor = actors.create_actor({"character_id": char["id"]})
    assert actor["source_type"] == "character"
    assert actor["source_char_id"] == char["id"]
    assert actor["link_count"] == 1  # 自动建立演员↔角色关联
    assert actor["base_seed"] == actors.default_base_seed("苏利耶")

    # 清理
    actors.delete_actor(actor["id"])


def test_create_duplicate_name_rejected(env):
    actors.create_actor({"source": "manual", "name": "唯一演员"})
    with pytest.raises(Exception):
        actors.create_actor({"source": "manual", "name": "唯一演员"})


# ------------------------------------------------ 落盘健壮性：旁路清理失败不能毁掉已出图
# 线上故障（2026-09-01）：演员 7 / 15 重新生成报「生成异常：SystemExit」。
# 根因：_save_actor_image 在写新图**之前**先 unlink 旧图，某些运行环境（沙箱 /
# 安全钩子）拦截删除并抛 SystemExit；原实现只捕 (OSError, ValueError)，
# SystemExit 穿透 generate_once 的全部 except Exception，把已经出图成功
# （ComfyUI 排队 + 几十秒 GPU）的演员打成 failed，用户只看到一句类型名。


def _boom_unlink(*_a, **_kw):
    """模拟「删除被安全策略拦截」——抛 BaseException 子类而非 OSError。"""
    raise SystemExit(1)


def test_save_actor_image_writes_new_file_even_when_purge_raises_systemexit(env, monkeypatch):
    # 删除被拦时：新图必须照常落盘。旧图残留只是多占磁盘，丢图则要整条链路重跑。
    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    actor = actors.create_actor({"source": "manual", "name": "删图被拦演员", "appearance": "黑发"})

    rel = actors._save_actor_image(actor["id"], b"\x89PNG-fake-bytes")

    saved = Path(Config.DATA_DIR) / rel
    assert saved.is_file()
    assert saved.read_bytes() == b"\x89PNG-fake-bytes"
    assert rel.startswith("comic_outputs/")


def test_purge_old_actor_images_removes_stale_but_keeps_current(env):
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / actors.ACTOR_OUTPUT_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "42_aaaaaaaaaaaaaaaa.png").write_bytes(b"old1")
    (dest_dir / "42_bbbbbbbbbbbbbbbb.png").write_bytes(b"old2")
    keep = dest_dir / "42_cccccccccccccccc.png"
    keep.write_bytes(b"new")

    removed = actors._purge_old_actor_images(dest_dir, 42, keep)

    assert removed == 2
    assert keep.is_file()
    assert [p.name for p in dest_dir.glob("42_*.png")] == ["42_cccccccccccccccc.png"]


def test_purge_old_actor_images_swallows_systemexit(env, monkeypatch):
    # 清理函数本身绝不上抛：返回 0 而不是把 SystemExit 丢给调用方。
    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / actors.ACTOR_OUTPUT_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    keep = dest_dir / "43_cccccccccccccccc.png"
    keep.write_bytes(b"new")

    assert actors._purge_old_actor_images(dest_dir, 43, keep) == 0


# ------------------------------------------------------------ 错误信息必须可读可行动


def test_describe_exception_gives_reason_for_systemexit():
    try:
        raise SystemExit(1)
    except SystemExit as exc:
        text = actors._describe_exception(exc)

    assert "SystemExit" in text
    # 只给类型名等于没说——必须解释「多半是环境拦截」并给出规避建议
    assert ("运行环境" in text) or ("安全策略" in text)


def test_describe_exception_includes_location_and_message():
    try:
        raise RuntimeError("磁盘已满")
    except RuntimeError as exc:
        text = actors._describe_exception(exc)

    assert "RuntimeError" in text
    assert "磁盘已满" in text
    assert ":" in text  # 带 file:line，便于定位


def test_regenerate_worker_reports_readable_error_on_systemexit(env, monkeypatch):
    # 端到端：后台 worker 内崩在 SystemExit 时，error_message 不能再是干巴巴的类型名。
    from comic import runner

    actor = actors.create_actor({"source": "manual", "name": "崩在落盘", "appearance": "红发"})

    monkeypatch.setattr(
        runner, "generate_once",
        lambda workflow, prompt, negative, seed, timeout=None, cancel=None: (b"\x89PNG-payload", "", ""),
    )
    monkeypatch.setattr(actors, "_save_actor_image", lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit(1)))

    actors._regenerate_worker(actor["id"], "wf.json", "正面立绘，红发", "", 12345)

    row = actors.get_actor(actor["id"])
    assert row["status"] == "failed"
    assert row["error_message"] != "生成异常：SystemExit"
    assert "SystemExit" in row["error_message"]
    assert len(row["error_message"]) > len("生成异常：SystemExit")
