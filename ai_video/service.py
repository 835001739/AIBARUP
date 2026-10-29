"""AI 视频创作工作台服务层。

把 Excel 里的 L0-L5 分层方法、真人写实短剧的 28 镜骨架和质检清单，
落成可编辑、可统计、可重复使用的项目数据。此模块只依赖 ``core``，不反向
调用漫画、视频转帧等业务包；跨模块协作通过 ID/相对引用字段完成。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from urllib.parse import quote
from typing import Any

from config import Config
from core import db
from core.errors import AIBARError

PROJECT_FORMATS = ("talking_head", "short_drama", "product_ad", "batch_content")
ASPECT_RATIOS = ("9:16", "16:9", "1:1", "4:5")
VISUAL_STYLES = ("realistic", "anime", "3d", "illustration")
AUTOMATION_LEVELS = ("L1", "L2", "L3")
PROJECT_STATUSES = ("planning", "asset_building", "production", "review", "done", "paused")
SHOT_STATUSES = (
    "pending", "tts_ready", "frame_draft", "frame_approved", "video_draft",
    "auto_qc", "human_review", "approved", "hq_render", "done", "rejected", "needs_human",
)
ASSET_TYPES = ("character", "scene", "prop", "wardrobe", "voice", "style", "music", "sfx")
ASSET_STATUSES = ("draft", "generating", "ready", "locked", "failed", "archived")
ASSET_MANUAL_STATUSES = ("draft", "ready", "locked", "archived")
ASSET_STATUS_LABELS = {
    "draft": "草稿",
    "generating": "生成中",
    "ready": "已就绪",
    "locked": "已锁定",
    "failed": "生成失败",
    "archived": "已归档",
}
QUALITY_TIERS = ("draft", "preview", "final")
SHOT_TYPES = (
    "insert_prop", "reaction_cu", "ots", "speak_cu", "establishing", "silhouette",
    "two_shot", "tracking_back", "mirror", "empty", "wide", "other",
)
MAX_TEXT = 12000
MAX_NAME = 120
MAX_ATTEMPTS = 20
QC_KEYS = (
    "face_consistency", "hands_anatomy", "text_artifact", "axis_continuity",
    "emotion_continuity", "wardrobe_continuity", "audio_sync", "duration_match",
    "skin_texture", "eye_focus", "mouth_quality", "grain_consistency", "ai_label",
)
WORKFLOW_STAGES = (
    {"key": "L0", "name": "剧本与分镜", "desc": "结构化场景、镜头、对白与时长"},
    {"key": "L1", "name": "资产层", "desc": "角色、场景、造型、音色与风格锚点"},
    {"key": "L2", "name": "生成层", "desc": "TTS、首尾帧、图生视频与口型"},
    {"key": "L3", "name": "组装层", "desc": "时间线、字幕、转场、调色与混音"},
    {"key": "L4", "name": "质检回流", "desc": "自动过滤、人工验收、失败重试"},
    {"key": "L5", "name": "工程层", "desc": "参数快照、成本、状态与复现"},
)


def _text(value: Any, field: str, *, max_len: int = MAX_TEXT, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise AIBARError("invalid_input", f"{field}不能为空")
    if len(result) > max_len:
        raise AIBARError("invalid_input", f"{field}不能超过 {max_len} 个字符")
    return result


def _enum(value: Any, field: str, allowed: tuple[str, ...], default: str) -> str:
    result = str(value or default).strip()
    if result not in allowed:
        raise AIBARError("invalid_input", f"{field}不支持：{result}")
    return result


def _int(value: Any, field: str, default: int, lo: int, hi: int) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", f"{field}必须是整数")
    if result < lo or result > hi:
        raise AIBARError("invalid_input", f"{field}必须在 {lo}–{hi} 之间")
    return result


def _optional_int(value: Any, field: str, lo: int, hi: int) -> int | None:
    if value in (None, ""):
        return None
    return _int(value, field, 0, lo, hi)


def _float(value: Any, field: str, default: float, lo: float, hi: float) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise AIBARError("invalid_input", f"{field}必须是数字")
    if result < lo or result > hi:
        raise AIBARError("invalid_input", f"{field}必须在 {lo:g}–{hi:g} 之间")
    return round(result, 3)


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise AIBARError("invalid_input", f"{field}必须是对象")
    return value


def _controlled_ref(value: Any) -> str:
    """只接受受控相对引用，避免把本机绝对路径写入数据库或 API。"""
    result = _text(value, "受控引用", max_len=500)
    normalized = result.replace("\\", "/")
    if normalized.startswith("/") or ":/" in normalized or ".." in normalized.split("/"):
        raise AIBARError("invalid_input", "资产引用必须是受控相对路径或资源 ID")
    return result


def _workflow_filename(value: Any) -> str:
    """校验出图工作流必须是已同步到 workflows 表的文件名。"""
    result = _text(value, "出图工作流", max_len=500)
    if not result:
        return ""
    row = db.query_one("SELECT filename FROM workflows WHERE filename=?", (result,))
    if row is None:
        raise AIBARError("invalid_input", "出图工作流不存在，请先同步 ComfyUI 工作流")
    return result


def _project_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["compliance"] = db.json_field(item.get("compliance"), {})
    return item


def _scene_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["continuity"] = db.json_field(item.get("continuity"), {})
    return item


def _frame_url(ref: Any) -> str:
    value = str(ref or "")
    if value.startswith("asset-image:"):
        image_id = value.split(":", 1)[1]
        if image_id.isdigit():
            return "/api/ai-video/asset-images/%s/image" % image_id
    if value.startswith("frame:"):
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2]:
            return "/api/ai-video/shots/%s/frames/%s" % (parts[1], quote(parts[2]))
    return ""


def _shot_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["lipsync"] = bool(item.get("lipsync"))
    item["snapshot"] = db.json_field(item.get("snapshot"), {})
    item["qc"] = db.json_field(item.get("qc"), {})
    item["frame_status"] = db.json_field(item.get("frame_status"), {})
    item["first_frame_url"] = _frame_url(item.get("first_frame"))
    item["last_frame_url"] = _frame_url(item.get("last_frame"))
    return item


def _asset_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = db.json_field(item.get("metadata"), {})
    item["image_url"] = ""
    if item.get("image_path"):
        # 路径本身含内容哈希，换封面/重生成都会得到新的 URL，避免浏览器缓存旧图。
        stamp = sha1(str(item.get("image_path") or "").encode("utf-8")).hexdigest()[:10]
        item["image_url"] = "/api/ai-video/assets/%s/image?v=%s" % (item.get("id"), stamp)
    item["status_label"] = ASSET_STATUS_LABELS.get(str(item.get("status") or ""), str(item.get("status") or ""))
    count = int(
        db.query_scalar("SELECT COUNT(*) FROM ai_video_asset_images WHERE asset_id=?", (item.get("id"),), 0) or 0
    )
    item["image_count"] = max(count, 1 if item.get("image_path") else 0)
    return item


def _asset_image_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["image_url"] = ""
    if item.get("image_path"):
        item["image_url"] = "/api/ai-video/assets/%s/images/%s/image" % (
            item.get("asset_id"),
            item.get("id"),
        )
    return item


def list_asset_images(asset_id: int) -> dict[str, Any]:
    asset = get_asset(asset_id)
    rows = db.query_all(
        "SELECT * FROM ai_video_asset_images WHERE asset_id=? ORDER BY id DESC", (int(asset_id),)
    )
    # 兼容多图表上线前已生成过封面的资产：首次打开图库时把封面补登记为一条变体。
    if not rows and asset.get("image_path"):
        meta = asset.get("metadata") or {}
        db.execute(
            """INSERT INTO ai_video_asset_images
               (asset_id,project_id,label,prompt,image_path,workflow,seed,width,height,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                int(asset_id), int(asset["project_id"]), "当前封面", asset.get("prompt") or "",
                asset["image_path"], asset.get("workflow") or "", asset.get("seed"),
                int(meta.get("width") or 0), int(meta.get("height") or 0), db.now(),
            ),
        )
        rows = db.query_all(
            "SELECT * FROM ai_video_asset_images WHERE asset_id=? ORDER BY id DESC", (int(asset_id),)
        )
    return {"items": [_asset_image_row(row) for row in rows], "total": len(rows)}


