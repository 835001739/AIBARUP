"""ComfyUI 源目录探测（M1/M2/M3 与 M8 模型页共用）。

优先级：``Config`` 显式配置 > ``COMFYUI_DIR`` 下的标准子目录 > 常见安装位置自动探测（找到即止）。

设计要点：
- 只返回**确实存在**的目录，配置的目录不存在时自动回退到下一级候选，避免"配错即全站空"；
- 所有探测都包在 try/except 里，磁盘权限异常只降级为 None，不影响主流程；
- 便携版 ComfyUI（``ComfyUI_windows_portable`` 等）把真正仓库放在子目录 ``ComfyUI/`` 下，
  因此每个候选根同时检查 ``root/<sub>`` 与 ``root/ComfyUI/<sub>``。
"""

from __future__ import annotations

import glob
from pathlib import Path, PurePosixPath

from config import Config

# ComfyUI 默认目录布局
WORKFLOWS_SUBDIR = "user/default/workflows"
OUTPUT_SUBDIR = "output"
MODELS_SUBDIR = "models"
# 输入目录：``LoadImage`` 节点的文件下拉框就读这里。姿势库（ControlNet 骨架图）
# 必须落到这里，工作流里的 LoadImage 才能选到。
INPUT_SUBDIR = "input"

# 显式配置与子目录的对应关系（配置缺失时自动走探测）
_EXPLICIT_KEYS: dict[str, str] = {
    WORKFLOWS_SUBDIR: "COMFYUI_WORKFLOWS_DIR",
    OUTPUT_SUBDIR: "COMFYUI_OUTPUT_DIR",
    MODELS_SUBDIR: "COMFYUI_MODELS_DIR",
    INPUT_SUBDIR: "COMFYUI_INPUT_DIR",
}

# 便携版发行版常用的内层仓库目录名
_PORTABLE_INNER = "ComfyUI"


def _home() -> Path:
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return Path(".")


def _candidate_roots() -> list[Path]:
    """常见安装位置：显式 COMFYUI_DIR 优先，其后按命中概率排序。"""
    roots: list[Path] = []
    if Config.COMFYUI_DIR:
        roots.append(Path(Config.COMFYUI_DIR).expanduser())
    home = _home()
    roots.extend(
        [
            home / "ComfyUI",
            home / "Documents" / "ComfyUI",
            home / "AI" / "ComfyUI",
            Path("/opt/ComfyUI"),
        ]
    )
    # ~/ComfyUI-* 变体（windows_portable、ComfyUI-aki 等），保证顺序稳定便于排障
    try:
        for match in sorted(glob.glob(str(home / "ComfyUI-*"))):
            roots.append(Path(match))
    except (OSError, ValueError):
        pass
    return roots


def _candidates(subdir: str) -> list[Path]:
    """按优先级展开候选目录列表。"""
    out: list[Path] = []
    key = _EXPLICIT_KEYS.get(subdir)
    explicit = getattr(Config, key, "") if key else ""
    if explicit:
        out.append(Path(explicit).expanduser())
    for root in _candidate_roots():
        out.append(root / subdir)
        out.append(root / _PORTABLE_INNER / subdir)
    return out


def resolve_dir(subdir: str) -> Path | None:
    """返回第一个存在的候选目录；全部不存在时返回 None。"""
    seen: set[str] = set()
    for candidate in _candidates(subdir):
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            # 权限/挂载异常只跳过该候选，不中断探测
            continue
    return None


def safe_join(base: Path | None, filename: str) -> Path | None:
    """把用户传入的相对路径限制在 ``base`` 目录内，防止路径穿越。

    Returns:
        解析后的绝对路径；入参含 ``..``、绝对路径、盘符、或逃出 ``base`` 时返回 None。

    为什么必须有这么一个函数：曾经有人（就是我）图省事写了
    ``str(target).startswith(str(base))``，看着像那么回事，其实
    ``base = /a/workflows`` 时 ``../workflows-evil/secret.json`` 会解析成
    ``/a/workflows-evil/secret.json``，前缀判断照样通过——兄弟目录就这么被读穿了。
    ``is_relative_to`` 才是正确的边界判断。所有接受文件名的地方都必须走这里。
    """
    if base is None or not filename or not isinstance(filename, str):
        return None
    if filename.startswith(("/", "\\")) or ":" in filename:
        return None
    parts = PurePosixPath(filename.replace("\\", "/")).parts
    if not parts or any(part == ".." for part in parts):
        return None
    try:
        base_resolved = base.resolve()
        target = (base_resolved.joinpath(*parts)).resolve()
    except (OSError, ValueError):
        return None
    try:
        if not target.is_relative_to(base_resolved):
            return None
    except (OSError, ValueError):
        return None
    return target


def workflows_dir() -> Path | None:
    """ComfyUI 保存工作流 JSON 的目录。"""
    return resolve_dir(WORKFLOWS_SUBDIR)


def output_dir() -> Path | None:
    """ComfyUI 产出目录（M3 图库数据源）。"""
    return resolve_dir(OUTPUT_SUBDIR)


def models_dir() -> Path | None:
    """ComfyUI 模型根目录（M8 模型页数据源）。"""
    return resolve_dir(MODELS_SUBDIR)


def input_dir() -> Path | None:
    """ComfyUI 输入目录（``LoadImage`` 节点的文件来源）。

    与 ``workflows_dir`` 等不同，这个目录**可能不存在**（ComfyUI 默认安装会建，
    但精简安装 / 手动部署可能没有）。姿势库需要在里面写骨架图，因此写之前
    必须先确保目录存在；这里只负责探测。
    """
    return resolve_dir(INPUT_SUBDIR)


__all__ = [
    "WORKFLOWS_SUBDIR",
    "OUTPUT_SUBDIR",
    "MODELS_SUBDIR",
    "INPUT_SUBDIR",
    "resolve_dir",
    "safe_join",
    "workflows_dir",
    "output_dir",
    "models_dir",
    "input_dir",
]
