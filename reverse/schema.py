"""M9.4 结构化反推结果 Schema 与严格校验。

设计要点（PRD M9.4 / M9.5 / M9.9）：
- 模型输出一律视为**不可信文本**：先经 ``core.textutil.sanitize_model_text`` 清理控制字符；
- ``dimension`` 只能取 ``core.textutil.dimension_keys("image")`` 中的 13 个图片维度，
  未知维度、越界置信度、空文本、超长文本一律丢弃该片段并留下 warning；
- 没有可靠内容的维度**不生成空词条**；
- ``format_result`` 按模型档案格式化，``flux_flux2`` 不输出负向提示词。

校验策略分两层：
- Provider 适配器做「宽容 JSON 提取 + 类型修复 / 枚举映射 / 长度裁剪」；
- 本模块做「严格校验」，任何格式错误都不得进入正式结果或词库。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from core.errors import AIBARError
from core.textutil import (
    dimension_keys,
    dimension_label,
    get_profile,
    profile_keys,
    sanitize_model_text,
)

# ---------------------------------------------------------------- 常量

MEDIA_TYPE = "image"

SOURCE_METADATA = "metadata"
SOURCE_VISION = "vision"
SOURCE_TYPES = (SOURCE_METADATA, SOURCE_VISION)
SOURCE_LABELS = {
    SOURCE_METADATA: "原始提示词恢复",
    SOURCE_VISION: "视觉提示词反推",
}

PRECISION_FAST = "fast"
PRECISION_STANDARD = "standard"
PRECISION_FINE = "fine"
PRECISIONS = (PRECISION_FAST, PRECISION_STANDARD, PRECISION_FINE)
DEFAULT_PRECISION = PRECISION_STANDARD

QUALITY_ORIGINAL = "original"
QUALITY_BASIC = "basic"
QUALITY_ADVANCED = "advanced"
QUALITY_TIERS = (QUALITY_ORIGINAL, QUALITY_BASIC, QUALITY_ADVANCED)

MIN_CONFIDENCE = 0.0
MAX_CONFIDENCE = 1.0

MAX_SECTION_TEXT = 300
MAX_PROMPT_TEXT = 8000
MAX_WARNING_TEXT = 200
MAX_WARNINGS = 20
MAX_SECTIONS = 40
MAX_SUBCATEGORY = 64
MAX_SOURCE_LABEL = 60
MAX_PROVIDER_MODEL = 120
MAX_EVIDENCE = 300
MAX_UNCERTAINTY = 300
MAX_ID_TEXT = 64

# 置信度上限：不同来源的可信度必须区分表达（PRD M9.1 / M10.2 / M11.1）
CONFIDENCE_CAP = {
    SOURCE_METADATA: 0.95,
    SOURCE_VISION: 0.95,
}

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 中文短名 -> 维度 key。分类体系（taxonomy.json）是维度真源，这里只补短名与常见写法。
_DIMENSION_ALIASES: dict[str, str] = {
    "subject": "subject",
    "主体": "subject",
    "主体与外观": "subject",
    "主体外观": "subject",
    "人物与主体": "subject",
    "外观": "subject",
    "pose": "pose",
    "姿势": "pose",
    "人物姿势": "pose",
    "姿态": "pose",
    "人物姿态": "pose",
    "action": "action",
    "动作": "action",
    "clothing": "clothing",
    "服装": "clothing",
    "服装与材质": "clothing",
    "材质": "clothing",
    "environment": "environment",
    "环境": "environment",
    "环境与空间": "environment",
    "场景": "environment",
    "composition": "composition",
    "构图": "composition",
    "camera": "camera",
    "镜头": "camera",
    "景别": "camera",
    "景别与镜头": "camera",
    "lighting": "lighting",
    "光影": "lighting",
    "光线": "lighting",
    "光": "lighting",
    "color": "color",
    "色彩": "color",
    "颜色": "color",
    "style": "style",
    "风格": "style",
    "视觉风格": "style",
    "mood": "mood",
    "氛围": "mood",
    "typography": "typography",
    "文字": "typography",
    "文字排版": "typography",
    "排版": "typography",
    "negative": "negative",
    "负向": "negative",
    "负向约束": "negative",
    "负面": "negative",
}


# ---------------------------------------------------------------- 维度工具


def image_dimension_keys() -> list[str]:
    """图片维度 key 列表（来自版本化分类体系，13 个）。"""
    try:
        keys = dimension_keys(MEDIA_TYPE)
    except Exception:  # 分类体系缺失时不阻断反推，回退到最小集合
        return ["subject", "environment", "style", "negative"]
    return list(keys) or ["subject", "environment", "style", "negative"]


def dimension_label_of(dimension: str) -> str:
    try:
        return dimension_label(MEDIA_TYPE, dimension)
    except Exception:
        return dimension


def coerce_dimension(value: Any) -> str | None:
    """把模型或用户给出的维度写法归一化为维度 key；无法识别返回 ``None``。

    只做枚举映射，绝不猜测：无法映射的维度由调用方丢弃该片段。
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    keys = image_dimension_keys()
    low = raw.lower()
    if low in keys:
        return low
    for key in keys:
        if raw == dimension_label_of(key):
            return key
    alias = _DIMENSION_ALIASES.get(raw) or _DIMENSION_ALIASES.get(low)
    if alias in keys:
        return alias
    return None


