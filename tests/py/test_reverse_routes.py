"""M9 / M10 / M11 路由冒烟测试：统一 envelope、入参校验、绝不接受任意本机路径。

只注册 ``reverse.routes.bp``，不依赖 ``app.py`` 装配；Provider 全部替换为替身，
不发起任何网络请求。
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from config import Config
from core import db as core_db
from reverse import schema, service, uploads
from reverse.providers import base, registry
from reverse.providers.metadata import MetadataProvider
from reverse.providers.openai_compatible import create_openai_compatible_providers
from reverse.routes import bp

POSITIVE_TEXT = "一位少女站在雨夜街道，逆光形成金色轮廓光"
PARAMETERS = (
    f"{POSITIVE_TEXT}\nNegative prompt: 模糊，多余的手指\n"
    "Steps: 20, Sampler: Euler a, CFG scale: 7"
)


def _png_bytes(parameters: str | None = None, color: tuple[int, int, int] = (120, 80, 200)) -> bytes:
    image = Image.new("RGB", (8, 8), color)
    meta: PngInfo | None = None
    if parameters is not None:
        meta = PngInfo()
        meta.add_text("parameters", parameters)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=meta)
    return buffer.getvalue()


# ---------------------------------------------------------------- 环境


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
    Path(Config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

    yield {"data": data_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 仅清理
            pass
    core_db._local.conn = None


@pytest.fixture
def client(env):
    """只注册反推蓝图，不依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _providers():
    """只注册不需要 ComfyUI 的 Provider，保证路由测试不发起任何网络请求。"""
    registry.reset_registry()
    registry.reset_health_cache()
    base.reset_vlm_queue()
    registry.register_provider(MetadataProvider())
    for provider in create_openai_compatible_providers():
        registry.register_provider(provider)
    yield
    registry.reset_registry()
    registry.reset_health_cache()
    base.reset_vlm_queue()


# ---------------------------------------------------------------- 工具


