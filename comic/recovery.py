"""M12 · 漫画出图任务的「卡死」回收。

为什么需要这个模块
------------------
出图是跨进程的：AIBAR 把任务置 ``running`` / 页面置 ``generating``，然后交给
ComfyUI 慢慢跑（最长 3 分钟）。如果这期间 AIBAR 被 Ctrl-C、崩溃、断电或热重启，
数据库里的状态就永远停在那儿了 —— worker 重启后只捞 ``queued``，这些任务谁也不管。

更糟的是**卡住之后无路可退**：

- ``service._enqueue`` 见到 ``generating`` 直接拒绝（防重复入队）；
- ``enqueue_chapter`` / ``enqueue_project`` 用 ``status <> 'generating'`` 把它排除。

于是「单页重出图」和「整本出图」都会绕开这一页，用户只能改数据库。

本模块提供 ``reap_stale_jobs()``：找出超过阈值的 ``running`` 任务与没有活任务的
``generating`` 页面，把它们复位到「可以重新出图」的状态，并写清楚中文原因。
启动时自动跑一次，也可由用户在界面上手动触发。

M14 扩展：``reap_stale_frames()`` 同模式收 ``shot_frames`` 表 —— AIBAR 重启后
``_claim_frame`` 会拒绝接管卡在 ``generating`` 的帧（``status<>'generating'`` 守门），
没有 reap 机制就只能改 DB。补完这条就是「分镜有子片 + 组图有子片」的完整守护。
"""

from __future__ import annotations

from typing import Any

from core import db
from core.db import now
from core.logging_setup import get_logger, safe_log

_LOGGER = get_logger("aibar.comic.recovery")

# ComfyUI 单张出图正常在几十秒到几分钟，给足 15 分钟余量，避免误伤正在跑的任务。
STALE_RUNNING_SECONDS = 15 * 60

# 组图帧同样以 15 分钟为阈值；超出的「generating 帧」视为孤儿（见 reap_stale_frames）。
STALE_FRAME_SECONDS = 15 * 60

_INTERRUPTED_MESSAGE = "上次进程退出时中断，请重新出图"
_INTERRUPTED_FRAME_MESSAGE = "上次进程退出时中断，请在「动作帧」里点「重生成」"

# 仍在推进中的任务状态（有这些状态的任务存在时，页面不认为是卡住的）
_ACTIVE_JOB_STATUSES = ("queued", "running")


