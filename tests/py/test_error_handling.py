"""P1-5 / P1-9：错误码与异常处理收口的回归测试。

这两件事的共同点是：**错误被静默降级**。

- P1-5 是"状态码被降级"：``AIBARError("not_found", ...)`` 漏写 status 后落到 400，
  语义还在（前端按 code 分流），但监控、缓存、调用方全被误导；
- P1-9 是"异常被降级"：各处 ``except Exception`` 把程序缺陷包装成用户错误
  （写入失败报 415"图片损坏"），或者更糟——吞掉异常后返回一个看似正常的空值
  （``referenced_upload_ids`` 返回空集 ⇒ 清理流程删掉仍被引用的图片）。

本文件的每个测试都是一条**反证**：先证明"改之前的行为"确实有害，再锁定新行为。
"""

from __future__ import annotations

import io
import logging
import types
from pathlib import Path

import pytest
from PIL import Image

from config import Config
from core import db as core_db
from core.db import execute, query_all
from core.errors import AIBARError, CODE_STATUS, DEFAULT_STATUS
from reverse import service, uploads
from sync import scanner, watcher

# ---------------------------------------------------------------- 辅助


def _png_bytes(color: tuple[int, int, int] = (120, 80, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _ExplodingStream:
    """``read()`` 抛异常的上传流，用来模拟写盘阶段的非预期故障。"""

    def __init__(self, error: BaseException):
        self._error = error

    def read(self, _size: int = -1):
        raise self._error


def _raiser(error: BaseException):
    """返回一个调用即抛 ``error`` 的函数，用于 monkeypatch。"""
    def _fn(*_args, **_kwargs):
        raise error
    return _fn


def _insert_job(upload_id: str) -> int:
    """插入一条已完成的反推历史，返回自增 id。"""
    execute(
        "INSERT INTO prompt_reverse_jobs (status, upload_ref, created_at, completed_at) "
        "VALUES ('completed', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        (uploads.upload_ref_of(upload_id),),
    )
    rows = query_all("SELECT id FROM prompt_reverse_jobs ORDER BY id DESC LIMIT 1")
    return int(rows[0]["id"])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与上传缓存重定向到临时目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "STATIC_DIR", tmp_path / "static")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")

    core_db._local.conn = None
    core_db.migrate()

    upload_dir = Path(Config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    Path(Config.GALLERY_DIR).mkdir(parents=True, exist_ok=True)

    yield {"data": data_dir, "uploads": upload_dir}

    core_db._local.conn = None


# ---------------------------------------------------------------- P1-5 错误码决定状态码


def test_registered_codes_never_fall_back_to_400():
    """登记过的错误码不许落到 400 —— 400 意味着"你传错了"，而它们不是。"""
    for code, status in CODE_STATUS.items():
        assert AIBARError(code, "x").status == status
        assert status != DEFAULT_STATUS


def test_unregistered_code_still_defaults_to_400():
    """未登记的错误码按"请求有问题"处理，这是兜底不是漏洞。"""
    assert AIBARError("some_new_code", "x").status == 400


def test_explicit_status_wins():
    """显式给 status 时不做推导，保留调用方覆盖的能力。"""
    assert AIBARError("not_found", "x", 410).status == 410


def test_not_found_is_404_without_writing_status():
    """反证：这是 ``sync/comfyui_link.py`` 漏写 status 的那处，此前返回 400。"""
    from core import errors

    assert errors.not_found("图片不存在").status == 404


def test_unavailable_variants_all_503():
    """反证：``reverse/service.py`` 两处 provider_unavailable 曾一处 400 一处 503。"""
    from core import errors

    assert errors.unavailable("x").status == 503
    assert errors.unavailable("x", "provider_unavailable").status == 503
    assert errors.unavailable("x", "provider_busy").status == 503


# ---------------------------------------------------------------- P1-9 上传：程序缺陷不伪装成用户错误


def test_upload_write_failure_is_500_not_415(env):
    """写盘阶段失败只能是环境问题，报 415"图片损坏"会骗人。"""
    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(_ExplodingStream(RuntimeError("disk on fire")), "x.png")

    assert excinfo.value.code == "internal_error"
    assert excinfo.value.status == 500


def test_upload_write_failure_leaves_no_temp_file(env):
    """失败必须清理临时文件，否则上传目录会被 .tmp_ 垃圾慢慢填满。"""
    with pytest.raises(AIBARError):
        uploads.save_upload(_ExplodingStream(RuntimeError("boom")), "x.png")

    assert [p.name for p in env["uploads"].iterdir()] == []


def test_upload_unexpected_parse_error_is_500(env, monkeypatch):
    """解析阶段抛出**非图片类**异常 ⇒ 是我们的 bug，必须 500 并留日志。"""
    monkeypatch.setattr(uploads, "image_basic_info", _raiser(RuntimeError("bug")))

    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(_png_bytes()), "x.png")

    assert excinfo.value.code == "internal_error"
    assert excinfo.value.status == 500


def test_upload_expected_parse_error_is_415(env, monkeypatch):
    """解析阶段抛出**图片类**异常 ⇒ 用户传了坏图，415 且不记错误日志。"""
    monkeypatch.setattr(uploads, "image_basic_info", _raiser(ValueError("truncated")))

    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(_png_bytes()), "x.png")

    assert excinfo.value.code == "invalid_image"
    assert excinfo.value.status == 415
    # 坏图不该留下残留文件
    assert [p.name for p in env["uploads"].iterdir()] == []


