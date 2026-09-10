"""M9.5 可选 OpenAI-compatible 视觉 Provider（本地端点 / 外部服务共用一个实现）。

隐私约束（PRD M9.1 / M9.5 / M10.3）：
- 外部 Provider 默认关闭；**未确认外发前绝不发起请求**；
- 确认状态只保存 ``provider_key`` + ``provider_fingerprint``（服务标识）与时间，不保存密钥；
- API Key 只从环境变量（``Config.OPENAI_VISION_API_KEY``）读取，
  不写数据库、不返回前端、不进日志；列表与诊断响应不返回完整 endpoint。
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from config import Config
from core.db import execute, now, query_one
from core.errors import AIBARError
from core.logging_setup import get_logger

from .. import schema
from . import base
from .base import (
    DEFAULT_PRECISION,
    STATUS_OFFLINE,
    STATUS_READY,
    STATUS_UNAUTHORIZED,
    STATUS_UNCONFIGURED,
    VisionProvider,
    profile_or_default,
    skipped_layer,
    status_payload,
)
from .qwen3vl import build_instruction, extract_json_object

_LOGGER = get_logger("aibar.reverse.provider.openai")

# 置信度上限：外部视觉模型仍是推断结果，不得表达为原始事实
EXTERNAL_MAX_CONFIDENCE = 0.90


def provider_fingerprint(endpoint: str, model: str) -> str:
    """服务标识指纹：只用 host 与模型名，不含路径、密钥与完整 endpoint。"""
    host = ""
    try:
        host = urlparse(endpoint or "").netloc
    except ValueError:
        host = ""
    basis = f"{host or 'unknown'}|{model or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- 授权记录


def consent_state(provider_key: str, fingerprint: str) -> dict[str, Any]:
    """读取外发确认状态；指纹变化需要重新确认。"""
    row = query_one(
        "SELECT consented_at, provider_fingerprint FROM prompt_reverse_consents WHERE provider_key=?",
        (provider_key,),
    )
    if row is None:
        return {"consented": False, "consented_at": None, "fingerprint": fingerprint}
    stored = row["provider_fingerprint"] or ""
    valid = bool(row["consented_at"]) and (not stored or not fingerprint or stored == fingerprint)
    return {
        "consented": valid,
        "consented_at": row["consented_at"] if valid else None,
        "fingerprint": fingerprint,
    }


def has_consent(provider_key: str, fingerprint: str) -> bool:
    return bool(consent_state(provider_key, fingerprint).get("consented"))


def grant_consent(provider_key: str, fingerprint: str) -> dict[str, Any]:
    """记录一次外发确认（UPSERT，幂等）。"""
    execute(
        "INSERT INTO prompt_reverse_consents (provider_key, consented_at, provider_fingerprint) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(provider_key) DO UPDATE SET consented_at=excluded.consented_at, "
        "provider_fingerprint=excluded.provider_fingerprint",
        (provider_key, now(), fingerprint),
    )
    return consent_state(provider_key, fingerprint)


def revoke_consent(provider_key: str) -> None:
    execute("DELETE FROM prompt_reverse_consents WHERE provider_key=?", (provider_key,))


# ---------------------------------------------------------------- Provider


class OpenAICompatibleProvider(VisionProvider):
    """OpenAI-compatible 视觉 Provider。

    通过 ``settings()`` 惰性读取配置，便于测试 monkeypatch ``Config`` 后即时生效。
    """

    quality_tier = schema.QUALITY_ADVANCED
    precision_map = {
        "fast": {"max_tokens": 512, "temperature": 0.2},
        "standard": {"max_tokens": 1024, "temperature": 0.3},
        "fine": {"max_tokens": 1536, "temperature": 0.4},
    }

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        settings: Callable[[], dict[str, Any]],
        *,
        local: bool,
        requires_consent: bool,
        api_key_getter: Callable[[], str] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.local = local
        self.requires_consent = requires_consent
        self._settings = settings
        self._api_key_getter = api_key_getter
        self.capabilities = {"precision": ["fast", "standard", "fine"], "vision": True}

    # ---------------------------------------------------------------- 配置

    def settings(self) -> dict[str, Any]:
        try:
            raw = self._settings() or {}
        except Exception:
            return {}
        return {
            "enabled": bool(raw.get("enabled")),
            "endpoint": str(raw.get("endpoint") or ""),
            "model": str(raw.get("model") or ""),
            "timeout": int(raw.get("timeout") or 60),
        }

    def fingerprint(self) -> str:
        settings = self.settings()
        return provider_fingerprint(settings["endpoint"], settings["model"])

    def api_key(self) -> str:
        if self._api_key_getter is None:
            return ""
        try:
            return str(self._api_key_getter() or "")
        except Exception:
            return ""

    def _configuration_error(self, settings: dict[str, Any]) -> tuple[str, str, str] | None:
        """返回 ``(status, reason_code, message)``；配置齐全返回 ``None``。"""
        if not settings.get("enabled"):
            return STATUS_UNCONFIGURED, "not_configured", "该 Provider 未启用"
        if not settings.get("endpoint") or not settings.get("model"):
            return STATUS_UNCONFIGURED, "no_endpoint", "未配置服务地址或模型名"
        if not self.local and not self.api_key():
            return STATUS_UNCONFIGURED, "no_api_key", "未配置 API Key（只从环境变量读取）"
        if self.requires_consent and not has_consent(self.key, self.fingerprint()):
            return STATUS_UNAUTHORIZED, "unauthorized", "尚未确认将图片发送到该外部服务"
        return None

    # ---------------------------------------------------------------- 健康探测

    def probe(self) -> dict[str, Any]:
        settings = self.settings()
        model = settings.get("model") or ""
        problem = self._configuration_error(settings)
        if problem is not None:
            status, reason_code, message = problem
            return status_payload(status, reason_code, message, model=model)
        return status_payload(STATUS_READY, model=model)

    def test_layers(self) -> list[dict[str, Any]]:
        settings = self.settings()
        problem = self._configuration_error(settings)
        layers: list[dict[str, Any]] = []
        if problem is not None:
            status, reason_code, _message = problem
            layers.append(
                {"name": "service", "status": status, "available": False, "latency_ms": 0, "reason_code": reason_code}
            )
            layers.append(skipped_layer("model"))
            layers.append(skipped_layer("inference"))
            return layers

        started = time.perf_counter()
        ok, reason_code, models = self._list_models(settings)
        latency = int((time.perf_counter() - started) * 1000)
        layers.append(
            {
                "name": "service",
                "status": STATUS_READY if ok else STATUS_OFFLINE,
                "available": ok,
                "latency_ms": latency,
                "reason_code": "" if ok else reason_code,
            }
        )
        if not ok:
            layers.append(skipped_layer("model"))
            layers.append(skipped_layer("inference"))
            return layers

        model_ok = (not models) or (settings["model"] in models)
        layers.append(
            {
                "name": "model",
                "status": STATUS_READY if model_ok else STATUS_OFFLINE,
                "available": model_ok,
                "latency_ms": 0,
                "reason_code": "" if model_ok else "missing_model",
            }
        )
        # 最小视觉请求会真实发送图片（自带的无隐私合成图），需先完成外发授权
        if not model_ok:
            layers.append(skipped_layer("inference"))
            return layers
        layers.append(self._minimal_inference_layer(settings))
        return layers

    def _minimal_inference_layer(self, settings: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            from PIL import Image

            import io

            image = Image.new("RGB", (64, 64), (210, 190, 170))
            for x in range(16, 48):
                for y in range(16, 48):
                    image.putpixel((x, y), (50, 70, 130))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            payload = self._chat_completions(settings, buffer.getvalue(), "fast")
            ok = bool(payload)
            reason_code = "" if ok else "bad_response"
        except AIBARError as exc:
            ok = False
            reason_code = str(exc.code or "workflow_failed")
        except Exception:
            ok = False
            reason_code = "internal_error"
        return {
            "name": "inference",
            "status": STATUS_READY if ok else STATUS_OFFLINE,
            "available": ok,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "reason_code": reason_code,
        }

    # ---------------------------------------------------------------- HTTP

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _list_models(self, settings: dict[str, Any]) -> tuple[bool, str, list[str]]:
        """读取模型清单做连通性验证（不发送任何图片）。"""
        url = settings["endpoint"].rstrip("/") + "/models"
        try:
            response = requests.get(
                url, headers=self._headers(), timeout=min(15, max(3, settings["timeout"]))
            )
        except requests.RequestException:
            return False, "comfyui_offline", []
        except Exception:
            return False, "internal_error", []
        if response.status_code in (401, 403):
            return False, "unauthorized", []
        if response.status_code >= 500:
            return False, "comfyui_offline", []
        if response.status_code >= 400:
            # 端点不支持 /models 也算可达，只是无法核对模型名
            return True, "", []
        try:
            body = response.json()
        except ValueError:
            return True, "", []
        items = body.get("data") if isinstance(body, dict) else None
        models: list[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    models.append(item["id"])
        return True, "", models

    def _chat_completions(
        self, settings: dict[str, Any], image_bytes: bytes, precision: str
    ) -> str:
        """发起一次视觉请求，返回模型输出的文本内容。"""
        url = settings["endpoint"].rstrip("/") + "/chat/completions"
        params = self.map_precision(precision)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": settings["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                        {"type": "text", "text": build_instruction()},
                    ],
                }
            ],
            "max_tokens": int(params.get("max_tokens", 1024)),
            "temperature": float(params.get("temperature", 0.3)),
        }
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=max(10, settings["timeout"]),
            )
        except requests.RequestException as exc:
            raise AIBARError("provider_offline", "无法连接视觉服务，请检查地址与网络") from exc
        except Exception as exc:
            raise AIBARError("internal_error", "视觉服务调用失败") from exc
        if response.status_code in (401, 403):
            raise AIBARError("unauthorized", "视觉服务拒绝了请求，请检查密钥")
        if response.status_code >= 400:
            raise AIBARError("bad_response", "视觉服务返回了错误响应")
        try:
            body = response.json()
        except ValueError as exc:
            raise AIBARError("bad_response", "视觉服务返回内容无法解析") from exc
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise AIBARError("bad_response", "视觉服务返回内容缺少结果")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            parts = [str(part.get("text", "")) for part in content if isinstance(part, dict)]
            return "".join(parts)
        return str(content or "")

    # ---------------------------------------------------------------- 分析

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        settings = self.settings()
        # 未授权时**绝不发起请求**（PRD M9.5 / M10.6）
        problem = self._configuration_error(settings)
        if problem is not None:
            _status, reason_code, message = problem
            raise AIBARError(reason_code, message, 403 if reason_code == "unauthorized" else 400)

        path = (image_ref or {}).get("path")
        if not path:
            raise AIBARError("invalid_input", "缺少可读的图片副本")
        try:
            with open(path, "rb") as fh:
                image_bytes = fh.read()
        except OSError as exc:
            raise AIBARError("invalid_image", "读取受控图片副本失败") from exc

        raw_text = self._chat_completions(settings, image_bytes, precision)
        parsed = extract_json_object(raw_text)
        if parsed is None:
            raise AIBARError("bad_response", "视觉服务返回的 JSON 无法解析")

        sections, negative_text, warnings = base.adapt_model_json(
            parsed, evidence="来自外部视觉服务反推"
        )
        warnings.append("图片已发送至外部视觉服务，结果属于模型推断，不得视为原始提示词")

        raw = {
            "source_type": schema.SOURCE_VISION,
            "source_label": schema.SOURCE_LABELS[schema.SOURCE_VISION],
            "provider": self.key,
            "provider_model": settings["model"],
            "model_profile": profile_or_default(profile),
            "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
            "recovered_original_prompt": "",
            "recovered_negative_prompt": negative_text,
            "sections": sections,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "quality_tier": schema.QUALITY_ADVANCED,
            "raw_caption": raw_text,
            "image_ref": {
                "image_id": (image_ref or {}).get("image_id"),
                "upload_id": (image_ref or {}).get("upload_id"),
                "content_hash": (image_ref or {}).get("content_hash") or "",
            },
        }
        return schema.validate_structured_result(raw)


def _openai_settings() -> dict[str, Any]:
    return {
        "enabled": Config.OPENAI_VISION_ENABLED,
        "endpoint": Config.OPENAI_VISION_ENDPOINT,
        "model": Config.OPENAI_VISION_MODEL,
        "timeout": Config.OPENAI_VISION_TIMEOUT,
    }


def _local_settings() -> dict[str, Any]:
    return {
        "enabled": Config.LOCAL_VISION_ENABLED,
        "endpoint": Config.LOCAL_VISION_ENDPOINT,
        "model": Config.LOCAL_VISION_MODEL,
        "timeout": Config.LOCAL_VISION_TIMEOUT,
    }


def create_openai_compatible_providers() -> list[OpenAICompatibleProvider]:
    """构造本地端点与外部服务两个实例。"""
    return [
        OpenAICompatibleProvider(
            key="local_vision",
            label="本地视觉服务端点",
            description="本机或局域网内的 OpenAI-compatible 视觉端点，需在 .env 中配置",
            settings=_local_settings,
            local=True,
            requires_consent=False,
        ),
        OpenAICompatibleProvider(
            key="openai_compatible_vision",
            label="OpenAI-compatible 外部视觉",
            description="外部视觉服务，默认关闭；首次使用需确认图片外发",
            settings=_openai_settings,
            local=False,
            requires_consent=True,
            api_key_getter=lambda: Config.OPENAI_VISION_API_KEY,
        ),
    ]


__all__ = [
    "EXTERNAL_MAX_CONFIDENCE",
    "OpenAICompatibleProvider",
    "consent_state",
    "create_openai_compatible_providers",
    "grant_consent",
    "has_consent",
    "provider_fingerprint",
    "revoke_consent",
]
