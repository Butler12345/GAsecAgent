from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from gasecagent.agent import AgentRuntime
from gasecagent.config import PROJECT_ROOT, load_settings
from gasecagent.mcp import MCPClientRuntime, MCPConfigError, load_mcp_configuration
from gasecagent.tool_output import normalize_tool_output


TEST_SERVER = PROJECT_ROOT / "tests" / "fixtures" / "mcp_test_server.py"
BROKEN_SERVER = PROJECT_ROOT / "tests" / "fixtures" / "mcp_broken_server.py"


def write_mcp_config(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def mcp_settings(tmp_path: Path, servers: dict):
    path = write_mcp_config(tmp_path, servers)
    return load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
            "GA_MCP_CONFIG": str(path),
        },
    )


def local_server_definition() -> dict:
    return {
        "enabled": True,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(TEST_SERVER)],
        "cwd": "${PROJECT_ROOT}",
        "env": {"PYTHONIOENCODING": "utf-8"},
        "cacheToolsList": True,
        "clientSessionTimeoutSeconds": 30,
    }


def test_configuration_isolates_invalid_and_disabled_servers(tmp_path: Path) -> None:
    settings = mcp_settings(
        tmp_path,
        {
            "working": local_server_definition(),
            "disabled": {
                "enabled": False,
                "command": "${MISSING_BUT_DISABLED}",
            },
            "broken": {
                "command": str(tmp_path / "missing-program.exe"),
            },
        },
    )

    configuration = load_mcp_configuration(settings, environ={})

    assert [server.name for server in configuration.servers] == ["working"]
    assert configuration.disabled_servers == ("disabled",)
    assert configuration.issues[0].server_name == "broken"
    assert "外部程序不存在" in configuration.issues[0].message
    assert configuration.servers[0].cwd == tmp_path.resolve()
    assert configuration.servers[0].env == {"PYTHONIOENCODING": "utf-8"}
    assert "GA_LLM_API_KEY" not in (configuration.servers[0].env or {})


def test_configuration_reads_only_explicit_child_secret_from_dotenv(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "MCP_TEST_SECRET=dotenv-secret\nUNRELATED_SECRET=do-not-forward\n",
        encoding="utf-8",
    )
    definition = local_server_definition()
    definition["env"] = {"MCP_TEST_SECRET": "${MCP_TEST_SECRET}"}
    settings = mcp_settings(tmp_path, {"local-test": definition})

    configuration = load_mcp_configuration(settings, environ={})

    assert configuration.servers[0].env == {
        "MCP_TEST_SECRET": "dotenv-secret"
    }


def test_configuration_rejects_whitespace_only_environment_value(
    tmp_path: Path,
) -> None:
    definition = local_server_definition()
    definition["env"] = {"MCP_TEST_SECRET": "${MCP_TEST_SECRET}"}
    settings = mcp_settings(tmp_path, {"local-test": definition})

    configuration = load_mcp_configuration(
        settings,
        environ={"MCP_TEST_SECRET": "   "},
    )

    assert configuration.servers == ()
    assert configuration.issues[0].server_name == "local-test"
    assert "缺少环境变量：MCP_TEST_SECRET" in configuration.issues[0].message


