"""M9.4 结构化反推结果的严格校验测试。

重点：枚举、长度、置信度范围与类型错误**不得**进入正式结果或词库；
用户编辑场景（``strict_sections=True``）必须报错而不是静默丢弃。
"""

from __future__ import annotations

import pytest
from core.textutil import dimension_keys

from reverse import schema


def _raw(**overrides) -> dict:
    base = {
        "source_type": "vision",
        "provider": "comfyui_blip",
        "provider_model": "blip-image-captioning-base",
        "model_profile": "generic",
        "precision": "standard",
        "sections": [{"dimension": "subject", "text": "一只橘色短毛猫", "confidence": 0.7}],
        "warnings": [],
        "duration_ms": 12,
        "quality_tier": "basic",
        "image_ref": {"upload_id": "a" * 64, "image_id": None, "content_hash": "a" * 64},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 维度枚举


def test_dimension_keys_match_taxonomy():
    keys = schema.image_dimension_keys()
    assert keys == dimension_keys("image")
    assert len(keys) == 13
    assert "negative" in keys


def test_coerce_dimension_maps_chinese_labels():
    assert schema.coerce_dimension("主体与外观") == "subject"
    assert schema.coerce_dimension("负向") == "negative"
    assert schema.coerce_dimension("lighting") == "lighting"
    assert schema.coerce_dimension("not_a_dimension") is None
    assert schema.coerce_dimension(123) is None


def test_unknown_dimension_is_dropped_with_warning():
    result = schema.validate_structured_result(
        _raw(sections=[{"dimension": "not_a_dimension", "text": "一只橘色短毛猫", "confidence": 0.8}])
    )
    assert result["sections"] == []
    assert any("维度" in item for item in result["warnings"])


# ---------------------------------------------------------------- 置信度与类型


def test_out_of_range_confidence_is_dropped():
    result = schema.validate_structured_result(
        _raw(sections=[{"dimension": "subject", "text": "一只橘色短毛猫", "confidence": 1.8}])
    )
    assert result["sections"] == []


def test_negative_confidence_is_dropped():
    result = schema.validate_structured_result(
        _raw(sections=[{"dimension": "subject", "text": "一只橘色短毛猫", "confidence": -0.2}])
    )
    assert result["sections"] == []


def test_non_object_section_is_dropped():
    result = schema.validate_structured_result(_raw(sections=["纯文本片段", 42, None]))
    assert result["sections"] == []
    assert result["warnings"]


def test_empty_text_is_dropped():
    result = schema.validate_structured_result(
        _raw(sections=[{"dimension": "subject", "text": "   ", "confidence": 0.9}])
    )
    assert result["sections"] == []


def test_long_text_is_truncated():
    result = schema.validate_structured_result(
        _raw(sections=[{"dimension": "subject", "text": "猫" * 500, "confidence": 0.5}])
    )
    assert len(result["sections"]) == 1
    assert len(result["sections"][0]["text"]) == schema.MAX_SECTION_TEXT


def test_sections_limit_is_enforced():
    sections = [
        {"dimension": "subject", "text": f"描述片段{index}", "confidence": 0.9}
        for index in range(schema.MAX_SECTIONS + 5)
    ]
    result = schema.validate_structured_result(_raw(sections=sections))
    assert len(result["sections"]) == schema.MAX_SECTIONS


# ---------------------------------------------------------------- 严格模式


def test_strict_mode_raises_on_bad_section():
    from core.errors import AIBARError

    with pytest.raises(AIBARError) as excinfo:
        schema.validate_structured_result(
            _raw(sections=[{"dimension": "nope", "text": "一只橘色短毛猫", "confidence": 0.5}]),
            strict_sections=True,
        )
    assert excinfo.value.code == "invalid_input"


def test_strict_mode_raises_on_bad_confidence():
    from core.errors import AIBARError

    with pytest.raises(AIBARError) as excinfo:
        schema.validate_structured_result(
            _raw(sections=[{"dimension": "subject", "text": "一只橘色短毛猫", "confidence": 3}]),
            strict_sections=True,
        )
    assert excinfo.value.code == "invalid_input"


def test_missing_source_type_raises():
    from core.errors import AIBARError

    with pytest.raises(AIBARError) as excinfo:
        schema.validate_structured_result({"sections": []})
    assert excinfo.value.code == "invalid_provider_result"


def test_non_object_result_raises():
    from core.errors import AIBARError

    with pytest.raises(AIBARError) as excinfo:
        schema.validate_structured_result(["not", "a", "dict"])
    assert excinfo.value.code == "invalid_provider_result"


# ---------------------------------------------------------------- 置信度上限与格式化


def test_confidence_cap_by_source():
    assert schema.cap_confidence(0.99, "metadata") == 0.95
    assert schema.cap_confidence(0.99, "vision") == 0.95
    assert schema.cap_confidence(0.42, "vision") == 0.42


def test_make_section_labels_and_caps():
    section = schema.make_section(0, "lighting", "逆光形成金色轮廓光", 0.99)
    assert section["id"] == "s1"
    assert section["dimension_label"] == schema.dimension_label_of("lighting")
    assert section["confidence"] == pytest.approx(0.99)


def _negative_sections() -> list[dict]:
    return [
        {"dimension": "subject", "text": "雪原上的独行者", "confidence": 0.9},
        {"dimension": "negative", "text": "模糊，低质量", "confidence": 0.6},
    ]


def test_flux_profile_never_emits_negative():
    result = schema.validate_structured_result(
        _raw(model_profile="flux_flux2", sections=_negative_sections())
    )
    assert result["formatted_negative"] == ""
    positive, negative = schema.format_result(result["sections"], "flux_flux2")
    assert negative == ""
    assert "模糊" not in positive


def test_sd15_profile_keeps_negative():
    result = schema.validate_structured_result(
        _raw(model_profile="sd15_sdxl", sections=_negative_sections())
    )
    assert "模糊" in result["formatted_negative"]
    assert "雪原上的独行者" in result["formatted_positive"]


def test_format_result_orders_by_taxonomy():
    sections = [
        schema.make_section(0, "mood", "孤寂的氛围", 0.8),
        schema.make_section(1, "subject", "雪原上的独行者", 0.9),
    ]
    positive, _negative = schema.format_result(sections, "generic")
    assert positive.index("雪原上的独行者") < positive.index("孤寂的氛围")


# ---------------------------------------------------------------- 归一化


def test_precision_and_quality_fall_back():
    result = schema.validate_structured_result(_raw(precision="ultra", quality_tier="magic"))
    assert result["precision"] == schema.DEFAULT_PRECISION
    assert result["quality_tier"] == schema.QUALITY_BASIC


def test_unknown_profile_falls_back_to_generic():
    result = schema.validate_structured_result(_raw(model_profile="not_a_profile"))
    assert result["model_profile"] == "generic"


def test_image_ref_never_exposes_path():
    result = schema.validate_structured_result(
        _raw(image_ref={"path": "/Users/someone/secret.png", "upload_id": "b" * 64})
    )
    assert "path" not in result["image_ref"]
    assert result["image_ref"]["upload_id"] == "b" * 64
    assert result["image_ref"]["image_id"] is None


def test_warnings_are_deduped_and_capped():
    warnings = [f"提示{index}" for index in range(schema.MAX_WARNINGS + 5)]
    result = schema.validate_structured_result(_raw(warnings=warnings))
    assert len(result["warnings"]) == schema.MAX_WARNINGS

    duplicated = ["重复提示"] * 5
    result = schema.validate_structured_result(_raw(warnings=duplicated))
    assert result["warnings"] == ["重复提示"]


def test_has_usable_content():
    assert schema.has_usable_content(_raw()) is True
    assert schema.has_usable_content(_raw(sections=[], recovered_original_prompt="")) is False
    assert schema.has_usable_content(_raw(sections=[], recovered_original_prompt="原始提示词")) is True
