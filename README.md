# GAsecAgent

GAsecAgent 是一个基于大语言模型（LLM）、RAG 与 MCP（Model Context Protocol）的中文自动化安全 Agent。

项目通过自然语言交互，让 LLM 根据任务自动选择并调用 MCP 工具，可用于授权安全测试、资产信息查询、安全分析和本地知识库增强等场景。

基于 OpenAI Agents SDK 构建，支持 OpenAI-compatible 模型，并可通过 `mcp.json` 灵活扩展 MCP Server。

```text
用户任务 -> LLM Agent -> MCP Tool -> Tool Result -> LLM Agent -> ... -> 最终回答
```

当前版本为 `v0.1.0`。本项目仅用于已获得明确授权的安全测试、教学与研究；使用者必须遵守目标授权范围和适用法律。

## 核心能力

- **自然语言交互**：通过中文自然语言向 Agent 下达安全分析与工具调用任务。
- **Agent 工具调用**：基于 OpenAI Agents SDK，由模型自主选择并连续调用工具。
- **MCP Server 集成**：通过 `mcp.json` 配置多个 MCP Server，支持动态工具发现与调用。
- **安全工具接入**：已实现 Filesystem、Fscan、Tavily、FOFA 等 MCP 接入能力。
- **多轮上下文**：支持进程内多轮对话，使 Agent 能结合前文继续完成任务。
- **流式输出**：模型回答及工具调用过程以 CLI 形式实时展示。
- **RAG 知识库增强**：可选加载本地知识库，通过 Embedding 与向量检索增强回答。
- **模型可配置**：支持 OpenAI-compatible LLM，可配置模型、Base URL 和生成参数。

### MCP 接入

| MCP | 实现方式 | 主要配置 |
|---|---|---|
| Filesystem | 官方 `@modelcontextprotocol/server-filesystem@2026.8.31` | `GA_FILESYSTEM_ROOT`，默认 `workspace` |
| Fscan | 独立 FastMCP 适配器，调用外部 `fscan` v2.2.0 二进制 | `GA_FSCAN_BINARY`、`GA_FSCAN_SHA256` |
| Tavily | 官方 `tavily-mcp@0.2.22` | `TAVILY_API_KEY` |
| FOFA | 独立 FastMCP 适配器，调用 FOFA 官方 API | `FOFA_KEY` |

`v0.1.0` 当前接入 Filesystem、Fscan、Tavily 和 FOFA，其他安全工具不在本版本范围内。

## 环境要求

- Windows 10/11；本文命令使用 CMD
- Python 3.11 或 3.12
- Node.js 20 或更高版本；本项目使用 Node.js 24.19.0 LTS 验证
- npm / npx
- Git
- 仅在启用相应能力时，需要访问 LLM、Embedding、Tavily 或 FOFA 服务的网络与凭据

## 快速开始

以下命令均在项目根目录执行。

### 1. 安装 Python 依赖

推荐按 `uv.lock` 创建可复现环境：

```cmd
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install uv==0.12.18
set UV_CACHE_DIR=%CD%\.cache\uv
.venv\Scripts\uv.exe sync --all-groups --locked
```

`requirements.txt` 是从同一锁文件导出的 pip 兼容清单。Python 的完整传递依赖以 `uv.lock` 为准。

### 2. 安装官方 Node MCP

```cmd
set npm_config_cache=%CD%\.cache\npm-runtime
npm install --prefix .external\node-mcp --save-exact @modelcontextprotocol/server-filesystem@2026.8.31 tavily-mcp@0.2.22
```

第三方包安装在被 Git 忽略的 `.external` 目录，不作为 GAsecAgent 源码提交。

### 3. 创建本地配置

```cmd
if not exist .env copy .env.example .env
if not exist mcp.json copy mcp.example.json mcp.json
notepad .env
```

上述命令只在目标文件不存在时创建配置，不会覆盖已有 `.env` 或 `mcp.json`。两个文件均已被 Git 忽略；不要把真实 Key 写入示例文件或提交到 Git。

### 4. 检查并启动

```cmd
.venv\Scripts\python.exe main.py --check
.venv\Scripts\python.exe main.py
```

启动时可选择是否开启 RAG。输入任务后用空行提交；输入 `quit` 或 `退出` 可直接退出。即使从其他当前目录使用绝对路径启动 `main.py`，项目资源仍按 GAsecAgent 根目录解析。

## 配置

### LLM

至少配置一个支持 Chat Completions、streaming 与 tool calling 的 OpenAI-compatible 服务：

```env
GA_LLM_API_KEY=
GA_LLM_BASE_URL=
GA_LLM_MODEL=
```

项目不绑定特定供应商，可连接 DeepSeek、OpenAI 或其他兼容服务。默认参数可在 `.env` 中调整：

