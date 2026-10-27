"""A server whose tools/list reply is one line over 64 KB must still connect.

Google Workspace's MCP server lists ~87 KB of tool schemas in one JSON-RPC
line; asyncio's default 64 KB StreamReader limit killed the read loop and the
handshake reported "the server stopped".
"""
import asyncio

from service.mcp import MCPServer

# Reads two requests (initialize, tools/list) and answers each with one line.
# Builtins only: the description is doubled up to ~128 KB.
_SERVER = r'''
read line
printf '%s\n' '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{}}}'
read line
read line
d=x; i=0
while [ $i -lt 17 ]; do d="$d$d"; i=$((i+1)); done
printf '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"big","description":"%s","inputSchema":{"type":"object"}}]}}\n' "$d"
while read line; do :; done
'''


def test_tools_list_line_over_64kb_connects():
    async def go():
        server = MCPServer("big", {"command": "sh", "args": ["-c", _SERVER]})
        try:
            assert await server.start(), server.error
            assert [t["name"] for t in server.tools] == ["big"]
            assert len(server.tools[0]["description"]) > 64 * 1024
        finally:
            await server.stop()
    asyncio.run(go())
