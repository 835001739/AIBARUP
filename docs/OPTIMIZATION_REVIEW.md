# 功能与代码优化点排查

排查范围：全项目（Python 约 20567 行 / 63 文件；前端 `static/js` 8 文件 5638 行；后端约 50 条路由）。
方法：两个探查代理分头做只读全量分析 → 逐条核实证据 → 高优先级项当场修复并补回归测试。

结论先说：

- **本轮已修 2 个 P0**（路径穿越可读取目录外文件、`tx()` 假事务），均已实测可利用/可复现，并做了「反证」确认新测试非空断言。
- **`tests/js` 的 12 个失败全是测试过时，无一是生产代码 bug** —— 这是之前一直没查清的疑点。
- **最大的产品缺口是词库词条无法增删改**（原结论）：后端路由写好但前端只接 3 条；**该缺口已在后续会话修复**（见「一（续）· P0-2」），现前端已接通新建 / 编辑 / 删除，产品闭环补齐。
- **未发现** SQL 注入与密钥硬编码问题。

---

## 一、本轮已修复

### P0-1 路径穿越：`?workflow=../<兄弟目录>/x.json` 能读到工作流目录之外的文件

**位置**：`sync/comfyui_link.py::workflow_file_analysis`

```python
# 修复前
target = (base.resolve() / Path(filename)).resolve()
if not str(target).startswith(str(base.resolve())):   # ← 前缀判断没有分隔符
    return {}
```

`filename` 直接来自 URL 查询参数 `GET /api/comfyui/editor-link?workflow=...`，未经任何白名单校验。
`startswith` 挡得住绝对路径（`/etc/passwd` 解析后不以 base 开头），但**挡不住兄弟目录**：
base 为 `.../workflows` 时，`../workflows-evil/secret.json` 会解析成 `.../workflows-evil/secret.json`，
前缀判断照样通过。

**实测可利用**（修复前）：

```
GET /api/comfyui/editor-link?workflow=../workflows-evil/secret.json
→ {"mode": "library", "prompt": "TOP-SECRET-LEAKED-42", ...}
```

目录外的 JSON 被解析后，内容通过 `prompt` 字段带回响应 —— 一次有限但真实的任意文件读。

**修复**：`sync/paths.py` 新增 `safe_join()`（拒绝 `..` / 绝对路径 / 盘符，并用 `is_relative_to` 做边界判断），
`sync/routes.py::_safe_join` 收敛为对它的薄封装（原实现其实是正确的，只是在 `web` 层重复了一份）。
`sync/comfyui_link.py` 改用它。

**回归测试**：`tests/py/test_comfyui_link.py` 新增 2 条（单测覆盖 6 种逃逸写法 + 端到端不泄露）。
反证：同一输入下旧逻辑放行、新逻辑拒绝，确认测试有效。

### P0-2 `tx()` 是假事务，唯一使用者会留下脏状态

**位置**：`core/db.py`

```python
def execute(sql, params=()):
    cur = conn.execute(sql, params)
    conn.commit()          # ← 每句无条件提交，tx() 的 with conn: 形同虚设
    return cur
```

全项目 `tx()` 只有一处使用 —— `reverse/providers/registry.py:326 set_default()`：

```python
with tx():
    execute("UPDATE prompt_provider_preferences SET is_default=0 ...")   # 已提交
    execute("INSERT ... ON CONFLICT ... DO UPDATE ...")                  # 失败则前功尽弃
```

第二步失败时第一步**已经落盘**，库里会留下「没有任何默认 Provider」的脏状态。

**修复**：线程局部深度计数 `_local.tx_depth`；`tx()` 嵌套时只有最外层 `with conn:` 收口，
`execute()` 在事务内不自提交。

> 坑：`sqlite3.Connection` **不允许挂自定义属性**（`setattr` 直接 `AttributeError`），
> 所以深度标记只能放 `threading.local()` —— 正好与连接本身同为线程局部。

**回归测试**：`tests/py/test_sync.py` 新增 2 条（失败回滚、嵌套、异常后标记复原）。同样做了反证。

---

## 一（续）· 后续会话补充修复

> 以下 P0-1、P0-2、P1-5、P1-6、P1-8、P1-9 在后续会话中已修复（对应验证与设计记录见 `docs/设计文档.md` §5.1–§5.4）。代码已 grep 复核，非空断言回归测试齐备。

### P0-1 反推真实进度 + 可中断 ✅

- `static/js/reverse.js` 去掉客户端假进度条（`setInterval` 点亮一格），改为轮询 `GET /api/prompt-reverse/jobs/<id>` 渲染后端真实 `stage`：`pollTimer` 字段注释明确「轮询真实进度的定时器（替代原来的假进度条）」（`reverse.js:207`）；`pollOnce()` 经 `api().get(.../jobs/<id>)` 拉真实阶段（`:784-789`、`:773` 起 Timer）。
- 提交携带 `AbortController`（`reverse.js:210`），点「取消」调用 `.../jobs/<id>/cancel` 并 `controller.abort()` 中断在途请求（`:757`）。

### P0-2 词库 CRUD 前端入口 ✅

- `static/js/library.js` 词条卡片补「编辑 / 删除」按钮（`:740` / `:750`）+ 工具栏「新建词条」模态（`:976`）+ 删除确认写入忽略指纹（`:1090-1097`），复用 `ui().modal`；后端 `POST/PATCH/DELETE /api/prompt-library/entries` 现已接通，产品闭环补齐。

