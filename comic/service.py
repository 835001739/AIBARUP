"""M12 · 漫画工作室业务层：CRUD + 出图入队编排。

纯数据库 + JSON 操作，不负责与 ComfyUI 通信（那部分在 ``runner`` 里）。
职责边界与 HARNESS 一致：所有入参校验、枚举约束、ID 归属都在这里完成，
路由层只做 envelope 转换。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config import Config
from core import db
from core.db import dumps, now
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from . import characters
from . import styles

_LOGGER = get_logger("aibar.comic.service")

_PAGE_STATUS = {"pending", "queued", "generating", "done", "failed"}
_PROJECT_STATUS = {"draft", "production", "done"}

# 队列列表的默认返回上限：comic_jobs 只增不减，不设上限会全表返回
MAX_JOBS = 200


def _unlink_outputs(rows: list[dict]) -> int:
    """删除一批分镜页的产出图文件，返回删除个数。

    库里删行不删盘会在 ``data/comic_outputs`` 下留下永远没人认领的孤儿文件；
    这里配合删除操作同步清理。任何失败都只记日志 —— 磁盘清理失败不该阻断数据库删除。
    """
    root = Path(Config.DATA_DIR)
    removed = 0
    for row in rows or []:
        rel = (row.get("image_path") or "").strip()
        if not rel:
            continue
        try:
            from sync.paths import safe_join

            target = safe_join(root, rel)
            if target is not None and target.is_file():
                target.unlink()
                removed += 1
        except Exception as exc:
            safe_log(_LOGGER, 30, "comic_output_unlink_failed", error_type=type(exc).__name__)
    return removed


def _require(d: dict, key: str, label: str):
    val = d.get(key)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        raise AIBARError("invalid_input", f"缺少必填字段：{label}")
    return val


def _json_param(value) -> str:
    if isinstance(value, (dict, list)):
        return dumps(value)
    if isinstance(value, str) and value.strip():
        return value
    return "{}"


# 分镜工作流「出图数量」：每章漫画页数的合法范围
_MIN_PAGES_PER_CHAPTER = 1
_MAX_PAGES_PER_CHAPTER = 12


def _opt_int(value) -> int | None:
    """把任意输入收敛成整数或 None（空值 / 非法值一律 None）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_pages(value, fallback: int = 1) -> int:
    """把任意输入收敛为 [1, 12] 的整数页数；非法/缺失回退 fallback。"""
    if value is None or value == "":
        return max(_MIN_PAGES_PER_CHAPTER, min(_MAX_PAGES_PER_CHAPTER, int(fallback)))
    try:
        n = int(value)
    except (TypeError, ValueError):
        return max(_MIN_PAGES_PER_CHAPTER, min(_MAX_PAGES_PER_CHAPTER, int(fallback)))
    return max(_MIN_PAGES_PER_CHAPTER, min(_MAX_PAGES_PER_CHAPTER, n))


# ---------------------------------------------------------------- 漫画（概览）


def create_project(payload: dict) -> dict:
    name = str(_require(payload, "name", "name")).strip()
    if len(name) > 200:
        raise AIBARError("invalid_input", "漫画名称过长")
    description = str(payload.get("description") or "")
    status = str(payload.get("status") or "draft")
    if status not in _PROJECT_STATUS:
        status = "draft"
    default_workflow = str(payload.get("default_workflow") or "")
    worldview = str(payload.get("worldview") or "")
    plot_summary = str(payload.get("plot_summary") or "")
    pages_per_chapter = _clamp_pages(payload.get("pages_per_chapter"))
    # 出图风格：未知取值一律收敛为默认预设（none = 不限制）
    style_preset = styles.normalize_style(payload.get("style_preset"))
    # 基准种子：留空时由角色模块在首次生成分镜时按 project_id 确定性初始化
    base_seed = _opt_int(payload.get("base_seed"))
    ts = now()
    cur = db.execute(
        "INSERT INTO comic_projects (name, description, status, default_workflow, worldview, plot_summary, global_params, pages_per_chapter, style_preset, base_seed, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name, description, status, default_workflow, worldview, plot_summary,
            _json_param(payload.get("global_params")), pages_per_chapter, style_preset,
            base_seed, ts, ts,
        ),
    )
    return get_project(cur.lastrowid)


def get_project(project_id: int) -> dict:
    """单个项目。**带上与列表一致的统计字段**。

    否则详情页头部读 ``page_count`` 永远拿到 undefined，只能显示「分镜 0 / 完成 0」，
    和同页面下方实时进度卡的 6/7 自相矛盾。
    """
    row = db.row_to_dict(
        db.query_one(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM comic_chapters c WHERE c.project_id=p.id) AS chapter_count, "
            "(SELECT COUNT(*) FROM comic_pages pg WHERE pg.project_id=p.id) AS page_count, "
            "(SELECT COUNT(*) FROM comic_pages pg WHERE pg.project_id=p.id AND pg.status='done') AS done_count "
            "FROM comic_projects p WHERE p.id=?",
            (project_id,),
        )
    )
    if row is None:
        raise AIBARError("not_found", "漫画不存在")
    return row


