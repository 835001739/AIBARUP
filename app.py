"""AIBAR 应用装配入口。

职责（HARNESS §2）：只做「装配 + 启动」，不含任何业务规则。

- 注册四个业务 Blueprint：``sync``（M1-M4）、``prompt``（M6）、
  ``promptlib``（M7）、``reverse``（M9-M11）；
- 注册全局错误处理器，保证任何异常都返回统一 envelope；
- 启动时执行幂等迁移、内置种子导入、Provider 自检与同步线程拉起；
- 所有启动动作都可失败降级：单个子系统故障绝不让应用起不来。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from config import Config
from core import db
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log, setup_logging
from core.responses import ok

from comic.routes import bp as comic_bp
from prompt.routes import bp as prompt_bp
from promptlib.routes import bp as promptlib_bp
from reverse.routes import bp as reverse_bp
from sync.routes import bp as sync_bp
from video.routes import bp as video_bp
from videopaint.routes import bp as videopaint_bp

logger = get_logger("aibar.app")

# 上传缓存清理周期（秒）：默认每小时一次，失败只记告警
UPLOAD_CLEANUP_INTERVAL = 3600


def create_app() -> Flask:
    """构造并装配 Flask 应用。"""
    setup_logging()
    Config.ensure_dirs()

    app = Flask(
        __name__,
        static_folder=str(Config.STATIC_DIR),
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = max(1, int(Config.REVERSE_UPLOAD_MAX_MB)) * 1024 * 1024

    app.register_blueprint(sync_bp)
    app.register_blueprint(prompt_bp)
    app.register_blueprint(promptlib_bp)
    app.register_blueprint(reverse_bp)
    app.register_blueprint(comic_bp)
    app.register_blueprint(video_bp)
    app.register_blueprint(videopaint_bp)

    _register_error_handlers(app)
    _register_cors(app)
    _register_pages(app)
    return app


def _register_cors(app: Flask) -> None:
    """按白名单放行跨域，供 ComfyUI 前端桥梁拉取工作流 JSON。

    只做同源策略层面的放行，不做任何鉴权放宽：白名单默认只有 ComfyUI 自身来源，
    未命中的来源不写任何 CORS 头（浏览器会照样拦截），因此不会出现"配错即全放开"。
    """

    @app.after_request
    def _apply_cors(resp):  # type: ignore[no-untyped-def]
        try:
            origin = request.headers.get("Origin", "").strip()
            allowed = Config.CORS_ALLOWED_ORIGINS
            if not origin or not allowed:
                return resp
            if "*" in allowed:
                resp.headers["Access-Control-Allow-Origin"] = "*"
            elif origin in allowed:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
            else:
                return resp
            resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Max-Age"] = "600"
        except Exception as exc:  # CORS 头写失败绝不能影响正常响应
            safe_log(logger, logging.WARNING, "cors_header_failed", error_code=type(exc).__name__)
        return resp


# ---------------------------------------------------------------- 错误处理


def _register_error_handlers(app: Flask) -> None:
    """全局兜底：任何异常都转成统一 ``{ok:false,error:{code,message}}`` 结构。"""

    @app.errorhandler(AIBARError)
    def _handle_aibar_error(exc: AIBARError):  # type: ignore[no-untyped-def]
        return jsonify(
            {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        ), exc.status

    @app.errorhandler(404)
    def _handle_not_found(_error):  # type: ignore[no-untyped-def]
        # API 路径返回 JSON，页面路径交给前端路由处理（返回 index.html）
        return jsonify(
            {"ok": False, "error": {"code": "not_found", "message": "资源不存在"}}
        ), 404

    @app.errorhandler(405)
    def _handle_method(_error):  # type: ignore[no-untyped-def]
        return jsonify(
            {"ok": False, "error": {"code": "method_not_allowed", "message": "请求方法不被支持"}}
        ), 405

    @app.errorhandler(413)
    def _handle_too_large(_error):  # type: ignore[no-untyped-def]
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": "file_too_large",
                    "message": f"上传文件超过上限（{Config.REVERSE_UPLOAD_MAX_MB}MB）",
                },
            }
        ), 413

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):  # type: ignore[no-untyped-def]
        # 不向前端泄漏堆栈，只记错误类型
        safe_log(logger, logging.ERROR, "unhandled_exception", error_code=type(exc).__name__)
        return jsonify(
            {"ok": False, "error": {"code": "internal_error", "message": "服务内部错误，请稍后重试"}}
        ), 500


# ---------------------------------------------------------------- 页面


def _register_pages(app: Flask) -> None:
    """页面与服务自描述接口。"""

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        return send_from_directory(str(Config.STATIC_DIR), "index.html")

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        return jsonify({"ok": True, "data": {"status": "ok"}})

    @app.get("/api/error-codes")
    def error_codes():  # type: ignore[no-untyped-def]
        """错误码 → 中文文案表（全项目共用）。

        前端拿它把 ``comfyui_offline`` 这类机器码翻成人话，
        避免各处维护副本导致文案不一致。
        """
        from core.error_text import ERROR_TEXT

        # 直接返回「码→文案」映射本体：前端拿到即可当字典查
        return jsonify({"ok": True, "data": ERROR_TEXT})


# ---------------------------------------------------------------- 启动流程


def bootstrap(app: Flask) -> dict[str, Any]:
    """执行一次启动准备，返回自检摘要（供启动日志与排障使用）。

    每一步都独立 try/except：任一子系统故障只降级该能力，不影响其他模块。
    """
    summary: dict[str, Any] = {"migrations": [], "seeded": 0, "providers": [], "sync": False}

    try:
        summary["migrations"] = db.migrate()
        safe_log(logger, logging.INFO, "db_migrated", actions=len(summary["migrations"]))
    except Exception as exc:
        safe_log(logger, logging.ERROR, "db_migrate_failed", error_code=type(exc).__name__)

    summary["seeded"] = _seed_prompt_library()
    summary["providers"] = _probe_providers()
    summary["sync"] = _start_sync()
    _start_upload_cleanup(app)
    # 先回收上次进程遗留的卡死任务，再拉起 worker —— 顺序反了会把刚复位的页面又抢成 running
    summary["comic_reaped"] = _reap_comic_jobs()
    summary["comic_orphans"] = _cleanup_comic_orphans()
    summary["comic_frame_reaped"] = _reap_comic_frames()
    summary["comic"] = _start_comic_worker()
    return summary


def _cleanup_comic_orphans() -> dict[str, Any]:
    """启动时清理产出目录里的孤儿文件（库记录已删、文件还在）。"""
    try:
        from comic import recovery

        return recovery.cleanup_orphan_outputs()
    except Exception as exc:
        safe_log(logger, logging.WARNING, "comic_orphan_cleanup_failed", error_code=type(exc).__name__)
        return {"removed": 0, "error": type(exc).__name__}


def _reap_comic_jobs() -> dict[str, Any]:
    """启动时回收卡死的出图任务（上次进程被杀留下的 running / generating）。

    失败只记告警：回收是修复动作，它自己挂了也绝不能阻止应用启动。
    """
    try:
        from comic import recovery

        result = recovery.reap_stale_jobs()
        if result.get("stale_jobs") or result.get("reset_pages"):
            safe_log(
                logger, logging.WARNING, "comic_stale_reaped_at_boot",
                jobs=result.get("stale_jobs", 0), pages=result.get("reset_pages", 0),
            )
        return result
    except Exception as exc:
        safe_log(logger, logging.WARNING, "comic_reap_failed", error_code=type(exc).__name__)
        return {"stale_jobs": 0, "reset_pages": 0, "error": type(exc).__name__}


def _reap_comic_frames() -> dict[str, Any]:
    """启动时回收 ``shot_frames`` 表里卡在 ``generating`` 的孤儿帧。

    同 reap_stale_jobs 的设计：超过阈值的 generating 帧视为孤儿，复位为 failed。
    失败只记告警，绝不阻断启动。详见 ``comic/recovery.py::reap_stale_frames``。
    """
    try:
        from comic import recovery

        result = recovery.reap_stale_frames()
        if result.get("stale_frames"):
            safe_log(
                logger, logging.WARNING, "comic_frame_stale_reaped_at_boot",
                frames=result.get("stale_frames", 0),
                groups=len(result.get("group_ids", [])),
            )
        return result
    except Exception as exc:
        safe_log(logger, logging.WARNING, "comic_frame_reap_failed", error_code=type(exc).__name__)
        return {"stale_frames": 0, "error": type(exc).__name__}


def _seed_prompt_library() -> int:
    """导入 M7 内置种子（幂等）。失败只记告警，不阻塞启动。"""
    try:
        from promptlib import entries

        result = entries.seed_from_resources()
        count = int(result.get("inserted", 0)) if isinstance(result, dict) else int(result or 0)
        safe_log(logger, logging.INFO, "prompt_seed_imported", inserted=count)
        return count
    except Exception as exc:
        safe_log(logger, logging.WARNING, "prompt_seed_failed", error_code=type(exc).__name__)
        return 0


def _probe_providers() -> list[dict[str, Any]]:
    """Provider 启动自检摘要（PRD M10.4：只记 provider/status/reason_code/model）。"""
    try:
        from reverse.providers import registry

        providers = registry.list_providers()
        items = providers.get("items", []) if isinstance(providers, dict) else []
        for item in items:
            safe_log(
                logger,
                logging.INFO,
                "provider_selfcheck",
                provider=item.get("key"),
                status=item.get("status"),
                reason_code=item.get("reason_code", ""),
            )
        return items
    except Exception as exc:
        safe_log(logger, logging.WARNING, "provider_probe_failed", error_code=type(exc).__name__)
        return []


def _start_sync() -> bool:
    """拉起后台同步线程；首跑用种子策略快速出图。"""
    try:
        from sync import watcher as sync_watcher

        watcher = sync_watcher.get_watcher()
        watcher.start(run_now=True)
        return True
    except Exception as exc:
        safe_log(logger, logging.WARNING, "sync_start_failed", error_code=type(exc).__name__)
        return False


def _start_upload_cleanup(app: Flask) -> None:
    """后台定期清理过期上传缓存（PRD M9.9：失败只记告警）。"""

    def _loop() -> None:
        while True:
            time.sleep(UPLOAD_CLEANUP_INTERVAL)
            try:
                from reverse import uploads

                stats = uploads.cleanup_expired()
                safe_log(
                    logger,
                    logging.INFO,
                    "upload_cleanup",
                    removed=stats.get("removed", 0),
                    kept=stats.get("kept_referenced", 0),
                )
            except Exception as exc:
                safe_log(
                    logger, logging.WARNING, "upload_cleanup_failed", error_code=type(exc).__name__
                )

    thread = threading.Thread(target=_loop, name="aibar-upload-cleanup", daemon=True)
    thread.start()


def _start_comic_worker() -> bool:
    """拉起 M12 漫画工作室出图队列 worker；失败只记告警，不阻塞启动。"""
    try:
        from comic import start_worker as start_comic_worker

        return bool(start_comic_worker())
    except Exception as exc:
        safe_log(logger, logging.WARNING, "comic_worker_start_failed", error_code=type(exc).__name__)
        return False


app = create_app()


def main() -> None:
    """本地启动入口。"""
    summary = bootstrap(app)
    safe_log(
        logger,
        logging.INFO,
        "app_ready",
        host=Config.HOST,
        port=Config.PORT,
        providers=len(summary.get("providers", [])),
    )
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
