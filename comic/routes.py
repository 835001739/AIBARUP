"""M12 · 漫画工作室 REST 接口（url_prefix=/api/comic）。

所有接口使用统一 envelope：成功 ``{ok:true,data:...}``，失败 ``{ok:false,error:{code,message}}``。
业务异常抛 ``core.errors.AIBARError``，由本蓝图（与 app.py 兜底）统一转成响应。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, request, send_from_directory

from config import Config
from core import db
from core.errors import AIBARError
from core.responses import ok
from . import actors
from . import service
from . import skill_import
from . import diagnostics
from . import storyboard
from . import styles
from . import groups

bp = Blueprint("comic", __name__, url_prefix="/api/comic")


def _json_body() -> dict:
    """解析请求 JSON 体；非对象体统一视为 400（P1-9：预期解析错误不伪装成 500）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AIBARError("invalid_input", "请求体必须是 JSON 对象")
    return data


# ---------------------------------------------------------------- 漫画（概览）


@bp.get("/projects")
def projects_list():
    return ok({"items": service.list_projects()})


@bp.post("/projects")
def project_create():
    return ok(service.create_project(_json_body()))


@bp.get("/projects/<int:project_id>")
def project_get(project_id: int):
    return ok(service.get_project(project_id))


@bp.patch("/projects/<int:project_id>")
def project_update(project_id: int):
    return ok(service.update_project(project_id, _json_body()))


@bp.delete("/projects/<int:project_id>")
def project_delete(project_id: int):
    service.delete_project(project_id)
    return ok({})


# ---------------------------------------------------------------- 从 Skill 导入工作流


@bp.post("/import-skill")
def project_import_skill():
    """把 SKILL.md 的流程转换为漫画工作室工作流（project→chapter→page）。

    请求体：``{path?: str, markdown?: str, title?: str, workflow_filename?: str}``。
    ``path`` 与 ``markdown`` 至少其一；``path`` 优先。省略 ``workflow_filename`` 时，
    若本机 ComfyUI 工作流目录可用，会自动写入一份 FLUX.2 最小化工作流作为默认出图工作流。
    """
    body = _json_body()
    path = (body.get("path") or "").strip()
    text = (body.get("markdown") or "").strip()
    if path and not text:
        from pathlib import Path

        p = Path(path)
        if not p.is_file():
            raise AIBARError("not_found", f"skill 文件不存在：{path}")
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise AIBARError("read_error", f"读取 skill 失败：{exc}")
    if not text.strip():
        raise AIBARError("invalid_input", "请提供 skill 的 markdown 内容或 path")
    title = (body.get("title") or "").strip() or None
    workflow_filename = (body.get("workflow_filename") or "").strip() or None
    return ok(skill_import.import_skill(text, title=title, workflow_filename=workflow_filename))


# ---------------------------------------------------------------- 分镜工作流（世界观 + 剧情摘要 → 自动分集出图）


@bp.get("/style-presets")
def style_presets():
    """出图风格清单：``{items:[{key,label,desc}], default:"none"}``，供前端下拉渲染。

    每一页出图提示词都会按所选风格统一规范（追加同一套风格描述词与排除项），
    从而保证整部漫画的每一集风格一致。
    """
    return ok({"items": styles.style_options(), "default": styles.DEFAULT_STYLE})


