"""M6 提示词知识库：加载、校验与查询 ``resources/prompt_library.json``。

设计约束（PRD M6.2 / M6.7）：
- 知识库是**版本化产品配置**，放在 ``resources/`` 不入 ``data/``，以 ``schema_version`` 标识；
- 词条按维度组织，模型档案不支持的词条要能被标记出来；
- 加载失败必须给出可操作错误，且不影响其他模块（异常限制在 M6 内部传播）。

所有查询都是**确定性**的：返回顺序只取决于资源文件顺序与固定的维度顺序，
不使用集合迭代、不使用随机、不依赖字典遍历顺序。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from config import Config
from core.errors import AIBARError
from core.textutil import normalize_text

LIBRARY_FILENAME = "prompt_library.json"
DEFAULT_PROFILE = "generic"
FALLBACK_DIMENSION = "subject"

# 资源文件整体缓存：知识库是只读配置，进程内只解析一次
_CACHE: dict | None = None


# ---------------------------------------------------------------- 词条模型


@dataclass(frozen=True)
class Term:
    """一条标准词条。

    Attributes:
        id: 稳定唯一标识，用于升级合并与外部引用。
        name: 中文名称，用于界面展示。
        text: 默认英文插入文本（SD 系标签/短语风格）。
        text_zh: 中文插入文本（通用档案下的自然语言描述）。
        dimension: 所属维度 key。
        profiles: 适用模型档案列表。
        note: 简短说明，解释"为什么用"。
        example: 使用示例。
        tags: 检索与相关性打分用的标签。
        variants: 按模型档案定制的插入文本，形如 ``{profile: {text, text_zh}}``。
    """

    id: str
    name: str
    text: str
    text_zh: str
    dimension: str
    profiles: tuple[str, ...]
    note: str
    example: str
    tags: tuple[str, ...]
    variants: dict = field(default_factory=dict)

    def supports(self, profile_key: str) -> bool:
        return profile_key in self.profiles

    def insert_text(self, profile_key: str) -> str:
        """按模型档案取插入文本。

        - ``generic``：输出中文自然语言（FLUX 与通用档案都不使用权重语法）；
        - ``sd15_sdxl``：输出英文标签/短语，未定制时回退 ``text``；
        - ``flux_flux2``：输出自然语言短句，未定制时回退 ``text``。
        """
        variant = self.variants.get(profile_key) or {}
        if profile_key == DEFAULT_PROFILE:
            return str(variant.get("text_zh") or self.text_zh or self.text)
        return str(variant.get("text") or self.text)

    def insert_text_zh(self, profile_key: str) -> str:
        variant = self.variants.get(profile_key) or {}
        return str(variant.get("text_zh") or self.text_zh)

    def to_dict(self, profile_key: str | None = None) -> dict:
        payload = {
            "id": self.id,
            "name": self.name,
            "text": self.text,
            "text_zh": self.text_zh,
            "dimension": self.dimension,
            "dimension_label": dimension_label(self.dimension),
            "profiles": list(self.profiles),
            "note": self.note,
            "example": self.example,
            "tags": list(self.tags),
        }
        if profile_key:
            payload["insert_text"] = self.insert_text(profile_key)
            payload["applicable"] = self.supports(profile_key)
        return payload


# ---------------------------------------------------------------- 加载


def _raw(refresh: bool = False) -> dict:
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    path = Path(Config.RESOURCES_DIR) / LIBRARY_FILENAME
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError:
        raise AIBARError(
            "library_unavailable",
            f"提示词知识库不可用：无法读取 resources/{LIBRARY_FILENAME}",
        )
    except ValueError:
        raise AIBARError(
            "library_unavailable",
            f"提示词知识库格式错误：resources/{LIBRARY_FILENAME} 不是合法 JSON",
        )
    if not isinstance(data, dict) or "schema_version" not in data:
        raise AIBARError("library_unavailable", "提示词知识库缺少 schema_version 字段")
    _CACHE = data
    return _CACHE


def load(refresh: bool = False) -> dict:
    """返回原始知识库字典（只读用途）。加载失败抛 ``AIBARError``。"""
    return _raw(refresh)


def refresh() -> None:
    """丢弃缓存，下一次访问重新读盘（供测试与运维使用）。"""
    global _CACHE
    _CACHE = None


def schema_version() -> int:
    try:
        return int(_raw().get("schema_version", 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 模型档案


def profiles() -> list[dict]:
    """三个模型档案（generic / sd15_sdxl / flux_flux2）的完整定义。"""
    return [dict(item) for item in _raw().get("profiles", [])]


def profile_keys() -> list[str]:
    return [str(item.get("key", "")) for item in profiles() if item.get("key")]


def profile(key: str) -> dict:
    for item in profiles():
        if item.get("key") == key:
            return dict(item)
    raise AIBARError("invalid_input", f"未知的模型档案：{key}")


# ---------------------------------------------------------------- 维度


def dimensions() -> list[dict]:
    """按 ``order`` 升序返回 13 个内容维度。"""
    items = [dict(item) for item in _raw().get("dimensions", [])]
    items.sort(key=lambda item: (int(item.get("order", 99)), str(item.get("key", ""))))
    return items


def dimension_keys() -> list[str]:
    return [str(item.get("key", "")) for item in dimensions() if item.get("key")]


def dimension_label(key: str) -> str:
    for item in dimensions():
        if item.get("key") == key:
            return str(item.get("label") or key)
    return key


def dimension_order(key: str) -> int:
    for index, item in enumerate(dimensions()):
        if item.get("key") == key:
            return index
    return len(dimensions())


# ---------------------------------------------------------------- 词条


def _to_term(payload: dict) -> Term:
    return Term(
        id=str(payload.get("id", "")),
        name=str(payload.get("name", "")),
        text=str(payload.get("text", "")),
        text_zh=str(payload.get("text_zh", "")),
        dimension=str(payload.get("dimension", FALLBACK_DIMENSION)),
        profiles=tuple(str(item) for item in payload.get("profiles", [])),
        note=str(payload.get("note", "")),
        example=str(payload.get("example", "")),
        tags=tuple(str(item) for item in payload.get("tags", [])),
        variants=dict(payload.get("variants") or {}),
    )


def terms() -> list[Term]:
    """全部词条，顺序与资源文件一致（确定性）。"""
    return [_to_term(item) for item in _raw().get("terms", []) if isinstance(item, dict)]


def term(term_id: str) -> Term | None:
    for item in terms():
        if item.id == term_id:
            return item
    return None


def _matches(term_item: Term, keyword: str) -> bool:
    if not keyword:
        return True
    haystack = " ".join(
        [term_item.name, term_item.text, term_item.text_zh, term_item.note, " ".join(term_item.tags)]
    )
    return keyword in normalize_text(haystack)


def query_terms(profile: str | None = None, dimension: str | None = None, q: str = "") -> list[Term]:
    """按维度与关键词过滤词条。

    Args:
        profile: 传入时校验档案合法性；不支持当前档案的词条仍会返回，
            由调用方通过 ``Term.supports`` 标记 ``applicable:false``。
        dimension: 维度 key，未知维度抛 ``invalid_input``。
        q: 关键词，覆盖中文名称、中英文插入文本、说明与标签。
    """
    if profile and profile not in profile_keys():
        raise AIBARError("invalid_input", f"未知的模型档案：{profile}")
    if dimension and dimension not in dimension_keys():
        raise AIBARError("invalid_input", f"未知的内容维度：{dimension}")
    keyword = normalize_text(q or "")
    result = [
        item
        for item in terms()
        if (not dimension or item.dimension == dimension) and _matches(item, keyword)
    ]
    result.sort(key=lambda item: (dimension_order(item.dimension), item.id))
    return result


# ---------------------------------------------------------------- 模板与约束


def templates() -> list[dict]:
    return [dict(item) for item in _raw().get("templates", [])]


def template(template_id: str) -> dict | None:
    for item in templates():
        if item.get("id") == template_id:
            return dict(item)
    return None


def template_ids() -> list[str]:
    return [str(item.get("id", "")) for item in templates() if item.get("id")]


def conflicts() -> list[dict]:
    """互斥词表：同一组内只允许出现一个成员。"""
    return [dict(item) for item in _raw().get("conflicts", [])]


def guards() -> dict:
    """维度补充约束，例如文字排版需要用户明示意图。"""
    return dict(_raw().get("dimension_guards", {}))


def stats() -> dict:
    """知识库规模统计，供测试与页面展示使用。"""
    items = terms()
    per_dimension: dict[str, int] = {}
    for item in items:
        per_dimension[item.dimension] = per_dimension.get(item.dimension, 0) + 1
    return {
        "schema_version": schema_version(),
        "profiles": len(profiles()),
        "dimensions": len(dimensions()),
        "terms": len(items),
        "terms_by_dimension": per_dimension,
        "templates": len(templates()),
        "conflicts": len(conflicts()),
    }


__all__ = [
    "LIBRARY_FILENAME",
    "DEFAULT_PROFILE",
    "Term",
    "load",
    "refresh",
    "schema_version",
    "profiles",
    "profile_keys",
    "profile",
    "dimensions",
    "dimension_keys",
    "dimension_label",
    "dimension_order",
    "terms",
    "term",
    "query_terms",
    "templates",
    "template",
    "template_ids",
    "conflicts",
    "guards",
    "stats",
]
