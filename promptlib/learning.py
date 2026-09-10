"""M7 · 自动学习流水线：候选语义片段的分流、去重、评分与候选审核。

流水线固定顺序（PRD M7.5 / M9.6）：
``规范化 → 按媒体分类 → 按维度切分语义片段 → 移除停用句式/运行参数 → 精确去重 → 质量评分 → 入库或候选``

对外契约（M9 反推模块会直接调用，签名不得变动）：
- ``ingest_segments(segments, media_type, source_type, source_ref, thresholds=None)``
- ``learn_from_generation(usage_id)``
- ``record_generation(...)``
- ``list_candidates / approve_candidate / reject_candidate``

阈值一律来自本模块顶部的常量，运行参数、空泛质量口号、身份/品牌/艺术家猜测与整段未拆分文本
全部直接丢弃，不产生候选。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core import textutil
from core.db import dumps, execute, json_field, now, query_all, query_one, query_scalar
from core import errors
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log

from . import entries

logger = get_logger("aibar.promptlib.learning")

# ---------------------------------------------------------------- 阈值常量

# auto（生成学习）：分类置信度 ≥ 0.85 且质量分 ≥ 80
AUTO_MIN_CONFIDENCE = 0.85
AUTO_MIN_QUALITY = 80
# reverse_metadata（元数据恢复）：来源可靠但内容仍需精品，质量分 ≥ 85
REVERSE_METADATA_MIN_QUALITY = 85
# reverse_vision（视觉反推）：置信度 ≥ 0.90 且质量分 ≥ 85 且无风险标记
REVERSE_VISION_MIN_CONFIDENCE = 0.90
REVERSE_VISION_MIN_QUALITY = 85
# 候选区：质量 60–84 或（auto / reverse_vision）置信度 0.60–阈值上限
CANDIDATE_MIN_QUALITY = 60
CANDIDATE_MAX_QUALITY = 84
CANDIDATE_MIN_CONFIDENCE = 0.60

DEFAULT_THRESHOLDS: dict[str, float] = {
    "auto_min_confidence": AUTO_MIN_CONFIDENCE,
    "auto_min_quality": AUTO_MIN_QUALITY,
    "reverse_metadata_min_quality": REVERSE_METADATA_MIN_QUALITY,
    "reverse_vision_min_confidence": REVERSE_VISION_MIN_CONFIDENCE,
    "reverse_vision_min_quality": REVERSE_VISION_MIN_QUALITY,
    "candidate_min_quality": CANDIDATE_MIN_QUALITY,
    "candidate_max_quality": CANDIDATE_MAX_QUALITY,
    "candidate_min_confidence": CANDIDATE_MIN_CONFIDENCE,
}

INGEST_SOURCE_TYPES = ("auto", "reverse_metadata", "reverse_vision")
CANDIDATE_STATUSES = ("pending", "approved", "rejected")
GENERATION_STATUSES = ("pending", "running", "completed", "failed", "cancelled")

# 来源追溯只保留可对外暴露的标识，绝不写入本机绝对路径
_FORBIDDEN_REF_KEYS = {
    "path",
    "source_path",
    "file_path",
    "abs_path",
    "absolute_path",
    "dir",
    "directory",
    "filename",
}
MAX_REF_VALUE_LEN = 200


# ---------------------------------------------------------------- 校验小工具


def _validate_media_type(media_type: Any) -> str:
    media = str(media_type or "").strip()
    if media not in entries.media_type_keys():
        raise AIBARError("invalid_input", f"未知的媒体类型：{media or '(空)'}")
    return media


def _validate_page(value: Any, default: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", "分页参数必须是整数")
    if page < 1 or page > high:
        raise AIBARError("invalid_input", f"分页参数必须在 1 到 {high} 之间")
    return page


# ---------------------------------------------------------------- 对外主入口


def ingest_segments(
    segments: list[dict],
    media_type: str,
    source_type: str,
    source_ref: dict,
    thresholds: dict | None = None,
) -> dict:
    """把候选语义片段按阈值分流到正式词库 / 候选区 / 丢弃。

    Args:
        segments: ``[{"text", "dimension", "subcategory", "confidence", "model_profiles", "tags"}]``；
            ``dimension`` 为空时由 ``classify_dimension`` 归类并给出置信度。
        media_type: ``image`` | ``video`` | ``music``。
        source_type: ``auto``（生成学习）| ``reverse_metadata`` | ``reverse_vision``。
        source_ref: 来源追溯字典（工作流 ID / 生成记录 ID / 反推 job id 等，不存绝对路径）。
        thresholds: 可选覆盖默认阈值。

    Returns:
        ``{"inserted": [entry_id...], "merged": [entry_id...], "candidates": [candidate_id...],
        "discarded": [{"text", "reason"}...]}``
    """
    if not isinstance(segments, list):
        raise AIBARError("invalid_input", "segments 必须是数组")
    media = _validate_media_type(media_type)
    if source_type not in INGEST_SOURCE_TYPES:
        raise AIBARError("invalid_input", f"未知的入库来源：{source_type}")
    resolved = _resolve_thresholds(thresholds)
    reference = _clean_source_ref(source_ref)

    result: dict[str, list] = {"inserted": [], "merged": [], "candidates": [], "discarded": []}
    # 近似重复的候选集在整批里几乎不变，建一次索引给所有片段复用
    near_index = _NearDupIndex()
    for segment in segments:
        if not isinstance(segment, dict):
            result["discarded"].append({"text": str(segment), "reason": "invalid_segment"})
            continue
        _process_segment(segment, media, source_type, reference, resolved, result, near_index)

    safe_log(
        logger,
        logging.INFO,
        "ingest_segments",
        media_type=media,
        source_type=source_type,
        inserted=len(result["inserted"]),
        merged=len(result["merged"]),
        candidates=len(result["candidates"]),
        discarded=len(result["discarded"]),
    )
    return result


# ---------------------------------------------------------------- 单片段处理


def _process_segment(
    segment: dict,
    media_type: str,
    source_type: str,
    source_ref: dict,
    thresholds: dict[str, float],
    result: dict[str, list],
    near_index: "_NearDupIndex | None" = None,
) -> None:
    text = textutil.sanitize_model_text(str(segment.get("text") or ""), 400).strip()
    if not text:
        result["discarded"].append({"text": "", "reason": "empty"})
        return

    dimension, confidence = _resolve_dimension(text, media_type, segment)
    subcategory = _resolve_subcategory(media_type, dimension, segment.get("subcategory"))
    profiles = _resolve_profiles(segment.get("model_profiles"))
    tags = _resolve_tags(segment.get("tags"))

    # ---- 全部过滤：停用句式 / 质量口号 / 运行参数、内容价值、风险、忽略指纹
    # 先判定具体噪声类型再判定内容价值：``has_content_value`` 内部已包含前三类检查，
    # 若先调用它，噪声片段会一律落到 ``no_content_value``，丢弃原因无法区分。
    if textutil.is_stop_phrase(text) or textutil.is_quality_slogan(text) or textutil.is_run_parameter(text):
        result["discarded"].append({"text": text, "reason": "noise"})
        return
    if not textutil.has_content_value(text):
        result["discarded"].append({"text": text, "reason": "no_content_value"})
        return
    flags = textutil.risk_flags(text)
    if flags:
        result["discarded"].append({"text": text, "reason": f"risk:{','.join(flags)}"})
        return

    fingerprint = textutil.content_fingerprint(media_type, dimension, text)
    if entries.is_ignored_fingerprint(fingerprint):
        result["discarded"].append({"text": text, "reason": "ignored"})
        return

    quality = textutil.quality_score(text, dimension)

    # ---- 精确去重：命中已有词条或别名只累计使用记录
    existing = entries.find_entry_by_fingerprint(fingerprint)
    if existing is not None:
        entries.touch_entry(int(existing["id"]), source_ref)
        result["merged"].append(int(existing["id"]))
        return
    alias = entries.find_alias_entry(textutil.normalize_text(text))
    if alias is not None:
        entries.touch_entry(int(alias["prompt_entry_id"]), source_ref)
        result["merged"].append(int(alias["prompt_entry_id"]))
        return

    # ---- 近似重复：记为既有词条的别名，不新建正式词条
    near = _find_near_duplicate(media_type, dimension, text, near_index)
    if near is not None:
        if quality >= thresholds["candidate_min_quality"]:
            entries.add_alias(near, text, source_type, source_ref)
            if near_index is not None:
                near_index.register(media_type, dimension, near, text)
            entries.touch_entry(near, source_ref)
            result["merged"].append(near)
        else:
            result["discarded"].append({"text": text, "reason": "low_quality_near_duplicate"})
        return

    if _should_insert(source_type, confidence, quality, thresholds):
        entry = _insert_entry(
            text=text,
            media_type=media_type,
            dimension=dimension,
            subcategory=subcategory,
            source_type=source_type,
            source_ref=source_ref,
            quality=quality,
            profiles=profiles,
            tags=tags,
        )
        if near_index is not None:
            # 同批次的下一个片段要比对新写进去的这条，语义才与"每次都查库"一致
            near_index.register(media_type, dimension, int(entry["id"]), text)
        result["inserted"].append(int(entry["id"]))
        return

    if _should_candidate(source_type, confidence, quality, thresholds):
        candidate_id = _upsert_candidate(
            text=text,
            media_type=media_type,
            dimension=dimension,
            subcategory=subcategory,
            source_ref=source_ref,
            confidence=confidence,
            quality=quality,
            fingerprint=fingerprint,
        )
        result["candidates"].append(candidate_id)
        return

    result["discarded"].append(
        {"text": text, "reason": _discard_reason(source_type, confidence, quality, thresholds)}
    )


def _resolve_dimension(text: str, media_type: str, segment: dict) -> tuple[str, float]:
    """确定维度与置信度：调用方给出维度时以调用方为准，否则用确定性归类。"""
    provided = str(segment.get("dimension") or "").strip()
    confidence_raw = segment.get("confidence")
    confidence: float | None = None
    if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
        confidence = float(confidence_raw)

    if provided and provided in textutil.dimension_keys(media_type):
        # 维度来自发起时记录或用户确认，视为可信；未给置信度时按完全可信处理
        return provided, (1.0 if confidence is None else max(0.0, min(1.0, confidence)))

    dimension, auto_confidence = textutil.classify_dimension(text, media_type)
    return dimension, (auto_confidence if confidence is None else max(0.0, min(1.0, confidence)))


def _resolve_subcategory(media_type: str, dimension: str, value: Any) -> str:
    subcategory = str(value or "").strip()
    allowed = entries.subcategory_keys(media_type, dimension)
    if allowed and subcategory not in allowed:
        return ""
    return subcategory


def _resolve_profiles(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["generic"]
    allowed = textutil.profile_keys()
    profiles = [str(item).strip() for item in value if isinstance(item, str) and str(item).strip() in allowed]
    return profiles or ["generic"]


def _resolve_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()[:24]
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:20]


# ---------------------------------------------------------------- 阈值判定


def _resolve_thresholds(thresholds: dict | None) -> dict[str, float]:
    resolved = dict(DEFAULT_THRESHOLDS)
    if not thresholds:
        return resolved
    if not isinstance(thresholds, dict):
        raise AIBARError("invalid_input", "thresholds 必须是对象")
    for key, value in thresholds.items():
        if key not in resolved:
            raise AIBARError("invalid_input", f"未知的阈值项：{key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AIBARError("invalid_input", f"阈值 {key} 必须是数字")
        resolved[key] = float(value)
    return resolved


def _should_insert(source_type: str, confidence: float, quality: int, thresholds: dict[str, float]) -> bool:
    if source_type == "auto":
        return confidence >= thresholds["auto_min_confidence"] and quality >= thresholds["auto_min_quality"]
    if source_type == "reverse_metadata":
        return quality >= thresholds["reverse_metadata_min_quality"]
    if source_type == "reverse_vision":
        return (
            confidence >= thresholds["reverse_vision_min_confidence"]
            and quality >= thresholds["reverse_vision_min_quality"]
        )
    return False


def _should_candidate(source_type: str, confidence: float, quality: int, thresholds: dict[str, float]) -> bool:
    if quality < thresholds["candidate_min_quality"]:
        return False
    if quality <= thresholds["candidate_max_quality"]:
        return True
    # 质量达标但置信度不足：auto 与 reverse_vision 的中低置信片段进入候选
    if source_type == "auto":
        return thresholds["candidate_min_confidence"] <= confidence < thresholds["auto_min_confidence"]
    if source_type == "reverse_vision":
        return thresholds["candidate_min_confidence"] <= confidence < thresholds["reverse_vision_min_confidence"]
    return False


def _discard_reason(source_type: str, confidence: float, quality: int, thresholds: dict[str, float]) -> str:
    if quality < thresholds["candidate_min_quality"]:
        return "low_quality"
    if source_type in ("auto", "reverse_vision") and confidence < thresholds["candidate_min_confidence"]:
        return "low_confidence"
    return "below_threshold"


# ---------------------------------------------------------------- 近似重复


def _load_near_candidates(media_type: str, dimension: str) -> list[tuple[int, str]]:
    """同媒体同维度内可用于比对的全部文本（词条正文 + 别名）。

    隐藏词条不参与：它们已经被用户"删掉"了，不该再把新片段并到它们身上。
    """
    rows = query_all(
        "SELECT id, prompt_text FROM prompt_entries WHERE media_type = ? AND dimension = ? AND is_hidden = 0",
        (media_type, dimension),
    )
    candidates = [(int(row["id"]), row["prompt_text"]) for row in rows]
    alias_rows = query_all(
        """
        SELECT a.prompt_entry_id AS entry_id, a.alias_text AS alias_text
        FROM prompt_entry_aliases a
        JOIN prompt_entries e ON e.id = a.prompt_entry_id
        WHERE e.media_type = ? AND e.dimension = ? AND e.is_hidden = 0
        """,
        (media_type, dimension),
    )
    candidates += [(int(row["entry_id"]), row["alias_text"]) for row in alias_rows]
    return candidates


class _NearDupIndex:
    """一次 ``ingest_segments`` 内复用的近似重复候选索引。

    原本 :func:`_find_near_duplicate` 对**每个片段**都把同维度的全部词条与别名
    重新查一遍：40 个片段 × 800 条词条 = 3.2 万行记录反复从 SQLite 取出、构造
    Row 对象再丢弃，而这些候选在同一批次里几乎不变。这里按
    ``(media_type, dimension)`` 只加载一次（实测 40 片段的 SQL 次数 240 → 162）。

    .. note::
       它**不减少相似度计算的次数** —— 片段与候选两两比对是 O(N×M)，
       这才是当前的主要开销（实测占 0.41s 中的约 0.32s）。要省掉它得引入
       倒排/分块索引，会牺牲召回率，属于独立议题，不在本次改动范围内。

    **正确性的关键在于写入要回填**：批次内新建的词条与新登记的别名，必须让
    后续片段看得见，否则同一批次里两个高度相似的片段会被当成两条互不相干的
    词条各自入库。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._loads = 0

    @property
    def loads(self) -> int:
        """实际回库加载的次数（测试用来断言"没有退化成逐片段查库"）。"""
        return self._loads

    def find(self, media_type: str, dimension: str, text: str) -> int | None:
        key = (media_type, dimension)
        candidates = self._cache.get(key)
        if candidates is None:
            candidates = _load_near_candidates(media_type, dimension)
            self._cache[key] = candidates
            self._loads += 1
        best_id: int | None = None
        best_score = 0.0
        for entry_id, other_text in candidates:
            score = textutil.similarity(text, other_text or "")
            if score >= textutil.NEAR_DUPLICATE_THRESHOLD and score > best_score:
                best_id, best_score = entry_id, score
        return best_id

    def register(self, media_type: str, dimension: str, entry_id: int, text: str) -> None:
        """登记一个刚写入本维度的文本（新词条或新别名）。

        该维度尚未加载过时无需登记 —— 下次 ``find`` 会整体从库里读，
        自然包含这条。
        """
        candidates = self._cache.get((media_type, dimension))
        if candidates is not None:
            candidates.append((int(entry_id), str(text or "")))


