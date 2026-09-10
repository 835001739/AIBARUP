"""M1-M4 与 M8 导航页的 HTTP 接口层（Blueprint ``sync``，前缀 ``/api``）。

约束（HARNESS §3/§5）：
- 统一 envelope：``core.responses.ok`` / ``fail``；
- 业务异常抛 ``AIBARError``，由 Blueprint 级 errorhandler 统一转换；
- 分页上限 100，关键词长度受限，所有外部入参先校验再使用；
- 响应与日志都不暴露本机绝对路径、提示词正文只在"详情"接口按需返回（列表给预览）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, request, send_from_directory

from config import Config
from core.db import json_field, query_all, query_one, query_scalar
from core import errors
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from core.responses import from_exception, ok

from . import comfyui_ctl, comfyui_link, models, parser, paths, watcher

logger = get_logger("aibar.sync.routes")

bp = Blueprint("sync", __name__, url_prefix="/api")

VERSION = "0.1.0"

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100
MAX_Q_LEN = 100
PREVIEW_LEN = 140
LOG_MAX_LIMIT = 200
UNCATEGORIZED = "uncategorized"


# ---------------------------------------------------------------- 入参处理


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    """读取并校验整数参数；非法值返回 400 而不是静默取默认值。"""
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


def _paginate(default_size: int = DEFAULT_PAGE_SIZE) -> tuple[int, int]:
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("page_size", default_size, 1, MAX_PAGE_SIZE)
    return page, size


def _q() -> str:
    return (request.args.get("q") or "").strip()[:MAX_Q_LEN]


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _preview(text: str | None, limit: int = PREVIEW_LEN) -> str:
    """列表用的提示词预览：折叠换行并截断，避免列表响应过大。"""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _safe_join(base: Path | None, filename: str) -> Path | None:
    """路径穿越防护的唯一入口，实现已下沉到 ``sync.paths.safe_join``。

    这里保留一层薄封装，是为了让本文件 4 处调用点不用改动；新代码请直接用
    ``paths.safe_join``，别再各写一份——前缀 startswith 那种写法挡不住兄弟目录。
    """
    return paths.safe_join(base, filename)


# ---------------------------------------------------------------- M1 联动


@bp.get("/health")
def health():
    """健康检查：ComfyUI 未运行时仍返回 200，只把 running 置为 false。"""
    return ok(
        {
            "status": "ok",
            "version": VERSION,
            "comfyui": {"running": bool(comfyui_ctl.probe_cached().get("running"))},
        }
    )


@bp.get("/comfyui/status")
def comfyui_status():
    """探测 ComfyUI 运行状态（5s 缓存，``?force=1`` 可绕过）。"""
    force = _to_bool(request.args.get("force"), False)
    return ok(comfyui_ctl.status(force=force))


@bp.post("/comfyui/start")
def comfyui_start():
    """后台拉起 ComfyUI；目录未配置时也不报错，返回带 error_code 的可操作提示。"""
    result = comfyui_ctl.start()
    if not result.get("started"):
        # 环境未就绪属于可预期降级，记错误码即可，不计为服务异常
        safe_log(logger, logging.INFO, "comfyui_start_rejected", error_code=result.get("error_code", ""))
    return ok(result)


@bp.get("/status")
def site_status():
    """全站概览：ComfyUI 状态、计数、最近同步时间与自动同步开关。"""
    watcher_ref = watcher.get_watcher()
    return ok(
        {
            "comfyui": {"running": bool(comfyui_ctl.probe_cached().get("running"))},
            "counts": {
                "workflows": query_scalar("SELECT COUNT(*) FROM workflows", default=0),
                "images": query_scalar("SELECT COUNT(*) FROM images", default=0),
            },
            "last_sync": watcher_ref.last_sync(),
            "auto_sync": watcher_ref.is_auto(),
            "sync_interval": Config.SYNC_INTERVAL,
            "syncing": watcher_ref.is_running(),
        }
    )


# ---------------------------------------------------------------- M2 工作流


def _workflow_row(row) -> dict[str, Any]:
    """列表项：只给提示词预览，不给全文，避免列表接口返回过大。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "filename": row["filename"],
        "node_count": row["node_count"],
        "node_types": json_field(row["node_types"], []),
        "positive_preview": _preview(row["positive_prompt"]),
        "negative_preview": _preview(row["negative_prompt"]),
        "synced_at": row["synced_at"],
        "updated_at": row["updated_at"],
    }


