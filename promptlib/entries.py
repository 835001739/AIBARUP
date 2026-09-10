"""M7 · 多模态精品提示词库核心服务：检索、分面统计、增删改、收藏、回填与内置种子导入。

职责边界（HARNESS §2 / §4，PRD M7.2 / M7.4 / M7.6）：
- 只依赖 ``config`` 与 ``core``；自动学习模块（``learning.py``）单向复用本文件的写入与查重能力；
- 所有 SQL 走 ``core.db``，JSON 字段用 ``dumps``/``json_field`` 读写，时间统一用 ``now()``；
- 分面统计只返回当前筛选条件下仍有数据的选项，不产生空层级；
- 内置种子按 ``content_fingerprint`` 幂等合并，绝不覆盖用户编辑与收藏。

说明：内置种子的 ``quality_score`` 是人工策展评分；自动学习词条使用 ``core.textutil.quality_score`` 的算法分。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from config import Config
from core.db import (
    dumps,
    execute,
    get_conn,
    json_field,
    now,
    query_all,
    query_one,
    query_scalar,
)
from core import errors
from core.errors import AIBARError
from core import textutil

# ---------------------------------------------------------------- 常量

SEED_FILENAME = "prompt_seed.json"

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100
MAX_PAGE = 10000
MAX_Q_LEN = 60
MAX_TITLE_LEN = 60
MAX_TEXT_LEN = 200
MAX_DESC_LEN = 300
MAX_NEGATIVE_LEN = 300
MAX_EXISTING_LEN = 4000
MAX_TAG_LEN = 24
MAX_TAGS = 20
MIN_TEXT_LEN = 4
RECENT_WINDOW_DAYS = 30

SORTS = ("recommended", "recent", "recent_used", "most_used")
SOURCE_TYPES = ("builtin", "manual", "auto", "reverse_metadata", "reverse_vision")
LANGUAGES = ("zh", "en")

# 排序必须确定：每个排序规则都以 id 兜底，避免同分项分页抖动
_ORDER_BY: dict[str, str] = {
    "recommended": "quality_score DESC, use_count DESC, id DESC",
    "recent": "created_at DESC, id DESC",
    "recent_used": "last_used_at DESC, id DESC",
    "most_used": "use_count DESC, last_used_at DESC, id DESC",
}

_FALLBACK_SOURCE_LABELS = {
    "builtin": "内置",
    "manual": "手动",
    "auto": "自动学习",
    "reverse_metadata": "元数据恢复",
    "reverse_vision": "视觉反推",
}

_LIKE_ESCAPE_RE = re.compile(r"[\\%_]")
_TITLE_SPLIT_RE = re.compile(r"[，,。；;！!？?]")

_UPDATABLE_FIELDS = (
    "media_type",
    "dimension",
    "subcategory",
    "title",
    "prompt_text",
    "negative_text",
    "language",
    "model_profiles",
    "tags",
    "description",
    "quality_score",
)


# ---------------------------------------------------------------- 分类体系


def media_type_keys() -> list[str]:
    return [item["key"] for item in textutil.media_types()]


def media_type_label(media_type: str) -> str:
    for item in textutil.media_types():
        if item["key"] == media_type:
            return item.get("label") or media_type
    return media_type


def subcategory_keys(media_type: str, dimension: str) -> list[str]:
    for item in textutil.dimensions_of(media_type):
        if item["key"] == dimension:
            return [sub["key"] for sub in item.get("subcategories", [])]
    return []


def source_labels() -> dict[str, str]:
    labels = dict(_FALLBACK_SOURCE_LABELS)
    labels.update(textutil.load_taxonomy().get("source_labels") or {})
    return labels


def source_label(source_type: str) -> str:
    return source_labels().get(source_type, source_type)


# ---------------------------------------------------------------- 入参清洗


def _escape_like(value: str) -> str:
    return _LIKE_ESCAPE_RE.sub(lambda match: "\\" + match.group(0), value)


def _clean_text(value: Any, field_name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AIBARError("invalid_input", f"字段 {field_name} 必须是字符串")
    text = value.strip()
    if len(text) > limit:
        raise AIBARError("invalid_input", f"字段 {field_name} 超过 {limit} 字符上限")
    return text


def _clean_media_type(value: Any) -> str:
    media_type = _clean_text(value, "media_type", 32)
    if media_type not in media_type_keys():
        raise AIBARError("invalid_input", f"未知的媒体类型：{media_type or '(空)'}")
    return media_type


def _clean_dimension(media_type: str, value: Any) -> str:
    dimension = _clean_text(value, "dimension", 64)
    if dimension not in textutil.dimension_keys(media_type):
        raise AIBARError("invalid_input", f"媒体 {media_type} 没有维度：{dimension or '(空)'}")
    return dimension


def _clean_subcategory(media_type: str, dimension: str, value: Any) -> str:
    subcategory = _clean_text(value, "subcategory", 64)
    if not subcategory:
        return ""
    allowed = subcategory_keys(media_type, dimension)
    if allowed and subcategory not in allowed:
        raise AIBARError("invalid_input", f"维度 {dimension} 没有子类：{subcategory}")
    return subcategory


def _clean_profiles(value: Any) -> list[str]:
    if value is None:
        return ["generic"]
    if not isinstance(value, list):
        raise AIBARError("invalid_input", "字段 model_profiles 必须是数组")
    allowed = textutil.profile_keys()
    profiles: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AIBARError("invalid_input", "字段 model_profiles 只能包含字符串")
        key = item.strip()
        if key not in allowed:
            raise AIBARError("invalid_input", f"未知的模型档案：{key}")
        if key not in profiles:
            profiles.append(key)
    return profiles or ["generic"]


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AIBARError("invalid_input", "字段 tags 必须是数组")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AIBARError("invalid_input", "字段 tags 只能包含字符串")
        tag = item.strip()[:MAX_TAG_LEN]
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) > MAX_TAGS:
            raise AIBARError("invalid_input", f"标签数量不能超过 {MAX_TAGS} 个")
    return tags


def _clean_language(value: Any) -> str:
    language = _clean_text(value, "language", 8) or "zh"
    if language not in LANGUAGES:
        raise AIBARError("invalid_input", f"未知的语言：{language}")
    return language


def _clean_score(value: Any, default: int = 80) -> int:
    if value is None or value == "":
        return default
    try:
        score = int(value)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", "字段 quality_score 必须是整数")
    if not 0 <= score <= 100:
        raise AIBARError("invalid_input", "字段 quality_score 必须在 0 到 100 之间")
    return score


def _clean_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _derive_title(text: str) -> str:
    head = _TITLE_SPLIT_RE.split(text.strip())[0].strip()
    return (head or text.strip())[:MAX_TITLE_LEN]


def _detect_language(text: str) -> str:
    for char in text:
        if "一" <= char <= "鿿":
            return "zh"
    return "en"


# ---------------------------------------------------------------- 筛选条件


def _normalize_filters(filters: dict | None) -> dict:
    """把外部传入的筛选条件规范化为内部字典，非法枚举直接抛 ``invalid_input``。"""
    raw = filters or {}
    if not isinstance(raw, dict):
        raise AIBARError("invalid_input", "筛选参数必须是对象")

    normalized: dict[str, Any] = {
        "media_type": "",
        "dimension": "",
        "subcategory": "",
        "profile": "",
        "q": "",
        "favorite": None,
        "source": [],
        "tag": "",
        "sort": "recommended",
        "page": 1,
        "page_size": DEFAULT_PAGE_SIZE,
    }

    media_type = _clean_text(raw.get("media_type"), "media_type", 32)
    if media_type:
        if media_type not in media_type_keys():
            raise AIBARError("invalid_input", f"未知的媒体类型：{media_type}")
        normalized["media_type"] = media_type

    dimension = _clean_text(raw.get("dimension"), "dimension", 64)
    if dimension:
        # 未指定媒体类型时只要求维度在任一分类体系中存在
        known = any(dimension in textutil.dimension_keys(key) for key in media_type_keys())
        if not known:
            raise AIBARError("invalid_input", f"未知的维度：{dimension}")
        if media_type and dimension not in textutil.dimension_keys(media_type):
            raise AIBARError("invalid_input", f"媒体 {media_type} 没有维度：{dimension}")
        normalized["dimension"] = dimension

    subcategory = _clean_text(raw.get("subcategory"), "subcategory", 64)
    if subcategory:
        if media_type and dimension:
            allowed = subcategory_keys(media_type, dimension)
            if allowed and subcategory not in allowed:
                raise AIBARError("invalid_input", f"维度 {dimension} 没有子类：{subcategory}")
        normalized["subcategory"] = subcategory

    profile = _clean_text(raw.get("profile"), "profile", 64)
    if profile:
        if profile not in textutil.profile_keys():
            raise AIBARError("invalid_input", f"未知的模型档案：{profile}")
        normalized["profile"] = profile

    normalized["q"] = _clean_text(raw.get("q"), "q", MAX_Q_LEN)
    normalized["tag"] = _clean_text(raw.get("tag"), "tag", MAX_TAG_LEN)

    if raw.get("favorite") is not None and raw.get("favorite") != "":
        normalized["favorite"] = _clean_bool(raw.get("favorite"), True)

    source = raw.get("source")
    if source:
        values = source if isinstance(source, (list, tuple)) else str(source).split(",")
        for item in values:
            key = str(item).strip()
            if key not in SOURCE_TYPES:
                raise AIBARError("invalid_input", f"未知的来源类型：{key}")
            if key not in normalized["source"]:
                normalized["source"].append(key)

    sort = _clean_text(raw.get("sort"), "sort", 32) or "recommended"
    if sort not in SORTS:
        raise AIBARError("invalid_input", f"未知的排序方式：{sort}")
    normalized["sort"] = sort

    normalized["page"] = _clean_page(raw.get("page"), 1, MAX_PAGE)
    normalized["page_size"] = _clean_page(raw.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    return normalized


def _clean_page(value: Any, default: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", "分页参数必须是整数")
    if page < 1 or page > high:
        raise AIBARError("invalid_input", f"分页参数必须在 1 到 {high} 之间")
    return page


def _build_where(filters: dict, skip: Iterable[str] = ()) -> tuple[str, list[Any]]:
    """构造 WHERE 子句。

    ``skip`` 用于分面统计：统计某一层级自身的候选选项时，需要排除该层级对应的筛选条件，
    这样用户在某一级上做的选择不会把同层级的其他选项挤掉，同时保证返回的选项都有数据。
    """
    skip = set(skip)
    clauses = ["is_hidden = 0"]
    params: list[Any] = []

    if filters["media_type"] and "media_type" not in skip:
        clauses.append("media_type = ?")
        params.append(filters["media_type"])
    if filters["dimension"] and "dimension" not in skip:
        clauses.append("dimension = ?")
        params.append(filters["dimension"])
    if filters["subcategory"] and "subcategory" not in skip:
        clauses.append("subcategory = ?")
        params.append(filters["subcategory"])
    if filters["profile"] and "profile" not in skip:
        # model_profiles 是 JSON 数组文本，按带引号的键名精确匹配，保证确定性
        clauses.append("instr(model_profiles, ?) > 0")
        params.append(f'"{filters["profile"]}"')
    if filters["tag"] and "tag" not in skip:
        clauses.append("instr(tags, ?) > 0")
        params.append(f'"{filters["tag"]}"')
    if filters["source"] and "source" not in skip:
        placeholders = ", ".join("?" for _ in filters["source"])
        clauses.append(f"source_type IN ({placeholders})")
        params.extend(filters["source"])
    if filters["favorite"] is not None and "favorite" not in skip:
        clauses.append("is_favorite = ?")
        params.append(1 if filters["favorite"] else 0)
    if filters["q"] and "q" not in skip:
        pattern = f"%{_escape_like(filters['q'])}%"
        clauses.append(
            "(title LIKE ? ESCAPE '\\' OR prompt_text LIKE ? ESCAPE '\\' "
            "OR description LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])

    return " AND ".join(clauses), params


# ---------------------------------------------------------------- 读取


def _row_to_item(row: sqlite3.Row) -> dict:
    media_type = row["media_type"]
    dimension = row["dimension"]
    subcategory = row["subcategory"] or ""
    return {
        "id": row["id"],
        "media_type": media_type,
        "media_type_label": media_type_label(media_type),
        "dimension": dimension,
        "dimension_label": textutil.dimension_label(media_type, dimension),
        "subcategory": subcategory,
        "subcategory_label": textutil.subcategory_label(media_type, dimension, subcategory),
        "title": row["title"],
        "prompt_text": row["prompt_text"],
        "negative_text": row["negative_text"] or "",
        "language": row["language"],
        "model_profiles": json_field(row["model_profiles"], []) or [],
        "tags": json_field(row["tags"], []) or [],
        "description": row["description"] or "",
        "source_type": row["source_type"],
        "source_label": source_label(row["source_type"]),
        "quality_score": row["quality_score"],
        "is_favorite": bool(row["is_favorite"]),
        "use_count": row["use_count"],
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_entry(entry_id: int) -> dict:
    """读取单个词条（隐藏词条视为不存在）。"""
    return _row_to_item(_load_entry(entry_id))


def _load_entry(entry_id: int, include_hidden: bool = False) -> sqlite3.Row:
    sql = "SELECT * FROM prompt_entries WHERE id = ?"
    if not include_hidden:
        sql += " AND is_hidden = 0"
    row = query_one(sql, (entry_id,))
    if row is None:
        raise errors.not_found("词条不存在")
    return row


def list_entries(filters: dict | None = None) -> dict:
    """分页查询正式词库。

    Args:
        filters: ``media_type/dimension/subcategory/profile/q/favorite/source/tag/sort/page/page_size``。

    Returns:
        ``{"items": [...], "total", "page", "page_size", "has_more"}``。
    """
    normalized = _normalize_filters(filters)
    where, params = _build_where(normalized)
    total = int(query_scalar(f"SELECT COUNT(*) FROM prompt_entries WHERE {where}", params, 0) or 0)
    offset = (normalized["page"] - 1) * normalized["page_size"]
    rows = query_all(
        f"SELECT * FROM prompt_entries WHERE {where} ORDER BY {_ORDER_BY[normalized['sort']]} LIMIT ? OFFSET ?",
        params + [normalized["page_size"], offset],
    )
    items = [_row_to_item(row) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": normalized["page"],
        "page_size": normalized["page_size"],
        "has_more": offset + len(items) < total,
    }


# ---------------------------------------------------------------- 分面统计


def _recent_cutoff() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - RECENT_WINDOW_DAYS * 86400))


def get_facets(filters: dict | None = None) -> dict:
    """返回当前筛选条件下仍有数据的层级选项与数量（渐进筛选，无空层级）。"""
    normalized = _normalize_filters(filters)
    return {
        "media_types": _facet_media_types(normalized),
        "dimensions": _facet_dimensions(normalized),
        "subcategories": _facet_subcategories(normalized),
        "tags": _facet_tags(normalized),
        "profiles": _facet_profiles(normalized),
        "sources": _facet_sources(normalized),
    }


def _facet_media_types(filters: dict) -> list[dict]:
    cutoff = _recent_cutoff()
    where, params = _build_where(filters, skip={"media_type"})
    rows = query_all(
        f"""
        SELECT media_type,
               COUNT(*) AS total_count,
               COALESCE(SUM(is_favorite), 0) AS favorite_count,
               COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_count
        FROM prompt_entries
        WHERE {where}
        GROUP BY media_type
        """,
        [cutoff] + list(params),
    )
    counts = {row["media_type"]: row for row in rows}
    # 媒体类型是第一级入口，即使当前无数据也要展示（count=0）
    items: list[dict] = []
    for meta in textutil.media_types():
        row = counts.get(meta["key"])
        items.append(
            {
                "key": meta["key"],
                "label": meta.get("label") or meta["key"],
                "count": int(row["total_count"]) if row is not None else 0,
                "favorite_count": int(row["favorite_count"]) if row is not None else 0,
                "recent_count": int(row["recent_count"]) if row is not None else 0,
            }
        )
    return items


def _dimension_meta(filters: dict) -> dict[str, str]:
    """当前媒体类型（或全部媒体类型）下 dimension key -> label 的映射，按分类顺序。"""
    meta: dict[str, str] = {}
    for media in textutil.media_types():
        if filters["media_type"] and media["key"] != filters["media_type"]:
            continue
        for dimension in media.get("dimensions", []):
            meta.setdefault(dimension["key"], dimension.get("label") or dimension["key"])
    return meta


def _facet_dimensions(filters: dict) -> list[dict]:
    where, params = _build_where(filters, skip={"dimension", "subcategory"})
    rows = query_all(
        f"SELECT dimension, COUNT(*) AS total_count FROM prompt_entries WHERE {where} GROUP BY dimension",
        params,
    )
    counts = {row["dimension"]: int(row["total_count"]) for row in rows}
    return [
        {"key": key, "label": label, "count": counts[key]}
        for key, label in _dimension_meta(filters).items()
        if counts.get(key, 0) > 0
    ]


def _facet_subcategories(filters: dict) -> list[dict]:
    where, params = _build_where(filters, skip={"subcategory"})
    rows = query_all(
        f"""
        SELECT dimension, subcategory, COUNT(*) AS total_count
        FROM prompt_entries
        WHERE {where}
        GROUP BY dimension, subcategory
        """,
        params,
    )
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for row in rows:
        key = row["subcategory"] or ""
        if not key:
            continue
        counts[key] = counts.get(key, 0) + int(row["total_count"])
        labels.setdefault(
            key,
            textutil.subcategory_label(filters["media_type"] or _media_of(row["dimension"]), row["dimension"], key),
        )

    if filters["media_type"] and filters["dimension"]:
        ordered = [
            (key, textutil.subcategory_label(filters["media_type"], filters["dimension"], key))
            for key in subcategory_keys(filters["media_type"], filters["dimension"])
        ]
        return [
            {"key": key, "label": label, "count": counts[key]} for key, label in ordered if counts.get(key, 0) > 0
        ]
    return [
        {"key": key, "label": labels.get(key, key), "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _media_of(dimension: str) -> str:
    for media in textutil.media_types():
        if dimension in [item["key"] for item in media.get("dimensions", [])]:
            return media["key"]
    return "image"


def _count_json_field(filters: dict, column: str, skip_key: str) -> dict[str, int]:
    """统计 JSON 数组字段的取值分布。

    ``column`` 是真实列名（``tags`` / ``model_profiles``），``skip_key`` 是 ``_build_where``
    使用的筛选键（``tag`` / ``profile``），两者不同名，因此必须分开传入。
    """
    where, params = _build_where(filters, skip={skip_key})
    rows = query_all(f"SELECT {column} AS payload FROM prompt_entries WHERE {where}", params)
    counts: dict[str, int] = {}
    for row in rows:
        values = json_field(row["payload"], [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            key = value.strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _facet_tags(filters: dict) -> list[dict]:
    counts = _count_json_field(filters, "tags", "tag")
    return [
        {"key": key, "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _facet_profiles(filters: dict) -> list[dict]:
    counts = _count_json_field(filters, "model_profiles", "profile")
    ordered = [key for key in textutil.profile_keys() if counts.get(key, 0) > 0]
    ordered += sorted(key for key in counts if key not in ordered)
    return [
        {
            "key": key,
            "label": textutil.get_profile(key).get("label") or key,
            "count": counts[key],
        }
        for key in ordered
        if counts.get(key, 0) > 0
    ]


def _facet_sources(filters: dict) -> list[dict]:
    where, params = _build_where(filters, skip={"source"})
    rows = query_all(
        f"SELECT source_type, COUNT(*) AS total_count FROM prompt_entries WHERE {where} GROUP BY source_type",
        params,
    )
    counts = {row["source_type"]: int(row["total_count"]) for row in rows}
    ordered = [key for key in SOURCE_TYPES if counts.get(key, 0) > 0]
    ordered += sorted(key for key in counts if key not in ordered)
    return [
        {"key": key, "label": source_label(key), "count": counts[key]}
        for key in ordered
        if counts.get(key, 0) > 0
    ]


# ---------------------------------------------------------------- 写入


def insert_entry(record: dict) -> dict:
    """写入一条正式词条，是手动新增与自动学习共用的唯一入口。

    Args:
        record: 至少包含 ``media_type`` / ``dimension`` / ``prompt_text``；
                缺省字段按词库默认值补齐（来源 ``manual``、质量分 80、未收藏未隐藏）。

    Returns:
        新建词条条目（含 ``id`` 与展示字段）。
    """
    if not isinstance(record, dict):
        raise AIBARError("invalid_input", "词条数据必须是对象")

    media_type = _clean_media_type(record.get("media_type"))
    dimension = _clean_dimension(media_type, record.get("dimension"))
    subcategory = _clean_subcategory(media_type, dimension, record.get("subcategory"))
    prompt_text = _clean_text(record.get("prompt_text"), "prompt_text", MAX_TEXT_LEN).strip()
    if len(prompt_text) < MIN_TEXT_LEN:
        raise AIBARError("invalid_input", f"字段 prompt_text 至少需要 {MIN_TEXT_LEN} 个字符")

    title = _clean_text(record.get("title"), "title", MAX_TITLE_LEN).strip() or _derive_title(prompt_text)
    language = _clean_language(record.get("language")) if record.get("language") else _detect_language(prompt_text)
    source_type = _clean_text(record.get("source_type"), "source_type", 32) or "manual"
    if source_type not in SOURCE_TYPES:
        raise AIBARError("invalid_input", f"未知的来源类型：{source_type}")

    source_ref = record.get("source_ref")
    fingerprint = textutil.content_fingerprint(media_type, dimension, prompt_text)
    timestamp = now()
    values = (
        media_type,
        dimension,
        subcategory,
        title,
        prompt_text,
        _clean_text(record.get("negative_text"), "negative_text", MAX_NEGATIVE_LEN) or None,
        language,
        dumps(_clean_profiles(record.get("model_profiles"))),
        dumps(_clean_tags(record.get("tags"))),
        _clean_text(record.get("description"), "description", MAX_DESC_LEN),
        source_type,
        dumps(source_ref) if source_ref else None,
        _clean_score(record.get("quality_score"), 80),
        fingerprint,
        1 if _clean_bool(record.get("is_favorite"), False) else 0,
        0,
        int(record.get("use_count") or 0),
        record.get("last_used_at"),
        timestamp,
        timestamp,
    )
    try:
        cursor = execute(
            """
            INSERT INTO prompt_entries (
                media_type, dimension, subcategory, title, prompt_text, negative_text,
                language, model_profiles, tags, description, source_type, source_ref,
                quality_score, content_fingerprint, is_favorite, is_hidden,
                use_count, last_used_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    except sqlite3.IntegrityError:
        # 并发或调用方未预检时兜底：回滚未提交事务并给出明确错误码
        try:
            get_conn().rollback()
        except sqlite3.Error:
            pass
        existing = find_entry_by_fingerprint(fingerprint)
        if existing is not None:
            raise AIBARError("already_exists", "相同内容的词条已存在", 409)
        raise
    return _row_to_item(_load_entry(int(cursor.lastrowid), include_hidden=True))


def create_entry(payload: dict) -> dict:
    """手动新增个人词条。"""
    if not isinstance(payload, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    record = dict(payload)
    record.setdefault("source_type", "manual")
    return insert_entry(record)


def update_entry(entry_id: int, payload: dict) -> dict:
    """编辑词条。``source_type`` 保持不变以便追溯内置来源。"""
    if not isinstance(payload, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    row = _load_entry(entry_id)
    current = dict(row)

    media_type = _clean_media_type(payload["media_type"]) if payload.get("media_type") else current["media_type"]
    dimension = _clean_dimension(media_type, payload["dimension"]) if payload.get("dimension") else current["dimension"]
    if dimension not in textutil.dimension_keys(media_type):
        raise AIBARError("invalid_input", f"媒体 {media_type} 没有维度：{dimension}")
    if payload.get("subcategory") is not None:
        subcategory = _clean_subcategory(media_type, dimension, payload["subcategory"])
    else:
        subcategory = current["subcategory"] or ""

    if payload.get("prompt_text") is not None:
        prompt_text = _clean_text(payload["prompt_text"], "prompt_text", MAX_TEXT_LEN).strip()
        if len(prompt_text) < MIN_TEXT_LEN:
            raise AIBARError("invalid_input", f"字段 prompt_text 至少需要 {MIN_TEXT_LEN} 个字符")
    else:
        prompt_text = current["prompt_text"]

    title = _clean_text(payload.get("title"), "title", MAX_TITLE_LEN).strip() or current["title"]
    if payload.get("negative_text") is not None:
        negative_text = _clean_text(payload["negative_text"], "negative_text", MAX_NEGATIVE_LEN)
    else:
        negative_text = current["negative_text"] or ""
    if payload.get("description") is not None:
        description = _clean_text(payload["description"], "description", MAX_DESC_LEN)
    else:
        description = current["description"] or ""
    if payload.get("model_profiles") is not None:
        profiles = _clean_profiles(payload["model_profiles"])
    else:
        profiles = list(json_field(current["model_profiles"], []) or [])
    if payload.get("tags") is not None:
        tags = _clean_tags(payload["tags"])
    else:
        tags = list(json_field(current["tags"], []) or [])
    language = _clean_language(payload["language"]) if payload.get("language") else current["language"]
    if payload.get("quality_score") is not None:
        score = _clean_score(payload["quality_score"], int(current["quality_score"]))
    else:
        score = int(current["quality_score"])

    fingerprint = textutil.content_fingerprint(media_type, dimension, prompt_text)
    other = query_one(
        "SELECT id FROM prompt_entries WHERE content_fingerprint = ? AND id != ?",
        (fingerprint, entry_id),
    )
    if other is not None:
        raise AIBARError("already_exists", "相同内容的词条已存在", 409)

    execute(
        """
        UPDATE prompt_entries SET
            media_type = ?, dimension = ?, subcategory = ?, title = ?, prompt_text = ?,
            negative_text = ?, language = ?, model_profiles = ?, tags = ?, description = ?,
            quality_score = ?, content_fingerprint = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            media_type,
            dimension,
            subcategory,
            title,
            prompt_text,
            negative_text or None,
            language,
            dumps(profiles),
            dumps(tags),
            description,
            score,
            fingerprint,
            now(),
            entry_id,
        ),
    )
    return _row_to_item(_load_entry(entry_id))


def delete_entry(entry_id: int) -> dict:
    """删除词条。

    内置词条只做本地隐藏（不改版本化资源），其他来源物理删除；两种情况都写入忽略指纹，
    避免后续同步或自动学习把它再次推荐出来（PRD M7.5）。
    """
    row = _load_entry(entry_id)
    fingerprint = row["content_fingerprint"]
    if row["source_type"] == "builtin":
        execute("UPDATE prompt_entries SET is_hidden = 1, updated_at = ? WHERE id = ?", (now(), entry_id))
        deleted = False
    else:
        execute("DELETE FROM prompt_entries WHERE id = ?", (entry_id,))
        deleted = True
    add_ignored_fingerprint(fingerprint, "deleted")
    return {"id": entry_id, "deleted": deleted, "hidden": not deleted}


def favorite(entry_id: int, value: bool) -> dict:
    """设置或取消收藏，返回更新后的词条。"""
    _load_entry(entry_id)
    execute(
        "UPDATE prompt_entries SET is_favorite = ?, updated_at = ? WHERE id = ?",
        (1 if value else 0, now(), entry_id),
    )
    return _row_to_item(_load_entry(entry_id))


def record_use(entry_id: int, profile: str = "generic", existing: str = "") -> dict:
    """记录一次成功回填，并返回按模型档案拼接后的插入文本。

    Args:
        entry_id: 词条 ID。
        profile: 目标模型档案，决定分隔符。
        existing: 前端当前提示词框内容，用于重复检测。

    Returns:
        ``{"insert_text", "separator", "duplicate", "segment", "id"}``；
        ``duplicate`` 为真表示已有相同片段，前端不应重复插入。
    """
    row = _load_entry(entry_id)
    profile_key = _clean_text(profile, "profile", 64) or "generic"
    if profile_key not in textutil.profile_keys():
        raise AIBARError("invalid_input", f"未知的模型档案：{profile_key}")
    existing_text = _clean_text(existing, "existing", MAX_EXISTING_LEN)
    merged, duplicate = textutil.join_text(existing_text, row["prompt_text"], profile_key)
    if not duplicate:
        execute(
            "UPDATE prompt_entries SET use_count = use_count + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
            (now(), now(), entry_id),
        )
    return {
        "id": entry_id,
        "insert_text": merged,
        "separator": textutil.get_profile(profile_key).get("separator") or "，",
        "duplicate": bool(duplicate),
        "segment": row["prompt_text"],
    }


# ---------------------------------------------------------------- 去重与忽略


def find_entry_by_fingerprint(fingerprint: str) -> sqlite3.Row | None:
    """按内容指纹查找已有词条（包含隐藏词条，避免违反唯一约束）。"""
    return query_one("SELECT * FROM prompt_entries WHERE content_fingerprint = ?", (fingerprint,))


def find_alias_entry(normalized_text: str) -> sqlite3.Row | None:
    return query_one(
        "SELECT * FROM prompt_entry_aliases WHERE normalized_alias = ? ORDER BY id LIMIT 1",
        (normalized_text,),
    )


def add_alias(entry_id: int, alias_text: str, source_type: str = "", source_ref: dict | None = None) -> bool:
    """登记一条别名（近似重复），已存在时静默跳过。返回是否新增。"""
    normalized = textutil.normalize_text(alias_text)
    if not normalized:
        return False
    cursor = execute(
        """
        INSERT OR IGNORE INTO prompt_entry_aliases (
            prompt_entry_id, alias_text, normalized_alias, source_type, source_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entry_id, alias_text, normalized, source_type, dumps(source_ref) if source_ref else None, now()),
    )
    return cursor.rowcount > 0


def touch_entry(entry_id: int, source_ref: dict | None = None) -> None:
    """命中已有内容时累计使用记录并合并来源追溯，不新建词条。"""
    if source_ref:
        row = query_one("SELECT source_ref FROM prompt_entries WHERE id = ?", (entry_id,))
        merged = json_field(row["source_ref"], {}) if row is not None else {}
        if not isinstance(merged, dict):
            merged = {}
        merged.update(source_ref)
        execute(
            "UPDATE prompt_entries SET use_count = use_count + 1, last_used_at = ?, source_ref = ?, updated_at = ? WHERE id = ?",
            (now(), dumps(merged), now(), entry_id),
        )
    else:
        execute(
            "UPDATE prompt_entries SET use_count = use_count + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
            (now(), now(), entry_id),
        )


def is_ignored_fingerprint(fingerprint: str) -> bool:
    return (
        query_scalar(
            "SELECT 1 FROM prompt_ignored_fingerprints WHERE content_fingerprint = ?",
            (fingerprint,),
            0,
        )
        == 1
    )


def add_ignored_fingerprint(fingerprint: str, reason: str = "") -> None:
    if not fingerprint:
        return
    execute(
        "INSERT OR IGNORE INTO prompt_ignored_fingerprints (content_fingerprint, reason, created_at) VALUES (?, ?, ?)",
        (fingerprint, reason[:200], now()),
    )


def remove_ignored_fingerprint(fingerprint: str) -> None:
    execute("DELETE FROM prompt_ignored_fingerprints WHERE content_fingerprint = ?", (fingerprint,))


# ---------------------------------------------------------------- 内置种子


def seed_from_resources(path: str | Path | None = None) -> dict:
    """导入版本化内置种子，按内容指纹幂等合并。

    已存在的词条、被用户删除/拒绝过的指纹都会被跳过，因此重复执行不会新增数据，
    也不会覆盖用户编辑的标题、标签与收藏状态。

    Returns:
        ``{"file", "total", "inserted", "skipped_existing", "skipped_ignored", "invalid"}``。
    """
    seed_path = Path(path) if path else Path(Config.RESOURCES_DIR) / SEED_FILENAME
    try:
        with open(seed_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AIBARError("seed_unavailable", f"内置种子文件不可读：{seed_path.name}", 500) from exc
    except ValueError as exc:
        raise AIBARError("seed_invalid", f"内置种子文件不是合法 JSON：{seed_path.name}", 500) from exc

    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        raise AIBARError("seed_invalid", "内置种子文件缺少 entries 数组", 500)

    stats = {
        "file": seed_path.name,
        "total": len(raw_entries),
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_ignored": 0,
        "invalid": 0,
    }
    for item in raw_entries:
        record = _seed_to_record(item)
        if record is None:
            stats["invalid"] += 1
            continue
        fingerprint = textutil.content_fingerprint(
            record["media_type"], record["dimension"], record["prompt_text"]
        )
        if is_ignored_fingerprint(fingerprint):
            stats["skipped_ignored"] += 1
            continue
        if find_entry_by_fingerprint(fingerprint) is not None:
            stats["skipped_existing"] += 1
            continue
        insert_entry(record)
        stats["inserted"] += 1
    return stats


def _seed_to_record(item: Any) -> dict | None:
    """把一条种子数据转为写入记录；结构或枚举非法时返回 None（跳过而不中断整体导入）。"""
    if not isinstance(item, dict):
        return None
    try:
        score = _clean_score(item.get("quality_score"), 80)
    except AIBARError:
        # 单条种子数据异常不应阻断整体导入
        score = 80
    media_type = str(item.get("media_type") or "").strip()
    dimension = str(item.get("dimension") or "").strip()
    prompt_text = str(item.get("prompt_text") or "").strip()
    if media_type not in media_type_keys() or dimension not in textutil.dimension_keys(media_type):
        return None
    if len(prompt_text) < MIN_TEXT_LEN:
        return None
    subcategory = str(item.get("subcategory") or "").strip()
    allowed_subcategories = subcategory_keys(media_type, dimension)
    if allowed_subcategories and subcategory not in allowed_subcategories:
        subcategory = ""
    profiles = [key for key in (item.get("model_profiles") or []) if key in textutil.profile_keys()]
    return {
        "media_type": media_type,
        "dimension": dimension,
        "subcategory": subcategory,
        "title": str(item.get("title") or "").strip() or _derive_title(prompt_text),
        "prompt_text": prompt_text,
        "negative_text": str(item.get("negative_text") or "").strip(),
        "language": item.get("language") if item.get("language") in LANGUAGES else _detect_language(prompt_text),
        "model_profiles": profiles or ["generic"],
        "tags": [str(tag).strip()[:MAX_TAG_LEN] for tag in (item.get("tags") or []) if str(tag).strip()][:MAX_TAGS],
        "description": str(item.get("description") or "").strip()[:MAX_DESC_LEN],
        "source_type": "builtin",
        "source_ref": {"seed_id": str(item.get("id") or "").strip()} if item.get("id") else None,
        "quality_score": score,
    }


__all__ = [
    "SEED_FILENAME",
    "SORTS",
    "SOURCE_TYPES",
    "media_type_keys",
    "media_type_label",
    "source_label",
    "subcategory_keys",
    "list_entries",
    "get_facets",
    "get_entry",
    "create_entry",
    "insert_entry",
    "update_entry",
    "delete_entry",
    "favorite",
    "record_use",
    "find_entry_by_fingerprint",
    "find_alias_entry",
    "add_alias",
    "touch_entry",
    "is_ignored_fingerprint",
    "add_ignored_fingerprint",
    "remove_ignored_fingerprint",
    "seed_from_resources",
]
