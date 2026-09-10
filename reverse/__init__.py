"""M9-M11 · 图片提示词反推与视觉 Provider 中心。

分层（依赖方向严格单向：config/core ← promptlib ← reverse）：
- ``schema``          M9.4 结构化结果定义与严格校验；
- ``uploads``         上传内容校验、受控缓存与过期清理；
- ``comfyui_client``  ComfyUI HTTP API 客户端（全部带超时，不抛未捕获异常）；
- ``providers``       可插拔视觉 Provider 与注册表；
- ``service``         反推任务编排、阶段进度、降级与 M9.6 入库流水线；
- ``routes``          Flask Blueprint（``/api/prompt-reverse/*``）。

隐私约束（PRD M9.9 / M10.4）：对外只暴露受控相对引用，
绝不返回或记录用户原始绝对路径、API Key 与完整外部 endpoint。

本包不导入 ``routes``，避免装配期循环依赖；由 ``app.py`` 显式注册蓝图。
"""

from __future__ import annotations

__all__ = ["schema", "uploads", "comfyui_client", "service", "providers"]
