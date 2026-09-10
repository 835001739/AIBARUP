"""M16 · ComfyUI API prompt 格式 → 前端 UI 格式（可 loadGraphData）转换器。

背景：AIBAR 的 ComfyUI 编辑器深链接（``AIBAR-Bridge`` 扩展，``aibar_bridge.js``）
在读取 ``aibar_wf`` 参数时，**只可靠地支持 UI 格式** 工作流（``loadGraphData`` 路径）：

- UI 格式：``{"nodes": [...], "links": [...]}``，节点是完整的前端图对象；
- API 格式：``{"<id>": {"class_type", "inputs"}}``，是 ``/prompt`` 接口用的精简图。

但 AIBAR 内部（``comic/pose.build_pose_workflow`` 等）只产出 **API 格式** 的工作流。
本模块把 API 图反序列化成 ComfyUI 编辑器可直接 ``loadGraphData`` 的 UI 图，让
「视频转绘详情页 → 工作流按钮 → 在画布里调整/重新指定帧多次出图」成为可能。

转换严格对齐 ``sync/workflow_convert.convert_ui_to_api`` 的控件顺序契约：

- 节点输入按 ``object_info`` 的 ``required`` → ``optional`` 顺序声明；
- STRING/INT/FLOAT/BOOLEAN/COMBO（及文件候选列表）视为「挂件输入」，进
  ``widgets_values``，被连线的挂件不消费槽位（连线优先）；
- MODEL/CLIP/VAE/CONDITIONING/LATENT/IMAGE/MASK/CONTROL_NET/... 视为「仅连线」，
  进 ``inputs`` 的 ``link`` 端口；
- ``links`` 元组格式：``[link_id, origin_id, origin_slot, target_id, target_slot, type]``，
  其中 ``target_slot`` 必须等于该输入在节点 ``inputs`` 数组中的自然下标（位置依赖）。

因为顺序契约一致，``convert_ui_to_api(ui_graph)`` 可无损还原出原始 API 图，
本模块暴露 ``validate_roundtrip`` 供单测与运行期自校验使用。
"""

from __future__ import annotations

from typing import Any, Callable

from sync.workflow_convert import _object_info_all, convert_ui_to_api, is_api_format

# 与 sync.workflow_convert._WIDGET_TYPES 保持一致（挂件输入原始类型）
_WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}


def _is_widget_input(cfg: Any) -> bool:
    """输入声明是否为「挂件输入」（有控件，可序列化进 widgets_values）。

    与 ``sync.workflow_convert._input_has_widget`` 逻辑完全一致：
    - 声明首元素是列表（文件候选名）→ 文件上传控件；
    - 首元素是 STRING/INT/FLOAT/BOOLEAN/COMBO → 普通控件；
    - 其余（MODEL/CLIP/IMAGE…）→ 仅连线。
    """
    if not isinstance(cfg, list) or not cfg:
        return False
    token = cfg[0]
    if isinstance(token, list):
        return True
    if not isinstance(token, str):
        return False
    return token in _WIDGET_TYPES


def _input_type(cfg: Any) -> str:
    """输入声明的端口类型字符串（用于 UI ``inputs[].type``）。"""
    if not isinstance(cfg, list) or not cfg:
        return "COMBO"
    token = cfg[0]
    if isinstance(token, list):
        return "COMBO"  # 文件候选列表 → 下拉控件
    if isinstance(token, str):
        return token  # "MODEL" / "STRING" / "IMAGE" / "COMBO" ...
    return "COMBO"


def _declared_inputs(class_type: str, info: dict | None) -> list[tuple[str, str, bool]]:
    """返回 ``(name, type, is_widget)`` 列表，按 required → optional 顺序。"""
    out: list[tuple[str, str, bool]] = []
    if not isinstance(info, dict):
        return out
    inputs = info.get("input") if isinstance(info.get("input"), dict) else {}
    for section in ("required", "optional"):
        spec = inputs.get(section) if isinstance(inputs.get(section), dict) else {}
        for name, cfg in spec.items():
            out.append((name, _input_type(cfg), _is_widget_input(cfg)))
    return out


def _declared_outputs(class_type: str, info: dict | None) -> list[tuple[str, str]]:
    """返回 ``(name, type)`` 列表，按输出声明顺序。"""
    if not isinstance(info, dict):
        return []
    names = info.get("output_name")
    types = info.get("output")
    if not isinstance(types, list):
        return []
    if not isinstance(names, list):
        names = types
    return [(str(names[i]) if i < len(names) else str(types[i]), types[i]) for i in range(len(types))]


def _is_connection(val: Any) -> bool:
    """API 格式输入值是否为「连线引用」（``["<origin_id>", <slot>]``）。"""
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return isinstance(val[1], int)
    return False


def _output_type(info_map: dict, api_graph: dict, origin_key: str, slot: int) -> str:
    """取 origin 节点第 ``slot`` 个输出的类型（用于连线 type 字段，缺失时回退 IMAGE）。"""
    node = api_graph.get(str(origin_key))
    class_type = node.get("class_type") if isinstance(node, dict) else None
    info = info_map.get(class_type) if isinstance(info_map, dict) else None
    outs = _declared_outputs(class_type or "", info)
    if 0 <= slot < len(outs):
        return outs[slot][1]
    return "IMAGE"


def _layout_pos(index: int) -> list[int]:
    """给第 ``index`` 个节点一个简单的网格坐标，避免重叠（纯装饰，编辑器可拖动）。"""
    col = index % 5
    row = index // 5
    return [60 + col * 300, 60 + row * 360]


