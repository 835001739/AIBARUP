"""M2/M3 联动：构造「打开 ComfyUI 并载入工作流 + 提示词」的深链接。

背景
----
AIBAR（:8099）与 ComfyUI（:8188）是两个不同的源，浏览器同源策略禁止 AIBAR
直接往 ComfyUI 页面里注入 ``loadGraphData``。因此这里采用**协议式深链接**：

1. AIBAR 给出一个工作流 JSON 的直链（带 CORS 头）；
2. ComfyUI 侧的桥梁扩展（``custom_nodes/AIBAR-Bridge``）在页面启动时读取 URL 参数，
   自行 fetch 该 JSON 并 ``app.loadGraphData`` 载入画布；
3. 提示词通过同样的 URL 参数传递，由扩展写入对应的文本 widget。

这样 AIBAR 不需要任何 ComfyUI 页面的控制权，ComfyUI 也不需要知道 AIBAR 的存在，
双方只依赖一份公开的 URL 参数约定。

工作流来源优先级（针对"点图片/案例"的场景）
-----------------------------------------
1. **图片内嵌的 UI 工作流**（PNG 的 ``workflow`` 文本块）——这是真正产出该图的
   那张画布，最准确；提示词也一并从这张图里解析，节点 id 天然对得上。
2. **按 ``workflow_link`` 回工作流库匹配**——图片没有内嵌工作流时的退路。
   注意：库里的文件名与图片名常常对不上（如图片 ``FLUX2_Klein_4B_00337_`` ↔
   工作流 ``FLUX2-Klein-4B-Text-to-Image-Test.json``），命中率有限。
3. 都没有 → 只打开 ComfyUI 首页，前端据此提示。

桥梁扩展未安装时本模块依然工作，只是链接退化为「打开 ComfyUI 首页」。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from config import Config
from core.db import query_one
from core import errors
from core.imagemeta import read_png_text_chunks
from core.logging_setup import get_logger, safe_log

from . import graph_prompt, parser, paths

logger = get_logger("aibar.sync.comfyui_link")

# ComfyUI 侧桥梁扩展的安装位置（相对 COMFYUI_DIR）
BRIDGE_DIRNAME = "AIBAR-Bridge"
BRIDGE_JS_REL = "js/aibar_bridge.js"

# URL 参数名：必须与 ComfyUI 桥梁扩展保持一致
PARAM_GRAPH = "aibar_wf"
PARAM_PROMPT = "aibar_prompt"
PARAM_NEGATIVE = "aibar_neg"
PARAM_TARGET = "aibar_target"
PARAM_NAME = "aibar_name"

# 提示词经 URL 传递，做上限保护（浏览器与地址栏都对超长 URL 不友好）
MAX_PROMPT_LEN = 8000

# 从被截断的 JSON 片段里抢救提示词：匹配 "value" / "text" 后面足够长的内容
_FRAGMENT_RE = re.compile(r'"(?:value|text)"\s*:\s*"((?:[^"\\]|\\.){20,})"')


# ---------------------------------------------------------------- 基础探测


def bridge_installed() -> bool:
    """ComfyUI 的 AIBAR 桥梁扩展是否已安装（只看文件，不启动进程）。"""
    if not Config.COMFYUI_DIR:
        return False
    target = Path(Config.COMFYUI_DIR).expanduser() / "custom_nodes" / BRIDGE_DIRNAME / BRIDGE_JS_REL
    try:
        return target.is_file()
    except OSError:
        return False


def aibar_base() -> str:
    """AIBAR 对外的绝对基址：ComfyUI 页面要用它来反向拉取工作流 JSON。"""
    base = (Config.AIBAR_PUBLIC_BASE or "").strip().rstrip("/")
    return base or f"http://{Config.HOST}:{Config.PORT}"


def comfyui_base() -> str:
    return f"http://{Config.COMFYUI_HOST}:{Config.COMFYUI_PORT}"


# ------------------------------------------------------------ 提示词清洗


def split_prompt(text: str | None) -> tuple[str, str]:
    """把图库里的 ``prompt`` 字段还原成 (正向, 负向)。

    入库时用的是 ``parser.combine_prompt``（``正向\\nNegative: 负向``），
    这里做对称还原；没有负向标记时整段视为正向。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    for sep in ("\nNegative: ", "\nNegative:", "\n负向：", "\n负面："):
        idx = raw.find(sep)
        if idx > 0:
            return raw[:idx].strip(), raw[idx + len(sep):].strip()
    return raw, ""


