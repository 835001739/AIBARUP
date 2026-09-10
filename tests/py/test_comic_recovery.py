"""M12 漫画出图「可靠性加固」测试。

覆盖本轮修复的四类真实缺陷：
- **孤儿任务回收**（``comic/recovery.py``）：进程被杀留下的 running/generating 能复位重跑；
- **取消不被覆盖**（``runner`` 收尾守卫）：出图期间取消，任务不会被复活成 done/failed；
- **原子抢占**（``runner._claim_job``）：同一任务不会被两个 worker 抢到；
- **下载上限 / 删盘**（``runner._download_output_bytes`` / ``service`` 删除级联清理文件）。

另含 worker 启动幂等、心跳状态、队列列表上限、孤儿产出文件清理。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import recovery
from comic import routes as comic_routes
from comic import runner
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


def _mk(env, pages: int = 3, workflow: str = "flux_dev.json"):
    p = comic_service.create_project({"name": "可靠性", "default_workflow": workflow})
    ch = comic_service.create_chapter(p["id"], {"title": "第1集"})
    created = []
    for i in range(pages):
        created.append(
            comic_service.create_page(
                ch["id"], {"title": "页%d" % (i + 1), "prompt_text": "分镜%d" % (i + 1)}
            )
        )
    return p, ch, created


def _job_row(job_id: int) -> dict:
    row = core_db.row_to_dict(core_db.query_one("SELECT * FROM comic_jobs WHERE id=?", (job_id,)))
    assert row is not None
    return row


def _page_row(page_id: int) -> dict:
    row = core_db.row_to_dict(core_db.query_one("SELECT * FROM comic_pages WHERE id=?", (page_id,)))
    assert row is not None
    return row


def _attach_output(page_id: int) -> str:
    """产出图落盘并回填 ``image_path``（真实链路里这两步都在 ``_execute`` 里完成）。"""
    page = _page_row(page_id)
    rel = runner._save_output(page["project_id"], page["chapter_id"], page_id, b"PNGDATA")
    core_db.execute("UPDATE comic_pages SET image_path=? WHERE id=?", (rel, page_id))
    return rel


def _backdate_job(job_id: int, seconds: int) -> None:
    """把任务的创建时间往前调，模拟「跑了很久还没收尾」。"""
    past = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds))
    core_db.execute("UPDATE comic_jobs SET created_at=?, status='running' WHERE id=?", (past, job_id))


# ---------------------------------------------------------------- 孤儿任务回收


def test_reap_resets_stuck_running_job(env):
    """卡在 running 的旧任务应被标 failed，并写清中文原因。"""
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    _backdate_job(job["job_id"], 3600)

    result = recovery.reap_stale_jobs(max_age_seconds=60)

    assert result["stale_jobs"] == 1
    row = _job_row(job["job_id"])
    assert row["status"] == "failed"
    assert row["error_code"] == "interrupted"
    assert "重新出图" in (row["error_message"] or "")


def test_reap_frees_generating_page_so_it_can_be_requeued(env):
    """回收后页面必须回到 pending —— 否则 _enqueue 与整本出图都会跳过它。"""
    p, _ch, pages = _mk(env, pages=1)
    page = pages[0]
    job = comic_service.enqueue_page(page["id"])
    _backdate_job(job["job_id"], 3600)
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (page["id"],))

    # 回收前：_enqueue 拒收
    with pytest.raises(Exception):
        comic_service.enqueue_page(page["id"])

    recovery.reap_stale_jobs(max_age_seconds=60)

    assert _page_row(page["id"])["status"] == "pending"
    # 回收后：可以重新入队了
    assert comic_service.enqueue_page(page["id"])["status"] == "queued"


def test_reap_leaves_fresh_running_job_alone(env):
    """刚刚开始跑的任务不能被误伤。"""
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    core_db.execute("UPDATE comic_jobs SET status='running' WHERE id=?", (job["job_id"],))

    result = recovery.reap_stale_jobs(max_age_seconds=900)

    assert result["stale_jobs"] == 0
    assert _job_row(job["job_id"])["status"] == "running"


def test_reap_resets_orphan_generating_page_without_job(env):
    """页面停在 generating 但任务已被删/已终态 —— 同样是孤儿，要复位。"""
    _p, _ch, pages = _mk(env, pages=1)
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (pages[0]["id"],))

    result = recovery.reap_stale_jobs()

    assert result["reset_pages"] == 1
    assert _page_row(pages[0]["id"])["status"] == "pending"


def test_reap_dry_run_changes_nothing(env):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    _backdate_job(job["job_id"], 3600)
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (pages[0]["id"],))

    result = recovery.reap_stale_jobs(max_age_seconds=60, dry_run=True)

    assert result["dry_run"] is True
    assert result["stale_jobs"] == 1
    assert _job_row(job["job_id"])["status"] == "running"  # 没被改动
    assert _page_row(pages[0]["id"])["status"] == "generating"


def test_reap_can_requeue_immediately(env):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    _backdate_job(job["job_id"], 3600)
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (pages[0]["id"],))

    result = recovery.reap_stale_jobs(max_age_seconds=60, requeue=True)

    assert result["requeued"] == 1
    assert _page_row(pages[0]["id"])["status"] == "queued"


def test_reap_scoped_to_project(env):
    p1, _c1, pages1 = _mk(env, pages=1)
    p2 = comic_service.create_project({"name": "另一个"})
    p2_ch = comic_service.create_chapter(p2["id"], {"title": "第1集"})
    page2 = comic_service.create_page(p2_ch["id"], {"title": "页", "prompt_text": "x"})
    j1 = comic_service.enqueue_page(pages1[0]["id"])
    j2 = comic_service.enqueue_page(page2["id"])
    _backdate_job(j1["job_id"], 3600)
    _backdate_job(j2["job_id"], 3600)

    result = recovery.reap_stale_jobs(max_age_seconds=60, project_id=p1["id"])

    assert result["stale_jobs"] == 1
    assert _job_row(j1["job_id"])["status"] == "failed"
    assert _job_row(j2["job_id"])["status"] == "running"


def test_stale_summary_is_read_only(env):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    _backdate_job(job["job_id"], 3600)

    summary = recovery.stale_summary()

    assert summary["dry_run"] is True
    assert summary["stale_jobs"] == 1
    assert _job_row(job["job_id"])["status"] == "running"


def test_maintenance_routes(env, client):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    _backdate_job(job["job_id"], 3600)

    r = client.get("/api/comic/maintenance/stale")
    assert r.status_code == 200
    assert r.get_json()["data"]["stale_jobs"] == 1
    # 只读体检不改动
    assert _job_row(job["job_id"])["status"] == "running"

    r = client.post("/api/comic/maintenance/reap", json={"max_age_seconds": 60})
    assert r.status_code == 200
    assert r.get_json()["data"]["stale_jobs"] == 1
    assert _job_row(job["job_id"])["status"] == "failed"


# ---------------------------------------------------------------- 取消不被覆盖


def test_claim_job_is_atomic(env):
    """抢占后第二次抢占必须失败，避免同一任务被跑两遍。"""
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])

    assert runner._claim_job(job["job_id"]) is True
    assert runner._claim_job(job["job_id"]) is False


def test_claim_job_refuses_non_queued(env):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    comic_service.cancel_job(job["job_id"])

    assert runner._claim_job(job["job_id"]) is False


def test_success_update_does_not_resurrect_cancelled_job(env, monkeypatch):
    """出图期间被取消：图虽跑完，任务与页面都必须保持取消时的状态。"""
    p, _ch, pages = _mk(env, pages=1)
    page = pages[0]
    job = comic_service.enqueue_page(page["id"])
    assert runner._claim_job(job["job_id"]) is True
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (page["id"],))

    # 让出图链路直接成功
    monkeypatch.setattr(runner, "build_api_graph", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "_cc", type(
        "C", (), {
            "submit_prompt": staticmethod(lambda *a, **k: "pid-1"),
            "wait_history": staticmethod(lambda *a, **k: {"outputs": {"9": {"images": [
                {"filename": "a.png", "subfolder": "", "type": "output"}]}}}),
            "ComfyUIError": runner._cc.ComfyUIError,
        }
    )())
    monkeypatch.setattr(runner, "_download_output_bytes", lambda *a, **k: b"\x89PNG\r\n\x1a\n")

    # 出图途中用户取消
    comic_service.cancel_job(job["job_id"])
    runner.run_job(job["job_id"])

    assert _job_row(job["job_id"])["status"] == "cancelled"


def test_job_still_running_reflects_cancel(env):
    p, _ch, pages = _mk(env, pages=1)
    job = comic_service.enqueue_page(pages[0]["id"])
    runner._claim_job(job["job_id"])

    assert runner._job_still_running(job["job_id"]) is True
    comic_service.cancel_job(job["job_id"])
    assert runner._job_still_running(job["job_id"]) is False


# ---------------------------------------------------------------- 下载上限


def test_download_rejects_oversized_image(env, monkeypatch):
    """超过上限的图必须中断下载并返回 None，不能整个读进内存。"""
    captured = {}

    class _Resp:
        status_code = 200

        def iter_content(self, chunk_size=65536):
            captured["streamed"] = True
            # 每次 1MB，共 100 次 = 100MB，超过 64MB 上限
            for _ in range(100):
                yield b"x" * (1024 * 1024)

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(runner, "DOWNLOAD_MAX_BYTES", 8 * 1024 * 1024)

    assert runner._download_output_bytes("a.png", "", "output") is None
    assert captured.get("streamed") is True
    assert captured.get("closed") is True  # 中断也要关连接


def test_download_accepts_normal_image(env, monkeypatch):
    class _Resp:
        status_code = 200

        def iter_content(self, chunk_size=65536):
            yield b"PNGDATA"

        def close(self):
            pass

    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: _Resp())
    assert runner._download_output_bytes("a.png", "", "output") == b"PNGDATA"


# ---------------------------------------------------------------- worker 幂等与心跳


def test_worker_start_is_idempotent():
    w = runner.ComicWorker(poll_interval=0.05)
    try:
        t1 = w.start()
        t2 = w.start()
        assert t1 is t2
        assert w.is_alive() is True
    finally:
        w.stop()


def test_worker_status_reports_heartbeat():
    status = runner.worker_status()
    assert "alive" in status
    assert "stale_seconds" in status
    assert "restarts" in status


# ---------------------------------------------------------------- 删除级联清理文件


def test_delete_page_removes_output_file(env):
    _p, _ch, pages = _mk(env, pages=1)
    page = pages[0]
    rel = _attach_output(page["id"])
    assert (env["data"] / rel).is_file()

    comic_service.delete_page(page["id"])

    assert not (env["data"] / rel).exists()


def test_delete_chapter_removes_output_files(env):
    _p, _ch, pages = _mk(env, pages=2)
    rels = [_attach_output(pg["id"]) for pg in pages]

    comic_service.delete_chapter(pages[0]["chapter_id"])

    assert all(not (env["data"] / r).exists() for r in rels)


def test_delete_project_removes_output_dir(env):
    p, _ch, pages = _mk(env, pages=1)
    rel = _attach_output(pages[0]["id"])
    assert (env["data"] / rel).is_file()

    comic_service.delete_project(p["id"])

    assert not (env["data"] / "comic_outputs" / str(p["id"])).exists()


def test_cleanup_orphan_outputs(env, caplog):
    _p, _ch, pages = _mk(env, pages=1)
    kept = _attach_output(pages[0]["id"])
    orphan_dir = env["data"] / kept
    orphan = orphan_dir.parent / "999_deadbeef.png"
    orphan.write_bytes(b"ORPHAN")

    result = recovery.cleanup_orphan_outputs()

    assert result["removed"] == 1
    assert not orphan.exists()
    assert (env["data"] / kept).is_file()  # 正常页的图不能误删


def test_cleanup_orphan_outputs_dry_run(env):
    _p, ch, _pages = _mk(env, pages=1)
    orphan = env["data"] / "comic_outputs" / "1" / str(ch["id"]) / "999_deadbeef.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"ORPHAN")

    result = recovery.cleanup_orphan_outputs(dry_run=True)

    assert result["removed"] == 1
    assert orphan.exists()


# ---------------------------------------------------------------- 队列列表上限


def test_list_jobs_has_default_limit(env):
    p, _ch, pages = _mk(env, pages=3)
    for pg in pages:
        comic_service.enqueue_page(pg["id"])

    items = comic_service.list_jobs(project_id=p["id"])
    assert len(items) == 3

    # 上限生效：只取最近 2 条
    assert len(comic_service.list_jobs(project_id=p["id"], limit=2)) == 2


def test_enqueue_all_or_nothing(env, monkeypatch):
    """批量入队中途失败不能留下「入了一半」的半批状态。"""
    p, _ch, pages = _mk(env, pages=3)
    original = core_db.execute
    calls = {"n": 0}

    def _flaky(sql, params=()):
        if "INSERT INTO comic_jobs" in sql:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
        return original(sql, params)

    monkeypatch.setattr(core_db, "execute", _flaky)
    with pytest.raises(RuntimeError):
        comic_service.enqueue_chapter(pages[0]["chapter_id"])

    monkeypatch.setattr(core_db, "execute", original)
    # 事务回滚：一个都没入进去
    assert len(comic_service.list_jobs(project_id=p["id"])) == 0


def test_enqueue_project_filters_by_status(env):
    p, _ch, pages = _mk(env, pages=3)
    core_db.execute("UPDATE comic_pages SET status='failed' WHERE id=?", (pages[0]["id"],))
    core_db.execute("UPDATE comic_pages SET status='done' WHERE id=?", (pages[1]["id"],))

    result = comic_service.enqueue_project(p["id"], statuses=["failed"])

    assert result["enqueued"] == 1


def test_enqueue_project_skips_generating(env):
    p, _ch, pages = _mk(env, pages=2)
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (pages[0]["id"],))

    result = comic_service.enqueue_project(p["id"])

    assert result["enqueued"] == 1


def test_project_generate_route_filters_by_shot(env, client):
    p, ch, pages = _mk(env, pages=3)
    comic_service.update_page(pages[0]["id"], {"shot_note": "近景"})
    comic_service.update_page(pages[1]["id"], {"shot_note": "远景"})

    r = client.post(f"/api/comic/projects/{p['id']}/generate", json={"shot": "近景"})

    assert r.status_code == 200
    assert r.get_json()["data"]["enqueued"] == 1
    assert _page_row(pages[0]["id"])["status"] == "queued"
    assert _page_row(pages[1]["id"])["status"] == "pending"


def test_cleanup_orphan_outputs_spares_actor_portraits(env):
    """演员定妆图不能被当成孤儿分镜产出删掉。

    两者同在 ``comic_outputs`` 下、命名都是 ``<id>_<hash>.png``，但 actor_id 与
    page_id 是两个互不相干的编号空间。修复前只要 comic_pages 里没有同号页面，
    演员图每次启动都会被删一次，演员库会莫名其妙掉图。
    """
    _p, _ch, pages = _mk(env, pages=1)
    _attach_output(pages[0]["id"])

    actors_dir = env["data"] / "comic_outputs" / "actors"
    actors_dir.mkdir(parents=True, exist_ok=True)
    # 这三个 actor_id 在 comic_pages 里都不存在（页 id 只有 1）
    portraits = []
    for actor_id in (7, 17, 18):
        f = actors_dir / f"{actor_id}_deadbeef{actor_id}.png"
        f.write_bytes(b"PORTRAIT")
        portraits.append(f)

    result = recovery.cleanup_orphan_outputs()

    assert result["removed"] == 0
    for f in portraits:
        assert f.is_file(), f"演员定妆图被误删：{f.name}"
    # 顺带确认扫描确实覆盖了分镜目录（不是因为没扫到才没删）
    assert result["scanned"] >= 1


def test_cleanup_orphan_outputs_spares_shotgroup_frames(env):
    """组图产出图不能被当成孤儿分镜产出删掉。

    真实事故（2026-09-02 23:09）：服务启动时 recovery 跑完留下
    ``comic_orphan_outputs_removed bytes=4542813 removed=3``，把刚生成的 3 张
    组图产出（每张 ~1.4MB）全删干净——DB 完好、磁盘空空，玩家空白。

    根因：组图片命名是 ``<frame_id>_<hash>.png``，frame_id 与 comic_pages 的
    page_id 是**两个互不相干的编号空间**，reaper 只看了 comic_pages，活着的
    frame_id 都不在它的 alive 集合里，于是全被当成孤儿。同样的修法：把
    ``shotgroups/`` 加进 skip_dirs。
    """
    _p, _ch, pages = _mk(env, pages=1)  # 只有 1 个 comic_page（id=1）
    _attach_output(pages[0]["id"])

    # 创建 3 个组图产出文件，frame_id 都与 comic_pages 同号（1,2,3），
    # 模拟「活 frame_id 不在 reaper 的 alive 集合里」的真实场景。
    from comic import groups as groups_mod

    shotgroups_dir = env["data"] / "comic_outputs" / groups_mod.GROUPS_OUTPUT_SUBDIR / "1"
    shotgroups_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for frame_id in (1, 2, 3):
        f = shotgroups_dir / f"{frame_id}_cafebabe{frame_id}.png"
        f.write_bytes(b"FRAME")
        frames.append(f)

    result = recovery.cleanup_orphan_outputs()

    assert result["removed"] == 0
    for f in frames:
        assert f.is_file(), f"组图片被误删：{f.name}"
    # 而且空目录（被 reaper 顺手 rmdir）不应该出现，否则下次出图又要补建
    assert shotgroups_dir.is_dir(), "shotgroups 目录被顺手删了"


def test_cleanup_orphan_outputs_still_removes_real_orphans(env):
    """skip_dirs 只豁免已知业务目录，真正的孤儿（非分镜/非演员/非组图片）仍要清。

    防「过保护」回归：前面两个 spares_x 测试是把命名撞 id 的文件当噪声验证，
    这里再验一道：完全不属于 comic_pages / actors / shotgroups 的孤儿
    （比如用户从其它目录拼错过来的乱命名文件）依然要被 reaper 删掉。
    """
    # 一张正常分镜图（保留）
    _p, _ch, pages = _mk(env, pages=1)
    _attach_output(pages[0]["id"])

    # 一张**正经孤儿**分镜图：页 id 不在 comic_pages 里
    orphan = env["data"] / "comic_outputs" / "9" / "999_orphanhash.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"ORPHAN")

    result = recovery.cleanup_orphan_outputs()

    assert not orphan.is_file(), "正经孤儿未被清理（reaper 误把所有分镜目录都豁免了）"
    assert result["removed"] >= 1


# ---------------------------------------------------------- shot_frames stale reap


def test_reap_stale_frames_resets_orphan_to_failed(env):
    """AIBAR 被 kill 时跑着的帧会卡在 generating；reap 必须把它复位成 failed
    （带可读原因），否则 /generate /regenerate 都会被 _claim_frame 的
    status<>'generating' 守门挡下，用户永远救不回来。
    """
    from comic import groups as groups_mod

    group = groups_mod.create_group({
        "name": "stale 测试",
        "preset_prefix": "测试",
        "actions": ["一", "二", "三"],
    })
    # 第二帧模拟「跑了一半被杀」：把 updated_at 拨到 30 分钟前，超过 15 分钟阈值
    stuck_frame_id = group["frames"][1]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-30 minutes') WHERE id=?",
        (core_db.now(), stuck_frame_id),
    )

    result = recovery.reap_stale_frames()

    assert result["dry_run"] is False
    assert stuck_frame_id in result["frame_ids"]
    after = core_db.query_one("SELECT status, error_message FROM shot_frames WHERE id=?", (stuck_frame_id,))
    assert after["status"] == "failed"
    assert "进程退出" in after["error_message"]


def test_reap_stale_frames_dry_run_changes_nothing(env):
    """dry_run 必须只统计不写库。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "dry-run 测试", "actions": ["a"]})
    fid = group["frames"][0]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-30 minutes') WHERE id=?",
        (core_db.now(), fid),
    )

    result = recovery.reap_stale_frames(dry_run=True)

    assert result["dry_run"] is True
    assert fid in result["frame_ids"]
    after = core_db.query_one("SELECT status FROM shot_frames WHERE id=?", (fid,))
    assert after["status"] == "generating", "dry_run 不该改库"


