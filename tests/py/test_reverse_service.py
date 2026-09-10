"""M9.2 / M9.3 / M9.6 / M9.7 反推任务编排、上传校验与入库流水线测试。

测试策略：
- Provider 全部替换为可观测的 ``FakeVisionProvider``，只保留真实的 ``MetadataProvider``，
  因此不依赖 ComfyUI、不发起任何网络请求；
- 与 M7 的耦合只通过 ``reverse.service._ingest`` 打桩，不依赖 ``promptlib`` 的真实实现；
- 评分与去重结果完全确定：片段文本直接选用 ``core.textutil`` 可静态推算的样例。
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from config import Config
from core import db as core_db
from core import textutil
from core.db import dumps, execute, json_field, now as db_now, query_one
from core.errors import AIBARError
from reverse import schema, service, uploads
from reverse.providers import base, registry
from reverse.providers.metadata import METADATA_NOT_FOUND, MetadataProvider

# ---------------------------------------------------------------- 常量

POSITIVE_TEXT = "一位少女站在雨夜街道，逆光形成金色轮廓光"
NEGATIVE_TEXT = "模糊，多余的手指"
PARAMETERS = (
    f"{POSITIVE_TEXT}\nNegative prompt: {NEGATIVE_TEXT}\n"
    "Steps: 20, Sampler: Euler a, CFG scale: 7"
)


# ---------------------------------------------------------------- 图片构造


def _png_bytes(color: tuple[int, int, int] = (120, 80, 200), size: tuple[int, int] = (8, 8),
               parameters: str | None = None) -> bytes:
    """生成一张 PNG；``parameters`` 会写入 tEXt 块，模拟 ComfyUI / A1111 内嵌元数据。"""
    image = Image.new("RGB", size, color)
    meta: PngInfo | None = None
    if parameters is not None:
        meta = PngInfo()
        meta.add_text("parameters", parameters)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=meta)
    return buffer.getvalue()


def _make_image(env: dict, image_id: str, parameters: str | None = None,
                color: tuple[int, int, int] = (120, 80, 200)) -> Path:
    """写入一张图库图片并登记到 ``images`` 表（``gallery_path`` 是相对路径）。"""
    gallery: Path = env["gallery"]
    gallery.mkdir(parents=True, exist_ok=True)
    name = f"{image_id}.png"
    path = gallery / name
    path.write_bytes(_png_bytes(color=color, parameters=parameters))
    execute(
        "INSERT OR REPLACE INTO images "
        "(id, filename, gallery_path, width, height, size_bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (image_id, name, f"gallery/{name}", 8, 8, path.stat().st_size, db_now()),
    )
    return path


# ---------------------------------------------------------------- Provider 打桩


class FakeVisionProvider(base.VisionProvider):
    """可观测的视觉 Provider 替身：记录调用、可注入状态与失败。"""

    key = "fake_vision"
    label = "测试视觉反推"
    description = "仅用于测试，不发起任何网络请求"
    local = False  # 非本地才会被 auto_chain 追加到链尾，便于验证降级
    requires_consent = False
    advanced_vlm = False
    quality_tier = schema.QUALITY_ADVANCED
    model = "fake-vision-1"
    quantization = "q4"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.probes = 0
        self.status = base.STATUS_READY
        self.failure: Exception | None = None
        self.sections: tuple[tuple[str, str, float], ...] = (
            ("lighting", "逆光形成金色轮廓光", 0.88),
            ("environment", "雨夜的城市街道", 0.72),
        )

    def probe(self) -> dict[str, Any]:
        self.probes += 1
        if self.status == base.STATUS_READY:
            return base.status_payload(base.STATUS_READY, model=self.model)
        return base.status_payload(self.status, "missing_model", model=self.model)

    def test_layers(self) -> list[dict[str, Any]]:
        state = self.probe()
        return [
            {
                "name": "service",
                "status": state["status"],
                "available": state["available"],
                "latency_ms": 1,
                "reason_code": state["reason_code"],
            },
            {
                "name": "model",
                "status": state["status"],
                "available": state["available"],
                "latency_ms": 2,
                "reason_code": state["reason_code"],
            },
            base.skipped_layer("inference"),
        ]

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = base.DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "profile": profile,
                "precision": precision,
                "has_path": bool((image_ref or {}).get("path")),
                "options": dict(options or {}),
            }
        )
        if self.failure is not None:
            raise self.failure
        sections = [
            schema.make_section(
                index,
                dimension,
                text,
                schema.cap_confidence(confidence, schema.SOURCE_VISION),
                evidence="来自测试视觉 Provider",
            )
            for index, (dimension, text, confidence) in enumerate(self.sections)
        ]
        return schema.validate_structured_result(
            {
                "source_type": schema.SOURCE_VISION,
                "source_label": schema.SOURCE_LABELS[schema.SOURCE_VISION],
                "provider": self.key,
                "provider_model": self.model,
                "model_profile": profile,
                "precision": precision,
                "sections": sections,
                "warnings": ["结果由测试 Provider 生成"],
                "duration_ms": 15,
                "quality_tier": schema.QUALITY_ADVANCED,
                "image_ref": {
                    "image_id": (image_ref or {}).get("image_id"),
                    "upload_id": (image_ref or {}).get("upload_id"),
                    "content_hash": (image_ref or {}).get("content_hash") or "",
                },
            }
        )


# ---------------------------------------------------------------- 环境


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库、图库与上传缓存重定向到临时目录。"""
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

    gallery = Path(Config.GALLERY_DIR)
    gallery.mkdir(parents=True, exist_ok=True)
    upload_dir = Path(Config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    yield {"data": data_dir, "gallery": gallery, "uploads": upload_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 仅清理
            pass
    core_db._local.conn = None


@pytest.fixture
def providers(env):
    """注册「内嵌元数据 + 测试视觉」两个 Provider，并清理注册表与 VLM 队列。"""
    registry.reset_registry()
    base.reset_vlm_queue()
    registry.reset_health_cache()

    metadata = MetadataProvider()
    vision = FakeVisionProvider()
    registry.register_provider(metadata)
    registry.register_provider(vision)

    yield metadata, vision

    registry.reset_registry()
    registry.reset_health_cache()
    base.reset_vlm_queue()


# ---------------------------------------------------------------- M9.2 上传校验


def test_save_upload_accepts_valid_png(env):
    payload = uploads.save_upload(io.BytesIO(_png_bytes()), "sample.png")

    assert payload["upload_id"]
    assert payload["preview_url"] == f"/api/prompt-reverse/uploads/{payload['upload_id']}/preview"
    assert payload["width"] == 8 and payload["height"] == 8
    assert payload["size_bytes"] > 0
    # 只保留 basename，绝不返回用户原始路径
    assert payload["filename"] == "sample.png"
    assert str(env["data"]) not in dumps(payload)
    # 落盘文件名就是内容哈希，对外只有受控相对引用
    stored = uploads.resolve_path(payload["upload_id"])
    assert stored is not None and stored.parent == env["uploads"]


def test_save_upload_rejects_empty_file(env):
    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(b""), "empty.png")
    assert excinfo.value.code == "empty_file"
    assert excinfo.value.status == 400


def test_save_upload_rejects_oversized_file(env, monkeypatch):
    monkeypatch.setattr(Config, "REVERSE_UPLOAD_MAX_MB", 1)
    noise = Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    assert buffer.tell() > 1024 * 1024

    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(buffer.getvalue()), "big.png")
    assert excinfo.value.code == "file_too_large"
    assert excinfo.value.status == 413


