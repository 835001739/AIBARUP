"""M14 · 组图（Shot Group）：同一人物的连贯动作序列，出完可连播成动画。

**为什么单独建一组表而不是复用 comic_pages**：``comic_pages`` 的每一页是**不同场景**
的叙事画面，页数跟着剧情节奏走；组图的每一帧是**同一场景、同一人物，只有动作在变**。
硬塞进 comic_pages 会让「分镜页数」与「动作帧数」两个语义互相污染，也会让分镜的剧情
扩写逻辑误伤动作帧。

**「强制预设提示词」是本模块的核心**。用户在组图上一次性设定：

    preset_prefix    场景 / 环境 / 画风（所有帧共用）
    preset_suffix    镜头 / 光照 / 画质（所有帧共用）
    preset_negative  强制负面词（所有帧共用）
    actor_id         绑定演员 → 人物锚点（所有帧共用）

每一帧**只提供 ``action_text``**（这一帧做什么动作），最终提示词由组图统一拼装::

    [人物锚点]，[一致性指令]，[preset_prefix]，[action_text]，[preset_suffix]

顺序不是随意的——图像模型对提示词**靠前**的描述权重更高：

- **人物锚点必须排在最前**（M12 已验证：设定抢在锚点前面会把人物特征挤到后面去）；
- 锚点之后紧跟一致性指令，明确要求模型维持同一张脸 / 发型 / 服装；
- 场景画风（每帧都一样、信息量低）退到锚点之后；
- 动作文本放在画风之后、镜头之前——它是唯一逐帧变化的量，位置越靠后对构图的影响
  越接近「微调」，这正是连贯动作想要的（人物不动，只有动作在变）。

``shot_frames.prompt_text`` 虽然落库，但**恒由预设拼装而来**：改了预设后调
``refresh_prompts`` 会用当前预设覆盖全部帧，单帧改不动——这就是「强制」的含义，
也是连播时人物不漂、画风不跳的根本保障。

**种子策略**是连贯动作的另一个支点::

    seed = (base_seed + order_idx * seed_step) % 2**31-1

``seed_step`` 默认 **0**，即**所有帧共用同一个种子**——同种子 + 不同提示词时，
模型会保留构图与人物特征只改动文本描述的部分，动作因此连贯；若每帧都换种子，
脸会跟着噪声一起漂，连播起来就是「同一个人每帧都在换脸」。想要动作幅度更大时
把 seed_step 调大即可（代价是脸略微漂移）。

设计约束（沿用 M12 / M13）：
- **串行出图**：ComfyUI 单卡排队，并发只会互相抢显存并拉长总时长；
- **先写后清**：出图要排 ComfyUI 队列 + 几十秒 GPU，是整条链路最贵的一步，
  清理旧图这种免费动作绝不能把它拖下水（见 ``core.fileops`` 模块说明）；
- **后台线程绝不上抛异常**：失败只写 ``status='failed'`` + 可读原因；
- **绝不污染公共状态**：所有资源（取消事件、运行标记）按 group_id 隔离。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.fileops import purge_stale
from core.logging_setup import get_logger, safe_log
from . import actors
from . import characters

_LOGGER = get_logger("aibar.comic.groups")

# 组图总量上限：组图是「动作序列」而不是图库，超量说明用法跑偏了
MAX_GROUPS = 200

# 单个组图的帧数上限：再多连播起来也看不清，且会显著拉长出图总时长
MAX_FRAMES = 60

# 列表接口默认返回上限
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

GROUP_STATUS = {"idle", "generating", "ready", "partial", "failed"}
FRAME_STATUS = {"pending", "generating", "done", "failed"}

# 连播默认帧间隔（毫秒）。300ms ≈ 3.3fps，是「能看清动作又不卡顿」的经验值
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 40
MAX_INTERVAL = 3000

# 组图产出图落盘子目录（放在 comic_outputs 下，直接复用 /api/comic/output 的路径穿越防护）
GROUPS_OUTPUT_SUBDIR = "shotgroups"

# 单帧出图等待上限（秒）。组图是串行跑整组，单帧给足余量，总时长由前端进度条反馈
FRAME_TIMEOUT = 240.0

# 种子上限：ComfyUI 的 seed/noise_seed 通常要求 < 2^32，这里取 2^31-1 稳妥
_SEED_MODULUS = 2**31 - 1

# 组图基准种子的派生盐（与其他模块的派生盐区分开，避免不同实体撞种子）
_SEED_SALT = 20260901

# 运行中的出图任务：group_id → 取消事件。按 group_id 隔离，互不干扰。
_CANCELS: dict[int, threading.Event] = {}
_RUNNING: set[int] = set()
_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 小工具


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, fallback: int = 0) -> int:
    got = _opt_int(value)
    return fallback if got is None else got


def _clamp(value: Any, fallback: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, _int(value, fallback)))


def _truthy(value: Any, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return fallback


def _human(code: str, message: str) -> str:
    try:
        from . import diagnostics

        return diagnostics.human_error(code, message)
    except Exception:
        return message or code


def _describe_exception(exc: BaseException) -> str:
    """把异常压成一句人能看懂的话（与 actors._describe_exception 同款）。

    只显示 ``type(exc).__name__`` 会让 UI 出现「生成异常：SystemExit」这种毫无
    信息量的字符串。这里补上出错位置与 ``str(exc)``，并对 SystemExit 单独解释。
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


# ---------------------------------------------------------------- 提示词组装（模块核心）