def _default_size(class_type: str) -> list[int]:
    """常见节点默认尺寸；编辑器载入后会自适应。"""
    if class_type in ("KSampler", "ControlNetApplyAdvanced", "IPAdapterFaceID"):
        return [320, 260]
    if class_type in ("LoadImage", "SaveImage"):
        return [340, 310]
    return [300, 180]


def api_to_ui_graph(
    api_graph: dict[str, Any],
    info_lookup: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把 ComfyUI API prompt 图转换成编辑器可 ``loadGraphData`` 的 UI 格式图。

    Args:
        api_graph: ``{"<id>": {"class_type": ..., "inputs": {...}}, ...}``。
        info_lookup: 返回 ``{class_type: object_info}`` 的回调；默认拉全量并缓存。

    Returns:
        UI 格式工作流：``{"last_node_id", "last_link_id", "nodes", "links", ...}``。
    """
    if not isinstance(api_graph, dict):
        raise ValueError("API 工作流必须是 dict")

    info_map = info_lookup() if callable(info_lookup) else _object_info_all()

    # 1) 分配稳定的整数节点 id（API 键多为字符串 "1".."15"）
    id_map: dict[str, int] = {}
    next_id = 1
    for key in api_graph:
        if str(key).lstrip("-").isdigit():
            iid = int(key)
        else:
            iid = next_id
            next_id += 1
        while iid in id_map.values():
            iid = next_id
            next_id += 1
        id_map[str(key)] = iid

    nodes: list[dict] = []
    links: list[list] = []
    out_links: dict[tuple[int, int], list[int]] = {}
    link_id = 1

    for key, node in api_graph.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not class_type:
            continue
        iid = id_map[str(key)]
        info = info_map.get(class_type) if isinstance(info_map, dict) else None
        inputs_decl = _declared_inputs(class_type, info)
        outputs_decl = _declared_outputs(class_type, info)
        api_inputs = node.get("inputs") or {}

        ui_inputs: list[dict] = []
        for idx, (name, typ, is_widget) in enumerate(inputs_decl):
            val = api_inputs.get(name)
            if _is_connection(val):
                origin_key = str(val[0])
                origin_slot = int(val[1])
                origin_iid = id_map.get(origin_key, iid)
                otyp = _output_type(info_map, api_graph, origin_key, origin_slot)
                lid = link_id
                link_id += 1
                ui_inputs.append({"name": name, "type": otyp, "link": lid})
                links.append([lid, origin_iid, origin_slot, iid, idx, otyp])
                out_links.setdefault((origin_iid, origin_slot), []).append(lid)
            else:
                if is_widget:
                    ui_inputs.append({"name": name, "type": typ, "widget": {"name": name}, "link": None})
                else:
                    ui_inputs.append({"name": name, "type": typ, "link": None})

        # widgets_values：挂件输入按声明顺序，被连线的挂件不消费槽位
        widgets_values: list[Any] = []
        for (name, _typ, is_widget) in inputs_decl:
            if not is_widget:
                continue
            val = api_inputs.get(name)
            if _is_connection(val):
                continue
            widgets_values.append(val)

        ui_outputs: list[dict] = []
        for oi, (oname, otype) in enumerate(outputs_decl):
            ui_outputs.append({"name": oname, "type": otype, "links": out_links.get((iid, oi), [])})

        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        ui_node = {
            "id": iid,
            "type": class_type,
            "pos": _layout_pos(len(nodes)),
            "size": _default_size(class_type),
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": ui_inputs,
            "outputs": ui_outputs,
            "title": meta.get("title") or class_type,
            "properties": {"Node name for S&R": class_type},
            "widgets_values": widgets_values,
        }
        nodes.append(ui_node)

    return {
        "last_node_id": max((n["id"] for n in nodes), default=0),
        "last_link_id": link_id - 1,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def _normalize_api(graph: dict[str, Any]) -> dict[str, Any]:
    """去掉 ``_meta`` 等噪声，仅保留 class_type + inputs 用于比较。

    ``inputs`` 里值为 ``None`` 的键会被一并丢掉：UI→API 回写时会把未设置的可选输入
    补成 ``None``（如 CLIPLoader 的 ``device``），而原始 API 图里根本没这个键。
    两者语义等价（"没填"），不归一化的话往返校验会误报 mismatch。
    """
    out: dict[str, Any] = {}
    for k, v in graph.items():
        if isinstance(v, dict) and "class_type" in v:
            ins = {kk: vv for kk, vv in (v.get("inputs") or {}).items() if vv is not None}
            out[str(k)] = {"class_type": v["class_type"], "inputs": ins}
    return out


def validate_roundtrip(
    api_graph: dict[str, Any],
    info_lookup: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """自校验：API → UI → API 是否无损。

    Returns:
        ``{"ok": bool, "error": str|None, "ui": {...}, "api2": {...}}``。
    """
    try:
        ui = api_to_ui_graph(api_graph, info_lookup=info_lookup)
        api2 = convert_ui_to_api(ui, info_lookup=info_lookup)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "ui": None, "api2": None}
    a1 = _normalize_api(api_graph)
    a2 = _normalize_api(api2)
    if a1 == a2:
        return {"ok": True, "error": None, "ui": ui, "api2": a2}
    # 找出差异，便于定位
    diffs = []
    for k in sorted(set(a1) | set(a2)):
        if a1.get(k) != a2.get(k):
            diffs.append({"node": k, "orig": a1.get(k), "back": a2.get(k)})
    return {
        "ok": False,
        "error": "roundtrip_mismatch:%s" % json_dumps(diffs),
        "ui": ui,
        "api2": a2,
    }


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


__all__ = [
    "api_to_ui_graph",
    "validate_roundtrip",
    "is_api_graph",
]


def is_api_graph(data: Any) -> bool:
    """便捷封装：判断对象是否已经是 API prompt 格式。"""
    return is_api_format(data)