### P1-5 错误码工厂 ✅

- `core/errors.py::not_found()`（→404）、`unavailable()`（→503）强制启用；`sync/routes.py:267` 的 `not_found` 落到 404（原 400），`reverse/service.py:738/767` 的 `unavailable` 统一 503；`provider_unavailable` 不再在 400/503 间摇摆。
- 验证：`tests/py/test_error_handling.py`（状态码语义）、`test_errors.py`。

### P1-6 N+1 查询 ✅

- `reverse/service.py::_stale_reasons`（`:407`）一次 `IN` 预取图片存在性 + 纯函数 `_scan_superseded`，100 条历史由 201 次 → 2 次 SQL。
- `promptlib/learning.py::_NearDupIndex`（`:381`）按 `(media_type,dimension)` 缓存候选，每个 batch 只加载一次（`_find_near_duplicate` `:434`），批内新增经 `register()` 立即可见。
- 验证：`tests/py/test_reverse_stale.py`、`test_prompt_learning.py`（含 `loads==1` 锁证）。

### P1-8 大文件一次性读入内存 ✅

- `core/imagemeta.py::read_png_text_chunks`（`:111`）按 64KB chunk 流式读取，仅缓冲文本块，`_skip_bytes` 用 `seek` 跳过 IDAT，命中 `IEND` 即停；缺 IDAT 的畸形 PNG 由 `validate_image_file` 统一 `invalid_image`。
- 验证：`tests/py/test_imagemeta.py`（24 项）。实测 24.5MB 文件仅读 5.6KB，峰值内存 24MB→8.6KB。

### P1-9 吞异常掩盖真实故障 ✅

- `reverse/uploads.py` 区分可预期 `_EXPECTED_IMAGE_ERRORS=(OSError,ValueError,EOFError)`（→415 `invalid_image`）与程序缺陷（→500 + `_LOGGER.error("...error_type=%s", type(exc).__name__)`，保留异常链，`:207-213`）；dedup 命中后 `owned=None` 不删共享文件（`_discard` `:224`）。
- 验证：`tests/py/test_error_handling.py`（写失败→500 非 415、不留临时文件、`upload_failure_after_dedup_hit_keeps_shared_file` 反证）。

---

## 二、待修复清单

> 状态说明：本节中标注 **✅ 已修复** 的条目已在后续会话完成（详见「一（续）」），仅保留原描述作追溯；其余条目仍为待修复。

### P0

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | ✅ **已修复** · 反推进度条是假的，且取消不中断在途请求（见「一（续）」） | `static/js/reverse.js` | 进度条已改为轮询后端真实 `stage`，并提交用 `AbortController` 中断在途请求。 |
| 2 | ✅ **已修复** · 词库词条无法增删改（见「一（续）」） | `static/js/library.js` | 后端 `POST/PATCH/DELETE /api/prompt-library/entries` 已接通前端「新建/编辑/删除」入口。 |

### P1

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 3 | **12 个前端测试失败全是测试过时** | `tests/js/` | 已逐条核实，无一是生产 bug：<br>• `api-request` 8 项：测试桩 `jsonResponse()` 返回**裸对象**而非 Promise，而 `api.js:96` 按规范写 `fetch(...).then(...)` —— 返回真 Promise 的 2 项全过，是桩错了。<br>• `api-envelope` 2 项：`:17` 期望 `buildQuery` 为 `undefined`，但同文件 `:81` 又在用 `api.buildQuery`，断言自相矛盾。<br>• `insert-at-cursor` 2 项：期望 `cursorPos=6`，实际 `3+1+4=8`；同文件 `:31` 同类用例写的就是 8 —— 夹具陈旧（疑似 `'窗台'` 改成 `'坐在窗台'` 未同步）。<br>**改**：修 4 处测试（不动生产码），并把 `node --test tests/js/` 接进 CI。 |
| 4 | **工作流/画廊/案例/模型四个列表无分页，数据静默截断** | `pages.js:84/636/796`、`studio.js:395` | 一律 `page_size: 60` 取第一页，超过 60 条用户**永远看不到且无任何提示**。当前只有 `lib-pager`、`lib-cand-pager` 两个分页容器。<br>**改**：复用 `library.js:683 renderPager`。 |
| 5 | ✅ **已修复** · HTTP 状态码不统一（见「一（续）」） | `core/errors.py` | `not_found()`→404、`unavailable()`→503 已强制启用，不再摇摆。 |
| 6 | ✅ **已修复** · N+1 查询（见「一（续）」） | `reverse/service.py`、`promptlib/learning.py` | `list_history` 与近重复学习均已批处理预取。 |
| 7 | **分面统计把整列 JSON 拉进 Python 计数** | `promptlib/entries.py:578` | `get_facets()` 对 `tags`/`profiles` 各全表扫一遍，10k 词条 × 整段 JSON 全部载入内存解析。<br>**改**：改用 SQLite `json_each()` 在 SQL 内 `GROUP BY`，或加缓存。（当前仅 156 词条，YAGNI，待规模上来再改） |
| 8 | ✅ **已修复** · 大文件一次性读入内存（见「一（续）」） | `core/imagemeta.py` | `read_png_text_chunks` 已改为流式读取，命中 `IEND` 即停。 |
| 9 | ✅ **已修复** · 吞异常掩盖真实故障（见「一（续）」） | `reverse/uploads.py` | 区分可预期异常（→415）与程序缺陷（→500 + `error_type`），不再统一伪装。 |
| 10 | **重复代码**（各 3–4 份） | 见下表 | 应下沉到 `core/`。 |