def list_projects() -> list[dict]:
    rows = db.query_all(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM comic_chapters c WHERE c.project_id=p.id) AS chapter_count, "
        "(SELECT COUNT(*) FROM comic_pages pg WHERE pg.project_id=p.id) AS page_count, "
        "(SELECT COUNT(*) FROM comic_pages pg WHERE pg.project_id=p.id AND pg.status='done') AS done_count "
        "FROM comic_projects p ORDER BY p.updated_at DESC, p.id DESC"
    )
    return db.rows_to_dicts(rows)


def update_project(project_id: int, payload: dict) -> dict:
    row = get_project(project_id)
    name = str(payload.get("name", row["name"])).strip() or row["name"]
    description = payload.get("description", row["description"])
    status = payload.get("status", row["status"])
    if status not in _PROJECT_STATUS:
        status = row["status"]
    default_workflow = payload.get("default_workflow", row["default_workflow"])
    worldview = payload.get("worldview", row.get("worldview", ""))
    plot_summary = payload.get("plot_summary", row.get("plot_summary", ""))
    # 出图数量（每章页数）：未传则保留原值；传了则按 [1,12] 约束
    if "pages_per_chapter" in payload:
        pages_per_chapter = _clamp_pages(payload.get("pages_per_chapter"), row.get("pages_per_chapter", 1))
    else:
        pages_per_chapter = row.get("pages_per_chapter", 1)
    # 出图风格：未传则保留原值；传了则按预设表收敛（非法值回退原值，而不是 none）
    if "style_preset" in payload:
        style_preset = styles.normalize_style(
            payload.get("style_preset"), row.get("style_preset") or styles.DEFAULT_STYLE
        )
    else:
        style_preset = styles.normalize_style(row.get("style_preset") or styles.DEFAULT_STYLE)
    # 基准种子：未传则保留；传空字符串视为清空（下次生成时重新确定性初始化）
    if "base_seed" in payload:
        base_seed = _opt_int(payload.get("base_seed"))
    else:
        base_seed = row.get("base_seed")
    gp = payload.get("global_params", row["global_params"])
    gp = gp if isinstance(gp, str) and gp.strip() else _json_param(gp)
    db.execute(
        "UPDATE comic_projects SET name=?, description=?, status=?, default_workflow=?, worldview=?, plot_summary=?, global_params=?, pages_per_chapter=?, style_preset=?, base_seed=?, updated_at=? "
        "WHERE id=?",
        (
            name, description, status, default_workflow, worldview, plot_summary,
            gp, pages_per_chapter, style_preset, base_seed, now(), project_id,
        ),
    )
    return get_project(project_id)


def delete_project(project_id: int) -> None:
    get_project(project_id)
    outputs = db.rows_to_dicts(
        db.query_all("SELECT image_path FROM comic_pages WHERE project_id=?", (project_id,))
    )
    with db.tx():
        db.execute("DELETE FROM comic_jobs WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM comic_pages WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM comic_chapters WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM comic_characters WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM comic_projects WHERE id=?", (project_id,))
    _unlink_outputs(outputs)
    _remove_project_dir(project_id)


def _remove_project_dir(project_id: int) -> None:
    """删除项目的产出目录（``comic_outputs/<project_id>/``）。

    按页删文件只能清掉库里还记着的图；历史孤儿（库记录已丢、文件还在）要靠这一并清掉。
    目录非空时也不能直接 rmdir，故逐层清理空目录。
    """
    from pathlib import Path as _Path

    root = _Path(Config.DATA_DIR) / "comic_outputs" / str(int(project_id))
    try:
        if not root.is_dir():
            return
        for child in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            except OSError:
                pass
        root.rmdir()
    except Exception as exc:
        safe_log(_LOGGER, 30, "comic_project_dir_remove_failed", error_type=type(exc).__name__)


# ---------------------------------------------------------------- 章节管理


def create_chapter(project_id: int, payload: dict) -> dict:
    get_project(project_id)
    title = str(_require(payload, "title", "title")).strip()
    order_idx = int(payload.get("order_idx") or 0)
    summary = str(payload.get("summary") or "")
    ts = now()
    cur = db.execute(
        "INSERT INTO comic_chapters (project_id, title, order_idx, summary, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (project_id, title, order_idx, summary, ts, ts),
    )
    return get_chapter(cur.lastrowid)


def get_chapter(chapter_id: int) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM comic_chapters WHERE id=?", (chapter_id,)))
    if row is None:
        raise AIBARError("not_found", "章节不存在")
    return row


