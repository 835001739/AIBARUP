"""M14 组图（Shot Group）测试：强制预设提示词 + 连贯动作种子 + 串行出图 + 连播清单。

覆盖的核心不变量（改动前务必读完）：

1. **提示词顺序**：人物锚点 → 一致性指令 → 场景前缀 → 动作 → 镜头后缀。
   锚点前置是 M12 验证过的结论（模型对靠前描述权重更高），顺序错了人物就会漂。
2. **「强制」的语义**：改了预设后 ``refresh_prompts`` 会用当前预设**覆盖全部帧**，
   帧里存什么 prompt_text 都不算数——这是连播时人物不漂的根本保障。
3. **种子默认全帧相同**（``seed_step=0``）：同种子 + 不同提示词，模型只改动文本
   描述的部分，动作因此连贯；每帧换种子则脸会跟着噪声一起漂。
4. **先写后清**：出图是最贵的一步，清理旧图失败绝不能把已经出好的帧打成 failed。
5. **整组出图串行且互斥**：同一组图不会被跑两遍，单帧重生成要避让整组出图。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from flask import Flask

from comic import characters
from comic import groups
from comic import routes as comic_routes
from config import Config
from core import db as core_db


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
    # 清理出图线程留下的运行标记，避免用例间互相干扰
    with groups._STATE_LOCK:
        groups._RUNNING.clear()
        groups._CANCELS.clear()
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


def _make_group(**kwargs):
    payload = {
        "name": "拔刀三连",
        "preset_prefix": "雨天的天台",
        "preset_suffix": "电影感侧光",
        "preset_negative": "blurry",
        "anchor_override": "黑发少年，红色外套",
        "actions": ["握紧刀柄", "拔出长刀", "收刀入鞘"],
    }
    payload.update(kwargs)
    return groups.create_group(payload)


# ---------------------------------------------------------------- 提示词组装（模块核心）


def test_compose_frame_prompt_puts_anchor_first(env):
    """顺序必须是 锚点 → 一致性指令 → 前缀 → 动作 → 后缀。"""
    group = _make_group()
    prompt = groups.compose_frame_prompt(group, "拔出长刀")
    assert prompt.startswith("黑发少年，红色外套"), prompt
    assert characters.CONSISTENCY_DIRECTIVE in prompt
    assert prompt.index(characters.CONSISTENCY_DIRECTIVE) < prompt.index("雨天的天台")
    assert prompt.index("雨天的天台") < prompt.index("拔出长刀")
    assert prompt.index("拔出长刀") < prompt.index("电影感侧光")


def test_compose_frame_prompt_without_anchor_skips_directive(env):
    """没有人物锚点时不该挂一致性指令——空镜头要求「保持同一张脸」没有意义。"""
    group = _make_group(anchor_override="")
    prompt = groups.compose_frame_prompt(group, "挥手")
    assert characters.CONSISTENCY_DIRECTIVE not in prompt
    assert prompt == "雨天的天台，挥手，电影感侧光"


def test_compose_frame_negative_always_includes_anti_drift(env):
    """反漂移词恒在：组图的全部意义就是连播起来像同一个人在动。"""
    neg = groups.compose_frame_negative(_make_group(preset_negative=""))
    assert characters.ANTI_DRIFT_NEGATIVE in neg


def test_compose_frame_negative_dedupes(env):
    neg = groups.compose_frame_negative(
        _make_group(preset_negative=characters.ANTI_DRIFT_NEGATIVE)
    )
    assert neg.lower().count(characters.ANTI_DRIFT_NEGATIVE.lower()) == 1


def test_resolve_anchor_prefers_binding_then_override(env):
    """绑定锚点在前、手填补充在后（手填是补充而不是覆盖）。"""
    actor = None
    try:
        from comic import actors

        actor = actors.create_actor({"name": "林越", "appearance": "黑短发", "outfit": "校服"})
        group = _make_group(actor_id=actor["id"], anchor_override="扎马尾")
        anchor = groups.resolve_anchor(group)
        assert anchor.startswith("林越，黑短发，校服"), anchor
        assert anchor.endswith("扎马尾"), anchor
    finally:
        pass


def test_resolve_anchor_falls_back_to_override(env):
    assert groups.resolve_anchor(_make_group()) == "黑发少年，红色外套"


# ---------------------------------------------------------------- 种子


def test_frame_seed_is_identical_across_frames_by_default(env):
    """seed_step=0（默认）时全部帧同一种子 —— 这是连贯动作的关键。"""
    group = _make_group(seed_step=0)
    seeds = {groups.frame_seed(group, i) for i in range(5)}
    assert len(seeds) == 1


def test_frame_seed_steps_when_seed_step_positive(env):
    group = _make_group(base_seed=100, seed_step=13)
    assert groups.frame_seed(group, 0) == 100
    assert groups.frame_seed(group, 1) == 113
    assert groups.frame_seed(group, 7) == 191


def test_frame_seed_is_deterministic(env):
    group = _make_group(base_seed=42)
    assert groups.frame_seed(group, 3) == groups.frame_seed(group, 3)


# ---------------------------------------------------------------- 帧管理


def test_create_group_persists_frames_with_composed_prompts(env):
    group = _make_group()
    assert len(group["frames"]) == 3
    for idx, frame in enumerate(group["frames"]):
        assert frame["order_idx"] == idx
        assert frame["action_text"] in frame["prompt_text"]
        assert frame["prompt_text"] == groups.compose_frame_prompt(group, frame["action_text"])
        assert frame["status"] == "pending"


def test_set_frames_is_whole_replacement(env):
    group = _make_group()
    updated = groups.set_frames(group["id"], ["A", "B"])
    assert len(updated["frames"]) == 2
    assert [f["action_text"] for f in updated["frames"]] == ["A", "B"]


def test_set_frames_keeps_generated_images(env):
    """缩短序列时不能把已出图的帧连带删掉——几十秒 GPU 的成果不该被一次编辑清空。"""
    group = _make_group()
    frame_id = group["frames"][0]["id"]
    core_db.execute(
        "UPDATE shot_frames SET status='done', image_path='comic_outputs/shotgroups/1/1_abc.png' WHERE id=?",
        (frame_id,),
    )
    updated = groups.set_frames(group["id"], ["改过的动作", "第二步"])
    kept = [f for f in updated["frames"] if f["id"] == frame_id]
    assert kept and kept[0]["status"] == "done"
    assert kept[0]["image_path"]


def test_set_frames_rejects_too_many(env):
    group = _make_group()
    with pytest.raises(Exception):
        groups.set_frames(group["id"], ["a"] * (groups.MAX_FRAMES + 1))


def test_refresh_prompts_overwrites_every_frame(env):
    """「强制」的执行点：无论帧里存了什么，一律用当前预设覆盖。"""
    group = _make_group()
    for frame in group["frames"]:
        core_db.execute("UPDATE shot_frames SET prompt_text='手改的提示词' WHERE id=?", (frame["id"],))

    groups.update_group(group["id"], {"preset_prefix": "雪地"})
    after = groups.refresh_prompts(group["id"])
    for frame in after["frames"]:
        assert "手改的提示词" not in frame["prompt_text"]
        assert "雪地" in frame["prompt_text"]


def test_update_frame_recomputes_prompt_from_preset(env):
    group = _make_group()
    frame = group["frames"][1]
    updated = groups.update_frame(frame["id"], {"action_text": "转身回望"})
    assert "转身回望" in updated["prompt_text"]
    assert "拔出长刀" not in updated["prompt_text"]


def test_update_frame_rejects_empty_action(env):
    group = _make_group()
    with pytest.raises(Exception):
        groups.update_frame(group["frames"][0]["id"], {"action_text": "  "})


def test_delete_frame_closes_the_gap(env):
    group = _make_group()
    groups.delete_frame(group["frames"][0]["id"])
    left = groups.get_group(group["id"])["frames"]
    assert [f["order_idx"] for f in left] == [0, 1]
    assert [f["action_text"] for f in left] == ["拔出长刀", "收刀入鞘"]


def test_parse_actions_splits_lines_and_array(env):
    assert groups.parse_actions({"actions": [" a ", "", "b"]}) == ["a", "b"]
    assert groups.parse_actions({"action_text": "一\n二\n\n三"}) == ["一", "二", "三"]


# ---------------------------------------------------------------- 落盘（先写后清）


class _BoomUnlink:
    """让 unlink 抛 SystemExit，模拟运行环境安全策略拦截删除。"""

    def __init__(self, original):
        self._original = original
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise SystemExit(1)


def test_save_frame_image_writes_new_file_even_when_purge_raises_systemexit(
    env, monkeypatch: pytest.MonkeyPatch
):
    """清理旧图失败绝不能把已经出好的帧打成 failed（2026-09-01 线上故障的同类防线）。"""
    import pathlib

    boom = _BoomUnlink(pathlib.Path.unlink)
    monkeypatch.setattr(pathlib.Path, "unlink", boom)
    saved = groups._save_frame_image(1, 7, b"\x89PNG-fake")
    assert (Path(Config.DATA_DIR) / saved).is_file()


def test_save_frame_image_purges_stale_but_keeps_current(env):
    dest_dir = Path(Config.DATA_DIR) / "comic_outputs" / groups.GROUPS_OUTPUT_SUBDIR / "9"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "5_aaaaaaaaaaaaaaaa.png").write_bytes(b"old")

    saved = groups._save_frame_image(9, 5, b"new")
    names = {p.name for p in dest_dir.glob("5_*.png")}
    assert names == {Path(saved).name}


# ---------------------------------------------------------------- 出图编排


class _FakeRunner:
    """替身出图器：可控成功/失败，并记录调用顺序。"""

    def __init__(self, results=None):
        self.calls: list[tuple[str, int]] = []
        self._results = results or {}

    def generate_once(self, workflow, prompt, negative, seed, timeout=0.0, cancel=None):
        self.calls.append((prompt, int(seed)))
        if self._results.get("fail_on_second") and len(self.calls) == 2:
            return None, "comfyui_offline", "ComfyUI 掉线了"
        return b"\x89PNG-fake", "", ""


def _install_runner(monkeypatch, fake):
    import types

    mod = types.SimpleNamespace(generate_once=fake.generate_once)
    monkeypatch.setattr(groups, "_default_workflow", lambda: "wf.json")
    monkeypatch.setitem(
        __import__("sys").modules, "comic.runner", mod
    )
    # _execute_frame 内部 `from . import runner`，需要真正挂到包上
    import comic

    monkeypatch.setattr(comic, "runner", mod, raising=False)


def _wait_threads(timeout=5.0):
    """等后台出图线程跑完（测试里不能用 sleep 猜时间）。"""
    deadline = threading.Event()
    import time

    end = time.time() + timeout
    while time.time() < end:
        with groups._STATE_LOCK:
            busy = bool(groups._RUNNING)
        if not busy:
            return True
        time.sleep(0.02)
    return False


def test_generate_group_runs_frames_serially_and_marks_done(env, monkeypatch):
    fake = _FakeRunner()
    _install_runner(monkeypatch, fake)
    group = _make_group()
    groups.generate_group(group["id"], {})
    assert _wait_threads()

    after = groups.get_group(group["id"])
    assert [f["status"] for f in after["frames"]] == ["done"] * 3
    assert len(fake.calls) == 3
    # 串行：调用顺序必须等于帧序
    assert [c[0] for c in fake.calls] == [f["prompt_text"] for f in after["frames"]]


def test_generate_group_uses_same_seed_for_all_frames(env, monkeypatch):
    """默认 seed_step=0 → 全部帧同一个种子，动作由文本驱动。"""
    fake = _FakeRunner()
    _install_runner(monkeypatch, fake)
    group = _make_group(seed_step=0)
    groups.generate_group(group["id"], {})
    assert _wait_threads()
    assert len({c[1] for c in fake.calls}) == 1


def test_generate_group_only_missing_skips_done_frames(env, monkeypatch):
    fake = _FakeRunner()
    _install_runner(monkeypatch, fake)
    group = _make_group()
    core_db.execute("UPDATE shot_frames SET status='done' WHERE id=?", (group["frames"][0]["id"],))
    groups.generate_group(group["id"], {"only_missing": True})
    assert _wait_threads()
    assert len(fake.calls) == 2


def test_generate_group_reports_failure_with_readable_message(env, monkeypatch):
    fake = _FakeRunner({"fail_on_second": True})
    _install_runner(monkeypatch, fake)
    group = _make_group()
    groups.generate_group(group["id"], {})
    assert _wait_threads()

    after = groups.get_group(group["id"])
    failed = [f for f in after["frames"] if f["status"] == "failed"]
    assert len(failed) == 1
    # 不能只有机器码/异常类名，必须带可读原因
    assert failed[0]["error_message"]
    assert failed[0]["error_message"] != "comfyui_offline"
    assert after["status"] == "partial"


def test_generate_group_rejects_concurrent_run(env, monkeypatch):
    """同一组图不允许跑两个出图线程——否则两个线程抢同一张卡且状态互相覆盖。"""
    gate = threading.Event()
    fake = _FakeRunner()
    original = fake.generate_once

    def slow(*args, **kwargs):
        gate.wait(2.0)
        return original(*args, **kwargs)

    fake.generate_once = slow
    _install_runner(monkeypatch, fake)

    group = _make_group()
    groups.generate_group(group["id"], {})
    try:
        with pytest.raises(Exception) as exc:
            groups.generate_group(group["id"], {})
        assert "正在出图" in str(exc.value)
    finally:
        gate.set()
        assert _wait_threads()


def test_regenerate_frame_conflicts_with_group_run(env, monkeypatch):
    gate = threading.Event()
    fake = _FakeRunner()
    original = fake.generate_once

    def slow(*args, **kwargs):
        gate.wait(2.0)
        return original(*args, **kwargs)

    fake.generate_once = slow
    _install_runner(monkeypatch, fake)

    group = _make_group()
    groups.generate_group(group["id"], {})
    try:
        with pytest.raises(Exception) as exc:
            groups.regenerate_frame(group["frames"][0]["id"], {})
        assert "正在整组出图" in str(exc.value)
    finally:
        gate.set()
        assert _wait_threads()


def test_progress_stays_generating_while_running(env, monkeypatch):
    """轮询看到 partial 会误判「出完了」而停掉进度条——运行期必须压住 generating。"""
    gate = threading.Event()
    fake = _FakeRunner()
    original = fake.generate_once

    def slow(*args, **kwargs):
        gate.wait(2.0)
        return original(*args, **kwargs)

    fake.generate_once = slow
    _install_runner(monkeypatch, fake)

    group = _make_group()
    groups.generate_group(group["id"], {})
    try:
        # 等第一帧出完，此时剩下的帧还是 pending，纯按计数会得出 partial
        for _ in range(400):
            if len(fake.calls) >= 1:
                break
            gate.wait(0.01)
        progress = groups.group_progress(group["id"])
        assert progress["running"] is True
        assert progress["status"] == "generating"
    finally:
        gate.set()
        assert _wait_threads()

    final = groups.group_progress(group["id"])
    assert final["running"] is False
    assert final["status"] == "ready"
    assert final["done"] == 3


# ---------------------------------------------------------------- 连播清单


def test_playlist_only_returns_done_frames_in_order(env):
    group = _make_group()
    ids = [f["id"] for f in group["frames"]]
    core_db.execute(
        "UPDATE shot_frames SET status='done', image_path='comic_outputs/shotgroups/1/1_a.png' WHERE id=?",
        (ids[0],),
    )
    core_db.execute(
        "UPDATE shot_frames SET status='done', image_path='comic_outputs/shotgroups/1/3_c.png' WHERE id=?",
        (ids[2],),
    )
    data = groups.playlist(group["id"])
    assert [it["order_idx"] for it in data["items"]] == [0, 2]
    assert all(it["url"] for it in data["items"])
    assert data["interval"] == groups.DEFAULT_INTERVAL
    assert data["loop"] is True


def test_playlist_returns_interval_and_loop(env):
    group = _make_group(frame_interval=120, loop_play=False)
    data = groups.playlist(group["id"])
    assert data["interval"] == 120
    assert data["loop"] is False


def test_playlist_clamps_interval(env):
    data = groups.playlist(_make_group(frame_interval=99999)["id"])
    assert data["interval"] == groups.MAX_INTERVAL


# ---------------------------------------------------------------- 路由


def test_routes_create_list_get(client, env):
    resp = client.post("/api/comic/groups", json={
        "name": "挥手", "preset_prefix": "操场", "actions": ["抬手", "挥手"]
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    gid = body["data"]["id"]

    listed = client.get("/api/comic/groups").get_json()
    assert listed["data"]["total"] == 1
    assert listed["data"]["items"][0]["name"] == "挥手"

    detail = client.get("/api/comic/groups/%d" % gid).get_json()
    assert len(detail["data"]["frames"]) == 2


def test_routes_replace_frames(client, env):
    gid = client.post("/api/comic/groups", json={"name": "x", "actions": ["a"]}).get_json()["data"]["id"]
    resp = client.put("/api/comic/groups/%d/frames" % gid, json={"actions": ["一", "二", "三"]})
    assert resp.get_json()["ok"] is True
    assert len(client.get("/api/comic/groups/%d" % gid).get_json()["data"]["frames"]) == 3


def test_routes_delete_group(client, env):
    gid = client.post("/api/comic/groups", json={"name": "x", "actions": ["a"]}).get_json()["data"]["id"]
    assert client.delete("/api/comic/groups/%d" % gid).get_json()["ok"] is True
    assert client.get("/api/comic/groups").get_json()["data"]["total"] == 0


def test_routes_missing_group_returns_404(client, env):
    resp = client.get("/api/comic/groups/9999")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_routes_progress_endpoint(client, env):
    gid = client.post("/api/comic/groups", json={"name": "x", "actions": ["a", "b"]}).get_json()["data"]["id"]
    data = client.get("/api/comic/groups/%d/progress" % gid).get_json()["data"]
    assert data["total"] == 2
    assert data["done"] == 0
    assert data["running"] is False


# ---------------------------------------------------------------- 列表与统计


def test_list_groups_and_count(env):
    _make_group(name="甲组")
    _make_group(name="乙组", description="另一组动作")
    assert groups.count_groups() == 2
    assert groups.count_groups(keyword="乙") == 1
    assert len(groups.list_groups(keyword="甲")) == 1
    # 更新的排在前面
    assert groups.list_groups()[0]["name"] == "乙组"


def test_new_group_status_is_idle_not_partial(env):
    """一帧都没出过时必须是 idle —— 还没开始就显示「部分完成」是误导。"""
    assert _make_group()["status"] == "idle"


def test_status_becomes_partial_after_a_failure(env):
    """出过一部分才谈 partial。"""
    group = _make_group()
    core_db.execute("UPDATE shot_frames SET status='done' WHERE id=?", (group["frames"][0]["id"],))
    core_db.execute("UPDATE shot_frames SET status='failed' WHERE id=?", (group["frames"][1]["id"],))
    assert groups.group_progress(group["id"])["status"] == "partial"


def test_status_ready_when_all_done(env):
    group = _make_group()
    core_db.execute("UPDATE shot_frames SET status='done', image_path='x.png' WHERE group_id=?", (group["id"],))
    assert groups.group_progress(group["id"])["status"] == "ready"


def test_delete_group_removes_frames(env):
    group = _make_group()
    gid = group["id"]
    groups.delete_group(gid)
    assert groups.count_groups() == 0
    assert core_db.query_scalar(
        "SELECT COUNT(*) FROM shot_frames WHERE group_id=?", (gid,), default=0
    ) == 0


def test_update_group_validates_actor(env):
    group = _make_group()
    with pytest.raises(Exception):
        groups.update_group(group["id"], {"actor_id": 999999})
