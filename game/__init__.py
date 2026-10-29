"""M18 · 游戏工作台（内嵌 XSXB Animation Director）的站点侧支撑。

XSXB 本身是纯前端应用，本站不复制它的源码也不复制它的素材，只做两件事：
把构建产物当静态资源托管（见 ``static/game/``），以及把 ``/local-assets/``
代理回 XSXB 的素材目录——否则它取不到动作库清单。
"""

from __future__ import annotations

from .routes import bp

__all__ = ["bp"]