def test_upload_expected_errors_are_narrow():
    """白名单要窄：只认图片解析会抛的类型，宽了就会把 bug 吞成 415。"""
    assert set(uploads._EXPECTED_IMAGE_ERRORS) == {OSError, ValueError, EOFError}


def test_upload_failure_after_dedup_hit_keeps_shared_file(env, monkeypatch):
    """同一份内容第二次上传会命中去重（复用已有文件）。

    此时后续步骤失败，**不许删那个文件** —— 它不是本次写的，
    删了会让第一次上传的历史记录指向一个不存在的预览。
    """
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    assert uploads.resolve_path(payload["upload_id"]) is not None

    monkeypatch.setattr(uploads, "image_basic_info", _raiser(ValueError("truncated")))
    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")

    assert excinfo.value.status == 415
    assert uploads.resolve_path(payload["upload_id"]) is not None, "别人的文件一个字节都不许动"
    assert [p.name for p in env["uploads"].iterdir()] == [f"{payload['upload_id']}.png"]


def test_upload_dedup_hit_does_not_duplicate_file(env):
    """去重路径本身：同一份内容只存一份，且返回的 upload_id 相同。"""
    first = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    second = uploads.save_upload(io.BytesIO(_png_bytes()), "b.png")

    assert first["upload_id"] == second["upload_id"]
    assert [p.name for p in env["uploads"].iterdir()] == [f"{first['upload_id']}.png"]


# ---------------------------------------------------------------- P1-9 引用集合：查不到必须抛，不能返回空集


def test_referenced_upload_ids_returns_ids(env):
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    uploads.save_upload(io.BytesIO(_png_bytes(color=(9, 9, 9))), "b.png")
    _insert_job(payload["upload_id"])

    assert uploads.referenced_upload_ids() == {payload["upload_id"]}


def test_referenced_upload_ids_propagates_on_db_error(env, monkeypatch):
    """反证：此前这里 try/except 后返回空集 —— 空集等于"所有人都可以删"。"""
    monkeypatch.setattr(uploads, "query_all", _raiser(RuntimeError("db gone")))

    with pytest.raises(RuntimeError):
        uploads.referenced_upload_ids()