def _upload(client, parameters: str | None = PARAMETERS, name: str = "sample.png") -> dict[str, Any]:
    response = client.post(
        "/api/prompt-reverse/uploads",
        data={"file": (io.BytesIO(_png_bytes(parameters)), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _create_job(client, **payload: Any) -> Any:
    return client.post("/api/prompt-reverse/jobs", json=payload)


# ---------------------------------------------------------------- M9.2 上传


def test_upload_endpoint_returns_unified_envelope(client):
    payload = _upload(client)

    assert payload["upload_id"]
    assert payload["filename"] == "sample.png"
    assert payload["preview_url"].startswith("/api/prompt-reverse/uploads/")
    assert payload["has_metadata"] is True
    assert "path" not in payload


def test_upload_endpoint_rejects_empty_field(client):
    response = client.post(
        "/api/prompt-reverse/uploads", data={}, content_type="multipart/form-data"
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "empty_file"


def test_upload_endpoint_rejects_unsupported_extension(client):
    response = client.post(
        "/api/prompt-reverse/uploads",
        data={"file": (io.BytesIO(_png_bytes()), "sample.gif")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "bad_extension"


def test_preview_endpoint_rejects_unknown_upload(client):
    response = client.get("/api/prompt-reverse/uploads/deadbeef/preview")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- M9.3 任务


def test_create_job_requires_exactly_one_reference(client):
    missing = _create_job(client)
    assert missing.status_code == 400
    assert missing.get_json()["error"]["code"] == "invalid_input"

    both = _create_job(client, image_id="img-1", upload_id="a" * 64)
    assert both.status_code == 400
    assert both.get_json()["error"]["code"] == "invalid_input"


def test_create_job_rejects_arbitrary_local_paths(client):
    for image_id in ("../../etc/passwd", "/etc/passwd", "a/b", "..%2F.."):
        response = _create_job(client, image_id=image_id)
        assert response.status_code == 400, image_id
        assert response.get_json()["error"]["code"] == "invalid_input"


def test_create_job_rejects_unknown_source_mode(client):
    payload = _upload(client)
    response = _create_job(client, upload_id=payload["upload_id"], source_mode="telepathy")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_create_job_rejects_non_object_options(client):
    payload = _upload(client)
    response = _create_job(client, upload_id=payload["upload_id"], options=[1, 2])
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_job_roundtrip_uses_metadata_provider(client):
    payload = _upload(client)

    created = _create_job(client, upload_id=payload["upload_id"])
    assert created.status_code == 200
    job = created.get_json()["data"]
    assert job["status"] == service.STATUS_COMPLETED
    assert job["provider"] == "metadata"
    assert job["source_type"] == schema.SOURCE_METADATA
    assert job["recovered_original_prompt"] == POSITIVE_TEXT

    fetched = client.get(f"/api/prompt-reverse/jobs/{job['job_id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["data"]["job_id"] == job["job_id"]

    cancelled = client.post(f"/api/prompt-reverse/jobs/{job['job_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.get_json()["data"]["status"] == service.STATUS_COMPLETED, "取消不得删掉成功结果"


def test_create_job_async_returns_202_with_pending_job(client):
    """async=true 时立即返回 job_id（202 pending），前端才有得可轮询。"""
    payload = _upload(client)

    created = _create_job(client, upload_id=payload["upload_id"], **{"async": True})
    assert created.status_code == 202
    job = created.get_json()["data"]
    assert job["job_id"] > 0
    assert job["status"] == service.STATUS_PENDING
    assert job["stage"] == service.STAGE_QUEUED

    # 轮询几次总能等到终态；每一轮都必须带得上阶段名
    deadline = time.monotonic() + 10
    seen_labels = []
    while time.monotonic() < deadline:
        polled = client.get(f"/api/prompt-reverse/jobs/{job['job_id']}").get_json()["data"]
        seen_labels.append(polled["stage_label"])
        if polled["status"] != service.STATUS_PENDING:
            break
        time.sleep(0.02)

    assert polled["status"] == service.STATUS_COMPLETED
    assert polled["provider"] == "metadata"
    assert polled["recovered_original_prompt"] == POSITIVE_TEXT
    assert polled["duration_ms"] >= 0


def test_create_job_async_rejects_non_boolean_async(client):
    """async 只认布尔：传字符串不能悄悄变成「真」。"""
    response = _create_job(client, upload_id=_upload(client)["upload_id"], **{"async": "yes"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_patch_result_accepts_valid_sections_and_rejects_bad_ones(client):
    job = _create_job(client, upload_id=_upload(client)["upload_id"]).get_json()["data"]
    url = f"/api/prompt-reverse/jobs/{job['job_id']}/result"

    not_array = client.patch(url, json={"sections": "nope"})
    assert not_array.status_code == 400
    assert not_array.get_json()["error"]["code"] == "invalid_input"

    bad_dimension = client.patch(url, json={"sections": [{"dimension": "bogus", "text": "x"}]})
    assert bad_dimension.status_code == 400
    assert bad_dimension.get_json()["error"]["code"] == "invalid_input"

    patched = client.patch(
        url,
        json={
            "sections": [
                {"dimension": "lighting", "text": "逆光形成金色轮廓光", "confidence": 0.9}
            ]
        },
    )
    assert patched.status_code == 200
    data = patched.get_json()["data"]
    assert len(data["sections"]) == 1
    assert data["formatted_positive"] == "逆光形成金色轮廓光"


def test_save_endpoint_reports_library_split(client, monkeypatch):
    job = _create_job(client, upload_id=_upload(client)["upload_id"]).get_json()["data"]

    def fake_ingest(segments, media_type, source_type, source_ref, thresholds=None):
        return {
            "inserted": [101],
            "merged": [102],
            "candidates": [103],
            "discarded": [{"text": "低质", "reason": "low_quality"}],
        }

    monkeypatch.setattr(service, "_ingest", fake_ingest)

    response = client.post(f"/api/prompt-reverse/jobs/{job['job_id']}/save")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["inserted"] == 1
    assert data["merged"] == 1
    assert data["candidates"] == 1
    assert data["saved"] is True
    assert data["entry_ids"] == [101, 102]


def test_save_endpoint_reports_not_found_without_result(client):
    response = client.post("/api/prompt-reverse/jobs/999999/save")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_missing_job_returns_not_found(client):
    response = client.get("/api/prompt-reverse/jobs/999999")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- M9.7 历史


def test_history_endpoints(client):
    empty = client.get("/api/prompt-reverse/history?limit=5")
    assert empty.status_code == 200
    assert empty.get_json()["data"]["items"] == []

    bad_filter = client.get("/api/prompt-reverse/history?source_type=telepathy")
    assert bad_filter.status_code == 400
    assert bad_filter.get_json()["error"]["code"] == "invalid_input"

    missing = client.delete("/api/prompt-reverse/history/424242")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "not_found"


def test_history_delete_cleans_upload_cache(client):
    payload = _upload(client)
    job = _create_job(client, upload_id=payload["upload_id"]).get_json()["data"]
    assert uploads.resolve_path(payload["upload_id"]) is not None

    response = client.delete(f"/api/prompt-reverse/history/{job['job_id']}")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["deleted"] is True
    assert uploads.resolve_path(payload["upload_id"]) is None


# ---------------------------------------------------------------- M10 / M11 Provider 中心


def test_providers_endpoints_smoke(client):
    listed = client.get("/api/prompt-reverse/providers")
    assert listed.status_code == 200
    body = listed.get_json()["data"]
    assert body["default_provider"]
    assert isinstance(body["items"], list)
    assert all("endpoint" not in item for item in body["items"])

    refreshed = client.post("/api/prompt-reverse/providers/refresh")
    assert refreshed.status_code == 200
    assert refreshed.get_json()["data"]["items"]


def test_provider_test_endpoint_returns_layers(client):
    response = client.post("/api/prompt-reverse/providers/metadata/test")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ok"] is True
    assert [layer["name"] for layer in data["layers"]] == list(registry.LAYER_NAMES)

    unknown = client.post("/api/prompt-reverse/providers/does_not_exist/test")
    assert unknown.status_code == 404
    assert unknown.get_json()["error"]["code"] == "provider_not_found"


def test_consent_endpoints(client):
    local = client.get("/api/prompt-reverse/providers/metadata/consent")
    assert local.status_code == 200
    assert local.get_json()["data"]["requires_consent"] is False

    revoked = client.post(
        "/api/prompt-reverse/providers/openai_compatible_vision/consent",
        json={"consented": False},
    )
    assert revoked.status_code == 200
    assert revoked.get_json()["data"]["consented"] is False

    bad = client.post("/api/prompt-reverse/providers/metadata/consent", json={"consented": "yes"})
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_input"


def test_default_provider_endpoint(client):
    response = client.patch("/api/prompt-reverse/providers/default", json={"provider_key": "metadata"})
    assert response.status_code == 200
    assert response.get_json()["data"]["default_provider"] == "metadata"

    auto = client.patch("/api/prompt-reverse/providers/default", json={"provider_key": "auto"})
    assert auto.get_json()["data"]["auto_enabled"] is True

    invalid = client.patch("/api/prompt-reverse/providers/default", json={"provider_key": "no/such"})
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_input"

    empty = client.patch("/api/prompt-reverse/providers/default", json={})
    assert empty.status_code == 400
    assert empty.get_json()["error"]["code"] == "invalid_input"


def test_target_modes_endpoint(client):
    response = client.get("/api/prompt-reverse/target-modes")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["media_type"] == schema.MEDIA_TYPE
    assert [item["key"] for item in data["precisions"]] == list(schema.PRECISIONS)
    assert data["dimensions"], "维度选项必须来自分类体系"


# ---------------------------------------------------------------- 错误处理


def test_failure_uses_unified_envelope_without_data(client):
    response = _create_job(client, image_id="missing-image")
    body = response.get_json()
    assert response.status_code == 400
    assert body["ok"] is False
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "image_not_found"
    assert "data" not in body


def test_unexpected_exception_becomes_internal_error(client, monkeypatch):
    def boom() -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "target_modes", boom)

    response = client.get("/api/prompt-reverse/target-modes")
    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "internal_error"
    assert "boom" not in body["error"]["message"], "不得把异常细节泄漏给前端"