def resolve_anchor(group: dict) -> str:
    """解析该组图的人物锚点：**绑定优先，手填补充**。

    优先级：演员定妆锚点 → 角色卡锚点 → ``anchor_override`` 手填。
    两者同时存在时以「演员锚点，手填补充」拼接——手填通常用于补充本组图特有的
    临时设定（如「今天扎马尾」），而不是覆盖定妆。

    锚点**不落库快照**：改演员定妆后调一次 ``refresh_prompts``，全部帧同步更新，
    这正是「以演员为唯一权威来源」的收益，也是「强制」的一部分。
    """
    parts: list[str] = []

    actor_id = _opt_int(group.get("actor_id"))
    if actor_id:
        try:
            actor = actors.get_actor(actor_id)
        except Exception:
            actor = None
        anchor = actors.build_actor_anchor(actor) if actor else ""
        if anchor:
            parts.append(anchor)

    character_id = _opt_int(group.get("character_id"))
    if character_id and not parts:
        row = db.query_one("SELECT * FROM comic_characters WHERE id=?", (character_id,))
        anchor = characters.build_character_anchor(db.row_to_dict(row)) if row else ""
        if anchor:
            parts.append(anchor)

    override = _text(group.get("anchor_override")).strip()
    if override:
        parts.append(override.strip("，, "))

    return "，".join([p for p in parts if p])


def compose_frame_prompt(group: dict, action_text: str = "") -> str:
    """按**强制预设**拼装一帧的提示词。顺序见模块 docstring，改动前务必读完。

    Args:
        group: 组图行（需含 preset_prefix / preset_suffix / actor_id 等字段）。
        action_text: 该帧的动作描述，是唯一逐帧变化的量。

    Returns:
        拼好的提示词。全部字段为空时返回空串（调用方据此报「缺少预设」）。
    """
    parts: list[str] = []

    anchor = resolve_anchor(group)
    if anchor:
        parts.append(anchor)
        # 一致性指令只在真的有锚点时才追加——空镜头挂一句「保持同一张脸」没有意义
        parts.append(characters.CONSISTENCY_DIRECTIVE)

    prefix = _text(group.get("preset_prefix")).strip().strip("，, ")
    if prefix:
        parts.append(prefix)

    action = _text(action_text).strip().strip("，, ")
    if action:
        parts.append(action)

    suffix = _text(group.get("preset_suffix")).strip().strip("，, ")
    if suffix:
        parts.append(suffix)

    return "，".join([p for p in parts if p])


def compose_frame_negative(group: dict) -> str:
    """负面提示词 = 组图强制负面 + 角色排除项 + 反漂移词（去重后合并）。"""
    parts: list[str] = []

    forced = _text(group.get("preset_negative")).strip().strip("，, ")
    if forced:
        parts.append(forced)

    actor_id = _opt_int(group.get("actor_id"))
    if actor_id:
        try:
            actor = actors.get_actor(actor_id)
        except Exception:
            actor = None
        if actor and _text(actor.get("negative")).strip():
            parts.append(_text(actor.get("negative")).strip())

    # 反漂移词恒在：组图的全部意义就是「连播起来像同一个人在动」，
    # 少这一句等于把最核心的保障交给运气。
    parts.append(characters.ANTI_DRIFT_NEGATIVE)

    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(p.strip())
    return ", ".join(out)


def frame_seed(group: dict, order_idx: int) -> int:
    """帧级种子：``base_seed + order_idx * seed_step``（确定性、可复现）。

    ``seed_step`` 为 0（默认）时全部帧共用同一种子——**这是连贯动作的关键**：
    同种子 + 不同提示词，模型保留人物与构图、只改动文本描述的部分，连播起来
    就是同一个人在做连续动作。每帧换种子则脸会跟着噪声漂，连播即「换脸动画」。
    """
    base = _opt_int(group.get("base_seed"))
    if base is None:
        base = characters.derive_seed(_SEED_SALT, _int(group.get("id"), 0), 0, 0, "shotgroup")
    step = _clamp(group.get("seed_step"), 0, 0, 1_000_000)
    return (int(base) + int(order_idx) * step) % _SEED_MODULUS


def default_base_seed() -> int:
    """组图基准种子默认值：随机取一个，避免所有新建组图共用同一张脸。"""
    return int(hashlib.sha256(("%s|%s" % (_SEED_SALT, now())).encode("utf-8")).hexdigest()[:12], 16) % _SEED_MODULUS


# ---------------------------------------------------------------- 读取与装饰


def _output_url(rel_or_path: str) -> str:
    """把落盘的相对路径（``comic_outputs/...``）转成前端可访问的 URL。"""
    rel = _text(rel_or_path).strip()
    if not rel:
        return ""
    return "/api/comic/output/%s" % rel.replace("comic_outputs/", "", 1)


def _attach_cover(items: list[dict]) -> None:
    """批量补充组图封面 URL（优先图库副本，退回自有产出路径）。

    一次查库搞定，避免列表页 N+1。
    """
    want = [it.get("cover_image_id") for it in items if it.get("cover_image_id")]
    info: dict[str, dict] = {}
    if want:
        placeholders = ",".join("?" for _ in want)
        rows = db.query_all(
            "SELECT id, gallery_path FROM images WHERE id IN (%s)" % placeholders, tuple(want)
        )
        info = {r["id"]: db.row_to_dict(r) for r in rows}
    for it in items:
        img = info.get(it.get("cover_image_id"))
        if img and img.get("gallery_path"):
            it["cover_url"] = "/static/%s" % img["gallery_path"]
        else:
            it["cover_url"] = _output_url(it.get("cover_path"))
        it["is_generating"] = _text(it.get("status")) == "generating"


