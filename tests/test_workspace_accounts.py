"""Workspace tools follow the user's per-account choice. Scratch ~/.moe only;
no subprocess, no network."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-wsacct-")
os.environ["WISP_HOME"] = _scratch.name

from service import gmail_source as GS
from service import google_workspace as W

SCHEMA = {"properties": {"user_google_email": {}, "query": {}}}
ME, GUEST = "me@gmail.com", "guest@gmail.com"


class WorkspaceAccounts(unittest.TestCase):
    def setUp(self):
        root = Path(_scratch.name) / self._testMethodName
        root.mkdir()
        self.creds = root / "credentials"
        self.mcp = root / "mcp.json"
        self.enterContext(patch.object(W, "CREDENTIALS_DIR", self.creds))
        self.enterContext(patch.object(W, "CONFIG_PATH", self.mcp))
        self.enterContext(patch.object(GS, "CONFIG_PATH", root / "gmail.json"))
        self.accounts = {}
        self.enterContext(patch.object(GS, "connected_accounts",
                                       lambda cfg=None: {k: dict(v) for k, v in self.accounts.items()}))

    def account(self, email, workspace, token="rt"):
        self.accounts[email] = {"client_id": "cid", "client_secret": "sec", "refresh_token": token,
                                "scopes": [GS.SCOPE], "workspace": workspace}

    def test_route_confines_calls_to_switched_on_accounts(self):
        self.assertIn("no Google account", W.route_arguments(SCHEMA, {"query": "x"}))
        self.account(ME, True)
        self.account(GUEST, False)
        # One account on: a missing or other address goes to it.
        self.assertEqual(W.route_arguments(SCHEMA, {"query": "x"})["user_google_email"], ME)
        self.assertEqual(W.route_arguments(SCHEMA, {"user_google_email": GUEST})["user_google_email"], ME)
        self.account(GUEST, True)
        self.assertEqual(W.route_arguments(SCHEMA, {"user_google_email": "Guest@Gmail.com"})
                         ["user_google_email"], GUEST)
        refusal = W.route_arguments(SCHEMA, {"user_google_email": "erased@gmail.com"})
        self.assertIsInstance(refusal, str)
        self.assertIn(ME, refusal)
        # Tools without an account parameter pass through untouched.
        self.assertEqual(W.route_arguments({"properties": {"q": {}}}, {"q": 1}), {"q": 1})

    def test_sync_writes_private_files_only_for_switched_on_accounts(self):
        self.account(ME, True)
        self.account(GUEST, False)
        self.creds.mkdir(parents=True)
        (self.creds / "stale@gmail.com.json").write_text("{}")
        W.sync_credentials(self.accounts)
        self.assertEqual(sorted(p.name for p in self.creds.glob("*.json")), [ME + ".json"])
        data = json.loads(W.credentials_path(ME).read_text())
        self.assertEqual((data["refresh_token"], data["client_id"], data["token"]), ("rt", "cid", None))
        self.assertEqual(stat.S_IMODE(W.credentials_path(ME).stat().st_mode), 0o600)
        # A file the server extended is kept; a fresh sign-in replaces it.
        W.credentials_path(ME).write_text(json.dumps({"refresh_token": "server-extended"}))
        W.sync_credentials(self.accounts)
        self.assertIn("server-extended", W.credentials_path(ME).read_text())
        self.account(ME, True, token="new")
        W.sync_credentials(self.accounts, refresh=(ME,))
        self.assertEqual(json.loads(W.credentials_path(ME).read_text())["refresh_token"], "new")
        self.account(ME, False)
        W.sync_credentials(self.accounts)
        self.assertFalse(W.credentials_path(ME).exists())

    def test_ensure_env_points_at_wisp_folder_and_drops_fixed_account(self):
        self.mcp.write_text(json.dumps({"servers": {"google_workspace": {
            "env": {"GOOGLE_OAUTH_CLIENT_ID": "cid", "USER_GOOGLE_EMAIL": "erased@gmail.com"},
            "trusted_read_only": ["get_events"]}}}))
        self.assertTrue(W.ensure_env())
        entry = json.loads(self.mcp.read_text())["servers"]["google_workspace"]
        self.assertNotIn("USER_GOOGLE_EMAIL", entry["env"])
        self.assertEqual(entry["env"]["WORKSPACE_MCP_CREDENTIALS_DIR"], str(self.creds))
        self.assertIn("search_drive_files", entry["trusted_read_only"])
        self.assertFalse(W.ensure_env())

    def test_consent_asks_for_read_only_workspace_scopes(self):
        scopes = GS._consent_scopes()
        self.assertEqual(scopes[:2], (GS.SCOPE, GS.CALENDAR_SCOPE))
        self.assertIn(W.FULL_SCOPE, scopes)
        self.assertTrue(all("readonly" in s for s in scopes))

    def test_full_scopes_reported_from_file_or_sign_in(self):
        self.account(ME, True)
        self.assertFalse(W.has_full_scopes(ME, self.accounts[ME]))
        self.accounts[ME]["scopes"].append(W.FULL_SCOPE)
        self.assertTrue(W.has_full_scopes(ME, self.accounts[ME]))


if __name__ == "__main__":
    unittest.main()
