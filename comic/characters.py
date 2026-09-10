"""M12 · 人物一致性：角色卡（Character Card）抽取、锚点组装与确定性种子。

解决漫画分镜最容易翻车的问题——**同一个人每一页长得都不一样**。做法是给每个角色建一张
「角色卡」，生成分镜时把**完全相同的一段锚点描述**注入到每一页提示词里，并让页级种子
由角色确定性派生，从而跨页、跨集保持稳定。

设计约束（沿用 HARNESS / PRD 风格）：
- **纯规则、可离线**：角色抽取不依赖 LLM；``AI_PROVIDER_*`` 未启用时完全可用；
- **绝不抛异常**：任何非法输入都收敛为空/默认值；
- **幂等**：重复抽取不会产生重复角色卡；
- **用户可覆盖**：自动抽取只是起点，角色卡可增删改，且**手填的字段永不被自动覆盖**。

典型链路::

    chars = list_characters(project_id)           # 没有则 extract 后落库
    hit   = match_characters(beat_text, chars)    # 这一页出现了谁
    prompt = compose_page_prompt(beat_text, hit, shot="（近景）")
    seed  = derive_seed(base_seed, project_id, chapter_id, page_idx, anchor_key(hit))
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_CHARACTERS = 12  # 单部漫画角色卡上限（防止长篇把提示词撑爆）
MAX_ANCHOR_LEN = 220  # 单页角色锚点描述的最大长度

# 单页最多注入几个角色锚点。规则抽取常产出「电影 / 登场」这类噪声角色卡，
# 一页挂 6 个锚点时每段描述都被稀释，模型反而一个都记不住 —— 宁可少而准。
MAX_ANCHORS_PER_PAGE = 3

# 一致性指令：紧跟角色锚点之后，明确要求模型维持同一张脸 / 发型 / 服装。
# 纯文本条件下这是唯一能直接「命令」模型保持身份的手段，缺了它模型只会把
# 锚点当成画面里的一段普通描述。
CONSISTENCY_DIRECTIVE = "同一角色在所有画面中保持一致的脸型、发型与服装"

# 反漂移排除项：合并进每一页的负面提示词，压掉最常见的人物走形。
ANTI_DRIFT_NEGATIVE = (
    "different face, inconsistent character design, changing hairstyle, "
    "changing outfit, face swap, blended faces, multiple identities"
)

# 种子上限：ComfyUI 的 seed/noise_seed 通常要求 < 2^32，这里取 2^31-1 稳妥
_SEED_MODULUS = 2**31 - 1

# 常见角色称谓 / 身份词：剧本里出现即视为一个角色候选
_TITLE_WORDS = [
    "少年", "少女", "青年", "老人", "老者", "男子", "女子", "男人", "女人",
    "男孩", "女孩", "孩子", "骑士", "剑客", "法师", "术士", "巫女", "祭司",
    "公主", "王子", "国王", "王后", "将军", "士兵", "商人", "旅人", "旅者",
    "引路人", "导师", "师父", "徒弟", "医生", "护士", "警察", "侦探", "杀手",
    "船长", "水手", "铁匠", "农夫", "猎人", "精灵", "妖精", "巨龙", "狼人",
    "吸血鬼", "机器人", "少女兵", "少女", "少年兵", "同伴", "伙伴", "队长",
]

# 代词 / 泛指词：抽取人名时必须排除，否则「他说」「他们」会被当成角色
_STOP_WORDS = {
    "他们", "她们", "它们", "大家", "众人", "有人", "有人", "别人", "彼此",
    "这个", "那个", "这样", "那样", "什么", "怎么", "为什么", "于是", "忽然",
    "突然", "此时", "此刻", "这时", "那里", "这里", "一个", "两个", "所有",
    "我们", "你们", "自己", "对方", "周围", "远处", "近处", "空中", "地面",
}

# 高频「非人名」双字词：常紧跟在称谓之后（「骑士沉默不语」），规则很容易误当成名字。
# 这是纯规则抽取的主要噪声来源，用一张小而准的黑名单拦掉。
_NON_NAME_WORDS = {
    "沉默", "微笑", "点头", "摇头", "说话", "回答", "回应", "出现", "消失",
    "离开", "回来", "转身", "抬头", "低头", "弯腰", "闭上", "睁开", "举起",
    "放下", "握住", "松开", "走开", "跑开", "停下", "站起", "坐下", "睡去",
    "醒来", "哭泣", "大笑", "皱眉", "叹息", "自语", "低语", "呢喃", "回望",
    "遥望", "凝望", "注视", "观察", "思考", "犹豫", "决定", "想起", "忘记",
    "知道", "明白", "发现", "感觉", "以为", "认为", "开始", "继续", "准备",
    "之中", "之下", "之上", "之间", "一样", "一起", "一直", "仍旧", "依然",
    "已经", "还是", "或许", "也许", "慢慢", "渐渐", "连忙", "立刻", "马上",
    "用力", "轻轻", "缓缓", "悄悄", "独自", "两人", "二人", "数人", "一名",
}

# 漫画剧本纯规则抽取反复误识别的「叙事片段」(从线上数据反推收集)；
# 与 ``_NON_NAME_WORDS`` 不同，这里收的是**整体候选名**(如「林砚徒」= 真名「林砚」+ 动词尾)，
# 漏桶最后一层兜底，避免污染 12 张角色卡配额。
_KNOWN_NOISE_NAMES = {
    # 紧跟动词尾被切成 3-4 字(首字恰好是常见姓：林/秦/阿/俯，姓氏校验拦不住，必须显式列)
    "林砚徒", "秦峰遇", "阿岚登上", "俯瞰万千", "仑玄王墓", "宝者尽数",
    "荒淫无", "覆大众固", "质登山靴", "鸟图腾暗", "十七岁独",
    # 风景 / 物件 / 动作短语被误当人名(姓氏校验可拦，这里是双保险)
    "半透", "古实习", "圣火灵台", "残垣矗", "电影", "登场",
    "腻沙粒质", "远处沙丘", "静静", "风沙", "风雨", "星河", "仅存",
    # 2-3 字常见词被误识(首字恰好是常见姓：周/林/石/万/叶/朱/金/星)
    "周遭", "周重新", "林砚终", "叶随风", "朱砂作", "金色光", "星图", "万珍非",
    "石板墓", "石刃横", "石像同", "石壁暗", "石壁挤", "石壁骤",
    # 注：「引路人」是合法 _TITLE_WORD（无后接名字时也允许成角），不进黑名单。
}

# 常见中文姓氏(单字 Top 100 + 复姓 8)：用于人名可能性校验。
# 漫画人名 2-3 字时首字 95%+ 是常见姓氏；不命中则视为叙事片段丢弃。
# 剧本原创人物可使用常见/文学姓；4 字名罕见，不在考虑范围。
_COMMON_SURNAMES_1 = set(
    "王李张刘陈杨黄赵周吴徐孙朱马胡郭林何高梁郑罗宋谢唐韩冯于董萧程"
    "曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石"
    "姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武"
    "乔贺赖龚文"
    # 罕见但剧本/文学常用姓氏(阿Q式虚构；小/墨/柳/星见 project 17 真实数据)
    "阿小墨柳星"
)
_COMPOUND_SURNAMES = {
    "欧阳", "司马", "诸葛", "上官", "皇甫", "尉迟", "慕容", "东方", "独孤",
    "南宫", "宇文", "长孙", "司空", "端木", "令狐",
}

# 「阿岚离开家乡」「苏叶抬起头」式人名捕获：2~4 个汉字，后接言说 / 叙事动词。
# 只认「说话动词」会漏掉大量叙事句（「阿岚走进森林」里的人名抓不到），
# 因此这里收了常见言说 + 叙事动词的**首字**，覆盖面更广；误伤由后面的
# ``_clean_name`` 用 ``_NAME_FORBIDDEN`` 兜住（含动词字的候选会被判为误抽取）。
_SPEECH_VERB_CHARS = set(
    # 言说 / 表情：阿岚说道 / 苏叶微笑
    "说道问答喊叫呼唤叹念吟唱骂祝谢批评赞美"
    # 视线 / 头部动作：她望向窗外 / 他抬起头
    "望看瞧盯瞄瞥见视观测注视抬头低点摇回扭侧"
    # 身体 / 移动：阿岚离开家乡 / 少年走进森林
    "站走跑奔步行冲退跨迈踏跳跃蹲趴跪躺倒跌摔爬游飞穿越追逃躲藏"
    # 手部动作：她握紧拳头 / 他拔出长剑 / 引路人递给他一枚齿轮
    "握拉推举抬拿放拔插挥拍摸抱扶牵扯穿戴脱系解翻掀敲打攻击挡救"
    "递送收抛扔捡撕折揉捏拧按压撞碰触抚掩遮挡塞装填倒洒泼浇烧熄"
    # 心理 / 状态：他想起了往事 / 少女发现真相
    "想思念懂明白觉感知道认为发沉默犹豫决忘"
    # 其它高频叙事动词
    "离到抵达进去出返停始继续等找救伤害杀喝吃吞呼吸喘哭笑皱眉"
    # 高频叙事虚词 / 介词：「苏叶在城门口等他」「他把信收进怀里」里的人名靠它们切出来。
    # 这些字同时也在 ``_TAIL_NOISE`` 里，含它们的候选（「现在」「有人」）会被 _clean_name 拒掉。
    "在是有于为把被将使对向给从用"
)

_SPEECH_RE = re.compile(
    r"([\u4e00-\u9fff]{2,3})(?=[" + "".join(sorted(_SPEECH_VERB_CHARS)) + r"])"
)

# 常见「非人名」字：助词 / 量词 / 方位 / 高频虚词。出现在候选名里即判为误抽取，
# 例如「少年时期的城市」会被规则截成「时期的城」，含「的」→ 直接丢弃。
_TAIL_NOISE = set(
    # 助词 / 虚词 / 量词：出现在人名里几乎一定是误抽取
    "的了着过得地是与在和跟对被把让给从向到里中后前时却又就才很太还再"
    "一不没们这那谁都也只更最并而或但个些种次件条张片点位分秒其之如若"
    # 常见动作字：规则会把「阿岚握紧」截成「阿岚握」，尾巴要削掉
    "握拿放拉推开关闭睁举松走跑跳飞落升降倒挂穿戴脱吃喝听念怕哭笑怒惊"
    "忙进退去回出入住停等找寻追逃战斗攻击防守躲藏紧慢轻缓悄站坐睡醒"
    "说话问答喊叫望看向转抬低微皱眉沉默点摇叹息凝注观察思考豫决忘记"
    "道明白现感觉以为认准继续准备完毕结束成完起生死亡活站立坐趴跪"
)

# 候选名里**任意位置**都不允许出现的字：高频助词/量词/方位/虚词 + 标点。
# 注意：``_TAIL_NOISE`` 里也有不少「常出现在人名里」的文学字（如/若/玉/月...），
# 那些**只能在尾部**裁剪（_strip_name），不能放进 _NAME_FORBIDDEN，否则
# 「柳如烟」「苏如意」这类真名会被整体误杀。
_NAME_FORBIDDEN = {
    # 助词 / 虚词 / 量词
    "的了着过得地是与在和跟对被把让给从向到里中后前时却又就才很太还再",
    # 常用单字虚词
    "一不没们这那谁都也只更最并而或但个些种次件条张片点位分秒其之",
    # 强叙事动词（任何位置出现都几乎一定是误抽）
    "说话问答喊叫望看向转抬低微皱眉沉默点摇叹息凝注观察思考豫决忘",
    "道明白现感觉以为认准继续准备完毕结束成完起生死亡活站立坐趴跪",
    # 标点
    "，。！？；：、“”‘’《》",
}
# 拆成单字集合
_NAME_FORBIDDEN = set("".join(_NAME_FORBIDDEN)) | _SPEECH_VERB_CHARS

# 英文人名（首字母大写），排除常见非人名英文词
_EN_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z]{2,15})\b")
_EN_STOP = {
    "The", "This", "That", "Then", "There", "They", "When", "What", "Where",
    "Which", "While", "With", "Into", "From", "Chapter", "Episode", "Page",
    "But", "And", "For", "His", "Her", "She", "Him", "Its", "Not", "All",
}


# ---------------------------------------------------------------- 工具


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_name(value: Any) -> str:
    """角色名归一化：去空白、去包裹符号、限长。"""
    name = _s(value)
    name = re.sub(r"[《》〈〉「」『』（）()【】\[\]“”\"'’‘:：,，。！？!?；;]", "", name)
    return name.strip()[:24]


def parse_aliases(value: Any) -> list[str]:
    """把别名串解析为列表（支持中英文逗号 / 顿号 / 竖线分隔）。"""
    raw = _s(value)
    if not raw:
        return []
    parts = re.split(r"[,，、|/]+", raw)
    out: list[str] = []
    for p in parts:
        n = normalize_name(p)
        if n and n not in out:
            out.append(n)
    return out


def join_aliases(items: list[str] | None) -> str:
    return ", ".join([normalize_name(i) for i in (items or []) if normalize_name(i)])


# ---------------------------------------------------------------- 角色抽取（规则优先）


def _strip_name(name: str) -> str:
    """把候选名里的「粘连成分」剥掉，只留下真正的人名。

    - 尾部的动词 / 虚词：正则贪婪匹配会把「阿岚说道」截成「阿岚说」、「少女苏叶点头」
      截成「苏叶点」，需要把尾巴削掉；
    - 头部的称谓前缀：「少女苏叶」里真正的名字是「苏叶」，「少年」只是身份描述。
    """
    n = name
    while len(n) > 2 and n[-1] in (_TAIL_NOISE | _SPEECH_VERB_CHARS):
        n = n[:-1]
    for title in _TITLE_WORDS:
        if len(n) > len(title) + 1 and n.startswith(title):
            n = n[len(title):]
            break
    return n


def _looks_like_name(name: str) -> bool:
    """人名可能性校验：2-3 字候选必须以常见姓氏开头，否则视为叙事片段。

    这是纯规则抽取的**源头闸门**——``_SPEECH_RE`` 的 2-4 字窗口会抓到「秦峰遇」「林砚徒」
    这类「真名 + 动词尾」短语，也会抓到「电影」「风沙」这种 2 字普通名词。它们的共同
    特征是首字不在常见姓氏表里。姓氏表覆盖 95%+ 人口，对漫画原创人物(常见/文学姓)
    也基本够用。复姓单独处理；4 字名极罕见且容易误抽，不放行。
    """
    if not name:
        return False
    n = len(name)
    if n in (2, 3):
        if name[:2] in _COMPOUND_SURNAMES:
            return True
        return name[0] in _COMMON_SURNAMES_1
    if n == 4:
        return name[:2] in _COMPOUND_SURNAMES
    return False


def _clean_name(name: str, *, require_surname: bool = True) -> str:
    """归一 + 剥离粘连 + 合法性校验；不合法的候选返回空串（被丢弃）。

    ``require_surname=True`` 时（叙事/英文正则抽出的路径），2-3 字候选必须以常见
    姓氏开头；称谓词典路径会传 ``False`` 跳过此校验，因为「骑士/旅人/引路人」等
    本身就不带姓氏、却是合法角色类别。
    """
    n = _strip_name(normalize_name(name))
    if not (2 <= len(n) <= 4):
        return ""
    if any(ch in _NAME_FORBIDDEN for ch in n):
        return ""
    if n in _STOP_WORDS or n in _NON_NAME_WORDS:
        return ""
    if n in _KNOWN_NOISE_NAMES:
        return ""
    if require_surname and not _looks_like_name(n):
        return ""
    return n


def extract_candidates(*texts: str, limit: int = MAX_CHARACTERS) -> list[str]:
    """从世界观 / 剧情摘要中抽取角色名候选（按出现频次降序，去重）。

    三条互补规则：
    1. 称谓词典直击（``少年`` / ``引路人`` 等）；
    2. 「XX说 / XX道 / XX问」式人名捕获（过滤代词与泛指词）；
    3. 英文首字母大写人名（过滤常见非人名英文词）。

    4. 「称谓 + 人名」组合捕获：「少年阿岚」「少女苏叶」里真正的名字在称谓之后。

    后处理（决定抽取质量的关键）：
    - ``_clean_name`` 剥掉粘连的动词与称谓前缀，避免产出「阿岚说」「少女苏叶」这类噪声；
    - 若某个称谓后面确实跟着人名（「少年阿岚」→「阿岚」），则丢掉裸称谓「少年」，
      只保留完整人名，避免同一个角色被拆成两张卡片。
    """
    blob = "\n".join([t or "" for t in texts])
    if not blob.strip():
        return []

    counts: dict[str, int] = {}
    first_pos: dict[str, int] = {}  # 候选名首次出场位置（决定谁排第一 = 默认主角）
    absorbed: set[str] = set()  # 已被「称谓 + 人名」吸收的裸称谓
    _FAR = 1 << 30

    def _bump(name: str, pos: int = -1, *, require_surname: bool = True) -> None:
        n = _clean_name(name, require_surname=require_surname)
        if not n:
            return
        counts[n] = counts.get(n, 0) + 1
        if 0 <= pos < first_pos.get(n, _FAR):
            first_pos[n] = pos

    for word in _TITLE_WORDS:
        if word not in blob:
            continue
        # 称谓（少年/骑士/引路人…）自身允许单独成角，**不要求姓氏开头**；
        # 紧跟其后的 2~3 字才是真名（要求姓氏），所以下面 derived 仍走默认校验。
        _bump(word, blob.find(word), require_surname=False)
        # 称谓后面紧跟的 2~3 个汉字多半就是人名：「少年阿岚」→「阿岚」。
        # 但若紧跟的是动词（「引路人递给他」→「递给他」），说明这只是叙事承接、
        # 不是人名，必须跳过，否则会抽出「引路人递」这种不存在的角色。
        for m in re.finditer(re.escape(word) + r"([\u4e00-\u9fff]{2,3})", blob):
            if m.group(1)[0] in _SPEECH_VERB_CHARS:
                continue
            tail = m.group(1)
            # 贪婪 3 字可能是「真名+动词尾」(林砚徒)被 _KNOWN_NOISE_NAMES 拦掉，
            # 此时回退到 2 字窗再试一次，捞回真名「林砚」。
            for width in (3, 2):
                if len(tail) < width:
                    continue
                derived = _clean_name(word + tail[:width])
                if derived and derived != _clean_name(word):
                    absorbed.add(word)
                    _bump(derived, m.start())
                    break
    for m in _SPEECH_RE.finditer(blob):
        _bump(m.group(1), m.start())
    for m in _EN_NAME_RE.finditer(blob):
        if m.group(1) not in _EN_STOP:
            _bump(m.group(1), m.start())

    # 全称里已含该称谓时，裸称谓不再单独成角（「少年」属于「少年阿岚」）
    for title in absorbed:
        counts.pop(title, None)

    # 排序：出现频次 → 首次出场位置 → 名字长度 → 字典序。
    # 把「首次出场位置」放在频次之后，是为了让同频次时**先登场的排在前面**：
    # 第一个被创建的角色卡默认成为主角，按出场顺序排才符合「主角先登场」的直觉
    # （否则「引路人」会因为多命中一次规则而抢在主角「阿岚」前面）。
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], first_pos.get(kv[0], _FAR), -len(kv[0]), kv[0]),
    )
    return [name for name, _c in ranked[: max(1, int(limit))]]


def extract_characters(
    worldview: str | None,
    plot_summary: str | None,
    existing: list[dict] | None = None,
    limit: int = MAX_CHARACTERS,
) -> list[dict]:
    """抽取角色并生成角色卡草稿：已存在的角色按名/别名去重，返回**待新增**的角色卡列表。"""
    have: set[str] = set()
    for row in existing or []:
        have.add(normalize_name(row.get("name")).lower())
        for a in parse_aliases(row.get("aliases")):
            have.add(a.lower())

    drafts: list[dict] = []
    for name in extract_candidates(worldview or "", plot_summary or "", limit=limit):
        if name.lower() in have:
            continue
        if len(drafts) + len(have) >= MAX_CHARACTERS:
            break
        drafts.append(
            {
                "name": name,
                "aliases": "",
                "appearance": "",
                "outfit": "",
                "palette": "",
                "negative": "",
                "seed_offset": len(drafts),
                "source": "auto",
            }
        )
    return drafts


# ---------------------------------------------------------------- 角色锚点（一致性核心）


def _strip_trailing_punct(s: str) -> str:
    """剥掉尾随的标点(，/。/；/：/、/空格)，避免与 join 的 "，" 拼出 "，，"。"""
    return s.rstrip("，。；：、 \t\r\n")


def build_character_anchor(char: dict | None, max_len: int = MAX_ANCHOR_LEN) -> str:
    """把角色卡拼成一段**稳定不变**的锚点描述，注入到每一页提示词里。

    只拼非空字段，顺序固定（名字 → 外貌 → 服装 → 配色），因此同一角色在任何一页
    得到的锚点文本**逐字相同**，这是人物一致性的关键。
    """
    if not char:
        return ""
    parts = [normalize_name(char.get("name"))]
    for key, suffix in (("appearance", ""), ("outfit", ""), ("palette", "配色")):
        val = _strip_trailing_punct(_s(char.get(key)))
        if not val:
            continue
        parts.append(("%s%s" % (suffix, val)) if suffix else val)
    anchor = "，".join([p for p in parts if p])
    return anchor[:max_len]


def anchor_key(chars: list[dict] | None) -> str:
    """参与种子派生的角色键：多角色时用排序后的名字拼接，保证角色组合相同则种子相同。"""
    names = sorted({normalize_name(c.get("name")).lower() for c in (chars or [])})
    return "|".join(names)


def has_identity(char: dict | None) -> bool:
    """角色是否带「可辨识的身份信息」：外貌 / 服装 / 配色三者至少填了一项。

    只有名字的锚点等于什么都没说——模型拿到「登场」这种噪声名只会更混乱。
    用它来决定谁配占用提示词里宝贵的前排位置。
    """
    if not char:
        return False
    for key in ("appearance", "outfit", "palette"):
        if _s(char.get(key)):
            return True
    return False


def identity_key(chars: list[dict] | None) -> str:
    """参与种子派生的身份键：名字 + ``seed_offset``。

    ``seed_offset`` 此前是一个**只存不用的死字段**——角色卡上改了它，出图毫无变化。
    这里把身份信息真正接进种子派生：同一个角色组合在同一页位置稳定复现同一张图，
    换角色卡也能通过偏移量把不同角色拉开距离，避免几个人共用相近的种子。
    """
    items = []
    for c in chars or []:
        name = normalize_name(c.get("name")).lower()
        try:
            offset = int(c.get("seed_offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        items.append("%s#%d" % (name, offset))
    return "|".join(sorted(items))


def character_negative(chars: list[dict] | None) -> str:
    """汇总角色的排除项（如「不要改变发色」），合并进页面负面提示词。"""
    negs: list[str] = []
    for c in chars or []:
        val = _s(c.get("negative"))
        if val and val not in negs:
            negs.append(val)
    return ", ".join(negs)


def _name_hits(text: str, char: dict) -> bool:
    """判断一段文本是否提到该角色（按名字与别名做子串匹配，单字别名忽略）。"""
    blob = text or ""
    if not blob:
        return False
    for name in [normalize_name(char.get("name"))] + parse_aliases(char.get("aliases")):
        if len(name) >= 2 and name in blob:
            return True
    return False


def match_characters(text: str, characters: list[dict] | None) -> list[dict]:
    """找出一段分镜描述里出现的角色（保持角色卡顺序，便于提示词稳定）。"""
    if not characters:
        return []
    return [c for c in characters if _name_hits(text, c)]


def is_main(char: dict | None) -> bool:
    """角色是否为主角（主角在每一页都注入锚点）。

    数据库存 0/1，也可能因历史数据存成 "1"/"true"，这里统一宽容解析。
    """
    if not char:
        return False
    raw = char.get("is_main")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) == 1
    return _s(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def select_page_characters(
    text: str,
    characters: list[dict] | None,
    include_main: bool = True,
) -> list[dict]:
    """挑选一页要注入锚点的角色：文本命中的角色 **并集** 所有主角。

    纯按文本命中会漏掉「这一页只写『他走进城里』没写名字」的情况，人物特征就丢了一次；
    主角（``is_main``）因此无条件贯穿全部页，保证整本漫画里主角长相始终一致。

    返回顺序恒为角色卡顺序（先主角后配角由卡片顺序决定），保证提示词与种子稳定可复现。
    """
    if not characters:
        return []
    # 用「id 优先、name 兜底」做去重键：内存态的角色卡（测试/预览）可能还没有 id
    def _key(c: dict) -> str:
        cid = c.get("id")
        if cid is not None:
            return "#%s" % cid
        return normalize_name(c.get("name")).lower()

    keys = {_key(c) for c in characters if _name_hits(text, c)}
    if include_main:
        keys |= {_key(c) for c in characters if is_main(c)}
    return [c for c in characters if _key(c) in keys]


def setting_prefix(worldview: Any, max_len: int = 80) -> str:
    """把世界观压缩成一句「设定前缀」，注入每一页提示词，保证全本设定与氛围一致。

    取世界观的第一个句子（中英文句末切分），超长则截断；空世界观返回空串。
    """
    text = _s(worldview)
    if not text:
        return ""
    first = re.split(r"(?<=[。！？!?；;\n])", text)[0].strip()
    if not first:
        first = text.strip()
    first = first.rstrip("。！？!?；;，,、")
    if len(first) > max_len:
        first = first[:max_len].rstrip("，,、；; ") + "……"
    return first


def _pick_anchors(chars: list[dict] | None, scene_text: str = "") -> list[dict]:
    """挑出这一页真正要注入的锚点角色。

    规则（人物一致性的第二道闸门）：

    1. **有身份信息的角色优先**：填了外貌 / 服装 / 配色的角色卡才描述得出长相，
       只有名字的噪声卡（规则抽取常产出「电影」「登场」）在这一页会被直接丢掉；
    2. **主角排在前面**：权重最高的前排位置留给他；
    3. **限流到 ``MAX_ANCHORS_PER_PAGE``**：一页挂太多锚点会互相稀释，谁都记不住。
    """
    pool = [c for c in (chars or []) if c]
    if not pool:
        return []
    strong = [c for c in pool if has_identity(c)]
    if strong:
        # 稳定排序：主角提前，其余保持角色卡顺序（顺序稳定 → 提示词稳定）
        return sorted(strong, key=lambda c: 0 if is_main(c) else 1)[:MAX_ANCHORS_PER_PAGE]
    # 整页都没有带身份的角色：退回原顺序，至少名字还在，但仍要限流
    return pool[:MAX_ANCHORS_PER_PAGE]


def compose_page_prompt(
    scene: str,
    chars: list[dict] | None = None,
    shot: str | None = None,
    setting: str | None = None,
) -> str:
    """组装一页的提示词主体：**角色锚点 → 世界观设定 → 画面描述 → 镜头景别**。

    顺序是人物一致性的关键，改动前务必读完下面三条：

    - **锚点排在最前**：图像模型对提示词靠前的描述权重更高。此前世界观设定抢在锚点
      前面，而设定每页都一样、信息量低，却占着权重最高的位置 —— 等于把人物特征
      挤到后面去了。现在锚点前置，设定退居其次。
    - **锚点之后紧跟一致性指令**（``CONSISTENCY_DIRECTIVE``）：明确要求模型维持同一张
      脸 / 发型 / 服装。没有这句，模型只会把锚点当成画面里一段普通描述。
    - **只有名字的锚点会被丢掉**：前提是这一页还有别的「有身份信息」的角色。规则抽取
      常产出「电影 / 登场」这类噪声角色卡，把它们的空名字塞进提示词只会稀释有效锚点。
      若整页都没有带身份的角色，则保留（孤例也比全空好）。
    - 若角色卡只有名字且名字已出现在场景里，也**不再重复追加**，避免「少年，少年离开家乡」。
    - ``setting`` 为世界观摘要，作为全本统一前缀，进一步保证每一集的设定与氛围一致。
    """
    scene_text = _s(scene)
    parts: list[str] = []
    chosen = _pick_anchors(chars, scene_text)
    for c in chosen:
        anchor = build_character_anchor(c)
        if not anchor:
            continue
        # 纯名字锚点且场景已提到 → 跳过（锚点没有带来任何新信息）
        if anchor == normalize_name(c.get("name")) and anchor and anchor in scene_text:
            continue
        parts.append(anchor)
    # 一致性指令只在真的注入了角色锚点时才追加，避免空镜头也挂一句无意义的要求
    if parts and any(has_identity(c) for c in chosen):
        parts.append(CONSISTENCY_DIRECTIVE)
    setting_text = _s(setting)
    if setting_text:
        parts.append(setting_text)
    if scene_text:
        parts.append(scene_text)
    shot_text = _s(shot)
    if shot_text:
        parts.append(shot_text.strip("（）()"))
    return "，".join([p for p in parts if p])


# ---------------------------------------------------------------- 确定性种子


def derive_seed(
    base_seed: Any,
    project_id: Any,
    chapter_id: Any,
    page_idx: Any,
    key: str = "",
) -> int:
    """由「项目基准种子 + 项目/章节/页序 + 角色键」确定性派生页级种子。

    同一角色组合在同一位置**永远得到同一个种子**，重新生成也保持人物稳定；
    不同页之间由 ``page_idx`` 拉开差异，避免整本图千篇一律。
    """
    material = "%s|%s|%s|%s|%s" % (
        0 if base_seed in (None, "") else base_seed,
        project_id, chapter_id, page_idx, _s(key) or "-",
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % _SEED_MODULUS


def default_base_seed(project_id: Any) -> int:
    """项目基准种子的默认值：由项目 id 确定性生成，保证同一项目每次新建都一致。"""
    return derive_seed(20240901, project_id, 0, 0, "base")


__all__ = [
    "MAX_CHARACTERS",
    "MAX_ANCHOR_LEN",
    "MAX_ANCHORS_PER_PAGE",
    "CONSISTENCY_DIRECTIVE",
    "ANTI_DRIFT_NEGATIVE",
    "normalize_name",
    "parse_aliases",
    "join_aliases",
    "extract_candidates",
    "extract_characters",
    "build_character_anchor",
    "anchor_key",
    "identity_key",
    "has_identity",
    "character_negative",
    "match_characters",
    "is_main",
    "select_page_characters",
    "setting_prefix",
    "compose_page_prompt",
    "derive_seed",
    "default_base_seed",
]
