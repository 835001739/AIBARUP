"""P1-6：历史列表的过期判定改为批量计算后的**等价性**与**复杂度**回归测试。

修复前 ``list_history`` 每行跑 1~2 次查询（实测 100 条 = 201 次 SQL）。
改成批量后是常数次——但"少查库"不是免费的：判定规则从 SQL 搬到了 Python，
只要搬错一点，用户看到的历史状态就是错的，而且**不报错**。

所以这里做两件事：
1. 把修复前的逐行 SQL 实现原样保留成 ``_legacy_stale_reason``，当作基准，
   对同一批数据逐一比对新实现（等价性）；
2. 用查询计数器证明复杂度真的从 O(N) 降到了 O(1)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from config import Config
from core import db as core_db
from core.db import execute, query_all, query_one, query_scalar
from reverse import service, uploads

# ---------------------------------------------------------------- 基准：修复前的逐行实现


def _legacy_stale_reason(row) -> str:
    """``service._stale_reason`` / ``_superseded`` 的修复前版本，逐行查库。

    只在测试里保留，用来证明新实现没有改变语义。
    """
    ref = row["upload_ref"]
    upload_id = Path(str(ref or "")).stem if ref else ""
    if upload_id:
        if uploads.resolve_path(upload_id) is None:
            return "upload_expired"
        return _legacy_superseded(row, "upload_ref", ref)

    if row["image_id"]:
        if query_scalar("SELECT 1 FROM images WHERE id=?", (row["image_id"],)) is None:
            return "image_missing"
        return _legacy_superseded(row, "image_id", row["image_id"])
    return ""


def _legacy_superseded(row, column: str, value) -> str:
    """``service._superseded`` 的修复前版本。

    注意那个 ``status != completed`` 的提前返回：只有**已完成**的结果才谈得上
    "被更新的结果取代"，失败/取消的旧任务不参与比较。
    """
    if row["status"] != service.STATUS_COMPLETED:
        return ""
    newer = query_one(
        f"SELECT id FROM prompt_reverse_jobs WHERE id > ? AND status = ? AND {column} = ? "
        "AND IFNULL(content_hash, '') != IFNULL(?, '') LIMIT 1",
        (row["id"], service.STATUS_COMPLETED, value, row["content_hash"]),
    )
    return "source_replaced" if newer is not None else ""


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    Path(Config.GALLERY_DIR).mkdir(parents=True, exist_ok=True)
    Path(Config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

    yield {"gallery": Path(Config.GALLERY_DIR), "uploads": Path(Config.UPLOAD_DIR)}

    core_db._local.conn = None


@pytest.fixture
def counter(monkeypatch):
    """统计 service 模块内实际发出的查询次数。"""
    state = {"n": 0}
    for name in ("query_one", "query_scalar", "query_all"):
        monkeypatch.setattr(service, name, _counting(getattr(service, name), state), raising=True)
    return state


def _counting(original, state):
    def wrapper(*args, **kwargs):
        state["n"] += 1
        return original(*args, **kwargs)

    return wrapper


def _add_image(env, image_id: str) -> None:
    path = env["gallery"] / f"{image_id}.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    execute(
        "INSERT OR REPLACE INTO images (id, filename, gallery_path, width, height, "
        "size_bytes, created_at) VALUES (?,?,?,?,?,?,?)",
        (image_id, f"{image_id}.png", f"gallery/{image_id}.png", 8, 8, 100, "2026-01-01"),
    )


def _add_job(image_id: str = "", upload_ref: str = "", content_hash: str = "",
             status: str = "completed") -> int:
    execute(
        "INSERT INTO prompt_reverse_jobs (image_id, upload_ref, status, stage, source_mode, "
        "content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
        (image_id or None, upload_ref or None, status, "done", "auto", content_hash or None,
         "2026-01-01 00:00:00"),
    )
    return int(query_all("SELECT id FROM prompt_reverse_jobs ORDER BY id DESC LIMIT 1")[0]["id"])


def _all_rows() -> list:
    return query_all("SELECT * FROM prompt_reverse_jobs ORDER BY id DESC")


# ---------------------------------------------------------------- 等价性：新旧实现对同一批数据必须一致


def test_batch_matches_legacy_on_mixed_scenarios(env):
    """一个库里同时塞进 5 种情形，逐行比对新旧判定结果。"""
    # ① 同一张图跑了两次、内容不同 ⇒ 老的那条被取代
    _add_image(env, "img-a")
    _add_job(image_id="img-a", content_hash="hash-1")
    _add_job(image_id="img-a", content_hash="hash-2")
    # ② 同一张图跑了两次、内容相同 ⇒ 谁都不算过期
    _add_image(env, "img-b")
    _add_job(image_id="img-b", content_hash="same")
    _add_job(image_id="img-b", content_hash="same")
    # ③ 图已被删 ⇒ image_missing
    _add_job(image_id="img-gone", content_hash="x")
    # ④ 上传缓存已过期 ⇒ upload_expired
    _add_job(upload_ref="uploads/deadbeef.png", content_hash="y")
    # ⑤ 失败的任务永不算 source_replaced
    _add_image(env, "img-c")
    _add_job(image_id="img-c", content_hash="c1", status="failed")
    _add_job(image_id="img-c", content_hash="c2")

    rows = _all_rows()
    assert len(rows) == 8

    batch = service._stale_reasons(rows)
    for row in rows:
        expected = _legacy_stale_reason(row)
        assert batch.get(int(row["id"]), "") == expected, f"job {row['id']} 判定不一致"


def test_batch_reports_expected_reasons(env):
    """不比对了，直接把 4 种情形的期望值写死，防止新旧一起错。"""
    _add_image(env, "img-a")
    older = _add_job(image_id="img-a", content_hash="hash-1")
    newer = _add_job(image_id="img-a", content_hash="hash-2")

    _add_image(env, "img-b")
    _add_job(image_id="img-b", content_hash="same")
    _add_job(image_id="img-b", content_hash="same")

    missing = _add_job(image_id="img-gone", content_hash="x")
    expired = _add_job(upload_ref="uploads/deadbeef.png", content_hash="y")

    reasons = service._stale_reasons(_all_rows())

    assert reasons.get(older) == "source_replaced"
    assert reasons.get(newer) is None, "最新的结果不该被判过期"
    assert reasons.get(missing) == "image_missing"
    assert reasons.get(expired) == "upload_expired"


def test_same_hash_is_not_superseded(env):
    """反证：只比 id 不比哈希会把"重跑出同样结果"误判成过期。"""
    _add_image(env, "img-b")
    first = _add_job(image_id="img-b", content_hash="same")
    second = _add_job(image_id="img-b", content_hash="same")

    reasons = service._stale_reasons(_all_rows())

    assert reasons.get(first) is None
    assert reasons.get(second) is None


def test_failed_job_is_never_source_replaced(env):
    """只有**已完成**的结果才谈得上"被更新的结果取代"。"""
    _add_image(env, "img-c")
    failed = _add_job(image_id="img-c", content_hash="c1", status="failed")
    _add_job(image_id="img-c", content_hash="c2")

    assert service._stale_reasons(_all_rows()).get(failed) is None


def test_scan_superseded_is_pure_and_order_insensitive():
    """判定是纯函数：输入顺序不影响结果（排序在里面做）。"""
    records = [(1, "a"), (2, "b"), (3, "a")]
    assert service._scan_superseded(records) == {1, 2}
    assert service._scan_superseded(list(reversed(records))) == {1, 2}


def test_scan_superseded_edge_cases():
    """空集、单条、全同哈希 —— 都不该被判过期。"""
    assert service._scan_superseded([]) == set()
    assert service._scan_superseded([(1, "a")]) == set()
    assert service._scan_superseded([(1, "a"), (2, "a"), (3, "a")]) == set()


# ---------------------------------------------------------------- 复杂度：查询次数必须与行数无关


def test_list_history_query_count_is_constant(env, counter):
    """反证核心：修复前 100 条 = 201 次 SQL，现在必须是常数次。"""
    for i in range(30):
        image_id = f"img{i:03d}"
        _add_image(env, image_id)
        _add_job(image_id=image_id, content_hash=f"h{i}-1")
        _add_job(image_id=image_id, content_hash=f"h{i}-2")

    counter["n"] = 0
    service.list_history(60)
    at_60 = counter["n"]

    counter["n"] = 0
    service.list_history(30)
    at_30 = counter["n"]

    # 行数翻倍，查询次数不许变
    assert at_60 == at_30, f"查询次数随行数增长：{at_30} → {at_60}"
    assert at_60 <= 4, f"60 条历史用了 {at_60} 次查询，仍是 N+1"


def test_list_history_results_unchanged_by_batching(env):
    """批量化之后，列表里每条的 stale 标记仍与逐行判定一致。"""
    for i in range(6):
        image_id = f"img{i:03d}"
        _add_image(env, image_id)
        _add_job(image_id=image_id, content_hash=f"h{i}-1")
        _add_job(image_id=image_id, content_hash=f"h{i}-2")
    _add_job(image_id="img-gone", content_hash="z")

    history = service.list_history(50)
    rows = {int(row["id"]): row for row in _all_rows()}

    assert history["total"] == 13
    for item in history["items"]:
        row = rows[item["job_id"]]
        assert item["stale_reason"] == _legacy_stale_reason(row)
        assert item["stale"] == bool(item["stale_reason"])


def test_single_job_path_still_scans_whole_table(env):
    """单条任务没有"页"可依赖，必须回查同来源任务——不能因为批量化而漏判。

    这条锁的是：先取一条老历史（它的更新结果在库里），再单独查它，
    仍然要判出 source_replaced。
    """
    _add_image(env, "img-a")
    older = _add_job(image_id="img-a", content_hash="hash-1")
    _add_job(image_id="img-a", content_hash="hash-2")

    payload = service.get_job(older)

    assert payload["stale_reason"] == "source_replaced"
    assert payload["stale"] is True


def test_empty_rows_returns_empty_reasons():
    assert service._stale_reasons([]) == {}
