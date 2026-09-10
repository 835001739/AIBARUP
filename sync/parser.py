"""M2 · 工作流解析 与 M3 · 图片元数据解析封装。

支持两种 ComfyUI 工作流格式：
- **UI 格式**（前端保存）：``{"nodes": [...], "links": [...]}``，节点用 ``type`` 表示类型、
  文本存放在 ``widgets_values``，连线用 ``links`` + ``node.inputs[].link`` 表达；
- **API 格式**（后端可执行的 prompt）：``{"1": {"class_type": ..., "inputs": {...}}}``，
  输入值可能是字面量，也可能是 ``["<node_id>", <slot>]`` 引用。

正负提示词判定顺序（PRD M2）：
1. 节点标题含 negative/负面 → 负向；含 positive/正面 → 正向；
2. 未命中标题时，由 KSampler 的 ``negative`` 输入沿连线追溯到 CLIPTextEncode；
3. 仍未命中则取第一个非空 CLIPTextEncode 作为正向。

本模块是纯函数层：不读写数据库、不做网络 IO，便于单测。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.imagemeta import extract_prompt_metadata
from core.textutil import sanitize_model_text

# 判定为"文本编码器"的类型片段：CLIPTextEncode / CLIPTextEncodeSDXL 等均覆盖
_ENCODER_MARK = "CLIPTextEncode"
_SAMPLER_PREFIX = "KSampler"
_NEGATIVE_WORDS = ("negative", "负面", "负向")
_POSITIVE_WORDS = ("positive", "正面", "正向")

# ComfyUI 默认产出命名：ComfyUI_00001_.png / xxx_00042_-1.png
_SEQ_RE = re.compile(r"^(?P<name>.+?)_(?P<seq>\d{3,6})_?(?:-[A-Za-z0-9]+)?$")


def _text_of(value: Any) -> str:
    """从 widgets_values / inputs 里取出可读文本，忽略非字符串与控制字符。"""
    if isinstance(value, str):
        return sanitize_model_text(value).strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return sanitize_model_text(item).strip()
    return ""


def _links_index(raw_links: Any) -> dict[Any, tuple[Any, Any]]:
    """把 links 归一化成 ``{link_id: (源节点, 目标节点)}``。

    兼容三种写法：ComfyUI 新版数组 ``[id, origin, origin_slot, target, target_slot, type]``、
    旧版对象 ``{"id", "origin_id", "target_id"}``，以及对象里使用 ``origin/target`` 键的情况。
    """
    index: dict[Any, tuple[Any, Any]] = {}
    if not isinstance(raw_links, list):
        return index
    for item in raw_links:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 4:
                index[item[0]] = (item[1], item[3])
            elif isinstance(item, dict) and "id" in item:
                origin = item.get("origin_id", item.get("origin"))
                target = item.get("target_id", item.get("target"))
                index[item["id"]] = (origin, target)
        except (IndexError, TypeError, KeyError):
            continue
    return index


def _build_nodes_ui(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 UI 格式：文本来自 widgets_values，负向连线来自 node.inputs[].link。"""
    raw_nodes = data.get("nodes") or []
    links = _links_index(data.get("links"))
    nodes: list[dict[str, Any]] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        class_type = str(raw.get("type") or "")
        title = str(raw.get("title") or "")
        text = ""
        if _ENCODER_MARK in class_type:
            text = _text_of(raw.get("widgets_values"))
        negative_ref = None
        if class_type.startswith(_SAMPLER_PREFIX):
            for port in raw.get("inputs") or []:
                if isinstance(port, dict) and str(port.get("name") or "") == "negative":
                    link_id = port.get("link")
                    if link_id is not None and link_id in links:
                        negative_ref = links[link_id][0]
                    break
        nodes.append(
            {
                "id": str(raw.get("id") if raw.get("id") is not None else ""),
                "class_type": class_type,
                "title": title,
                "text": text,
                "negative_ref": str(negative_ref) if negative_ref is not None else None,
            }
        )
    return nodes


