"""M6 规则扩写引擎、Provider 回退与路由冒烟测试。

隔离策略（HARNESS §7）：需要数据库的用例把 ``Config.DB_PATH`` 指向 pytest 的
``tmp_path``，切换后重置 ``core.db`` 的线程本地连接，确保不污染仓库里的 ``data/``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import requests
from flask import Flask

from config import Config
from core import db as core_db
from core.errors import AIBARError
from core.textutil import normalize_text
from prompt import engine, library, providers
from prompt.routes import bp

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

RESULT_KEYS = (
    "original_prompt",
    "expanded_positive",
    "expanded_negative",
    "profile",
    "intensity",
    "provider",
    "sections",
    "additions",
    "warnings",
    "duration_ms",
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与数据目录重定向到临时目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")

    # core.db 用线程本地连接，切换 DB_PATH 后必须丢弃旧连接
    core_db._local.conn = None
    core_db.migrate()

    yield {"data": data_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    core_db._local.conn = None


@pytest.fixture
def client(env):
    """现场构造 Flask 实例并只注册 prompt_bp，避免依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    with app.test_client() as test_client:
        yield test_client


def _payload(response) -> dict:
    body = response.get_json()
    assert body is not None
    return body


# ---------------------------------------------------------------- 确定性


def test_expand_is_deterministic():
    """相同输入与选项连续两次扩写，结果逐字符相同（duration_ms 除外）。"""
    first = engine.expand("一个女孩站在雨夜的街道上", "sd15_sdxl", "balanced")
    second = engine.expand("一个女孩站在雨夜的街道上", "sd15_sdxl", "balanced")
    first.pop("duration_ms")
    second.pop("duration_ms")
    assert first == second
    for key in RESULT_KEYS:
        if key != "duration_ms":
            assert key in first


def test_expand_is_deterministic_across_templates():
    """带场景模板时同样保持确定性。"""
    kwargs = {
        "original_prompt": "a lonely robot",
        "profile": "flux_flux2",
        "intensity": "creative",
        "template_id": "tpl.cinematic_scene",
        "options": {"enable_weight": True},
    }
    assert engine.expand(**kwargs)["expanded_positive"] == engine.expand(**kwargs)["expanded_positive"]


# ---------------------------------------------------------------- 保留意图


def test_original_subject_and_quantity_are_preserved():
    """原文主体与数量词在三种档案下都原样出现在结果开头。"""
    original = "three cats sitting on a wooden table"
    for profile in ("generic", "sd15_sdxl", "flux_flux2"):
        result = engine.expand(original, profile, "balanced")
        positive = result["expanded_positive"]
        assert positive.startswith(original), profile
        assert "three cats" in positive
        assert result["original_prompt"] == original


def test_user_specified_style_is_not_replaced():
    """用户明确指定的风格不会被其他风格词条替换。"""
    result = engine.expand("a young girl, anime illustration style", "sd15_sdxl", "creative")
    positive = result["expanded_positive"]
    assert "anime illustration style" in positive
    assert "photorealistic" not in positive
    assert "oil painting" not in positive


def test_no_repeated_terms_in_output():
    """结果中不出现重复的插入片段。"""
    result = engine.expand("a girl standing in a rainy neon alley at night", "sd15_sdxl", "creative")
    pieces = [piece.strip().lower() for piece in result["expanded_positive"].split(",")]
    pieces = [piece for piece in pieces if piece]
    assert len(pieces) == len(set(pieces))


# ---------------------------------------------------------------- 三档强度


def test_intensity_scales_additions():
    """creative 的新增数量严格多于 conservative，balanced 介于两者之间。"""
    conservative = engine.expand("a girl", "generic", "conservative")
    balanced = engine.expand("a girl", "generic", "balanced")
    creative = engine.expand("a girl", "generic", "creative")

    assert len(conservative["additions"]) >= 1
    assert len(balanced["additions"]) >= len(conservative["additions"])
    assert len(creative["additions"]) > len(conservative["additions"])


def test_conservative_does_not_add_narrative_elements():
    """保守档只补光线/构图，不引入环境、氛围或风格。"""
    result = engine.expand("a girl", "generic", "conservative")
    dimensions = {
        section["dimension"] for section in result["sections"] if section["is_new"]
    }
    assert dimensions <= {"lighting", "composition"}