def test_save_upload_rejects_bad_extension(env):
    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(_png_bytes()), "sample.gif")
    assert excinfo.value.code == "bad_extension"


def test_save_upload_rejects_broken_image(env):
    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(b"not-an-image-at-all"), "broken.png")
    assert excinfo.value.code == "invalid_image"
    # 失败不留下任何残留文件
    assert [p.name for p in env["uploads"].iterdir() if not p.name.startswith(".tmp_")] == []


def test_save_upload_rejects_animated_image(env):
    first = Image.new("RGB", (8, 8), (255, 0, 0))
    second = Image.new("RGB", (8, 8), (0, 0, 255))
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second])

    with pytest.raises(AIBARError) as excinfo:
        uploads.save_upload(io.BytesIO(buffer.getvalue()), "anim.webp")
    assert excinfo.value.code == "animated_image"


# ---------------------------------------------------------------- M9.3 元数据优先，不惊动视觉 Provider


def test_metadata_recovery_does_not_call_vision_provider(env, providers):
    _metadata, vision = providers
    _make_image(env, "img1", parameters=PARAMETERS)

    job = service.create_job(image_id="img1")

    assert job["status"] == service.STATUS_COMPLETED
    assert job["provider"] == "metadata"
    assert job["source_type"] == schema.SOURCE_METADATA
    assert job["quality_tier"] == schema.QUALITY_ORIGINAL
    assert vision.calls == [], "元数据恢复成功时不得调用任何视觉 Provider"
    # 原始正/负向提示词完整保留
    assert job["recovered_original_prompt"] == POSITIVE_TEXT
    assert job["recovered_negative_prompt"] == NEGATIVE_TEXT
    # 结果引用绝不携带本机路径
    assert "path" not in job["image_ref"]
    assert job["image_ref"]["image_id"] == "img1"
    # 运行参数不进入片段，但仍保留在恢复原文里
    assert all("Steps" not in section["text"] for section in job["sections"])
    assert all(section["dimension"] in schema.image_dimension_keys() for section in job["sections"])