| 逻辑 | 位置 | 建议 |
|---|---|---|
| `_to_bool` | `sync/routes.py:69`、`prompt/routes.py:82`、`promptlib/routes.py:40` | → `core/web.py` |
| `_json_body` | `prompt/routes.py:37`、`promptlib/routes.py:31`、`reverse/routes.py:39` | → `core/web.py`（注意行为已分叉：后者**静默返回 {}**，另两个抛 400） |
| `_int_arg` | `sync/routes.py:45`、`prompt/routes.py:46`（逐字相同） | → `core/web.py` |
| Blueprint errorhandler | `sync/routes.py:720`、`prompt/routes.py:298`、`promptlib/routes.py:192`、`reverse/routes.py:238` | 只在 `app.py:129` 保留一处全局 handler |
| `fillSelect` / `startComfyUI` / `ui()` / `api()` 取值器 | `library.js`、`reverse.js`、`app.js`（各 2–5 份） | 收敛到 `AIBAR.ui` |
| 双全局命名空间 | `api.js:208`、`library.js:1039`、`reverse.js:1357` 另挂 `window.AibarUtil` | `AibarUtil` 只留兼容别名 |

### P2

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 11 | `models.scan_models()` 缓存穿透时 `rglob` 整个模型盘 | `sync/routes.py:629`、`sync/models.py:22` | 被侧栏 `/api/nav/counts` 每次渲染触发；虽有 300s 缓存，穿透时扫数 GB 文件。建议 stale-while-revalidate。 |
| 12 | `instr(model_profiles, '"x"')` 匹配 JSON 数组必然全表扫 | `promptlib/entries.py:352/355` | 索引完全用不上。建议改 `EXISTS (SELECT 1 FROM json_each(...))` 或冗余关联表。 |
| 13 | `validate_image_file` 连续 3 次 `stat()`；`sha256_file` 与 `_file_sha256` 两份相同实现 | `core/imagemeta.py:199`、`sync/scanner.py:312`、`reverse/uploads.py:84` | 合并到 `core/imagemeta.py`。 |
| 14 | 灯箱无焦点陷阱 | `pages.js:535-600` | 手工构建，未走 `ui.modal`/`pushLayer`，不在 `layerStack` 中，Tab 会跑到背景。 |
| 15 | 长列表不虚拟化 | `pages.js:14/459` | 画廊每次 append 48 条，累加后 DOM 无回收。 |
| 16 | 移动端覆盖不足 | `static/css/components.css` | 该文件 1011 行，**0 处** `@media`；全站仅 7 处。 |
| 17 | `ACCEPTANCE.md` 已失真 | `ACCEPTANCE.md:47/55` | 称「`tests/js`（7 文件）齐备、215 passed 全绿」，实际 11 文件、122 例、12 失败。 |

---

## 三、功能缺口对照

前端 57 处 API 调用 vs 后端约 50 条路由 —— **反向缺口为零**（前端调的接口后端全有），契约一致性是这个项目的强项。

**后端有 / 前端无**（真正的缺口）：

| 接口 | 后端 | 缺失能力 | 状态 |
|---|---|---|---|
| `POST /api/prompt-library/entries` | `promptlib/routes.py:104` | 手动新增词条 | ✅ 已接通（`library.js` 新建词条） |
| `PATCH /api/prompt-library/entries/<id>` | `:113` | 编辑词条 | ✅ 已接通（`library.js` 编辑） |
| `DELETE /api/prompt-library/entries/<id>` | `:122` | 删除词条 | ✅ 已接通（`library.js` 删除确认） |
| `GET /api/prompt-reverse/jobs/<id>` | `reverse/routes.py` | 真实进度轮询 | ✅ 已接通（`reverse.js` 轮询真实 stage） |
| `GET /api/workflows/<f>/graph` | `sync/routes.py:290` | 工作流图结构可视化 | ⏳ 后端就绪，前端入口未接 |
| `GET /api/gallery/<id>/workflow` | `sync/routes.py:564` | 图片 → 工作流溯源 | ⏳ 后端就绪，前端入口未接 |

---

## 四、已核实「不是问题」的项

排查中一并验证过，避免后续重复投入：

- **SQL 注入**：无。所有 f-string 拼接的都是内部白名单（`_ORDER_BY` 字典查表、`_build_where` 的列名常量），用户输入一律走 `?` 占位符。
- **密钥硬编码**：无。`OPENAI_VISION_API_KEY` / `AI_PROVIDER_API_KEY` 均来自 `.env`（已在 `.gitignore`），无明文落盘。
- **XSS**：整体安全。全量 96 处 `innerHTML` 只有两类：赋 `''` 清空，或注入 `AIBAR.icons.get()` 的可信 SVG；用户数据一律走 `textContent`。唯一泛型 sink 是 `ui.js:235/352` 的 `bodyHtml`，当前唯一调用方 `confirm()` 已 `escapeHtml` —— 建议加注释约定「仅接受可信静态 HTML」。
- **WAL / 索引**：`core/db.py:269` 已开 WAL，索引覆盖较完整。

---

## 五、建议执行顺序

