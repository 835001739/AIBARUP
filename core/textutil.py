"""共享文本工具：规范化、指纹、语义片段切分、维度归类、质量评分、风险识别。

这是 M7（自动学习）与 M9（反推入库）共用的确定性算法层。
设计原则（PRD M7.5 / M9.6）：
- 全部本地确定性计算，同一输入永远得到同一结果；
- 只做保守判断，无法可靠拆分的内容宁可丢弃，不伪装成精品词条；
- 运行参数、空泛句式、连接词、整段口语一律不进入正式词库。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from config import Config

# ---------------------------------------------------------------- 规范化

_PUNCT_MAP = {
    "，": ",",
    "、": ",",
    "；": ",",
    ";": ",",
    "：": ":",
    ":": ":",
    "。": ".",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "《": "<",
    "》": ">",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "～": "~",
    "－": "-",
    "—": "-",
    "－": "-",
    "\u3000": " ",
    "\t": " ",
}

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_TRAIL_RE = re.compile(r"^[\s,.:;!?\-~*#\"'()\[\]<>/\\|]+|[\s,.:;!?\-~*#\"'()\[\]<>/\\|]+$")


def sanitize_model_text(text: str, max_len: int = 4000) -> str:
    """模型输出视为不可信文本：清理控制字符并限制长度，只作为纯文本展示。"""
    if not isinstance(text, str):
        return ""
    cleaned = _CTRL_RE.sub(" ", text)
    cleaned = cleaned.replace("\r", "\n")
    return cleaned[:max_len]


def normalize_text(text: str) -> str:
    """规范化文本：全角转半角、统一标点、折叠空白、去掉首尾标点、英文小写。

    用于指纹与去重比较，不用于展示。
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = "".join(_PUNCT_MAP.get(ch, ch) for ch in out)
    out = _CTRL_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out)
    out = out.strip()
    out = _TRAIL_RE.sub("", out)
    return out.lower()