def _find_near_duplicate(
    media_type: str,
    dimension: str,
    text: str,
    index: "_NearDupIndex | None" = None,
) -> int | None:
    """在同媒体同维度内找一个高度相似的既有词条，返回其 ID。

    Args:
        index: 批次内复用的候选索引。省略时退化成"每次都查库"，
            语义完全一致，只是慢 —— 单条调用方走这条路。
    """
    if index is not None:
        return index.find(media_type, dimension, text)

    best_id: int | None = None
    best_score = 0.0
    for entry_id, other_text in _load_near_candidates(media_type, dimension):
        score = textutil.similarity(text, other_text or "")
        if score >= textutil.NEAR_DUPLICATE_THRESHOLD and score > best_score:
            best_id, best_score = entry_id, score
    return best_id


# ---------------------------------------------------------------- 写入


def _insert_entry(
    text: str,
    media_type: str,
    dimension: str,
    subcategory: str,
    source_type: str,
    source_ref: dict,
    quality: int,
    profiles: list[str],
    tags: list[str],
) -> dict:
    return entries.insert_entry(
        {
            "media_type": media_type,
            "dimension": dimension,
            "subcategory": subcategory,
            "title": _derive_title(text),
            "prompt_text": text,
            "language": "zh" if any("一" <= char <= "鿿" for char in text) else "en",
            "model_profiles": profiles,
            "tags": tags,
            "description": "",
            "source_type": source_type,
            "source_ref": source_ref,
            "quality_score": quality,
        }
    )