> 注：P0-1、P0-2、P1-5、P1-6、P1-8、P1-9 已在后续会话修复（见「一（续）」），下表相应项已划掉，可跳过。

1. ~~P0-1 反推真实进度 + 可中断~~ ✅ 已修复
2. ~~P0-2 词库 CRUD 入口~~ ✅ 已修复
3. **P1-3 修 4 处过时测试并接 CI**（成本低，且能终结「12 个失败到底是啥」的长期疑问；当前 `tests/js` 仍有 12 项失败，均为测试桩/夹具陈旧，非生产 bug）
4. **P1-4 四个列表分页**（数据静默截断是隐性丢数据）
5. ~~P1-5 错误码工厂 + P1-9 异常处理~~ ✅ 已修复
6. 其余按 P1（P1-7 分面计数、P1-10 重复代码）/ P2 顺序推进

---

## 附：本轮改动与验证

| 文件 | 改动 |
|---|---|
| `sync/paths.py` | 新增 `safe_join()`（路径穿越防护的唯一实现） |
| `sync/comfyui_link.py` | `workflow_file_analysis` 改用 `safe_join` |
| `sync/routes.py` | `_safe_join` 收敛为薄封装；清理不再使用的 `import os`、`PurePosixPath` |
| `core/db.py` | `tx()` 真事务化（线程局部深度计数）；`execute()` 事务内不自提交 |
| `tests/py/test_comfyui_link.py` | +2 路径穿越回归测试 |
| `tests/py/test_sync.py` | +2 事务回归测试 |

**验证**：`tests/py` **249 passed**（245 → 249）；桥梁前端 16/16 通过；深链全链路 `PASS=18 WARN=0 FAIL=0`；
外部复测 `../workflows-evil/secret.json` 已被拦截，正常功能不受影响。

---

## 六、整体打磨轮（易用性 / 可靠性 / 功能扩展）

> 用户原话：*「再整体打磨下项目，找出还有优化的地方，可以从易用性，可靠性这几个方向，以及功能扩展提升等方面进行分析」*。本轮在先前 P0/P1 修复基础上，沿三个方向继续推进（编号 #73–#83），全部代码完成、单测与浏览器走查通过。

### 可靠性（#73–#78）· 进程崩溃不留孤儿

- **卡死任务回收**（`comic/recovery.py`）：`GET /maintenance/stale` 只读体检、`POST /maintenance/reap`（`requeue` 复位后重入队 / `dry_run` 只统计）；**应用启动时自动 reap 一次**（日志 `comic_stale_reaped jobs=N pages=M`），并清理 ComfyUI 已移走的孤儿产出（`comic_orphan_outputs_removed`）。
- **worker 心跳**：`progress.worker={alive, polls, restarts, stale_seconds}` + `progress.stale`，前端可据以提示「上次进程中断」。
- **验证**：重启 AIBAR 后 `/maintenance/stale` 返回 `stale_jobs:0`，上一轮遗留的卡死 job 128 / page 135 已被清除；`progress.worker.stale_seconds≈1` 心跳存活。

### 易用性（#79–#81）

- 项目详情头部补齐「分镜 N / 完成 M」聚合计数（`get_project` 此前漏选 count 子查询，显示为 `0 / 0`）。
- 错误码形态统一：全局 `/api/error-codes` 与 `/api/comic/error-codes` **直接返回 dict**（约 36 条），前端 `_errorCodeMap = data || {}`（不再解 `data.items`）。
- 其余交互细节打磨（见 PR 内改动与 `使用说明书.md`）。

### 功能扩展（#82 F1 产出图回流图库 / #83 F3 单页重扩写 + F4 按条件批量重出）

- **F1 产出图回流图库**：`sync.scanner.register_image`（内容 sha256 去重、幂等），`runner._register_gallery` 出图成功后回填 `comic_pages.image_id`；`service.sync_gallery` 提供单项目 / 全项目补跑入口，并**清掉悬空 `image_path`/`image_id`**（产出文件已被 ComfyUI 移走时 `missing++`，不让卡片裂图）。漫画成图进图库后，M9「反推提示词」与图库深链对其可用，闭环打通。
- **F3 单页重扩写**：`storyboard.reexpand_page` 用分镜页 `beat_text`（分镜工作流落库的剧情原文）重新扩写并经 `compose_page_texts` 按当前角色锚点 / 风格重排（与「重刷提示词」共用同一纯函数，不漂移）；`generate_storyboard` 落库每页 `beat_text`；前端每页新增「重扩写」按钮。
- **F4 按条件批量重出**：项目工具栏「按条件重出」弹窗，按范围（全部页 / 只重跑失败的 / 只跑还没出图的）批量入队，实时显示各范围页数。
  - **修复浏览器崩溃**：`generateProjectFiltered` 曾误把 `api()` 已解包的响应体 `{items:[...]}` 当数组传入 `openGenerateFilterModal`，触发 `(pages || []).forEach is not a function`（在 `.then` 异步回调里抛出，被 `window.addEventListener('unhandledrejection')` 捕获才发现）；改为传 `(data && data.items) || []` 后弹窗正常弹出（「全部页 7 张 / 失败 0 张 / 未出图 1 张」）。

### 本轮新增改动与验证

