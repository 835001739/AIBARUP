"""M9 / M10 / M11 HTTP 路由层。

约定（HARNESS / docs/API_CONTRACT.md）：
- 统一 envelope：``core.responses.ok / fail``；
- 业务异常 ``AIBARError`` 与未知异常都在这里转成统一失败响应，不泄漏堆栈；
- 入参一律校验：枚举、ID 归属、文件上限、字段长度；
- **绝不接受任意本机路径**：图片只能通过 ``image_id``（图库记录）或 ``upload_id``（受控缓存）引用；
- Provider 列表与诊断响应不返回密钥与完整 endpoint。
"""

from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, request, send_file
from werkzeug.exceptions import HTTPException

from core import errors
from core.errors import AIBARError
from core.logging_setup import get_logger
from core.responses import fail, ok

from . import schema, service, uploads
from .providers import registry

_LOGGER = get_logger("aibar.reverse.routes")

bp = Blueprint("reverse", __name__, url_prefix="/api")

MAX_KEY_LENGTH = 64
MAX_ID_LENGTH = 200
MAX_SECTIONS_INPUT = schema.MAX_SECTIONS
_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.:]{1,200}$")


# ---------------------------------------------------------------- 入参工具


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {}


def _str_field(payload: dict[str, Any], key: str, max_len: int = MAX_KEY_LENGTH) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIBARError("invalid_input", f"{key} 必须是字符串")
    text = value.strip()
    if len(text) > max_len:
        raise AIBARError("invalid_input", f"{key} 超出长度上限")
    return text or None


def _id_field(payload: dict[str, Any], key: str) -> str | None:
    value = _str_field(payload, key, MAX_ID_LENGTH)
    if value is None:
        return None
    if not _ID_RE.match(value):
        raise AIBARError("invalid_input", f"{key} 含有不允许的字符")
    return value