def content_fingerprint(media_type: str, dimension: str, prompt_text: str) -> str:
    """内容指纹：规范化后的 `media_type + dimension + prompt_text` 的 SHA-256。

    忽略大小写、首尾标点、重复空白和中英文逗号差异。
    """
    basis = "|".join(
        [
            normalize_text(media_type or ""),
            normalize_text(dimension or ""),
            normalize_text(prompt_text or ""),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 片段切分

_SPLIT_RE = re.compile(r"[,，。;；!！?？\n\r]+|(?<=[a-z])\.(?=\s)")


def split_segments(text: str) -> list[str]:
    """按标点切分语义片段。保留短横线连接的复合描述。"""
    if not text:
        return []
    raw = _CTRL_RE.sub(" ", text)
    parts = _SPLIT_RE.split(raw)
    return [p.strip() for p in parts if p and p.strip()]


_MIN_SEGMENT = 4
_MAX_SEGMENT = 80


# ---------------------------------------------------------------- 停用与噪声

# 无独立语义的模板外壳与连接词
_STOP_EXACT = {
    "and", "with", "of", "the", "a", "an", "in", "on", "at", "to", "for",
    "",
}
_STOP_PHRASES = (
    "请生成一张",
    "请生成",
    "帮我生成",
    "生成一张",
    "画面中可以看到",
    "画面中有",
    "图片中",
    "这是一张",
    "这是一个",
    "这是一段",
    "非常好看的",
    "很好看",
    "很漂亮",
    "超级好看",
    "好看的",
    "高质量",
    "高清",
    "超清",
    "超高清",
    "this image shows",
    "this image depicts",
    "the image shows",
    "the image features",
    "a picture of",
    "an image of",
    "in the image",
    "there is",
    "there are",
)
# 机械重复的质量口号（单独成条时不构成精品描述）
_QUALITY_SLOGANS = (
    "best quality",
    "high quality",
    "masterpiece",
    "ultra detailed",
    "8k",
    "4k",
    "uhd",
    "hdr",
    "high resolution",
    "超细节",
    "极致细节",
    "顶级画质",
    "杰作",
)
_PARAM_PATTERNS = (
    r"\bseed\s*[:=]?\s*\d+",
    r"\bsteps?\s*[:=]?\s*\d+",
    r"\bcfg\s*[:=]?\s*[\d.]+",
    r"\bsampler\w*\s*[:=]",
    r"\bscheduler\w*\s*[:=]",
    r"\bdenois\w*\s*[:=]?\s*[\d.]+",
    r"\bclip\s*skip\s*[:=]?\s*\d+",
    r"\b\d{3,5}\s*[x×]\s*\d{3,5}\b",
    r"\blora\w*\s*[:=]",
    r"<lora:[^>]+>",
    r"\bcheckpoint\s*[:=]",
    r"\bmodel\s*[:=]\s*\w+",
    r"\bar\b\s*\d+:\d+",
    r"\bsize\s*[:=]",
    r"\bbatch\s*size\b",
    r"\bvae\s*[:=]",
)
_PARAM_RE = re.compile("|".join(_PARAM_PATTERNS), re.IGNORECASE)
_EN_STOP_ONLY = re.compile(r"^[\s\w\-',.+&/()]+$")


def is_stop_phrase(segment: str) -> bool:
    norm = normalize_text(segment)
    if norm in _STOP_EXACT or len(norm) < _MIN_SEGMENT:
        return True
    for phrase in _STOP_PHRASES:
        if phrase in norm:
            return True
    return False


def is_quality_slogan(segment: str) -> bool:
    """整段只是机械重复的质量口号。"""
    norm = normalize_text(segment)
    if not norm:
        return True
    for slogan in _QUALITY_SLOGANS:
        if norm == slogan:
            return True
    # 由质量口号 + 连接词堆砌而成
    tokens = [t for t in re.split(r"[,\s]+", norm) if t]
    if tokens and all(any(s in t or t in s for s in _QUALITY_SLOGANS) or t in _STOP_EXACT for t in tokens):
        return True
    return False


def is_run_parameter(segment: str) -> bool:
    """运行参数（seed/steps/cfg/lora/尺寸等）不属于精品描述。"""
    return bool(_PARAM_RE.search(segment or ""))


# ---------------------------------------------------------------- 风险识别

_RISK_PATTERNS = (
    (r"\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]+", "artist"),
    (r"(?:in the style of|风格类似|模仿)\s*[\w\u4e00-\u9fa5\-]+", "artist_style"),
    (r"(?:品牌|brand)\s*[:：]?\s*[\w\u4e00-\u9fa5]+", "brand"),
    (r"(?:coca[- ]?cola|nike|apple inc|disney|marvel|pokemon)", "brand"),
    (r"(?:real person|celebrity|明星|名人| politician)", "identity"),
    (r"(?:looks like|resembles|看起来像)\s*[\w\u4e00-\u9fa5]+", "identity"),
    (r"\b(?:sdxl|flux|midjourney|stable diffusion|dalle|comfyui)\b", "model_name"),
)
_RISK_RE = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _RISK_PATTERNS]


def risk_flags(segment: str) -> list[str]:
    """识别身份/品牌/艺术家/模型名等推测性内容。

    命中风险的片段禁止自动入库（PRD M9.1 / M9.6）。
    """
    flags: list[str] = []
    for regex, tag in _RISK_RE:
        if regex.search(segment or ""):
            flags.append(tag)
    return flags


# ---------------------------------------------------------------- 维度归类

_TAXONOMY_CACHE: dict | None = None


def load_taxonomy() -> dict:
    """读取版本化分类体系。加载失败时回退到最小内置体系，不影响其他模块。"""
    global _TAXONOMY_CACHE
    if _TAXONOMY_CACHE is not None:
        return _TAXONOMY_CACHE
    path = Path(Config.RESOURCES_DIR) / "taxonomy.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            _TAXONOMY_CACHE = json.load(fh)
    except (OSError, ValueError):
        _TAXONOMY_CACHE = {"schema_version": 0, "media_types": [], "model_profiles": []}
    return _TAXONOMY_CACHE


def media_types() -> list[dict]:
    return load_taxonomy().get("media_types", [])


def dimensions_of(media_type: str) -> list[dict]:
    for mt in media_types():
        if mt["key"] == media_type:
            return mt.get("dimensions", [])
    return []


def dimension_keys(media_type: str) -> list[str]:
    return [d["key"] for d in dimensions_of(media_type)]


def dimension_label(media_type: str, dimension: str) -> str:
    for d in dimensions_of(media_type):
        if d["key"] == dimension:
            return d["label"]
    return dimension


def subcategory_label(media_type: str, dimension: str, subcategory: str) -> str:
    if not subcategory:
        return ""
    for d in dimensions_of(media_type):
        if d["key"] == dimension:
            for s in d.get("subcategories", []):
                if s["key"] == subcategory:
                    return s["label"]
    return subcategory


def model_profiles() -> list[dict]:
    return load_taxonomy().get("model_profiles", [])


def profile_keys() -> list[str]:
    return [p["key"] for p in model_profiles()]


def get_profile(key: str) -> dict:
    for p in model_profiles():
        if p["key"] == key:
            return p
    return {
        "key": "generic",
        "label": "通用",
        "supports_negative": False,
        "supports_weight": False,
        "separator": "，",
        "style": "natural",
    }


# 维度关键词映射（确定性、可维护；不追求覆盖率，宁缺勿错）
_IMAGE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("negative", ("不要", "避免", "无 ", "没有", "排除", "no ", "without", "avoid", "negative", "低质量", "模糊", "畸形", "多余的手指", "extra fingers", "blurry", "watermark", " deformed", "bad anatomy")),
    ("typography", ("文字", "标题", "排版", "字体", "logo", "text ", "typography", "版式", "字幕")),
    ("mood", ("氛围", "情绪", "宁静", "孤寂", "温暖", "压抑", "梦幻", "moody", "atmosphere", "cozy", "tense")),
    ("style", ("风格", "写实", "插画", "动漫", "水彩", "油画", "赛博朋克", "极简", "illustration", "anime", "watercolor", "oil painting", "minimalist", "cinematic", " photorealistic", "photorealistic")),
    ("color", ("色调", "配色", "冷暖", "饱和", "单色", "color palette", "monochrome", "pastel", "teal and orange", "color grading")),
    ("lighting", ("光", "逆光", "侧光", "轮廓光", "柔光", "阴影", "丁达尔", "backlit", "rim light", "soft light", "golden hour", "chiaroscuro", "volumetric")),
    ("camera", ("镜头", "景别", "特写", "远景", "广角", "长焦", "俯视", "仰视", "低角度", "close-up", "wide shot", "low angle", "35mm", "85mm", "depth of field", "bokeh")),
    ("composition", ("构图", "三分", "对称", "留白", "引导线", "框架", "rule of thirds", "symmetry", "centered", "leading lines", "foreground")),
    ("environment", ("场景", "环境", "背景", "街道", "森林", "海边", "室内", "雨夜", "雪地", "城市", "background", "street", "forest", "interior", "cityscape", "alley")),
    ("clothing", ("服装", "材质", "丝绸", "皮革", "金属", "布料", "配饰", "clothing", "fabric", "silk", "leather", "armor", "texture")),
    ("action", ("动作", "奔跑", "跳跃", "挥手", "转身", "回望", "伸手", "running", "jumping", "waving", "turning", "walking")),
    ("pose", ("姿势", "站姿", "坐姿", "侧身", "重心", "双手", "肩膀", "pose", "standing", "sitting", "leaning", "posture")),
    ("subject", ("女孩", "少年", "老人", "角色", "人物", "猫", "狗", "机械", "建筑", "a woman", "a man", "a girl", "portrait of", "character")),
]

