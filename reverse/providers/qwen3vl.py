"""M11.1 / M11.3 Qwen3-VL 8B / 4B GGUF 视觉 Provider（Apple Silicon · Metal）。

设计要点：
- 节点 class name 与输入枚举**通过 ``/object_info`` 动态发现**，实测契约写回
  ``resources/provider_manifest.json`` 的 ``node_contract``，manifest 只作兜底；
- 统一结构化指令直接要求模型输出 M9.4 的 JSON Schema；
- 适配器做**宽容 JSON 提取 + 严格 Schema 校验**：只允许类型修复、枚举映射与长度裁剪，
  绝不伪造缺失的视觉细节；
- 健康检查链：ComfyUI 可达 → 节点存在 → 主模型完整 → mmproj 完整 → Metal handler 可用；
- 全局同时最多 1 个高级 VLM 任务（``base.vlm_slot``），默认每次分析后卸载模型。
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from config import Config
from core.errors import AIBARError

from .. import comfyui_client, schema
from ..comfyui_client import ComfyUIError
from . import base
from .base import (
    DEFAULT_PRECISION,
    STATUS_ERROR,
    STATUS_INCOMPATIBLE_RUNTIME,
    STATUS_MISSING_MMPROJ,
    STATUS_MISSING_MODEL,
    STATUS_MISSING_NODE,
    STATUS_OFFLINE,
    STATUS_READY,
    VisionProvider,
    build_inputs,
    classify_error,
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
    status_payload,
    vlm_slot,
)

LOAD_IMAGE_CLASS = "LoadImage"
PREVIEW_CLASS = "PreviewAny"

# 发现用关键字：不硬编码节点名，只约束命名特征
QWEN_INCLUDE = ("qwen", "vl")
QWEN_EXCLUDE = ("clip", "textencode", "loader", "lora", "controlnet", "upscale")
QWEN_CANDIDATES = (
    "AILab_QwenVL_GGUF",
    "QwenVLGGUF",
    "Qwen3VLGGUF",
    "Qwen2VLGGUF",
    "Qwen3-VLGGUF",
    "QwenVL_GGUF",
    "QwenVL",
)

PRECISION_PARAMS: dict[str, dict[str, Any]] = {
    "fast": {"max_tokens": 640, "temperature": 0.2, "top_p": 0.8},
    "standard": {"max_tokens": 1024, "temperature": 0.3, "top_p": 0.9},
    "fine": {"max_tokens": 2048, "temperature": 0.4, "top_p": 0.95},
}

SYSTEM_PROMPT = (
    "你是严谨的图片提示词反推助手，只输出 JSON，不输出解释、注释或 Markdown 代码块。"
)

NEGATIVE_CONFIDENCE = 0.65

_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*")


def build_instruction() -> str:
    """统一结构化指令：直接要求输出 M9.4 的 JSON Schema。"""
    dimensions = " / ".join(schema.dimension_label_of(k) for k in schema.image_dimension_keys())
    return (
        "请只依据画面中确实可见的内容，输出如下 JSON：\n"
        '{"sections":[{"dimension":"主体与外观","text":"简洁中文描述","confidence":0.9,'
        '"uncertainty":""}],"negative":"画面中明确不应出现的元素","warnings":[]}\n'
        f"dimension 只能取：{dimensions}\n"
        "要求：\n"
        "1. 只输出有可靠依据的维度，没有可靠内容的维度不要输出；\n"
        "2. text 必须是可独立复用的短描述，不超过 60 字，不要写“这张图展示了”；\n"
        "3. confidence 取值 0 到 1，表示你对这条描述的把握；\n"
        "4. 严禁推测艺术家、品牌、真实人物身份、生成模型或运行参数；"
        "不确定时降低 confidence 并在 uncertainty 中说明；\n"
        "5. negative 只填写画面中明确要排除的元素，没有就留空。"
    )


def extract_json_object(text: str) -> dict | None:
    """宽容 JSON 提取：容忍 Markdown 围栏、前后废话与尾随逗号。"""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text).strip()
    for candidate in (cleaned, _slice_object(cleaned)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _slice_object(text: str) -> str:
    """截取第一个完整的顶层 JSON 对象（正确跳过字符串内的花括号）。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


