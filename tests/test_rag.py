from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from agents.testing import ScriptedModel, assistant_message

from gasecagent.agent import AgentRuntime
from gasecagent.config import load_settings
from gasecagent.rag import (
    KnowledgeBase,
    OpenAIEmbeddingProvider,
    RAGConfigurationError,
    cosine_similarities,
    load_knowledge_documents,
    missing_embedding_settings,
    split_text,
)


class FakeEmbeddingProvider:
    def __init__(self, vectors: dict[str, Sequence[float]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        self.calls.append(tuple(texts))
        return np.asarray([self.vectors[text] for text in texts], dtype=np.float64)

    async def aclose(self) -> None:
        self.closed = True


def configured_settings(tmp_path: Path, **extra: str):
    environment = {
        "GA_LLM_API_KEY": "test-key",
        "GA_LLM_BASE_URL": "https://example.invalid/v1",
        "GA_LLM_MODEL": "test-model",
        "GA_EMBEDDING_API_KEY": "embedding-key",
        "GA_EMBEDDING_BASE_URL": "https://example.invalid/v1",
        "GA_EMBEDDING_MODEL": "embedding-model",
        "GA_EMBEDDING_DIMENSIONS": "2",
    }
    environment.update(extra)
    return load_settings(tmp_path, environ=environment)


def test_fixed_size_chunks_have_no_overlap() -> None:
    content = "甲" * 5000 + "乙" * 5000 + "丙"
    chunks = split_text(content)

    assert [len(chunk) for chunk in chunks] == [5000, 5000, 1]
    assert chunks[0][-1] == "甲"
    assert chunks[1][0] == "乙"
    assert "".join(chunks) == content


def test_documents_are_loaded_from_configured_directory_in_sorted_order(
    tmp_path: Path,
) -> None:
    knowledge_dir = tmp_path / "knowledge_base_docs"
    knowledge_dir.mkdir()
    (knowledge_dir / "b.txt").write_text("第二份", encoding="utf-8")
    (knowledge_dir / "a.txt").write_text("第一份", encoding="utf-8")
    (knowledge_dir / ".gitkeep").write_text("", encoding="utf-8")

    documents = load_knowledge_documents(knowledge_dir)

    assert documents.text == "第一份\n第二份"
    assert documents.file_count == 2
    assert documents.skipped_files == ()


@pytest.mark.asyncio
async def test_empty_knowledge_directory_does_not_call_embedding(
    tmp_path: Path,
) -> None:
    settings = configured_settings(tmp_path)
    settings.knowledge_base_path.mkdir()
    provider = FakeEmbeddingProvider({})
    knowledge_base = KnowledgeBase(settings, provider=provider)

    result = await knowledge_base.build()

    assert result.chunk_count == 0
    assert await knowledge_base.retrieve_context("任意问题") is None
    assert provider.calls == []
    await knowledge_base.aclose()
    assert provider.closed is True


def test_embedding_configuration_is_checked_only_when_provider_is_created(
    tmp_path: Path,
) -> None:
    settings = load_settings(tmp_path, environ={})

    assert missing_embedding_settings(settings) == ("GA_EMBEDDING_API_KEY",)
    with pytest.raises(RAGConfigurationError, match="GA_EMBEDDING_API_KEY"):
        OpenAIEmbeddingProvider(settings)


@pytest.mark.asyncio
async def test_openai_embedding_provider_constructs_without_network(
    tmp_path: Path,
) -> None:
    provider = OpenAIEmbeddingProvider(configured_settings(tmp_path))

    assert provider.model == "embedding-model"
    assert provider.dimensions == 2
    await provider.aclose()


def test_cosine_similarity_preserves_negative_scores_and_zero_vectors() -> None:
    scores = cosine_similarities(
        np.asarray([1.0, 0.0]),
        np.asarray([[-1.0, 0.0], [-0.5, 1.0], [0.0, 0.0]]),
    )

    assert scores[0] == pytest.approx(-1.0)
    assert scores[1] == pytest.approx(-0.4472135955)
    assert scores[2] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_retrieval_uses_cosine_similarity_and_default_top_one(
    tmp_path: Path,
) -> None:
    settings = configured_settings(
        tmp_path,
        GA_RAG_CHUNK_SIZE="2",
        GA_RAG_TOP_K="1",
    )
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "kb.txt").write_text("甲甲乙乙", encoding="utf-8")
    provider = FakeEmbeddingProvider(
        {
            "甲甲": [1.0, 0.0],
            "乙乙": [0.0, 1.0],
            "查询": [0.1, 1.0],
            "负查询": [-1.0, -0.1],
        }
    )
    knowledge_base = KnowledgeBase(settings, provider=provider)
    await knowledge_base.build()

    results = await knowledge_base.search("查询")

    assert len(results) == 1
    assert results[0].text == "乙乙"
    assert results[0].index == 1
    assert await knowledge_base.retrieve_context("查询") == "乙乙"
    negative_results = await knowledge_base.search("负查询")
    assert negative_results[0].text == "乙乙"
    assert negative_results[0].similarity < 0
    await knowledge_base.aclose()


@pytest.mark.asyncio
async def test_agent_injects_query_specific_knowledge_context(
    tmp_path: Path,
) -> None:
    class StubRetriever:
        async def retrieve_context(self, query: str) -> str:
            assert query == "分析这个问题"
            return "仅用于当前查询的知识片段"

    settings = configured_settings(tmp_path)
    model = ScriptedModel([[assistant_message("已结合知识回答")]])
    runtime = AgentRuntime(
        settings,
        model=model,
        knowledge_base=StubRetriever(),
    )

    response = await runtime.stream_response("分析这个问题", lambda _text: None)

    assert response == "已结合知识回答"
    assert "当前知识库检索上下文" in (model.calls[0].system_instructions or "")
    assert "仅用于当前查询的知识片段" in (
        model.calls[0].system_instructions or ""
    )
    model.assert_complete()
    await runtime.aclose()
