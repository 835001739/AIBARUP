"""M2/M3 解析层单元测试：不依赖数据库，纯函数级验证。

覆盖：
- UI 格式（nodes/links）与 API 格式（class_type/inputs）双解析；
- 正负提示词判定：标题优先 → KSampler negative 连线追溯 → 兜底；
- 图片文件名序列推断关联工作流。
"""

from __future__ import annotations

import json

import pytest

from sync import parser


def _ui_workflow() -> dict:
    """ComfyUI 前端保存的格式：类型在 type，文本在 widgets_values，连线靠 links。"""
    return {
        "last_node_id": 4,
        "nodes": [
            {
                "id": 1,
                "type": "CLIPTextEncode",
                "title": "Positive Prompt",
                "widgets_values": ["  a cat sitting on a bench  "],
                "inputs": [{"name": "clip", "link": 9}],
            },
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "title": "Negative Prompt",
                "widgets_values": ["blurry, low quality"],
                "inputs": [{"name": "clip", "link": 9}],
            },
            {"id": 3, "type": "CheckpointLoaderSimple", "widgets_values": ["sd_xl_base.safetensors"]},
            {
                "id": 4,
                "type": "KSampler",
                "widgets_values": [1, 20, 7.0],
                "inputs": [
                    {"name": "positive", "link": 10},
                    {"name": "negative", "link": 11},
                ],
            },
            {"id": 5, "type": "SaveImage", "widgets_values": ["ComfyUI"]},
        ],
        # [link_id, 源节点, 源槽位, 目标节点, 目标槽位, 类型]
        "links": [
            [9, 3, 1, 1, 0, "CLIP"],
            [9, 3, 1, 2, 0, "CLIP"],
            [10, 1, 0, 4, 1, "CONDITIONING"],
            [11, 2, 0, 4, 2, "CONDITIONING"],
        ],
    }


def _api_workflow(with_titles: bool = False) -> dict:
    """ComfyUI 后端可执行格式：类型在 class_type，文本在 inputs，引用用 [node_id, slot]。"""
    meta_pos = {"_meta": {"title": "正面提示词"}} if with_titles else {}
    meta_neg = {"_meta": {"title": "负面提示词"}} if with_titles else {}
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "2": {**meta_pos, "class_type": "CLIPTextEncode", "inputs": {"text": "a dog", "clip": ["1", 1]}},
        "3": {**meta_neg, "class_type": "CLIPTextEncode", "inputs": {"text": "ugly", "clip": ["1", 1]}},
        "4": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "positive": ["2", 0], "negative": ["3", 0], "model": ["1", 0]},
        },
        "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }


def test_parse_ui_format_nodes_and_types():
    """UI 格式：节点数、去重排序后的类型列表。"""
    result = parser.parse_workflow(_ui_workflow())

    assert result["format"] == "ui"
    assert result["node_count"] == 5
    assert result["node_types"] == [
        "CLIPTextEncode",
        "CheckpointLoaderSimple",
        "KSampler",
        "SaveImage",
    ]


def test_parse_ui_format_prompts():
    """UI 格式：标题含 Positive/Negative 时按标题取。"""
    result = parser.parse_workflow(_ui_workflow())

    assert result["positive_prompt"] == "a cat sitting on a bench"
    assert result["negative_prompt"] == "blurry, low quality"
    assert result["nodes"][0]["title"] == "Positive Prompt"


def test_parse_api_format_basic():
    """API 格式：节点数、类型与标题命中正负。"""
    result = parser.parse_workflow(_api_workflow(with_titles=True))

    assert result["format"] == "api"
    assert result["node_count"] == 5
    assert "KSampler" in result["node_types"]
    assert result["positive_prompt"] == "a dog"
    assert result["negative_prompt"] == "ugly"


def test_parse_api_format_negative_from_ksampler_link():
    """API 格式无标题时，负向由 KSampler 的 negative 输入链路追溯得到。"""
    result = parser.parse_workflow(_api_workflow(with_titles=False))

    assert result["negative_prompt"] == "ugly"
    assert result["positive_prompt"] == "a dog"


def test_parse_api_format_wrapped_prompt_key():
    """导出文件把 API 图包在 prompt 键里时也能解析。"""
    result = parser.parse_workflow({"prompt": _api_workflow(with_titles=True), "extra": {}})

    assert result["node_count"] == 5
    assert result["positive_prompt"] == "a dog"


def test_parse_positive_fallback_to_first_encoder():
    """既无标题也无 KSampler 连线时，取第一个非空编码节点作为正向。"""
    data = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "only one encoder"}},
        "2": {"class_type": "SaveImage", "inputs": {}},
    }
    result = parser.parse_workflow(data)

    assert result["positive_prompt"] == "only one encoder"
    assert result["negative_prompt"] == ""


def test_parse_invalid_json_raises_value_error():
    """非法 JSON 向上抛 ValueError，由 scanner 负责隔离计数。"""
    with pytest.raises(ValueError):
        parser.parse_workflow_text("{not json")


def test_combine_prompt_joins_negative():
    assert parser.combine_prompt("pos", "neg") == "pos\nNegative: neg"
    assert parser.combine_prompt("pos", "") == "pos"
    assert parser.combine_prompt("", "neg") == "neg"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("ComfyUI_00001_.png", "ComfyUI"),
        ("ComfyUI_00042_-1.png", "ComfyUI"),
        ("my_flow_00007_.png", "my_flow"),
        ("random name.png", ""),
    ],
)
def test_guess_workflow_link(filename, expected):
    """只有形如「名称_序号_」的序列命名才推断关联工作流，无法推断则留空。"""
    assert parser.guess_workflow_link(filename) == expected


def test_guess_workflow_link_from_metadata_name():
    """元数据里带 name 时优先使用元数据的名称。"""
    meta = {"workflow_json": json.dumps({"name": "my nice flow", "nodes": []})}
    assert parser.guess_workflow_link("ComfyUI_00001_.png", meta) == "my nice flow"
