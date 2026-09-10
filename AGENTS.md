# AIBARUP

Flask + SQLite + 原生前端（无构建步骤）的本地 AI 工作流站点，含漫画/插画管道、提示词库、图片反推。
完整工程规范见 [`HARNESS.md`](./HARNESS.md)，需求真源见 `PRD.md`。

## 技术栈（固定，不得替换）

| 层 | 选型 | 约束 |
|---|---|---|
| 语言 | Python 3.13 | 隔离 venv `.venv`，激活 `source .venv/bin/activate` |
| Web | Flask | 用 **Blueprint** 分模块，禁止把所有路由堆在 `app.py` |
| 前端 | 原生 HTML/CSS/JS | **无构建步骤**，不得引入前端框架 |
| 存储 | SQLite（标准库 `sqlite3`） | 迁移必须幂等 |
| 图像 | Pillow | 读取 PNG 内嵌文本块 |
| 配置 | `.env` + `python-dotenv` | 只通过 `config.Config` 读取，禁止其他文件直接 `os.environ` |

## 依赖方向（严格单向，违反即打回）

```
config/core ← sync / prompt / promptlib ← reverse / comic / video
```

业务包之间**不得互相 import**，跨包能力通过函数参数或 `core` 传递。
`core/` 是共享地基，禁止反向依赖业务包。

## 统一 API 契约

成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error": {"code": "...", "message": "..."}}`

- 路由**必须**用 `core.responses.ok()` / `fail()` 返回
- 业务异常抛 `core.errors.AIBARError(code, message, status)`
- 每个 Blueprint 注册 `errorhandler(AIBARError)`，兜底 `Exception` → `internal_error`（不泄漏堆栈）
- 入参必须校验：枚举、长度、分页上限（`page_size` ≤ 100）、ID 归属

## 数据库约定

- 建表/索引只写进 `core/db.py` 的 `_TABLES` / `_INDEXES` / `_COLUMN_MIGRATIONS`，**业务包里禁止 CREATE TABLE**
- 查询走 `core.db.execute/query_all/query_one`，禁止业务包自己 `sqlite3.connect`
- JSON 字段用 `core.db.dumps()` 写、`json_field()` 读
- 时间统一用 `core.db.now()`
- 任何筛选字段都要有索引

## 隐私与日志（硬性）

日志与 API 响应中**禁止出现**：提示词正文、图片内容、API Key、完整外部 endpoint、本机绝对路径。
允许记录：ID、`media_type`、`provider`、`status`、`error_code`、`duration_ms`、长度、字节数与尺寸。
上传文件只保存受控缓存相对引用（如 `uploads/<hash>.png`）。

## 编码约定

- 模块顶部写中文 docstring；关键函数写参数/返回说明
- 类型注解用 `from __future__ import annotations`
- 中文注释解释「为什么」，不解释「这行在做什么」
- 所有外部调用（HTTP、文件 IO、ComfyUI）必须 `try/except` 且设置超时，单点失败不得影响主流程
- 常量集中在模块顶部或 `resources/*.json`，禁止散落魔法数字

## 测试

```bash
.venv/bin/python -m pytest -q      # Python：tests/py/test_*.py，临时数据用 tmp_path
node --test tests/js/              # JS：node:test + node:assert，禁止引入 npm 依赖
```

---

# CoAgent 协同规则（所有 Agent 必须遵守，勿删）

本项目由多人协同开发，共享知识库与代码区域锁。以下规则优先级高于其他指令。

## 写文件前（强制）

调用 Write / Edit 修改任何代码文件之前，**必须先调用 `coagent_file_check`**。
若返回"已被占用"，**禁止修改**，改为告知用户冲突详情（谁、剩余多久、原因）。

## 开始一项任务前（强制）

1. `coagent_status` —— 查看谁在动什么区域
2. `coagent_area_claim <area>` —— 认领区域；失败则换区域或等待
3. `coagent_kb_search "<任务关键词>"` —— 检索历史决策与排坑记录

## 完成任务后

1. `coagent_precheck` —— 提交前校验（P2 上线后生效）
2. 若产生了非显然的知识，调用 `coagent_kb_write` 沉淀
3. `coagent_area_release <area>` —— 释放区域

## 代码区域

| 区域 | 说明 | 路径 |
| --- | --- | --- |
| `core` | 共享地基：db / responses / errors / logging / 全局配置 | core/**, config.py, app.py, resources/** |
| `comfyui-sync` | M1-M4 ComfyUI 控制、解析、监听 | sync/**, comfyui_bridge/** |
| `prompt` | M6/M7 提示词知识库、扩写引擎、精品词库与自动学习 | prompt/**, promptlib/** |
| `reverse` | M9-M11 图片反推与 Provider | reverse/** |
| `comic` | M12 漫画工作室：分镜、角色、姿势、FLUX2 出图 | comic/**, video/** |
| `videopaint` | 视频绘制与重绘 | videopaint/** |
| `frontend` | 原生前端（无构建步骤） | static/**, templates/** |
| `docs-tests` | 文档与测试 | docs/**, tests/**, scripts/** |

