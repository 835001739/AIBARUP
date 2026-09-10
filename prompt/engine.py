"""M6 规则扩写引擎：解析 → 选择模型档案 → 识别已有维度 → 仅补缺失维度 → 去重与冲突检查 → 格式化。

硬性约束（PRD M6.3）：
1. **确定性**：相同输入 + 相同选项必须产生逐字符相同的输出。禁止随机、禁止依赖
   集合/字典遍历顺序、禁止时间参与内容生成（时间只用于 ``duration_ms`` 计量）。
2. **保留用户意图**：不替换、不删除原文中的主体、动作、数量、关系、专有名词或
   用户明确指定的风格；扩写结果以原文（或其术语映射结果）开头。
3. **可解释**：输出按维度拆分的 ``sections``、本次新增说明 ``additions`` 与 ``warnings``。
4. **不凭空添加**：不添加艺术家姓名、品牌、人物身份；文字排版类词条只在用户
   明确要求文字时才补充。
"""

from __future__ import annotations

import re
import time
from typing import Any

from core.errors import AIBARError
from core.textutil import is_near_duplicate, normalize_text, split_segments

from . import library

# ---------------------------------------------------------------- 常量

MAX_PROMPT_LEN = 2000
INTENSITIES: tuple[str, ...] = ("conservative", "balanced", "creative")
DEFAULT_INTENSITY = "balanced"
DEFAULT_PROFILE = library.DEFAULT_PROFILE
NEGATIVE_DIMENSION = "negative"
TYPOGRAPHY_DIMENSION = "typography"

WEIGHT_MIN = 1.0
WEIGHT_MAX = 1.5
DEFAULT_WEIGHT = 1.15
MAX_NEGATIVE_ITEMS = 14

# 三档强度：只声明"允许补充哪些维度、各补几条"，具体补哪些词条由相关性打分决定
INTENSITY_PLAN: dict[str, tuple[tuple[str, int], ...]] = {
    # 保守：只规范表达并补齐明显缺失的光线/构图，不新增叙事元素
    "conservative": (("lighting", 1), ("composition", 1)),
    # 均衡：补齐主体细节、环境、构图、光线、色彩与质量
    # （typography 只有用户明确写出文字意图时才会被 _candidate_dimensions 放行）
    "balanced": (
        ("appearance", 1),
        ("environment", 1),
        ("composition", 1),
        ("lighting", 1),
        ("color", 1),
        ("typography", 1),
        ("quality", 1),
    ),
    # 创意：在不动核心主体与动作的前提下增加氛围、材质、镜头语言与次要环境细节
    "creative": (
        ("appearance", 2),
        ("environment", 2),
        ("composition", 1),
        ("camera", 1),
        ("lighting", 1),
        ("color", 1),
        ("style", 1),
        ("mood", 1),
        ("typography", 1),
        ("quality", 1),
    ),
}

