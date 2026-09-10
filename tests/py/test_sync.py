"""M1-M4 与 M8 集成测试：扫描落库、去重、种子、失败隔离、探活降级、路由冒烟。

隔离策略（HARNESS §7）：所有数据落在 pytest 的 ``tmp_path`` 下，
通过 monkeypatch 改写 ``Config`` 的目录配置，并在切换后重置 ``core.db`` 的线程本地连接，
确保不污染仓库里的 ``data/``。
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from config import Config
from core import db as core_db
from core.db import query_all, query_scalar
from sync import comfyui_ctl, models, scanner, watcher
from sync.routes import bp


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库、状态文件、图库与 ComfyUI 源目录全部重定向到临时目录。"""
    data_dir = tmp_path / "data"
    workflows = tmp_path / "wf"
    output = tmp_path / "out"
    model_root = tmp_path / "models"
    for path in (data_dir, workflows, output, model_root):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "COMFYUI_DIR", None)
    monkeypatch.setattr(Config, "COMFYUI_WORKFLOWS_DIR", str(workflows))
    monkeypatch.setattr(Config, "COMFYUI_OUTPUT_DIR", str(output))
    monkeypatch.setattr(Config, "COMFYUI_MODELS_DIR", str(model_root))

    # core.db 用线程本地连接，切换 DB_PATH 后必须丢弃旧连接
    core_db._local.conn = None
    core_db.migrate()

    yield {"data": data_dir, "workflows": workflows, "output": output, "models": model_root}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    core_db._local.conn = None


# ---------------------------------------------------------------- 造数据