def test_auto_chain_falls_back_to_vision_when_metadata_missing(env, providers):
    _metadata, vision = providers
    _make_image(env, "img2")  # 无内嵌元数据

    job = service.create_job(image_id="img2")

    assert job["provider"] == "fake_vision"
    assert job["source_type"] == schema.SOURCE_VISION
    assert len(vision.calls) == 1
    assert vision.calls[0]["has_path"] is True, "Provider 内部能拿到受控副本，但绝不外泄"
    assert job["stale"] is False


def test_vision_mode_skips_metadata_even_when_available(env, providers):
    _metadata, vision = providers
    _make_image(env, "img3", parameters=PARAMETERS)

    job = service.create_job(image_id="img3", source_mode=service.SOURCE_MODE_VISION)

    assert job["provider"] == "fake_vision"
    assert len(vision.calls) == 1


def test_metadata_mode_fails_without_metadata(env, providers):
    _metadata, vision = providers
    _make_image(env, "img4")

    with pytest.raises(AIBARError) as excinfo:
        service.create_job(image_id="img4", source_mode=service.SOURCE_MODE_METADATA)

    assert excinfo.value.code == METADATA_NOT_FOUND
    assert vision.calls == []
    row = query_one("SELECT status, error_code FROM prompt_reverse_jobs ORDER BY id DESC LIMIT 1")
    assert row["status"] == service.STATUS_FAILED
    assert row["error_code"] == METADATA_NOT_FOUND


def test_explicit_provider_is_respected(env, providers):
    _metadata, vision = providers
    _make_image(env, "img5", parameters=PARAMETERS)

    job = service.create_job(image_id="img5", provider="fake_vision")

    assert job["provider"] == "fake_vision"
    assert len(vision.calls) == 1


def test_unavailable_provider_reports_provider_unavailable(env, providers):
    _metadata, vision = providers
    vision.status = base.STATUS_MISSING_MODEL
    registry.reset_health_cache()
    _make_image(env, "img6")

    with pytest.raises(AIBARError) as excinfo:
        service.create_job(
            image_id="img6", source_mode=service.SOURCE_MODE_VISION, provider="fake_vision"
        )

    assert excinfo.value.code == "provider_unavailable"
    assert excinfo.value.status == 503
    assert vision.calls == []


def test_runtime_failure_is_not_silently_downgraded(env, providers):
    """推理错误 / OOM 必须停下来告知用户，绝不自动加载下一个大模型。"""
    _metadata, vision = providers
    vision.failure = AIBARError("out_of_memory", "内存不足，请关闭其他大模型后重试", 503)
    _make_image(env, "img7")

    with pytest.raises(AIBARError) as excinfo:
        service.create_job(image_id="img7")

    assert excinfo.value.code == "out_of_memory"
    # 链上排在后面的 Provider 没有被继续尝试
    assert len(vision.calls) == 1


# ---------------------------------------------------------------- M9.3 状态与阶段


def test_job_status_and_stage_labels(env, providers):
    _make_image(env, "img8", parameters=PARAMETERS)

    job = service.create_job(image_id="img8")

    assert job["status"] == service.STATUS_COMPLETED
    assert job["stage"] == service.STAGE_DONE
    assert job["stage_label"] == service.STAGE_LABELS[service.STAGE_DONE]
    assert job["duration_ms"] >= 0
    # 阶段只报中文可读标签，绝不使用虚假百分比
    assert not any("%" in label for label in service.STAGE_LABELS.values())
    assert set(service.STAGE_LABELS) == {
        service.STAGE_QUEUED,
        service.STAGE_READING,
        service.STAGE_METADATA,
        service.STAGE_VISION,
        service.STAGE_STRUCTURING,
        service.STAGE_DONE,
        service.STAGE_FAILED,
        service.STAGE_CANCELLED,
    }


