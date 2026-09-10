# AIBAR API 契约

> 所有接口使用统一 envelope：成功 `{"ok":true,"data":...}`，失败 `{"ok":false,"error":{"code":"...","message":"..."}}`。
> 本文档是前后端并行开发的唯一契约，任何变更必须同步更新本文件。

---

## 通用

- `GET /api/health` → `{status:"ok", version, comfyui:{running:bool}}`
- 分页参数：`page`（默认 1）、`page_size`（默认 24，上限 100）
- 排序参数：`sort` ∈ `recommended|recent|recent_used|most_used`

---

## M1 · ComfyUI 联动

| 方法 | 路径 | 说明 | data |
|---|---|---|---|
| GET | `/api/comfyui/status` | 探测 8188 | `{running, host, port, system_stats?}` |
| POST | `/api/comfyui/start` | 后台拉起 ComfyUI | `{started, message}` |
| GET | `/api/status` | 全站概览 | `{comfyui:{running}, counts:{workflows, images}, last_sync, auto_sync, sync_interval}` |

ComfyUI 未运行时以上接口**不得报错**，只返回 `running:false`。

## M2 · 工作流

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workflows?q=` | 列表：`{id,name,filename,node_count,node_types[],positive_preview,negative_preview,synced_at}` |
| GET | `/api/workflows/<filename>/detail` | 详情：节点清单、正/负提示词全文 |
| GET | `/download/workflow/<filename>` | 下载原始 JSON |

## M3 · 图库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/gallery?page=&page_size=&q=` | `{items:[{id,filename,gallery_path,width,height,size_bytes,prompt,workflow_link,created_at}],total,page,page_size}` |
| GET | `/static/gallery/<path>` | 静态图片 |

图库详情复用 `/api/gallery` 的 item 字段（灯箱展示），新增 `GET /api/gallery/<id>` 返回完整元数据。

## M4 · 同步与日志

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sync/now` | 立即同步一次 → `{workflows, images, added_images, added_workflows, duration_ms}` |
| POST | `/api/sync/auto` | body `{enabled:bool}` → 切换自动同步 |
| GET | `/api/logs?limit=` | `{items:[{id,ts,type,message,status}]}` |

## 导航计数（M8 侧栏）

| 方法 | 路径 | data |
|---|---|---|
| GET | `/api/nav/counts` | `{workflows, images, cases, models}` |

## 案例库 / 模型（M8 导航项，轻量实现）

| 方法 | 路径 | data |
|---|---|---|
| GET | `/api/cases?style=&q=` | `{items:[{id,title,style,image_path,prompt,workflow_name}],styles:[{key,count}]}` |
| GET | `/api/models` | `{items:[{id,name,type,path,size_bytes,ext}],total}` |

---

## M6 · 提示词工作台

### `GET /api/prompt-library?profile=&dimension=&q=`
返回：
```json
{
  "profiles": [{"key","label","description","supports_negative","supports_weight"}],
  "dimensions": [{"key","label","terms":[...]}],
  "templates": [{"id","name","description","profile","positive","negative","dimensions"}],
  "schema_version": 1
}
```
- `profile` 过滤：只返回该模型支持的词条，不支持的标记 `applicable:false`。
- `dimension` 过滤：只返回该维度的词条。

### `POST /api/prompts/expand`
入参：
```json
{"original_prompt":"...","profile":"generic","intensity":"balanced",
 "template_id":null,"options":{},"provider":"rules"}
```
返回：
```json
{
  "original_prompt": "...",
  "expanded_positive": "...",
  "expanded_negative": "",
  "profile": "sd15_sdxl",
  "intensity": "balanced",
  "provider": "rules",
  "sections": [{"dimension","dimension_label","text","is_new"}],
  "additions": ["..."],
  "warnings": ["..."],
  "duration_ms": 3
}
```
校验：空输入 / 仅标点 / 超过 2000 字符 / 未知 `intensity` → `400 invalid_input`，不调用引擎。

### 历史
| 方法 | 路径 |
|---|---|
| GET | `/api/prompts/history?limit=` |
| POST | `/api/prompts/history`（保存一条扩写结果） |
| DELETE | `/api/prompts/history/<id>` |
| PUT | `/api/prompts/history/<id>/favorite` |

---

## M7 · 多模态精品提示词库

### `GET /api/prompt-library/facets?media_type=&dimension=&subcategory=&profile=`
```json
{
  "media_types": [{"key","label","count","favorite_count","recent_count"}],
  "dimensions": [{"key","label","count"}],
  "subcategories": [{"key","label","count"}],
  "tags": [{"key","count"}],
  "profiles": [{"key","count"}],
  "sources": [{"key","label","count"}]
}
```
只返回**当前筛选条件下仍有数据**的层级选项及数量（渐进筛选，无空层级）。

### `GET /api/prompt-library/entries?media_type=&dimension=&subcategory=&profile=&q=&favorite=&source=&tag=&sort=&page=&page_size=`
```json
{
  "items":[{
    "id","media_type","dimension","dimension_label","subcategory","subcategory_label",
    "title","prompt_text","negative_text","language","model_profiles":[],"tags":[],
    "description","source_type","source_label","quality_score",
    "is_favorite","use_count","last_used_at","created_at"
  }],
  "total": 0, "page": 1, "page_size": 24, "has_more": false
}
```

### 写操作
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/prompt-library/entries` | 手动新增 |
| PATCH | `/api/prompt-library/entries/<id>` | 编辑（内置词条编辑后 `source_type` 保持可追溯） |
| DELETE | `/api/prompt-library/entries/<id>` | 删除；内置词条表现为本地隐藏，并写入忽略指纹 |
| POST | `/api/prompt-library/entries/<id>/use` | 记录一次回填 → `{insert_text, separator, duplicate:false}` |
| PUT | `/api/prompt-library/entries/<id>/favorite` | 收藏/取消 |

### 候选审核
| 方法 | 路径 |
|---|---|
| GET | `/api/prompt-library/candidates?status=pending&media_type=&page=&page_size=` |
| POST | `/api/prompt-library/candidates/<id>/approve`（body 可带 `dimension/subcategory/title`） |
| POST | `/api/prompt-library/candidates/<id>/reject`（写入忽略指纹） |

---

## M9 · 图片提示词反推

### `POST /api/prompt-reverse/uploads`
`multipart/form-data`，字段 `file`。返回：
```json
{"upload_id":"...","preview_url":"/api/prompt-reverse/uploads/<id>/preview",
 "filename":"a.png","width":1024,"height":1024,"size_bytes":12345,
 "content_hash":"...","has_metadata":true}
```
限制：仅 `png/jpg/jpeg/webp`，≤20MB，校验真实图片内容；动画图/损坏文件返回 `415 invalid_image`。

### `POST /api/prompt-reverse/jobs`
```json
{"image_id":"...","upload_id":"...","source_mode":"auto|metadata|vision",
 "profile":"generic","precision":"fast|standard|fine","provider":"auto","options":{},
 "async":false}
```
`async` 只能是布尔值，缺省 `false`。两种语义：

| `async` | HTTP | 返回 |
|---|---|---|
| `false`（缺省） | 200 | **同步执行完毕**的完整结构化结果（下方响应体） |
| `true` | 202 | 立即返回的**待处理任务**：`status:"pending"`、`stage:"queued"`、带 `job_id`；真正的工作在后台线程跑 |

前端请用 `async:true` + 轮询 `GET /jobs/<id>`：同步模式下 `job_id` 与结果一起回来，
界面无从得知进度，只能靠定时器假装推进（这正是我们要去掉的假进度条）。

轮询响应字段（两种模式的任务视图一致）：
```json
{"job_id":1,"status":"pending|running|completed|failed|cancelled",
 "stage":"queued|reading|metadata|vision|structuring|done|failed|cancelled",
 "stage_label":"排队中","duration_ms":0,"error_code":""}
```
`status` 进入 `completed|failed|cancelled` 即为终态，此时响应体与下方完整结果一致。
任务若在服务重启后残留为 `pending/running`，首次读取会判死为 `failed` + `error_code:"interrupted"`，
避免前端一直轮询等不到终态。