def _sync_counts(group_id: int) -> dict[str, int]:
    """重算并回写 ``frame_count`` / ``done_count`` / ``status``，返回计数快照。

    状态机：
    - 有帧在 generating → ``generating``（前端据此轮询进度）
    - 全部 done      → ``ready``
    - 部分 done      → ``partial``（有成功也有失败/待出）
    - 全部 failed    → ``failed``
    - 无帧           → ``idle``
    """
    row = db.query_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status='generating' THEN 1 ELSE 0 END) AS generating "
        "FROM shot_frames WHERE group_id=?",
        (group_id,),
    )
    snap = db.row_to_dict(row) or {}
    total = _int(snap.get("total"), 0)
    done = _int(snap.get("done"), 0)
    failed = _int(snap.get("failed"), 0)
    generating = _int(snap.get("generating"), 0)

    # 出图线程在跑时**强制**保持 generating：worker 每出完一帧就会重算计数，
    # 此时剩下的帧还是 pending，纯按计数会得出 partial，前端轮询看到它会误判
    # 「已经出完了」而停止进度条。用运行标记压住，只有收尾那次才落定真实状态。
    with _STATE_LOCK:
        running = group_id in _RUNNING

    if running:
        status = "generating"
    elif generating:
        status = "generating"
    elif total == 0 or (done == 0 and failed == 0):
        # 一帧都没出过 = idle。少了这一支，新建的组图会被标成 partial，
        # 前端显示「部分完成」——还没开始就谈部分完成是误导。
        status = "idle"
    elif done == total:
        status = "ready"
    elif done == 0:
        status = "failed"
    else:
        status = "partial"

    db.execute(
        "UPDATE shot_groups SET frame_count=?, done_count=?, status=?, updated_at=? WHERE id=?",
        (total, done, status, now(), group_id),
    )
    return {"total": total, "done": done, "failed": failed, "generating": generating, "status": status}


def _load_frames(group_id: int) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM shot_frames WHERE group_id=? ORDER BY order_idx ASC, id ASC", (group_id,)
    )
    items = db.rows_to_dicts(rows)
    for it in items:
        it["image_url"] = _output_url(it.get("image_path"))
    return items


def list_groups(keyword: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0) -> list[dict]:
    lim = max(1, min(MAX_LIMIT, _int(limit, DEFAULT_LIMIT)))
    off = max(0, _int(offset, 0))
    kw = _text(keyword).strip()
    where = ""
    params: list[Any] = []
    if kw:
        where = "WHERE g.name LIKE ? OR g.description LIKE ?"
        params = ["%%%s%%" % kw, "%%%s%%" % kw]
    rows = db.query_all(
        "SELECT g.*, f.image_id AS cover_image_id FROM shot_groups g "
        "LEFT JOIN shot_frames f ON f.group_id=g.id AND f.order_idx=( "
        "  SELECT MIN(order_idx) FROM shot_frames WHERE group_id=g.id AND status='done' "
        ") "
        "%s ORDER BY g.updated_at DESC, g.id DESC LIMIT ? OFFSET ?" % where,
        tuple(params + [lim, off]),
    )
    items = db.rows_to_dicts(rows)
    _attach_cover(items)
    return items


def count_groups(keyword: str | None = None) -> int:
    kw = _text(keyword).strip()
    if kw:
        return _int(
            db.query_scalar(
                "SELECT COUNT(*) FROM shot_groups WHERE name LIKE ? OR description LIKE ?",
                ("%%%s%%" % kw, "%%%s%%" % kw),
                default=0,
            ),
            0,
        )
    return _int(db.query_scalar("SELECT COUNT(*) FROM shot_groups", default=0), 0)


def get_group(group_id: int) -> dict:
    row = db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,))
    if row is None:
        raise AIBARError("not_found", "组图不存在：%d" % group_id)
    group = db.row_to_dict(row)
    frames = _load_frames(group_id)
    group["frames"] = frames
    group["frame_count"] = len(frames)
    group["done_count"] = sum(1 for f in frames if f.get("status") == "done")
    # 封面：最小 order_idx 且已出图的那一帧
    cover = next((f for f in frames if f.get("status") == "done" and f.get("image_path")), None)
    group["cover_url"] = _output_url(cover.get("image_path")) if cover else ""
    group["anchor_text"] = resolve_anchor(group)
    group["is_generating"] = _text(group.get("status")) == "generating"
    return group


# ---------------------------------------------------------------- 组图增删改


def _require_name(payload: dict, fallback: str = "未命名组图") -> str:
    name = _text(payload.get("name")).strip()
    return name or fallback


