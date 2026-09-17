"""
Claude Desktop entry point — stdio transport, no OAuth.

JWT is read from XELTA_JWT_TOKEN in the environment (set via claude_desktop_config.json).
This file is NOT used by the deployed HTTP server (mcp.xelta.ai). See server.py for that.
"""

from mcp.server.fastmcp import FastMCP

from tools.credits import check_balance as _check_balance
from tools.connection_status import xelta_connection_status as _xelta_connection_status
from tools.utils import format_tool_error

mcp = FastMCP("xelta")


@mcp.tool(
    annotations={
        "title": "Xelta Connection Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def xelta_connection_status() -> str:
    """Check whether the Xelta MCP connection is authenticated and working."""
    try:
        return await _xelta_connection_status()
    except Exception as e:
        return format_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Check Xelta Balance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def check_balance() -> str:
    """Check your Xelta credit balance."""
    try:
        data = await _check_balance()
        return f"Your Xelta balance: {data.get('data', 0)} credits"
    except Exception as e:
        return format_tool_error(e)


if __name__ == "__main__":
    mcp.run()
