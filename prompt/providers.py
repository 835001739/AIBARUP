"""M6 可插拔扩写 Provider。

- ``rules``：本地规则引擎，**首期必须实现**，永远可用（PRD M6.4）。
- ``openai_compatible``：可选 AI 语义扩写，读 ``CFG.AI_PROVIDER_*``，默认关闭。

降级策略（PRD M6.4 / §9）：
外部 Provider 未启用、超时、限流、响应格式错误或返回非法结构时，
一律自动回退 ``rules``，**整体请求不失败**，只在 ``warnings`` 里说明降级原因。

隐私约束（HARNESS §5）：
API Key 只从环境变量读取，不写数据库、不返回前端、不进日志；
日志只记录 ``profile/provider/intensity/duration_ms/status`` 与输入输出长度。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from config import CFG
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from core.textutil import sanitize_model_text

from . import engine, library

logger = get_logger("aibar.prompt.providers")

RULES = "rules"
OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_KEYS: tuple[str, ...] = (RULES, OPENAI_COMPATIBLE)

MAX_AI_TEXT_LEN = 4000
MAX_LIST_ITEMS = 40

# 外部返回必须是这套字段，缺字段或类型不符即视为格式错误并回退
_REQUIRED_STRING_FIELDS = ("expanded_positive",)
_OPTIONAL_STRING_FIELDS = ("expanded_negative",)


class ProviderError(Exception):
    """外部 Provider 不可用或返回非法，携带可展示的降级原因。"""

    def __init__(self, reason_code: str, reason: str):
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


# ---------------------------------------------------------------- 本地规则


class RulesProvider:
    """本地确定性规则引擎，不联网、可复现。"""

    key = RULES

    def expand(
        self,
        original_prompt: str,
        profile: str,
        intensity: str,
        template_id: str | None = None,
        options: dict | None = None,
    ) -> dict:
        result = engine.expand(original_prompt, profile, intensity, template_id, options)
        result["provider"] = RULES
        return result


# ---------------------------------------------------------------- OpenAI 兼容


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions 扩写，默认关闭。

    只做三件事：拼请求、取回文本、严格校验结构；任何一步失败都抛
    ``ProviderError``，由上层统一回退到规则引擎。
    """

    key = OPENAI_COMPATIBLE
    system_prompt = (
        "你是提示词工程助手。根据用户给出的原始提示词、目标模型档案和扩写强度，"
        "只补充缺失的画面信息。严格约束：不得替换或删除原文中的主体、动作、数量、"
        "关系、专有名词和用户明确指定的风格；不得添加艺术家姓名、品牌或真实人物身份；"
        "不得出现互斥描述；不要输出解释性文字。"
        "必须只返回一个 JSON 对象，字段为 expanded_positive（字符串，必填）、"
        "expanded_negative（字符串）、additions（字符串数组）、warnings（字符串数组）。"
    )

    def is_enabled(self) -> bool:
        return bool(CFG.AI_PROVIDER_ENABLED and CFG.AI_PROVIDER_ENDPOINT and CFG.AI_PROVIDER_MODEL)

    def expand(
        self,
        original_prompt: str,
        profile: str,
        intensity: str,
        template_id: str | None = None,
        options: dict | None = None,
    ) -> dict:
        if not CFG.AI_PROVIDER_ENABLED:
            raise ProviderError("provider_disabled", "AI 扩写服务未启用")
        if not CFG.AI_PROVIDER_ENDPOINT or not CFG.AI_PROVIDER_MODEL:
            raise ProviderError("provider_unconfigured", "AI 扩写服务未配置 endpoint 或 model")

        payload = self._build_payload(original_prompt, profile, intensity, template_id)
        headers = {"Content-Type": "application/json"}
        if CFG.AI_PROVIDER_API_KEY:
            headers["Authorization"] = f"Bearer {CFG.AI_PROVIDER_API_KEY}"

        try:
            response = requests.post(
                f"{CFG.AI_PROVIDER_ENDPOINT.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=max(1, int(CFG.AI_PROVIDER_TIMEOUT or 60)),
            )
        except requests.exceptions.Timeout:
            raise ProviderError("provider_timeout", "AI 扩写服务请求超时")
        except requests.exceptions.RequestException:
            raise ProviderError("provider_offline", "AI 扩写服务连接失败")

        status = int(getattr(response, "status_code", 0) or 0)
        if status == 429:
            raise ProviderError("provider_rate_limited", "AI 扩写服务触发限流")
        if status >= 400:
            raise ProviderError("provider_http_error", f"AI 扩写服务返回 HTTP {status}")

        raw_text = self._extract_text(response)
        structured = self._parse_structure(raw_text)
        return self._to_result(
            structured, original_prompt, profile, intensity, template_id
        )

    # ---- 内部辅助

    def _build_payload(
        self,
        original_prompt: str,
        profile: str,
        intensity: str,
        template_id: str | None,
    ) -> dict:
        profile_meta = library.profile(profile)
        user_payload = {
            "original_prompt": original_prompt,
            "profile": profile,
            "profile_style": profile_meta.get("style", "natural"),
            "structure_hint": profile_meta.get("structure_hint", ""),
            "supports_negative": bool(profile_meta.get("supports_negative")),
            "supports_weight": bool(profile_meta.get("supports_weight")),
            "intensity": intensity,
            "template_id": template_id or "",
        }
        return {
            "model": CFG.AI_PROVIDER_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }

    def _extract_text(self, response: Any) -> str:
        try:
            data = response.json()
        except (ValueError, AttributeError):
            raise ProviderError("provider_bad_response", "AI 扩写服务返回不是合法 JSON")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("provider_bad_response", "AI 扩写服务返回结构不符合预期")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("provider_bad_response", "AI 扩写服务返回内容为空")
        return sanitize_model_text(content, MAX_AI_TEXT_LEN)

    def _parse_structure(self, raw_text: str) -> dict:
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ProviderError("provider_bad_response", "AI 扩写服务未返回 JSON 结果")
            try:
                data = json.loads(text[start : end + 1])
            except ValueError:
                raise ProviderError("provider_bad_response", "AI 扩写服务返回的 JSON 无法解析")
        if not isinstance(data, dict):
            raise ProviderError("provider_bad_response", "AI 扩写服务返回的不是 JSON 对象")
        for field in _REQUIRED_STRING_FIELDS:
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ProviderError(
                    "provider_schema_invalid", f"AI 扩写服务缺少有效字段：{field}"
                )
        return data

    def _to_result(
        self,
        data: dict,
        original_prompt: str,
        profile: str,
        intensity: str,
        template_id: str | None,
    ) -> dict:
        """只接受白名单字段，全部做类型与长度校验后才进入结果。"""
        profile_meta = library.profile(profile)
        positive = sanitize_model_text(str(data.get("expanded_positive", "")), MAX_AI_TEXT_LEN).strip()
        negative = sanitize_model_text(str(data.get("expanded_negative") or ""), MAX_AI_TEXT_LEN).strip()
        if not profile_meta.get("supports_negative"):
            negative = ""
        sections = self._sections(data)
        return {
            "original_prompt": original_prompt,
            "expanded_positive": positive,
            "expanded_negative": negative,
            "profile": profile,
            "intensity": intensity,
            "provider": OPENAI_COMPATIBLE,
            "template_id": str(template_id) if template_id else None,
            "sections": sections,
            "additions": self._string_list(data.get("additions"), "additions"),
            "warnings": self._string_list(data.get("warnings"), "warnings"),
            "duration_ms": 0,
        }

    def _sections(self, data: dict) -> list[dict]:
        raw = data.get("sections")
        if not isinstance(raw, list):
            return []
        sections: list[dict] = []
        for item in raw[:MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                continue
            dimension = str(item.get("dimension") or "").strip()
            text = sanitize_model_text(str(item.get("text") or ""), 1000).strip()
            if not dimension or not text:
                continue
            sections.append(
                {
                    "dimension": dimension if dimension in library.dimension_keys() else library.FALLBACK_DIMENSION,
                    "dimension_label": library.dimension_label(dimension),
                    "text": text,
                    "is_new": bool(item.get("is_new", True)),
                }
            )
        return sections

    def _string_list(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            return []
        items: list[str] = []
        for item in value[:MAX_LIST_ITEMS]:
            if isinstance(item, str):
                text = sanitize_model_text(item, 500).strip()
                if text:
                    items.append(text)
        return items


# ---------------------------------------------------------------- 调度


_PROVIDERS: dict[str, Any] = {
    RULES: RulesProvider(),
    OPENAI_COMPATIBLE: OpenAICompatibleProvider(),
}


def available_providers() -> list[str]:
    return list(PROVIDER_KEYS)


def _log(
    provider_key: str,
    profile: str,
    intensity: str,
    duration_ms: int,
    status: str,
    in_len: int,
    out_len: int,
    error_code: str = "",
) -> None:
    """结构化安全日志：不含提示词正文与密钥。"""
    safe_log(
        logger,
        logging.INFO,
        "prompt_expand",
        profile=profile,
        provider=provider_key,
        intensity=intensity,
        duration_ms=duration_ms,
        status=status,
        in_len=in_len,
        out_len=out_len,
        error_code=error_code,
    )


def expand(
    original_prompt: str,
    profile: str = engine.DEFAULT_PROFILE,
    intensity: str = engine.DEFAULT_INTENSITY,
    template_id: str | None = None,
    options: dict | None = None,
    provider: str = RULES,
) -> dict:
    """按指定 Provider 执行扩写；外部 Provider 失败时自动回退规则引擎。

    Raises:
        AIBARError: 入参非法（``invalid_input``）。注意校验在任何引擎调用之前完成。
    """
    # 先校验：非法输入绝不触达任何 Provider（PRD M6.3）
    engine.validate_input(original_prompt, profile, intensity, template_id, options)

    provider_key = (provider or RULES).strip() or RULES
    if provider_key not in PROVIDER_KEYS:
        raise AIBARError("invalid_input", f"未知的扩写 Provider：{provider_key}")

    started = time.perf_counter()
    in_len = len(original_prompt or "")

    if provider_key == RULES:
        result = _PROVIDERS[RULES].expand(original_prompt, profile, intensity, template_id, options)
        duration_ms = int(result.get("duration_ms") or 0)
        _log(RULES, profile, intensity, duration_ms, "ok", in_len, len(result["expanded_positive"]))
        return result

    try:
        result = _PROVIDERS[OPENAI_COMPATIBLE].expand(
            original_prompt, profile, intensity, template_id, options
        )
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        result["requested_provider"] = provider_key
        _log(
            OPENAI_COMPATIBLE,
            profile,
            intensity,
            int(result["duration_ms"]),
            "ok",
            in_len,
            len(result["expanded_positive"]),
        )
        return result
    except ProviderError as exc:
        reason_code, reason = exc.reason_code, exc.reason
    except Exception:  # 兜底：任何未预期异常都不得让整体请求失败
        reason_code, reason = "provider_error", "AI 扩写服务返回异常"

    fallback = _PROVIDERS[RULES].expand(original_prompt, profile, intensity, template_id, options)
    duration_ms = int((time.perf_counter() - started) * 1000)
    fallback["provider"] = RULES
    fallback["requested_provider"] = provider_key
    fallback["duration_ms"] = duration_ms
    fallback["warnings"] = list(fallback.get("warnings") or []) + [
        f"AI 扩写服务不可用（{reason}），已自动回退本地规则引擎"
    ]
    _log(
        RULES,
        profile,
        intensity,
        duration_ms,
        "fallback",
        in_len,
        len(fallback["expanded_positive"]),
        error_code=reason_code,
    )
    return fallback


__all__ = [
    "RULES",
    "OPENAI_COMPATIBLE",
    "PROVIDER_KEYS",
    "ProviderError",
    "RulesProvider",
    "OpenAICompatibleProvider",
    "available_providers",
    "expand",
]
