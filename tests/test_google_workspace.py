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

_REAL_WARM = G._warm
# The exact launch pin: the server version, its dependency tree as of a fixed
# date (the server itself one later cutoff), and the Python it was resolved for.
PINNED = ["--exclude-newer", "2026-10-12T00:00:00Z", "--exclude-newer-package", "workspace-mcp=2026-10-24T00:00:00Z",
          "--python", "3.12", "--from", "workspace-mcp==2.2.0", "workspace-mcp"]


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
        self.assertEqual(entry["args"], [*PINNED, "--tool-tier", "core"])
        self.assertEqual(entry["env_clear_prefixes"], ["UV_"])
        self.assertEqual(entry["wisp_client_source"], "user")
        self.assertEqual(entry["env"]["GOOGLE_OAUTH_CLIENT_SECRET"], "sec")
        self.assertEqual(os.stat(self.cfg).st_mode & 0o777, 0o600)

    def test_reenable_without_secret_keeps_saved_secret_and_status_hides_it(self):
        asyncio.run(G.enable("id", "S3cr3t-value", "a@b.c"))
        asyncio.run(G.enable("id", "", "a@b.c"))
        env = json.loads(self.cfg.read_text())["servers"]["google_workspace"]["env"]
        self.assertEqual((env["GOOGLE_OAUTH_CLIENT_ID"], env["GOOGLE_OAUTH_CLIENT_SECRET"]),
                         ("id", "S3cr3t-value"))
        with patch.object(G.manager, "status", return_value={"servers": []}):
            st = G.status()
        self.assertTrue(st["has_secret"])
        self.assertNotIn("S3cr3t-value", json.dumps(st))

    def test_changed_client_without_matching_secret_preserves_valid_configuration(self):
        asyncio.run(G.enable("client-A", "secret-A", "a@b.c"))
        before = self.cfg.read_bytes()
        result = asyncio.run(G.enable("client-B", "", "a@b.c"))
        self.assertFalse(result["ok"])
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(G.saved_credentials(), ("client-A", "secret-A"))
        self.assertTrue(asyncio.run(G.enable("client-B", "secret-B", "a@b.c"))["ok"])
        self.assertEqual(G.saved_credentials(), ("client-B", "secret-B"))

    def test_existing_temporary_file_is_private_before_credentials_are_written(self):
        temp = self.cfg.with_suffix(".tmp")
        temp.write_text("old")
        temp.chmod(0o644)
        G._write_file({"servers": {"existing": {"env": {"SECRET": "synthetic"}}}})
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.cfg.read_text())["servers"]["existing"]["env"]["SECRET"], "synthetic")

    def test_symlink_temporary_file_is_refused_without_touching_target(self):
        target = self.cfg.with_name("unrelated")
        target.write_text("untouched")
        temp = self.cfg.with_suffix(".tmp")
        temp.unlink(missing_ok=True)
        temp.symlink_to(target)
        try:
            with self.assertRaises(OSError):
                G._write_file({"secret": "must not be written"})
            self.assertEqual(target.read_text(), "untouched")
        finally:
            temp.unlink(missing_ok=True)

    def test_write_failure_retains_existing_configuration_and_cleans_staging(self):
        self.cfg.write_text('{"servers":{"unrelated":{}}}')
        before = self.cfg.read_bytes()
        with patch.object(G.json, "dump", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                G._write_file({"secret": "synthetic"})
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertFalse(self.cfg.with_suffix(".tmp").exists())

    def test_warm_downloads_the_pinned_server_only(self):
        calls = []

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _Proc()
        with patch.object(G.asyncio, "create_subprocess_exec", fake_exec), \
                patch.object(G.manager, "reload", new=AsyncMock()):
            asyncio.run(_REAL_WARM("/x/uvx"))
        self.assertEqual(calls, [("/x/uvx", *PINNED, "--help")])
        self.assertEqual(G._prepare["state"], "ready")

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



class SavedCredentials(unittest.TestCase):
    def test_saved_credentials_come_from_the_workspace_entry(self):
        cfg = Path(_scratch.name) / "mcp-saved.json"
        cfg.write_text(json.dumps({"servers": {"google_workspace": {"env": {
            "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"}}}}))
        with patch.object(G, "CONFIG_PATH", cfg):
            self.assertEqual(G.saved_credentials(), ("cid", "csec"))
        with patch.object(G, "CONFIG_PATH", Path(_scratch.name) / "absent.json"):
            self.assertEqual(G.saved_credentials(), ("", ""))


class WorkspaceToolsAskBeforeWriting(unittest.TestCase):
    """Workspace is now on for every connected account, so this pins the rule
    that makes that safe: only the pre-listed plain lookups run unprompted;
    every other Workspace tool, including every one that sends, creates,
    edits or deletes, asks first (and is refused in view-only mode)."""

    WRITE_VERBS = ("send", "draft", "create", "update", "modify", "delete", "remove", "trash", "manage",
                   "share", "move", "copy", "import", "upload", "insert", "append", "write", "edit",
                   "reply", "add", "set", "batch_update", "clear", "complete", "respond")
    # Representative write tools of workspace-mcp 2.2.0 (names as the server
    # registers them). Their annotations are the server's claim, so each is
    # tried both unannotated and falsely labelled read-only.
    WRITE_TOOLS = ("send_gmail_message", "draft_gmail_message", "modify_gmail_message_labels",
                   "create_event", "modify_event", "delete_event", "create_drive_file",
                   "share_drive_file", "create_doc", "modify_doc_text", "modify_sheet_values",
                   "create_presentation", "send_message", "create_task", "update_task",
                   "delete_task", "manage_contact", "create_form")

    def setUp(self):
        from service.safety import policy
        self.policy = policy
        self.enterContext(patch.object(policy, "_FULL_ACCESS", False))
        self.enterContext(patch.object(policy, "_READ_ONLY", False))
        # The trust list exactly as Wisp writes the server entry.
        self.config = {"trusted_read_only": list(G.TRUSTED_READ_ONLY)}

    def tier(self, spec):
        from service.mcp import _category_for
        category = _category_for(spec, self.config)
        return category, self.policy.decide(category, {}, "mcp_google_workspace_" + spec["name"]).tier

    def test_no_pre_trusted_lookup_is_a_write(self):
        for name in G.TRUSTED_READ_ONLY:
            words = name.split("_")
            with self.subTest(name=name):
                self.assertTrue(any(word in ("get", "list", "search", "read") for word in words[:1]), name)
                self.assertFalse(any(verb in words or name.startswith(verb + "_") for verb in self.WRITE_VERBS), name)
        self.assertFalse(set(self.WRITE_TOOLS) & set(G.TRUSTED_READ_ONLY))

    def test_every_write_tool_asks_and_view_only_refuses_it(self):
        for name in self.WRITE_TOOLS:
            for annotations in (None, {"readOnlyHint": True}, {"readOnlyHint": False, "destructiveHint": True}):
                spec = {"name": name, **({"annotations": annotations} if annotations else {})}
                with self.subTest(name=name, annotations=annotations):
                    category, tier = self.tier(spec)
                    self.assertEqual(category, "mcp_action")
                    self.assertEqual(tier, self.policy.Tier.CONFIRM)
                    with patch.object(self.policy, "_READ_ONLY", True):
                        self.assertEqual(self.policy.decide(category, {}, name).tier, self.policy.Tier.DENY)

    def test_a_trusted_lookup_still_asks_unless_the_server_says_it_is_read_only(self):
        name = G.TRUSTED_READ_ONLY[0]
        self.assertEqual(self.tier({"name": name, "annotations": {"readOnlyHint": True}}),
                         ("mcp_read", self.policy.Tier.ALLOW))
        for annotations in (None, {"readOnlyHint": False},
                            {"readOnlyHint": True, "destructiveHint": True}):
            spec = {"name": name, **({"annotations": annotations} if annotations else {})}
            with self.subTest(annotations=annotations):
                self.assertEqual(self.tier(spec)[1], self.policy.Tier.CONFIRM)

    def test_enable_writes_exactly_the_reviewed_trust_list(self):
        cfg = Path(_scratch.name) / "trust-mcp.json"
        cfg.unlink(missing_ok=True)
        with patch.object(G, "CONFIG_PATH", cfg), patch.object(G, "find_uvx", return_value="/x/uvx"), \
             patch.object(G, "_warm", new=AsyncMock()):
            self.assertTrue(asyncio.run(G.enable("id", "sec", "a@b.c"))["ok"])
        entry = json.loads(cfg.read_text())["servers"]["google_workspace"]
        self.assertEqual(entry["trusted_read_only"], G.TRUSTED_READ_ONLY)



class BundledUV(unittest.TestCase):
    """The uvx Wisp ships comes first, and runs with Wisp's own uv dirs."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_scratch.name))
        self.bundled = self.root / "Wisp.app/Contents/Resources/bin/uvx"
        self.bundled.parent.mkdir(parents=True)
        self.path_uvx = self.root / "path-bin/uvx"
        self.path_uvx.parent.mkdir()
        self.brew = self.root / "brew/uvx"
        self.brew.parent.mkdir()
        for tool in (self.bundled, self.path_uvx, self.brew):
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)

    def test_bundle_location_is_derived_only_inside_an_app_bundle(self):
        # In a source checkout the backend is not under Contents/Resources/backend.
        self.assertIsNone(G.BUNDLED_UVX)
        import importlib.util
        backend = self.root / "Wisp.app/Contents/Resources/backend/service"
        backend.mkdir(parents=True)
        source = Path(G.__file__).read_text()
        (backend / "google_workspace.py").write_text(source)
        spec = importlib.util.spec_from_file_location("bundled_google_workspace", backend / "google_workspace.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.BUNDLED_UVX, self.bundled.resolve())

    def test_find_uvx_prefers_bundled_then_path_then_install_places(self):
        with patch.object(G, "BUNDLED_UVX", self.bundled), \
             patch.object(G.shutil, "which", return_value=str(self.path_uvx)), \
             patch.object(G, "_UVX_FALLBACKS", (str(self.brew),)):
            self.assertEqual(G.find_uvx(), str(self.bundled))
            self.bundled.chmod(0o644)              # not executable: skipped
            self.assertEqual(G.find_uvx(), str(self.path_uvx))
        with patch.object(G, "BUNDLED_UVX", None), patch.object(G.shutil, "which", return_value=None), \
             patch.object(G, "_UVX_FALLBACKS", (str(self.root / "absent/uvx"), str(self.brew))):
            self.assertEqual(G.find_uvx(), str(self.brew))
        with patch.object(G, "BUNDLED_UVX", None), patch.object(G.shutil, "which", return_value=None), \
             patch.object(G, "_UVX_FALLBACKS", ()):
            self.assertEqual(G.find_uvx(), "")

    def test_uv_state_lives_under_wisp_home(self):
        env = G.uv_env()
        self.assertEqual(set(env), {"UV_CACHE_DIR", "UV_TOOL_DIR", "UV_PYTHON_INSTALL_DIR", "UV_NO_CONFIG"})
        self.assertEqual(env.pop("UV_NO_CONFIG"), "1")      # no uv.toml of the user's
        home = Path(G.MOE_DIR).resolve()          # WISP_HOME, never the user's own uv dirs
        for value in env.values():
            self.assertTrue(Path(value).resolve().is_relative_to(home), value)
            self.assertNotIn(str(Path.home() / ".cache"), value)
            self.assertNotIn(str(Path.home() / ".local"), value)

    def test_enable_and_warm_pass_wisps_uv_dirs(self):
        cfg = self.root / "mcp.json"
        with patch.object(G, "CONFIG_PATH", cfg), patch.object(G, "find_uvx", return_value=str(self.bundled)), \
             patch.object(G, "_warm", new=AsyncMock()):
            self.assertTrue(asyncio.run(G.enable("id", "sec", "a@b.c"))["ok"])
        entry = json.loads(cfg.read_text())["servers"]["google_workspace"]
        self.assertEqual(entry["command"], str(self.bundled))
        for key, value in G.uv_env().items():
            self.assertEqual(entry["env"][key], value)
        seen = {}
        class Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        async def spawn(*args, **kwargs):
            seen.update(kwargs.get("env") or {})
            return Proc()
        with patch.object(G.asyncio, "create_subprocess_exec", spawn), \
             patch.object(G.manager, "reload", AsyncMock()):
            asyncio.run(_REAL_WARM(str(self.bundled)))
        for key, value in G.uv_env().items():
            self.assertEqual(seen[key], value)

    def test_existing_entry_moves_to_the_bundled_uvx_and_wisps_dirs(self):
        cfg = self.root / "mcp.json"
        cfg.write_text(json.dumps({"servers": {"google_workspace": {
            "command": "/opt/homebrew/bin/uvx", "args": ["--from", G.PACKAGE, "workspace-mcp"],
            "env": {"GOOGLE_OAUTH_CLIENT_ID": "id"}}, "other": {"command": "npx", "env": {}}}}))
        with patch.object(G, "CONFIG_PATH", cfg), patch.object(G, "BUNDLED_UVX", self.bundled):
            self.assertTrue(G.ensure_env())
            G.ensure_env()                        # the trust list settles into sorted order once
            self.assertFalse(G.ensure_env())
        servers = json.loads(cfg.read_text())["servers"]
        self.assertEqual(servers["google_workspace"]["command"], str(self.bundled))
        self.assertEqual(servers["google_workspace"]["env"]["UV_TOOL_DIR"], G.uv_env()["UV_TOOL_DIR"])
        self.assertEqual(servers["other"], {"command": "npx", "env": {}})
        # A hand-written, non-uvx entry is never repointed.
        cfg.write_text(json.dumps({"servers": {"google_workspace": {"command": "/usr/local/bin/workspace-mcp"}}}))
        with patch.object(G, "CONFIG_PATH", cfg), patch.object(G, "BUNDLED_UVX", self.bundled):
            G.ensure_env()
        entry = json.loads(cfg.read_text())["servers"]["google_workspace"]
        self.assertEqual(entry["command"], "/usr/local/bin/workspace-mcp")
        self.assertNotIn("UV_TOOL_DIR", entry.get("env", {}))


    def test_inherited_uv_settings_never_reach_uvx(self):
        inherited = {"PATH": "/usr/bin", "HOME": "/Users/someone", "UV_INDEX_URL": "https://mirror.invalid/simple",
                     "UV_CACHE_DIR": "/Users/someone/.cache/uv", "UV_PYTHON": "3.9", "UVICORN_X": "kept"}
        env = G.uv_process_env(inherited)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/Users/someone", "UVICORN_X": "kept", **G.uv_env()})
        # The warm-up download gets the same environment.
        seen = {}
        class Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        async def spawn(*args, **kwargs):
            seen.update(kwargs["env"])
            return Proc()
        with patch.dict(os.environ, {"UV_INDEX_URL": "https://mirror.invalid/simple"}), \
             patch.object(G.asyncio, "create_subprocess_exec", spawn), patch.object(G.manager, "reload", AsyncMock()):
            asyncio.run(_REAL_WARM(str(self.bundled)))
        self.assertNotIn("UV_INDEX_URL", seen)
        self.assertEqual(seen["UV_NO_CONFIG"], "1")

    def test_the_mcp_client_drops_inherited_prefixes_for_the_server(self):
        from service import mcp
        with patch.dict(os.environ, {"UV_INDEX_URL": "https://mirror.invalid/simple", "KEEP_ME": "1"}):
            cleared = mcp._inherited_env({"env_clear_prefixes": ["UV_"]})
            self.assertNotIn("UV_INDEX_URL", cleared)
            self.assertEqual(cleared["KEEP_ME"], "1")
            # Any other server, or a malformed list, inherits everything.
            for config in ({}, {"env_clear_prefixes": "UV_"}, {"env_clear_prefixes": [""]},
                           {"env_clear_prefixes": [1]}):
                self.assertIn("UV_INDEX_URL", mcp._inherited_env(config))

    def test_launch_args_carry_the_dependency_pin(self):
        self.assertEqual(list(G._SERVER_ARGS), PINNED)
        self.assertEqual(G._pinned_args(["--tool-tier", "extended"]), [*PINNED, "--tool-tier", "extended"])
        # The cutoffs are fixed dates: the dependency one at least two weeks
        # before the server release it was set with (2026-10-23).
        from datetime import datetime
        cutoff = datetime.fromisoformat(G.DEPENDENCY_CUTOFF.replace("Z", "+00:00"))
        released = datetime.fromisoformat("2026-10-23T22:17:31+00:00")
        self.assertGreaterEqual((released - cutoff).days, 11)
        self.assertLessEqual(cutoff, datetime.fromisoformat("2026-10-12T00:00:00+00:00"))
        self.assertTrue(G.PACKAGE_CUTOFF.startswith("workspace-mcp="))


class WorkspaceClientSource(unittest.TestCase):
    """The Workspace entry remembers which client it was set up with."""

    def setUp(self):
        self.cfg = Path(tempfile.mkdtemp(dir=_scratch.name)) / "mcp.json"
        self.enterContext(patch.object(G, "CONFIG_PATH", self.cfg))
        self.enterContext(patch.object(G, "find_uvx", return_value="/x/uvx"))
        self.enterContext(patch.object(G, "_warm", new=AsyncMock()))

    def test_a_built_in_setup_is_never_the_users_own_client_even_after_rotation(self):
        from service import gmail_source
        with patch.object(gmail_source, "builtin_client", return_value=("old-builtin.apps", "FAKE-old")):
            asyncio.run(G.enable("old-builtin.apps", "FAKE-old", "a@b.c", client_source="builtin"))
        self.assertEqual(json.loads(self.cfg.read_text())["servers"]["google_workspace"]["wisp_client_source"],
                         "builtin")
        # A later build ships a different built-in client: the old id is still not the user's.
        with patch.object(gmail_source, "builtin_client", return_value=("new-builtin.apps", "FAKE-new")):
            self.assertEqual(G.saved_credentials(), ("", ""))
            status = G.status()
            self.assertEqual((status["client_id"], status["client_source"]), ("", "builtin"))
            with patch.object(gmail_source, "load_config", return_value={}):
                self.assertEqual(gmail_source.resolve_client(), ("new-builtin.apps", "FAKE-new", "builtin"))

    def test_a_user_setup_is_the_users_client(self):
        asyncio.run(G.enable("mine.apps", "FAKE-mine", "a@b.c"))
        self.assertEqual(G.saved_credentials(), ("mine.apps", "FAKE-mine"))
        self.assertEqual(G.status()["client_source"], "user")

    def test_an_untagged_older_entry_is_judged_by_the_shipped_id(self):
        from service import gmail_source
        self.cfg.write_text(json.dumps({"servers": {"google_workspace": {"command": "/x/uvx", "env": {
            "GOOGLE_OAUTH_CLIENT_ID": "shipped.apps", "GOOGLE_OAUTH_CLIENT_SECRET": "FAKE"}}}}))
        with patch.object(gmail_source, "builtin_client", return_value=("shipped.apps", "FAKE")):
            self.assertEqual(G.saved_credentials(), ("", ""))
        with patch.object(gmail_source, "builtin_client", return_value=("", "")):
            self.assertEqual(G.saved_credentials(), ("shipped.apps", "FAKE"))


if __name__ == "__main__":
    unittest.main()