def test_cancel_does_not_delete_last_successful_result(env, providers):
    _make_image(env, "img9", parameters=PARAMETERS)
    job = service.create_job(image_id="img9")

    cancelled = service.cancel_job(job["job_id"])

    assert cancelled["status"] == service.STATUS_COMPLETED
    assert cancelled["has_result"] is True
    assert cancelled["sections"], "取消不得删除上次成功结果"
    assert cancelled["recovered_original_prompt"] == POSITIVE_TEXT


def test_unknown_image_id_fails_without_touching_providers(env, providers):
    _metadata, vision = providers

    with pytest.raises(AIBARError) as excinfo:
        service.create_job(image_id="missing-image")

    assert excinfo.value.code == "image_not_found"
    assert vision.calls == []


def test_replaced_image_marks_previous_result_stale(env, providers):
    _make_image(env, "img10", parameters=PARAMETERS)
    first = service.create_job(image_id="img10")

    # 同一 image_id 换了一张图（内容哈希变化）
    _make_image(env, "img10", parameters=PARAMETERS + "，电影感胶片颗粒", color=(20, 30, 40))
    second = service.create_job(image_id="img10")

    assert first["job_id"] != second["job_id"]
    stale = service.get_job(first["job_id"])
    assert stale["stale"] is True
    assert stale["stale_reason"] == "source_replaced"
    assert service.get_job(second["job_id"])["stale"] is False


# ---------------------------------------------------------------- M9.6 入库流水线


def _ingest_spy(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]] | None = None):
    """打桩 ``service._ingest``，记录每次调用的入参并按序返回给定结果。"""
    calls: list[dict[str, Any]] = []
    queue = list(responses or [])
    default = {"inserted": [], "merged": [], "candidates": [], "discarded": []}

    def fake_ingest(segments, media_type, source_type, source_ref, thresholds=None):
        calls.append(
            {
                "segments": [dict(segment) for segment in segments],
                "media_type": media_type,
                "source_type": source_type,
                "source_ref": dict(source_ref or {}),
                "thresholds": thresholds,
            }
        )
        return queue.pop(0) if queue else default

    monkeypatch.setattr(service, "_ingest", fake_ingest)
    return calls


def test_save_job_result_dispatches_to_library(env, providers, monkeypatch):
    _make_image(env, "img11", parameters=PARAMETERS)
    job = service.create_job(image_id="img11")

    calls = _ingest_spy(
        monkeypatch,
        [
            {
                "inserted": [11, 12],
                "merged": [13],
                "candidates": [14],
                "discarded": [{"text": "低质片段", "reason": "low_quality"}],
            }
        ],
    )

    saved = service.save_job_result(job["job_id"])

    assert saved["inserted"] == 2
    assert saved["merged"] == 1
    assert saved["candidates"] == 1
    assert saved["entry_ids"] == [11, 12, 13]
    assert saved["saved"] is True
    assert saved["discarded"] >= 1

    assert len(calls) == 1
    assert calls[0]["media_type"] == schema.MEDIA_TYPE
    assert calls[0]["source_type"] == "reverse_metadata"
    assert calls[0]["source_ref"]["job_id"] == job["job_id"]
    # 来源引用绝不携带本机路径或提示词正文
    assert not {"path", "source_path", "file_path", "prompt", "text"} & set(calls[0]["source_ref"])

    segments = calls[0]["segments"]
    assert segments, "应当有拆分后的片段进入词库"
    # 绝不能把 formatted_positive 或整段恢复提示词作为单条词条
    assert all(segment["text"] != job["formatted_positive"] for segment in segments)
    assert all(segment["text"] != POSITIVE_TEXT for segment in segments)
    assert all(len(segment["text"]) <= service.MAX_SEGMENT_LENGTH for segment in segments)

    row = query_one("SELECT saved_at FROM prompt_reverse_jobs WHERE id=?", (job["job_id"],))
    assert row["saved_at"], "保存后必须写入 saved_at"


