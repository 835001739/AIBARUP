"""AI 视频工作台 · 资产出图执行器。

角色/场景资产的图片生成走与漫画工作室相同的 ComfyUI 链路：
读取 UI 工作流 → 转 API 图 → 注入提示词与种子 → 提交 → 轮询 → 下载首张图。

本模块刻意只依赖 ``sync``、``reverse.comfyui_client`` 与 ``core``，不 import
``comic``，避免两个业务包互相耦合；工作流注入逻辑与漫画侧保持同一套节点
识别规则，保证角色设定图的提示词/种子行为一致。

失败不抛出到后台线程外：状态只写 ``failed`` + 可读原因，前端轮询后展示。
"""

from __future__ import annotations

import hashlib
import threading
from io import BytesIO
import uuid
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from reverse import comfyui_client as cc
from sync import paths as sync_paths
from sync import workflow_convert

from . import service

_LOGGER = get_logger("aibar.ai_video.generation")

TEXT_ENCODE_HINT = "CLIPTextEncode"
NEGATIVE_HINTS = (
    "ugly", "blurry", "low quality", "worst quality", "bad anatomy", "watermark",
    "text,", "signature", "jpeg artifacts", "低质量", "模糊", "畸形",
)
DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
POLL_INTERVAL = 2.0

# 多角度/表情组：同一角色、同一服装，只改视角或表情，便于挑设定图。
PROMPT_PRESETS = {
    "angles": [
        ("正面", "front view, looking at camera"),
        ("四分之三侧", "three-quarter view, looking slightly away"),
        ("侧面", "side profile view"),
        ("背面", "back view"),
    ],
    "expressions": [
        ("中性", "neutral expression"),
        ("微笑", "subtle warm smile"),
        ("愤怒", "angry expression, furrowed brows"),
        ("悲伤", "sad expression, downcast eyes"),
        ("惊讶", "surprised expression, wide eyes"),
    ],
}


_SHOT_TYPE_PROMPT = {
    "insert_prop": "insert shot of a prop, close-up, shallow depth of field, no people",
    "reaction_cu": "reaction close-up of the listener, natural expression",
    "ots": "over-the-shoulder shot, speaker seen from behind, listener in focus",
    "speak_cu": "frontal close-up of the speaker, speaking, lips visible",
    "establishing": "establishing wide shot, environment and spatial context",
    "silhouette": "side silhouette or back view of the character",
    "two_shot": "two-shot, two characters in frame, consistent axis",
    "tracking_back": "tracking shot following the character from behind",
    "mirror": "mirror reflection shot, careful left-right continuity",
    "empty": "empty environment insert, no people",
    "wide": "wide master shot, full scene",
    "other": "cinematic shot",
}
_FRAME_CLAIM_LOCK = threading.Lock()


def _style_suffix(project: dict[str, Any]) -> str:
    style = str(project.get("visual_style") or "realistic")
    return {
        "realistic": "photorealistic, cinematic realism, ARRI Alexa, visible skin texture, soft natural light",
        "anime": "anime style, clean line art, expressive eyes, high detail",
        "3d": "3D render, physically based shading, soft studio light, high detail",
        "illustration": "digital illustration, painterly, rich color, high detail",
    }.get(style, "cinematic realism, high detail")


def _character_context(shot: dict[str, Any], project: dict[str, Any]) -> str:
    face = str(shot.get("visible_face") or shot.get("speaker") or "").strip()
    if not face or face == "群":
        return ""
    rows = db.query_all(
        "SELECT name,asset_key,description,prompt FROM ai_video_assets "
        "WHERE project_id=? AND asset_type='character'",
        (int(project["id"]),),
    )
    for row in rows:
        key = str(row.get("asset_key") or "")
        aliases = {str(row.get("name") or ""), key, key.replace("char_", "").replace("角色_", "")}
        if face not in aliases:
            continue
        detail = str(row.get("description") or "").strip()
        if detail:
            return "character identity: " + detail
        return "character identity: " + str(row.get("name") or face)
    return ""


