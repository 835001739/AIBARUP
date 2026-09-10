"""M10 / M11 Provider 中心测试：状态枚举、健康缓存、自动选择链、外发授权、VLM 队列、分层测试。

测试不使用真实 ComfyUI：除「内嵌元数据」外，Provider 一律用可观测的替身注册进
``registry``；需要验证真实实现的场景（OpenAI-compatible 外发授权、节点契约工具）
全部通过 monkeypatch 拦截网络与 ``/object_info``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from config import Config
from core import db as core_db
from core.db import query_all, query_one
from core.errors import AIBARError
from reverse import comfyui_client, schema
from reverse.providers import base, registry
from reverse.providers.comfyui_blip import BLIP_MAX_CONFIDENCE, PRECISION_PARAMS as BLIP_PRECISION
from reverse.providers.metadata import MetadataProvider
from reverse.providers.openai_compatible import (
    create_openai_compatible_providers,
    provider_fingerprint,
)
from reverse.providers.qwen3vl import PRECISION_PARAMS as QWEN_PRECISION, extract_json_object

EXTERNAL_JSON = json.dumps(
    {
        "sections": [
            {"dimension": "光影", "text": "逆光形成金色轮廓光", "confidence": 0.82},
        ],
        "negative": "模糊",
        "warnings": [],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------- 环境


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")

    core_db._local.conn = None
    core_db.migrate()

    yield {"data": data_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 仅清理
            pass
    core_db._local.conn = None


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后都重置注册表、健康缓存与 VLM 队列，避免互相污染。"""
    registry.reset_registry()
    registry.reset_health_cache()
    base.reset_vlm_queue()
    yield
    registry.reset_registry()
    registry.reset_health_cache()
    base.reset_vlm_queue()


# ---------------------------------------------------------------- 替身