def test_cleanup_unreferenced_propagates_when_reference_lookup_fails(env, monkeypatch):
    """扫描清理依赖引用集合；查不到时宁可整轮失败，也不能冒险误删。"""
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    monkeypatch.setattr(uploads, "referenced_upload_ids", _raiser(RuntimeError("db gone")))

    with pytest.raises(RuntimeError):
        uploads.cleanup_unreferenced()

    assert uploads.resolve_path(payload["upload_id"]) is not None, "查询失败时一个文件都不许删"


def test_cleanup_unreferenced_still_keeps_referenced_files(env):
    """正常路径：被引用的留、没人要的删。"""
    referenced = uploads.save_upload(io.BytesIO(_png_bytes()), "keep.png")
    orphan = uploads.save_upload(io.BytesIO(_png_bytes(color=(9, 9, 9))), "drop.png")
    _insert_job(referenced["upload_id"])

    stats = uploads.cleanup_unreferenced()

    assert uploads.resolve_path(referenced["upload_id"]) is not None
    assert uploads.resolve_path(orphan["upload_id"]) is None
    assert stats["removed"] == 1
    assert stats["kept_referenced"] == 1


# ---------------------------------------------------------------- P1-9 删除历史：缓存清理失败不能拖垮主流程


def test_delete_history_survives_cleanup_failure(env, monkeypatch):
    """记录已经删掉了，缓存清理失败应该记告警并继续，而不是整笔回滚/报错。"""
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    job_id = _insert_job(payload["upload_id"])
    monkeypatch.setattr(uploads, "cleanup_unreferenced", _raiser(RuntimeError("cleanup down")))

    result = service.delete_history(job_id)

    assert result["deleted"] is True
    assert result["upload_cleanup"] == {"removed": 0, "kept_referenced": 0, "failed": 1}
    # 主流程不受影响：记录确实删掉了
    assert query_all("SELECT id FROM prompt_reverse_jobs") == []


def test_delete_history_reports_real_cleanup_when_it_works(env):
    """清理正常时，返回值里必须带上真实统计。"""
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    job_id = _insert_job(payload["upload_id"])

    result = service.delete_history(job_id)

    assert result["upload_cleanup"]["removed"] == 1
    assert uploads.resolve_path(payload["upload_id"]) is None


# ---------------------------------------------------------------- P1-9 后台任务：吞掉的异常要留痕


def test_scan_count_returns_zero_but_logs(env, caplog):
    """统计失败不该中断整轮同步（返回 0），但必须记一笔，否则表坏了没人知道。"""
    with caplog.at_level(logging.WARNING):
        assert scanner._count("no_such_table_xyz") == 0

    assert any("scan_count_failed" in r.message for r in caplog.records)


def test_scan_count_returns_real_number(env):
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "a.png")
    _insert_job(payload["upload_id"])
    assert scanner._count("prompt_reverse_jobs") == 1


def test_watcher_start_logs_failed_first_sync(env, caplog, monkeypatch):
    """反证：此前这里只有一个 pass，首次同步坏了界面上毫无迹象。"""
    instance = watcher.SyncWatcher()
    monkeypatch.setattr(instance, "sync_once", _raiser(RuntimeError("sync down")))
    monkeypatch.setattr(watcher, "threading", _FakeThreading(), raising=True)

    with caplog.at_level(logging.ERROR):
        assert instance.start(run_now=True) is True

    assert any("sync_start_once_failed" in r.message for r in caplog.records)
    # 首次同步失败不能阻止线程启动
    assert instance.is_running() is True


class _FakeThread:
    """替身线程：只记录是否 start()，不真的跑循环。"""

    def __init__(self, *_args, **kwargs):
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive


class _FakeThreading(types.SimpleNamespace):
    """只替换 ``watcher.threading.Thread``，其余（Event/Lock）沿用真实实现。"""

    def __init__(self):
        import threading as _real

        super().__init__(Event=_real.Event, Lock=_real.Lock, Thread=_FakeThread)