def set_asset_cover(asset_id: int, image_id: int) -> dict[str, Any]:
    asset = get_asset(asset_id)
    row = db.query_one(
        "SELECT * FROM ai_video_asset_images WHERE id=? AND asset_id=?", (int(image_id), int(asset_id))
    )
    if row is None:
        raise AIBARError("not_found", "资产图片不存在", 404)
    metadata = dict(asset.get("metadata") or {})
    metadata.update({"width": row["width"], "height": row["height"]})
    db.execute(
        "UPDATE ai_video_assets SET image_path=?, workflow=?, seed=?, metadata=?, updated_at=? WHERE id=?",
        (row["image_path"], row["workflow"] or "", row["seed"], db.dumps(metadata), db.now(), int(asset_id)),
    )
    return get_asset(asset_id)


def delete_asset_image(asset_id: int, image_id: int) -> None:
    asset = get_asset(asset_id)
    row = db.query_one(
        "SELECT * FROM ai_video_asset_images WHERE id=? AND asset_id=?", (int(image_id), int(asset_id))
    )
    if row is None:
        raise AIBARError("not_found", "资产图片不存在", 404)
    db.execute("DELETE FROM ai_video_asset_images WHERE id=?", (int(image_id),))
    try:
        target = Path(Config.DATA_DIR) / str(row["image_path"])
        if target.is_file():
            target.unlink()
    except OSError:
        pass
    if str(asset.get("image_path") or "") == str(row["image_path"] or ""):
        latest = db.query_one(
            "SELECT * FROM ai_video_asset_images WHERE asset_id=? ORDER BY id DESC LIMIT 1", (int(asset_id),)
        )
        if latest is not None:
            db.execute(
                "UPDATE ai_video_assets SET image_path=?, workflow=?, seed=?, updated_at=? WHERE id=?",
                (latest["image_path"], latest["workflow"] or "", latest["seed"], db.now(), int(asset_id)),
            )
        else:
            db.execute(
                "UPDATE ai_video_assets SET image_path='', status='draft', updated_at=? WHERE id=?",
                (db.now(), int(asset_id)),
            )
    db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (db.now(), asset["project_id"]))


