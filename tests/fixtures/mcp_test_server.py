"""仅用于测试真实 stdio MCP 协议的本地 Server。"""

from mcp.server.fastmcp import FastMCP


server = FastMCP("GAsecAgent Test Server")


@server.tool()
def echo(text: str) -> dict[str, str]:
    """原样返回输入文本。"""

    return {"echo": text}


@server.tool()
def add(a: int, b: int) -> int:
    """返回两个整数之和。"""

    return a + b


@server.tool()
def fail(message: str) -> str:
    """返回一个带错误标记的 MCP Tool Result。"""

    raise ValueError(message)


if __name__ == "__main__":
    server.run(transport="stdio")