def test_save_job_result_is_idempotent(env, providers, monkeypatch):
    """重复保存不会重复入库：第二次命中既有词条，计入 merged。"""
    _make_image(env, "img12", parameters=PARAMETERS)
    job = service.create_job(image_id="img12")

    calls = _ingest_spy(
        monkeypatch,
        [
            {"inserted": [21], "merged": [], "candidates": [], "discarded": []},
            {"inserted": [], "merged": [21], "candidates": [], "discarded": []},
        ],
    )

    first = service.save_job_result(job["job_id"])
    second = service.save_job_result(job["job_id"])

    assert (first["inserted"], first["merged"]) == (1, 0)
    assert (second["inserted"], second["merged"]) == (0, 1)
    assert len(calls) == 2
    # 两次交给 M7 的片段完全一致，这是精确指纹去重的前提
    assert calls[0]["segments"] == calls[1]["segments"]


def test_prepare_segments_never_stores_full_paragraph(env):
    """整段未拆分、运行参数、风险内容一律不得成为词条。"""
    unsplittable = "这是一段没有任何标点的超长完整视觉描述" * 6
    result = {
        "model_profile": "generic",
        "sections": [
            {"dimension": "subject", "text": unsplittable, "confidence": 0.9},
            {"dimension": "camera", "text": "镜头，85mm 人像特写，浅景深背景虚化", "confidence": 0.9},
            {"dimension": "style", "text": "Steps: 20, Sampler: Euler a", "confidence": 0.9},
            {"dimension": "subject", "text": "by Van Gogh 的笔触", "confidence": 0.9},
        ],
    }

    kept, discarded = service._prepare_segments(result)

    reasons = {item["reason"] for item in discarded}
    assert "unsplit_text" in reasons
    assert "noise" in reasons
    assert any(reason.startswith("risk:") for reason in reasons)
    assert all(len(segment["text"]) <= service.MAX_SEGMENT_LENGTH for segment in kept)
    assert all(segment["text"] != unsplittable for segment in kept)


def test_near_duplicate_segments_collapse_to_one(env):
    result = {
        "model_profile": "generic",
        "sections": [
            {"dimension": "lighting", "text": "逆光形成金色轮廓光", "confidence": 0.9},
            {"dimension": "lighting", "text": "逆光形成金色的轮廓光", "confidence": 0.9},
            {"dimension": "lighting", "text": "侧光勾勒出人物的面部轮廓光", "confidence": 0.9},
        ],
    }

    kept, discarded = service._prepare_segments(result)

    texts = [segment["text"] for segment in kept]
    assert len(kept) == 2
    assert "逆光形成金色的轮廓光" not in texts, "近似重复只保留质量更高的一条"
    assert any(item["reason"] == "near_duplicate" for item in discarded)
    # 保留条目的指纹就是 M7 做精确匹配与写别名用的同一个指纹
    for segment in kept:
        assert segment["fingerprint"] == textutil.content_fingerprint(
            schema.MEDIA_TYPE, segment["dimension"], segment["text"]
        )


def test_uncertain_low_confidence_segments_are_dropped(env):
    result = {
        "model_profile": "generic",
        "sections": [
            {
                "dimension": "subject",
                "text": "少女也许拿着一把旧伞",
                "confidence": 0.4,
                "uncertainty": "模型不确定手部细节",
            }
        ],
    }

    kept, discarded = service._prepare_segments(result)

    assert kept == []
    assert [item["reason"] for item in discarded] == ["low_confidence_uncertain"]


def test_save_uses_edited_result(env, providers, monkeypatch):
    _make_image(env, "img13", parameters=PARAMETERS)
    job = service.create_job(image_id="img13")

    service.update_result(
        job["job_id"],
        [{"dimension": "mood", "text": "宁静的雨夜氛围", "confidence": 0.9}],
    )
    calls = _ingest_spy(monkeypatch)

    service.save_job_result(job["job_id"])

    assert len(calls) == 1
    assert [segment["dimension"] for segment in calls[0]["segments"]] == ["mood"]


def test_update_result_rejects_invalid_sections(env, providers):
    _make_image(env, "img14", parameters=PARAMETERS)
    job = service.create_job(image_id="img14")

    with pytest.raises(AIBARError) as excinfo:
        service.update_result(
            job["job_id"], [{"dimension": "not_a_dimension", "text": "未知维度"}]
        )
    assert excinfo.value.code == "invalid_input"

    updated = service.update_result(
        job["job_id"], [{"dimension": "lighting", "text": "逆光形成金色轮廓光", "confidence": 0.9}]
    )
    assert len(updated["sections"]) == 1
    assert updated["formatted_positive"] == "逆光形成金色轮廓光"