def get_project(project_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ai_video_projects WHERE id=?", (int(project_id),))
    if row is None:
        raise AIBARError("not_found", "AI 视频项目不存在", 404)
    return _project_row(row)


def list_projects() -> list[dict[str, Any]]:
    rows = db.query_all(
        """SELECT p.*,
                  COUNT(DISTINCT s.id) AS scene_count,
                  COUNT(DISTINCT sh.id) AS shot_count,
                  SUM(CASE WHEN sh.status IN ('approved','hq_render','done') THEN 1 ELSE 0 END) AS approved_count
             FROM ai_video_projects p
        LEFT JOIN ai_video_scenes s ON s.project_id=p.id
        LEFT JOIN ai_video_shots sh ON sh.project_id=p.id
         GROUP BY p.id
         ORDER BY p.updated_at DESC, p.id DESC"""
    )
    items = []
    for row in rows:
        item = _project_row(row)
        item["scene_count"] = int(item.get("scene_count") or 0)
        item["shot_count"] = int(item.get("shot_count") or 0)
        item["approved_count"] = int(item.get("approved_count") or 0)
        items.append(item)
    return items


def create_project(payload: dict[str, Any]) -> dict[str, Any]:
    name = _text(payload.get("name"), "项目名称", max_len=MAX_NAME, required=True)
    now = db.now()
    values = (
        name,
        _enum(payload.get("video_format"), "视频形态", PROJECT_FORMATS, "short_drama"),
        _enum(payload.get("aspect_ratio"), "画幅", ASPECT_RATIOS, "9:16"),
        _enum(payload.get("visual_style"), "视觉风格", VISUAL_STYLES, "realistic"),
        _float(payload.get("target_duration"), "目标时长", 120, 5, 7200),
        _int(payload.get("weekly_output"), "每周产能", 3, 1, 1000),
        _enum(payload.get("automation_level"), "自动化等级", AUTOMATION_LEVELS, "L2"),
        _enum(payload.get("status"), "项目状态", PROJECT_STATUSES, "planning"),
        _text(payload.get("notes"), "备注"),
        db.dumps(_json_object(payload.get("compliance"), "合规项")),
        _workflow_filename(payload.get("default_workflow")),
        _text(payload.get("default_negative"), "默认负向提示词"),
        now,
        now,
    )
    cur = db.execute(
        """INSERT INTO ai_video_projects
           (name,video_format,aspect_ratio,visual_style,target_duration,weekly_output,
            automation_level,status,notes,compliance,default_workflow,default_negative,
            created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    return get_project(int(cur.lastrowid))


def update_project(project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_project(project_id)
    fields: list[str] = []
    values: list[Any] = []
    specs = {
        "name": lambda v: _text(v, "项目名称", max_len=MAX_NAME, required=True),
        "video_format": lambda v: _enum(v, "视频形态", PROJECT_FORMATS, current["video_format"]),
        "aspect_ratio": lambda v: _enum(v, "画幅", ASPECT_RATIOS, current["aspect_ratio"]),
        "visual_style": lambda v: _enum(v, "视觉风格", VISUAL_STYLES, current["visual_style"]),
        "target_duration": lambda v: _float(v, "目标时长", current["target_duration"], 5, 7200),
        "weekly_output": lambda v: _int(v, "每周产能", current["weekly_output"], 1, 1000),
        "automation_level": lambda v: _enum(v, "自动化等级", AUTOMATION_LEVELS, current["automation_level"]),
        "status": lambda v: _enum(v, "项目状态", PROJECT_STATUSES, current["status"]),
        "notes": lambda v: _text(v, "备注"),
        "compliance": lambda v: db.dumps(_json_object(v, "合规项")),
        "default_workflow": _workflow_filename,
        "default_negative": lambda v: _text(v, "默认负向提示词"),
    }
    for key, validator in specs.items():
        if key in payload:
            fields.append(f"{key}=?")
            values.append(validator(payload[key]))
    if fields:
        fields.append("updated_at=?")
        values.extend([db.now(), int(project_id)])
        db.execute(f"UPDATE ai_video_projects SET {', '.join(fields)} WHERE id=?", values)
    return get_project(project_id)


def delete_project(project_id: int) -> None:
    get_project(project_id)
    with db.tx():
        db.execute("DELETE FROM ai_video_shots WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM ai_video_scenes WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM ai_video_assets WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM ai_video_projects WHERE id=?", (project_id,))


def create_scene(project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    get_project(project_id)
    code = _text(payload.get("scene_code"), "场景编号", max_len=40, required=True)
    order_idx = _int(payload.get("order_idx"), "排序", 0, 0, 10000)
    now = db.now()
    try:
        cur = db.execute(
            """INSERT INTO ai_video_scenes
               (project_id,scene_code,title,location,time_of_day,axis_rule,lighting,continuity,order_idx,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, code, _text(payload.get("title"), "场景标题", max_len=MAX_NAME),
                _text(payload.get("location"), "地点", max_len=MAX_NAME),
                _text(payload.get("time_of_day"), "时间", max_len=40),
                _text(payload.get("axis_rule"), "轴线规则", max_len=1000),
                _text(payload.get("lighting"), "光线", max_len=1000),
                db.dumps(_json_object(payload.get("continuity"), "连续性")), order_idx, now, now,
            ),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise AIBARError("duplicate_scene", "同一项目内场景编号不能重复", 409)
        raise
    row = db.query_one("SELECT * FROM ai_video_scenes WHERE id=?", (cur.lastrowid,))
    return _scene_row(row)


def _get_scene(scene_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ai_video_scenes WHERE id=?", (scene_id,))
    if row is None:
        raise AIBARError("not_found", "场景不存在", 404)
    return _scene_row(row)


def create_shot(scene_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    scene = _get_scene(scene_id)
    project_id = int(scene["project_id"])
    code = _text(payload.get("shot_code"), "镜头编号", max_len=60, required=True)
    now = db.now()
    qc = _json_object(payload.get("qc"), "质检项")
    qc = {key: bool(qc.get(key, False)) for key in QC_KEYS}
    try:
        cur = db.execute(
            """INSERT INTO ai_video_shots
               (project_id,scene_id,shot_code,order_idx,shot_type,duration,lens,camera,
                prompt_image,prompt_motion,speaker,dialogue,lipsync,visible_face,first_frame,last_frame,
                status,attempts,max_attempts,quality_tier,cost_estimate,snapshot,qc,notes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, scene_id, code, _int(payload.get("order_idx"), "排序", 0, 0, 100000),
                _enum(payload.get("shot_type"), "镜头类型", SHOT_TYPES, "establishing"),
                _float(payload.get("duration"), "镜头时长", 3, 0.2, 120),
                _text(payload.get("lens"), "焦段", max_len=200), _text(payload.get("camera"), "机位", max_len=1000),
                _text(payload.get("prompt_image"), "画面提示词"), _text(payload.get("prompt_motion"), "运动提示词"),
                _text(payload.get("speaker"), "说话人", max_len=120), _text(payload.get("dialogue"), "台词", max_len=4000),
                1 if bool(payload.get("lipsync")) else 0, _text(payload.get("visible_face"), "露脸角色", max_len=240),
                _text(payload.get("first_frame"), "首帧引用", max_len=500), _text(payload.get("last_frame"), "尾帧引用", max_len=500),
                _enum(payload.get("status"), "镜头状态", SHOT_STATUSES, "pending"),
                _int(payload.get("attempts"), "尝试次数", 0, 0, MAX_ATTEMPTS),
                _int(payload.get("max_attempts"), "最大尝试次数", 3, 1, MAX_ATTEMPTS),
                _enum(payload.get("quality_tier"), "质量档位", QUALITY_TIERS, "draft"),
                _float(payload.get("cost_estimate"), "预估成本", 0, 0, 1_000_000),
                db.dumps(_json_object(payload.get("snapshot"), "参数快照")), db.dumps(qc),
                _text(payload.get("notes"), "备注"), now, now,
            ),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise AIBARError("duplicate_shot", "同一项目内镜头编号不能重复", 409)
        raise
    return get_shot(int(cur.lastrowid))


def get_shot(shot_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ai_video_shots WHERE id=?", (shot_id,))
    if row is None:
        raise AIBARError("not_found", "镜头不存在", 404)
    return _shot_row(row)


def _frame_column(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value in ("start", "first", "first_frame"):
        return "first_frame"
    if value in ("end", "last", "last_frame"):
        return "last_frame"
    raise AIBARError("invalid_input", "帧类型必须是 start 或 end")


def get_asset_image(image_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ai_video_asset_images WHERE id=?", (int(image_id),))
    if row is None:
        raise AIBARError("not_found", "资产图片不存在", 404)
    return dict(row)


def set_shot_frame_ref(shot_id: int, kind: str, ref: str) -> dict[str, Any]:
    shot = get_shot(shot_id)
    column = _frame_column(kind)
    key = "start" if column == "first_frame" else "end"
    value = _text(ref, "首尾帧引用", max_len=500)
    status_data = dict(shot.get("frame_status") or {})
    status_data.pop(key, None)
    db.execute(
        "UPDATE ai_video_shots SET %s=?, frame_status=?, updated_at=? WHERE id=?" % column,
        (value, db.dumps(status_data), db.now(), int(shot_id)),
    )
    db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (db.now(), shot["project_id"]))
    return get_shot(shot_id)


def attach_shot_frame(shot_id: int, kind: str, image_id: int) -> dict[str, Any]:
    shot = get_shot(shot_id)
    image = db.query_one(
        "SELECT ai.*, a.project_id AS asset_project_id FROM ai_video_asset_images ai "
        "JOIN ai_video_assets a ON a.id=ai.asset_id WHERE ai.id=?",
        (int(image_id),),
    )
    if image is None:
        raise AIBARError("not_found", "资产图片不存在", 404)
    if int(image["asset_project_id"]) != int(shot["project_id"]):
        raise AIBARError("invalid_input", "只能选择当前项目的角色/场景图片")
    return set_shot_frame_ref(shot_id, kind, "asset-image:%d" % int(image_id))


def clear_shot_frame(shot_id: int, kind: str) -> dict[str, Any]:
    return set_shot_frame_ref(shot_id, kind, "")


def set_shot_frame_status(shot_id: int, kind: str, status: str, **extra: Any) -> dict[str, Any]:
    shot = get_shot(shot_id)
    key = "start" if _frame_column(kind) == "first_frame" else "end"
    data = dict(shot.get("frame_status") or {})
    item = dict(data.get(key) or {})
    item.update(extra)
    item["status"] = status
    data[key] = item
    db.execute(
        "UPDATE ai_video_shots SET frame_status=?, updated_at=? WHERE id=?",
        (db.dumps(data), db.now(), int(shot_id)),
    )
    return get_shot(shot_id)


def update_shot(shot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_shot(shot_id)
    next_payload = dict(payload)
    # 拒绝一次就记一次尝试；达到阈值后停止自动重试，转人工处理。
    if next_payload.get("status") == "rejected" and current.get("status") != "rejected":
        next_payload.setdefault("attempts", int(current.get("attempts") or 0) + 1)
    next_attempts = int(next_payload.get("attempts", current.get("attempts") or 0) or 0)
    next_max = int(next_payload.get("max_attempts", current.get("max_attempts") or 3) or 3)
    if next_payload.get("status") == "rejected" and next_attempts >= next_max:
        next_payload["status"] = "needs_human"

    fields: list[str] = []
    values: list[Any] = []
    validators = {
        "shot_code": lambda v: _text(v, "镜头编号", max_len=60, required=True),
        "order_idx": lambda v: _int(v, "排序", current["order_idx"], 0, 100000),
        "shot_type": lambda v: _enum(v, "镜头类型", SHOT_TYPES, current["shot_type"]),
        "duration": lambda v: _float(v, "镜头时长", current["duration"], 0.2, 120),
        "lens": lambda v: _text(v, "焦段", max_len=200),
        "camera": lambda v: _text(v, "机位", max_len=1000),
        "prompt_image": lambda v: _text(v, "画面提示词"),
        "prompt_motion": lambda v: _text(v, "运动提示词"),
        "speaker": lambda v: _text(v, "说话人", max_len=120),
        "dialogue": lambda v: _text(v, "台词", max_len=4000),
        "lipsync": lambda v: 1 if bool(v) else 0,
        "visible_face": lambda v: _text(v, "露脸角色", max_len=240),
        "first_frame": lambda v: _text(v, "首帧引用", max_len=500),
        "last_frame": lambda v: _text(v, "尾帧引用", max_len=500),
        "status": lambda v: _enum(v, "镜头状态", SHOT_STATUSES, current["status"]),
        "attempts": lambda v: _int(v, "尝试次数", current["attempts"], 0, MAX_ATTEMPTS),
        "max_attempts": lambda v: _int(v, "最大尝试次数", current["max_attempts"], 1, MAX_ATTEMPTS),
        "quality_tier": lambda v: _enum(v, "质量档位", QUALITY_TIERS, current["quality_tier"]),
        "cost_estimate": lambda v: _float(v, "预估成本", current["cost_estimate"], 0, 1_000_000),
        "snapshot": lambda v: db.dumps(_json_object(v, "参数快照")),
        "qc": lambda v: db.dumps({key: bool(_json_object(v, "质检项").get(key, False)) for key in QC_KEYS}),
        "notes": lambda v: _text(v, "备注"),
    }
    for key, validator in validators.items():
        if key in next_payload:
            fields.append(f"{key}=?")
            values.append(validator(next_payload[key]))
    if fields:
        fields.append("updated_at=?")
        values.extend([db.now(), shot_id])
        try:
            db.execute(f"UPDATE ai_video_shots SET {', '.join(fields)} WHERE id=?", values)
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise AIBARError("duplicate_shot", "同一项目内镜头编号不能重复", 409)
            raise
        db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (db.now(), current["project_id"]))
    return get_shot(shot_id)


def create_asset(project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    get_project(project_id)
    asset_type = _enum(payload.get("asset_type"), "资产类型", ASSET_TYPES, "character")
    name = _text(payload.get("name"), "资产名称", max_len=MAX_NAME, required=True)
    asset_key = _text(payload.get("asset_key") or name, "资产标识", max_len=120, required=True)
    now = db.now()
    try:
        cur = db.execute(
            """INSERT INTO ai_video_assets
               (project_id,asset_type,asset_key,name,description,ref_path,metadata,status,
                prompt,negative,workflow,seed,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, asset_type, asset_key, name,
                _text(payload.get("description"), "资产描述"),
                _controlled_ref(payload.get("ref_path")),
                db.dumps(_json_object(payload.get("metadata"), "资产元数据")),
                _enum(payload.get("status"), "资产状态", ASSET_MANUAL_STATUSES, "draft"),
                _text(payload.get("prompt"), "角色/场景提示词"),
                _text(payload.get("negative"), "负向提示词"),
                _workflow_filename(payload.get("workflow")),
                _optional_int(payload.get("seed"), "种子", 0, 2_147_483_647),
                now, now,
            ),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise AIBARError("duplicate_asset", "同类型资产标识不能重复", 409)
        raise
    row = db.query_one("SELECT * FROM ai_video_assets WHERE id=?", (cur.lastrowid,))
    return _asset_row(row)


def get_asset(asset_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM ai_video_assets WHERE id=?", (int(asset_id),))
    if row is None:
        raise AIBARError("not_found", "资产不存在", 404)
    return _asset_row(row)


def update_asset(asset_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_asset(asset_id)
    fields: list[str] = []
    values: list[Any] = []
    specs = {
        "name": lambda v: _text(v, "资产名称", max_len=MAX_NAME, required=True),
        "description": lambda v: _text(v, "资产描述"),
        "status": lambda v: _enum(v, "资产状态", ASSET_MANUAL_STATUSES, current["status"]),
        "ref_path": _controlled_ref,
        "metadata": lambda v: db.dumps(_json_object(v, "资产元数据")),
        "prompt": lambda v: _text(v, "角色/场景提示词"),
        "negative": lambda v: _text(v, "负向提示词"),
        "workflow": _workflow_filename,
        "seed": lambda v: _optional_int(v, "种子", 0, 2_147_483_647),
    }
    for key, validator in specs.items():
        if key in payload:
            fields.append(f"{key}=?")
            values.append(validator(payload[key]))
    if fields:
        fields.append("updated_at=?")
        values.extend([db.now(), int(asset_id)])
        db.execute(f"UPDATE ai_video_assets SET {', '.join(fields)} WHERE id=?", values)
        db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (db.now(), current["project_id"]))
    return get_asset(asset_id)


# 节点类型 → 工作流能力判定。素材来自 workflows 表，不在请求时读工作流 JSON。
_VIDEO_NODE_HINTS = (
    "CreateVideo", "SaveVideo", "VHS_VideoCombine", "WanVideo", "MiniMaxH3",
    "EmptyMiniMax", "SaveAnimated",
)
_AUDIO_NODE_HINTS = (
    "SaveAudio", "PreviewAudio", "EmptyAceStep", "TextEncodeAceStep", "VAEDecodeAudio",
)
_REFERENCE_NODE_HINTS = ("LoadImage", "LoadImageMask", "LoadImageOutput", "IPAdapterFaceID")


def classify_workflow(node_types: list[str]) -> dict[str, Any]:
    """判断工作流能否用于“只给文字提示词”的角色出图，并给出不可用原因。"""
    types = {str(item) for item in (node_types or [])}
    has_text = any("TextEncode" in item or "CLIPTextEncode" in item for item in types)
    has_save = any("SaveImage" in item or "PreviewImage" in item for item in types)
    is_video = any(any(hint in item for hint in _VIDEO_NODE_HINTS) for item in types)
    is_audio = any(any(hint in item for hint in _AUDIO_NODE_HINTS) for item in types)
    requires_image = any(item in types for item in _REFERENCE_NODE_HINTS)
    if is_video:
        kind, ready, reason = "video", False, "视频工作流"
    elif is_audio:
        kind, ready, reason = "audio", False, "音频工作流"
    elif not has_save:
        kind, ready, reason = "unsupported", False, "没有图片输出节点"
    elif not has_text:
        kind, ready, reason = "unsupported", False, "没有可注入提示词的文本节点"
    elif requires_image:
        kind, ready, reason = "reference", False, "需要参考图/骨架图"
    else:
        kind, ready, reason = "character", True, ""
    return {
        "kind": kind,
        "character_ready": ready,
        "workflow_reason": reason,
        "requires_image": requires_image,
    }


def list_workflows() -> dict[str, Any]:
    """可选出图工作流清单；带能力标记，不返回提示词正文。"""
    rows = db.query_all(
        "SELECT id,name,filename,node_count,node_types,synced_at FROM workflows "
        "ORDER BY synced_at DESC, id DESC"
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["node_types"] = db.json_field(item.pop("node_types", None), [])
        item.update(classify_workflow(item["node_types"]))
        # 只给前端节点数，避免把完整节点类型塞进响应。
        item.pop("node_types", None)
        items.append(item)
    return {"items": items, "total": len(items)}


def recommended_workflow() -> str:
    """返回最适合角色出图的最新工作流；没有时退回最新一条。"""
    items = list_workflows()["items"]
    for item in items:
        if item.get("character_ready"):
            return str(item.get("filename") or "")
    return str(items[0].get("filename") or "") if items else ""


def build_asset_prompt(asset: dict[str, Any], project: dict[str, Any]) -> str:
    """没有手写提示词时，按资产类型生成一条可用的角色/场景锚点提示词。"""
    explicit = _text(asset.get("prompt"), "出图提示词")
    if explicit:
        return explicit
    name = _text(asset.get("name"), "资产名称")
    desc = _text(asset.get("description"), "资产描述")
    style = _text(project.get("visual_style"), "视觉风格") or "realistic"
    style_words = {
        "realistic": "photorealistic, cinematic realism, ARRI Alexa, visible skin texture, soft natural light, subtle film grain",
        "anime": "anime style, clean line art, expressive eyes, high detail",
        "3d": "3D render, physically based shading, soft studio light, high detail",
        "illustration": "digital illustration, painterly, rich color, high detail",
    }.get(style, "cinematic realism, high detail")
    asset_type = _text(asset.get("asset_type"), "资产类型") or "character"
    if asset_type == "character":
        subject = (
            "character reference sheet of %s, %s, front view, three-quarter view, side view, "
            "full body, neutral expression, plain background, consistent identity"
            % (name, desc)
        )
    elif asset_type == "scene":
        subject = "location reference of %s, %s, wide establishing view, consistent lighting" % (name, desc)
    elif asset_type == "style":
        subject = "style reference for %s, %s" % (name, desc)
    elif asset_type == "prop":
        subject = "prop reference of %s, %s, isolated on plain background" % (name, desc)
    elif asset_type == "wardrobe":
        subject = "wardrobe reference of %s, %s, full outfit, front view" % (name, desc)
    else:
        subject = "%s, %s" % (name, desc)
    return "%s, %s" % (subject.strip(", "), style_words)


def build_asset_negative(asset: dict[str, Any], project: dict[str, Any]) -> str:
    explicit = _text(asset.get("negative"), "负向提示词")
    if explicit:
        return explicit
    project_negative = _text(project.get("default_negative"), "默认负向提示词")
    base = "blurry, low quality, deformed, extra fingers, text, watermark, logo, duplicate face, inconsistent identity"
    return (project_negative + ", " + base).strip(", ") if project_negative else base


def reap_stale_asset_generations(project_id: int, minutes: int = 15) -> int:
    """进程中断后不让资产永久卡在 generating；打开项目时复位为失败。"""
    cutoff = (datetime.now() - timedelta(minutes=max(1, int(minutes)))).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        """UPDATE ai_video_assets
           SET status='failed', error_message='生成进程已中断，可重新提交', updated_at=?
           WHERE project_id=? AND status='generating' AND (updated_at IS NULL OR updated_at<?)""",
        (db.now(), int(project_id), cutoff),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def reap_stale_shot_frames(project_id: int, minutes: int = 15) -> int:
    """进程中断后复位卡在 generating 的首/尾帧，避免永久转圈。"""
    cutoff = (datetime.now() - timedelta(minutes=max(1, int(minutes)))).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.query_all(
        "SELECT id, frame_status FROM ai_video_shots WHERE project_id=?", (int(project_id),)
    )
    changed = 0
    for row in rows:
        data = db.json_field(dict(row).get("frame_status"), {})
        touched = False
        for kind in ("start", "end"):
            item = dict(data.get(kind) or {})
            if str(item.get("status") or "") != "generating":
                continue
            started = str(item.get("started_at") or "")
            if started and started >= cutoff:
                continue
            item.update({"status": "failed", "error": "生成进程已中断，可重新提交", "finished_at": db.now()})
            data[kind] = item
            touched = True
        if touched:
            db.execute(
                "UPDATE ai_video_shots SET frame_status=?, updated_at=? WHERE id=?",
                (db.dumps(data), db.now(), int(row["id"])),
            )
            changed += 1
    return changed


def project_dashboard(project_id: int) -> dict[str, Any]:
    reap_stale_asset_generations(project_id)
    reap_stale_shot_frames(project_id)
    project = get_project(project_id)
    scenes = [_scene_row(row) for row in db.query_all(
        "SELECT * FROM ai_video_scenes WHERE project_id=? ORDER BY order_idx,id", (project_id,)
    )]
    shots = [_shot_row(row) for row in db.query_all(
        "SELECT * FROM ai_video_shots WHERE project_id=? ORDER BY order_idx,id", (project_id,)
    )]
    assets = [_asset_row(row) for row in db.query_all(
        "SELECT * FROM ai_video_assets WHERE project_id=? ORDER BY asset_type,name", (project_id,)
    )]
    by_scene: dict[int, list[dict[str, Any]]] = {}
    for shot in shots:
        by_scene.setdefault(int(shot["scene_id"]), []).append(shot)
    for scene in scenes:
        scene["shots"] = by_scene.get(int(scene["id"]), [])

    status_counts: dict[str, int] = {key: 0 for key in SHOT_STATUSES}
    total_duration = 0.0
    total_cost = 0.0
    qc_done = 0
    qc_total = len(shots) * len(QC_KEYS)
    for shot in shots:
        status_counts[shot["status"]] = status_counts.get(shot["status"], 0) + 1
        total_duration += float(shot.get("duration") or 0)
        total_cost += float(shot.get("cost_estimate") or 0)
        qc_done += sum(1 for key in QC_KEYS if shot.get("qc", {}).get(key))
    approved = sum(status_counts.get(key, 0) for key in ("approved", "hq_render", "done"))
    return {
        "project": project,
        "scenes": scenes,
        "assets": assets,
        "stages": WORKFLOW_STAGES,
        "stats": {
            "scene_count": len(scenes), "shot_count": len(shots),
            "approved_count": approved, "lipsync_count": sum(1 for shot in shots if shot["lipsync"]),
            "duration": round(total_duration, 2), "cost_estimate": round(total_cost, 2),
            "progress": round(approved * 100 / len(shots), 1) if shots else 0,
            "qc_progress": round(qc_done * 100 / qc_total, 1) if qc_total else 0,
            "asset_count": len(assets),
            "asset_ready_count": sum(1 for item in assets if item.get("status") in ("ready", "locked")),
            "asset_missing_image_count": sum(1 for item in assets if not item.get("image_path")),
            "status_counts": status_counts,
        },
    }


def metadata() -> dict[str, Any]:
    return {
        "project_formats": PROJECT_FORMATS, "aspect_ratios": ASPECT_RATIOS,
        "visual_styles": VISUAL_STYLES, "automation_levels": AUTOMATION_LEVELS,
        "project_statuses": PROJECT_STATUSES, "shot_statuses": SHOT_STATUSES,
        "shot_types": SHOT_TYPES, "asset_types": ASSET_TYPES,
        "asset_statuses": ASSET_STATUSES,
        "asset_manual_statuses": ASSET_MANUAL_STATUSES,
        "asset_status_labels": ASSET_STATUS_LABELS,
        "quality_tiers": QUALITY_TIERS,
        "qc_keys": QC_KEYS, "stages": WORKFLOW_STAGES,
    }


_TEMPLATE_SCENES = (
    ("SC01", "冷开场钩子", "会议室", "A 坐画面左，B 立画面右，全场禁止翻转", 0),
    ("SC02", "核心对白戏", "会议室", "沿用 SC01 轴线，A 左 B 右", 1),
    ("SC03", "转折与情绪低点", "走廊 / 卫生间", "镜面镜头注意左右关系", 2),
    ("SC04", "反转钩子与片尾断点", "电梯 / 车内", "车内正反打保持视线方向", 3),
)
_TEMPLATE_SHOTS = (
    ("SC01", "S01", "insert_prop", 2.0, "", "", False, "文件摔在桌上，低成本钩子"),
    ("SC01", "S02", "reaction_cu", 2.5, "B", "A", False, "听者反应，压抑情绪"),
    ("SC01", "S03", "ots", 3.5, "B", "A", False, "说者后脑，画面给听者"),
    ("SC01", "S04", "speak_cu", 2.2, "A", "A", True, "第一句主角台词，≤2.5 秒"),
    ("SC01", "S05", "establishing", 3.0, "", "群", False, "24mm 定场镜头"),
    ("SC02", "S01", "silhouette", 3.0, "B", "B 侧影", False, "侧影对白"),
    ("SC02", "S02", "reaction_cu", 1.8, "", "C", False, "旁观者反应"),
    ("SC02", "S03", "ots", 3.5, "A", "B", False, "A 说话，画面给 B"),
    ("SC02", "S04", "speak_cu", 2.4, "B", "B", True, "对手亮底牌"),
    ("SC02", "S05", "insert_prop", 2.0, "A", "", False, "茶杯或手部空镜，画外音"),
    ("SC02", "S06", "two_shot", 4.0, "", "A+B", False, "轴线基准镜"),
    ("SC02", "S07", "reaction_cu", 2.0, "", "A", False, "85mm 眼神大特写"),
    ("SC02", "S08", "speak_cu", 2.5, "A", "A", True, "情绪爆点"),
    ("SC02", "S09", "ots", 3.0, "B", "A", False, "B 后脑前景"),
    ("SC02", "S10", "speak_cu", 2.3, "B", "B", True, "最后通牒"),
    ("SC02", "S11", "tracking_back", 3.0, "", "A 背影", False, "离场转场"),
    ("SC03", "S01", "empty", 2.5, "", "", False, "走廊纵深，节奏喘息"),
    ("SC03", "S02", "tracking_back", 3.5, "", "A 背影", False, "轻微手持跟拍"),
    ("SC03", "S03", "mirror", 3.0, "", "A", False, "镜中反应"),
    ("SC03", "S04", "insert_prop", 2.0, "", "", False, "水流/水龙头环境声"),
    ("SC03", "S05", "speak_cu", 2.5, "A", "A", True, "低语自白"),
    ("SC03", "S06", "silhouette", 3.0, "", "A 侧影", False, "低头侧影"),
    ("SC03", "S07", "ots", 3.0, "C", "A", False, "信息交付用已完成态"),
    ("SC04", "S01", "insert_prop", 2.0, "", "", False, "电梯数字，悬念累积"),
    ("SC04", "S02", "reaction_cu", 2.5, "", "A", False, "全集情绪转折点"),
    ("SC04", "S03", "ots", 3.5, "C", "A", False, "车内电话画外音"),
    ("SC04", "S04", "speak_cu", 2.0, "A", "A", True, "片尾金句"),
    ("SC04", "S05", "wide", 3.0, "", "", False, "车窗外推远，悬念断点"),
)


def seed_realistic_drama_template(project_id: int) -> dict[str, Any]:
    """为一个空项目载入 Excel 建议的真人写实短剧 4 场 28 镜骨架。"""
    project = get_project(project_id)
    existing = db.query_scalar("SELECT COUNT(*) FROM ai_video_scenes WHERE project_id=?", (project_id,), 0)
    if int(existing or 0) > 0:
        raise AIBARError("project_not_empty", "项目已有场景，不能重复载入模板", 409)
    scene_ids: dict[str, int] = {}
    with db.tx():
        for code, title, location, axis, order_idx in _TEMPLATE_SCENES:
            scene = create_scene(project_id, {
                "scene_code": code, "title": title, "location": location,
                "axis_rule": axis, "time_of_day": "day", "order_idx": order_idx,
                "lighting": "自然不均匀实景光；全剧保持统一色温",
            })
            scene_ids[code] = int(scene["id"])
        for idx, (scene_code, local_code, shot_type, duration, speaker, face, lipsync, notes) in enumerate(_TEMPLATE_SHOTS):
            code = f"EP01_{scene_code}_{local_code}"
            create_shot(scene_ids[scene_code], {
                "shot_code": code, "order_idx": idx, "shot_type": shot_type,
                "duration": duration, "speaker": speaker, "visible_face": face,
                "lipsync": lipsync, "notes": notes,
                "lens": "85mm, f/1.8" if shot_type in ("reaction_cu", "speak_cu") else "35mm, f/2.8",
                "prompt_motion": "动作保持简单；复杂动作拆镜；首尾帧锁内容，提示词只描述运动与氛围",
            })
        for asset_type, key, name, desc in (
            ("character", "char_A", "主角 A", "建立正面、侧面、3/4 侧和表情参考；主角建议 LoRA + 首尾帧换脸"),
            ("character", "char_B", "对手 B", "统一西装造型与音色；正脸口型镜控制在 2.5 秒内"),
            ("character", "char_C", "配角 C", "工具人角色，减少正脸与复杂交互"),
            ("scene", "office_meeting_room", "会议室", "百叶窗侧逆光；A 左 B 右；建立 room tone"),
            ("style", "realistic_film", "真人写实质感", "ARRI Alexa、可见皮肤纹理、轻颗粒、低饱和、非摆拍感"),
        ):
            create_asset(project_id, {"asset_type": asset_type, "asset_key": key, "name": name, "description": desc})
        update_project(project_id, {"video_format": "short_drama", "visual_style": "realistic", "automation_level": "L2", "status": "asset_building"})
    return {"project": project, "scenes": len(_TEMPLATE_SCENES), "shots": len(_TEMPLATE_SHOTS), "assets": 5}
