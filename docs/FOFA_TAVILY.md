# FOFA 与 Tavily MCP

本文说明 GAsecAgent 的 Tavily 实时 Web 搜索与 FOFA 资产情报接入。两项能力均为可选增强：缺少其中任一 Key 不会影响 Filesystem、Fscan 或另一个可用 MCP；没有任何 MCP 可用时，程序会拒绝进入自动工具调用模式。

## 通用配置原则

- Secret 只从被 Git 忽略的 `.env` 或进程环境读取
- MCP 入口使用项目相对路径，不依赖开发者本机目录
- 缺 Key、入口缺失或连接失败时，只禁用对应 Server
- 只有完成 MCP 握手和 `tools/list` 的 Tool 才会交给 Agent
- stdout 只承载 MCP JSON-RPC；敏感请求信息不写入日志或 Tool Result

## Tavily MCP

GAsecAgent 使用 Tavily 官方 `tavily-mcp@0.2.22`，许可证为 MIT。

- 上游：<https://github.com/tavily-ai/tavily-mcp>
- npm：`tavily-mcp@0.2.22`
- 本地入口：`.external\node-mcp\node_modules\tavily-mcp\build\index.js`
- npm 包 SHA-256：`ac1aaffea2131d87d51c5229195d62d61c081f7bc762024c00af0faf48b8eacf`
- 运行要求：Node.js 20 或更高版本
- Key：`TAVILY_API_KEY`

在项目根目录使用 Windows CMD 安装：

```cmd
set npm_config_cache=%CD%\.cache\npm-runtime
npm install --prefix .external\node-mcp --save-exact tavily-mcp@0.2.22
```

配置：

```env
TAVILY_API_KEY=
GA_TAVILY_MCP_ENTRY=.external\node-mcp\node_modules\tavily-mcp\build\index.js
```

当前官方 Server 的真实 `tools/list` 返回：

```text
tavily_search
tavily_extract
tavily_crawl
tavily_map
tavily_research
```

即使官方包存在 keyless 使用方式，GAsecAgent `v0.1.0` 仍按既定配置规则要求 `TAVILY_API_KEY`；未配置时不启动该 Server，Agent 也不会收到 Tavily Tool。

## FOFA MCP

调研过的社区实现没有明确仓库许可证，并存在硬编码 Key、stdout 输出敏感 URL 或结果、导入期全局 Client、错误被吞掉等问题。GAsecAgent 未复制这些源码，而是依据 FOFA 官方 API 接口独立实现轻量 FastMCP stdio 适配器。

配置：

```env
FOFA_KEY=
```

当前 API 只使用 `FOFA_KEY`。`.env.example` 保留的 `FOFA_EMAIL` 是旧配置兼容占位，适配器不会发送或使用邮箱。Key 不会出现在 Tool Result 或错误文本中。

### `fofa_search`

接受 FOFA 查询语法，参数为：

```text
query, fields, size, page, full
```

默认 `fields` 为 `host,ip,port`，`size` 允许 1～10000。查询使用 UTF-8 Base64 编码后请求 `/api/v1/search/all`，结果行按照 `fields` 转换为 JSON object，并保留总数、页码、模式与 F 点消耗信息。

### `get_alerts`

提供结构化筛选参数：

```text
domain, ip, port, host, body, icon_hash, icp, status_code, size
```

工具会组合 FOFA 查询语句并复用 `fofa_search` 请求链路。空条件不会默认发起宽泛查询，而是返回明确 Tool Error。

HTTP 状态错误、FOFA `error=true`、无效 JSON、网络错误及参数错误均以 MCP Tool Error 回传。适配器不会向 stdout 打印请求 URL、Key 或搜索结果。

## 启用与检查

在项目根目录执行：

```cmd
if not exist .env copy .env.example .env
if not exist mcp.json copy mcp.example.json mcp.json
notepad .env
.venv\Scripts\python.exe main.py --check
.venv\Scripts\python.exe main.py
```

命令会保留已有 `.env` 和 `mcp.json`；如已有配置，直接补充所需变量。缺 Key 时 `--check` 会报告“缺少 Key”，主程序会跳过相应 Server 并继续连接其他 MCP。

## 验证边界

- Tavily：已在 Node.js 24.19.0 LTS 下完成官方 Server 的真实 stdio 握手、`tools/list` 与 5 个工具 schema 验证。
- FOFA：已完成真实 stdio 握手、工具发现、结果及 Tool Error 回传。
- FOFA 本地 HTTP Server 测试覆盖请求编码、结构化结果和 API 错误，但不代表官方在线资产查询成功。
- 当前未使用私人 Key 调用 Tavily 或 FOFA 官方在线 API；权限、配额和在线结果属于部署环境的最终验收事项。