def create_group(payload: dict) -> dict:
    """新建组图。可同时给出首批动作帧（``actions`` 数组，一行一个动作）。

    body:
    - ``name``          组图名（可空，默认「未命名组图」）；
    - ``actor_id``      绑定演员（人物一致性的锚点来源，强烈建议填）；
    - ``character_id``  绑定角色卡（未绑演员时作为锚点来源）；
    - ``preset_prefix`` / ``preset_suffix`` / ``preset_negative``  强制预设提示词；
    - ``anchor_override``  手填人物设定（补充在绑定锚点之后）；
    - ``actions``       首批动作文本数组；也可给 ``action_text``（单个字符串，按行拆）；
    - ``workflow`` / ``base_seed`` / ``seed_step`` / ``frame_interval`` / ``loop_play``。
    """
    body = payload or {}
    total = _int(db.query_scalar("SELECT COUNT(*) FROM shot_groups", default=0), 0)
    if total >= MAX_GROUPS:
        raise AIBARError("limit_exceeded", "组图数量已达上限（%d 个），请先删除不再使用的组图" % MAX_GROUPS)

    actor_id = _opt_int(body.get("actor_id"))
    if actor_id is not None:
        try:
            actors.get_actor(actor_id)
        except AIBARError:
            raise AIBARError("not_found", "绑定的演员不存在：%d" % actor_id)

    values = {
        "name": _require_name(body, "未命名组图")[:80],
        "actor_id": actor_id,
        "character_id": _opt_int(body.get("character_id")),
        "description": _text(body.get("description")).strip()[:500],
        "preset_prefix": _text(body.get("preset_prefix")).strip(),
        "preset_suffix": _text(body.get("preset_suffix")).strip(),
        "preset_negative": _text(body.get("preset_negative")).strip(),
        "anchor_override": _text(body.get("anchor_override")).strip(),
        "workflow": _text(body.get("workflow")).strip(),
        "base_seed": _opt_int(body.get("base_seed")) or default_base_seed(),
        "seed_step": _clamp(body.get("seed_step"), 0, 0, 1_000_000),
        "frame_interval": _clamp(body.get("frame_interval"), DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL),
        "loop_play": 1 if _truthy(body.get("loop_play"), True) else 0,
        "status": "idle",
        "created_at": now(),
        "updated_at": now(),
    }
    cols = ", ".join(values.keys())
    marks = ", ".join("?" for _ in values)
    cur = db.execute("INSERT INTO shot_groups (%s) VALUES (%s)" % (cols, marks), tuple(values.values()))
    group_id = _int(cur.lastrowid, 0)

    actions = parse_actions(body)
    if actions:
        set_frames(group_id, actions)
    return get_group(group_id)


def parse_actions(body: dict) -> list[str]:
    """从请求体里取动作列表：``actions`` 数组，或 ``action_text`` 按行拆分。

    公开是因为路由层也要用（``PUT /groups/<id>/frames`` 直接吃前端的多行文本框）。
    """
    raw = body.get("actions")
    if isinstance(raw, list):
        lines = [_text(x) for x in raw]
    else:
        lines = _text(body.get("action_text")).splitlines()
    out: list[str] = []
    for line in lines:
        text = line.strip().strip("，,。.")
        if text:
            out.append(text)
    return out


def update_group(group_id: int, payload: dict) -> dict:
    """修改组图预设。``refresh``（默认 false）为真时按新预设重算全部帧提示词。

    只改预设不改帧 → 前端会提示「预设已改，需重算帧提示词」；
    ``refresh=true`` → 直接重算，出过的图保留（重算出图才会换图）。
    """
    body = payload or {}
    get_group(group_id)  # 存在性校验，不存在时抛 404

    allowed = (
        "name", "description", "preset_prefix", "preset_suffix", "preset_negative",
        "anchor_override", "workflow", "base_seed", "seed_step", "frame_interval", "loop_play",
    )
    sets: list[str] = []
    params: list[Any] = []

    for key in allowed:
        if key not in body:
            continue
        value = body.get(key)
        if key == "name":
            value = _text(value).strip()[:80] or "未命名组图"
        elif key == "description":
            value = _text(value).strip()[:500]
        elif key == "seed_step":
            value = _clamp(value, 0, 0, 1_000_000)
        elif key == "frame_interval":
            value = _clamp(value, DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL)
        elif key == "loop_play":
            value = 1 if _truthy(value, True) else 0
        elif key == "base_seed":
            value = _opt_int(value)
            if value is None:
                continue
        elif key in ("preset_prefix", "preset_suffix", "preset_negative", "anchor_override", "workflow"):
            value = _text(value).strip()
        sets.append("%s=?" % key)
        params.append(value)

    # actor_id 需要校验存在性，单独处理
    if "actor_id" in body:
        actor_id = _opt_int(body.get("actor_id"))
        if actor_id is not None:
            try:
                actors.get_actor(actor_id)
            except AIBARError:
                raise AIBARError("not_found", "绑定的演员不存在：%d" % actor_id)
        sets.append("actor_id=?")
        params.append(actor_id)

    if "character_id" in body:
        sets.append("character_id=?")
        params.append(_opt_int(body.get("character_id")))

    if sets:
        sets.append("updated_at=?")
        params.append(now())
        params.append(group_id)
        db.execute("UPDATE shot_groups SET %s WHERE id=?" % ", ".join(sets), tuple(params))

    if _truthy(body.get("refresh")):
        refresh_prompts(group_id)
    return get_group(group_id)


def delete_group(group_id: int) -> None:
    """删除组图及其全部帧。产出图一并清理（旁路，失败只记日志）。"""
    get_group(group_id)
    cancel_group(group_id)
    db.execute("DELETE FROM shot_frames WHERE group_id=?", (group_id,))
    db.execute("DELETE FROM shot_groups WHERE id=?", (group_id,))
    _purge_group_dir(group_id)


def _purge_group_dir(group_id: int) -> int:
    """清理该组图的产出目录（旁路）。**绝不上抛异常**——删不掉只是多占磁盘。"""
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / GROUPS_OUTPUT_SUBDIR / str(int(group_id))
    if not dest_dir.is_dir():
        return 0
    return purge_stale(dest_dir, "*_*.png")


# ---------------------------------------------------------------- 帧管理