_VIDEO_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("negative", ("不要", "避免", "无抖动", "排除", "no ", "without", "avoid", "no flicker", "no morphing")),
    ("duration", ("时长", "秒", "循环", "loop", "seamless", "duration", "seconds")),
    ("transition", ("转场", "切", "淡入", "淡出", "叠化", "cut to", "fade", "dissolve", "match cut")),
    ("style", ("风格", "写实", "动画", "胶片", "黑白", "cinematic", "anime", "documentary")),
    ("lighting_color", ("光线", "光影", "色调", "夜色", "逆光", "lighting", "color grade", "sunset", "neon")),
    ("focus", ("焦点", "景深", "变焦", "虚化", "focus pull", "rack focus", "bokeh")),
    ("shot", ("景别", "机位", "特写", "全景", "低机位", "俯拍", "close-up", "wide shot", "overhead", "pov")),
    ("camera_move", ("镜头", "推", "拉", "摇", "移", "跟拍", "环绕", "手持", "dolly", "pan", "tracking", "orbit", "handheld", "crane")),
    ("environment", ("环境变化", "天气", "下雨", "下雪", "日出", "季节", "weather", "rain", "snow", "sunrise")),
    ("performance", ("表情", "表演", "微笑", "眼神", "emotion", "expression", "smile", "gaze")),
    ("tempo", ("速度", "节奏", "慢动作", "加速", "匀速", "slow motion", "time-lapse", "rhythm", "fast cut")),
    ("action", ("动作", "行走", "奔跑", "转身", "挥手", "飘动", "walking", "running", "turning", "flowing")),
    ("subject", ("人物", "主体", "角色", "动物", "车辆", "a person", "a woman", "a man", "subject")),
]

_MUSIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("exclude", ("排除", "不要", "避免", "无 ", "no ", "without", "avoid")),
    ("era", ("年代", "复古", "80", "90", "地域", "city pop", "retro", "vintage", "lo-fi")),
    ("mix", ("混音", "混响", "空间感", "动态", "压缩", "母带", "reverb", "mix", "mastering", "stereo")),
    ("arrangement", ("编曲", "层次", "织体", "副旋律", "铺底", "arrangement", "layer", "pad", "counter-melody")),
    ("instrument", ("乐器", "钢琴", "吉他", "贝斯", "鼓", "弦乐", "合成器", "音色", "piano", "guitar", "bass", "drums", "strings", "synth")),
    ("lyric", ("歌词", "语言", "中文", "英文", "主题", "lyrics", "chinese", "english", "theme")),
    ("vocal", ("人声", "女声", "男声", "合唱", "说唱", "气声", "戏腔", "真声", "vocal", "female", "male", "choir", "rap", "falsetto")),
    ("structure", ("段落", "主歌", "副歌", "前奏", "间奏", "尾奏", "bridge", "verse", "chorus", "intro", "outro")),
    ("harmony", ("调性", "和声", "大调", "小调", "和弦", "major", "minor", "chord", "progression")),
    ("groove", ("节拍", "律动", "拍号", "切分", "摇摆", "four on the floor", "syncopation", "swing", "4/4", "3/4")),
    ("tempo", ("速度", "bpm", "慢板", "中速", "快节奏", "bpm", "slow", "uptempo", "mid-tempo")),
    ("emotion", ("情绪", "温柔", "忧伤", "激昂", "治愈", "孤独", "emotional", "melancholic", "uplifting", "warm")),
    ("genre", ("曲风", "流行", "电子", "摇滚", "民谣", "爵士", "古典", "r&b", "pop", "electronic", "rock", "folk", "jazz", "classical")),
]

_RULES_BY_MEDIA = {"image": _IMAGE_RULES, "video": _VIDEO_RULES, "music": _MUSIC_RULES}

# 有内容价值的实词信号，用于质量加分
_DETAIL_HINTS = (
    "逆光",
    "侧光",
    "轮廓光",
    "景深",
    "材质",
    "构图",
    "镜头",
    "色调",
    "氛围",
    "质感",
    "层次",
    "细节",
    "rim light",
    "depth of field",
    "composition",
    "texture",
    "layering",
)


def classify_dimension(segment: str, media_type: str) -> tuple[str, float]:
    """按关键词映射归类片段到维度，返回 ``(dimension, confidence)``。

    置信度是确定性的启发式值：命中关键词数量越多越高，未命中返回兜底维度与低置信度。
    """
    norm = normalize_text(segment)
    rules = _RULES_BY_MEDIA.get(media_type, _IMAGE_RULES)
    valid = dimension_keys(media_type)
    best_key, best_score = "", 0
    for key, keywords in rules:
        if key not in valid:
            continue
        hits = sum(1 for kw in keywords if kw in norm)
        if hits > best_score:
            best_key, best_score = key, hits
    if not best_key:
        return (valid[-1] if valid else "subject"), 0.30
    confidence = min(0.95, 0.55 + 0.15 * (best_score - 1) + 0.05 * min(len(norm), 40) / 40)
    return best_key, round(confidence, 3)


def has_content_value(segment: str) -> bool:
    """片段是否具备可独立复用的内容价值。"""
    if is_stop_phrase(segment) or is_quality_slogan(segment) or is_run_parameter(segment):
        return False
    length = len(segment.strip())
    if length < _MIN_SEGMENT or length > _MAX_SEGMENT:
        return False
    # 纯 ASCII 且只有停用词/连接词时不收录
    if _EN_STOP_ONLY.match(segment or ""):
        tokens = [t for t in re.split(r"[,\s]+", normalize_text(segment)) if t]
        meaningful = [t for t in tokens if t not in _STOP_EXACT and len(t) > 2]
        if len(meaningful) < 1:
            return False
    return True