```env
GA_LLM_TEMPERATURE=0.6
GA_LLM_TOP_P=0.9
GA_LLM_MAX_TOKENS=20000
GA_LLM_MAX_TURNS=10
GA_HISTORY_MAX_TURNS=50
```

### RAG 与 Embedding

默认配置兼容 DashScope / 百炼的 OpenAI-compatible Embeddings API：

```env
GA_EMBEDDING_API_KEY=
GA_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
GA_EMBEDDING_MODEL=text-embedding-v3
GA_EMBEDDING_DIMENSIONS=1024
GA_RAG_CHUNK_SIZE=5000
GA_RAG_TOP_K=1
GA_KNOWLEDGE_BASE_DIR=knowledge_base_docs
```

知识库读取配置目录顶层的 UTF-8 文本文件。启动时选择 `no` 会跳过 Embedding Client 初始化。

### MCP

`mcp.json` 使用 `mcpServers` object。`${PROJECT_ROOT}` 由程序解析为项目根目录，`${ENV_NAME}` 从 `.env` 或进程环境展开。只有 `env` 中明确声明的变量会传给 MCP 子进程。

Filesystem 默认只授权项目中的独立工作目录：

```env
GA_FILESYSTEM_ROOT=workspace
```

相对路径始终基于项目根解析。默认设置不会向 Filesystem MCP 暴露项目根目录、`.env`、`mcp.json` 或源码；如需其他目录，可在自己的 `.env` 中显式覆盖。运行时写入 `workspace` 的文件默认被 Git 忽略。

其他 MCP 的主要配置为：

```env
GA_FSCAN_BINARY=tools\fscan\fscan.exe
GA_FSCAN_SHA256=5aefcbfa98b8e8814dc415a87e4e7e8716004048857d80b37f03e600b6fd2441
TAVILY_API_KEY=
FOFA_KEY=
```

缺少 Fscan 二进制、Tavily Key 或 FOFA Key 时，对应服务器会单独禁用，其他 MCP 仍可连接。若没有任何 MCP 可用，程序会拒绝进入自动工具调用模式，而不是静默退化成普通聊天机器人。

详细安装、来源与接口说明：

- [Filesystem 与 Fscan](docs/FILESYSTEM_FSCAN.md)
- [FOFA 与 Tavily](docs/FOFA_TAVILY.md)
- [依赖与外部资源基线](docs/DEPENDENCIES.md)

## 环境检查

```cmd
.venv\Scripts\python.exe main.py --check
```

检查结果会区分未配置、缺少 Key、缺少外部程序、配置存在但尚未连接等状态。`--check` 不启动 MCP；只有主程序完成握手、`tools/list` 并报告连接成功后，工具才会交给 Agent。

## 测试与验证

### 自动化测试

当前版本的 CLI、配置、Agent streaming、多轮上下文、RAG、MCP 管理、输出规范化及四类 MCP 适配逻辑已纳入完整测试套件。发布前实际运行结果为 `85 passed`。可执行：

```cmd
set UV_CACHE_DIR=%CD%\.cache\uv
.venv\Scripts\uv.exe lock --check --offline
.venv\Scripts\python.exe -m compileall -q main.py gasecagent tests
.venv\Scripts\python.exe -m pytest -q
```

### 真实 MCP 协议与工具调用

- Filesystem：真实 stdio 握手、工具发现、列目录、创建、读取、修改文件，以及阻止访问 `workspace` 外部路径。
- Fscan：真实 stdio 握手、`fscan_scan` schema、参数处理、缺少二进制及错误回传。
- Tavily：官方 MCP 真实 stdio 握手和 5 个工具 schema 发现。
- FOFA：真实 stdio 握手、工具发现、结果与 Tool Error 回传。

### 本地替身环境测试

- Fscan 使用 Mock 子进程验证命令参数、超时、隔离结果文件和 Tool Result 契约；这不是一次真实扫描。
- FOFA 使用本地 HTTP Server 验证请求编码、结构化结果和 API 错误处理；这不是官方 FOFA 在线查询。
- Agent 使用 ScriptedModel 验证 streaming、工具事件和多轮上下文；这不是公网 LLM 调用。

### 待外部环境验收

- 使用用户自己的凭据完成 LLM、Embedding、Tavily 和 FOFA 官方在线调用。
- 在明确授权且隔离的测试环境中执行真实 Fscan 扫描。

Fscan 二进制不随仓库分发。项目核验过的 v2.2.0 Windows x64 文件哈希与上游发布记录一致，但本机 Defender 将其识别为 HackTool；项目未关闭防护、添加排除项、强制恢复或执行该文件。哈希一致不等于安全背书。

## License

[MIT License](LICENSE) © 2026 GAlun
