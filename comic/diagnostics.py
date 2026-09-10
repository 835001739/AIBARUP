"""M12 · 出图前置诊断与可读错误文案。

目标：把「点了出图没反应 / 一堆任务莫名失败」这类黑盒问题，变成**出图前就能看到的体检报告**
与**失败后能读懂的原因**，从而显著提升与 ComfyUI 的衔接体验。

设计约束（沿用 HARNESS / PRD）：
- **只读、可失败降级**：诊断不写数据库、不改动任何状态；ComfyUI 不可达或接口异常时
  只把对应项标记为「未知」并给出提示，**绝不抛异常到路由**；
- **不泄漏细节**：只暴露工作流内节点类名、队列长度等安全信息。
"""

from __future__ import annotations

from typing import Any

import requests

from config import Config
from core.logging_setup import get_logger, safe_log
from sync import workflow_convert
from reverse import comfyui_client as _cc
from . import runner as _runner
from . import service

_LOGGER = get_logger("aibar.comic.diagnostics")

# 错误码 → 中文可读文案（前端直接展示，避免用户看到英文机器码）
#
# 映射表现在是全项目共用的一份，放在 ``core/error_text.py``：
# 反推（reverse）链路抛的是同一批 ComfyUI 错误码，以前前端会把它们原样渲染给用户。
# 这里保留 ``ERROR_TEXT`` / ``human_error`` 两个名字，既有调用方与测试无需改动。
from core.error_text import ERROR_TEXT, human_error  # noqa: F401  (向后兼容的重导出)


def human_error(code: Any, message: Any = None) -> str:
    """错误码 → 中文文案；未知码回退原始 message，再回退码本身。"""
    key = str(code or "").strip()
    if key in ERROR_TEXT:
        return ERROR_TEXT[key]
    text = str(message or "").strip()
    return text or (key or "未知错误")


# ---------------------------------------------------------------- 单项探测


def probe_comfyui(timeout: float = 4.0) -> dict:
    """探测 ComfyUI 是否可达。"""
    try:
        online, latency = _cc.is_reachable(timeout=timeout)
    except Exception:
        online, latency = False, 0
    return {"online": bool(online), "latency_ms": int(latency or 0), "base_url": Config.comfyui_base()}


def _queue_snapshot(timeout: float = 5.0) -> dict:
    """读取 ComfyUI 队列长度；不可读时返回 -1（未知）。"""
    try:
        resp = requests.get(f"{Config.comfyui_base()}/queue", timeout=timeout)
        if resp.status_code != 200:
            return {"running": -1, "pending": -1}
        data = resp.json()
        running = data.get("queue_running")
        pending = data.get("queue_pending")
        return {
            "running": len(running) if isinstance(running, list) else -1,
            "pending": len(pending) if isinstance(pending, list) else -1,
        }
    except Exception:
        return {"running": -1, "pending": -1}


def workflow_report(filename: str, check_nodes: bool = False) -> dict:
    """检查工作流的可用性：文件是否存在、节点数、以及（可选）节点是否都已在 ComfyUI 安装。"""
    report: dict[str, Any] = {
        "filename": (filename or "").strip(),
        "exists": False,
        "node_count": 0,
        "class_types": [],
        "missing_nodes": [],
        "nodes_checked": False,
    }
    target = _runner._workflow_file(report["filename"])
    if target is None:
        return report
    report["exists"] = True
    try:
        raw = target.read_text(encoding="utf-8", errors="ignore")
        graph = workflow_convert.convert_text(raw)
    except Exception:
        return report
    if not isinstance(graph, dict) or not graph:
        return report
    classes = sorted(
        {
            str(n.get("class_type") or "")
            for n in graph.values()
            if isinstance(n, dict) and n.get("class_type")
        }
    )
    report["node_count"] = len(graph)
    report["class_types"] = classes

    if check_nodes:
        try:
            info = _cc.object_info(None, timeout=20.0)
        except Exception:
            info = None
        if isinstance(info, dict) and info:
            report["nodes_checked"] = True
            report["missing_nodes"] = [c for c in classes if c not in info]
    return report


# ---------------------------------------------------------------- 综合体检


def precheck(project_id: int, check_nodes: bool = True) -> dict:
    """出图前置体检：ComfyUI 在线？工作流在不在？节点装没装？队列堵不堵？

    Returns:
        ``{project_id, ready, comfyui, workflow, queue, hints, checked_at}``
        ``ready`` 为 True 表示「现在点出图大概率能跑通」；``hints`` 是中文可操作建议。
    """
    from core.db import now

    project = service.get_project(project_id)
    filename = (project.get("default_workflow") or "").strip()

    comfyui = probe_comfyui()
    wf = workflow_report(filename, check_nodes=check_nodes and comfyui["online"])
    queue = _queue_snapshot() if comfyui["online"] else {"running": -1, "pending": -1}

    hints: list[str] = []
    if not comfyui["online"]:
        hints.append("ComfyUI 未启动：请先在左侧点「启动 ComfyUI」，或手动运行 scripts/up.sh start comfyui")
    if not filename:
        hints.append("项目尚未设置默认工作流：生成分镜时会自动写入 FLUX.2 工作流，或手动指定")
    elif not wf["exists"]:
        hints.append("工作流文件不存在（%s）：请重新导入 Skill 或重新生成分镜" % filename)
    if wf["missing_nodes"]:
        hints.append(
            "工作流缺少节点：%s —— 请在 ComfyUI 中安装对应自定义节点"
            % "、".join(wf["missing_nodes"][:8])
        )
    if isinstance(queue.get("pending"), int) and queue["pending"] > 0:
        hints.append("ComfyUI 队列中还有 %d 个任务，出图会排队等待" % queue["pending"])
    if not hints:
        hints.append("一切就绪，可以出图")

    ready = bool(comfyui["online"] and wf["exists"] and not wf["missing_nodes"])
    result = {
        "project_id": project_id,
        "ready": ready,
        "comfyui": comfyui,
        "workflow": wf,
        "queue": queue,
        "hints": hints,
        "checked_at": now(),
    }
    safe_log(_LOGGER, 20 if ready else 30, "comic_precheck", project_id=project_id, ready=ready)
    return result


__all__ = ["ERROR_TEXT", "human_error", "probe_comfyui", "workflow_report", "precheck"]