class CountingProvider(base.VisionProvider):
    """只记录调用次数的健康 Provider。"""

    key = "counting"
    label = "计数 Provider"
    local = True
    model = "counting-model"

    def __init__(self) -> None:
        self.probes = 0
        self.analyses = 0

    def probe(self) -> dict[str, Any]:
        self.probes += 1
        return base.status_payload(base.STATUS_READY, model=self.model)

    def analyze_image(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.analyses += 1
        raise AssertionError("检测阶段不得启动真实图片分析")


class BrokenProvider(base.VisionProvider):
    """探测时抛出可归类业务异常的 Provider。"""

    key = "broken"
    label = "异常 Provider"
    local = True
    model = "broken-model"

    def probe(self) -> dict[str, Any]:
        raise AIBARError("missing_node", "缺少节点")


class CrashingProvider(base.VisionProvider):
    """探测时抛出未预期异常的 Provider（不得冒泡）。"""

    key = "crashing"
    label = "崩溃 Provider"
    local = True
    model = "crashing-model"

    def probe(self) -> dict[str, Any]:
        raise RuntimeError("boom")


class AdvancedProvider(base.VisionProvider):
    """占用高级 VLM 单任务队列的 Provider。"""

    key = "advanced_fake"
    label = "高级 VLM 替身"
    local = True
    advanced_vlm = True
    model = "advanced-fake"
    quality_tier = schema.QUALITY_ADVANCED

    def probe(self) -> dict[str, Any]:
        return base.status_payload(base.STATUS_READY, model=self.model)

    def analyze_image(
        self,
        image_ref: dict[str, Any],
        profile: str = "generic",
        precision: str = base.DEFAULT_PRECISION,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with base.vlm_slot(self.key, job=(options or {}).get("job_id")):
            return schema.validate_structured_result(
                {
                    "source_type": schema.SOURCE_VISION,
                    "provider": self.key,
                    "provider_model": self.model,
                    "model_profile": profile,
                    "precision": precision if precision in schema.PRECISIONS else schema.DEFAULT_PRECISION,
                    "sections": [
                        schema.make_section(
                            0,
                            "lighting",
                            "逆光形成金色轮廓光",
                            schema.cap_confidence(0.8, schema.SOURCE_VISION),
                            evidence="来自高级 VLM 替身",
                        )
                    ],
                    "warnings": [],
                    "duration_ms": 1,
                    "quality_tier": schema.QUALITY_ADVANCED,
                }
            )


# ---------------------------------------------------------------- 状态枚举


def test_status_enum_has_exactly_ten_values():
    assert len(base.STATUSES) == 10
    assert set(base.STATUSES) == {
        "ready",
        "unconfigured",
        "offline",
        "missing_node",
        "missing_model",
        "missing_mmproj",
        "incompatible_runtime",
        "out_of_memory",
        "unauthorized",
        "error",
    }
    assert all(base.status_label(status) for status in base.STATUSES)
    assert base.status_payload(base.STATUS_READY)["available"] is True
    assert all(
        base.status_payload(status)["available"] is False
        for status in base.STATUSES
        if status != base.STATUS_READY
    )


def test_classify_error_maps_comfyui_codes_to_states():
    offline = base.classify_error(comfyui_client.ComfyUIError("comfyui_offline", "连不上"))
    assert offline[0] == base.STATUS_OFFLINE and offline[1] == "comfyui_offline"

    missing = base.classify_error(
        comfyui_client.ComfyUIError("missing_mmproj", "缺少视觉投影文件")
    )
    assert missing[0] == base.STATUS_MISSING_MMPROJ

    oom = base.classify_error(AIBARError("out_of_memory", "显存不足"))
    assert oom[0] == base.STATUS_OUT_OF_MEMORY

    unknown = base.classify_error(RuntimeError("boom"))
    assert unknown[0] == base.STATUS_ERROR and unknown[1] == "internal_error"


def test_status_and_reason_texts_are_actionable():
    for reason in (
        "comfyui_offline",
        "missing_node",
        "missing_model",
        "missing_mmproj",
        "incompatible_runtime",
        "out_of_memory",
        "unauthorized",
        "workflow_failed",
        "comfyui_timeout",
        "provider_offline",
    ):
        assert base.reason_text(reason) != base.reason_text("no_such_reason"), reason


# ---------------------------------------------------------------- 健康探测与缓存


def test_probe_result_is_cached_until_refresh(env, monkeypatch):
    provider = CountingProvider()
    registry.register_provider(provider)
    monkeypatch.setattr(registry, "HEALTH_TTL_SECONDS", 60.0)

    assert registry.provider_health("counting")["status"] == base.STATUS_READY
    registry.provider_health("counting")
    registry.list_providers()
    assert provider.probes == 1, "TTL 内复用缓存，不重复探测"

    registry.refresh()
    assert provider.probes == 2, "refresh 必须绕过缓存重新探测"

    monkeypatch.setattr(registry, "HEALTH_TTL_SECONDS", 0.0)
    registry.provider_health("counting")
    assert provider.probes == 3, "缓存过期后重新探测"


def test_probe_failures_are_normalized(env):
    registry.register_provider(BrokenProvider())
    registry.register_provider(CrashingProvider())

    broken = registry.provider_health("broken")
    assert broken["status"] == base.STATUS_MISSING_NODE
    assert broken["available"] is False
    assert broken["reason_code"] == "missing_node"
    assert broken["reason"]
    assert broken["status_label"] == base.STATUS_LABELS[base.STATUS_MISSING_NODE]

    crashing = registry.provider_health("crashing")
    assert crashing["status"] == base.STATUS_ERROR
    assert crashing["reason_code"] == "internal_error"


def test_detection_never_starts_real_analysis(env):
    provider = CountingProvider()
    registry.register_provider(provider)

    registry.refresh()
    registry.list_providers(force=True)
    registry.test_provider("counting")
    registry.auto_chain()
    registry.chain_status()

    assert provider.probes >= 1
    assert provider.analyses == 0, "检测/刷新/分层测试都不得启动真实图片分析"


# ---------------------------------------------------------------- 列表与隐私


def test_list_providers_hides_secrets_and_endpoints(env, monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_VISION_ENABLED", True)
    monkeypatch.setattr(Config, "OPENAI_VISION_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setattr(Config, "OPENAI_VISION_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(Config, "OPENAI_VISION_API_KEY", "sk-super-secret")

    registry.register_provider(MetadataProvider())
    for provider in create_openai_compatible_providers():
        registry.register_provider(provider)

    payload = registry.list_providers()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "sk-super-secret" not in serialized, "密钥绝不出现在列表响应里"
    assert "api.example.com" not in serialized, "完整 endpoint 绝不出现在列表响应里"

    required = (
        "key",
        "label",
        "local",
        "quality_tier",
        "status",
        "available",
        "reason_code",
        "reason",
        "model",
        "is_default",
        "capabilities",
        "last_checked_at",
    )
    for item in payload["items"]:
        for field in required:
            assert field in item, field
        assert "endpoint" not in item
        assert "api_key" not in item

    external = next(item for item in payload["items"] if item["key"] == "openai_compatible_vision")
    assert external["requires_consent"] is True
    assert external["status"] == base.STATUS_UNAUTHORIZED
    assert external["available"] is False

    # API Key 只在 Provider 内部可读，不写库、不进日志、不返前端
    provider = registry.get_provider("openai_compatible_vision")
    assert provider.api_key() == "sk-super-secret"
    assert query_one("SELECT * FROM prompt_reverse_consents") is None


# ---------------------------------------------------------------- 自动选择链


def test_auto_chain_is_fixed_and_respects_consent(env, monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_VISION_ENABLED", True)
    monkeypatch.setattr(Config, "OPENAI_VISION_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setattr(Config, "OPENAI_VISION_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(Config, "OPENAI_VISION_API_KEY", "sk-test")

    chain = registry.auto_chain()
    assert chain[0] == "metadata", "自动链固定从内嵌元数据开始"
    assert chain[1 : 1 + len(registry.AUTO_CHAIN_FIXED)] == list(registry.AUTO_CHAIN_FIXED)
    assert "openai_compatible_vision" not in chain, "未授权的外部 Provider 不得进入自动链"

    registry.set_consent("openai_compatible_vision", True)
    authorized = registry.auto_chain()
    assert authorized[-1] == "openai_compatible_vision", "已授权外部只排在链尾"
    assert set(authorized) - set(chain) == {"openai_compatible_vision"}

    registry.set_default("local_vision")
    with_default = registry.auto_chain()
    assert with_default[0] == "metadata"
    assert with_default[1] == "local_vision", "用户默认 Provider 紧跟内嵌元数据"
    assert "local_vision" not in with_default[2:]


# ---------------------------------------------------------------- 外发确认


def test_external_provider_never_sends_without_consent(env, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "OPENAI_VISION_ENABLED", True)
    monkeypatch.setattr(Config, "OPENAI_VISION_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setattr(Config, "OPENAI_VISION_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(Config, "OPENAI_VISION_API_KEY", "sk-test")

    def forbidden(*args: Any, **kwargs: Any):
        raise AssertionError("未完成外发确认前不得发起任何网络请求")

    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)

    for provider in create_openai_compatible_providers():
        registry.register_provider(provider)
    external = registry.get_provider("openai_compatible_vision")

    assert external.probe()["status"] == base.STATUS_UNAUTHORIZED
    assert external.probe()["available"] is False

    image = tmp_path / "sample.png"
    image.write_bytes(b"fake-image-bytes")
    reference = {"path": str(image), "image_id": None, "upload_id": "u1", "content_hash": "c1"}

    with pytest.raises(AIBARError) as excinfo:
        external.analyze_image(reference, "generic", "standard", {})
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.status == 403

    # 确认外发后才会真正发起请求
    registry.set_consent("openai_compatible_vision", True)
    sent: list[str] = []
    monkeypatch.setattr(
        external,
        "_chat_completions",
        lambda settings, image_bytes, precision: sent.append(precision) or EXTERNAL_JSON,
    )

    result = external.analyze_image(reference, "generic", "fast", {})
    assert sent == ["fast"]
    assert result["source_type"] == schema.SOURCE_VISION
    assert result["sections"], "确认后应正常产出结构化结果"


def test_consent_fingerprint_invalidates_on_change(env, monkeypatch):
    monkeypatch.setattr(Config, "OPENAI_VISION_ENABLED", True)
    monkeypatch.setattr(Config, "OPENAI_VISION_ENDPOINT", "https://api.example.com/v1")
    monkeypatch.setattr(Config, "OPENAI_VISION_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(Config, "OPENAI_VISION_API_KEY", "sk-test")

    for provider in create_openai_compatible_providers():
        registry.register_provider(provider)

    status = registry.consent_status("openai_compatible_vision")
    assert status["requires_consent"] is True
    assert status["consented"] is False
    assert len(status["fingerprint"]) == 32
    assert "api.example.com" not in status["fingerprint"]

    granted = registry.set_consent("openai_compatible_vision", True)
    assert granted["consented"] is True
    row = query_one("SELECT * FROM prompt_reverse_consents WHERE provider_key=?", ("openai_compatible_vision",))
    assert row is not None and row["consented_at"]
    assert "sk-test" not in str(row["provider_fingerprint"]), "确认记录只存指纹，不存密钥"

    # 换模型即换服务标识，必须重新确认
    monkeypatch.setattr(Config, "OPENAI_VISION_MODEL", "gpt-4o")
    assert registry.consent_status("openai_compatible_vision")["consented"] is False

    registry.set_consent("openai_compatible_vision", False)
    assert registry.consent_status("openai_compatible_vision")["consented"] is False
    assert query_one("SELECT * FROM prompt_reverse_consents") is None

    local = registry.consent_status("local_vision")
    assert local["requires_consent"] is False
    assert local["consented"] is True

    assert provider_fingerprint("https://api.example.com/v1", "m") == provider_fingerprint(
        "https://api.example.com/other", "m"
    )
    assert provider_fingerprint("https://api.example.com/v1", "m") != provider_fingerprint(
        "https://api.example.com/v1", "other"
    )


# ---------------------------------------------------------------- 高级 VLM 队列


def test_vlm_queue_allows_only_one_advanced_task():
    base.reset_vlm_queue()

    assert base.acquire_vlm_slot("comfyui_qwen3vl_8b", job=1) is True
    assert base.acquire_vlm_slot("comfyui_qwen3vl_4b", job=2) is False

    state = base.vlm_queue_state()
    assert state["busy"] is True
    assert state["provider"] == "comfyui_qwen3vl_8b"
    assert state["job_id"] == 1
    assert state["queued"] == 1
    assert state["max_parallel"] == 1

    with pytest.raises(AIBARError) as excinfo:
        with base.vlm_slot("comfyui_qwen3vl_4b", job=2):
            pass
    assert excinfo.value.code == "provider_busy"
    assert excinfo.value.status == 503

    # key 不匹配时不得误释放别人的槽位
    base.release_vlm_slot("comfyui_qwen3vl_4b")
    assert base.vlm_queue_state()["busy"] is True
    base.release_vlm_slot("comfyui_qwen3vl_8b")
    assert base.vlm_queue_state()["busy"] is False


def test_advanced_provider_rejects_parallel_analysis_and_releases_slot(env):
    provider = AdvancedProvider()
    registry.register_provider(provider)

    assert base.acquire_vlm_slot("other_provider", job=99) is True
    with pytest.raises(AIBARError) as excinfo:
        provider.analyze_image({"image_id": "x"}, "generic", "standard", {})
    assert excinfo.value.code == "provider_busy", "高级 VLM 全局同时只允许一个任务"

    base.release_vlm_slot("other_provider")
    result = provider.analyze_image({"image_id": "x"}, "generic", "standard", {"job_id": 7})
    assert result["sections"]
    assert base.vlm_queue_state()["busy"] is False, "分析结束必须立即释放槽位"


# ---------------------------------------------------------------- 分层测试


def test_test_provider_returns_three_layers(env):
    registry.register_provider(MetadataProvider())

    payload = registry.test_provider("metadata")
    assert payload["key"] == "metadata"
    assert payload["ok"] is True
    assert [layer["name"] for layer in payload["layers"]] == list(registry.LAYER_NAMES)
    assert all(layer["available"] for layer in payload["layers"])
    assert {layer["status"] for layer in payload["layers"]} == {base.STATUS_READY}
    assert all("status_label" in layer and "reason_code" in layer for layer in payload["layers"])


def test_test_provider_stops_at_failing_layer(env):
    registry.register_provider(BrokenProvider())

    payload = registry.test_provider("broken")
    layers = {layer["name"]: layer for layer in payload["layers"]}

    assert payload["ok"] is False
    assert layers["service"]["available"] is False
    assert layers["service"]["reason_code"] == "missing_node"
    assert layers["model"]["status"] == "pending", "前一层不通时后续层标记为未执行"
    assert layers["inference"]["status"] == "pending"


def test_test_provider_normalizes_unexpected_errors(env):
    registry.register_provider(CrashingProvider())

    payload = registry.test_provider("crashing")
    assert payload["ok"] is False
    assert payload["layers"][0]["reason_code"] == "internal_error"
    assert len(payload["layers"]) == len(registry.LAYER_NAMES)


def test_unknown_provider_key_raises_not_found(env):
    registry.register_provider(MetadataProvider())
    with pytest.raises(AIBARError) as excinfo:
        registry.get_provider("no_such_provider")
    assert excinfo.value.code == "provider_not_found"
    assert excinfo.value.status == 404


# ---------------------------------------------------------------- 默认偏好


def test_set_default_persists_only_provider_key(env):
    registry.register_provider(MetadataProvider())
    assert registry.default_provider_key() == registry.AUTO_KEY

    payload = registry.set_default("metadata")
    assert payload["default_provider"] == "metadata"
    assert payload["auto_enabled"] is False

    rows = query_all("SELECT provider_key, is_default FROM prompt_provider_preferences")
    assert [(row["provider_key"], row["is_default"]) for row in rows] == [("metadata", 1)]

    listed = registry.list_providers()
    assert [item["key"] for item in listed["items"] if item["is_default"]] == ["metadata"]
    assert listed["auto_enabled"] is False

    restored = registry.set_default(registry.AUTO_KEY)
    assert restored["default_provider"] == registry.AUTO_KEY
    assert restored["auto_enabled"] is True
    # 只保留一条默认偏好，不残留多条 is_default=1
    assert sum(row["is_default"] for row in query_all("SELECT is_default FROM prompt_provider_preferences")) == 1

    with pytest.raises(AIBARError) as excinfo:
        registry.set_default("no_such_provider")
    assert excinfo.value.code == "provider_not_found"


# ---------------------------------------------------------------- 精度与节点契约


def test_precision_maps_cover_all_levels():
    assert base.PRECISIONS == ("fast", "standard", "fine")
    assert set(base.PRECISION_LABELS) == set(base.PRECISIONS)
    assert base.normalize_precision("bogus") == base.DEFAULT_PRECISION

    assert set(BLIP_PRECISION) == set(base.PRECISIONS)
    assert BLIP_PRECISION["fast"]["mode"] == "Natural"
    assert BLIP_PRECISION["fast"]["max_tokens"] == 40
    assert BLIP_PRECISION["fast"]["num_beams"] == 1
    assert BLIP_PRECISION["standard"]["mode"] == "Detailed"
    assert BLIP_PRECISION["standard"]["max_tokens"] == 80
    assert BLIP_PRECISION["standard"]["num_beams"] == 3
    assert BLIP_PRECISION["fine"]["max_tokens"] == 140
    assert BLIP_PRECISION["fine"]["num_beams"] == 5
    assert BLIP_MAX_CONFIDENCE == 0.80

    assert set(QWEN_PRECISION) == set(base.PRECISIONS)
    assert (
        QWEN_PRECISION["fast"]["max_tokens"]
        < QWEN_PRECISION["standard"]["max_tokens"]
        < QWEN_PRECISION["fine"]["max_tokens"]
    )


def test_node_input_spec_merges_required_and_optional():
    info = {
        "input": {
            "required": {"image": ["IMAGE"]},
            "optional": {"mode": [["Natural", "Detailed"]]},
        },
        "output": ["STRING"],
    }
    assert base.node_input_spec(info) == {"image": ["IMAGE"], "mode": [["Natural", "Detailed"]]}
    assert base.node_input_spec(None) == {}
    assert base.find_input_of_type(info, "image") == "image"
    assert base.find_input_of_type(info, "mask") is None
    assert base.node_output_types(info) == ["STRING"]
    assert base.node_output_names({"output_name": ["TEXT"]}) == ["TEXT"]


def test_build_inputs_never_invents_missing_inputs():
    info = {
        "input": {
            "required": {
                "image": ["IMAGE"],
                "mode": [["Natural", "Detailed"]],
                "max_tokens": ["INT", {"default": 80}],
                "unload_after": [["Enable", "Disable"]],
            }
        }
    }
    built = base.build_inputs(
        info,
        {
            "mode": ["Detailed", "Natural"],
            "max_tokens": 120,
            "unload_after": "Enable",
            "not_in_node": "必须被忽略",
        },
    )
    assert built == {"mode": "Detailed", "max_tokens": 120, "unload_after": "Enable"}


def test_find_option_does_not_fall_back_to_first_candidate():
    assert base.find_option([["Detailed", "Natural"]], "Detailed") == "Detailed"
    assert base.find_option([["Fast", "Slow"]], "Detailed") is None, "节点里没有目标模型时不得回退"
    assert base.find_option(["INT", {"default": 1}], "Detailed") is None
    assert base.pick_enum([["Fast", "Slow"]], "Detailed") == "Fast", "pick_enum 才允许回退"


def test_discover_node_class_uses_object_info_only():
    all_nodes = {
        "LoadImage": {"input": {"required": {"image": [["a.png", "b.png"]]}}, "output": ["IMAGE"]},
        "Qwen3VL_Interrogator": {
            "input": {"required": {"image": ["IMAGE"], "model": [["A", "B"]]}},
            "output": ["STRING"],
        },
        "UnrelatedTextNode": {"input": {"required": {"text": ["STRING"]}}, "output": ["STRING"]},
    }
    found = base.discover_node_class(all_nodes, include=("qwen", "vl"), exclude=("loader",))
    assert found == "Qwen3VL_Interrogator"

    assert base.discover_node_class(all_nodes, include=("nope",)) is None
    assert base.discover_node_class({"NoImageInput": {"input": {"required": {"text": ["STRING"]}}}},
                                    include=("noimage",)) is None


def test_graph_and_link_build_minimal_workflow():
    workflow = base.graph(
        ("1", "LoadImage", {"image": "aibar_reverse_abc.png"}),
        ("2", "Qwen3VL_Interrogator", {"image": base.link("1", 0)}),
        ("3", "PreviewAny", {"source": base.link("2", 0)}),
    )
    assert workflow["2"]["class_type"] == "Qwen3VL_Interrogator"
    assert workflow["2"]["inputs"]["image"] == ["1", 0]
    assert workflow["3"]["inputs"]["source"] == ["2", 0]


# ---------------------------------------------------------------- 宽容 JSON 提取


def test_extract_json_object_tolerates_fences_and_noise():
    fenced = f"好的，结果如下：\n```json\n{EXTERNAL_JSON}\n```\n以上。"
    assert extract_json_object(fenced) == json.loads(EXTERNAL_JSON)

    noisy = f"前缀废话 {EXTERNAL_JSON} 后缀废话"
    assert extract_json_object(noisy) == json.loads(EXTERNAL_JSON)

    assert extract_json_object("") is None
    assert extract_json_object("完全没有 JSON") is None
    assert extract_json_object('{"a": 1') is None

    # 字符串内的花括号不得被误判为对象结束（强制走切片分支）
    tricky = '前缀 {"sections": [{"text": "花括号 } 在字符串里"}]} 后缀'
    assert extract_json_object(tricky)["sections"][0]["text"] == "花括号 } 在字符串里"
