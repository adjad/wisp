"""Workspace MCP setup: no subprocess, no network, scratch ~/.moe only."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-gws-")
os.environ["WISP_HOME"] = _scratch.name

from service import google_workspace as G


class _FakeServer:
    running = True
    tools = [{"name": "start_google_auth", "inputSchema": {"properties": {
        "service_name": {}, "user_google_email": {}}}}]

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.reply


class GoogleWorkspace(unittest.TestCase):
    def setUp(self):
        self.cfg = Path(_scratch.name) / "mcp.json"
        self.cfg.unlink(missing_ok=True)
        self.enterContext(patch.object(G, "CONFIG_PATH", self.cfg))
        self.enterContext(patch.object(G, "find_uvx", return_value="/x/uvx"))
        self.warm = self.enterContext(patch.object(G, "_warm", new=AsyncMock()))
        G._prepare.update(state="idle", detail="")

    def test_enable_merges_with_existing_servers_privately(self):
        self.cfg.write_text(json.dumps({"servers": {"notion": {"command": "npx"}}}))
        out = asyncio.run(G.enable("id.apps", "sec", "me@gmail.com", "bogus"))
        self.assertTrue(out["ok"])
        servers = json.loads(self.cfg.read_text())["servers"]
        self.assertEqual(set(servers), {"notion", "google_workspace"})
        entry = servers["google_workspace"]
        self.assertEqual(entry["command"], "/x/uvx")
        self.assertEqual(entry["args"], ["workspace-mcp", "--tool-tier", "core"])
        self.assertEqual(entry["env"]["GOOGLE_OAUTH_CLIENT_SECRET"], "sec")
        self.assertEqual(os.stat(self.cfg).st_mode & 0o777, 0o600)

    def test_reenable_without_secret_keeps_saved_secret_and_status_hides_it(self):
        asyncio.run(G.enable("id", "S3cr3t-value", "a@b.c"))
        asyncio.run(G.enable("id2", "", "a@b.c"))
        env = json.loads(self.cfg.read_text())["servers"]["google_workspace"]["env"]
        self.assertEqual((env["GOOGLE_OAUTH_CLIENT_ID"], env["GOOGLE_OAUTH_CLIENT_SECRET"]),
                         ("id2", "S3cr3t-value"))
        with patch.object(G.manager, "status", return_value={"servers": []}):
            st = G.status()
        self.assertTrue(st["has_secret"])
        self.assertNotIn("S3cr3t-value", json.dumps(st))

    def test_enable_needs_uv_and_a_client_id(self):
        with patch.object(G, "find_uvx", return_value=""):
            self.assertFalse(asyncio.run(G.enable("id", "", ""))["ok"])
        self.assertFalse(asyncio.run(G.enable("  ", "", ""))["ok"])

    def test_disable_removes_only_its_entry(self):
        self.cfg.write_text(json.dumps({"servers": {"notion": {}, "google_workspace": {}}}))
        with patch.object(G.manager, "reload", new=AsyncMock()):
            asyncio.run(G.disable())
        self.assertEqual(set(json.loads(self.cfg.read_text())["servers"]), {"notion"})

    def test_signin_maps_arguments_and_extracts_the_google_link(self):
        server = _FakeServer("Open https://accounts.google.com/o/oauth2/auth?x=1&y=2 to continue")
        with patch.object(G.manager, "servers", {"google_workspace": server}):
            out = asyncio.run(G.signin("me@gmail.com"))
        self.assertEqual(out, {"ok": True,
                               "auth_url": "https://accounts.google.com/o/oauth2/auth?x=1&y=2"})
        self.assertEqual(server.calls[0][1],
                         {"service_name": "gmail", "user_google_email": "me@gmail.com"})

    def test_signin_refuses_non_google_links_and_missing_server(self):
        with patch.object(G.manager, "servers", {"google_workspace": _FakeServer("see http://evil.test/a")}):
            self.assertFalse(asyncio.run(G.signin("me@gmail.com"))["ok"])
        with patch.object(G.manager, "servers", {}):
            self.assertFalse(asyncio.run(G.signin("me@gmail.com"))["ok"])


if __name__ == "__main__":
    unittest.main()


class SavedCredentials(unittest.TestCase):
    def test_saved_credentials_come_from_the_workspace_entry(self):
        cfg = Path(_scratch.name) / "mcp-saved.json"
        cfg.write_text(json.dumps({"servers": {"google_workspace": {"env": {
            "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"}}}}))
        with patch.object(G, "CONFIG_PATH", cfg):
            self.assertEqual(G.saved_credentials(), ("cid", "csec"))
        with patch.object(G, "CONFIG_PATH", Path(_scratch.name) / "absent.json"):
            self.assertEqual(G.saved_credentials(), ("", ""))