def sorted_dimensions(dimension: str) -> int:
    """维度排序权重：按分类体系顺序，负向约束固定最后。"""
    keys = image_dimension_keys()
    if dimension in keys:
        return keys.index(dimension)
    return len(keys)


# ---------------------------------------------------------------- 片段构造


def new_section_id(index: int) -> str:
    return f"s{index + 1}"


def make_section(
    index: int,
    dimension: str,
    text: str,
    confidence: float,
    *,
    subcategory: str = "",
    evidence: str = "",
    uncertainty: str = "",
    editable: bool = True,
    selected_for_library: bool = True,
    section_id: str = "",
) -> dict[str, Any]:
    """构造一个规范化片段。文本一律先做不可信文本清理。"""
    return {
        "id": str(section_id or new_section_id(index)),
        "dimension": dimension,
        "dimension_label": dimension_label_of(dimension),
        "subcategory": sanitize_model_text(subcategory, MAX_SUBCATEGORY).strip(),
        "text": sanitize_model_text(text, MAX_SECTION_TEXT).strip(),
        "confidence": round(max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, float(confidence))), 4),
        "evidence": sanitize_model_text(evidence, MAX_EVIDENCE).strip(),
        "uncertainty": sanitize_model_text(uncertainty, MAX_UNCERTAINTY).strip(),
        "editable": bool(editable),
        "selected_for_library": bool(selected_for_library),
    }


def cap_confidence(confidence: float, source_type: str) -> float:
    """按来源限制置信度上限，避免把模型推测表达成原始事实。"""
    limit = CONFIDENCE_CAP.get(source_type, MAX_CONFIDENCE)
    return round(max(MIN_CONFIDENCE, min(float(confidence), limit)), 4)


# ---------------------------------------------------------------- 文本清理


def clean_text(value: Any, max_len: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return sanitize_model_text(value, max_len).strip()


def _clean_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


def _clean_confidence(value: Any) -> float | None:
    """类型修复，但**不做范围裁剪**：越界即视为格式错误。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if number < MIN_CONFIDENCE or number > MAX_CONFIDENCE:
        return None
    return round(number, 4)


# ---------------------------------------------------------------- 片段校验


def _validate_section(
    raw: Any,
    index: int,
    *,
    strict: bool,
    warnings: list[str],
) -> dict[str, Any] | None:
    """校验单个片段。``strict=True`` 抛错（用户编辑），``False`` 丢弃并记 warning。"""
    if not isinstance(raw, dict):
        message = f"第 {index + 1} 个片段不是对象，已丢弃"
        if strict:
            raise AIBARError("invalid_input", message)
        warnings.append(message)
        return None

    text = clean_text(raw.get("text"), MAX_SECTION_TEXT)
    if not text:
        message = f"第 {index + 1} 个片段缺少有效文本"
        if strict:
            raise AIBARError("invalid_input", message)
        warnings.append(message)
        return None

    dimension = coerce_dimension(raw.get("dimension"))
    if dimension is None:
        message = f"第 {index + 1} 个片段的维度无法识别，已丢弃"
        if strict:
            raise AIBARError("invalid_input", message)
        warnings.append(message)
        return None

    confidence = _clean_confidence(raw.get("confidence", 0.5))
    if confidence is None:
        message = f"第 {index + 1} 个片段的置信度超出 0-1 范围，已丢弃"
        if strict:
            raise AIBARError("invalid_input", message)
        warnings.append(message)
        return None

    section_id = clean_text(raw.get("id"), MAX_ID_TEXT) or new_section_id(index)
    if not _SAFE_ID_RE.match(section_id):
        section_id = new_section_id(index)

    subcategory = clean_text(raw.get("subcategory"), MAX_SUBCATEGORY)
    if subcategory and not _SAFE_ID_RE.match(subcategory):
        subcategory = ""

    return {
        "id": section_id,
        "dimension": dimension,
        "dimension_label": dimension_label_of(dimension),
        "subcategory": subcategory,
        "text": text,
        "confidence": confidence,
        "evidence": clean_text(raw.get("evidence"), MAX_EVIDENCE),
        "uncertainty": clean_text(raw.get("uncertainty"), MAX_UNCERTAINTY),
        "editable": _clean_bool(raw.get("editable"), True),
        "selected_for_library": _clean_bool(raw.get("selected_for_library"), True),
    }


def validate_sections(
    raw_sections: Any,
    *,
    strict: bool = False,
    warnings: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验片段列表，返回 ``(有效片段, 警告列表)``。

    ``strict=False``（模型输出）：坏片段直接丢弃；
    ``strict=True``（用户编辑）：坏片段直接报错，避免静默改动用户输入。
    """
    sink: list[str] = warnings if warnings is not None else []
    if raw_sections is None:
        return [], sink
    if not isinstance(raw_sections, (list, tuple)):
        message = "sections 字段必须是数组"
        if strict:
            raise AIBARError("invalid_input", message)
        sink.append(message)
        return [], sink

    sections: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_sections):
        if len(sections) >= MAX_SECTIONS:
            sink.append(f"片段数量超过上限 {MAX_SECTIONS}，多余片段已丢弃")
            break
        section = _validate_section(raw, index, strict=strict, warnings=sink)
        if section is not None:
            sections.append(section)
    return sections, sink


