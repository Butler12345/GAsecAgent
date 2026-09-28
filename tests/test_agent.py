from __future__ import annotations

from pathlib import Path

import pytest
from agents.testing import ScriptedModel, assistant_message

from gasecagent.agent import (
    AgentRuntime,
    ConversationMemory,
    LLMConfigurationError,
    OpenAICompatibleProvider,
    build_system_prompt,
    missing_llm_settings,
)
from gasecagent.config import load_settings


def configured_settings(tmp_path: Path):
    return load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
        },
    )


def test_system_prompt_uses_only_runtime_capabilities() -> None:
    prompt_without_tools = build_system_prompt()
    assert "GAsecAgent" in prompt_without_tools
    assert "Tavily" not in prompt_without_tools
    assert "FOFA" not in prompt_without_tools

    prompt_with_tool = build_system_prompt(["实时搜索：search_web"])
    assert "实时搜索：search_web" in prompt_with_tool


def test_memory_keeps_role_based_recent_turns() -> None:
    memory = ConversationMemory(max_turns=2)
    memory.remember("问题一", "回答一")
    memory.remember("问题二", "回答二")
    memory.remember("问题三", "回答三")

    assert memory.messages == [
        {"role": "user", "content": "问题二"},
        {"role": "assistant", "content": "回答二"},
        {"role": "user", "content": "问题三"},
        {"role": "assistant", "content": "回答三"},
    ]
    assert memory.input_for("问题四")[-1] == {
        "role": "user",
        "content": "问题四",
    }


def test_missing_configuration_lists_only_missing_names(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, environ={})
    assert missing_llm_settings(settings) == (
        "GA_LLM_API_KEY",
        "GA_LLM_BASE_URL",
        "GA_LLM_MODEL",
    )
    with pytest.raises(LLMConfigurationError, match="GA_LLM_API_KEY"):
        AgentRuntime(settings)


@pytest.mark.asyncio
async def test_openai_compatible_provider_constructs_without_network(
    tmp_path: Path,
) -> None:
    provider = OpenAICompatibleProvider(configured_settings(tmp_path))
    assert type(provider.model).__name__ == "OpenAIChatCompletionsModel"
    assert provider.model.model == "test-model"
    await provider.aclose()


@pytest.mark.asyncio
async def test_scripted_model_streaming_and_multi_turn_history(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path)
    model = ScriptedModel(
        [
            [assistant_message("第一轮回答")],
            [assistant_message("第二轮回答")],
        ]
    )
    runtime = AgentRuntime(settings, model=model)
    first_chunks: list[str] = []
    second_chunks: list[str] = []

    first = await runtime.stream_response("第一轮问题", first_chunks.append)
    second = await runtime.stream_response("继续说明", second_chunks.append)

    assert first == "第一轮回答"
    assert second == "第二轮回答"
    assert "".join(first_chunks) == "第一轮回答"
    assert "".join(second_chunks) == "第二轮回答"
    assert runtime.memory.messages[-2:] == [
        {"role": "user", "content": "继续说明"},
        {"role": "assistant", "content": "第二轮回答"},
    ]
    assert len(model.calls) == 2
    second_input = model.calls[1].input
    assert isinstance(second_input, list)
    assert second_input[:2] == [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
    ]
    assert model.calls[0].model_settings.temperature == 0.6
    assert model.calls[0].model_settings.top_p == 0.9
    assert model.calls[0].model_settings.max_tokens == 20000
    assert model.calls[0].model_settings.tool_choice is None
    assert model.calls[0].model_settings.parallel_tool_calls is None
    model.assert_complete()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_failed_model_run_is_not_saved_to_history(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path)
    model = ScriptedModel([RuntimeError("mock provider failed")])
    runtime = AgentRuntime(settings, model=model)

    with pytest.raises(RuntimeError, match="mock provider failed"):
        await runtime.stream_response("失败的问题", lambda _text: None)

    assert runtime.memory.messages == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_passes_configured_max_turns_to_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(
        tmp_path,
        environ={
            "GA_LLM_API_KEY": "test-key",
            "GA_LLM_BASE_URL": "https://example.invalid/v1",
            "GA_LLM_MODEL": "test-model",
            "GA_LLM_MAX_TURNS": "7",
        },
    )
    captured: dict[str, object] = {}

    class FakeStreamResult:
        final_output = "完成"

        async def stream_events(self):
            if False:
                yield None

    def fake_run_streamed(*args, **kwargs):
        captured.update(kwargs)
        return FakeStreamResult()

    monkeypatch.setattr(
        "gasecagent.agent.Runner.run_streamed",
        fake_run_streamed,
    )
    runtime = AgentRuntime(settings, model=ScriptedModel())
    chunks: list[str] = []

    assert await runtime.stream_response("检查 turn", chunks.append) == "完成"
    assert captured["max_turns"] == 7
    assert chunks == ["完成"]
    await runtime.aclose()