def list_chapters(project_id: int) -> list[dict]:
    get_project(project_id)
    rows = db.query_all(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM comic_pages p WHERE p.chapter_id=c.id) AS page_count, "
        "(SELECT COUNT(*) FROM comic_pages p WHERE p.chapter_id=c.id AND p.status='done') AS done_count "
        "FROM comic_chapters c WHERE c.project_id=? ORDER BY c.order_idx ASC, c.id ASC",
        (project_id,),
    )
    return db.rows_to_dicts(rows)


def update_chapter(chapter_id: int, payload: dict) -> dict:
    row = get_chapter(chapter_id)
    title = str(payload.get("title", row["title"])).strip() or row["title"]
    order_idx = payload.get("order_idx", row["order_idx"])
    summary = payload.get("summary", row["summary"])
    db.execute(
        "UPDATE comic_chapters SET title=?, order_idx=?, summary=?, updated_at=? WHERE id=?",
        (title, int(order_idx or 0), summary, now(), chapter_id),
    )
    return get_chapter(chapter_id)


def delete_chapter(chapter_id: int) -> None:
    get_chapter(chapter_id)
    outputs = db.rows_to_dicts(
        db.query_all("SELECT image_path FROM comic_pages WHERE chapter_id=?", (chapter_id,))
    )
    with db.tx():
        db.execute("DELETE FROM comic_jobs WHERE chapter_id=?", (chapter_id,))
        db.execute("DELETE FROM comic_pages WHERE chapter_id=?", (chapter_id,))
        db.execute("DELETE FROM comic_chapters WHERE id=?", (chapter_id,))
    _unlink_outputs(outputs)


# ---------------------------------------------------------------- 分镜（每章节图片：预制提示词 + 出图工作流）


def create_page(chapter_id: int, payload: dict) -> dict:
    ch = get_chapter(chapter_id)
    project_id = ch["project_id"]
    title = str(payload.get("title") or "").strip()
    order_idx = int(payload.get("order_idx") or 0)
    prompt_text = str(payload.get("prompt_text") or "")
    negative_text = str(payload.get("negative_text") or "")
    workflow_filename = str(payload.get("workflow_filename") or "")
    seed = _opt_int(payload.get("seed"))
    # 原始扩写结果与镜头景别：保存「注入角色锚点与风格词之前」的内容，
    # 供「改角色卡 / 换风格后重刷提示词」反复复用（见 storyboard.refresh_project_prompts）。
    base_prompt = str(payload.get("base_prompt") or "")
    base_negative = str(payload.get("base_negative") or "")
    shot_note = str(payload.get("shot_note") or "")
    # 剧情原文（扩写前）。单页「重扩写」要拿它重新过一次模型；没有它时
    # 只能退回用 base_prompt 扩写（历史页没有该字段，效果打折但仍可用）。
    beat_text = str(payload.get("beat_text") or "")
    # 该页涉及的角色名（逗号分隔），用于人物一致性追溯与前端展示
    character_names = characters.join_aliases(
        payload.get("character_names") if isinstance(payload.get("character_names"), (list, tuple))
        else characters.parse_aliases(payload.get("character_names"))
    )
    ts = now()
    cur = db.execute(
        "INSERT INTO comic_pages (project_id, chapter_id, title, order_idx, prompt_text, negative_text, "
        "workflow_filename, seed, character_names, base_prompt, base_negative, shot_note, beat_text, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
        (
            project_id, chapter_id, title, order_idx, prompt_text, negative_text,
            workflow_filename, seed, character_names, base_prompt, base_negative, shot_note, beat_text, ts, ts,
        ),
    )
    return get_page(cur.lastrowid)


def get_page(page_id: int) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM comic_pages WHERE id=?", (page_id,)))
    if row is None:
        raise AIBARError("not_found", "分镜不存在")
    return row


def list_pages(chapter_id: int) -> list[dict]:
    get_chapter(chapter_id)
    rows = db.query_all(
        "SELECT * FROM comic_pages WHERE chapter_id=? ORDER BY order_idx ASC, id ASC",
        (chapter_id,),
    )
    return db.rows_to_dicts(rows)


def list_project_pages(project_id: int) -> list[dict]:
    """列出项目下**全部**章节的分镜页（按章节序 → 页序）。

    按条件批量重出图需要跨章节统计景别/角色，逐个章节拉会造成 N+1 次查询。
    """
    get_project(project_id)
    rows = db.query_all(
        "SELECT p.* FROM comic_pages p "
        "JOIN comic_chapters c ON c.id=p.chapter_id "
        "WHERE p.project_id=? ORDER BY c.order_idx ASC, c.id ASC, p.order_idx ASC, p.id ASC",
        (project_id,),
    )
    return db.rows_to_dicts(rows)