def reap_stale_jobs(
    max_age_seconds: int = STALE_RUNNING_SECONDS,
    project_id: int | None = None,
    requeue: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """回收卡死的出图任务与页面，返回处理摘要。

    Args:
        max_age_seconds: ``running`` 任务超过这个秒数未收尾即视为孤儿。
        project_id: 限定某个项目；None 表示全部。
        requeue: 是否把复位的页面直接重新入队（False 则留给用户在界面上点）。
        dry_run: 只统计不改动，用于体检预览。

    Returns:
        ``{"stale_jobs", "reset_pages", "requeued", "dry_run", "max_age_seconds", "job_ids", "page_ids"}``
    """
    age = max(1, int(max_age_seconds))
    params: list[Any] = []

    # 1) 卡在 running 的任务：超时的先标 failed（写清中文原因）
    job_sql = (
        "SELECT id, page_id, project_id FROM comic_jobs WHERE status='running' "
        "AND created_at IS NOT NULL AND created_at <= ?"
    )
    cutoff = _cutoff(age)
    params.append(cutoff)
    if project_id is not None:
        job_sql += " AND project_id=?"
        params.append(int(project_id))
    stale_jobs = db.rows_to_dicts(db.query_all(job_sql, params))

    # 2) 没有任何进行中任务、却停在 generating / queued 的页面 —— 同样是孤儿
    page_sql = (
        "SELECT p.id, p.project_id, p.status FROM comic_pages p "
        "WHERE p.status='generating' AND NOT EXISTS ("
        "  SELECT 1 FROM comic_jobs j WHERE j.page_id=p.id "
        "  AND j.status IN ('queued','running')"
        ")"
    )
    page_params: list[Any] = []
    if project_id is not None:
        page_sql += " AND p.project_id=?"
        page_params.append(int(project_id))
    orphan_pages = db.rows_to_dicts(db.query_all(page_sql, page_params))

    job_ids = [int(j["id"]) for j in stale_jobs]
    page_ids = sorted({int(p["id"]) for p in orphan_pages} | {int(j["page_id"]) for j in stale_jobs})

    if dry_run:
        return {
            "stale_jobs": len(job_ids),
            "reset_pages": len(page_ids),
            "requeued": 0,
            "dry_run": True,
            "max_age_seconds": age,
            "job_ids": job_ids,
            "page_ids": page_ids,
        }

    if job_ids:
        with db.tx():
            for job in stale_jobs:
                db.execute(
                    "UPDATE comic_jobs SET status='failed', stage='interrupted', "
                    "error_code='interrupted', error_message=?, finished_at=? "
                    "WHERE id=? AND status='running'",
                    (_INTERRUPTED_MESSAGE, now(), int(job["id"])),
                )

    # 页面复位为 pending —— 关键一步：不复位的话 _enqueue 与整本出图都会跳过它
    reset = 0
    if page_ids:
        with db.tx():
            for pid in page_ids:
                cur = db.execute(
                    "UPDATE comic_pages SET status='pending', error_message=? "
                    "WHERE id=? AND status IN ('generating','queued')",
                    (_INTERRUPTED_MESSAGE, pid),
                )
                try:
                    reset += int(cur.rowcount or 0)
                except Exception:
                    reset += 1

    requeued = 0
    if requeue and page_ids:
        from . import service

        for pid in page_ids:
            try:
                service.enqueue_page(pid)
                requeued += 1
            except Exception as exc:  # 单页失败不影响其余
                safe_log(_LOGGER, 30, "comic_reap_requeue_failed", page_id=pid, error_type=type(exc).__name__)

    if job_ids or page_ids:
        safe_log(
            _LOGGER, 30, "comic_stale_reaped",
            jobs=len(job_ids), pages=reset, requeued=requeued,
        )

    return {
        "stale_jobs": len(job_ids),
        "reset_pages": reset,
        "requeued": requeued,
        "dry_run": False,
        "max_age_seconds": age,
        "job_ids": job_ids,
        "page_ids": page_ids,
    }


def reap_stale_frames(
    max_age_seconds: int = STALE_FRAME_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """回收 ``shot_frames`` 表里卡在 ``generating`` 的孤儿帧。

    **为什么有这个需求**：组图的 ``_claim_frame`` 用 ``status<>'generating'`` 守门
    防止同一帧被两个 worker 抢跑。代价是：上一轮进程被杀时跑着的帧会永远停在
    ``generating``，重启后 ``/generate`` 与 ``/frames/<id>/regenerate`` 都被这道
    守门挡下，用户改不了也跑不了重出。这是 comic_pages 同类问题的组图片段：

    - comic_pages 有 ``reap_stale_jobs``（见上）兜底；
    - shot_frames **没有**——补完才有完整守护。

    行为：找出 ``status='generating' AND updated_at <= cutoff`` 的帧，复位成
    ``failed``（写清「上次进程退出时中断」原因）；对应组图的状态按其
    ``_sync_counts`` 重算一遍（之前的 ``generating`` 标记可能被这次 reap 推翻）。
    **不复位为 pending**：用户已经在界面上的图不该被偷偷重跑，需要重出请在
    「动作帧」弹窗里点「重生成」单帧触发。
    """
    age = max(1, int(max_age_seconds))
    cutoff = _cutoff(age)
    rows = db.rows_to_dicts(db.query_all(
        "SELECT id, group_id FROM shot_frames "
        "WHERE status='generating' AND updated_at IS NOT NULL AND updated_at<=?",
        (cutoff,),
    ))
    stale_ids = sorted({int(r["id"]) for r in rows})
    affected_groups = sorted({int(r["group_id"]) for r in rows})

    if dry_run:
        return {
            "stale_frames": len(stale_ids),
            "frame_ids": stale_ids,
            "group_ids": affected_groups,
            "dry_run": True,
            "max_age_seconds": age,
        }

    reset = 0
    if stale_ids:
        with db.tx():
            for r in rows:
                cur = db.execute(
                    "UPDATE shot_frames SET status='failed', error_message=?, updated_at=? "
                    "WHERE id=? AND status='generating'",
                    (_INTERRUPTED_FRAME_MESSAGE, now(), int(r["id"])),
                )
                try:
                    if int(cur.rowcount or 0) > 0:
                        reset += 1
                except Exception:
                    reset += 1

        # 复位帧状态后，所属组图的聚合状态需要重算（可能从 generating 跌到 partial/failed）
        if affected_groups:
            try:
                from comic import groups as _groups

                for gid in affected_groups:
                    _groups._sync_counts(gid)
            except Exception as exc:
                safe_log(_LOGGER, 30, "shot_frame_reap_sync_failed", error_type=type(exc).__name__)

    if reset:
        safe_log(_LOGGER, 30, "shot_frame_stale_reaped", frames=reset, groups=len(affected_groups))

    return {
        "stale_frames": reset,
        "frame_ids": stale_ids,
        "group_ids": affected_groups,
        "dry_run": False,
        "max_age_seconds": age,
    }

    if job_ids:
        with db.tx():
            for job in stale_jobs:
                db.execute(
                    "UPDATE comic_jobs SET status='failed', stage='interrupted', "
                    "error_code='interrupted', error_message=?, finished_at=? "
                    "WHERE id=? AND status='running'",
                    (_INTERRUPTED_MESSAGE, now(), int(job["id"])),
                )

    # 页面复位为 pending —— 关键一步：不复位的话 _enqueue 与整本出图都会跳过它
    reset = 0
    if page_ids:
        with db.tx():
            for pid in page_ids:
                cur = db.execute(
                    "UPDATE comic_pages SET status='pending', error_message=? "
                    "WHERE id=? AND status IN ('generating','queued')",
                    (_INTERRUPTED_MESSAGE, pid),
                )
                try:
                    reset += int(cur.rowcount or 0)
                except Exception:
                    reset += 1

    requeued = 0
    if requeue and page_ids:
        from . import service

        for pid in page_ids:
            try:
                service.enqueue_page(pid)
                requeued += 1
            except Exception as exc:  # 单页失败不影响其余
                safe_log(_LOGGER, 30, "comic_reap_requeue_failed", page_id=pid, error_type=type(exc).__name__)

    if job_ids or page_ids:
        safe_log(
            _LOGGER, 30, "comic_stale_reaped",
            jobs=len(job_ids), pages=reset, requeued=requeued,
        )

    return {
        "stale_jobs": len(job_ids),
        "reset_pages": reset,
        "requeued": requeued,
        "dry_run": False,
        "max_age_seconds": age,
        "job_ids": job_ids,
        "page_ids": page_ids,
    }


def _cutoff(age_seconds: int) -> str:
    """算出「早于这个时间的 running 任务即为孤儿」的时间戳字符串。

    库里的时间是 ``core.db.now()`` 写的本地时间字符串（``%Y-%m-%d %H:%M:%S``），
    可直接与字符串比较，无需在 SQL 里做日期运算。
    """
    import time as _time

    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - age_seconds))


