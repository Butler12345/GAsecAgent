"""GAsecAgent 项目配置与路径解析。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
DEFAULT_FSCAN_SHA256 = (
    "5aefcbfa98b8e8814dc415a87e4e7e8716004048857d80b37f03e600b6fd2441"
)


class ConfigError(ValueError):
    """配置值无效。"""


def _optional_text(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw_value = values.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是正整数，当前值为：{raw_value!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0，当前值为：{value}")
    return value


def _bounded_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = values.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前值为：{raw_value!r}") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(
            f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间，当前值为：{raw_value!r}"
        )
    return value


def _project_path(project_root: Path, raw_value: str, default: str) -> Path:
    configured = Path(raw_value.strip() or default).expanduser()
    if not configured.is_absolute():
        configured = project_root / configured
    return configured.resolve(strict=False)


def _sha256_value(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, "").strip() or default
    if not SHA256_PATTERN.fullmatch(value):
        raise ConfigError(f"{name} 必须是 64 位十六进制 SHA-256")
    return value.lower()


@dataclass(frozen=True, slots=True)
class Settings:
    """从项目根 `.env` 与进程环境组合得到的只读配置。"""

    project_root: Path
    env_file: Path
    env_file_exists: bool
    llm_api_key: str | None
    llm_base_url: str | None
    llm_model: str | None
    llm_temperature: float
    llm_top_p: float
    llm_max_tokens: int
    llm_max_turns: int
    history_max_turns: int
    embedding_api_key: str | None
    embedding_base_url: str
    embedding_model: str
    embedding_dimensions: int
    rag_chunk_size: int
    rag_top_k: int
    mcp_config_path: Path
    knowledge_base_path: Path
    filesystem_root: Path
    filesystem_mcp_entry_path: Path
    fscan_binary_path: Path
    fscan_expected_sha256: str
    tavily_api_key: str | None
    tavily_mcp_entry_path: Path
    fofa_api_key: str | None
    fofa_email: str | None

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def embedding_configured(self) -> bool:
        return bool(
            self.embedding_api_key
            and self.embedding_base_url
            and self.embedding_model
            and self.embedding_dimensions > 0
        )


def load_settings(
    project_root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """加载配置；进程环境优先于项目根目录中的 `.env`。"""

    root = project_root.resolve(strict=False)
    env_file = root / ".env"
    values = load_project_environment(root, environ)

    return Settings(
        project_root=root,
        env_file=env_file,
        env_file_exists=env_file.is_file(),
        llm_api_key=_optional_text(values, "GA_LLM_API_KEY"),
        llm_base_url=_optional_text(values, "GA_LLM_BASE_URL"),
        llm_model=_optional_text(values, "GA_LLM_MODEL"),
        llm_temperature=_bounded_float(
            values, "GA_LLM_TEMPERATURE", 0.6, 0.0, 2.0
        ),
        llm_top_p=_bounded_float(values, "GA_LLM_TOP_P", 0.9, 0.0, 1.0),
        llm_max_tokens=_positive_int(values, "GA_LLM_MAX_TOKENS", 20000),
        llm_max_turns=_positive_int(values, "GA_LLM_MAX_TURNS", 10),
        history_max_turns=_positive_int(values, "GA_HISTORY_MAX_TURNS", 50),
        embedding_api_key=_optional_text(values, "GA_EMBEDDING_API_KEY"),
        embedding_base_url=values.get(
            "GA_EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).strip(),
        embedding_model=values.get(
            "GA_EMBEDDING_MODEL", "text-embedding-v3"
        ).strip(),
        embedding_dimensions=_positive_int(
            values, "GA_EMBEDDING_DIMENSIONS", 1024
        ),
        rag_chunk_size=_positive_int(values, "GA_RAG_CHUNK_SIZE", 5000),
        rag_top_k=_positive_int(values, "GA_RAG_TOP_K", 1),
        mcp_config_path=_project_path(
            root, values.get("GA_MCP_CONFIG", ""), "mcp.json"
        ),
        knowledge_base_path=_project_path(
            root,
            values.get("GA_KNOWLEDGE_BASE_DIR", ""),
            "knowledge_base_docs",
        ),
        filesystem_root=_project_path(
            root,
            values.get("GA_FILESYSTEM_ROOT", ""),
            "workspace",
        ),
        filesystem_mcp_entry_path=_project_path(
            root,
            values.get("GA_FILESYSTEM_MCP_ENTRY", ""),
            (
                ".external/node-mcp/node_modules/@modelcontextprotocol/"
                "server-filesystem/dist/index.js"
            ),
        ),
        fscan_binary_path=_project_path(
            root,
            values.get("GA_FSCAN_BINARY", ""),
            "tools/fscan/fscan.exe",
        ),
        fscan_expected_sha256=_sha256_value(
            values,
            "GA_FSCAN_SHA256",
            DEFAULT_FSCAN_SHA256,
        ),
        tavily_api_key=_optional_text(values, "TAVILY_API_KEY"),
        tavily_mcp_entry_path=_project_path(
            root,
            values.get("GA_TAVILY_MCP_ENTRY", ""),
            ".external/node-mcp/node_modules/tavily-mcp/build/index.js",
        ),
        fofa_api_key=_optional_text(values, "FOFA_KEY"),
        fofa_email=_optional_text(values, "FOFA_EMAIL"),
    )


def load_project_environment(
    project_root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """读取项目 `.env` 并以进程环境覆盖，供子进程安全传递配置。"""

    root = project_root.resolve(strict=False)
    env_file = root / ".env"
    file_values = {
        key: value
        for key, value in dotenv_values(env_file).items()
        if value is not None
    }
    process_values = dict(os.environ if environ is None else environ)
    return {**file_values, **process_values}