同步模式 / 终态轮询返回的完整结构化结果：
```json
{
  "job_id": 1, "status":"completed", "stage":"done",
  "source_type":"metadata|vision", "source_label":"原始提示词恢复",
  "provider":"metadata","provider_model":"","model_profile":"generic",
  "precision":"standard","duration_ms": 120,
  "recovered_original_prompt":"...","recovered_negative_prompt":"",
  "sections":[{
     "id":"s1","dimension":"subject","dimension_label":"主体与外观","subcategory":"",
     "text":"...","confidence":0.9,"evidence":"","uncertainty":"",
     "editable":true,"selected_for_library":false
  }],
  "formatted_positive":"...","formatted_negative":"",
  "warnings":[], "quality_tier":"original|basic|advanced",
  "image_ref":{"image_id":null,"upload_id":"...","content_hash":"..."}
}
```

### 其他
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/prompt-reverse/jobs/<id>` | 读取状态与结果 |
| POST | `/api/prompt-reverse/jobs/<id>/cancel` | 取消未完成任务（已完成幂等） |
| PATCH | `/api/prompt-reverse/jobs/<id>/result` | 保存用户编辑：`{sections:[...]}` |
| POST | `/api/prompt-reverse/jobs/<id>/save` | 入库，返回 `{inserted,merged,candidates,discarded,entry_ids:[]}`（幂等） |
| GET | `/api/prompt-reverse/history?limit=&source_type=` | 反推历史 |
| DELETE | `/api/prompt-reverse/history/<id>` | 删除历史并清理无引用上传缓存 |
| GET | `/api/prompt-reverse/providers` | Provider 列表（不返回密钥/完整 endpoint） |
| POST | `/api/prompt-reverse/providers/<key>/consent` | 外部 Provider 外发确认 |
| GET | `/api/prompt-reverse/target-modes` | 目标模式推荐 |

---

## M10/M11 · Provider 中心

### `GET /api/prompt-reverse/providers`
```json
{"items":[{
  "key":"comfyui_blip","label":"ComfyUI 本地 BLIP","local":true,
  "quality_tier":"basic","status":"ready","available":true,
  "reason_code":"","reason":"","model":"blip-image-captioning-base",
  "is_default":false,"capabilities":{"precision":["fast","standard","fine"]},
  "last_checked_at":"..."
}], "default_provider":"comfyui_qwen3vl_8b", "auto_enabled":true}
```
状态枚举：`ready|unconfigured|offline|missing_node|missing_model|missing_mmproj|incompatible_runtime|out_of_memory|unauthorized|error`

### 其他
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/prompt-reverse/providers/refresh` | 绕过缓存重新探测全部 Provider |
| POST | `/api/prompt-reverse/providers/<key>/test` | 分层测试 → `{layers:[{name,status,latency_ms,reason_code}]}` |
| PATCH | `/api/prompt-reverse/providers/default` | body `{provider_key:"..."` 或 `"auto"}` |

---

## M12 · 漫画工作室（大工作流 / ComfyUI 出图队列）

层级：`project → chapter → page`。所有写接口请求体须为 JSON 对象，否则 `400 invalid_input`。

### 项目 `/api/comic/projects`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/projects` | 概览网格：`{items:[{id,name,description,status,chapter_count,page_count,done_count,created_at,updated_at}]}`（状态筛选在前端做） |
| POST | `/api/comic/projects` | 新建：`{name, status:"draft", description?, default_workflow?, global_params?}` → `{id,...}`（`name` 必填，≤200 字） |
| GET | `/api/comic/projects/<id>` | 项目详情 |
| PATCH | `/api/comic/projects/<id>` | 编辑：`{name?,status?,description?,default_workflow?,global_params?}` |
| DELETE | `/api/comic/projects/<id>` | 级联删除其下章节 + 分镜页 + 关联 job（404 若项目不存在） |
| POST | `/api/comic/projects/<id>/generate` | 出图全本：把状态≠`generating` 的分镜页全部入队（含已 `done`，等于整本重生成）→ `{enqueued:int}` |

### 章节 `/api/comic/projects/<id>/chapters`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/projects/<id>/chapters` | 章节列表：`{items:[{id,title,order_idx,summary,page_count,done_count}]}` |
| GET | `/api/comic/chapters/<id>` | 单个章节详情：`{id,title,order_idx,summary,project_id,status,created_at,updated_at}`（章节下钻页 `renderChapter` 依赖此接口） |
| POST | `/api/comic/projects/<id>/chapters` | 新建：`{title, order_idx?, summary?, status:"draft"}`（`title` 必填） |
| PATCH | `/api/comic/chapters/<id>` | 编辑章节 |
| DELETE | `/api/comic/chapters/<id>` | 级联删除分镜页 + 关联 job |
| POST | `/api/comic/chapters/<id>/generate` | 出图整章：把状态≠`generating` 的页全部入队（含已 `done`）→ `{enqueued:int}` |

### 分镜页 `/api/comic/chapters/<id>/pages`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/chapters/<id>/pages` | 分镜列表：`{items:[{id,title,order_idx,prompt_text,negative_text,workflow_filename,seed,status,output_path,created_at}]}` |
| POST | `/api/comic/chapters/<id>/pages` | 新建分镜：`{title?, prompt_text?, negative_text?, workflow_filename?, seed?, order_idx?, status:"pending"}` |
| PATCH | `/api/comic/pages/<id>` | 编辑分镜（含改预制提示词 / 工作流 / 种子） |
| DELETE | `/api/comic/pages/<id>` | 删除分镜页 + 关联 job |
| POST | `/api/comic/pages/<id>/generate` | 首次出图：页入队 → `{job_id, page_id, status:"queued"}`（已 `generating` 拒绝再入队） |
| POST | `/api/comic/pages/<id>/regenerate` | 重生成：无视当前状态重新出图（大工作流完成后单图重出入口）→ `{job_id, page_id, status:"queued"}` |

### 队列与产出图

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/jobs?project_id=&status=` | 出图任务列表：`{items:[{id,page_id,project_id,chapter_id,status,stage,error_code,created_at,finished_at}]}` |
| POST | `/api/comic/jobs/<id>/cancel` | 取消排队中任务，对应页（仅 `queued`）重置 `pending`（已 `done` 页不动；终态任务幂等返回原记录） |
| GET | `/api/comic/output/<path:rel>` | 同源代理 `data/comic_outputs/<rel>` 成图（缺失 404，路径穿越被 `safe_join` 拒绝） |

**状态枚举**：`project.status` ∈ `draft|production|done`；`page.status` ∈ `pending|queued|generating|done|failed`；`job.status` ∈ `queued|running|done|failed|cancelled`。
**出图流程**：worker 读页 `workflow_filename` → `sync.workflow_convert.convert_text` 转 API 图 → 注入 `prompt_text/negative_text/seed` → `reverse.comfyui_client.submit_prompt` 提交 ComfyUI → `wait_history` 轮询 → 取首张成图落盘 `comic_outputs/<project>/<chapter>/<page>_<hash>.png`，写回页 `status=done` + 相对 `output_path`；失败页置 `failed` + `error_code`。

### Skill 导入 `/api/comic/import-skill`（可复用：任意 skill 流程 → 漫画大工作流）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/comic/import-skill` | 把 `SKILL.md` 流程导入为漫画大工作流：`{path?, markdown?, title?, workflow_filename?}` → `{project_id, project_name, workflow_filename, chapters, pages}` |