# 维度探测词表：只用于判断"用户是否已经写了某个维度"，宁缺勿错
_DIMENSION_DETECTORS: dict[str, tuple[str, ...]] = {
    "subject": (
        "女孩", "少女", "少年", "女人", "男人", "老人", "人物", "人像", "肖像", "角色",
        "猫", "狗", "鸟", "机器人", "跑车", "汽车", "料理", "角色设计",
        "a girl", "a man", "a woman", "a cat", "a dog", "a bird", "a robot", "a car",
        "portrait of", "full body", "young girl", "elderly", "character",
    ),
    "appearance": (
        "材质", "丝绸", "金属", "玻璃", "皮革", "木纹", "皮肤", "毛孔", "发丝", "陶瓷",
        "天鹅绒", "锈蚀", "面料", "质感",
        "silk", "metal", "glass", "leather", "wood", "skin", "pore", "ceramic", "velvet",
        "rust", "fabric", "texture",
    ),
    "action": (
        "站", "走", "跑", "跳", "坐", "转身", "回望", "回头", "伸手", "挥手", "倚靠",
        "旋转", "飘动", "躺", "姿势", "姿态", "动作",
        "standing", "walking", "running", "jumping", "sitting", "turning", "reaching",
        "leaning", "spinning", "pose", "posture", "waving",
    ),
    "environment": (
        "街道", "森林", "沙漠", "室内", "小巷", "巷", "山", "海", "悬崖", "图书馆",
        "雪", "城市", "房间", "背景", "环境", "场景", "天空", "废墟", "桥",
        "street", "forest", "desert", "interior", "alley", "mountain", "sea", "cliff",
        "library", "snow", "city", "background", "skyline", "ruins",
    ),
    "composition": (
        "构图", "三分", "对称", "留白", "引导线", "框景", "层次", "平铺", "倾斜", "黄金螺旋",
        "rule of thirds", "symmetry", "symmetrical", "negative space", "leading lines",
        "flat lay", "dutch angle", "composition", "centered", "depth layering",
    ),
    "camera": (
        "镜头", "景别", "特写", "远景", "广角", "长焦", "微距", "俯拍", "仰拍", "低机位",
        "机位", "景深", "虚化", "散景", "手持", "35mm", "50mm", "85mm", "24mm",
        "close-up", "wide shot", "wide angle", "macro", "telephoto", "bokeh",
        "depth of field", "overhead", "low angle", "handheld",
    ),
    "lighting": (
        "逆光", "侧光", "轮廓光", "柔光", "硬光", "阴影", "丁达尔", "体积光", "月光",
        "烛光", "曝光", "光线", "光影", "打光",
        "backlit", "rim light", "side light", "soft light", "hard light", "shadow",
        "volumetric", "moonlight", "candlelight", "golden hour", "chiaroscuro", "lighting",
    ),
    "color": (
        "色调", "配色", "暖色", "冷色", "饱和", "单色", "黑白", "粉彩", "大地色", "胶片",
        "调色", "色彩", "双色调",
        "warm tone", "cool tone", "color tone", "color palette", "saturated", "monochrome",
        "black and white", "pastel", "duotone", "color grading", "color grad",
    ),
    "style": (
        "风格", "写实", "插画", "动漫", "油画", "水彩", "赛博朋克", "极简", "水墨",
        "像素", "复古", "概念设计", "国风",
        "photorealistic", "anime", "illustration", "oil painting", "watercolor",
        "cyberpunk", "minimalist", "ink wash", "pixel art", "concept art", "cinematic",
    ),
    "mood": (
        "氛围", "情绪", "宁静", "孤寂", "梦幻", "紧张", "悬疑", "治愈", "史诗", "神秘",
        "怀旧", "活力", "静谧", "孤独",
        "atmosphere", "mood", "peaceful", "lonely", "dreamy", "tense", "cozy", "epic",
        "mysterious", "nostalgic", "energetic", "serene",
    ),
    "quality": (
        "质量", "清晰", "锐利", "细节", "分辨率", "画质", "渲染", "伪影", "高清", "超清",
        "8k", "4k", "hdr", "uhd", "精致",
        "quality", "sharp", "detailed", "high resolution", "clean render", "artifacts",
    ),
    "typography": (
        "文字", "标题", "字体", "排版", "版式", "海报", "字幕", "标语", "logo",
        "typography", "headline", "title", "font", "poster", "lettering", "serif",
    ),
    # 注意：这里只放"明确的否定表述"，不能放单个虚词（例如裸的"无"），
    # 否则"无尽的雪原"这类正常描写会被误判为负向约束
    "negative": (
        "不要", "避免", "排除", "没有", "无水印", "负向",
        "negative", "without", "avoid", "no blurry", "not blurry",
    ),
}

# 主体类型标签：命中后给同类词条加分，让补充内容贴合原文描述的对象
_SUBJECT_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("人物", ("女孩", "少女", "少年", "女人", "男人", "老人", "人物", "人像", "肖像", "她", "他",
              "girl", "woman", "man", "boy", "person", "portrait", "people", "elderly")),
    ("动物", ("猫", "狗", "鸟", "动物", "宠物", "cat", "dog", "bird", "animal")),
    ("自然", ("森林", "山", "海", "雪", "沙漠", "天空", "河", "湖", "花", "树",
              "forest", "mountain", "sea", "snow", "desert", "sky", "river", "flower")),
    ("都市", ("街道", "城市", "巷", "楼", "建筑", "霓虹", "天际线",
              "street", "city", "alley", "building", "neon", "skyline")),
    ("室内", ("室内", "房间", "客厅", "图书馆", "咖啡馆", "窗",
              "interior", "room", "living room", "library", "cafe", "window")),
    ("产品", ("产品", "瓶", "相机", "手表", "鞋", "包", "罐",
              "product", "bottle", "camera", "watch", "shoe", "bag", "packaging")),
    ("美食", ("美食", "料理", "菜", "食物", "咖啡", "蛋糕", "茶",
              "food", "dish", "cuisine", "coffee", "cake", "tea")),
    ("科幻", ("机器人", "机械", "未来", "赛博", "太空",
              "robot", "mech", "cyberpunk", "futuristic", "space")),
    ("夜景", ("夜", "月光", "霓虹", "night", "moonlight", "neon")),
)