def test_invalid_json_has_clear_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{invalid", encoding="utf-8")
    settings = load_settings(
        tmp_path,
        environ={"GA_MCP_CONFIG": str(path)},
    )

    with pytest.raises(MCPConfigError, match="无法读取 MCP 配置"):
        load_mcp_configuration(settings, environ={})


def test_required_external_paths_are_isolated(tmp_path: Path) -> None:
    valid_root = tmp_path / "allowed"
    valid_root.mkdir()
    working = local_server_definition()
    working["requiredDirectories"] = [str(valid_root)]
    settings = mcp_settings(
        tmp_path,
        {
            "working": working,
            "missing-fscan": {
                **local_server_definition(),
                "requiredFiles": ["${GA_FSCAN_BINARY}"],
            },
        },
    )

    configuration = load_mcp_configuration(
        settings,
        environ={"GA_FSCAN_BINARY": str(tmp_path / "missing.exe")},
    )

    assert [server.name for server in configuration.servers] == ["working"]
    assert configuration.issues[0].server_name == "missing-fscan"
    assert "外部文件不存在" in configuration.issues[0].message


@pytest.mark.asyncio
async def test_real_stdio_mcp_list_tools_and_call_tool(tmp_path: Path) -> None:
    runtime = MCPClientRuntime(
        mcp_settings(tmp_path, {"local-test": local_server_definition()})
    )
    try:
        connections = await runtime.connect_all()
        assert len(connections) == 1
        assert connections[0].tool_names == ("echo", "add", "fail")

        result = await connections[0].server.call_tool(
            "echo", {"text": "真实 MCP 协议"}
        )
        assert "真实 MCP 协议" in normalize_tool_output(result)
        error_result = await connections[0].server.call_tool(
            "fail", {"message": "预期错误"}
        )
        assert error_result.isError is True
        assert "预期错误" in normalize_tool_output(error_result)
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_multiple_real_stdio_servers_connect_and_cleanup(tmp_path: Path) -> None:
    runtime = MCPClientRuntime(
        mcp_settings(
            tmp_path,
            {
                "local-a": local_server_definition(),
                "local-b": local_server_definition(),
            },
        )
    )
    try:
        connections = await runtime.connect_all()
        assert [connection.name for connection in connections] == [
            "local-a",
            "local-b",
        ]
        assert all("echo" in connection.tool_names for connection in connections)
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_agent_runner_executes_real_mcp_tool_loop(tmp_path: Path) -> None:
    settings = mcp_settings(
        tmp_path,
        {
            "local-test": local_server_definition(),
            "broken": {
                "command": sys.executable,
                "args": [str(BROKEN_SERVER)],
            },
        },
    )
    mcp_runtime = MCPClientRuntime(settings)
    model = ScriptedModel(
        [
            [
                function_call(
                    "echo",
                    {"text": "Tool Result 回传验证"},
                    call_id="call-1",
                )
            ],
            [
                function_call(
                    "fail",
                    {"message": "连续工具调用错误验证"},
                    call_id="call-2",
                )
            ],
            [assistant_message("已收到真实 MCP 工具结果。")],
        ]
    )
    tool_calls = []
    tool_results = []
    try:
        connections = await mcp_runtime.connect_all()
        assert len(connections) == 1
        assert any(failure.name == "broken" for failure in mcp_runtime.failures)
        agent_runtime = AgentRuntime(
            settings,
            model=model,
            mcp_servers=mcp_runtime.active_servers,
            available_capabilities=mcp_runtime.capabilities,
        )

        answer = await agent_runtime.stream_response(
            "调用 echo 并分析结果",
            lambda _text: None,
            on_tool_call=tool_calls.append,
            on_tool_result=tool_results.append,
        )

        assert answer == "已收到真实 MCP 工具结果。"
        assert [item.name for item in tool_calls] == ["echo", "fail"]
        assert len(tool_results) == 2
        assert "Tool Result 回传验证" in tool_results[0].output
        assert tool_results[0].is_error is False
        assert "连续工具调用错误验证" in tool_results[1].output
        assert tool_results[1].is_error is True
        assert len(model.calls) == 3
        assert model.calls[0].model_settings.tool_choice == "auto"
        assert model.calls[0].model_settings.parallel_tool_calls is True
        tools_by_name = {tool.name: tool for tool in model.calls[0].tools}
        assert {"echo", "add", "fail"} <= tools_by_name.keys()
        assert "text" in tools_by_name["echo"].params_json_schema["properties"]
        second_input = model.calls[1].input
        assert isinstance(second_input, list)
        assert any(
            item.get("type") == "function_call_output"
            and "Tool Result 回传验证" in str(item.get("output"))
            for item in second_input
            if isinstance(item, dict)
        )
        third_input = model.calls[2].input
        assert isinstance(third_input, list)
        assert any(
            item.get("type") == "function_call_output"
            and "连续工具调用错误验证" in str(item.get("output"))
            for item in third_input
            if isinstance(item, dict)
        )
        await agent_runtime.aclose()
    finally:
        await mcp_runtime.cleanup()