def test_typography_terms_only_added_with_user_intent():
    """文字排版词条只在用户明确提到文字/海报/标题时才补充。"""
    plain = engine.expand("a minimalist landscape", "sd15_sdxl", "creative")
    assert not [item for item in plain["additions"] if "[文字排版]" in item]

    poster = engine.expand("a movie poster with a big title", "sd15_sdxl", "creative")
    assert any("[文字排版]" in item for item in poster["additions"])


# ---------------------------------------------------------------- 模型档案


def test_flux_returns_empty_negative_with_warning():
    result = engine.expand("a girl walking in the rain", "flux_flux2", "balanced")
    assert result["expanded_negative"] == ""
    assert any("FLUX.2 不支持负向提示词" in item for item in result["warnings"])


def test_sd15_returns_non_empty_negative():
    result = engine.expand("a girl walking in the rain", "sd15_sdxl", "balanced")
    assert result["expanded_negative"].strip()
    assert "blurry" in result["expanded_negative"]
    # 负向词条不得进入正向提示词
    assert "blurry" not in result["expanded_positive"]


def test_generic_does_not_emit_weight_syntax():
    result = engine.expand("一个女孩", "generic", "creative", options={"enable_weight": True})
    assert "(" not in result["expanded_positive"] and ":" not in result["expanded_positive"]
    assert any("不支持权重语法" in item for item in result["warnings"])


def test_sd15_supports_optional_weight_syntax():
    plain = engine.expand("a girl", "sd15_sdxl", "balanced")
    weighted = engine.expand("a girl", "sd15_sdxl", "balanced", options={"enable_weight": True})
    assert plain["expanded_positive"] != weighted["expanded_positive"]
    assert re.search(r"\(([^()]+):\d\.\d{2}\)", weighted["expanded_positive"])
    # 权重只作用于新增内容，原文保持不变
    assert weighted["expanded_positive"].startswith("a girl")


# ---------------------------------------------------------------- 冲突与去重


def test_conflict_table_detects_required_pairs():
    corpus_soft = normalize_text("portrait with soft light")
    assert engine._conflict_group(["hard light"], corpus_soft) is not None
    corpus_warm = normalize_text("warm tone portrait")
    assert engine._conflict_group(["cool tone"], corpus_warm) is not None
    corpus_minimal = normalize_text("a minimalist poster")
    assert engine._conflict_group(["highly detailed"], corpus_minimal) is not None
    # 无冲突时返回 None
    assert engine._conflict_group(["rim lighting"], corpus_soft) is None


def test_soft_light_input_does_not_add_hard_light():
    result = engine.expand("a girl, soft light", "sd15_sdxl", "creative")
    positive = result["expanded_positive"].lower()
    assert "soft light" in positive
    assert "hard light" not in positive


def test_existing_content_is_not_duplicated():
    """原文已有的描述不会被重复添加。"""
    result = engine.expand("a girl, sharp focus, shallow depth of field", "sd15_sdxl", "balanced")
    positive_lower = result["expanded_positive"].lower()
    assert positive_lower.count("sharp focus") == 1
    assert positive_lower.count("shallow depth of field") == 1


# ---------------------------------------------------------------- 中文输入


def test_chinese_input_maps_to_english_tags_for_sd15():
    result = engine.expand("一个女孩站在雨夜的街道上", "sd15_sdxl", "balanced")
    positive = result["expanded_positive"]
    assert "girl" in positive
    assert "rainy night" in positive
    assert "street" in positive
    assert not _CJK_RE.search(positive)


def test_untranslatable_proper_noun_is_kept_and_warned():
    """无法可靠翻译的专有名词保留原文，并提示用户检查。"""
    result = engine.expand("麒麟站在山脊上", "sd15_sdxl", "conservative")
    assert "麒麟" in result["expanded_positive"]
    assert any("无法可靠翻译" in item for item in result["warnings"])


def test_chinese_input_kept_verbatim_for_generic_and_flux():
    for profile in ("generic", "flux_flux2"):
        result = engine.expand("一个女孩站在雨夜的街道上", profile, "balanced")
        assert result["expanded_positive"].startswith("一个女孩站在雨夜的街道上")


# ---------------------------------------------------------------- 输入校验