def set_frames(group_id: int, actions: list[str]) -> dict:
    """**整组替换**动作帧（前端「一行一个动作」文本框的唯一落库入口）。

    为什么是替换而不是追加：组图的意义在于「帧序 = 动作时序」，用户编辑时是在
    调整整条序列（增删改某一步）。增量追加会让「前端显示的顺序」与「用户的意图」
    在反复编辑后逐渐脱节。

    已出图的帧会被**保留**（连同 image_path），只有动作文本与提示词被更新；
    序列变短时多出来的旧帧直接删除。
    """
    get_group(group_id)
    wanted = [_text(a).strip() for a in actions if _text(a).strip()]
    if len(wanted) > MAX_FRAMES:
        raise AIBARError("limit_exceeded", "单个组图最多 %d 帧，当前 %d 帧" % (MAX_FRAMES, len(wanted)))

    group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,))) or {}
    existing = _load_frames(group_id)

    keep = len(wanted)
    for idx, action in enumerate(wanted):
        prompt = compose_frame_prompt(group, action)
        negative = compose_frame_negative(group)
        seed = frame_seed(group, idx)
        if idx < len(existing):
            frame = existing[idx]
            db.execute(
                "UPDATE shot_frames SET action_text=?, prompt_text=?, negative_text=?, seed=?, "
                "order_idx=?, error_message=CASE WHEN status='failed' THEN '' ELSE error_message END, "
                "status=CASE WHEN status='failed' THEN 'pending' ELSE status END, updated_at=? WHERE id=?",
                (action, prompt, negative, seed, idx, now(), frame["id"]),
            )
        else:
            db.execute(
                "INSERT INTO shot_frames (group_id, order_idx, action_text, prompt_text, negative_text, "
                "seed, workflow, status, created_at, updated_at) VALUES (?,?,?,?,?,?,'','pending',?,?)",
                (group_id, idx, action, prompt, negative, seed, now(), now()),
            )

    # 序列变短：删掉多出来的旧帧（连同其产出图）
    stale = existing[keep:]
    for frame in stale:
        db.execute("DELETE FROM shot_frames WHERE id=?", (frame["id"],))
    if stale:
        _purge_frames(group_id, [f["id"] for f in stale])

    _sync_counts(group_id)
    _refresh_cover(group_id)
    return get_group(group_id)


def update_frame(frame_id: int, payload: dict) -> dict:
    """改单帧：只改动作文本（提示词恒由预设重算，改不了——这是「强制」的一部分）。"""
    body = payload or {}
    row = db.query_one("SELECT * FROM shot_frames WHERE id=?", (frame_id,))
    if row is None:
        raise AIBARError("not_found", "帧不存在：%d" % frame_id)
    frame = db.row_to_dict(row)
    group_id = _int(frame.get("group_id"))
    group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,))) or {}

    action = _text(body.get("action_text")).strip()
    if "action_text" in body:
        if not action:
            raise AIBARError("invalid_input", "动作描述不能为空")
        db.execute(
            "UPDATE shot_frames SET action_text=?, prompt_text=?, negative_text=?, seed=?, "
            "error_message=CASE WHEN status='failed' THEN '' ELSE error_message END, "
            "status=CASE WHEN status='failed' THEN 'pending' ELSE status END, updated_at=? WHERE id=?",
            (action, compose_frame_prompt(group, action), compose_frame_negative(group),
             frame_seed(group, _int(frame.get("order_idx"))), now(), frame_id),
        )
    if "order_idx" in body:
        db.execute(
            "UPDATE shot_frames SET order_idx=?, updated_at=? WHERE id=?",
            (_int(body.get("order_idx"), _int(frame.get("order_idx"))), now(), frame_id),
        )
    updated = db.row_to_dict(db.query_one("SELECT * FROM shot_frames WHERE id=?", (frame_id,))) or {}
    updated["image_url"] = _output_url(updated.get("image_path"))
    return updated


def delete_frame(frame_id: int) -> dict:
    """删单帧，并把后面的帧序整体前移（保持 order_idx 连续，连播不会跳号）。"""
    row = db.query_one("SELECT * FROM shot_frames WHERE id=?", (frame_id,))
    if row is None:
        raise AIBARError("not_found", "帧不存在：%d" % frame_id)
    frame = db.row_to_dict(row)
    group_id = _int(frame.get("group_id"))
    order_idx = _int(frame.get("order_idx"))

    db.execute("DELETE FROM shot_frames WHERE id=?", (frame_id,))
    rows = db.query_all(
        "SELECT id, order_idx FROM shot_frames WHERE group_id=? AND order_idx>? ORDER BY order_idx ASC",
        (group_id, order_idx),
    )
    for item in rows:
        db.execute(
            "UPDATE shot_frames SET order_idx=?, updated_at=? WHERE id=?",
            (_int(item["order_idx"]) - 1, now(), _int(item["id"])),
        )
    _purge_frames(group_id, [frame_id])
    _sync_counts(group_id)
    _refresh_cover(group_id)
    return {"deleted": frame_id, "group_id": group_id}


def _purge_frames(group_id: int, frame_ids: list[int]) -> int:
    """清理若干帧的产出图（旁路）。**绝不上抛异常**。"""
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / GROUPS_OUTPUT_SUBDIR / str(int(group_id))
    if not dest_dir.is_dir():
        return 0
    removed = 0
    for fid in frame_ids:
        removed += purge_stale(dest_dir, "%d_*.png" % int(fid))
    return removed


