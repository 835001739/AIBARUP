"""M16 视频转绘：视频 → 逐帧提姿态（DWPose）→ ComfyUI 逐帧重绘 → 组图 + 无背景序列帧。

两段式流水线（方案 A）：
  prepare  : 从源视频抽帧 → 在「组图管理」建一个 shot_group 与 N 个 shot_frame
             → 在 video_paint_frames 登记 N 行（pending）
  pose     : 每帧上传 ComfyUI → DWPreprocessor/OpenposePreprocessor → 下载骨架图（动作拆解）
  generate : 每帧把骨架图 + 参考图喂给 build_pose_workflow → 出图 → 写回 shot_frame（组图管理即可播放）
  nobg     : 对生成图做去背景（连通抠图/颜色键控）→ 透明序列帧
  sheet    : 把生成图（或去背景图）拼成一张组图 contact sheet

不依赖 numpy/cv2：姿态提取走 ComfyUI 预处理器，去背景用纯 PIL + deque 区域生长。
"""

from __future__ import annotations

import io
import math
import shutil
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.fileops import purge_stale, rmtree_quiet, unlink_quiet
from core.logging_setup import get_logger, safe_log

import comic.runner as comic_runner
from comic import flux2 as comic_flux2
from comic import groups as comic_groups
from comic import pose as comic_pose
from comic import workflow_ui as comic_workflow_ui
from reverse import comfyui_client
import video.service as video_service
from sync import comfyui_link

logger = get_logger("aibar.videopaint")

SUBDIR = "videopaint"
FRAME_NAME_FMT = "frame_%04d.jpg"
POSE_NAME_FMT = "pose_%04d.png"
GEN_NAME_FMT = "frame_%04d.png"
NOBG_NAME_FMT = "frame_%04d.png"

_POSE_NODE = {"dwpose": "DWPreprocessor", "openpose": "OpenposePreprocessor"}

# 「骨架控姿」不能只靠一句自然语言承诺：输入控制图、文本构图和最终产物都要有约束。
# 这里的默认值偏向动作准确率；外观仍由用户提示词与 IPAdapter 决定。
STRICT_POSE_MIN_STRENGTH = 1.15
STRICT_POSE_RETRY_STRENGTH = 1.35
STRICT_POSE_MAX_ATTEMPTS = 2
STRICT_POSE_PASS_SCORE = 0.24
STRICT_POSE_POSITIVE = (
    "solo, one person, single character, full body, head-to-toe, entire body visible, "
    "dynamic action pose, exact pose match, clear limbs, simple single-scene composition"
)
STRICT_POSE_NEGATIVE = (
    "multiple people, extra person, duplicate character, cloned face, collage, split screen, "
    "comic panels, manga page, book cover, poster, typography, text, logo, speech bubble, "
    "close-up, bust shot, upper-body crop, cropped feet, out of frame"
)


# ----------------------------------------------------------------------------- 目录
def job_dir(job_id: int) -> Path:
    return Path(Config.DATA_DIR) / SUBDIR / str(int(job_id))


def job_frames_dir(job_id: int) -> Path:
    return job_dir(job_id) / "frames"


def job_poses_dir(job_id: int) -> Path:
    return job_dir(job_id) / "poses"


def job_gen_dir(job_id: int) -> Path:
    return job_dir(job_id) / "gen"


def job_nobg_dir(job_id: int) -> Path:
    return job_dir(job_id) / "nobg"


def job_sheet_path(job_id: int, variant: str = "original") -> Path:
    return job_dir(job_id) / ("sheet_%s.png" % variant)


# ----------------------------------------------------------------------------- 行映射
def _row_to_job(row: Any) -> dict[str, Any]:
    d = db.row_to_dict(row)
    # 参考图统一走本任务目录下的 reference.png（create_job 落库后复制进来）
    rel = d.get("reference_rel") or ""
    d["reference_url"] = ("/api/videopaint/jobs/%s/file/reference/0" % d.get("id")) if rel else ""
    return d


def _row_to_frame(row: Any) -> dict[str, Any]:
    d = db.row_to_dict(row)
    for k in ("pose_path", "image_path", "nobg_path"):
        rel = d.get(k) or ""
        d[k] = rel
        d[k + "_url"] = ("/api/videopaint/jobs/%s/file/%s/%s" % (d.get("job_id"), _file_kind_for(k), d.get("order_idx"))) if rel else ""
    # 抽帧原图（动作拆解前的源帧）也在本任务目录下，供前端与骨架/生成图并排对比
    d["frame_url"] = "/api/videopaint/jobs/%s/file/source/%s" % (d.get("job_id"), d.get("order_idx"))
    return d


def _file_kind_for(col: str) -> str:
    return {"pose_path": "pose", "image_path": "image", "nobg_path": "nobg"}.get(col, "")


# ----------------------------------------------------------------------------- 列表 / 读取
def list_jobs(limit: int = 50, offset: int = 0) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM video_paint_jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (int(limit), int(offset)),
    )
    return [db.row_to_dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM video_paint_jobs WHERE id=?", (int(job_id),))
    return _row_to_job(row) if row else None


def list_frames(job_id: int) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM video_paint_frames WHERE job_id=? ORDER BY order_idx ASC, id ASC",
        (int(job_id),),
    )
    return [_row_to_frame(r) for r in rows]


def get_frame(job_id: int, order_idx: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM video_paint_frames WHERE job_id=? AND order_idx=?",
        (int(job_id), int(order_idx)),
    )
    return _row_to_frame(row) if row else None