def sync_gallery(project_id: int | None = None) -> dict[str, Any]:
    """把「已出图但还没登记进图库」的历史分镜页补登记一遍。

    图库登记是后面才加上的能力，改造前已经出好的图都缺 ``image_id``。
    没有这个回溯入口的话，老项目要重新出一遍图才能用上反推/深链，代价太大。

    Args:
        project_id: 只补登记该项目；None 表示全库。

    Returns:
        ``{"scanned", "registered", "skipped", "failed", "missing", "project_id"}``。

        其中 ``missing`` 是「库里有路径、磁盘上文件没了」的页 —— 这类引用会让
        前端渲染出破图，这里顺手把悬空路径清掉，让页面回到「未出图」的诚实状态。
    """
    sql = (
        "SELECT id, image_id, image_path, prompt_text, workflow_filename FROM comic_pages "
        "WHERE (image_path IS NOT NULL AND image_path<>'')"
    )
    args: list[Any] = []
    if project_id is not None:
        get_project(project_id)
        sql += " AND project_id=?"
        args.append(project_id)
    rows = db.rows_to_dicts(db.query_all(sql, tuple(args)))

    result: dict[str, Any] = {
        "scanned": 0,
        "registered": 0,
        "skipped": 0,
        "failed": 0,
        "missing": 0,
        "project_id": project_id,
    }
    root = Path(Config.DATA_DIR)
    for row in rows:
        rel = (row.get("image_path") or "").strip()
        if not rel:
            continue
        result["scanned"] += 1
        try:
            from sync.paths import safe_join

            target = safe_join(root, rel)
        except Exception:
            target = None
        if target is None or not target.is_file():
            # 文件没了还留着路径，前端只会渲染出一张破图；清掉反而诚实
            result["missing"] += 1
            db.execute(
                "UPDATE comic_pages SET image_path='', image_id=NULL WHERE id=?", (row["id"],)
            )
            safe_log(_LOGGER, 30, "comic_output_missing", page_id=row["id"])
            continue
        if row.get("image_id"):
            result["skipped"] += 1
            continue
        try:
            from sync.scanner import register_image

            image_id = register_image(
                target,
                prompt=row.get("prompt_text") or "",
                workflow_link=row.get("workflow_filename") or "",
            )
        except Exception as exc:
            safe_log(_LOGGER, 30, "comic_gallery_sync_failed", error_type=type(exc).__name__)
            result["failed"] += 1
            continue
        if not image_id:
            result["failed"] += 1
            continue
        db.execute("UPDATE comic_pages SET image_id=? WHERE id=?", (image_id, row["id"]))
        result["registered"] += 1
    return result


def update_page(page_id: int, payload: dict) -> dict:
    row = get_page(page_id)
    title = payload.get("title", row["title"])
    order_idx = payload.get("order_idx", row["order_idx"])
    prompt_text = payload.get("prompt_text", row["prompt_text"])
    negative_text = payload.get("negative_text", row["negative_text"])
    workflow_filename = payload.get("workflow_filename", row["workflow_filename"])
    seed = _opt_int(payload.get("seed", row["seed"]))
    base_prompt = payload.get("base_prompt", row.get("base_prompt") or "")
    base_negative = payload.get("base_negative", row.get("base_negative") or "")
    shot_note = payload.get("shot_note", row.get("shot_note") or "")
    beat_text = payload.get("beat_text", row.get("beat_text") or "")
    if "character_names" in payload:
        raw_cn = payload.get("character_names")
        character_names = characters.join_aliases(
            raw_cn if isinstance(raw_cn, (list, tuple)) else characters.parse_aliases(raw_cn)
        )
    else:
        character_names = row.get("character_names") or ""
    if "error_message" in payload:
        error_message = str(payload.get("error_message") or "")
    else:
        error_message = row.get("error_message") or ""
    db.execute(
        "UPDATE comic_pages SET title=?, order_idx=?, prompt_text=?, negative_text=?, workflow_filename=?, seed=?, character_names=?, error_message=?, base_prompt=?, base_negative=?, shot_note=?, beat_text=?, updated_at=? "
        "WHERE id=?",
        (
            title, int(order_idx or 0), prompt_text, negative_text, workflow_filename, seed,
            character_names, error_message, base_prompt, base_negative, shot_note, beat_text, now(), page_id,
        ),
    )
    return get_page(page_id)


def delete_page(page_id: int) -> None:
    page = get_page(page_id)
    with db.tx():
        db.execute("DELETE FROM comic_jobs WHERE page_id=?", (page_id,))
        db.execute("DELETE FROM comic_pages WHERE id=?", (page_id,))
    _unlink_outputs([page])


# ---------------------------------------------------------------- 出图队列


def _enqueue(page_id: int) -> int:
    """把一页入队。

    INSERT job 与 UPDATE page 必须同一事务：分两次自动提交的话，中间崩溃会留下
    「job 已 queued 但页还是 failed」的不一致状态，worker 会去跑一个用户看来没在排队的页。
    """
    page = get_page(page_id)
    if page["status"] == "generating":
        raise AIBARError("invalid_input", "该分镜正在出图中，无法重复入队")
    with db.tx():
        cur = db.execute(
            "INSERT INTO comic_jobs (page_id, chapter_id, project_id, status, stage, created_at) "
            "VALUES (?,?,?, 'queued', 'queued', ?)",
            (page_id, page["chapter_id"], page["project_id"], now()),
        )
        db.execute("UPDATE comic_pages SET status='queued', error_message='' WHERE id=?", (page_id,))
    return cur.lastrowid