@bp.get("/workflows")
def workflows():
    """工作流列表，支持关键词检索与分页。"""
    page, size = _paginate()
    keyword = _q()
    where, params = ["1=1"], []
    if keyword:
        like = f"%{keyword}%"
        where.append(
            "(name LIKE ? OR filename LIKE ? OR positive_prompt LIKE ?"
            " OR negative_prompt LIKE ? OR node_types LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    where_sql = " AND ".join(where)
    total = query_scalar(f"SELECT COUNT(*) FROM workflows WHERE {where_sql}", params, 0)
    rows = query_all(
        f"SELECT * FROM workflows WHERE {where_sql}"
        " ORDER BY COALESCE(updated_at, synced_at) DESC, id DESC LIMIT ? OFFSET ?",
        [*params, size, (page - 1) * size],
    )
    return ok(
        {
            "items": [_workflow_row(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": size,
        }
    )


@bp.get("/workflows/<path:filename>/detail")
def workflow_detail(filename: str):
    """工作流详情：节点清单与提示词全文。"""
    row = query_one("SELECT * FROM workflows WHERE filename = ?", (filename,))
    if row is None:
        raise errors.not_found("工作流不存在")

    nodes: list[dict[str, Any]] = []
    base = paths.workflows_dir()
    if base is not None:
        target = _safe_join(base, filename)
        # 源文件可能已被删除：此时退回库中已解析的摘要，不报错
        if target is not None and target.is_file():
            try:
                parsed = parser.parse_workflow_text(
                    target.read_text(encoding="utf-8", errors="ignore")
                )
                nodes = parsed.get("nodes") or []
            except (OSError, ValueError) as exc:
                safe_log(logger, logging.INFO, "workflow_detail_parse_failed", error_code=type(exc).__name__)

    return ok(
        {
            "id": row["id"],
            "name": row["name"],
            "filename": row["filename"],
            "node_count": row["node_count"],
            "node_types": json_field(row["node_types"], []),
            "positive_prompt": row["positive_prompt"],
            "negative_prompt": row["negative_prompt"],
            "nodes": nodes,
            "synced_at": row["synced_at"],
            "updated_at": row["updated_at"],
        }
    )


@bp.record_once
def _register_download_alias(state):
    """额外注册不带 ``/api`` 前缀的下载路由。

    API_CONTRACT 把下载链接定义为 ``/download/workflow/<filename>``（可直接放进
    ``<a href>``，不经过 JSON envelope），而 Blueprint 统一带 ``/api`` 前缀，
    因此这里在蓝图注册时补一条同视图的应用级规则，两种路径都可用。
    """
    app = state.app
    if app is None:
        return
    try:
        app.add_url_rule(
            "/download/workflow/<path:filename>",
            endpoint="sync.download_workflow",
            view_func=download_workflow,
            methods=["GET"],
        )
    except Exception as exc:
        # 别名注册失败不能影响主装配：/api/download/... 仍然可用
        safe_log(logger, logging.WARNING, "download_alias_skipped", error_code=type(exc).__name__)


@bp.get("/download/workflow/<path:filename>")
def download_workflow(filename: str):
    """下载原始工作流 JSON（同时可从 ``/download/workflow/...`` 访问）。

    含路径穿越防护，越界一律 404。
    """
    base = paths.workflows_dir()
    if base is None:
        raise errors.not_found("工作流目录不存在")
    target = _safe_join(base, filename)
    if target is None or not target.is_file():
        # 不做区分：越界与不存在都返回 404，避免向攻击者泄露目录结构
        raise errors.not_found("工作流文件不存在")
    rel = target.relative_to(base.resolve())
    return send_from_directory(str(base.resolve()), str(rel), as_attachment=True)


@bp.get("/workflows/<path:filename>/graph")
def workflow_graph(filename: str):
    """原始工作流 JSON（不套 envelope），供 ComfyUI 前端桥梁跨域拉取。

    与 ``/download/workflow/...`` 的区别：这里以 ``application/json`` 内联返回、
    不带 Content-Disposition，便于浏览器直接 ``fetch().json()``。
    跨域由 ``app._register_cors`` 统一放行，越界文件同样一律 404。
    """
    base = paths.workflows_dir()
    if base is None:
        raise errors.not_found("工作流目录不存在")
    target = _safe_join(base, filename)
    if target is None or not target.is_file():
        raise errors.not_found("工作流文件不存在")
    rel = target.relative_to(base.resolve())
    resp = send_from_directory(
        str(base.resolve()), str(rel), mimetype="application/json", max_age=0
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/comfyui/editor-link")
def comfyui_editor_link():
    """生成「打开 ComfyUI 并载入工作流 + 提示词」的深链接。

    入参（至少给一个）：
    - ``workflow``：工作流文件名；
    - ``image_id``：图库/案例 id，用于反查关联工作流与内嵌提示词；
    - ``prompt`` / ``negative``：显式提示词，优先于自动推导。
    """
    workflow = (request.args.get("workflow") or "").strip()
    image_id = (request.args.get("image_id") or "").strip()
    prompt = (request.args.get("prompt") or "").strip()
    negative = (request.args.get("negative") or "").strip()
    if not workflow and not image_id:
        raise AIBARError("invalid_input", "需要 workflow 或 image_id 参数")

    data = comfyui_link.build_editor_link(
        workflow=workflow, image_id=image_id, prompt=prompt, negative=negative
    )
    safe_log(
        logger,
        logging.INFO,
        "comfyui_editor_link",
        workflow=data.get("workflow") or "",
        source=data.get("source") or "",
        bridge=bool(data.get("bridge_installed")),
    )
    return ok(data)


# ---------------------------------------------------------------- M2 出图（联动 ComfyUI）

import uuid

from . import workflow_convert
from reverse import comfyui_client as _cc


def _comfyui_base() -> str:
    return Config.comfyui_base()


def _resolve_workflow_file(filename: str):
    """路径穿越防护下取工作流文件绝对路径；不存在返回 None。"""
    base = paths.workflows_dir()
    if base is None:
        return None
    return _safe_join(base, filename)


@bp.post("/workflows/<path:filename>/generate")
def workflow_generate(filename: str):
    """用 ComfyUI 直接跑这个工作流出图。

    流程：读取 UI 格式工作流 → 转成 API prompt 图 → POST ComfyUI ``/prompt``
    → 返回 ``prompt_id``。前端再轮询 ``/api/workflows/generate/<prompt_id>``
    拿产出图片。ComfyUI 未运行或拒绝工作流时返回可操作的错误码。
    """
    target = _resolve_workflow_file(filename)
    if target is None or not target.is_file():
        raise errors.not_found("工作流文件不存在")

    try:
        raw_text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise AIBARError("read_error", f"读取工作流失败：{exc}")

    try:
        api_graph = workflow_convert.convert_text(raw_text)
    except ValueError as exc:
        raise AIBARError("invalid_workflow", f"工作流无法解析：{exc}")
    except Exception as exc:  # 转换异常（多为 object_info 拉取失败）
        raise AIBARError("convert_failed", f"工作流转换失败：{exc}")

    if not api_graph:
        raise AIBARError("empty_workflow", "工作流没有可执行的节点")

    client_id = str(uuid.uuid4())
    try:
        prompt_id = _cc.submit_prompt(api_graph, client_id, timeout=_cc.SUBMIT_TIMEOUT)
    except _cc.ComfyUIError as exc:
        # 错误码已稳定（missing_model / submit_failed / comfyui_offline …），原样透传
        raise AIBARError(exc.code, exc.message, status=502)

    return ok(
        {
            "prompt_id": prompt_id,
            "client_id": client_id,
            "comfyui_url": _comfyui_base(),
            "node_count": len(api_graph),
        }
    )


@bp.get("/workflows/generate/<prompt_id>")
def workflow_generate_status(prompt_id: str):
    """轮询一次出图任务的状态与产出。

    Returns:
        ``{"status": "running"|"done"|"error", "outputs": [...], "error": ...}``。
        ``outputs`` 为图片引用，每项含 ``view_url``（经 AIBAR 同源代理，可直接给 <img>）。
    """
    entry = _cc.history(prompt_id, timeout=_cc.HISTORY_TIMEOUT)
    if entry is None:
        # 还没进历史：可能仍在队列/执行中
        return ok({"status": "running", "outputs": [], "error": None})

    status = entry.get("status") or {}
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    if status.get("completed") or outputs:
        images = _collect_images(outputs)
        error = _cc._status_error(status)
        return ok(
            {
                "status": "error" if error else "done",
                "outputs": images,
                "error": error,
            }
        )
    return ok({"status": "running", "outputs": [], "error": None})


def _collect_images(outputs: dict) -> list[dict[str, Any]]:
    """从 history 的 outputs 里抽取图片引用，并附上 AIBAR 同源 view 代理 URL。"""
    items: list[dict[str, Any]] = []
    for node_id, value in outputs.items():
        if not isinstance(value, dict):
            continue
        for key in ("images", "gifs"):
            files = value.get(key)
            if not isinstance(files, list):
                continue
            for f in files:
                if not isinstance(f, dict):
                    continue
                fname = f.get("filename")
                if not fname:
                    continue
                params = {
                    "filename": fname,
                    "subfolder": f.get("subfolder") or "",
                    "type": f.get("type") or "output",
                }
                items.append(
                    {
                        "node_id": str(node_id),
                        "kind": key,
                        "filename": fname,
                        "subfolder": params["subfolder"],
                        "type": params["type"],
                        "view_url": (
                            "/api/comfyui/view?"
                            + "&".join(f"{k}={_u(v)}" for k, v in params.items())
                        ),
                    }
                )
    return items


def _u(value: str) -> str:
    from urllib.parse import quote

    return quote(value or "", safe="")


@bp.get("/comfyui/view")
def comfyui_view():
    """同源代理 ComfyUI 的 ``/view``，避免前端跨域直接拉 :8188。

    仅转发图片/动图字节；任何异常都落到 404，不泄漏后端细节。
    """
    from flask import request as _req, send_file
    from io import BytesIO

    import requests

    filename = _req.args.get("filename") or ""
    subfolder = _req.args.get("subfolder") or ""
    ctype = _req.args.get("type") or "output"
    if not filename:
        raise AIBARError("invalid_input", "缺少 filename")

    url = (
        f"{_comfyui_base()}/view?filename={_u(filename)}"
        f"&subfolder={_u(subfolder)}&type={_u(ctype)}"
    )
    try:
        resp = requests.get(url, timeout=20.0)
    except Exception:
        resp = None
    if resp is None or getattr(resp, "status_code", 500) != 200:
        raise errors.not_found("图片不存在")
    bio = BytesIO(resp.content)
    ctype_header = resp.headers.get("Content-Type", "application/octet-stream")
    return send_file(bio, mimetype=ctype_header)


# ---------------------------------------------------------------- M3 图库


def _gallery_row(row) -> dict[str, Any]:
    """图库列表项；``url`` 为可直接给 <img> 使用的站点相对路径。"""
    gallery_path = row["gallery_path"] or ""
    return {
        "id": row["id"],
        "filename": row["filename"],
        "gallery_path": gallery_path,
        "url": f"/static/{gallery_path}" if gallery_path else "",
        "width": row["width"],
        "height": row["height"],
        "size_bytes": row["size_bytes"],
        "prompt": row["prompt"],
        "workflow_link": row["workflow_link"],
        "created_at": row["created_at"],
    }


@bp.get("/gallery")
def gallery():
    """图库列表，按文件创建时间倒序。"""
    page, size = _paginate()
    keyword = _q()
    where, params = ["1=1"], []
    if keyword:
        like = f"%{keyword}%"
        where.append("(filename LIKE ? OR prompt LIKE ? OR workflow_link LIKE ?)")
        params.extend([like, like, like])
    where_sql = " AND ".join(where)
    total = query_scalar(f"SELECT COUNT(*) FROM images WHERE {where_sql}", params, 0)
    rows = query_all(
        f"SELECT * FROM images WHERE {where_sql} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, size, (page - 1) * size],
    )
    return ok(
        {
            "items": [_gallery_row(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": size,
        }
    )


@bp.get("/gallery/<image_id>")
def gallery_detail(image_id: str):
    """图库详情（灯箱）：完整元数据，不含本机绝对路径。"""
    row = query_one("SELECT * FROM images WHERE id = ?", (image_id,))
    if row is None:
        raise errors.not_found("图片不存在")
    return ok(_gallery_row(row))


@bp.get("/gallery/<image_id>/workflow")
def gallery_embedded_workflow(image_id: str):
    """图片内嵌的 UI 工作流 JSON（不套 envelope），供 ComfyUI 桥梁跨域拉取。

    这是 PNG 元数据里存的、真正产出该图片的那张画布，比按名字回工作流库
    匹配准确得多。跨域由 ``app._register_cors`` 放行。

    统一 404：图片不存在、源文件被删、没有内嵌工作流三种情况都返回 404，
    避免向外泄露"哪些图片有元数据"。
    """
    graph = comfyui_link.image_embedded_graph(image_id)
    if graph is None:
        raise errors.not_found("该图片没有内嵌工作流")
    resp = current_app.response_class(
        json.dumps(graph, ensure_ascii=False),
        mimetype="application/json",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- M4 同步


@bp.route("/sync/now", methods=["POST", "GET"])
def sync_now():
    """立即同步一次并返回统计。"""
    force = _to_bool(request.args.get("force") or (request.get_json(silent=True) or {}).get("force"), False)
    watcher_ref = watcher.get_watcher()
    result = watcher_ref.sync_once(force_all=force)
    return ok({**result, "last_sync": watcher_ref.last_sync()})


@bp.post("/sync/auto")
def sync_auto():
    """切换自动同步开关。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = request.form.to_dict()
    if "enabled" not in payload:
        raise AIBARError("invalid_input", "缺少参数 enabled")
    enabled = _to_bool(payload.get("enabled"))
    watcher_ref = watcher.get_watcher()
    return ok({"enabled": watcher_ref.set_auto(enabled)})


@bp.get("/logs")
def logs():
    """同步日志，倒序返回。"""
    limit = _int_arg("limit", 50, 1, LOG_MAX_LIMIT)
    rows = query_all("SELECT id, ts, type, message, status FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))
    return ok({"items": [dict(r) for r in rows], "total": len(rows)})


# ---------------------------------------------------------------- M8 导航


@bp.get("/nav/counts")
def nav_counts():
    """侧栏徽标计数：工作流、图片、案例、模型。"""
    return ok(
        {
            "workflows": query_scalar("SELECT COUNT(*) FROM workflows", default=0),
            "images": query_scalar("SELECT COUNT(*) FROM images", default=0),
            "cases": query_scalar("SELECT COUNT(*) FROM images WHERE prompt <> ''", default=0),
            "models": models.scan_models().get("total", 0),
        }
    )


def _case_title(filename: str) -> str:
    """案例标题：去掉扩展名与 ``_00001_`` 序列，回退为文件名主干。"""
    stem = Path(filename or "").stem
    return parser.guess_workflow_link(filename) or stem


def _case_row(row) -> dict[str, Any]:
    style = (row["workflow_link"] or "").strip() or UNCATEGORIZED
    gallery_path = row["gallery_path"] or ""
    return {
        "id": row["id"],
        "title": _case_title(row["filename"]),
        "style": style,
        "image_path": f"/static/{gallery_path}" if gallery_path else "",
        "prompt": row["prompt"],
        "workflow_name": "" if style == UNCATEGORIZED else style,
    }


@bp.get("/cases")
def cases():
    """案例库：从图库派生，按关联工作流（空则 uncategorized）分组。"""
    page, size = _paginate()
    keyword = _q()
    style = (request.args.get("style") or "").strip()[:MAX_Q_LEN]

    # 基础条件：只有带提示词的图片才算"可复用案例"
    where, params = ["prompt <> ''"], []
    if keyword:
        like = f"%{keyword}%"
        where.append("(filename LIKE ? OR prompt LIKE ?)")
        params.extend([like, like])
    # 分组计数只受关键词影响，保证切换 style 时仍能显示其它分组的数量
    style_where = list(where)
    style_params = list(params)
    if style:
        if style == UNCATEGORIZED:
            where.append("(workflow_link IS NULL OR workflow_link = '')")
        else:
            where.append("workflow_link = ?")
            params.append(style)

    where_sql = " AND ".join(where)
    total = query_scalar(f"SELECT COUNT(*) FROM images WHERE {where_sql}", params, 0)
    rows = query_all(
        f"SELECT * FROM images WHERE {where_sql} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, size, (page - 1) * size],
    )
    style_rows = query_all(
        "SELECT COALESCE(NULLIF(TRIM(workflow_link), ''), ?) AS style_key, COUNT(*) AS cnt"
        f" FROM images WHERE {' AND '.join(style_where)}"
        " GROUP BY style_key ORDER BY cnt DESC, style_key ASC",
        [UNCATEGORIZED, *style_params],
    )
    return ok(
        {
            "items": [_case_row(r) for r in rows],
            "styles": [{"key": r["style_key"], "count": r["cnt"]} for r in style_rows],
            "total": total,
            "page": page,
            "page_size": size,
        }
    )


@bp.get("/models")
def models_route():
    """模型目录扫描结果（5 分钟缓存，``?refresh=1`` 强制重扫）。"""
    refresh = _to_bool(request.args.get("refresh"), False)
    data = models.scan_models(refresh=refresh)
    keyword = _q()
    items = data.get("items") or []
    if keyword:
        needle = keyword.lower()
        items = [
            item
            for item in items
            if needle in str(item.get("name", "")).lower()
            or needle in str(item.get("type", "")).lower()
        ]
    return ok({"items": items, "total": len(items)})


# ---------------------------------------------------------------- 错误处理


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    return from_exception(exc)


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    """兜底：记录错误码（不记录堆栈与请求正文），对外统一 internal_error。"""
    try:
        current_app.logger.exception("unhandled error in sync blueprint")
    except Exception:
        pass
    safe_log(logger, logging.ERROR, "route_error", error_code=type(exc).__name__)
    return from_exception(exc)


__all__ = ["bp"]
