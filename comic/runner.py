"""M12 · 漫画工作室出图执行器（与 ComfyUI 对接的队列后端）。

职责：把一个 ``comic_pages`` 行的预制提示词 + 工作流，提交给 ComfyUI 生成，
轮询历史并下载产出图，落盘到 ``data/comic_outputs/`` 并更新数据库。

硬性约束（沿用 HARNESS §6 + M9/M10 风格）：
- 每个外部调用都带超时、可失败降级；ComfyUI 不可达时任务标记 failed + comfyui_offline，不阻塞队列；
- 不抛出未捕获异常到 worker 线程之外；失败只记 error_code 与类型；
- 产出图只存受控相对路径（``comic_outputs/<project>/<chapter>/<page>_<hash>.png``），
  不保存用户原始路径、不泄漏密钥。
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from config import Config
from core import db
from core.db import now
from core.errors import AIBARError
from core.fileops import purge_stale
from core.logging_setup import get_logger, safe_log
from sync import paths as _sync_paths
from sync import workflow_convert
from reverse import comfyui_client as _cc

_LOGGER = get_logger("aibar.comic.runner")

OUTPUT_ROOT = Path(Config.DATA_DIR) / "comic_outputs"

# 视为"文本编码"节点的 class（用于注入正负向提示词）
_TEXT_ENCODE_HINT = "CLIPTextEncode"

# 负向提示词常见开头（用于在节点没有标题时，靠既有文本判断它是负向节点）
_NEGATIVE_HINTS = (
    "ugly", "blurry", "low quality", "worst quality", "bad anatomy", "watermark",
    "text,", "signature", "jpeg artifacts", "低质量", "模糊", "畸形",
)

# 失败重试：只对「瞬时/环境类」故障重试（ComfyUI 掉线、提交被拒、等待超时、下载失败），
# 工作流本身有问题（缺节点、缺模型、无输出）重试也没用，直接失败更诚实。
MAX_ATTEMPTS = 3
RETRYABLE_CODES = {
    "comfyui_offline",
    "comfyui_timeout",
    "submit_failed",
    "wait_failed",
    "timeout",
    "download_failed",
}
RETRY_BACKOFF_SECONDS = (8, 25)  # 第 1 次重试等 8s，之后 25s

# 单张产出图的大小上限（默认 64MB）。ComfyUI 可能产出超大图，
# 无上限地 resp.content 一次性读入内存会拖垮整个进程（连带所有后台线程）。
DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024

# 判定「卡死」的阈值：running 超过这个秒数仍未收尾，视为上次进程被杀留下的孤儿任务。
# ComfyUI 单张出图正常在几十秒到几分钟，给足 15 分钟余量以避免误伤。
STALE_RUNNING_SECONDS = 15 * 60

# worker 心跳：_loop 每轮写一次，看护线程据此判断 worker 是否还活着。
_HEARTBEAT: dict[str, float | int | bool] = {"at": 0.0, "polls": 0, "restarts": 0, "alive": False}

_DEFAULT_POLL_INTERVAL = 2.0


def output_root() -> Path:
    return OUTPUT_ROOT


def _workflow_file(filename: str) -> Path | None:
    """路径穿越防护下取工作流文件绝对路径；不存在返回 None。"""
    if not filename:
        return None
    base = _sync_paths.workflows_dir()
    if base is None:
        return None
    from sync.paths import safe_join

    target = safe_join(base, filename)
    if target is None or not target.is_file():
        return None
    return target


def build_graph(workflow: str, prompt_text: str, negative_text: str = "", seed: int | None = None) -> dict:
    """读取工作流文件并注入提示词/种子，得到可提交给 ComfyUI 的 API prompt 图。

    这是「工作流 → 可执行图」的唯一实现：分镜出图（``build_api_graph``）与
    演员定妆出图（M13 演员库）共用同一条路径，保证两处的节点识别、正负向判定、
    种子注入行为**完全一致** —— 否则演员定妆图和分镜图会因注入逻辑分叉而长得不像同一个人。

    Raises:
        AIBARError: 工作流缺失或无法解析。
    """
    wf = (workflow or "").strip()
    target = _workflow_file(wf)
    if target is None:
        raise AIBARError("workflow_missing", f"工作流文件不存在：{wf}")
    try:
        raw_text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise AIBARError("read_error", f"读取工作流失败：{exc}")
    try:
        graph = workflow_convert.convert_text(raw_text)
    except ValueError as exc:
        raise AIBARError("invalid_workflow", f"工作流无法解析：{exc}")
    except Exception as exc:  # 转换异常（多为 object_info 拉取失败）
        raise AIBARError("convert_failed", f"工作流转换失败：{exc}")
    if not graph:
        raise AIBARError("empty_workflow", "工作流没有可执行的节点")
    _inject_prompt(graph, prompt_text or "", negative_text or "", seed)
    return graph


def build_api_graph(project: dict, page: dict) -> dict:
    """根据页面预制提示词 + 工作流，构造可提交给 ComfyUI 的 API prompt 图。

    Raises:
        AIBARError: 工作流缺失或无法解析。
    """
    wf = (page.get("workflow_filename") or project.get("default_workflow") or "").strip()
    return build_graph(wf, page.get("prompt_text") or "", page.get("negative_text") or "", page.get("seed"))


def run_graph_once(
    graph: dict,
    timeout: float = 180.0,
    cancel=None,
) -> tuple[bytes | None, str, str]:
    """提交一张已构造好的 API graph 并同步取回首张产出图。

    返回 ``(图片字节, 错误码, 原始信息)``；成功时错误码为空串。绝不抛异常，
    全部收敛为错误码。与 ``generate_once`` 的区别：本函数吃**已经构造好的 graph**
    （而不是工作流文件名），视频转绘（M16）这类需要手搓 graph 的场景直接复用它。
    """
    try:
        prompt_id = _cc.submit_prompt(graph, str(uuid.uuid4()), timeout=_cc.SUBMIT_TIMEOUT)
    except _cc.ComfyUIError as exc:
        return None, exc.code, exc.message
    except Exception as exc:
        return None, "submit_failed", f"提交失败：{type(exc).__name__}"

    try:
        entry = _cc.wait_history(prompt_id, timeout=timeout, cancel=cancel)
    except _cc.ComfyUIError as exc:
        return None, exc.code, exc.message
    except Exception as exc:
        return None, "wait_failed", f"等待结果失败：{type(exc).__name__}"

    if entry is None:
        return None, "timeout", "ComfyUI 任务超时（超过 %d 秒未产出）" % int(timeout)

    img = _first_image(entry)
    if img is None:
        return None, "no_output", "ComfyUI 未产出图片（工作流缺少 SaveImage 节点？）"

    data = _download_output_bytes(img["filename"], img["subfolder"], img["type"])
    if not data:
        return None, "download_failed", "下载产出图失败"
    return data, "", ""


def generate_once(
    workflow: str,
    prompt_text: str,
    negative_text: str = "",
    seed: int | None = None,
    timeout: float = 180.0,
    cancel=None,
) -> tuple[bytes | None, str, str]:
    """同步跑一次出图，返回 ``(图片字节, 错误码, 原始信息)``；成功时错误码为空串。

    与 ``_execute`` 的区别：**不碰任何数据库**（不依赖 comic_jobs / comic_pages），
    因此可被「演员定妆重新生成」这类非分镜场景直接复用。
    调用方自己负责状态机与落盘。绝不抛异常，全部收敛为错误码。
    """
    try:
        graph = build_graph(workflow, prompt_text, negative_text, seed)
    except AIBARError as exc:
        return None, exc.code, exc.message
    except Exception as exc:
        return None, "internal_error", f"构造工作流失败：{type(exc).__name__}"

    return run_graph_once(graph, timeout=timeout, cancel=cancel)


def _node_title(node: dict) -> str:
    """取节点标题（ComfyUI 的 ``_meta.title`` 或节点上的 title），用于识别正负向。"""
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    title = meta.get("title") or node.get("title") or ""
    return str(title).strip().lower()


def _is_negative_node(node: dict) -> bool:
    """判断一个文本编码节点是否为「负向」节点。

    优先级：节点标题（``negative`` / ``neg`` / ``负面`` / ``反向``）→ 节点已有文本是否像负面词 → 位置。
    只按「前两个 CLIPTextEncode 的顺序」猜会在自定义工作流上把正负提示词写反，这里做多层兜底。
    """
    title = _node_title(node)
    if title:
        return any(k in title for k in ("neg", "负面", "反向", "排除"))
    text = str((node.get("inputs") or {}).get("text") or "").lower()
    if text:
        return any(k in text for k in _NEGATIVE_HINTS)
    return False


def _inject_prompt(graph: dict, positive: str, negative: str, seed: int | None) -> None:
    """把预制提示词注入到工作流的文本编码节点，可选设置种子。"""
    if positive:
        nodes = _text_encode_nodes(graph)
        if nodes:
            # 优先按标题 / 既有内容定位负向节点；识别不出来时退回「第一个正向、其余里第一个负向」
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
            pos_node["inputs"]["text"] = positive
            if negative and neg_node is not None and neg_node is not pos_node:
                neg_node["inputs"]["text"] = negative
    if seed is not None:
        for node in graph.values():
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            # 兼容 KSampler 的 ``seed`` 与 FLUX.2 RandomNoise 的 ``noise_seed``
            for key in ("seed", "noise_seed"):
                if key in inputs:
                    inputs[key] = int(seed)
                    break


def _text_encode_nodes(graph: dict) -> list:
    found = []
    for _nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        if _TEXT_ENCODE_HINT in ct:
            found.append((_nid, node))
    return found


def _download_output_bytes(filename: str, subfolder: str, ctype: str) -> bytes | None:
    """从 ComfyUI 下载一张产出图（同源 /view）。返回字节或 None。

    流式读取并强制大小上限（``DOWNLOAD_MAX_BYTES``）：超过上限立即中断，
    绝不让一张异常大的图把整个进程的内存吃光。
    """
    from urllib.parse import quote

    params = {
        "filename": filename,
        "subfolder": subfolder or "",
        "type": ctype or "output",
    }
    url = (
        f"{Config.comfyui_base()}/view?"
        + "&".join(f"{k}={quote(v)}" for k, v in params.items())
    )
    try:
        resp = requests.get(url, timeout=30.0, stream=True)
    except Exception:
        return None
    if getattr(resp, "status_code", 500) != 200:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > DOWNLOAD_MAX_BYTES:
                safe_log(_LOGGER, 30, "comic_download_too_large", bytes=total)
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception:
        return None
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _first_image(entry: dict) -> dict | None:
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    for _node_id, value in outputs.items():
        if not isinstance(value, dict):
            continue
        for key in ("images", "gifs"):
            files = value.get(key)
            if not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, dict) and f.get("filename"):
                    return {
                        "filename": f.get("filename"),
                        "subfolder": f.get("subfolder") or "",
                        "type": f.get("type") or "output",
                    }
    return None


def _save_output(project_id: int, chapter_id: int, page_id: int, data: bytes) -> str:
    """落盘分镜图并清理该页的历史产出；返回相对 DATA_DIR 的受控路径。

    **次序是先写新图、再清旧图**（见 ``core.fileops`` 模块说明）：出图要排
    ComfyUI 队列 + 几十秒 GPU，是整条链路里最贵的一步；原先「先删旧图」的写法
    在删除被运行环境拦截（抛 ``SystemExit``，``except OSError`` 抓不到）时，
    会把已经出图成功的分镜整页打成 failed。旧图残留只是多占一点磁盘。
    """
    root = output_root()
    dest_dir = root / str(project_id) / str(chapter_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    dest = dest_dir / f"{page_id}_{digest}.png"
    dest.write_bytes(data)
    # 文件名带内容哈希，反复重生成会不断堆积旧图；清理失败只降级，绝不影响主流程
    purge_stale(dest_dir, "%d_*.png" % int(page_id), keep=dest)
    # 受控相对路径（相对 DATA_DIR），供 /api/comic/output 安全解析
    return str(dest.relative_to(Config.DATA_DIR))


def _register_gallery(saved_rel: str, page: dict) -> str | None:
    """把刚落盘的产出图登记进图库，返回 image_id。

    漫画产出写在 ``data/comic_outputs``，而图库扫描只覆盖 ComfyUI 产出目录，
    于是分镜图永远进不了图库 —— 「对这张分镜反推提示词」「从图库打开深链」
    这两条现成链路对漫画完全不可用。出图成功后顺手登记一次即可打通。

    图库登记属于旁路增强：任何失败都只记日志并返回 None，绝不能让
    「图已出好」这件事因为登记失败而变成失败任务。
    """
    try:
        from sync import scanner

        abs_path = Path(Config.DATA_DIR) / saved_rel
        return scanner.register_image(
            abs_path,
            prompt=page.get("prompt_text") or "",
            workflow_link=page.get("workflow_filename") or "",
        )
    except Exception as exc:
        safe_log(
            _LOGGER,
            30,
            "comic_gallery_register_failed",
            page_id=page.get("id"),
            error_type=type(exc).__name__,
        )
        return None


def _job_still_running(job_id: int) -> bool:
    """任务是否仍处于本 worker 的「运行中」状态。

    用户在出图过程中点「取消」时，``cancel_job`` 会把 job 置为 ``cancelled``。
    收尾更新若不带这个守卫，就会把已取消的任务复活成 done/failed —— 取消操作凭空消失。
    """
    row = db.query_one("SELECT status FROM comic_jobs WHERE id=?", (job_id,))
    return row is not None and row["status"] == "running"


def _mark_failed(job_id: int, page: dict | None, code: str, message: str) -> None:
    """标记任务最终失败：把可读原因同时写到 job 与 page，供 UI 直接展示。

    收尾更新带 ``AND status='running'`` 守卫：任务已被取消时**不动**页面，
    否则会把用户刚取消的页重新打成 failed，或者更糟——把取消后又重新入队的页抹掉。
    """
    with db.tx():
        cur = db.execute(
            "UPDATE comic_jobs SET status='failed', stage='failed', error_code=?, error_message=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (code, message or "", now(), job_id),
        )
        if not _affected(cur):
            safe_log(_LOGGER, 20, "comic_job_abandoned", job_id=job_id, reason="not_running")
            return
        if page is not None:
            db.execute(
                "UPDATE comic_pages SET status='failed', error_message=? WHERE id=? AND status='generating'",
                (message or "", page["id"]),
            )
    safe_log(_LOGGER, 30, "comic_job_failed", job_id=job_id, error_code=code)


def _schedule_retry(job_id: int, page: dict | None, code: str, message: str, attempt: int) -> None:
    """把可重试的失败重新排队（带退避），避免 ComfyUI 短暂掉线就整批作废。"""
    import time as _time

    backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
    next_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_time.time() + backoff))
    with db.tx():
        cur = db.execute(
            "UPDATE comic_jobs SET status='queued', stage='retry_wait', error_code=?, error_message=?, attempt=?, next_retry_at=? "
            "WHERE id=? AND status='running'",
            (code, message or "", attempt + 1, next_at, job_id),
        )
        if not _affected(cur):
            safe_log(_LOGGER, 20, "comic_job_abandoned", job_id=job_id, reason="cancelled_before_retry")
            return
        if page is not None:
            db.execute(
                "UPDATE comic_pages SET status='queued', error_message=? WHERE id=? AND status='generating'",
                ("%s（%d/%d 次重试，%ds 后重跑）" % (message or "", attempt + 1, MAX_ATTEMPTS, backoff), page["id"]),
            )
    safe_log(_LOGGER, 30, "comic_job_retry_scheduled", job_id=job_id, error_code=code, attempt=attempt + 1)


def _affected(cur) -> int:
    """取 UPDATE/DELETE 的影响行数（不同驱动字段名不同，统一兜底）。"""
    try:
        return int(cur.rowcount or 0)
    except Exception:
        return 1  # 取不到时按「已生效」处理，保持旧行为，不至于误判为被取消


def _human(code: str, message: str) -> str:
    """错误码 + 原始信息 → 中文可读文案（延迟导入，避免与 diagnostics 循环引用）。"""
    try:
        from . import diagnostics

        return diagnostics.human_error(code, message)
    except Exception:
        return message or code


def _claim_job(job_id: int) -> bool:
    """原子地把一个 queued 任务抢占为 running。

    只靠「先 SELECT 再 UPDATE」在多 worker / 多进程部署下会有窗口期，
    同一个 job 可能被跑两次（重复出图、互相覆盖产出文件）。
    这里用带条件的 UPDATE 让数据库来保证只有一个执行者能抢到。
    """
    cur = db.execute(
        "UPDATE comic_jobs SET status='running', stage='submitted', next_retry_at=NULL "
        "WHERE id=? AND status='queued'",
        (job_id,),
    )
    return _affected(cur) > 0


def run_job(job_id: int) -> None:
    """执行单个出图任务：提交 → 轮询 → 下载 → 落盘 → 更新状态。

    瞬时故障（ComfyUI 掉线 / 提交被拒 / 等待超时 / 下载失败）最多重试 ``MAX_ATTEMPTS`` 次，
    带退避重新排队；其余失败直接标记 failed 并写入可读原因，绝不让异常冒泡到 worker。
    """
    if not _claim_job(job_id):
        return  # 已被取消、已被别的 worker 抢走，或不存在

    job = db.row_to_dict(db.query_one("SELECT * FROM comic_jobs WHERE id=?", (job_id,)))
    if job is None:
        return

    page = db.row_to_dict(db.query_one("SELECT * FROM comic_pages WHERE id=?", (job["page_id"],)))
    project = db.row_to_dict(db.query_one("SELECT * FROM comic_projects WHERE id=?", (job["project_id"],)))
    if page is None or project is None:
        _mark_failed(job_id, page, "not_found", _human("not_found", "页面或漫画已删除"))
        return

    attempt = int(job.get("attempt") or 0)
    with db.tx():
        db.execute("UPDATE comic_pages SET status='generating' WHERE id=?", (page["id"],))

    code, message = _execute(job_id, page, project)
    if code is None:
        return
    if code in RETRYABLE_CODES and attempt + 1 < MAX_ATTEMPTS:
        _schedule_retry(job_id, page, code, _human(code, message), attempt)
        return
    _mark_failed(job_id, page, code, _human(code, message))


def _execute(job_id: int, page: dict, project: dict) -> tuple[str | None, str]:
    """真正跑一次出图。成功返回 ``(None, "")``；失败返回 ``(错误码, 原始信息)``。"""
    try:
        graph = build_api_graph(project, page)
    except AIBARError as exc:
        return exc.code, exc.message
    except Exception as exc:
        return "internal_error", f"构造工作流失败：{type(exc).__name__}"

    try:
        prompt_id = _cc.submit_prompt(graph, str(uuid.uuid4()), timeout=_cc.SUBMIT_TIMEOUT)
    except _cc.ComfyUIError as exc:
        return exc.code, exc.message
    except Exception as exc:
        return "submit_failed", f"提交失败：{type(exc).__name__}"

    db.execute(
        "UPDATE comic_jobs SET prompt_id=?, stage='running' WHERE id=? AND status='running'",
        (prompt_id, job_id),
    )

    # 等待期间周期性检查取消信号：用户在长任务中途点「取消」能真的停下来，
    # 而不是等图出完了才发现取消操作被收尾逻辑覆盖掉。
    try:
        entry = _cc.wait_history(
            prompt_id, timeout=180.0, cancel=lambda: not _job_still_running(job_id)
        )
    except _cc.ComfyUIError as exc:
        return exc.code, exc.message
    except Exception as exc:
        return "wait_failed", f"等待结果失败：{type(exc).__name__}"

    if entry is None:
        return "timeout", "ComfyUI 任务超时（超过 3 分钟未产出）"

    img = _first_image(entry)
    if img is None:
        return "no_output", "ComfyUI 未产出图片（工作流缺少 SaveImage 节点？）"

    data = _download_output_bytes(img["filename"], img["subfolder"], img["type"])
    if not data:
        return "download_failed", "下载产出图失败"

    try:
        saved = _save_output(project["id"], page["chapter_id"], page["id"], data)
    except Exception as exc:
        return "save_failed", f"保存产出图失败：{type(exc).__name__}"

    image_id = _register_gallery(saved, page)

    with db.tx():
        cur = db.execute(
            "UPDATE comic_jobs SET status='done', stage='done', error_code='', error_message='', output_path=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (saved, now(), job_id),
        )
        if not _affected(cur):
            # 出图期间被取消了：图已落盘但任务不再是 running，按用户的意图丢弃结果
            safe_log(_LOGGER, 20, "comic_job_discarded", job_id=job_id, reason="cancelled")
            return None, ""
        db.execute(
            "UPDATE comic_pages SET status='done', image_path=?, image_id=?, error_message='', last_job_id=? "
            "WHERE id=? AND status='generating'",
            (saved, image_id, job_id, page["id"]),
        )
    safe_log(_LOGGER, 20, "comic_job_done", job_id=job_id, page_id=page["id"])
    return None, ""


# ---------------------------------------------------------------- 队列 worker


class ComicWorker:
    """后台出图队列 worker：拉取 queued 任务顺序执行。

    可靠性三件事：
    1. **启动幂等** —— 重复 ``start()`` 不会拉起第二个线程（否则同一 job 会被跑两遍）；
    2. **心跳** —— 每轮循环写一次时间戳，外部可据此判断队列是不是真的在转；
    3. **看护** —— 独立线程定期探测，发现 worker 线程死了就重新拉起，
       避免「队列静默停摆，界面上所有页面永远 queued 却没有任何异常提示」。
    """

    def __init__(self, poll_interval: float = _DEFAULT_POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._stop = False
        self._thread = None
        self._guard = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 生命周期

    def start(self):
        """拉起 worker 与看护线程；已在运行则原样返回（幂等）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="aibar-comic-worker", daemon=True)
            self._thread.start()
            if self._guard is None or not self._guard.is_alive():
                self._guard = threading.Thread(target=self._guard_loop, name="aibar-comic-guard", daemon=True)
                self._guard.start()
            return self._thread

    def stop(self) -> None:
        self._stop = True

    def is_alive(self) -> bool:
        """worker 线程是否还活着（``start()`` 之前返回 False）。"""
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------- 主循环

    def _loop(self) -> None:
        while not self._stop:
            _HEARTBEAT["at"] = time.time()
            _HEARTBEAT["alive"] = True
            _HEARTBEAT["polls"] = int(_HEARTBEAT["polls"]) + 1
            # ComfyUI 离线时提交会立刻失败并把任务标 failed，不会空转卡死；
            # 单任务异常绝不终止 worker，保证队列持续可用。
            try:
                # next_retry_at 未到点的重试任务先跳过（退避，避免失败风暴）
                job = db.query_one(
                    "SELECT id FROM comic_jobs WHERE status='queued' "
                    "AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY id ASC LIMIT 1",
                    (now(),),
                )
                if job is None:
                    time.sleep(self.poll_interval)
                    continue
                run_job(job["id"])
            except BaseException as exc:  # 含 BaseException：线程绝不静默死亡
                safe_log(_LOGGER, 30, "comic_worker_error", error_type=type(exc).__name__)
                time.sleep(self.poll_interval)
        _HEARTBEAT["alive"] = False

    # ---------------------------------------------------------- 看护

    def _guard_loop(self) -> None:
        """worker 死了就重新拉起。探测间隔取轮询间隔的 10 倍且有下限。"""
        interval = max(15.0, self.poll_interval * 10)
        while not self._stop:
            time.sleep(interval)
            try:
                if self._stop:
                    return
                if self._thread is not None and self._thread.is_alive():
                    continue
                _HEARTBEAT["restarts"] = int(_HEARTBEAT["restarts"]) + 1
                safe_log(_LOGGER, 30, "comic_worker_restart", restarts=_HEARTBEAT["restarts"])
                with self._lock:
                    self._thread = None
                self.start()
                return  # 新 worker 自带新的看护，当前看护功成身退
            except BaseException as exc:
                safe_log(_LOGGER, 30, "comic_guard_error", error_type=type(exc).__name__)


def worker_status() -> dict:
    """出图队列 worker 的存活状态，供进度接口与排障使用。

    ``stale_seconds`` 为 None 表示从未心跳过（worker 尚未启动）。
    """
    at = float(_HEARTBEAT.get("at") or 0.0)
    stale = int(max(0.0, time.time() - at)) if at > 0 else None
    return {
        "alive": bool(_HEARTBEAT.get("alive")),
        "stale_seconds": stale,
        "polls": int(_HEARTBEAT.get("polls") or 0),
        "restarts": int(_HEARTBEAT.get("restarts") or 0),
        "poll_interval": _DEFAULT_POLL_INTERVAL,
    }
