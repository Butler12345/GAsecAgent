"""MCP stdio 配置、连接、动态工具发现与生命周期管理。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any

from agents.mcp import MCPServer, MCPServerManager, MCPServerStdio

from .config import Settings, load_project_environment


PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPConfigError(ValueError):
    """MCP 配置文件整体无效。"""


@dataclass(frozen=True, slots=True)
class MCPServerDefinition:
    name: str
    command: str
    args: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    cache_tools_list: bool
    client_session_timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class MCPConfigIssue:
    server_name: str
    message: str


@dataclass(frozen=True, slots=True)
class MCPConfiguration:
    path: Path
    servers: tuple[MCPServerDefinition, ...]
    disabled_servers: tuple[str, ...]
    issues: tuple[MCPConfigIssue, ...]


@dataclass(frozen=True, slots=True)
class MCPConnection:
    name: str
    server: MCPServer
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPConnectionFailure:
    name: str
    message: str


def _tool_custom_data(context: Any) -> dict[str, Any]:
    """保留 SDK 默认模型输出之外的 MCP error/structured 元数据供 CLI 展示。"""

    data: dict[str, Any] = {
        "server_name": context.server_name,
        "tool_name": context.tool_name,
        "is_error": bool(context.is_error),
    }
    if context.structured_content is not None:
        data["structured_content"] = dict(context.structured_content)
    return data


def _expand(value: str, environment: dict[str, str], project_root: Path) -> str:
    variables = {**environment, "PROJECT_ROOT": str(project_root)}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = variables.get(name)
        if replacement is None or replacement.strip() == "":
            raise MCPConfigError(f"缺少环境变量：{name}")
        return replacement

    return PLACEHOLDER_PATTERN.sub(replace, value)


def _resolve_command(command: str, project_root: Path) -> str:
    if Path(command).is_absolute() or "/" in command or "\\" in command:
        path = Path(command)
        if not path.is_absolute():
            path = project_root / path
        resolved = path.resolve(strict=False)
        if not resolved.is_file():
            raise MCPConfigError(f"外部程序不存在：{resolved}")
        return str(resolved)
    location = shutil.which(command)
    if location is None:
        raise MCPConfigError(f"PATH 中找不到外部程序：{command}")
    return location


def _optional_timeout(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise MCPConfigError(f"{field_name} 必须是正数或 null")
    return float(value)


def _required_paths(
    raw: dict[str, Any],
    field_name: str,
    environment: dict[str, str],
    project_root: Path,
    *,
    directories: bool,
) -> None:
    values = raw.get(field_name, [])
    if not isinstance(values, list) or not all(
        isinstance(item, str) and item.strip() for item in values
    ):
        raise MCPConfigError(f"{field_name} 必须是非空字符串数组")
    for item in values:
        path = Path(_expand(item, environment, project_root))
        if not path.is_absolute():
            path = project_root / path
        resolved = path.resolve(strict=False)
        exists = resolved.is_dir() if directories else resolved.is_file()
        if not exists:
            kind = "外部目录" if directories else "外部文件"
            raise MCPConfigError(f"{kind}不存在：{resolved}")


def _parse_server(
    name: str,
    raw: Any,
    project_root: Path,
    environment: dict[str, str],
) -> MCPServerDefinition | None:
    if not isinstance(raw, dict):
        raise MCPConfigError("配置必须是 JSON object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MCPConfigError("enabled 必须是 true 或 false")
    if not enabled:
        return None
    if raw.get("transport", "stdio") != "stdio":
        raise MCPConfigError("当前版本仅支持 stdio transport")

    command_value = raw.get("command")
    if not isinstance(command_value, str) or not command_value.strip():
        raise MCPConfigError("command 必须是非空字符串")
    command = _resolve_command(
        _expand(command_value.strip(), environment, project_root),
        project_root,
    )

    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list) or not all(
        isinstance(item, str) for item in raw_args
    ):
        raise MCPConfigError("args 必须是字符串数组")
    args = tuple(_expand(item, environment, project_root) for item in raw_args)

    cwd: Path | None = None
    raw_cwd = raw.get("cwd")
    if raw_cwd is not None:
        if not isinstance(raw_cwd, str) or not raw_cwd.strip():
            raise MCPConfigError("cwd 必须是非空字符串或省略")
        expanded_cwd = Path(_expand(raw_cwd, environment, project_root))
        if not expanded_cwd.is_absolute():
            expanded_cwd = project_root / expanded_cwd
        cwd = expanded_cwd.resolve(strict=False)
        if not cwd.is_dir():
            raise MCPConfigError(f"cwd 目录不存在：{cwd}")

    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_env.items()
    ):
        raise MCPConfigError("env 必须是字符串到字符串的 JSON object")
    child_env = None
    if raw_env:
        child_env = {
            key: _expand(value, environment, project_root)
            for key, value in raw_env.items()
        }

    _required_paths(
        raw,
        "requiredFiles",
        environment,
        project_root,
        directories=False,
    )
    _required_paths(
        raw,
        "requiredDirectories",
        environment,
        project_root,
        directories=True,
    )

    cache_tools_list = raw.get("cacheToolsList", False)
    if not isinstance(cache_tools_list, bool):
        raise MCPConfigError("cacheToolsList 必须是 true 或 false")
    timeout = _optional_timeout(
        raw.get("clientSessionTimeoutSeconds", 30),
        "clientSessionTimeoutSeconds",
    )
    return MCPServerDefinition(
        name=name,
        command=command,
        args=args,
        cwd=cwd,
        env=child_env,
        cache_tools_list=cache_tools_list,
        client_session_timeout_seconds=timeout,
    )


def load_mcp_configuration(
    settings: Settings,
    environ: dict[str, str] | None = None,
) -> MCPConfiguration:
    """读取 `mcp.json`；单个服务器配置错误不会丢弃其他服务器。"""

    path = settings.mcp_config_path
    if not path.is_file():
        return MCPConfiguration(
            path=path,
            servers=(),
            disabled_servers=(),
            issues=(MCPConfigIssue("配置文件", f"不存在：{path}"),),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConfigError(f"无法读取 MCP 配置：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("mcpServers"), dict
    ):
        raise MCPConfigError("MCP 配置根节点必须包含 mcpServers object")

    environment = load_project_environment(
        settings.project_root,
        os.environ if environ is None else environ,
    )
    environment.setdefault("GA_FILESYSTEM_ROOT", str(settings.filesystem_root))
    environment.setdefault(
        "GA_FILESYSTEM_MCP_ENTRY", str(settings.filesystem_mcp_entry_path)
    )
    environment.setdefault("GA_FSCAN_BINARY", str(settings.fscan_binary_path))
    environment.setdefault("GA_FSCAN_SHA256", settings.fscan_expected_sha256)
    environment.setdefault(
        "GA_TAVILY_MCP_ENTRY", str(settings.tavily_mcp_entry_path)
    )
    if settings.tavily_api_key:
        environment.setdefault("TAVILY_API_KEY", settings.tavily_api_key)
    if settings.fofa_api_key:
        environment.setdefault("FOFA_KEY", settings.fofa_api_key)
    if settings.fofa_email:
        environment.setdefault("FOFA_EMAIL", settings.fofa_email)
    definitions: list[MCPServerDefinition] = []
    disabled: list[str] = []
    issues: list[MCPConfigIssue] = []
    for name, raw in payload["mcpServers"].items():
        if not isinstance(name, str) or not name.strip():
            issues.append(MCPConfigIssue("<未命名>", "服务器名称必须是非空字符串"))
            continue
        try:
            definition = _parse_server(
                name.strip(), raw, settings.project_root, environment
            )
        except MCPConfigError as exc:
            issues.append(MCPConfigIssue(name.strip(), str(exc)))
            continue
        if definition is None:
            disabled.append(name.strip())
        else:
            definitions.append(definition)
    return MCPConfiguration(
        path=path,
        servers=tuple(definitions),
        disabled_servers=tuple(disabled),
        issues=tuple(issues),
    )


class MCPClientRuntime:
    """连接多个 stdio MCP，并只向 Agent 暴露握手和工具发现成功的服务器。"""

    def __init__(self, settings: Settings) -> None:
        self.configuration = load_mcp_configuration(settings)
        self.connections: list[MCPConnection] = []
        self.failures: list[MCPConnectionFailure] = [
            MCPConnectionFailure(issue.server_name, issue.message)
            for issue in self.configuration.issues
        ]
        self._servers: list[MCPServerStdio] = [
            MCPServerStdio(
                name=definition.name,
                params={
                    "command": definition.command,
                    "args": list(definition.args),
                    "env": definition.env,
                    "cwd": str(definition.cwd) if definition.cwd else None,
                    "encoding": "utf-8",
                    "encoding_error_handler": "replace",
                },
                cache_tools_list=definition.cache_tools_list,
                client_session_timeout_seconds=(
                    definition.client_session_timeout_seconds
                ),
                custom_data_extractor=_tool_custom_data,
            )
            for definition in self.configuration.servers
        ]
        self._manager = MCPServerManager(
            self._servers,
            connect_timeout_seconds=10,
            cleanup_timeout_seconds=10,
            drop_failed_servers=True,
            strict=False,
            connect_in_parallel=False,
        )

    @property
    def active_servers(self) -> list[MCPServer]:
        return [connection.server for connection in self.connections]

    @property
    def capabilities(self) -> list[str]:
        return [
            f"MCP {connection.name}：{', '.join(connection.tool_names)}"
            for connection in self.connections
        ]

    async def connect_all(self) -> list[MCPConnection]:
        active = await self._manager.connect_all()
        for server, error in self._manager.errors.items():
            self.failures.append(MCPConnectionFailure(server.name, str(error)))

        self.connections = []
        for server in active:
            try:
                tools = await server.list_tools()
            except Exception as exc:
                self.failures.append(
                    MCPConnectionFailure(server.name, f"工具发现失败：{exc}")
                )
                continue
            if not tools:
                self.failures.append(
                    MCPConnectionFailure(server.name, "工具发现成功但未提供任何 Tool")
                )
                continue
            self.connections.append(
                MCPConnection(
                    name=server.name,
                    server=server,
                    tool_names=tuple(tool.name for tool in tools),
                )
            )
        return list(self.connections)

    async def cleanup(self) -> None:
        await self._manager.cleanup_all()