@bp.post("/projects/<int:project_id>/storyboard")
def project_storyboard(project_id: int):
    """漫画分镜工作流：用项目的世界观 / 剧情摘要自动拆分分集，把剧情转换为每页图片提示词并入队出图。

    请求体（全部可选）：
    ``{profile?, intensity?, provider?, worldview?, plot_summary?, pages_per_chapter?, style?}``。
    - ``worldview`` / ``plot_summary`` 若提供，会先写回项目（编辑器一键生成时复用当前编辑框内容）；
    - ``pages_per_chapter`` 控制每集（章节）拆出的漫画页数，省略时取项目保存的设置（默认 1）；
    - ``style`` 出图风格（``none`` / ``japanese_manga`` / ``shinkai`` / ``american_comic`` /
      ``ink_wash`` / ``watercolor`` / ``cyberpunk`` / ``pixel_art`` / ``realistic``，清单见
      ``GET /api/comic/style-presets``）；传入时写回项目并**统一套用到每一集的每一页**提示词，
      省略时取项目保存的 ``style_preset``（默认 ``none`` = 不限制）；
    - ``profile`` / ``intensity`` 透传给提示词扩写引擎（默认 generic / balanced）；
    - ``provider`` 省略时按 ``AI_PROVIDER_*`` 是否启用自动选择（启用则 ``openai_compatible``，否则 ``rules``）；
      AI 拆分或扩写失败都会自动回退规则引擎，整体不失败；
    - ``mode`` 为 ``append``（默认，新分集追加在后面）或 ``rebuild``
      （先删除该项目已有章节再重建，避免反复生成叠加出重复分集）。
    """
    body = _json_body()
    profile = (body.get("profile") or "").strip() or None
    intensity = (body.get("intensity") or "").strip() or None
    provider = (body.get("provider") or "").strip() or None
    ppc_raw = body.get("pages_per_chapter")
    pages_per_chapter = None
    if ppc_raw not in (None, ""):
        try:
            pages_per_chapter = int(ppc_raw)
        except (TypeError, ValueError):
            pages_per_chapter = None
    style = (body.get("style") or "").strip() or None
    return ok(
        storyboard.generate_storyboard(
            project_id,
            profile=profile,
            intensity=intensity,
            provider=provider,
            worldview=body.get("worldview"),
            plot_summary=body.get("plot_summary"),
            pages_per_chapter=pages_per_chapter,
            style=style,
            mode=body.get("mode"),
        )
    )


@bp.post("/projects/<int:project_id>/refresh-prompts")
def project_refresh_prompts(project_id: int):
    """按**当前**角色卡 / 世界观 / 出图风格，重算项目所有分镜页的提示词与种子。

    每一页保存了生成时的原始扩写结果，因此改完角色外貌或换风格后，
    不必重新拆分剧情，调用本接口即可让全本提示词同步生效。

    请求体（全部可选）：
    ``{style?, requeue?}``
    - ``style`` 覆盖出图风格并写回项目；省略则沿用项目已保存的风格；
    - ``requeue`` 重算后自动重新出图的范围：``failed``（默认，只重跑失败的页）/
      ``all``（全部重跑）/ ``none``（只改提示词，不出图）。
    """
    body = _json_body()
    style = (body.get("style") or "").strip() or None
    requeue = (body.get("requeue") or "").strip() or "failed"
    return ok(storyboard.refresh_project_prompts(project_id, style=style, requeue=requeue))


# ---------------------------------------------------------------- 角色卡（人物一致性）


@bp.get("/projects/<int:project_id>/characters")
def characters_list(project_id: int):
    """角色卡列表：``{items:[{id,name,aliases,appearance,outfit,palette,negative,seed_offset,anchor}]}``。

    ``anchor`` 是注入到每一页提示词的锚点文本（同一角色逐字相同），用于前端预览一致性效果。
    """
    return ok({"items": service.list_characters(project_id)})


@bp.post("/projects/<int:project_id>/characters")
def character_create(project_id: int):
    return ok(service.create_character(project_id, _json_body()))


@bp.post("/projects/<int:project_id>/characters/extract")
def characters_extract(project_id: int):
    """从项目世界观 / 剧情摘要补充角色卡（只新增缺失的，不覆盖已编辑的卡片）。"""
    body = _json_body()
    project = service.get_project(project_id)
    worldview = body.get("worldview") if body.get("worldview") is not None else project.get("worldview") or ""
    plot = body.get("plot_summary") if body.get("plot_summary") is not None else project.get("plot_summary") or ""
    return ok(service.sync_characters(project_id, str(worldview), str(plot)))


@bp.patch("/characters/<int:character_id>")
def character_update(character_id: int):
    return ok(service.update_character(character_id, _json_body()))


@bp.delete("/characters/<int:character_id>")
def character_delete(character_id: int):
    service.delete_character(character_id)
    return ok({})


# ---------------------------------------------------------------- 演员库（M13 · 跨漫画人物一致性基准）


@bp.get("/actors")
def actors_list():
    """演员列表：``{items:[...], total:n}``。

    每个演员含 ``anchor``（定妆锚点，与分镜注入的文本逐字一致）、``image_url``
    （定妆图）、``characters``（出演的漫画角色）与 ``link_count``。
    query: ``keyword`` 模糊匹配名字/别名，``limit`` / ``offset`` 分页。
    """
    keyword = request.args.get("keyword") or None
    limit = request.args.get("limit")
    offset = request.args.get("offset")
    lim = int(limit) if limit and str(limit).isdigit() else actors.DEFAULT_LIMIT
    off = int(offset) if offset and str(offset).isdigit() else 0
    return ok(
        {
            "items": actors.list_actors(keyword=keyword, limit=lim, offset=off),
            "total": actors.count_actors(keyword=keyword),
        }
    )


