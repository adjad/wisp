"""The Gemini API's own cap on one inline request, checked before anything is sent.

Synthetic only: no Keychain, no network. A request inside every other cap (each
picture under 5 MiB, all media under 16 MiB, text under the transcript budget) but
past about 19 MiB in all is refused with a plain message, instead of Google's error
after the upload. The same request to another provider is not held to it.
"""
from tests.test_remote_inference_policy import EndpointConfigurationError, policy
from tests.test_remote_media import FULL, MediaBase, image_url
from service.inference.providers import GEMINI_API_PREFIX, GEMINI_ORIGIN


def big_request(model):
    big = image_url(b"\x89PNG\r\n\x1a\n" + b"\0" * int(3.9 * 1024 * 1024))
    messages = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                             {"type": "image_url", "image_url": {"url": big}}]}
                for _ in range(3)]
    messages.append({"role": "user", "content": "x" * int(3.8 * 1024 * 1024)})
    return {"model": model, "stream": False, "max_tokens": 100, "messages": messages}


class GeminiInlineCap(MediaBase):
    profile_overrides = {**FULL, "model_id": "gemini-3.8-flash", "base_url": GEMINI_ORIGIN,
                         "api_prefix": GEMINI_API_PREFIX}

    def test_a_request_past_geminis_inline_cap_is_refused_before_sending(self):
        with self.assertRaisesRegex(EndpointConfigurationError, "too large to send to Gemini"):
            policy.remote_only_profile().validate_payload(big_request("gemini-3.8-flash"))
        self.assertEqual(self.bodies, [])

    def test_a_small_picture_request_still_passes(self):
        policy.remote_only_profile().validate_payload(
            {"model": "gemini-3.8-flash", "stream": False, "max_tokens": 100,
             "messages": [{"role": "user", "content": [{"type": "text", "text": "look"},
                                                       {"type": "image_url", "image_url": {"url": image_url()}}]}]})
