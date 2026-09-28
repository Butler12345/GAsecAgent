"""GAsecAgent 中文命令行界面。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
import hashlib
from importlib.util import find_spec
import os
from pathlib import Path
import shutil
import sys

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .agent import (
    AgentRuntime,
    AgentToolCall,
    AgentToolResult,
    LLMConfigurationError,
    missing_llm_settings,
)
from .config import ConfigError, PROJECT_ROOT, Settings, load_settings
from .mcp import (
    MCPClientRuntime,
    MCPConfigError,
    load_mcp_configuration,
)
from .rag import (
    KnowledgeBase,
    RAGConfigurationError,
    missing_embedding_settings,
)
from .tool_output import normalize_tool_output


ASCII_LOGO = r"""
  ██████╗  █████╗ ███████╗███████╗ ██████╗
 ██╔════╝ ██╔══██╗██╔════╝██╔════╝██╔════╝
 ██║  ███╗███████║███████╗█████╗  ██║
 ██║   ██║██╔══██║╚════██║██╔══╝  ██║
 ╚██████╔╝██║  ██║███████║███████╗╚██████╗
  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝
             G A s e c A g e n t
"""

EXIT_COMMANDS = frozenset({"quit", "exit", "退出"})
YES_ANSWERS = frozenset({"y", "yes", "是", "开启"})
NO_ANSWERS = frozenset({"", "n", "no", "否", "关闭"})
InputFunction = Callable[[str], str]


class ExitRequested(Exception):
    """用户通过退出命令结束交互。"""


def configure_windows_utf8() -> None:
    """让 Windows CMD 与重定向输出统一使用 UTF-8。"""

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except (AttributeError, OSError):
        pass

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def show_banner(console: Console) -> None:
    console.print(ASCII_LOGO, style="bold bright_blue", highlight=False)
    console.print(
        "[bold white]基于 LLM + RAG + MCP 的中文自动化安全渗透 Agent[/bold white]"
    )
    console.print(
        f"[dim]GAsecAgent v{__version__} · "
        "基于 LLM + RAG + MCP 的自动化安全 Agent[/dim]\n"
    )
    console.print("[yellow]示例任务：[/yellow]")
    console.print("[cyan]1. 检测授权目标的常见安全风险[/cyan]")
    console.print("[cyan]2. 查询域名与资产情报[/cyan]")
    console.print("[cyan]3. 分析授权 IP 的安全状态[/cyan]")
    console.print("[cyan]4. 对本地流量分析文本进行安全审计[/cyan]")
    console.print("[red]输入 quit、exit 或 退出可直接结束程序[/red]")


def _read(input_func: InputFunction | None, console: Console, prompt: str) -> str:
    if input_func is None:
        return console.input(prompt)
    return input_func(prompt)


def prompt_rag_choice(
    console: Console,
    input_func: InputFunction | None = None,
) -> bool:
    """询问是否启用 RAG；空输入默认关闭。"""

    prompt = "[yellow]是否开启知识库增强？(yes/no，默认 no)：[/yellow] "
    while True:
        answer = _read(input_func, console, prompt).strip().lower()
        if answer in YES_ANSWERS:
            return True
        if answer in NO_ANSWERS:
            return False
        if answer in EXIT_COMMANDS:
            raise ExitRequested
        console.print("[red]请输入 yes 或 no。[/red]")


def read_multiline_query(
    console: Console,
    input_func: InputFunction | None = None,
) -> str | None:
    """读取多行任务；空行提交，首行退出命令立即退出。"""

    lines: list[str] = []
    while True:
        prompt = (
            "\n[bold cyan][>][/bold cyan] [white](空行提交)：[/white] "
            if not lines
            else "[cyan]...[/cyan] "
        )
        line = _read(input_func, console, prompt)
        stripped = line.strip()
        if not lines and stripped.lower() in EXIT_COMMANDS:
            raise ExitRequested
        if line == "":
            query = "\n".join(lines).strip()
            return query or None
        lines.append(line)


def _status(configured: bool, ready: str, missing: str) -> tuple[str, str]:
    if configured:
        return "[green]已配置[/green]", ready
    return "[yellow]未配置[/yellow]", missing


def _path_state(path: Path) -> str:
    return "[green]存在[/green]" if path.exists() else "[yellow]不存在[/yellow]"


def _fscan_binary_check(settings: Settings) -> tuple[str, str]:
    path = settings.fscan_binary_path
    if not path.is_file():
        return "[yellow]缺少外部程序[/yellow]", str(path)
    try:
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        return "[red]无法读取[/red]", f"{path}；{exc}"
    if actual != settings.fscan_expected_sha256:
        return (
            "[red]哈希不匹配[/red]",
            f"{path}；实际 SHA-256：{actual}",
        )
    return "[green]哈希校验通过，尚未执行[/green]", str(path)


def _tavily_runtime_check(settings: Settings) -> tuple[str, str]:
    if not settings.tavily_api_key:
        return "[yellow]缺少 Key[/yellow]", "需要 TAVILY_API_KEY"
    if not settings.tavily_mcp_entry_path.is_file():
        return "[yellow]缺少依赖[/yellow]", str(settings.tavily_mcp_entry_path)
    return (
        "[green]已配置[/green]",
        f"MCP 入口：{settings.tavily_mcp_entry_path}；尚未连接",
    )


def run_check(settings: Settings, console: Console) -> int:
    """输出本地环境检查，不进行网络请求或 MCP 连接。"""

    table = Table(title="GAsecAgent 配置检查", show_lines=False)
    table.add_column("检查项", style="cyan", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("说明", style="white")

    supported_python = sys.version_info[:2] in {(3, 11), (3, 12)}
    table.add_row(
        "Python",
        "[green]兼容[/green]" if supported_python else "[yellow]版本警告[/yellow]",
        f"{sys.version.split()[0]}；项目支持 3.11 / 3.12",
    )
    table.add_row("项目根", "[green]已确认[/green]", str(settings.project_root))
    table.add_row(
        ".env",
        "[green]配置文件存在[/green]"
        if settings.env_file_exists
        else "[yellow]配置文件不存在[/yellow]",
        str(settings.env_file),
    )

    llm_status, llm_note = _status(
        settings.llm_configured,
        f"模型 {escape(settings.llm_model or '')}；API Key 与 Base URL 已配置；尚未连接",
        "需设置 GA_LLM_API_KEY / GA_LLM_BASE_URL / GA_LLM_MODEL",
    )
    table.add_row("LLM", llm_status, llm_note)
    table.add_row(
        "Agent 参数",
        "[green]已加载[/green]",
        (
            f"temperature={settings.llm_temperature:g}，"
            f"top_p={settings.llm_top_p:g}，"
            f"max_tokens={settings.llm_max_tokens}，"
            f"max_turns={settings.llm_max_turns}，"
            f"历史={settings.history_max_turns} 轮"
        ),
    )

    embedding_status, embedding_note = _status(
        settings.embedding_configured,
        "Embedding 配置齐全；尚未初始化",
        "RAG 开启时需要 GA_EMBEDDING_API_KEY",
    )
    table.add_row("Embedding", embedding_status, embedding_note)

    try:
        mcp_configuration = load_mcp_configuration(settings)
    except MCPConfigError as exc:
        table.add_row("MCP", "[red]配置错误[/red]", str(exc))
    else:
        if settings.mcp_config_path.is_file():
            mcp_status = "[yellow]配置存在但未连接[/yellow]"
            mcp_note = (
                f"{len(mcp_configuration.servers)} 个启用，"
                f"{len(mcp_configuration.disabled_servers)} 个禁用"
            )
        else:
            mcp_status = "[yellow]未配置[/yellow]"
            mcp_note = f"目标路径：{settings.mcp_config_path}"
        table.add_row("MCP", mcp_status, mcp_note)
        for definition in mcp_configuration.servers:
            table.add_row(
                f"  └ {definition.name}",
                "[yellow]配置存在但未连接[/yellow]",
                definition.command,
            )
        for name in mcp_configuration.disabled_servers:
            table.add_row(f"  └ {name}", "[dim]已禁用[/dim]", "不会启动")
        for issue in (
            mcp_configuration.issues if settings.mcp_config_path.is_file() else ()
        ):
            missing_program = (
                "外部程序不存在" in issue.message
                or "PATH 中找不到外部程序" in issue.message
                or "外部文件不存在" in issue.message
            )
            missing_key = (
                "缺少环境变量" in issue.message
                and "KEY" in issue.message.upper()
            )
            table.add_row(
                f"  └ {issue.server_name}",
                (
                    "[red]缺少外部程序[/red]"
                    if missing_program
                    else (
                        "[yellow]缺少 Key[/yellow]"
                        if missing_key
                        else "[red]配置错误[/red]"
                    )
                ),
                issue.message,
            )
    table.add_row(
        "Filesystem 根目录",
        "[green]已配置[/green]"
        if settings.filesystem_root.is_dir()
        else "[red]目录不存在[/red]",
        str(settings.filesystem_root),
    )
    table.add_row(
        "Filesystem MCP",
        "[green]本地入口存在[/green]"
        if settings.filesystem_mcp_entry_path.is_file()
        else "[yellow]缺少依赖[/yellow]",
        str(settings.filesystem_mcp_entry_path),
    )
    fscan_status, fscan_note = _fscan_binary_check(settings)
    table.add_row("Fscan 二进制", fscan_status, fscan_note)
    tavily_status, tavily_note = _tavily_runtime_check(settings)
    table.add_row("Tavily", tavily_status, tavily_note)
    table.add_row(
        "FOFA",
        "[green]已配置[/green]"
        if settings.fofa_api_key
        else "[yellow]缺少 Key[/yellow]",
        "尚未连接" if settings.fofa_api_key else "需要 FOFA_KEY",
    )
    knowledge_file_count = (
        sum(
            1
            for path in settings.knowledge_base_path.iterdir()
            if path.is_file() and path.name != ".gitkeep"
        )
        if settings.knowledge_base_path.is_dir()
        else 0
    )
    table.add_row(
        "知识库目录",
        _path_state(settings.knowledge_base_path),
        (
            f"{settings.knowledge_base_path}；"
            f"{knowledge_file_count} 个文件；"
            "尚未构建索引"
        ),
    )

    agents_available = find_spec("agents") is not None
    table.add_row(
        "Agents SDK",
        "[green]依赖已安装[/green]"
        if agents_available
        else "[yellow]缺少依赖[/yellow]",
        "使用 Agents SDK Agent/MCP 工具循环、可选 RAG 与动态工具能力",
    )

    uv_path = shutil.which("uv")
    project_uv = settings.project_root / ".venv" / "Scripts" / "uv.exe"
    table.add_row(
        "uv",
        "[green]已安装[/green]"
        if uv_path or project_uv.is_file()
        else "[yellow]缺少依赖[/yellow]",
        uv_path or str(project_uv),
    )
    for command in ("node", "npm", "npx", "git"):
        location = shutil.which(command)
        table.add_row(
            command,
            "[green]PATH 可用[/green]" if location else "[yellow]PATH 不可用[/yellow]",
            location or "未找到",
        )

    console.print(table)
    console.print(
        "[yellow]说明：--check 只检查本地配置与依赖，"
        "不会进行 LLM/API 请求或 MCP 连接。[/yellow]"
    )
    return 0


async def run_interactive(
    settings: Settings,
    console: Console,
    input_func: InputFunction | None = None,
    runtime_factory: Callable[..., AgentRuntime] = AgentRuntime,
    mcp_factory: Callable[[Settings], MCPClientRuntime] = MCPClientRuntime,
    rag_factory: Callable[[Settings], KnowledgeBase] = KnowledgeBase,
) -> int:
    show_banner(console)
    runtime: AgentRuntime | None = None
    mcp_runtime: MCPClientRuntime | None = None
    knowledge_base: KnowledgeBase | None = None
    try:
        rag_enabled = prompt_rag_choice(console, input_func)
        if rag_enabled:
            console.print("[magenta]正在初始化知识库增强...[/magenta]")
            missing_embedding = missing_embedding_settings(settings)
            if missing_embedding:
                raise RAGConfigurationError(
                    "Embedding 配置不完整，缺少："
                    + "、".join(missing_embedding)
                )
            try:
                knowledge_base = rag_factory(settings)
                build_result = await knowledge_base.build()
            except RAGConfigurationError:
                raise
            except Exception as exc:
                console.print("[red]RAG 初始化失败：[/red]", end="")
                console.print(str(exc), style="red", markup=False, highlight=False)
                return 4
            if build_result.skipped_files:
                console.print(
                    "[yellow]知识库跳过无法读取的文件："
                    f"{escape('、'.join(build_result.skipped_files))}[/yellow]"
                )
            if build_result.chunk_count:
                console.print(
                    "[magenta]知识库索引已构建："
                    f"{build_result.file_count} 个文件，"
                    f"{build_result.chunk_count} 个文本块。[/magenta]"
                )
            else:
                console.print(
                    "[yellow]知识库为空；本次不会注入知识上下文，"
                    "Agent 仍可使用已连接的 MCP Tool。[/yellow]"
                )
        else:
            console.print("[magenta]知识库增强已关闭；不会初始化 Embedding。[/magenta]")

        missing_llm = missing_llm_settings(settings)
        if missing_llm:
            exc = LLMConfigurationError(
                "LLM 配置不完整，缺少：" + "、".join(missing_llm)
            )
            console.print(f"[red]LLM 初始化失败：{exc}[/red]")
            console.print(
                "[yellow]请根据 .env.example 配置后重试，或运行 "
                "python main.py --check 查看状态。[/yellow]"
            )
            return 2

        console.print("[yellow]正在初始化 MCP Server...[/yellow]")
        try:
            mcp_runtime = mcp_factory(settings)
            connections = await mcp_runtime.connect_all()
        except MCPConfigError as exc:
            console.print(f"[red]MCP 配置错误：{exc}[/red]")
            return 3

        for name in mcp_runtime.configuration.disabled_servers:
            console.print(f"[yellow]MCP {escape(name)}：已禁用[/yellow]")
        for failure in mcp_runtime.failures:
            console.print(
                f"[red]MCP {escape(failure.name)} 连接失败："
                f"{escape(failure.message)}[/red]"
            )
        for connection in connections:
            tools = "、".join(connection.tool_names) or "未发现工具"
            console.print(
                f"[green]MCP {escape(connection.name)}：连接成功 "
                f"({escape(tools)})[/green]"
            )
        console.print(
            f"[cyan]成功连接 {len(connections)} 个 MCP Server。[/cyan]"
        )
        if not connections:
            console.print(
                "[red]当前没有任何可用 MCP Server，无法进入自动工具调用模式。[/red]"
            )
            return 3

        try:
            runtime = runtime_factory(
                settings,
                mcp_servers=mcp_runtime.active_servers,
                available_capabilities=mcp_runtime.capabilities,
                knowledge_base=knowledge_base,
            )
        except Exception as exc:
            console.print("[red]LLM 初始化失败：[/red]", end="")
            console.print(str(exc), style="red", markup=False, highlight=False)
            return 2
        console.print(
            f"[green]LLM Agent 已初始化：{escape(settings.llm_model or '')}[/green]"
        )
        while True:
            query = read_multiline_query(console, input_func)
            if query is None:
                console.print("[red]任务内容不能为空，请重新输入。[/red]")
                continue
            console.print("\n[cyan]正在处理：[/cyan]", end="")
            console.print(query, style="white", markup=False, highlight=False)
            console.print("[green]回复：[/green]", end="")

            def write_delta(delta: str) -> None:
                console.print(
                    delta,
                    end="",
                    markup=False,
                    highlight=False,
                )

            def show_tool_call(tool_call: AgentToolCall) -> None:
                console.print(
                    f"\n[cyan]Tool Call：{escape(tool_call.name)}[/cyan]"
                )
                console.print("[cyan]Tool 参数：[/cyan]", end="")
                console.print(
                    normalize_tool_output(tool_call.arguments),
                    markup=False,
                    highlight=False,
                )

            def show_tool_result(tool_result: AgentToolResult) -> None:
                style = "red" if tool_result.is_error else "green"
                console.print(f"[{style}]Tool Result：[/{style}]", end="")
                console.print(
                    tool_result.output or "(空结果)",
                    style=style,
                    markup=False,
                    highlight=False,
                )

            try:
                await runtime.stream_response(
                    query,
                    write_delta,
                    on_tool_call=show_tool_call,
                    on_tool_result=show_tool_result,
                )
            except Exception as exc:
                console.print("\n[red]Agent 执行失败：[/red]", end="")
                console.print(str(exc), style="red", markup=False, highlight=False)
            else:
                console.print("\n[green]查询完成。[/green]")
    except ExitRequested:
        console.print("\n[bold red]已退出 GAsecAgent。[/bold red]")
        return 0
    except KeyboardInterrupt:
        console.print("\n[bold red]收到 Ctrl+C，已安全退出 GAsecAgent。[/bold red]")
        return 0
    except EOFError:
        console.print("\n[bold red]输入流已结束，GAsecAgent 已安全退出。[/bold red]")
        return 0
    except RAGConfigurationError as exc:
        console.print(f"[red]RAG 初始化失败：{exc}[/red]")
        console.print(
            "[yellow]请根据 .env.example 配置 Embedding，"
            "或重新启动并选择 no。[/yellow]"
        )
        return 4
    finally:
        try:
            if runtime is not None:
                await runtime.aclose()
        finally:
            try:
                if mcp_runtime is not None:
                    await mcp_runtime.cleanup()
            finally:
                if knowledge_base is not None:
                    await knowledge_base.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="GAsecAgent",
        description="GAsecAgent 中文自动化安全 Agent",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查本地配置和依赖，不连接外部服务",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"GAsecAgent v{__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_windows_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(highlight=False)
    try:
        settings = load_settings(PROJECT_ROOT)
    except ConfigError as exc:
        console.print(f"[red]配置错误：{exc}[/red]")
        return 2
    if args.check:
        return run_check(settings, console)
    return asyncio.run(run_interactive(settings, console))
