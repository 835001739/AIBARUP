"""M6 知识库（resources/prompt_library.json）的结构、内容与查询测试。

这些用例只依赖版本化资源文件，不接触数据库，因此不需要 tmp_path 隔离。
"""

from __future__ import annotations

import pytest

from core.errors import AIBARError
from prompt import library

EXPECTED_PROFILES = ("generic", "sd15_sdxl", "flux_flux2")
EXPECTED_DIMENSIONS = (
    "subject",
    "appearance",
    "action",
    "environment",
    "composition",
    "camera",
    "lighting",
    "color",
    "style",
    "mood",
    "quality",
    "typography",
    "negative",
)
MIN_TERMS_TOTAL = 120
MIN_TERMS_PER_DIMENSION = 8
MIN_TEMPLATES = 6


# ---------------------------------------------------------------- 结构校验


def test_schema_has_version_and_profiles():
    """知识库带 schema_version，且三个模型档案齐全。"""
    assert library.schema_version() >= 1
    assert tuple(library.profile_keys()) == EXPECTED_PROFILES

    for key in EXPECTED_PROFILES:
        profile = library.profile(key)
        assert profile["label"] and profile["description"]
        assert isinstance(profile["supports_negative"], bool)
        assert isinstance(profile["supports_weight"], bool)
        assert profile["separator"]
        assert profile["style"] in {"natural", "tags"}
        assert profile.get("structure_hint")

    assert library.profile("sd15_sdxl")["supports_negative"] is True
    assert library.profile("sd15_sdxl")["supports_weight"] is True
    assert library.profile("flux_flux2")["supports_negative"] is False
    assert library.profile("flux_flux2")["supports_weight"] is False
    assert library.profile("generic")["supports_weight"] is False


def test_unknown_profile_raises_invalid_input():
    with pytest.raises(AIBARError) as excinfo:
        library.profile("sd3")
    assert excinfo.value.code == "invalid_input"


def test_all_dimensions_present():
    """PRD M6.2 要求的 13 个维度全部存在且顺序稳定。"""
    assert tuple(library.dimension_keys()) == EXPECTED_DIMENSIONS
    for key in EXPECTED_DIMENSIONS:
        assert library.dimension_label(key) != key


def test_terms_count_and_shape():
    """词条总数不少于 120，每个维度至少 8 条，字段完整且 id 唯一。"""
    terms = library.terms()
    assert len(terms) >= MIN_TERMS_TOTAL

    per_dimension: dict[str, int] = {}
    seen: set[str] = set()
    for item in terms:
        assert item.id not in seen, f"词条 id 重复：{item.id}"
        seen.add(item.id)
        assert item.name and item.text and item.text_zh
        assert item.dimension in EXPECTED_DIMENSIONS
        assert item.profiles and set(item.profiles) <= set(EXPECTED_PROFILES)
        assert item.note and item.example
        assert isinstance(item.tags, tuple) and item.tags
        per_dimension[item.dimension] = per_dimension.get(item.dimension, 0) + 1

    for dimension in EXPECTED_DIMENSIONS:
        assert per_dimension.get(dimension, 0) >= MIN_TERMS_PER_DIMENSION, dimension

    # 负向词条只允许出现在支持负向提示词的档案里
    for item in terms:
        if item.dimension == "negative":
            assert item.profiles == ("sd15_sdxl",)


def test_terms_are_profile_aware():
    """sd15_sdxl 用英文标签，flux_flux2 用自然语言短句，generic 用中文。"""
    terms = [item for item in library.terms() if item.dimension != "negative"]
    assert terms
    flux_sentences = 0
    for item in terms:
        sd_text = item.insert_text("sd15_sdxl")
        flux_text = item.insert_text("flux_flux2")
        generic_text = item.insert_text("generic")
        assert sd_text and flux_text and generic_text
        # SD 标签不应带句号结尾，保持短语形态
        assert not sd_text.endswith(".")
        if " " in flux_text and len(flux_text.split()) >= 5:
            flux_sentences += 1
    assert flux_sentences >= 60

    sample = library.term("lighting.rim_light")
    assert sample is not None
    assert sample.insert_text("sd15_sdxl") == "rim lighting"
    assert "rim of light" in sample.insert_text("flux_flux2")
    assert "轮廓光" in sample.insert_text("generic")


def test_templates_are_complete():
    """至少 6 组场景模板，字段齐全且覆盖 PRD 列出的六个场景。"""
    templates = library.templates()
    assert len(templates) >= MIN_TEMPLATES
    ids = {item["id"] for item in templates}
    assert len(ids) == len(templates)
    for item in templates:
        for field in ("id", "name", "description", "profile", "positive", "negative", "dimensions"):
            assert field in item, f"{item['id']} 缺少 {field}"
        assert item["profile"] in EXPECTED_PROFILES
        assert item["positive"].strip()
        assert item["dimensions"]
        for dimension in item["dimensions"]:
            assert dimension in EXPECTED_DIMENSIONS
        if item["profile"] == "flux_flux2":
            assert item["negative"] == ""
        else:
            assert item["negative"].strip()

    names = "".join(str(item["name"]) for item in templates)
    for keyword in ("人像", "动漫", "电影", "产品", "室内", "海报"):
        assert keyword in names, keyword


def test_conflict_table_covers_required_pairs():
    """PRD 要求的内置互斥词对必须存在。"""
    conflicts = library.conflicts()
    assert conflicts
    pairs = {
        ("soft light", "hard light"),
        ("warm tone", "cool tone"),
        ("minimalist", "highly detailed"),
    }
    for left, right in pairs:
        hit = False
        for group in conflicts:
            keys = [str(key).lower() for key in group["keys"]]
            if any(left in key for key in keys) and any(right in key for key in keys):
                hit = True
                break
        assert hit, f"互斥词表缺少 {left} / {right}"


# ---------------------------------------------------------------- 查询


def test_query_by_dimension():
    terms = library.query_terms(dimension="lighting")
    assert terms
    assert {item.dimension for item in terms} == {"lighting"}
    assert len(terms) >= MIN_TERMS_PER_DIMENSION


def test_query_by_keyword_matches_name_text_and_tags():
    assert library.query_terms(q="轮廓光")
    assert library.query_terms(q="depth of field")
    assert library.query_terms(q="夜景")
    assert library.query_terms(q="绝对不存在的词条xyz") == []


def test_query_marks_applicability_by_profile():
    """不支持当前档案的词条仍返回，但 applicable 为 False。"""
    payload = [item.to_dict("flux_flux2") for item in library.query_terms(profile="flux_flux2")]
    negatives = [item for item in payload if item["dimension"] == "negative"]
    assert negatives
    assert all(item["applicable"] is False for item in negatives)

    positives = [item for item in payload if item["dimension"] != "negative"]
    assert positives
    assert all(item["applicable"] is True for item in positives)
    assert all(item["insert_text"] for item in positives)


def test_query_rejects_unknown_dimension_and_profile():
    with pytest.raises(AIBARError) as excinfo:
        library.query_terms(dimension="not-a-dimension")
    assert excinfo.value.code == "invalid_input"

    with pytest.raises(AIBARError):
        library.query_terms(profile="midjourney")


def test_stats_reports_library_size():
    stats = library.stats()
    assert stats["profiles"] == 3
    assert stats["dimensions"] == 13
    assert stats["terms"] >= MIN_TERMS_TOTAL
    assert stats["templates"] >= MIN_TEMPLATES
    assert library.term("subject.portrait_woman") is not None
    assert library.term("nope.nope") is None