- **入参**（JSON 对象）：
  - `markdown`：直接粘贴 SKILL.md 全文（优先于 `path`）；
  - `path`：本地 SKILL.md 文件路径（`markdown` 为空时回退使用）；
  - `title`：覆盖项目名（留空用 frontmatter `name`）；
  - `workflow_filename`：指定出图工作流文件名；省略时自动写入 FLUX.2 最小化 13 节点工作流 `flux2_storyboard_aibar.json` 并设为默认。
  - 二者皆空 → `400 invalid_input`；`path` 指向不存在文件 → `404 not_found`。
- **映射规则**：`frontmatter.name` → 项目名；`## 段落` → 章节；段落内编号步骤（`1. …`）→ 分镜页（`prompt_text` = 步骤原文）；段落无编号步骤 → 整段作为单页 `prompt_text`。说明性段落（When to use / 前置条件 / Pitfalls / Verification / Files，含带括号变体，大小写不敏感）自动跳过，不建章节。
- **返回示例**：
  ```json
  {
    "ok": true,
    "data": {
      "project_id": 4,
      "project_name": "comfyui-flux-storyboard",
      "workflow_filename": "flux2_storyboard_aibar.json",
      "chapters": 2,
      "pages": 7
    }
  }
  ```
- **副作用**：自动写入的 FLUX.2 工作流落盘到 ComfyUI 工作流目录（`sync.paths.workflows_dir()`），可由 M2 同步与 M12 出图共同复用；不依赖 ComfyUI 在线。

### 分镜工作流 `/api/comic/projects/<id>/storyboard`（世界观 + 剧情摘要 → 自动分集出图）

从项目的「世界观 / 剧情摘要」一键生成分镜大工作流：自动按「集」拆分剧情，按「出图数量（每章页数）」把每集剧情转换为 N 张分镜页、按「出图风格」为每一页规范提示词，以「集=章节、每集 N 页=分镜页」落库并自动入队出图。对应前端项目详情页的「分镜工作流」面板（可编辑世界观/剧情摘要/出图数量/出图风格，点「自动生成分镜出图」）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/style-presets` | 出图风格清单：`{items:[{key,label,desc}], default:"none"}`，供前端下拉渲染 |
| POST | `/api/comic/projects/<id>/storyboard` | 自动分镜出图：`{profile?, intensity?, provider?, worldview?, plot_summary?, pages_per_chapter?, style?, mode?}` → `{project_id, episodes, chapters, pages, enqueued, mode, removed_chapters, pages_per_chapter, style, style_label, provider, warnings, chapters_detail:[{chapter_id,title,page_ids:[...]}]}` |

- **入参**（全部可选）：
  - `worldview` / `plot_summary`：若提供，先写回项目（编辑器一键生成时复用当前编辑框内容）；省略则用项目已存字段。
  - `pages_per_chapter`：每集（章节）拆出的漫画页数，范围 `[1, 12]`，越界自动收敛；省略则取项目保存的 `pages_per_chapter` 设置（默认 1）。
  - `style`：出图风格（见下表取值）。传入**合法**预设时写回项目的 `style_preset`，并统一套用到**每一集的每一页**；传入非法值时忽略（不污染项目设置），回退项目已存 `style_preset`；省略时取项目 `style_preset`（默认 `none`）。
  - `profile` / `intensity`：透传给提示词扩写引擎（默认 `generic` / `balanced`）。
  - `provider`：省略时按 `AI_PROVIDER_*` 是否启用自动选择（`openai_compatible` 或 `rules`）；AI 拆分或扩写失败一律自动回退规则引擎，整体不失败。
  - `mode`：与已有章节的关系 —— `append`（默认）保留已有章节、新分集追加在后面；`rebuild` 先删除该项目所有已有章节（连带分镜页与队列任务）再重建，用于「改完剧情重新生成」而不至于叠加出重复分集。非法值 → `400 invalid_input`。返回值里的 `removed_chapters` 记录本次清理掉的旧章节数。
- **出图风格取值**（`comic/styles.py`，key 大小写/空格容错）：

  | key | 界面名称 | 规范方式 |
  |---|---|---|
  | `none` | 不限制（按剧情自由发挥） | 不追加风格词，画面由剧情提示词决定 |
  | `japanese_manga` | 日式黑白漫画 | 黑白线稿 + 网点 + 速度线，排除彩色/照片/3D |
  | `shinkai` | 新海诚动画电影风 | 通透天空 + 逆光光晕 + 饱和色彩，排除暗浊/线稿 |
  | `american_comic` | 美式漫画（厚描边平涂） | 硬朗轮廓 + 平涂色块，排除水彩/柔和笔触 |
  | `ink_wash` | 国风水墨 | 写意笔触 + 留白意境，排除霓虹/照片/3D |
  | `watercolor` | 水彩绘本 | 柔和晕染 + 纸纹，排除重描边/暗黑 |
  | `cyberpunk` | 赛博朋克 | 霓虹 + 雨夜反射 + 青紫调色 |
  | `pixel_art` | 像素游戏风 | 16bit 像素块 + 有限调色板，排除写实/平滑渐变 |
  | `realistic` | 写实厚涂插画 | 厚涂笔触 + 真实光影，排除平涂/线稿/Q版 |

- **风格统一（核心保证）**：`styles.apply_style(positive, negative, style)` 对**每一集拆分出的每一页**都追加**完全相同**的一段风格描述词（`positive`）并合并同一套排除项（`negative`），因此同一部漫画的所有页面风格一致；该函数幂等（已含则不重复追加），且超长截断时优先保留风格词。
- **分集拆分规则（确定性、可离线）**：剧情摘要按「第N集 / 第N话 / EPn / 数字.数字 / （数字）」标记或空行分段；单段落内联编号（如「1. a 2. b」）也会拆分；无标记则整体作为一集；上限 `MAX_EPISODES=24` 集。
- **出图数量（每章页数）**：每集剧情会被转换为 `pages_per_chapter` 个分镜页画面描述 —— 先按句切分（中英文句末 + 换行），句数 ≥ N 时均匀取样覆盖首尾；句数 < N 时**循环复用已有句子**（而不是全部堆在末句上）并为每一页叠加不同的镜头景别（远景 / 中景 / 近景 / 特写 / 过肩 / 俯仰 / 动态 / 空镜 等），保证每页得到一句独立、可出图、且页与页之间**有区分度**的画面描述。景别单独存进 `comic_pages.shot_note`，重刷提示词时保留。
- **零配置出图**：项目未设 `default_workflow` 时，自动写入 FLUX.2 最小化工作流 `flux2_storyboard_aibar.json` 并设为默认，使分镜页无需手动指定即经 ComfyUI 出图（与 Skill 导入一致）。
- **扩写**：每个分镜页画面描述经 `prompt.providers.expand`（规则引擎优先，AI 可选）补画面维度（光线 / 构图 / 材质 / 色彩等），结果写入分镜页 `prompt_text` / `negative_text`。
- **校验**：`plot_summary` 为空 → `400 invalid_input`。
- **数据层**：`comic_projects` 新增 `worldview` / `plot_summary` / `pages_per_chapter` / `style_preset` 四列（`core/db.py` 幂等补列，启动迁移自动执行）；`style_preset` 经 `styles.normalize_style` 收敛，非法值保留原值。

### 角色卡（人物一致性）`/api/comic/projects/<id>/characters`

解决「同一个人每一页长得都不一样」：为每个角色维护一张角色卡，生成分镜时把**逐字相同**的锚点描述注入到出现该角色的每一页提示词，并让页级种子由角色组合确定性派生。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/projects/<id>/characters` | 角色卡列表：`{items:[{id,name,aliases,appearance,outfit,palette,negative,seed_offset,is_main,anchor}]}` |
| POST | `/api/comic/projects/<id>/characters` | 新建：`{name, aliases?, appearance?, outfit?, palette?, negative?, seed_offset?, is_main?}`（同名拒绝，上限 12 个）；**不传 `is_main` 时，该项目第一张角色卡默认为主角** |
| POST | `/api/comic/projects/<id>/characters/extract` | 从世界观 / 剧情摘要补充角色卡：`{worldview?, plot_summary?}` → `{characters:[...], created:int, candidates:[name]}`；**只新增缺失角色，绝不覆盖已编辑的卡片** |
| PATCH | `/api/comic/characters/<id>` | 编辑角色卡（任意字段，含 `is_main` 主角开关） |
| DELETE | `/api/comic/characters/<id>` | 删除角色卡（已生成页面的提示词不受影响） |
| POST | `/api/comic/projects/<id>/refresh-prompts` | **重刷提示词**：`{style?, requeue?}` → `{updated, changed, enqueued, style, characters, warnings}` |