| 文件 | 改动 |
|---|---|
| `sync/scanner.py` | 新增 `register_image(path, prompt, workflow_link)`（sha256 去重、幂等），导出到 `__all__` |
| `core/db.py` | 幂等补列 `comic_pages.image_id` / `comic_pages.beat_text` |
| `comic/runner.py` | 出图成功后 `_register_gallery` 回填 `image_id` |
| `comic/service.py` | `get_project` 补 count 子查询；`create_page`/`update_page` 处理 `beat_text`；新增 `sync_gallery`（含悬空引用清理） |
| `comic/storyboard.py` | `generate_storyboard` 落库 `beat_text`；新增 `reexpand_page` |
| `comic/routes.py` | 新增 `GET /pages/<id>`、`POST /projects/<id>/sync-gallery`、`POST /sync-gallery`、`POST /pages/<id>/reexpand`；`GET /error-codes` 改返回 dict；既有 `/maintenance/stale`、`/maintenance/reap` |
| `app.py` | `GET /api/error-codes` 改返回 dict |
| `static/js/comic.js` | 卡片加「反推提示词 / 重扩写」按钮；`generateProjectFiltered` 修复 `data.items` 解包；`reversePageImage`/`reexpandPage` |
| `static/js/reverse.js` | `loadErrorCodes` 改为 `data || {}` |
| `static/js/icons.js` | 去重 `filter`/`activity` 键，`sparkles` 用于重扩写 |
| `tests/py/test_comic_extensions.py` | 本轮新增 18 项（register_image / sync_gallery / reexpand / 新路由） |

**验证**：`tests/py` **523 passed**（505 → 523，+18）；`tests/js` **161 passed / 0 failed**；全部改动 JS 文件 `node --check` 通过；浏览器 `agent-browser` 实测：启动 reap 清卡死、`sync-gallery` 回填项目 4（registered 6）、`reexpand` 工作、「按条件重出」弹窗正常、项目详情计数正确、错误码 dict 直查。截图见 `tmp/polish1_pages.png`、`tmp/screenshot-1788171586514.png`。

---

## 七、M17 · FLUX.2 Klein 结构控制工作流（2026-09-08）

> 用户原话：*「请帮我开发一个 flux2 带 controlnet 的工作流，检查依赖并安装好」*。
> 本轮决策：放弃 FLUX.2-dev 真 ControlNet 路线，改走 **Klein 4B + ReferenceLatent** 路线
> （架构级结构控制）；零下载、零安装、零额外权重，本机磁盘 28GB / M3 Pro 36GB 上唯一可用方案。

### 为什么不是"真 ControlNet"

调查三处官方源（截至 2026-09-08）：

| 来源 | 结论 |
|---|---|
| `comfy/controlnet.py` 全文搜索 | 仅 `ControlNetFlux`（Flux1 架构），无 Flux2 分支 |
| 上游 `origin/master`（`git fetch origin master` 后 183 个新 commit） | 仍未加 Flux2 ControlNet；唯一相关提交是 `Minimax h3 controlnet as a model patch`（与 Flux2 无关） |
| DiffSynth-Studio / `Template-KleinBase4B-ControlNet` | 真 ControlNet 但仅 DiffSynth 生态（需 24-40GB CUDA GPU + `klein-base-4B`），无 ComfyUI 节点 |
| `JLC-Flux2-ControlNet` + `alibaba-pai/FLUX.2-dev-Fun-Controlnet-Union` | ComfyUI 节点，但需 FLUX.2-dev（23.8GB bf16 装不下，fp8 MPS 不支持，36GB 内存跑不动 bf16） |

结论：本机约束下唯一可行方案是 **Klein 4B + `comfy_extras/nodes_edit_model.py::ReferenceLatent`**（BFL 为 Klein 训练的多参考图原生机制），属于「架构级 ControlNet」——结构图作为 VAE latent 挂到 conditioning，由 DiT 的 attention 直接消费。

### 实际验证（端到端）

| 验证项 | 结果 |
|---|---|
| ComfyUI object_info（`/object_info` 全量拉取） | 14 个目标节点全部存在：`UNETLoader` / `CLIPLoader(type=flux2)` / `VAELoader` / `EmptyFlux2LatentImage` / `Flux2Scheduler` / `ReferenceLatent` / `VAEEncode` / `CFGGuider` / `BasicGuider` / `KSamplerSelect` / `RandomNoise` / `SamplerCustomAdvanced` / `CLIPTextEncode` / `LoadImage` / `VAEDecode` / `SaveImage` |
| 预处理器模型（`comfyui_controlnet_aux/ckpts/`） | DWPose (`dw-ll_ucoco_384.onnx` 128M + `yolox_l.onnx` 207M) 与 OpenPose (`body_pose_model.pth` 200M + `hand/facenet`) 均已下载 |
| 模型三件套 | `flux-2-klein-4b.safetensors`（7.8GB，纯 transformer，DiT 块前缀 `double_blocks.*` / `single_blocks.*`）/ `qwen_3_4b.safetensors`（8GB，纯 Qwen3-4B，`model.*` 前缀）/ `flux2-vae.safetensors`（0.3GB，`decoder.*`/`encoder.*`） |
| 最小文生图（1024×1024, 4 步, CFG=1.0, euler） | 83 秒，橘子猫窗台图清晰 |
| ReferenceLatent 对比（prompt 同一：白狗；无 vs 有猫图参考） | 无参考 → 白色比熊犬；有参考 → **白色长毛猫**（坐姿、窗台、阳光方向、木地板**完全复刻**猫参考图）→ 证明控制链路在结构/构图/光照层级生效 |
| API→UI roundtrip（`comic.workflow_ui.api_to_ui_graph` + `sync.workflow_convert.convert_ui_to_api`） | 三档（t2i / ref / full）全部 `ok=True`，确认 `AIBAR-Bridge::loadGraphData` 能正确载入 |

