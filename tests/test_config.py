from __future__ import annotations

from pathlib import Path

import pytest

from gasecagent.config import ConfigError, PROJECT_ROOT, load_settings


def test_project_root_points_to_repository() -> None:
    assert PROJECT_ROOT == Path(__file__).resolve().parents[1]
    assert (PROJECT_ROOT / "mcp.example.json").is_file()


def test_load_settings_uses_project_env_and_process_precedence(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "GA_LLM_API_KEY=file-key\n"
        "GA_LLM_BASE_URL=https://example.invalid/v1\n"
        "GA_LLM_MODEL=file-model\n"
        "GA_MCP_CONFIG=config/mcp.json\n",
        encoding="utf-8",
    )

    settings = load_settings(
        tmp_path,
        environ={"GA_LLM_MODEL": "process-model"},
    )

    assert settings.env_file_exists is True
    assert settings.llm_api_key == "file-key"
    assert settings.llm_model == "process-model"
    assert settings.llm_configured is True
    assert settings.mcp_config_path == (tmp_path / "config" / "mcp.json").resolve()
    assert settings.knowledge_base_path == (tmp_path / "knowledge_base_docs").resolve()


def test_load_settings_keeps_embedding_optional(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, environ={})

    assert settings.embedding_api_key is None
    assert settings.embedding_configured is False
    assert settings.embedding_model == "text-embedding-v3"
    assert settings.embedding_dimensions == 1024
    assert settings.rag_chunk_size == 5000
    assert settings.rag_top_k == 1
    assert settings.llm_temperature == 0.6
    assert settings.llm_top_p == 0.9
    assert settings.llm_max_tokens == 20000
    assert settings.llm_max_turns == 10
    assert settings.history_max_turns == 50
    assert settings.filesystem_root == (tmp_path / "workspace").resolve()
    assert settings.filesystem_mcp_entry_path == (
        tmp_path
        / ".external"
        / "node-mcp"
        / "node_modules"
        / "@modelcontextprotocol"
        / "server-filesystem"
        / "dist"
        / "index.js"
    ).resolve()
    assert settings.fscan_binary_path == (
        tmp_path / "tools" / "fscan" / "fscan.exe"
    ).resolve()
    assert len(settings.fscan_expected_sha256) == 64
    assert settings.tavily_api_key is None
    assert settings.tavily_mcp_entry_path == (
        tmp_path
        / ".external"
        / "node-mcp"
        / "node_modules"
        / "tavily-mcp"
        / "build"
        / "index.js"
    ).resolve()
    assert settings.fofa_api_key is None
    assert settings.fofa_email is None


def test_filesystem_root_can_be_overridden_without_changing_default(
    tmp_path: Path,
) -> None:
    custom_root = tmp_path / "authorized-files"

    settings = load_settings(
        tmp_path,
        environ={"GA_FILESYSTEM_ROOT": str(custom_root)},
    )

    assert settings.filesystem_root == custom_root.resolve()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GA_RAG_CHUNK_SIZE", "0"),
        ("GA_RAG_TOP_K", "-1"),
        ("GA_EMBEDDING_DIMENSIONS", "not-a-number"),
        ("GA_LLM_MAX_TOKENS", "0"),
        ("GA_LLM_MAX_TURNS", "-1"),
        ("GA_HISTORY_MAX_TURNS", "invalid"),
    ],
)
def test_invalid_positive_integer_has_clear_error(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(tmp_path, environ={name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GA_LLM_TEMPERATURE", "2.1"),
        ("GA_LLM_TEMPERATURE", "nan"),
        ("GA_LLM_TOP_P", "-0.1"),
        ("GA_LLM_TOP_P", "not-a-number"),
    ],
)
def test_invalid_llm_float_has_clear_error(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(tmp_path, environ={name: value})


def test_invalid_fscan_sha256_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="GA_FSCAN_SHA256"):
        load_settings(tmp_path, environ={"GA_FSCAN_SHA256": "not-a-hash"})