@bp.post("/actors")
def actor_create():
    """新增演员。

    body 三种来源任选：
    - 手填：``{name, appearance?, outfit?, palette?, negative?, notes?}``；
    - 从图库收人：``{name, image_id}``（外貌留空时自动用该图出图提示词填充）；
    - 从漫画角色提升：``{character_id}``（名字默认沿用角色名，并自动建立演员↔角色关联）。
    """
    return ok(actors.create_actor(_json_body()))


@bp.get("/actors/<int:actor_id>")
def actor_get(actor_id: int):
    return ok(actors.get_actor(actor_id))


@bp.patch("/actors/<int:actor_id>")
def actor_update(actor_id: int):
    """修改演员定妆信息。默认把改动同步给全部关联角色（``apply_to_characters:false`` 可关闭）。"""
    return ok(actors.update_actor(actor_id, _json_body()))


@bp.delete("/actors/<int:actor_id>")
def actor_delete(actor_id: int):
    actors.delete_actor(actor_id)
    return ok({})


@bp.post("/actors/<int:actor_id>/regenerate")
def actor_regenerate(actor_id: int):
    """重新生成演员定妆图（后台执行，立即返回 ``status='generating'``）。

    body 可选 ``{workflow?, prompt?, randomize?, seed?}``。``randomize`` 为真才换脸，
    否则沿用确定性种子——同一个演员反复重生成仍是同一张脸。
    """
    return ok(actors.regenerate_actor(actor_id, _json_body() if request.data else {}))


@bp.post("/actors/<int:actor_id>/links")
def actor_link(actor_id: int):
    """把演员关联到一个漫画角色：``{character_id, role_note?, apply?}``。

    ``apply`` 默认 true，即把演员定妆信息写进该角色卡（以演员为基准保持一致性）。
    """
    return ok(actors.link_character(actor_id, _json_body()))


@bp.delete("/actors/<int:actor_id>/links/<int:character_id>")
def actor_unlink(actor_id: int, character_id: int):
    return ok(actors.unlink_character(actor_id, character_id))


@bp.post("/actors/<int:actor_id>/apply")
def actor_apply(actor_id: int):
    """把演员当前定妆信息推送给全部关联角色（批量校正入口）。"""
    return ok(actors.apply_to_characters(actor_id))


@bp.post("/projects/<int:project_id>/characters/from-actor")
def character_from_actor(project_id: int):
    """以演员为基准，在该漫画里新建角色卡并自动关联：``{actor_id, name?, role_note?, is_main?}``。"""
    return ok(actors.create_character_from_actor(project_id, _json_body()))


# ---------------------------------------------------------------- 进度与诊断（ComfyUI 衔接）


@bp.get("/projects/<int:project_id>/progress")
def project_progress(project_id: int):
    """出图进度总览：各状态页数、完成百分比、章节级进度、当前生成中页、最近失败原因。

    附带 ``comfyui`` 在线状态（只读探测，失败降级为 offline，不影响进度数据）。
    """
    data = service.project_progress(project_id)
    try:
        data["comfyui"] = diagnostics.probe_comfyui()
    except Exception:
        data["comfyui"] = {"online": False, "latency_ms": 0, "base_url": ""}
    # 队列 worker 的存活状态：它要是死了，所有页面会永远停在 queued 且界面毫无异常
    try:
        from .runner import worker_status

        data["worker"] = worker_status()
    except Exception:
        data["worker"] = {"alive": False, "stale_seconds": None, "polls": 0, "restarts": 0}
    # 卡死任务统计（只读）：驱动界面上的「重置并重新出图」入口
    try:
        from . import recovery

        data["stale"] = recovery.stale_summary()
    except Exception:
        data["stale"] = {"stale_jobs": 0, "reset_pages": 0, "dry_run": True}
    return ok(data)