### 决策清单

1. **不升级 ComfyUI**（8/7 版本 vs 上游 9/8）：183 个新提交涉及 flux2 仅 minimax h3 controlnet（不相关），现有版本已含 `Flux2Scheduler` / `EmptyFlux2LatentImage` / `CLIPLoader(type=flux2)` / `Flux2.extra_conds` 继承 `Flux.reference_latents` 处理。升级风险（破坏现有 M12/M14/M16 工作流）大于收益。
2. **不下载 FLUX.2-dev / JLC-Flux2-ControlNet**：磁盘 28GB 装不下；MPS 不支持 fp8；36GB 内存跑不动 bf16。
3. **不依赖 DiffSynth 真 ControlNet**：仅 DiffSynth 生态，无 ComfyUI 节点 → 接入 AIBARUP 体系成本巨大。
4. **走 `BasicGuider` 还是 `CFGGuider`**：默认 `CFGGuider(cfg=1.0)`，给负向留接口；`use_basic_guider=True` 时切 `BasicGuider`（只吃 positive，等价 cfg=1，节点数 -1）。
5. **节点编号沿用 `comic.pose.build_pose_workflow` 习惯**（1-3 模型、4-5 提示词、14-15 调度），便于 `comic.workflow_ui.api_to_ui_graph` 与 `sync.comfyui_link.build_editor_link_graph` 复用。

### 本轮新增改动

| 文件 | 改动 |
|---|---|
| `comic/flux2.py` | 新增（248 行）：`DEFAULT_DIFFUSION/TEXT_ENCODER/VAE/STEPS_DISTILLED/CFG_DISTILLED/...` 常量 + `build_flux2_workflow(...)`（21 节点 API 图构造） + `ensure_flux2_workflow(...)`（落盘到 `user/default/workflows/aibar_flux2_structural.json`，自动扫描 input/） |
| `tests/py/test_comic_flux2.py` | 新增（13 项）：节点拓扑稳定性（6）+ roundtrip（3 case）+ UI 图形状 + link target_slot 契约 + 落盘 + 防回归（自动扫描） |
| `docs/设计文档.md` | §3.6 末追加 M17 段（节点拓扑 + 关键决策 + 依赖清单） |
| `docs/使用说明书.md` | 新增 §4.13（Python 调用示例 + 节点表 + 排障） |
| `docs/OPTIMIZATION_REVIEW.md` | 本段 |

**验证**：`tests/py` **708 passed**（695 → 708，+13）；零回归；ComfyUI 端到端 1024×1024 / 4 步实测 83 秒，输出图清晰；落盘文件 `user/default/workflows/aibar_flux2_structural.json` 幂等覆盖。

---

## 八、M17 · 视频转绘加 FLUX.2 并列按钮（2026-09-08）

> 上一轮只交付了 Python 侧的 `comic.flux2`，用户在前端点不到——视频转绘里
> 「打开工作流」按钮只会打开 SDXL 姿态链路。本轮把 FLUX.2 Klein 链路也接到
> 同一帧的逐帧卡片与详情操作区，与「工作流」按钮**并列**，同段前置逻辑、
> 不同构造函数、不同深链 `target`。

### 改动表

| 文件 | 改动 |
|---|---|
| `videopaint/service.py` | 抽出 `_prepare_frame_workflow_inputs(job_id, order_idx)` 公共前置（参考图 + 骨架图上传 + seed 计算）；新增 `build_frame_flux2_ui_graph` / `frame_flux2_editor_link`（Klein 蒸馏默认 4 步 / CFG 1.0 / 1024×1024；`build_flux2_workflow` 显式传 `diffusion/text_encoder/vae` 常量便于测试钉死） |
| `videopaint/routes.py` | `frame_flux2_workflow_graph`（UI JSON, `Cache-Control: no-store`）+ `frame_flux2_editor_link`（深链 dict）两个新路由；docstring 路由表同步 |
| `static/js/videopaint.js` | 抽出 `openWorkflowLink(url, errText)` 公共深链打开逻辑（GET → window.open + 桥接未装 warning）；新增 `frameFlux2Btn(f)` + `openFrameFlux2Workflow(jobId, orderIdx)`；帧卡片与详情操作区各挂 1 个 FLUX.2 按钮（sparkles 图标），与原「工作流」按钮并排 |
| `static/css/pages.css` | `.vp-frame-actions` 加 `gap: 6px` + `.btn { flex: 1 1 0; min-width: 0 }`；两按钮等宽并排，单按钮时仍占满一行 |
| `tests/py/test_videopaint_workflow_ui.py` | +5：上传+构建、参数钉死（步骤/采样器/扩散/编码器/VAE/Guider）、骨架缺失拦截、editor-link URL 形状、UI roundtrip |
| `tests/js/videopaint-wiring.test.js` | +3：路由表加两条 + 「FLUX.2 按钮并列」断言（按钮文案 / sparkles 图标 / `flux2-editor-link` 字面量 / `frameFlux2Btn(f)` 接线 / 后端路由 ↔ service 函数对齐）+ `target="4"` 钉死（Klein 正向 CLIPTextEncode 节点 id） |
| `comic/workflow_ui.py` | `_normalize_api` 把 inputs 里的 `None` 值丢掉：UI→API 回写时把未填的可选挂件补成 `None`（如 CLIPLoader.device），与「原图根本没有这个键」语义等价——先前 roundtrip 会误报 mismatch，现统一视为「未填，用节点默认值」 |
| `sync/workflow_convert.py` | `convert_ui_to_api` 在写 widgets_values 时**跳过 None**：避免 `value_not_in_list` 校验报错（实测 `device: None not in ['default', 'cpu']`），桥接路径（loadGraphData→运行）也避免踩坑 |
| `docs/设计文档.md` / `docs/使用说明书.md` / `docs/OPTIMIZATION_REVIEW.md` | 同步 M17→M16 的接入细节（按钮表格、target 差异、采样默认、路由变更） |

