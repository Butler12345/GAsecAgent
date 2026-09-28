from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from gasecagent.tool_output import normalize_tool_output


def test_normalizes_plain_and_json_strings() -> None:
    assert normalize_tool_output("普通文本") == "普通文本"
    assert '"name": "value"' in normalize_tool_output('{"name":"value"}')
    assert normalize_tool_output("[1, 2]").startswith("[")
    assert normalize_tool_output('["a", "b"]').startswith("[")


def test_normalizes_dict_array_and_text_blocks() -> None:
    assert '"ok": true' in normalize_tool_output({"ok": True})
    assert normalize_tool_output(
        [
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
        ]
    ) == "第一段\n第二段"


def test_normalizes_mcp_content_and_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="文本结果")],
        structuredContent={"items": [1, 2]},
        isError=False,
    )

    rendered = normalize_tool_output(result)

    assert "文本结果" in rendered
    assert '"items"' in rendered
    assert '"isError": false' in rendered


def test_normalizes_error_result() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="工具失败")],
        isError=True,
    )
    rendered = normalize_tool_output(result)
    assert "工具失败" in rendered
    assert '"isError": true' in rendered