def _build_nodes_api(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 API 格式：输入值可能是字面量，也可能是 [node_id, slot] 引用。"""
    nodes: list[dict[str, Any]] = []
    for key, raw in graph.items():
        if not isinstance(raw, dict):
            continue
        class_type = str(raw.get("class_type") or "")
        inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        text = ""
        if _ENCODER_MARK in class_type:
            # inputs.text 可能是字符串，也可能是 ["3", 0] 形式的引用
            value = inputs.get("text")
            if isinstance(value, (list, tuple)) and value:
                value = graph.get(str(value[0]), {}).get("inputs", {}).get("text")
            text = _text_of(value)
        negative_ref = None
        if class_type.startswith(_SAMPLER_PREFIX):
            value = inputs.get("negative")
            if isinstance(value, (list, tuple)) and value:
                negative_ref = str(value[0])
        nodes.append(
            {
                "id": str(key),
                "class_type": class_type,
                "title": str(meta.get("title") or ""),
                "text": text,
                "negative_ref": negative_ref,
            }
        )
    return nodes


def _normalize(data: Any) -> tuple[str, list[dict[str, Any]]]:
    """把任意来源的工作流 JSON 归一化为节点列表，返回 ``(格式标记, 节点列表)``。"""
    if not isinstance(data, dict):
        return "api", []
    if isinstance(data.get("nodes"), list):
        return "ui", _build_nodes_ui(data)
    # 有些导出把 API 图包在 "prompt" 键里、把 UI 图包在 "workflow" 键里
    inner = data.get("prompt")
    if isinstance(inner, dict) and inner:
        return "api", _build_nodes_api(inner)
    workflow = data.get("workflow")
    if isinstance(workflow, dict) and isinstance(workflow.get("nodes"), list):
        return "ui", _build_nodes_ui(workflow)
    return "api", _build_nodes_api(data)


def _pick_prompts(nodes: list[dict[str, Any]]) -> tuple[str, str]:
    """按"标题 → KSampler 连线 → 兜底"顺序确定正/负提示词。"""
    encoders = [n for n in nodes if _ENCODER_MARK in n["class_type"] and n["text"]]
    positive = ""
    negative = ""

    for node in encoders:
        title = (node["title"] or "").lower()
        if any(word in title for word in _NEGATIVE_WORDS):
            negative = negative or node["text"]
        elif any(word in title for word in _POSITIVE_WORDS):
            positive = positive or node["text"]
        if positive and negative:
            break

    if not negative:
        by_id = {n["id"]: n for n in nodes}
        for node in nodes:
            ref = node.get("negative_ref")
            if not ref:
                continue
            target = by_id.get(str(ref))
            if target and target["text"]:
                negative = target["text"]
                break

    if not positive:
        for node in encoders:
            if node["text"] != negative:
                positive = node["text"]
                break

    # 极端情况下正负指向同一节点：只允许一个占位，避免列表页重复展示
    if positive and positive == negative:
        negative = ""
    return positive, negative


def parse_workflow(data: Any) -> dict[str, Any]:
    """解析工作流对象，返回结构化摘要。

    Returns:
        ``{"format", "node_count", "node_types", "positive_prompt",
        "negative_prompt", "nodes"}``。输入非对象时返回空摘要，不抛异常。
    """
    fmt, nodes = _normalize(data)
    positive, negative = _pick_prompts(nodes)
    node_types = sorted({n["class_type"] for n in nodes if n["class_type"]})
    return {
        "format": fmt,
        "node_count": len(nodes),
        "node_types": node_types,
        "positive_prompt": positive,
        "negative_prompt": negative,
        "nodes": [
            {
                "id": n["id"],
                "class_type": n["class_type"],
                "title": n["title"],
                "text": n["text"],
            }
            for n in nodes
        ],
    }


def parse_workflow_text(text: str) -> dict[str, Any]:
    """解析工作流 JSON 文本；格式非法时向上抛 ``ValueError``，由调用方隔离计数。"""
    data = json.loads(text)
    return parse_workflow(data)


def combine_prompt(positive: str, negative: str) -> str:
    """把正负提示词拼成可检索的单字段（M3 图库 ``images.prompt``）。"""
    positive = (positive or "").strip()
    negative = (negative or "").strip()
    if positive and negative:
        return f"{positive}\nNegative: {negative}"
    return positive or negative


def guess_workflow_link(filename: str, meta: dict[str, Any] | None = None) -> str:
    """从元数据或文件名推断关联工作流名；无法推断时返回空串。

    例：``ComfyUI_00001_.png`` → ``ComfyUI``；``my_flow_00042_-1.png`` → ``my_flow``。
    """
    meta = meta or {}
    raw = meta.get("workflow_json") or ""
    if raw:
        try:
            graph = json.loads(raw)
            name = graph.get("name") if isinstance(graph, dict) else None
            if isinstance(name, str) and name.strip():
                return sanitize_model_text(name).strip()[:120]
        except (ValueError, TypeError):
            pass
    stem = Path(str(filename or "")).stem
    match = _SEQ_RE.match(stem)
    if match:
        name = (match.group("name") or "").strip(" _-")
        if name:
            return sanitize_model_text(name)[:120]
    return ""


def parse_image_metadata(path: str | Path) -> dict[str, Any]:
    """读取图片内嵌元数据，返回 ``{"positive","negative","prompt","workflow_link","has_metadata"}``。

    底层能力来自 ``core.imagemeta``，本函数只做归一化与隐私清洗。
    """
    meta = extract_prompt_metadata(path)
    positive = meta.get("positive") or ""
    negative = meta.get("negative") or ""
    return {
        "has_metadata": bool(meta.get("has_metadata")),
        "positive": positive,
        "negative": negative,
        "prompt": combine_prompt(positive, negative),
        "workflow_link": guess_workflow_link(Path(path).name, meta),
    }


__all__ = [
    "parse_workflow",
    "parse_workflow_text",
    "parse_image_metadata",
    "combine_prompt",
    "guess_workflow_link",
]