def enqueue_page(page_id: int) -> dict:
    job_id = _enqueue(page_id)
    return {"job_id": job_id, "page_id": page_id, "status": "queued"}


def regenerate_page(page_id: int) -> dict:
    """对单张分镜重新生成：无论当前状态都新建一个出图任务。"""
    return enqueue_page(page_id)


def _enqueue_many(page_ids: list[int]) -> int:
    """批量入队，整体一个事务：中途失败不会留下「入了一半」的半批状态。"""
    count = 0
    with db.tx():
        for pid in page_ids:
            page = db.row_to_dict(db.query_one("SELECT * FROM comic_pages WHERE id=?", (pid,)))
            if page is None or page.get("status") == "generating":
                continue
            db.execute(
                "INSERT INTO comic_jobs (page_id, chapter_id, project_id, status, stage, created_at) "
                "VALUES (?,?,?, 'queued', 'queued', ?)",
                (pid, page["chapter_id"], page["project_id"], now()),
            )
            db.execute(
                "UPDATE comic_pages SET status='queued', error_message='' WHERE id=?", (pid,)
            )
            count += 1
    return count


def enqueue_chapter(chapter_id: int) -> dict:
    get_chapter(chapter_id)
    pages = db.query_all(
        "SELECT id FROM comic_pages WHERE chapter_id=? AND status<>'generating' ORDER BY order_idx ASC, id ASC",
        (chapter_id,),
    )
    return {"enqueued": _enqueue_many([p["id"] for p in pages])}


def enqueue_project(project_id: int, page_ids: list[int] | None = None,
                    statuses: list[str] | None = None) -> dict:
    """整本出图。

    Args:
        project_id: 项目 ID。
        page_ids: 只入队这些页（用于「按条件批量重出图」）；None 表示全部。
        statuses: 只入队处于这些状态的页（如 ``["failed"]`` 只重跑失败页）。
    """
    get_project(project_id)
    sql = "SELECT id FROM comic_pages WHERE project_id=? AND status<>'generating'"
    params: list[Any] = [project_id]
    if page_ids:
        sql += " AND id IN (%s)" % ",".join("?" * len(page_ids))
        params.extend(int(i) for i in page_ids)
    if statuses:
        allowed = [s for s in statuses if s in _PAGE_STATUS and s != "generating"]
        if allowed:
            sql += " AND status IN (%s)" % ",".join("?" * len(allowed))
            params.extend(allowed)
    sql += " ORDER BY id ASC"
    pages = db.query_all(sql, params)
    return {"enqueued": _enqueue_many([p["id"] for p in pages])}


def list_jobs(project_id: int | None = None, status: str | None = None,
              limit: int = MAX_JOBS) -> list[dict]:
    """出图任务列表。

    ``comic_jobs`` 只增不减（现网已达数百行），不设上限会全表返回拖慢界面。
    """
    sql = "SELECT * FROM comic_jobs WHERE 1=1"
    params: list[Any] = []
    if project_id is not None:
        sql += " AND project_id=?"
        params.append(project_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or MAX_JOBS), 1000)))
    return db.rows_to_dicts(db.query_all(sql, params))


def cancel_job(job_id: int) -> dict:
    job = db.row_to_dict(db.query_one("SELECT * FROM comic_jobs WHERE id=?", (job_id,)))
    if job is None:
        raise AIBARError("not_found", "任务不存在")
    if job["status"] in ("done", "failed", "cancelled"):
        return job
    with db.tx():
        db.execute(
            "UPDATE comic_jobs SET status='cancelled', stage='cancelled', finished_at=? WHERE id=?",
            (now(), job_id),
        )
        # 仅当页面没有其它进行中任务时才把页面复位，避免误清 generating
        other = db.query_one(
            "SELECT id FROM comic_jobs WHERE page_id=? AND status IN ('queued','running') AND id<>?",
            (job["page_id"], job_id),
        )
        if other is None:
            db.execute(
                "UPDATE comic_pages SET status='pending' WHERE id=? AND status='queued'",
                (job["page_id"],),
            )
    return db.row_to_dict(db.query_one("SELECT * FROM comic_jobs WHERE id=?", (job_id,)))


# ---------------------------------------------------------------- 角色卡（人物一致性）


def _truthy(value, fallback: bool = False) -> bool:
    """宽容解析布尔参数：True/1/"1"/"true"/"on" 为真；None 时取 fallback。"""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _text(value) -> str:
    return "" if value is None else str(value)