### 真机端到端（同一帧 job=1 order=0，prompt="1girl, blue coat..."）

| 链路 | URL | nodes | target | 实测出图 | 时长 |
|---|---|---|---|---|---|
| SDXL 姿态链路（既有的） | `/editor-link` | 15（KSampler + ControlNet + IPAdapter） | `9` | 已验证 | ~25s |
| **FLUX.2 Klein 链路（新增）** | `/flux2-editor-link` | 21（UNET + 双 ReferenceLatent） | `4` | 1024×1024，姿态跟随 DWPose 骨架；蓝色大衣 | ~190s |

### 关键决策

1. **不共用端点 + `engine=` query**：姿势链路和 Klein 链路是两个不同的「工作流」概念，深链 `target` 不同（`9` vs `4`），如果共用一个端点加参数会让 URL 语义变脆。两条独立路由 + 两条独立 service 函数 + 两个独立 JS 入口，更直白。
2. **采样参数不继承任务**：任务字段是 SDXL 训练出来的（28 步 / cfg 6.5 / 896×1152）。Klein 蒸馏版吃这套直接出废图（黑色 / 高频噪声）。函数里显式传 `DEFAULT_STEPS_DISTILLED` / `DEFAULT_CFG_DISTILLED` / `DEFAULT_WIDTH` / `DEFAULT_HEIGHT`，与任务字段解耦。
3. **`_normalize_api` None 归一**：不止本轮受益——所有走 roundtrip 比对的人脸/动物测试都可能被可选 None 误报。改的是「None ≡ 未填」的语义一致性，没改任何对得上 ComfyUI 实际行为的语义。
4. **`openWorkflowLink` 抽出**：M14 时代 `openFrameWorkflow` 内联 toast 与 window.open；M17 新增 FLUX.2 后必然需要复用。两函数只剩一行 `GET + url 拼接`，body 走公共函数。

### 验证

- `tests/py` **713 passed**（708 → 713，+5 新增）；零回归。
- `tests/js/videopaint-wiring.test.js` **19 passed**（16 → 19，+3 新增）。
- 真机端到端：取 `/flux2-workflow-graph` → `convert_ui_to_api` → `POST /prompt` → history `success=True` → `AIBAR_vp_flux2_00001_.png`（1024×1024，姿态跟随）。

---

## 九、M17.3 · FLUX.2 姿势参考 + 人物一致（2026-09-08）

### 改动

| 文件 | 改动 |
|---|---|
| `comic/flux2.py` | 新增 `_add_reference()`（单参考链构造，两个 builder 共用）；`build_flux2_workflow` 重构为调用它（原 6-13 节点 id 由单测钉死，零回归）；新增 `build_flux2_pose_workflow()` / `ensure_flux2_pose_workflow()`；新增常量 `FLUX2_POSE_WORKFLOW_FILENAME` / `MAX_CHARACTER_REFS=3` / `CHARACTER_REF_ID_BASE=100` |
| `tests/py/test_comic_flux2.py` | +8 项（13 → 21）：单链 / 三人物+姿势 / 上限截断 / 无姿势 / helper 复用一致性 / full roundtrip / 落盘 / 自动扫图 |
| `docs/设计文档.md` | §3.6 M17 段补 `build_flux2_pose_workflow` 的链构造、ID 分配、上限依据与实测限制 |
| `docs/使用说明书.md` | 新增 §4.14 |

### 设计要点

1. **`ReferenceLatent` 没有权重参数**（`/object_info` 实测：`input` 仅 `required.conditioning` + `optional.latent`）。因此「参考图有多重要」**只能靠重复次数表达** → `character_images` 设计成 `Sequence[str]`，`["a","a","a"]` 即权重 ×3；设 `MAX_CHARACTER_REFS=3` 防止注意力被参考图吃满。
2. **ID 命名空间隔离**：人物链从 `100` 起、步长 4，刻意避让 `build_flux2_workflow` 的 6-13，两套图不冲突；姿势链追加在所有人物链之后，保持「先锁人、后锁姿势」的链序语义。
3. **`_add_reference` 抽公共 helper**：两个 builder 的参考链构造完全一致，不抽就会漂移。重构后旧 builder 的 21 节点拓扑与 id 一字未变（单测验证）。

### ⚠️ 实测结论（真机 5 组出图，1024×1024 / 4 步 / CFG 1.0 / seed 12345）

同人设图 ×2 + 同提示词，只换姿势图：