# ----------------------------------------------------------------------------- 创建 / 更新 / 删除
def _opt_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _opt_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def create_job(payload: dict) -> dict:
    """新建一个视频转绘任务。

    body:
      clip_id        源视频（video_clips.id，必须先用视频模块导入并抽帧）
      name           任务名
      prompt/negative 正向/负向提示词
      reference_path  参考图本地绝对路径（可选，创建时上传到 ComfyUI input）
      reference_name  已在 ComfyUI input 的参考图文件名（与 reference_path 二选一）
      其余为生成参数（checkpoint/controlnet/pose_mode/steps/cfg/宽高/种子/抽帧参数…）
    """
    body = payload or {}
    clip_id = _opt_int(body.get("clip_id"))
    clip = video_service.get_clip(clip_id)
    if not clip:
        raise AIBARError("not_found", "源视频不存在：%s" % clip_id)

    reference_rel = ""
    reference_name = _text(body.get("reference_name"))
    rp = _text(body.get("reference_path"))

    values = {
        "name": _text(body.get("name")).strip() or "未命名视频转绘",
        "clip_id": clip_id,
        "group_id": 0,
        "reference_rel": reference_rel,
        "reference_name": reference_name,
        "prompt": _text(body.get("prompt")).strip(),
        "negative": _text(body.get("negative")).strip(),
        "checkpoint": _text(body.get("checkpoint")).strip(),
        "controlnet": _text(body.get("controlnet")).strip(),
        "pose_mode": _text(body.get("pose_mode")).strip() or "dwpose",
        "pose_resolution": _opt_int(body.get("pose_resolution"), 512),
        "steps": _opt_int(body.get("steps"), comic_pose.DEFAULT_STEPS),
        "cfg": _opt_float(body.get("cfg"), comic_pose.DEFAULT_CFG),
        "controlnet_strength": _opt_float(body.get("controlnet_strength"), STRICT_POSE_MIN_STRENGTH),
        "ipadapter_weight": _opt_float(body.get("ipadapter_weight"), comic_pose.DEFAULT_IPADAPTER_WEIGHT),
        "faceidv2_weight": _opt_float(body.get("faceidv2_weight"), comic_pose.DEFAULT_FACEIDV2_WEIGHT),
        "width": _opt_int(body.get("width"), comic_pose.DEFAULT_WIDTH),
        "height": _opt_int(body.get("height"), comic_pose.DEFAULT_HEIGHT),
        "base_seed": _opt_int(body.get("base_seed"), 0),
        "seed_step": _opt_int(body.get("seed_step"), 0),
        "fps": _opt_int(body.get("fps"), 8),
        "max_frames": _opt_int(body.get("max_frames"), 4),
        "scale_width": _opt_int(body.get("scale_width"), 480),
        "start_sec": _opt_float(body.get("start_sec")) if body.get("start_sec") not in (None, "") else None,
        "end_sec": _opt_float(body.get("end_sec")) if body.get("end_sec") not in (None, "") else None,
        "frame_count": 0,
        "pose_count": 0,
        "done_count": 0,
        "status": "idle",
        "last_error": "",
        "created_at": now(),
        "updated_at": now(),
    }
    cols = ", ".join(values.keys())
    marks = ", ".join("?" for _ in values)
    cur = db.execute("INSERT INTO video_paint_jobs (%s) VALUES (%s)" % (cols, marks), tuple(values.values()))
    job_id = cur.lastrowid
    # 参考图在拿到 job_id 后再落库到本任务专属目录，避免多任务共用同一 clip 时互相覆盖
    if rp:
        p = Path(rp).expanduser()
        if not p.is_file():
            raise AIBARError("invalid_input", "参考图文件不存在：%s" % rp)
        d = job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        dst = d / "reference.png"
        shutil.copy(p, dst)
        reference_rel = str(dst.relative_to(Config.DATA_DIR))
        db.execute(
            "UPDATE video_paint_jobs SET reference_rel=?, reference_name=? WHERE id=?",
            (reference_rel, reference_name, int(job_id)),
        )
    return get_job(job_id)


def update_job(job_id: int, payload: dict) -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    if job["status"] in ("prepared", "posing", "generating"):
        raise AIBARError("conflict", "任务正在执行中，无法修改配置（请先等待或取消）")
    body = payload or {}
    fields = {
        "name": _text(body.get("name")),
        "prompt": _text(body.get("prompt")),
        "negative": _text(body.get("negative")),
        "checkpoint": _text(body.get("checkpoint")),
        "controlnet": _text(body.get("controlnet")),
        "pose_mode": _text(body.get("pose_mode")),
        "pose_resolution": body.get("pose_resolution"),
        "steps": body.get("steps"),
        "cfg": body.get("cfg"),
        "controlnet_strength": body.get("controlnet_strength"),
        "ipadapter_weight": body.get("ipadapter_weight"),
        "faceidv2_weight": body.get("faceidv2_weight"),
        "width": body.get("width"),
        "height": body.get("height"),
        "base_seed": body.get("base_seed"),
        "seed_step": body.get("seed_step"),
        "fps": body.get("fps"),
        "max_frames": body.get("max_frames"),
        "scale_width": body.get("scale_width"),
        "start_sec": body.get("start_sec"),
        "end_sec": body.get("end_sec"),
    }
    sets, params = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k in ("name", "prompt", "negative", "checkpoint", "controlnet", "pose_mode"):
            sets.append("%s=?" % k)
            params.append(_text(v))
        else:
            sets.append("%s=?" % k)
            if k in ("start_sec", "end_sec"):
                params.append(_opt_float(v) if v not in (None, "") else None)
            elif k in ("pose_resolution", "steps", "width", "height", "base_seed", "seed_step", "fps", "max_frames", "scale_width"):
                params.append(_opt_int(v))
            else:
                params.append(_opt_float(v))
    if sets:
        sets.append("updated_at=?")
        params.append(now())
        db.execute("UPDATE video_paint_jobs SET %s WHERE id=?" % ", ".join(sets), tuple(params) + (int(job_id),))
    return get_job(job_id)


def delete_job(job_id: int) -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    if job["status"] in ("prepared", "posing", "generating"):
        raise AIBARError("conflict", "任务正在执行中，无法删除（请先取消）")
    group_id = job.get("group_id") or 0
    if group_id:
        try:
            comic_groups.delete_group(group_id)
        except AIBARError:
            pass
    db.execute("DELETE FROM video_paint_frames WHERE job_id=?", (int(job_id),))
    db.execute("DELETE FROM video_paint_jobs WHERE id=?", (int(job_id),))
    rmtree_quiet(job_dir(job_id))
    return {"ok": True, "job_id": int(job_id), "group_id": group_id}