@bp.get("/error-codes")
def error_codes():
    """错误码 → 中文文案表（前端展示用，避免把机器码直接甩给用户）。

    单一数据源在 ``core.error_text``（全项目共用，反推模块也用同一批 ComfyUI 错误码）。
    """
    from core.error_text import ERROR_TEXT

    # 直接返回「码→文案」映射本体（不是 ``{"items": ...}``）：
    # 前端拿到就能当字典查，少一层解包也少一处记错的机会。
    return ok(ERROR_TEXT)


@bp.get("/maintenance/stale")
def maintenance_stale():
    """只读体检：当前有多少「卡死」的出图任务与页面（不做任何改动）。"""
    from . import recovery

    return ok(recovery.stale_summary())


@bp.post("/maintenance/reap")
def maintenance_reap():
    """回收卡死的出图任务与页面（上次进程被杀留下的 running / generating）。

    body: ``{"requeue": true}`` 可在复位后直接重新入队；``{"dry_run": true}`` 只统计不改动。
    """
    from . import recovery

    body = _json_body()
    requeue = str(body.get("requeue") or "").strip().lower() in ("1", "true", "yes", "on")
    dry_run = str(body.get("dry_run") or "").strip().lower() in ("1", "true", "yes", "on")
    max_age = body.get("max_age_seconds")
    kwargs = {"requeue": requeue, "dry_run": dry_run}
    if max_age is not None:
        kwargs["max_age_seconds"] = int(max_age)
    return ok(recovery.reap_stale_jobs(**kwargs))


# ---------------------------------------------------------------- 组图姿势（M14+ · ControlNet OpenPose + IPAdapter FaceID）


@bp.get("/poses")
def poses_list():
    """预置姿势清单（前端下拉框使用）。"""
    from . import pose

    return ok({"items": pose.list_poses()})


@bp.post("/poses/ensure")
def poses_ensure():
    """把全部预置姿势骨架 PNG 落盘到 ComfyUI ``input/aibar_poses/``，并落盘工作流。

    幂等：同名 PNG 已存在且字节相同则跳过；工作流文件每次覆盖写。
    """
    from . import pose

    lib = pose.ensure_pose_library()
    wf = pose.ensure_pose_workflow()
    return ok({"library": lib, "workflow": wf})


@bp.post("/poses/custom")
def poses_custom():
    """用户上传 18 点 keypoints，渲染并落盘为自定义姿势，同时加入内存中的 ``POSES``。

    body ``{key, keypoints: [[x,y]*18], label?, desc?}``，key 必填且唯一。
    """
    from . import pose

    body = _json_body()
    key = str(body.get("key") or "").strip()
    kps = body.get("keypoints") or []
    if not key or not isinstance(kps, list) or len(kps) < 18:
        raise AIBARError("invalid_input", "需要 key 与 18 个关键点")
    result = pose.add_custom_pose(
        key,
        [[float(p[0]), float(p[1])] for p in kps],
        label=str(body.get("label") or "").strip() or key,
        desc=str(body.get("desc") or "").strip() or "自定义姿势",
    )
    if not result.get("ok"):
        raise AIBARError("render_failed", result.get("reason") or "渲染失败")
    return ok(result)


@bp.get("/poses/workflow")
def poses_workflow():
    """返回工作流 JSON 文本（前端预览 / 嵌入使用，文件名固定）。"""
    from . import pose
    import json as _json

    g = pose.build_pose_workflow()
    return ok({"workflow": _json.dumps(g, ensure_ascii=False)})


def _first_pose_asset() -> str:
    """``input/aibar_poses/`` 下第一张骨架图（``pose_*.png``），没有则返回空串。

    用途：构造纯 openpose 工作流时，用户没选骨架就替他选一张——否则落出来的
    是 7 节点裸版（无控姿链），对用户毫无价值。
    """
    from . import pose as _pose
    from sync import paths as _sync_paths

    try:
        root = _sync_paths.input_dir()
        sub = (root / _pose.POSE_SUBDIR) if root else None
        if sub and sub.is_dir():
            first = next(iter(sorted(sub.glob("pose_*.png"))), None)
            if first:
                return f"{_pose.POSE_SUBDIR}/{first.name}"
    except Exception:  # noqa: BLE001
        pass
    return ""