def _clamp_seed_offset(value, fallback: int = 0) -> int:
    n = _opt_int(value)
    if n is None:
        n = fallback
    return max(0, min(9999, n))


def _extract_prompt_text(raw: Any) -> str:
    """从 images.prompt（通常是 ComfyUI workflow JSON）尽力提取自然语言正提示词。

    图库图片的 prompt 字段存的是 workflow 字典序列化，而非纯文本；直接塞进角色卡
    锚点会是一大段 JSON。这里提取其中最长的自然语言文本串作为「出图提示词」，
    提取不到则返回空串（绝不把整段 JSON 写进角色卡）。
    """
    if not raw:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # 非 JSON：视为已经是纯文本提示词
        return text[:2000]
    candidates: list[str] = []
    nodes: list = []
    if isinstance(data, dict):
        if isinstance(data.get("nodes"), list):
            nodes = data["nodes"]
        else:
            nodes = list(data.values())
    for node in nodes:
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        val = inputs.get("text") or inputs.get("value") or ""
        if isinstance(val, str) and len(val.strip()) >= 8:
            candidates.append(val.strip())
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return candidates[0][:2000]


def _attach_character_image(items: list[dict]) -> None:
    """为角色卡补充样板图 URL 与出图提示词（导入图库图片作为角色时使用）。

    样板图关联 ``images.id``，URL 复用图库约定（``/static/{gallery_path}``）；
    ``sample_prompt`` 取该图片的出图提示词，供编辑框展示与「自动更新角色卡提示词」参考。
    """
    want = [it.get("image_id") for it in items if it.get("image_id")]
    if not want:
        for it in items:
            it.setdefault("image_url", "")
            it.setdefault("sample_prompt", "")
        return
    placeholders = ",".join("?" for _ in want)
    rows = db.query_all(
        "SELECT id, gallery_path, prompt FROM images WHERE id IN (%s)" % placeholders, tuple(want)
    )
    info = {r["id"]: db.row_to_dict(r) for r in rows}
    for it in items:
        img = info.get(it.get("image_id"))
        if img and img.get("gallery_path"):
            it["image_url"] = "/static/%s" % img["gallery_path"]
            it["sample_prompt"] = _extract_prompt_text(img.get("prompt"))
        else:
            it["image_url"] = ""
            it["sample_prompt"] = ""


def _attach_character_actor(items: list[dict]) -> None:
    """标注每个角色卡由哪位演员出演（M13 演员库）。

    这里直接查关联表而不是调用 ``actors`` 模块，是为了避免 ``service ↔ actors``
    互相导入成环（``actors`` 需要复用 ``service.create_character``）。
    """
    ids = [int(it["id"]) for it in items if it.get("id") is not None]
    info: dict[int, dict] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = db.query_all(
            "SELECT l.character_id, l.actor_id, l.role_note, a.name AS actor_name, a.status AS actor_status "
            "FROM actor_character_links l JOIN actors a ON a.id = l.actor_id "
            "WHERE l.character_id IN (%s)" % placeholders,
            tuple(ids),
        )
        for r in rows:
            row = db.row_to_dict(r) or {}
            info[int(row["character_id"])] = row
    for it in items:
        hit = info.get(int(it["id"])) if it.get("id") is not None else None
        it["actor_id"] = hit.get("actor_id") if hit else None
        it["actor_name"] = (hit.get("actor_name") or "") if hit else ""
        it["actor_status"] = (hit.get("actor_status") or "") if hit else ""


def list_characters(project_id: int) -> list[dict]:
    get_project(project_id)
    rows = db.query_all(
        "SELECT * FROM comic_characters WHERE project_id=? ORDER BY id ASC", (project_id,)
    )
    items = db.rows_to_dicts(rows)
    for it in items:
        it["anchor"] = characters.build_character_anchor(it)
    _attach_character_image(items)
    _attach_character_actor(items)
    return items


def get_character(character_id: int, with_anchor: bool = True) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM comic_characters WHERE id=?", (character_id,)))
    if row is None:
        raise AIBARError("not_found", "角色不存在")
    if with_anchor:
        row["anchor"] = characters.build_character_anchor(row)
    _attach_character_image([row])
    _attach_character_actor([row])
    return row


def _resolve_import_image(payload: dict) -> tuple:
    """从导入请求解析关联样板图；返回 (image_id, appearance)。

    - 未传 image_id → (None, None)：不关联，外貌沿用 payload。
    - 传了 image_id → 校验图片存在；外貌为空时自动用该图片出图提示词填充角色卡提示词。
    """
    raw = _text(payload.get("image_id"))
    if not raw:
        return None, None
    img = db.row_to_dict(db.query_one("SELECT id, prompt FROM images WHERE id=?", (raw,)))
    if img is None:
        raise AIBARError("not_found", "关联图库图片不存在：%s" % raw)
    appearance = None
    if not _text(payload.get("appearance")):
        appearance = _extract_prompt_text(img.get("prompt"))
    return img["id"], appearance


