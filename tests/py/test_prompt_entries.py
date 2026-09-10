"""M7 词条服务测试：种子幂等、种子规模与内容合规、分面无空层级、检索/收藏/排序/分页、回填去重。

隔离策略（HARNESS §7）：数据落在 ``tmp_path`` 下，monkeypatch ``Config`` 后重置
``core.db`` 的线程本地连接，不污染仓库里的 ``data/``。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db
from core import textutil
from core.db import query_scalar
from core.errors import AIBARError
from promptlib import entries
from promptlib.routes import bp


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与运行期目录重定向到临时目录；``RESOURCES_DIR`` 保持真实值以便读取种子。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")

    # core.db 用线程本地连接，切换 DB_PATH 后必须丢弃旧连接
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
    """只注册 promptlib 蓝图，不依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    with app.test_client() as test_client:
        yield test_client


# 模板外壳：整段套话不属于可复用片段（PRD M7.1）
_TEMPLATE_SHELLS = (
    "请生成",
    "帮我生成",
    "生成一张",
    "生成一段",
    "这是一张",
    "这是一个",
    "这是一段",
    "画面中",
    "图片中",
    "视频中",
    "非常好看",
    "很好看",
)


def _seed_path() -> Path:
    return Path(Config.RESOURCES_DIR) / entries.SEED_FILENAME


def _seed_items() -> list[dict]:
    return json.loads(_seed_path().read_text(encoding="utf-8"))["entries"]


# ---------------------------------------------------------------- 内置种子


def test_seed_from_resources_is_idempotent(env):
    """重复导入不新增数据：全部命中内容指纹被跳过。"""
    first = entries.seed_from_resources()

    assert first["total"] >= 150
    assert first["inserted"] == first["total"]
    assert first["invalid"] == 0
    assert first["skipped_existing"] == 0
    total_after_first = query_scalar("SELECT COUNT(*) FROM prompt_entries", (), 0)
    assert total_after_first == first["total"]

    second = entries.seed_from_resources()

    assert second["inserted"] == 0
    assert second["skipped_existing"] == first["total"]
    assert query_scalar("SELECT COUNT(*) FROM prompt_entries", (), 0) == total_after_first


def test_seed_does_not_overwrite_user_edits(env):
    """重新导入不覆盖用户改过的标题，也不重置收藏状态。"""
    entries.seed_from_resources()
    item = entries.list_entries({"media_type": "image", "page_size": 1})["items"][0]
    entries.favorite(item["id"], True)
    entries.update_entry(item["id"], {"title": "我的自定义标题"})

    entries.seed_from_resources()

    reloaded = entries.get_entry(item["id"])
    assert reloaded["title"] == "我的自定义标题"
    assert reloaded["is_favorite"] is True
    assert reloaded["source_type"] == "builtin"


def test_seed_covers_every_media_and_dimension():
    """总量、分媒体下限，以及每种媒体每个维度至少 3 条。"""
    items = _seed_items()
    assert len(items) >= 150

    by_media = Counter(item["media_type"] for item in items)
    assert by_media["image"] >= 60
    assert by_media["video"] >= 50
    assert by_media["music"] >= 40

    for media in textutil.media_types():
        per_dimension = Counter(
            item["dimension"] for item in items if item["media_type"] == media["key"]
        )
        for dimension in media["dimensions"]:
            key = dimension["key"]
            assert per_dimension[key] >= 3, f'{media["key"]}.{key} 只有 {per_dimension[key]} 条种子'


def test_seed_content_is_independently_reusable():
    """每条种子都是可直接复用的语义片段：无模板外壳、无口号、无运行参数、无风险内容。"""
    for item in _seed_items():
        text = item["prompt_text"]
        assert 4 <= len(text) <= 80, item["id"]
        assert not any(shell in text for shell in _TEMPLATE_SHELLS), item["id"]
        assert not textutil.is_stop_phrase(text), item["id"]
        assert not textutil.is_quality_slogan(text), item["id"]
        assert not textutil.is_run_parameter(text), item["id"]
        assert not textutil.risk_flags(text), item["id"]
        assert textutil.has_content_value(text), item["id"]
        assert item["source_type"] == "builtin", item["id"]
        assert 0 <= int(item["quality_score"]) <= 100, item["id"]
        allowed = entries.subcategory_keys(item["media_type"], item["dimension"])
        assert item["subcategory"] in allowed, item["id"]