| 对比 | 输入图差异 | 出图 MAD | 判定 |
|---|---|---|---|
| 骨架 A vs 骨架 B | 5.73 | 1.97 | ❌ 没跟随 |
| 真实帧 A vs 真实帧 B | 25.98 | 3.33 | ❌ 没跟随 |
| 有骨架 vs 无骨架 | — | 9.67 | ⚠️ 仅整体扰动 |
| 骨架 vs 真实帧（都当姿势参考） | 52.63 | 66.73 | ⚠️ 整图被拖向照片 |

与人设参考图平均色差：骨架 / 无姿势 **1.24~1.36**（人物保住）；真实帧 **59.5~60.6**（人物丢失）。

**两条决策性结论**：

1. **Klein + ReferenceLatent 不是姿势控制机制**，是外观 / 整体构图迁移。姿势参考无论用骨架还是真实照片都被忽略（输出差异远小于输入差异）。
2. **用真实照片当姿势参考比骨架更糟**——会把人换掉。这条推翻了过程中的初始假设（原以为骨架离训练分布远、换真实照片会好），**只有真跑出来才发现**。

因此 `pose_image` 的语义从「动作控制」降级为「构图参考」，docstring 与两份文档都写明了限制；严格姿势控制指向 M14（SDXL + ControlNet-Union openpose）。

### 验证

- `tests/py` **721 passed**（713 → 721，+8 新增）；零回归。
- 真机 5 组出图全部 `success`，1024×1024。
- 落盘 `ComfyUI/user/default/workflows/aibar_flux2_pose_consistent.json`（21 节点完整链）。

---

## 十、M14 复核 · SDXL + ControlNet-Union openpose 工作流（2026-09-08）

### 背景

M17.3 实测发现 Klein + ReferenceLatent 做不到姿势控制，结论指向「严格姿势走 M14」。
为验证该结论，对 `comic/pose.py::build_pose_workflow` 做了完整复核与真机对比。

### 改动

| 文件 | 改动 |
|---|---|
| `comic/pose.py` | 新增 `POSE_OPENPOSE_FILENAME`（纯 openpose 版文件名）；`ensure_pose_workflow` 新增 `filename` 与 `auto_fill` 两个可选参数（均向后兼容），`__all__` 补导出 |
| `tests/py/test_comic_pose.py` | +2 项：`test_ensure_pose_workflow_auto_fill_false_keeps_pure_openpose`（双向钉死 auto_fill 开/关的节点数差异 + 纯版无 IPAdapter 链 + `type=openpose`）、`test_pose_openpose_filename_differs_from_default` |
| `docs/使用说明书.md` | 新增 §4.15（两个变体 / 构造示例 / 拓扑 / 模型依赖 / 素材约定 / 排障） |
| `docs/设计文档.md` | §3.6 补 M14 姿势链路复核段 |

### ⚠️ 踩到的坑：`auto_fill` 让「纯 openpose 版」根本落不出来

`ensure_pose_workflow` 默认在图像留空时自动扫 `input/aibar_poses/` 补齐。
想落纯控姿版时，只留空 `reference_image` **不够**——扫描会把第一张 `actor_*.png`
补进来，落出来仍是 15 节点锁脸版（第一次运行就复现了：预期 11 节点，实际 15）。

⇒ 新增 `auto_fill: bool = True`（默认保持向后兼容），落纯版必须显式 `auto_fill=False`。
测试双向覆盖：开 → 15 节点、关 → 11 节点，避免以后有人"顺手删掉这个参数"。

### 实测对比（真机，同 seed 12345，同一对骨架 pose_0000 / pose_0002）

| 链路 | 输入差异 | 出图 MAD | >16 像素占比 | 判定 |
|---|---|---|---|---|
| SDXL + ControlNet-Union openpose | 5.73 | **50.28** | 72.8% | ✅ 姿势驱动出图 |
| FLUX.2 Klein + ReferenceLatent | 5.73 | 1.97 | 3.3% | ❌ 几乎无响应 |

差一个数量级 —— 证实「真 ControlNet」与「注意力迁移参考」在姿势控制上是两回事。

### ⚠️ 另一个发现：DWPose 反向校验在本项目不可用

想用「出图 → 抽骨架 → 对比输入骨架」自动校验姿势，结果四张出图抽出来**全是纯黑**
（mean=0, stddev=0）。做对照实验：拿参考图 `actor_ref.png` 本身去抽，**同样全黑**。
⇒ 是 `yolox_l.onnx`（真实人像训练）在动漫风图上失效，不是出图有问题
（出图 stddev 42~64，内容正常）。已写进文档排障表：**姿势正确性当前只能肉眼判**，
自动校验需换检测器或改用人像照片。

### 验证

- `tests/py` **723 passed**（721 → 723，+2 新增）；零回归。
- 落盘两个工作流：`aibar_pose_consistent.json`（15 节点）/ `aibar_pose_openpose.json`（11 节点）。
- 真机出图：SDXL 896×1152 ×2 均 `success`；`SetUnionControlNetType.type=openpose` 校验通过。
- 模型依赖全部就位（animagine_xl_3.1 / xinsir-union-sdxl / CLIP-ViT-H / ip-adapter-faceid-plusv2 / insightface buffalo_l）。
  `models/loras/` 下 LoRA 是**符号链接**，`du -h` 显示 0B 属正常（实际 371MB）。
