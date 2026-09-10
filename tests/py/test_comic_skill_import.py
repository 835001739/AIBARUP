"""M12 · Skill → 漫画工作流导入测试（可复用能力）。

覆盖：SKILL.md 解析、说明性段落跳过（含带括号变体）、project→chapter→page 映射、
FLUX.2 工作流自动落盘与可解析、以及 runner 对 FLUX.2 ``noise_seed`` 的种子注入兼容。
数据落 ``tmp_path``，FLUX 工作流目录通过 monkeypatch 隔离，不污染真实 ComfyUI。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from config import Config
from core import db as core_db

from comic import routes as comic_routes
from comic import runner as comic_runner
from comic import service as comic_service
from comic import skill_import
from sync import paths as sync_paths
from sync.workflow_convert import convert_text


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据库与运行期目录重定向到临时目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "comic_outputs").mkdir(parents=True, exist_ok=True)

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
    """只注册 comic 蓝图，不依赖 app.py 装配。"""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(comic_routes.bp)
    with app.test_client() as test_client:
        yield test_client



SAMPLE_SKILL = """---
name: demo-skill
description: 一个用于测试导入的示例 skill
---

## When to use

当需要做某事时使用本 skill。

## 前置条件

本机需要安装 Foo。

## Steps

1. **第一步**：准备好输入文件。
2. **第二步**：运行主流程。
3. **第三步**：校验输出结果。

## 核心工作流（最小化 13 节点）

这里是工作流的节点说明，不是编号步骤。

## Pitfalls（全部踩过，务必遵守）

- 不要忘记设置环境变量。
- 路径要用绝对路径。

## Verification

检查输出是否非空。

## Files

- scripts/run.py
"""


def test_parse_skill_md():
    skill = skill_import.parse_skill_md(SAMPLE_SKILL)
    assert skill["name"] == "demo-skill"
    assert skill["description"] == "一个用于测试导入的示例 skill"
    titles = [s["title"] for s in skill["sections"]]
    assert "Steps" in titles
    assert "Pitfalls（全部踩过，务必遵守）" in titles
    steps = next(s for s in skill["sections"] if s["title"] == "Steps")
    assert [st["index"] for st in steps["steps"]] == [1, 2, 3]


def test_should_skip_advisory_sections():
    assert skill_import._should_skip("When to use")
    assert skill_import._should_skip("前置条件")
    assert skill_import._should_skip("Pitfalls（全部踩过，务必遵守）")
    assert skill_import._should_skip("Verification")
    assert skill_import._should_skip("Files")
    # 流程段落不应被跳过
    assert not skill_import._should_skip("Steps")
    assert not skill_import._should_skip("核心工作流（最小化 13 节点）")


def test_import_skill_builds_chapters_and_pages(env):
    result = skill_import.import_skill(SAMPLE_SKILL, workflow_filename="wf.json")
    # When to use / 前置条件 / Pitfalls / Verification / Files 被跳过
    # 仅 Steps（3 步）与 核心工作流（1 无编号段落）成为章节
    assert result["chapters"] == 2
    assert result["pages"] == 4  # 3 + 1
    assert result["workflow_filename"] == "wf.json"

    project = comic_service.get_project(result["project_id"])
    assert project["name"] == "demo-skill"
    assert project["default_workflow"] == "wf.json"

    chapters = comic_service.list_chapters(result["project_id"])
    titles = [c["title"] for c in chapters]
    assert "Steps" in titles
    assert "Pitfalls（全部踩过，务必遵守）" not in titles


def test_import_skill_via_api(client):
    resp = client.post(
        "/api/comic/import-skill",
        json={"markdown": SAMPLE_SKILL, "workflow_filename": "wf.json"},
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["chapters"] == 2
    assert data["pages"] == 4
    assert data["project_name"] == "demo-skill"


def test_ensure_flux_workflow_writes_and_parses(tmp_path, monkeypatch):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sync_paths, "workflows_dir", lambda: wf_dir)

    fname = skill_import.ensure_flux_workflow()
    assert fname == skill_import.FLUX_WORKFLOW_FILENAME
    fpath = wf_dir / fname
    assert fpath.is_file()

    graph = convert_text(fpath.read_text(encoding="utf-8"))
    assert isinstance(graph, dict) and len(graph) == 13
    # FLUX.2 关键节点
    assert graph["3"]["inputs"]["type"] == "flux2"
    assert "noise_seed" in graph["8"]["inputs"]


def test_inject_prompt_noise_seed_compat():
    graph = convert_text(__import__("json").dumps(skill_import._FLUX2_WORKFLOW))
    comic_runner._inject_prompt(graph, "测试提示词", "", 777)
    assert graph["8"]["inputs"]["noise_seed"] == 777
    assert graph["5"]["inputs"]["text"] == "测试提示词"
