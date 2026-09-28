"""将不同 MCP Tool Result 统一为适合中文 CLI 展示的文本。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from typing import Any


def _json_from_string(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _plain_value(value: Any, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        parsed = _json_from_string(value)
        return value if parsed is value else _plain_value(parsed, seen)

    value_id = id(value)
    if value_id in seen:
        return "<循环引用>"
    seen.add(value_id)
    try:
        if isinstance(value, dict):
            if value.get("type") == "text" and isinstance(value.get("text"), str):
                return value["text"]
            return {str(key): _plain_value(item, seen) for key, item in value.items()}

        if isinstance(value, (list, tuple, set)):
            text_blocks: list[str] = []
            all_text_blocks = bool(value)
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(
                    item.get("text"), str
                ):
                    text_blocks.append(item["text"])
                elif getattr(item, "type", None) == "text" and isinstance(
                    getattr(item, "text", None), str
                ):
                    text_blocks.append(item.text)
                else:
                    all_text_blocks = False
                    break
            if all_text_blocks:
                return "\n".join(text_blocks)
            items = [_plain_value(item, seen) for item in value]
            return items

        block_type = getattr(value, "type", None)
        if block_type == "text" and isinstance(getattr(value, "text", None), str):
            return value.text
        if block_type in {"image", "audio"}:
            data = getattr(value, "data", "")
            return {
                "type": block_type,
                "mimeType": getattr(value, "mimeType", None),
                "data": f"<{len(data)} 个字符>",
            }

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _plain_value(model_dump(by_alias=True, exclude_none=True), seen)
        if is_dataclass(value):
            return _plain_value(asdict(value), seen)

        result_fields: dict[str, Any] = {}
        for attribute, label in (
            ("content", "content"),
            ("structuredContent", "structuredContent"),
            ("structured_content", "structuredContent"),
            ("isError", "isError"),
            ("is_error", "isError"),
        ):
            if hasattr(value, attribute):
                result_fields[label] = _plain_value(getattr(value, attribute), seen)
        if result_fields:
            return result_fields
        return str(value)
    finally:
        seen.discard(value_id)


def normalize_tool_output(value: Any) -> str:
    """兼容 str、JSON、集合、MCP content block 与 structured content。"""

    plain = _plain_value(value, set())
    if isinstance(plain, str):
        return plain
    return json.dumps(plain, ensure_ascii=False, indent=2, default=str)