# ----------------------------------------------------------------------------- prepare
def prepare(job_id: int, fps: int | None = None, max_frames: int | None = None,
            scale_width: int | None = None, start_sec: float | None = None,
            end_sec: float | None = None) -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    clip = video_service.get_clip(job["clip_id"])
    if not clip:
        raise AIBARError("not_found", "源视频不存在：%s" % job["clip_id"])

    fps = fps or job["fps"] or 8
    max_frames = max_frames or job["max_frames"] or 4
    scale_width = scale_width or job["scale_width"] or 480
    start_sec = job["start_sec"] if start_sec is None else start_sec
    end_sec = job["end_sec"] if end_sec is None else end_sec

    # 抽帧到 clip 的暂存目录（M15 的能力），再复制到本任务自有目录，避免多任务互相覆盖
    video_service.extract_frames(
        job["clip_id"], fps=fps, max_frames=max_frames,
        scale_width=scale_width, start_sec=start_sec, end_sec=end_sec,
    )
    src_files = sorted(video_service.list_frames(job["clip_id"], "original"))
    if not src_files:
        raise AIBARError("invalid_input", "未抽到任何帧，请检查视频或抽帧参数")

    # 在组图管理建一份组图（直接写 shot_frames，便于精确绑定 video_paint_frames）
    group = comic_groups.create_group({
        "name": job["name"] or "视频转绘组图",
        "base_seed": job["base_seed"],
        "seed_step": job["seed_step"],
        "frame_interval": 220,
        "loop_play": 1,
    })
    group_id = group["id"]

    jfdir = job_frames_dir(job_id)
    jfdir.mkdir(parents=True, exist_ok=True)

    db.execute("DELETE FROM video_paint_frames WHERE job_id=?", (int(job_id),))
    for idx, fname in enumerate(src_files):
        src = video_service.frames_dir(job["clip_id"]) / fname
        dst = jfdir / (FRAME_NAME_FMT % (idx + 1))
        shutil.copy(src, dst)
        cur = db.execute(
            "INSERT INTO video_paint_frames (job_id, order_idx, frame_name, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (int(job_id), idx, dst.name, "pending", now(), now()),
        )
        pfid = cur.lastrowid
        cur2 = db.execute(
            "INSERT INTO shot_frames (group_id, order_idx, action_text, prompt_text, negative_text, seed, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group_id, idx, "frame %d" % (idx + 1), "", "", job["base_seed"] + idx * job["seed_step"], "pending", now(), now()),
        )
        sfid = cur2.lastrowid
        db.execute("UPDATE video_paint_frames SET shot_frame_id=? WHERE id=?", (sfid, pfid))

    db.execute(
        "UPDATE video_paint_jobs SET group_id=?, frame_count=?, status='prepared', updated_at=? WHERE id=?",
        (group_id, len(src_files), now(), int(job_id)),
    )
    return get_job(job_id)


# ----------------------------------------------------------------------------- pose（提骨架）
def _build_dwpose_graph(
    image_name: str,
    node_type: str,
    resolution: int,
    *,
    xinsir_controlnet: bool = False,
) -> dict:
    inputs: dict[str, Any] = {
        "image": ["1", 0],
        "detect_hand": "enable",
        "detect_body": "enable",
        "detect_face": "enable",
        "resolution": int(resolution),
        # 当前默认控制模型是 Xinsir Union。它在较大画布上需要更粗的 stick，
        # 否则骨架缩放后只剩很细的彩线，ControlNet 很容易把它当成弱噪声。
        "scale_stick_for_xinsr_cn": "enable" if xinsir_controlnet else "disable",
    }
    if node_type == "DWPreprocessor":
        inputs["bbox_detector"] = "yolox_l.onnx"
        inputs["pose_estimator"] = "dw-ll_ucoco_384.onnx"
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": node_type, "inputs": inputs},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "AIBAR_vp_pose"}},
    }


def _merge_prompt(prefix: str, value: Any) -> str:
    """把结构约束放在用户词前面，同时避免空串和重复分隔符。"""
    user = _text(value).strip().strip(",")
    return "%s, %s" % (prefix, user) if user else prefix


def _strict_pose_prompts(job: dict) -> tuple[str, str]:
    return (
        _merge_prompt(STRICT_POSE_POSITIVE, job.get("prompt")),
        _merge_prompt(STRICT_POSE_NEGATIVE, job.get("negative")),
    )


def _effective_pose_strength(value: Any, attempt: int = 0) -> float:
    """严格骨架模式不允许控制链被配置成几乎无效；重试时再加强一次。"""
    configured = _opt_float(value, STRICT_POSE_MIN_STRENGTH)
    floor = STRICT_POSE_RETRY_STRENGTH if attempt > 0 else STRICT_POSE_MIN_STRENGTH
    return max(floor, configured)


def _assert_pose_control_graph(graph: dict[str, Any], pose_image: str) -> None:
    """生成前断言骨架确实贯穿 LoadImage → ControlNet → KSampler。

    这是工作流接线的硬校验：以后即使底层构图函数被改坏，也不会悄悄退化成
    仅凭提示词出图。
    """
    pose = graph.get("6") or {}
    apply = graph.get("11") or {}
    sampler = graph.get("13") or {}
    if (
        pose.get("class_type") != "LoadImage"
        or (pose.get("inputs") or {}).get("image") != pose_image
        or apply.get("class_type") != "ControlNetApplyAdvanced"
        or (apply.get("inputs") or {}).get("image") != ["6", 0]
        or (apply.get("inputs") or {}).get("control_net") != ["8", 0]
        or float((apply.get("inputs") or {}).get("strength") or 0.0) <= 0.0
        or (sampler.get("inputs") or {}).get("positive") != ["11", 0]
        or (sampler.get("inputs") or {}).get("negative") != ["11", 1]
    ):
        raise AIBARError("invalid_workflow", "骨架控制链未正确接入采样器，已阻止无控姿生成")


