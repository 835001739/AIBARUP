"""M7 自动学习流水线测试：阈值分流、精确/近似去重、忽略指纹、风险丢弃、生成学习与候选审核。

所有片段的 ``dimension`` 与 ``confidence`` 都显式给出，评分只依赖共享的
``core.textutil.quality_score``，因此结果完全确定、可静态推算。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db
from core import textutil
from core.db import query_all, query_scalar
from core.errors import AIBARError
from promptlib import entries, learning
from promptlib.routes import bp


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与运行期目录重定向到临时目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "DB_PATH", data_dir / "aibar.db")
    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")

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


# 三段质量分（core.textutil.quality_score）分别是 85 / 73 / 28，用于验证阈值分流
HIGH = "逆光形成金色轮廓光"  # 85 分：命中细节信号「逆光」+ 维度关键词
MID = "窗边漫射的柔和自然光"  # 73 分：只命中维度关键词
LOW = "柔和的照明"  # 28 分：过短且无关键词


def _segment(text: str, confidence: float = 0.95, **extra) -> dict:
    payload = {"text": text, "dimension": "lighting", "confidence": confidence}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------- 阈值分流


def test_ingest_routes_by_quality_threshold(env):
    """auto 来源：质量与置信度双达标入库，60–84 进候选，低于 60 丢弃。"""
    result = learning.ingest_segments(
        [_segment(HIGH), _segment(MID), _segment(LOW)],
        "image",
        "auto",
        {"usage_id": 1},
    )

    assert len(result["inserted"]) == 1
    assert len(result["candidates"]) == 1
    assert len(result["discarded"]) == 1
    assert result["discarded"][0] == {"text": LOW, "reason": "low_quality"}
    assert entries.get_entry(result["inserted"][0])["source_type"] == "auto"


def test_auto_needs_confidence(env):
    """质量达标但分类置信度不足时进候选，不进正式词库。"""
    result = learning.ingest_segments([_segment(HIGH, confidence=0.70)], "image", "auto", {"usage_id": 2})

    assert result["inserted"] == []
    assert len(result["candidates"]) == 1
    candidate = query_all("SELECT * FROM prompt_candidates")[0]
    assert candidate["review_status"] == "pending"
    assert candidate["suggested_dimension"] == "lighting"


def test_reverse_metadata_requires_high_quality(env):
    """元数据恢复：质量 ≥ 85 才入库，73 分落到候选区。"""
    high = learning.ingest_segments([_segment(HIGH)], "image", "reverse_metadata", {"job_id": 1})
    assert len(high["inserted"]) == 1
    assert entries.get_entry(high["inserted"][0])["source_type"] == "reverse_metadata"

    mid = learning.ingest_segments([_segment(MID)], "image", "reverse_metadata", {"job_id": 2})
    assert mid["inserted"] == []
    assert len(mid["candidates"]) == 1


def test_reverse_vision_needs_confidence_and_quality(env):
    """视觉反推：置信度 ≥ 0.90 且质量 ≥ 85 才入库。"""
    low_confidence = learning.ingest_segments(
        [_segment(HIGH, confidence=0.88)], "image", "reverse_vision", {"job_id": 3}
    )
    assert low_confidence["inserted"] == []
    assert len(low_confidence["candidates"]) == 1

    qualified = learning.ingest_segments(
        [_segment(HIGH, confidence=0.95)], "image", "reverse_vision", {"job_id": 4}
    )
    assert len(qualified["inserted"]) == 1
    assert entries.get_entry(qualified["inserted"][0])["source_type"] == "reverse_vision"


def test_thresholds_can_be_overridden(env):
    """thresholds 覆盖默认阈值：把候选下限抬到 80 后，73 分片段直接丢弃。"""
    result = learning.ingest_segments(
        [_segment(MID)],
        "image",
        "auto",
        {"usage_id": 5},
        thresholds={"candidate_min_quality": 80},
    )

    assert result["candidates"] == []
    assert result["discarded"][0]["reason"] == "low_quality"


def test_unknown_media_or_source_is_rejected(env):
    """媒体类型与来源类型必须是已知枚举。"""
    with pytest.raises(AIBARError):
        learning.ingest_segments([], "photo", "auto", {})
    with pytest.raises(AIBARError):
        learning.ingest_segments([], "image", "unknown_source", {})
    with pytest.raises(AIBARError):
        learning.ingest_segments("not-a-list", "image", "auto", {})


# ---------------------------------------------------------------- 过滤


def test_noise_and_risk_are_discarded(env):
    """模板外壳、质量口号、运行参数与风险内容全部丢弃，不产生候选。"""
    result = learning.ingest_segments(
        [
            _segment("best quality"),
            _segment("seed: 12345"),
            {"text": "请生成一张好看的图", "dimension": "subject", "confidence": 0.99},
            {"text": "模仿某位艺术家的笔触", "dimension": "style", "confidence": 0.99},
        ],
        "image",
        "auto",
        {"usage_id": 6},
    )

    assert result["inserted"] == []
    assert result["merged"] == []
    assert result["candidates"] == []
    assert [item["reason"] for item in result["discarded"]] == [
        "noise",
        "noise",
        "noise",
        "risk:artist_style",
    ]


def test_too_long_segment_is_discarded(env):
    """整段未拆解的长文本没有内容价值，直接丢弃。"""
    result = learning.ingest_segments([_segment("光" * 120)], "image", "auto", {"usage_id": 7})

    assert result["inserted"] == []
    assert result["discarded"][0]["reason"] == "no_content_value"


# ---------------------------------------------------------------- 去重


def test_exact_duplicate_is_merged(env):
    """完全相同的内容只累计使用记录，不新建词条。"""
    first = learning.ingest_segments([_segment(HIGH)], "image", "auto", {"usage_id": 8})
    entry_id = first["inserted"][0]
    assert entries.get_entry(entry_id)["use_count"] == 0

    second = learning.ingest_segments([_segment(HIGH)], "image", "auto", {"usage_id": 9})

    assert second["inserted"] == []
    assert second["merged"] == [entry_id]
    assert query_scalar("SELECT COUNT(*) FROM prompt_entries", (), 0) == 1
    assert entries.get_entry(entry_id)["use_count"] == 1


def test_near_duplicate_writes_alias_and_merges(env):
    """近似重复写别名并计入 merged；再次命中别名时不重复登记。"""
    first = learning.ingest_segments([_segment(HIGH)], "image", "auto", {"usage_id": 10})
    entry_id = first["inserted"][0]

    alias_text = "逆光形成的金色轮廓光"
    assert textutil.is_near_duplicate(HIGH, alias_text) is True

    second = learning.ingest_segments([_segment(alias_text)], "image", "auto", {"usage_id": 11})
    assert second["inserted"] == []
    assert second["merged"] == [entry_id]
    assert query_scalar("SELECT COUNT(*) FROM prompt_entries", (), 0) == 1

    aliases = query_all("SELECT * FROM prompt_entry_aliases WHERE prompt_entry_id = ?", (entry_id,))
    assert [row["alias_text"] for row in aliases] == [alias_text]
    assert aliases[0]["source_type"] == "auto"

    third = learning.ingest_segments([_segment(alias_text)], "image", "auto", {"usage_id": 12})
    assert third["merged"] == [entry_id]
    assert query_scalar("SELECT COUNT(*) FROM prompt_entry_aliases WHERE prompt_entry_id = ?", (entry_id,), 0) == 1


def test_ignored_fingerprint_blocks_ingest(env):
    """被删除或拒绝过的指纹不再入库，也不再进候选。"""
    entries.add_ignored_fingerprint(textutil.content_fingerprint("image", "lighting", HIGH), "deleted")

    result = learning.ingest_segments([_segment(HIGH)], "image", "auto", {"usage_id": 13})

    assert result["inserted"] == []
    assert result["candidates"] == []
    assert result["discarded"] == [{"text": HIGH, "reason": "ignored"}]


def test_source_ref_never_persists_absolute_paths(env):
    """来源追溯只保留可控标识，路径类字段与文件名一律剔除。"""
    result = learning.ingest_segments(
        [_segment(HIGH)],
        "image",
        "auto",
        {"usage_id": 1, "path": "/Users/someone/secret/a.png", "filename": "a.png"},
    )
    stored = query_scalar("SELECT source_ref FROM prompt_entries WHERE id = ?", (result["inserted"][0],))

    assert stored is not None
    assert "usage_id" in stored
    assert "/Users" not in stored
    assert "a.png" not in stored


# ---------------------------------------------------------------- 生成后学习


def test_learn_from_generation_requires_completed_status(env):
    """未完成（失败/中止）的生成记录不学习，也不写 learned_at。"""
    usage_id = learning.record_generation(
        "image",
        "wf-1",
        "flux_flux2",
        f"{HIGH}，薄雾中的雨夜巷道",
        [{"text": HIGH, "dimension": "lighting", "confidence": 0.95}],
    )

    pending = learning.learn_from_generation(usage_id)
    assert pending["learned"] is False
    assert pending["reason"] == "status_not_completed"
    assert query_scalar("SELECT learned_at FROM generation_prompt_usage WHERE id = ?", (usage_id,)) is None

    learning.mark_generation_status(usage_id, "completed")
    done = learning.learn_from_generation(usage_id)

    assert done["learned"] is True
    assert len(done["inserted"]) == 1
    assert query_scalar("SELECT learned_at FROM generation_prompt_usage WHERE id = ?", (usage_id,))


def test_learn_from_generation_skips_parameter_only_prompt(env):
    """只有运行参数、没有内容描述的提示词不学习，不写 learned_at。"""
    usage_id = learning.record_generation("image", None, "generic", "seed: 123", [])
    learning.mark_generation_status(usage_id, "completed")

    result = learning.learn_from_generation(usage_id)

    assert result["learned"] is False
    assert result["reason"] == "no_content"
    assert query_scalar("SELECT learned_at FROM generation_prompt_usage WHERE id = ?", (usage_id,)) is None


def test_learn_from_generation_falls_back_to_splitting(env):
    """没有结构化段落时按标点切分原始提示词，结构化内容优先。"""
    usage_id = learning.record_generation("image", "wf-2", "generic", f"{HIGH}，{MID}", None)
    learning.mark_generation_status(usage_id, "completed")

    result = learning.learn_from_generation(usage_id)

    assert result["learned"] is True
    assert len(result["inserted"]) == 1  # 85 分入库
    assert len(result["candidates"]) == 1  # 73 分进候选

    with pytest.raises(AIBARError):
        learning.learn_from_generation(999999)


# ---------------------------------------------------------------- 候选审核


def test_candidate_review_approve_and_reject(env):
    """批准后转为正式词条并带审核时指定的分类；拒绝后写入忽略指纹。"""
    learning.ingest_segments([_segment(MID)], "image", "auto", {"usage_id": 20})
    pending = learning.list_candidates()
    assert pending["total"] == 1
    candidate_id = pending["items"][0]["id"]

    approved = learning.approve_candidate(candidate_id, {"dimension": "lighting", "subcategory": "quality"})

    assert approved["review_status"] == "approved"
    assert approved["merged"] is False
    entry = entries.get_entry(approved["entry_id"])
    assert entry["dimension"] == "lighting"
    assert entry["subcategory"] == "quality"
    assert entry["source_type"] == "manual"
    assert learning.list_candidates()["total"] == 0

    rejected_text = "柔光箱打出的无影照明"
    learning.ingest_segments([_segment(rejected_text)], "image", "auto", {"usage_id": 21})
    target = learning.list_candidates()["items"][0]
    rejected = learning.reject_candidate(target["id"])

    assert rejected["review_status"] == "rejected"
    assert entries.is_ignored_fingerprint(textutil.content_fingerprint("image", "lighting", rejected_text))

    again = learning.ingest_segments([_segment("柔光箱打出的无影照明")], "image", "auto", {"usage_id": 22})
    assert again["candidates"] == []
    assert again["discarded"][0]["reason"] == "ignored"


def test_list_candidates_filters_and_paging(env):
    """候选区支持状态与媒体类型过滤，并给出分页元数据。"""
    learning.ingest_segments([_segment(MID)], "image", "auto", {"usage_id": 23})
    learning.ingest_segments(
        [{"text": "四踩底鼓的浩室舞曲", "dimension": "genre", "confidence": 0.65}],
        "music",
        "auto",
        {"usage_id": 24},
    )

    all_pending = learning.list_candidates(status="", page_size=1)
    assert all_pending["total"] == 2
    assert all_pending["has_more"] is True
    assert len(all_pending["items"]) == 1

    music_only = learning.list_candidates(media_type="music")
    assert music_only["total"] == 1
    assert music_only["items"][0]["media_type"] == "music"

    with pytest.raises(AIBARError):
        learning.list_candidates(status="unknown")


# ---------------------------------------------------------------- 路由


def test_candidate_routes_smoke(client, env):
    """候选审核接口冒烟：列表、批准、拒绝与 404。"""
    learning.ingest_segments([_segment(MID)], "image", "auto", {"usage_id": 30})

    listing = client.get("/api/prompt-library/candidates").get_json()
    assert listing["ok"] is True
    assert listing["data"]["total"] == 1
    candidate_id = listing["data"]["items"][0]["id"]

    approved = client.post(
        f"/api/prompt-library/candidates/{candidate_id}/approve",
        json={"dimension": "lighting", "subcategory": "quality"},
    ).get_json()
    assert approved["ok"] is True
    assert approved["data"]["review_status"] == "approved"
    assert approved["data"]["entry_id"]

    learning.ingest_segments([_segment("柔光箱打出的无影照明")], "image", "auto", {"usage_id": 31})
    pending = client.get("/api/prompt-library/candidates").get_json()["data"]["items"]
    rejected = client.post(f"/api/prompt-library/candidates/{pending[0]['id']}/reject").get_json()
    assert rejected["data"]["review_status"] == "rejected"

    missing = client.post("/api/prompt-library/candidates/999999/approve", json={}).get_json()
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"

    bad_status = client.get("/api/prompt-library/candidates?status=nope").get_json()
    assert bad_status["ok"] is False
    assert bad_status["error"]["code"] == "invalid_input"


# ---------------------------------------------------------------- P1-6 近似重复候选索引


#: 与 ``HIGH`` 相似度 0.889（≥ NEAR_DUPLICATE_THRESHOLD），质量分同为 85
NEAR_OF_HIGH = "逆光形成金色轮廓光线"


def test_intra_batch_near_duplicate_merges_into_first(env):
    """同一批次里两个高度相似的片段：第二条必须并进第一条，不能各建一条词条。

    这条锁的是 ``_NearDupIndex.register``。候选集在批次内被缓存复用，
    第一条新建的词条如果不回填进索引，第二条就"看不见"它，
    于是两条几乎一样的提示词各占一个词条 —— 去重能力在批量化后悄悄失效。
    """
    result = learning.ingest_segments(
        [_segment(HIGH), _segment(NEAR_OF_HIGH)], "image", "auto", {"usage_id": 20}
    )

    assert len(result["inserted"]) == 1, "同批的近似重复只应新建一条"
    assert result["merged"] == result["inserted"], "第二条应并入第一条"
    assert query_scalar("SELECT COUNT(*) FROM prompt_entries", (), 0) == 1

    entry_id = result["inserted"][0]
    aliases = query_all("SELECT alias_text FROM prompt_entry_aliases WHERE prompt_entry_id = ?", (entry_id,))
    assert [row["alias_text"] for row in aliases] == [NEAR_OF_HIGH]


def test_near_dup_index_loads_once_per_batch(env, monkeypatch):
    """反证核心：修复前每个片段各查 2 次库，现在是整批 2 次（一个维度只加载一次）。"""
    created: list = []

    class _Counting(learning._NearDupIndex):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(learning, "_NearDupIndex", _Counting)

    segments = [_segment(f"侧逆光打在人物肩线形成柔和的金色边缘 {i}") for i in range(8)]
    learning.ingest_segments(segments, "image", "auto", {"usage_id": 21})

    assert len(created) == 1, "一次 ingest 只建一个索引"
    assert created[0].loads == 1, f"8 个片段只该加载 1 次，实际 {created[0].loads} 次"


def test_near_dup_index_loads_once_per_dimension(env, monkeypatch):
    """跨维度各自加载一次：不同维度的候选集互不相干，不能混。"""
    created: list = []

    class _Counting(learning._NearDupIndex):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(learning, "_NearDupIndex", _Counting)

    learning.ingest_segments(
        [_segment(HIGH, dimension="lighting"), _segment("雨夜街道上的霓虹倒影在积水里", dimension="lighting")],
        "image",
        "auto",
        {"usage_id": 22},
    )

    assert created[0].loads == 1, "两个片段同一维度 ⇒ 仍然只加载一次"


def test_find_near_duplicate_matches_with_and_without_index(env):
    """带索引与不带索引必须给出同一个答案 —— 索引只是缓存，不能改变判定。"""
    entries.insert_entry(
        {
            "media_type": "image",
            "dimension": "lighting",
            "subcategory": "quality",
            "title": "既有",
            "prompt_text": HIGH,
            "source_type": "manual",
        }
    )
    index = learning._NearDupIndex()

    with_index = learning._find_near_duplicate("image", "lighting", NEAR_OF_HIGH, index)
    without_index = learning._find_near_duplicate("image", "lighting", NEAR_OF_HIGH)

    assert with_index is not None
    assert with_index == without_index


def test_near_dup_candidates_ignore_hidden_entries(env):
    """隐藏词条（内置词条"删除"后的状态）不参与近似重复。

    否则新片段会被并到一条用户已经删掉的词条上 —— 删掉的东西又从后门回来了。
    """
    entry = entries.insert_entry(
        {
            "media_type": "image",
            "dimension": "lighting",
            "subcategory": "quality",
            "title": "待隐藏",
            "prompt_text": HIGH,
            "source_type": "builtin",
        }
    )
    outcome = entries.delete_entry(int(entry["id"]))
    assert outcome["hidden"] is True, "内置词条走隐藏而非物理删除"

    assert learning._find_near_duplicate("image", "lighting", NEAR_OF_HIGH) is None
    assert learning._find_near_duplicate("image", "lighting", NEAR_OF_HIGH, learning._NearDupIndex()) is None

