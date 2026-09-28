"""FOFA 与 Tavily MCP 集成测试。"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from gasecagent.config import PROJECT_ROOT, load_settings
from gasecagent.mcp import MCPClientRuntime, load_mcp_configuration
from gasecagent.mcp_servers.fofa import build_asset_query, search_fofa
from gasecagent.tool_output import normalize_tool_output


TAVILY_ENTRY = (
    PROJECT_ROOT
    / ".external"
    / "node-mcp"
    / "node_modules"
    / "tavily-mcp"
    / "build"
    / "index.js"
)
FILESYSTEM_ENTRY = (
    PROJECT_ROOT
    / ".external"
    / "node-mcp"
    / "node_modules"
    / "@modelcontextprotocol"
    / "server-filesystem"
    / "dist"
    / "index.js"
)


def _settings(tmp_path: Path, servers: dict, **environment: str):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_settings(
        tmp_path,
        environ={"GA_MCP_CONFIG": str(config_path), **environment},
    )


@pytest.fixture
def fofa_api_server() -> Iterator[tuple[str, list[dict[str, list[str]]]]]:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            requests.append(params)
            encoded_query = params.get("qbase64", [""])[0]
            query = base64.b64decode(encoded_query).decode("utf-8")
            if "trigger-error" in query:
                payload = {"error": True, "errmsg": "测试 API 错误"}
            else:
                payload = {
                    "error": False,
                    "size": 1,
                    "page": int(params.get("page", ["1"])[0]),
                    "mode": "extended",
                    "query": query,
                    "consumed_fpoint": 1,
                    "required_fpoints": 1,
                    "results": [["https://example.com", "192.0.2.10", "443"]],
                }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/api/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_example_configuration_disables_missing_key_servers(tmp_path: Path) -> None:
    if not FILESYSTEM_ENTRY.is_file() or not TAVILY_ENTRY.is_file():
        pytest.skip("本地 Node MCP 依赖不存在")
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        (PROJECT_ROOT / "mcp.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    settings = load_settings(
        PROJECT_ROOT,
        environ={
            "GA_MCP_CONFIG": str(config_path),
            "GA_FILESYSTEM_ROOT": str(tmp_path),
            "GA_FILESYSTEM_MCP_ENTRY": str(FILESYSTEM_ENTRY),
            "GA_FSCAN_BINARY": str(tmp_path / "missing-fscan.exe"),
            "GA_TAVILY_MCP_ENTRY": str(TAVILY_ENTRY),
        },
    )

    configuration = load_mcp_configuration(settings, environ={})

    assert [server.name for server in configuration.servers] == ["filesystem"]
    issues = {issue.server_name: issue.message for issue in configuration.issues}
    assert "TAVILY_API_KEY" in issues["tavily-search"]
    assert "FOFA_KEY" in issues["fofa"]


@pytest.mark.asyncio
async def test_official_tavily_real_mcp_protocol_and_tool_schema(
    tmp_path: Path,
) -> None:
    if not TAVILY_ENTRY.is_file():
        pytest.skip("本地 Tavily MCP 依赖不存在")
    runtime = MCPClientRuntime(
        _settings(
            tmp_path,
            {
                "tavily-search": {
                    "command": "node",
                    "args": [str(TAVILY_ENTRY)],
                    "cwd": str(PROJECT_ROOT),
                    "env": {"TAVILY_API_KEY": "test-key-not-used"},
                }
            },
        )
    )
    try:
        connections = await runtime.connect_all()
        assert len(connections) == 1
        tools = await connections[0].server.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert {
            "tavily_search",
            "tavily_extract",
            "tavily_crawl",
            "tavily_map",
            "tavily_research",
        } <= by_name.keys()
        search_properties = by_name["tavily_search"].inputSchema["properties"]
        assert {"query", "search_depth", "max_results"} <= search_properties.keys()
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_fofa_real_mcp_protocol_and_local_api_round_trip(
    tmp_path: Path,
    fofa_api_server: tuple[str, list[dict[str, list[str]]]],
) -> None:
    base_url, requests = fofa_api_server
    runtime = MCPClientRuntime(
        _settings(
            tmp_path,
            {
                "fofa": {
                    "command": sys.executable,
                    "args": ["-m", "gasecagent.mcp_servers.fofa"],
                    "cwd": str(PROJECT_ROOT),
                    "env": {
                        "FOFA_KEY": "test-fofa-key",
                        "FOFA_API_BASE_URL": base_url,
                        "PYTHONIOENCODING": "utf-8",
                    },
                }
            },
        )
    )
    try:
        connections = await runtime.connect_all()
        assert len(connections) == 1
        server = connections[0].server
        tools = await server.list_tools()
        assert [tool.name for tool in tools] == ["fofa_search", "get_alerts"]
        by_name = {tool.name: tool for tool in tools}
        assert {"query", "fields", "size", "page", "full"} == set(
            by_name["fofa_search"].inputSchema["properties"]
        )

        result = await server.call_tool(
            "fofa_search",
            {
                "query": 'domain="example.com"',
                "fields": "host,ip,port",
                "size": 1,
            },
        )
        rendered = normalize_tool_output(result)
        assert result.isError is not True
        assert "192.0.2.10" in rendered
        assert "consumed_fpoint" in rendered
        assert requests[0]["key"] == ["test-fofa-key"]
        decoded = base64.b64decode(requests[0]["qbase64"][0]).decode("utf-8")
        assert decoded == 'domain="example.com"'

        compatible = await server.call_tool(
            "get_alerts", {"domain": "example.com", "status_code": "200", "size": 1}
        )
        assert compatible.isError is not True
        assert 'domain=\\"example.com\\"' in normalize_tool_output(compatible)

        error_result = await server.call_tool(
            "fofa_search", {"query": "trigger-error", "size": 1}
        )
        assert error_result.isError is True
        assert "测试 API 错误" in normalize_tool_output(error_result)
    finally:
        await runtime.cleanup()


def test_fofa_compatibility_query_builder() -> None:
    query = build_asset_query(
        domain="example.com",
        ip="192.0.2.10",
        status_code="200",
    )

    assert query == 'domain="example.com" && ip="192.0.2.10" && status_code=200'


def test_fofa_compatibility_query_requires_filter() -> None:
    with pytest.raises(ToolError, match="至少提供一个"):
        build_asset_query()


@pytest.mark.asyncio
async def test_fofa_network_error_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fofa-secret-must-not-leak"

    class FailingClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FailingClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.RequestError(
                f"request URL contained key={secret}",
                request=httpx.Request("GET", "https://fofa.invalid/"),
            )

    monkeypatch.setenv("FOFA_KEY", secret)
    monkeypatch.setattr(
        "gasecagent.mcp_servers.fofa.httpx.AsyncClient",
        FailingClient,
    )

    with pytest.raises(ToolError) as exc_info:
        await search_fofa(query='domain="example.com"')

    assert secret not in str(exc_info.value)
    assert "RequestError" in str(exc_info.value)