def _build_shot_prompt(shot: dict[str, Any], scene: dict[str, Any], project: dict[str, Any], kind: str) -> str:
    explicit = str(shot.get("prompt_image") or "").strip()
    parts: list[str] = []
    if explicit:
        parts.append(explicit)
    else:
        location = str(scene.get("location") or "").strip()
        time_of_day = str(scene.get("time_of_day") or "").strip()
        if location:
            parts.append(location)
        if time_of_day:
            parts.append(time_of_day + " lighting")
        parts.append(_SHOT_TYPE_PROMPT.get(str(shot.get("shot_type") or ""), "cinematic shot"))
    for key in ("lens", "camera"):
        value = str(shot.get(key) or "").strip()
        if value:
            parts.append(value)
    face = str(shot.get("visible_face") or shot.get("speaker") or "").strip()
    if face:
        parts.append("character " + face)
        context = _character_context(shot, project)
        if context:
            parts.append(context)
    elif str(shot.get("shot_type") or "") in ("insert_prop", "empty", "establishing"):
        parts.append("no people")
    parts.append("same character identity, consistent wardrobe and scene continuity")
    parts.append(
        "opening frame of the shot" if kind == "start"
        else "final frame of the shot, final pose, continuity with the opening frame"
    )
    parts.append(_style_suffix(project))
    return ", ".join(part for part in parts if part)


def _save_shot_frame(shot_id: int, kind: str, data: bytes) -> str:
    root = Path(Config.DATA_DIR) / "ai_video_frames" / str(int(shot_id))
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = "%s_%s.png" % (kind, digest)
    target = root / name
    try:
        with Image.open(BytesIO(data)) as image:
            image.convert("RGB").save(target, format="PNG")
    except Exception:
        raise AIBARError("bad_response", "产出图无法识别")
    try:
        for old in root.glob("%s_*.png" % kind):
            if old != target:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return name


def _mark_shot_frame_failed(shot_id: int, kind: str, message: str) -> None:
    try:
        service.set_shot_frame_status(
            shot_id, kind, "failed", error=message[:500], finished_at=now(),
        )
    except Exception:
        pass


def _shot_frame_worker(shot_id: int, kind: str, workflow: str, prompt: str,
                       negative: str, seed: int) -> None:
    try:
        graph = build_graph(workflow, prompt, negative, seed)
        prompt_id = cc.submit_prompt(graph, str(uuid.uuid4()), timeout=cc.SUBMIT_TIMEOUT)
        entry = cc.wait_history(
            prompt_id,
            timeout=max(30, int(Config.AI_VIDEO_GENERATE_TIMEOUT or 300)),
            interval=POLL_INTERVAL,
        )
        if entry is None:
            raise AIBARError("comfyui_timeout", "ComfyUI 任务超时")
        image = _first_image(entry)
        if image is None:
            raise AIBARError("no_output", "工作流没有产出图片")
        data = _download_image(image)
        if not data:
            raise AIBARError("download_failed", "产出图下载失败")
        name = _save_shot_frame(shot_id, kind, data)
        service.set_shot_frame_ref(shot_id, kind, "frame:%d:%s" % (int(shot_id), name))
        service.set_shot_frame_status(
            shot_id, kind, "ready", error="", finished_at=now(),
            seed=int(seed), workflow=workflow, filename=name,
        )
        safe_log(_LOGGER, 20, "ai_video_shot_frame_generated", shot_id=shot_id, kind=kind)
    except cc.ComfyUIError as exc:
        _mark_shot_frame_failed(shot_id, kind, _human_error(exc.code, exc.message))
    except AIBARError as exc:
        _mark_shot_frame_failed(shot_id, kind, _human_error(exc.code, exc.message))
    except BaseException as exc:
        _mark_shot_frame_failed(shot_id, kind, "生成异常：%s" % type(exc).__name__)