def _derive_title(text: str) -> str:
    head = re.split(r"[，,。；;！!？?]", text.strip())[0].strip()
    return (head or text.strip())[:60]


def _upsert_candidate(
    text: str,
    media_type: str,
    dimension: str,
    subcategory: str,
    source_ref: dict,
    confidence: float,
    quality: int,
    fingerprint: str,
) -> int:
    timestamp = now()
    existing = query_one("SELECT id FROM prompt_candidates WHERE content_fingerprint = ?", (fingerprint,))
    if existing is not None:
        execute(
            """
            UPDATE prompt_candidates
            SET occurrence_count = occurrence_count + 1, last_seen_at = ?,
                confidence = ?, quality_score = ?, source_ref = ?,
                suggested_dimension = ?, suggested_subcategory = ?
            WHERE id = ?
            """,
            (
                timestamp,
                confidence,
                quality,
                dumps(source_ref) if source_ref else None,
                dimension,
                subcategory,
                int(existing["id"]),
            ),
        )
        return int(existing["id"])
    cursor = execute(
        """
        INSERT INTO prompt_candidates (
            media_type, raw_text, normalized_text, suggested_dimension, suggested_subcategory,
            source_ref, confidence, quality_score, occurrence_count, review_status,
            content_fingerprint, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media_type,
            text,
            textutil.normalize_text(text),
            dimension,
            subcategory,
            dumps(source_ref) if source_ref else None,
            confidence,
            quality,
            1,
            "pending",
            fingerprint,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------- 生成后学习


def record_generation(
    media_type: str,
    workflow_id: str | None,
    model_profile: str | None,
    original_prompt: str,
    structured_sections: list[dict] | None,
    output_ref: dict | None = None,
) -> int:
    """记录一次生成发起时真正送入工作流的提示词（PRD M7.5「以发起时记录为主」）。

    Returns:
        新的 ``generation_prompt_usage`` 记录 ID。
    """
    media = _validate_media_type(media_type)
    cursor = execute(
        """
        INSERT INTO generation_prompt_usage (
            media_type, workflow_id, model_profile, original_prompt,
            structured_sections, output_ref, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            media,
            (str(workflow_id).strip()[:200] if workflow_id else None),
            (str(model_profile).strip()[:64] if model_profile else None),
            str(original_prompt or "")[:4000],
            dumps(structured_sections or []),
            dumps(_clean_source_ref(output_ref)) if output_ref else None,
            "pending",
            now(),
        ),
    )
    return int(cursor.lastrowid)