def _make_png(path: Path, color: tuple[int, int, int] = (10, 20, 30), prompt: str | None = None) -> Path:
    """生成一张小 PNG；传入 prompt 时把 ComfyUI 风格的 prompt 元数据写进 tEXt 块。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngInfo()
    if prompt:
        # 与 ComfyUI 落盘格式一致：prompt 块里是 API 格式的 JSON
        pnginfo.add_text("prompt", json.dumps(_api_workflow(prompt, "blurry")))
    Image.new("RGB", (4, 4), color).save(path, pnginfo=pnginfo)
    return path


def _write_workflow(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _api_workflow(positive: str, negative: str) -> dict:
    return {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": positive}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": negative}},
        "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {}},
    }


# ---------------------------------------------------------------- M2 工作流


def test_workflow_scan_persists_parsed_fields(env):
    """工作流落库：节点数、类型、正负提示词都来自解析结果。"""
    _write_workflow(env["workflows"] / "flow_a.json", _api_workflow("a cat", "blurry"))

    counter = scanner.scan_workflows()

    assert counter["added"] == 1
    assert counter["failed"] == 0
    row = query_all("SELECT * FROM workflows")[0]
    assert row["filename"] == "flow_a.json"
    assert row["node_count"] == 4
    assert json.loads(row["node_types"]) == ["CLIPTextEncode", "KSampler", "SaveImage"]
    assert row["positive_prompt"] == "a cat"
    assert row["negative_prompt"] == "blurry"


def test_workflow_scan_is_incremental(env):
    """未变更的工作流（mtime+size 相同）在第二次扫描时被跳过。"""
    _write_workflow(env["workflows"] / "flow_a.json", _api_workflow("a cat", "blurry"))
    scanner.scan_workflows()

    counter = scanner.scan_workflows()

    assert counter["added"] == 0
    assert counter["skipped"] == 1
    assert query_scalar("SELECT COUNT(*) FROM workflows") == 1


def test_workflow_scan_re_parses_changed_file(env):
    """文件内容变化后重新解析并更新（不产生重复行）。"""
    path = _write_workflow(env["workflows"] / "flow_a.json", _api_workflow("a cat", "blurry"))
    scanner.scan_workflows()
    _write_workflow(path, _api_workflow("a dog", "messy"))

    counter = scanner.scan_workflows()

    assert counter["added"] == 0
    assert counter["updated"] == 1
    assert query_scalar("SELECT COUNT(*) FROM workflows") == 1
    assert query_scalar("SELECT positive_prompt FROM workflows") == "a dog"


# ---------------------------------------------------------------- M3 图库


def test_image_hash_dedupe(env):
    """同一内容的两份文件只入库一条；重复扫描不会重复入库。"""
    first = _make_png(env["output"] / "a.png", (1, 2, 3))
    _make_png(env["output"] / "copy_of_a.png", (1, 2, 3))
    _make_png(env["output"] / "b.png", (9, 9, 9))

    first_run = scanner.scan_images()
    total_after_first = query_scalar("SELECT COUNT(*) FROM images")

    second_run = scanner.scan_images()
    total_after_second = query_scalar("SELECT COUNT(*) FROM images")

    assert first_run["added"] == 2
    assert first_run["skipped"] == 1  # 重复内容被去重
    assert total_after_first == 2
    assert second_run["added"] == 0
    assert total_after_second == 2
    assert scanner.sha256_file(first) == query_scalar("SELECT id FROM images WHERE filename = 'a.png'")


def test_image_copied_to_gallery_with_relative_path(env):
    """图片被复制到 static/gallery，库里保存相对 static 的路径。"""
    _make_png(env["output"] / "ComfyUI_00001_.png", (5, 5, 5))
    scanner.scan_images()

    row = query_all("SELECT * FROM images")[0]
    digest = row["id"]
    assert row["gallery_path"] == f"gallery/{digest[:2]}/{digest}.png"
    # 复制目标落在被 monkeypatch 的临时 GALLERY_DIR 下
    assert (Path(Config.GALLERY_DIR) / digest[:2] / f"{digest}.png").is_file()
    assert row["width"] == 4 and row["height"] == 4
    assert row["workflow_link"] == "ComfyUI"
    assert row["created_at"]


# ---------------------------------------------------------------- M4 同步引擎


def test_first_run_seeds_only_latest_n_images(env, monkeypatch):
    """首跑只同步最近 N 张图 + 全部工作流，之后全量增量。"""
    monkeypatch.setattr(Config, "SEED_LIMIT", 2)
    for index in range(5):
        path = _make_png(env["output"] / f"img_{index}.png", (index, index, index))
        # mtime 递增，保证"最近 2 张"可预期
        stamp = 1_700_000_000 + index * 100
        os.utime(path, (stamp, stamp))
    _write_workflow(env["workflows"] / "wf.json", _api_workflow("seed", "none"))

    watcher_ref = watcher.get_watcher()
    seed_result = watcher_ref.sync_once()
    assert seed_result["images"] == 2
    assert seed_result["workflows"] == 1
    assert scanner.is_seeded() is True

    full_result = watcher_ref.sync_once(force_all=True)
    assert full_result["images"] == 5
    assert query_scalar("SELECT COUNT(*) FROM images") == 5


def test_bad_file_does_not_break_the_loop(env):
    """单文件解析失败被隔离并记录日志，其余文件照常入库。"""
    _write_workflow(env["workflows"] / "broken.json", "{ this is not json")
    _write_workflow(env["workflows"] / "good.json", _api_workflow("ok", ""))

    counter = scanner.scan_workflows()

    assert counter["added"] == 1
    assert counter["failed"] == 1
    assert query_scalar("SELECT COUNT(*) FROM workflows") == 1
    assert query_scalar("SELECT filename FROM workflows") == "good.json"
    assert query_scalar("SELECT COUNT(*) FROM sync_log WHERE status = 'error'") == 1


def test_sync_once_returns_expected_shape(env):
    """sync_once 返回契约要求的全部字段。"""
    _make_png(env["output"] / "a.png", (1, 1, 1))
    _write_workflow(env["workflows"] / "wf.json", _api_workflow("p", "n"))

    result = watcher.get_watcher().sync_once()

    for key in (
        "workflows",
        "images",
        "added_workflows",
        "added_images",
        "skipped",
        "failed",
        "duration_ms",
    ):
        assert key in result, key
    assert result["workflows"] == 1 and result["images"] == 1
    assert result["duration_ms"] >= 0


def test_watcher_auto_toggle_and_running_state(env):
    """自动开关与运行状态可切换，后台线程可启停且重复启动幂等。"""
    watcher_ref = watcher.get_watcher()

    watcher_ref.set_auto(False)
    assert watcher_ref.is_auto() is False
    watcher_ref.set_auto(True)
    assert watcher_ref.is_auto() is True

    # 关闭自动同步后再启动线程：只验证线程生命周期，避免后台线程与用例竞争临时库
    watcher_ref.set_auto(False)
    assert watcher_ref.start() is True
    assert watcher_ref.is_running() is True
    assert watcher_ref.start() is False  # 已在运行，重复启动幂等
    watcher_ref.stop()
    assert watcher_ref.is_running() is False


# ---------------------------------------------------------------- M1 探活降级


def test_comfyui_probe_offline_returns_false(env, monkeypatch):
    """连接失败时 probe 返回 running:false 且不抛异常（连接被拒的确定性用例）。"""
    import requests

    def refuse(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", refuse)

    result = comfyui_ctl.probe(timeout=1.0)

    assert result["running"] is False
    assert result["system_stats"] is None
    assert result["error_code"] == "offline"

    status = comfyui_ctl.status(force=True)
    assert status["running"] is False
    assert status["checked_at"]


def test_comfyui_probe_timeout_is_not_raised(env, monkeypatch):
    """超时同样降级为 running:false，异常不外泄。"""
    import requests

    def hang(*_args, **_kwargs):
        raise requests.exceptions.Timeout("too slow")

    monkeypatch.setattr(requests, "get", hang)

    assert comfyui_ctl.probe(timeout=1.0)["error_code"] == "timeout"


def test_comfyui_probe_real_closed_port(env, monkeypatch):
    """对真实未监听端口探测：任何异常都被吞掉，只返回 running:false。"""
    try:
        port = _free_port()
    except OSError:  # 受限环境可能禁止绑定端口
        pytest.skip("当前环境不允许绑定本地端口")
    monkeypatch.setattr(Config, "COMFYUI_PORT", port)

    result = comfyui_ctl.probe(timeout=1.0)

    assert result["running"] is False
    assert result["error_code"]


def test_comfyui_start_without_dir_returns_error_code(env, monkeypatch):
    """COMFYUI_DIR 未配置时，start 返回明确错误码而不是异常。"""
    monkeypatch.setattr(Config, "COMFYUI_DIR", None)

    result = comfyui_ctl.start()

    assert result["started"] is False
    assert result["error_code"] == "comfyui_dir_missing"
    assert result["message"]


# ---------------------------------------------------------------- M8 模型


def test_models_scan_groups_by_subdir(env, monkeypatch):
    """模型按子目录归类，目录不存在时返回空列表。"""
    (env["models"] / "checkpoints").mkdir()
    (env["models"] / "loras").mkdir()
    (env["models"] / "checkpoints" / "sd_xl.safetensors").write_bytes(b"x" * 10)
    (env["models"] / "loras" / "style.ckpt").write_bytes(b"y" * 20)

    data = models.scan_models(refresh=True)

    assert data["total"] == 2
    kinds = {item["type"] for item in data["items"]}
    assert kinds == {"checkpoints", "loras"}
    assert all(item["id"] and item["path"] and item["ext"] for item in data["items"])

    monkeypatch.setattr(Config, "COMFYUI_MODELS_DIR", str(env["models"] / "missing"))
    assert models.scan_models(refresh=True) == {"items": [], "total": 0}


# ---------------------------------------------------------------- 路由


@pytest.fixture
def client(env):
    """现场构造 Flask 实例并只注册 sync_bp，避免依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    with app.test_client() as test_client:
        yield test_client