@pytest.mark.parametrize(
    "bad_prompt",
    ["", "   ", "。。。", "!!!", "，；、", "x" * 2001],
)
def test_invalid_prompt_is_rejected_without_calling_engine(bad_prompt, monkeypatch):
    """非法输入返回 400，且绝不触达扩写流程内部。"""

    def boom(*_args, **_kwargs):
        raise AssertionError("非法输入不应触达扩写流程")

    # 只要走到维度识别就说明校验被绕过
    monkeypatch.setattr(engine, "_detect_dimensions", boom)
    with pytest.raises(AIBARError) as excinfo:
        engine.expand(bad_prompt, "generic", "balanced")
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.status == 400


def test_unknown_intensity_and_profile_are_rejected(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("非法输入不应触达扩写流程")

    monkeypatch.setattr(engine, "_detect_dimensions", boom)

    with pytest.raises(AIBARError) as excinfo:
        engine.expand("a girl", "generic", "ultra")
    assert excinfo.value.code == "invalid_input"

    with pytest.raises(AIBARError):
        engine.expand("a girl", "midjourney", "balanced")

    with pytest.raises(AIBARError):
        engine.expand("a girl", "generic", "balanced", template_id="tpl.not-exist")

    with pytest.raises(AIBARError):
        engine.expand("a girl", "generic", "balanced", options=[1, 2])


def test_validate_input_accepts_legal_values():
    engine.validate_input("a girl", "sd15_sdxl", "creative", "tpl.realistic_portrait", {})


# ---------------------------------------------------------------- Provider 回退


class _FakeResponse:
    def __init__(self, content: str, status_code: int = 200):
        self._content = content
        self.status_code = status_code

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


def _enable_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", True)
    monkeypatch.setattr(Config, "AI_PROVIDER_ENDPOINT", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(Config, "AI_PROVIDER_MODEL", "test-model")


def test_openai_provider_disabled_falls_back_to_rules(monkeypatch):
    """未启用时自动回退规则引擎，请求整体不失败。"""
    monkeypatch.setattr(Config, "AI_PROVIDER_ENABLED", False)
    result = providers.expand("a girl", "generic", "balanced", provider="openai_compatible")
    assert result["provider"] == "rules"
    assert result["requested_provider"] == "openai_compatible"
    assert any("回退" in item for item in result["warnings"])
    assert result["expanded_positive"]


def test_openai_provider_timeout_falls_back_to_rules(monkeypatch):
    _enable_ai_provider(monkeypatch)

    def boom(*_args, **_kwargs):
        raise requests.exceptions.Timeout("too slow")

    monkeypatch.setattr(requests, "post", boom)
    result = providers.expand("a girl", "generic", "balanced", provider="openai_compatible")
    assert result["provider"] == "rules"
    assert any("超时" in item or "回退" in item for item in result["warnings"])


def test_openai_provider_bad_schema_falls_back_to_rules(monkeypatch):
    _enable_ai_provider(monkeypatch)
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: _FakeResponse("这不是 JSON"))

    result = providers.expand("a girl", "generic", "balanced", provider="openai_compatible")
    assert result["provider"] == "rules"
    assert any("回退" in item for item in result["warnings"])


def test_openai_provider_success_is_used(monkeypatch):
    """合法响应用 AI 结果，并且密钥不出现在返回值里。"""
    _enable_ai_provider(monkeypatch)
    monkeypatch.setattr(Config, "AI_PROVIDER_API_KEY", "sk-should-not-leak")
    captured: dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        # 注意：形参名为 json，会遮蔽模块级的 json，故此处用局部别名
        import json as _json

        captured["url"] = url
        captured["headers"] = headers or {}
        captured["body"] = json
        return _FakeResponse(
            _json.dumps(
                {
                    "expanded_positive": "a girl, rim lighting, shallow depth of field",
                    "expanded_negative": "blurry",
                    "additions": ["补充轮廓光"],
                    "warnings": [],
                },
                ensure_ascii=False,
            )
        )

    monkeypatch.setattr(requests, "post", fake_post)
    result = providers.expand("a girl", "sd15_sdxl", "balanced", provider="openai_compatible")

    assert result["provider"] == "openai_compatible"
    assert result["expanded_positive"] == "a girl, rim lighting, shallow depth of field"
    assert "sk-should-not-leak" not in json.dumps(result, ensure_ascii=False)
    assert captured["headers"].get("Authorization") == "Bearer sk-should-not-leak"
    assert "a girl" in captured["body"]["messages"][1]["content"]


def test_unknown_provider_is_rejected():
    with pytest.raises(AIBARError) as excinfo:
        providers.expand("a girl", "generic", "balanced", provider="gemini")
    assert excinfo.value.code == "invalid_input"


# ---------------------------------------------------------------- 路由冒烟


def test_route_prompt_library(client):
    body = _payload(client.get("/api/prompt-library?profile=sd15_sdxl&dimension=lighting"))
    assert body["ok"] is True
    data = body["data"]
    assert data["schema_version"] >= 1
    assert {item["key"] for item in data["profiles"]} == set(library.profile_keys())
    assert [item["key"] for item in data["dimensions"]] == ["lighting"]
    assert data["dimensions"][0]["terms"]
    assert all(term["applicable"] for term in data["dimensions"][0]["terms"])
    assert len(data["templates"]) >= 6


def test_route_prompt_library_marks_inapplicable_terms(client):
    body = _payload(client.get("/api/prompt-library?profile=flux_flux2&q=模糊"))
    data = body["data"]
    flat = [term for group in data["dimensions"] for term in group["terms"]]
    terms = [term for term in flat if term["id"] == "negative.blurry_lowres"]
    assert terms and terms[0]["applicable"] is False


def test_route_expand_and_history_crud(client):
    expand_body = _payload(
        client.post(
            "/api/prompts/expand",
            json={
                "original_prompt": "一个女孩站在雨夜的街道上",
                "profile": "sd15_sdxl",
                "intensity": "creative",
                "provider": "rules",
            },
        )
    )
    assert expand_body["ok"] is True
    result = expand_body["data"]
    for key in RESULT_KEYS:
        assert key in result, key
    assert result["profile"] == "sd15_sdxl"
    assert result["sections"]
    assert any(section["is_new"] for section in result["sections"])

    saved = _payload(client.post("/api/prompts/history", json=result))
    assert saved["ok"] is True
    record_id = saved["data"]["id"]
    assert saved["data"]["original_prompt"] == result["original_prompt"]

    listed = _payload(client.get("/api/prompts/history?limit=10"))
    assert listed["data"]["total"] == 1
    assert listed["data"]["items"][0]["id"] == record_id
    assert json.loads(json.dumps(listed["data"]["items"][0]["sections"]))

    favored = _payload(client.put(f"/api/prompts/history/{record_id}/favorite", json={"favorite": True}))
    assert favored["data"]["is_favorite"] is True

    removed = _payload(client.delete(f"/api/prompts/history/{record_id}"))
    assert removed["data"]["deleted"] is True
    assert _payload(client.get("/api/prompts/history"))["data"]["total"] == 0


def test_route_expand_rejects_invalid_input_without_calling_engine(client, monkeypatch):
    """空输入/仅标点/超长/未知强度都应返回 400，且不调用引擎。"""

    def boom(*_args, **_kwargs):
        raise AssertionError("非法输入不应调用扩写引擎")

    monkeypatch.setattr(engine, "expand", boom)

    bad_bodies = [
        {"original_prompt": "   "},
        {"original_prompt": "。。。"},
        {"original_prompt": "x" * 2001},
        {"original_prompt": "a girl", "intensity": "ultra"},
        {"original_prompt": "a girl", "profile": "midjourney"},
        {"profile": "generic"},
    ]
    for body in bad_bodies:
        response = client.post("/api/prompts/expand", json=body)
        payload = _payload(response)
        assert response.status_code == 400, body
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_input"


def test_route_history_validates_limit_and_missing_id(client):
    assert _payload(client.get("/api/prompts/history?limit=999"))["error"]["code"] == "invalid_input"
    assert _payload(client.delete("/api/prompts/history/404"))["error"]["code"] == "not_found"
    assert _payload(client.put("/api/prompts/history/404/favorite", json={}))["error"]["code"] == "not_found"


def test_route_unexpected_exception_becomes_internal_error(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(providers, "expand", boom)

    response = client.post("/api/prompts/expand", json={"original_prompt": "a girl"})
    payload = _payload(response)
    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["error"]["code"] == "internal_error"
    assert "boom" not in json.dumps(payload, ensure_ascii=False)