def mark_generation_status(usage_id: int, status: str, output_ref: dict | None = None) -> dict:
    """推进生成记录状态；完成时写入 ``completed_at`` 与输出关联键。"""
    if status not in GENERATION_STATUSES:
        raise AIBARError("invalid_input", f"未知的 generation 状态：{status}")
    row = query_one("SELECT * FROM generation_prompt_usage WHERE id = ?", (usage_id,))
    if row is None:
        raise errors.not_found("生成记录不存在")
    timestamp = now()
    if output_ref:
        merged = dict(json_field(row["output_ref"], {}) or {})
        if not isinstance(merged, dict):
            merged = {}
        merged.update(_clean_source_ref(output_ref))
        execute(
            "UPDATE generation_prompt_usage SET status = ?, completed_at = ?, output_ref = ? WHERE id = ?",
            (status, timestamp if status == "completed" else row["completed_at"], dumps(merged), usage_id),
        )
    else:
        execute(
            "UPDATE generation_prompt_usage SET status = ?, completed_at = ? WHERE id = ?",
            (status, timestamp if status == "completed" else row["completed_at"], usage_id),
        )
    return {
        "id": usage_id,
        "status": status,
        "completed_at": query_scalar("SELECT completed_at FROM generation_prompt_usage WHERE id = ?", (usage_id,)),
    }