# 中英术语映射表：仅 sd15_sdxl 档案使用，把中文提示词转成英文标签
_ZH_EN_TERMS: tuple[tuple[str, str], ...] = (
    # 数量与限定
    ("三个", "three"), ("两个", "two"), ("四个", "four"), ("一群", "a group of"),
    ("一个", "a"), ("一只", "a"), ("一位", "a"), ("一名", "a"), ("几只", "several"),
    # 主体
    ("女孩", "girl"), ("少女", "young girl"), ("女人", "woman"), ("男人", "man"),
    ("男孩", "boy"), ("少年", "teenage boy"), ("老人", "elderly person"),
    ("人物", "person"), ("人像", "portrait"), ("肖像", "portrait"), ("角色", "character"),
    ("橘猫", "orange tabby cat"), ("橘", "orange"), ("黑猫", "black cat"), ("白猫", "white cat"),
    ("猫", "cat"), ("小狗", "dog"), ("狗", "dog"), ("飞鸟", "bird in flight"), ("鸟", "bird"),
    ("马", "horse"), ("鱼", "fish"), ("龙", "dragon"), ("蝴蝶", "butterfly"),
    ("机器人", "robot"), ("机械", "mechanical"), ("跑车", "sports car"), ("汽车", "car"),
    ("自行车", "bicycle"), ("相机", "camera"), ("手表", "wristwatch"), ("花", "flower"),
    ("树", "tree"), ("料理", "dish"), ("美食", "food photography"), ("咖啡", "coffee"),
    ("蛋糕", "cake"), ("茶", "tea"), ("书", "book"), ("雨伞", "umbrella"), ("吉他", "guitar"),
    ("瓶子", "bottle"), ("杯子", "cup"), ("建筑", "architecture"), ("房子", "house"),
    # 服装与外观
    ("连衣裙", "dress"), ("裙子", "skirt"), ("西装", "suit"), ("外套", "coat"),
    ("毛衣", "sweater"), ("衬衫", "shirt"), ("帽子", "hat"), ("眼镜", "glasses"),
    ("长发", "long hair"), ("短发", "short hair"), ("白发", "white hair"),
    ("头发", "hair"), ("皮肤", "skin"), ("发丝", "hair strands"), ("眼睛", "eyes"),
    ("笑容", "smile"), ("微笑", "smiling"),
    # 动作与姿态
    ("站立", "standing"), ("站在", "standing"), ("站着", "standing"), ("站", "standing"),
    ("坐着", "sitting"), ("坐", "sitting"), ("盘腿", "sitting cross-legged"), ("躺", "lying down"),
    ("行走", "walking"), ("走", "walking"), ("奔跑", "running"), ("跑", "running"),
    ("跳跃", "jumping"), ("转身", "turning around"), ("回望", "looking back over the shoulder"),
    ("回头", "looking back"), ("伸手", "reaching out"), ("挥手", "waving"),
    ("倚靠", "leaning against"), ("旋转", "spinning"), ("漂浮", "floating"),
    # 环境
    ("雨夜", "rainy night"), ("雨天", "rainy day"), ("下雨", "rain"), ("雨", "rain"),
    ("街道", "street"), ("小巷", "alley"), ("巷子", "alley"), ("城市", "city"),
    ("天际线", "skyline"), ("森林", "forest"), ("树林", "forest"), ("沙漠", "desert"),
    ("沙丘", "sand dunes"), ("山脊", "mountain ridge"), ("山", "mountain"),
    ("海边", "seaside"), ("悬崖", "cliff"), ("海", "sea"), ("湖", "lake"), ("河", "river"),
    ("雪原", "snow field"), ("雪地", "snowy ground"), ("雪", "snow"), ("图书馆", "library"),
    ("咖啡馆", "cafe"), ("房间", "room"), ("客厅", "living room"), ("室内", "interior"),
    ("窗台", "on a windowsill"), ("窗边", "by the window"), ("窗外", "by the window"),
    ("洒进来", "streaming in"), ("洒下", "pouring down"), ("透过", "through"), ("天空", "sky"), ("云", "clouds"), ("星空", "starry sky"),
    ("未来城市", "futuristic city"), ("废墟", "ruins"), ("桥", "bridge"), ("楼梯", "staircase"),
    # 构图与镜头
    ("构图", "composition"), ("三分法", "rule of thirds"), ("对称", "symmetrical composition"),
    ("留白", "negative space"), ("特写", "close-up"), ("全景", "wide shot"), ("远景", "wide shot"),
    ("广角", "wide angle lens"), ("长焦", "telephoto lens"), ("微距", "macro photography"),
    ("景深", "shallow depth of field"), ("虚化", "bokeh"), ("低机位", "low angle shot"),
    ("俯拍", "overhead shot"), ("仰拍", "low angle shot"), ("镜头", "lens"), ("机位", "camera angle"),
    # 光线
    ("逆光", "backlit"), ("轮廓光", "rim lighting"), ("侧光", "side lighting"),
    ("柔光", "soft light"), ("硬光", "hard light"), ("阳光", "sunlight"),
    ("黄金时刻", "golden hour"), ("黄昏", "dusk"), ("日出", "sunrise"), ("日落", "sunset"),
    ("月光", "moonlight"), ("烛光", "candlelight"), ("霓虹灯", "neon light"), ("霓虹", "neon glow"),
    ("体积光", "volumetric light rays"), ("丁达尔", "volumetric light rays"), ("阴影", "shadow"),
    ("光线", "light"), ("灯光", "studio lighting"), ("光", "light"),
    # 色彩
    ("暖色调", "warm color tone"), ("冷色调", "cool color tone"), ("色调", "color tone"),
    ("配色", "color palette"), ("黑白", "black and white"), ("单色", "monochrome"),
    ("饱和", "saturated colors"), ("粉彩", "pastel palette"),
    ("青橙", "teal and orange color grading"), ("胶片", "film-like color grading"),
    ("调色", "color grading"),
    # 风格与氛围
    ("写实", "photorealistic"), ("照片级", "photorealistic"), ("动漫", "anime style"),
    ("二次元", "anime style"), ("插画", "illustration"), ("油画", "oil painting"),
    ("水彩", "watercolor"), ("水墨", "chinese ink wash painting"), ("赛博朋克", "cyberpunk"),
    ("极简", "minimalist"), ("像素", "pixel art"), ("复古", "vintage"), ("概念设计", "concept art"),
    ("电影感", "cinematic"), ("氛围", "atmosphere"), ("宁静", "peaceful"), ("孤寂", "lonely"),
    ("梦幻", "dreamy"), ("神秘", "mysterious"), ("怀旧", "nostalgic"), ("治愈", "cozy"),
    ("史诗", "epic"), ("紧张", "tense"), ("悬疑", "suspenseful"), ("孤独", "lonely"),
    # 质量
    ("高清", "high resolution"), ("超清", "ultra high resolution"), ("细节", "highly detailed"),
    ("质感", "fine texture detail"), ("锐利", "sharp focus"), ("清晰", "sharp focus"),
    ("高质量", "high quality"), ("精致", "intricate detail"),
    # 文字排版
    ("海报", "poster design"), ("标题", "headline text"), ("文字", "text"),
    ("字体", "typography"), ("排版", "layout"),
    # 常见修饰与虚词
    ("美丽", "beautiful"), ("漂亮", "beautiful"), ("可爱", "cute"), ("帅气", "handsome"),
    ("优雅", "elegant"), ("快乐", "happy"), ("安静", "quiet"), ("红色", "red"),
    ("蓝色", "blue"), ("绿色", "green"), ("白色", "white"), ("黑色", "black"), ("金色", "golden"),
    ("红", "red"), ("蓝", "blue"), ("绿", "green"), ("白", "white"), ("黑", "black"), ("金", "golden"),
    ("穿着", "wearing"), ("戴着", "wearing"), ("拿着", "holding"),
    ("和", "and"), ("与", "and"), ("有", "with"), ("的", ""), ("了", ""), ("着", ""),
    ("在", "in"), ("上", ""), ("里", "in"), ("中", "in"),
)