def refresh_prompts(group_id: int) -> dict:
    """按**当前预设**重算全部帧的提示词与种子（改预设后的同步入口）。

    这是「强制」的执行点：无论某帧此前存了什么 prompt_text，一律用当前预设覆盖。
    已出过的图**不会被删**——重算提示词只是把它们标成「提示词已更新，需重出」，
    是否重出由用户决定（避免手一抖把几十秒 GPU 的成果清掉）。
    """
    group = get_group(group_id)
    negative = compose_frame_negative(group)
    frames = _load_frames(group_id)
    for idx, frame in enumerate(frames):
        db.execute(
            "UPDATE shot_frames SET prompt_text=?, negative_text=?, seed=?, updated_at=? WHERE id=?",
            (compose_frame_prompt(group, frame.get("action_text")), negative,
             frame_seed(group, _int(frame.get("order_idx"), idx)), now(), frame["id"]),
        )
    safe_log(_LOGGER, 20, "shot_refresh_prompts", group_id=group_id, frames=len(frames))
    return get_group(group_id)


# ---------------------------------------------------------------- 封面


def _refresh_cover(group_id: int) -> None:
    """把封面重设为「最小 order_idx 且已出图」的那一帧（旁路，失败只记日志）。"""
    try:
        row = db.query_one(
            "SELECT image_path FROM shot_frames WHERE group_id=? AND status='done' AND image_path<>'' "
            "ORDER BY order_idx ASC, id ASC LIMIT 1",
            (group_id,),
        )
        cover = _text(row["image_path"]).strip() if row is not None else ""
        db.execute("UPDATE shot_groups SET cover_path=?, updated_at=? WHERE id=?", (cover, now(), group_id))
    except Exception as exc:
        safe_log(_LOGGER, 30, "shot_cover_failed", group_id=group_id, error_type=type(exc).__name__)


# ---------------------------------------------------------------- 出图


def _default_workflow() -> str:
    """挑一个可用工作流：组图未指定时沿用漫画项目最近用过的，再退回最新同步的工作流。"""
    row = db.query_one(
        "SELECT default_workflow FROM comic_projects WHERE default_workflow<>'' "
        "ORDER BY updated_at DESC, id DESC LIMIT 1"
    )
    if row is not None and _text(row["default_workflow"]).strip():
        return _text(row["default_workflow"]).strip()
    row = db.query_one("SELECT filename FROM workflows ORDER BY synced_at DESC, id DESC LIMIT 1")
    return _text(row["filename"]).strip() if row is not None else ""


def _resolve_workflow(group: dict, override: str = "") -> str:
    wf = _text(override).strip() or _text(group.get("workflow")).strip() or _default_workflow()
    if not wf:
        raise AIBARError("workflow_missing", "本机没有可用的出图工作流，请先同步 ComfyUI 工作流")
    return wf


def _save_frame_image(group_id: int, frame_id: int, data: bytes) -> str:
    """落盘一帧并清理该帧的历史产出；返回相对 DATA_DIR 的受控路径。

    **先写新图、再清旧图**（见 ``core.fileops`` 模块说明）：出图要排 ComfyUI 队列
    + 几十秒 GPU，是整条链路最贵的一步，绝不能让「删旧文件」这种免费动作把它拖下水。
    """
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / GROUPS_OUTPUT_SUBDIR / str(int(group_id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    dest = dest_dir / ("%d_%s.png" % (int(frame_id), digest))
    dest.write_bytes(data)
    # 按 frame_id 清理（不是 order_idx）：帧序调整后旧图依然能被精确命中
    purge_stale(dest_dir, "%d_*.png" % int(frame_id), keep=dest)
    return str(dest.relative_to(Config.DATA_DIR))


def _register_gallery(saved_rel: str, prompt_text: str, workflow: str) -> str | None:
    """把产出图登记进图库（旁路增强，失败只记日志）。"""
    try:
        from sync import scanner

        return scanner.register_image(
            Path(Config.DATA_DIR) / saved_rel, prompt=prompt_text, workflow_link=workflow
        )
    except Exception as exc:
        safe_log(_LOGGER, 30, "shot_gallery_register_failed", error_type=type(exc).__name__)
        return None


def _claim_frame(frame_id: int) -> bool:
    """原子占位：把帧置为 generating。已在生成中则返回 False（防重复提交）。"""
    cur = db.execute(
        "UPDATE shot_frames SET status='generating', error_message='', updated_at=? "
        "WHERE id=? AND status<>'generating'",
        (now(), frame_id),
    )
    try:
        return int(cur.rowcount or 0) > 0
    except Exception:
        return True


def _execute_frame(group: dict, frame: dict, workflow: str, cancel: threading.Event | None) -> bool:
    """同步出一帧并落库。返回是否成功。**绝不抛异常**。"""
    from . import runner

    frame_id = _int(frame.get("id"))
    group_id = _int(group.get("id"))
    prompt_text = _text(frame.get("prompt_text")).strip() or compose_frame_prompt(
        group, frame.get("action_text")
    )
    negative_text = _text(frame.get("negative_text")).strip() or compose_frame_negative(group)
    seed = _opt_int(frame.get("seed"))
    if seed is None:
        seed = frame_seed(group, _int(frame.get("order_idx")))

    if not prompt_text:
        db.execute(
            "UPDATE shot_frames SET status='failed', error_message=?, updated_at=? WHERE id=?",
            ("该帧没有可出图的提示词：请先在组图上填写预设提示词与动作描述", now(), frame_id),
        )
        return False

    try:
        data, code, message = runner.generate_once(
            workflow, prompt_text, negative_text, int(seed),
            timeout=FRAME_TIMEOUT, cancel=(cancel.is_set if cancel else None),
        )
    except BaseException as exc:  # 后台线程绝不静默死亡
        detail = _describe_exception(exc)
        db.execute(
            "UPDATE shot_frames SET status='failed', error_message=?, updated_at=? WHERE id=?",
            ("出图异常：%s" % detail, now(), frame_id),
        )
        safe_log(_LOGGER, 40, "shot_frame_crashed", group_id=group_id, frame_id=frame_id,
                 error_type=type(exc).__name__, detail=detail[:300])
        return False

    if not data:
        db.execute(
            "UPDATE shot_frames SET status='failed', error_message=?, updated_at=? WHERE id=?",
            (_human(code, message), now(), frame_id),
        )
        safe_log(_LOGGER, 30, "shot_frame_failed", group_id=group_id, frame_id=frame_id, error_code=code)
        return False

    try:
        saved = _save_frame_image(group_id, frame_id, data)
        image_id = _register_gallery(saved, prompt_text, workflow)
        db.execute(
            "UPDATE shot_frames SET status='done', error_message='', image_path=?, "
            "image_id=COALESCE(?, image_id), workflow=?, seed=?, updated_at=? WHERE id=?",
            (saved, image_id, workflow, int(seed), now(), frame_id),
        )
        _refresh_cover(group_id)
        safe_log(_LOGGER, 20, "shot_frame_done", group_id=group_id, frame_id=frame_id)
        return True
    except BaseException as exc:
        detail = _describe_exception(exc)
        db.execute(
            "UPDATE shot_frames SET status='failed', error_message=?, updated_at=? WHERE id=?",
            ("落盘失败：%s" % detail, now(), frame_id),
        )
        safe_log(_LOGGER, 40, "shot_frame_save_crashed", group_id=group_id, frame_id=frame_id,
                 error_type=type(exc).__name__, detail=detail[:300])
        return False


def _run_worker(group_id: int, frame_ids: list[int], workflow: str) -> None:
    """后台串行出一整组。**串行**是刻意的：ComfyUI 单卡排队，并发只会互相抢显存。"""
    cancel = _CANCELS.get(group_id)
    try:
        for fid in frame_ids:
            if cancel is not None and cancel.is_set():
                break
            frame = db.row_to_dict(db.query_one("SELECT * FROM shot_frames WHERE id=?", (fid,)))
            group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,)))
            if not frame or not group:
                continue
            if not _claim_frame(fid):
                continue
            _execute_frame(group, frame, workflow, cancel)
            _sync_counts(group_id)
    except BaseException as exc:
        detail = _describe_exception(exc)
        db.execute("UPDATE shot_groups SET last_error=?, updated_at=? WHERE id=?", (detail[:300], now(), group_id))
        safe_log(_LOGGER, 40, "shot_group_crashed", group_id=group_id,
                 error_type=type(exc).__name__, detail=detail[:300])
    finally:
        with _STATE_LOCK:
            _RUNNING.discard(group_id)
            _CANCELS.pop(group_id, None)
        try:
            _sync_counts(group_id)
            _refresh_cover(group_id)
        except Exception:
            pass


