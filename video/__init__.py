"""M15 · 视频转序列帧 / GIF。

对外只有 ``video.routes.bp``（Flask 蓝图，``url_prefix=/api/video``），由
``app.py`` 注册。业务实现全部在 ``video.service``，路由层只做参数校验与转发。
"""

from . import service

__all__ = ["service"]