def _bool_field(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise AIBARError("invalid_input", f"{key} 必须是布尔值")


def _sections_field(payload: dict[str, Any]) -> list[Any]:
    value = payload.get("sections")
    if not isinstance(value, list):
        raise AIBARError("invalid_input", "sections 必须是数组")
    if len(value) > MAX_SECTIONS_INPUT:
        raise AIBARError("invalid_input", f"片段数量超过上限 {MAX_SECTIONS_INPUT}")
    return value


def _validate_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LENGTH or not _ID_RE.match(key):
        raise AIBARError("invalid_input", "Provider 标识不合法")
    return key


# ---------------------------------------------------------------- M9.2 上传

@bp.route("/prompt-reverse/uploads", methods=["POST"])
def upload_image():
    if "file" not in request.files:
        raise AIBARError("empty_file", "请选择要上传的图片")
    storage = request.files["file"]
    filename = storage.filename or ""
    if not filename.strip():
        raise AIBARError("empty_file", "上传文件为空，请重新选择图片")
    payload = uploads.save_upload(storage, filename)
    return ok(payload)


@bp.route("/prompt-reverse/uploads/<upload_id>/preview", methods=["GET"])
def upload_preview(upload_id: str):
    """受控缓存的图片预览（只接受 uploads 目录内的哈希文件名）。"""
    path = uploads.resolve_path(upload_id)
    if path is None:
        raise errors.not_found("预览图片不存在或已过期")
    return send_file(str(path))


# ---------------------------------------------------------------- M9.3 任务


@bp.route("/prompt-reverse/jobs", methods=["POST"])
def create_job():
    payload = _body()
    image_id = _id_field(payload, "image_id")
    upload_id = _id_field(payload, "upload_id")
    if not image_id and not upload_id:
        raise AIBARError("invalid_input", "需要提供 image_id 或 upload_id")
    if image_id and upload_id:
        raise AIBARError("invalid_input", "image_id 与 upload_id 只能选择其一")

    options = payload.get("options")
    if options is not None and not isinstance(options, dict):
        raise AIBARError("invalid_input", "options 必须是对象")

    # async=true：立即返回 pending 任务（202），由前端轮询 GET /jobs/<id> 拿真实阶段。
    # 不传或 false 时保持首期的同步语义（200 + 完整结果），脚本与既有客户端不受影响。
    run_async = _bool_field(payload, "async", False)

    result = service.create_job(
        image_id=image_id,
        upload_id=upload_id,
        source_mode=payload.get("source_mode", service.SOURCE_MODE_AUTO),
        profile=payload.get("profile", "generic"),
        precision=payload.get("precision", schema.DEFAULT_PRECISION),
        provider=payload.get("provider", "auto"),
        options=options if isinstance(options, dict) else None,
        run_async=run_async,
    )
    pending = run_async and result.get("status") == service.STATUS_PENDING
    return ok(result, 202 if pending else 200)


@bp.route("/prompt-reverse/jobs/<int:job_id>", methods=["GET"])
def get_job(job_id: int):
    return ok(service.get_job(job_id))


@bp.route("/prompt-reverse/jobs/<int:job_id>/cancel", methods=["POST"])
def cancel_job(job_id: int):
    return ok(service.cancel_job(job_id))


@bp.route("/prompt-reverse/jobs/<int:job_id>/result", methods=["PATCH"])
def patch_result(job_id: int):
    payload = _body()
    sections = _sections_field(payload)
    profile = _str_field(payload, "profile")
    return ok(service.update_result(job_id, sections, profile=profile))


@bp.route("/prompt-reverse/jobs/<int:job_id>/save", methods=["POST"])
def save_result(job_id: int):
    return ok(service.save_job_result(job_id))


# ---------------------------------------------------------------- M9.7 历史


@bp.route("/prompt-reverse/history", methods=["GET"])
def list_history():
    source_type = request.args.get("source_type") or None
    if source_type is not None and source_type not in schema.SOURCE_TYPES:
        raise AIBARError("invalid_input", "未知的结果来源类型")
    return ok(service.list_history(request.args.get("limit"), source_type))


@bp.route("/prompt-reverse/history/<int:job_id>", methods=["DELETE"])
def delete_history(job_id: int):
    return ok(service.delete_history(job_id))


# ---------------------------------------------------------------- M10 / M11 Provider 中心


@bp.route("/prompt-reverse/providers", methods=["GET"])
def list_providers():
    force = request.args.get("force") in ("1", "true", "True")
    return ok(registry.list_providers(force=force))


@bp.route("/prompt-reverse/providers/refresh", methods=["POST"])
def refresh_providers():
    return ok(registry.refresh())


@bp.route("/prompt-reverse/providers/default", methods=["PATCH"])
def set_default_provider():
    payload = _body()
    key = _str_field(payload, "provider_key")
    if key is None:
        raise AIBARError("invalid_input", "需要提供 provider_key")
    if key != registry.AUTO_KEY:
        _validate_key(key)
    return ok(registry.set_default(key))


@bp.route("/prompt-reverse/providers/<key>/test", methods=["POST"])
def test_provider(key: str):
    _validate_key(key)
    return ok(registry.test_provider(key))


@bp.route("/prompt-reverse/providers/<key>/consent", methods=["POST"])
def set_provider_consent(key: str):
    _validate_key(key)
    payload = _body()
    consented = _bool_field(payload, "consented", True)
    return ok(registry.set_consent(key, consented))


@bp.route("/prompt-reverse/providers/<key>/consent", methods=["GET"])
def get_provider_consent(key: str):
    _validate_key(key)
    return ok(registry.consent_status(key))


# ---------------------------------------------------------------- 目标模式


@bp.route("/prompt-reverse/target-modes", methods=["GET"])
def target_modes():
    return ok(service.target_modes())


# ---------------------------------------------------------------- 错误处理


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    return fail(exc.code, exc.message, exc.status)


@bp.errorhandler(HTTPException)
def _handle_http_error(exc: HTTPException):
    code = str(getattr(exc, "name", "error") or "error").lower().replace(" ", "_")
    return fail(code, str(getattr(exc, "description", "") or "请求无法处理"), int(exc.code or 500))


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    # 只记录异常类型，不记录请求正文（提示词正文与图片内容一律不进日志）
    _LOGGER.exception("reverse_route_unhandled type=%s", type(exc).__name__)
    return fail("internal_error", "服务内部错误，请稍后重试", 500)


__all__ = ["bp"]
