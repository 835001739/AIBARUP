"""M2 · 工作流 UI 格式 → ComfyUI API prompt 格式转换。

背景：AIBAR 同步进来的工作流是 ComfyUI 前端的 **UI 格式**
（``{"nodes": [...], "links": [...]}``），节点输入分两种：

- **连线输入（link）**：出现在 ``node.inputs`` 里，``link`` 字段指向 ``links``
  数组里的某条连线，最终要变成 API 格式里的 ``["<origin_id>", <slot>]`` 引用；
- **挂件输入（widget）**：文本/数值/下拉等字面量，存在 ``node.widgets_values``
  数组里，按节点 ``INPUT_TYPES()`` 的声明顺序序列化。

ComfyUI 的 ``/prompt`` 接口只接受 **API 格式**
（``{"<node_id>": {"class_type": ..., "inputs": {...}}}``），直接把 UI 格式
POST 过去会 500。本模块复刻前端 ``app.graphToPrompt`` 的核心逻辑，纯 Python 实现：

1. 通过 ``/object_info`` 拿到每个节点类型的输入声明（有序）；
2. 输入 TYPE 为 STRING/INT/FLOAT/BOOLEAN/COMBO（或 LoadImage 的图片上传列表）
   视为"挂件输入"，其余（MODEL/CLIP/VAE/CONDITIONING/LATENT/IMAGE…）视为"仅连线"；
3. ``widgets_values`` 按挂件输入顺序逐个填入；被连线的挂件不消费槽位（连线优先）；
4. 连线输入覆盖同名挂件，得到最终的 ``inputs``。

纯函数层：不读写数据库，仅在需要节点契约时通过注入的 ``info_lookup`` 拉取
ComfyUI 的 ``/object_info``，便于单测与缓存。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

# 视为"挂件输入"的原始类型（有对应控件、可序列化进 widgets_values）
_WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}
# 仅作为连线的类型：没有控件，不能从 widgets_values 取
_LINK_ONLY_HINT = {
    "MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "IMAGE", "MASK",
    "AUDIO", "VIDEO", "SIGNAL", "PHOTO", "FRAME", "LAYERMASK",
}

# object_info 缓存（模块级 TTL，避免每次生成都拉全量节点清单）
_INFO_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_INFO_TTL = 60.0


def is_api_format(data: Any) -> bool:
    """判断工作流是否已经是 API prompt 格式（而非 UI 格式）。"""
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("nodes"), list):
        return False
    inner = data.get("prompt")
    if isinstance(inner, dict) and inner:
        return True
    # API 格式：顶层是一组 "<id>": {"class_type", "inputs"}
    if data and all(
        isinstance(v, dict) and "class_type" in v for v in data.values()
    ):
        return True
    return False


def _input_has_widget(cfg: Any) -> bool:
    """根据 object_info 的输入声明判断该输入是否有控件。

    - 声明是列表（如 LoadImage 的图片名候选列表）→ 图片上传控件；
    - 声明首元素是 STRING/INT/FLOAT/BOOLEAN/COMBO → 普通控件；
    - 其余（MODEL/CLIP/IMAGE…）→ 仅连线，无控件。
    """
    if not isinstance(cfg, list) or not cfg:
        return False
    token = cfg[0]
    if isinstance(token, list):
        return True  # 图片上传控件的候选文件名列表
    if not isinstance(token, str):
        return False
    return token in _WIDGET_TYPES


def _object_info_all() -> dict[str, Any]:
    """拉取全量节点契约（带 60s TTL 缓存）。失败返回空字典。"""
    cached = _INFO_CACHE.get("data")
    if cached is not None and (time.monotonic() - float(_INFO_CACHE.get("ts") or 0.0)) < _INFO_TTL:
        return cached
    try:
        from reverse.comfyui_client import object_info

        payload = object_info(None, timeout=20.0) or {}
    except Exception:
        payload = {}
    _INFO_CACHE["data"] = payload
    _INFO_CACHE["ts"] = time.monotonic()
    return payload


def _widget_input_names(class_type: str, info_map: dict[str, Any]) -> list[str]:
    """返回某节点类型按声明顺序排列的"挂件输入"名列表（required 在前，optional 在后）。"""
    info = info_map.get(class_type) if isinstance(info_map, dict) else None
    if not isinstance(info, dict):
        return []
    inputs = info.get("input") if isinstance(info.get("input"), dict) else {}
    names: list[str] = []
    for section in ("required", "optional"):
        spec = inputs.get(section) if isinstance(inputs.get(section), dict) else {}
        for name, cfg in spec.items():
            if _input_has_widget(cfg):
                names.append(name)
    return names


def _links_index(links: Any) -> dict[Any, tuple[Any, int]]:
    """``{link_id: (origin_node_id, origin_slot)}``，兼容新/旧连线数组写法。"""
    index: dict[Any, tuple[Any, int]] = {}
    if not isinstance(links, list):
        return index
    for item in links:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 4:
                index[item[0]] = (item[1], int(item[2]))
        except (IndexError, TypeError, ValueError):
            continue
    return index


def convert_ui_to_api(
    ui_data: Any,
    info_lookup: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把 UI 格式工作流对象转换成 ComfyUI 可执行的 API prompt 图。

    Args:
        ui_data: 解析后的 UI 格式工作流 dict（含 ``nodes`` / ``links``）。
        info_lookup: 返回 ``{class_type: object_info}`` 的回调；默认拉全量并缓存。

    Returns:
        API prompt 图：``{"<node_id>": {"class_type": ..., "inputs": {...}}, ...}``。
    """
    if not isinstance(ui_data, dict):
        raise ValueError("工作流不是合法的对象")

    # 兼容把 API 图包在 prompt/workflow 键里的导出
    if isinstance(ui_data.get("prompt"), dict) and ui_data["prompt"]:
        return ui_data["prompt"]
    if isinstance(ui_data.get("workflow"), dict) and isinstance(
        ui_data["workflow"].get("nodes"), list
    ):
        ui_data = ui_data["workflow"]

    nodes = ui_data.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("工作流缺少 nodes 数组")
    links = _links_index(ui_data.get("links"))

    info_map = info_lookup() if callable(info_lookup) else _object_info_all()

    graph: dict[str, Any] = {}
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        class_type = str(raw.get("type") or "")
        if not class_type:
            continue
        node_id = str(raw.get("id") if raw.get("id") is not None else "")
        if node_id == "":
            continue

        widget_names = _widget_input_names(class_type, info_map)
        # 连线输入：name -> [origin_id, origin_slot]
        linked: dict[str, list[Any]] = {}
        for port in raw.get("inputs") or []:
            if not isinstance(port, dict):
                continue
            name = port.get("name")
            link_id = port.get("link")
            if name is None or link_id is None:
                continue
            if link_id in links:
                origin_id, origin_slot = links[link_id]
                linked[str(name)] = [str(origin_id), origin_slot]

        wv = raw.get("widgets_values") or []
        wv = wv if isinstance(wv, list) else []
        inputs: dict[str, Any] = {}
        wi = 0
        for name in widget_names:
            if name in linked:
                # 被连线覆盖：不消费 widgets_values 槽位，连线优先
                continue
            if wi < len(wv):
                val = wv[wi]
                # UI 图里「没填」的可选挂件是 None（如 CLIPLoader.device）。
                # 原样回写成 null 会被 ComfyUI 校验拒掉：
                #   "device: None not in ['default', 'cpu']"
                # 正确语义是「不填，交给节点默认值」→ 直接省略这个键。
                if val is not None:
                    inputs[name] = val
            wi += 1
        # 连线覆盖同名挂件
        inputs.update(linked)

        graph[node_id] = {"class_type": class_type, "inputs": inputs}
    return graph


def convert_text(text: str, info_lookup: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """从工作流 JSON 文本转换；已是 API 格式则原样返回。"""
    data = json.loads(text)
    if is_api_format(data):
        # 抽出内层 prompt（若有）或整图
        if isinstance(data.get("prompt"), dict) and data["prompt"]:
            return data["prompt"]
        return data
    return convert_ui_to_api(data, info_lookup)


__all__ = [
    "is_api_format",
    "convert_ui_to_api",
    "convert_text",
    "_object_info_all",
]
