"""AI 视频创作工作台 REST 接口（``/api/ai-video``）。"""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import Path

from flask import Blueprint, request, send_from_directory
from PIL import Image

from config import Config
from core import db
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from core.responses import fail, ok

from . import generation, service

bp = Blueprint("ai_video", __name__, url_prefix="/api/ai-video")
_logger = get_logger("aibar.ai_video.routes")


def _body() -> dict:
    """读取 JSON 对象；空体与非对象均作为可读 400 返回。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象", 400)
    return data


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    return fail(exc.code, exc.message, exc.status)


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    safe_log(_logger, logging.ERROR, "ai_video_unhandled", error_code=type(exc).__name__)
    return fail("internal_error", "服务内部错误，请稍后重试", 500)


@bp.get("/meta")
def meta():
    return ok(service.metadata())


@bp.get("/projects")
def projects_list():
    return ok({"items": service.list_projects()})


@bp.post("/projects")
def project_create():
    return ok(service.create_project(_body()))


@bp.get("/projects/<int:project_id>")
def project_get(project_id: int):
    return ok(service.get_project(project_id))


@bp.patch("/projects/<int:project_id>")
def project_update(project_id: int):
    return ok(service.update_project(project_id, _body()))


@bp.delete("/projects/<int:project_id>")
def project_delete(project_id: int):
    service.delete_project(project_id)
    return ok({"deleted": True, "id": project_id})


@bp.get("/projects/<int:project_id>/dashboard")
def project_dashboard(project_id: int):
    return ok(service.project_dashboard(project_id))


@bp.post("/projects/<int:project_id>/seed-template")
def project_seed_template(project_id: int):
    return ok(service.seed_realistic_drama_template(project_id))


@bp.post("/projects/<int:project_id>/scenes")
def scene_create(project_id: int):
    return ok(service.create_scene(project_id, _body()))


@bp.post("/scenes/<int:scene_id>/shots")
def shot_create(scene_id: int):
    return ok(service.create_shot(scene_id, _body()))


@bp.get("/shots/<int:shot_id>")
def shot_get(shot_id: int):
    return ok(service.get_shot(shot_id))


@bp.patch("/shots/<int:shot_id>")
def shot_update(shot_id: int):
    return ok(service.update_shot(shot_id, _body()))


@bp.post("/shots/<int:shot_id>/frames/<kind>/generate")
def shot_frame_generate(shot_id: int, kind: str):
    return ok(generation.generate_shot_frame(shot_id, _frame_kind(kind), _body()))


@bp.post("/projects/<int:project_id>/assets")
def asset_create(project_id: int):
    return ok(service.create_asset(project_id, _body()))


@bp.patch("/assets/<int:asset_id>")
def asset_update(asset_id: int):
    return ok(service.update_asset(asset_id, _body()))


@bp.delete("/assets/<int:asset_id>")
def asset_delete(asset_id: int):
    service.delete_asset(asset_id)
    return ok({"deleted": True, "id": asset_id})


@bp.get("/workflows")
def workflows_list():
    """可选出图工作流清单（名称/文件名/节点数），供项目与资产选择。"""
    return ok(service.list_workflows())


@bp.post("/assets/<int:asset_id>/generate")
def asset_generate(asset_id: int):
    """为角色/场景资产生成或重新生成图片；后台执行，立即返回 generating。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    return ok(generation.generate_asset(asset_id, body))


