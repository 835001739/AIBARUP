"""M15 · 视频转序列帧 / GIF REST 接口（url_prefix=/api/video）。

接口一览：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | ``/api/video/clips`` | 视频列表（按更新时间倒序，支持 keyword 模糊搜索） |
| POST | ``/api/video/clips`` | **上传**视频并导入（multipart，字段名 ``file``） |
| POST | ``/api/video/clips/import-path`` | 导入**本机已有**视频文件路径（不复制） |
| GET | ``/api/video/clips/<id>`` | 详情（含元信息与产物状态） |
| DELETE | ``/api/video/clips/<id>`` | 删除（仅删 data/ 内的源视频，用户指定的本机路径不动） |
| POST | ``/api/video/clips/<id>/extract`` | 抽序列帧（body 可带 fps / max_frames / scale_width / start_sec / end_sec） |
| POST | ``/api/video/clips/<id>/gif`` | 从**已抽出的帧**合成 GIF（body 可带 fps / variant） |
| POST | ``/api/video/clips/<id>/remove-bg`` | 按颜色抠掉帧背景 → 透明 PNG 变体（body 可带 color / similarity / blend；连通抠图加 mode="flood" + seed="x,y"） |
| POST | ``/api/video/clips/<id>/clear-bg`` | 清除去背景变体（**保留原帧**） |
| POST | ``/api/video/clips/<id>/sheet`` | 把序列帧拼成一张网格大图（body 可带 cols / padding / variant） |
| GET | ``/api/video/clips/<id>/sheet`` | 拼图大图（``?variant=nobg`` 取透明版） |
| GET | ``/api/video/clips/<id>/frames`` | 帧清单（返回播放器可直接消费的 playlist 形状，``?variant=nobg``） |
| GET | ``/api/video/clips/<id>/frame/<file>`` | 单帧图片（静态资源，``?variant=nobg``） |
| GET | ``/api/video/clips/<id>/gif`` | GIF 文件（可下载） |
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request, send_from_directory

from core.errors import AIBARError
from core.responses import ok

from . import service

bp = Blueprint("video", __name__, url_prefix="/api/video")


def _json_body() -> dict:
    """解析 JSON 请求体；非对象体一律 400。

    与 comic 不同，这里**允许空体**（如「按默认参数抽帧」POST 无 body），
    空体返回 ``{}`` 让各接口的默认值生效。
    """
    if not request.is_json:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    return data


def _int_arg(name: str, default: int | None = None) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", f"参数 {name} 必须是整数")


# ---------------------------------------------------------------- 列表 / 详情


@bp.get("/clips")
def clips_list():
    """视频列表。"""
    keyword = (request.args.get("keyword") or "").strip()
    limit = _int_arg("limit", 50)
    offset = _int_arg("offset", 0)
    return ok(service.list_clips(keyword=keyword, limit=limit or 50, offset=offset or 0))


@bp.get("/clips/<int:clip_id>")
def clip_detail(clip_id: int):
    clip = service.get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)
    return ok(clip)


@bp.delete("/clips/<int:clip_id>")
def clip_delete(clip_id: int):
    if not service.delete_clip(clip_id):
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)
    return ok({"deleted": True, "id": int(clip_id)})


# ---------------------------------------------------------------- 导入


@bp.post("/clips")
def clips_import():
    """上传视频并导入。

    multipart/form-data，文件字段名 ``file``；可选表单字段 ``name`` 作展示名。
    """
    file_storage = request.files.get("file")
    if file_storage is None or not (file_storage.filename or "").strip():
        raise AIBARError("invalid_input", "缺少文件：请用 multipart 表单提交，字段名 file")

    filename = file_storage.filename or "clip"
    # 先流式落临时盘（边写边查大小上限），再交给 service 搬进 data/
    tmp = service.save_upload_stream(file_storage, filename)
    try:
        name = (request.form.get("name") or "").strip()
        clip = service.import_video(tmp, filename, name=name)
    finally:
        # import_video 成功时会把文件 move 走；失败/异常时清掉残留临时文件
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
    return ok(clip)


@bp.post("/clips/import-path")
def clips_import_path():
    """导入本机已有的视频文件（**不复制**，只登记路径）。"""
    body = _json_body()
    path = str(body.get("path") or "").strip()
    if not path:
        raise AIBARError("invalid_input", "缺少 path")
    return ok(service.import_local_path(path, name=str(body.get("name") or "").strip()))


# ---------------------------------------------------------------- 抽帧 / 转 GIF


@bp.post("/clips/<int:clip_id>/extract")
def clip_extract(clip_id: int):
    """抽序列帧。body 全部可选，缺省沿用视频上次的参数或模块默认值。"""
    body = _json_body()
    return ok(service.extract_frames(
        clip_id,
        fps=body.get("fps"),
        max_frames=body.get("max_frames"),
        scale_width=body.get("scale_width"),
        start_sec=body.get("start_sec"),
        end_sec=body.get("end_sec"),
    ))


@bp.post("/clips/<int:clip_id>/gif")
def clip_gif(clip_id: int):
    """从已抽出的帧合成 GIF（必须先抽帧）。body 可选 ``fps`` / ``variant``。

    ``variant="nobg"`` 时用去背景透明帧合成，GIF 保留透明（1-bit）。
    """
    body = _json_body()
    return ok(service.build_gif(
        clip_id,
        fps=body.get("fps"),
        variant=str(body.get("variant") or "original"),
    ))


# ---------------------------------------------------------------- 去背景 / 拼图


@bp.post("/clips/<int:clip_id>/remove-bg")
def clip_remove_bg(clip_id: int):
    """按颜色抠掉帧背景，生成带透明通道的 PNG 帧变体（原帧不动，可逆）。

    body 全部可选：``color``（#RRGGBB，默认沿用上次的值或绿幕）、
    ``similarity``（容差 0.01-1）、``blend``（边缘羽化 0-1）。
    连通抠图（flood）：``mode="flood"`` + ``seed="x,y"``（种子点，默认左上角 0,0）；
    该模式下忽略 ``color2`` 等双色参数，只抠与种子**连通**的背景。
    """
    body = _json_body()
    return ok(service.remove_background(
        clip_id,
        color=body.get("color"),
        similarity=body.get("similarity"),
        blend=body.get("blend"),
        color2=body.get("color2"),
        similarity2=body.get("similarity2"),
        blend2=body.get("blend2"),
        mode=body.get("mode"),
        seed=body.get("seed"),
    ))


@bp.post("/clips/<int:clip_id>/clear-bg")
def clip_clear_bg(clip_id: int):
    """清除去背景变体，回到「只有原始序列帧」的状态。"""
    return ok(service.clear_background(clip_id))


@bp.post("/clips/<int:clip_id>/sheet")
def clip_sheet_build(clip_id: int):
    """把序列帧拼成一张网格大图（sprite sheet）。

    body 全部可选：``cols``（列数，行数按帧数自动算）、``padding``（帧间留白 px）、
    ``variant``（original / nobg，两种拼图可共存）。
    """
    body = _json_body()
    return ok(service.build_sheet(
        clip_id,
        cols=body.get("cols"),
        padding=body.get("padding"),
        variant=str(body.get("variant") or "original"),
    ))


@bp.get("/clips/<int:clip_id>/sheet")
def clip_sheet_file(clip_id: int):
    """取拼图大图（浏览器可直接打开 / 另存）。``?variant=nobg`` 取透明版。"""
    variant = request.args.get("variant") or "original"
    target = service.sheet_output(clip_id, variant)
    if target is None:
        raise AIBARError("not_found", "还没有拼图：请先抽帧再拼图")
    return send_from_directory(str(target.parent), target.name, mimetype="image/png")


# ---------------------------------------------------------------- 帧清单 / 静态资源


@bp.get("/clips/<int:clip_id>/frames")
def clip_frames(clip_id: int):
    """帧清单，返回**播放器可直接消费的 playlist 形状**。

    ``?variant=nobg`` 返回去背景透明帧（没去过背景时为空清单）。
    ``interval`` 由 fps 推出（1000/fps），前端连播时默认就按视频原速播放。
    """
    clip = service.get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "视频不存在：%s" % clip_id)

    variant = request.args.get("variant") or "original"
    files = service.list_frames(clip_id, variant)
    fps = int(clip.get("fps") or service.DEFAULT_FPS) or service.DEFAULT_FPS
    interval = max(20, int(round(1000.0 / max(1, fps))))
    # 去背景变体的帧 URL 要带上 variant，静态帧接口才知道从哪个目录取
    suffix = "?variant=nobg" if variant == "nobg" else ""
    items = [
        {
            "index": i,
            # 帧文件名由后端按序号生成，这里拼 URL 安全（不含用户输入的路径）
            "url": "/api/video/clips/%d/frame/%s%s" % (int(clip_id), name, suffix),
            "label": "#%d" % (i + 1),
        }
        for i, name in enumerate(files)
    ]
    return ok({
        "clip_id": int(clip_id),
        "name": clip.get("name") or "",
        "variant": variant,
        "fps": fps,
        "interval": interval,
        "loop": True,
        "total": len(items),
        "items": items,
    })


@bp.get("/clips/<int:clip_id>/frame/<path:filename>")
def clip_frame_file(clip_id: int, filename: str):
    """取单帧图片。``?variant=nobg`` 取去背景透明帧（PNG）。"""
    variant = request.args.get("variant") or "original"
    target = service.frame_file(clip_id, filename, variant)
    if target is None:
        raise AIBARError("not_found", "帧不存在：%s" % filename)
    mime = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
    return send_from_directory(str(target.parent), target.name, mimetype=mime)


@bp.get("/clips/<int:clip_id>/gif")
def clip_gif_file(clip_id: int):
    """取 GIF 文件（浏览器可直接打开 / 另存）。"""
    target = service.gif_file(clip_id)
    if target is None:
        raise AIBARError("not_found", "还没有生成 GIF：请先抽帧并转 GIF")
    return send_from_directory(str(target.parent), target.name, mimetype="image/gif")