- **锚点 `anchor`**：由 `comic/characters.py::build_character_anchor` 按固定顺序拼装（名字 → 外貌 → 服装 → 配色），只拼非空字段，因此同一角色在任何一页得到**逐字相同**的锚点文本。
- **主角贯穿 `is_main`**：主角角色的锚点会注入到该项目**每一页**，即使某一页的文案里没提到他的名字（「他走进森林」这种写法也不会丢人物特征）；非主角仍按文本命中注入。选角由 `select_page_characters` 完成（文本命中 ∪ 主角，顺序恒为角色卡顺序）。
- **抽取规则**（纯规则、可离线，见 `extract_candidates`）：称谓词典直击 + 「人名 + 叙事动词」捕获 + 英文人名 + 「称谓 + 人名」组合捕获；后处理剥掉粘连的动词与称谓前缀（`阿岚说` → `阿岚`、`少女苏叶` → `苏叶`），并用非人名黑名单词典拦掉「骑士沉默不语」这类误抽取；已被完整人名吸收的裸称谓（「少年阿岚」里的「少年」）不再单独成角；称谓后紧跟动词时视为叙事承接而非人名（`引路人递给他` 只出 `引路人`，不会抽出「引路人递」）。
- **候选排序**：出现频次降序 → **首次出场位置**升序 → 名字长度降序。把出场位置放在频次之后，是为了让同频次时**先登场的排第一**（第一张角色卡默认成为主角，按出场顺序排才符合「主角先登场」的直觉）。
- **种子派生**：`derive_seed(base_seed, project_id, chapter_id, page_idx, anchor_key)` → 同一角色组合在同一位置永远得到同一个种子；项目的 `base_seed` 首次生成时由 `default_base_seed(project_id)` 固化，之后可复现。

### 提示词重刷 `/api/comic/projects/<id>/refresh-prompts`

**解决「改完角色外貌，已生成的几十页提示词还停在旧版本」**。每一页在生成时都会把「扩写后、注入锚点与风格词之前」的原文存进 `base_prompt` / `base_negative`，因此重刷时只需重做最后一步组装，不必重新拆分剧情、也不会打乱已有分镜。

| 参数 | 取值 | 说明 |
|---|---|---|
| `style` | 风格 key，可省略 | 覆盖出图风格并写回项目；省略则沿用项目已保存的 `style_preset` |
| `requeue` | `failed` / `all` / `none`，默认 `failed` | 重算后自动重新出图的范围：只重跑失败的页 / 全部重跑 / 只改提示词不出图 |

返回 `{updated, changed, enqueued, style, style_label, characters, character_names, requeue, warnings}`：`updated` 为处理的页数，`changed` 为提示词或种子真的发生变化的页数。

- **幂等**：同样输入重复调用结果完全一致（`changed` 归零），可以放心当「同步按钮」用。
- **兜底**：历史页 / 手工新建页没有 `base_prompt` 时，用当前 `prompt_text` 作为原文，不会把已有提示词清空。
- 组装逻辑集中在纯函数 `storyboard.compose_page_texts`（锚点 → 排除项 → 风格 → 种子），生成与重刷共用同一条路径，避免两处实现漂移。

### 进度与诊断（ComfyUI 衔接）`/api/comic/projects/<id>/progress`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/projects/<id>/progress` | 出图进度总览（见字段表），附带 ComfyUI 在线探测（探测失败降级为 offline，不影响进度数据） |
| GET | `/api/comic/projects/<id>/precheck?check_nodes=1` | 出图前置体检：`{ready, comfyui:{online,latency_ms,base_url}, workflow:{filename,exists,node_count,class_types,missing_nodes,nodes_checked}, queue:{running,pending}, hints:[中文建议], checked_at}` |
| GET | `/api/comic/pages/<id>/editor-link` | 生成「在 ComfyUI 中打开该分镜」的深链接（载入工作流 + 该页提示词 + 排除项）；分镜未指定工作流 → `400 invalid_input` |

`progress` 返回字段：

| 字段 | 说明 |
|---|---|
| `total` / `done` / `failed` / `percent` | 总页数、完成数、失败数与完成百分比 |
| `counts` | `{pending, queued, generating, done, failed}` 各状态页数 |
| `chapters` | 章节级进度：`[{chapter_id, title, total, done, percent}]` |
| `queued_jobs` | 队列中待跑的任务数 |
| `active` | 当前生成中的任务：`{job_id, page_id, page_title, stage, attempt}` |
| `last_error` | 最近一次失败：`{error_code, error_message, page_id, attempt}`（`error_message` 为中文可读文案） |
| `failure_reasons` | 失败原因分布：`[{code, count}]` |
| `characters` | 角色卡数量 |
| `avg_job_seconds` | 最近 5 个已完成任务的实测耗时均值（秒），样本不足为 0 |
| `eta_seconds` | 预计剩余时间 = 均速 ×（排队任务数 + 正在跑的 1 个）；无样本时为 0 |
| `comfyui` | `{online, latency_ms, base_url}`（路由层合并，只读探测） |

- **失败重试**（`comic/runner.py`）：可重试错误码（`comfyui_offline` / `comfyui_timeout` / `submit_failed` / `wait_failed` / `timeout` / `download_failed`）按 `RETRY_BACKOFF_SECONDS` 退避重排，最多 `MAX_ATTEMPTS=3` 次；重试期间任务回到 `queued` 且 `stage='retry_wait'`、`next_retry_at` 指向未来，worker 只捞 `next_retry_at <= now` 的任务。
- **可读错误**（`comic/diagnostics.py`）：错误码 → 中文文案映射（`human_error`），同时写入 `comic_jobs.error_message` 与 `comic_pages.error_message`，前端直接展示，不再出现裸英文报错。
- **数据层**：新增 `comic_characters` 表；`comic_pages` 补 `character_names` / `error_message` / `base_prompt` / `base_negative` / `shot_note`；`comic_jobs` 补 `attempt` / `error_message` / `next_retry_at`；`comic_characters` 补 `is_main`；`comic_projects` 补 `base_seed`（均为 `core/db.py` 幂等补列）。
- **出图加固**（`comic/runner.py`）：
  - 提示词注入不再只看「前两个 `CLIPTextEncode`」的顺序，改为按节点标题（`negative` / `负面` / `反向`）→ 既有文本里的负面词 → 位置 三层兜底，自定工作流上不会把正负提示词写反；
  - 同一页重新出图时会先删掉该页的**历史产出文件**（文件名带内容哈希，不清理会不断堆积）。

### 错误码字典 / 分镜页详情 / 产出图回流图库（F1）/ 单页重扩写（F3）/ 运维回收

