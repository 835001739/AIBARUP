"""M12 · 分镜工作流「出图风格」预设（保证整部漫画每一集、每一页风格统一）。

设计约束（与 HARNESS / PRD 一致）：
- **纯数据 + 纯函数**：不依赖数据库、不依赖网络，可离线、可单测；
- **绝不抛异常**：``normalize_style`` 把任何非法输入收敛到合法预设，未知风格回退 ``DEFAULT_STYLE``；
- **幂等**：``apply_style`` 对同一段提示词重复套用不会无限叠加风格词；
- **风格词用英文**：图像模型（FLUX / SD 系）对英文风格标签响应最稳定；中文只用于界面展示 ``label`` / ``desc``。

用法（storyboard 生成每一页提示词时统一调用）：::

    from . import styles
    pos, neg = styles.apply_style(expanded_positive, expanded_negative, style_key)
"""

from __future__ import annotations

from typing import Any

# 未选择风格时的默认预设：不追加任何统一风格词
DEFAULT_STYLE = "none"

# 风格预设表：key -> {key, label, desc, positive, negative}
# - positive：追加到「正面提示词」末尾的统一风格描述；
# - negative：合并进「负面提示词」的统一排除项（避免风格被其它元素污染）。
STYLE_PRESETS: dict[str, dict[str, str]] = {
    "none": {
        "key": "none",
        "label": "不限制（按剧情自由发挥）",
        "desc": "不追加统一风格词，画面完全由剧情提示词决定。",
        "positive": "",
        "negative": "",
    },
    "japanese_manga": {
        "key": "japanese_manga",
        "label": "日式黑白漫画",
        "desc": "黑白线稿 + 网点纸 + 速度线，高对比，适合长篇连载。",
        "positive": (
            "japanese manga style, black and white lineart, screentone shading, "
            "bold ink outlines, high contrast monochrome, dynamic speed lines, "
            "clean manga panel composition, expressive facial expressions"
        ),
        "negative": "color, full color, photograph, 3d render, watercolor, oil painting, blurry, extra fingers, deformed hands",
    },
    "shinkai": {
        "key": "shinkai",
        "label": "新海诚动画电影风",
        "desc": "通透天空、逆光光晕、细腻云层与强烈氛围光，色彩饱和。",
        "positive": (
            "makoto shinkai style anime film background, luminous sky, dramatic volumetric clouds, "
            "warm backlighting, lens flare, vibrant saturated colors, delicate light rays, "
            "cinematic composition, highly detailed atmospheric scenery"
        ),
        "negative": "dark muddy colors, low contrast, sketch, lineart, monochrome, photograph, blurry, extra fingers, deformed hands",
    },
    "american_comic": {
        "key": "american_comic",
        "label": "美式漫画（厚描边平涂）",
        "desc": "硬朗轮廓、厚重描边、平涂色块，力量感强，适合超级英雄题材。",
        "positive": (
            "american comic book style, heavy bold ink outlines, flat cel shading, "
            "strong muscular anatomy, dramatic low angle, punchy saturated palette, "
            "halftone dots, dynamic action pose"
        ),
        "negative": "watercolor, soft pastel, sketch, photograph, 3d render, blurry, extra fingers, deformed hands",
    },
    "ink_wash": {
        "key": "ink_wash",
        "label": "国风水墨",
        "desc": "水墨晕染、留白意境、写意线条，适合仙侠 / 国风题材。",
        "positive": (
            "traditional chinese ink wash painting, sumi-e, expressive brush strokes, "
            "ink bleeding on rice paper, elegant negative space, monochrome with subtle ink gradients, "
            "mist and mountains atmosphere"
        ),
        "negative": "neon, cyberpunk, photograph, 3d render, heavy digital painting, blurry, extra fingers, deformed hands",
    },
    "watercolor": {
        "key": "watercolor",
        "label": "水彩绘本",
        "desc": "柔和渐变、纸张纹理、轻盈通透，适合温情 / 儿童绘本。",
        "positive": (
            "watercolor illustration, soft color bleeding, visible paper texture, "
            "gentle pastel palette, airy light washes, hand painted children book style, warm cozy mood"
        ),
        "negative": "heavy ink outlines, dark gritty, neon, photograph, 3d render, blurry, extra fingers, deformed hands",
    },
    "cyberpunk": {
        "key": "cyberpunk",
        "label": "赛博朋克",
        "desc": "霓虹灯光、雨后湿地反射、机械义体与高楼峡谷。",
        "positive": (
            "cyberpunk style, neon lights, rain soaked streets with reflections, "
            "dense futuristic cityscape, volumetric fog, teal and magenta color grading, "
            "high tech low life atmosphere, cinematic wide shot"
        ),
        "negative": "pastoral, watercolor, ink wash, bright daylight, sketch, blurry, extra fingers, deformed hands",
    },
    "pixel_art": {
        "key": "pixel_art",
        "label": "像素游戏风",
        "desc": "低分辨率像素块、有限调色板、锯齿边缘，复古 JRPG 质感。",
        "positive": (
            "pixel art style, 16 bit retro game graphics, limited color palette, "
            "visible square pixels, crisp aliased edges, dithering shading, top down adventure game scene"
        ),
        "negative": "photorealistic, smooth gradients, watercolor, 3d render, blurry, extra fingers, deformed hands",
    },
    "realistic": {
        "key": "realistic",
        "label": "写实厚涂插画",
        "desc": "厚重笔触、真实光影与材质，接近电影概念设定图。",
        "positive": (
            "semi realistic painted illustration, thick painterly brush strokes, "
            "detailed textures, cinematic lighting, accurate anatomy, atmospheric depth, "
            "movie concept art quality"
        ),
        "negative": "flat cel shading, lineart, chibi, pixel art, sketch, blurry, extra fingers, deformed hands",
    },
}

