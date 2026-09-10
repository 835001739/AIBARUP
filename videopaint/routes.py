"""M16 · 视频转绘 REST 接口（url_prefix=/api/videopaint）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | ``/api/videopaint/clips`` | 源视频列表（复用视频模块，挑一个已抽帧的视频） |
| GET | ``/api/videopaint/jobs`` | 任务列表 |
| POST | ``/api/videopaint/jobs`` | 新建任务（body 带 clip_id / prompt / reference_path 等） |
| GET | ``/api/videopaint/jobs/<id>`` | 任务详情 |
| PUT | ``/api/videopaint/jobs/<id>`` | 改配置（仅空闲态可改） |
| DELETE | ``/api/videopaint/jobs/<id>`` | 删除任务（连带组图） |
| POST | ``/api/videopaint/jobs/<id>/prepare`` | 抽帧 + 建组图 + 登记帧 |
| POST | ``/api/videopaint/jobs/<id>/pose`` | 逐帧提骨架（动作拆解） |
| POST | ``/api/videopaint/jobs/<id>/generate`` | 逐帧重绘（参考图锁脸 + 骨架控姿） |
| POST | ``/api/videopaint/jobs/<id>/nobg`` | 去背景序列帧 |
| POST | ``/api/videopaint/jobs/<id>/sheet`` | 拼组图 contact sheet |
| GET | ``/api/videopaint/jobs/<id>/frames`` | 逐帧清单（含各阶段产物 URL） |
| GET | ``/api/videopaint/jobs/<id>/sheet/<variant>`` | 组图大图 |
| GET | ``/api/videopaint/jobs/<id>/file/<kind>/<order>`` | 单帧产物（pose/image/nobg） |
| GET | ``/api/videopaint/jobs/<id>/frames/<order>/workflow-graph`` | 该帧姿态工作流的 UI 格式 JSON（供 ComfyUI 深链接） |
| GET | ``/api/videopaint/jobs/<id>/frames/<order>/editor-link`` | 打开 ComfyUI 并载入该帧工作流的深链接 |
| GET | ``/api/videopaint/jobs/<id>/frames/<order>/flux2-workflow-graph`` | 该帧 FLUX.2 Klein 工作流的 UI 格式 JSON（M17） |
| GET | ``/api/videopaint/jobs/<id>/frames/<order>/flux2-editor-link`` | 打开 ComfyUI 并载入该帧 FLUX.2 工作流的深链接（M17） |
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

from flask import Blueprint, request, send_file, Response

from config import Config
from core.errors import AIBARError
from core.responses import ok

from . import service

bp = Blueprint("videopaint", __name__, url_prefix="/api/videopaint")


@bp.post("/reference-upload")
def reference_upload():
    """前端选了本地参考图文件时，先上传到服务端一份，再把绝对路径回传作为 reference_path。"""
    f = request.files.get("file")
    if not f:
        raise AIBARError("invalid_input", "请选择参考图文件")
    ref_dir = Path(Config.DATA_DIR) / service.SUBDIR / "_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(f.filename or "x.png").suffix or ".png"
    dst = ref_dir / ("ref_%d%s" % (int(time.time() * 1000), ext))
    f.save(str(dst))
    return ok({"path": str(dst)})


def _json_body() -> dict:
    if not request.is_json:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    return data


@bp.get("/clips")
def list_clips():
    from video import service as video_service
    res = video_service.list_clips(keyword=request.args.get("keyword", ""), limit=100, offset=0)
    return ok({"items": res.get("items") if isinstance(res, dict) else res})


@bp.get("/jobs")
def list_jobs():
    from . import service as svc
    return ok({"items": svc.list_jobs()})


@bp.post("/jobs")
def create_job():
    job = service.create_job(_json_body())
    return ok({"job": job})


@bp.get("/jobs/<int:job_id>")
def get_job(job_id: int):
    job = service.get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    return ok({"job": job})


@bp.put("/jobs/<int:job_id>")
def update_job(job_id: int):
    job = service.update_job(job_id, _json_body())
    return ok({"job": job})


@bp.delete("/jobs/<int:job_id>")
def delete_job(job_id: int):
    res = service.delete_job(job_id)
    return ok(res)


@bp.post("/jobs/<int:job_id>/prepare")
def prepare(job_id: int):
    body = _json_body()
    job = service.prepare(
        job_id,
        fps=body.get("fps"), max_frames=body.get("max_frames"),
        scale_width=body.get("scale_width"), start_sec=body.get("start_sec"), end_sec=body.get("end_sec"),
    )
    return ok({"job": job})


@bp.post("/jobs/<int:job_id>/pose")
def pose(job_id: int):
    job = service.pose(job_id)
    return ok({"job": job})


@bp.post("/jobs/<int:job_id>/generate")
def generate(job_id: int):
    body = _json_body()
    job = service.generate(job_id, force=bool(body.get("force", False)))
    return ok({"job": job})


@bp.post("/jobs/<int:job_id>/nobg")
def nobg(job_id: int):
    body = _json_body()
    job = service.build_nobg(
        job_id,
        mode=body.get("mode", "flood"),
        seed=body.get("seed", "0,0"),
        similarity=float(body.get("similarity", 0.4)),
        blend=float(body.get("blend", 0.0)),
        color=body.get("color", ""),
    )
    return ok({"job": job})


@bp.post("/jobs/<int:job_id>/sheet")
def sheet(job_id: int):
    body = _json_body()
    res = service.build_sheet(
        job_id,
        cols=int(body.get("cols", 4)),
        padding=int(body.get("padding", 4)),
        variant=body.get("variant", "original"),
    )
    return ok(res)


@bp.get("/jobs/<int:job_id>/frames")
def frames(job_id: int):
    return ok({"items": service.list_frames(job_id)})


@bp.get("/jobs/<int:job_id>/sheet/<variant>")
def sheet_file(job_id: int, variant: str):
    from core import db
    row = db.query_one("SELECT id FROM video_paint_jobs WHERE id=?", (job_id,))
    if not row:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    data = service.read_job_file(job_id, "sheet", 1 if variant == "nobg" else 0)
    if not data:
        raise AIBARError("not_found", "还没有组图，请先生成并拼图")
    buf, mime = data
    return send_file(io.BytesIO(buf), mimetype=mime)


@bp.get("/jobs/<int:job_id>/file/<kind>/<int:order_idx>")
def file(job_id: int, kind: str, order_idx: int):
    data = service.read_job_file(job_id, kind, order_idx)
    if not data:
        raise AIBARError("not_found", "文件不存在：%s/%s" % (kind, order_idx))
    buf, mime = data
    return send_file(io.BytesIO(buf), mimetype=mime)


@bp.get("/jobs/<int:job_id>/frames/<int:order_idx>/workflow-graph")
def frame_workflow_graph(job_id: int, order_idx: int):
    """返回本帧姿态重绘的 **UI 格式** 工作流 JSON（供 ComfyUI 编辑器深链接直接 fetch）。

    不带 CORS 之外的特殊头；强制 ``no-store`` 以免浏览器缓存过期的工作流。
    """
    info = service.build_frame_pose_ui_graph(job_id, order_idx)
    return Response(
        json.dumps(info["ui"], ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@bp.get("/jobs/<int:job_id>/frames/<int:order_idx>/editor-link")
def frame_editor_link(job_id: int, order_idx: int):
    """返回打开 ComfyUI 并载入本帧姿态工作流的深链接 dict。"""
    return ok(service.frame_editor_link(job_id, order_idx))


@bp.get("/jobs/<int:job_id>/frames/<int:order_idx>/flux2-workflow-graph")
def frame_flux2_workflow_graph(job_id: int, order_idx: int):
    """返回本帧 **FLUX.2 Klein** 结构控制工作流的 UI 格式 JSON（M17）。

    与 ``workflow-graph`` 并列：同一帧可任选 SDXL 姿态链路或 Klein 结构控制链路打开。
    """
    info = service.build_frame_flux2_ui_graph(job_id, order_idx)
    return Response(
        json.dumps(info["ui"], ensure_ascii=False),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@bp.get("/jobs/<int:job_id>/frames/<int:order_idx>/flux2-editor-link")
def frame_flux2_editor_link(job_id: int, order_idx: int):
    """返回打开 ComfyUI 并载入本帧 FLUX.2 Klein 工作流的深链接 dict（M17）。"""
    return ok(service.frame_flux2_editor_link(job_id, order_idx))