def _data(response) -> dict:
    payload = response.get_json()
    assert payload is not None
    return payload


def test_route_smoke_main_endpoints(client, env):
    """主要接口冒烟：统一 envelope 与关键字段。"""
    _write_workflow(env["workflows"] / "flow.json", _api_workflow("a cat", "blurry"))
    # 带内嵌提示词的图片才会进入案例库
    _make_png(env["output"] / "ComfyUI_00002_.png", (7, 7, 7), prompt="a cat on a bench")
    scanner.sync_all()

    assert _data(client.get("/api/health"))["data"]["status"] == "ok"

    workflows_payload = _data(client.get("/api/workflows"))
    assert workflows_payload["ok"] is True
    assert workflows_payload["data"]["total"] == 1
    assert workflows_payload["data"]["items"][0]["positive_preview"] == "a cat"

    detail = _data(client.get("/api/workflows/flow.json/detail"))
    assert detail["data"]["positive_prompt"] == "a cat"
    assert len(detail["data"]["nodes"]) == 4

    gallery_payload = _data(client.get("/api/gallery"))
    assert gallery_payload["data"]["total"] == 1
    image_id = gallery_payload["data"]["items"][0]["id"]

    single = _data(client.get(f"/api/gallery/{image_id}"))
    assert single["data"]["id"] == image_id
    assert single["data"]["url"].startswith("/static/gallery/")

    counts = _data(client.get("/api/nav/counts"))["data"]
    assert counts["workflows"] == 1 and counts["images"] == 1

    status_payload = _data(client.get("/api/status"))["data"]
    assert status_payload["counts"]["images"] == 1
    assert "auto_sync" in status_payload and "sync_interval" in status_payload

    cases = _data(client.get("/api/cases"))["data"]
    assert cases["total"] == 1
    assert cases["items"][0]["style"] == "ComfyUI"
    assert cases["items"][0]["title"] == "ComfyUI"
    assert cases["items"][0]["workflow_name"] == "ComfyUI"
    assert cases["styles"] == [{"key": "ComfyUI", "count": 1}]

    # refresh=1 绕过模块级 5 分钟缓存，避免读到上一个用例的模型结果
    models_payload = _data(client.get("/api/models?refresh=1"))["data"]
    assert models_payload["total"] == 0

    logs_payload = _data(client.get("/api/logs?limit=5"))["data"]
    assert isinstance(logs_payload["items"], list)

    synced = _data(client.post("/api/sync/now"))["data"]
    assert synced["workflows"] == 1 and synced["images"] == 1

    auto = _data(client.post("/api/sync/auto", json={"enabled": False}))["data"]
    assert auto["enabled"] is False


