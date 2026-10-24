"""Remote inference with model-only mode: no local-template options, no local residency."""
import asyncio
from unittest.mock import patch

from tests.test_remote_inference_policy import RemoteFixture, policy
from service import config
from service.agent import loop


class RemoteModelOnly(RemoteFixture):
    def test_no_template_kwargs_reach_a_remote_request(self):
        model = policy.remote_only_profile().model_id
        with patch("service.model_only.model_only_enabled", return_value=True):
            self.assertEqual(loop._template_kwargs(model, no_thinking=False), {})
            self.assertEqual(loop._template_kwargs(model, no_thinking=True, think_off=True), {})
        self.assertEqual(config.thinking_kwargs(model, on=True), {})
        self.assertEqual(config.no_thinking_kwargs(model), {})

    def test_brief_reply_skips_the_local_residency_check(self):
        from service.assistant import mail_gists

        class Client:
            async def loaded_models(self):
                raise AssertionError("remote mode has no residency API")

            async def chat(self, model, messages, **kw):
                assert "chat_template_kwargs" not in kw
                return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

        with patch("service.assistant.brief._c", return_value=Client()):
            self.assertEqual(asyncio.run(mail_gists._resident_reply("s", "m", lambda: False)), "ok")
            self.assertIsNone(asyncio.run(mail_gists._resident_reply("s", "m", lambda: True)))

    def test_readiness_accepts_a_models_prefixed_inventory(self):
        import httpx
        from service.inference.omlx_client import OMLXClient
        model = policy.remote_only_profile().model_id
        with patch("service.inference.attributed_transport.CredentialTransport",
                   return_value=httpx.MockTransport(lambda r: httpx.Response(200, json={}))):
            client = OMLXClient()
            async def listed():
                return [f"models/{model}"]
            with patch.object(client, "models", listed):
                asyncio.run(client.ensure_only(model))
            asyncio.run(client.aclose())


class RemoteParity(RemoteFixture):
    def test_off_by_default_keeps_the_remote_limits(self):
        self.assertFalse(policy.remote_parity())
        self.assertFalse(policy.remote_tool_allowed("create_tool"))

    def test_parity_lifts_the_data_limits_only(self):
        import os
        from service import behavior_switch
        self.cfg["remote_parity"] = True
        self.assertTrue(policy.remote_parity())
        self.assertTrue(policy.remote_tool_allowed("create_tool"))
        self.assertFalse(policy.remote_tool_allowed("no_such_tool"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WISP_BEHAVIOR_PHRASER", None)
            self.assertTrue(behavior_switch.phraser_enabled())
        with patch.dict(os.environ, {"WISP_REMOTE_PARITY": "0"}):
            self.assertFalse(policy.remote_parity())
        # The pinned payload contract is untouched.
        with self.assertRaises(Exception):
            policy.remote_only_profile().validate_payload({"model": "x", "messages": [], "stream": False})


class CloudEverything(RemoteFixture):
    """Settings ▸ Cloud inference ▸ Everything: config alone (no launch flag) runs all of Wisp remotely."""

    def _saved(self, cfg):
        self.cfg = cfg
        policy.remote_only_profile.cache_clear()

    def test_config_opt_in_activates_without_the_launch_flag(self):
        with patch.object(policy, "_FORCED_MODE", None):
            self._saved({"inference": {"remote_only": dict(self.profile)}})
            self.assertIsNone(policy.remote_only_profile())         # a stray profile alone does nothing
            self._saved({"remote_parity": True, "inference": {"remote_only": dict(self.profile)}})
            self.assertIsNotNone(policy.remote_only_profile())
            self._saved({"remote_parity": True,
                         "inference": {"remote_only": {**self.profile, "enabled": False}}})
            self.assertIsNone(policy.remote_only_profile())         # disconnected

    def test_set_and_disable_round_trip(self):
        saved = {}
        def save(update, **_):
            def merge(a, b):
                for k, v in b.items():
                    a[k] = merge(a.get(k, {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
                return a
            merge(saved, update)
        endpoint = {"enabled": True, "provider": "openai-compatible",
                    "base_url": "https://generativelanguage.googleapis.com", "api_prefix": "/v1beta/openai",
                    "credential_ref": "keychain:cloud", "readiness_timeout": 10}
        with patch.object(config, "_save_overlay", side_effect=save), \
             patch.object(config, "models_config", side_effect=lambda: saved):
            config.set_cloud_everything(endpoint, "gemma-4-e4b-it", 128000)
            self.assertTrue(saved["remote_parity"])
            profile = saved["inference"]["remote_only"]
            self.assertEqual((profile["base_url"], profile["api_prefix"], profile["model_id"], profile["credential_ref"]),
                             (endpoint["base_url"], "/v1beta/openai", "gemma-4-e4b-it", "keychain:cloud"))
            self.assertTrue(config.cloud_everything_enabled())
            self.assertEqual(config.cloud_provider_settings()["roles"], ["everything"])
            config.disable_cloud_provider()
            self.assertFalse(saved["remote_parity"])
            self.assertFalse(config.cloud_everything_enabled())
