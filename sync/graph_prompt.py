"""从 ComfyUI **UI 格式画布**里定位「提示词写在哪、现在写的是什么」。

为什么单独一个模块
------------------
``parser`` 面向的是"提取摘要"（正向/负向文本），但深链接需要的是**节点坐标**：
把提示词写回画布时，写进哪个节点才真的生效？

实战里至少有四种拓扑，``parser`` 只能覆盖前两种：

1. 直连式（多数 SD/SDXL 图）：``CLIPTextEncode.widgets_values[0]`` 就是提示词；
2. 上游喂入式（FLUX.2 Klein）：``PrimitiveStringMultiline`` → ``CLIPTextEncode.text``；
   编码节点自己的 widget 是空的，写它**不起作用**，必须写上游；
3. 多级管道式（Anime_Consistent）：``Primitive`` → ``ZH2ENTranslator`` →
   ``AIPromptOptimizer`` → ``CLIPTextEncode``，要一直回溯到用户真正手打的那个框；
4. 无标题式（beaty_gen6）：节点没有"正向/负向"字样，只能靠 KSampler 的
   ``negative`` 输入连线来区分正负。
5. 子图式（官方模板 / MiniMax H3）：整条管线被封进 ``definitions.subgraphs``，
   画布上只剩一个 UUID 类型的包装节点，提示词要下钻到子图里取、却只能写到
   画布上的包装节点。
6. 专用节点式（MiniMax-H3-T2V-Local）：根本没有文本编码节点，提示词由
   ``PrimitiveStringMultiline`` 直接喂给专用模型节点。

因此本模块做三件事：
- **沿 STRING 类型连线回溯**，找到真正承载文本、且用户能编辑的那个节点；
- 按「标题 → KSampler 负向连线 → 文本反查」的优先级判定正负，与
  ``parser._pick_prompts`` 的结论保持一致；
- 单层画布 → 子图下钻 → 全图兜底，三级降级保证尽量不交白卷。

本模块是纯函数层：不读写数据库、不做网络 IO。
"""

from __future__ import annotations

from typing import Any

from . import parser

ENCODER_MARK = "CLIPTextEncode"
# 文本编码器远不止 CLIPTextEncode：Qwen-Image 系列用 TextEncodeQwenImageEditPlus、
# 一些整合包用 TextEncode*，只认 CLIPTextEncode 会让这些工作流整个识别不到。
ENCODER_MARKS = ("CLIPTextEncode", "TextEncode")
SAMPLER_PREFIX = "KSampler"
POSITIVE_WORDS = ("positive", "正面", "正向")
NEGATIVE_WORDS = ("negative", "负面", "负向")

# 可能承载文本的 widget 输入口（大小写不敏感）
_STRING_INPUT_NAMES = (
    "text",
    "string",
    "value",
    "prompt",
    "positive",
    "negative",
    "text_g",
    "text_l",
)
_STRING_TYPES = ("STRING", "*")

