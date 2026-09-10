"""全项目统一的「错误码 → 中文可读文案」表。

为什么单独成模块
----------------
错误码最初只在 ``comic/diagnostics.py`` 里有一份中文映射，但反推（``reverse``）
链路抛出的 ``comfyui_offline`` / ``missing_node`` / ``provider_offline`` 等码
在前端是**直接渲染**的 —— 用户看到一串英文机器码，既不知道为什么失败，也不知道该怎么办。

把映射收到这里，做到：

- **单一数据源**：后端改文案，所有模块与前端同步生效；
- **跨模块复用**：``comic`` 与 ``reverse`` 共用 ComfyUI 侧的错误码，不必各写一份；
- **可暴露给前端**：经 ``GET /api/error-codes`` 下发，前端不再维护副本。
"""

from __future__ import annotations

from typing import Any

# 错误码 → 中文可读文案
ERROR_TEXT: dict[str, str] = {
    # ---- ComfyUI 侧（comic 与 reverse 共用） ----
    "comfyui_offline": "ComfyUI 未启动或不可达（127.0.0.1:8188 无响应）",
    "comfyui_timeout": "ComfyUI 响应超时",
    "submit_failed": "提交到 ComfyUI 失败",
    "workflow_failed": "ComfyUI 执行工作流失败",
    "cancelled": "任务已取消",
    "upload_failed": "上传图片到 ComfyUI 失败",
    "missing_node": "工作流引用了 ComfyUI 未安装的节点",
    "out_of_memory": "显存/内存不足，请降低分辨率或关闭其他任务",
    "missing_model": "缺少模型文件，请在 ComfyUI 中确认模型已下载",
    "missing_mmproj": "缺少多模态投影文件（mmproj / clip vision）",
    "incompatible_runtime": "ComfyUI 运行环境不兼容（依赖缺失或版本不匹配）",
    # ---- 出图执行器（comic） ----
    "workflow_missing": "工作流文件不存在",
    "read_error": "读取工作流文件失败",
    "invalid_workflow": "工作流无法解析（可能不是 ComfyUI API 图或 UI 图）",
    "convert_failed": "工作流转换失败（多为无法拉取节点定义）",
    "empty_workflow": "工作流没有可执行节点",
    "internal_error": "内部错误",
    "not_found": "页面或漫画已删除",
    "wait_failed": "等待 ComfyUI 结果失败",
    "timeout": "等待 ComfyUI 出图超时（>3 分钟）",
    "no_output": "ComfyUI 未产出图片（工作流缺少 SaveImage 节点？）",
    "download_failed": "下载产出图失败",
    "save_failed": "保存产出图失败",
    "interrupted": "上次进程退出时中断，请重新出图",
    # ---- 反推（reverse） ----
    "bad_response": "AI 服务返回了无法解析的内容",
    "empty_file": "文件内容为空，无法解析",
    "empty_result": "反推没有产出任何结果",
    "invalid_image": "图片无法识别（格式不支持或已损坏）",
    "invalid_provider_result": "AI 服务返回的结果格式不正确",
    "provider_busy": "AI 服务繁忙，请稍后重试",
    "provider_not_found": "未找到可用的 AI 服务，请先在设置中配置",
    "provider_offline": "AI 服务不可达，请检查网络与密钥配置",
    "unauthorized": "AI 服务鉴权失败，请检查密钥是否正确",
    "reverse_failed": "反推失败",
    # ---- 通用 ----
    "invalid_input": "请求参数不正确",
    "network_error": "网络连接失败，请检查服务是否运行",
}


def human_error(code: Any, message: Any = None) -> str:
    """错误码 + 原始信息 → 中文可读文案。

    优先级：映射表命中的中文 → 原始 message → 错误码本身。
    永远返回非空可读字符串，绝不把裸机器码丢给用户。
    """
    key = str(code or "").strip()
    text = str(message or "").strip()
    if key and key in ERROR_TEXT:
        # 原始 message 只是「错误码的英文复述」时用中文映射，避免出现两条重复信息
        return ERROR_TEXT[key]
    if text:
        return text
    return key or "未知错误"
