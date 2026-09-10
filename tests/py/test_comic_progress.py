"""M12 分镜工作流「ComfyUI 衔接 + 工作进度展示」测试。

覆盖：
- ``comic/diagnostics.py``：错误码中文文案、ComfyUI 探测降级、工作流体检报告、预检汇总；
- ``service.project_progress``：各状态计数、完成百分比、章节级进度、当前出图页、失败原因分布；
- ``comic/runner.py`` 重试：可重试错误退避重排、不可重试错误直接失败、超过上限落定；
- ``/api/comic/projects/<id>/progress`` 与 ``/precheck`` 路由。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import diagnostics
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
    p = comic_service.create_project({"name": "进度", "default_workflow": workflow})
    ch = comic_service.create_chapter(p["id"], {"title": "第1集"})
    created = []
    for i in range(pages):
        created.append(
            comic_service.create_page(
                ch["id"], {"title": "页%d" % (i + 1), "prompt_text": "分镜%d" % (i + 1)}
            )
        )
    return p, ch, created


def _set_page_status(page_id: int, status: str, **extra) -> None:
    """直接改状态：``update_page`` 只负责提示词类字段，状态由队列/Runner 维护。"""
    cols = ["status=?"]
    args = [status]
    for key, value in extra.items():
        cols.append("%s=?" % key)
        args.append(value)
    args.append(page_id)
    core_db.execute("UPDATE comic_pages SET %s WHERE id=?" % ",".join(cols), tuple(args))


def _new_job(page_id: int) -> dict:
    """走真实入队链路建任务，返回任务行。"""
    res = comic_service.enqueue_page(page_id)
    row = core_db.row_to_dict(
        core_db.query_one("SELECT * FROM comic_jobs WHERE id=?", (res["job_id"],))
    )
    assert row is not None
    return row


def _new_running_job(page_id: int) -> dict:
    """建任务并完整模拟 worker 抢占后的状态，返回任务行。

    ``run_job`` 抢占成功后是「job → running」+「page → generating」两步，
    ``_schedule_retry`` / ``_mark_failed`` 对两步都带状态守卫
    （防止把用户刚取消的任务复活），所以测试必须走到这一步才是真实生命周期。
    """
    job = _new_job(page_id)
    assert runner._claim_job(job["id"]) is True
    core_db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (page_id,))
    return _job_row(job["id"])


# ---------------------------------------------------------------- diagnostics


def test_human_error_has_chinese_text():
    for code in (
        "comfyui_offline", "missing_node", "out_of_memory", "no_output",
        "timeout", "save_failed", "submit_failed", "wait_failed", "download_failed",
    ):
        text = diagnostics.human_error(code, "")
        assert text and any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_human_error_falls_back_to_message():
    text = diagnostics.human_error("完全没见过的错误码", "原始英文报错")
    assert "原始英文报错" in text


def test_probe_comfyui_never_raises(monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **k):  # noqa: ANN001
        raise RuntimeError("网络不可用")

    # 必须打桩在 probe_comfyui 真正调用的那层（``_cc.is_reachable``）。
    # 此前打的是 comfyui_request / _http_json，压根不在调用链上，于是本机
    # ComfyUI 一旦在跑，探测就返回 online=True，这条断言必然挂 —— 与代码无关的环境抖动。
    monkeypatch.setattr(diagnostics._cc, "is_reachable", boom, raising=False)
    snap = diagnostics.probe_comfyui(timeout=0.01)
    assert snap["online"] is False
    assert snap["latency_ms"] >= 0


def test_workflow_report_missing_file(env, tmp_path: Path):
    report = diagnostics.workflow_report("不存在的_workflow.json")
    assert report["exists"] is False
    assert report["node_count"] == 0
    assert report["missing_nodes"] == []


def test_workflow_report_reads_graph(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from sync import paths as sync_paths

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "ok.json").write_text(
        '{"1": {"class_type": "CheckpointLoaderSimple"}, "2": {"class_type": "KSampler"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: wf_dir)
    report = diagnostics.workflow_report("ok.json")
    assert report["exists"] is True
    assert report["node_count"] == 2
    assert "CheckpointLoaderSimple" in report["class_types"]


def test_workflow_report_rejects_path_traversal(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from sync import paths as sync_paths

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: wf_dir)
    report = diagnostics.workflow_report("../../etc/passwd")
    assert report["exists"] is False


def test_precheck_shape(env):
    p, _ch, _pages = _mk(env)
    res = diagnostics.precheck(p["id"], check_nodes=False)
    assert res["project_id"] == p["id"]
    assert set(["ready", "comfyui", "workflow", "queue", "hints"]).issubset(res)
    assert isinstance(res["hints"], list)
    assert res["checked_at"]


# ---------------------------------------------------------------- 进度聚合


def test_progress_counts(env):
    p, ch, pages = _mk(env, pages=3)
    _set_page_status(pages[0]["id"], "done", image_path="a.png")
    _set_page_status(pages[1]["id"], "generating")
    _set_page_status(pages[2]["id"], "failed", error_message="ComfyUI 未启动")

    prog = comic_service.project_progress(p["id"])
    assert prog["total"] == 3
    assert prog["counts"]["done"] == 1
    assert prog["counts"]["generating"] == 1
    assert prog["counts"]["failed"] == 1
    assert prog["percent"] == pytest.approx(33.3, abs=0.1)
    assert prog["chapters"][0]["chapter_id"] == ch["id"]
    assert prog["chapters"][0]["total"] == 3


def test_progress_empty_project(env):
    p = comic_service.create_project({"name": "空项目"})
    prog = comic_service.project_progress(p["id"])
    assert prog["total"] == 0
    assert prog["percent"] == 0
    assert prog["active"] is None


def test_progress_reports_failure_reasons(env):
    p, ch, pages = _mk(env, pages=2)
    for pg in pages:
        _set_page_status(pg["id"], "failed", error_message="ComfyUI 超时")
        core_db.execute(
            "INSERT INTO comic_jobs (project_id, chapter_id, page_id, status, stage, error_code, error_message, attempt, created_at) "
            "VALUES (?,?,?, 'failed','done','timeout','ComfyUI 超时', 3, ?)",
            (p["id"], ch["id"], pg["id"], time.strftime("%Y-%m-%d %H:%M:%S")),
        )
    prog = comic_service.project_progress(p["id"])
    assert prog["failure_reasons"] == [{"code": "timeout", "count": 2}]
    assert prog["last_error"]["error_code"] == "timeout"


def test_progress_includes_character_count(env):
    p, _ch, _pages = _mk(env, pages=1)
    comic_service.create_character(p["id"], {"name": "阿岚"})
    assert comic_service.project_progress(p["id"])["characters"] == 1


# ---------------------------------------------------------------- 重试与可读错误


def test_retryable_codes_defined():
    assert "comfyui_offline" in runner.RETRYABLE_CODES
    assert "missing_node" not in runner.RETRYABLE_CODES
    assert runner.MAX_ATTEMPTS >= 2


def test_schedule_retry_defers_job(env):
    p, _ch, pages = _mk(env, pages=1)
    job = _new_running_job(pages[0]["id"])
    runner._schedule_retry(job["id"], pages[0], "comfyui_offline", "ComfyUI 未启动", 0)

    row = _job_row(job["id"])
    assert row["status"] == "queued"
    assert row["attempt"] == 1
    assert row["stage"] == "retry_wait"
    assert row["next_retry_at"]
    assert "重试" in (pages_msg(pages[0]["id"]) or "")


def _job_row(job_id: int) -> dict:
    row = core_db.row_to_dict(
        core_db.query_one("SELECT * FROM comic_jobs WHERE id=?", (job_id,))
    )
    assert row is not None
    return row


def pages_msg(page_id: int):
    row = core_db.row_to_dict(
        core_db.query_one("SELECT error_message FROM comic_pages WHERE id=?", (page_id,))
    )
    return row["error_message"] if row else None


def test_retry_stops_after_max_attempts(env):
    p, _ch, pages = _mk(env, pages=1)
    job = _new_running_job(pages[0]["id"])
    runner._mark_failed(job["id"], pages[0], "timeout", "ComfyUI 超时，请检查显存")
    row = _job_row(job["id"])
    assert row["status"] == "failed"
    assert "超时" in (row["error_message"] or "")
    assert "超时" in (pages_msg(pages[0]["id"]) or "")


def test_mark_failed_respects_cancelled_job(env):
    """任务在出图期间被取消时，收尾更新不得把它复活成 failed。"""
    p, _ch, pages = _mk(env, pages=1)
    job = _new_running_job(pages[0]["id"])
    comic_service.cancel_job(job["id"])
    runner._mark_failed(job["id"], pages[0], "timeout", "ComfyUI 超时，请检查显存")

    row = _job_row(job["id"])
    assert row["status"] == "cancelled"
    # 页面也不该被重新打成 failed（否则用户刚取消的页又变成「失败」）
    assert pages_msg(pages[0]["id"]) == ""


def test_worker_skips_deferred_jobs(env):
    """next_retry_at 在未来的任务不应被 worker 捞起。"""
    p, _ch, pages = _mk(env, pages=1)
    job = _new_job(pages[0]["id"])
    core_db.execute(
        "UPDATE comic_jobs SET status='queued', next_retry_at=? WHERE id=?",
        ("2999-01-01 00:00:00", job["id"]),
    )
    picked = core_db.query_one(
        "SELECT id FROM comic_jobs WHERE status='queued' "
        "AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY id ASC LIMIT 1",
        (time.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    assert picked is None


# ---------------------------------------------------------------- 路由


def test_progress_route(client, env):
    p, _ch, pages = _mk(env, pages=2)
    _set_page_status(pages[0]["id"], "done", image_path="a.png")
    res = client.get("/api/comic/projects/%d/progress" % p["id"]).get_json()
    assert res["ok"] is True
    data = res["data"]
    assert data["total"] == 2 and data["done"] == 1
    assert "comfyui" in data  # 探测失败也不影响进度数据
    assert isinstance(data["comfyui"]["online"], bool)


def test_precheck_route(client, env):
    p, _ch, _pages = _mk(env, pages=1)
    res = client.get(
        "/api/comic/projects/%d/precheck?check_nodes=0" % p["id"]
    ).get_json()
    assert res["ok"] is True
    assert res["data"]["project_id"] == p["id"]


def test_progress_route_404(client, env):
    res = client.get("/api/comic/projects/999999/progress").get_json()
    assert res["ok"] is False
    assert res["error"]["code"] == "not_found"
