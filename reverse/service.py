"""M9.3 / M9.6 / M9.7 反推任务编排与入库流水线。

设计要点（PRD M9.3 / M9.6 / M9.7 / M11.4）：
- 任务**同步执行**，但数据模型保留 ``job_id / status / stage``，便于后续改为异步；
- 阶段进度只报**可读阶段名**（读取图片 / 检查元数据 / 视觉分析 / 结构化整理），不使用虚假百分比；
- 取消只停止未完成任务，**不删除上次成功结果**；
- 失败保留图片、设置与上次有效结果；替换图片后旧结果标记过期；
- 只有用户点「保存」才触发词库更新，流水线为
  ``用户编辑后文本 → 按维度拆分 → 移除停用句式/运行参数 → 规范化 → 精确指纹去重 →
  近似重复判断 → 风险与不确定性过滤 → 质量/置信度评分 → 正式入库或候选``；
- **禁止**把 ``formatted_positive``、整段恢复提示词或完整视觉描述作为单条词条入库；
- 与 M7 的耦合只通过 ``_ingest()`` 薄封装（函数内延迟 import，便于测试打桩）。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any

from core.db import (
    dumps,
    execute,
    json_field,
    now as db_now,
    query_all,
    query_one,
    query_scalar,
)
from core import errors
from core.errors import AIBARError
from core.logging_setup import get_logger
from core.textutil import (
    classify_dimension,
    content_fingerprint,
    get_profile,
    has_content_value,
    is_near_duplicate,
    is_quality_slogan,
    is_run_parameter,
    is_stop_phrase,
    profile_keys,
    quality_score,
    risk_flags,
    split_segments,
)

from . import schema, uploads
from .providers import base, registry
from .providers.metadata import METADATA_NOT_FOUND

_LOGGER = get_logger("aibar.reverse.service")

# ---------------------------------------------------------------- 常量

SOURCE_MODE_AUTO = "auto"
SOURCE_MODE_METADATA = "metadata"
SOURCE_MODE_VISION = "vision"
SOURCE_MODES = (SOURCE_MODE_AUTO, SOURCE_MODE_METADATA, SOURCE_MODE_VISION)
SOURCE_MODE_LABELS = {
    SOURCE_MODE_AUTO: "自动（优先元数据）",
    SOURCE_MODE_METADATA: "仅恢复内嵌元数据",
    SOURCE_MODE_VISION: "仅视觉反推",
}

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
JOB_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

STAGE_QUEUED = "queued"
STAGE_READING = "reading"
STAGE_METADATA = "metadata"
STAGE_VISION = "vision"
STAGE_STRUCTURING = "structuring"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"

# 阶段进度只用可读阶段名，不编造百分比（PRD M9.3）
STAGE_LABELS = {
    STAGE_QUEUED: "排队中",
    STAGE_READING: "读取图片",
    STAGE_METADATA: "检查元数据",
    STAGE_VISION: "视觉分析",
    STAGE_STRUCTURING: "结构化整理",
    STAGE_DONE: "已完成",
    STAGE_FAILED: "失败",
    STAGE_CANCELLED: "已取消",
}
STAGE_ORDER = (
    STAGE_QUEUED,
    STAGE_READING,
    STAGE_METADATA,
    STAGE_VISION,
    STAGE_STRUCTURING,
    STAGE_DONE,
)

DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100

# 单条词条的长度上限与 core.textutil 保持一致（超过即视为未拆分的整段）
MAX_SEGMENT_LENGTH = 80
# 带不确定性说明且低于该置信度的片段不入库
UNCERTAIN_MIN_CONFIDENCE = 0.60
# 低于该质量分的片段直接丢弃，连候选都不进（与 M7 候选下限一致）
MIN_SAVE_QUALITY = 60

RESULT_FIELDS = (
    "source_type",
    "source_label",
    "sections",
    "formatted_positive",
    "formatted_negative",
    "recovered_original_prompt",
    "recovered_negative_prompt",
    "raw_caption",
    "warnings",
    "quality_tier",
    "image_ref",
)

_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
_CHUNK_SIZE = 65536

_CANCEL_EVENTS: dict[int, threading.Event] = {}
_CANCEL_LOCK = threading.RLock()

# 后台执行中的任务：job_id -> 线程。锁同时保护 _ACTIVE_JOBS。
_WORKERS: dict[int, threading.Thread] = {}
# 正在跑的 job_id（同步与异步两种模式都登记），用来判断「还在跑」还是「已经成了孤儿」。
_ACTIVE_JOBS: set[int] = set()
_WORKER_LOCK = threading.RLock()


# ---------------------------------------------------------------- M7 薄封装


def _ingest(
    segments: list[dict],
    media_type: str,
    source_type: str,
    source_ref: dict,
    thresholds: dict | None = None,
) -> dict:
    """薄封装 M7 的入库流水线。

    延迟 import 有两个原因：避免 ``reverse`` 与 ``promptlib`` 在装配期互相依赖，
    并让测试可以直接 monkeypatch ``promptlib.learning.ingest_segments``。
    """
    from promptlib.learning import ingest_segments

    return ingest_segments(segments, media_type, source_type, source_ref, thresholds)


# ---------------------------------------------------------------- 参数校验


def _validate_source_mode(value: Any) -> str:
    mode = str(value or SOURCE_MODE_AUTO).strip() or SOURCE_MODE_AUTO
    if mode not in SOURCE_MODES:
        raise AIBARError("invalid_input", "未知的反推来源模式")
    return mode


def _validate_profile(value: Any) -> str:
    profile = str(value or "generic").strip() or "generic"
    if profile != "generic" and profile not in profile_keys():
        raise AIBARError("invalid_input", "未知的目标模型档案")
    return profile


def _validate_precision(value: Any) -> str:
    precision = str(value or schema.DEFAULT_PRECISION).strip() or schema.DEFAULT_PRECISION
    if precision not in schema.PRECISIONS:
        raise AIBARError("invalid_input", "未知的分析精度")
    return precision


def _validate_provider(value: Any) -> str:
    requested = str(value or "auto").strip() or "auto"
    if requested != "auto":
        registry.get_provider(requested)
    return requested


def _validate_limit(value: Any, default: int = DEFAULT_HISTORY_LIMIT) -> int:
    if value is None or value == "":
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", "limit 必须是整数")
    return max(1, min(MAX_HISTORY_LIMIT, limit))


# ---------------------------------------------------------------- 取消


def _register_cancel(job_id: int) -> threading.Event:
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[job_id] = event
    return event


def _clear_cancel(job_id: int) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(job_id, None)


def request_cancel(job_id: int) -> bool:
    """标记取消。只影响仍在运行的任务，不触碰已完成的结果。"""
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(int(job_id))
    if event is None:
        return False
    event.set()
    return True


# ---------------------------------------------------------------- 在途标记


def _mark_active(job_id: int) -> None:
    with _WORKER_LOCK:
        _ACTIVE_JOBS.add(int(job_id))


def _clear_active(job_id: int) -> None:
    with _WORKER_LOCK:
        _ACTIVE_JOBS.discard(int(job_id))
        _WORKERS.pop(int(job_id), None)


def is_active(job_id: int) -> bool:
    """该任务此刻是否有线程在跑（同步模式也算）。

    用途：区分「还在跑」与「卡住了」。服务重启后，数据库里上一轮留下的
    ``pending/running`` 任务再也不会有人推进，前端会一直轮询下去；
    靠这个标记就能在读取时把它们判死，而不是让界面转圈到天荒地老。
    """
    with _WORKER_LOCK:
        return int(job_id) in _ACTIVE_JOBS


# ---------------------------------------------------------------- 任务读写


def _insert_job(
    *,
    image_id: str | None,
    source_mode: str,
    profile: str,
    precision: str,
    provider: str,
) -> int:
    cursor = execute(
        "INSERT INTO prompt_reverse_jobs "
        "(image_id, upload_ref, content_hash, source_mode, provider, provider_model, "
        " model_profile, precision_level, status, stage, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            image_id or None,
            None,  # upload_ref 在读取图片后按真实后缀回填
            "",
            source_mode,
            "",
            "",
            profile,
            precision,
            STATUS_PENDING,
            STAGE_QUEUED,
            db_now(),
        ),
    )
    return int(cursor.lastrowid)


def _set_stage(job_id: int, stage: str, status: str | None = None) -> None:
    if status:
        execute("UPDATE prompt_reverse_jobs SET stage=?, status=? WHERE id=?", (stage, status, job_id))
    else:
        execute("UPDATE prompt_reverse_jobs SET stage=? WHERE id=?", (stage, job_id))


def _fail(job_id: int, error_code: str, stage: str = STAGE_FAILED) -> None:
    execute(
        "UPDATE prompt_reverse_jobs SET status=?, stage=?, error_code=?, completed_at=? WHERE id=?",
        (STATUS_FAILED, stage, str(error_code or "")[:64], db_now(), job_id),
    )


def _mark_cancelled(job_id: int) -> None:
    execute(
        "UPDATE prompt_reverse_jobs SET status=?, stage=?, error_code=?, completed_at=? WHERE id=?",
        (STATUS_CANCELLED, STAGE_CANCELLED, "cancelled", db_now(), job_id),
    )


def _complete(job_id: int, result: dict, provider_key: str, duration_ms: int) -> None:
    payload = dumps(result)
    execute(
        "UPDATE prompt_reverse_jobs SET status=?, stage=?, provider=?, provider_model=?, "
        "model_profile=?, precision_level=?, source_type=?, raw_result=?, structured_result=?, "
        "duration_ms=?, completed_at=?, error_code=NULL WHERE id=?",
        (
            STATUS_COMPLETED,
            STAGE_DONE,
            provider_key,
            str(result.get("provider_model") or "")[:120],
            str(result.get("model_profile") or "generic"),
            str(result.get("precision") or schema.DEFAULT_PRECISION),
            str(result.get("source_type") or ""),
            payload,
            payload,
            int(duration_ms),
            db_now(),
            job_id,
        ),
    )


def _job_row(job_id: int):
    row = query_one("SELECT * FROM prompt_reverse_jobs WHERE id=?", (int(job_id),))
    if row is None:
        raise errors.not_found("反推任务不存在")
    return row


def _upload_id_from_ref(ref: str | None) -> str:
    return Path(str(ref or "")).stem if ref else ""


def _source_key(row) -> tuple[str, str]:
    """任务的来源键：``(列名, 值)``。上传来源优先于图库来源。"""
    upload_id = _upload_id_from_ref(row["upload_ref"])
    if upload_id:
        return ("upload_ref", str(row["upload_ref"]))
    if row["image_id"]:
        return ("image_id", str(row["image_id"]))
    return ("", "")


def _scan_superseded(records: list[tuple[int, str]]) -> set[int]:
    """标出「后面出现过不同内容哈希」的任务 id —— 判定规则在此唯一实现。

    Args:
        records: ``(job_id, content_hash)`` 列表，只含**已完成**任务。

    Returns:
        被更新结果取代的 id 集合。

    从后往前扫一遍即可，无需两两比较：只要 ``id`` 更大的那批里存在
    **任意一个**与我不同的哈希，我就过期了。

    .. note::
       正确性依赖调用方把"所有可能构成取代关系的行"都放进 ``records``。
       历史列表按 ``id DESC`` 分页 ⇒ 页内就是全局 id 最大的一批，
       任何"更新的一条"必然同页，故传页内行即可（见 :func:`_stale_reasons`）。
    """
    ordered = sorted(records, key=lambda item: item[0])
    stale: set[int] = set()
    hashes_after: set[str] = set()
    for job_id, content_hash in reversed(ordered):
        if hashes_after - {content_hash}:
            stale.add(job_id)
        hashes_after.add(content_hash)
    return stale


#: 允许拼进 SQL 的来源列名白名单（``_source_key`` 只可能返回这些值）
_SOURCE_COLUMNS = ("upload_ref", "image_id")


def _stale_reason(row) -> str:
    """单条任务：旧结果是否因图片被替换或缓存过期而失效。

    与 :func:`_stale_reasons` 唯一的差别在「被更新的结果取代」这一步：
    列表路径可以只比页内行（见那里的注释），**单条任务没有"页"**，
    必须把同来源的已完成任务取出来比（1 次查询），否则会漏判——
    一条 3 天前的历史，它的更新结果很可能根本不在任何"当前页"里。
    """
    reason = _stale_reasons([row]).get(int(row["id"]), "")
    if reason:
        return reason
    if row["status"] != STATUS_COMPLETED:
        return ""
    column, value = _source_key(row)
    if column not in _SOURCE_COLUMNS:
        return ""
    peers = query_all(
        f"SELECT id, IFNULL(content_hash, '') AS content_hash FROM prompt_reverse_jobs "
        f"WHERE status = ? AND {column} = ?",
        (STATUS_COMPLETED, value),
    )
    records = [(int(item["id"]), str(item["content_hash"])) for item in peers]
    return "source_replaced" if int(row["id"]) in _scan_superseded(records) else ""


def _stale_reasons(rows: list) -> dict[int, str]:
    """批量判定过期原因 —— 恒定 1~2 次查询，与行数无关。

    此前每一行各跑 1~2 次查询（100 条历史 = 201 次 SQL）。这里改成一次
    ``WHERE id IN (...)`` 预取图片存在性，再用 :func:`_scan_superseded`
    在内存里一次性标出被取代的任务。

    Args:
        rows: **同一条 SQL 取出的**任务行，按 ``id DESC`` 排序（历史列表的分页结果）。
              单条任务路径同样适用：此时只有一行，退化成「只查这一行」。
    """
    reasons: dict[int, str] = {}
    if not rows:
        return reasons

    # ---- 1) 上传缓存是否还在（纯文件系统判断，无 SQL）
    upload_ids = {_upload_id_from_ref(row["upload_ref"]) for row in rows}
    upload_ids.discard("")
    alive_uploads = {uid for uid in upload_ids if uploads.resolve_path(uid) is not None}

    # ---- 2) 图库图片是否还在（1 次 IN 查询，替代逐行 EXISTS）
    image_ids = {str(row["image_id"]) for row in rows if row["image_id"]}
    alive_images: set[str] = set()
    if image_ids:
        placeholders = ",".join("?" * len(image_ids))
        alive_images = {
            str(row["id"])
            for row in query_all(f"SELECT id FROM images WHERE id IN ({placeholders})", tuple(image_ids))
        }

    # ---- 3) 被更新的结果取代（0 次查询）
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in rows:
        if row["status"] != STATUS_COMPLETED:
            continue
        key = _source_key(row)
        if not key[0]:
            continue
        groups.setdefault(key, []).append((int(row["id"]), str(row["content_hash"] or "")))
    superseded: set[int] = set()
    for records in groups.values():
        superseded |= _scan_superseded(records)

    for row in rows:
        job_id = int(row["id"])
        upload_id = _upload_id_from_ref(row["upload_ref"])
        if upload_id:
            if upload_id not in alive_uploads:
                reasons[job_id] = "upload_expired"
            elif job_id in superseded:
                reasons[job_id] = "source_replaced"
            continue
        if row["image_id"]:
            if str(row["image_id"]) not in alive_images:
                reasons[job_id] = "image_missing"
            elif job_id in superseded:
                reasons[job_id] = "source_replaced"
    return reasons


def _public_job(row, stale_reason: str | None = None) -> dict[str, Any]:
    """任务公共视图（不含任何本机绝对路径）。

    Args:
        stale_reason: 调用方已批量算好的过期原因。省略时才单算一次
            （单条任务路径；列表路径一律用 :func:`_stale_reasons` 预取以避免 N+1）。
    """
    structured = json_field(row["structured_result"], {}) or {}
    edited = json_field(row["edited_result"], {}) or {}
    result = edited if isinstance(edited, dict) and edited else structured
    result = result if isinstance(result, dict) else {}
    if stale_reason is None:
        stale_reason = _stale_reason(row)
    image_ref = result.get("image_ref") if isinstance(result.get("image_ref"), dict) else {}
    return {
        "job_id": int(row["id"]),
        "status": row["status"],
        "stage": row["stage"],
        "stage_label": STAGE_LABELS.get(row["stage"], str(row["stage"] or "")),
        "source_mode": row["source_mode"],
        "provider": row["provider"],
        "provider_model": row["provider_model"],
        "model_profile": row["model_profile"],
        "precision": row["precision_level"],
        "duration_ms": int(row["duration_ms"] or 0),
        "error_code": row["error_code"] or "",
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "saved_at": row["saved_at"],
        "stale": bool(stale_reason),
        "stale_reason": stale_reason,
        "section_count": len(result.get("sections") or []),
        "result": result,
        "image_ref": image_ref,
        "has_result": bool(result),
    }


def _flat_job(row) -> dict[str, Any]:
    """``POST /jobs`` 与 ``GET /jobs/<id>`` 使用的扁平结果（契约字段）。"""
    payload = _public_job(row)
    result = payload.pop("result")
    for field in RESULT_FIELDS:
        payload[field] = result.get(field, "" if field != "sections" else [])
    return payload


def get_job(job_id: int) -> dict[str, Any]:
    """读取任务状态与结果。

    顺带把「已经没人推进的在途任务」判死：服务重启后上一轮留下的
    ``pending/running`` 行不会有人再动，前端会一直轮询等不到终态。
    这里读到这种行就把它标成 ``failed/interrupted``，轮询才能收敛。
    """
    row = _job_row(job_id)
    if row["status"] in (STATUS_PENDING, STATUS_RUNNING) and not is_active(row["id"]):
        _LOGGER.info("reverse_job_orphan job_id=%s status=%s", row["id"], row["status"])
        _fail(int(row["id"]), "interrupted")
        row = _job_row(job_id)
    return _flat_job(row)


# ---------------------------------------------------------------- 创建任务


def create_job(
    *,
    image_id: str | None = None,
    upload_id: str | None = None,
    source_mode: str = SOURCE_MODE_AUTO,
    profile: str = "generic",
    precision: str = schema.DEFAULT_PRECISION,
    provider: str = "auto",
    options: dict | None = None,
    run_async: bool = False,
) -> dict[str, Any]:
    """创建并执行一次反推任务。

    Args:
        run_async: False（默认）在当前线程跑完再返回完整结果，供脚本与测试使用；
            True 时**立即返回**处于 ``pending`` 的任务，真正的工作交给后台线程，
            调用方通过 ``GET /jobs/<id>`` 轮询 ``stage``。

    Returns:
        同步模式返回完整结构化结果；异步模式返回 ``status="pending"`` 的任务视图
        （带 ``job_id``，供前端轮询）。

    Raises:
        AIBARError: 入参不合法（两种模式都在返回前校验，异步也不会带着坏参数起线程）。
            执行期错误只在**同步模式**抛出；异步模式一律落库为 ``failed``。
            失败时任务行保留（图片、设置与上次有效结果都不删除）。

    为什么要 ``run_async``：同步模式下客户端拿不到 ``job_id``（它和结果一起返回），
    自然无法轮询进度——界面只能靠假定时器。异步模式让 ``job_id`` 先落地，
    ``_set_stage`` 写库的真实阶段才读得到。
    """
    mode = _validate_source_mode(source_mode)
    profile = _validate_profile(profile)
    precision = _validate_precision(precision)
    requested = _validate_provider(provider)
    extra = dict(options or {})

    job_id = _insert_job(
        image_id=image_id,
        source_mode=mode,
        profile=profile,
        precision=precision,
        provider=requested,
    )

    if not run_async:
        _run_job(job_id, image_id, upload_id, mode, profile, precision, requested, extra)
        return _flat_job(_job_row(job_id))

    worker = threading.Thread(
        target=_run_job_async,
        args=(job_id, image_id, upload_id, mode, profile, precision, requested, extra),
        name=f"aibar-reverse-{job_id}",
        daemon=True,
    )
    # 先登记在途再起线程：反过来的话，poll 可能在线程真正开跑前读到
    # 「pending + 不在途」，把它误判成孤儿直接判死。
    with _WORKER_LOCK:
        _WORKERS[job_id] = worker
        _ACTIVE_JOBS.add(job_id)
    try:
        worker.start()
    except Exception as exc:  # 起不了线程（资源耗尽）就让任务当场失败，而不是留个僵尸
        _clear_active(job_id)
        _fail(job_id, "internal_error")
        raise AIBARError("internal_error", "无法启动反推任务，请稍后重试", 500) from exc
    return _flat_job(_job_row(job_id))


def _run_job(
    job_id: int,
    image_id: str | None,
    upload_id: str | None,
    mode: str,
    profile: str,
    precision: str,
    requested: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """执行一次反推：成功 / 失败 / 取消都写库，同步模式额外把异常抛给调用方。"""
    _mark_active(job_id)
    cancel_event = _register_cancel(job_id)
    started = time.perf_counter()
    try:
        ref = _read_image(job_id, image_id, upload_id)
        _set_stage(job_id, STAGE_METADATA, STATUS_RUNNING)
        raw = _analyze(ref, mode, requested, profile, precision, job_id, cancel_event, extra)

        _set_stage(job_id, STAGE_STRUCTURING)
        result = schema.validate_structured_result(raw)
        result["image_ref"] = schema.normalize_image_ref(
            {
                "image_id": ref.get("image_id"),
                "upload_id": ref.get("upload_id"),
                "content_hash": ref.get("content_hash") or "",
            }
        )
        if not schema.has_usable_content(result):
            raise AIBARError("empty_result", "未能获得可用的反推结果，可尝试降低精度或更换 Provider")

        _complete(job_id, result, str(result.get("provider") or ""), int((time.perf_counter() - started) * 1000))
        _LOGGER.info(
            "reverse_job_done job_id=%s provider=%s source_type=%s sections=%s duration_ms=%s",
            job_id,
            result.get("provider"),
            result.get("source_type"),
            len(result.get("sections") or []),
            result.get("duration_ms"),
        )
        return _flat_job(_job_row(job_id))
    except AIBARError as exc:
        if exc.code == "cancelled":
            _mark_cancelled(job_id)
        else:
            _fail(job_id, exc.code)
        _LOGGER.info("reverse_job_failed job_id=%s error_code=%s", job_id, exc.code)
        raise
    except Exception as exc:  # 未知异常不泄漏堆栈
        _fail(job_id, "internal_error")
        raise AIBARError("internal_error", "反推失败，请稍后重试", 500) from exc
    finally:
        _clear_cancel(job_id)
        _clear_active(job_id)


def _run_job_async(job_id: int, *args: Any) -> None:
    """后台线程入口：异常**绝不外抛**——没有调用栈接得住，只能落库为 failed。

    少了这层兜底会怎样：线程里抛出的异常只会打一行 traceback 到 stderr，
    任务行永远停在 ``running``，前端的轮询就再也等不到终态。
    """
    try:
        _run_job(job_id, *args)
    except AIBARError as exc:
        _LOGGER.info("reverse_async_job_failed job_id=%s error_code=%s", job_id, exc.code)
    except Exception as exc:
        _LOGGER.exception("reverse_async_job_crash job_id=%s type=%s", job_id, type(exc).__name__)
    finally:
        with _WORKER_LOCK:
            _WORKERS.pop(job_id, None)


def _read_image(job_id: int, image_id: str | None, upload_id: str | None) -> dict[str, Any]:
    _set_stage(job_id, STAGE_READING, STATUS_RUNNING)
    ref = uploads.resolve_ref(image_id=image_id, upload_id=upload_id)
    content_hash = _content_hash_of(ref)
    execute(
        "UPDATE prompt_reverse_jobs SET image_id=?, upload_ref=?, content_hash=? WHERE id=?",
        (
            ref.get("image_id") or None,
            uploads.upload_ref_for(ref),
            content_hash,
            job_id,
        ),
    )
    return ref


def _content_hash_of(ref: dict[str, Any]) -> str:
    """内容的稳定哈希，用于检测图片是否被替换（只存哈希，不存路径）。"""
    existing = str(ref.get("content_hash") or "")
    if _HEX64_RE.match(existing):
        return existing
    path = ref.get("path")
    if not path:
        return ""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _analyze(
    ref: dict[str, Any],
    source_mode: str,
    requested: str,
    profile: str,
    precision: str,
    job_id: int,
    cancel_event: threading.Event,
    options: dict[str, Any],
) -> dict[str, Any]:
    """按决策链选择 Provider 并执行分析。

    只对「当前 Provider 不可用」沿链降级；推理错误 / OOM / 超时一律**立即抛出**，
    由用户决定是否重试，绝不连续加载下一个大模型。
    """
    provider_options = dict(options)
    provider_options["job_id"] = job_id
    provider_options["cancel"] = cancel_event.is_set

    if requested != "auto":
        chain = [requested]
    elif source_mode == SOURCE_MODE_METADATA:
        chain = ["metadata"]
    else:
        chain = list(registry.auto_chain())
    if source_mode == SOURCE_MODE_VISION:
        chain = [key for key in chain if key != "metadata"]
    if not chain:
        # 与下面「链上全部失败」保持同一个 503：此前这处漏了 status，
        # 同一个 provider_unavailable 错误码一处 400 一处 503，前端没法统一处理。
        raise errors.unavailable(base.reason_text("not_configured"), "provider_unavailable")

    failures: list[dict[str, Any]] = []
    for key in chain:
        if cancel_event.is_set():
            raise AIBARError("cancelled", "任务已取消", 409)
        health = registry.provider_health(key)
        if not health.get("available"):
            failures.append(
                {
                    "provider": key,
                    "status": health.get("status"),
                    "reason_code": health.get("reason_code"),
                }
            )
            continue
        _set_stage(job_id, STAGE_METADATA if key == "metadata" else STAGE_VISION)
        provider = registry.get_provider(key)
        try:
            return provider.analyze_image(ref, profile, precision, provider_options)
        except AIBARError as exc:
            # 自动模式下「没有内嵌元数据」属于正常的不可用，继续沿链降级
            if exc.code == METADATA_NOT_FOUND and source_mode == SOURCE_MODE_AUTO:
                failures.append({"provider": key, "status": "unavailable", "reason_code": exc.code})
                continue
            raise

    last = failures[-1] if failures else {}
    reason_code = str(last.get("reason_code") or "not_configured")
    raise errors.unavailable(
        f"没有可用的视觉 Provider：{base.reason_text(reason_code)}",
        "provider_unavailable",
    )


# ---------------------------------------------------------------- 取消


def cancel_job(job_id: int) -> dict[str, Any]:
    """取消未完成任务；已完成任务幂等返回，绝不删除上次成功结果。"""
    row = _job_row(job_id)
    if row["status"] in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
        return _flat_job(row)
    request_cancel(job_id)
    _mark_cancelled(job_id)
    return _flat_job(_job_row(job_id))


# ---------------------------------------------------------------- 编辑结果


def update_result(job_id: int, sections: Any, *, profile: str | None = None) -> dict[str, Any]:
    """保存用户编辑后的片段（严格校验，坏片段直接报错而不是静默丢弃）。"""
    row = _job_row(job_id)
    structured = json_field(row["structured_result"], {}) or {}
    if not isinstance(structured, dict):
        structured = {}

    target_profile = _validate_profile(profile or row["model_profile"] or "generic")
    merged = dict(structured)
    merged["sections"] = sections
    merged["model_profile"] = target_profile
    merged["formatted_positive"] = ""
    merged["formatted_negative"] = ""
    validated = schema.validate_structured_result(merged, strict_sections=True)

    positive, negative = schema.format_result(validated["sections"], target_profile)
    validated["formatted_positive"] = positive
    validated["formatted_negative"] = negative

    execute(
        "UPDATE prompt_reverse_jobs SET edited_result=?, model_profile=? WHERE id=?",
        (dumps(validated), target_profile, job_id),
    )
    return _flat_job(_job_row(job_id))


def _current_result(row) -> dict[str, Any]:
    edited = json_field(row["edited_result"], {}) or {}
    if isinstance(edited, dict) and edited:
        return edited
    structured = json_field(row["structured_result"], {}) or {}
    return structured if isinstance(structured, dict) else {}


# ---------------------------------------------------------------- 入库流水线


def _prepare_segments(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``用户编辑后文本 → 按维度拆分 → 过滤 → 规范化 → 去重 → 近似重复 → 评分``。

    **绝不**把 ``formatted_positive``、整段恢复提示词或完整视觉描述作为单条词条：
    每条文本都先按标点拆分，拆不开的长文本直接丢弃。

    Returns:
        ``(segments, discarded)``，``discarded`` 元素为 ``{"text", "reason"}``。
    """
    profile = str(result.get("model_profile") or "generic")
    sections = result.get("sections") or []
    discarded: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    seen_fingerprints: dict[str, int] = {}

    for section in sections:
        if not isinstance(section, dict):
            continue
        if not section.get("selected_for_library", True):
            continue
        text = schema.clean_text(section.get("text"), schema.MAX_PROMPT_TEXT)
        if not text:
            continue
        dimension = schema.coerce_dimension(section.get("dimension")) or ""
        confidence = float(section.get("confidence") or 0.0)
        uncertainty = str(section.get("uncertainty") or "")

        pieces = split_segments(text)
        if not pieces:
            discarded.append({"text": text[:60], "reason": "unsplit_text"})
            continue
        for piece in pieces:
            piece = piece.strip()
            if len(piece) > MAX_SEGMENT_LENGTH:
                discarded.append({"text": piece[:60], "reason": "unsplit_text"})
                continue

            if is_stop_phrase(piece) or is_quality_slogan(piece) or is_run_parameter(piece):
                discarded.append({"text": piece, "reason": "noise"})
                continue
            if not has_content_value(piece):
                discarded.append({"text": piece, "reason": "no_content_value"})
                continue
            flags = risk_flags(piece)
            if flags:
                discarded.append({"text": piece, "reason": f"risk:{','.join(flags)}"})
                continue
            if uncertainty and confidence < UNCERTAIN_MIN_CONFIDENCE:
                discarded.append({"text": piece, "reason": "low_confidence_uncertain"})
                continue

            resolved_dimension = dimension
            resolved_confidence = confidence
            if not resolved_dimension:
                resolved_dimension, auto_confidence = classify_dimension(piece, schema.MEDIA_TYPE)
                resolved_confidence = min(confidence, float(auto_confidence))

            fingerprint = content_fingerprint(schema.MEDIA_TYPE, resolved_dimension, piece)
            if fingerprint in seen_fingerprints:
                discarded.append({"text": piece, "reason": "duplicate_in_batch"})
                continue

            score = quality_score(piece, resolved_dimension)
            if score < MIN_SAVE_QUALITY:
                discarded.append({"text": piece, "reason": "low_quality"})
                continue

            near_index = _near_duplicate_index(kept, piece)
            if near_index is not None:
                # 近似重复：只保留质量分更高的一条，另一条丢弃而不是重复入库
                if score > int(kept[near_index]["quality"]):
                    seen_fingerprints.pop(kept[near_index]["fingerprint"], None)
                    discarded.append({"text": kept[near_index]["text"], "reason": "near_duplicate"})
                    kept[near_index] = _make_segment(
                        piece,
                        resolved_dimension,
                        resolved_confidence,
                        score,
                        profile,
                        section,
                        fingerprint,
                    )
                    seen_fingerprints[fingerprint] = near_index
                else:
                    discarded.append({"text": piece, "reason": "near_duplicate"})
                continue

            seen_fingerprints[fingerprint] = len(kept)
            kept.append(
                _make_segment(
                    piece,
                    resolved_dimension,
                    resolved_confidence,
                    score,
                    profile,
                    section,
                    fingerprint,
                )
            )
    return kept, discarded