def test_save_without_result_raises_not_found(env, providers):
    _make_image(env, "img15")
    job_id = service._insert_job(
        image_id="img15",
        source_mode=service.SOURCE_MODE_AUTO,
        profile="generic",
        precision=schema.DEFAULT_PRECISION,
        provider="auto",
    )

    with pytest.raises(AIBARError) as excinfo:
        service.save_job_result(job_id)
    assert excinfo.value.code == "not_found"
    assert excinfo.value.status == 404


# ---------------------------------------------------------------- M9.7 历史与缓存清理


def test_delete_history_cleans_unreferenced_uploads(env, providers):
    first = uploads.save_upload(io.BytesIO(_png_bytes(color=(10, 20, 30), parameters=PARAMETERS)), "a.png")
    second = uploads.save_upload(io.BytesIO(_png_bytes(color=(200, 30, 40), parameters=PARAMETERS)), "b.png")

    job_a = service.create_job(upload_id=first["upload_id"])
    job_b = service.create_job(upload_id=second["upload_id"])
    assert job_a["status"] == service.STATUS_COMPLETED
    assert job_b["status"] == service.STATUS_COMPLETED

    row = query_one("SELECT upload_ref FROM prompt_reverse_jobs WHERE id=?", (job_b["job_id"],))
    assert row["upload_ref"].startswith("uploads/")
    assert ".." not in row["upload_ref"]

    result = service.delete_history(job_a["job_id"])

    assert result["deleted"] is True
    assert uploads.resolve_path(first["upload_id"]) is None, "无引用的缓存必须清理"
    assert uploads.resolve_path(second["upload_id"]) is not None, "仍被引用的缓存必须保留"
    assert result["upload_cleanup"]["removed"] >= 1


def test_delete_history_keeps_entries_but_detaches_source_ref(env, providers):
    _make_image(env, "img16", parameters=PARAMETERS)
    job = service.create_job(image_id="img16")

    fingerprint = textutil.content_fingerprint(schema.MEDIA_TYPE, "lighting", "逆光形成金色轮廓光")
    execute(
        "INSERT INTO prompt_entries "
        "(media_type, dimension, title, prompt_text, source_type, source_ref, "
        " content_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schema.MEDIA_TYPE,
            "lighting",
            "逆光形成金色轮廓光",
            "逆光形成金色轮廓光",
            "reverse_vision",
            dumps({"job_id": job["job_id"], "provider": "metadata"}),
            fingerprint,
            db_now(),
        ),
    )

    result = service.delete_history(job["job_id"])

    assert result["detached_source_refs"] == 1
    row = query_one("SELECT id, source_ref FROM prompt_entries WHERE content_fingerprint=?", (fingerprint,))
    assert row is not None, "已入库词条不随历史级联删除"
    remaining = json_field(row["source_ref"], {}) or {}
    assert "job_id" not in remaining


def test_cleanup_expired_uploads_keeps_referenced_files(env, providers):
    referenced = uploads.save_upload(io.BytesIO(_png_bytes(parameters=PARAMETERS)), "keep.png")
    orphan = uploads.save_upload(io.BytesIO(_png_bytes(color=(9, 9, 9), parameters=PARAMETERS)), "drop.png")
    job = service.create_job(upload_id=referenced["upload_id"])
    assert job["status"] == service.STATUS_COMPLETED

    stats = service.cleanup_uploads(0)

    assert uploads.resolve_path(referenced["upload_id"]) is not None
    assert uploads.resolve_path(orphan["upload_id"]) is None
    assert stats["removed"] >= 1
    assert stats["kept_referenced"] >= 1


