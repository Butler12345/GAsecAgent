from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from gasecagent.config import PROJECT_ROOT


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONUTF8", None)
    environment.pop("PYTHONIOENCODING", None)
    environment["GA_LLM_API_KEY"] = ""
    environment["GA_LLM_BASE_URL"] = ""
    environment["GA_LLM_MODEL"] = ""
    environment["GA_EMBEDDING_API_KEY"] = ""
    return environment


def test_check_works_outside_project_directory(tmp_path: Path) -> None:
    environment = _environment()
    environment["GA_MCP_CONFIG"] = str(tmp_path / "missing-mcp.json")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), "--check"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "项目根" in result.stdout
    assert "已确认" in result.stdout
    assert "配置存在但未连接" not in result.stdout
    assert "未配置" in result.stdout


def test_version_uses_release_format(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), "--version"],
        cwd=tmp_path,
        env=_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "GAsecAgent v0.1.0"


def test_quit_exits_without_trailing_blank_line(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py")],
        cwd=tmp_path,
        env=_environment(),
        input="quit\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "已退出 GAsecAgent" in result.stdout
    assert "LLM 初始化失败" not in result.stdout


def test_eof_exits_cleanly(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py")],
        cwd=tmp_path,
        env=_environment(),
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "输入流已结束" in result.stdout


def test_missing_llm_configuration_fails_in_chinese(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py")],
        cwd=tmp_path,
        env=_environment(),
        input="no\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "LLM 初始化失败" in result.stdout
    assert "GA_LLM_API_KEY" in result.stdout
    assert "查询完成" not in result.stdout