@bp.post("/assets/<int:asset_id>/generate-set")
def asset_generate_set(asset_id: int):
    """按多角度/表情一次生成一组角色图。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    return ok(generation.generate_asset_set(asset_id, body))


@bp.get("/assets/<int:asset_id>/images")
def asset_images(asset_id: int):
    return ok(service.list_asset_images(asset_id))


@bp.post("/assets/<int:asset_id>/images/<int:image_id>/cover")
def asset_image_cover(asset_id: int, image_id: int):
    return ok(service.set_asset_cover(asset_id, image_id))


@bp.delete("/assets/<int:asset_id>/images/<int:image_id>")
def asset_image_delete(asset_id: int, image_id: int):
    service.delete_asset_image(asset_id, image_id)
    return ok({"deleted": True, "id": image_id})


@bp.get("/assets/<int:asset_id>/images/<int:image_id>/image")
def asset_variant_image(asset_id: int, image_id: int):
    row = db.query_one(
        "SELECT image_path FROM ai_video_asset_images WHERE id=? AND asset_id=?",
        (int(image_id), int(asset_id)),
    )
    if row is None:
        raise AIBARError("not_found", "资产图片不存在", 404)
    return _serve_data_image(row["image_path"])


@bp.get("/asset-images/<int:image_id>/image")
def asset_image_by_id(image_id: int):
    row = service.get_asset_image(image_id)
    return _serve_data_image(str(row.get("image_path") or ""))


def _frame_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value in ("start", "first", "first_frame"):
        return "start"
    if value in ("end", "last", "last_frame"):
        return "end"
    raise AIBARError("invalid_input", "帧类型必须是 start 或 end")


@bp.post("/shots/<int:shot_id>/frames/<kind>/asset")
def shot_frame_asset(shot_id: int, kind: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    image_id = body.get("image_id")
    if image_id in (None, ""):
        raise AIBARError("invalid_input", "请选择一张图片")
    try:
        image_id_int = int(image_id)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", "image_id 必须是整数")
    return ok(service.attach_shot_frame(shot_id, _frame_kind(kind), image_id_int))


@bp.post("/shots/<int:shot_id>/frames/<kind>/upload")
def shot_frame_upload(shot_id: int, kind: str):
    kind_value = _frame_kind(kind)
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        raise AIBARError("invalid_input", "请选择要上传的图片")
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise AIBARError("invalid_input", "只支持 PNG / JPG / WEBP 图片")
    limit = max(1, int(Config.AI_VIDEO_ASSET_MAX_MB or 32)) * 1024 * 1024
    data = upload.read(limit + 1)
    if len(data) > limit:
        raise AIBARError("invalid_input", "图片超过大小上限")
    try:
        Image.open(BytesIO(data)).verify()
    except Exception:
        raise AIBARError("invalid_input", "文件不是有效图片")
    shot = service.get_shot(shot_id)
    digest = hashlib.sha256(data).hexdigest()[:16]
    root = Path(Config.DATA_DIR) / "ai_video_frames" / str(int(shot_id))
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("%s_%s%s" % (kind_value, digest, suffix))
    target.write_bytes(data)
    ref = "frame:%d:%s" % (int(shot_id), target.name)
    return ok(service.set_shot_frame_ref(shot_id, kind_value, ref))


@bp.delete("/shots/<int:shot_id>/frames/<kind>")
def shot_frame_clear(shot_id: int, kind: str):
    return ok(service.clear_shot_frame(shot_id, _frame_kind(kind)))


@bp.get("/shots/<int:shot_id>/frames/<path:filename>")
def shot_frame_file(shot_id: int, filename: str):
    shot = service.get_shot(shot_id)
    ref = "frame:%d:%s" % (int(shot_id), filename)
    if ref not in (shot.get("first_frame"), shot.get("last_frame")):
        raise AIBARError("not_found", "首尾帧图片不存在", 404)
    root = Path(Config.DATA_DIR).resolve()
    target = (root / "ai_video_frames" / str(int(shot_id)) / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise AIBARError("not_found", "图片引用无效", 404)
    if not target.is_file():
        raise AIBARError("not_found", "图片文件不存在", 404)
    suffix = target.suffix.lower()
    mimetype = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
    return send_from_directory(str(target.parent), target.name, mimetype=mimetype)


def _serve_data_image(rel: str):
    """只允许 data/ 内的受控相对路径，统一处理图片响应。"""
    if not rel:
        raise AIBARError("not_found", "图片不存在", 404)
    root = Path(Config.DATA_DIR).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise AIBARError("not_found", "图片引用无效", 404)
    if not target.is_file():
        raise AIBARError("not_found", "图片文件不存在", 404)
    suffix = target.suffix.lower()
    mimetype = "image/png" if suffix == ".png" else ("image/webp" if suffix == ".webp" else "image/jpeg")
    return send_from_directory(str(target.parent), target.name, mimetype=mimetype)


@bp.get("/assets/<int:asset_id>/image")
def asset_image(asset_id: int):
    """读取资产当前封面图；只允许 data/ 内的受控相对路径。"""
    asset = service.get_asset(asset_id)
    return _serve_data_image(str(asset.get("image_path") or ""))