# ---------------------------------------------------------------- 分面与检索


def test_facets_never_return_empty_levels(env):
    """分面只返回仍有数据的选项；当前所选层级自身不会被自己的条件挤掉。"""
    entries.seed_from_resources()

    facets = entries.get_facets({"media_type": "image", "dimension": "lighting"})

    # 媒体类型是一级入口，即使无数据也要返回
    assert {item["key"] for item in facets["media_types"]} == set(entries.media_type_keys())
    for key in ("dimensions", "subcategories", "tags", "profiles", "sources"):
        assert facets[key], key
        assert all(int(option["count"]) > 0 for option in facets[key]), key

    # 选了 lighting 之后，同层级的其他维度仍然可选（skip 掉自身条件）
    dimension_keys = {option["key"] for option in facets["dimensions"]}
    assert "lighting" in dimension_keys
    assert dimension_keys == set(textutil.dimension_keys("image"))
    # 子类层级返回当前维度下有数据的子类
    assert {option["key"] for option in facets["subcategories"]} == {"direction", "quality", "effect"}


def test_search_hits_title_prompt_and_tags(env):
    """关键词覆盖 title / prompt_text，标签按 JSON 数组精确匹配。"""
    entries.seed_from_resources()

    by_title = entries.list_entries({"q": "雨夜巷道", "page_size": 100})
    assert by_title["total"] == 1
    assert by_title["items"][0]["title"] == "雨夜巷道"

    by_tag = entries.list_entries({"media_type": "image", "tag": "夜景", "page_size": 100})
    assert by_tag["total"] >= 1
    assert all("夜景" in item["tags"] for item in by_tag["items"])

    assert entries.list_entries({"q": "这个词条一定不存在", "page_size": 10})["total"] == 0


def test_favorite_filter_and_toggle(env):
    """收藏过滤只返回已收藏词条，取消后不再出现。"""
    entries.seed_from_resources()
    target = entries.list_entries({"media_type": "music", "page_size": 1})["items"][0]

    entries.favorite(target["id"], True)
    favorites = entries.list_entries({"favorite": True, "page_size": 100})
    assert favorites["total"] == 1
    assert favorites["items"][0]["id"] == target["id"]

    entries.favorite(target["id"], False)
    assert entries.list_entries({"favorite": True, "page_size": 100})["total"] == 0


def test_sorting_and_paging_are_deterministic(env):
    """排序以 id 兜底，分页不重不漏；page_size 超过 100 直接报错。"""
    entries.seed_from_resources()
    total = entries.list_entries({"page_size": 1})["total"]

    first = entries.list_entries({"sort": "recommended", "page": 1, "page_size": 20})
    second = entries.list_entries({"sort": "recommended", "page": 2, "page_size": 20})

    assert first["total"] == total
    assert first["has_more"] is True
    assert len(first["items"]) == 20
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}

    scores = [item["quality_score"] for item in first["items"]]
    assert scores == sorted(scores, reverse=True)

    # 同一请求重复执行结果完全一致
    assert first["items"] == entries.list_entries({"sort": "recommended", "page": 1, "page_size": 20})["items"]

    with pytest.raises(AIBARError) as error:
        entries.list_entries({"page_size": entries.MAX_PAGE_SIZE + 1})
    assert error.value.code == "invalid_input"

    with pytest.raises(AIBARError) as error:
        entries.list_entries({"sort": "hot"})
    assert error.value.code == "invalid_input"


# ---------------------------------------------------------------- 回填与删除


def test_record_use_appends_and_detects_duplicate(env):
    """回填按模型档案拼接；重复片段不插入也不累计使用次数。"""
    item = entries.create_entry(
        {
            "media_type": "image",
            "dimension": "lighting",
            "subcategory": "effect",
            "title": "窗边柔光",
            "prompt_text": "窗边漫射的柔和自然光",
            "tags": ["光效"],
        }
    )
    entry_id = item["id"]

    first = entries.record_use(entry_id, "generic", "逆光形成金色轮廓光")
    assert first["duplicate"] is False
    assert first["insert_text"] == "逆光形成金色轮廓光，窗边漫射的柔和自然光"
    assert entries.get_entry(entry_id)["use_count"] == 1

    second = entries.record_use(entry_id, "generic", first["insert_text"])
    assert second["duplicate"] is True
    assert second["insert_text"] == first["insert_text"]
    assert entries.get_entry(entry_id)["use_count"] == 1

    tagged = entries.record_use(entry_id, "sd15_sdxl", "rim light")
    assert tagged["separator"] == ", "
    assert tagged["insert_text"] == "rim light, 窗边漫射的柔和自然光"


