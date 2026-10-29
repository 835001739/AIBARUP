# 本地 GPT 接入与排障

> 适用：AIBAR 的提示词扩写、漫画分镜 AI 拆分等所有走 `AI_PROVIDER_*` 的能力。
> 目标：把 Ollama / LM Studio / llama.cpp server / vLLM 等本地模型接进来，并在连不上时给出可执行的结论。

---

## 1. 三步接入

1. **启动本地模型服务**，并确认它暴露 OpenAI-compatible 接口。
2. 在 `.env` 写入：

   ```dotenv
   AI_PROVIDER_ENABLED=true
   AI_PROVIDER_ENDPOINT=http://127.0.0.1:11434/v1
   AI_PROVIDER_MODEL=
   AI_PROVIDER_TIMEOUT=120
   ```

   - `AI_PROVIDER_MODEL` 留空且 `AI_PROVIDER_AUTO_MODEL=true`（默认）时，AIBAR 会自动使用 `/v1/models` 的第一个模型。
   - 本地服务通常不需要 `AI_PROVIDER_API_KEY`。
3. **重启 AIBAR**：

   ```bash
   ./scripts/up.sh restart --force
   ```

   然后打开提示词工作台，点右上角「本地 GPT」状态胶囊 →「测试连接」。

---

## 2. 常见本地服务的端点

| 运行时 | 启动方式（示例） | 端点 |
|---|---|---|
| Ollama | `ollama serve`；`ollama pull qwen2.5:7b` | `http://127.0.0.1:11434/v1` |
| LM Studio | 打开 app → Developer → Start Server（默认 1234） | `http://127.0.0.1:1234/v1` |
| llama.cpp server | `llama-server -m model.gguf --port 8080` | `http://127.0.0.1:8080/v1` |
| vLLM | `vllm serve <model> --port 8000` | `http://127.0.0.1:8000/v1` |

`AI_PROVIDER_ENDPOINT` 写根地址也可以，例如 `http://127.0.0.1:11434`；AIBAR 会自动补 `/v1`。

### 2.1 本项目自带的 Ollama 启停脚本

如果 Ollama 应用放在 `data/ollama/Ollama.app`（或临时的 `tmp/ollama/Ollama.app`），用：

```bash
./scripts/ollama.sh start          # 启动，等待 /api/version
./scripts/ollama.sh status
./scripts/ollama.sh pull qwen2.5:3b
./scripts/ollama.sh stop
```

脚本会把模型与密钥放在 `data/ollama/`（gitignored），不污染仓库。若 Ollama 已通过其他方式安装，直接用官方服务即可，脚本不是必需的。

`./scripts/up.sh start` 会在检测到 `data/ollama/Ollama.app` 时自动拉起本地 GPT；
`stop` 会一并停止。`./scripts/up.sh status` 会多显示一行「本地GPT」。

---

## 3. 端点归一化规则

`core/llm.py::normalize_base_url` 会按顺序处理：

| 你填的 | 实际请求 |
|---|---|
| `http://127.0.0.1:11434` | `http://127.0.0.1:11434/v1/chat/completions` |
| `http://127.0.0.1:11434/v1` | 同上 |
| `http://127.0.0.1:11434/api` | `http://127.0.0.1:11434/v1/...` |
| `http://127.0.0.1:11434/api/chat` | `http://127.0.0.1:11434/v1/...` |
| `http://127.0.0.1:11434/v1/chat/completions` | 去掉接口后缀后重新拼接 |
| `http://127.0.0.1:8080/openai/v1` | 保留自定义前缀 |

因此「按官方文档复制了根地址」「多贴了 `/chat/completions`」都不会再导致 404。

---

## 4. 三种自检方式

### 4.1 界面

提示词工作台右上角状态胶囊：

- 灰点「未启用」：`.env` 的 `AI_PROVIDER_ENABLED` 还是 false；
- 黄点「异常」：已启用但最近一次探测失败；
- 绿点「已连接」：模型列表与极短对话都通过。

点「测试连接」会实际发一条 `ping` 对话，成功才算通过。

### 4.2 命令行（推荐排障用）

```bash
.venv/bin/python scripts/check_local_llm.py
# 临时试一个端点，不写 .env：
.venv/bin/python scripts/check_local_llm.py --endpoint http://127.0.0.1:11434
# 只列模型，不发对话：
.venv/bin/python scripts/check_local_llm.py --no-chat
```

返回码 `0` 表示模型已正常响应，`1` 表示仍未连通。

### 4.3 API

```bash
curl 'http://127.0.0.1:8099/api/prompts/ai-provider/status?probe=1'
curl -X POST 'http://127.0.0.1:8099/api/prompts/ai-provider/test'
```

接口不会返回 API Key；非回环端点只显示「外部服务（地址已隐藏）」。

---

## 5. 故障速查

| 错误码 / 现象 | 原因 | 处理 |
|---|---|---|
| `connection_refused` | 本地服务没启动，或端口不对 | 先 `curl http://127.0.0.1:<端口>/v1/models`；Ollama 默认 11434，LM Studio 默认 1234 |
| `endpoint_not_found`（404） | 端点路径不对，服务其实已通 | 用 `/v1`；AIBAR 已自动归一化根地址与 `/api` |
| `model_not_found`（404） | 模型名不存在 | 看 `/v1/models` 的 `id`，或把 `AI_PROVIDER_MODEL` 留空自动选择 |
| `timeout` | 首次加载大模型 / 显存不足 / 推理太慢 | 调大 `AI_PROVIDER_TIMEOUT`（如 300）；确认模型能在本地终端正常回答 |
| `proxy_error` | `HTTP_PROXY` 把 127.0.0.1 也发给了代理 | 把 `127.0.0.1,localhost,::1` 加入 `NO_PROXY`；AIBAR 也会自动补 |
| `auth_failed`（401/403） | 服务要求 Key，但没配或配错 | 本地服务通常留空；确实需要时填 `AI_PROVIDER_API_KEY` |
| `empty_content` | 模型返回空内容 | 调大 `AI_PROVIDER_MAX_TOKENS`；推理模型可能需要更大预算 |
| `bad_response` | 返回不是 Chat Completions 结构 | 确认端点指向 OpenAI-compatible `/v1`，不是网页或原生 `/api/chat` |
| 胶囊一直「未启用」 | 改了 `.env` 但没重启 | `./scripts/up.sh restart --force` |

---

## 6. 安全与隐私

- API Key 只从环境变量读取，不写数据库、不返回前端、不进日志。
- 日志只记录状态、错误码、模型来源与耗时；不记录提示词正文。
- 外部（非回环）端点只在状态里显示「已配置」，不显示完整地址。
- 请求只发往你配置的端点；AIBAR 不会自动把内容发给第三方。

---

## 7. 实现位置

| 文件 | 职责 |
|---|---|
| `core/llm.py` | 端点归一化、代理兜底、模型发现、错误映射、`chat()` |
| `prompt/providers.py` | 扩写 Provider 接入 + 失败自动回退规则引擎 |
| `comic/storyboard.py` | 漫画分集 AI 拆分复用同一连接层 |
| `prompt/routes.py` | 状态与自检 API |
| `scripts/check_local_llm.py` | 命令行自检 |
| `static/js/studio.js` | 状态胶囊与连接弹窗 |