def _make_segment(
    piece: str,
    dimension: str,
    confidence: float,
    score: int,
    profile: str,
    section: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "text": piece,
        "dimension": dimension,
        "subcategory": str(section.get("subcategory") or ""),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "model_profiles": [profile] if profile in profile_keys() else ["generic"],
        "tags": [],
        "quality": score,
        "fingerprint": fingerprint,
    }


def _near_duplicate_index(kept: list[dict[str, Any]], piece: str) -> int | None:
    for index, item in enumerate(kept):
        if is_near_duplicate(piece, str(item.get("text") or "")):
            return index
    return None


def save_job_result(job_id: int, thresholds: dict | None = None) -> dict[str, Any]:
    """把当前结果入库（只有用户点保存才调用）。

    Returns:
        ``{"inserted", "merged", "candidates", "discarded", "entry_ids", "saved", "discarded_reasons"}``，
        重复调用不会重复入库（命中既有词条计入 ``merged``）。
    """
    row = _job_row(job_id)
    result = _current_result(row)
    if not result:
        raise errors.not_found("该任务还没有可保存的结果")

    segments, discarded = _prepare_segments(result)
    source_type = (
        "reverse_metadata" if result.get("source_type") == schema.SOURCE_METADATA else "reverse_vision"
    )
    image_ref = result.get("image_ref") if isinstance(result.get("image_ref"), dict) else {}
    source_ref = {
        "job_id": int(job_id),
        "provider": str(result.get("provider") or ""),
        "image_id": image_ref.get("image_id"),
        "upload_id": image_ref.get("upload_id"),
    }

    ingested = _ingest(segments, schema.MEDIA_TYPE, source_type, source_ref, thresholds)
    entry_ids = [int(i) for i in ingested.get("inserted", [])] + [
        int(i) for i in ingested.get("merged", [])
    ]
    discarded_total = list(discarded) + [
        {"text": str(item.get("text") or "")[:60], "reason": str(item.get("reason") or "")}
        for item in ingested.get("discarded", [])
    ]
    execute("UPDATE prompt_reverse_jobs SET saved_at=? WHERE id=?", (db_now(), job_id))
    _LOGGER.info(
        "reverse_job_saved job_id=%s inserted=%s merged=%s candidates=%s discarded=%s",
        job_id,
        len(ingested.get("inserted", [])),
        len(ingested.get("merged", [])),
        len(ingested.get("candidates", [])),
        len(discarded_total),
    )
    return {
        "inserted": len(ingested.get("inserted", [])),
        "merged": len(ingested.get("merged", [])),
        "candidates": len(ingested.get("candidates", [])),
        "discarded": len(discarded_total),
        "entry_ids": entry_ids,
        "saved": True,
        "discarded_reasons": discarded_total,
    }