本轮整体打磨（易用性 / 可靠性 / 功能扩展，详见 `OPTIMIZATION_REVIEW.md`「整体打磨轮」与 `设计文档.md` §5）新增的接口与数据列：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/error-codes` | 全局错误码 → 中文文案映射（`core.error_text.ERROR_TEXT`），**直接返回 dict 本体**（约 36 条），不是 `{"items":...}`；前端拿到即当字典查 |
| GET | `/api/comic/error-codes` | 漫画模块同款错误码字典（单一数据源同上），同样直接返回 dict |
| GET | `/api/comic/pages/<id>` | 单张分镜页详情（落库全字段，含 `image_id` / `beat_text`）；`sync-gallery` 回填 `image_id` 后前端靠它重新拉取 |
| POST | `/api/comic/projects/<id>/sync-gallery` | 把该项目已完成分镜页的产出图回流进图库 `images` 表（见 F1）；返回 `{scanned, registered, skipped, failed, missing, project_id}` |
| POST | `/api/comic/sync-gallery` | 同上但作用于**全部项目**（body 可省） |
| POST | `/api/comic/pages/<id>/reexpand` | 单页重扩写（F3）：用 `beat_text` 重新扩写并套当前角色锚点 / 风格重组成图；body `{provider?, profile?, intensity?, requeue?}`；返回 `{page_id, previous_prompt, prompt_text, negative_text, base_prompt, changed, reexpanded, provider, style, character_names, job_id, warnings, updated}` |
| GET | `/api/comic/maintenance/stale` | 只读体检：当前卡死（`running`/`generating` 且超过存活阈值）的出图任务与页计数，不做改动 |
| POST | `/api/comic/maintenance/reap` | 回收卡死任务（进程被杀死留下的孤儿）：body `{"requeue": true}` 复位后直接重入队，`{"dry_run": true}` 只统计 |

- **`sync-gallery` 回填规则（F1）**：遍历有 `image_path` 的分镜页，文件仍在盘则经 `sync.scanner.register_image` 入库（按内容 sha256 去重，幂等，落 `static/gallery` 副本 + `images` 表，写回页 `image_id`）；文件已不在盘（ComfyUI 移动过产出）则 `missing++` 并清空页的 `image_path`/`image_id`，让卡片诚实显示「未出图」而非裂图。`failed` 仅统计真正无法解析的图。
- **`reexpand` 行为（F3）**：页有 `beat_text` → 重新走 `_safe_expand` 扩写并重组成图（`reexpanded=true`，`provider` 取实际所用引擎）；无 `beat_text` → 跳过扩写（`reexpanded=false, provider=skip`），仅按当前角色 / 风格重排已有 `base_prompt`，并给 `warnings` 提示「该分镜缺少剧情原文，未重新扩写」。`requeue=true` 时顺带把该页重新入队出图。结果与「重刷提示词」共用 `compose_page_texts` 纯函数，不会漂移。
- **数据层**：`comic_pages` 新增 `image_id`（TEXT，关联 `images.id`，回流图库后填）/ `beat_text`（TEXT NOT NULL DEFAULT ''，分镜工作流落库的剧情原文，重扩写原材料）；均为 `core/db.py` 幂等补列，启动迁移自动执行。
- **错误码形状统一**：`reverse.js` / `comic.js` 的加载器改为 `_errorCodeMap = data || {}`（不再解 `data.items`），与后端 dict 形态对齐。
- **`progress` 新增字段（可靠性）**：`/api/comic/projects/<id>/progress` 现额外返回 `worker`（worker 心跳：`{alive, polls, restarts, stale_seconds}`）与 `stale`（当前卡死任务计数），前端可据此提示「上次进程中断」。`/maintenance/stale` 是只读版体检，`/maintenance/reap` 是修复动作（启动时会自动先 reap 一次）。


---

## M14 · 组图（同一人物的连贯动作序列 + GIF 式连播）

组图 = 一个「同一人物做连贯动作」的有序帧序列。**「强制预设提示词」**是模块核心：
组图上一次性设定 `preset_prefix`（场景/画风）/ `preset_suffix`（镜头/画质）/
`preset_negative`，帧只提供 `action_text`。改了预设后调 `refresh-prompts` 会
**覆盖全部帧**，单帧改不动提示词——这是连播时人物不漂、画风不跳的根本保障。

种子默认 `seed_step=0`（全帧同种子）：同种子 + 不同动作文本 = 模型保留人物只改
动作 = 连贯动作的关键。调大 step 可拉远动作幅度（代价是脸开始漂）。

串行出图（ComfyUI 单卡排队），整组互互斥：同组图不会被两个 worker 抢；单帧
重生成要避让整组出图。

### 组图 CRUD `/api/comic/groups`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/groups?keyword=&limit=&offset=` | 列表 `{items, total}`；每个 item 含 `frame_count`/`done_count`/`is_generating`/`cover_url` |
| POST | `/api/comic/groups` | 新建（body 可同时给首批 `actions:[...]`，与 `action_text` 多行文本二选一） |
| GET | `/api/comic/groups/<id>` | 详情（含全部 `frames:[]` 与解析后 `anchor_text`） |
| PATCH | `/api/comic/groups/<id>` | 修改预设/参数；`refresh:true` 时按新预设重算全部帧提示词 |
| DELETE | `/api/comic/groups/<id>` | 删除组图与全部帧（产出图一并清理） |

body 关键字段：
- `name` / `description`；
- `actor_id` 绑定演员（强烈建议；演员定妆作为人物锚点，是连播脸不漂的保证）；
- `character_id` 绑定角色卡（无演员时作为锚点来源）；
- `preset_prefix` / `preset_suffix` / `preset_negative` 强制预设；
- `anchor_override` 手填人物设定（追加在绑定锚点之后）；
- `seed_step`（默认 0）/ `frame_interval`（连播毫秒，默认 300）/ `loop_play`（默认 true）；
- `actions` 首批动作数组，或 `action_text` 按行拆分。

### 帧管理 `/api/comic/groups/<id>/frames`

| 方法 | 路径 | 说明 |
|---|---|---|
| PUT | `/api/comic/groups/<id>/frames` | **整组替换**动作帧：`{actions:[...]}` 或 `{action_text:"多行"}`。已出图的帧保留（连图一起），序列变短时多余旧帧删除 |
| POST | `/api/comic/groups/<id>/refresh-prompts` | 按当前预设重算全部帧提示词（这是「强制」的执行点；已出图不清） |

### 出图编排 `/api/comic/groups/<id>/generate`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/comic/groups/<id>/generate` | 整组串行出图（后台执行，立即返回进度）；body 可选 `{workflow, only_missing:true, frame_ids:[...]}` |
| POST | `/api/comic/groups/<id>/cancel` | 取消整组出图：worker 在**当前帧结束后**停下（不硬杀线程，避免状态卡在 generating） |
| GET | `/api/comic/groups/<id>/progress` | 出图进度快照：`{status, total, done, failed, pending, generating, running}` |

### 连播清单 `/api/comic/groups/<id>/playlist`

```
GET /api/comic/groups/<id>/playlist
```

返回：

```json
{
  "group_id": 1,
  "name": "舞·挥袖三连",
  "interval": 320,         // 连播毫秒（来自组图 frame_interval）
  "loop": true,
  "items": [
    {"id": 1, "order_idx": 0, "action": "垂手静立", "url": "/static/gallery/..."},
    {"id": 2, "order_idx": 1, "action": "缓缓抬臂", "url": "/static/gallery/..."}
  ]
}
```

只返回 `status='done'` 的帧，前端全量预加载后逐帧切换 `src`（像 GIF 一样播放，不闪）。
URL 优先用图库副本（`/static/gallery/...`），图库登记失败时退回
`/api/comic/output/shotgroups/<id>/<frame_id>_<digest>.png`。

### 单帧管理 `/api/comic/frames/<id>`

| 方法 | 路径 | 说明 |
|---|---|---|
| PATCH | `/api/comic/frames/<id>` | 改单帧：`{action_text}`（提示词恒由预设重算，改不了）或 `{order_idx}`（调序） |
| DELETE | `/api/comic/frames/<id>` | 删单帧，后续帧序整体前移（保持 `order_idx` 连续，连播不会跳号） |
| POST | `/api/comic/frames/<id>/regenerate` | 单帧重生成（后台执行）；body 可选 `{workflow, randomize}`；当该组正在整组出图时拒绝（互斥） |

### 提示词拼装顺序（核心不变量，改动前请读完）

