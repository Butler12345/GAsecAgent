from __future__ import annotations

from collections.abc import Iterable
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys

import pytest
from rich.console import Console

from gasecagent.agent import AgentToolCall, AgentToolResult
from gasecagent.cli import (
    ExitRequested,
    _tavily_runtime_check,
    prompt_rag_choice,
    read_multiline_query,
    run_check,
    run_interactive,
    show_banner,
)
from gasecagent.config import load_settings
from gasecagent.rag import KnowledgeBuildResult


class ScriptedInput:
    def __init__(self, values: Iterable[str | BaseException]) -> None:
        self._values = iter(values)

    def __call__(self, _prompt: str) -> str:
        value = next(self._values)
        if isinstance(value, BaseException):
            raise value
        return value


def capture_console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_banner_uses_release_brand_version_and_colors() -> None:
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="standard",
        legacy_windows=False,
        record=True,
        width=120,
    )

    show_banner(console)

    rendered = output.getvalue()
    plain = console.export_text(styles=False)
    assert "G A s e c A g e n t" in plain
    assert "GAsecAgent v0.1.0" in plain
    assert "基于 LLM + RAG + MCP 的自动化安全 Agent" in plain
    assert "Phase" not in plain
    assert "\x1b[" in rendered


class StubRuntime:
    def __init__(
        self,
        _settings: object,
        *,
        failure: Exception | None = None,
        emit_tools: bool = False,
    ) -> None:
        self.failure = failure
        self.emit_tools = emit_tools
        self.closed = False

    async def stream_response(
        self,
        _query: str,
        on_text: object,
        on_tool_call: object = None,
        on_tool_result: object = None,
    ) -> str:
        if self.failure is not None:
            raise self.failure
        if self.emit_tools and callable(on_tool_call):
            on_tool_call(AgentToolCall("echo", {"text": "测试"}, "call-1"))
        if self.emit_tools and callable(on_tool_result):
            on_tool_result(
                AgentToolResult("call-1", '[\n  {\n    "ok": true\n  }\n]', False)
            )
        assert callable(on_text)
        on_text("模拟回答")
        return "模拟回答"

    async def aclose(self) -> None:
        self.closed = True


class StubMCPRuntime:
    def __init__(self, _settings: object, *, available: bool = True) -> None:
        self.available = available
        self.cleaned = False
        self.failures = []
        self.active_servers = [object()] if available else []
        self.capabilities = ["MCP 测试：echo"] if available else []
        self.configuration = type(
            "Configuration",
            (),
            {"disabled_servers": ()},
        )()

    async def connect_all(self):
        if not self.available:
            return []
        return [
            type(
                "Connection",
                (),
                {"name": "测试", "tool_names": ("echo",)},
            )()
        ]

    async def cleanup(self) -> None:
        self.cleaned = True


class StubKnowledgeBase:
    def __init__(self, *, chunk_count: int = 1) -> None:
        self.chunk_count = chunk_count
        self.built = False
        self.closed = False

    async def build(self) -> KnowledgeBuildResult:
        self.built = True
        return KnowledgeBuildResult(
            file_count=1 if self.chunk_count else 0,
            chunk_count=self.chunk_count,
            skipped_files=(),
        )

    async def aclose(self) -> None:
        self.closed = True


def test_rag_prompt_defaults_to_disabled() -> None:
    console, _ = capture_console()
    assert prompt_rag_choice(console, ScriptedInput([""])) is False


def test_rag_prompt_retries_invalid_answer() -> None:
    console, output = capture_console()
    assert prompt_rag_choice(console, ScriptedInput(["maybe", "是"])) is True
    assert "请输入 yes 或 no" in output.getvalue()


@pytest.mark.asyncio
async def test_rag_off_never_creates_embedding_runtime(tmp_path: Path) -> None:
    console, _ = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    runtime = StubRuntime(settings)
    mcp_runtime = StubMCPRuntime(settings)

    def unexpected_rag_factory(_settings: object):
        raise AssertionError("RAG 关闭时不应初始化 Embedding")

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no", EOFError()]),
        runtime_factory=lambda _settings, **_kwargs: runtime,
        mcp_factory=lambda _settings: mcp_runtime,
        rag_factory=unexpected_rag_factory,
    )

    assert result == 0