def test_delete_builtin_hides_and_ignores_fingerprint(env):
    """内置词条删除表现为隐藏并写入忽略指纹；手动词条物理删除。"""
    entries.seed_from_resources()
    builtin = entries.list_entries({"media_type": "image", "source": "builtin", "page_size": 1})["items"][0]

    result = entries.delete_entry(builtin["id"])

    assert result["deleted"] is False
    assert result["hidden"] is True
    assert query_scalar("SELECT is_hidden FROM prompt_entries WHERE id = ?", (builtin["id"],)) == 1
    assert entries.is_ignored_fingerprint(
        textutil.content_fingerprint("image", builtin["dimension"], builtin["prompt_text"])
    )
    with pytest.raises(AIBARError) as error:
        entries.get_entry(builtin["id"])
    assert error.value.status == 404

    # 被忽略的指纹不会再被种子重新导入
    reseed = entries.seed_from_resources()
    assert reseed["inserted"] == 0
    assert reseed["skipped_ignored"] >= 1

    manual = entries.create_entry(
        {"media_type": "image", "dimension": "mood", "prompt_text": "湿润空气中的静默张力"}
    )
    assert entries.delete_entry(manual["id"])["deleted"] is True
    assert query_scalar("SELECT COUNT(*) FROM prompt_entries WHERE id = ?", (manual["id"],), 0) == 0


def test_entry_items_expose_source_label(env):
    """列表项带上可直接展示的来源标签。"""
    entries.seed_from_resources()
    item = entries.list_entries({"page_size": 1})["items"][0]
    assert item["source_type"] == "builtin"
    assert item["source_label"] == "内置"
    assert item["dimension_label"] == textutil.dimension_label(item["media_type"], item["dimension"])


# ---------------------------------------------------------------- 路由


def test_routes_smoke_main_endpoints(client, env):
    """M7 主接口冒烟：统一 envelope、入参校验、404 处理。"""
    entries.seed_from_resources()

    facets = client.get("/api/prompt-library/facets?media_type=image").get_json()
    assert facets["ok"] is True
    assert facets["data"]["media_types"]
    assert facets["data"]["dimensions"]

    listing = client.get("/api/prompt-library/entries?media_type=image&page_size=10").get_json()
    assert listing["data"]["total"] >= 60
    assert len(listing["data"]["items"]) == 10

    created = client.post(
        "/api/prompt-library/entries",
        json={
            "media_type": "image",
            "dimension": "mood",
            "subcategory": "emotion",
            "title": "静默张力",
            "prompt_text": "湿润空气中的静默张力",
        },
    ).get_json()
    assert created["ok"] is True
    entry_id = created["data"]["id"]

    patched = client.patch(f"/api/prompt-library/entries/{entry_id}", json={"title": "改过的标题"}).get_json()
    assert patched["data"]["title"] == "改过的标题"

    favored = client.put(f"/api/prompt-library/entries/{entry_id}/favorite", json={"favorite": True}).get_json()
    assert favored["data"]["is_favorite"] is True

    used = client.post(
        f"/api/prompt-library/entries/{entry_id}/use",
        json={"profile": "generic", "existing": "黄昏最后一缕光的静谧"},
    ).get_json()
    assert used["data"]["duplicate"] is False
    assert used["data"]["insert_text"].startswith("黄昏最后一缕光的静谧")

    deleted = client.delete(f"/api/prompt-library/entries/{entry_id}").get_json()
    assert deleted["data"]["deleted"] is True

    oversized = client.get("/api/prompt-library/entries?page_size=101").get_json()
    assert oversized["ok"] is False
    assert oversized["error"]["code"] == "invalid_input"

    unknown = client.get("/api/prompt-library/entries?media_type=photo").get_json()
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "invalid_input"

    missing = client.put("/api/prompt-library/entries/999999/favorite", json={"favorite": True}).get_json()
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"
