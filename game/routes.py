"""M18 · 游戏工作台路由（url_prefix 为空：内嵌应用用的是根相对路径）。

XSXB Animation Director 取动作库时固定请求 ``/local-assets/animation-packs.json``，
且清单里的 clip url 形如 ``/local-assets/packs/<pack>/<file>.glb``。
这两个路径都**不受** ``--base`` 影响（是硬编码的根相对路径），所以在子目录下
托管 iframe 时，必须由本站把它们代理到 XSXB 的真实素材目录。

素材（16MB GLB）不进本仓库：它是用户自己下载的第三方包，且 XSXB 自己的边界就
写着「仓库只发布工具代码，不附带任何动作素材」。代理而非复制还带来一个好处——
在 XSXB 里增删素材，游戏工作台无需重新构建就能看到。
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, abort, send_from_directory

from config import Config
from core import logging_setup

bp = Blueprint("game", __name__)

logger = logging_setup.get_logger("aibar.game")


def _assets_root() -> Path | None:
    """素材目录：优先 ``XSXB_LOCAL_ASSETS_DIR``，否则回落到默认位置。"""
    configured = Config.XSXB_LOCAL_ASSETS_DIR
    if configured:
        return configured
    fallback = Config.ROOT.parent / "XSXB-animation-director" / "dist" / "local-assets"
    return fallback


@bp.get("/local-assets/<path:filename>")
def local_assets(filename: str):
    """把 ``/local-assets/**`` 代理到 XSXB 的动作素材目录。

    目录不存在或文件缺失时返回 404 而不是 500——内嵌应用会按「无素材」降级，
    界面提示用户载入素材，而不是整个页面报错。
    """
    root = _assets_root()
    if root is None or not root.is_dir():
        abort(404)

    # 防目录穿越：send_from_directory 本身会校验，这里再显式拒绝绝对路径片段
    if filename.startswith("/") or ".." in Path(filename).parts:
        abort(404)

    target = root / filename
    if not target.is_file():
        logging_setup.safe_log(
            logger,
            logging.INFO,
            "game_asset_missing",
            name=filename,
        )
        abort(404)

    return send_from_directory(str(root), filename)