@pytest.mark.asyncio
async def test_rag_on_reports_missing_embedding_key_before_mcp(
    tmp_path: Path,
) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    mcp_created = False

    def mcp_factory(_settings: object):
        nonlocal mcp_created
        mcp_created = True
        return StubMCPRuntime(_settings)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["yes"]),
        mcp_factory=mcp_factory,
    )

    assert result == 4
    assert "RAG 初始化失败" in output.getvalue()
    assert "GA_EMBEDDING_API_KEY" in output.getvalue()
    assert mcp_created is False


@pytest.mark.asyncio
async def test_rag_on_builds_index_and_passes_it_to_agent(tmp_path: Path) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
            "GA_EMBEDDING_API_KEY": "embedding-key",
        },
    )
    runtime = StubRuntime(settings)
    mcp_runtime = StubMCPRuntime(settings)
    knowledge_base = StubKnowledgeBase()
    captured: dict[str, object] = {}

    def runtime_factory(_settings: object, **kwargs: object):
        captured.update(kwargs)
        return runtime

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["yes", EOFError()]),
        runtime_factory=runtime_factory,
        mcp_factory=lambda _settings: mcp_runtime,
        rag_factory=lambda _settings: knowledge_base,
    )

    assert result == 0
    assert knowledge_base.built is True
    assert knowledge_base.closed is True
    assert captured["knowledge_base"] is knowledge_base
    assert "知识库索引已构建" in output.getvalue()


def test_multiline_input_submits_on_blank_line() -> None:
    console, _ = capture_console()
    query = read_multiline_query(
        console,
        ScriptedInput(["第一行", "第二行", ""]),
    )
    assert query == "第一行\n第二行"


@pytest.mark.parametrize("command", ["quit", "EXIT", "退出"])
def test_exit_command_does_not_require_extra_blank_line(command: str) -> None:
    console, _ = capture_console()
    with pytest.raises(ExitRequested):
        read_multiline_query(console, ScriptedInput([command]))


@pytest.mark.asyncio
async def test_interactive_handles_eof_gracefully(tmp_path: Path) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    runtime = StubRuntime(settings)
    mcp_runtime = StubMCPRuntime(settings)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no", EOFError()]),
        runtime_factory=lambda _settings, **_kwargs: runtime,
        mcp_factory=lambda _settings: mcp_runtime,
    )

    assert result == 0
    assert "输入流已结束" in output.getvalue()
    assert runtime.closed is True
    assert mcp_runtime.cleaned is True


@pytest.mark.asyncio
async def test_interactive_handles_ctrl_c_and_cleans_resources(
    tmp_path: Path,
) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    runtime = StubRuntime(settings)
    mcp_runtime = StubMCPRuntime(settings)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no", KeyboardInterrupt()]),
        runtime_factory=lambda _settings, **_kwargs: runtime,
        mcp_factory=lambda _settings: mcp_runtime,
    )

    assert result == 0
    assert "收到 Ctrl+C" in output.getvalue()
    assert runtime.closed is True
    assert mcp_runtime.cleaned is True


@pytest.mark.asyncio
async def test_interactive_does_not_print_completed_after_agent_error(
    tmp_path: Path,
) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    runtime = StubRuntime(settings, failure=RuntimeError("模拟失败"))
    mcp_runtime = StubMCPRuntime(settings)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no", "测试任务", "", "quit"]),
        runtime_factory=lambda _settings, **_kwargs: runtime,
        mcp_factory=lambda _settings: mcp_runtime,
    )

    rendered = output.getvalue()
    assert result == 0
    assert "Agent 执行失败：模拟失败" in rendered
    assert "查询完成" not in rendered
    assert runtime.closed is True
    assert mcp_runtime.cleaned is True


@pytest.mark.asyncio
async def test_interactive_refuses_zero_available_mcp_servers(
    tmp_path: Path,
) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    mcp_runtime = StubMCPRuntime(settings, available=False)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no"]),
        runtime_factory=lambda _settings, **_kwargs: StubRuntime(_settings),
        mcp_factory=lambda _settings: mcp_runtime,
    )

    assert result == 3
    assert "当前没有任何可用 MCP Server" in output.getvalue()
    assert mcp_runtime.cleaned is True


