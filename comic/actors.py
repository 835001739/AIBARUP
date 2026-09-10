"""M13 · 演员库（Actor Library）：跨漫画共享的人物一致性基准源。

**为什么需要它**：``comic_characters`` 是项目级角色卡，同一个人物在第二部漫画里
必须重新填一遍外貌/服装/配色，两边稍有出入长相就漂了。演员库把「这个人长什么样」
上升为全局资产——一次定妆，多部漫画复用；改演员即可批量校正所有关联角色。

分工::

    actors                （演员本人）  ← 定妆信息的唯一权威来源，无 project_id
      └── actor_character_links       ← 演员在某部漫画里演了哪个角色
            └── comic_characters      （角色卡）  ← 每一页注入锚点，可从演员继承

设计约束（沿用 M12 风格）：
- **一致性优先**：角色锚点仍由 ``characters.build_character_anchor`` 组装，
  演员与角色共用同一套拼装逻辑，保证「演员定妆图」和「分镜图」是同一个人；
- **确定性种子**：演员种子由 ``base_seed + seed_offset + 名字`` 派生，
  重新生成默认得到同一张脸；只有显式 ``randomize`` 才换脸；
- **一个角色只能绑一个演员**（表级 UNIQUE 保证），否则「以演员为基准」会出现两个冲突基准；
- **绝不抛未捕获异常到后台线程之外**：重新生成失败只写 ``status='failed'`` + 可读原因。
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.fileops import purge_stale
from core.logging_setup import get_logger, safe_log
from . import characters

_LOGGER = get_logger("aibar.comic.actors")

# 演员总量上限：演员库是花名册而不是图库，超量说明用法跑偏了
MAX_ACTORS = 200

# 列表接口默认返回上限
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

ACTOR_STATUS = {"ready", "generating", "failed"}

# 定妆照镜头前缀：所有演员统一用同一套构图，横向对比时不会因构图差异误判「不像」
ACTOR_SHOT_PREFIX = "角色设定立绘，正面全身，纯色背景，光线均匀，清晰五官"

# 演员定妆图落盘目录（放在 comic_outputs 下，直接复用 /api/comic/output 的路径穿越防护）
ACTOR_OUTPUT_SUBDIR = "actors"

# 重新生成的等待上限（秒）。定妆图只出一张，比整本分镜宽松些即可
REGENERATE_TIMEOUT = 240.0

# 演员定妆锚点允许比页级锚点长：这里是权威定妆信息，不需要为省 token 截断
ACTOR_ANCHOR_LEN = 400

# 可从演员继承到角色卡的字段（定妆信息）。名字与别名不继承——
# 同一个演员在不同漫画里可以叫不同角色名，这正是「演员 ≠ 角色」的意义。
INHERIT_FIELDS = ("appearance", "outfit", "palette", "negative", "image_id")

_SEED_MODULUS = 2**31 - 1


# ---------------------------------------------------------------- 小工具


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _truthy(value: Any, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _opt_int(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_offset(value: Any, fallback: int = 0) -> int:
    n = _opt_int(value)
    if n is None:
        n = fallback
    return max(0, min(9999, n))


def _require_name(payload: dict) -> str:
    name = characters.normalize_name(payload.get("name"))
    if not name:
        raise AIBARError("invalid_input", "缺少必填字段：演员名")
    return name


def default_base_seed(name: str) -> int:
    """演员基准种子：由名字确定性派生，保证同名演员每次新建都得到同一张脸。"""
    return characters.derive_seed(20260101, 0, 0, 0, name)


def build_actor_anchor(actor: dict | None) -> str:
    """演员定妆锚点。与角色锚点共用同一套拼装逻辑（一致性的关键）。"""
    return characters.build_character_anchor(actor, max_len=ACTOR_ANCHOR_LEN)


def build_actor_prompt(actor: dict, extra: str = "") -> str:
    """组装演员定妆图提示词：统一镜头前缀 → 定妆锚点 → 备注 → 本次追加描述。"""
    parts = [
        ACTOR_SHOT_PREFIX,
        build_actor_anchor(actor),
        _text(actor.get("notes")).strip(),
        _text(extra).strip(),
    ]
    return "，".join([p for p in parts if p])


def actor_seed(actor: dict, randomize: bool = False) -> int:
    """演员种子：默认确定性（重生成还是同一张脸），``randomize`` 时才换脸。"""
    if randomize:
        material = "%s|%s|%s" % (actor.get("id"), actor.get("name"), time.time_ns())
        return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:12], 16) % _SEED_MODULUS
    return characters.derive_seed(
        actor.get("base_seed"), 0, 0, _clamp_offset(actor.get("seed_offset")), _text(actor.get("name"))
    )


# ---------------------------------------------------------------- 读取与装饰


def _attach_image(items: list[dict]) -> None:
    """补充定妆图 URL 与出图提示词。

    优先用图库副本（``/static/{gallery_path}``，与角色卡样板图完全一致的约定）；
    图库登记失败时退回自有产出路径（``/api/comic/output/actors/...``），
    保证「图已经出好了」不会因为旁路登记失败而在界面上看不见。
    """
    want = [it.get("image_id") for it in items if it.get("image_id")]
    info: dict[str, dict] = {}
    if want:
        placeholders = ",".join("?" for _ in want)
        rows = db.query_all(
            "SELECT id, gallery_path, prompt FROM images WHERE id IN (%s)" % placeholders,
            tuple(want),
        )
        info = {r["id"]: db.row_to_dict(r) for r in rows}
    for it in items:
        img = info.get(it.get("image_id"))
        if img and img.get("gallery_path"):
            it["image_url"] = "/static/%s" % img["gallery_path"]
            it["sample_prompt"] = _sample_prompt(img.get("prompt"))
            continue
        rel = _text(it.get("image_path")).strip()
        if rel:
            it["image_url"] = "/api/comic/output/%s" % rel.replace("comic_outputs/", "", 1)
            it["sample_prompt"] = ""
        else:
            it["image_url"] = ""
            it["sample_prompt"] = ""


def _sample_prompt(raw: Any) -> str:
    """复用角色卡的「从 workflow JSON 提取自然语言提示词」逻辑（避免两份实现漂移）。"""
    from . import service

    return service._extract_prompt_text(raw)


def _attach_links(items: list[dict]) -> None:
    """批量补充每个演员关联的漫画角色（一次查询，避免 N+1）。"""
    ids = [int(it["id"]) for it in items if it.get("id") is not None]
    grouped: dict[int, list[dict]] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = db.query_all(
            "SELECT l.actor_id, l.character_id, l.role_note, l.project_id, "
            "       c.name AS character_name, p.name AS project_name "
            "FROM actor_character_links l "
            "LEFT JOIN comic_characters c ON c.id = l.character_id "
            "LEFT JOIN comic_projects   p ON p.id = l.project_id "
            "WHERE l.actor_id IN (%s) ORDER BY l.id ASC" % placeholders,
            tuple(ids),
        )
        for r in rows:
            row = db.row_to_dict(r) or {}
            grouped.setdefault(int(row["actor_id"]), []).append(
                {
                    "character_id": row.get("character_id"),
                    "character_name": row.get("character_name") or "（角色已删除）",
                    "project_id": row.get("project_id"),
                    "project_name": row.get("project_name") or "",
                    "role_note": row.get("role_note") or "",
                }
            )
    for it in items:
        links = grouped.get(int(it["id"]), [])
        it["characters"] = links
        it["link_count"] = len(links)


def _decorate(items: list[dict], with_links: bool = True) -> list[dict]:
    for it in items:
        it["anchor"] = build_actor_anchor(it)
        # 展示「最近一次实际出图种子」；尚未出过图的演员没有 seed 记录，
        # 退回到由 base_seed 确定性派生的值（即首次重生成会用到的种子），保证前后一致。
        stored = it.get("seed")
        it["seed"] = int(stored) if stored is not None else actor_seed(it)
        it["is_generating"] = it.get("status") == "generating"
    _attach_image(items)
    if with_links:
        _attach_links(items)
    return items


def list_actors(keyword: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0) -> list[dict]:
    """演员列表（按更新时间倒序）。``keyword`` 同时匹配名字与别名。"""
    sql = "SELECT * FROM actors"
    params: list[Any] = []
    kw = _text(keyword).strip()
    if kw:
        sql += " WHERE (name LIKE ? OR aliases LIKE ?)"
        params.extend(["%" + kw + "%", "%" + kw + "%"])
    lim = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    off = max(0, int(offset or 0))
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([lim, off])
    return _decorate(db.rows_to_dicts(db.query_all(sql, params)))


def count_actors(keyword: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM actors"
    params: list[Any] = []
    kw = _text(keyword).strip()
    if kw:
        sql += " WHERE (name LIKE ? OR aliases LIKE ?)"
        params.extend(["%" + kw + "%", "%" + kw + "%"])
    return int((db.query_one(sql, params) or {"n": 0})["n"] or 0)


def get_actor(actor_id: int) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM actors WHERE id=?", (actor_id,)))
    if row is None:
        raise AIBARError("not_found", "演员不存在")
    return _decorate([row])[0]


def _get_character_row(character_id: int) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM comic_characters WHERE id=?", (character_id,)))
    if row is None:
        raise AIBARError("not_found", "漫画角色不存在")
    return row


# ---------------------------------------------------------------- 写入


def _resolve_gallery_image(raw: Any) -> tuple[str | None, str]:
    """校验图库图片存在，返回 ``(image_id, 该图出图提示词)``。空输入返回 ``(None, "")``。"""
    key = _text(raw).strip()
    if not key:
        return None, ""
    img = db.row_to_dict(db.query_one("SELECT id, prompt FROM images WHERE id=?", (key,)))
    if img is None:
        raise AIBARError("not_found", "关联图库图片不存在：%s" % key)
    return img["id"], _sample_prompt(img.get("prompt"))


def create_actor(payload: dict) -> dict:
    """新增演员。三种来源：

    - ``manual``：手填定妆信息；
    - ``gallery``：传 ``image_id``，把图库里已生成的人物收为演员，
      外貌留空时自动用该图的出图提示词填充（与角色卡导入行为一致）；
    - ``character``：传 ``character_id``，把某部漫画里已调好的角色**提升**为全局演员，
      并自动建立演员↔角色关联（这条链路是「把生成的人物作为我的演员」的主路径）。
    """
    if count_actors() >= MAX_ACTORS:
        raise AIBARError("invalid_input", "演员数已达上限 %d 个" % MAX_ACTORS)

    src_char: dict | None = None
    char_id = _opt_int(payload.get("character_id"))
    if char_id is not None:
        src_char = _get_character_row(char_id)

    # 名字：显式传入优先；从角色提升时默认沿用角色名
    name = characters.normalize_name(payload.get("name")) or (
        characters.normalize_name(src_char.get("name")) if src_char else ""
    )
    if not name:
        raise AIBARError("invalid_input", "缺少必填字段：演员名")
    if db.query_one("SELECT id FROM actors WHERE name=?", (name,)) is not None:
        raise AIBARError("invalid_input", "同名演员已存在：%s" % name)

    def _pick(key: str) -> str:
        """取值优先级：请求体 → 来源角色 → 空串。"""
        if key in payload and payload.get(key) is not None:
            return _text(payload.get(key))
        if src_char is not None:
            return _text(src_char.get(key))
        return ""

    image_id, sample_prompt = _resolve_gallery_image(
        payload.get("image_id") if "image_id" in payload else (src_char or {}).get("image_id")
    )
    appearance = _pick("appearance")
    if not appearance.strip() and sample_prompt:
        # 「自动根据出图提示词填充定妆描述」：与角色卡导入图库图片的行为保持一致
        appearance = sample_prompt

    source_type = "character" if src_char else ("gallery" if image_id else "manual")
    base_seed = _opt_int(payload.get("base_seed"))
    if base_seed is None:
        # 从角色提升为演员时，沿用该角色的基准种子——这样首次重生成能复现角色原本的脸，
        # 而不是另起一个由演员名派生的新基准（那会让「把生成的人物作为我的演员」失去意义）。
        if src_char is not None and src_char.get("base_seed") is not None:
            base_seed = int(src_char["base_seed"])
        else:
            base_seed = default_base_seed(name)
    ts = now()
    cur = db.execute(
        "INSERT INTO actors (name, aliases, appearance, outfit, palette, negative, seed_offset, base_seed, "
        "notes, image_id, image_path, workflow, status, error_message, source_type, source_char_id, "
        "source_project_id, use_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name,
            characters.join_aliases(characters.parse_aliases(_pick("aliases"))),
            appearance,
            _pick("outfit"),
            _pick("palette"),
            _pick("negative"),
            _clamp_offset(payload.get("seed_offset"), int((src_char or {}).get("seed_offset") or 0)),
            base_seed,
            _text(payload.get("notes")),
            image_id,
            "",
            _text(payload.get("workflow")).strip(),
            "ready",
            "",
            source_type,
            src_char.get("id") if src_char else None,
            src_char.get("project_id") if src_char else None,
            0,
            ts, ts,
        ),
    )
    actor_id = int(cur.lastrowid)
    # 从角色提升为演员时，顺手建立关联：用户的意图就是「这个角色由这个演员出演」。
    # 此处不回写角色（角色本来就是定妆信息的来源，回写等于原地覆盖，纯属多余）。
    if src_char is not None:
        _link(actor_id, src_char, role_note=_text(payload.get("role_note")), apply_to_character=False)
    safe_log(_LOGGER, 20, "actor_created", actor_id=actor_id, source_type=source_type)
    return get_actor(actor_id)


def update_actor(actor_id: int, payload: dict) -> dict:
    row = db.row_to_dict(db.query_one("SELECT * FROM actors WHERE id=?", (actor_id,)))
    if row is None:
        raise AIBARError("not_found", "演员不存在")
    name = characters.normalize_name(payload.get("name", row["name"])) or row["name"]
    if name != row["name"]:
        dup = db.query_one("SELECT id FROM actors WHERE name=? AND id<>?", (name, actor_id))
        if dup is not None:
            raise AIBARError("invalid_input", "同名演员已存在：%s" % name)

    aliases = (
        characters.join_aliases(characters.parse_aliases(payload.get("aliases")))
        if "aliases" in payload else row["aliases"]
    )
    appearance = _text(payload.get("appearance", row["appearance"]))
    image_id = row.get("image_id") or None
    image_path = _text(row.get("image_path"))
    if "image_id" in payload:
        new_id, sample_prompt = _resolve_gallery_image(payload.get("image_id"))
        image_id = new_id
        if new_id is None:
            image_path = ""  # 解除关联时同时清掉自有产出路径，避免界面显示一张陈旧的图
        elif not _text(payload.get("appearance", "")).strip() and sample_prompt:
            appearance = sample_prompt

    base_seed = _opt_int(payload.get("base_seed"))
    if base_seed is None:
        base_seed = _opt_int(row.get("base_seed"))
    db.execute(
        "UPDATE actors SET name=?, aliases=?, appearance=?, outfit=?, palette=?, negative=?, seed_offset=?, "
        "base_seed=?, notes=?, image_id=?, image_path=?, workflow=?, updated_at=? WHERE id=?",
        (
            name, aliases, appearance,
            _text(payload.get("outfit", row["outfit"])),
            _text(payload.get("palette", row["palette"])),
            _text(payload.get("negative", row["negative"])),
            _clamp_offset(payload.get("seed_offset", row["seed_offset"]), int(row["seed_offset"] or 0)),
            base_seed,
            _text(payload.get("notes", row["notes"])),
            image_id, image_path,
            _text(payload.get("workflow", row["workflow"])).strip(),
            now(), actor_id,
        ),
    )
    # 「改演员即批量校正所有关联角色」：定妆信息变了就同步下去，否则演员库只是个摆设
    if _truthy(payload.get("apply_to_characters"), fallback=True):
        apply_to_characters(actor_id)
    return get_actor(actor_id)


def delete_actor(actor_id: int) -> None:
    """删除演员。同时解除全部角色关联，但**不删角色**——角色属于漫画，不该被演员带走。"""
    get_actor(actor_id)
    with db.tx():
        db.execute("DELETE FROM actor_character_links WHERE actor_id=?", (actor_id,))
        db.execute("DELETE FROM actors WHERE id=?", (actor_id,))
    safe_log(_LOGGER, 20, "actor_deleted", actor_id=actor_id)


# ---------------------------------------------------------------- 演员 ↔ 角色关联


def _apply_actor_fields(actor: dict, character_id: int) -> None:
    """把演员定妆信息写进角色卡（名字与别名不动，见 ``INHERIT_FIELDS`` 注释）。"""
    db.execute(
        "UPDATE comic_characters SET appearance=?, outfit=?, palette=?, negative=?, image_id=?, updated_at=? "
        "WHERE id=?",
        (
            _text(actor.get("appearance")),
            _text(actor.get("outfit")),
            _text(actor.get("palette")),
            _text(actor.get("negative")),
            actor.get("image_id") or None,
            now(), character_id,
        ),
    )


def _link(actor_id: int, char_row: dict, role_note: str = "", apply_to_character: bool = True) -> None:
    """建立/改绑关联。一个角色只能绑一个演员，已绑别人时视为**改绑**。"""
    character_id = int(char_row["id"])
    existing = db.row_to_dict(
        db.query_one("SELECT * FROM actor_character_links WHERE character_id=?", (character_id,))
    )
    with db.tx():
        if existing is None:
            db.execute(
                "INSERT INTO actor_character_links (actor_id, character_id, project_id, role_note, created_at) "
                "VALUES (?,?,?,?,?)",
                (actor_id, character_id, int(char_row.get("project_id") or 0), role_note, now()),
            )
        else:
            db.execute(
                "UPDATE actor_character_links SET actor_id=?, project_id=?, role_note=? WHERE character_id=?",
                (actor_id, int(char_row.get("project_id") or 0), role_note or existing.get("role_note") or "",
                 character_id),
            )
            if int(existing["actor_id"]) != actor_id:
                db.execute(
                    "UPDATE actors SET use_count=MAX(0, use_count-1) WHERE id=?", (existing["actor_id"],)
                )
        if existing is None or int(existing["actor_id"]) != actor_id:
            db.execute("UPDATE actors SET use_count=use_count+1, updated_at=? WHERE id=?", (now(), actor_id))
        if apply_to_character:
            actor = db.row_to_dict(db.query_one("SELECT * FROM actors WHERE id=?", (actor_id,)))
            if actor is not None:
                _apply_actor_fields(actor, character_id)


def link_character(actor_id: int, payload: dict) -> dict:
    """把演员关联到一个漫画角色。默认把定妆信息同步给角色（以演员为基准）。"""
    get_actor(actor_id)
    character_id = _opt_int(payload.get("character_id"))
    if character_id is None:
        raise AIBARError("invalid_input", "缺少必填字段：角色 id")
    char_row = _get_character_row(character_id)
    _link(
        actor_id,
        char_row,
        role_note=_text(payload.get("role_note")),
        apply_to_character=_truthy(payload.get("apply"), fallback=True),
    )
    return get_actor(actor_id)


def unlink_character(actor_id: int, character_id: int) -> dict:
    get_actor(actor_id)
    row = db.query_one(
        "SELECT id FROM actor_character_links WHERE actor_id=? AND character_id=?", (actor_id, character_id)
    )
    if row is None:
        raise AIBARError("not_found", "该演员未关联此角色")
    with db.tx():
        db.execute(
            "DELETE FROM actor_character_links WHERE actor_id=? AND character_id=?", (actor_id, character_id)
        )
        db.execute("UPDATE actors SET use_count=MAX(0, use_count-1), updated_at=? WHERE id=?", (now(), actor_id))
    return get_actor(actor_id)


def apply_to_characters(actor_id: int) -> dict:
    """把演员当前定妆信息推送给全部关联角色，返回被更新的角色数。"""
    actor = db.row_to_dict(db.query_one("SELECT * FROM actors WHERE id=?", (actor_id,)))
    if actor is None:
        raise AIBARError("not_found", "演员不存在")
    rows = db.query_all("SELECT character_id FROM actor_character_links WHERE actor_id=?", (actor_id,))
    updated = 0
    with db.tx():
        for r in rows:
            if db.query_one("SELECT id FROM comic_characters WHERE id=?", (r["character_id"],)) is None:
                continue
            _apply_actor_fields(actor, int(r["character_id"]))
            updated += 1
    return {"actor_id": actor_id, "updated": updated}


def create_character_from_actor(project_id: int, payload: dict) -> dict:
    """以演员为基准，在某部漫画里新建角色卡并建立关联。

    这是「后续漫画以演员为基准保持一致性」的落地入口：角色卡的外貌/服装/配色/排除项
    全部继承演员，因此新漫画里的每一页锚点与演员定妆**逐字一致**。
    """
    from . import service

    actor_id = _opt_int(payload.get("actor_id"))
    if actor_id is None:
        raise AIBARError("invalid_input", "缺少必填字段：演员 id")
    actor = get_actor(actor_id)
    body = {
        "name": characters.normalize_name(payload.get("name")) or actor["name"],
        "aliases": payload.get("aliases", actor.get("aliases") or ""),
        "appearance": actor.get("appearance") or "",
        "outfit": actor.get("outfit") or "",
        "palette": actor.get("palette") or "",
        "negative": actor.get("negative") or "",
        "seed_offset": actor.get("seed_offset") or 0,
        "image_id": actor.get("image_id") or "",
    }
    if "is_main" in payload:
        body["is_main"] = payload.get("is_main")
    char = service.create_character(project_id, body)
    _link(actor_id, char, role_note=_text(payload.get("role_note")), apply_to_character=False)
    return {"character": service.get_character(char["id"]), "actor": get_actor(actor_id)}


def character_actor_map(project_id: int) -> dict[str, dict]:
    """项目下「角色 id → 演员简要」映射，供角色卡列表标注演员归属。"""
    rows = db.query_all(
        "SELECT l.character_id, l.actor_id, l.role_note, a.name AS actor_name, a.status "
        "FROM actor_character_links l JOIN actors a ON a.id = l.actor_id WHERE l.project_id=?",
        (project_id,),
    )
    out: dict[str, dict] = {}
    for r in rows:
        row = db.row_to_dict(r) or {}
        out[str(row["character_id"])] = {
            "actor_id": row["actor_id"],
            "actor_name": row.get("actor_name") or "",
            "role_note": row.get("role_note") or "",
            "status": row.get("status") or "ready",
        }
    return out


# ---------------------------------------------------------------- 重新生成定妆图


def _default_workflow() -> str:
    """挑一个可用工作流：演员自身未指定时，沿用漫画项目里最近用过的默认工作流。"""
    row = db.query_one(
        "SELECT default_workflow FROM comic_projects WHERE default_workflow<>'' "
        "ORDER BY updated_at DESC, id DESC LIMIT 1"
    )
    if row is not None and _text(row["default_workflow"]).strip():
        return _text(row["default_workflow"]).strip()
    row = db.query_one("SELECT filename FROM workflows ORDER BY synced_at DESC, id DESC LIMIT 1")
    return _text(row["filename"]).strip() if row is not None else ""


def _purge_old_actor_images(dest_dir: Path, actor_id: int, keep: Path) -> int:
    """清理该演员的历史定妆图（旁路），返回实际删除张数。

    **绝不上抛异常**——清理只是省磁盘的旁路动作，失败最坏结果是留几张旧图。
    曾经这里只捕 ``(OSError, ValueError)``，结果在「删除被运行环境安全策略
    拦截并以 ``SystemExit`` 中断」的场景下，异常一路穿透 ``generate_once``
    的 ``except Exception``，把**已经出图成功**的演员打成 failed，
    用户只看到一句「生成异常：SystemExit」，几十秒 GPU 白跑。
    """
    return purge_stale(dest_dir, "%d_*.png" % int(actor_id), keep=keep)


def _save_actor_image(actor_id: int, data: bytes) -> str:
    """落盘定妆图并清理该演员的历史产出；返回相对 DATA_DIR 的受控路径。

    次序是**先写新图、再清旧图**：出图要经过 ComfyUI 排队 + 几十秒 GPU，
    是整个链路里最贵的一步，绝不能让「删旧文件」这种免费动作把它拖下水。
    """
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / ACTOR_OUTPUT_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    dest = dest_dir / ("%d_%s.png" % (int(actor_id), digest))
    dest.write_bytes(data)
    _purge_old_actor_images(dest_dir, actor_id, dest)
    return str(dest.relative_to(Config.DATA_DIR))


def _register_gallery(saved_rel: str, prompt_text: str, workflow: str) -> str | None:
    """把定妆图登记进图库（旁路增强，失败只记日志）。"""
    try:
        from sync import scanner

        return scanner.register_image(
            Path(Config.DATA_DIR) / saved_rel, prompt=prompt_text, workflow_link=workflow
        )
    except Exception as exc:
        safe_log(_LOGGER, 30, "actor_gallery_register_failed", error_type=type(exc).__name__)
        return None


def _claim_regenerate(actor_id: int) -> bool:
    """原子占位：把演员置为 generating。已在生成中则返回 False（防重复提交）。"""
    cur = db.execute(
        "UPDATE actors SET status='generating', error_message='', updated_at=? "
        "WHERE id=? AND status<>'generating'",
        (now(), actor_id),
    )
    try:
        return int(cur.rowcount or 0) > 0
    except Exception:
        return True


def _human(code: str, message: str) -> str:
    try:
        from . import diagnostics

        return diagnostics.human_error(code, message)
    except Exception:
        return message or code


def _describe_exception(exc: BaseException) -> str:
    """把异常压成一句人能看懂的话（供 UI 的 error_message 直接展示）。

    只显示 ``type(exc).__name__`` 是不够的：``SystemExit``、``KeyboardInterrupt``
    这类 BaseException 子类丢给用户的就是「生成异常：SystemExit」——既看不出
    错在哪一步，也看不出是不是自己操作的问题。这里补上出错位置和 ``str(exc)``，
    并对 SystemExit 单独解释（它通常不是业务逻辑失败，而是运行环境在中断
    某个旁路操作，如删除文件被安全策略拦截）。
    """
    name = type(exc).__name__
    text = str(exc).strip()
    where = ""
    tb = exc.__traceback__
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        where = "%s:%d" % (Path(tb.tb_frame.f_code.co_filename).name, tb.tb_lineno)

    if name == "SystemExit":
        head = "生成被运行环境中断（SystemExit%s）" % (" @ %s" % where if where else "")
        if text:
            return "%s：%s" % (head, text)
        return head + "，多为删除/写盘等旁路操作被安全策略拦截，可在终端直接启动服务规避"

    parts = [name]
    if where:
        parts.append("@ %s" % where)
    if text:
        parts.append(text)
    return " ".join(parts)


def _regenerate_worker(actor_id: int, workflow: str, prompt_text: str, negative: str, seed: int) -> None:
    """后台执行一次定妆出图。绝不抛异常——失败只写 ``status='failed'`` + 可读原因。"""
    from . import runner

    try:
        data, code, message = runner.generate_once(
            workflow, prompt_text, negative, seed, timeout=REGENERATE_TIMEOUT
        )
        if not data:
            db.execute(
                "UPDATE actors SET status='failed', error_message=?, updated_at=? WHERE id=?",
                (_human(code, message), now(), actor_id),
            )
            safe_log(_LOGGER, 30, "actor_regenerate_failed", actor_id=actor_id, error_code=code)
            return
        saved = _save_actor_image(actor_id, data)
        image_id = _register_gallery(saved, prompt_text, workflow)
        # 关键：把「本次实际出图所用种子」写进 seed 列；base_seed 是确定性基准，
        # 绝不能覆盖——否则每次 randomize=False 重生成都会因为基准漂移而换一张脸，
        # 破坏「以演员为基准保持角色一致性」的核心承诺。
        db.execute(
            "UPDATE actors SET status='ready', error_message='', image_path=?, image_id=COALESCE(?, image_id), "
            "workflow=?, seed=?, updated_at=? WHERE id=?",
            (saved, image_id, workflow, int(seed), now(), actor_id),
        )
        safe_log(_LOGGER, 20, "actor_regenerate_done", actor_id=actor_id)
    except BaseException as exc:  # 后台线程绝不静默死亡
        # 只记 type(exc).__name__ 会让 UI 显示「生成异常：SystemExit」这种毫无信息量的
        # 字符串，用户既不知道错在哪也不知道怎么绕。这里带上出错位置与可读说明。
        detail = _describe_exception(exc)
        try:
            db.execute(
                "UPDATE actors SET status='failed', error_message=?, updated_at=? WHERE id=?",
                ("生成异常：%s" % detail, now(), actor_id),
            )
        except Exception:
            pass
        safe_log(_LOGGER, 40, "actor_regenerate_crashed", actor_id=actor_id,
                 error_type=type(exc).__name__, detail=detail[:300])


def regenerate_actor(actor_id: int, payload: dict | None = None) -> dict:
    """重新生成演员定妆图（后台线程，立即返回 ``status='generating'``）。

    请求体（全部可选）：
    - ``workflow``  指定出图工作流；省略时沿用演员已用过的，再省略取项目默认；
    - ``prompt``    追加描述（如「换成雪地背景」），不改动演员定妆字段；
    - ``randomize`` 为真则换一张脸（新种子）；默认沿用确定性种子，只提升画质/构图；
    - ``seed``      直接指定种子（优先级最高）。
    """
    body = payload or {}
    actor = get_actor(actor_id)

    workflow = _text(body.get("workflow")).strip() or _text(actor.get("workflow")).strip() or _default_workflow()
    if not workflow:
        raise AIBARError("workflow_missing", "本机没有可用的出图工作流，请先同步 ComfyUI 工作流")

    prompt_text = build_actor_prompt(actor, extra=_text(body.get("prompt")))
    if not prompt_text.strip():
        raise AIBARError("invalid_input", "演员缺少定妆描述，无法生成：请先填写外貌 / 服装")

    seed = _opt_int(body.get("seed"))
    if seed is None:
        seed = actor_seed(actor, randomize=_truthy(body.get("randomize")))

    if not _claim_regenerate(actor_id):
        raise AIBARError("conflict", "该演员正在生成中，请稍后再试")

    threading.Thread(
        target=_regenerate_worker,
        args=(actor_id, workflow, prompt_text, _text(actor.get("negative")), int(seed)),
        name="aibar-actor-regen-%d" % actor_id,
        daemon=True,
    ).start()

    return {
        "actor": get_actor(actor_id),
        "workflow": workflow,
        "prompt": prompt_text,
        "seed": int(seed),
    }


__all__ = [
    "MAX_ACTORS",
    "ACTOR_STATUS",
    "INHERIT_FIELDS",
    "build_actor_anchor",
    "build_actor_prompt",
    "actor_seed",
    "default_base_seed",
    "list_actors",
    "count_actors",
    "get_actor",
    "create_actor",
    "update_actor",
    "delete_actor",
    "link_character",
    "unlink_character",
    "apply_to_characters",
    "create_character_from_actor",
    "character_actor_map",
    "regenerate_actor",
]