```
[人物锚点]，[一致性指令]，[preset_prefix]，[action_text]，[preset_suffix]
```

锚点前置是 M12 已验证的结论：图像模型对提示词靠前的描述权重更高，锚点之后才
跟一致性指令（`"同一角色在所有画面中保持一致的脸型、发型与服装"`）。

### 数据层（`core/db.py` 幂等迁移）

- 新增 `shot_groups`（21 列）：`actor_id` / `character_id` / `preset_*` /
  `anchor_override` / `workflow` / `base_seed` / `seed_step` / `frame_interval` /
  `loop_play` / `status` / `frame_count` / `done_count` / `cover_path` /
  `last_error` 等；
- 新增 `shot_frames`（14 列）：`group_id` / `order_idx` / `action_text` /
  `prompt_text` / `negative_text` / `seed` / `workflow` / `status` /
  `image_path` / `image_id` / `error_message` 等；
- 索引：`idx_shot_groups_updated` / `idx_shot_groups_status` /
`idx_shot_groups_actor` / `idx_shot_frames_group`（含 order_idx）/
`idx_shot_frames_status`。

### 状态机

`idle`（无帧/一帧都没出）→ `generating`（有 worker 在跑，按 `_RUNNING` 集合强制）
→ `ready`（全部 done） / `partial`（部分 done） / `failed`（全部失败）。
**陷阱**：原版 `else: partial` 会让新建组图（pending 0 / failed 0 / generating 0）
被标成 `partial` —— 必须先用 `if total==0 or done==0 and failed==0: idle` 短路。

### 启动期防误删（`comic/recovery.py`）

`cleanup_orphan_outputs` 启动时会扫描 `data/comic_outputs` 删除无对应分镜页的
孤儿文件。**`actors/` 与 `shotgroups/` 必须列入 `skip_dirs`**——这两类文件
按 `<id>_<hash>.png` 命名，`id` 与 `comic_pages.id` 是三个互不相干的编号空间，
reaper 只看 `comic_pages` 的活着 id，凡同号不撞的 actor / 组图产出都会被当成
孤儿删掉。真实事故（2026-09-02）：reaper 启动时 `removed=3 bytes=4542813` 把
刚生成的 3 张组图产出全删干净，DB 完好、磁盘空空，播放器全空白。

### 运维回收：`reap_stale_jobs` 与 `reap_stale_frames`（启动自动 + 手动触发）

`comic/recovery.py` 提供两套 reap，对应两类实体（**id 命名空间**不同）：

| 实体 | 卡死状态 | reap 函数 | 阈值 | 复位为 | 路由 |
|---|---|---|---|---|---|
| 漫画任务/页面 | `comic_jobs.status='running'`、游离的 `comic_pages.status='generating'` | `reap_stale_jobs` | 15 min | `failed`（任务）+ `pending`（页面） | `GET /api/comic/maintenance/stale`、`POST /api/comic/maintenance/reap` |
| 组图帧 | `shot_frames.status='generating'` | `reap_stale_frames` | 15 min | `failed`（**不**入队） | `GET /api/comic/maintenance/frames-stale`、`POST /api/comic/maintenance/frames-reap` |

启动时 `app.py` 自动依次调两者（顺序：reap_stale_jobs → cleanup_orphan_outputs
→ reap_stale_frames → 拉起 worker，顺序反了会把刚复位的页又抢成 running）。

手动路由都支持：
- `max_age_seconds` 覆盖默认阈值（生产环境紧急清理时可调小）；
- `dry_run=true` 只统计不改动（与 reap_stale_jobs 同语义）；

**为什么有两个 reap**：漫画任务和组图帧是**两个互不相干的 id 命名空间**
（`page_id` vs `frame_id`）。同 `cleanup_orphan_outputs` 的 `skip_dirs` 是同款
问题——共用一个 reap 会误把另一类的活实体当成孤儿。补「自己的 id 命名空间」
= 补 reap 函数 + skip_dirs，缺一就出线上事故（2026-09-02 同日内连续踩两次）。

---

## M14+ · 姿势可控出图（`/api/comic/poses/*`）

把「动作」从**文本**搬到**结构化骨架**上：ControlNet OpenPose 吃骨架图控姿势，
IPAdapter FaceID Plus v2 吃参考图锁脸。两者**正交**（前者改 conditioning，后者
改 model），一起接到 KSampler 上。

### 姿势库与工作流

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/comic/poses` | 预置姿势清单 `{items:[{key,label,desc}]}`（23 个：站立/抱臂/张开双臂/挥手/指向/走路/跑步/跳跃/出拳/踢腿/防御/持剑/挥剑/拉弓/坐姿/蹲下/跪下/鞠躬/思考/敬礼/舞蹈/扛举/后仰） |
| POST | `/api/comic/poses/ensure` | 把全部预置姿势渲染成骨架 PNG 落盘到 ComfyUI `input/aibar_poses/`，并落盘工作流到 `user/default/workflows/aibar_pose_consistent.json`。**幂等**：PNG 内容哈希命名 + 字节比对，内容不变则 skipped；工作流每次覆盖写 |
| POST | `/api/comic/poses/custom` | 用户上传 18 点 keypoints → 渲染落盘 + 加入内存 `POSES`。body `{key, keypoints:[[x,y]*18], label?, desc?}`；key 与 18 点缺一即 `invalid_input` |
| GET | `/api/comic/poses/workflow` | 返回工作流 JSON 文本（前端预览 / 嵌入）；不带参考图/骨架图的**最小版** 7 节点 |

### 工作流节点拓扑（`comic/pose.py::build_pose_workflow`）

```
1  CheckpointLoaderSimple ─┬─ MODEL → 3 IPAdapterUnifiedLoaderFaceID
                          │             (MODEL, IPADAPTER)
4  LoadImage(参考图) ──────┴──────────→ 5 IPAdapterFaceID ── MODEL' ──┐
2  CLIPVisionLoader ──────────────────→ 5                            │
                                                                    ├→ 13 KSampler