@bp.get("/poses/assets")
def poses_assets():
    """列出 ``input/aibar_poses/`` 下的可用素材，供前端下拉框选择。

    - ``references``：参考图（锁脸用），命名约定 ``actor_*`` / ``ref_*`` / ``face_*``；
    - ``poses``：骨架图（控姿用），命名约定 ``pose_*``。

    目录不可用时返回两个空数组（不报错）——页面只是少几个下拉选项。
    """
    from . import pose as _pose
    from sync import paths as _sync_paths

    refs: list[dict[str, Any]] = []
    poses: list[dict[str, Any]] = []
    try:
        root = _sync_paths.input_dir()
        sub = (root / _pose.POSE_SUBDIR) if root else None
        if sub and sub.is_dir():
            for p in sorted(sub.glob("*.png")):
                name = p.name
                if name.startswith(("actor_", "ref_", "face_")):
                    refs.append({"name": name, "value": f"{_pose.POSE_SUBDIR}/{name}"})
                elif name.startswith("pose_"):
                    poses.append({"name": name, "value": f"{_pose.POSE_SUBDIR}/{name}"})
    except Exception:  # noqa: BLE001
        pass
    return ok({"references": refs, "poses": poses})


@bp.post("/poses/build-workflow")
def poses_build_workflow():
    """生成 SDXL + ControlNet-Union openpose 工作流并落盘（两个变体）。

    body::

        {
          "variant": "consistent" | "openpose",
          "reference_image": "aibar_poses/actor_ref.png",   # 仅 consistent 需要
          "pose_image": "aibar_poses/pose_0000.png",
          "positive": "...", "negative": "..."
        }

    - ``consistent``：锁脸 + 控姿（15 节点）；
    - ``openpose``：纯 openpose 控姿（11 节点）——**必须 ``auto_fill=False``**，
      否则留空的参考图会被自动扫描补上，落出来还是 15 节点锁脸版。

    Returns:
        ``{filename, nodes, variant, workflow}``；目录不可用时 ``filename`` 为 None。
    """
    from . import pose

    body = _json_body()
    variant = str(body.get("variant") or "consistent").strip().lower()
    if variant not in ("consistent", "openpose"):
        raise AIBARError("invalid_input", "variant 只能是 consistent 或 openpose")

    positive = str(body.get("positive") or "").strip()
    negative = str(body.get("negative") or "").strip()
    pose_image = str(body.get("pose_image") or "").strip()

    # openpose 变体下骨架图不能留空：auto_fill=False 会把「补参考图」和「补骨架」
    # 一起关掉，两张都空会落出 **7 节点裸版**（既无锁脸也无控姿 = 废工作流）。
    # 用户点「生成 openpose 工作流」就是要控姿，这里替他选一张。
    if variant == "openpose" and not pose_image:
        pose_image = _first_pose_asset()

    if variant == "openpose":
        # 纯控姿：绝不给 reference_image，且必须关掉 auto_fill
        filename = pose.ensure_pose_workflow(
            pose_image=pose_image,
            positive=positive,
            negative=negative,
            filename=pose.POSE_OPENPOSE_FILENAME,
            auto_fill=False,
        )
    else:
        filename = pose.ensure_pose_workflow(
            reference_image=str(body.get("reference_image") or "").strip(),
            pose_image=pose_image,
            positive=positive,
            negative=negative,
        )

    nodes = 0
    if filename:
        try:
            from sync import paths as _sync_paths
            import json as _json

            target = _sync_paths.workflows_dir()
            if target:
                nodes = len(_json.loads((target / filename).read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            nodes = 0
    return ok({"filename": filename, "nodes": nodes, "variant": variant})


@bp.get("/maintenance/frames-stale")
def maintenance_frames_stale():
    """只读体检：当前有多少「卡死」的组图帧（``generating`` 超过阈值），不做任何改动。

    与 ``GET /maintenance/stale`` 同款——漫画工作流的卡死体检在那个路由，组图模块的
    卡死体检在这个路由。两类实体的「id 命名空间」不同（page_id vs frame_id），
    复用会误判，故分别暴露。
    """
    from . import recovery

    body = request.args if request.args else {}
    kwargs: dict[str, Any] = {}
    if body.get("max_age_seconds"):
        kwargs["max_age_seconds"] = int(body["max_age_seconds"])
    return ok(recovery.reap_stale_frames(dry_run=True, **kwargs))


@bp.post("/maintenance/frames-reap")
def maintenance_frames_reap():
    """手动回收卡死的组图帧（``status='generating'`` 超过阈值）。

    与 ``POST /maintenance/reap`` 同款：漫画任务在那个路由，组图帧在这个路由。
    body 可选：
    - ``max_age_seconds`` 覆盖默认 15 分钟阈值（生产环境紧急清理时可调小）；
    - ``dry_run=true`` 只统计不改动。

    复位后的帧状态变为 ``failed``（带「上次进程退出时中断，请在「动作帧」里点「重生成」」
    原因）——不自动入队，避免偷偷重跑用户已经认可的图。需要重出请在界面上手动触发。
    """
    from . import recovery

    body = _json_body() if request.data else {}
    kwargs: dict[str, Any] = {}
    dry_run = str(body.get("dry_run") or "").strip().lower() in ("1", "true", "yes", "on")
    if body.get("max_age_seconds") is not None:
        kwargs["max_age_seconds"] = int(body["max_age_seconds"])
    return ok(recovery.reap_stale_frames(dry_run=dry_run, **kwargs))


@bp.get("/projects/<int:project_id>/precheck")
def project_precheck(project_id: int):
    """出图前置体检：ComfyUI 是否在线、工作流是否存在、节点是否齐全、队列是否拥堵。"""
    check = request.args.get("check_nodes", "1").strip().lower() not in ("0", "false", "no")
    return ok(diagnostics.precheck(project_id, check_nodes=check))


@bp.get("/pages/<int:page_id>/editor-link")
def page_editor_link(page_id: int):
    """生成「在 ComfyUI 中打开该分镜」的深链接（载入工作流 + 该页提示词 + 种子）。"""
    from sync import comfyui_link

    page = service.get_page(page_id)
    project = service.get_project(page["project_id"])
    workflow = (page.get("workflow_filename") or project.get("default_workflow") or "").strip()
    if not workflow:
        raise AIBARError("invalid_input", "该分镜未指定工作流，无法在 ComfyUI 中打开")
    data = comfyui_link.build_editor_link(
        workflow=workflow,
        prompt=page.get("prompt_text") or "",
        negative=page.get("negative_text") or "",
    )
    return ok(data)


# ---------------------------------------------------------------- 章节管理


@bp.get("/projects/<int:project_id>/chapters")
def chapters_list(project_id: int):
    return ok({"items": service.list_chapters(project_id)})


@bp.post("/projects/<int:project_id>/chapters")
def chapter_create(project_id: int):
    return ok(service.create_chapter(project_id, _json_body()))


@bp.get("/chapters/<int:chapter_id>")
def chapter_get(chapter_id: int):
    return ok(service.get_chapter(chapter_id))


@bp.patch("/chapters/<int:chapter_id>")
def chapter_update(chapter_id: int):
    return ok(service.update_chapter(chapter_id, _json_body()))


@bp.delete("/chapters/<int:chapter_id>")
def chapter_delete(chapter_id: int):
    service.delete_chapter(chapter_id)
    return ok({})


# ---------------------------------------------------------------- 分镜（预制提示词 + 出图工作流）


@bp.get("/projects/<int:project_id>/pages")
def project_pages_list(project_id: int):
    """项目下全部分镜页（跨章节聚合），供「按条件批量重出图」统计景别/角色。"""
    return ok({"items": service.list_project_pages(project_id)})


@bp.post("/projects/<int:project_id>/sync-gallery")
def project_sync_gallery(project_id: int):
    """把该项目已出图、但还没进图库的历史分镜页补登记进图库。

    登记后才能对这些图做提示词反推、在图库里检索/深链打开。
    图库登记是后加的能力，改造前出好的图需要靠这里回溯。
    """
    return ok(service.sync_gallery(project_id))


@bp.post("/sync-gallery")
def sync_gallery_all():
    """全库补登记（不限项目）。"""
    return ok(service.sync_gallery(None))


@bp.get("/chapters/<int:chapter_id>/pages")
def pages_list(chapter_id: int):
    return ok({"items": service.list_pages(chapter_id)})


@bp.post("/chapters/<int:chapter_id>/pages")
def page_create(chapter_id: int):
    return ok(service.create_page(chapter_id, _json_body()))


@bp.get("/pages/<int:page_id>")
def page_get(page_id: int):
    return ok(service.get_page(page_id))


@bp.patch("/pages/<int:page_id>")
def page_update(page_id: int):
    return ok(service.update_page(page_id, _json_body()))


@bp.delete("/pages/<int:page_id>")
def page_delete(page_id: int):
    service.delete_page(page_id)
    return ok({})


# ---------------------------------------------------------------- 出图队列（与 ComfyUI 对接）


@bp.post("/pages/<int:page_id>/generate")
def page_generate(page_id: int):
    return ok(service.enqueue_page(page_id))


@bp.post("/pages/<int:page_id>/regenerate")
def page_regenerate(page_id: int):
    """大工作流完成后，对单张分镜重新生成图片。"""
    return ok(service.regenerate_page(page_id))


@bp.post("/pages/<int:page_id>/reexpand")
def page_reexpand(page_id: int):
    """单页重扩写提示词。

    与项目级「重刷提示词」不同：这里会重跑一次扩写模型，适合对某一页的
    扩写结果不满意、又不想重跑全部分镜的场景。body 可选
    ``provider`` / ``profile`` / ``intensity`` / ``requeue``。
    """
    from . import storyboard

    body = _json_body()
    return ok(
        storyboard.reexpand_page(
            page_id,
            provider=body.get("provider") or None,
            profile=body.get("profile") or None,
            intensity=body.get("intensity") or None,
            requeue=bool(body.get("requeue")),
        )
    )


@bp.post("/chapters/<int:chapter_id>/generate")
def chapter_generate(chapter_id: int):
    return ok(service.enqueue_chapter(chapter_id))


@bp.post("/projects/<int:project_id>/generate")
def project_generate(project_id: int):
    """整本出图。

    body 可选过滤条件，用于「按条件批量重出图」而不必整本重跑：
    ``{"statuses": ["failed"]}`` 只重跑失败页；``{"page_ids": [1,2,3]}`` 只跑指定页；
    ``{"character": "阿岚"}`` 只跑出现某角色的页；``{"shot": "近景"}`` 只跑某景别。
    """
    body = _json_body()
    page_ids = _filter_pages(project_id, body)
    statuses = body.get("statuses")
    statuses = [str(s).strip() for s in statuses] if isinstance(statuses, list) else None
    return ok(service.enqueue_project(project_id, page_ids=page_ids, statuses=statuses))


def _filter_pages(project_id: int, body: dict) -> list[int] | None:
    """按角色 / 景别把「整本出图」收敛到符合条件的页；无条件时返回 None（表示全部）。"""
    character = str(body.get("character") or "").strip()
    shot = str(body.get("shot") or "").strip()
    if not character and not shot:
        raw = body.get("page_ids")
        if isinstance(raw, list) and raw:
            return [int(i) for i in raw if str(i).strip().isdigit()]
        return None
    sql = "SELECT id FROM comic_pages WHERE project_id=?"
    params: list[Any] = [project_id]
    if character:
        sql += " AND (character_names LIKE ? OR base_prompt LIKE ?)"
        params.extend(["%" + character + "%", "%" + character + "%"])
    if shot:
        sql += " AND shot_note LIKE ?"
        params.append("%" + shot + "%")
    rows = db.query_all(sql, params)
    return [int(r["id"]) for r in rows]


@bp.get("/jobs")
def jobs_list():
    project_id = request.args.get("project_id")
    status = request.args.get("status")
    limit = request.args.get("limit")
    pid = int(project_id) if project_id and str(project_id).isdigit() else None
    lim = int(limit) if limit and str(limit).isdigit() else service.MAX_JOBS
    return ok({"items": service.list_jobs(pid, status, lim)})


@bp.post("/jobs/<int:job_id>/cancel")
def job_cancel(job_id: int):
    return ok(service.cancel_job(job_id))


# ---------------------------------------------------------------- 组图（M14 · 同一人物的连贯动作序列 + 连播）


@bp.get("/groups")
def groups_list():
    """组图列表：``{items:[...], total:n}``。

    query: ``keyword`` 模糊匹配名字/说明，``limit`` / ``offset`` 分页。
    """
    keyword = request.args.get("keyword") or None
    limit = request.args.get("limit")
    offset = request.args.get("offset")
    lim = int(limit) if limit and str(limit).isdigit() else groups.DEFAULT_LIMIT
    off = int(offset) if offset and str(offset).isdigit() else 0
    return ok(
        {
            "items": groups.list_groups(keyword=keyword, limit=lim, offset=off),
            "total": groups.count_groups(keyword=keyword),
        }
    )


@bp.post("/groups")
def group_create():
    """新建组图。

    body 关键字段：
    - ``name`` / ``description``；
    - ``actor_id`` 绑定演员（人物锚点来源，强烈建议填）；
    - ``preset_prefix`` / ``preset_suffix`` / ``preset_negative`` **强制预设提示词**；
    - ``actions`` 首批动作文本数组（一行一个动作），或 ``action_text`` 按行拆分；
    - ``seed_step``（默认 0）、``frame_interval``（连播毫秒）、``loop_play``。
    """
    return ok(groups.create_group(_json_body()))


@bp.get("/groups/<int:group_id>")
def group_get(group_id: int):
    """组图详情（含全部帧与解析后的人物锚点）。"""
    return ok(groups.get_group(group_id))


@bp.patch("/groups/<int:group_id>")
def group_update(group_id: int):
    """修改组图预设。``refresh:true`` 时按新预设重算全部帧提示词（已出的图不清）。"""
    return ok(groups.update_group(group_id, _json_body()))


@bp.delete("/groups/<int:group_id>")
def group_delete(group_id: int):
    groups.delete_group(group_id)
    return ok({})


@bp.put("/groups/<int:group_id>/frames")
def group_frames_set(group_id: int):
    """**整组替换**动作帧：``{actions:[...]}`` 或 ``{action_text:"多行文本"}``。

    已出图的帧保留（连图一起），只有动作文本与提示词被更新；序列变短时多余旧帧删除。
    """
    return ok(groups.set_frames(group_id, groups.parse_actions(_json_body())))


@bp.post("/groups/<int:group_id>/refresh-prompts")
def group_refresh_prompts(group_id: int):
    """按当前预设重算全部帧提示词与种子（「强制」的执行点）。"""
    return ok(groups.refresh_prompts(group_id))


@bp.post("/groups/<int:group_id>/generate")
def group_generate(group_id: int):
    """整组出图（后台串行，立即返回进度）。

    body 可选：``workflow``、``only_missing``（默认 true，只出没出过/失败的帧）、
    ``frame_ids``（只出指定帧）。
    """
    return ok(groups.generate_group(group_id, _json_body() if request.data else {}))


@bp.post("/groups/<int:group_id>/cancel")
def group_cancel(group_id: int):
    """取消整组出图：worker 会在**当前帧结束后**停下（不硬杀线程，避免状态卡在 generating）。"""
    return ok(groups.cancel_group(group_id))


@bp.get("/groups/<int:group_id>/progress")
def group_progress(group_id: int):
    """出图进度快照，供前端轮询：``{status,total,done,failed,pending,generating,running}``。"""
    return ok(groups.group_progress(group_id))


@bp.get("/groups/<int:group_id>/playlist")
def group_playlist(group_id: int):
    """连播清单：按帧序返回已出图的帧，前端全量预加载后逐帧切换（像 GIF 一样播）。"""
    return ok(groups.playlist(group_id))


@bp.patch("/frames/<int:frame_id>")
def frame_update(frame_id: int):
    """改单帧：``{action_text}``（提示词恒由预设重算）或 ``{order_idx}``（调序）。"""
    return ok(groups.update_frame(frame_id, _json_body()))


@bp.delete("/frames/<int:frame_id>")
def frame_delete(frame_id: int):
    """删单帧，后续帧序整体前移。"""
    return ok(groups.delete_frame(frame_id))


@bp.post("/frames/<int:frame_id>/regenerate")
def frame_regenerate(frame_id: int):
    """单帧重生成（后台执行）。body 可选 ``{workflow, randomize}``。"""
    return ok(groups.regenerate_frame(frame_id, _json_body() if request.data else {}))


# ---------------------------------------------------------------- 产出图代理


@bp.get("/output/<path:rel>")
def output_file(rel: str):
    """同源代理漫画产出图；路径穿越防护下只解析 ``comic_outputs`` 目录内的文件。"""
    from sync.paths import safe_join

    root = Path(Config.DATA_DIR) / "comic_outputs"
    target = safe_join(root, rel)
    if target is None or not target.is_file():
        raise AIBARError("not_found", "图片不存在")
    return send_from_directory(str(root), str(target.relative_to(root)), mimetype="image/png")


@bp.errorhandler(AIBARError)
def _handle_aibar_error(exc: AIBARError):
    from flask import jsonify

    return jsonify({"ok": False, "error": {"code": exc.code, "message": exc.message}}), exc.status
