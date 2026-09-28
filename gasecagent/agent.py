"""GAsecAgent 的 OpenAI-compatible LLM Agent 运行时。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from agents import Agent, Model, ModelSettings, RunConfig, Runner
from agents.mcp import MCPServer
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent

from .config import Settings
from .tool_output import normalize_tool_output


BASE_SYSTEM_PROMPT = """你是 GAsecAgent，一名专业的中文网络安全与渗透测试助手。

你的职责是理解用户提出的授权安全测试任务，给出准确、清晰、可验证的分析。遵循以下规则：
1. 使用中文回答，必要时保留标准英文安全术语、CVE 编号和命令参数。
2. 区分已验证事实、工具实际结果、合理推断和仍需验证的事项；绝不伪造扫描、搜索或漏洞结果。
3. 运行时可能动态提供 MCP 工具。仅使用当前真实可用的工具，根据任务选择工具，分析工具结果后决定继续调用或给出最终回答。
4. 如果当前没有合适工具，应明确说明能力边界，不得声称已经执行不存在的工具。
5. 当运行时提供知识库上下文时，将其作为补充证据，并在知识库信息与实时工具结果冲突时指出差异。
6. 输出应聚焦任务结论、证据、风险和可执行的修复建议，避免无关扩展。
"""


class LLMConfigurationError(RuntimeError):
    """LLM 配置缺失或无效。"""


class AgentRuntimeError(RuntimeError):
    """Agent 运行完成但没有可用结果。"""


def missing_llm_settings(settings: Settings) -> tuple[str, ...]:
    """返回缺失的 LLM 环境变量名，不暴露 Secret。"""

    missing: list[str] = []
    if not settings.llm_api_key:
        missing.append("GA_LLM_API_KEY")
    if not settings.llm_base_url:
        missing.append("GA_LLM_BASE_URL")
    if not settings.llm_model:
        missing.append("GA_LLM_MODEL")
    return tuple(missing)


def build_system_prompt(
    available_capabilities: Sequence[str] = (),
    knowledge_context: str | None = None,
) -> str:
    """按运行时真实能力构建 Prompt，不写入未连接的工具。"""

    sections = [BASE_SYSTEM_PROMPT.strip()]
    capabilities = [item.strip() for item in available_capabilities if item.strip()]
    if capabilities:
        sections.append("当前已连接能力：\n- " + "\n- ".join(capabilities))
    if knowledge_context and knowledge_context.strip():
        sections.append("当前知识库检索上下文：\n" + knowledge_context.strip())
    return "\n\n".join(sections)


@dataclass(slots=True)
class ConversationMemory:
    """仅在当前进程保存最近若干轮 user/assistant 对话。"""

    max_turns: int = 50
    _messages: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_turns <= 0:
            raise ValueError("max_turns 必须大于 0")

    @property
    def messages(self) -> list[dict[str, str]]:
        return [message.copy() for message in self._messages]

    def input_for(self, query: str) -> list[dict[str, str]]:
        return [*self.messages, {"role": "user", "content": query}]

    def remember(self, query: str, response: str) -> None:
        self._messages.extend(
            (
                {"role": "user", "content": query},
                {"role": "assistant", "content": response},
            )
        )
        maximum_messages = self.max_turns * 2
        if len(self._messages) > maximum_messages:
            del self._messages[:-maximum_messages]


class OpenAICompatibleProvider:
    """为 Agents SDK 创建 OpenAI-compatible Chat Completions 模型。"""

    def __init__(self, settings: Settings) -> None:
        missing = missing_llm_settings(settings)
        if missing:
            raise LLMConfigurationError(
                "LLM 配置不完整，缺少：" + "、".join(missing)
            )
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        self.model = OpenAIChatCompletionsModel(
            model=settings.llm_model or "",
            openai_client=self.client,
        )

    async def aclose(self) -> None:
        await self.client.close()


TextSink = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    name: str
    arguments: Any
    call_id: str | None


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    call_id: str | None
    output: str
    is_error: bool


ToolCallSink = Callable[[AgentToolCall], None]
ToolResultSink = Callable[[AgentToolResult], None]


class KnowledgeRetriever(Protocol):
    """Agent 只依赖查询级知识上下文接口。"""

    async def retrieve_context(self, query: str) -> str | None: ...


def extract_text_delta(event: Any) -> str | None:
    """从 Agents SDK raw stream event 中提取文本增量。"""

    if getattr(event, "type", None) != "raw_response_event":
        return None
    data = getattr(event, "data", None)
    if not isinstance(data, ResponseTextDeltaEvent) and getattr(
        data, "type", None
    ) != "response.output_text.delta":
        return None
    delta = getattr(data, "delta", None)
    return delta if isinstance(delta, str) and delta else None


def _tool_arguments(raw_arguments: Any) -> Any:
    if not isinstance(raw_arguments, str):
        return raw_arguments
    try:
        return json.loads(raw_arguments)
    except json.JSONDecodeError:
        return raw_arguments


def extract_tool_call(event: Any) -> AgentToolCall | None:
    """提取 SDK Tool Call 展示信息，不介入工具分发。"""

    if getattr(event, "type", None) != "run_item_stream_event":
        return None
    item = getattr(event, "item", None)
    if getattr(item, "type", None) != "tool_call_item":
        return None
    raw_item = getattr(item, "raw_item", None)
    name = getattr(item, "tool_name", None)
    call_id = getattr(item, "call_id", None)
    if isinstance(raw_item, dict):
        name = name or raw_item.get("name")
        call_id = call_id or raw_item.get("call_id") or raw_item.get("id")
        arguments = raw_item.get("arguments", {})
    else:
        name = name or getattr(raw_item, "name", None)
        call_id = call_id or getattr(raw_item, "call_id", None)
        arguments = getattr(raw_item, "arguments", {})
    return AgentToolCall(
        name=str(name or "未知工具"),
        arguments=_tool_arguments(arguments),
        call_id=str(call_id) if call_id is not None else None,
    )


def extract_tool_result(event: Any) -> AgentToolResult | None:
    """提取并规范化 SDK Tool Result 展示信息。"""

    if getattr(event, "type", None) != "run_item_stream_event":
        return None
    item = getattr(event, "item", None)
    if getattr(item, "type", None) != "tool_call_output_item":
        return None
    output = getattr(item, "output", None)
    raw_item = getattr(item, "raw_item", None)
    custom_data = getattr(item, "custom_data", None)
    call_id = getattr(item, "call_id", None)
    is_error = False
    for candidate in (output, raw_item, custom_data):
        if isinstance(candidate, dict):
            is_error = is_error or bool(
                candidate.get("isError") or candidate.get("is_error")
            )
        else:
            is_error = is_error or bool(
                getattr(candidate, "isError", False)
                or getattr(candidate, "is_error", False)
            )
    display_output: Any = output
    if isinstance(custom_data, dict) and custom_data.get("structured_content") is not None:
        display_output = {
            "content": output,
            "structuredContent": custom_data["structured_content"],
        }
    return AgentToolResult(
        call_id=str(call_id) if call_id is not None else None,
        output=normalize_tool_output(display_output),
        is_error=is_error,
    )


class AgentRuntime:
    """封装单 Agent、流式运行和进程内多轮上下文。"""

    def __init__(
        self,
        settings: Settings,
        *,
        model: Model | None = None,
        memory: ConversationMemory | None = None,
        mcp_servers: Sequence[MCPServer] = (),
        available_capabilities: Sequence[str] = (),
        knowledge_base: KnowledgeRetriever | None = None,
    ) -> None:
        missing = missing_llm_settings(settings)
        if missing and model is None:
            raise LLMConfigurationError(
                "LLM 配置不完整，缺少：" + "、".join(missing)
            )

        self.settings = settings
        self.memory = memory or ConversationMemory(settings.history_max_turns)
        self.knowledge_base = knowledge_base
        self.available_capabilities = tuple(available_capabilities)
        self._provider = None if model is not None else OpenAICompatibleProvider(settings)
        selected_model = model if model is not None else self._provider.model
        connected_mcp_servers = list(mcp_servers)
        self.agent = Agent(
            name="GAsecAgent 网络安全专家",
            instructions=build_system_prompt(self.available_capabilities),
            model=selected_model,
            mcp_servers=connected_mcp_servers,
            model_settings=ModelSettings(
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
                max_tokens=settings.llm_max_tokens,
                tool_choice="auto" if connected_mcp_servers else None,
                parallel_tool_calls=True if connected_mcp_servers else None,
            ),
        )
        self.run_config = RunConfig(
            tracing_disabled=True,
            trace_include_sensitive_data=False,
            workflow_name="GAsecAgent LLM Agent",
        )

    async def stream_response(
        self,
        query: str,
        on_text: TextSink,
        on_tool_call: ToolCallSink | None = None,
        on_tool_result: ToolResultSink | None = None,
    ) -> str:
        """流式运行一次 Agent，仅在成功后写入多轮历史。"""

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("任务内容不能为空")

        knowledge_context = None
        if self.knowledge_base is not None:
            knowledge_context = await self.knowledge_base.retrieve_context(
                normalized_query
            )
        run_agent = self.agent.clone(
            instructions=build_system_prompt(
                self.available_capabilities,
                knowledge_context=knowledge_context,
            )
        )

        result = Runner.run_streamed(
            run_agent,
            input=self.memory.input_for(normalized_query),
            max_turns=self.settings.llm_max_turns,
            run_config=self.run_config,
        )
        emitted_text = False
        async for event in result.stream_events():
            delta = extract_text_delta(event)
            if delta is not None:
                emitted_text = True
                on_text(delta)
            tool_call = extract_tool_call(event)
            if tool_call is not None and on_tool_call is not None:
                on_tool_call(tool_call)
            tool_result = extract_tool_result(event)
            if tool_result is not None and on_tool_result is not None:
                on_tool_result(tool_result)

        final_output = result.final_output
        response = final_output if isinstance(final_output, str) else str(final_output or "")
        response = response.strip()
        if not response:
            raise AgentRuntimeError("模型未返回可用的最终回答")
        if not emitted_text:
            on_text(response)
        self.memory.remember(normalized_query, response)
        return response

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()
