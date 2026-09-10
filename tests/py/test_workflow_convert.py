"""UI→API 工作流转换的单测（不依赖运行的 ComfyUI，用假 object_info）。"""

from __future__ import annotations

import json

import pytest

from sync import workflow_convert as wc

# 一个最小但真实的 UI 工作流：LoadImage(挂件) -> ImageScale(连线image+挂件) -> SaveImage(连线)
_UI_WORKFLOW = {
    "nodes": [
        {
            "id": 1,
            "type": "LoadImage",
            "inputs": [],
            "widgets_values": ["cat.png", "image"],
        },
        {
            "id": 2,
            "type": "ImageScaleToTotalPixels",
            "inputs": [{"name": "image", "type": "IMAGE", "link": 10}],
            "widgets_values": ["lanczos", 1, 1],
        },
        {
            "id": 3,
            "type": "SaveImage",
            "inputs": [{"name": "images", "type": "IMAGE", "link": 11}],
            "widgets_values": ["Out"],
        },
    ],
    "links": [
        [10, 1, 0, 2, 0, "IMAGE"],
        [11, 2, 0, 3, 0, "IMAGE"],
    ],
}

# 假 object_info：只暴露本测试用到的节点类型
_FAKE_INFO = {
    "LoadImage": {
        "input": {
            "required": {
                "image": [["cat.png", "dog.png"], {}],  # 图片上传控件（列表候选）
                "upload": ["COMBO", {"options": ["image", "mask"]}],
            }
        }
    },
    "ImageScaleToTotalPixels": {
        "input": {
            "required": {
                "image": ["IMAGE", {}],                 # 仅连线
                "upscale_method": ["COMBO", {"options": ["lanczos"]}],
                "megapixels": ["FLOAT", {"default": 1.0}],
                "min_pixels": ["INT", {"default": 1}],
            }
        }
    },
    "SaveImage": {
        "input": {
            "required": {
                "images": ["IMAGE", {}],               # 仅连线
            },
            "optional": {
                "filename_prefix": ["STRING", {"default": "ComfyUI"}],
            }
        }
    },
}


def _info_lookup():
    return _FAKE_INFO


def test_is_api_format():
    assert wc.is_api_format(_UI_WORKFLOW) is False
    assert wc.is_api_format({"1": {"class_type": "LoadImage", "inputs": {}}}) is True
    assert wc.is_api_format({"prompt": {"1": {"class_type": "X", "inputs": {}}}}) is True


def test_convert_maps_widgets_and_links():
    graph = wc.convert_ui_to_api(_UI_WORKFLOW, _info_lookup)
    assert set(graph.keys()) == {"1", "2", "3"}

    # LoadImage：两个挂件都填上了
    assert graph["1"]["class_type"] == "LoadImage"
    assert graph["1"]["inputs"]["image"] == "cat.png"
    assert graph["1"]["inputs"]["upload"] == "image"

    # ImageScale：image 是连线（覆盖），其余挂件按序填入
    assert graph["2"]["inputs"]["image"] == ["1", 0]      # 来自 link 10 -> 节点1 slot0
    assert graph["2"]["inputs"]["upscale_method"] == "lanczos"
    assert graph["2"]["inputs"]["megapixels"] == 1
    assert graph["2"]["inputs"]["min_pixels"] == 1

    # SaveImage：images 是连线
    assert graph["3"]["inputs"]["images"] == ["2", 0]
    assert graph["3"]["inputs"]["filename_prefix"] == "Out"


def test_convert_resolves_links_correctly():
    graph = wc.convert_ui_to_api(_UI_WORKFLOW, _info_lookup)
    ids = set(graph.keys())
    for node in graph.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in ids


def test_convert_text_passthrough_api():
    api_graph = {"9": {"class_type": "KSampler", "inputs": {}}}
    assert wc.convert_text(json.dumps(api_graph), _info_lookup) == api_graph


def test_convert_unknown_node_type_is_safe():
    # 未知节点类型：退化为只填连线，不抛异常
    wf = {
        "nodes": [
            {"id": 5, "type": "MysteryNode", "inputs": [{"name": "x", "link": 99}], "widgets_values": [42]},
        ],
        "links": [[99, 5, 0, 5, 0, "WHATEVER"]],
    }
    graph = wc.convert_ui_to_api(wf, _info_lookup)
    assert "5" in graph
    assert graph["5"]["inputs"]["x"] == ["5", 0]