# 兜底扫描时要跳过的"文本很长但不是提示词"的节点
_NOISE_TYPES = ("MarkdownNote", "Note", "Reroute", "PrimitiveInt", "PrimitiveFloat")
# 模型权重文件名后缀：出现在 widget 里通常不是提示词
_MODEL_EXT = (".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".bin", ".onnx", ".yaml")

MAX_UPSTREAM_DEPTH = 6


# ---------------------------------------------------------------- 索引


def _index(data: dict[str, Any]) -> tuple[dict[str, dict], dict[Any, dict]]:
    """把 UI 图整理成 ``(节点表, 连线表)``，兼容数组式与对象式 links 写法。"""
    nodes: dict[str, dict] = {}
    for raw in data.get("nodes") or []:
        if isinstance(raw, dict):
            nodes[str(raw.get("id"))] = raw

    links: dict[Any, dict] = {}
    for item in data.get("links") or []:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 6:
                links[item[0]] = {
                    "origin": str(item[1]),
                    "target": str(item[3]),
                    "type": str(item[5]),
                }
            elif isinstance(item, dict) and "id" in item:
                links[item["id"]] = {
                    "origin": str(item.get("origin_id", item.get("origin"))),
                    "target": str(item.get("target_id", item.get("target"))),
                    "type": str(item.get("type") or ""),
                }
        except (IndexError, TypeError):
            continue
    return nodes, links


def _is_string_port(port: dict[str, Any]) -> bool:
    name = str(port.get("name") or "").lower()
    ptype = str(port.get("type") or "").upper()
    return name in _STRING_INPUT_NAMES or ptype in _STRING_TYPES


def _widget_text(node: dict[str, Any] | None) -> str:
    """取节点首个文本 widget 的值。

    只看 ``widgets_values[0]``：像 ``AIPromptOptimizer`` 这类节点的第 2、3 个
    widget 是模式/风格枚举（"内置补全"等），当成提示词会严重串味。
    """
    if not node:
        return ""
    values = node.get("widgets_values")
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return ""


def _find_upstream(
    node: dict[str, Any],
    nodes: dict[str, dict],
    links: dict[Any, dict],
    depth: int = 0,
) -> dict[str, Any] | None:
    """沿 STRING 输入口向上回溯，返回**最深**的那个带文本的节点。

    "最深"是关键：像 ``AIPromptOptimizer`` 这类中间节点，自己的
    ``widgets_values[0]`` 装的是模式枚举（"内置补全"之类）而不是提示词，
    取最近的那个就会把枚举值写进画布。一路走到链条末端（用户手打的
    ``PrimitiveStringMultiline``）才是真正要回填的地方。
    """
    if depth >= MAX_UPSTREAM_DEPTH:
        return None
    fallback: dict[str, Any] | None = None
    for port in node.get("inputs") or []:
        if not isinstance(port, dict) or port.get("link") is None:
            continue
        if not _is_string_port(port):
            continue
        link = links.get(port["link"])
        if not link:
            continue
        source = nodes.get(link["origin"])
        if source is None:
            continue
        deeper = _find_upstream(source, nodes, links, depth + 1)
        if deeper is not None:
            return deeper
        if fallback is None and _widget_text(source):
            fallback = source
    return fallback


def text_holder(
    node: dict[str, Any] | None,
    nodes: dict[str, dict],
    links: dict[Any, dict],
) -> tuple[dict[str, Any] | None, str]:
    """返回真正承载文本的 ``(节点, 文本)``。

    节点自身带文本就用自身；否则沿连线回溯到上游那个用户真正手打的框。
    """
    if node is None:
        return None, ""
    own = _widget_text(node)
    if own:
        return node, own
    upstream = _find_upstream(node, nodes, links)
    if upstream is not None:
        return upstream, _widget_text(upstream)
    return node, ""


# ---------------------------------------------------------------- 正负判定


def _ksampler_negative_id(data: dict[str, Any]) -> str:
    """沿 KSampler 的 ``negative`` 输入追溯到负向编码节点 id。"""
    for raw in data.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        if not str(raw.get("type") or "").startswith(SAMPLER_PREFIX):
            continue
        for port in raw.get("inputs") or []:
            if not isinstance(port, dict):
                continue
            if str(port.get("name") or "") != "negative" or port.get("link") is None:
                continue
            for item in data.get("links") or []:
                try:
                    if isinstance(item, (list, tuple)) and len(item) >= 4 and item[0] == port["link"]:
                        return str(item[1])
                    if isinstance(item, dict) and item.get("id") == port["link"]:
                        return str(item.get("origin_id", item.get("origin")))
                except (IndexError, TypeError):
                    continue
    return ""


def _title_hits(node: dict[str, Any], words: tuple[str, ...]) -> bool:
    title = str(node.get("title") or "").lower()
    return any(word in title for word in words)


def _is_prompt_like(text: str) -> bool:
    """粗筛：这段文本像不像一句提示词（而不是模型文件名、数字、下拉枚举）。"""
    raw = (text or "").strip()
    if len(raw) < 12:
        return False
    low = raw.lower()
    if any(low.endswith(ext) for ext in _MODEL_EXT):
        return False
    if low.replace(".", "").replace("-", "").isdigit():
        return False
    return True


def _find_wrapper(top_nodes: dict[str, dict], subgraph: dict[str, Any]) -> dict[str, Any] | None:
    """找到画布上代表该子图的包装节点（其 ``type`` 就是子图的 ``id``）。"""
    sub_id = str(subgraph.get("id") or "")
    if sub_id:
        for node in top_nodes.values():
            if str(node.get("type") or "") == sub_id:
                return node
    # 退化：画布上只有一个"UUID 型"节点时就是它
    uuid_like = [
        n
        for n in top_nodes.values()
        if len(str(n.get("type") or "")) == 36 and str(n.get("type") or "").count("-") == 4
    ]
    return uuid_like[0] if len(uuid_like) == 1 else None


def _subgraph_target(wrapper: dict[str, Any] | None) -> str:
    """算出提示词该写到包装节点的哪个 widget。

    返回 ``"<节点id>"``（默认 text widget）或带提示的 ``"<id>@<widget名>"``
     ``"<id>#<下标>"``，由 ComfyUI 侧桥梁解析。
    """
    if wrapper is None:
        return ""
    node_id = str(wrapper.get("id") or "")
    if not node_id:
        return ""

    # 子图把内部输入框"提升"到包装节点上时，inputs 里会带 widget 定义
    for port in wrapper.get("inputs") or []:
        if not isinstance(port, dict):
            continue
        widget = port.get("widget")
        name = widget.get("name") if isinstance(widget, dict) else None
        if name and _is_string_port(port):
            return f"{node_id}@{name}"

    # 没提升成 input 时，提示词往往就躺在 widgets_values 里，挑最长的那句
    values = wrapper.get("widgets_values")
    if isinstance(values, list):
        best_idx, best_len = -1, 0
        for idx, value in enumerate(values):
            if not isinstance(value, str) or not _is_prompt_like(value):
                continue
            if len(value) > best_len:
                best_idx, best_len = idx, len(value)
        if best_idx >= 0:
            return f"{node_id}#{best_idx}"
    return node_id


def _analyze_subgraph(data: dict[str, Any]) -> dict[str, str]:
    """子图（subgraph）工作流的提示词定位。

    ComfyUI 0.30 起，官方模板与不少整合包会把整条管线包成一个子图：画布上只剩
    一个 UUID 类型的包装节点，真正的 ``CLIPTextEncode`` 藏在
    ``definitions.subgraphs[].nodes`` 里。此时顶层分析一无所获，需要下钻。

    文本取自子图内部（那是真正生效的值），写入目标则是画布上的包装节点——
    子图内部节点不在 ``app.graph._nodes`` 里，桥梁够不着。
    """
    empty = {"positive": "", "negative": "", "positive_node": "", "negative_node": ""}
    subs = (data.get("definitions") or {}).get("subgraphs") or []
    subgraphs = [s for s in subs if isinstance(s, dict) and isinstance(s.get("nodes"), list)]
    if not subgraphs:
        return empty

    top_nodes, _ = _index(data)
    for subgraph in subgraphs:
        inner = _analyze_canvas(
            {"nodes": subgraph.get("nodes"), "links": subgraph.get("links")}
        )
        if not inner.get("positive"):
            continue
        return {
            "positive": inner["positive"],
            "negative": inner.get("negative") or "",
            "positive_node": _subgraph_target(_find_wrapper(top_nodes, subgraph)),
            "negative_node": "",
        }
    return empty


def _analyze_canvas(data: dict[str, Any]) -> dict[str, str]:
    """分析单层画布（不含子图下钻）。"""
    empty = {"positive": "", "negative": "", "positive_node": "", "negative_node": ""}
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return empty

    nodes, links = _index(data)
    encoders = [
        n
        for n in nodes.values()
        if any(mark in str(n.get("type") or "") for mark in ENCODER_MARKS)
    ]
    if not encoders:
        return empty

    # parser 对"标准图"的正负判定很准（标题 → KSampler 连线 → 兜底），复用它的结论
    try:
        parsed = parser.parse_workflow(data)
    except Exception:  # 解析异常不应阻断深链接，降级为纯结构判定
        parsed = {}
    pos_text = str(parsed.get("positive_prompt") or "")
    neg_text = str(parsed.get("negative_prompt") or "")

    negative_node: dict[str, Any] | None = None
    # 1) 标题命中负向
    for node in encoders:
        if _title_hits(node, NEGATIVE_WORDS):
            negative_node = node
            break
    # 2) KSampler 的 negative 连线
    if negative_node is None:
        nid = _ksampler_negative_id(data)
        if nid and nid in nodes:
            negative_node = nodes[nid]
    # 3) 文本反查
    if negative_node is None and neg_text:
        for node in encoders:
            if _widget_text(node) == neg_text.strip():
                negative_node = node
                break

    positive_node: dict[str, Any] | None = None
    # 1) 标题命中正向
    for node in encoders:
        if node is negative_node:
            continue
        if _title_hits(node, POSITIVE_WORDS):
            positive_node = node
            break
    # 2) 文本反查
    if positive_node is None and pos_text:
        for node in encoders:
            if node is negative_node:
                continue
            if _widget_text(node) == pos_text.strip():
                positive_node = node
                break
    # 3) 兜底：第一个非负向的编码节点
    if positive_node is None:
        for node in encoders:
            if node is not negative_node:
                positive_node = node
                break

    pos_holder, resolved_pos = text_holder(positive_node, nodes, links)
    neg_holder, resolved_neg = text_holder(negative_node, nodes, links)

    return {
        "positive": resolved_pos or pos_text,
        "negative": resolved_neg or neg_text,
        "positive_node": str(pos_holder.get("id")) if pos_holder else "",
        "negative_node": str(neg_holder.get("id")) if neg_holder else "",
    }


def _analyze_loose(data: dict[str, Any]) -> dict[str, str]:
    """最后兜底：整张图都没有文本编码节点时，挑最长的一段"像提示词"的文本。

    命中这类图的多是专用模型节点（如 ``MiniMaxH3ImageToVideo`` 直接吃
    ``PrimitiveStringMultiline`` 的输出），编码链路上没有 ``CLIPTextEncode``
    可锚定。此时退而求其次：全图扫一遍，取最长的自然语言片段。

    只按文本筛选、不做节点类型白名单，因此放在最后一步——前面两种更精确
    的判定都失败时才轮到它。
    """
    empty = {"positive": "", "negative": "", "positive_node": "", "negative_node": ""}
    nodes, _links = _index(data)
    best: dict[str, Any] | None = None
    best_len = 0
    for node in nodes.values():
        if any(mark in str(node.get("type") or "") for mark in _NOISE_TYPES):
            continue
        text = _widget_text(node)
        if not _is_prompt_like(text) or len(text) < 16:
            continue
        if len(text) > best_len:
            best, best_len = node, len(text)
    if best is None:
        return empty
    return {
        "positive": _widget_text(best),
        "negative": "",
        "positive_node": str(best.get("id") or ""),
        "negative_node": "",
    }


def analyze_ui_graph(data: Any) -> dict[str, str]:
    """分析 UI 图，给出正负提示词及其「应写入的节点 id」。

    三级降级：单层画布 → 子图下钻 → 全图兜底。

    Returns:
        ``{"positive", "negative", "positive_node", "negative_node"}``。
        输入非 UI 图或实在定位不到时，四个字段全为空字符串——调用方据此降级。
        ``positive_node`` 可能是 ``"12"``、``"12@text"`` 或 ``"12#0"``，
        后两种用于子图包装节点，由 ComfyUI 侧桥梁解析。
    """
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return {"positive": "", "negative": "", "positive_node": "", "negative_node": ""}

    canvas = _analyze_canvas(data)
    if canvas.get("positive"):
        return canvas
    subgraph = _analyze_subgraph(data)
    if subgraph.get("positive"):
        return subgraph
    return _analyze_loose(data)


__all__ = ["analyze_ui_graph", "text_holder", "ENCODER_MARK"]