def generate_group(group_id: int, payload: dict | None = None) -> dict:
    """整组出图（后台串行，立即返回进度）。

    body 可选：
    - ``workflow``   覆盖出图工作流；
    - ``only_missing`` 默认 true：只出「没出过 / 失败」的帧；传 false 则全部重出；
    - ``frame_ids``  只出指定帧（优先级最高）。
    """
    body = payload or {}
    group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,)))
    if group is None:
        raise AIBARError("not_found", "组图不存在：%d" % group_id)

    with _STATE_LOCK:
        if group_id in _RUNNING:
            raise AIBARError("conflict", "该组图正在出图中，请等待完成或先取消")

    workflow = _resolve_workflow(group, _text(body.get("workflow")))

    wanted = body.get("frame_ids")
    if isinstance(wanted, list) and wanted:
        ids = [_int(x) for x in wanted if _opt_int(x) is not None]
    else:
        only_missing = _truthy(body.get("only_missing"), True)
        rows = db.query_all(
            "SELECT id FROM shot_frames WHERE group_id=? %s ORDER BY order_idx ASC, id ASC"
            % ("AND status<>'done'" if only_missing else ""),
            (group_id,),
        )
        ids = [_int(r["id"]) for r in rows]

    if not ids:
        return {"group_id": group_id, "queued": 0, "workflow": workflow, "progress": group_progress(group_id)}

    # 帧提示词为空（预设刚建、还没重算过）时先补一次，避免白跑一趟 ComfyUI
    # 参数顺序必须与 SQL 里的 ? 一一对应：先 group_id，再 IN 列表
    empty = _int(
        db.query_scalar(
            "SELECT COUNT(*) FROM shot_frames WHERE group_id=? AND id IN (%s) AND prompt_text='' "
            % ",".join("?" for _ in ids),
            tuple([group_id] + [int(i) for i in ids]),
            default=0,
        ),
        0,
    )
    if empty:
        refresh_prompts(group_id)

    cancel = threading.Event()
    with _STATE_LOCK:
        _CANCELS[group_id] = cancel
        _RUNNING.add(group_id)
    db.execute(
        "UPDATE shot_groups SET status='generating', last_error='', updated_at=? WHERE id=?",
        (now(), group_id),
    )

    threading.Thread(
        target=_run_worker, args=(group_id, ids, workflow),
        name="aibar-shotgen-%d" % group_id, daemon=True,
    ).start()

    safe_log(_LOGGER, 20, "shot_group_start", group_id=group_id, frames=len(ids), workflow=workflow)
    return {"group_id": group_id, "queued": len(ids), "workflow": workflow, "progress": group_progress(group_id)}


