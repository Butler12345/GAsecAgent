# GAsecAgent 依赖与外部资源基线

本文记录 `v0.1.0` 的可复现依赖、外部 MCP 来源与运行边界。第三方包、本地缓存和安全工具二进制均安装到 Git 忽略目录，不作为 GAsecAgent 自有源码发布。

## 支持与验证环境

| 工具 | 项目要求 | 开发验证版本 |
|---|---|---:|
| Python | `>=3.11,<3.13` | 3.12.9 |
| uv | 使用锁文件安装 | 0.12.18 |
| Node.js | 20 或更高版本 | 24.19.0 LTS |
| npm / npx | 与 Node.js 配套 | 10.2.3 / 10.2.3 |

命令示例面向 Windows CMD。项目资源路径由 `PROJECT_ROOT` 解析，不依赖启动时的当前目录。

## Python 依赖

直接依赖声明在 `pyproject.toml`，完整传递依赖固定在 `uv.lock`。`requirements.txt` 从同一锁文件导出，供只使用 pip 的环境参考，不是独立的版本来源。

### 运行依赖

| 包 | 固定版本 | 用途 |
|---|---:|---|
| `openai-agents` | 0.22.3 | Agent、Runner、streaming 与 MCP 集成 |
| `openai` | 3.19.0 | `AsyncOpenAI` 与 OpenAI-compatible client |
| `httpx` | 0.28.1 | FOFA MCP 异步 HTTP 请求 |
| `mcp` | 1.30.0 | MCP Python SDK 与 FastMCP |
| `numpy` | 2.3.5 | 内存向量与余弦相似度 |
| `python-dotenv` | 1.2.3 | 项目根 `.env` 配置 |
| `rich` | 15.0.0 | 中文彩色 CLI |

### 开发依赖

| 包 | 固定版本 | 用途 |
|---|---:|---|
| `pytest` | 9.1.1 | 单元与回归测试 |
| `pytest-asyncio` | 1.4.0 | 异步 Agent/MCP 测试 |

项目没有直接依赖 Ollama、LangChain、LangChain Community、`mcp-sdk` 或 uvicorn。uvicorn 由 MCP SDK 间接安装。

### Windows CMD 安装

在项目根目录执行：

```cmd
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install uv==0.12.18
set UV_CACHE_DIR=%CD%\.cache\uv
.venv\Scripts\uv.exe sync --all-groups --locked
```

检查锁文件与测试环境：

```cmd
set UV_CACHE_DIR=%CD%\.cache\uv
.venv\Scripts\uv.exe lock --check --offline
.venv\Scripts\python.exe -m pytest -q
```

## Node MCP

在项目根目录安装固定版本：

```cmd
set npm_config_cache=%CD%\.cache\npm-runtime
npm install --prefix .external\node-mcp --save-exact @modelcontextprotocol/server-filesystem@2026.8.31 tavily-mcp@0.2.22
```

### Filesystem

- 来源：<https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem>
- npm：`@modelcontextprotocol/server-filesystem@2026.8.31`
- 核验的上游 commit：`a40bc270fb5ece62673f8a1196f57116d885c5eb`
- npm 包 SHA-256：`a239da270c403c42eb03e1ca7cca07c858085819797b74856ddbb91b50491b1a`
- 入口：`.external\node-mcp\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js`
- 默认允许目录：`workspace`

### Tavily

- 来源：<https://github.com/tavily-ai/tavily-mcp>
- 许可证：MIT
- npm：`tavily-mcp@0.2.22`
- npm 包 SHA-256：`ac1aaffea2131d87d51c5229195d62d61c081f7bc762024c00af0faf48b8eacf`
- 入口：`.external\node-mcp\node_modules\tavily-mcp\build\index.js`
- Key：`TAVILY_API_KEY`

## Fscan 外部工具

- MCP 接口参考：<https://github.com/hnking-star/Fscan_mcp>，核验快照 `a625df9ae0dd27911d3cd9b70ef78ad999441cac`
- 扫描器来源：<https://github.com/shadow1ng/fscan>
- 稳定发行版：`v2.2.0`
- v2.2.0 tag commit：`bf036fd9b272a56d493796badcf0a269f0a83ec0`
- Windows x64 官方 SHA-256：`5aefcbfa98b8e8814dc415a87e4e7e8716004048857d80b37f03e600b6fd2441`

参考 MCP 仓库没有明确许可证，GAsecAgent 未复制其源码。项目独立实现保留 `fscan_scan` 调用语义的 FastMCP 适配器。Fscan 二进制不进入 Git；已核验样本被 Defender 识别为 HackTool，因此没有绕过防护或执行扫描。详见 [Filesystem 与 Fscan](FILESYSTEM_FSCAN.md)。

## FOFA 接口参考

调研过以下社区项目：

- <https://github.com/hnking-star/fofa_MCP>，核验快照 `4878d7f3f1f5d1eaba8aeb77293ecc9aec17fd69`
- <https://github.com/intbjw/fofa-mcp-server>，核验快照 `1bf968745dc20b58c4e3ad97faecdf05c38f79ee`

这些仓库未提供明确的根目录许可证，且实现存在 Secret 或 stdio 兼容问题，因此 GAsecAgent 未复制其源码。项目根据 FOFA 官方 API 接口独立实现 FastMCP 适配器，运行时仅需要 `FOFA_KEY`。详见 [FOFA 与 Tavily](FOFA_TAVILY.md)。

## Git 忽略的本地资源

- `.venv/`：Python 虚拟环境
- `.cache/`：uv、npm 和测试缓存
- `.external/`：第三方源码快照及 Node MCP 安装
- `tools/` 与 `*.exe`：外部工具和二进制
- `.env`、`mcp.json`：本地 Secret 与运行配置
- `workspace/` 的运行内容：Agent 可读写文件，仅保留 `.gitkeep`

## 验证边界

- Filesystem 已通过官方 MCP 的真实 stdio、工具发现、文件操作与目录边界测试。
- Tavily 已通过官方 MCP 的真实 stdio 和工具 schema 测试，尚未使用私人 Key 访问官方在线 API。
- Fscan 适配器已通过真实 MCP 与 Mock 子进程测试，尚未执行外部扫描器。
- FOFA 适配器已通过真实 MCP 和本地 HTTP 往返测试，尚未使用私人 Key 访问官方在线 API。
- LLM 与 Embedding 在线调用需要部署者自己的服务与凭据，未由仓库测试代替。