# ---------------------------------------------------------------- 历史


def list_history(limit: Any = None, source_type: Any = None) -> dict[str, Any]:
    """反推历史（只返回元信息，不返回提示词正文）。"""
    limit_value = _validate_limit(limit)
    clauses: list[str] = []
    params: list[Any] = []
    source = str(source_type or "").strip()
    if source:
        if source not in schema.SOURCE_TYPES:
            raise AIBARError("invalid_input", "未知的结果来源类型")
        clauses.append("source_type = ?")
        params.append(source)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = query_all(
        f"SELECT * FROM prompt_reverse_jobs WHERE {where} ORDER BY id DESC LIMIT ?",
        params + [limit_value],
    )
    # 过期判定一次算完：否则每行各 1~2 次查询，100 条历史就是 201 次 SQL
    stale_reasons = _stale_reasons(rows)
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _public_job(row, stale_reasons.get(int(row["id"]), ""))
        payload.pop("result")
        reference = payload.get("image_ref") or {}
        upload_id = reference.get("upload_id")
        payload["preview_url"] = uploads.preview_url(upload_id) if upload_id else ""
        items.append(payload)
    return {"items": items, "total": len(items), "limit": limit_value}


def delete_history(job_id: int) -> dict[str, Any]:
    """删除一条反推历史，并清理不再被引用的上传缓存。

    已入库词条**不级联删除**，只移除其指向该任务的来源引用。
    """
    row = _job_row(job_id)
    execute("DELETE FROM prompt_reverse_jobs WHERE id=?", (int(job_id),))
    detached = _detach_source_refs(int(job_id))
    # 缓存清理是尽力而为：referenced_upload_ids() 查不到时会抛（故意的，避免误删），
    # 但不能因此让"删除历史"整笔失败——记录已经删掉了，只是缓存留到下次清理。
    try:
        cleanup = uploads.cleanup_unreferenced()
    except Exception as exc:
        _LOGGER.warning(
            "reverse_history_cleanup_failed job_id=%s error_type=%s", job_id, type(exc).__name__
        )
        cleanup = {"removed": 0, "kept_referenced": 0, "failed": 1}
    _LOGGER.info(
        "reverse_history_deleted job_id=%s detached_refs=%s removed_uploads=%s",
        job_id,
        detached,
        cleanup.get("removed", 0),
    )
    return {
        "deleted": True,
        "job_id": int(job_id),
        "detached_source_refs": detached,
        "upload_cleanup": cleanup,
    }