def clean_stored_prompt(text: str | None) -> str:
    """清洗图库里存的提示词，尽量还原成可直接使用的自然语言。

    ``images.prompt`` 混着三类脏数据：完整的 API 图 JSON、被截断的 JSON 片段
    （元数据超长被截断，只留第一行）、以及正常的提示词文本。前两类直接塞进
    ComfyUI 的文本框毫无意义，这里做尽力而为的还原；还原不了就返回空串，
    由调用方决定"不填提示词"，好过填一堆 JSON。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    if not raw.startswith("{"):
        return split_prompt(raw)[0] or raw

    # 完整 JSON：走解析器取正向
    try:
        parsed = parser.parse_workflow(json.loads(raw))
        return str(parsed.get("positive_prompt") or "")
    except (ValueError, TypeError):
        pass

    # 截断 JSON：正则抢救 value/text 字段里最长的那段
    matches = _FRAGMENT_RE.findall(raw)
    if matches:
        candidate = max(matches, key=len)
        try:
            return str(json.loads('"' + candidate + '"'))
        except ValueError:
            return candidate
    return ""


# -------------------------------------------------------- 工作流定位


def resolve_workflow_filename(link: str) -> str:
    """把图片的 ``workflow_link``（工作流名）解析为工作流库里的 ``filename``。

    匹配顺序：``name`` 精确相等 → ``filename`` 相等 → ``filename`` 去扩展名相等。
    命中不到返回空串（调用方降级）。
    """
    key = (link or "").strip()
    if not key:
        return ""
    try:
        row = query_one(
            "SELECT filename FROM workflows"
            " WHERE name = ? OR filename = ? OR filename = ? LIMIT 1",
            (key, key, key + ".json"),
        )
    except Exception as exc:  # 库未迁移/临时故障：降级而不是 500
        safe_log(logger, logging.WARNING, "workflow_link_lookup_failed", error_code=type(exc).__name__)
        return ""
    return str(row["filename"]) if row else ""


def workflow_file_analysis(filename: str) -> dict[str, str]:
    """读工作流文件，用 ``graph_prompt`` 分析出提示词文本与待写入的节点 id。

    返回 ``{"positive", "negative", "positive_node", "negative_node"}``，
    读不到/不是 UI 图时返回空 dict。

    为什么不直接用 ``workflows`` 表里已存的 ``positive_prompt``：那是由
    ``parser`` 抽的，遇到 ``PrimitiveStringMultiline → CLIPTextEncode``、
    ``AIPromptOptimizer`` 这类"提示词不在编码器 widget 里"的拓扑会抽成空串
    （实测 25 个工作流里有 20 个是空的）。``graph_prompt`` 会沿 STRING 连线
    回溯到真正可编辑的文本节点，命中率显著更高。
    """
    # 文件名直接来自 URL 查询参数（?workflow=...），必须走 safe_join 防穿越。
    # 别再用 startswith 判断前缀——它挡不住兄弟目录。
    target = paths.safe_join(paths.workflows_dir(), filename)
    if target is None:
        return {}
    try:
        if not target.is_file():
            return {}
        data = json.loads(target.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return {}
    return graph_prompt.analyze_ui_graph(data)


def image_embedded_graph(image_id: str) -> dict[str, Any] | None:
    """读取图片内嵌的 **UI 格式**工作流（PNG 的 ``workflow`` 文本块）。

    这是真正产出该图片的那张画布，比按名字回库匹配准确得多。
    返回 None 表示：图片不存在 / 源文件已删除 / 没有内嵌工作流 / 不是 UI 图。
    """
    if not image_id:
        return None
    try:
        row = query_one("SELECT source_path FROM images WHERE id = ?", (image_id,))
    except Exception as exc:
        safe_log(logger, logging.WARNING, "embedded_graph_lookup_failed", error_code=type(exc).__name__)
        return None
    if row is None:
        return None

    path = Path(str(row["source_path"] or ""))
    try:
        if not path.is_file() or path.suffix.lower() != ".png":
            return None
        raw = read_png_text_chunks(path).get("workflow")
        if not raw:
            return None
        graph = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        return None
    return graph


# ------------------------------------------------------------ 链接构造


def build_editor_link(
    workflow: str = "",
    image_id: str = "",
    prompt: str = "",
    negative: str = "",
) -> dict[str, Any]:
    """生成一个可直接 ``window.open`` 的 ComfyUI 深链接。

    Args:
        workflow: 工作流文件名（库内相对名）。留空时尝试由 ``image_id`` 反查。
        image_id: 图库/案例 id。用于取内嵌工作流与提示词。
        prompt: 显式指定的正向提示词，优先于自动推导。
        negative: 显式指定的负向提示词，优先于自动推导。

    Returns:
        ``{"url", "comfyui_url", "mode", "workflow", "workflow_name", "prompt",
        "negative", "target", "bridge_installed", "source"}``；
        ``mode`` 取值 ``embedded``（图片内嵌工作流）/ ``library``（工作流库匹配）
        / ``none``（只打开 ComfyUI）。
    """
    explicit_workflow = (workflow or "").strip()
    explicit_prompt = (prompt or "").strip()
    explicit_negative = (negative or "").strip()

    graph_url = ""
    workflow_name = ""
    resolved_workflow = ""
    target = ""
    source = "explicit"
    mode = "none"
    resolved_prompt = explicit_prompt
    resolved_negative = explicit_negative

    if image_id:
        row = None
        try:
            row = query_one("SELECT * FROM images WHERE id = ?", (image_id,))
        except Exception as exc:
            safe_log(logger, logging.WARNING, "editor_link_image_failed", error_code=type(exc).__name__)
        if row is None:
            # 走工厂而不是手写 code：此前这里漏了 status，404 语义的错误一直按 400 返回
            raise errors.not_found("图片不存在")

        stem = Path(str(row["filename"] or "")).stem or str(image_id)[:8]

        # 途径 1：图片内嵌的 UI 工作流（最准确）
        embedded = image_embedded_graph(image_id)
        if embedded is not None:
            analysis = graph_prompt.analyze_ui_graph(embedded)
            graph_url = f"{aibar_base()}/api/gallery/{quote(image_id, safe='')}/workflow"
            workflow_name = stem
            target = analysis.get("positive_node") or ""
            if not resolved_prompt:
                resolved_prompt = analysis.get("positive") or ""
            if not resolved_negative:
                resolved_negative = analysis.get("negative") or ""
            mode = "embedded"
            source = "image"
        elif not explicit_workflow:
            # 途径 2：按 workflow_link 回工作流库匹配
            resolved_workflow = resolve_workflow_filename(str(row["workflow_link"] or ""))
            if resolved_workflow:
                workflow_name = Path(resolved_workflow).stem
                analysis = workflow_file_analysis(resolved_workflow)
                target = analysis.get("positive_node") or ""
                if not resolved_prompt:
                    resolved_prompt = analysis.get("positive") or ""
                if not resolved_negative:
                    resolved_negative = analysis.get("negative") or ""
                mode = "library"
            if not resolved_prompt and not resolved_negative:
                stored = split_prompt(row["prompt"])
                resolved_prompt = clean_stored_prompt(stored[0])
                resolved_negative = stored[1]
                if not resolved_prompt:
                    resolved_prompt = clean_stored_prompt(row["prompt"])
                source = "image"
            elif not resolved_workflow:
                source = "image"

    if explicit_workflow and not graph_url:
        resolved_workflow = explicit_workflow
        workflow_name = Path(resolved_workflow).stem
        analysis = workflow_file_analysis(resolved_workflow)
        target = analysis.get("positive_node") or ""
        if not resolved_prompt:
            resolved_prompt = analysis.get("positive") or ""
        if not resolved_negative:
            resolved_negative = analysis.get("negative") or ""
        graph_url = f"{aibar_base()}/api/workflows/{quote(resolved_workflow, safe='/')}/graph"
        mode = "library"

    # 工作流库兜底提示词：图片自己没带，就用工作流文件里存的
    if resolved_workflow and not resolved_prompt:
        try:
            wf = query_one(
                "SELECT positive_prompt, negative_prompt FROM workflows WHERE filename = ?",
                (resolved_workflow,),
            )
        except Exception as exc:
            safe_log(logger, logging.WARNING, "editor_link_wf_failed", error_code=type(exc).__name__)
            wf = None
        if wf:
            resolved_prompt = str(wf["positive_prompt"] or "").strip()
            if not resolved_negative:
                resolved_negative = str(wf["negative_prompt"] or "").strip()
            if source == "explicit":
                source = "workflow"

    params: list[tuple[str, str]] = []
    if graph_url:
        params.append((PARAM_GRAPH, graph_url))
        params.append((PARAM_NAME, workflow_name))
        # 提示词只在确实载入了工作流时才带上。没有工作流却带提示词的话，
        # 桥梁会把它写进用户当前画布的某个文本框，等于毁掉人家手头的工作。
        if resolved_prompt:
            params.append((PARAM_PROMPT, resolved_prompt[:MAX_PROMPT_LEN]))
        if resolved_negative:
            params.append((PARAM_NEGATIVE, resolved_negative[:MAX_PROMPT_LEN]))
        if target:
            params.append((PARAM_TARGET, target))

    base = comfyui_base()
    url = base + "/"
    if params:
        url += "?" + urlencode(params, doseq=False, safe=":/")

    return {
        "url": url,
        "comfyui_url": base,
        "mode": mode,
        "workflow": resolved_workflow,
        "workflow_name": workflow_name,
        "prompt": resolved_prompt,
        "negative": resolved_negative,
        "target": target,
        "bridge_installed": bridge_installed(),
        "source": source,
    }


def build_editor_link_graph(
    graph_url: str,
    name: str = "",
    prompt: str = "",
    negative: str = "",
    target: str = "",
) -> dict[str, Any]:
    """构造一个「打开 ComfyUI 并载入**动态生成的工作流直链**」的深链接。

    与 :func:`build_editor_link` 的区别：这里的工作流不是工作流库里的静态文件，
    而是运行时由 AIBAR 后端按业务参数（如某帧的姿态重绘图）即时生成的 UI 格式
    JSON 直链。调用方负责把 ``graph_url`` 指向一个返回 UI 工作流的 HTTP 路由
    （如 ``/api/videopaint/jobs/<id>/frames/<order>/workflow-graph``）。

    其余语义（CORS、提示词只在有工作流时带、``bridge_installed`` 降级）与
    :func:`build_editor_link` 完全一致。

    Args:
        graph_url: 返回 UI 格式工作流 JSON 的直链（需带 CORS 头、``no-store``）。
        name: 载入后在 ComfyUI 标题栏显示的工作流名。
        prompt: 正向提示词（写入目标文本节点）。
        negative: 负向提示词。
        target: 提示词应写入的节点 id（由调用方依据工作流拓扑给出，可空）。

    Returns:
        同 :func:`build_editor_link` 的 dict；``mode`` 恒为 ``graph``，
        ``workflow`` 为空串。
    """
    clean_url = (graph_url or "").strip()
    if not clean_url:
        # 没有可用直链：退化成只打开 ComfyUI 首页
        base = comfyui_base()
        return {
            "url": base + "/",
            "comfyui_url": base,
            "mode": "none",
            "workflow": "",
            "workflow_name": (name or "").strip(),
            "prompt": (prompt or "").strip(),
            "negative": (negative or "").strip(),
            "target": (target or "").strip(),
            "bridge_installed": bridge_installed(),
            "source": "graph",
        }

    resolved_prompt = (prompt or "").strip()
    resolved_negative = (negative or "").strip()
    workflow_name = (name or "").strip()
    resolved_target = (target or "").strip()

    params: list[tuple[str, str]] = [(PARAM_GRAPH, clean_url)]
    if workflow_name:
        params.append((PARAM_NAME, workflow_name))
    if resolved_prompt:
        params.append((PARAM_PROMPT, resolved_prompt[:MAX_PROMPT_LEN]))
    if resolved_negative:
        params.append((PARAM_NEGATIVE, resolved_negative[:MAX_PROMPT_LEN]))
    if resolved_target:
        params.append((PARAM_TARGET, resolved_target))

    base = comfyui_base()
    url = base + "/?" + urlencode(params, doseq=False, safe=":/")

    return {
        "url": url,
        "comfyui_url": base,
        "mode": "graph",
        "workflow": "",
        "workflow_name": workflow_name,
        "prompt": resolved_prompt,
        "negative": resolved_negative,
        "target": resolved_target,
        "bridge_installed": bridge_installed(),
        "source": "graph",
    }


__all__ = [
    "BRIDGE_DIRNAME",
    "BRIDGE_JS_REL",
    "bridge_installed",
    "aibar_base",
    "comfyui_base",
    "split_prompt",
    "clean_stored_prompt",
    "resolve_workflow_filename",
    "workflow_file_analysis",
    "image_embedded_graph",
    "build_editor_link",
    "build_editor_link_graph",
]