def cancel_group(group_id: int) -> dict:
    """请求取消整组出图：置位取消事件，worker 在**当前帧结束后**停下。

    为什么不硬杀线程：出图跑到一半被中断，ComfyUI 侧的任务还在队列里，
    数据库状态却停在 generating，反而更难收拾。让它跑完手上这一张再收尾。
    """
    with _STATE_LOCK:
        cancel = _CANCELS.get(group_id)
        running = group_id in _RUNNING
    if cancel is not None:
        cancel.set()
    return {"group_id": group_id, "cancelling": running}


def regenerate_frame(frame_id: int, payload: dict | None = None) -> dict:
    """单帧重生成（后台执行）。

    与整组出图互斥：该组正在跑整组出图时拒绝，避免两个线程抢同一张卡。
    """
    body = payload or {}
    row = db.query_one("SELECT * FROM shot_frames WHERE id=?", (frame_id,))
    if row is None:
        raise AIBARError("not_found", "帧不存在：%d" % frame_id)
    frame = db.row_to_dict(row)
    group_id = _int(frame.get("group_id"))

    with _STATE_LOCK:
        if group_id in _RUNNING:
            raise AIBARError("conflict", "该组图正在整组出图中，请先取消或等待完成")

    group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,))) or {}
    workflow = _resolve_workflow(group, _text(body.get("workflow")))

    if _truthy(body.get("randomize")):
        seed = default_base_seed()
        db.execute("UPDATE shot_frames SET seed=?, updated_at=? WHERE id=?", (seed, now(), frame_id))
        frame["seed"] = seed

    def _worker() -> None:
        with _STATE_LOCK:
            _RUNNING.add(group_id)
        try:
            fresh = db.row_to_dict(db.query_one("SELECT * FROM shot_frames WHERE id=?", (frame_id,)))
            if not fresh or not _claim_frame(frame_id):
                return
            _execute_frame(group, fresh, workflow, None)
            _sync_counts(group_id)
        except BaseException as exc:
            safe_log(_LOGGER, 40, "shot_frame_regen_crashed", frame_id=frame_id,
                     error_type=type(exc).__name__, detail=_describe_exception(exc)[:300])
        finally:
            with _STATE_LOCK:
                _RUNNING.discard(group_id)
            try:
                # 必须先离开 _RUNNING 再重算状态，否则会被运行标记压回 generating
                _sync_counts(group_id)
                _refresh_cover(group_id)
            except Exception:
                pass

    threading.Thread(target=_worker, name="aibar-shotframe-%d" % frame_id, daemon=True).start()
    return {"frame_id": frame_id, "group_id": group_id, "workflow": workflow}


def group_progress(group_id: int) -> dict:
    """出图进度快照（前端轮询）：``{status, total, done, failed, pending, generating, running}``。"""
    snap = _sync_counts(group_id)
    with _STATE_LOCK:
        running = group_id in _RUNNING
    return {
        "group_id": group_id,
        "status": snap.get("status", "idle"),
        "total": snap.get("total", 0),
        "done": snap.get("done", 0),
        "failed": snap.get("failed", 0),
        "generating": snap.get("generating", 0),
        "pending": max(0, snap.get("total", 0) - snap.get("done", 0) - snap.get("failed", 0) - snap.get("generating", 0)),
        "running": running,
    }


def playlist(group_id: int) -> dict:
    """连播清单：按帧序返回已出图的帧（供前端预加载与逐帧播放）。

    只返回 ``status='done'`` 的帧——连播是给人看的，缺帧时不该用占位图把节奏打断；
    前端拿到清单后全量预加载，播放时只切 ``src``，不会闪。
    """
    group = db.row_to_dict(db.query_one("SELECT * FROM shot_groups WHERE id=?", (group_id,)))
    if group is None:
        raise AIBARError("not_found", "组图不存在：%d" % group_id)

    frames = [
        f for f in _load_frames(group_id)
        if f.get("status") == "done" and f.get("image_path")
    ]
    # 图库副本优先（与演员卡/分镜卡一致的约定：图库副本是同源可缓存的静态文件）
    ids = [f.get("image_id") for f in frames if f.get("image_id")]
    gallery: dict[str, str] = {}
    if ids:
        rows = db.query_all(
            "SELECT id, gallery_path FROM images WHERE id IN (%s)" % ",".join("?" for _ in ids), tuple(ids)
        )
        gallery = {r["id"]: _text(r["gallery_path"]) for r in rows}

    items = []
    for f in frames:
        gp = gallery.get(f.get("image_id"))
        url = ("/static/%s" % gp) if gp else _output_url(f.get("image_path"))
        items.append({
            "id": f.get("id"),
            "order_idx": _int(f.get("order_idx")),
            "action": _text(f.get("action_text")),
            "url": url,
        })

    return {
        "group_id": group_id,
        "name": _text(group.get("name")),
        "interval": _clamp(group.get("frame_interval"), DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL),
        "loop": _truthy(group.get("loop_play"), True),
        "items": items,
    }


__all__ = [
    "MAX_GROUPS",
    "MAX_FRAMES",
    "DEFAULT_INTERVAL",
    "GROUP_STATUS",
    "FRAME_STATUS",
    "GROUPS_OUTPUT_SUBDIR",
    "resolve_anchor",
    "compose_frame_prompt",
    "compose_frame_negative",
    "frame_seed",
    "default_base_seed",
    "list_groups",
    "count_groups",
    "get_group",
    "create_group",
    "update_group",
    "delete_group",
    "set_frames",
    "parse_actions",
    "update_frame",
    "delete_frame",
    "refresh_prompts",
    "generate_group",
    "cancel_group",
    "regenerate_frame",
    "group_progress",
    "playlist",
]
