"""``sync.graph_prompt`` 的单元测试：提示词定位与节点定位。

覆盖实战里出现过的全部拓扑。每个用例都是最小化的 UI 图，刻意不依赖真实
工作流文件——真实文件会随 ComfyUI 版本变动，合成样本才锁得住行为。

UI 图 ``links`` 用 ComfyUI 的数组写法：
``[连线id, 起点节点id, 起点槽位, 终点节点id, 终点槽位, 类型]``
"""

from __future__ import annotations

import pytest

from sync import graph_prompt


def _encode(node_id, text, title=""):
    return {
        "id": node_id,
        "type": "CLIPTextEncode",
        "title": title,
        "widgets_values": [text],
        "inputs": [{"name": "clip", "type": "CLIP", "link": None}],
    }


def test_direct_encoder_text_and_node():
    """直连式：提示词就在编码器自己的 widget 里。"""
    data = {
        "nodes": [_encode(1, "a cat on a table", "Positive"), _encode(2, "blurry", "Negative")],
        "links": [],
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "a cat on a table"
    assert got["negative"] == "blurry"
    assert got["positive_node"] == "1"
    assert got["negative_node"] == "2"


def test_primitive_upstream_is_the_real_holder():
    """上游喂入式：编码器 widget 是空的，必须回溯到 PrimitiveStringMultiline。

    写在编码器上是不生效的——这是 FLUX.2 Klein 那批图的坑。
    """
    data = {
        "nodes": [
            {
                "id": 1,
                "type": "CLIPTextEncode",
                "widgets_values": [""],
                "inputs": [{"name": "text", "type": "STRING", "link": 10}],
            },
            {"id": 3, "type": "PrimitiveStringMultiline", "widgets_values": ["hello upstream"]},
        ],
        "links": [[10, 3, 0, 1, 0, "STRING"]],
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "hello upstream"
    assert got["positive_node"] == "3"


def test_multi_stage_pipeline_traces_to_first_box():
    """多级管道式：Primitive → 翻译 → 优化器 → 编码器，一路回溯到手打的那个框。"""
    data = {
        "nodes": [
            {
                "id": 1,
                "type": "CLIPTextEncode",
                "widgets_values": [""],
                "inputs": [{"name": "text", "type": "STRING", "link": 11}],
            },
            {
                "id": 5,
                "type": "AIPromptOptimizer",
                "widgets_values": ["内置补全", ""],
                "inputs": [{"name": "text", "type": "STRING", "link": 12}],
            },
            {
                "id": 7,
                "type": "PrimitiveStringMultiline",
                "widgets_values": ["原始中文提示词，一只猫坐在窗台上"],
            },
        ],
        "links": [
            [11, 5, 0, 1, 0, "STRING"],
            [12, 7, 0, 5, 0, "STRING"],
        ],
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "原始中文提示词，一只猫坐在窗台上"
    assert got["positive_node"] == "7"


def test_untitled_nodes_split_by_ksampler_negative_link():
    """无标题式：只能靠 KSampler 的 negative 连线区分正负。"""
    data = {
        "nodes": [
            _encode(1, "masterpiece"),
            _encode(2, "lowres, bad hands"),
            {
                "id": 9,
                "type": "KSampler",
                "inputs": [
                    {"name": "positive", "type": "CONDITIONING", "link": 20},
                    {"name": "negative", "type": "CONDITIONING", "link": 21},
                ],
            },
        ],
        "links": [
            [20, 1, 0, 9, 0, "CONDITIONING"],
            [21, 2, 0, 9, 1, "CONDITIONING"],
        ],
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["negative_node"] == "2"
    assert got["positive_node"] == "1"
    assert got["positive"] == "masterpiece"


def test_subgraph_workflow_targets_wrapper_widget():
    """子图式：文本取自子图内部，写入目标必须是画布上的包装节点。

    子图内部节点不在 ``app.graph._nodes`` 里，桥梁够不着，所以这里返回的是
    包装节点 id + 提升出来的 widget 名（``68@text``）。
    """
    sub_id = "7651d2ae-5471-47b0-b064-b34ee62343c1"
    data = {
        "nodes": [
            {
                "id": 68,
                "type": sub_id,
                "inputs": [
                    {"name": "pixels", "type": "IMAGE", "link": None},
                    {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None},
                ],
            }
        ],
        "links": [],
        "definitions": {
            "subgraphs": [
                {
                    "id": sub_id,
                    "name": "Image Edit (Flux.2 Dev)",
                    "nodes": [_encode(6, "inner subgraph prompt")],
                    "links": [],
                }
            ]
        },
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "inner subgraph prompt"
    assert got["positive_node"] == "68@text"


def test_subgraph_without_promoted_widget_falls_back_to_index():
    """子图没把输入框提升成 widget 时，提示词躺在包装节点的 widgets_values 里。"""
    sub_id = "4c314f31-ecda-4b08-ae98-faaba1bf613f"
    data = {
        "nodes": [
            {
                "id": 105,
                "type": sub_id,
                "inputs": [{"name": "width", "type": "INT", "widget": {"name": "width"}}],
                "widgets_values": ["一段足够长的提示词文本内容啊", "1344", "768"],
            }
        ],
        "links": [],
        "definitions": {
            "subgraphs": [
                {"id": sub_id, "nodes": [_encode(13, "一段足够长的提示词文本内容啊")], "links": []}
            ]
        },
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "一段足够长的提示词文本内容啊"
    assert got["positive_node"] == "105#0"


def test_loose_fallback_for_workflows_without_encoders():
    """专用节点式：整张图没有文本编码节点时，兜底取最长的自然语言片段。"""
    data = {
        "nodes": [
            {
                "id": 7,
                "type": "PrimitiveStringMultiline",
                "widgets_values": ["迷你max的提示词内容，一只猫在屋顶上看月亮"],
            },
            {"id": 8, "type": "MiniMaxH3ImageToVideo"},
            {"id": 3, "type": "VAELoader", "widgets_values": ["ae.safetensors"]},
        ],
        "links": [],
    }
    got = graph_prompt.analyze_ui_graph(data)
    assert got["positive"] == "迷你max的提示词内容，一只猫在屋顶上看月亮"
    assert got["positive_node"] == "7"


@pytest.mark.parametrize("bad", [None, "not a graph", {}, {"nodes": {}}, 42])
def test_invalid_input_returns_empty(bad):
    """非 UI 图输入一律返回四个空串，不能抛异常（调用方靠空值降级）。"""
    assert graph_prompt.analyze_ui_graph(bad) == {
        "positive": "",
        "negative": "",
        "positive_node": "",
        "negative_node": "",
    }


def test_model_filenames_are_not_prompts():
    """模型权重文件名长得像文本，但不能被当成提示词。"""
    assert graph_prompt._is_prompt_like("flux2_dev_fp8mixed.safetensors") is False
    assert graph_prompt._is_prompt_like("12345") is False
    assert graph_prompt._is_prompt_like("short") is False
    assert graph_prompt._is_prompt_like("一位年轻的亚洲女剑客站在雨夜的霓虹东京街头") is True