class Qwen3VLProvider(VisionProvider):
    """Qwen3-VL GGUF 结构化反推（8B / 4B 共用实现）。"""

    advanced_vlm = True
    local = True
    requires_consent = False
    quality_tier = schema.QUALITY_ADVANCED

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        model: str,
        quantization: str = "Q4_K_M",
        match_keys: tuple[str, ...] = (),
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.model = model
        self.quantization = quantization
        self.match_keys = tuple(match_keys)
        self.precision_map = PRECISION_PARAMS
        self.capabilities = {
            "precision": ["fast", "standard", "fine"],
            "vision": True,
            "structured_output": True,
        }

    # ---------------------------------------------------------------- 清单

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

    # ---------------------------------------------------------------- 节点发现

    def _discover_node(self) -> tuple[str | None, dict | None]:
        """先按清单兜底名与候选名精确匹配，再在全部节点中按关键字发现。"""
        contract = base.manifest_entry(self.key).get("node_contract")
        contract = contract if isinstance(contract, dict) else {}
        preferred: list[str] = []
        for value in (
            contract.get("node_class"),
            *(contract.get("node_class_candidates") or []),
            *QWEN_CANDIDATES,
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
            all_info, include=QWEN_INCLUDE, exclude=QWEN_EXCLUDE, require_image_input=True
        )
        if found:
            return found, all_info.get(found)
        return None, None

    def _record_contract(self, node_class: str, info: dict) -> None:
        """把实测契约写回清单（内容无变化时不落盘）。"""
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

    # ---------------------------------------------------------------- 资源检查

    def _enum_input(self, info: dict, names: tuple[str, ...]) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name in names:
            if name in spec and enum_options(spec[name]):
                return name, spec[name]
        for name, entry in spec.items():
            if "model" in name.lower() and enum_options(entry):
                return name, entry
        return None, None

    def _model_input(self, info: dict) -> tuple[str | None, Any | None]:
        return self._enum_input(
            info, ("model", "gguf", "model_file", "llm_model", "model_name", "model_path", "unet_name")
        )

    def _mmproj_input(self, info: dict) -> tuple[str | None, Any | None]:
        spec = node_input_spec(info)
        for name, entry in spec.items():
            if "mmproj" in name.lower() and enum_options(entry):
                return name, entry
        return None, None

    def _file_present(self, filename: str) -> tuple[bool, str]:
        """按清单文件名检查模型文件是否完整（只回传文件名，不回传绝对路径）。"""
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
        """主模型 / mmproj 完整性检查，返回 ``(status, reason_code, detail)``。"""
        model_key, model_entry = self._model_input(info)
        expected_model = self._expected_model_file()
        if model_key and enum_options(model_entry):
            choice = base.find_option(model_entry, expected_model, *self.match_keys)
            if choice is None:
                return STATUS_MISSING_MODEL, "missing_model", f"节点未列出 {expected_model or self.model}"
        else:
            ok, detail = self._file_present(expected_model or self._fallback_model_file())
            if not ok:
                return STATUS_MISSING_MODEL, "missing_model", f"未找到模型文件 {detail}"

        mmproj_key, mmproj_entry = self._mmproj_input(info)
        expected_mmproj = self._expected_mmproj_file()
        if mmproj_key and enum_options(mmproj_entry):
            if expected_mmproj and base.find_option(mmproj_entry, expected_mmproj) is None:
                return STATUS_MISSING_MMPROJ, "missing_mmproj", f"节点未列出 {expected_mmproj}"
        elif expected_mmproj:
            ok, detail = self._file_present(expected_mmproj)
            if not ok:
                return STATUS_MISSING_MMPROJ, "missing_mmproj", f"未找到视觉投影文件 {detail}"
        return STATUS_READY, "", ""

    def _fallback_model_file(self) -> str:
        return f"{self.model}.gguf"

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
                "ComfyUI 中未发现 Qwen3-VL GGUF 自定义节点",
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
            layers.append(base.skipped_layer("model"))
            layers.append(base.skipped_layer("inference"))
            return layers

        model_started = time.perf_counter()
        node_class, node_info = self._discover_node()
        if node_class is None:
            layers.append(
                {
                    "name": "model",
                    "status": STATUS_MISSING_NODE,
                    "available": False,
                    "latency_ms": int((time.perf_counter() - model_started) * 1000),
                    "reason_code": "missing_node",
                }
            )
            layers.append(base.skipped_layer("inference"))
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
            layers.append(base.skipped_layer("inference"))
            return layers
        runtime_ok, runtime_detail = probe_runtime(Config.COMFYUI_PYTHON)
        layers.append(
            {
                "name": "model",
                "status": STATUS_READY if runtime_ok else STATUS_INCOMPATIBLE_RUNTIME,
                "available": runtime_ok,
                "latency_ms": int((time.perf_counter() - model_started) * 1000),
                "reason_code": "" if runtime_ok else "incompatible_runtime",
            }
        )
        if not runtime_ok:
            layers.append(base.skipped_layer("inference"))
            return layers
        # 最小视觉请求：自带无隐私合成图，由用户点击“测试连接”显式触发
        layers.append(self._minimal_inference_layer())
        return layers

    def _write_probe_image(self) -> Path:
        """生成一张无隐私的合成测试图（不含任何用户内容）。"""
        from PIL import Image

        image = Image.new("RGB", (96, 96), (214, 198, 176))
        for x in range(24, 72):
            for y in range(24, 72):
                image.putpixel((x, y), (48, 72, 136))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        directory = Path(Config.UPLOAD_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f".tmp_probe_{uuid.uuid4().hex}.png"
        target.write_bytes(buffer.getvalue())
        return target

    def _minimal_inference_layer(self) -> dict[str, Any]:
        started = time.perf_counter()
        probe_path: Path | None = None
        try:
            probe_path = self._write_probe_image()
            result = self._run(
                {"path": probe_path, "content_hash": "probe", "image_id": None, "upload_id": None},
                "generic",
                "fast",
                {"timeout": 180},
            )
            ok = bool(result.get("sections"))
            return {
                "name": "inference",
                "status": STATUS_READY if ok else STATUS_ERROR,
                "available": ok,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "reason_code": "" if ok else "workflow_failed",
            }
        except AIBARError as exc:
            status, reason_code, _message = classify_error(exc)
            return {
                "name": "inference",
                "status": status,
                "available": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "reason_code": reason_code,
            }
        except Exception:
            return {
                "name": "inference",
                "status": STATUS_ERROR,
                "available": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "reason_code": "internal_error",
            }
        finally:
            if probe_path is not None:
                try:
                    probe_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # ---------------------------------------------------------------- 分析

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        return self._run(image_ref, profile, precision, options, started)

    def _run(
        self,
        image_ref: dict[str, Any],
        profile: str,
        precision: str,
        options: dict[str, Any] | None,
        started: float | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter() if started is None else started
        options = options or {}
        # 全局单任务队列：被占用时直接返回排队状态，绝不并行加载第二个大模型
        with vlm_slot(self.key, job=options.get("job_id")):
            path = (image_ref or {}).get("path")
            if not path:
                raise AIBARError("invalid_input", "缺少可读的图片副本")

            node_class, node_info = self._discover_node()
            if not node_class or not node_info:
                raise ComfyUIError("missing_node", "ComfyUI 中未发现 Qwen3-VL GGUF 节点")
            self._record_contract(node_class, node_info)

            preview_info = comfyui_client.object_info(PREVIEW_CLASS)
            if not preview_info:
                raise ComfyUIError("missing_node", f"ComfyUI 缺少 {PREVIEW_CLASS} 节点")

            suffix = Path(str(path)).suffix.lower() or ".png"
            controlled = f"aibar_reverse_{(image_ref.get('content_hash') or uuid.uuid4().hex)[:24]}{suffix}"
            comfyui_client.upload_image(str(path), controlled, overwrite=True)

            params = self.map_precision(precision)
            instruction = build_instruction()
            load_info = comfyui_client.object_info(LOAD_IMAGE_CLASS)
            load_inputs = build_inputs(load_info, {"image": controlled}) or {"image": controlled}

            # AILab_QwenVL_GGUF 实测输入：image(IMAGE 连线)、model_name(COMBO)、
            # preset_prompt(COMBO 必填下拉预设)、custom_prompt(STRING 指令)、
            # max_tokens(INT)、seed(INT, 下限 1)、keep_model_loaded(BOOLEAN)。
            # 节点逻辑：custom_prompt 非空时覆盖 preset_prompt，故 preset_prompt 填任一有效预设即可。
            # 节点无 temperature/top_p/top_k/mmproj 输入，多余键由 build_inputs 安全忽略。
            desired: dict[str, Any] = {
                "image": link("1", 0),
                "preset_prompt": "🖼️ Detailed Description",
                "custom_prompt": instruction,
                "max_tokens": params.get("max_tokens", 1024),
                "seed": max(1, int(time.time() * 1000) % 2147483647),
                "keep_model_loaded": False,
            }
            model_key, model_entry = self._model_input(node_info)
            if model_key:
                choice = base.find_option(model_entry, self._expected_model_file(), *self.match_keys)
                if choice is not None:
                    desired[model_key] = choice
            mmproj_key, mmproj_entry = self._mmproj_input(node_info)
            if mmproj_key:
                choice = base.find_option(mmproj_entry, self._expected_mmproj_file())
                if choice is not None:
                    desired[mmproj_key] = choice

            vlm_inputs = build_inputs(node_info, desired)
            if "image" not in vlm_inputs:
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

            workflow = graph(
                ("1", LOAD_IMAGE_CLASS, load_inputs),
                ("2", node_class, vlm_inputs),
                ("3", PREVIEW_CLASS, preview_inputs),
            )
            prompt_id = comfyui_client.submit_prompt(workflow, new_client_id())
            entry = comfyui_client.wait_history(
                prompt_id,
                timeout=self._timeout(options),
                cancel=self._cancel_flag(options),
            )
            raw_text = self._pick_text(entry, controlled)
            if not raw_text:
                raise ComfyUIError("workflow_failed", "模型未返回可用文本")

            parsed = extract_json_object(raw_text)
            if parsed is None:
                raise ComfyUIError("bad_response", "模型返回的 JSON 无法解析")

            raw = self._adapt(parsed, raw_text, image_ref, profile, precision, started)
            return schema.validate_structured_result(raw)

    # ---------------------------------------------------------------- 输出解析

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

    def _adapt(
        self,
        data: dict,
        raw_text: str,
        image_ref: dict[str, Any],
        profile: str,
        precision: str,
        started: float,
    ) -> dict[str, Any]:
        """把模型 JSON 适配为 M9.4 结构。

        只允许类型修复、枚举映射与长度裁剪；维度无法映射时改用关键词归类并标记不确定，
        **不伪造任何视觉细节**。
        """
        sections, negative_text, warnings = base.adapt_model_json(
            data, evidence="来自 Qwen3-VL 视觉反推", negative_confidence=NEGATIVE_CONFIDENCE
        )
        warnings.append("结果为视觉模型推断，非原始提示词；不得声称可还原 seed、采样器或模型")

        return {
            "source_type": schema.SOURCE_VISION,
            "source_label": schema.SOURCE_LABELS[schema.SOURCE_VISION],
            "provider": self.key,
            "provider_model": f"{self.model} · {self.quantization}",
            "model_profile": profile_or_default(profile),
            "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
            "recovered_original_prompt": "",
            "recovered_negative_prompt": negative_text,
            "sections": sections,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "quality_tier": schema.QUALITY_ADVANCED,
            "raw_caption": raw_text,
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

def create_qwen3vl_providers() -> list[Qwen3VLProvider]:
    """构造 Qwen3-VL 8B / 4B 两个 Provider 实例。"""
    return [
        Qwen3VLProvider(
            key="comfyui_qwen3vl_8b",
            label="Qwen3-VL 8B · 高质量结构化",
            description="本机 GGUF + Metal，输出主体、动作、环境、镜头、光影、色彩与风格的结构化中文描述",
            model="Qwen3VL-8B-Instruct-Q4_K_M",
            quantization="Q4_K_M",
            match_keys=("Qwen3VL-8B-Instruct-Q4_K_M.gguf", "qwen3vl-8b", "qwen3-vl-8b", "8b"),
        ),
        Qwen3VLProvider(
            key="comfyui_qwen3vl_4b",
            label="Qwen3-VL 4B · 均衡结构化",
            description="本机 GGUF + Metal，速度更快、占用更低的结构化反推",
            model="Qwen3VL-4B-Instruct-Q4_K_M",
            quantization="Q4_K_M",
            match_keys=("Qwen3VL-4B-Instruct-Q4_K_M.gguf", "qwen3vl-4b", "qwen3-vl-4b", "4b"),
        ),
    ]


__all__ = [
    "Qwen3VLProvider",
    "PRECISION_PARAMS",
    "build_instruction",
    "create_qwen3vl_providers",
    "extract_json_object",
]