def create_character(project_id: int, payload: dict) -> dict:
    get_project(project_id)
    name = characters.normalize_name(_require(payload, "name", "角色名"))
    if not name:
        raise AIBARError("invalid_input", "角色名不能为空")
    existing = list_characters(project_id)
    if len(existing) >= characters.MAX_CHARACTERS:
        raise AIBARError("invalid_input", "角色数已达上限 %d 个" % characters.MAX_CHARACTERS)
    dup = db.query_one(
        "SELECT id FROM comic_characters WHERE project_id=? AND name=?", (project_id, name)
    )
    if dup is not None:
        raise AIBARError("invalid_input", "同名角色已存在：%s" % name)
    # 导入图库图片作为角色：关联样板图，外貌为空时自动用出图提示词填充角色卡提示词
    image_id, appearance = _resolve_import_image(payload)
    # 主角：第一个创建的角色默认为主角（整本贯穿），之后创建的默认配角
    is_main = 1 if _truthy(payload.get("is_main"), fallback=not existing) else 0
    ts = now()
    cur = db.execute(
        "INSERT INTO comic_characters (project_id, name, aliases, appearance, outfit, palette, negative, seed_offset, is_main, image_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            name,
            characters.join_aliases(characters.parse_aliases(payload.get("aliases"))),
            _text(appearance) if appearance is not None else _text(payload.get("appearance")),
            _text(payload.get("outfit")),
            _text(payload.get("palette")),
            _text(payload.get("negative")),
            _clamp_seed_offset(payload.get("seed_offset"), len(existing)),
            is_main,
            image_id,
            ts, ts,
        ),
    )
    return get_character(cur.lastrowid)


def update_character(character_id: int, payload: dict) -> dict:
    row = get_character(character_id)
    name = characters.normalize_name(payload.get("name", row["name"])) or row["name"]
    if name != row["name"]:
        dup = db.query_one(
            "SELECT id FROM comic_characters WHERE project_id=? AND name=? AND id<>?",
            (row["project_id"], name, character_id),
        )
        if dup is not None:
            raise AIBARError("invalid_input", "同名角色已存在：%s" % name)
    aliases = (
        characters.join_aliases(characters.parse_aliases(payload.get("aliases")))
        if "aliases" in payload else row["aliases"]
    )
    is_main = 1 if _truthy(payload.get("is_main"), fallback=characters.is_main(row)) else 0
    # 样板图：仅当请求显式带 image_id 时变更（重新关联或清空）；外貌未编辑时
    # 自动用新图出图提示词刷新角色卡提示词（"自动根据出图提示词更新角色卡提示词"）
    image_id = row.get("image_id") or None
    appearance = _text(payload.get("appearance", row["appearance"]))
    if "image_id" in payload:
        raw = _text(payload.get("image_id"))
        if raw:
            img = db.row_to_dict(db.query_one("SELECT id, prompt FROM images WHERE id=?", (raw,)))
            if img is None:
                raise AIBARError("not_found", "关联图库图片不存在：%s" % raw)
            image_id = img["id"]
            if not _text(payload.get("appearance", "")):
                appearance = _extract_prompt_text(img.get("prompt"))
        else:
            image_id = None
    db.execute(
        "UPDATE comic_characters SET name=?, aliases=?, appearance=?, outfit=?, palette=?, negative=?, seed_offset=?, is_main=?, image_id=?, updated_at=? "
        "WHERE id=?",
        (
            name, aliases,
            appearance,
            _text(payload.get("outfit", row["outfit"])),
            _text(payload.get("palette", row["palette"])),
            _text(payload.get("negative", row["negative"])),
            _clamp_seed_offset(payload.get("seed_offset", row["seed_offset"]), row["seed_offset"] or 0),
            is_main,
            image_id,
            now(), character_id,
        ),
    )
    return get_character(character_id)


def delete_character(character_id: int) -> None:
    """删除角色卡。同步解除演员关联并回收演员的使用计数（否则演员库会显示幽灵出演记录）。"""
    get_character(character_id)
    with db.tx():
        link = db.row_to_dict(
            db.query_one("SELECT actor_id FROM actor_character_links WHERE character_id=?", (character_id,))
        )
        if link is not None:
            db.execute("DELETE FROM actor_character_links WHERE character_id=?", (character_id,))
            db.execute(
                "UPDATE actors SET use_count=MAX(0, use_count-1) WHERE id=?", (link["actor_id"],)
            )
        db.execute("DELETE FROM comic_characters WHERE id=?", (character_id,))