def learn_from_generation(usage_id: int) -> dict:
    """从一条已完成的生成记录中学习高质量片段。

    生成失败、中止或只有参数没有内容描述时不学习（不写 ``learned_at``）。
    """
    empty: dict[str, list] = {"inserted": [], "merged": [], "candidates": [], "discarded": []}
    row = query_one("SELECT * FROM generation_prompt_usage WHERE id = ?", (usage_id,))
    if row is None:
        raise errors.not_found("生成记录不存在")

    status = row["status"] or ""
    if status != "completed":
        return {**empty, "learned": False, "reason": "status_not_completed", "status": status}

    media_type = row["media_type"] or "image"
    # 以发起时记录的结构化片段为主，缺失时再按标点切分原始提示词作为补充
    segments = _sections_to_segments(json_field(row["structured_sections"], []))
    if not segments:
        segments = _split_prompt_segments(row["original_prompt"] or "", media_type)
    usable = [item for item in segments if textutil.has_content_value(str(item.get("text") or ""))]
    if not usable:
        return {**empty, "learned": False, "reason": "no_content", "status": status}

    source_ref = _clean_source_ref(
        {
            "usage_id": usage_id,
            "workflow_id": row["workflow_id"],
            "model_profile": row["model_profile"],
        }
    )
    result = ingest_segments(usable, media_type, "auto", source_ref)
    execute("UPDATE generation_prompt_usage SET learned_at = ? WHERE id = ?", (now(), usage_id))
    return {**result, "learned": True, "reason": "", "status": status}


