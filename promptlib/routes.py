"""M7 精品提示词库的 HTTP 接口层（Blueprint ``promptlib``，前缀 ``/api``）。

约束（HARNESS §3 / §5、API_CONTRACT M7）：
- 统一 envelope：``core.responses.ok`` / ``fail``；
- 业务异常抛 ``AIBARError``，由 Blueprint 级 errorhandler 统一转换；
- 入参先校验（枚举、长度、分页上限、ID 存在性）再进入业务层，业务层做二次校验；
- 日志只记录筛选维度、数量与长度，绝不记录提示词正文。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, request

from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from core.responses import from_exception, ok

from . import entries, learning

logger = get_logger("aibar.promptlib.routes")

bp = Blueprint("promptlib", __name__, url_prefix="/api")


# ---------------------------------------------------------------- 入参处理


def _json_body() -> dict:
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    return body


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _filters_from_args() -> dict:
    """把查询串收集成业务层筛选字典；空值交给 ``entries._normalize_filters`` 兜底。"""
    return {
        "media_type": (request.args.get("media_type") or "").strip(),
        "dimension": (request.args.get("dimension") or "").strip(),
        "subcategory": (request.args.get("subcategory") or "").strip(),
        "profile": (request.args.get("profile") or "").strip(),
        "q": (request.args.get("q") or "").strip(),
        "favorite": (request.args.get("favorite") or "").strip(),
        "source": (request.args.get("source") or "").strip(),
        "tag": (request.args.get("tag") or "").strip(),
        "sort": (request.args.get("sort") or "").strip(),
        "page": (request.args.get("page") or "").strip(),
        "page_size": (request.args.get("page_size") or "").strip(),
    }


# ---------------------------------------------------------------- 检索与分面


@bp.get("/prompt-library/facets")
def facets_route():
    """当前筛选条件下仍有数据的层级选项与数量（渐进筛选，无空层级）。"""
    filters = _filters_from_args()
    facets = entries.get_facets(filters)
    safe_log(
        logger,
        logging.INFO,
        "facets",
        media_type=filters["media_type"],
        dimension=filters["dimension"],
        dimension_count=len(facets["dimensions"]),
    )
    return ok(facets)


@bp.get("/prompt-library/entries")
def list_entries_route():
    """分页查询正式词库。"""
    filters = _filters_from_args()
    result = entries.list_entries(filters)
    safe_log(
        logger,
        logging.INFO,
        "list_entries",
        media_type=filters["media_type"],
        dimension=filters["dimension"],
        sort=filters["sort"] or "recommended",
        total=result["total"],
        returned=len(result["items"]),
    )
    return ok(result)


@bp.post("/prompt-library/entries")
def create_entry_route():
    """手动新增个人词条。"""
    body = _json_body()
    item = entries.create_entry(body)
    safe_log(logger, logging.INFO, "create_entry", media_type=item["media_type"], dimension=item["dimension"])
    return ok(item, 201)


@bp.patch("/prompt-library/entries/<int:eid>")
def update_entry_route(eid: int):
    """编辑词条；内置词条编辑后 ``source_type`` 保持不变以便追溯。"""
    body = _json_body()
    item = entries.update_entry(eid, body)
    safe_log(logger, logging.INFO, "update_entry", entry_id=eid)
    return ok(item)


@bp.delete("/prompt-library/entries/<int:eid>")
def delete_entry_route(eid: int):
    """删除词条；内置词条表现为本地隐藏并写入忽略指纹。"""
    result = entries.delete_entry(eid)
    safe_log(logger, logging.INFO, "delete_entry", entry_id=eid, deleted=result["deleted"])
    return ok(result)


@bp.post("/prompt-library/entries/<int:eid>/use")
def use_entry_route(eid: int):
    """记录一次成功回填，返回按模型档案拼接后的插入文本。"""
    body = _json_body()
    profile = str(body.get("profile") or "generic").strip()
    existing = body.get("existing")
    result = entries.record_use(eid, profile, existing if isinstance(existing, str) else "")
    safe_log(logger, logging.INFO, "record_use", entry_id=eid, profile=profile, duplicate=result["duplicate"])
    return ok(result)


@bp.put("/prompt-library/entries/<int:eid>/favorite")
def favorite_entry_route(eid: int):
    """收藏或取消收藏；body 未给 ``favorite`` 时切换到相反状态。"""
    body = _json_body()
    current = entries.get_entry(eid)
    value = _to_bool(body.get("favorite"), not current["is_favorite"])
    item = entries.favorite(eid, value)
    safe_log(logger, logging.INFO, "favorite_entry", entry_id=eid, is_favorite=value)
    return ok(item)


# ---------------------------------------------------------------- 候选审核


@bp.get("/prompt-library/candidates")
def list_candidates_route():
    """分页查询候选区。"""
    status = (request.args.get("status") or "pending").strip()
    media_type = (request.args.get("media_type") or "").strip()
    page = (request.args.get("page") or "").strip()
    page_size = (request.args.get("page_size") or "").strip()
    result = learning.list_candidates(
        status=status,
        media_type=media_type or None,
        page=page or 1,
        page_size=page_size or entries.DEFAULT_PAGE_SIZE,
    )
    safe_log(logger, logging.INFO, "list_candidates", status=status, total=result["total"])
    return ok(result)


@bp.post("/prompt-library/candidates/<int:cid>/approve")
def approve_candidate_route(cid: int):
    """批准候选并转为正式词条；body 可带 ``dimension`` / ``subcategory`` / ``title``。"""
    body = _json_body()
    result = learning.approve_candidate(cid, body)
    safe_log(logger, logging.INFO, "approve_candidate", candidate_id=cid, merged=result["merged"])
    return ok(result)


@bp.post("/prompt-library/candidates/<int:cid>/reject")
def reject_candidate_route(cid: int):
    """拒绝候选：标记 rejected 并写入忽略指纹，后续不再推荐。"""
    result = learning.reject_candidate(cid)
    safe_log(logger, logging.INFO, "reject_candidate", candidate_id=cid)
    return ok(result)


# ---------------------------------------------------------------- 错误处理


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    return from_exception(exc)


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    """兜底：只记录异常类型，不记录请求正文与堆栈。"""
    try:
        current_app.logger.exception("unhandled error in promptlib blueprint")
    except Exception:
        pass
    safe_log(logger, logging.ERROR, "route_error", error_code=type(exc).__name__)
    return from_exception(exc)


__all__ = ["bp"]
