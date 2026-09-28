"""FOFA API 的 stdio MCP 适配器。"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


SERVER_NAME = "GAsecAgent FOFA MCP"
DEFAULT_API_BASE_URL = "https://fofa.info/api/v1"
DEFAULT_FIELDS = "host,ip,port"
FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.]+$")

mcp = FastMCP(SERVER_NAME, log_level="ERROR")


def _api_key() -> str:
    key = os.environ.get("FOFA_KEY", "").strip()
    if not key:
        raise ToolError("缺少 FOFA_KEY 配置")
    return key


def _api_base_url() -> str:
    return (
        os.environ.get("FOFA_API_BASE_URL", DEFAULT_API_BASE_URL)
        .strip()
        .rstrip("/")
    )


def _request_timeout() -> float:
    raw = os.environ.get("FOFA_REQUEST_TIMEOUT_SECONDS", "30").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ToolError("FOFA_REQUEST_TIMEOUT_SECONDS 必须是正数") from exc
    if value <= 0:
        raise ToolError("FOFA_REQUEST_TIMEOUT_SECONDS 必须是正数")
    return value


def _normalize_fields(fields: str) -> tuple[str, list[str]]:
    names = [item.strip() for item in fields.split(",") if item.strip()]
    if not names:
        names = DEFAULT_FIELDS.split(",")
    if len(names) > 64 or any(not FIELD_PATTERN.fullmatch(item) for item in names):
        raise ToolError("fields 必须是逗号分隔的 FOFA 字段名")
    return ",".join(names), names


def _validate_search(query: str, size: int, page: int) -> str:
    normalized = query.strip()
    if not normalized:
        raise ToolError("FOFA query 不能为空")
    if len(normalized) > 4096:
        raise ToolError("FOFA query 不能超过 4096 个字符")
    if isinstance(size, bool) or not 1 <= size <= 10000:
        raise ToolError("FOFA size 必须在 1 到 10000 之间")
    if isinstance(page, bool) or page <= 0:
        raise ToolError("FOFA page 必须是正整数")
    return normalized


async def _get(path: str, params: dict[str, str | int]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=_request_timeout(),
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "GAsecAgent/0.1"},
        ) as client:
            response = await client.get(f"{_api_base_url()}/{path}", params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"FOFA API HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        # httpx 异常可能附带包含 query 参数的请求 URL，不能把 Key 回传给模型。
        raise ToolError(f"FOFA API 请求失败：{type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise ToolError("FOFA API 返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise ToolError("FOFA API 返回格式不是 JSON object")
    if payload.get("error"):
        message = payload.get("errmsg") or payload.get("message") or "未知错误"
        raise ToolError(f"FOFA API 错误：{message}")
    return payload


def _format_search_result(
    payload: dict[str, Any], query: str, field_names: list[str]
) -> dict[str, Any]:
    raw_results = payload.get("results", [])
    results: list[Any] = []
    if isinstance(raw_results, list):
        for item in raw_results:
            if isinstance(item, list):
                results.append(dict(zip(field_names, item)))
            else:
                results.append(item)
    return {
        "status": "completed",
        "query": payload.get("query", query),
        "fields": field_names,
        "total": payload.get("size", len(results)),
        "returned": len(results),
        "page": payload.get("page", 1),
        "mode": payload.get("mode"),
        "consumed_fpoint": payload.get("consumed_fpoint"),
        "required_fpoints": payload.get("required_fpoints"),
        "results": results,
    }


async def search_fofa(
    *,
    query: str,
    fields: str = DEFAULT_FIELDS,
    size: int = 100,
    page: int = 1,
    full: bool = False,
) -> dict[str, Any]:
    """调用 FOFA search/all API，供 MCP Tool 与测试复用。"""

    normalized_query = _validate_search(query, size, page)
    normalized_fields, field_names = _normalize_fields(fields)
    payload = await _get(
        "search/all",
        {
            "key": _api_key(),
            "qbase64": base64.b64encode(normalized_query.encode("utf-8")).decode(),
            "fields": normalized_fields,
            "size": size,
            "page": page,
            "full": str(full).lower(),
        },
    )
    return _format_search_result(payload, normalized_query, field_names)


@mcp.tool()
async def fofa_search(
    query: str,
    fields: str = DEFAULT_FIELDS,
    size: int = 100,
    page: int = 1,
    full: bool = False,
) -> dict[str, Any]:
    """使用原始 FOFA 查询语法检索授权范围内的资产情报。"""

    return await search_fofa(
        query=query,
        fields=fields,
        size=size,
        page=page,
        full=full,
    )


def build_asset_query(
    *,
    domain: str = "",
    ip: str = "",
    port: str = "",
    host: str = "",
    body: str = "",
    icon_hash: str = "",
    icp: str = "",
    status_code: str = "",
) -> str:
    """按参考 FOFA 工具字段构造查询语句。"""

    values = {
        "domain": domain,
        "ip": ip,
        "port": port,
        "host": host,
        "body": body,
        "icon_hash": icon_hash,
        "icp": icp,
    }
    parts = [
        f"{name}={json.dumps(value.strip(), ensure_ascii=False)}"
        for name, value in values.items()
        if value.strip()
    ]
    normalized_status = status_code.strip()
    if normalized_status:
        if not normalized_status.isdigit() or not 100 <= int(normalized_status) <= 599:
            raise ToolError("status_code 必须是 100 到 599 的 HTTP 状态码")
        parts.append(f"status_code={normalized_status}")
    if not parts:
        raise ToolError("至少提供一个 FOFA 资产筛选条件")
    return " && ".join(parts)


@mcp.tool()
async def get_alerts(
    domain: str = "",
    ip: str = "",
    port: str = "",
    host: str = "",
    body: str = "",
    icon_hash: str = "",
    icp: str = "",
    status_code: str = "",
    size: int = 100,
) -> dict[str, Any]:
    """按域名、IP、端口等字段组合查询 FOFA 资产，兼容原参考工具语义。"""

    query = build_asset_query(
        domain=domain,
        ip=ip,
        port=port,
        host=host,
        body=body,
        icon_hash=icon_hash,
        icp=icp,
        status_code=status_code,
    )
    return await search_fofa(query=query, fields=DEFAULT_FIELDS, size=size)


if __name__ == "__main__":
    mcp.run(transport="stdio")