def test_download_workflow_and_path_traversal(client, env):
    """合法文件可下载（带/不带 /api 前缀），``..`` 穿越一律被拒绝且不泄露内容。

    穿越请求可能在 Werkzeug 路由层就被拒绝（400），也可能由视图的
    ``_safe_join`` 判为越界（404）。安全属性是"绝不返回目标文件内容"，
    因此这里断言状态码属于拒绝集合，而不是写死某一种实现细节。
    """
    _write_workflow(env["workflows"] / "flow.json", _api_workflow("a", "b"))

    for prefix in ("/download/workflow", "/api/download/workflow"):
        good = client.get(f"{prefix}/flow.json")
        assert good.status_code == 200
        assert json.loads(good.data.decode("utf-8"))["1"]["inputs"]["text"] == "a"

        for attack in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "nested/not-exist.json"):
            resp = client.get(f"{prefix}/{attack}")
            assert resp.status_code in (400, 403, 404), f"{attack} 未被拒绝：{resp.status_code}"
            body = resp.data.decode("utf-8", "ignore")
            assert "root:" not in body, f"{attack} 泄露了系统文件内容"


def test_page_size_cap(client):
    """page_size 超过 100 返回 400，不静默放大。"""
    payload = _data(client.get("/api/gallery?page_size=200"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"


def test_not_found_uses_unified_envelope(client):
    """资源不存在返回统一失败结构。"""
    payload = _data(client.get("/api/gallery/not-exists"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


def test_unexpected_exception_becomes_internal_error(client, monkeypatch):
    """未预期异常被 Blueprint 兜底处理器转换为 internal_error，不泄漏堆栈。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(comfyui_ctl, "probe_cached", boom)

    response = client.get("/api/health")
    payload = _data(response)

    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["error"]["code"] == "internal_error"
    assert "boom" not in json.dumps(payload, ensure_ascii=False)


def _free_port() -> int:
    """取一个当前未被监听的端口，用于验证连接失败时的降级路径。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------------ 事务


def test_tx_rolls_back_when_body_fails(tmp_path, monkeypatch):
    """tx() 必须是真事务：中途抛异常时，前面的写入要一起回滚。

    曾经 execute() 每句无条件 commit，tx() 名存实亡——
    reverse.providers.registry.set_default 就是靠它做「先清空默认、再写入新默认」，
    一旦第二步失败，库里会留下「没有任何默认 Provider」的脏状态。
    """
    from core import db as core_db

    db_file = tmp_path / "tx.db"
    monkeypatch.setattr(core_db, "db_path", lambda: db_file)
    core_db._local.conn = None

    conn = core_db.get_conn()
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()

    # 事务外：照旧自提交
    core_db.execute("INSERT INTO t (v) VALUES (?)", ("outside",))
    assert core_db.query_scalar("SELECT COUNT(*) FROM t") == 1

    # 事务内失败：两步都要回滚，第一步不能留在库里
    with pytest.raises(RuntimeError):
        with core_db.tx():
            core_db.execute("INSERT INTO t (v) VALUES (?)", ("step1",))
            raise RuntimeError("boom")

    assert core_db.query_scalar("SELECT COUNT(*) FROM t") == 1, "失败的事务必须整体回滚"
    assert core_db.query_scalar("SELECT COUNT(*) FROM t WHERE v='step1'") == 0

    # 事务内成功：正常提交
    with core_db.tx():
        core_db.execute("INSERT INTO t (v) VALUES (?)", ("step2",))
    assert core_db.query_scalar("SELECT COUNT(*) FROM t WHERE v='step2'") == 1

    # 嵌套：只有最外层收口
    with core_db.tx():
        with core_db.tx():
            core_db.execute("INSERT INTO t (v) VALUES (?)", ("nested",))
    assert core_db.query_scalar("SELECT COUNT(*) FROM t WHERE v='nested'") == 1


def test_tx_depth_marker_is_cleared_after_exception(tmp_path, monkeypatch):
    """异常退出后深度标记必须复原，否则后续所有写入都不再提交。"""
    from core import db as core_db

    db_file = tmp_path / "tx2.db"
    monkeypatch.setattr(core_db, "db_path", lambda: db_file)
    core_db._local.conn = None

    conn = core_db.get_conn()
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()

    with pytest.raises(RuntimeError):
        with core_db.tx():
            raise RuntimeError("boom")

    assert getattr(core_db._local, core_db._TX_DEPTH_ATTR, 0) == 0

    # 标记没复原的话，这条会写不进去
    core_db.execute("INSERT INTO t (v) VALUES (?)", ("after",))
    assert core_db.query_scalar("SELECT COUNT(*) FROM t WHERE v='after'") == 1
