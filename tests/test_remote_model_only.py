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
