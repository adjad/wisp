"""MCP client — capabilities from servers the user configures, not from us.

Skills (service/skills) cover procedures and local commands. MCP covers the
other half of extensibility: an ecosystem of existing servers that already
speak to Notion, Linear, GitHub, Postgres, a home-automation bridge. Supporting
the protocol means Wisp gains those integrations without a single one of them
being written here.

Configured at ~/.moe/mcp.json:

    {
      "servers": {
        "notion": {
          "command": "npx",
          "args": ["-y", "@notionhq/notion-mcp-server"],
          "env": {"NOTION_TOKEN": "secret_..."},
          "enabled": true
        }
      }
    }

Each server's tools are mirrored into the normal registry as
`mcp_<server>_<tool>` and gated by the policy engine like everything else.

**stdio transport only.** Every MCP server is a local subprocess this machine
owns. There is no HTTP/SSE transport here on purpose — a remote MCP server
would mean the contents of the user's requests leaving the device to a third
party, which is the one thing Wisp exists to avoid. If a user wants a remote
service, they run its official local server, which keeps the credential and
the traffic decision explicitly theirs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from service.paths import MOE_DIR
from service.tools.registry import REGISTRY, Tool

CONFIG_PATH = MOE_DIR / "mcp.json"

# A server that hasn't answered `initialize` in this long is treated as broken
# rather than retried forever — a wrong command in mcp.json shouldn't leave a
# zombie subprocess and a hanging startup task.
_HANDSHAKE_TIMEOUT_S = 20.0
_CALL_TIMEOUT_S = 90.0
_MAX_RESULT_CHARS = 8000

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    """One stdio MCP subprocess and the JSON-RPC conversation with it."""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []
        self.error: str = ""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> bool:
        command = str(self.config.get("command") or "")
        if not command:
            self.error = "no command configured"
            return False
        resolved = shutil.which(command)
        if not resolved:
            self.error = f"{command} isn't installed or isn't on PATH"
            return False

        args = [str(a) for a in (self.config.get("args") or [])]
        env = {**os.environ, **{str(k): str(v)
                                for k, v in (self.config.get("env") or {}).items()}}
        try:
            self.proc = await asyncio.create_subprocess_exec(
                resolved, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:  # noqa: BLE001
            self.error = f"couldn't start: {e}"
            return False

        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._handshake(), _HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.error = "timed out during the MCP handshake"
            await self.stop()
            return False
        except Exception as e:  # noqa: BLE001
            self.error = f"handshake failed: {e}"
            await self.stop()
            return False
        self.error = ""
        return True

    async def _handshake(self) -> None:
        await self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "wisp", "version": "1.0"},
        })
        await self._notify("notifications/initialized", {})
        listed = await self._call("tools/list", {})
        self.tools = [t for t in (listed.get("tools") or []) if t.get("name")]

    async def _read_loop(self) -> None:
        """One JSON-RPC message per line off stdout, resolving whoever asked.

        A server that dies mid-conversation must fail its in-flight callers
        rather than leaving them awaiting a future nothing will ever resolve —
        that's what the finally block is for.
        """
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001 — servers log non-JSON to stdout
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue          # a notification from the server
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(RuntimeError(
                            err.get("message") if isinstance(err, dict) else str(err)))
                    else:
                        fut.set_result(msg.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"the {self.name} MCP server stopped"))
            self._pending.clear()

    async def _send(self, payload: dict) -> None:
        if not (self.proc and self.proc.stdin):
            raise RuntimeError(f"the {self.name} MCP server isn't running")
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self.proc.stdin.drain()

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _call(self, method: str, params: dict, timeout: float = _HANDSHAKE_TIMEOUT_S) -> dict:
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": mid,
                              "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def call_tool(self, tool_name: str, args: dict) -> str:
        result = await self._call("tools/call",
                                  {"name": tool_name, "arguments": args},
                                  timeout=_CALL_TIMEOUT_S)
        return _render_content(result)

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None


def _render_content(result: dict) -> str:
    """Flatten an MCP tool result into the plain string the agent loop expects."""
    parts: list[str] = []
    for block in (result.get("content") or []):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(str(res.get("text") or res.get("uri") or ""))
        else:
            parts.append(f"[{kind} content]")
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        text = json.dumps(result)[:_MAX_RESULT_CHARS] if result else "(no result)"
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n…[truncated]"
    if result.get("isError"):
        return f"(the MCP tool reported an error)\n{text}"
    return text


class MCPManager:
    def __init__(self) -> None:
        self.servers: dict[str, MCPServer] = {}
        self._registered: set[str] = set()

    def _read_config(self) -> dict:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text())
                servers = data.get("servers")
                if isinstance(servers, dict):
                    return servers
        except Exception:  # noqa: BLE001 — a malformed file disables MCP, nothing more
            pass
        return {}

    async def start(self) -> dict:
        """Launch every enabled server and mirror its tools into the registry."""
        await self.stop()
        config = self._read_config()
        for name, server_config in config.items():
            if not isinstance(server_config, dict) or not server_config.get("enabled", True):
                continue
            server = MCPServer(name, server_config)
            self.servers[name] = server
            try:
                if await server.start():
                    self._register(server)
            except Exception as e:  # noqa: BLE001 — one bad server can't stop the rest
                server.error = str(e)
        return self.status()

    def _register(self, server: MCPServer) -> None:
        for spec in server.tools:
            local = f"mcp_{server.name}_{spec['name']}".lower()
            local = "".join(c if c.isalnum() or c == "_" else "_" for c in local)[:64]
            if local in REGISTRY and local not in self._registered:
                continue                # never shadow a built-in
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
            description = (str(spec.get("description") or spec["name"])
                           + f" (via the '{server.name}' MCP server)")
            REGISTRY[local] = Tool(
                name=local, description=description, parameters=schema,
                category=_category_for(spec),
                func=_make_caller(server, spec["name"]),
            )
            self._registered.add(local)

    async def stop(self) -> None:
        for name in self._registered:
            REGISTRY.pop(name, None)
        self._registered.clear()
        for server in self.servers.values():
            await server.stop()
        self.servers.clear()

    async def reload(self) -> dict:
        return await self.start()

    def status(self) -> dict:
        return {
            "configured": bool(self._read_config()),
            "config_path": str(CONFIG_PATH),
            "servers": [
                {"name": s.name, "running": s.running, "error": s.error,
                 "tools": [t.get("name") for t in s.tools]}
                for s in self.servers.values()
            ],
        }


def _category_for(spec: dict) -> str:
    """Map an MCP tool onto a safety category.

    MCP's `readOnlyHint` is an ANNOTATION from the server — a claim, not a
    guarantee — so it can only ever downgrade to `mcp_read` (allow-tier reads,
    same as web_fetch or reading the inbox). Everything else, and anything
    unannotated, is confirm-tier. A server we didn't write doesn't get to
    self-certify that its `delete_page` tool is harmless.
    """
    hints = spec.get("annotations") or {}
    if isinstance(hints, dict) and hints.get("readOnlyHint") is True:
        return "mcp_read"
    return "mcp_action"


def _make_caller(server: MCPServer, remote_name: str):
    async def call(**args) -> str:
        if not server.running:
            return (f"(the '{server.name}' MCP server isn't running"
                    + (f": {server.error}" if server.error else "") + ")")
        try:
            return await server.call_tool(remote_name, args)
        except asyncio.TimeoutError:
            return f"(the '{server.name}' MCP server timed out running {remote_name})"
        except Exception as e:  # noqa: BLE001
            return f"(error from the '{server.name}' MCP server: {e})"
    return call


manager = MCPManager()
