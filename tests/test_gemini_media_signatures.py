"""A picture and tool calls in one Gemini API turn (Gemini handles everything).

Synthetic only: a mocked transport, no Keychain, no network. The picture goes as an
image part on the Gemini API's origin, the model's signed tool call is held for the
turn, and the next request carries both the picture and the signature back,
unchanged, through the remote-only payload validator. With the cloud mode off the
same picture is refused before anything is sent.
"""
import json

from tests.test_remote_inference_policy import EndpointConfigurationError, policy
from tests.test_remote_media import FULL, MediaBase, image_url, run
from service.inference.omlx_client import replay_tool_calls, without_replay_reasoning
from service.inference.providers import GEMINI_API_PREFIX, GEMINI_ORIGIN

SIG = "SYNTHETIC-SIGNATURE-" + "S" * 80
TOOL = {"type": "function", "function": {"name": "lookup", "description": "Synthetic lookup.",
                                         "parameters": {"type": "object", "properties": {
                                             "q": {"type": "string"}}}}}


def signed_call_reply():
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
         "extra_content": {"google": {"thought_signature": SIG}}}]}, "finish_reason": "stop"}]}


def answer(text="Done."):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class GeminiPictureTurn(MediaBase):
    profile_overrides = {**FULL, "model_id": "gemini-3.8-flash", "base_url": GEMINI_ORIGIN,
                         "api_prefix": GEMINI_API_PREFIX}

    def ask(self):
        return [{"role": "user", "content": [{"type": "text", "text": "what is this, look it up"},
                                             {"type": "image_url", "image_url": {"url": image_url()}}]}]

    def test_picture_and_signature_go_back_together(self):
        client = self.client([signed_call_reply(), answer()])

        async def go():
            try:
                first = (await client.chat(client.target.model, self.ask(), tools=[TOOL],
                                           max_tokens=3000))["choices"][0]["message"]
                self.assertNotIn(SIG, json.dumps(without_replay_reasoning(first), default=repr))
                calls = replay_tool_calls(client, first, first["tool_calls"])
                row = {"role": "assistant", "content": "", "tool_calls": calls}
                result = {"role": "tool", "tool_call_id": calls[0]["id"], "content": "synthetic result"}
                await client.chat(client.target.model, self.ask() + [row, result], tools=[TOOL],
                                  max_tokens=3000)
            finally:
                await client.aclose()
        run(go())
        self.assertEqual(len(self.bodies), 2)
        first, second = self.bodies
        self.assertEqual(first["messages"][-1]["content"][1]["type"], "image_url")
        self.assertNotIn("extra_content", json.dumps(first))
        picture = [m for m in second["messages"] if isinstance(m.get("content"), list)]
        self.assertEqual(len(picture), 1)
        self.assertEqual(picture[0]["content"][1]["image_url"]["url"], image_url())
        signed = [m for m in second["messages"] if m.get("tool_calls")]
        self.assertEqual(signed[0]["tool_calls"][0]["extra_content"],
                         {"google": {"thought_signature": SIG}})
        self.assertEqual(second.get("reasoning_effort"), "low")

    def test_with_the_cloud_mode_off_the_picture_is_refused_before_sending(self):
        self.use_model("gemini-3.8-flash", parity=False)
        with self.assertRaises(EndpointConfigurationError):
            policy.remote_only_profile().validate_payload(
                {"model": "gemini-3.8-flash", "stream": False, "max_tokens": 100,
                 "messages": self.ask()})


class GeminiVoice(GeminiPictureTurn):
    """Voice Ask's speech-to-text step on the Gemini API origin."""

    def test_the_clip_goes_to_gemini_as_one_wav_part(self):
        from unittest.mock import patch
        from service import voice
        from tests.test_remote_media import b64, wav
        client = self.client([answer("Synthetic words.")])
        clip = wav(2.0)
        with patch("service.tools.imessage_tools._c", return_value=client):
            self.assertEqual(run(voice.transcribe(b64(clip))), "Synthetic words.")
        body = self.bodies[0]
        self.assertEqual(body["model"], "gemini-3.8-flash")
        self.assertEqual(body["messages"][0]["content"][0],
                         {"type": "input_audio", "input_audio": {"data": b64(clip), "format": "wav"}})

    def test_a_clip_longer_than_a_window_is_refused_not_sent(self):
        from unittest.mock import patch
        from service import voice
        from tests.test_remote_media import b64, wav
        client = self.client([])
        with patch("service.tools.imessage_tools._c", return_value=client):
            with self.assertRaisesRegex(ValueError, "too long"):
                run(voice.transcribe(b64(wav(voice.MAX_SECONDS + 5))))
        self.assertEqual(self.bodies, [])

    def test_with_the_cloud_mode_off_voice_sends_nothing(self):
        from unittest.mock import patch
        from service import voice
        from tests.test_remote_media import b64, wav
        client = self.client([])
        self.use_model("gemini-3.8-flash", parity=False)
        with patch("service.tools.imessage_tools._c", return_value=client):
            with self.assertRaisesRegex(voice.VoiceUnavailable, "only with a model on this Mac"):
                run(voice.transcribe(b64(wav())))
        self.assertEqual(self.bodies, [])
