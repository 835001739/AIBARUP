"""M12 · 漫画分镜工作流：从世界观 / 剧情摘要自动生成分集与出图队列。

能力映射（用户需求）：
1. 进入分镜工作流时展示**可编辑的世界观 / 剧情摘要**（存于 ``comic_projects.worldview`` /
   ``plot_summary``，前端编辑面板负责）。
2. 自动把剧情拆分为「集（episode）」，按「出图数量（每章页数）」把每集剧情转换为 N 张分镜页，
   为**每一页**生成（并扩写）图片提示词，以「集 = 章节、每集 N 页 = 分镜页」的模型落库，并入队出图。
3. 每集对应一个章节，用户点击章节即可查看该集产出的漫画图片（复用既有章节视图）。
4. **出图风格**：每一页提示词都会按项目选择的风格（``comic_projects.style_preset``，见
   ``comic/styles.py``）统一规范——追加同一套风格描述词与排除项，保证整部漫画每一集风格一致。
5. **人物一致性**：为项目维护「角色卡」（``comic_characters``，见 ``comic/characters.py``），
   每一页提示词按出现的角色注入**逐字相同**的角色锚点（外貌 / 服装 / 配色），并用
   「基准种子 + 角色组合 + 章节页序」确定性派生页级种子，使同一个角色跨页、跨集保持稳定。

设计约束（沿用 HARNESS / PRD 风格）：
- 分集拆分**规则优先、确定性、可离线**；``AI_PROVIDER_*`` 启用时尝试用 LLM 拆分，
  任何失败（未启用 / 超时 / 格式错误）一律**自动回退规则引擎**，整体不失败。
- 每集图片提示词的扩写复用 ``prompt.providers.expand``（同样具备 AI→规则降级）。
- 不抛未捕获异常到路由：所有外部依赖调用都包了 try/except，失败只记 warning。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from config import CFG
from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from prompt import engine as _engine
from prompt.providers import expand as _expand_prompt
from . import characters
from . import service
from . import skill_import
from . import styles

_LOGGER = get_logger("aibar.comic.storyboard")

MAX_EPISODES = 24
MAX_PROMPT_LEN = _engine.MAX_PROMPT_LEN
MAX_PAGES_PER_CHAPTER = 12  # 分镜工作流「出图数量」上限：每章最多拆出的漫画页数

# 镜头景别补位：当一集剧情描述不足 N 句时，用不同景别派生出 N 个独立分镜页提示词
_SHOT_FRAMING = [
    "（远景，建立镜头）", "（中景）", "（近景）", "（特写）",
    "（过肩镜头）", "（俯视角度）", "（仰视角度）", "（动态镜头）",
    "（面部特写，情绪）", "（环境空镜）", "（主观镜头）", "（收尾镜头）",
]

# 集标记：第N集/第N话/第N章/第N幕、EPn / Episode n、数字. 数字、 数字）、
_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*(?P<num>[0-9零一二三四五六七八九十百千两]+)\s*(?P<unit>[集话章幕卷])"  # 第1集
    r"|(?:[Ee][Pp]?\s*(?P<ep>[0-9]+))"  # EP1 / Ep 1
    r"|(?:[(（]?\s*(?P<d1>[0-9]+)\s*[.、)）])"  # 1. 2、 3) （4）
    r")"
)


# ---------------------------------------------------------------- 分集拆分（规则）

def _marker_label(m: re.Match) -> str:
    if m.group("num") is not None:
        return "第" + m.group("num").replace(" ", "") + (m.group("unit") or "集")
    if m.group("ep") is not None:
        return "EP" + m.group("ep")
    if m.group("d1") is not None:
        return "第" + m.group("d1") + "集"
    return ""


def _has_marker(blocks: list[str]) -> bool:
    for b in blocks:
        if _MARKER_RE.match(b.splitlines()[0] if b.splitlines() else b):
            return True
    return False


def _split_inline_markers(text: str) -> list[str]:
    """单行内联编号（如「1. 场景A 2. 场景B」）拆成多块。"""
    parts = re.split(r"(?=(?:第\s*[0-9零一二三四五六七八九十百千两]+\s*[集话章幕卷])"
                     r"|(?:[Ee][Pp]?\s*[0-9]+)"
                     r"|(?:[(（]?\s*[0-9]+\s*[.、)）]))", text)
    return [p.strip() for p in parts if p and p.strip()]


def _strip_leading_marks(text: str) -> str:
    """去掉场景描述开头残留的分隔标点：「第1集：少年…」→「少年…」。

    不做这一步时，提示词里会出现「锚点，：少年离开家乡」这种带空标点的脏文本。
    """
    return re.sub(r"^[\s：:，,、；;|·・—\-–~～]+", "", text or "")


def _extract_title_scene(block: str, n: int) -> tuple[str, str]:
    lines = block.splitlines()
    first = lines[0] if lines else block
    m = _MARKER_RE.match(first)
    if m:
        label = _marker_label(m) or ("第%d集" % n)
        # 去掉首行标记前缀，保留后续作为场景描述；若首行本身就是纯标记则整块作场景
        rest = _strip_leading_marks(block[m.end():].strip())
        scene = rest if rest else _strip_leading_marks(block.strip())
        return label, scene
    return ("第%d集" % n), _strip_leading_marks(block.strip())


def _split_text_into_episodes(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    # 优先按内联编号拆分（覆盖「1. a 2. b」与「第1集…第2集…」两种写法）
    inline = _split_inline_markers(text)
    if len(inline) >= 2:
        blocks = inline
    else:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    episodes: list[dict] = []
    for idx, block in enumerate(blocks):
        title, scene = _extract_title_scene(block, idx + 1)
        if not scene:
            continue
        episodes.append({"title": title, "scene": scene})
    return episodes[:MAX_EPISODES]


# ---------------------------------------------------------------- 分集拆分（AI，可选）

_AI_SYSTEM = (
    "你是漫画分镜编剧助手。根据给出的世界观与剧情摘要，把故事拆分为若干「集（episode）」。"
    "只返回一个 JSON 数组，每个元素为对象：{\"title\": 集标题（简短，如「第1集 启程」）, "
    "\"scene\": 该集的核心画面/剧情描述，用于后续生成图片提示词}。"
    "集数控制在 3~12 集；不得输出解释性文字，只输出 JSON 数组。"
)


def _plan_episodes_ai(worldview: str, plot_summary: str) -> tuple[list[dict] | None, str | None]:
    """用 LLM 拆分剧情为集；任何异常返回 (None, reason) 交由上层回退规则。"""
    if not (CFG.AI_PROVIDER_ENABLED and CFG.AI_PROVIDER_ENDPOINT and CFG.AI_PROVIDER_MODEL):
        return None, "AI 扩写服务未启用"
    payload = {
        "model": CFG.AI_PROVIDER_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _AI_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"worldview": worldview or "", "plot_summary": plot_summary or ""},
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if CFG.AI_PROVIDER_API_KEY:
        headers["Authorization"] = "Bearer %s" % CFG.AI_PROVIDER_API_KEY
    try:
        resp = requests.post(
            "%s/chat/completions" % CFG.AI_PROVIDER_ENDPOINT.rstrip("/"),
            headers=headers,
            json=payload,
            timeout=max(1, int(CFG.AI_PROVIDER_TIMEOUT or 60)),
        )
    except Exception as exc:
        return None, "AI 分集请求失败：%s" % type(exc).__name__
    if int(getattr(resp, "status_code", 0) or 0) >= 400:
        return None, "AI 分集服务返回 HTTP %s" % getattr(resp, "status_code", 0)
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, "AI 分集返回结构异常"
    episodes = _parse_ai_episodes(content)
    if not episodes:
        return None, "AI 分集未返回有效集列表"
    return episodes, None


def _parse_ai_episodes(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(text[start : end + 1])
        except ValueError:
            return []
    if not isinstance(obj, list):
        return []
    out: list[dict] = []
    for item in obj[:MAX_EPISODES]:
        if not isinstance(item, dict):
            continue
        scene = str(item.get("scene") or "").strip()
        title = str(item.get("title") or "").strip()
        if not scene:
            continue
        out.append({"title": title or ("第%d集" % (len(out) + 1)), "scene": scene})
    return out


def plan_episodes(worldview: str, plot_summary: str, provider: str = "rules") -> dict:
    """把剧情摘要拆分为集列表。

    Returns:
        ``{"episodes": [...], "provider": str, "warnings": [...]}``
        ``provider`` 标记实际使用的拆分引擎（``rules`` / ``ai``）；AI 失败会回退 ``rules`` 并写入 warning。
    """
    warnings: list[str] = []
    if provider == "openai_compatible":
        ai_episodes, reason = _plan_episodes_ai(worldview, plot_summary)
        if ai_episodes:
            return {"episodes": ai_episodes, "provider": "ai", "warnings": warnings}
        warnings.append("AI 分集不可用（%s），已回退本地规则拆分" % (reason or "未知原因"))
    episodes = _split_text_into_episodes(plot_summary)
    return {"episodes": episodes, "provider": "rules", "warnings": warnings}


# ---------------------------------------------------------------- 生成分镜

def _safe_expand(scene: str, profile: str, intensity: str, provider: str) -> dict:
    """扩写单集场景为图片提示词；非法 profile 自动回退默认档案，任何失败回退原文。"""
    prompt = scene.strip()[:MAX_PROMPT_LEN]
    try:
        return _expand_prompt(prompt, profile, intensity, provider=provider)
    except AIBARError:
        # 多半是 profile 不被识别，回退默认档案重试一次
        try:
            return _expand_prompt(prompt, _engine.DEFAULT_PROFILE, intensity, provider=provider)
        except Exception:
            pass
    except Exception:
        pass
    return {
        "expanded_positive": prompt,
        "expanded_negative": "",
        "provider": "rules",
        "warnings": ["提示词扩写失败，已使用原始场景描述"],
    }


def split_scene_into_beats(scene: str, n: int) -> list[dict]:
    """把一集的剧情描述拆成 n 个分镜页「画面/镜头」描述（每页一张出图提示词）。

    返回 ``[{"beat": 画面描述, "shot": 景别}, ...]``：

    - 句数 >= n：按句均匀取样，覆盖首句与末句，镜头景别按页序轮换；
    - 句数 < n：**循环复用已有句子**（而不是全都堆在末句上），再叠加不同的镜头景别，
      保证同一集里每一页的画面描述都有区分度，不会生成 N 张几乎一模一样的图。

    景别以独立字段返回，便于单独存库并在重算提示词时保留。
    """
    scene = _strip_leading_marks((scene or "").strip())
    n = max(1, min(MAX_PAGES_PER_CHAPTER, int(n)))
    if not scene:
        return [{"beat": "", "shot": ""} for _ in range(n)]

    sentences = [_strip_leading_marks(s.strip()) for s in re.split(r"(?<=[。！？!?；;\n])", scene) if s.strip()]
    if not sentences:
        sentences = [scene]

    # 均匀取样：首句必取、末句必取，中间按等距取样
    if len(sentences) >= n:
        if n == 1:
            picked = [sentences[0]]
        else:
            step = (len(sentences) - 1) / (n - 1)
            picked = [sentences[round(i * step)] for i in range(n)]
    else:
        # 句不够：循环取句（0,1,2,0,1,2…）而不是反复用末句，避免整集画面雷同
        picked = [sentences[i % len(sentences)] for i in range(n)]

    out: list[dict] = []
    for i, text in enumerate(picked):
        shot = _SHOT_FRAMING[i % len(_SHOT_FRAMING)]
        out.append({"beat": text, "shot": shot})
    return out


def _split_scene_into_beats(scene: str, n: int) -> list[str]:
    """兼容旧签名：只返回画面描述列表（景别已合并进文本）。"""
    return [b["beat"] + b["shot"] for b in split_scene_into_beats(scene, n)]


def compose_page_texts(
    base_prompt: str,
    base_negative: str,
    shot: str,
    chars: list[dict],
    setting: str,
    style: str,
    base_seed: Any,
    project_id: Any,
    chapter_id: Any,
    page_idx: int,
) -> tuple[str, str, int]:
    """把「原始扩写提示词」组装成最终出图用的一页提示词。

    这一步是**纯函数**：只依赖传入的 base_prompt + 当前角色卡 + 设定前缀 + 风格，
    因此角色卡改了外貌、或项目换了风格之后，可以拿同一份 base_prompt 重算出新提示词，
    而不必重新拆分剧情（见 ``refresh_project_prompts``）。

    返回 ``(positive, negative, seed)``。
    """
    pos = characters.compose_page_prompt(base_prompt, chars, shot=shot, setting=setting)
    neg = (base_negative or "").strip()
    # 角色自带的排除项 + 通用「反人物漂移」排除项，都并入负面提示词
    for extra in (characters.character_negative(chars), characters.ANTI_DRIFT_NEGATIVE):
        if extra and extra not in neg:
            neg = ("%s, %s" % (neg, extra)) if neg else extra
    # 先给风格词预留长度，避免 apply_style 触发「砍开头」的截断把锚点整段砍掉
    pos = _reserve_style_headroom(pos, style)
    pos, neg = styles.apply_style(pos, neg, style, MAX_PROMPT_LEN)
    seed = characters.derive_seed(
        base_seed, project_id, chapter_id, page_idx, characters.identity_key(chars)
    )
    return pos, neg, seed


def _reserve_style_headroom(positive: str, style: Any) -> str:
    """把正文裁到「风格词追加后仍不超限」的长度，从**尾部**裁。

    ``styles.apply_style`` 超长时走 ``_truncate_keep_tail``，语义是「保尾部风格词」，
    也就是**从开头砍**。而人物锚点正好排在最前面 —— 一旦提示词超长，最先被砍掉的
    就是全部角色特征，一致性当场归零。这里先把正文从尾部裁到预算内，
    让那次截断永远不会发生：宁可少几句画面细节，也不能丢人物锚点。
    """
    add_pos = (styles.get_style(style).get("positive") or "").strip()
    budget = MAX_PROMPT_LEN - len(add_pos) - 2
    if budget <= 0 or len(positive) <= budget:
        return positive
    return positive[:budget].rstrip(" ,，、；;")


def generate_storyboard(
    project_id: int,
    profile: str | None = None,
    intensity: str | None = None,
    provider: str | None = None,
    worldview: str | None = None,
    plot_summary: str | None = None,
    pages_per_chapter: int | None = None,
    style: str | None = None,
    mode: str = "append",
) -> dict:
    """根据项目世界观 / 剧情摘要自动生成分集分镜，并加入出图队列。

    可选先保存 worldview / plot_summary（前端一键生成时复用当前编辑框内容）。
    ``pages_per_chapter`` 控制每集（章节）拆出的漫画页数：剧情会转换为每一页的图片提示词；
    省略时取项目保存的 ``pages_per_chapter`` 设置（默认 1，上限 ``MAX_PAGES_PER_CHAPTER``）。
    ``style`` 指定出图风格（见 ``comic/styles.py``）：传入时会写回项目，并**统一套用到每一集
    的每一页**提示词（正面追加风格描述、负面合并排除项），保证整部漫画风格一致；
    省略时取项目保存的 ``style_preset``（默认 ``none`` = 不限制）。

    ``mode`` 控制与已有章节的关系：

    - ``append``（默认）：保留已有章节，新分集追加在后面；
    - ``rebuild``：先删除该项目已有章节（连带分镜页与队列任务），再重建，
      用于「改完剧情重新生成」而不至于叠加出重复的分集。
    """
    project = service.get_project(project_id)
    _mode = str(mode or "append").strip().lower()
    if _mode not in ("append", "rebuild"):
        raise AIBARError("invalid_input", "生成模式只能是 append 或 rebuild")

    # 可选：先保存编辑框中的世界观 / 剧情摘要 / 出图风格
    save = {}
    if worldview is not None:
        save["worldview"] = str(worldview)
    if plot_summary is not None:
        save["plot_summary"] = str(plot_summary)
    # 出图风格：仅当传入合法预设时才写回项目，非法值不污染已保存的项目设置
    _style_req = styles.normalize_style(style) if styles.is_valid_style(style) else None
    if _style_req is not None:
        save["style_preset"] = _style_req
    if save:
        service.update_project(project_id, save)
        project = service.get_project(project_id)

    wv = (project.get("worldview") or "").strip()
    ps = (project.get("plot_summary") or "").strip()
    if not ps:
        raise AIBARError("invalid_input", "请先填写剧情摘要（plot_summary）再生成分镜")

    if provider is None:
        provider = (
            "openai_compatible"
            if (CFG.AI_PROVIDER_ENABLED and CFG.AI_PROVIDER_ENDPOINT and CFG.AI_PROVIDER_MODEL)
            else "rules"
        )
    _profile = profile or _engine.DEFAULT_PROFILE
    _intensity = intensity or _engine.DEFAULT_INTENSITY
    if _intensity not in _engine.INTENSITIES:
        _intensity = _engine.DEFAULT_INTENSITY

    # 出图数量（每章页数）：请求优先，否则取项目设置，默认 1，收敛到 [1, MAX_PAGES_PER_CHAPTER]
    try:
        ppc = int(pages_per_chapter) if pages_per_chapter not in (None, "") else 0
    except (TypeError, ValueError):
        ppc = 0
    if ppc < 1:
        ppc = int((project.get("pages_per_chapter") or 1) or 1)
    ppc = max(1, min(MAX_PAGES_PER_CHAPTER, ppc))

    # 出图风格：请求优先（合法值已在上一步写回项目），否则取项目设置，非法值收敛为默认预设
    _style = _style_req or styles.normalize_style(project.get("style_preset"))

    plan = plan_episodes(wv, ps, provider)
    episodes = plan["episodes"]
    if not episodes:
        raise AIBARError("invalid_input", "未能从剧情摘要中解析出任何分集")

    default_workflow = (project.get("default_workflow") or "").strip()
    # 零配置兜底：项目未设默认工作流时，自动写入 FLUX.2 最小化工作流并设为默认，
    # 使分镜页无需手动指定即可经 ComfyUI 出图（与 Skill 导入行为一致）。
    if not default_workflow:
        try:
            wf = skill_import.ensure_flux_workflow()
        except Exception:
            wf = None
        if wf:
            default_workflow = wf
            try:
                service.update_project(project_id, {"default_workflow": wf})
                project = service.get_project(project_id)
            except Exception:
                pass
    # 人物一致性：补齐角色卡（只新增缺失的，绝不覆盖用户已编辑的卡片）
    try:
        service.sync_characters(project_id, wv, ps)
    except Exception as exc:  # 角色抽取失败不阻断主流程
        all_warnings_pre = ["角色抽取失败（%s），已跳过人物锚点" % type(exc).__name__]
    else:
        all_warnings_pre = []
    chars = service.list_characters(project_id)

    # 基准种子：缺失时按 project_id 确定性初始化并写回，保证同一项目每次生成都拿到同一组种子
    base_seed = project.get("base_seed")
    if base_seed is None:
        base_seed = characters.default_base_seed(project_id)
        try:
            service.update_project(project_id, {"base_seed": base_seed})
        except Exception:
            pass

    # 世界观设定前缀：全本每一页共用同一句设定描述，保证世界观与氛围一致
    setting = characters.setting_prefix(wv)

    # 重建模式：先清空已有章节（连带分镜页与队列任务），避免反复生成叠加重复分集
    removed_chapters = 0
    if _mode == "rebuild":
        for ch in service.list_chapters(project_id):
            service.delete_chapter(ch["id"])
            removed_chapters += 1
        all_warnings_pre.append("已重建：删除了 %d 个旧章节" % removed_chapters)

    created_chapters: list[dict] = []
    pages = 0
    enqueued = 0
    all_warnings = list(plan.get("warnings", [])) + all_warnings_pre

    for idx, ep in enumerate(episodes):
        scene = _strip_leading_marks((ep.get("scene") or "").strip())
        title = (ep.get("title") or ("第%d集" % (idx + 1))).strip() or ("第%d集" % (idx + 1))
        chapter = service.create_chapter(
            project_id, {"title": title, "order_idx": idx + 1, "summary": scene}
        )
        page_ids: list = []
        if scene:
            # 把这一集的剧情转换为 ppc 个分镜页画面描述，逐页扩写提示词并入队
            for j, item in enumerate(split_scene_into_beats(scene, ppc)):
                beat_text = (item.get("beat") or "").strip() or scene
                shot = item.get("shot") or ""
                exp = _safe_expand(beat_text, _profile, _intensity, provider)
                for w in exp.get("warnings", []) or []:
                    if w not in all_warnings:
                        all_warnings.append(w)
                # 原始扩写结果（未注入锚点 / 风格），单独存库，供「重刷提示词」反复复用
                base_pos = (exp.get("expanded_positive") or beat_text).strip() or beat_text
                base_neg = (exp.get("expanded_negative") or "").strip()

                # 人物一致性：文本命中的角色 ∪ 主角，注入**逐字相同**的角色锚点
                hits = characters.select_page_characters(beat_text, chars)
                pos, neg, seed = compose_page_texts(
                    base_pos, base_neg, shot, hits, setting,
                    _style, base_seed, project_id, chapter["id"], j + 1,
                )
                page = service.create_page(
                    chapter["id"],
                    {
                        "title": "%s · 第%d页" % (title, j + 1),
                        "order_idx": j + 1,
                        "prompt_text": pos,
                        "negative_text": neg,
                        "base_prompt": base_pos,
                        "base_negative": base_neg,
                        "shot_note": shot,
                        "beat_text": beat_text,
                        "workflow_filename": default_workflow,
                        "seed": seed,
                        "character_names": [c.get("name") for c in hits],
                    },
                )
                page_ids.append(page["id"])
                pages += 1
                try:
                    service.enqueue_page(page["id"])
                    enqueued += 1
                except AIBARError as exc:
                    all_warnings.append("分镜入队失败（%s 第%d页）：%s" % (title, j + 1, exc.message))
        created_chapters.append(
            {"chapter_id": chapter["id"], "title": title, "page_ids": page_ids}
        )

    safe_log(
        _LOGGER, 20, "storyboard_generated",
        project_id=project_id, episodes=len(episodes), pages=pages, enqueued=enqueued,
        characters=len(chars),
    )
    return {
        "project_id": project_id,
        "episodes": len(episodes),
        "chapters": len(created_chapters),
        "pages": pages,
        "enqueued": enqueued,
        "mode": _mode,
        "removed_chapters": removed_chapters,
        "pages_per_chapter": ppc,
        "style": _style,
        "style_label": styles.get_style(_style).get("label", ""),
        "characters": len(chars),
        "character_names": [c.get("name") for c in chars],
        "base_seed": base_seed,
        "provider": plan.get("provider"),
        "warnings": all_warnings,
        "chapters_detail": created_chapters,
    }


# ---------------------------------------------------------------- 提示词重算（改角色/换风格后一键刷新）


def refresh_project_prompts(
    project_id: int,
    style: str | None = None,
    requeue: str = "failed",
) -> dict:
    """按**当前**角色卡 / 世界观 / 出图风格，重算项目所有分镜页的提示词与种子。

    每一页保存了生成时的原始扩写结果（``base_prompt`` / ``base_negative``）与镜头景别，
    因此这里只需要重新做「注入角色锚点 → 合并角色排除项 → 套用风格 → 派生种子」这一步，
    **不重新拆分剧情、不重新扩写**，改完角色外貌后可以立刻让全本的提示词同步生效。

    Args:
        style: 覆盖出图风格；省略则沿用项目保存的 ``style_preset``。
        requeue: 重算后自动重新入队的页范围 —— ``failed``（只重跑失败的页，默认）、
            ``all``（全部重跑）、``none``（只改提示词，不出图）。

    Returns:
        统计信息：更新页数、变化页数、重新入队页数、使用的风格等。
    """
    project = service.get_project(project_id)

    _style = (
        styles.normalize_style(style)
        if (style is not None and styles.is_valid_style(style))
        else styles.normalize_style(project.get("style_preset"))
    )
    if style is not None and styles.is_valid_style(style) and _style != project.get("style_preset"):
        try:
            service.update_project(project_id, {"style_preset": _style})
        except Exception:
            pass

    # 角色卡：重新同步一次（只补缺失，不覆盖用户已编辑的卡片），再取全量
    try:
        service.sync_characters(project_id, project.get("worldview") or "", project.get("plot_summary") or "")
    except Exception:
        pass
    chars = service.list_characters(project_id)
    setting = characters.setting_prefix(project.get("worldview") or "")

    base_seed = project.get("base_seed")
    if base_seed is None:
        base_seed = characters.default_base_seed(project_id)
        try:
            service.update_project(project_id, {"base_seed": base_seed})
        except Exception:
            pass

    _requeue = str(requeue or "failed").strip().lower()
    if _requeue not in ("failed", "all", "none"):
        raise AIBARError("invalid_input", "重新出图范围只能是 failed / all / none")

    updated = 0
    changed = 0
    enqueued = 0
    warnings: list[str] = []

    for chapter in service.list_chapters(project_id):
        for page in service.list_pages(chapter["id"]):
            # 没有 base_prompt 的页（历史数据 / 手工新建）用当前 prompt_text 兜底，
            # 这样重刷不会把已有内容清空，只是重新套一遍锚点与风格。
            base_pos = (page.get("base_prompt") or "").strip() or (page.get("prompt_text") or "").strip()
            base_neg = (page.get("base_negative") or "").strip() or (page.get("negative_text") or "").strip()
            shot = (page.get("shot_note") or "").strip()
            page_idx = int(page.get("order_idx") or 0) or 1

            hits = characters.select_page_characters(base_pos, chars)
            pos, neg, seed = compose_page_texts(
                base_pos, base_neg, shot, hits, setting,
                _style, base_seed, project_id, chapter["id"], page_idx,
            )

            same_prompt = pos == (page.get("prompt_text") or "")
            same_seed = seed == page.get("seed")
            if not (same_prompt and same_seed):
                changed += 1
            # 始终写回：即使内容没变，也统一 updated_at 与 character_names，便于追溯。
            # ``character_names`` 记录的是**真正注入锚点**的角色（不是文本里出现过的全部），
            # 否则噪声卡（无身份的角色）会被记录进引用链，cleanup 脚本无法识别为孤儿。
            picked = characters._pick_anchors(hits, base_pos)
            try:
                service.update_page(
                    page["id"],
                    {
                        "prompt_text": pos,
                        "negative_text": neg,
                        "base_prompt": base_pos,
                        "base_negative": base_neg,
                        "shot_note": shot,
                        "seed": seed,
                        "character_names": [c.get("name") for c in picked],
                    },
                )
                updated += 1
            except AIBARError as exc:
                warnings.append("第%d页提示词更新失败：%s" % (page_idx, exc.message))
                continue

            need_requeue = (
                _requeue == "all"
                or (_requeue == "failed" and (page.get("status") or "") == "failed")
            )
            if need_requeue:
                try:
                    service.enqueue_page(page["id"])
                    enqueued += 1
                except AIBARError as exc:
                    warnings.append("第%d页重新入队失败：%s" % (page_idx, exc.message))

    safe_log(
        _LOGGER, 20, "storyboard_prompts_refreshed",
        project_id=project_id, updated=updated, changed=changed, enqueued=enqueued,
        characters=len(chars), style=_style,
    )
    return {
        "project_id": project_id,
        "updated": updated,
        "changed": changed,
        "enqueued": enqueued,
        "style": _style,
        "style_label": styles.get_style(_style).get("label", ""),
        "characters": len(chars),
        "character_names": [c.get("name") for c in chars],
        "requeue": _requeue,
        "warnings": warnings,
    }


def reexpand_page(
    page_id: int,
    provider: str | None = None,
    profile: str | None = None,
    intensity: str | None = None,
    requeue: bool = False,
) -> dict:
    """对**单页**重跑一次提示词：可选重新扩写，再按当前角色卡与风格重算。

    与 ``refresh_project_prompts`` 的区别：

    - 后者是「全本重算」，且**不重新扩写**（只重套角色锚点与风格），改角色外貌时用；
    - 本函数面向「这一页的扩写结果不满意」，重新过一次扩写模型再组装，
      省掉「为了改一页而重跑全部分镜」的代价。

    Args:
        page_id: 分镜页 id。
        provider: 扩写 provider；省略走默认（自动 AI→规则降级）。
        profile / intensity: 扩写档案与强度；省略走引擎默认值。
        requeue: 重算后是否立刻重新入队出图。

    Returns:
        该页的新旧提示词对照与使用的扩写方式，便于前端直接展示差异。
    """
    page = service.get_page(page_id)
    project = service.get_project(page["project_id"])

    _profile = profile or _engine.DEFAULT_PROFILE
    _intensity = intensity or _engine.DEFAULT_INTENSITY
    if _intensity not in _engine.INTENSITIES:
        _intensity = _engine.DEFAULT_INTENSITY
    _style = styles.normalize_style(project.get("style_preset"))

    beat_text = (page.get("beat_text") or "").strip()
    # 历史页没有 beat_text：退回拿 base_prompt 当原文扩写，效果打折但不会失败
    source = beat_text or (page.get("base_prompt") or "").strip() or (page.get("prompt_text") or "").strip()
    if not source:
        raise AIBARError("invalid_input", "该分镜没有任何剧情文本，无法重扩写")

    warnings: list[str] = []
    if beat_text:
        exp = _safe_expand(beat_text, _profile, _intensity, provider or "")
        for w in exp.get("warnings", []) or []:
            if w not in warnings:
                warnings.append(w)
        base_pos = (exp.get("expanded_positive") or beat_text).strip() or beat_text
        base_neg = (exp.get("expanded_negative") or "").strip()
        used = exp.get("provider") or "rules"
    else:
        # 没有剧情原文时不臆造扩写结果：只保留原文，仍走一遍组装（角色锚点/风格会刷新）
        base_pos = (page.get("base_prompt") or "").strip() or source
        base_neg = (page.get("base_negative") or "").strip()
        used = "skip"
        warnings.append("该分镜缺少剧情原文，已跳过重新扩写，仅按当前角色卡与风格重算")

    try:
        service.sync_characters(
            project["id"], project.get("worldview") or "", project.get("plot_summary") or ""
        )
    except Exception:
        pass
    chars = service.list_characters(project["id"])
    setting = characters.setting_prefix(project.get("worldview") or "")

    base_seed = project.get("base_seed")
    if base_seed is None:
        base_seed = characters.default_base_seed(project["id"])

    shot = (page.get("shot_note") or "").strip()
    page_idx = int(page.get("order_idx") or 0) or 1
    hits = characters.select_page_characters(beat_text or base_pos, chars)
    pos, neg, seed = compose_page_texts(
        base_pos, base_neg, shot, hits, setting,
        _style, base_seed, project["id"], page["chapter_id"], page_idx,
    )

    old_pos = page.get("prompt_text") or ""
    updated = service.update_page(
        page_id,
        {
            "prompt_text": pos,
            "negative_text": neg,
            "base_prompt": base_pos,
            "base_negative": base_neg,
            "shot_note": shot,
            "beat_text": beat_text,
            "seed": seed,
            "character_names": [c.get("name") for c in hits],
        },
    )

    job_id = None
    if requeue:
        try:
            job_id = service.enqueue_page(page_id).get("job_id")
        except AIBARError as exc:
            warnings.append("重新入队失败：%s" % exc.message)

    safe_log(
        _LOGGER, 20, "storyboard_page_reexpanded",
        page_id=page_id, project_id=project["id"], reexpand=bool(beat_text), provider=used,
    )
    return {
        "page_id": page_id,
        "previous_prompt": old_pos,
        "prompt_text": pos,
        "negative_text": neg,
        "base_prompt": base_pos,
        "changed": pos != old_pos,
        "reexpanded": bool(beat_text),
        "provider": used,
        "style": _style,
        "character_names": [c.get("name") for c in hits],
        "job_id": job_id,
        "warnings": warnings,
        "updated": updated,
    }


__all__ = [
    "plan_episodes",
    "generate_storyboard",
    "refresh_project_prompts",
    "reexpand_page",
    "compose_page_texts",
    "split_scene_into_beats",
    "MAX_EPISODES",
]