@pytest.mark.asyncio
async def test_interactive_displays_tool_call_and_array_result(
    tmp_path: Path,
) -> None:
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )
    runtime = StubRuntime(settings, emit_tools=True)
    mcp_runtime = StubMCPRuntime(settings)

    result = await run_interactive(
        settings,
        console,
        ScriptedInput(["no", "测试工具", "", "quit"]),
        runtime_factory=lambda _settings, **_kwargs: runtime,
        mcp_factory=lambda _settings: mcp_runtime,
    )

    rendered = output.getvalue()
    assert result == 0
    assert "Tool Call：echo" in rendered
    assert "Tool 参数" in rendered
    assert "Tool Result" in rendered
    assert '"ok": true' in rendered
    assert "查询完成" in rendered


def test_check_never_prints_secret_value(tmp_path: Path) -> None:
    console, output = capture_console()
    secret = "super-secret-value-must-not-appear"
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": secret,
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
            "TAVILY_API_KEY": secret,
            "FOFA_KEY": secret,
        },
    )

    assert run_check(settings, console) == 0
    rendered = output.getvalue()
    assert "LLM" in rendered
    assert "已配置" in rendered
    assert secret not in rendered


def test_check_marks_optional_mcp_keys_as_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        (Path(__file__).resolve().parents[1] / "mcp.example.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    console, output = capture_console()
    settings = load_settings(
        Path(__file__).resolve().parents[1],
        environ={
            "GA_MCP_CONFIG": str(config_path),
            "GA_FSCAN_BINARY": str(tmp_path / "missing-fscan.exe"),
        },
    )

    assert run_check(settings, console) == 0
    rendered = output.getvalue()
    assert "tavily-search" in rendered
    assert "TAVILY_API_KEY" in rendered
    assert "FOFA_KEY" in rendered
    assert rendered.count("缺少 Key") >= 4


def test_check_marks_tavily_entry_as_missing_dependency(tmp_path: Path) -> None:
    console, output = capture_console()
    missing_entry = tmp_path / "missing-tavily-mcp.js"
    settings = load_settings(
        tmp_path,
        environ={
            "TAVILY_API_KEY": "test-key",
            "GA_TAVILY_MCP_ENTRY": str(missing_entry),
        },
    )

    assert run_check(settings, console) == 0
    rendered = output.getvalue()
    status, note = _tavily_runtime_check(settings)
    assert "缺少依赖" in status
    assert note == str(missing_entry)
    assert "Tavily" in rendered
    assert "缺少依赖" in rendered


def test_check_marks_mcp_config_as_not_connected(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local-check": {
                        "command": sys.executable,
                        "args": ["${PROJECT_ROOT}/server.py"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={"GA_MCP_CONFIG": str(config_path)},
    )

    assert run_check(settings, console) == 0
    rendered = output.getvalue()
    assert "local-check" in rendered
    assert "配置存在但未连接" in rendered
    assert "连接成功" not in rendered


def test_check_verifies_fscan_binary_sha256(tmp_path: Path) -> None:
    binary = tmp_path / "fscan.exe"
    binary.write_bytes(b"verified fscan fixture")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_FSCAN_BINARY": str(binary),
            "GA_FSCAN_SHA256": digest,
        },
    )

    assert run_check(settings, console) == 0
    assert "哈希校验通过，尚未执行" in output.getvalue()


def test_check_reports_fscan_binary_hash_mismatch(tmp_path: Path) -> None:
    binary = tmp_path / "fscan.exe"
    binary.write_bytes(b"unexpected fscan fixture")
    console, output = capture_console()
    settings = load_settings(
        tmp_path,
        environ={
            "GA_FSCAN_BINARY": str(binary),
            "GA_FSCAN_SHA256": "0" * 64,
        },
    )

    assert run_check(settings, console) == 0
    rendered = output.getvalue()
    assert "哈希不匹配" in rendered
    assert hashlib.sha256(binary.read_bytes()).hexdigest() in rendered