def _normalize_pose_bytes(data: bytes, width: int, height: int) -> bytes:
    """把 DWPose 骨架无裁切地归一化到最终生成画布。

    ComfyUI 的 ControlNet 会用 ``center`` 方式把 hint 调到 latent 尺寸。横视频骨架
    直接喂给竖图时会先裁掉左右两侧（踢腿、伸手最容易消失）。这里先按非黑像素
    找到完整骨架，再等比缩放、居中放到目标黑底画布；此后 ControlNet 不再二次裁切。
    """
    target_w = max(64, int(width))
    target_h = max(64, int(height))
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    r, g, b = image.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda p: 255 if p > 8 else 0)
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("骨架图为空，未检测到有效关键点")

    left, top, right, bottom = bbox
    pose_w, pose_h = right - left, bottom - top
    pad = max(4, int(max(pose_w, pose_h) * 0.06))
    crop_box = (
        max(0, left - pad),
        max(0, top - pad),
        min(image.width, right + pad),
        min(image.height, bottom + pad),
    )
    cropped = image.crop(crop_box)
    usable_w = max(1, int(target_w * 0.90))
    usable_h = max(1, int(target_h * 0.90))
    scale = min(usable_w / cropped.width, usable_h / cropped.height)
    resized_w = max(1, int(round(cropped.width * scale)))
    resized_h = max(1, int(round(cropped.height * scale)))
    resampling = getattr(Image, "Resampling", Image).BICUBIC
    resized = cropped.resize((resized_w, resized_h), resampling)
    canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
    canvas.paste(resized, ((target_w - resized_w) // 2, (target_h - resized_h) // 2))
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _normalize_pose_file(path: Path, width: int, height: int) -> None:
    """原地升级旧任务的横版骨架；新任务在 pose 阶段已经是目标尺寸。"""
    with Image.open(path) as image:
        if image.size == (int(width), int(height)):
            return
    path.write_bytes(_normalize_pose_bytes(path.read_bytes(), width, height))


def _pose_mask(data: bytes, size: tuple[int, int] = (160, 208)) -> Image.Image:
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB").resize(size)
    r, g, b = image.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda p: 255 if p > 12 else 0)


def _pose_similarity(expected: bytes, actual: bytes) -> float:
    """比较两张 OpenPose 图的空间与肢体颜色重合度；额外人物会拉低 precision。"""
    target = _pose_mask(expected)
    candidate = _pose_mask(actual)

    def overlap(a: Image.Image, b: Image.Image) -> float:
        a_count = sum(a.histogram()[1:])
        b_count = sum(b.histogram()[1:])
        if not a_count or not b_count:
            return 0.0
        # 允许关键点检测和角色体型造成约 4% 画幅的偏移。
        a_near = a.filter(ImageFilter.MaxFilter(13))
        b_near = b.filter(ImageFilter.MaxFilter(13))
        recall = sum(ImageChops.multiply(a, b_near).histogram()[1:]) / a_count
        precision = sum(ImageChops.multiply(b, a_near).histogram()[1:]) / b_count
        return float(2.0 * precision * recall / (precision + recall)) if precision + recall else 0.0

    spatial = overlap(target, candidate)
    # OpenPose 用固定颜色标识不同肢体。只看亮度会把“左腿位置上出现右臂”也当匹配，
    # 因此再核对 RGB 三个通道的空间关系。
    with Image.open(io.BytesIO(expected)) as opened:
        expected_rgb = opened.convert("RGB").resize(target.size)
    with Image.open(io.BytesIO(actual)) as opened:
        actual_rgb = opened.convert("RGB").resize(target.size)
    channel_scores = []
    for expected_channel, actual_channel in zip(expected_rgb.split(), actual_rgb.split()):
        ea = expected_channel.point(lambda p: 255 if p > 48 else 0)
        aa = actual_channel.point(lambda p: 255 if p > 48 else 0)
        if sum(ea.histogram()[1:]) and sum(aa.histogram()[1:]):
            channel_scores.append(overlap(ea, aa))
    color = sum(channel_scores) / len(channel_scores) if channel_scores else 0.0
    return spatial * (0.5 + 0.5 * color)


def pose(job_id: int, cancel=None) -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    if not job["group_id"]:
        raise AIBARError("invalid_input", "请先 prepare")
    ok, _ = comfyui_client.is_reachable()
    if not ok:
        raise AIBARError("unavailable", "ComfyUI 不可达，请先启动")

    node_type = _POSE_NODE.get(job["pose_mode"], "DWPreprocessor")
    pdir = job_poses_dir(job_id)
    pdir.mkdir(parents=True, exist_ok=True)
    frames = list_frames(job_id)
    done = 0
    for fr in frames:
        if cancel and cancel():
            break
        if fr["status"] in ("done", "failed", "generating"):
            if fr["status"] == "done":
                done += 1
            continue
        jf = job_frames_dir(job_id) / fr["frame_name"]
        if not jf.exists():
            db.execute("UPDATE video_paint_frames SET status='failed', error_message='源帧缺失' WHERE id=?", (fr["id"],))
            continue
        up_name = "aibar_vp_%d_%d.png" % (int(job_id), fr["order_idx"])
        comfyui_client.upload_image(str(jf), up_name, overwrite=True)
        graph = _build_dwpose_graph(
            up_name,
            node_type,
            job["pose_resolution"],
            xinsir_controlnet="xinsir" in (job.get("controlnet") or comic_pose.DEFAULT_CONTROLNET).lower(),
        )
        data, code, msg = comic_runner.run_graph_once(graph, timeout=120.0, cancel=cancel)
        if data is None:
            db.execute("UPDATE video_paint_frames SET status='failed', error_message=? WHERE id=?", (msg or code, fr["id"]))
            continue
        pose_path = pdir / (POSE_NAME_FMT % fr["order_idx"])
        try:
            # 保存的就是下游实际消费的控制图，UI 里看到的骨架与 ControlNet 输入完全一致。
            data = _normalize_pose_bytes(data, job["width"], job["height"])
        except (OSError, ValueError) as exc:
            db.execute(
                "UPDATE video_paint_frames SET status='failed', error_message=? WHERE id=?",
                ("骨架归一化失败：%s" % str(exc), fr["id"]),
            )
            continue
        pose_path.write_bytes(data)
        db.execute(
            "UPDATE video_paint_frames SET pose_path=?, status='posed', updated_at=? WHERE id=?",
            (str(pose_path.relative_to(Config.DATA_DIR)), now(), fr["id"]),
        )
        done += 1
    db.execute(
        "UPDATE video_paint_jobs SET pose_count=?, status='posed', updated_at=? WHERE id=?",
        (done, now(), int(job_id)),
    )
    return get_job(job_id)


# ----------------------------------------------------------------------------- generate（逐帧重绘）
def _verify_generated_pose(
    job: dict,
    frame: dict,
    candidate_path: Path,
    expected_pose: bytes,
    cancel=None,
) -> tuple[float | None, str]:
    """对生成图再次跑 DWPose，返回与目标骨架的匹配分和错误说明。"""
    try:
        name = "aibar_vp_verify_%d_%d.png" % (int(job["id"]), int(frame["order_idx"]))
        comfyui_client.upload_image(str(candidate_path), name, overwrite=True)
        node_type = _POSE_NODE.get(job["pose_mode"], "DWPreprocessor")
        graph = _build_dwpose_graph(
            name,
            node_type,
            job["pose_resolution"],
            xinsir_controlnet="xinsir" in (job.get("controlnet") or comic_pose.DEFAULT_CONTROLNET).lower(),
        )
        detected, code, message = comic_runner.run_graph_once(graph, timeout=120.0, cancel=cancel)
    except Exception as exc:
        # 复检短暂异常时不能把已经完成的 ControlNet 生成永久卡在 generating；
        # 返回“不可测量”，由严格接线和强控制结果兜底。
        safe_log(logger, 30, "videopaint_pose_verify_unavailable", error_type=type(exc).__name__)
        return None, "姿态复检暂不可用：%s" % type(exc).__name__
    if detected is None:
        return None, message or code or "生成图未检测到人物骨架"
    try:
        normalized = _normalize_pose_bytes(detected, job["width"], job["height"])
    except (OSError, ValueError) as exc:
        # YOLOX 对部分二次元画风检测不到人物框。此时不能把“无法测量”伪装成 0 分，
        # 否则连肉眼明显匹配的动漫动作也会被全部拒绝。
        return None, str(exc)
    return _pose_similarity(expected_pose, normalized), ""


def generate(job_id: int, cancel=None, force: bool = False) -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    if not job["group_id"]:
        raise AIBARError("invalid_input", "请先 prepare")
    ok, _ = comfyui_client.is_reachable()
    if not ok:
        raise AIBARError("unavailable", "ComfyUI 不可达，请先启动")

    # 参考图：优先用已登记的 ComfyUI 文件名，否则从本地 reference.png 上传一次
    ref_name = job.get("reference_name") or ""
    if not ref_name and job.get("reference_rel"):
        ref_path = Path(Config.DATA_DIR) / job["reference_rel"]
        if ref_path.exists():
            ref_name = "aibar_vp_ref_%d.png" % int(job_id)
            comfyui_client.upload_image(str(ref_path), ref_name, overwrite=True)
            db.execute("UPDATE video_paint_jobs SET reference_name=? WHERE id=?", (ref_name, int(job_id)))

    gdir = job_gen_dir(job_id)
    gdir.mkdir(parents=True, exist_ok=True)
    frames = list_frames(job_id)
    total = len(frames)
    done = 0
    failed = 0
    positive, negative = _strict_pose_prompts(job)
    for fr in frames:
        if cancel and cancel():
            break
        if fr["status"] == "done" and not force:
            done += 1
            continue
        pose_rel = fr.get("pose_path") or ""
        if not pose_rel:
            db.execute("UPDATE video_paint_frames SET status='failed', error_message='缺少骨架图，请先 pose' WHERE id=?", (fr["id"],))
            failed += 1
            continue
        pose_path = Path(Config.DATA_DIR) / pose_rel
        if not pose_path.exists():
            db.execute("UPDATE video_paint_frames SET status='failed', error_message='骨架图文件缺失' WHERE id=?", (fr["id"],))
            failed += 1
            continue
        try:
            # 兼容修复前已经提取的 910×512 等横版骨架；归一化后同时刷新页面预览。
            _normalize_pose_file(pose_path, job["width"], job["height"])
            expected_pose = pose_path.read_bytes()
        except (OSError, ValueError) as exc:
            db.execute(
                "UPDATE video_paint_frames SET status='failed', error_message=? WHERE id=?",
                ("骨架图无效：%s" % str(exc), fr["id"]),
            )
            failed += 1
            continue
        pose_name = "aibar_vp_pose_%d_%d.png" % (int(job_id), fr["order_idx"])
        comfyui_client.upload_image(str(pose_path), pose_name, overwrite=True)
        gen_path = gdir / (GEN_NAME_FMT % fr["order_idx"])
        accepted: bytes | None = None
        best_score: float | None = None
        last_message = ""
        attempts = 0
        seed = job["base_seed"] + fr["order_idx"] * job["seed_step"]
        db.execute(
            "UPDATE video_paint_frames SET image_path='', status='generating', error_message='', "
            "pose_score=NULL, generation_attempts=0, seed=?, updated_at=? WHERE id=?",
            (seed, now(), fr["id"]),
        )
        for attempt in range(STRICT_POSE_MAX_ATTEMPTS):
            if cancel and cancel():
                break
            attempts = attempt + 1
            attempt_seed = seed + attempt * 7919
            graph = comic_pose.build_pose_workflow(
                reference_image=ref_name,
                pose_image=pose_name,
                positive=positive,
                negative=negative,
                checkpoint=job["checkpoint"] or comic_pose.DEFAULT_CHECKPOINT,
                controlnet=job["controlnet"] or comic_pose.DEFAULT_CONTROLNET,
                seed=attempt_seed,
                steps=job["steps"],
                cfg=job["cfg"],
                width=job["width"],
                height=job["height"],
                controlnet_strength=_effective_pose_strength(job["controlnet_strength"], attempt),
                ipadapter_weight=job["ipadapter_weight"],
                faceidv2_weight=job["faceidv2_weight"],
            )
            _assert_pose_control_graph(graph, pose_name)
            data, code, msg = comic_runner.run_graph_once(graph, timeout=300.0, cancel=cancel)
            if data is None:
                last_message = msg or code
                continue
            gen_path.write_bytes(data)
            score, verify_message = _verify_generated_pose(job, fr, gen_path, expected_pose, cancel=cancel)
            if score is None:
                # 检测器无法量化二次元人物时，以已经强化并贯穿全程的 ControlNet 结果为准。
                accepted = data
                best_score = None
                seed = attempt_seed
                break
            if best_score is None or score > best_score:
                best_score = score
                accepted = data
            last_message = verify_message
            if score >= STRICT_POSE_PASS_SCORE:
                accepted = data
                seed = attempt_seed
                break

        if accepted is None or (best_score is not None and best_score < STRICT_POSE_PASS_SCORE):
            unlink_quiet(gen_path)
            reason = last_message or "动作匹配度 %.0f%%，低于验收线 %.0f%%" % (
                (best_score or 0.0) * 100,
                STRICT_POSE_PASS_SCORE * 100,
            )
            db.execute(
                "UPDATE video_paint_frames SET image_path='', status='failed', error_message=?, "
                "pose_score=?, generation_attempts=?, updated_at=? WHERE id=?",
                ("骨架一致性校验未通过：%s" % reason, best_score, attempts, now(), fr["id"]),
            )
            db.execute(
                "UPDATE shot_frames SET image_path='', status='failed', updated_at=? WHERE id=?",
                (now(), fr["shot_frame_id"]),
            )
            failed += 1
            continue
        gen_path.write_bytes(accepted)
        # 写回组图管理：让生成的图直接进入 shot_frame，组图管理即可显示并播放
        rel = comic_groups._save_frame_image(job["group_id"], fr["shot_frame_id"], accepted)
        db.execute(
            "UPDATE shot_frames SET image_path=?, status='done', prompt_text=?, updated_at=? WHERE id=?",
            (rel, positive, now(), fr["shot_frame_id"]),
        )
        db.execute(
            "UPDATE video_paint_frames SET image_path=?, status='done', error_message='', seed=?, "
            "pose_score=?, generation_attempts=?, updated_at=? WHERE id=?",
            (str(gen_path.relative_to(Config.DATA_DIR)), seed, best_score, attempts, now(), fr["id"]),
        )
        done += 1

    if failed and not done:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "done"
    last_error = ("%d 帧未通过骨架一致性校验" % failed) if failed else ""
    db.execute(
        "UPDATE video_paint_jobs SET done_count=?, status=?, last_error=?, updated_at=? WHERE id=?",
        (done, status, last_error, now(), int(job_id)),
    )
    comic_groups._sync_counts(job["group_id"])
    return get_job(job_id)


# ----------------------------------------------------------------------------- 工作流按钮（打开 ComfyUI 编辑器）
def _prepare_frame_workflow_inputs(job_id: int, order_idx: int) -> tuple[dict, str, str, int]:
    """工作流按钮公共前置：校验任务/帧 → 上传参考图与骨架图到 ComfyUI input。

    SDXL 姿态工作流（M14）与 FLUX.2 Klein 结构控制工作流（M17）共用这一段：
    两者吃的都是「参考图（外观） + 骨架图（结构）」，只是下游构图函数不同。

    Returns:
        ``(job, ref_name, pose_name, seed)``。参考图没登记时 ``ref_name`` 为空串
        （工作流里那条参考链会被自动跳过，退化成纯结构控制）。

    Raises:
        AIBARError: 任务/帧不存在、未 prepare、骨架图缺失、ComfyUI 不可达。
    """
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    if not job.get("group_id"):
        raise AIBARError("invalid_input", "请先 prepare")
    frame = get_frame(job_id, order_idx)
    if not frame:
        raise AIBARError("not_found", "帧不存在：%s/%s" % (job_id, order_idx))

    pose_rel = frame.get("pose_path") or ""
    if not pose_rel:
        raise AIBARError("invalid_input", "该帧还没有骨架图，请先执行 pose / generate")
    pose_path = Path(Config.DATA_DIR) / pose_rel
    if not pose_path.exists():
        raise AIBARError("invalid_input", "骨架图文件缺失，请先 pose")
    try:
        _normalize_pose_file(pose_path, job["width"], job["height"])
    except (OSError, ValueError) as exc:
        raise AIBARError("invalid_input", "骨架图无效：%s" % str(exc))

    reachable, _ = comfyui_client.is_reachable()
    if not reachable:
        raise AIBARError("unavailable", "ComfyUI 不可达，请先启动")

    # 参考图：与 generate 同一套「已登记文件名优先，否则上传一次」逻辑
    ref_name = job.get("reference_name") or ""
    if not ref_name and job.get("reference_rel"):
        ref_path = Path(Config.DATA_DIR) / job["reference_rel"]
        if ref_path.exists():
            ref_name = "aibar_vp_ref_%d.png" % int(job_id)
            comfyui_client.upload_image(str(ref_path), ref_name, overwrite=True)
            db.execute(
                "UPDATE video_paint_jobs SET reference_name=? WHERE id=?",
                (ref_name, int(job_id)),
            )

    # 骨架图：每帧一个稳定文件名，上传（覆盖）到 ComfyUI input
    pose_name = "aibar_vp_pose_%d_%d.png" % (int(job_id), int(order_idx))
    comfyui_client.upload_image(str(pose_path), pose_name, overwrite=True)

    seed = job["base_seed"] + int(order_idx) * job["seed_step"]
    return job, ref_name, pose_name, seed


def build_frame_pose_ui_graph(job_id: int, order_idx: int) -> dict[str, Any]:
    """为某帧构造「姿态重绘」的 **UI 格式** 工作流，供 ComfyUI 编辑器直接载入。

    流程与 :func:`generate` 一致：先把参考图（如需）与骨架图上传到 ComfyUI input，
    再用 :func:`comic.pose.build_pose_workflow` 生成 API 图并转成 UI 图。这样点开
    按钮后在 ComfyUI 画布里就能看到完整链路，可调整参数 / 重新指定帧 / 多次出图。

    Returns:
        ``{"ui": <UI 工作流 dict>, "ref_name", "pose_name", "seed"}``。
        骨架图缺失时抛 ``invalid_input``（请先 pose / generate）。
    """
    job, ref_name, pose_name, seed = _prepare_frame_workflow_inputs(job_id, order_idx)
    api = comic_pose.build_pose_workflow(
        reference_image=ref_name,
        pose_image=pose_name,
        positive=_strict_pose_prompts(job)[0],
        negative=_strict_pose_prompts(job)[1],
        checkpoint=job["checkpoint"] or comic_pose.DEFAULT_CHECKPOINT,
        controlnet=job["controlnet"] or comic_pose.DEFAULT_CONTROLNET,
        seed=seed,
        steps=job["steps"],
        cfg=job["cfg"],
        width=job["width"],
        height=job["height"],
        controlnet_strength=_effective_pose_strength(job["controlnet_strength"]),
        ipadapter_weight=job["ipadapter_weight"],
        faceidv2_weight=job["faceidv2_weight"],
    )
    _assert_pose_control_graph(api, pose_name)
    ui = comic_workflow_ui.api_to_ui_graph(api)
    return {"ui": ui, "ref_name": ref_name, "pose_name": pose_name, "seed": seed}


def frame_editor_link(job_id: int, order_idx: int) -> dict[str, Any]:
    """构造打开 ComfyUI 并载入本帧姿态工作流的深链接。

    工作流 JSON 由运行期路由 ``/api/videopaint/jobs/<id>/frames/<order>/workflow-graph``
    即时生成（带 CORS、``no-store``），通过 ``aibar_wf`` 参数喂给桥梁扩展。
    """
    build_frame_pose_ui_graph(job_id, order_idx)  # 确保参考图/骨架已上传 ComfyUI input
    job = get_job(job_id) or {}
    graph_url = "%s/api/videopaint/jobs/%s/frames/%s/workflow-graph" % (
        comfyui_link.aibar_base(),
        int(job_id),
        int(order_idx),
    )
    name = "视频转绘#%s 帧%s" % (int(job_id), int(order_idx))
    return comfyui_link.build_editor_link_graph(
        graph_url=graph_url,
        name=name,
        prompt=job.get("prompt") or "",
        negative=job.get("negative") or "",
        target="9",  # 正向提示词节点（build_pose_workflow 中 CLIPTextEncode 的 id）
    )


# --------------------------------------------------------------- M17 · FLUX.2 Klein 结构控制工作流
def build_frame_flux2_ui_graph(job_id: int, order_idx: int) -> dict[str, Any]:
    """为某帧构造 **FLUX.2 Klein** 结构控制工作流（UI 格式），供 ComfyUI 编辑器载入。

    与 :func:`build_frame_pose_ui_graph` 的区别（两者在 UI 上并列两个按钮）：

    ==========  ==========================================  ======================================
    维度        姿态工作流（M14, SDXL）                      FLUX.2 工作流（M17, Klein）
    ==========  ==========================================  ======================================
    底模        animagine-xl 3.1 (SDXL)                    flux-2-klein-4b（蒸馏 4 步）
    文本编码    CLIP（T5XXL + CLIP-L）                      qwen_3_4b（CLIPLoader type=flux2）
    结构控制    ControlNet-Union（独立权重，可控强度）       骨架图走第二条 ReferenceLatent（架构级）
    外观锁脸    IPAdapter FaceID Plus v2（独立权重）         参考图走第一条 ReferenceLatent
    采样        steps 28 / cfg 6.5（SDXL 惯例）             steps 4 / cfg 1.0（蒸馏版惯例）
    额外权重    3 个（ckpt + controlnet + ipadapter）        0 个（单模型搞定）
    ==========  ==========================================  ======================================

    取样参数走 Klein 蒸馏版默认值（4 步 / cfg 1.0 / 1024×1024），**不继承任务的
    SDXL 参数**——把 cfg 6.5 / steps 28 灌进蒸馏 Klein 会直接出废图。

    Returns:
        ``{"ui": <UI 工作流 dict>, "ref_name", "pose_name", "seed"}``。
    """
    job, ref_name, pose_name, seed = _prepare_frame_workflow_inputs(job_id, order_idx)
    api = comic_flux2.build_flux2_workflow(
        reference_image=ref_name,   # 外观 / 角色参考 → 参考链（节点 8/9）
        control_image=pose_name,    # 骨架结构控制   → 控制链（节点 12/13）
        positive=job["prompt"],
        negative=job["negative"],
        diffusion=comic_flux2.DEFAULT_DIFFUSION,
        text_encoder=comic_flux2.DEFAULT_TEXT_ENCODER,
        vae=comic_flux2.DEFAULT_VAE,
        seed=seed,
        steps=comic_flux2.DEFAULT_STEPS_DISTILLED,
        cfg=comic_flux2.DEFAULT_CFG_DISTILLED,
        width=comic_flux2.DEFAULT_WIDTH,
        height=comic_flux2.DEFAULT_HEIGHT,
    )
    ui = comic_workflow_ui.api_to_ui_graph(api)
    return {"ui": ui, "ref_name": ref_name, "pose_name": pose_name, "seed": seed}


def frame_flux2_editor_link(job_id: int, order_idx: int) -> dict[str, Any]:
    """构造打开 ComfyUI 并载入本帧 **FLUX.2 Klein** 工作流的深链接。

    工作流 JSON 由运行期路由 ``/api/videopaint/jobs/<id>/frames/<order>/flux2-workflow-graph``
    即时生成（``no-store``），通过 ``aibar_wf`` 参数喂给 AIBAR-Bridge 扩展。
    """
    build_frame_flux2_ui_graph(job_id, order_idx)  # 确保参考图/骨架已上传 ComfyUI input
    job = get_job(job_id) or {}
    graph_url = "%s/api/videopaint/jobs/%s/frames/%s/flux2-workflow-graph" % (
        comfyui_link.aibar_base(),
        int(job_id),
        int(order_idx),
    )
    name = "FLUX.2转绘#%s 帧%s" % (int(job_id), int(order_idx))
    return comfyui_link.build_editor_link_graph(
        graph_url=graph_url,
        name=name,
        prompt=job.get("prompt") or "",
        negative=job.get("negative") or "",
        target="4",  # 正向提示词节点（build_flux2_workflow 中 CLIPTextEncode 的 id）
    )


# ----------------------------------------------------------------------------- nobg（去背景序列帧）
def _parse_seed(value: Any, w: int, h: int) -> tuple[int, int]:
    if not value:
        return 0, 0
    try:
        xs = str(value).split(",")
        x = max(0, min(w - 1, int(float(xs[0].strip()))))
        y = max(0, min(h - 1, int(float(xs[1].strip())))) if len(xs) > 1 else 0
        return x, y
    except (ValueError, IndexError):
        return 0, 0


def _flood_mask(px: list[tuple[int, int, int]], w: int, h: int, seed_x: int, seed_y: int, tol2: int) -> bytearray:
    """连通区域生长：像素被移除 ⟺ 与**种子色**相近 且 4-连通到种子点。"""
    base = seed_y * w + seed_x
    sr, sg, sb = px[base]
    visited = bytearray(w * h)
    dq: deque[int] = deque([base])
    visited[base] = 1
    while dq:
        idx = dq.popleft()
        cx, cy = idx % w, idx // w
        for ni in (idx - 1 if cx > 0 else -1, idx + 1 if cx < w - 1 else -1,
                   idx - w if cy > 0 else -1, idx + w if cy < h - 1 else -1):
            if ni < 0 or visited[ni]:
                continue
            r, g, b = px[ni]
            if (r - sr) ** 2 + (g - sg) ** 2 + (b - sb) ** 2 <= tol2:
                visited[ni] = 1
                dq.append(ni)
    return visited


def _remove_bg_bytes(data: bytes, mode: str, seed: str, similarity: float, blend: float, color: str = "") -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    raw = img.tobytes()
    px = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
    sx, sy = _parse_seed(seed, w, h)
    tol2 = int((max(0.0, min(1.0, similarity)) * 255) ** 2)
    removed = _flood_mask(px, w, h, sx, sy, tol2)
    out = Image.new("RGBA", (w, h))
    opx = out.load()
    for i, (r, g, b) in enumerate(px):
        opx[i % w, i // w] = (r, g, b, 0) if removed[i] else (r, g, b, 255)
    if blend > 0:
        mask = out.split()[3].filter(ImageFilter.GaussianBlur(float(blend)))
        out.putalpha(mask)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def build_nobg(job_id: int, mode: str = "flood", seed: str = "0,0",
               similarity: float = 0.4, blend: float = 0.0, color: str = "") -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    ndir = job_nobg_dir(job_id)
    ndir.mkdir(parents=True, exist_ok=True)
    frames = list_frames(job_id)
    count = 0
    for fr in frames:
        gen_rel = fr.get("image_path") or ""
        if not gen_rel:
            continue
        gen_path = Path(Config.DATA_DIR) / gen_rel
        if not gen_path.exists():
            continue
        out = _remove_bg_bytes(gen_path.read_bytes(), mode, seed, similarity, blend, color)
        nobg_path = ndir / (NOBG_NAME_FMT % fr["order_idx"])
        nobg_path.write_bytes(out)
        db.execute(
            "UPDATE video_paint_frames SET nobg_path=?, updated_at=? WHERE id=?",
            (str(nobg_path.relative_to(Config.DATA_DIR)), now(), fr["id"]),
        )
        count += 1
    db.execute(
        "UPDATE video_paint_jobs SET has_nobg=1, bg_mode=?, bg_seed=?, bg_similarity=?, bg_blend=?, bg_color=?, updated_at=? WHERE id=?",
        (mode, seed, similarity, blend, color, now(), int(job_id)),
    )
    return get_job(job_id)


# ----------------------------------------------------------------------------- sheet（组图 contact sheet）
def _montage(files: list[Path], cols: int, padding: int) -> Image.Image:
    imgs = [Image.open(f).convert("RGBA") for f in files]
    w = max(i.width for i in imgs)
    h = max(i.height for i in imgs)
    rows = math.ceil(len(imgs) / cols)
    canvas = Image.new("RGBA", (cols * w + padding * (cols + 1), rows * h + padding * (rows + 1)), (0, 0, 0, 0))
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        x = padding + c * (w + padding)
        y = padding + r * (h + padding)
        canvas.paste(im, (x, y))
    return canvas


def build_sheet(job_id: int, cols: int = 4, padding: int = 4, variant: str = "original") -> dict:
    job = get_job(job_id)
    if not job:
        raise AIBARError("not_found", "任务不存在：%s" % job_id)
    src_dir = job_gen_dir(job_id) if variant == "original" else job_nobg_dir(job_id)
    files = sorted(src_dir.glob(GEN_NAME_FMT.replace("%04d", "*")), key=lambda p: p.stem)
    if not files:
        raise AIBARError("not_found", "没有可拼图的帧（请先 generate / nobg）")
    canvas = _montage(files, max(1, cols), padding)
    sheet = job_sheet_path(job_id, variant)
    canvas.save(sheet, "PNG")
    return {"job_id": int(job_id), "variant": variant, "cols": cols, "frames": len(files),
            "path": str(sheet.relative_to(Config.DATA_DIR)), "url": "/api/videopaint/jobs/%s/sheet/%s" % (int(job_id), variant)}


# ----------------------------------------------------------------------------- 文件服务
def read_job_file(job_id: int, kind: str, order_idx: int | None = None) -> tuple[bytes, str] | None:
    job = get_job(job_id)
    if not job:
        return None
    if kind == "sheet":
        variant = order_idx == 1 and "nobg" or "original"
        p = job_sheet_path(job_id, variant)
        if not p.exists():
            return None
        return p.read_bytes(), "image/png"
    if kind == "reference":
        # 参考图锁脸用，固定存在本任务目录下的 reference.png
        p = job_dir(job_id) / "reference.png"
        if not p.exists():
            return None
        return p.read_bytes(), "image/png"
    if kind == "source":
        # 抽帧原图（动作拆解前），文件名按 order_idx 规律命名
        p = job_frames_dir(job_id) / (FRAME_NAME_FMT % (order_idx or 1))
        if not p.exists():
            return None
        return p.read_bytes(), "image/png"
    frame = get_frame(job_id, order_idx) if order_idx is not None else None
    if not frame:
        return None
    if kind == "pose":
        rel = frame.get("pose_path")
    elif kind == "image":
        rel = frame.get("image_path")
    elif kind == "nobg":
        rel = frame.get("nobg_path")
    else:
        return None
    if not rel:
        return None
    p = Path(Config.DATA_DIR) / rel
    if not p.exists():
        return None
    return p.read_bytes(), "image/png"


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()