def test_list_history_filters_and_limits(env, providers):
    _make_image(env, "img17", parameters=PARAMETERS)
    _make_image(env, "img18")
    service.create_job(image_id="img17")
    service.create_job(image_id="img18")

    history = service.list_history(10)
    assert history["total"] == 2
    assert history["limit"] == 10
    # 历史列表只给元信息，不给提示词正文
    assert all("recovered_original_prompt" not in item for item in history["items"])
    assert all("sections" not in item for item in history["items"])

    metadata_only = service.list_history(10, schema.SOURCE_METADATA)
    assert metadata_only["total"] == 1
    assert metadata_only["items"][0]["preview_url"] == ""

    assert service.list_history(0)["limit"] == 1
    assert service.list_history(999)["limit"] == service.MAX_HISTORY_LIMIT

    with pytest.raises(AIBARError) as excinfo:
        service.list_history(10, "not_a_source")
    assert excinfo.value.code == "invalid_input"


def test_target_modes_exposes_taxonomy_options(env):
    modes = service.target_modes()

    assert {profile["key"] for profile in modes["profiles"]} == set(textutil.profile_keys())
    assert [item["key"] for item in modes["precisions"]] == list(schema.PRECISIONS)
    assert [item["key"] for item in modes["dimensions"]] == schema.image_dimension_keys()
    assert [item["key"] for item in modes["source_modes"]] == list(service.SOURCE_MODES)
    assert modes["media_type"] == schema.MEDIA_TYPE


# ---------------------------------------------------------------- 异步执行与真实进度


def _await_terminal(job_id: int, timeout: float = 10.0) -> dict[str, Any]:
    """轮询任务直到终态。生产环境的轮询靠 HTTP，这里直接读 service。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job["status"] in (
            service.STATUS_COMPLETED,
            service.STATUS_FAILED,
            service.STATUS_CANCELLED,
        ):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 在 {timeout}s 内没有进入终态")


def test_create_job_async_returns_pending_job_then_completes(env, providers):
    """异步模式先给 job_id，前端才有得可轮询。"""
    _make_image(env, "img20", parameters=PARAMETERS)

    pending = service.create_job(image_id="img20", run_async=True)

    assert pending["job_id"] > 0
    assert pending["status"] == service.STATUS_PENDING
    assert pending["stage"] == service.STAGE_QUEUED
    assert pending["stage_label"] == "排队中"
    # 关键：job_id 必须在结果算出来之前就到手，否则无从轮询
    assert not pending.get("sections")

    done = _await_terminal(pending["job_id"])
    assert done["status"] == service.STATUS_COMPLETED
    assert done["provider"] == "metadata"
    assert done["sections"]


def test_create_job_async_failure_lands_in_db_without_escaping(env, providers):
    """后台线程里的异常没人接得住，必须落库成 failed，否则前端永远等不到终态。"""
    _metadata, vision = providers
    vision.failure = AIBARError("vision_failed", "视觉 Provider 炸了", 502)
    _make_image(env, "img21")  # 无内嵌元数据 -> 一定会走到视觉 Provider

    pending = service.create_job(image_id="img21", run_async=True)

    failed = _await_terminal(pending["job_id"])
    assert failed["status"] == service.STATUS_FAILED
    assert failed["error_code"] == "vision_failed"
    assert failed["stage"] == service.STAGE_FAILED


def test_create_job_async_rejects_bad_input_before_spawning_thread(env, providers):
    """参数错误必须同步抛：起线程再失败，客户端只能靠轮询才知道错了。"""
    _make_image(env, "img22", parameters=PARAMETERS)

    with pytest.raises(AIBARError) as excinfo:
        service.create_job(image_id="img22", source_mode="telepathy", run_async=True)
    assert excinfo.value.code == "invalid_input"


def test_is_active_tracks_in_flight_jobs(env, providers):
    """在途标记是「还在跑」与「已经成孤儿」的唯一分界线。"""
    _make_image(env, "img23", parameters=PARAMETERS)
    job = service.create_job(image_id="img23")
    assert service.is_active(job["job_id"]) is False, "同步任务结束后必须清掉在途标记"


def test_get_job_marks_orphaned_in_flight_job_as_failed(env, providers):
    """服务重启后残留的 running 任务：没人推进它，读取时判死，轮询才能收敛。"""
    orphan_id = service._insert_job(
        image_id="img24",
        source_mode=service.SOURCE_MODE_AUTO,
        profile="generic",
        precision=schema.DEFAULT_PRECISION,
        provider="auto",
    )
    assert service.is_active(orphan_id) is False

    job = service.get_job(orphan_id)

    assert job["status"] == service.STATUS_FAILED
    assert job["error_code"] == "interrupted"
    # 判死是一次性的：再读不该反复改写
    assert service.get_job(orphan_id)["error_code"] == "interrupted"