def generate_shot_frame(shot_id: int, kind: str, payload: dict[str, Any] | None = None,
                        *, background: bool = True) -> dict[str, Any]:
    """用当前镜头的画面提示生成首帧或尾帧，后台执行。"""
    body = payload or {}
    shot = service.get_shot(shot_id)
    project = service.get_project(int(shot["project_id"]))
    scene_row = db.query_one("SELECT * FROM ai_video_scenes WHERE id=?", (int(shot["scene_id"]),))
    scene = dict(scene_row) if scene_row is not None else {}
    normalized_kind = str(kind).strip().lower()
    if normalized_kind in ("start", "first", "first_frame"):
        key = "start"
    elif normalized_kind in ("end", "last", "last_frame"):
        key = "end"
    else:
        raise AIBARError("invalid_input", "帧类型必须是 start 或 end")

    statuses = dict(shot.get("frame_status") or {})
    if str((statuses.get(key) or {}).get("status") or "") == "generating":
        raise AIBARError("conflict", "该镜头这一帧正在生成中，请稍后再试", 409)

    snapshot = dict(shot.get("snapshot") or {})
    workflow = (
        str(body.get("workflow") or "").strip()
        or str(snapshot.get("workflow") or "").strip()
        or str(project.get("default_workflow") or "").strip()
        or service.recommended_workflow()
    )
    if not workflow:
        raise AIBARError("workflow_missing", "本机没有可用的出图工作流，请先同步 ComfyUI 工作流")
    prompt = str(body.get("prompt") or "").strip() or _build_shot_prompt(shot, scene, project, key)
    negative = str(body.get("negative") or "").strip() or service.build_asset_negative({}, project)

    seed_raw = body.get("seed")
    if seed_raw not in (None, ""):
        try:
            seed = int(seed_raw)
        except (TypeError, ValueError):
            raise AIBARError("invalid_input", "种子必须是整数")
    elif str(body.get("randomize") or "").lower() in ("1", "true", "yes", "on"):
        seed = int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647
    else:
        same = (statuses.get("start") or {}) if key == "end" else (statuses.get(key) or {})
        seed = int(same.get("seed")) if same.get("seed") not in (None, "") else int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647

    with _FRAME_CLAIM_LOCK:
        current = service.get_shot(shot_id)
        current_status = (current.get("frame_status") or {}).get(key) or {}
        if str(current_status.get("status") or "") == "generating":
            raise AIBARError("conflict", "该镜头这一帧正在生成中，请稍后再试", 409)
        service.set_shot_frame_status(
            shot_id, key, "generating", error="", started_at=now(),
            seed=int(seed), workflow=workflow,
        )
    if background:
        threading.Thread(
            target=_shot_frame_worker,
            args=(int(shot_id), key, workflow, prompt, negative, int(seed)),
            name="aibar-shot-frame-%s-%s" % (shot_id, key),
            daemon=True,
        ).start()
    else:
        _shot_frame_worker(int(shot_id), key, workflow, prompt, negative, int(seed))
    return service.get_shot(shot_id)


def _workflow_file(filename: str) -> Path | None:
    if not filename:
        return None
    base = sync_paths.workflows_dir()
    if base is None:
        return None
    target = sync_paths.safe_join(base, filename)
    return target if target is not None and target.is_file() else None


def _node_title(node: dict) -> str:
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    return str(meta.get("title") or node.get("title") or "").strip().lower()


def _is_negative_node(node: dict) -> bool:
    title = _node_title(node)
    if title:
        return any(key in title for key in ("neg", "负面", "反向", "排除"))
    text = str((node.get("inputs") or {}).get("text") or "").lower()
    if text:
        return any(key in text for key in NEGATIVE_HINTS)
    return False


def _text_encode_nodes(graph: dict) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for node_id, node in graph.items():
        if isinstance(node, dict) and TEXT_ENCODE_HINT in str(node.get("class_type") or ""):
            found.append((node_id, node))
    return found


def _inject_prompt(graph: dict, positive: str, negative: str, seed: int | None) -> None:
    if positive:
        nodes = _text_encode_nodes(graph)
        if nodes:
            pos_node = None
            neg_node = None
            for _nid, node in nodes:
                if _is_negative_node(node):
                    if neg_node is None:
                        neg_node = node
                elif pos_node is None:
                    pos_node = node
            if pos_node is None:
                pos_node = nodes[0][1]
            if neg_node is None and len(nodes) >= 2:
                neg_node = nodes[1][1]
            pos_node.setdefault("inputs", {})["text"] = positive
            if negative and neg_node is not None and neg_node is not pos_node:
                neg_node.setdefault("inputs", {})["text"] = negative
    if seed is not None:
        for node in graph.values():
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            for key in ("seed", "noise_seed"):
                if key in inputs:
                    inputs[key] = int(seed)
                    break