def quality_score(segment: str, dimension: str = "") -> int:
    """确定性的质量评分（0-100）。

    评分维度：长度适中、包含具体描述词、包含专业细节信号、风险与噪声惩罚。
    """
    if not segment:
        return 0
    norm = normalize_text(segment)
    score = 50

    length = len(segment.strip())
    if 8 <= length <= 60:
        score += 15
    elif 6 <= length <= 80:
        score += 8
    else:
        score -= 12

    if any(hint in norm for hint in _DETAIL_HINTS):
        score += 12
    # 含具体名词/动词信号：以维度关键词命中为依据
    rules = _RULES_BY_MEDIA.get(_media_of_dimension(dimension), _IMAGE_RULES)
    for _key, keywords in rules:
        if any(kw in norm for kw in keywords):
            score += 8
            break
    if dimension == "negative":
        score += 6
    if is_quality_slogan(segment):
        score -= 55
    if is_run_parameter(segment):
        score -= 60
    if risk_flags(segment):
        score -= 25
    if len(norm.split()) == 1 and len(norm) <= 6:
        score -= 10
    return max(0, min(100, score))


def _media_of_dimension(dimension: str) -> str:
    for media in ("image", "video", "music"):
        if dimension in dimension_keys(media):
            return media
    return "image"


# ---------------------------------------------------------------- 近似重复


def _tokens(text: str) -> set[str]:
    norm = normalize_text(text)
    if not norm:
        return set()
    cjk = [ch for ch in norm if "\u4e00" <= ch <= "\u9fff"]
    if cjk:
        # 中文使用字符二元组，兼顾局部顺序
        grams = {norm[i : i + 2] for i in range(len(norm) - 1)}
        grams.update(cjk)
        return grams
    return {t for t in re.split(r"[^\w]+", norm) if t}


def similarity(a: str, b: str) -> float:
    """确定性相似度（Jaccard），用于近似重复判断。取值 0-1。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return round(inter / union, 4) if union else 0.0


NEAR_DUPLICATE_THRESHOLD = 0.72


def is_near_duplicate(a: str, b: str) -> bool:
    return similarity(a, b) >= NEAR_DUPLICATE_THRESHOLD


# ---------------------------------------------------------------- 插入文本


def join_text(existing: str, addition: str, profile_key: str = "generic") -> tuple[str, bool]:
    """按模型档案把片段插入已有提示词，返回 ``(新文本, 是否重复未插入)``。"""
    addition = (addition or "").strip()
    if not addition:
        return existing, True
    profile = get_profile(profile_key)
    separator = profile.get("separator") or "，"
    existing = existing or ""
    if not existing.strip():
        return addition, False
    if normalize_text(addition) in normalize_text(existing):
        return existing, True
    if normalize_text(existing).endswith(normalize_text(addition)):
        return existing, True
    style = profile.get("style", "natural")
    if style == "tags":
        left = existing.rstrip(" ,，")
        return f"{left}{separator}{addition}", False
    if style == "natural":
        left = existing.rstrip()
        if left and left[-1] in ".!?。！？":
            return f"{left} {addition}", False
        return f"{left}{separator}{addition}", False
    return f"{existing}{separator}{addition}", False


@lru_cache(maxsize=1)
def _cached_taxonomy_signature() -> str:
    return str((Path(Config.RESOURCES_DIR) / "taxonomy.json").exists())


__all__ = [
    "sanitize_model_text",
    "normalize_text",
    "content_fingerprint",
    "split_segments",
    "is_stop_phrase",
    "is_quality_slogan",
    "is_run_parameter",
    "risk_flags",
    "load_taxonomy",
    "media_types",
    "dimensions_of",
    "dimension_keys",
    "dimension_label",
    "subcategory_label",
    "model_profiles",
    "profile_keys",
    "get_profile",
    "classify_dimension",
    "has_content_value",
    "quality_score",
    "similarity",
    "is_near_duplicate",
    "join_text",
    "NEAR_DUPLICATE_THRESHOLD",
]