def _detach_source_refs(job_id: int) -> int:
    """移除词条与候选中指向该任务的来源引用（不删除词条本身）。

    ``prompt_entries`` 带有 ``source_type`` 列，可用其先行过滤；``prompt_candidates``
    按 PRD M7.6 无 ``source_type`` 列，只能全表扫描后按 ``source_ref`` JSON 里的
    ``job_id`` 过滤。两张表不可共用同一条带 ``source_type`` 的 WHERE。
    """
    detached = 0
    for table, columns, where in (
        (
            "prompt_entries",
            ("id", "source_ref"),
            "WHERE source_type IN ('reverse_metadata', 'reverse_vision')",
        ),
        ("prompt_candidates", ("id", "source_ref"), ""),
    ):
        rows = query_all(
            f"SELECT {columns[0]} AS id, {columns[1]} AS source_ref FROM {table} {where}"
        )
        for row in rows:
            reference = json_field(row["source_ref"], {}) or {}
            if not isinstance(reference, dict) or reference.get("job_id") != job_id:
                continue
            reference.pop("job_id", None)
            execute(
                f"UPDATE {table} SET source_ref=? WHERE id=?",
                (dumps(reference) if reference else None, int(row["id"])),
            )
            detached += 1
    return detached


def cleanup_uploads(ttl_hours: int | None = None) -> dict[str, int]:
    """清理过期的上传缓存（被未删除历史引用的文件保留）。"""
    return uploads.cleanup_expired(ttl_hours)


