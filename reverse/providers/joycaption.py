"""M11.1 / M11.3 JoyCaption Beta One GGUF Provider（生图提示词专项）。

与 Qwen3-VL 的差异：
- 使用 ``Stable Diffusion Prompt`` / ``Descriptive`` 提示词模式，产出可直接喂给
  SDXL / FLUX 的扩散模型描述；
- 保留 ``raw_caption`` 英文原文，再由 AIBAR 用 ``classify_dimension`` + ``split_segments``
  归类；**原始整段文本绝不作为单条词条入库**；
- 与 Qwen3-VL 共用同一个Metal 运行时与全局高级 VLM 单任务队列。
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
from . import base
from .base import (
    DEFAULT_PRECISION,
    STATUS_INCOMPATIBLE_RUNTIME,
    STATUS_MISSING_MMPROJ,
    STATUS_MISSING_MODEL,
    STATUS_MISSING_NODE,
    STATUS_OFFLINE,
    STATUS_READY,
    VisionProvider,
    build_inputs,
    enum_options,
    find_input_of_type,
    graph,
    link,
    new_client_id,
    node_input_spec,
    node_output_names,
    node_output_types,
    probe_runtime,
    profile_or_default,
    skipped_layer,
    status_payload,
    vlm_slot,
)
from .qwen3vl import LOAD_IMAGE_CLASS, PREVIEW_CLASS

JOY_INCLUDE = ("joycaption",)
JOY_EXCLUDE = ("clip", "textencode", "loader", "lora", "controlnet")
JOY_CANDIDATES = (
    "JoyCaptionGGUF",
    "JoyCaptionBetaGGUF",
    "JoyCaptionBetaOneGGUF",
    "JoyCaption",
    "JoyCaptionTwoGGUF",
)

# 提示词模式：优先“Stable Diffusion Prompt”，其次“Descriptive”（PRD M11.1）
CAPTION_MODES = ("Stable Diffusion Prompt", "Descriptive", "Training Prompt")
CAPTION_LENGTHS = {"fast": "short", "standard": "medium", "fine": "long"}

PRECISION_PARAMS: dict[str, dict[str, Any]] = {
    "fast": {"max_tokens": 256, "temperature": 0.4, "top_p": 0.8},
    "standard": {"max_tokens": 512, "temperature": 0.6, "top_p": 0.9},
    "fine": {"max_tokens": 1024, "temperature": 0.7, "top_p": 0.95},
}

# JoyCaption 是生图提示词专项模型，置信度上限高于 BLIP，但仍不是原始事实
JOYCAPTION_MAX_CONFIDENCE = 0.90
WEAK_CLASSIFY_CONFIDENCE = 0.55


class JoyCaptionProvider(VisionProvider):
    """JoyCaption Beta One GGUF 生图提示词专项反推。"""

    key = "comfyui_joycaption_beta"
    label = "JoyCaption Beta One · 提示词专项"
    description = "本机 GGUF + Metal，输出可直接用于 SDXL / FLUX 的扩散模型英文描述"
    local = True
    requires_consent = False
    advanced_vlm = True
    quality_tier = schema.QUALITY_ADVANCED
    model = "llama-joycaption-beta-one-hf-llava"
    quantization = "Q5_K_M"
    precision_map = PRECISION_PARAMS
    capabilities = {"precision": ["fast", "standard", "fine"], "vision": True, "diffusion_prompt": True}

    # ---------------------------------------------------------------- 发现与资源

    def _expected_model_file(self) -> str:
        return str(base.manifest_entry(self.key).get("model_filename") or "")

    def _expected_mmproj_file(self) -> str:
        return str(base.manifest_entry(self.key).get("mmproj_filename") or "")

    def _models_root(self) -> Path | None:
        if Config.COMFYUI_MODELS_DIR:
            return Path(Config.COMFYUI_MODELS_DIR)
        if Config.COMFYUI_DIR:
            return Path(Config.COMFYUI_DIR) / "models"
        return None

    def _discover_node(self) -> tuple[str | None, dict | None]:
        contract = base.manifest_entry(self.key).get("node_contract")
        contract = contract if isinstance(contract, dict) else {}
        preferred: list[str] = []
        for value in (
            contract.get("node_class"),
            *(contract.get("node_class_candidates") or []),
            *JOY_CANDIDATES,
        ):
            text = str(value or "").strip()
            if text and text not in preferred:
                preferred.append(text)

        all_info = comfyui_client.object_info(None) or {}
        for name in preferred:
            info = all_info.get(name)
            if isinstance(info, dict) and find_input_of_type(info, "IMAGE") is not None:
                return name, info
        found = base.discover_node_class(
            all_info, include=JOY_INCLUDE, exclude=JOY_EXCLUDE, require_image_input=True
        )
        if found:
            return found, all_info.get(found)
        return None, None

    def _record_contract(self, node_class: str, info: dict) -> None:
        spec = node_input_spec(info)
        inputs: dict[str, Any] = {}
        for name, entry in spec.items():
            options = enum_options(entry)
            if options:
                inputs[name] = {"enum": [str(o) for o in options][:40]}
            elif isinstance(entry, list) and entry and isinstance(entry[0], str):
                inputs[name] = {"type": entry[0]}
        try:
            base.save_node_contract(
                self.key,
                {
                    "node_class": node_class,
                    "inputs": inputs,
                    "outputs": node_output_names(info),
                    "verified_at": base.db_now(),
                },
            )
        except Exception:
            pass

    def _model_input(self, info: dict) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name in ("model", "gguf", "model_file", "llm_model", "model_name", "model_path"):
            if name in spec and enum_options(spec[name]):
                return name, spec[name]
        for name, entry in spec.items():
            if "model" in name.lower() and "mmproj" not in name.lower() and enum_options(entry):
                return name, entry
        return None, None

    def _mmproj_input(self, info: dict) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name, entry in spec.items():
            if "mmproj" in name.lower() and enum_options(entry):
                return name, entry
        return None, None

    def _caption_type_input(self, info: dict) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name, entry in spec.items():
            lowered = name.lower()
            if ("caption_type" in lowered or "prompt_type" in lowered or "mode" in lowered) and enum_options(entry):
                return name, entry
        return None, None

    def _file_present(self, filename: str) -> tuple[bool, str]:
        if not filename:
            return True, "清单未指定文件，跳过检查"
        root = self._models_root()
        if root is None or not root.is_dir():
            return True, "未配置 ComfyUI 目录，跳过文件检查"
        patterns = (f"LLM/GGUF/{filename}", f"*/{filename}", f"**/{filename}")
        try:
            for pattern in patterns:
                for candidate in root.glob(pattern):
                    if candidate.is_file():
                        return True, candidate.name
        except OSError:
            return True, "模型目录不可读，跳过检查"
        return False, filename

    def _check_assets(self, info: dict) -> tuple[str, str, str]:
        model_key, model_entry = self._model_input(info)
        expected_model = self._expected_model_file()
        if model_key and enum_options(model_entry):
            if base.find_option(model_entry, expected_model, "joycaption", "Q5_K_M") is None:
                return STATUS_MISSING_MODEL, "missing_model", f"节点未列出 {expected_model or self.model}"
        elif expected_model:
            ok, detail = self._file_present(expected_model)
            if not ok:
                return STATUS_MISSING_MODEL, "missing_model", f"未找到模型文件 {detail}"

        mmproj_key, mmproj_entry = self._mmproj_input(info)
        expected_mmproj = self._expected_mmproj_file()
        if mmproj_key and enum_options(mmproj_entry):
            if expected_mmproj and base.find_option(mmproj_entry, expected_mmproj, "mmproj") is None:
                return STATUS_MISSING_MMPROJ, "missing_mmproj", f"节点未列出 {expected_mmproj}"
        elif expected_mmproj:
            ok, detail = self._file_present(expected_mmproj)
            if not ok:
                return STATUS_MISSING_MMPROJ, "missing_mmproj", f"未找到视觉投影文件 {detail}"
        return STATUS_READY, "", ""

    # ---------------------------------------------------------------- 健康探测

    def probe(self) -> dict[str, Any]:
        reachable, latency = comfyui_client.is_reachable()
        if not reachable:
            return status_payload(STATUS_OFFLINE, "comfyui_offline", latency_ms=latency, model=self.model)
        node_class, node_info = self._discover_node()
        if not node_class or not node_info:
            return status_payload(
                STATUS_MISSING_NODE,
                "missing_node",
                "ComfyUI 中未发现 JoyCaption GGUF 自定义节点",
                model=self.model,
                latency_ms=latency,
            )
        self._record_contract(node_class, node_info)
        status, reason_code, detail = self._check_assets(node_info)
        if status != STATUS_READY:
            return status_payload(status, reason_code, detail, model=self.model, latency_ms=latency)
        ok, detail = probe_runtime(Config.COMFYUI_PYTHON)
        if not ok:
            return status_payload(
                STATUS_INCOMPATIBLE_RUNTIME,
                "incompatible_runtime",
                detail,
                model=self.model,
                latency_ms=latency,
            )
        return status_payload(STATUS_READY, model=self.model, latency_ms=latency)

    def test_layers(self) -> list[dict[str, Any]]:
        layers: list[dict[str, Any]] = []
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

        model_started = time.perf_counter()
        node_class, node_info = self._discover_node()
        if node_class is None or node_info is None:
            layers.append(
                {
                    "name": "model",
                    "status": STATUS_MISSING_NODE,
                    "available": False,
                    "latency_ms": int((time.perf_counter() - model_started) * 1000),
                    "reason_code": "missing_node",
                }
            )
            layers.append(skipped_layer("inference"))
            return layers
        self._record_contract(node_class, node_info)
        status, reason_code, detail = self._check_assets(node_info)
        if status != STATUS_READY:
            layers.append(
                {
                    "name": "model",
                    "status": status,
                    "available": False,
                    "latency_ms": int((time.perf_counter() - model_started) * 1000),
                    "reason_code": reason_code,
                }
            )
            layers.append(skipped_layer("inference"))
            return layers
        # “最小视觉请求”只在用户显式点击测试连接时执行，避免列表刷新触发真实推理
        layers.append(
            {
                "name": "model",
                "status": STATUS_READY,
                "available": True,
                "latency_ms": int((time.perf_counter() - model_started) * 1000),
                "reason_code": "",
            }
        )
        layers.append(_skipped_layer("inference"))
        return layers

    # ---------------------------------------------------------------- 分析

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        options = options or {}
        path = (image_ref or {}).get("path")
        if not path:
            raise AIBARError("invalid_input", "缺少可读的图片副本")

        node_class, node_info = self._discover_node()
        if not node_class or not node_info:
            raise ComfyUIError("missing_node", "ComfyUI 中未发现 JoyCaption GGUF 节点")
        self._record_contract(node_class, node_info)

        preview_info = comfyui_client.object_info(PREVIEW_CLASS)
        if not preview_info:
            raise ComfyUIError("missing_node", f"ComfyUI 缺少 {PREVIEW_CLASS} 节点")

        suffix = Path(str(path)).suffix.lower() or ".png"
        controlled = f"aibar_reverse_{(image_ref.get('content_hash') or uuid.uuid4().hex)[:24]}{suffix}"

        workflow = self._build_workflow(
            node_class, node_info, preview_info, controlled, precision
        )
        # 全局单任务队列：绝不与其他高级 VLM 并行加载大模型
        with vlm_slot(self.key, job=options.get("job_id")):
            comfyui_client.upload_image(str(path), controlled, overwrite=True)
            prompt_id = comfyui_client.submit_prompt(workflow, new_client_id())
            entry = comfyui_client.wait_history(
                prompt_id,
                timeout=self._timeout(options),
                cancel=self._cancel_flag(options),
            )

        raw_caption = self._pick_text(entry, controlled)
        if not raw_caption:
            raise ComfyUIError("workflow_failed", "JoyCaption 未返回可用描述")

        sections = self._build_sections(raw_caption)
        warnings = [
            "JoyCaption 输出为英文扩散模型描述，已按维度拆分；整段原文不会作为单条词条入库",
            "结果属于视觉模型推断，不保证等于原始提示词",
        ]
        if not sections:
            warnings.append("未能从描述中拆出可独立复用的片段，可手动编辑后保存")

        raw = {
            "source_type": schema.SOURCE_VISION,
            "source_label": schema.SOURCE_LABELS[schema.SOURCE_VISION],
            "provider": self.key,
            "provider_model": f"{self.model} · {self.quantization}",
            "model_profile": profile_or_default(profile),
            "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
            "recovered_original_prompt": "",
            "recovered_negative_prompt": "",
            "sections": sections,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "quality_tier": schema.QUALITY_ADVANCED,
            "raw_caption": raw_caption,
            "provider_status_snapshot": {
                "status": STATUS_READY,
                "reason_code": "",
                "model": self.model,
                "queue": base.vlm_queue_state(),
            },
            "image_ref": {
                "image_id": (image_ref or {}).get("image_id"),
                "upload_id": (image_ref or {}).get("upload_id"),
                "content_hash": (image_ref or {}).get("content_hash") or "",
            },
        }
        return schema.validate_structured_result(raw)

    def _build_workflow(
        self,
        node_class: str,
        node_info: dict,
        preview_info: dict,
        controlled: str,
        precision: str,
    ) -> dict[str, dict[str, Any]]:
        params = self.map_precision(precision)
        load_info = comfyui_client.object_info(LOAD_IMAGE_CLASS)
        load_inputs = build_inputs(load_info, {"image": controlled}) or {"image": controlled}

        desired: dict[str, Any] = {
            "image": link("1", 0),
            "images": link("1", 0),
            "max_tokens": params.get("max_tokens", 512),
            "max_new_tokens": params.get("max_tokens", 512),
            "temperature": params.get("temperature", 0.6),
            "top_p": params.get("top_p", 0.9),
            "seed": 0,
            "unload_after": True,
            "unload_model": True,
            "keep_model_loaded": False,
            "keep_model_in_memory": False,
        }
        caption_key, caption_entry = self._caption_type_input(node_info)
        if caption_key:
            desired[caption_key] = CAPTION_MODES
        length_key, length_entry = self._caption_length_input(node_info)
        if length_key:
            desired[length_key] = CAPTION_LENGTHS.get(
                precision if precision in CAPTION_LENGTHS else "standard"
            )
        model_key, model_entry = self._model_input(node_info)
        if model_key:
            choice = base.find_option(model_entry, self._expected_model_file(), "joycaption", "Q5_K_M")
            if choice is not None:
                desired[model_key] = choice
        mmproj_key, mmproj_entry = self._mmproj_input(node_info)
        if mmproj_key:
            choice = base.find_option(mmproj_entry, self._expected_mmproj_file(), "mmproj")
            if choice is not None:
                desired[mmproj_key] = choice

        node_inputs = build_inputs(node_info, desired)
        if "image" not in node_inputs:
            raise ComfyUIError("missing_node", f"{node_class} 缺少图像输入")

        out_index = self._text_output_index(node_info)
        preview_inputs = build_inputs(
            preview_info,
            {
                "source": link("2", out_index),
                "text": link("2", out_index),
                "any": link("2", out_index),
            },
        ) or {"source": link("2", out_index)}
        return graph(
            ("1", LOAD_IMAGE_CLASS, load_inputs),
            ("2", node_class, node_inputs),
            ("3", PREVIEW_CLASS, preview_inputs),
        )

    def _caption_length_input(self, info: dict) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name, entry in spec.items():
            if "length" in name.lower() and enum_options(entry):
                return name, entry
        return None, None

    def _text_output_index(self, info: dict | None) -> int:
        for index, name in enumerate(node_output_types(info)):
            if str(name).upper() == "STRING":
                return index
        return 0

    def _pick_text(self, entry: dict | None, controlled_name: str) -> str:
        if not entry:
            return ""
        candidates = comfyui_client.collect_text_outputs(entry, node_id="3")
        if not candidates:
            candidates = comfyui_client.collect_text_outputs(entry)
        best = ""
        for _node, key, text in candidates:
            cleaned = schema.clean_text(text, schema.MAX_PROMPT_TEXT).strip()
            if not cleaned or controlled_name in cleaned or cleaned.endswith(".png"):
                continue
            if key.lower().endswith("filename"):
                continue
            if len(cleaned) > len(best):
                best = cleaned
        return best

    def _build_sections(self, caption: str) -> list[dict[str, Any]]:
        """把英文 caption 拆成可独立复用的片段并归类（整段原文不入库）。"""
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
            confidence = min(float(classify_conf), JOYCAPTION_MAX_CONFIDENCE)
            sections.append(
                schema.make_section(
                    len(sections),
                    dimension,
                    segment,
                    schema.cap_confidence(confidence, schema.SOURCE_VISION),
                    uncertainty=""
                    if classify_conf >= WEAK_CLASSIFY_CONFIDENCE
                    else "维度由关键词推断，可手动调整",
                    evidence="来自 JoyCaption 英文描述拆分",
                )
            )
            if len(sections) >= schema.MAX_SECTIONS:
                break
        return sections


__all__ = ["JOYCAPTION_MAX_CONFIDENCE", "CAPTION_MODES", "JoyCaptionProvider", "PRECISION_PARAMS"]
