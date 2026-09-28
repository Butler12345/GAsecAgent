"""Filesystem 与 Fscan MCP 集成测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from gasecagent.agent import AgentRuntime
from gasecagent.config import PROJECT_ROOT, load_settings
from gasecagent.mcp import MCPClientRuntime
from gasecagent.mcp_servers.fscan import build_fscan_command, run_fscan
from gasecagent.tool_output import normalize_tool_output


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
BROKEN_SERVER = PROJECT_ROOT / "tests" / "fixtures" / "mcp_broken_server.py"


def _settings(tmp_path: Path, servers: dict, **environment: str):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_settings(
        tmp_path,
        environ={
            "GA_MCP_CONFIG": str(config_path),
            "GA_FILESYSTEM_ROOT": str(tmp_path),
            **environment,
        },
    )


def _filesystem_definition(root: Path) -> dict:
    if not FILESYSTEM_ENTRY.is_file():
        pytest.skip("本地 Filesystem MCP 依赖不存在")
    return {
        "command": "node",
        "args": [str(FILESYSTEM_ENTRY), str(root)],
        "cwd": str(PROJECT_ROOT),
        "requiredDirectories": [str(root)],
        "cacheToolsList": True,
    }


@pytest.mark.asyncio
async def test_official_filesystem_real_protocol_and_local_file_operations(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    workspace = project_root / "workspace"
    workspace.mkdir(parents=True)
    fake_secret = "placeholder-secret-from-test-fixture"
    (project_root / ".env").write_text(
        f"GA_LLM_API_KEY={fake_secret}\n",
        encoding="utf-8",
    )
    (project_root / "main.py").write_text(
        "raise SystemExit('outside workspace')\n",
        encoding="utf-8",
    )
    runtime = MCPClientRuntime(
        _settings(tmp_path, {"filesystem": _filesystem_definition(workspace)})
    )
    try:
        connections = await runtime.connect_all()
        assert len(connections) == 1
        server = connections[0].server
        tools = await server.list_tools()
        by_name = {tool.name: tool for tool in tools}
        assert {
            "read_text_file",
            "write_file",
            "list_directory",
            "list_allowed_directories",
        } <= by_name.keys()
        assert "path" in by_name["write_file"].inputSchema["properties"]
        assert "content" in by_name["write_file"].inputSchema["properties"]

        target = workspace / "filesystem-test.txt"
        write_result = await server.call_tool(
            "write_file",
            {"path": str(target), "content": "第一版内容"},
        )
        assert write_result.isError is not True
        assert target.read_text(encoding="utf-8") == "第一版内容"

        modify_result = await server.call_tool(
            "write_file",
            {"path": str(target), "content": "修改后的内容"},
        )
        assert modify_result.isError is not True
        assert target.read_text(encoding="utf-8") == "修改后的内容"

        read_result = await server.call_tool(
            "read_text_file", {"path": str(target)}
        )
        assert "修改后的内容" in normalize_tool_output(read_result)
        listed = await server.call_tool(
            "list_directory", {"path": str(workspace)}
        )
        assert "filesystem-test.txt" in normalize_tool_output(listed)

        denied_env = await server.call_tool(
            "read_text_file",
            {"path": str(workspace / ".." / ".env")},
        )
        denied_source = await server.call_tool(
            "read_text_file",
            {"path": str(project_root / "main.py")},
        )
        assert denied_env.isError is True
        assert denied_source.isError is True
        assert fake_secret not in normalize_tool_output(denied_env)
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_example_configuration_connects_filesystem_and_isolates_optional_servers(
    tmp_path: Path,
) -> None:
    if not FILESYSTEM_ENTRY.is_file():
        pytest.skip("本地 Filesystem MCP 依赖不存在")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        (PROJECT_ROOT / "mcp.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    settings = load_settings(
        PROJECT_ROOT,
        environ={
            "GA_MCP_CONFIG": str(config_path),
            "GA_FILESYSTEM_ROOT": str(allowed),
            "GA_FILESYSTEM_MCP_ENTRY": str(FILESYSTEM_ENTRY),
            "GA_FSCAN_BINARY": str(tmp_path / "missing-fscan.exe"),
        },
    )
    runtime = MCPClientRuntime(settings)
    try:
        connections = await runtime.connect_all()
        assert [connection.name for connection in connections] == ["filesystem"]
        assert "write_file" in connections[0].tool_names
        assert {failure.name for failure in runtime.failures} == {
            "fscan",
            "tavily-search",
            "fofa",
        }
        assert any(
            "外部文件不存在" in failure.message
            for failure in runtime.failures
            if failure.name == "fscan"
        )
        assert all(
            "缺少环境变量" in failure.message
            for failure in runtime.failures
            if failure.name in {"tavily-search", "fofa"}
        )
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_filesystem_survives_another_mcp_connection_failure(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    runtime = MCPClientRuntime(
        _settings(
            tmp_path,
            {
                "filesystem": _filesystem_definition(allowed),
                "broken": {
                    "command": sys.executable,
                    "args": [str(BROKEN_SERVER)],
                },
            },
        )
    )
    try:
        connections = await runtime.connect_all()
        assert [connection.name for connection in connections] == ["filesystem"]
        assert any(failure.name == "broken" for failure in runtime.failures)
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_agent_runner_calls_official_filesystem_tool(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    settings = _settings(
        tmp_path, {"filesystem": _filesystem_definition(allowed)}
    )
    mcp_runtime = MCPClientRuntime(settings)
    target = allowed / "agent-created.txt"
    model = ScriptedModel(
        [
            [
                function_call(
                    "write_file",
                    {"path": str(target), "content": "Agent 真实工具调用"},
                    call_id="filesystem-call-1",
                )
            ],
            [assistant_message("文件写入完成。")],
        ]
    )
    calls = []
    results = []
    agent_runtime = None
    try:
        await mcp_runtime.connect_all()
        agent_runtime = AgentRuntime(
            settings,
            model=model,
            mcp_servers=mcp_runtime.active_servers,
            available_capabilities=mcp_runtime.capabilities,
        )
        answer = await agent_runtime.stream_response(
            "在允许目录创建测试文件",
            lambda _text: None,
            on_tool_call=calls.append,
            on_tool_result=results.append,
        )

        assert answer == "文件写入完成。"
        assert [item.name for item in calls] == ["write_file"]
        assert len(results) == 1
        assert results[0].is_error is False
        assert target.read_text(encoding="utf-8") == "Agent 真实工具调用"
    finally:
        if agent_runtime is not None:
            await agent_runtime.aclose()
        await mcp_runtime.cleanup()


@pytest.mark.asyncio
async def test_fscan_real_mcp_protocol_schema_and_missing_binary_error(
    tmp_path: Path,
) -> None:
    missing_binary = tmp_path / "missing-fscan.exe"
    runtime = MCPClientRuntime(
        _settings(
            tmp_path,
            {
                "fscan": {
                    "command": sys.executable,
                    "args": ["-m", "gasecagent.mcp_servers.fscan"],
                    "cwd": str(PROJECT_ROOT),
                    "env": {
                        "GA_FSCAN_BINARY": str(missing_binary),
                        "GA_FSCAN_SHA256": "",
                        "PYTHONIOENCODING": "utf-8",
                    },
                }
            },
        )
    )
    try:
        connections = await runtime.connect_all()
        assert len(connections) == 1
        tools = await connections[0].server.list_tools()
        assert [tool.name for tool in tools] == ["fscan_scan"]
        properties = tools[0].inputSchema["properties"]
        assert {
            "target",
            "mode",
            "ports",
            "threads",
            "output_format",
            "timeout",
            "proxy",
            "poc_name",
            "no_scan",
        } == properties.keys()

        result = await connections[0].server.call_tool(
            "fscan_scan", {"target": "127.0.0.1", "no_scan": True}
        )
        normalized = normalize_tool_output(result)
        assert result.isError is True
        assert "Fscan 外部程序不存在" in normalized
    finally:
        await runtime.cleanup()


def test_fscan_v220_command_mapping(tmp_path: Path) -> None:
    command = build_fscan_command(
        tmp_path / "fscan.exe",
        tmp_path / "result.json",
        target="https://127.0.0.1:8443/login",
        mode="web",
        ports="80,443",
        threads=10,
        output_format="json",
        timeout=15,
        proxy="http://127.0.0.1:8080",
        poc_name="test-poc",
        no_scan=True,
    )

    assert command[1:3] == ["-u", "https://127.0.0.1:8443/login"]
    assert "-pocname" in command
    assert "-ao" in command
    assert "-ns" not in command
    assert "-poc" not in command


@pytest.mark.asyncio
async def test_fscan_adapter_mock_process_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "fscan.exe"
    binary.write_bytes(b"test fscan binary")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setenv("GA_FSCAN_BINARY", str(binary))
    monkeypatch.setenv("GA_FSCAN_SHA256", digest)

    class FakeProcess:
        returncode = 0

        def __init__(self, output_path: Path) -> None:
            self.output_path = output_path

        async def communicate(self):
            self.output_path.write_text(
                '{"host":"127.0.0.1"}', encoding="utf-8"
            )
            return b"mock stdout", b""

        def kill(self) -> None:
            self.returncode = 1

    async def fake_create(*command: str, **_kwargs):
        output_path = Path(command[command.index("-o") + 1])
        return FakeProcess(output_path)

    monkeypatch.setattr(
        "gasecagent.mcp_servers.fscan.asyncio.create_subprocess_exec",
        fake_create,
    )
    result = await run_fscan(target="127.0.0.1", no_scan=True)

    assert result["status"] == "completed"
    assert result["output"] == '{"host":"127.0.0.1"}'
    assert result["binary_sha256"] == digest


@pytest.mark.asyncio
async def test_fscan_hash_mismatch_stops_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "fscan.exe"
    binary.write_bytes(b"unexpected binary")
    monkeypatch.setenv("GA_FSCAN_BINARY", str(binary))
    monkeypatch.setenv("GA_FSCAN_SHA256", "0" * 64)

    async def unexpected_create(*_args, **_kwargs):
        raise AssertionError("哈希不匹配时不应启动外部进程")

    monkeypatch.setattr(
        "gasecagent.mcp_servers.fscan.asyncio.create_subprocess_exec",
        unexpected_create,
    )
    result = await run_fscan(target="127.0.0.1")

    assert result["status"] == "error"
    assert "SHA-256 不匹配" in result["message"]
