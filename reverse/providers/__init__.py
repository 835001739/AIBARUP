"""M9.5 / M10 / M11 可插拔视觉 Provider。

全部 Provider 实现同一接口 ``analyze_image(image_ref, profile, precision, options)``，
返回经 ``reverse.schema.validate_structured_result`` 校验过的结构化结果。
"""

from __future__ import annotations

__all__ = ["base", "registry", "metadata", "comfyui_blip", "qwen3vl", "joycaption", "openai_compatible"]
