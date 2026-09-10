# AIBAR 工程规范（HARNESS）

> 本文件是所有开发（含并行 Agent）必须遵守的**唯一工程约定**。
> 需求真源是 `PRD.md`，本文件只约束"怎么实现"，不定义"做什么"。

---

## 1. 技术栈（固定，不得替换）

| 层 | 选型 | 约束 |
|---|---|---|
| 语言 | Python 3.13 | 隔离 venv：`.venv`，激活命令 `source .venv/bin/activate` |
| Web | Flask | 使用 **Blueprint** 分模块，禁止把所有路由堆在 `app.py` |
| 前端 | 原生 HTML/CSS/JS | **无构建步骤**，不得引入前端框架 |
| 存储 | SQLite（标准库 `sqlite3`） | 迁移必须幂等 |
| 图像 | Pillow | 读取 PNG 内嵌文本块 |
| 配置 | `.env` + `python-dotenv` | 只通过 `config.Config` 读取 |

---

## 2. 目录结构与模块归属

```
AIBARUP/
  config.py            # 全局配置（只读属性，禁止其他文件直接 os.environ）
  app.py               # 仅做：装配 Blueprint、注册错误处理器、启动同步线程
  core/                # 【共享地基，禁止业务模块反向依赖业务包】
    db.py              # 连接、幂等迁移、query/execute 助手
    responses.py       # ok() / fail() / from_exception()
    errors.py          # AIBARError
    logging_setup.py   # 日志配置 + safe_log()
    textutil.py        # 规范化/指纹/切分/归类/评分/去重/插入
  sync/                # M1-M4：ComfyUI 控制、解析、监听
    comfyui_ctl.py  parser.py  watcher.py  routes.py
  prompt/              # M6：知识库与规则扩写引擎
    library.py  engine.py  providers.py  routes.py
  promptlib/           # M7：精品词库、自动学习、候选审核
    entries.py  learning.py  routes.py
  reverse/             # M9-M11：图片反推与 Provider
    service.py  schema.py  uploads.py  routes.py
    providers/         # metadata / comfyui_blip / qwen3vl / joycaption / openai
  resources/           # 版本化配置（入 git）
    taxonomy.json  prompt_library.json  prompt_seed.json  provider_manifest.json
  static/              # 前端（无构建）
    index.html  css/*.css  js/*.js
  tests/py/            # pytest
  tests/js/            # 轻量 DOM 回归测试（Node 内置，无依赖）
```

**依赖方向（严格单向）**：
`config/core` ← `sync` / `prompt` / `promptlib` ← `reverse`（可调用 `promptlib.learning`）
业务包之间**不得互相 import**，跨包能力通过函数参数或 `core` 传递。

---

## 3. 统一 API 契约

成功：`{"ok": true, "data": ...}`；失败：`{"ok": false, "error": {"code": "...", "message": "..."}}`

- 路由函数**必须**用 `core.responses.ok()` / `fail()` 返回；
- 业务异常抛 `core.errors.AIBARError(code, message, status)`；
- 每个 Blueprint 必须注册 `errorhandler(AIBARError)`，兜底 `Exception` → `internal_error`（不泄漏堆栈）；
- 所有入参必须校验：枚举、长度、分页上限（`page_size` ≤ 100）、ID 归属；
- 错误码使用英文下划线，例如 `invalid_input` / `not_found` / `provider_offline`。

---

## 4. 数据库约定

- 建表/索引写进 `core/db.py` 的 `_TABLES` / `_INDEXES` / `_COLUMN_MIGRATIONS`，**不要在业务包里 CREATE TABLE**；
- 所有查询走 `core.db.execute/query_all/query_one`，禁止业务包自己 `sqlite3.connect`；
- JSON 字段用 `core.db.dumps()` 写入、用 `json_field()` 读取；
- 时间统一用 `core.db.now()`（本地时间字符串）；
- 任何筛选字段都要有索引。

---

## 5. 隐私与日志（硬性，违反即打回）

日志与 API 响应中**禁止出现**：提示词正文、图片内容、API Key、完整外部 endpoint、本机绝对路径。

允许记录：ID、`media_type`、`dimension`、`provider`、`profile`、`intensity`、`stage`、
`status`、`error_code`、`duration_ms`、输入输出**长度**、图片字节数与尺寸。

上传文件只保存**受控缓存相对引用**（如 `uploads/<hash>.png`），不保存用户原始路径。

---

## 6. 编码约定

- 每个模块顶部写中文 docstring 说明职责；关键函数写参数/返回说明；
- 类型注解使用 `from __future__ import annotations` + 标准写法；
- 中文注释解释"为什么"，不解释"这行在做什么"；
- 所有外部调用（HTTP、文件 IO、ComfyUI）必须 `try/except` 并设置超时，**单文件/单请求失败不得影响主流程**；
- 常量集中放在模块顶部或 `resources/*.json`，禁止散落魔法数字。

---

## 7. 测试约定

- Python 测试放 `tests/py/test_*.py`，用 `pytest`；临时数据用 `tmp_path` fixture，禁止污染 `data/`；
- JS 测试放 `tests/js/*.test.js`，用 Node 原生 `node:test` + `node:assert`，**不引入 npm 依赖**；
- 新增功能必须有对应测试；修改功能必须保证既有测试通过；
- 运行方式：
  ```bash
  .venv/bin/python -m pytest -q
  node --test tests/js/
  ```

---

## 8. 提交与自测

- 每完成一个模块，先自测（跑测试 + 实际接口冒烟）；
- 完成全部开发后，按 `PRD.md` §9 成功指标逐条验收，产出 `ACCEPTANCE.md`；
- 不得为了通过测试而放宽业务语义（例如降低入库阈值、去掉去重）。