def _sections_to_segments(sections: Any) -> list[dict]:
    """把发起时记录的结构化段落转成流水线输入；兼容纯字符串段落。"""
    segments: list[dict] = []
    if not isinstance(sections, list):
        return segments
    for item in sections:
        if isinstance(item, str):
            text = item.strip()
            if text:
                segments.append({"text": text, "dimension": "", "subcategory": "", "confidence": None})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or item.get("value") or "").strip()
        if not text:
            continue
        segments.append(
            {
                "text": text,
                "dimension": str(item.get("dimension") or "").strip(),
                "subcategory": str(item.get("subcategory") or "").strip(),
                "confidence": item.get("confidence"),
                "model_profiles": item.get("model_profiles") or [],
                "tags": item.get("tags") or [],
            }
        )
    return segments


def _split_prompt_segments(text: str, media_type: str) -> list[dict]:
    segments: list[dict] = []
    for piece in textutil.split_segments(text):
        dimension, confidence = textutil.classify_dimension(piece, media_type)
        segments.append({"text": piece, "dimension": dimension, "confidence": confidence})
    return segments


# ---------------------------------------------------------------- 候选审核


def list_candidates(
    status: str = "pending",
    media_type: str | None = None,
    page: int = 1,
    page_size: int = 24,
) -> dict:
    """分页查询候选区。``status`` 为空字符串时返回全部状态。"""
    status_key = str(status or "pending").strip()
    if status_key and status_key not in CANDIDATE_STATUSES:
        raise AIBARError("invalid_input", f"未知的审核状态：{status_key}")
    media = _validate_media_type(media_type) if media_type else ""
    page_value = _validate_page(page, 1, entries.MAX_PAGE)
    page_size_value = _validate_page(page_size, entries.DEFAULT_PAGE_SIZE, entries.MAX_PAGE_SIZE)

    clauses: list[str] = []
    params: list[Any] = []
    if status_key:
        clauses.append("review_status = ?")
        params.append(status_key)
    if media:
        clauses.append("media_type = ?")
        params.append(media)
    where = " AND ".join(clauses) if clauses else "1 = 1"

    total = int(query_scalar(f"SELECT COUNT(*) FROM prompt_candidates WHERE {where}", params, 0) or 0)
    offset = (page_value - 1) * page_size_value
    rows = query_all(
        f"SELECT * FROM prompt_candidates WHERE {where} ORDER BY quality_score DESC, id DESC LIMIT ? OFFSET ?",
        params + [page_size_value, offset],
    )
    items = [_candidate_item(row) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page_value,
        "page_size": page_size_value,
        "has_more": offset + len(items) < total,
    }


