"""M10.2 内置 Provider：零配置自动发现并使用 ComfyUI 本地 BLIP。

零配置检测三步（PRD M10.2）：
1. ``COMFYUI_HOST:PORT`` 可达；
2. ``/object_info/ImagePromptInterrogator`` 存在且输入/输出契约可用；
3. ``/object_info/PreviewAny`` 存在，可在任务历史中取回文本输出。

约束：
- 只构造最小工作流 ``LoadImage → ImagePromptInterrogator → PreviewAny``；
- **禁止**把用户提交的任意本机路径传给 ComfyUI，只能传上传到 ComfyUI input 的受控文件名；
- BLIP 片段置信度上限 ``0.80``、``quality_tier=basic``，默认进候选而不直接入库；
- 失败必须区分 ``offline / missing_node / missing_model / workflow_failed / timeout``。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from config import Config
from core.errors import AIBARError
from core.textutil import classify_dimension, has_content_value, normalize_text, split_segments

from .. import comfyui_client, schema
from ..comfyui_client import ComfyUIError
from .base import (
    DEFAULT_PRECISION,
    STATUS_MISSING_MODEL,
    STATUS_MISSING_NODE,
    STATUS_OFFLINE,
    STATUS_READY,
    VisionProvider,
    build_inputs,
    graph,
    link,
    new_client_id,
    node_input_spec,
    node_output_types,
    profile_or_default,
    skipped_layer,
    status_payload,
)

INTERROGATOR_CLASS = "ImagePromptInterrogator"
PREVIEW_CLASS = "PreviewAny"
LOAD_IMAGE_CLASS = "LoadImage"

# 精度 -> 可测试的固定参数（PRD M10.2）
PRECISION_PARAMS: dict[str, dict[str, Any]] = {
    "fast": {"mode": "Natural", "max_tokens": 40, "num_beams": 1},
    "standard": {"mode": "Detailed", "max_tokens": 80, "num_beams": 3},
    "fine": {"mode": "Detailed", "max_tokens": 140, "num_beams": 5},
}
UNLOAD_AFTER = "Enable"

# BLIP 是基础 caption 模型：置信度上限 0.80，默认只进候选（PRD M10.2）
BLIP_MAX_CONFIDENCE = 0.80
MODEL_NAME_PATTERNS = ("*blip*", "*/*blip*", "*/*/*blip*")
WEAK_CLASSIFY_CONFIDENCE = 0.55


class ComfyUIBlipProvider(VisionProvider):
    """ComfyUI 本地 BLIP 基础视觉反推。"""

    key = "comfyui_blip"
    label = "ComfyUI 本地 BLIP"
    description = "复用本机 ComfyUI 的 BLIP 看图节点，输出基础内容描述，零配置、不外发"
    local = True
    quality_tier = schema.QUALITY_BASIC
    model = "blip-image-captioning-base"
    requires_consent = False
    capabilities = {"precision": ["fast", "standard", "fine"], "vision": True}
    precision_map = PRECISION_PARAMS

    # ---------------------------------------------------------------- 健康探测

    def probe(self) -> dict[str, Any]:
        reachable, latency = comfyui_client.is_reachable()
        if not reachable:
            return status_payload(STATUS_OFFLINE, "comfyui_offline", latency_ms=latency, model=self.model)

        info = comfyui_client.object_info(INTERROGATOR_CLASS)
        if not info:
            return status_payload(
                STATUS_MISSING_NODE,
                "missing_node",
                f"ComfyUI 未安装 {INTERROGATOR_CLASS} 节点",
                model=self.model,
                latency_ms=latency,
            )
        usable, detail = self._contract_usable(info)
        if not usable:
            return status_payload(
                STATUS_MISSING_NODE,
                "missing_node",
                f"{INTERROGATOR_CLASS} 节点契约不可用：{detail}",
                model=self.model,
                latency_ms=latency,
            )
        if not comfyui_client.object_info(PREVIEW_CLASS):
            return status_payload(
                STATUS_MISSING_NODE,
                "missing_node",
                f"ComfyUI 未安装 {PREVIEW_CLASS} 节点，无法取回文本输出",
                model=self.model,
                latency_ms=latency,
            )
        present, detail = self._model_present()
        if not present:
            return status_payload(
                STATUS_MISSING_MODEL,
                "missing_model",
                f"未找到 BLIP 模型文件：{detail}",
                model=self.model,
                latency_ms=latency,
            )
        return status_payload(STATUS_READY, model=self.model, latency_ms=latency)

    def test_layers(self) -> list[dict[str, Any]]:
        """分层测试：服务可达 / 模型或节点可用 / 最小视觉请求（不自动跑真实分析）。"""
        layers: list[dict[str, Any]] = []
        started = time.perf_counter()
        reachable, latency = comfyui_client.is_reachable()
        layers.append(
            {
                "name": "service",
                "status": STATUS_READY if reachable else STATUS_OFFLINE,
                "available": reachable,
                "latency_ms": latency,
                "reason_code": "" if reachable else "comfyui_offline",
            }
        )
        if not reachable:
            layers.append(skipped_layer("model"))
            layers.append(skipped_layer("inference"))
            return layers

        started = time.perf_counter()
        info = comfyui_client.object_info(INTERROGATOR_CLASS)
        usable, _ = self._contract_usable(info) if info else (False, "")
        preview_ok = bool(comfyui_client.object_info(PREVIEW_CLASS))
        present, _ = self._model_present()
        model_ok = bool(info) and usable and preview_ok and present
        layers.append(
            {
                "name": "model",
                "status": STATUS_READY if model_ok else STATUS_MISSING_NODE,
                "available": model_ok,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "reason_code": "" if model_ok else ("missing_model" if info and usable and preview_ok and not present else "missing_node"),
            }
        )
        # 最小视觉请求需要真实图片，测试接口不自动发起；由前端显式触发
        layers.append(skipped_layer("inference"))
        return layers

    # ---------------------------------------------------------------- 契约与模型

    def _contract_usable(self, info: dict | None) -> tuple[bool, str]:
        """节点契约是否可用：必须有图像输入、可调参数与文本输出。"""
        if not info:
            return False, "缺少节点信息"
        spec = node_input_spec(info)
        if not spec:
            return False, "缺少输入定义"
        has_image = any(
            isinstance(entry, list) and entry and entry[0] == "IMAGE" for entry in spec.values()
        )
        if not has_image:
            return False, "缺少图像输入"
        outputs = node_output_types(info)
        if outputs and not any(str(o).upper() == "STRING" for o in outputs):
            return False, "缺少文本输出"
        return True, ""

    def _models_root(self) -> Path | None:
        if Config.COMFYUI_MODELS_DIR:
            return Path(Config.COMFYUI_MODELS_DIR)
        if Config.COMFYUI_DIR:
            return Path(Config.COMFYUI_DIR) / "models"
        return None

    def _model_present(self) -> tuple[bool, str]:
        """尽力检查 BLIP 模型是否存在；未配置 ComfyUI 目录时不做断言。

        只回传**相对/文件级**信息，绝不把绝对路径写进状态或日志。
        """
        root = self._models_root()
        if root is None or not root.is_dir():
            return True, "未配置 ComfyUI 目录，跳过模型文件检查"
        try:
            for pattern in MODEL_NAME_PATTERNS:
                for candidate in root.glob(pattern):
                    if candidate.name.startswith("."):
                        continue
                    return True, candidate.name
        except OSError:
            return True, "模型目录不可读，跳过检查"
        return False, "blip*"

    # ---------------------------------------------------------------- 分析

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
            raise AIBARError("invalid_input", "缺少可读的图片副本")

        interrogator_info = comfyui_client.object_info(INTERROGATOR_CLASS)
        if not interrogator_info:
            raise ComfyUIError("missing_node", f"ComfyUI 缺少 {INTERROGATOR_CLASS} 节点")
        preview_info = comfyui_client.object_info(PREVIEW_CLASS)
        if not preview_info:
            raise ComfyUIError("missing_node", f"ComfyUI 缺少 {PREVIEW_CLASS} 节点")

        # 受控文件名：内容哈希 + 真实后缀，绝不传递用户原始路径
        suffix = Path(str(path)).suffix.lower() or ".png"
        controlled_name = f"aibar_reverse_{(image_ref.get('content_hash') or uuid.uuid4().hex)[:24]}{suffix}"
        comfyui_client.upload_image(str(path), controlled_name, overwrite=True)

        params = self.map_precision(precision)
        load_info = comfyui_client.object_info(LOAD_IMAGE_CLASS)
        load_inputs = build_inputs(load_info, {"image": controlled_name}) or {"image": controlled_name}

        text_output_index = self._text_output_index(interrogator_info)
        interrogator_inputs = build_inputs(
            interrogator_info,
            {
                "image": link("1", 0),
                "mode": params.get("mode", "Detailed"),
                "max_tokens": params.get("max_tokens", 80),
                "max_new_tokens": params.get("max_tokens", 80),
                "num_beams": params.get("num_beams", 3),
                "min_length": 1,
                "unload_after": UNLOAD_AFTER,
                "unload_model": True,
                "keep_model_loaded": False,
            },
        )
        if "image" not in interrogator_inputs:
            raise ComfyUIError("missing_node", f"{INTERROGATOR_CLASS} 缺少图像输入")

        preview_inputs = build_inputs(
            preview_info,
            {
                "source": link("2", text_output_index),
                "text": link("2", text_output_index),
                "any": link("2", text_output_index),
            },
        )
        if not preview_inputs:
            preview_inputs = {"source": link("2", text_output_index)}

        workflow = graph(
            ("1", LOAD_IMAGE_CLASS, load_inputs),
            ("2", INTERROGATOR_CLASS, interrogator_inputs),
            ("3", PREVIEW_CLASS, preview_inputs),
        )

        client_id = new_client_id()
        prompt_id = comfyui_client.submit_prompt(workflow, client_id)
        entry = comfyui_client.wait_history(
            prompt_id,
            timeout=self._timeout(options),
            cancel=self._cancel_flag(options),
        )
        raw_caption = self._pick_caption(entry, controlled_name)
        if not raw_caption:
            raise ComfyUIError("workflow_failed", "BLIP 未返回可用描述")

        sections = self._build_sections(raw_caption)
        warnings = [
            "BLIP 为基础视觉识别，只描述可见内容，不推断艺术家、品牌或真实身份",
            "片段置信度上限 0.80，默认进入候选审核，确认后才会进入正式词库",
        ]
        if not sections:
            warnings.append("未能从描述中拆出可独立复用的片段，可手动编辑后保存")

        raw = {
            "source_type": schema.SOURCE_VISION,
            "source_label": schema.SOURCE_LABELS[schema.SOURCE_VISION],
            "provider": self.key,
            "provider_model": self.model,
            "model_profile": profile_or_default(profile),
            "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
            "recovered_original_prompt": "",
            "recovered_negative_prompt": "",
            "sections": sections,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "quality_tier": schema.QUALITY_BASIC,
            "raw_caption": raw_caption,
            "image_ref": {
                "image_id": (image_ref or {}).get("image_id"),
                "upload_id": (image_ref or {}).get("upload_id"),
                "content_hash": (image_ref or {}).get("content_hash") or "",
            },
        }
        return schema.validate_structured_result(raw)

    def _text_output_index(self, info: dict | None) -> int:
        outputs = node_output_types(info)
        for index, name in enumerate(outputs):
            if str(name).upper() == "STRING":
                return index
        return 0

    def _pick_caption(self, entry: dict | None, controlled_name: str) -> str:
        """从历史输出中挑选 BLIP 描述文本（不猜测内容，只挑选最长且非文件名的文本）。"""
        if not entry:
            return ""
        candidates = comfyui_client.collect_text_outputs(entry, node_id="3")
        if not candidates:
            candidates = comfyui_client.collect_text_outputs(entry)
        best = ""
        for _node, key, text in candidates:
            cleaned = schema.clean_text(text, schema.MAX_PROMPT_TEXT).strip()
            if not cleaned or controlled_name in cleaned:
                continue
            if key.lower().endswith("filename") or cleaned.endswith(".png"):
                continue
            if len(cleaned) > len(best):
                best = cleaned
        return best

    def _build_sections(self, caption: str) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        seen: set[str] = set()
        for segment in split_segments(caption):
            if not has_content_value(segment):
                continue
            key = normalize_text(segment)
            if not key or key in seen:
                continue
            seen.add(key)
            dimension, classify_conf = classify_dimension(segment, schema.MEDIA_TYPE)
            if not schema.coerce_dimension(dimension):
                continue
            confidence = min(float(classify_conf), BLIP_MAX_CONFIDENCE)
            sections.append(
                schema.make_section(
                    len(sections),
                    dimension,
                    segment,
                    schema.cap_confidence(confidence, schema.SOURCE_VISION),
                    uncertainty=""
                    if classify_conf >= WEAK_CLASSIFY_CONFIDENCE
                    else "基础识别结果，维度由关键词推断",
                    evidence="来自 ComfyUI BLIP 可见内容描述",
                )
            )
            if len(sections) >= schema.MAX_SECTIONS:
                break
        return sections


__all__ = [
    "BLIP_MAX_CONFIDENCE",
    "ComfyUIBlipProvider",
    "INTERROGATOR_CLASS",
    "PRECISION_PARAMS",
    "PREVIEW_CLASS",
]