def stale_summary(max_age_seconds: int = STALE_RUNNING_SECONDS) -> dict[str, Any]:
    """只读体检：当前有多少卡死的任务/页面（供界面提示用，不做任何改动）。"""
    return reap_stale_jobs(max_age_seconds=max_age_seconds, dry_run=True)


def cleanup_orphan_outputs(dry_run: bool = False) -> dict[str, Any]:
    """清理 ``data/comic_outputs`` 下已无对应分镜页的孤儿文件。

    删除页/章节/项目时我们已同步删文件，但历史上留下的、以及进程中途崩溃产生的
    孤儿文件仍会堆积（实测现网 92 个产出文件里有 6 个是孤儿）。这里按
    「文件名前缀 = 分镜页 ID」反查页面表，查不到的就删。

    Args:
        dry_run: 只统计不删除。

    Returns:
        ``{"scanned", "removed", "bytes", "dry_run", "samples"}``
    """
    from pathlib import Path

    from config import Config

    root = Path(Config.DATA_DIR) / "comic_outputs"
    scanned = removed = total_bytes = 0
    samples: list[str] = []
    if not root.is_dir():
        return {"scanned": 0, "removed": 0, "bytes": 0, "dry_run": dry_run, "samples": []}

    alive = {
        int(r["id"])
        for r in db.rows_to_dicts(db.query_all("SELECT id FROM comic_pages"))
    }
    # 演员定妆图与组图产出图都存在 comic_outputs 下，命名同样是 ``<id>_<hash>.png``，
    # 但这里的 id 分别来自 actors / shot_frames 两个表，与 comic_pages 是三个
    # **互不相干**的编号空间。不在 skip_dirs 列出的话，只要 comic_pages 里碰巧
    # 没有同号页面，演员图/组图片就会被判定成孤儿删掉 —— 每次启动都删一次，
    # 演员库与组图模块会莫名其妙地掉图。这是 2026-09-02 真实线上事故：
    # ``recovery.comic_orphan_outputs_removed removed=3 bytes=4542813`` 把
    # 刚生成的 3 张组图产出（每张 ~1.4MB）全删干净，DB 记录完好、磁盘空空，
    # 用户打开播放器全部空白。
    skip_dirs: set[str] = set()
    try:
        from comic.actors import ACTOR_OUTPUT_SUBDIR
        skip_dirs.add(str(ACTOR_OUTPUT_SUBDIR))
    except Exception:  # 导入失败也要保住默认行为，绝不因为兜底逻辑挂掉启动
        skip_dirs.add("actors")
    try:
        from comic.groups import GROUPS_OUTPUT_SUBDIR
        skip_dirs.add(str(GROUPS_OUTPUT_SUBDIR))
    except Exception:
        skip_dirs.add("shotgroups")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if len(rel_parts) > 1 and rel_parts[0] in skip_dirs:
            continue
        scanned += 1
        stem = path.name.split("_")[0]
        if stem.isdigit() and int(stem) in alive:
            continue
        # 非 ``<page_id>_<hash>.png`` 命名的文件不动，避免误删用户自己放的东西
        if not stem.isdigit() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            removed += 1
            total_bytes += size
            if len(samples) < 10:
                samples.append(str(path.relative_to(root)))
        except OSError as exc:
            safe_log(_LOGGER, 30, "comic_orphan_unlink_failed", error_type=type(exc).__name__)

    # 顺手清掉空目录（删完文件的章节目录会变成空壳）。演员目录即使空了也保留，
    # 否则下次生成定妆图时目录不存在，又要依赖调用方补建。
    if not dry_run:
        for d in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if not d.is_dir() or d.name in skip_dirs:
                    continue
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # dry_run 只是统计，别打「已删除」的日志误导人
    if removed and not dry_run:
        safe_log(_LOGGER, 20, "comic_orphan_outputs_removed", removed=removed, bytes=total_bytes)
    return {
        "scanned": scanned,
        "removed": removed,
        "bytes": total_bytes,
        "dry_run": dry_run,
        "samples": samples,
    }
