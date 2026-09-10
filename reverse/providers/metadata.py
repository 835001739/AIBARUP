"""M9.5 必选内置 Provider：从 AIBAR / ComfyUI 内嵌元数据恢复原始提示词。

- 无网络请求、无需授权、始终可用；
- 恢复成功时**默认不调用视觉 Provider**（由 service 的自动链保证）；
- 保留原始正向/负向提示词，并按 M7 规则用 ``split_segments`` + ``classify_dimension`` 拆分；
- 没有可靠内容的维度不生成空词条；运行参数不进入片段（但仍保留在恢复原文中）。
"""

from __future__ import annotations

import time
from typing import Any

from core.errors import AIBARError
from core.imagemeta import extract_prompt_metadata
from core.textutil import classify_dimension, has_content_value, normalize_text, split_segments

from .. import schema
from .base import (
    DEFAULT_PRECISION,
    STATUS_READY,
    VisionProvider,
    profile_or_default,
    status_payload,
)

METADATA_NOT_FOUND = "metadata_not_found"

# 元数据来源可信，置信度高于任何视觉推测，但仍低于 1.0（维度归类仍是启发式）
METADATA_CONFIDENCE = 0.92
WEAK_CLASSIFY_CONFIDENCE = 0.60


def no_metadata_error() -> AIBARError:
    return AIBARError(METADATA_NOT_FOUND, "该图片没有可用的生成元数据，可改用视觉反推")


class MetadataProvider(VisionProvider):
    """内嵌元数据恢复 Provider（必选内置）。"""

    key = "metadata"
    label = "内嵌元数据恢复"
    description = "读取 PNG 内嵌 prompt/workflow 与 AIBAR 图库记录，恢复原始提示词"
    local = True
    quality_tier = schema.QUALITY_ORIGINAL
    model = "png-metadata"
    requires_consent = False
    capabilities = {"precision": ["fast", "standard", "fine"], "vision": False}

    def probe(self) -> dict[str, Any]:
        return status_payload(STATUS_READY, model=self.model)

    def test_layers(self) -> list[dict[str, Any]]:
        return [
            {"name": "service", "status": STATUS_READY, "available": True, "latency_ms": 0, "reason_code": ""},
            {"name": "model", "status": STATUS_READY, "available": True, "latency_ms": 0, "reason_code": ""},
            {"name": "inference", "status": STATUS_READY, "available": True, "latency_ms": 0, "reason_code": ""},
        ]

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        path = (image_ref or {}).get("path")
        if not path:
            raise no_metadata_error()

        meta = extract_prompt_metadata(path) or {}
        positive = str(meta.get("positive") or "").strip()
        negative = str(meta.get("negative") or "").strip()
        if not meta.get("has_metadata") or not (positive or negative):
            raise no_metadata_error()

        warnings: list[str] = []
        sections: list[dict[str, Any]] = []
        seen: set[str] = set()

        for text, forced_negative in ((positive, False), (negative, True)):
            if not text:
                continue
            for segment in split_segments(text):
                if not has_content_value(segment):
                    continue
                key = normalize_text(segment)
                if not key or key in seen:
                    continue
                seen.add(key)
                if forced_negative:
                    dimension, classify_conf = "negative", 1.0
                else:
                    dimension, classify_conf = classify_dimension(segment, schema.MEDIA_TYPE)
                if not schema.coerce_dimension(dimension):
                    continue
                uncertainty = ""
                if classify_conf < WEAK_CLASSIFY_CONFIDENCE and not forced_negative:
                    uncertainty = "维度由关键词推断，可手动调整"
                sections.append(
                    schema.make_section(
                        len(sections),
                        dimension,
                        segment,
                        schema.cap_confidence(METADATA_CONFIDENCE, schema.SOURCE_METADATA),
                        uncertainty=uncertainty,
                        evidence="来自图片内嵌生成元数据",
                        selected_for_library=True,
                    )
                )
                if len(sections) >= schema.MAX_SECTIONS:
                    break
            if len(sections) >= schema.MAX_SECTIONS:
                break

        if meta.get("params"):
            warnings.append("已识别到运行参数（采样器/尺寸等），不会进入精品词库")
        if meta.get("workflow_json"):
            warnings.append("已从内嵌工作流恢复提示词，结果属于原始数据恢复而非模型推断")
        if not sections:
            warnings.append("元数据中未拆出可独立复用的片段，可直接使用恢复的原始提示词")

        raw = {
            "source_type": schema.SOURCE_METADATA,
            "source_label": schema.SOURCE_LABELS[schema.SOURCE_METADATA],
            "provider": self.key,
            "provider_model": self.model,
            "model_profile": profile_or_default(profile),
            "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
            "recovered_original_prompt": positive,
            "recovered_negative_prompt": negative,
            "sections": sections,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "quality_tier": schema.QUALITY_ORIGINAL,
            "image_ref": {
                "image_id": (image_ref or {}).get("image_id"),
                "upload_id": (image_ref or {}).get("upload_id"),
                "content_hash": (image_ref or {}).get("content_hash") or "",
            },
        }
        return schema.validate_structured_result(raw)


__all__ = ["MetadataProvider", "METADATA_NOT_FOUND", "no_metadata_error"]