def build_graph(workflow: str, prompt: str, negative: str, seed: int | None) -> dict:
    target = _workflow_file(workflow)
    if target is None:
        raise AIBARError("workflow_missing", "出图工作流不存在，请先同步 ComfyUI 工作流")
    try:
        raw_text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        raise AIBARError("read_error", "读取工作流失败")
    try:
        graph = workflow_convert.convert_text(raw_text)
    except ValueError:
        raise AIBARError("invalid_workflow", "工作流无法解析")
    except Exception:
        raise AIBARError("convert_failed", "工作流转换失败，请确认 ComfyUI 已运行")
    if not graph:
        raise AIBARError("empty_workflow", "工作流没有可执行节点")
    _inject_prompt(graph, prompt, negative, seed)
    return graph


def _first_image(entry: dict) -> dict | None:
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    for value in outputs.values():
        if not isinstance(value, dict):
            continue
        for key in ("images", "gifs"):
            files = value.get(key)
            if not isinstance(files, list):
                continue
            for item in files:
                if isinstance(item, dict) and item.get("filename"):
                    return {
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder") or "",
                        "type": item.get("type") or "output",
                    }
    return None


def _download_image(image: dict) -> bytes | None:
    from urllib.parse import quote

    params = {
        "filename": image["filename"],
        "subfolder": image.get("subfolder") or "",
        "type": image.get("type") or "output",
    }
    url = Config.comfyui_base() + "/view?" + "&".join(
        "%s=%s" % (key, quote(str(value))) for key, value in params.items()
    )
    try:
        response = requests.get(url, timeout=30.0, stream=True)
    except requests.RequestException:
        return None
    if int(getattr(response, "status_code", 500)) != 200:
        return None
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > DOWNLOAD_MAX_BYTES:
                return None
            chunks.append(chunk)
    except requests.RequestException:
        return None
    finally:
        try:
            response.close()
        except Exception:
            pass
    return b"".join(chunks)


def _save_image(project_id: int, asset_id: int, data: bytes, label: str = "single") -> tuple[str, dict[str, Any]]:
    root = Path(Config.DATA_DIR) / "ai_video_assets" / str(int(project_id))
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    slug = "".join(ch for ch in str(label or "single") if ch.isalnum())[:16] or "single"
    path = root / ("%d_%s_%s.png" % (int(asset_id), slug, digest))
    path.write_bytes(data)
    meta: dict[str, Any] = {}
    try:
        with Image.open(path) as image:
            meta["width"] = int(image.width)
            meta["height"] = int(image.height)
            meta["format"] = str(image.format or "PNG")
    except Exception:
        pass
    # 多图资产共用同一目录；这里不删旧图，由“删除图片”动作显式清理。
    return str(path.relative_to(Config.DATA_DIR)).replace("\\", "/"), meta


def _human_error(code: str, message: str) -> str:
    hints = {
        "comfyui_offline": "ComfyUI 未运行，请先启动 ComfyUI",
        "comfyui_timeout": "ComfyUI 等待超时，模型可能正在加载或显存不足",
        "workflow_missing": "出图工作流不存在，请先同步 ComfyUI 工作流",
        "missing_model": "工作流引用的模型不存在，请在 ComfyUI 中补齐模型",
        "workflow_failed": "工作流执行失败，请检查节点与参数",
        "no_output": "工作流没有产出图片，请确认有 SaveImage 节点",
        "download_failed": "产出图下载失败",
    }
    base = hints.get(code, "")
    detail = (message or "").strip()
    if base and detail:
        return base + "：" + detail[:200]
    return base or detail[:300] or "生成失败"


