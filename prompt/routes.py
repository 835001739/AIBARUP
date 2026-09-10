"""M6 提示词工作台的 HTTP 接口层（Blueprint ``prompt``，前缀 ``/api``）。

约束（HARNESS §3/§5、PRD M6.6）：
- 统一 envelope：``core.responses.ok`` / ``fail``；
- 业务异常抛 ``AIBARError``，由 Blueprint 级 errorhandler 统一转换；
- 所有入参先校验（枚举、长度、分页上限）再进入业务层；
- 日志不记录提示词正文，只记录 profile/provider/intensity/duration_ms/status 与长度。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, request

from core.db import dumps, execute, json_field, now, query_all, query_one
from core import errors
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from core.responses import from_exception, ok

from . import engine, library, providers

logger = get_logger("aibar.prompt.routes")

bp = Blueprint("prompt", __name__, url_prefix="/api")

MAX_Q_LEN = 60
MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20
MAX_TEXT_LEN = engine.MAX_PROMPT_LEN


# ---------------------------------------------------------------- 入参处理


def _json_body() -> dict:
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    return body


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise AIBARError("invalid_input", f"参数 {name} 必须是整数")
    if value < low or value > high:
        raise AIBARError("invalid_input", f"参数 {name} 必须在 {low} 到 {high} 之间")
    return value


def _clean_text(value: Any, field_name: str, required: bool = False, limit: int = MAX_TEXT_LEN) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        raise AIBARError("invalid_input", f"字段 {field_name} 必须是字符串")
    text = text.strip()
    if required and not text:
        raise AIBARError("invalid_input", f"字段 {field_name} 不能为空")
    if len(text) > limit:
        raise AIBARError("invalid_input", f"字段 {field_name} 超过 {limit} 字符上限")
    return text


def _clean_list(value: Any, field_name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AIBARError("invalid_input", f"字段 {field_name} 必须是数组")
    return value


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# ---------------------------------------------------------------- 知识库


@bp.get("/prompt-library")
def prompt_library_route():
    """查询模型档案、标准词条与场景模板。

    支持 ``profile``（标记 ``applicable``）、``dimension``（只返回该维度）、``q``（关键词）。
    """
    profile_key = (request.args.get("profile") or "").strip()
    dimension_key = (request.args.get("dimension") or "").strip()
    keyword = (request.args.get("q") or "").strip()[:MAX_Q_LEN]

    profile = profile_key or None
    dimension = dimension_key or None
    terms = library.query_terms(profile=profile, dimension=dimension, q=keyword)

    grouped: dict[str, list[dict]] = {}
    for item in terms:
        grouped.setdefault(item.dimension, []).append(item.to_dict(profile))

    dimensions = [
        {
            "key": meta["key"],
            "label": meta["label"],
            "terms": grouped.get(meta["key"], []),
        }
        for meta in library.dimensions()
        if grouped.get(meta["key"]) or (dimension == meta["key"])
    ]

    profiles = [
        {
            "key": item.get("key"),
            "label": item.get("label"),
            "description": item.get("description"),
            "supports_negative": bool(item.get("supports_negative")),
            "supports_weight": bool(item.get("supports_weight")),
        }
        for item in library.profiles()
    ]
    templates = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "profile": item.get("profile"),
            "positive": item.get("positive"),
            "negative": item.get("negative"),
            "dimensions": list(item.get("dimensions") or []),
        }
        for item in library.templates()
    ]
    return ok(
        {
            "profiles": profiles,
            "dimensions": dimensions,
            "templates": templates,
            "schema_version": library.schema_version(),
        }
    )


# ---------------------------------------------------------------- 扩写


@bp.post("/prompts/expand")
def prompts_expand_route():
    """执行扩写，返回结构化结果；外部 Provider 失败时自动回退规则引擎。"""
    body = _json_body()
    original_prompt = body.get("original_prompt")
    profile = body.get("profile") or engine.DEFAULT_PROFILE
    intensity = body.get("intensity") or engine.DEFAULT_INTENSITY
    template_id = body.get("template_id") or None
    options = body.get("options")
    provider = body.get("provider") or providers.RULES

    result = providers.expand(
        original_prompt=original_prompt,
        profile=profile,
        intensity=intensity,
        template_id=template_id,
        options=options,
        provider=provider,
    )
    safe_log(
        logger,
        logging.INFO,
        "route_expand",
        profile=result.get("profile", ""),
        provider=result.get("provider", ""),
        intensity=result.get("intensity", ""),
        duration_ms=result.get("duration_ms", 0),
        status="ok",
        out_len=len(result.get("expanded_positive", "")),
    )
    return ok(result)


# ---------------------------------------------------------------- 历史


def _row_to_item(row: Any) -> dict:
    return {
        "id": row["id"],
        "original_prompt": row["original_prompt"],
        "expanded_positive": row["expanded_positive"],
        "expanded_negative": row["expanded_negative"],
        "profile": row["profile"],
        "intensity": row["intensity"],
        "provider": row["provider"],
        "template_id": row["template_id"],
        "sections": json_field(row["sections"], []),
        "additions": json_field(row["additions"], []),
        "warnings": json_field(row["warnings"], []),
        "is_favorite": bool(row["is_favorite"]),
        "created_at": row["created_at"],
    }


def _load_history(pk: int) -> dict:
    row = query_one("SELECT * FROM prompt_expansions WHERE id = ?", (pk,))
    if row is None:
        raise errors.not_found("扩写历史不存在")
    return _row_to_item(row)


@bp.route("/prompts/history", methods=["GET", "POST"])
def prompts_history_route():
    """``GET`` 按创建时间倒序返回历史；``POST`` 保存一条扩写结果。"""
    if request.method == "GET":
        limit = _int_arg("limit", DEFAULT_HISTORY_LIMIT, 1, MAX_HISTORY_LIMIT)
        rows = query_all(
            "SELECT * FROM prompt_expansions ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return ok({"items": [_row_to_item(row) for row in rows], "total": len(rows)})

    body = _json_body()
    record = {
        "original_prompt": _clean_text(body.get("original_prompt"), "original_prompt", required=True),
        "expanded_positive": _clean_text(body.get("expanded_positive"), "expanded_positive"),
        "expanded_negative": _clean_text(body.get("expanded_negative"), "expanded_negative"),
        "profile": body.get("profile") or engine.DEFAULT_PROFILE,
        "intensity": body.get("intensity") or engine.DEFAULT_INTENSITY,
        "provider": body.get("provider") or providers.RULES,
        "template_id": body.get("template_id") or None,
        "sections": _clean_list(body.get("sections"), "sections"),
        "additions": _clean_list(body.get("additions"), "additions"),
        "warnings": _clean_list(body.get("warnings"), "warnings"),
        "is_favorite": 1 if _to_bool(body.get("is_favorite"), False) else 0,
    }
    if record["profile"] not in library.profile_keys():
        raise AIBARError("invalid_input", f"未知的模型档案：{record['profile']}")
    if record["intensity"] not in engine.INTENSITIES:
        raise AIBARError("invalid_input", f"未知的扩写强度：{record['intensity']}")

    cursor = execute(
        """
        INSERT INTO prompt_expansions (
            original_prompt, expanded_positive, expanded_negative, profile, intensity,
            provider, template_id, sections, additions, warnings, is_favorite, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["original_prompt"],
            record["expanded_positive"],
            record["expanded_negative"],
            record["profile"],
            record["intensity"],
            record["provider"],
            record["template_id"],
            dumps(record["sections"]),
            dumps(record["additions"]),
            dumps(record["warnings"]),
            record["is_favorite"],
            now(),
        ),
    )
    return ok(_load_history(int(cursor.lastrowid)), 201)


@bp.delete("/prompts/history/<int:pk>")
def delete_history_route(pk: int):
    """删除一条扩写历史。"""
    _load_history(pk)
    execute("DELETE FROM prompt_expansions WHERE id = ?", (pk,))
    return ok({"id": pk, "deleted": True})


@bp.put("/prompts/history/<int:pk>/favorite")
def favorite_history_route(pk: int):
    """设置或取消收藏；请求体 ``favorite`` 缺省时切换到相反状态。"""
    item = _load_history(pk)
    body = _json_body()
    favorite = _to_bool(body.get("favorite"), not item["is_favorite"])
    execute(
        "UPDATE prompt_expansions SET is_favorite = ? WHERE id = ?",
        (1 if favorite else 0, pk),
    )
    return ok({"id": pk, "is_favorite": favorite})


# ---------------------------------------------------------------- 错误处理


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    return from_exception(exc)


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    """兜底：记录错误类型（不记录请求正文与堆栈），对外统一 internal_error。"""
    try:
        current_app.logger.exception("unhandled error in prompt blueprint")
    except Exception:
        pass
    safe_log(logger, logging.ERROR, "route_error", error_code=type(exc).__name__)
    return from_exception(exc)


__all__ = ["bp"]
