# Filesystem 与 Fscan MCP

本文说明 GAsecAgent 的 Filesystem 与 Fscan 接入方式、外部依赖、配置与验证边界。两者均通过 stdio MCP 接入现有单 Agent 工具循环。

## Filesystem MCP

GAsecAgent 使用 Model Context Protocol 官方参考服务器 `@modelcontextprotocol/server-filesystem@2026.8.31`，没有用普通 Python 文件函数代替 MCP。

- 上游：<https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem>
- 固定版本：`2026.8.31`
- 核验的上游 commit：`a40bc270fb5ece62673f8a1196f57116d885c5eb`
- npm 包 SHA-256：`a239da270c403c42eb03e1ca7cca07c858085819797b74856ddbb91b50491b1a`
- 本地入口：`.external\node-mcp\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js`
- 运行要求：Node.js 20 或更高版本

在项目根目录使用 Windows CMD 安装：

```cmd
set npm_config_cache=%CD%\.cache\npm-runtime
npm install --prefix .external\node-mcp --save-exact @modelcontextprotocol/server-filesystem@2026.8.31
```

`.external` 与 `.cache` 均被 Git 忽略。

### 访问边界

允许目录由官方 Server 的命令行参数接收，GAsecAgent 通过以下环境变量传入：

```env
GA_FILESYSTEM_ROOT=workspace
GA_FILESYSTEM_MCP_ENTRY=.external\node-mcp\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js
```

默认根目录是项目下的独立 `workspace`，而不是项目根目录，因此 Filesystem Tool 默认无法读取 `.env`、`mcp.json` 或源码。相对路径以项目根为基准；用户可以在自己的 `.env` 中显式覆盖访问根，示例配置不会覆盖现有值。

启动前会检查入口文件与访问目录。Filesystem 连接失败只会禁用该 MCP，不会阻断其他配置正确的 MCP。

### 已验证行为

在 Node.js 24.19.0 LTS 下，已通过官方 Server 的真实 stdio 握手与 `tools/list`，并在 `workspace` 中完成列目录、创建、读取和修改文件。路径遍历读取项目根 `.env` 以及直接访问 `workspace` 外源码的尝试均被官方 Server 拒绝。

## Fscan MCP

原始功能参考来自外部项目 [hnking-star/Fscan_mcp](https://github.com/hnking-star/Fscan_mcp) 的 `fscanss.py`。该仓库未提供明确许可证，因此 GAsecAgent 没有复制其源码，而是依据公开工具接口独立实现 FastMCP stdio 适配器。

- 参考项目快照：`a625df9ae0dd27911d3cd9b70ef78ad999441cac`
- MCP Tool：`fscan_scan`
- 外部扫描器：[shadow1ng/fscan](https://github.com/shadow1ng/fscan) `v2.2.0`
- v2.2.0 tag commit：`bf036fd9b272a56d493796badcf0a269f0a83ec0`
- Windows x64 官方 SHA-256：`5aefcbfa98b8e8814dc415a87e4e7e8716004048857d80b37f03e600b6fd2441`

工具输入字段为：

```text
target, mode, ports, threads, output_format, timeout,
proxy, poc_name, no_scan
```

### 兼容性处理

- 二进制路径由 `GA_FSCAN_BINARY` 配置，不依赖任何作者本机目录
- stdout 只承载 MCP JSON-RPC，诊断信息不会破坏 stdio 协议
- URL 使用 fscan v2.2.0 的 `-u`，主机使用 `-h`
- `poc_name` 映射到 `-pocname`，`no_scan` 映射到 `-ao`
- 参数列表直接传给子进程，不通过 shell 拼接
- 每次调用使用独立临时目录和结果文件
- 请求超时会终止子进程并返回 Tool Error
- 支持 `json`、`txt`、`csv` 输出
- 可在执行前按 `GA_FSCAN_SHA256` 校验二进制完整性

成功结果包含：

```text
status, exit_code, output_format, output, stdout, stderr, binary_sha256
```

缺少文件、哈希不匹配、非法参数、启动失败和超时均以 MCP Tool Error 回传，再由现有 Agent 工具循环处理。

### 二进制安装与校验

Fscan 二进制不随仓库分发。确认组织安全策略允许保存该安全测试工具后，可从 [v2.2.0 上游发布页](https://github.com/shadow1ng/fscan/releases/tag/v2.2.0) 获取文件。在项目根目录执行：

```cmd
mkdir tools\fscan 2>nul
curl.exe -L --fail -o tools\fscan\fscan.exe https://github.com/shadow1ng/fscan/releases/download/v2.2.0/fscan_2.2.0_windows_x64.exe
curl.exe -L --fail -o tools\fscan\checksums.txt https://github.com/shadow1ng/fscan/releases/download/v2.2.0/checksums.txt
certutil -hashfile tools\fscan\fscan.exe SHA256
findstr /i "fscan_2.2.0_windows_x64.exe" tools\fscan\checksums.txt
```

将校验值与上游清单核对后配置：

```env
GA_FSCAN_BINARY=tools\fscan\fscan.exe
GA_FSCAN_SHA256=5aefcbfa98b8e8814dc415a87e4e7e8716004048857d80b37f03e600b6fd2441
```

随后运行：

```cmd
.venv\Scripts\python.exe main.py --check
```

“哈希校验通过，尚未执行”只表示路径和完整性配置匹配，不代表安全审查通过或扫描功能已实际运行。

### MCP 启动

用户不需要单独启动适配器。`mcp.json` 会让 MCP Manager 运行：

```cmd
.venv\Scripts\python.exe -m gasecagent.mcp_servers.fscan
```

该命令直接在终端运行时会等待 JSON-RPC 输入，这是 stdio Server 的正常行为。握手和 `tools/list` 成功后，`fscan_scan` 才会交给 LLM。

## 安全与验证边界

已缓存样本的 SHA-256 与官方 v2.2.0 校验清单一致，但文件没有 Authenticode 签名，且本机 Windows Defender 将其识别为 `HackTool:Win32/CVE-2021-26855!rfn`。项目没有关闭 Defender、添加排除项、强制恢复或执行该文件。

已完成真实 Fscan MCP 握手、工具发现、schema、参数与缺少二进制的 Tool Error 测试；使用 Mock 子进程验证了结果回传、超时和输出格式。Mock 测试不是实际扫描。真实 Fscan 执行必须留到明确授权且隔离的测试环境验收。