def _insert_asset_image(asset_id: int, project_id: int, label: str, prompt: str,
                        saved: str, workflow: str, seed: int, meta: dict[str, Any]) -> int:
    cur = db.execute(
        """INSERT INTO ai_video_asset_images
           (asset_id,project_id,label,prompt,image_path,workflow,seed,width,height,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            int(asset_id), int(project_id), str(label or ""), prompt, saved, workflow, int(seed),
            int(meta.get("width") or 0), int(meta.get("height") or 0), now(),
        ),
    )
    return int(getattr(cur, "lastrowid", 0) or 0)


def _worker(asset_id: int, project_id: int, workflow: str, prompt: str, negative: str, seed: int) -> None:
    try:
        graph = build_graph(workflow, prompt, negative, seed)
        prompt_id = cc.submit_prompt(graph, str(uuid.uuid4()), timeout=cc.SUBMIT_TIMEOUT)
        entry = cc.wait_history(
            prompt_id,
            timeout=max(30, int(Config.AI_VIDEO_GENERATE_TIMEOUT or 300)),
            interval=POLL_INTERVAL,
        )
        if entry is None:
            raise AIBARError("comfyui_timeout", "ComfyUI 任务超时")
        image = _first_image(entry)
        if image is None:
            raise AIBARError("no_output", "工作流没有产出图片")
        data = _download_image(image)
        if not data:
            raise AIBARError("download_failed", "产出图下载失败")
        saved, meta = _save_image(project_id, asset_id, data, label="single")
        _insert_asset_image(asset_id, project_id, "单张", prompt, saved, workflow, int(seed), meta)
        current = service.get_asset(asset_id)
        metadata = dict(current.get("metadata") or {})
        metadata.update(meta)
        db.execute(
            """UPDATE ai_video_assets
               SET status='ready', image_path=?, prompt=?, negative=?, workflow=?, seed=?,
                   metadata=?, error_message='', last_generated_at=?, updated_at=?
               WHERE id=?""",
            (
                saved, prompt, negative, workflow, int(seed), db.dumps(metadata), now(), now(), asset_id,
            ),
        )
        db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (now(), project_id))
        safe_log(_LOGGER, 20, "ai_video_asset_generated", asset_id=asset_id, workflow=workflow, bytes=len(data))
    except cc.ComfyUIError as exc:
        _mark_failed(asset_id, _human_error(exc.code, exc.message))
    except AIBARError as exc:
        _mark_failed(asset_id, _human_error(exc.code, exc.message))
    except BaseException as exc:
        _mark_failed(asset_id, "生成异常：%s" % type(exc).__name__)
        safe_log(_LOGGER, 40, "ai_video_asset_crashed", asset_id=asset_id, error_type=type(exc).__name__)


def _mark_failed(asset_id: int, message: str) -> None:
    try:
        db.execute(
            "UPDATE ai_video_assets SET status='failed', error_message=?, updated_at=? WHERE id=?",
            (message[:500], now(), asset_id),
        )
    except Exception:
        pass


def generate_asset(asset_id: int, payload: dict[str, Any] | None = None, *, background: bool = True) -> dict[str, Any]:
    """为角色/场景资产生成或重新生成一张图片。

    请求体可选：``workflow`` / ``prompt`` / ``negative`` / ``seed`` / ``randomize``。
    ``randomize=True`` 时换新种子；否则优先沿用资产已有种子，保证同一张脸稳定。
    """
    body = payload or {}
    asset = service.get_asset(asset_id)
    project = service.get_project(int(asset["project_id"]))
    workflow = (
        str(body.get("workflow") or "").strip()
        or str(asset.get("workflow") or "").strip()
        or str(project.get("default_workflow") or "").strip()
    )
    if not workflow:
        workflow = service.recommended_workflow()
    if not workflow:
        raise AIBARError("workflow_missing", "本机没有可用的出图工作流，请先同步 ComfyUI 工作流")

    prompt = service.build_asset_prompt(
        dict(asset, prompt=body.get("prompt") or asset.get("prompt") or ""), project
    )
    negative = service.build_asset_negative(
        dict(asset, negative=body.get("negative") or asset.get("negative") or ""), project
    )
    seed_raw = body.get("seed")
    if seed_raw not in (None, ""):
        try:
            seed = int(seed_raw)
        except (TypeError, ValueError):
            raise AIBARError("invalid_input", "种子必须是整数")
    elif str(body.get("randomize") or "").lower() in ("1", "true", "yes", "on"):
        seed = int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647
    else:
        existing = asset.get("seed")
        seed = int(existing) if existing not in (None, "") else int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647

    metadata = dict(asset.get("metadata") or {})
    metadata.pop("set_progress", None)
    cur = db.execute(
        """UPDATE ai_video_assets
           SET status='generating', error_message='', generation_attempts=generation_attempts+1,
               prompt=?, negative=?, workflow=?, seed=?, metadata=?, updated_at=?
           WHERE id=? AND status<>'generating'""",
        (prompt, negative, workflow, int(seed), db.dumps(metadata), now(), int(asset_id)),
    )
    if int(getattr(cur, "rowcount", 0) or 0) <= 0:
        raise AIBARError("conflict", "该资产正在生成中，请稍后再试", 409)

    if background:
        threading.Thread(
            target=_worker,
            args=(int(asset_id), int(asset["project_id"]), workflow, prompt, negative, int(seed)),
            name="aibar-ai-video-asset-%s" % asset_id,
            daemon=True,
        ).start()
    else:
        _worker(int(asset_id), int(asset["project_id"]), workflow, prompt, negative, int(seed))
    return service.get_asset(asset_id)


def _update_set_progress(asset_id: int, *, total: int, completed: int,
                         successes: int, failures: int, current_label: str = "") -> None:
    """把整组生成进度写回 metadata，供卡片/轮询实时显示。"""
    try:
        asset = service.get_asset(asset_id)
        metadata = dict(asset.get("metadata") or {})
        metadata["set_progress"] = {
            "total": int(total),
            "completed": int(completed),
            "successes": int(successes),
            "failures": int(failures),
            "current_label": current_label,
            "finished": completed >= total,
        }
        db.execute(
            "UPDATE ai_video_assets SET metadata=?, updated_at=? WHERE id=?",
            (db.dumps(metadata), now(), int(asset_id)),
        )
    except Exception:
        pass


def _set_worker(asset_id: int, project_id: int, workflow: str, base_prompt: str,
                negative: str, seed: int, items: list[tuple[str, str]]) -> None:
    successes = 0
    failures = 0
    completed = 0
    last_error = ""
    cover_set = False
    total = len(items)
    for label, suffix in items:
        _update_set_progress(
            asset_id, total=total, completed=completed, successes=successes,
            failures=failures, current_label=label,
        )
        prompt = base_prompt + ", " + suffix
        try:
            graph = build_graph(workflow, prompt, negative, seed)
            prompt_id = cc.submit_prompt(graph, str(uuid.uuid4()), timeout=cc.SUBMIT_TIMEOUT)
            entry = cc.wait_history(
                prompt_id,
                timeout=max(30, int(Config.AI_VIDEO_GENERATE_TIMEOUT or 300)),
                interval=POLL_INTERVAL,
            )
            if entry is None:
                raise AIBARError("comfyui_timeout", "ComfyUI 任务超时")
            image = _first_image(entry)
            if image is None:
                raise AIBARError("no_output", "工作流没有产出图片")
            data = _download_image(image)
            if not data:
                raise AIBARError("download_failed", "产出图下载失败")
            saved, meta = _save_image(project_id, asset_id, data, label=label)
            _insert_asset_image(asset_id, project_id, label, prompt, saved, workflow, seed, meta)
            if not cover_set:
                _apply_cover(asset_id, project_id, saved, workflow, seed, meta, prompt, negative)
                cover_set = True
            successes += 1
        except cc.ComfyUIError as exc:
            failures += 1
            last_error = _human_error(exc.code, exc.message)
        except AIBARError as exc:
            failures += 1
            last_error = _human_error(exc.code, exc.message)
        except BaseException as exc:
            failures += 1
            last_error = "生成异常：%s" % type(exc).__name__
        finally:
            completed += 1
            _update_set_progress(
                asset_id, total=total, completed=completed, successes=successes,
                failures=failures, current_label="",
            )

    if successes:
        partial = "" if not failures else "部分生成失败：%d/%d 张失败" % (failures, total)
        db.execute(
            "UPDATE ai_video_assets SET status='ready', error_message=?, last_generated_at=?, updated_at=? WHERE id=?",
            (partial, now(), now(), asset_id),
        )
        safe_log(
            _LOGGER, 20, "ai_video_asset_set_generated", asset_id=asset_id,
            count=successes, failed=failures,
        )
    else:
        _mark_failed(asset_id, last_error or "整组图片生成失败")


def _apply_cover(asset_id: int, project_id: int, saved: str, workflow: str, seed: int,
                 meta: dict[str, Any], prompt: str, negative: str) -> None:
    current = service.get_asset(asset_id)
    metadata = dict(current.get("metadata") or {})
    metadata.update(meta)
    db.execute(
        """UPDATE ai_video_assets
           SET image_path=?, prompt=?, negative=?, workflow=?, seed=?,
               metadata=?, error_message='', last_generated_at=?, updated_at=?
           WHERE id=?""",
        (saved, prompt, negative, workflow, int(seed), db.dumps(metadata), now(), now(), asset_id),
    )
    db.execute("UPDATE ai_video_projects SET updated_at=? WHERE id=?", (now(), project_id))


def generate_asset_set(asset_id: int, payload: dict[str, Any] | None = None, *, background: bool = True) -> dict[str, Any]:
    """按“多角度/表情”一次生成一组角色图。

    与单张生成共用同一套工作流/种子/提示词规则；差异是顺序跑多张，
    每张写一条 ``ai_video_asset_images``，首张成功图自动成为封面。
    """
    body = payload or {}
    asset = service.get_asset(asset_id)
    project = service.get_project(int(asset["project_id"]))
    preset = str(body.get("preset") or "angles").strip()
    if preset == "custom":
        raw = body.get("labels")
        if not isinstance(raw, list) or not raw:
            raise AIBARError("invalid_input", "自定义组需要提供 labels 数组")
        items = [(str(item).strip()[:40], str(item).strip()[:80]) for item in raw if str(item).strip()]
    else:
        items = list(PROMPT_PRESETS.get(preset, []))
    if not items:
        raise AIBARError("invalid_input", "未知的出图组类型")
    if len(items) > 8:
        raise AIBARError("invalid_input", "一次最多生成 8 张，请减少分组")
    labels = [label for label, _suffix in items]

    workflow = (
        str(body.get("workflow") or "").strip()
        or str(asset.get("workflow") or "").strip()
        or str(project.get("default_workflow") or "").strip()
    )
    if not workflow:
        workflow = service.recommended_workflow()
    if not workflow:
        raise AIBARError("workflow_missing", "本机没有可用的出图工作流，请先同步 ComfyUI 工作流")

    metadata = dict(asset.get("metadata") or {})
    base_prompt = service.build_asset_prompt(
        dict(asset, prompt=body.get("prompt") or metadata.get("base_prompt") or asset.get("prompt") or ""),
        project,
    )
    negative = service.build_asset_negative(
        dict(asset, negative=body.get("negative") or asset.get("negative") or ""), project
    )
    seed_raw = body.get("seed")
    if seed_raw not in (None, ""):
        try:
            seed = int(seed_raw)
        except (TypeError, ValueError):
            raise AIBARError("invalid_input", "种子必须是整数")
    elif str(body.get("randomize") or "").lower() in ("1", "true", "yes", "on"):
        seed = int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647
    else:
        existing = asset.get("seed")
        seed = int(existing) if existing not in (None, "") else int.from_bytes(uuid.uuid4().bytes[:4], "big") % 2_147_483_647

    metadata["base_prompt"] = base_prompt
    metadata["last_set"] = preset
    metadata["set_progress"] = {
        "total": len(items), "completed": 0, "successes": 0,
        "failures": 0, "current_label": labels[0] if labels else "", "finished": False,
    }
    cur = db.execute(
        """UPDATE ai_video_assets
           SET status='generating', error_message='', generation_attempts=generation_attempts+1,
               prompt=?, negative=?, workflow=?, seed=?, metadata=?, updated_at=?
           WHERE id=? AND status<>'generating'""",
        (base_prompt, negative, workflow, int(seed), db.dumps(metadata), now(), int(asset_id)),
    )
    if int(getattr(cur, "rowcount", 0) or 0) <= 0:
        raise AIBARError("conflict", "该资产正在生成中，请稍后再试", 409)

    if background:
        threading.Thread(
            target=_set_worker,
            args=(int(asset_id), int(asset["project_id"]), workflow, base_prompt, negative, int(seed), items),
            name="aibar-ai-video-asset-set-%s" % asset_id,
            daemon=True,
        ).start()
    else:
        _set_worker(int(asset_id), int(asset["project_id"]), workflow, base_prompt, negative, int(seed), items)
    return {"asset": service.get_asset(asset_id), "labels": labels, "count": len(items)}


__all__ = ["build_graph", "generate_asset", "generate_asset_set", "generate_shot_frame"]