# ---------------------------------------------------------------- 目标模式


def target_modes() -> dict[str, Any]:
    """目标模型档案、精度与维度选项（供前端下拉与分组展示）。"""
    return {
        "profiles": [
            {
                "key": profile.get("key"),
                "label": profile.get("label"),
                "description": profile.get("description", ""),
                "supports_negative": bool(profile.get("supports_negative")),
                "supports_weight": bool(profile.get("supports_weight")),
            }
            for profile in (get_profile(key) for key in profile_keys())
        ],
        "precisions": [
            {"key": key, "label": base.PRECISION_LABELS.get(key, key)} for key in schema.PRECISIONS
        ],
        "source_modes": [
            {"key": key, "label": SOURCE_MODE_LABELS[key]} for key in SOURCE_MODES
        ],
        "dimensions": [
            {"key": key, "label": schema.dimension_label_of(key)}
            for key in schema.image_dimension_keys()
        ],
        "media_type": schema.MEDIA_TYPE,
    }


__all__ = [
    "JOB_STATUSES",
    "SOURCE_MODES",
    "STAGE_LABELS",
    "STAGE_ORDER",
    "cancel_job",
    "cleanup_uploads",
    "create_job",
    "delete_history",
    "get_job",
    "is_active",
    "list_history",
    "request_cancel",
    "save_job_result",
    "target_modes",
    "update_result",
]
