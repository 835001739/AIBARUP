"""M12 分镜工作流「人物一致性」测试：角色卡 + 锚点注入 + 确定性种子 + 路由。

覆盖：
- ``comic/characters.py``：名字归一、别名拆分、候选抽取、锚点构造、种子派生；
- ``comic/service.py`` 角色卡 CRUD：去重、上限、只增不覆盖；
- ``generate_storyboard`` 把同一个角色的**逐字相同锚点**注入到每一页，且种子可复现；
- ``/api/comic/projects/<id>/characters`` 系列路由与抽取接口。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import characters
from comic import routes as comic_routes
from comic import service as comic_service
from comic import storyboard


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "comic_outputs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")
    monkeypatch.setenv("AI_PROVIDER_ENABLED", "false")

    core_db._local.conn = None
    core_db.migrate()

    yield {"data": data_dir}

    conn = getattr(core_db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    core_db._local.conn = None


@pytest.fixture
def client(env):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(comic_routes.bp)
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------- 纯函数


def test_normalize_name_trims_and_limits():
    assert characters.normalize_name("  阿岚  ") == "阿岚"
    assert characters.normalize_name("「林修」") == "林修"
    assert len(characters.normalize_name("名" * 100)) <= 24


def test_parse_and_join_aliases():
    assert characters.parse_aliases("小岚, 岚儿、阿岚 | 岚") == ["小岚", "岚儿", "阿岚", "岚"]
    assert characters.parse_aliases("") == []
    assert characters.join_aliases(["a", "b"]) == "a, b"


def test_extract_candidates_finds_titles_and_speakers():
    text = "少年阿岚握紧长刀。阿岚说道：我一定会回来。少女苏叶望向远方，骑士沉默不语。"
    names = characters.extract_candidates(text)
    assert "阿岚" in names
    assert "苏叶" in names
    assert "骑士" in names


def test_extract_candidates_strips_verbs_and_titles():
    """回归：不得产出「阿岚说」「少女苏叶」这类带着粘连成分的噪声名。"""
    names = characters.extract_candidates("少年阿岚说道：出发。少女苏叶点头。")
    assert "阿岚" in names and "苏叶" in names
    assert not [n for n in names if n.endswith("说") or n.endswith("点")]
    assert not [n for n in names if len(n) > 4]


def test_extract_candidates_drops_absorbed_title():
    """「少年阿岚」里的「少年」只是身份描述，不应单独成角。"""
    names = characters.extract_candidates("少年阿岚握紧长刀。阿岚登上飞艇。")
    assert "阿岚" in names
    assert "少年" not in names


def test_extract_candidates_filters_common_words():
    """「骑士沉默不语」里的「沉默」是动作不是人名。"""
    names = characters.extract_candidates("骑士沉默不语，少年时期的城市忽然安静。")
    assert "骑士" in names
    assert "沉默" not in names
    assert not [n for n in names if "的" in n]


def test_extract_candidates_limit_and_dedupe():
    text = "。".join("角色%d说道：出发。" % i for i in range(50))
    assert len(characters.extract_candidates(text, limit=5)) <= 5


def test_build_character_anchor_is_deterministic():
    c = {
        "name": "阿岚",
        "appearance": "黑色短发，琥珀色眼睛",
        "outfit": "靛蓝长袍，银色护腕",
        "palette": "靛蓝与银白",
    }
    a1 = characters.build_character_anchor(c)
    a2 = characters.build_character_anchor(c)
    assert a1 == a2
    assert a1.startswith("阿岚")
    assert "黑色短发" in a1 and "靛蓝长袍" in a1 and "靛蓝与银白" in a1
    # 顺序固定：名字 → 外貌 → 服装 → 配色
    assert a1.index("黑色短发") < a1.index("靛蓝长袍") < a1.index("靛蓝与银白")


def test_build_character_anchor_name_only():
    assert characters.build_character_anchor({"name": "苏叶"}) == "苏叶"


def test_build_character_anchor_respects_max_len():
    c = {"name": "阿岚", "appearance": "长" * 400, "outfit": "衣" * 400}
    assert len(characters.build_character_anchor(c, max_len=120)) <= 120


def test_extract_catches_narrative_sentences():
    """「阿岚离开家乡」「苏叶在城门口」这类纯叙事句也要抓得到人名（不只认「XX说」）。"""
    names = characters.extract_candidates(
        "阿岚离开家乡，走进雾气弥漫的森林。苏叶在城门口等他。"
    )
    assert "阿岚" in names
    assert "苏叶" in names


def test_extract_ignores_abstract_and_function_words():
    """加了更多叙事动词后，噪声句仍应被挡住（这是放宽匹配的代价控制点）。"""
    assert characters.extract_candidates("现在天色已晚，有人敲响了门。") == []
    assert characters.extract_candidates("他感到一阵眩晕，记忆像潮水涌来。") == []
    assert characters.extract_candidates("他没有回答，只是沉默地握紧了剑柄。") == []


def test_extract_does_not_glue_verb_after_title():
    """「引路人递给他一枚齿轮」不能抽出「引路人递」——称谓后紧跟动词只是叙事承接。"""
    names = characters.extract_candidates("引路人递给他一枚齿轮。")
    assert names == ["引路人"]


def test_extract_strips_title_before_name():
    """「少年阿岚握紧剑柄，少女苏叶站在一旁」应只留下真实人名。"""
    names = characters.extract_candidates("少年阿岚握紧了剑柄，少女苏叶站在他身旁。")
    assert "阿岚" in names and "苏叶" in names
    assert "少年" not in names and "少女" not in names


def test_extract_ranks_first_appearance_when_tied():
    """同频次时先登场的排前面：第一个角色卡默认主角，按出场顺序才符合直觉。

    这里「引路人」多命中一次称谓规则，若只按频次 + 长度排会抢在主角「阿岚」前面。
    """
    text = (
        "第1集 启程\n阿岚离开家乡，走进雾气弥漫的森林。\n\n"
        "第2集 觉醒\n引路人递给他一枚齿轮。阿岚感到力量在胸口翻涌。"
    )
    names = characters.extract_candidates(text)
    assert names[0] == "阿岚", "主角应当排在第一位，实际：%s" % names
    assert set(names) == {"阿岚", "引路人"}


def test_anchor_key_is_order_independent():
    a = [{"name": "阿岚"}, {"name": "苏叶"}]
    b = [{"name": "苏叶"}, {"name": "阿岚"}]
    assert characters.anchor_key(a) == characters.anchor_key(b)
    assert characters.anchor_key([]) == ""


def test_match_characters_by_name_and_alias():
    chars = [
        {"name": "阿岚", "aliases": "小岚, 岚儿"},
        {"name": "苏叶", "aliases": ""},
    ]
    assert [c["name"] for c in characters.match_characters("小岚抬头看向天空", chars)] == ["阿岚"]
    assert [c["name"] for c in characters.match_characters("苏叶转身", chars)] == ["苏叶"]
    assert characters.match_characters("无人出现的空镜头", chars) == []


def test_character_negative_merges():
    chars = [
        {"name": "阿岚", "negative": "不要金色头发"},
        {"name": "苏叶", "negative": "不要短发"},
    ]
    neg = characters.character_negative(chars)
    assert "不要金色头发" in neg and "不要短发" in neg


def test_setting_prefix_uses_first_sentence():
    assert characters.setting_prefix("蒸汽朋克浮空城。少年踏上旅程。") == "蒸汽朋克浮空城"
    assert characters.setting_prefix("") == ""


def test_compose_page_prompt_order():
    """人物锚点必须排在最前：模型对靠前的描述权重更高。

    注意此处曾断言「世界观设定在锚点之前」，那与人物一致性目标相悖——
    设定每页都一样、信息量低，却占着权重最高的位置，等于把人物特征挤到后面。
    现在顺序为：锚点 → 一致性指令 → 世界观设定 → 画面描述 → 镜头景别。
    """
    out = characters.compose_page_prompt(
        "少年离开家乡",
        [{"name": "阿岚", "appearance": "黑发"}],
        shot="medium shot",
        setting="浮空城",
    )
    assert out.index("阿岚") < out.index(characters.CONSISTENCY_DIRECTIVE)
    assert out.index(characters.CONSISTENCY_DIRECTIVE) < out.index("浮空城")
    assert out.index("浮空城") < out.index("少年离开家乡")
    assert "medium shot" in out


def test_compose_page_prompt_skips_duplicated_name():
    out = characters.compose_page_prompt("阿岚抬头", [{"name": "阿岚"}], setting="")
    assert out.count("阿岚") == 1  # 场景里已有名字，不再重复注入


def test_derive_seed_is_deterministic_and_bounded():
    s1 = characters.derive_seed(12345, 1, 2, 3, "阿岚")
    s2 = characters.derive_seed(12345, 1, 2, 3, "阿岚")
    s3 = characters.derive_seed(12345, 1, 2, 4, "阿岚")
    assert s1 == s2 and s1 != s3
    assert 0 <= s1 < 2 ** 31
    assert characters.derive_seed(12345, 1, 2, 3, "") != s1  # 角色组合不同 → 种子不同


def test_default_base_seed_stable():
    assert characters.default_base_seed(7) == characters.default_base_seed(7)
    assert characters.default_base_seed(7) != characters.default_base_seed(8)


# ---------------------------------------------------------------- 角色卡 CRUD


def test_character_crud(env):
    p = comic_service.create_project({"name": "角色卡 CRUD"})
    c = comic_service.create_character(
        p["id"], {"name": "阿岚", "aliases": "小岚", "appearance": "黑发", "palette": "靛蓝"}
    )
    assert c["name"] == "阿岚"
    assert comic_service.get_character(c["id"])["aliases"] == "小岚"

    comic_service.update_character(c["id"], {"outfit": "长袍"})
    assert comic_service.get_character(c["id"])["outfit"] == "长袍"

    comic_service.delete_character(c["id"])
    assert comic_service.list_characters(p["id"]) == []


def test_character_duplicate_rejected(env):
    p = comic_service.create_project({"name": "重名校验"})
    comic_service.create_character(p["id"], {"name": "阿岚"})
    with pytest.raises(Exception):
        comic_service.create_character(p["id"], {"name": "阿岚"})


def test_character_limit_enforced(env):
    p = comic_service.create_project({"name": "角色上限"})
    for i in range(characters.MAX_CHARACTERS):
        comic_service.create_character(p["id"], {"name": "角色%d" % i})
    with pytest.raises(Exception):
        comic_service.create_character(p["id"], {"name": "超编角色"})


def test_sync_characters_only_adds_missing(env):
    p = comic_service.create_project({"name": "增量抽取"})
    comic_service.create_character(p["id"], {"name": "阿岚", "appearance": "我手写的外貌"})
    res = comic_service.sync_characters(p["id"], "少年阿岚说道：出发。少女苏叶望向海面。")
    assert res["created"] == 1  # 只新建苏叶
    assert "苏叶" in res["candidates"]

    # 已存在的卡片不被覆盖
    ak = [c for c in comic_service.list_characters(p["id"]) if c["name"] == "阿岚"][0]
    assert ak["appearance"] == "我手写的外貌"


def test_delete_project_cascades_characters(env):
    p = comic_service.create_project({"name": "级联删除"})
    comic_service.create_character(p["id"], {"name": "阿岚"})
    comic_service.delete_project(p["id"])
    # 项目本身已不存在，直接查库确认角色卡被级联清理
    rows = core_db.query_all("SELECT id FROM comic_characters WHERE project_id=?", (p["id"],))
    assert rows == []


# ---------------------------------------------------------------- 分镜注入一致性


def test_storyboard_injects_identical_anchor_across_pages(env):
    p = comic_service.create_project({"name": "一致性分镜", "pages_per_chapter": 3})
    comic_service.create_character(
        p["id"],
        {"name": "阿岚", "appearance": "黑色短发，琥珀色眼睛", "outfit": "靛蓝长袍"},
    )
    res = storyboard.generate_storyboard(
        p["id"],
        worldview="浮空城的世界观。",
        plot_summary="第1集：阿岚离开家乡，阿岚在港口与苏叶告别。阿岚登上飞艇。",
        pages_per_chapter=3,
        style="none",
    )
    assert res["pages"] >= 2

    pages = []
    for ch in comic_service.list_chapters(p["id"]):
        pages.extend(comic_service.list_pages(ch["id"]))

    anchor = characters.build_character_anchor(
        [c for c in comic_service.list_characters(p["id"]) if c["name"] == "阿岚"][0]
    )
    matched = [pg for pg in pages if "阿岚" in (pg["character_names"] or "")]
    assert matched, "应当有页面匹配到角色阿岚"
    for pg in matched:
        assert anchor in pg["prompt_text"], "同一角色的锚点必须逐字相同地出现"


def test_storyboard_prompt_has_no_dangling_punctuation(env):
    """回归：「第1集：少年…」里的冒号不能残留成「锚点，：少年…」。"""
    p = comic_service.create_project({"name": "标点清洗", "pages_per_chapter": 2})
    comic_service.create_character(p["id"], {"name": "阿岚", "appearance": "黑发"})
    storyboard.generate_storyboard(
        p["id"],
        worldview="浮空城的世界。",
        plot_summary="第1集：少年阿岚在码头送别少女苏叶。阿岚握紧长刀。",
        pages_per_chapter=2,
    )
    for ch in comic_service.list_chapters(p["id"]):
        for pg in comic_service.list_pages(ch["id"]):
            assert "，：" not in pg["prompt_text"]
            assert not pg["prompt_text"].strip().startswith("：")


def test_storyboard_seeds_are_deterministic_and_unique(env):
    p = comic_service.create_project({"name": "种子可复现", "pages_per_chapter": 2})
    first = storyboard.generate_storyboard(
        p["id"], plot_summary="阿岚说道：出发。苏叶回答：等你回来。", pages_per_chapter=2
    )
    seeds_first = _all_seeds(p["id"])
    assert len(seeds_first) == len(set(seeds_first)), "同一批内每页种子应互不相同"
    assert first["base_seed"]

    second = storyboard.generate_storyboard(
        p["id"], plot_summary="阿岚说道：出发。苏叶回答：等你回来。", pages_per_chapter=2
    )
    # base_seed 固化后，重跑得到的种子集合应与上次完全重合（可复现）
    assert second["base_seed"] == first["base_seed"]
    assert set(_all_seeds(p["id"])) >= set(seeds_first)


def _all_seeds(project_id: int) -> list:
    seeds = []
    for ch in comic_service.list_chapters(project_id):
        for pg in comic_service.list_pages(ch["id"]):
            if pg.get("seed") is not None:
                seeds.append(int(pg["seed"]))
    return seeds


# ---------------------------------------------------------------- 路由


def test_characters_routes(client, env):
    created = client.post(
        "/api/comic/projects", json={"name": "角色路由"}
    ).get_json()["data"]
    pid = created["id"]

    res = client.post(
        "/api/comic/projects/%d/characters" % pid,
        json={"name": "阿岚", "appearance": "黑发", "outfit": "长袍", "palette": "靛蓝"},
    ).get_json()
    assert res["ok"] is True
    cid = res["data"]["id"]

    listed = client.get("/api/comic/projects/%d/characters" % pid).get_json()["data"]
    assert len(listed["items"]) == 1
    assert listed["items"][0]["anchor"].startswith("阿岚")

    patched = client.patch(
        "/api/comic/characters/%d" % cid, json={"outfit": "铠甲"}
    ).get_json()
    assert patched["data"]["outfit"] == "铠甲"

    assert client.delete("/api/comic/characters/%d" % cid).get_json()["ok"] is True
    assert client.get("/api/comic/projects/%d/characters" % pid).get_json()["data"]["items"] == []


def test_characters_extract_route(client, env):
    pid = client.post("/api/comic/projects", json={"name": "抽取路由"}).get_json()["data"]["id"]
    client.patch(
        "/api/comic/projects/%d" % pid,
        json={"worldview": "浮空城", "plot_summary": "少年阿岚说道：我走了。少女苏叶点头。"},
    )
    res = client.post(
        "/api/comic/projects/%d/characters/extract" % pid, json={}
    ).get_json()
    assert res["ok"] is True
    names = [c["name"] for c in res["data"]["characters"]]
    assert "阿岚" in names and "苏叶" in names


def test_page_editor_link_route(client, env):
    pid = client.post("/api/comic/projects", json={"name": "深链"}).get_json()["data"]["id"]
    ch = client.post(
        "/api/comic/projects/%d/chapters" % pid, json={"title": "第1集"}
    ).get_json()["data"]
    pg = client.post(
        "/api/comic/chapters/%d/pages" % ch["id"],
        json={"title": "页1", "prompt_text": "阿岚抬头", "workflow_filename": "flux_dev.json"},
    ).get_json()["data"]

    res = client.get("/api/comic/pages/%d/editor-link" % pg["id"]).get_json()
    assert res["ok"] is True
    assert res["data"]["url"]


def test_page_editor_link_requires_workflow(client, env):
    pid = client.post("/api/comic/projects", json={"name": "无工作流"}).get_json()["data"]["id"]
    ch = client.post(
        "/api/comic/projects/%d/chapters" % pid, json={"title": "第1集"}
    ).get_json()["data"]
    pg = client.post(
        "/api/comic/chapters/%d/pages" % ch["id"], json={"title": "页1", "prompt_text": "x"}
    ).get_json()["data"]
    res = client.get("/api/comic/pages/%d/editor-link" % pg["id"]).get_json()
    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_input"


# ---------------------------------------------------------------- 人物一致性加固


def test_has_identity_requires_appearance_or_outfit_or_palette():
    """只有名字的角色卡没有身份信息——注入它只会稀释有效锚点。"""
    assert characters.has_identity({"name": "电影"}) is False
    assert characters.has_identity({"name": "登场", "appearance": ""}) is False
    assert characters.has_identity({"name": "阿岚", "appearance": "黑发"}) is True
    assert characters.has_identity({"name": "阿岚", "outfit": "青衫"}) is True
    assert characters.has_identity({"name": "阿岚", "palette": "靛蓝"}) is True
    assert characters.has_identity({}) is False
    assert characters.has_identity(None) is False


def test_pick_anchors_drops_name_only_noise_when_strong_exists():
    """规则抽取产出的「电影 / 登场」这类空卡，在有真实角色时被丢掉。"""
    chars = [
        {"id": 1, "name": "电影"},
        {"id": 2, "name": "阿岚", "appearance": "黑发", "is_main": 1},
        {"id": 3, "name": "登场"},
    ]
    picked = characters._pick_anchors(chars, "他走进城门")
    assert [c["name"] for c in picked] == ["阿岚"]


def test_pick_anchors_falls_back_to_name_only_when_no_identity():
    """整页都没有带身份的角色时，保留名字（孤例也比全空好）。"""
    chars = [{"id": 1, "name": "电影"}, {"id": 2, "name": "登场"}]
    picked = characters._pick_anchors(chars, "镜头缓缓推近")
    assert [c["name"] for c in picked] == ["电影", "登场"]


def test_pick_anchors_puts_main_first_and_caps_count():
    """主角占权重最高的前排；锚点数量超限时限流，避免互相稀释。"""
    chars = [
        {"id": 1, "name": "路人甲", "appearance": "A"},
        {"id": 2, "name": "主角", "appearance": "B", "is_main": 1},
        {"id": 3, "name": "路人乙", "appearance": "C"},
    ]
    assert [c["name"] for c in characters._pick_anchors(chars, "")] == ["主角", "路人甲", "路人乙"]

    many = [{"id": i, "name": "角色%d" % i, "appearance": "x"} for i in range(8)]
    picked = characters._pick_anchors(many, "")
    assert len(picked) == characters.MAX_ANCHORS_PER_PAGE


def test_compose_prompt_skips_noise_anchor():
    out = characters.compose_page_prompt(
        "他走进城门",
        [{"name": "电影"}, {"name": "阿岚", "appearance": "黑发"}],
        setting="浮空城",
    )
    assert "电影" not in out
    assert "阿岚，黑发" in out


def test_compose_prompt_appends_consistency_directive_only_with_identity():
    """有身份信息才追加一致性指令；纯噪声角色页不该凭空挂一句要求。"""
    strong = characters.compose_page_prompt("x", [{"name": "阿岚", "appearance": "黑发"}])
    assert characters.CONSISTENCY_DIRECTIVE in strong

    weak = characters.compose_page_prompt("x", [{"name": "电影"}])
    assert characters.CONSISTENCY_DIRECTIVE not in weak


def test_identity_key_includes_seed_offset():
    """seed_offset 此前是只存不用的死字段，现在真正参与身份派生。"""
    assert characters.identity_key([{"name": "阿岚", "seed_offset": 0}]) != characters.identity_key(
        [{"name": "阿岚", "seed_offset": 1}]
    )
    # 顺序无关：角色组合相同则键相同
    a = characters.identity_key([{"name": "阿岚"}, {"name": "苏叶"}])
    b = characters.identity_key([{"name": "苏叶"}, {"name": "阿岚"}])
    assert a == b
    assert characters.identity_key([]) == ""


def test_compose_page_texts_merges_anti_drift_negative(env):
    """每一页的负面提示词都要带上「反人物漂移」排除项。"""
    pos, neg, _seed = storyboard.compose_page_texts(
        "少年离开家乡", "", "（近景）", [{"name": "阿岚", "appearance": "黑发"}],
        "浮空城", "none", 123, 1, 1, 1,
    )
    assert characters.ANTI_DRIFT_NEGATIVE in neg
    assert "阿岚" in pos


def test_compose_page_texts_keeps_anchor_when_prompt_is_huge(env):
    """超长提示词不得把锚点砍掉——这是截断方向反转前最致命的一致性漏洞。"""
    long_scene = "画面细节" * 1200  # 远超 MAX_PROMPT_LEN(2000)
    pos, _neg, _seed = storyboard.compose_page_texts(
        long_scene, "", "", [{"name": "阿岚", "appearance": "黑发"}],
        "浮空城", "shinkai", 123, 1, 1, 1,
    )
    assert len(pos) <= storyboard.MAX_PROMPT_LEN
    # 锚点必须在，且仍在最前；被牺牲的只能是尾部画面细节
    assert pos.index("阿岚") == 0
    assert characters.CONSISTENCY_DIRECTIVE in pos
    assert "浮空城" in pos


def test_compose_page_texts_seed_uses_identity_key(env):
    """改 seed_offset 应当换种子（否则这个字段对用户毫无意义）。"""
    base = dict(base_prompt="x", base_negative="", shot="", setting="", style="none",
                base_seed=123, project_id=1, chapter_id=1, page_idx=1)
    s0 = storyboard.compose_page_texts(chars=[{"name": "阿岚", "seed_offset": 0}], **base)[2]
    s1 = storyboard.compose_page_texts(chars=[{"name": "阿岚", "seed_offset": 5}], **base)[2]
    assert s0 != s1
    # 同样输入必须稳定复现
    assert s0 == storyboard.compose_page_texts(chars=[{"name": "阿岚", "seed_offset": 0}], **base)[2]


# --- 源头噪声过滤（_KNOWN_NOISE_NAMES + 姓氏校验 + 窗口收紧） -------------------


def test_known_noise_names_are_rejected():
    """剧本中反复误识别的「叙事片段」必须在抽取阶段被拦掉，不能进 12 张配额。"""
    # 紧跟动词尾被切成 3-4 字(首字恰好是常见姓：林/秦/阿/俯，姓氏校验拦不住)
    for n in ("林砚徒", "秦峰遇", "阿岚登上", "俯瞰万千", "仑玄王墓",
              "宝者尽数", "质登山靴", "鸟图腾暗", "十七岁独"):
        assert characters._clean_name(n, require_surname=True) == "", (
            "应当被拦掉: %s" % n)
    # 风景 / 物件 / 动作短语(姓氏校验可拦，这里是双保险)
    for n in ("电影", "登场", "半透", "风沙", "星河", "远处沙丘", "残垣矗"):
        assert characters._clean_name(n, require_surname=True) == "", (
            "应当被拦掉: %s" % n)


def test_known_noise_does_not_block_title_words():
    """「引路人」是合法 _TITLE_WORD：单独出现时也应被允许成角。"""
    assert characters._clean_name("引路人", require_surname=False) == "引路人"


def test_surname_filter_blocks_non_names():
    """姓氏校验：2-3 字首字不在常见姓表里，视为叙事片段丢弃。"""
    for n in ("风雨", "风沙", "星河", "静静", "电影", "登场", "远处沙丘"):
        assert characters._clean_name(n, require_surname=True) == "", n


def test_surname_filter_allows_real_names():
    """姓氏校验：常见姓开头的 2-3 字真名必须放行（含「阿」等文学常用姓）。"""
    for n in ("林砚", "苏叶", "秦峰", "钟伯", "王嬷", "小满", "阿岩",
              "柳如烟", "苏利耶", "欧阳锋", "司马懿"):
        out = characters._clean_name(n, require_surname=True)
        assert out, "应当放行: %s" % n


def test_extract_candidates_drops_production_noise():
    """端到端：剧情文本若含线上观察到的高频噪声，必须一个不进候选。"""
    text = (
        "少年林砚徒穿过浮空城。秦峰遇见柳如烟。电影在放映。"
        "远处沙丘风沙起，残垣矗立于视野。林砚徒告别师父，俯瞰万千风景。"
    )
    names = characters.extract_candidates(text)
    # 全部噪声名都不能进
    for n in ("林砚徒", "秦峰遇", "电影", "远处沙丘", "风沙", "残垣矗", "俯瞰万千"):
        assert n not in names, "噪声卡泄漏: %s -> %s" % (n, names)
    # 真名仍然能进
    assert "林砚" in names, "真名被误杀: %s" % names


def test_speech_re_window_tightened_to_2_3():
    """``_SPEECH_RE`` 窗口从 {2,4} 收紧到 {2,3}，避免 4 字叙事被误当人名。"""
    src = characters._SPEECH_RE.pattern
    # 不应再出现 {2,4} 之类的 4 字窗口
    assert "{2,4}" not in src
    # 应当是 {2,3}（2-3 字窗口）
    assert "{2,3}" in src


def test_build_character_anchor_strips_trailing_punct():
    """角色卡的 outfit/palette 若自带尾随逗号，拼接时不能产生 "，，"。"""
    char = {
        "name": "秦峰",
        "appearance": "二十五岁帅气男性",
        "outfit": "迷彩探险大衣，",   # 注意尾随中文逗号
        "palette": "冷灰，迷彩，",   # 尾随中文逗号
    }
    anchor = characters.build_character_anchor(char)
    # 关键回归断言：不能出现 "，，"
    assert "，，，" not in anchor
    assert "，，" not in anchor, "双逗号回归: %s" % anchor
    # 字段应当全部在场（顺序：名 → 外貌 → 服装 → 配色）
    assert anchor.startswith("秦峰，")
    assert "迷彩探险大衣" in anchor
    assert "配色冷灰" in anchor
    # 不应保留尾随逗号后的内容（这里没有）


def test_build_character_anchor_idempotent():
    """同一角色卡无论何时调用，锚点必须逐字相同（人物一致性的关键）。"""
    char = {
        "name": "苏叶",
        "appearance": "二十岁女剑客，",
        "outfit": "青衫，",
        "palette": "青白，",
    }
    a1 = characters.build_character_anchor(char)
    a2 = characters.build_character_anchor(char)
    assert a1 == a2
    # 即使卡内字段尾随标点被剥掉，输出也必须稳定
    assert "，，，" not in a1
    assert "，，" not in a1

