"""M12 · 把 Skill（SKILL.md）的流程转换为漫画工作室工作流。

动机：用户希望把任意 skill 的「流程」沉淀为漫画工作室可驱动的大工作流——
即 ``project → chapter → page`` 三层，每页带预制提示词 + 出图工作流，可经 ComfyUI 出图队列生成。

映射规则（已与用户确认，作为可复用约定）：
- **项目（project）** ← SKILL.md 的 frontmatter ``name``（可用导入时的 ``title`` 覆盖）+ ``description``。
- **章节（chapter）** ← SKILL.md 的 ``## 段落``；被忽略的段落（前置条件 / When to use / Pitfalls / Verification / Files）不建章节，避免噪声。
- **分镜页（page）** ← 段落内的编号步骤（``1. ...`` ``2. ...``）；每段一步 → 一页。
  页的 ``prompt_text`` = 该步骤原文（即"预制提示词"，用户出图前可编辑成真正的图像提示词）。
  若段落无编号步骤，则整段作为单页的 ``prompt_text``。
- **出图工作流** ← 导入时指定的 ``workflow_filename``；若未指定且本机 ComfyUI 工作流目录可用，
  自动写入一份 FLUX.2 Klein 4B 最小化 13 节点工作流（来自 ``comfyui-flux-storyboard`` skill 的已知图），
  并同时设为项目 ``default_workflow`` 与各页 ``workflow_filename``，使导入即得可出图能力。

注意：本模块不依赖 ComfyUI 在线；写入工作流文件只是把 JSON 落盘，真正出图由 M12 队列 worker 在 ComfyUI 可达时执行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.errors import AIBARError
from core.logging_setup import get_logger, safe_log
from sync import paths as _sync_paths

from . import service

_LOGGER = get_logger("aibar.comic.skill_import")

# 导入时跳过这些段落（纯说明性，不转为工作流节点）
_SKIP_SECTIONS = {
    "when to use",
    "when to use / 触发条件",
    "前置条件",
    "前置条件 / prerequisites",
    "prerequisites",
    "pitfalls",
    "常见坑",
    "verification",
    "验证",
    "files",
    "文件",
}

# 这些关键词作为段落标题「前缀」时也应跳过（兼容「Pitfalls（全部踩过，务必遵守）」这类带括号的标题）
_SKIP_PREFIXES = (
    "when to use",
    "前置条件",
    "prerequisites",
    "pitfalls",
    "常见坑",
    "verification",
    "验证",
    "files",
    "文件",
)


def _norm_title(title: str) -> str:
    """去掉括号内容（中英文括号）并小写，避免「Pitfalls（...）」匹配不到。"""
    t = re.sub(r"[（(].*?[）)]", "", title)
    return t.strip().lower()


def _should_skip(title: str) -> bool:
    norm = _norm_title(title)
    if norm in _SKIP_SECTIONS:
        return True
    for p in _SKIP_PREFIXES:
        if norm.startswith(p):
            return True
    return False

# 自动写入的 FLUX.2 最小化工作流文件名（API 格式，convert_text 原生支持）
FLUX_WORKFLOW_FILENAME = "flux2_storyboard_aibar.json"

# FLUX.2 Klein 4B 最小化 13 节点 API 工作流（复刻 comfyui-flux-storyboard skill 的 build_minimal_flux_workflow）
# 关键点：CLIPLoader(type=flux2)、Flux2Scheduler 显式 width/height、CFGGuider 键 positive/negative、RandomNoise 键 noise_seed。
_FLUX2_WORKFLOW: dict[str, Any] = {
    "2": {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "flux-2-klein-4b.safetensors", "weight_dtype": "default"},
    },
    "3": {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"},
    },
    "4": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 0], "text": ""}},
    "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
    "7": {
        "class_type": "EmptyFlux2LatentImage",
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
    },
    "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
    "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
    "10": {
        "class_type": "Flux2Scheduler",
        "inputs": {"steps": 4, "width": 1024, "height": 1024},
    },
    "11": {
        "class_type": "CFGGuider",
        "inputs": {
            "model": ["2", 0],
            "positive": ["5", 0],
            "negative": ["6", 0],
            "cfg": 1.0,
        },
    },
    "12": {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["8", 0],
            "guider": ["11", 0],
            "sampler": ["9", 0],
            "sigmas": ["10", 0],
            "latent_image": ["7", 0],
        },
    },
    "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["4", 0]}},
    "14": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "ComfyUI", "images": ["13", 0]},
    },
}


# ----------------------------------------------------------------- 解析


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def _split_sections(body: str) -> list[dict[str, str]]:
    chunks = re.split(r"(?m)^##\s+", body)
    sections: list[dict[str, str]] = []
    for chunk in chunks[1:]:
        lines = chunk.splitlines()
        title = lines[0].strip()
        sec_body = "\n".join(lines[1:]).strip()
        if title:
            sections.append({"title": title, "body": sec_body})
    return sections


def _split_steps(sec_body: str) -> list[dict[str, Any]]:
    raw = re.split(r"(?m)^\s*(\d+)\.\s+", sec_body)
    steps: list[dict[str, Any]] = []
    for i in range(1, len(raw), 2):
        num = raw[i]
        txt = raw[i + 1].strip() if i + 1 < len(raw) else ""
        if txt:
            steps.append({"index": int(num), "text": txt})
    return steps


def parse_skill_md(text: str) -> dict[str, Any]:
    """解析 SKILL.md 为结构化字典。

    Returns:
        ``{"name", "description", "intro", "sections":[{title, body, steps}]}``
    """
    fm, body = _parse_frontmatter(text)
    sections_raw = _split_sections(body)
    sections: list[dict[str, Any]] = []
    for s in sections_raw:
        steps = _split_steps(s["body"])
        sections.append({"title": s["title"], "body": s["body"], "steps": steps})
    intro = ""
    if sections_raw:
        # 第一段前的正文作为 intro（很少见）
        pass
    return {
        "name": fm.get("name") or "",
        "description": fm.get("description") or fm.get("description_zh") or "",
        "intro": intro,
        "sections": sections,
    }


# ----------------------------------------------------------------- 工作流 JSON 落盘


def ensure_flux_workflow() -> str | None:
    """把 FLUX.2 最小化工作流写入 ComfyUI 工作流目录（若可用）。

    Returns:
        工作流文件名（如 ``flux2_storyboard_aibar.json``）；目录不可用时返回 None。
    """
    base = _sync_paths.workflows_dir()
    if base is None:
        safe_log(_LOGGER, 30, "comic_flux_wf_skip", reason="workflows_dir_unavailable")
        return None
    target = base / FLUX_WORKFLOW_FILENAME
    try:
        target.write_text(json.dumps(_FLUX2_WORKFLOW, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        safe_log(_LOGGER, 30, "comic_flux_wf_write_failed", error_type=type(exc).__name__)
        return None
    safe_log(_LOGGER, 20, "comic_flux_wf_written", path=str(target))
    return FLUX_WORKFLOW_FILENAME


# ----------------------------------------------------------------- 导入为漫画工作流


def _chapter_payloads(skill: dict[str, Any], workflow_filename: str) -> list[dict[str, Any]]:
    """把 skill 段落转换为章节 + 分镜页载荷。"""
    chapters: list[dict[str, Any]] = []
    for sec in skill["sections"]:
        title = sec["title"]
        if _should_skip(title):
            continue
        steps = sec.get("steps") or []
        if steps:
            pages = [
                {
                    "title": f"步骤 {st['index']}：{_first_line(st['text'])}",
                    "prompt_text": st["text"],
                    "workflow_filename": workflow_filename,
                }
                for st in steps
            ]
        else:
            pages = [
                {
                    "title": title,
                    "prompt_text": sec["body"],
                    "workflow_filename": workflow_filename,
                }
            ]
        chapters.append({"title": title, "pages": pages})
    return chapters


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    # 去掉 markdown 加粗标记，保留可读标题
    line = re.sub(r"^\s*\*\*|\*\*\s*$", "", line).strip()
    return line[:40]


def import_skill(
    text: str,
    title: str | None = None,
    workflow_filename: str | None = None,
) -> dict[str, Any]:
    """把 SKILL.md 文本导入为漫画工作室工作流。

    Args:
        text: SKILL.md 全文。
        title: 覆盖项目名（默认用 frontmatter ``name``）。
        workflow_filename: 指定出图工作流文件名；省略时尝试自动写入 FLUX.2 工作流。

    Returns:
        ``{"project_id", "project_name", "workflow_filename", "chapters", "pages"}``
    """
    skill = parse_skill_md(text)
    name = (title or skill.get("name") or "未命名 Skill 工作流").strip()
    if not name:
        raise AIBARError("invalid_input", "无法从 skill 解析出名称，请通过 title 指定")
    description = skill.get("description") or f"由 Skill 导入：{name}"

    wf = workflow_filename
    if not wf:
        wf = ensure_flux_workflow()

    project = service.create_project(
        {
            "name": name,
            "description": description,
            "status": "draft",
            "default_workflow": wf or "",
        }
    )
    project_id = project["id"]

    chapter_payloads = _chapter_payloads(skill, wf or "")
    created_chapters = 0
    created_pages = 0
    for ch in chapter_payloads:
        chapter = service.create_chapter(project_id, {"title": ch["title"], "status": "draft"})
        created_chapters += 1
        for pg in ch["pages"]:
            service.create_page(chapter["id"], pg)
            created_pages += 1

    safe_log(
        _LOGGER,
        20,
        "comic_skill_imported",
        project_id=project_id,
        name=name,
        chapters=created_chapters,
        pages=created_pages,
    )
    return {
        "project_id": project_id,
        "project_name": name,
        "workflow_filename": wf or "",
        "chapters": created_chapters,
        "pages": created_pages,
    }


__all__ = [
    "FLUX_WORKFLOW_FILENAME",
    "parse_skill_md",
    "ensure_flux_workflow",
    "import_skill",
]
