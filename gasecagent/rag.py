"""GAsecAgent 的可选本地文本 RAG 知识库。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from openai import AsyncOpenAI

from .config import Settings


class RAGConfigurationError(RuntimeError):
    """Embedding 配置缺失或无效。"""


class RAGRuntimeError(RuntimeError):
    """知识库构建或检索失败。"""


def missing_embedding_settings(settings: Settings) -> tuple[str, ...]:
    """返回缺失的 Embedding 环境变量名，不暴露 Secret。"""

    missing: list[str] = []
    if not settings.embedding_api_key:
        missing.append("GA_EMBEDDING_API_KEY")
    if not settings.embedding_base_url:
        missing.append("GA_EMBEDDING_BASE_URL")
    if not settings.embedding_model:
        missing.append("GA_EMBEDDING_MODEL")
    return tuple(missing)


def split_text(text: str, chunk_size: int = 5000) -> list[str]:
    """将文本按固定字符数切块，不设置 overlap。"""

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    return [
        text[offset : offset + chunk_size]
        for offset in range(0, len(text), chunk_size)
        if text[offset : offset + chunk_size]
    ]


@dataclass(frozen=True, slots=True)
class KnowledgeDocuments:
    """从知识库目录读取到的文本与文件统计。"""

    text: str
    file_count: int
    skipped_files: tuple[str, ...] = ()


def load_knowledge_documents(directory: Path) -> KnowledgeDocuments:
    """读取目录顶层的 UTF-8 文本文件；缺失或空目录返回空内容。"""

    if not directory.is_dir():
        return KnowledgeDocuments(text="", file_count=0)

    texts: list[str] = []
    skipped: list[str] = []
    file_count = 0
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            skipped.append(path.name)
            continue
        file_count += 1
        if content:
            texts.append(content)
    return KnowledgeDocuments(
        text="\n".join(texts),
        file_count=file_count,
        skipped_files=tuple(skipped),
    )


class EmbeddingProvider(Protocol):
    """知识库所需的最小 Embedding 接口。"""

    async def embed(self, texts: Sequence[str]) -> NDArray[np.float64]: ...

    async def aclose(self) -> None: ...


class OpenAIEmbeddingProvider:
    """通过 OpenAI-compatible Embeddings API 获取向量。"""

    def __init__(self, settings: Settings) -> None:
        missing = missing_embedding_settings(settings)
        if missing:
            raise RAGConfigurationError(
                "Embedding 配置不完整，缺少：" + "、".join(missing)
            )
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )

    async def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        vectors: list[list[float]] = []
        for text in texts:
            response = await self.client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            if not response.data:
                raise RAGRuntimeError("Embedding API 未返回向量")
            vector = response.data[0].embedding
            if len(vector) != self.dimensions:
                raise RAGRuntimeError(
                    "Embedding 向量维度不符："
                    f"期望 {self.dimensions}，实际 {len(vector)}"
                )
            vectors.append(vector)
        if not vectors:
            return np.empty((0, self.dimensions), dtype=np.float64)
        return np.asarray(vectors, dtype=np.float64)

    async def aclose(self) -> None:
        await self.client.close()


@dataclass(frozen=True, slots=True)
class KnowledgeBuildResult:
    file_count: int
    chunk_count: int
    skipped_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    text: str
    similarity: float
    index: int


def cosine_similarities(
    query_vector: NDArray[np.float64],
    document_vectors: NDArray[np.float64],
) -> NDArray[np.float64]:
    """计算查询向量与所有文档向量的余弦相似度。"""

    query = np.asarray(query_vector, dtype=np.float64).reshape(-1)
    documents = np.asarray(document_vectors, dtype=np.float64)
    if documents.ndim != 2:
        raise ValueError("document_vectors 必须是二维数组")
    if documents.shape[1] != query.shape[0]:
        raise ValueError("查询向量与文档向量维度不一致")
    if documents.shape[0] == 0:
        return np.empty(0, dtype=np.float64)

    query_norm = np.linalg.norm(query)
    document_norms = np.linalg.norm(documents, axis=1)
    denominators = document_norms * query_norm
    scores = np.zeros(documents.shape[0], dtype=np.float64)
    valid = denominators > 0
    scores[valid] = (documents[valid] @ query) / denominators[valid]
    return scores


class KnowledgeBase:
    """进程内文本切块与 NumPy 向量索引。"""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider or OpenAIEmbeddingProvider(settings)
        self.chunks: list[str] = []
        self.vectors = np.empty(
            (0, settings.embedding_dimensions), dtype=np.float64
        )

    async def build(self) -> KnowledgeBuildResult:
        documents = load_knowledge_documents(self.settings.knowledge_base_path)
        chunks = split_text(documents.text, self.settings.rag_chunk_size)
        if not chunks:
            self.chunks = []
            self.vectors = np.empty(
                (0, self.settings.embedding_dimensions), dtype=np.float64
            )
            return KnowledgeBuildResult(
                file_count=documents.file_count,
                chunk_count=0,
                skipped_files=documents.skipped_files,
            )

        vectors = np.asarray(await self.provider.embed(chunks), dtype=np.float64)
        if vectors.ndim != 2 or vectors.shape[0] != len(chunks):
            raise RAGRuntimeError(
                "Embedding 返回数量与知识库切块数量不一致"
            )
        self.chunks = chunks
        self.vectors = vectors
        return KnowledgeBuildResult(
            file_count=documents.file_count,
            chunk_count=len(chunks),
            skipped_files=documents.skipped_files,
        )

    async def search(
        self,
        query: str,
        top_k: int | None = None,
    ) -> tuple[KnowledgeSearchResult, ...]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("检索内容不能为空")
        if not self.chunks:
            return ()

        requested = self.settings.rag_top_k if top_k is None else top_k
        if requested <= 0:
            raise ValueError("top_k 必须大于 0")
        query_vectors = np.asarray(
            await self.provider.embed([normalized_query]), dtype=np.float64
        )
        if query_vectors.ndim != 2 or query_vectors.shape[0] != 1:
            raise RAGRuntimeError("Embedding 未返回唯一的查询向量")
        scores = cosine_similarities(query_vectors[0], self.vectors)
        indices = np.argsort(scores)[::-1][: min(requested, len(self.chunks))]
        return tuple(
            KnowledgeSearchResult(
                text=self.chunks[int(index)],
                similarity=float(scores[int(index)]),
                index=int(index),
            )
            for index in indices
        )

    async def retrieve_context(self, query: str) -> str | None:
        results = await self.search(query)
        if not results:
            return None
        if len(results) == 1:
            return results[0].text
        return "\n\n".join(
            f"[知识片段 {position}]\n{result.text}"
            for position, result in enumerate(results, start=1)
        )

    async def aclose(self) -> None:
        await self.provider.aclose()