# ---------------------------------------------------------------- image_ref


def normalize_image_ref(raw: Any) -> dict[str, Any]:
    """归一化图片引用。**绝不包含本机绝对路径**。"""
    source = raw if isinstance(raw, dict) else {}
    image_id = clean_text(source.get("image_id"), 128) or None
    upload_id = clean_text(source.get("upload_id"), 128) or None
    content_hash = clean_text(source.get("content_hash"), 128)
    return {
        "image_id": image_id,
        "upload_id": upload_id,
        "content_hash": content_hash or "",
    }


def normalize_warnings(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    warnings: list[str] = []
    for item in raw:
        text = clean_text(item, MAX_WARNING_TEXT)
        if text and text not in warnings:
            warnings.append(text)
        if len(warnings) >= MAX_WARNINGS:
            break
    return warnings


def coerce_duration_ms(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    if isinstance(raw, str):
        try:
            return max(0, int(float(raw.strip())))
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------- 格式化


def format_result(
    sections: Iterable[dict[str, Any]],
    profile: str = "generic",
) -> tuple[str, str]:
    """按模型档案把片段格式化为完整正向/负向提示词。

    - 维度顺序沿用分类体系，``negative`` 维度只进入负向提示词；
    - 不支持负向提示词的档案（如 ``flux_flux2``）负向结果恒为空字符串。
    """
    prof = get_profile(profile) if profile in profile_keys() else get_profile("generic")
    separator = prof.get("separator") or "，"
    style = prof.get("style") or "natural"

    ordered = sorted(
        [s for s in sections if isinstance(s, dict) and s.get("text")],
        key=lambda s: (sorted_dimensions(str(s.get("dimension", "")))),
    )
    positive_parts = [str(s["text"]).strip() for s in ordered if s.get("dimension") != "negative"]
    negative_parts = [str(s["text"]).strip() for s in ordered if s.get("dimension") == "negative"]

    positive = _join_parts(positive_parts, separator, style)
    negative = ""
    if prof.get("supports_negative"):
        negative = _join_parts(negative_parts, separator, style)
    return positive, negative


def _join_parts(parts: list[str], separator: str, style: str) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if style == "tags":
        return separator.join(parts)
    # 自然语言档案：片段自带标点时优先用空格，否则用档案分隔符连接
    if separator.strip() == "":
        return " ".join(parts)
    return separator.join(parts)


# ---------------------------------------------------------------- 结果校验


def validate_structured_result(
    raw: Any,
    *,
    strict_sections: bool = False,
) -> dict[str, Any]:
    """严格校验并归一化结构化反推结果。

    Args:
        raw: Provider 构造的原始字典（模型输出先经适配器宽容处理）。
        strict_sections: True 时片段级错误直接抛 ``AIBARError``（用户编辑场景）；
            False 时丢弃坏片段并记录 warning（模型输出场景）。

    Raises:
        AIBARError: 顶层结构不合法（``invalid_provider_result``）或严格模式下片段不合法。

    Returns:
        归一化后的结构化结果，字段与取值均已受控，可直接入库与返回前端。
    """
    if not isinstance(raw, dict):
        raise AIBARError("invalid_provider_result", "反推结果结构不合法（不是对象）")

    source_type = raw.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise AIBARError("invalid_provider_result", "反推结果缺少合法的 source_type")

    warnings = normalize_warnings(raw.get("warnings"))
    sections, section_warnings = validate_sections(
        raw.get("sections"), strict=strict_sections, warnings=warnings
    )
    for message in section_warnings:
        if message not in warnings and len(warnings) < MAX_WARNINGS:
            warnings.append(message)

    profile = raw.get("model_profile")
    if profile not in profile_keys():
        profile = "generic"
    precision = raw.get("precision")
    if precision not in PRECISIONS:
        precision = DEFAULT_PRECISION
    quality_tier = raw.get("quality_tier")
    if quality_tier not in QUALITY_TIERS:
        quality_tier = QUALITY_ORIGINAL if source_type == SOURCE_METADATA else QUALITY_BASIC

    formatted_positive = clean_text(raw.get("formatted_positive"), MAX_PROMPT_TEXT)
    formatted_negative = clean_text(raw.get("formatted_negative"), MAX_PROMPT_TEXT)
    if not formatted_positive:
        formatted_positive, formatted_negative = format_result(sections, profile)
    if formatted_negative and not get_profile(profile).get("supports_negative"):
        formatted_negative = ""

    result: dict[str, Any] = {
        "source_type": source_type,
        "source_label": clean_text(raw.get("source_label"), MAX_SOURCE_LABEL)
        or SOURCE_LABELS[source_type],
        "provider": clean_text(raw.get("provider"), MAX_PROVIDER_MODEL),
        "provider_model": clean_text(raw.get("provider_model"), MAX_PROVIDER_MODEL),
        "model_profile": profile,
        "precision": precision,
        "recovered_original_prompt": clean_text(
            raw.get("recovered_original_prompt"), MAX_PROMPT_TEXT
        ),
        "recovered_negative_prompt": clean_text(
            raw.get("recovered_negative_prompt"), MAX_PROMPT_TEXT
        ),
        "sections": sections,
        "formatted_positive": formatted_positive,
        "formatted_negative": formatted_negative,
        "warnings": warnings,
        "duration_ms": coerce_duration_ms(raw.get("duration_ms")),
        "quality_tier": quality_tier,
        "image_ref": normalize_image_ref(raw.get("image_ref")),
    }

    raw_caption = clean_text(raw.get("raw_caption"), MAX_PROMPT_TEXT)
    if raw_caption:
        result["raw_caption"] = raw_caption
    snapshot = raw.get("provider_status_snapshot")
    if isinstance(snapshot, dict):
        result["provider_status_snapshot"] = {
            "status": clean_text(snapshot.get("status"), 32),
            "reason_code": clean_text(snapshot.get("reason_code"), 64),
            "model": clean_text(snapshot.get("model"), MAX_PROVIDER_MODEL),
            "queue": snapshot.get("queue") if isinstance(snapshot.get("queue"), dict) else None,
        }
    return result


def has_usable_content(result: dict[str, Any]) -> bool:
    """结果是否包含可交付内容（至少一个片段或恢复了原始提示词）。"""
    if not isinstance(result, dict):
        return False
    if result.get("sections"):
        return True
    return bool(result.get("recovered_original_prompt"))


__all__ = [
    "MEDIA_TYPE",
    "SOURCE_METADATA",
    "SOURCE_VISION",
    "SOURCE_TYPES",
    "SOURCE_LABELS",
    "PRECISIONS",
    "DEFAULT_PRECISION",
    "QUALITY_TIERS",
    "QUALITY_ORIGINAL",
    "QUALITY_BASIC",
    "QUALITY_ADVANCED",
    "CONFIDENCE_CAP",
    "MAX_SECTIONS",
    "image_dimension_keys",
    "dimension_label_of",
    "coerce_dimension",
    "make_section",
    "new_section_id",
    "cap_confidence",
    "clean_text",
    "validate_sections",
    "validate_structured_result",
    "normalize_image_ref",
    "normalize_warnings",
    "coerce_duration_ms",
    "format_result",
    "has_usable_content",
]