9  CLIPTextEncode("正向提示词") ─┐                                   │
10 CLIPTextEncode("负面提示词") ─┴→ 11 ControlNetApplyAdvanced ───────┘
6  LoadImage(骨架图) ───────────→ 11
7  ControlNetLoader → 8 SetUnionControlNetType("openpose") → 11
12 EmptyLatentImage → 13 → 14 VAEDecode → 15 SaveImage
```

- `reference_image` 留空 → 跳过 2/3/4/5（**只控姿势，不锁脸**）；
- `pose_image` 留空 → 跳过 6/7/8/11（**只锁脸，姿势自由**）；
- 两者都留空 → 只剩 1/9/10/12/13/14/15 共 7 节点（纯文生图）。

关键参数（可在 `build_pose_workflow` 调）：
`ipadapter_weight=0.85` / `weight_faceidv2=0.85` / `lora_strength=0.85` /
`embeds_scaling="K+V w/ C penalty"` / `controlnet_strength=0.9` / `steps=28` / `cfg=6.5`。

### 默认模型（都已在用户机器上验证存在）

| 用途 | 模型 |
|---|---|
| Checkpoint | `animagine_xl_3.1.safetensors`（动漫）／ `sd_xl_base_1.0.safetensors`（写实） |
| ControlNet | `xinsir_controlnet-union-sdxl-1.0.safetensors`（切 `openpose` 类型） |
| IPAdapter | `ip-adapter-faceid-plusv2_sdxl.bin` + LoRA `ip-adapter-faceid-plusv2_sdxl_lora.safetensors` |
| CLIP Vision | `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors`（FaceID **PLUS** 必需） |
| InsightFace | `models/insightface/models/buffalo_l/`（provider 可用 `CPU`） |

### 骨架渲染约定

- 18 点 OpenPose 格式，归一化 0~1（**r_* 在图像左侧**，与 OpenPose 标注一致）；
- 缺失点写 `[0, 0]`，渲染时当作「不画」；
- 纯黑底 + OpenPose 官方配色（`POSE_COLORS`）白关节，模型在这套颜色上训练。

### 与 AIBAR runner 的兼容

`sync/workflow_convert.convert_text` 对 API 格式工作流是**透传**，
`_inject_prompt` 靠 `class_type` 含 `CLIPTextEncode` + `_meta.title` 含「负面」
识别正负向、靠 `seed` 键注入种子 —— 本工作流已对齐这套约定（节点 9/10 带中文标题）。

---

## M15 · 视频转序列帧 / GIF（`/api/video/*`）

把视频拆成序列帧、或合成 GIF。三条产物各有明确用途：序列帧用于逐帧二次编辑，
GIF 用于贴进文档 / 聊天窗口，连播用于快速预览。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/video/clips` | 列表（按更新时间倒序，`keyword` 模糊搜名字） |
| POST | `/api/video/clips` | **上传**导入（multipart，字段 `file`，可选字段 `name`） |
| POST | `/api/video/clips/import-path` | 导入**本机已有**视频（body `{path, name?}`，**不复制**） |
| GET | `/api/video/clips/<id>` | 详情（元信息 + 产物状态） |
| DELETE | `/api/video/clips/<id>` | 删除记录 + 帧目录 + GIF；**用户指定的本机源视频不删** |
| POST | `/api/video/clips/<id>/extract` | 抽帧（body 可选 `fps` / `max_frames` / `scale_width` / `start_sec` / `end_sec`） |
| POST | `/api/video/clips/<id>/gif` | 从**已抽出的帧**合成 GIF（body 可选 `fps`） |
| GET | `/api/video/clips/<id>/frames` | 帧清单，返回播放器可直接消费的 playlist 形状 |
| GET | `/api/video/clips/<id>/frame/<file>` | 单帧 JPG（静态） |
| GET | `/api/video/clips/<id>/gif` | GIF 文件 |
| POST | `/api/video/clips/<id>/remove-bg` | 按颜色抠背景，产出带 alpha 的 PNG 到 `nobg` 变体（body 可选 `color` / `similarity` / `blend`；双色追加 `color2` / `similarity2` / `blend2`；连通抠图追加 `mode="flood"` + `seed="x,y"`） |
| POST | `/api/video/clips/<id>/clear-bg` | 撤销去背景，删除 `nobg` 变体目录；原帧不动 |
| POST | `/api/video/clips/<id>/sheet` | 拼序列帧图（sprite sheet），body `cols` / `padding` / `variant` |
| GET | `/api/video/clips/<id>/sheet` | 序列帧图文件（query `?variant=nobg` 取透明版） |

### 设计要点

- **GIF 从已抽出的帧合成，不从原视频重抽**：保证用户预览到的序列帧就是 GIF 里
  播放的内容。另抽一遍哪怕参数相同也可能错开帧，预览与产物对不上最难排查。
- **抽帧幂等**：每次先清空帧目录再写，不会新旧参数的文件混在一起。
- **不建帧表**：视频帧是「一次生成、整体播放」的批量产物，列目录天然有序、
  零维护；真需要逐帧操作时再补表不迟。
- **参数边界**：fps 1–30、宽度 64–1920、帧数上限 1000，越界自动钳制。

### 播放器复用

序列帧连播与 M14 组图连播共用 `static/js/player.js`（`AIBAR.player.open()`）。
`/frames` 直接返回 playlist 形状（`items[{url,label}]` + `interval` + `loop`），
前端无需再转换。`interval` 由 fps 推出（1000/fps），默认按视频原速播。

### 依赖与安全

- 需要系统装 `ffmpeg` / `ffprobe`（自动探测 PATH 与常见安装位置）；
- 外部命令一律带超时（180s），ffmpeg 卡死不会拖挂服务；
- 帧文件名由后端按序号生成，取帧走 `safe_join`，路径穿越返回 404；
- 上传流式落盘（边写边查 500MB 上限），不把大文件读进内存。

### 变体（original / nobg）与去背景 / 拼序列帧图

序列帧存在**两种变体**，所有派生产物（去背景帧、序列帧图、GIF）都按变体区分：

- **`original`**：抽帧得到的 JPG（有压缩噪点），是「源」，任何去背景 / 拼图都
  不改它。
- **`nobg`**：按颜色抠掉背景后的 PNG（带 alpha 通道），放在 `nobg/` 子目录。
  原帧**永不被改动**——`clear-bg` 只删 `nobg` 目录，去背景完全可逆。

**去背景（`POST /remove-bg`）**：

- `color`：可选，抠除的目标色，格式 `#RRGGBB`（或 `#RGB` / 裸 hex）。不传则复用
  上次的颜色；传了格式不对**直接报错**（不会静默回落成绿色去抠用户意想不到的东西）。
- `similarity`：容差 0–1，背景并非纯色（JPG 噪点）要给够，默认 0.3。
- `blend`：边缘羽化 0–1，默认 0.05，避免抠完留一圈硬边。

**双色抠图（可选）**：弹窗勾选「双色抠图」后，可再取第二把钥匙 `color2`
（格式同 `color`）。`similarity2` / `blend2` 不传则复用第一把钥匙的值。
后端合成 `alpha = min(alpha1, alpha2)`——某像素只要**接近任意一把钥匙色**就
透明，因此可同时抠掉两种背景色（例如绿幕 + 蓝幕）。两把钥匙各有独立容差 /
羽化，互不干扰。DB 侧持久化 `bg_color2` / `bg_similarity2` / `bg_blend2`；
`clear-bg` 会一并复位这些字段。

- 颜色键控走 `geq` 平方距离（见下「ffmpeg 颜色键控坑」），本机 ffmpeg 的
  `colorkey` 对 hex 颜色解析有 bug，不能用来路。

**连通抠图（flood，可选）**：弹窗「模式」选「连通抠图（只抠选点连通背景）」后，
body 带 `mode="flood"` 与 `seed="x,y"`（source 帧像素坐标；不传默认左上角
`0,0`，越界自动夹到画面内）。后端从**种子点**做 4-连通区域生长，**以种子色为全局
基准**判定「是不是背景」：

- 某像素要被抠掉，必须**同时满足**：① 颜色与种子色足够接近（`<= similarity` 阈值）；
  ② 通过 4-连通路径连到种子点。
- 因此「画面里另一块同色背景、但被主体隔开」→ 条件②不满足 → **保留**（不连通同色区域保留）。
- **不会跨背景色不一致处扩散**：背景若有渐变 / 偏色，远离种子色的那部分不满足①而自动
  停住，不会顺着渐变一路长到主体里（这正是「逐像素局部容差」区域生长会踩的坑）。
- 连通抠图**忽略双色参数**（`color2` 等），且逐帧用 PIL 计算（纯 Python，无 numpy 依赖）。
  DB 侧持久化 `bg_mode='flood'` / `bg_seed='x,y'`；`clear-bg` 会复位这两项。

**拼序列帧图（`POST /sheet`）**：用 ffmpeg `tile` 把全部帧拼成一张网格图，
body `cols`（列数，1–50，默认 5）/ `padding`（间距 0–50，默认 0）/`variant`
（`original` 或 `nobg`，默认 `original`）。`GET /sheet?variant=nobg` 取透明版。

**抽帧幂等连带重置**：重新抽帧会自动清空 `nobg` 目录与 sheet/gif 文件，并把
`has_nobg` / `has_sheet` / `gif_path` 复位，避免旧派生产物误导界面。

### ffmpeg 颜色键控坑（M15 去背景专用）

本机 ffmpeg（N-97810，2020 构建）的 `colorkey` **只认颜色名**（`green` 等），
`0xRRGGBB` / `#RRGGBB` / `rgb()` 全部解析失败（实测 `colorkey=0x00FF00` 对绿屏
完全无效）。改用 `geq` 滤镜、把键色**作为数值**嵌入表达式，做平方距离判断：

- 纯算术（`+ - * /` 和 `if(lt())`），避开 `sqrt` / `pow` / `st` / `ld`——
  这些在本机 ffmpeg 的 eval 里会**静默失效**导致整条滤镜不工作；
- 平方距离省掉开根号，alpha 用 `if(lt(d2,s2),0,255)`（无羽化）或按
  `[s2,e2]` 区间线性过渡（有羽化）；
- 输出 `format=rgba` + `-pix_fmt rgba -c:v png`，透明 PNG 落到 `nobg/`。

---

## M16 · 视频转绘（视频 → 逐帧提骨架 → ComfyUI 重绘 → 组图连播）

两段式流水线（方案 A）：**prepare** 抽帧并把每帧登记进 `video_paint_frames`，同时在组图管理建一份组图；**pose** 逐帧提骨架；**generate** 骨架 + 参考图喂进姿势可控工作流重绘、写回组图；**nobg** 纯 PIL 连通抠图；**sheet** 拼组图大图。复用 M15 抽帧能力与 M14 组图/播放器。不依赖 numpy/cv2：姿态走 ComfyUI 预处理器，去背景走纯 PIL + deque 区域生长。

### 源视频 / 任务 `/api/videopaint`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/videopaint/clips` | 源视频列表（复用 M15 `/api/video/clips`，挑一个已抽帧的视频） |
| GET | `/api/videopaint/jobs` | 任务列表：`{items:[{id,name,clip_id,group_id,status,frame_count,pose_count,done_count,reference_url,has_nobg,...}]}` |
| POST | `/api/videopaint/jobs` | 新建任务（body 见下） |
| GET | `/api/videopaint/jobs/<id>` | 任务详情（含 `reference_url`） |
| PUT | `/api/videopaint/jobs/<id>` | 改配置（仅 `idle` 态可改：提示词 / 参数 / 抽帧参数） |
| DELETE | `/api/videopaint/jobs/<id>` | 删除任务 + 连带组图 + 帧目录（执行中拒绝） |
| POST | `/api/videopaint/reference-upload` | 先把本地参考图上传到服务端，返回 `{path}`（前端拿 `path` 作为 `reference_path` 建任务） |

新建 body（`POST /jobs`）：
- `clip_id`（必填，源视频 id）；其余可选：`name` / `prompt` / `negative` / `reference_path`（本机绝对路径）或 `reference_name`（已在 ComfyUI input 的文件名，二选一）/ `pose_mode`(`dwpose`|`openpose`) / `pose_resolution` / `steps` / `cfg` / `controlnet_strength` / `ipadapter_weight` / `faceidv2_weight` / `width` / `height` / `base_seed` / `seed_step` / `fps` / `max_frames` / `scale_width` / `start_sec` / `end_sec`。
- 参考图在拿到 `job_id` 后复制到 `data/videopaint/<job_id>/reference.png`（多任务共用同一 clip 不互相覆盖）。

### 流水线 `/api/videopaint/jobs/<id>/<stage>`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/videopaint/jobs/<id>/prepare` | 抽帧 + 建组图 + 登记帧（body 可覆盖 fps/max_frames/scale_width/start_sec/end_sec） |
| POST | `/api/videopaint/jobs/<id>/pose` | 逐帧提骨架（动作拆解），驱动 ComfyUI |
| POST | `/api/videopaint/jobs/<id>/generate` | 逐帧重绘，写回组图 `shot_frames` |
| POST | `/api/videopaint/jobs/<id>/nobg` | 去背景序列帧：body `{mode:"flood", seed:"x,y", similarity, blend, color}` |
| POST | `/api/videopaint/jobs/<id>/sheet` | 拼组图大图：body `{cols, padding, variant:"original"|"nobg"}` |

- `pose` / `generate` 均同步逐帧跑 ComfyUI（每帧一张图），前端用 `timeout:600000` 长超时。
- `generate` 失败帧置 `failed`、成功帧置 `done`；任务最终态 `done`（全成功）/ `partial`（部分失败）/ `failed`（全失败）。已 `done` 的帧重跑会被跳过（幂等），可续跑。
- `nobg` 默认 `mode:"flood"` 连通抠图（与 M15 同语义：`seed` 种子点 4-连通区域生长，`similarity` 容差，`blend` 羽化），纯 PIL、无 numpy。

### 逐帧产物 / 文件服务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/videopaint/jobs/<id>/frames` | 逐帧清单：`{items:[{order_idx,frame_name,status,seed,frame_url,pose_path_url,image_path_url,nobg_path_url}]}` |
| GET | `/api/videopaint/jobs/<id>/file/<kind>/<order_idx>` | 单帧产物：`kind` ∈ `pose`/`image`/`nobg`；`order_idx` 从 0 起 |
| GET | `/api/videopaint/jobs/<id>/file/reference/0` | 参考图（锁脸用） |
| GET | `/api/videopaint/jobs/<id>/file/source/<order_idx>` | 抽帧原图（动作拆解前） |
| GET | `/api/videopaint/jobs/<id>/sheet/<variant>` | 组图大图（`variant` ∈ `original`/`nobg`） |
| GET | `/api/videopaint/jobs/<id>/frames/<order>/workflow-graph` | 该帧姿态重绘的 **UI 格式** 工作流 JSON（供 ComfyUI 编辑器深链接直接 fetch；`no-store`） |
| GET | `/api/videopaint/jobs/<id>/frames/<order>/editor-link` | 打开 ComfyUI 并载入该帧工作流的深链接：`{url, comfyui_url, mode:"graph", workflow_name, prompt, negative, target, bridge_installed}` |

前端「逐帧预览」用 `frame_url` / `pose_path_url` / `image_path_url` / `nobg_path_url` 四格并排对比；「连播生成序列」筛 `image_path_url` 调 `AIBAR.player.open`；「查看组图」跳 `AIBAR.app.navigate('groups')`。

### 工作流按钮（打开 ComfyUI 编辑器调整 / 重新指定帧）

逐帧卡片与详情操作区各有一个「工作流」按钮，点击后：后端把该帧的「参考图锁脸 + 骨架控姿」姿态工作流（API 格式）即时转换成 ComfyUI 编辑器可 `loadGraphData` 的 **UI 格式**（`comic/workflow_ui.api_to_ui_graph`，以 `sync.workflow_convert.convert_ui_to_api` 往返自校验无损），通过 `editor-link` 路由返回深链接；前端 `window.open` 在浏览器新标签打开 ComfyUI（:8188），由 `AIBAR-Bridge` 扩展读 `aibar_wf` 参数 fetch 该 JSON 并载入画布。

- `workflow-graph` 路由：先把参考图（如需）与骨架图上传到 ComfyUI input（`aibar_vp_ref_<job>.png` / `aibar_vp_pose_<job>_<order>.png`），再返回 UI 工作流；**该帧须已有骨架图（先 `pose`/`generate`），否则返回 `invalid_input`**。
- `editor-link` 路由：深链接指向 `workflow-graph` 直链，并带 `aibar_name`（展示名）、`aibar_prompt` / `aibar_neg`（提示词）、`aibar_target=9`（正向提示词节点）。
- 在 ComfyUI 画布里即可改参数 / 换参考图 / **重新指定帧（改 LoadImage 的骨架文件名）** 后多次出图；`bridge_installed:false` 时深链接退化为只打开 ComfyUI 首页。

### 字段 / 状态

- `video_paint_jobs` 状态：`idle`(待准备) / `prepared`(已抽帧) / `posed`(已拆解) / `done`(已完成) / `partial`(部分完成) / `failed`(失败)。
- `video_paint_frames.status`：`pending` / `posed` / `generating` / `done` / `failed`。
- `generate` 把图写回 `comic.shot_frames.image_path` + `video_paint_frames.image_path`，组图管理即可直接连播。