def sync_characters(project_id: int, *texts: str, create_missing: bool = True) -> dict:
    """从世界观 / 剧情摘要补充角色卡：只**新增**缺失角色，绝不覆盖或删除已有卡片。"""
    get_project(project_id)
    existing = list_characters(project_id)
    drafts = characters.extract_characters(
        texts[0] if texts else "", texts[1] if len(texts) > 1 else "", existing=existing
    )
    created = 0
    if create_missing:
        for d in drafts:
            create_character(
                project_id,
                {
                    "name": d["name"],
                    "aliases": d.get("aliases", ""),
                    "appearance": d.get("appearance", ""),
                    "outfit": d.get("outfit", ""),
                    "palette": d.get("palette", ""),
                    "negative": d.get("negative", ""),
                    "seed_offset": d.get("seed_offset", 0),
                },
            )
            created += 1
    return {
        "characters": list_characters(project_id),
        "created": created,
        "candidates": [d["name"] for d in drafts],
    }


# ---------------------------------------------------------------- 进度（工作进度展示）


def project_progress(project_id: int) -> dict:
    """项目出图进度：各状态页数、完成百分比、章节级进度、当前生成中页与最近失败原因。

    纯数据库聚合，不访问网络（ComfyUI 状态由路由层合并），因此任何情况下都不会拖慢或失败。
    """
    get_project(project_id)
    counts = {k: 0 for k in ("pending", "queued", "generating", "done", "failed")}
    for r in db.query_all(
        "SELECT status, COUNT(*) AS n FROM comic_pages WHERE project_id=? GROUP BY status",
        (project_id,),
    ):
        key = r["status"] if r["status"] in counts else "pending"
        counts[key] += int(r["n"] or 0)
    total = sum(counts.values())
    done = counts["done"]
    failed = counts["failed"]

    chapters = []
    for ch in list_chapters(project_id):
        ch_total = int(ch.get("page_count") or 0)
        ch_done = int(ch.get("done_count") or 0)
        chapters.append(
            {
                "chapter_id": ch["id"],
                "title": ch["title"],
                "total": ch_total,
                "done": ch_done,
                "percent": round(ch_done * 100.0 / ch_total, 1) if ch_total else 0.0,
            }
        )

    active_row = db.row_to_dict(
        db.query_one(
            "SELECT j.id AS job_id, j.stage, j.page_id, j.prompt_id, j.attempt, p.title AS page_title, p.chapter_id "
            "FROM comic_jobs j JOIN comic_pages p ON p.id=j.page_id "
            "WHERE j.project_id=? AND j.status='running' ORDER BY j.id DESC LIMIT 1",
            (project_id,),
        )
    )
    queued_jobs = int(
        (db.query_one(
            "SELECT COUNT(*) AS n FROM comic_jobs WHERE project_id=? AND status='queued'",
            (project_id,),
        ) or {"n": 0})["n"] or 0
    )
    err_row = db.row_to_dict(
        db.query_one(
            "SELECT error_code, error_message, page_id, attempt FROM comic_jobs "
            "WHERE project_id=? AND status='failed' ORDER BY id DESC LIMIT 1",
            (project_id,),
        )
    )
    reasons: list[dict] = []
    for r in db.query_all(
        "SELECT error_code, COUNT(*) AS n FROM comic_jobs WHERE project_id=? AND status='failed' AND error_code<>'' "
        "GROUP BY error_code ORDER BY n DESC",
        (project_id,),
    ):
        reasons.append({"code": r["error_code"], "count": int(r["n"] or 0)})

    # 预计剩余时间：用最近若干已完成任务的实测耗时均值 × 剩余待出图任务数
    avg_seconds = _avg_job_seconds(project_id)
    remaining = queued_jobs + (1 if active_row else 0)
    eta_seconds = int(round(avg_seconds * remaining)) if (avg_seconds > 0 and remaining) else 0

    return {
        "project_id": project_id,
        "total": total,
        "counts": counts,
        "done": done,
        "failed": failed,
        "percent": round(done * 100.0 / total, 1) if total else 0.0,
        "chapters": chapters,
        "queued_jobs": queued_jobs,
        "active": active_row,
        "last_error": err_row,
        "failure_reasons": reasons,
        "characters": len(list_characters(project_id)),
        "avg_job_seconds": int(round(avg_seconds)),
        "eta_seconds": eta_seconds,
    }


def _avg_job_seconds(project_id: int, sample: int = 5) -> float:
    """最近 N 个已完成任务的实际耗时均值（秒）；样本不足返回 0。"""
    rows = db.query_all(
        "SELECT created_at, finished_at FROM comic_jobs WHERE project_id=? AND status='done' "
        "AND created_at<>'' AND finished_at<>'' ORDER BY id DESC LIMIT ?",
        (project_id, int(sample)),
    )
    spans: list[float] = []
    for r in rows:
        try:
            t0 = time.strptime(str(r["created_at"]), "%Y-%m-%d %H:%M:%S")
            t1 = time.strptime(str(r["finished_at"]), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        span = time.mktime(t1) - time.mktime(t0)
        if 0 < span < 3600:  # 忽略异常样本（时钟回拨 / 卡死超 1 小时）
            spans.append(span)
    if not spans:
        return 0.0
    return sum(spans) / len(spans)