def test_reap_stale_frames_leaves_fresh_generating_alone(env):
    """正在跑（updated_at 在阈值内）的帧不能误杀。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "新生成中", "actions": ["a"]})
    fid = group["frames"][0]["id"]
    # 默认 updated_at 就是 now()，远在 15 分钟阈值内
    core_db.execute(
        "UPDATE shot_frames SET status='generating', updated_at=? WHERE id=?",
        (core_db.now(), fid),
    )

    result = recovery.reap_stale_frames()

    assert fid not in result["frame_ids"]
    after = core_db.query_one("SELECT status FROM shot_frames WHERE id=?", (fid,))
    assert after["status"] == "generating"


def test_reap_stale_frames_resets_group_status(env):
    """把唯一一张 generating 帧复位后，组图聚合状态应该从 generating 跌到 failed
    （剩下都是 done / failed / pending）。这里我们的用例就一张帧，
    复位后整组应该是 failed（因为没 done 帧）。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "group 状态", "actions": ["x"]})
    fid = group["frames"][0]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-30 minutes') WHERE id=?",
        (core_db.now(), fid),
    )
    # 模拟 worker 在跑：把组状态推到 generating（reap 后应该跌下来）
    core_db.execute("UPDATE shot_groups SET status='generating' WHERE id=?", (group["id"],))

    recovery.reap_stale_frames()

    after = core_db.query_one("SELECT status, frame_count, done_count FROM shot_groups WHERE id=?", (group["id"],))
    # 该帧已 failed（不是 done），所以整组不是 ready 也不是 idle，
    # 也不是 partial（partial 需要至少 1 张 done）。fall 到 failed。
    assert after["status"] == "failed"
    assert after["frame_count"] == 1
    assert after["done_count"] == 0


