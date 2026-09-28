"""通过 stdio MCP 调用外部 fscan 二进制。

本模块只负责参数校验、进程隔离和结果回传，不包含 fscan 扫描器本身。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


SERVER_NAME = "GAsecAgent Fscan MCP"
SUPPORTED_OUTPUT_FORMATS = frozenset({"json", "txt", "csv"})
SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:/,\[\]-]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

mcp = FastMCP(SERVER_NAME, log_level="ERROR")


class FscanConfigurationError(ValueError):
    """Fscan 本地依赖或调用参数无效。"""


def _binary_path() -> Path:
    value = os.environ.get("GA_FSCAN_BINARY", "").strip()
    if not value:
        raise FscanConfigurationError("缺少 GA_FSCAN_BINARY 配置")
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise FscanConfigurationError(f"Fscan 外部程序不存在：{path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_binary(path: Path) -> str:
    expected = os.environ.get("GA_FSCAN_SHA256", "").strip().lower()
    if not expected:
        return ""
    if not SHA256_PATTERN.fullmatch(expected):
        raise FscanConfigurationError("GA_FSCAN_SHA256 必须是 64 位十六进制值")
    actual = _sha256(path)
    if actual != expected:
        raise FscanConfigurationError(
            f"Fscan SHA-256 不匹配：期望 {expected}，实际 {actual}"
        )
    return actual


def _safe_value(value: str, name: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if not normalized or not SAFE_VALUE_PATTERN.fullmatch(normalized):
        raise FscanConfigurationError(f"{name} 包含不支持的字符")
    return normalized


def _target_arguments(target: str) -> list[str]:
    normalized = _safe_value(target, "target")
    parsed = urlsplit(normalized)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FscanConfigurationError("URL target 仅支持有效的 http/https 地址")
        return ["-u", normalized]
    return ["-h", normalized]


def build_fscan_command(
    binary: Path,
    output_path: Path,
    *,
    target: str,
    mode: str,
    ports: str,
    threads: int,
    output_format: str,
    timeout: int,
    proxy: str,
    poc_name: str,
    no_scan: bool,
) -> list[str]:
    """构造 fscan v2.2.0 参数列表；不经 shell 拼接。"""

    if isinstance(threads, bool) or threads <= 0:
        raise FscanConfigurationError("threads 必须是正整数")
    if isinstance(timeout, bool) or timeout <= 0:
        raise FscanConfigurationError("timeout 必须是正整数")
    normalized_format = output_format.strip().lower()
    if normalized_format not in SUPPORTED_OUTPUT_FORMATS:
        raise FscanConfigurationError("output_format 仅支持 json、txt 或 csv")

    command = [
        str(binary),
        *_target_arguments(target),
        "-m",
        _safe_value(mode, "mode"),
        "-t",
        str(threads),
        "-time",
        str(timeout),
        "-o",
        str(output_path),
        "-f",
        normalized_format,
        "-nocolor",
        "-nopg",
    ]
    normalized_ports = _safe_value(ports, "ports", allow_empty=True)
    if normalized_ports:
        command.extend(["-p", normalized_ports])
    normalized_proxy = proxy.strip()
    if normalized_proxy:
        parsed_proxy = urlsplit(normalized_proxy)
        if parsed_proxy.scheme not in {"http", "https", "socks5"} or not parsed_proxy.netloc:
            raise FscanConfigurationError("proxy 必须是有效的 http/https/socks5 URL")
        command.extend(["-proxy", normalized_proxy])
    normalized_poc = _safe_value(poc_name, "poc_name", allow_empty=True)
    if normalized_poc:
        command.extend(["-pocname", normalized_poc])
    if no_scan:
        command.append("-ao")
    return command


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


async def run_fscan(
    *,
    target: str = "127.0.0.1",
    mode: str = "All",
    ports: str = "",
    threads: int = 60,
    output_format: str = "json",
    timeout: int = 300,
    proxy: str = "",
    poc_name: str = "",
    no_scan: bool = False,
) -> dict[str, Any]:
    """执行一次隔离的 fscan 子进程，并返回可序列化结果。"""

    try:
        binary = _binary_path()
        verified_sha256 = _verify_binary(binary)
        normalized_output_format = output_format.strip().lower()
        with tempfile.TemporaryDirectory(prefix="gasecagent-fscan-") as temp_dir:
            output_path = Path(temp_dir) / f"result.{normalized_output_format}"
            command = build_fscan_command(
                binary,
                output_path,
                target=target,
                mode=mode,
                ports=ports,
                threads=threads,
                output_format=normalized_output_format,
                timeout=timeout,
                proxy=proxy,
                poc_name=poc_name,
                no_scan=no_scan,
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=temp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                return {
                    "status": "timeout",
                    "message": f"Fscan 执行超过 {timeout} 秒，进程已终止",
                    "exit_code": process.returncode,
                    "stdout": _decode(stdout),
                    "stderr": _decode(stderr),
                }

            output = ""
            if output_path.is_file():
                output = output_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            return {
                "status": "completed" if process.returncode == 0 else "error",
                "exit_code": process.returncode,
                "output_format": normalized_output_format,
                "output": output,
                "stdout": _decode(stdout),
                "stderr": _decode(stderr),
                "binary_sha256": verified_sha256 or None,
            }
    except (FscanConfigurationError, OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@mcp.tool()
async def fscan_scan(
    target: str = "127.0.0.1",
    mode: str = "All",
    ports: str = "",
    threads: int = 60,
    output_format: str = "json",
    timeout: int = 300,
    proxy: str = "",
    poc_name: str = "",
    no_scan: bool = False,
) -> dict[str, Any]:
    """对明确授权的目标调用外部 Fscan，并返回扫描结果。"""

    result = await run_fscan(
        target=target,
        mode=mode,
        ports=ports,
        threads=threads,
        output_format=output_format,
        timeout=timeout,
        proxy=proxy,
        poc_name=poc_name,
        no_scan=no_scan,
    )
    if result.get("status") in {"error", "timeout"}:
        detail = result.get("message") or result.get("stderr") or "Fscan 执行失败"
        raise ToolError(str(detail))
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