# 下拉展示顺序（DEFAULT_STYLE 必须排在首位）
STYLE_ORDER: list[str] = [
    "none",
    "japanese_manga",
    "shinkai",
    "american_comic",
    "ink_wash",
    "watercolor",
    "cyberpunk",
    "pixel_art",
    "realistic",
]


def normalize_style(value: Any, fallback: str = DEFAULT_STYLE) -> str:
    """把任意输入收敛为合法风格 key；缺失 / 非法一律回退 ``fallback``（再非法则 ``DEFAULT_STYLE``）。"""
    if fallback not in STYLE_PRESETS:
        fallback = DEFAULT_STYLE
    if value is None:
        return fallback
    key = str(value).strip().lower()
    if key in STYLE_PRESETS:
        return key
    return fallback


def is_valid_style(value: Any) -> bool:
    """判断取值是否为已注册的风格 key（大小写 / 首尾空格容错）。"""
    if value is None:
        return False
    return str(value).strip().lower() in STYLE_PRESETS


def get_style(key: Any) -> dict[str, str]:
    """取风格预设；未知 key 返回 ``none`` 预设（永不为 None）。"""
    return STYLE_PRESETS[normalize_style(key, DEFAULT_STYLE)]


def style_options() -> list[dict[str, str]]:
    """供前端下拉渲染的风格清单：``[{key, label, desc}]``，按 ``STYLE_ORDER`` 排序。"""
    keys = [k for k in STYLE_ORDER if k in STYLE_PRESETS]
    keys += [k for k in STYLE_PRESETS if k not in keys]
    return [
        {"key": k, "label": STYLE_PRESETS[k]["label"], "desc": STYLE_PRESETS[k]["desc"]}
        for k in keys
    ]


def apply_style(
    positive: str | None,
    negative: str | None,
    style: Any,
    max_len: int = 0,
) -> tuple[str, str]:
    """把「出图风格」规范地套用到一条提示词上，返回 ``(positive, negative)``。

    - 正面提示词：在**末尾**追加该风格的统一描述词（幂等：已包含则不重复追加）；
    - 负面提示词：合并该风格的统一排除项（幂等：已包含则不重复追加）；
    - ``max_len > 0`` 时对正面提示词做截断，**优先保留风格词**（风格统一优先于剧情细节）。
    """
    preset = get_style(style)
    pos = (positive or "").strip()
    add_pos = (preset.get("positive") or "").strip()
    if add_pos and add_pos not in pos:
        pos = ("%s, %s" % (pos, add_pos)) if pos else add_pos

    neg = (negative or "").strip()
    add_neg = (preset.get("negative") or "").strip()
    if add_neg and add_neg not in neg:
        neg = ("%s, %s" % (neg, add_neg)) if neg else add_neg

    if max_len and max_len > 0 and len(pos) > max_len:
        pos = _truncate_keep_tail(pos, add_pos, max_len)
    return pos.strip(" ,"), neg.strip(" ,")


def _truncate_keep_tail(text: str, tail: str, max_len: int) -> str:
    """截断正文但保留尾部风格词（风格统一优先）。"""
    tail = (tail or "").strip()
    if not tail or len(tail) + 2 >= max_len:
        return text[:max_len]
    keep = max_len - len(tail) - 2
    return (text[:keep].rstrip(" ,") + ", " + tail)[:max_len]


__all__ = [
    "DEFAULT_STYLE",
    "STYLE_PRESETS",
    "STYLE_ORDER",
    "normalize_style",
    "is_valid_style",
    "get_style",
    "style_options",
    "apply_style",
]