# ---------------------------------------------------------- 组图帧维护路由


def test_maintenance_frames_stale_route_is_read_only(env, client):
    """``GET /maintenance/frames-stale`` 必须只读——清不掉生成中的帧。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "stale 路由体检", "actions": ["a"]})
    fid = group["frames"][0]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-30 minutes') WHERE id=?",
        (core_db.now(), fid),
    )

    resp = client.get("/api/comic/maintenance/frames-stale")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["data"]["dry_run"] is True
    assert fid in body["data"]["frame_ids"]

    # 只读：不许改库
    after = core_db.query_one("SELECT status FROM shot_frames WHERE id=?", (fid,))
    assert after["status"] == "generating"


def test_maintenance_frames_reap_route_resets_stuck(env, client):
    """``POST /maintenance/frames-reap`` 必须真复位——这是用户手动清理的入口。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "stale 路由真复位", "actions": ["a"]})
    fid = group["frames"][0]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-30 minutes') WHERE id=?",
        (core_db.now(), fid),
    )

    resp = client.post(
        "/api/comic/maintenance/frames-reap",
        json={},
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["data"]["dry_run"] is False
    assert fid in body["data"]["frame_ids"]

    # 真复位
    after = core_db.query_one("SELECT status, error_message FROM shot_frames WHERE id=?", (fid,))
    assert after["status"] == "failed"
    assert "进程退出" in after["error_message"]


def test_maintenance_frames_reap_respects_max_age(env, client):
    """路由必须把 ``max_age_seconds`` 透传给 reap 函数（生产环境紧急清理要能调小阈值）。"""
    from comic import groups as groups_mod

    group = groups_mod.create_group({"name": "阈值路由", "actions": ["a"]})
    fid = group["frames"][0]["id"]
    # 设成 3 分钟前：默认 15 分钟阈值下不该被清，但传 60 秒应该清
    core_db.execute(
        "UPDATE shot_frames SET status='generating', "
        "updated_at=DATETIME(?, '-3 minutes') WHERE id=?",
        (core_db.now(), fid),
    )

    # 默认 15 分钟：3 分钟前的帧不该是 stale
    body1 = client.post("/api/comic/maintenance/frames-reap", json={"dry_run": True}).get_json()
    assert fid not in body1["data"]["frame_ids"]

    # 改成 60 秒：3 分钟前的帧就是 stale 了
    body2 = client.post(
        "/api/comic/maintenance/frames-reap",
        json={"max_age_seconds": 60, "dry_run": False},
    ).get_json()
    assert fid in body2["data"]["frame_ids"]
    after = core_db.query_one("SELECT status FROM shot_frames WHERE id=?", (fid,))
    assert after["status"] == "failed"
