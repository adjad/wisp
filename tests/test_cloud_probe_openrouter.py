"""Connect's model probe: OpenRouter's public model list never stands in for a key test."""
import asyncio
from types import SimpleNamespace

from service import main


class Probe:
    def __init__(self, provider, answer=True):
        self.provider = SimpleNamespace(name=provider)
        self.answer = answer
        self.chats = 0

    async def chat(self, model, messages, max_tokens=16):
        self.chats += 1
        if not self.answer:
            raise RuntimeError("401 synthetic bad key")
        return {"choices": [{"message": {"content": "OK"}}]}


def run(probe, listed):
    return asyncio.run(main._cloud_model_answers(probe, "anthropic/claude-haiku-5.5", listed))


def test_openrouter_listed_model_still_sends_one_completion():
    probe = Probe("openrouter")
    assert run(probe, ["anthropic/claude-haiku-5.5"]) is True
    assert probe.chats == 1


def test_openrouter_bad_key_fails_even_when_the_model_is_listed():
    probe = Probe("openrouter", answer=False)
    assert run(probe, ["anthropic/claude-haiku-5.5"]) is False


def test_other_providers_keep_the_listing_shortcut():
    probe = Probe("openai-compatible")
    assert run(probe, ["anthropic/claude-haiku-5.5"]) is True
    assert probe.chats == 0