def _candidate_item(row: Any) -> dict:
    media_type = row["media_type"]
    dimension = row["suggested_dimension"] or ""
    subcategory = row["suggested_subcategory"] or ""
    return {
        "id": row["id"],
        "media_type": media_type,
        "media_type_label": entries.media_type_label(media_type),
        "raw_text": row["raw_text"],
        "normalized_text": row["normalized_text"],
        "suggested_dimension": dimension,
        "dimension_label": textutil.dimension_label(media_type, dimension) if dimension else "",
        "suggested_subcategory": subcategory,
        "subcategory_label": textutil.subcategory_label(media_type, dimension, subcategory) if dimension else "",
        "source_ref": json_field(row["source_ref"], {}) or {},
        "confidence": row["confidence"],
        "quality_score": row["quality_score"],
        "occurrence_count": row["occurrence_count"],
        "review_status": row["review_status"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "reviewed_at": row["reviewed_at"],
    }


def approve_candidate(candidate_id: int, patch: dict | None = None) -> dict:
    """批准候选并转为正式词条。

    Args:
        candidate_id: 候选 ID。
        patch: 可覆盖 ``dimension`` / ``subcategory`` / ``title`` / ``source_type``。
    """
    row = query_one("SELECT * FROM prompt_candidates WHERE id = ?", (candidate_id,))
    if row is None:
        raise errors.not_found("候选词条不存在")
    if not isinstance(patch, dict):
        if patch is not None:
            raise AIBARError("invalid_input", "patch 必须是 JSON 对象")
        patch = {}

    media_type = row["media_type"]
    text = row["raw_text"] or row["normalized_text"]
    dimension = str(patch.get("dimension") or "").strip() or (row["suggested_dimension"] or "")
    if dimension not in textutil.dimension_keys(media_type):
        dimension, _confidence = textutil.classify_dimension(text, media_type)
    subcategory = _resolve_subcategory(
        media_type, dimension, patch.get("subcategory") or row["suggested_subcategory"] or ""
    )
    source_type = str(patch.get("source_type") or "manual").strip()
    if source_type not in entries.SOURCE_TYPES:
        raise AIBARError("invalid_input", f"未知的来源类型：{source_type}")

    fingerprint = textutil.content_fingerprint(media_type, dimension, text)
    existing = entries.find_entry_by_fingerprint(fingerprint)
    if existing is not None:
        entries.touch_entry(int(existing["id"]))
        entry_id = int(existing["id"])
    else:
        entry = entries.insert_entry(
            {
                "media_type": media_type,
                "dimension": dimension,
                "subcategory": subcategory,
                "title": str(patch.get("title") or "").strip() or _derive_title(text),
                "prompt_text": text,
                "language": "zh" if any("一" <= char <= "鿿" for char in text) else "en",
                "model_profiles": ["generic"],
                "tags": [],
                "description": "",
                "source_type": source_type,
                "source_ref": _clean_source_ref(json_field(row["source_ref"], {}) or {}),
                "quality_score": int(row["quality_score"] or 80),
            }
        )
        entry_id = int(entry["id"])

    execute(
        "UPDATE prompt_candidates SET review_status = 'approved', reviewed_at = ? WHERE id = ?",
        (now(), candidate_id),
    )
    return {
        "id": candidate_id,
        "entry_id": entry_id,
        "review_status": "approved",
        "merged": existing is not None,
    }


def reject_candidate(candidate_id: int) -> dict:
    """拒绝候选：标记 rejected 并写入忽略指纹，默认不再推荐。"""
    row = query_one("SELECT * FROM prompt_candidates WHERE id = ?", (candidate_id,))
    if row is None:
        raise errors.not_found("候选词条不存在")
    execute(
        "UPDATE prompt_candidates SET review_status = 'rejected', reviewed_at = ? WHERE id = ?",
        (now(), candidate_id),
    )
    entries.add_ignored_fingerprint(row["content_fingerprint"], "rejected")
    return {"id": candidate_id, "review_status": "rejected"}


# ---------------------------------------------------------------- 工具


def _clean_source_ref(source_ref: Any) -> dict:
    """只保留可对外暴露的标量标识，剔除路径类字段，避免泄露本机绝对路径。"""
    if not isinstance(source_ref, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for raw_key, value in source_ref.items():
        key = str(raw_key).strip()
        if not key or key.lower() in _FORBIDDEN_REF_KEYS:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, str):
            if len(value) <= MAX_REF_VALUE_LEN:
                cleaned[key] = value
    return cleaned


__all__ = [
    "ingest_segments",
    "learn_from_generation",
    "record_generation",
    "mark_generation_status",
    "list_candidates",
    "approve_candidate",
    "reject_candidate",
    "DEFAULT_THRESHOLDS",
    "CANDIDATE_STATUSES",
    "INGEST_SOURCE_TYPES",
]