_ZH_EN_MAP: dict[str, str] = dict(_ZH_EN_TERMS)
_MAX_ZH_KEY_LEN = max(len(key) for key in _ZH_EN_MAP)

# 兜底词条：当某一维度与原文毫无相关性信号（打分为 0）时，
# 宁可补一条中性、安全、几乎不会冲突的词条，也不按顺序抓一条强叙事词条
_NEUTRAL_FALLBACK: dict[str, str] = {
    "appearance": "appearance.skin_pore_detail",
    "environment": "environment.cozy_interior",
    "composition": "composition.rule_of_thirds",
    "camera": "camera.shallow_depth",
    "lighting": "lighting.soft_window_light",
    "color": "color.film_grade",
    "style": "style.photorealistic",
    "mood": "mood.peaceful",
    "quality": "quality.sharp_focus",
}
# 仅 appearance 需要按主体类型挑更贴切的中性材质
_NEUTRAL_BY_SUBJECT_TAG: dict[str, str] = {
    "动物": "appearance.flowing_hair",
    "产品": "appearance.ceramic_glaze",
    "美食": "appearance.ceramic_glaze",
    "室内": "appearance.wood_grain",
    "自然": "appearance.wood_grain",
    "都市": "appearance.weathered_rust",
    "科幻": "appearance.brushed_metal",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_WORD_RE = re.compile(r"[a-z]{4,}")
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_SENTENCE_END = (".", "!", "?", "。", "！", "？")


# ---------------------------------------------------------------- 输入校验


def validate_input(
    original_prompt: Any,
    profile: str | None = DEFAULT_PROFILE,
    intensity: str | None = DEFAULT_INTENSITY,
    template_id: str | None = None,
    options: Any = None,
) -> None:
    """校验扩写入参，任何一项非法都抛 ``AIBARError("invalid_input", ...)``。

    调用方必须在调用具体 Provider 之前先执行本函数，保证非法输入不会触达引擎。
    """
    if not isinstance(original_prompt, str):
        raise AIBARError("invalid_input", "原始提示词必须是字符串")
    stripped = original_prompt.strip()
    if not stripped:
        raise AIBARError("invalid_input", "原始提示词不能为空")
    if _PUNCT_ONLY_RE.match(stripped):
        raise AIBARError("invalid_input", "原始提示词不能只包含标点或符号")
    if len(original_prompt) > MAX_PROMPT_LEN:
        raise AIBARError(
            "invalid_input", f"原始提示词过长，最多 {MAX_PROMPT_LEN} 个字符"
        )

    resolved_intensity = intensity or DEFAULT_INTENSITY
    if resolved_intensity not in INTENSITIES:
        raise AIBARError(
            "invalid_input",
            f"未知的扩写强度：{resolved_intensity}，可选值为 {'/'.join(INTENSITIES)}",
        )

    resolved_profile = profile or DEFAULT_PROFILE
    if resolved_profile not in library.profile_keys():
        raise AIBARError("invalid_input", f"未知的模型档案：{resolved_profile}")

    if template_id:
        if library.template(str(template_id)) is None:
            raise AIBARError("invalid_input", f"未知的场景模板：{template_id}")

    if options is not None and not isinstance(options, dict):
        raise AIBARError("invalid_input", "options 必须是对象")


# ---------------------------------------------------------------- 解析


def _detect_dimensions(text: str) -> list[str]:
    """识别原文已经覆盖了哪些维度。返回顺序固定为维度定义顺序。"""
    norm = normalize_text(text)
    present: list[str] = []
    for dimension in library.dimension_keys():
        keywords = _DIMENSION_DETECTORS.get(dimension, ())
        for keyword in keywords:
            if normalize_text(keyword) in norm:
                present.append(dimension)
                break
    return present


def _detect_subject_tags(text: str) -> list[str]:
    """识别原文描述的对象类型，用于让补充词条贴合主体。"""
    norm = normalize_text(text)
    tags: list[str] = []
    for tag, keywords in _SUBJECT_TAGS:
        for keyword in keywords:
            if normalize_text(keyword) in norm:
                tags.append(tag)
                break
    return tags


def _classify_segment(segment: str) -> str:
    """把原文片段归入一个维度；无法判断时归入主体维度。"""
    norm = normalize_text(segment)
    for dimension in library.dimension_keys():
        for keyword in _DIMENSION_DETECTORS.get(dimension, ()):
            if normalize_text(keyword) in norm:
                return dimension
    return library.FALLBACK_DIMENSION


def _original_sections(segments: list[str], translator: Any = None) -> list[dict]:
    """按维度拆分原文，``is_new`` 恒为 False。

    Args:
        segments: 原文片段。
        translator: 可选的片段转换器。sd15_sdxl 会把中文片段按术语表转成英文，
            保证 ``sections`` 与最终正向提示词保持同一套表述。
    """
    grouped: dict[str, list[str]] = {}
    for segment in segments:
        text = segment
        if translator is not None:
            converted = translator(segment)
            if converted:
                text = converted
        grouped.setdefault(_classify_segment(segment), []).append(text)
    sections: list[dict] = []
    for dimension in library.dimension_keys():
        for text in grouped.get(dimension, []):
            sections.append(
                {
                    "dimension": dimension,
                    "dimension_label": library.dimension_label(dimension),
                    "text": text,
                    "is_new": False,
                }
            )
    return sections


def _translate(text: str) -> tuple[list[str], list[str]]:
    """把中文片段按术语表映射为英文标签。

    Returns:
        ``(parts, unknown_runs)``：映射后的英文片段列表，以及无法可靠翻译、
        需要保留原文并提示用户检查的中文片段列表。
    """
    parts: list[str] = []
    unknown: list[str] = []
    literal: list[str] = []
    pending: list[str] = []
    index = 0
    length = len(text)

    def flush_literal() -> None:
        if literal:
            parts.append("".join(literal).strip())
            literal.clear()

    def flush_pending() -> None:
        if pending:
            run = "".join(pending)
            parts.append(run)
            unknown.append(run)
            pending.clear()

    while index < length:
        char = text[index]
        if _CJK_RE.match(char):
            flush_literal()
            matched: tuple[str, str] | None = None
            for size in range(min(_MAX_ZH_KEY_LEN, length - index), 0, -1):
                key = text[index : index + size]
                if key in _ZH_EN_MAP:
                    matched = (key, _ZH_EN_MAP[key])
                    break
            if matched is None:
                pending.append(char)
                index += 1
                continue
            flush_pending()
            key, value = matched
            if value:
                parts.append(value)
            index += len(key)
        else:
            flush_pending()
            literal.append(char)
            index += 1

    flush_literal()
    flush_pending()
    return [part for part in parts if part], unknown


# ---------------------------------------------------------------- 选择词条


def _keywords(term: library.Term) -> tuple[set[str], set[str]]:
    """提取词条的英文实词与中文二元组，用于与原文本做确定性相关性比较。"""
    words = set(_EN_WORD_RE.findall(normalize_text(term.text)))
    zh = normalize_text(term.text_zh)
    grams = {zh[i : i + 2] for i in range(len(zh) - 1) if _CJK_RE.match(zh[i])}
    return words, grams


def _score(term: library.Term, norm_prompt: str, detected_tags: list[str]) -> int:
    """相关性打分：命中主体类型标签权重最高，其次英文实词与中文二元组。"""
    score = 0
    for tag in term.tags:
        if tag in detected_tags:
            score += 3
    words, grams = _keywords(term)
    score += sum(1 for word in words if word in norm_prompt)
    score += min(5, sum(1 for gram in grams if gram in norm_prompt))
    return score


def _candidate_dimensions(present: list[str]) -> list[str]:
    """当前强度下允许补充的维度，固定按维度定义顺序返回（PRD §11 的结构提示）。

    - ``positive_forbidden``（负向约束）永不进入正向提示词；
    - ``requires_user_intent``（文字排版）只在该维度已被原文命中时放行，
      否则一律跳过，避免凭空生成画面里没人要的文字。
    """
    guards = library.guards()
    result: list[str] = []
    for dimension in library.dimension_keys():
        guard = guards.get(dimension) or {}
        if guard.get("positive_forbidden"):
            continue
        if guard.get("requires_user_intent"):
            # 用户已写明文字意图时才补充；此时即便维度"已存在"也允许继续丰富
            if dimension not in present:
                continue
        elif dimension in present:
            continue
        result.append(dimension)
    return result


def _pick(
    dimension: str,
    count: int,
    profile_key: str,
    norm_prompt: str,
    detected_tags: list[str],
    template: dict | None,
) -> list[library.Term]:
    """按相关性确定性地挑出该维度最合适的词条。"""
    pool = [
        (index, item)
        for index, item in enumerate(library.terms())
        if item.dimension == dimension and item.supports(profile_key)
    ]
    if not pool:
        return []
    preferred = [str(item) for item in (template or {}).get("dimensions", [])]
    boost = 1 if dimension in preferred else 0
    # 只保留与原文真正相关的词条：强度决定条数上限，不决定"硬凑几条"。
    # boost 只用于在相关词条之间做偏好，不能把 0 分词条抬进候选集，
    # 否则会退化成"按 JSON 顺序取第一条"，与保留用户意图的目标相悖。
    scored: list[tuple[int, int, library.Term]] = []
    for index, item in pool:
        raw = _score(item, norm_prompt, detected_tags)
        if raw <= 0:
            continue
        scored.append((-(raw + boost), index, item))
    if not scored:
        return _neutral_pick(dimension, detected_tags, profile_key)
    scored.sort(key=lambda row: (row[0], row[1]))
    return [item for _neg, _index, item in scored[:count]]


def _neutral_pick(dimension: str, detected_tags: list[str], profile_key: str) -> list[library.Term]:
    """无相关性信号时的中性兜底选择（见 ``_NEUTRAL_FALLBACK`` 注释）。"""
    term_id = _NEUTRAL_FALLBACK.get(dimension, "")
    if dimension == "appearance":
        for tag in detected_tags:
            override = _NEUTRAL_BY_SUBJECT_TAG.get(tag)
            if override:
                term_id = override
                break
    term_item = library.term(term_id)
    if term_item is None or not term_item.supports(profile_key):
        return []
    return [term_item]


# ---------------------------------------------------------------- 去重与冲突


def _is_duplicate(candidate: str, corpus: str) -> bool:
    """与已有内容（原文 + 已接受的补充）判断是否语义重复。"""
    cand = normalize_text(candidate)
    if not cand:
        return True
    if cand in corpus:
        return True
    return is_near_duplicate(candidate, corpus)


def _conflict_group(candidate_texts: list[str], corpus: str) -> dict | None:
    """候选词条与已有内容是否命中互斥词表。命中则返回该互斥组。"""
    for group in library.conflicts():
        keys = [normalize_text(key) for key in group.get("keys", []) if key]
        hit = [key for key in keys if any(key in text for text in candidate_texts)]
        if not hit:
            continue
        for key in keys:
            if key in hit:
                continue
            if key and key in corpus:
                return group
    return None


# ---------------------------------------------------------------- 格式化


def _with_weight(text: str, weight: float) -> str:
    return f"({text}:{weight:.2f})"


def _weight_of(options: dict) -> float:
    raw = options.get("weight", DEFAULT_WEIGHT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WEIGHT
    return max(WEIGHT_MIN, min(WEIGHT_MAX, value))


def _join_chunks(profile_key: str, chunks: list[str]) -> str:
    """按模型档案的拼接风格把原文与补充内容组装成正向提示词。"""
    profile = library.profile(profile_key)
    separator = str(profile.get("separator") or ", ")
    style = str(profile.get("style") or "tags")
    chunks = [chunk.strip() for chunk in chunks if chunk and chunk.strip()]
    if not chunks:
        return ""
    if style == "tags":
        return separator.join(chunks)
    if profile_key == "flux_flux2":
        sentences = [
            chunk if chunk.endswith(_SENTENCE_END) else f"{chunk}."
            for chunk in chunks
        ]
        return " ".join(sentences)
    text = separator.join(chunks)
    if not text.endswith(_SENTENCE_END):
        text = f"{text}。"
    return text


def _build_negative(profile_key: str, template: dict | None) -> str:
    """生成负向提示词；仅当模型档案支持负向提示词时有内容。"""
    profile = library.profile(profile_key)
    if not profile.get("supports_negative"):
        return ""
    pieces: list[str] = []
    corpus = ""
    if template:
        for piece in str(template.get("negative") or "").split(","):
            piece = piece.strip()
            if piece and not _is_duplicate(piece, corpus):
                pieces.append(piece)
                corpus = normalize_text(", ".join(pieces))
    for item in library.terms():
        if item.dimension != NEGATIVE_DIMENSION or not item.supports(profile_key):
            continue
        if len(pieces) >= MAX_NEGATIVE_ITEMS:
            break
        if _is_duplicate(item.text, corpus):
            continue
        pieces.append(item.text)
        corpus = normalize_text(", ".join(pieces))
    return ", ".join(pieces)


# ---------------------------------------------------------------- 主流程


def expand(
    original_prompt: str,
    profile: str = DEFAULT_PROFILE,
    intensity: str = DEFAULT_INTENSITY,
    template_id: str | None = None,
    options: dict | None = None,
) -> dict:
    """执行规则扩写，返回与 API 契约一致的结构化结果。

    Args:
        original_prompt: 用户原始提示词（中英文均可）。
        profile: 模型档案，默认 ``generic``。
        intensity: 扩写强度，默认 ``balanced``。
        template_id: 可选场景模板 id。
        options: 可选参数，目前支持 ``enable_weight`` 与 ``weight``。

    Raises:
        AIBARError: 入参非法（``invalid_input``）时抛出，且不产生任何扩写行为。
    """
    validate_input(original_prompt, profile, intensity, template_id, options)

    started = time.perf_counter()
    profile_key = profile or DEFAULT_PROFILE
    intensity_key = intensity or DEFAULT_INTENSITY
    opts = options if isinstance(options, dict) else {}
    prompt_text = original_prompt.strip()
    template = library.template(str(template_id)) if template_id else None

    profile_meta = library.profile(profile_key)
    norm_prompt = normalize_text(prompt_text)
    segments = split_segments(prompt_text)
    present = _detect_dimensions(prompt_text)
    detected_tags = _detect_subject_tags(prompt_text)

    warnings: list[str] = []
    need_translate = profile_key == "sd15_sdxl" and bool(_CJK_RE.search(prompt_text))

    def _segment_text(segment: str) -> str:
        """把单个原文片段转成英文标签串，无法转换时回退原文。"""
        if not _CJK_RE.search(segment):
            return segment
        parts, _unknown = _translate(segment)
        return ", ".join(parts) if parts else segment

    sections: list[dict] = _original_sections(
        segments, _segment_text if need_translate else None
    )

    # ---- 1) 原文基线：sd15_sdxl 需要英文标签，其余档案原样保留
    chunks: list[str] = []
    if need_translate:
        parts, unknown = _translate(prompt_text)
        chunks.append(", ".join(parts))
        if unknown:
            warnings.append(
                f"检测到 {len(unknown)} 处中文内容无法可靠翻译，已保留原文，请检查后再使用"
            )
    else:
        chunks.append(prompt_text)

    # ---- 2) 套用场景模板：模板基线直接作为新增片段，后续词条对它去重
    if template:
        template_positive = str(template.get("positive") or "").strip()
        if template_positive:
            chunks.append(template_positive)
            sections.append(
                {
                    "dimension": "style",
                    "dimension_label": library.dimension_label("style"),
                    "text": template_positive,
                    "is_new": True,
                }
            )
        template_profile = str(template.get("profile") or "")
        if template_profile and template_profile != profile_key:
            # library.profile 遇到未知档案会抛 AIBARError；这里只取展示名，失败就退回 key
            try:
                template_profile_label = str(
                    library.profile(template_profile).get("label") or template_profile
                )
            except AIBARError:
                template_profile_label = template_profile
            warnings.append(
                f"场景模板「{template.get('name', template_id)}」默认适配 "
                f"{template_profile_label} 档案，已按当前档案重新格式化"
            )

    corpus = normalize_text(" ".join(chunks))

    # ---- 3) 仅补缺失维度
    plan = dict(INTENSITY_PLAN.get(intensity_key, INTENSITY_PLAN[DEFAULT_INTENSITY]))
    allowed = _candidate_dimensions(present)
    additions: list[str] = []
    use_weight = bool(opts.get("enable_weight")) and bool(profile_meta.get("supports_weight"))
    if opts.get("enable_weight") and not profile_meta.get("supports_weight"):
        warnings.append("当前模型档案不支持权重语法，已忽略权重设置")
    weight = _weight_of(opts)

    for dimension in sorted(allowed, key=library.dimension_order):
        count = plan.get(dimension, 0)
        if count <= 0:
            continue
        for term_item in _pick(dimension, count, profile_key, norm_prompt, detected_tags, template):
            text = term_item.insert_text(profile_key)
            texts = [normalize_text(text), normalize_text(term_item.insert_text_zh(profile_key))]
            if _is_duplicate(text, corpus):
                continue
            group = _conflict_group(texts, corpus)
            if group:
                warnings.append(
                    f"已跳过与原文冲突的「{term_item.name}」：{group.get('reason', '存在互斥描述')}"
                )
                continue
            final_text = _with_weight(text, weight) if use_weight else text
            chunks.append(final_text)
            sections.append(
                {
                    "dimension": dimension,
                    "dimension_label": library.dimension_label(dimension),
                    "text": final_text,
                    "is_new": True,
                }
            )
            additions.append(
                f"[{library.dimension_label(dimension)}] {term_item.name}：{final_text}"
            )
            corpus = normalize_text(" ".join(chunks))

    # ---- 4) 负向提示词
    expanded_negative = _build_negative(profile_key, template)
    if not profile_meta.get("supports_negative"):
        if profile_key == "flux_flux2":
            warnings.append("FLUX.2 不支持负向提示词，已跳过负向提示词生成")
            if NEGATIVE_DIMENSION in present:
                warnings.append("原文中的负向约束无法在 FLUX.2 中表达，已忽略对应内容")
        else:
            expanded_negative = ""

    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "original_prompt": original_prompt,
        "expanded_positive": _join_chunks(profile_key, chunks),
        "expanded_negative": expanded_negative,
        "profile": profile_key,
        "intensity": intensity_key,
        "provider": "rules",
        "template_id": str(template_id) if template_id else None,
        "sections": sections,
        "additions": additions,
        "warnings": warnings,
        "duration_ms": duration_ms,
    }


__all__ = [
    "MAX_PROMPT_LEN",
    "INTENSITIES",
    "DEFAULT_INTENSITY",
    "DEFAULT_PROFILE",
    "validate_input",
    "expand",
]
