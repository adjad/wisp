import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import service.inference.super_model as super_model


def decision(**overrides):
    values = {
        "needs_tools": False,
        "light_read": False,
        "tool_subset": [],
        "direct_calls": [],
        "required_tool_groups": (),
        "tool_argument_bindings": {},
        "strict_read_limits": {},
        "force_first_tool": None,
        "route_source": "test",
        "expect_tool_first": False,
        "reminder_action": "",
        "conditional_tools": (),
        "multi_round": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def classify(prompt, route=None):
    return asyncio.run(super_model.cloud_super_model_eligible(
        prompt, route or decision()))


def synthetic_public_story(summary):
    from service.tools.registry import PublicSearchToolResult
    from service.tools.web_tools import _public_search_display, _public_search_model_evidence

    hits = [SimpleNamespace(title="Public story", url="https://source.example/story",
                            snippet=summary)]
    return PublicSearchToolResult(
        "Synthetic local tool display",
        model_text=_public_search_model_evidence(hits),
        cloud_display=_public_search_display(hits))


def test_high_confidence_standalone_generation_uses_cloud(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.0009, 0.0327, 0.0061))
    assert classify("How does a rocket work?") == (
        True, "Laya classified this as standalone non-sensitive generation")


def test_laya_privacy_or_context_risk_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.02, 0.01, 0.71))
    allowed, reason = classify("What about the second option?")
    assert allowed is False
    assert "conversation context" in reason


def test_uncertain_private_score_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.49, 0.01, 0.01))
    assert classify("Can you help me interpret this personal situation?")[0] is False


def test_noisy_computer_score_still_blocks_ambiguous_creative_prompt(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.014, 0.3373, 0.0136))
    routed = decision(route_source="default", needs_tools=True,
                      tool_subset=["run_shell", "write_document"])
    assert classify("Write a haiku about the moon.", routed)[0] is False


def test_computer_question_excludes_public_web_tools():
    question = super_model._QUESTIONS["computer"]["instructions"]
    assert "public web search" in question
    assert "external tool" not in question


def test_private_or_unscoped_tools_stay_local_without_calling_laya(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("called")))
    assert classify("What is on my calendar?", decision(needs_tools=True))[0] is False
    assert classify("Summarize the source", decision(light_read=True))[0] is False


def test_public_read_only_tools_can_use_cloud_synthesis(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.0015, 0.6718, 0.0023))
    news = decision(
        needs_tools=True,
        tool_subset=["web_search"],
        direct_calls=[("web_search", {"query": "stock market news today"})],
        required_tool_groups=(frozenset({"web_search"}),),
        tool_argument_bindings={"web_search": {"query": "stock market news today"}},
        force_first_tool="web_search",
    )
    assert classify("What is happening in the stock market today?", news)[0] is True

    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (0.08, 0.04, 0.03))
    assert classify("What is happening in the stock market today?", news)[0] is False


def test_weather_route_trims_arbitrary_fetchers_before_cloud(monkeypatch):
    from service.router.router import route

    prompt = "What is the weather in Seattle?"
    routed = asyncio.run(route(prompt))
    assert "get_weather" in routed.tool_subset
    assert "web_fetch" in routed.tool_subset
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.0015, 0.5275, 0.0055))
    assert classify(prompt, routed)[0] is True
    super_model.prepare_cloud_standalone(routed)
    assert "get_weather" in routed.tool_subset
    assert "web_fetch" not in routed.tool_subset
    assert "http_request" not in routed.tool_subset


def test_explicit_personal_scope_stays_local_on_public_tool_route(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (_ for _ in ()).throw(AssertionError("called")))
    routed = decision(needs_tools=True, tool_subset=["web_search"],
                      route_source="rules")
    assert classify("Search the web for my latest emails", routed)[0] is False
    assert classify("What is the weather at my address?", routed)[0] is False


def test_arbitrary_page_fetch_stays_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (_ for _ in ()).throw(AssertionError("called")))
    page = decision(needs_tools=True, tool_subset=["web_fetch"],
                    direct_calls=[("web_fetch", {"url": "https://example.com/story"})])
    assert classify("Summarize https://example.com/story", page)[0] is False
    ambiguous = decision(route_source="default", needs_tools=True,
                         tool_subset=["run_shell", "web_fetch"])
    for prompt in ("Summarize https://example.com/story",
                   "What does publisher.example.com/story say?",
                   "What is on publisher.example.com?",
                   "What does publisher.公司/story say?",
                   "What does 例子。公司/story say?",
                   "What does publisher．公司/story say?",
                   "What is at 192.168.0.1/config?sig=synthetic?",
                   "What is on [::1]/admin?",
                   "What does intranet/config say?",
                   "What does intranet/a say?",
                   "What does intranet/ say?",
                   "What does intranet%2Fa say?",
                   "What does intranet%252Fa say?",
                   "What does 例子&#12290;公司 say?",
                   "What does intranet.\u200blocal say?",
                   "What does 2130706433 say?",
                   "What does 0x7f000001 say?",
                   "What does fd00:0:0:0:0:0:0:1 say?",
                   "What does intranet／a say?"):
        assert classify(prompt, ambiguous)[0] is False

    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.001, 0.001, 0.001))
    for prompt in ("How does a rocket work?", "Is 1/2 equal to 0.5?",
                   "Explain the phrase and/or"):
        assert classify(prompt, ambiguous)[0] is True


def test_actual_rocket_route_becomes_tool_free_cloud_generation(monkeypatch):
    from service.router.router import route

    prompt = "How does a rocket work?"
    routed = asyncio.run(route(prompt))
    assert routed.route_source == "default"
    assert routed.needs_tools
    assert "run_shell" in (routed.tool_subset or ())
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.0009, 0.0327, 0.0061))

    assert classify(prompt, routed)[0] is True
    super_model.prepare_cloud_standalone(routed)
    assert routed.needs_tools is False
    assert routed.tool_subset == []
    assert routed.expect_tool_first is False
    assert routed.multi_round is False


def test_default_tool_menu_stays_local_when_laya_finds_private_risk(monkeypatch):
    from service.router.router import route

    routed = asyncio.run(route("How does this personal situation work?"))
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (0.49, 0.01, 0.01))
    assert classify("How does this personal situation work?", routed)[0] is False
    assert routed.needs_tools is True


def test_default_route_preserves_installed_skill_tools(monkeypatch):
    from service.tools.registry import REGISTRY

    monkeypatch.setitem(REGISTRY, "count_vowels", SimpleNamespace(category="skill_tool"))
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda _prompt: (_ for _ in ()).throw(AssertionError("called")))
    for selected in (["run_shell", "count_vowels"], ["run_shell", "use_skill"]):
        routed = decision(route_source="default", needs_tools=True,
                          tool_subset=selected[:])
        assert classify("Count the vowels in banana", routed)[0] is False
        super_model.prepare_cloud_standalone(routed)
        assert routed.needs_tools is True
        assert routed.tool_subset == selected


def test_mixed_public_and_private_or_effect_tools_stay_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("called")))
    mixed = decision(
        needs_tools=True,
        tool_subset=["web_search", "send_message"],
        required_tool_groups=(frozenset({"web_search"}), frozenset({"send_message"})),
    )
    assert classify("Find the news and text it to Mom", mixed)[0] is False


def test_cloud_public_web_prompt_excludes_identity_memory_and_skills(monkeypatch):
    from service.agent import loop
    from service.memory import identity, prompt_blocks
    from service import skills

    monkeypatch.setattr(identity, "identity_prompt_block",
                        lambda **_kw: "PRIVATE_IDENTITY")
    monkeypatch.setattr(prompt_blocks, "memory_block",
                        lambda **_kw: "PRIVATE_MEMORY")
    monkeypatch.setattr(skills, "skills_context_block",
                        lambda *_args: "PRIVATE_SKILL")
    async def public_result(_tool, _args):
        return synthetic_public_story("Public update.")
    monkeypatch.setattr(loop, "run_tool", public_result)

    class Client:
        def __init__(self):
            self.requests = []

        async def ensure_only(self, *_args, **_kwargs):
            return None

        async def stream_events(self, _model, messages, **_kwargs):
            self.requests.append([dict(message) for message in messages])
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "A public answer.", "tool_calls": None}}

    class Approver:
        async def confirm(self, _action):
            raise AssertionError("unexpected approval")

    async def emit(_event):
        return None

    client = Client()
    asyncio.run(loop.run_agent(
        client, "Agents-A1-4B-oQe6",
        [{"role": "user", "content": "What is on the news today?"}],
        emit, Approver(), tools=["web_search"], max_steps=1,
        direct_calls=[("web_search", {"query": "news today"})],
        public_web_synthesis=True, include_memory_context=False))

    assert client.requests
    sent = str(client.requests[0])
    for private_marker in ("PRIVATE_IDENTITY", "PRIVATE_MEMORY", "PRIVATE_SKILL"):
        assert private_marker not in sent


@pytest.mark.parametrize("remote_fails", [False, True])
def test_cloud_news_direct_search_needs_no_remote_tools_and_falls_back(monkeypatch,
                                                                        remote_fails):
    from service.agent import loop
    from service.config.endpoints import Endpoint, EndpointConfigurationError, Target
    from service.inference.omlx_client import OMLXClient

    async def public_result(_tool, _args):
        return synthetic_public_story("Rates held steady.")
    monkeypatch.setattr(loop, "run_tool", public_result)

    target = Target(
        "agent", Endpoint("synthetic_cloud", "https://cloud.invalid", "env:SYNTHETIC_KEY",
                          provider="openai-compatible"),
        "synthetic-model", context_window=8192, capabilities=())
    remote = OMLXClient(target=target, api_key="synthetic")
    from service.tools import tool_schemas
    with pytest.raises(EndpointConfigurationError, match="not been qualified"):
        remote._fit_request(target.model, [{"role": "user", "content": "news"}],
                            tool_schemas(["web_search"]), 1000)
    requests = []

    class Client:
        target = remote.target

        async def ensure_only(self, *_args, **_kwargs):
            return None

        async def stream_events(self, model, messages, **kwargs):
            # Exercise the real remote guard. It rejects the nonempty schema
            # list the old narration step sent to this exact target shape.
            remote._fit_request(model, messages, kwargs.get("tools"),
                                kwargs["max_tokens"])
            requests.append((messages, kwargs))
            if remote_fails:
                raise EndpointConfigurationError("synthetic remote failure")
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "Public story: rates held steady.",
                "tool_calls": None}}

    class Approver:
        async def confirm(self, _action):
            raise AssertionError("unexpected approval")

    events = []

    async def emit(event):
        events.append(event)

    async def run():
        try:
            return await loop.run_agent(
                Client(), target.model,
                [{"role": "user", "content": "PRIVATE_HISTORY"},
                 {"role": "assistant", "content": "PRIVATE_ASSISTANT"},
                 {"role": "user", "content": "What is on the news today?"}],
                emit, Approver(), tools=["web_search"], max_steps=1,
                direct_calls=[("web_search", {"query": "news today"})],
                required_tool_groups=(frozenset({"web_search"}),),
                public_web_synthesis=True, include_memory_context=False)
        finally:
            await remote.aclose()

    answer = asyncio.run(run())
    assert len(requests) == 1
    sent_messages, kwargs = requests[0]
    assert kwargs["tools"] == []
    assert [message["role"] for message in sent_messages] == ["system", "user", "user"]
    sent = str(sent_messages)
    assert "Rates held steady" in sent
    assert "PRIVATE_HISTORY" not in sent
    assert "PRIVATE_ASSISTANT" not in sent
    assert "https://source.example/story" not in sent
    visible = "\n".join(str(event.get("text", "")) for event in events
                        if event.get("type") == "text")
    assert "[Public story](<https://source.example/story>)" in visible
    assert "EndpointConfigurationError" not in visible
    if remote_fails:
        assert "couldn't summarize" in answer
        assert "rates held steady" not in answer.lower()
    else:
        assert answer == "Public story: rates held steady."


def test_cloud_news_withholds_empty_evidence_from_remote(monkeypatch):
    from service.agent import loop

    async def no_evidence(_tool, _args):
        return "(no usable public tool evidence found.)"
    monkeypatch.setattr(loop, "run_tool", no_evidence)

    class Client:
        async def ensure_only(self, *_args, **_kwargs):
            raise AssertionError("remote inference should not start")

    class Approver:
        async def confirm(self, _action):
            raise AssertionError("unexpected approval")

    events = []

    async def emit(event):
        events.append(event)

    answer = asyncio.run(loop.run_agent(
        Client(), "synthetic-model",
        [{"role": "user", "content": "What is on the news today?"}],
        emit, Approver(), tools=["web_search"], max_steps=1,
        direct_calls=[("web_search", {"query": "news today"})],
        public_web_synthesis=True, include_memory_context=False))
    assert "couldn't retrieve usable public evidence" in answer
    assert any(event.get("type") == "text" and event.get("text") == answer
               for event in events)


@pytest.mark.parametrize("model_text,cloud_display", (
    ("(no public web results found; do not answer from memory.)",
     "No public web results found."),
    ("(no usable public web results found; do not answer from memory.)",
     "No usable public web results found."),
    ("(web search failed; no verified public result is available.)",
     "Public search is temporarily unavailable."),
))
def test_cloud_news_rejects_real_search_no_source_states(monkeypatch, model_text,
                                                          cloud_display):
    from service.agent import loop
    from service.tools.registry import PublicSearchToolResult

    async def no_sources(_tool, _args):
        return PublicSearchToolResult(
            "Synthetic local status", model_text=model_text,
            cloud_display=cloud_display)
    monkeypatch.setattr(loop, "run_tool", no_sources)

    class Client:
        async def ensure_only(self, *_args, **_kwargs):
            raise AssertionError("remote inference must not start without sources")

    class Approver:
        async def confirm(self, _action):
            raise AssertionError("unexpected approval")

    events = []

    async def emit(event):
        events.append(event)

    answer = asyncio.run(loop.run_agent(
        Client(), "synthetic-model",
        [{"role": "user", "content": "What is on the news today?"}],
        emit, Approver(), tools=["web_search"], max_steps=1,
        direct_calls=[("web_search", {"query": "news today"})],
        required_tool_groups=(frozenset({"web_search"}),),
        public_web_synthesis=True, include_memory_context=False))
    assert answer == "I couldn't retrieve usable public evidence for this request."
    visible = [event["text"] for event in events if event.get("type") == "text"]
    assert visible == [answer]
    assert "found public sources" not in str(events)


def test_public_route_needing_model_selected_arguments_stays_local(tmp_path,
                                                                   monkeypatch):
    from service import main
    from service.memory.store import SessionStore
    from service.router.router import route

    store = SessionStore(tmp_path / "weather-route.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "client", object(), raising=False)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: True)

    async def eligible(_prompt, _decision):
        return True, "synthetic public clearance"

    routed = asyncio.run(route("What is the weather in Seattle?"))
    assert routed.needs_tools and not routed.direct_calls

    async def selected_route(*_args, **_kwargs):
        return routed

    seen = []

    async def fake_agent(_client, _model, _messages, emit, _approver, **kwargs):
        seen.append(kwargs["public_web_synthesis"])
        await emit({"type": "text", "text": "Synthetic local weather answer."})
        return "Synthetic local weather answer."

    async def no_summary(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "cloud_super_model_eligible", eligible)
    monkeypatch.setattr(main, "route", selected_route)
    monkeypatch.setattr(main, "run_agent", fake_agent)
    monkeypatch.setattr(main, "maybe_summarize", no_summary)

    async def request():
        response = await main.agent({"prompt": "What is the weather in Seattle?",
                                     "session_id": store.create_session()})
        return [item async for item in response.body_iterator]

    asyncio.run(request())
    assert seen == [False]
    assert routed.route_source == "super_model_local"


def test_standalone_default_route_reaches_cloud_without_tools(tmp_path, monkeypatch):
    from service import main
    from service.config.endpoints import Endpoint, Target
    from service.memory.store import SessionStore
    from service.router.router import route

    prompt = "How does a rocket work?"
    routed = asyncio.run(route(prompt))
    assert routed.route_source == "default"
    assert routed.needs_tools and not routed.direct_calls
    store = SessionStore(tmp_path / "standalone-route.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "client", object(), raising=False)
    monkeypatch.setattr(main, "cloud_super_model_enabled", lambda: True)

    async def eligible(_prompt, _decision):
        return True, "synthetic standalone clearance"

    async def selected_route(*_args, **_kwargs):
        return routed

    target = Target("agent", Endpoint("synthetic_cloud", "https://cloud.invalid",
                                      "env:SYNTHETIC_KEY", provider="openai-compatible"),
                    "synthetic-cloud-model", context_window=8192)
    monkeypatch.setattr(main, "cloud_super_model_eligible", eligible)
    monkeypatch.setattr(main, "route", selected_route)
    monkeypatch.setattr(main, "cloud_super_model_target", lambda _role: target)

    async def request():
        response = await main.agent({"prompt": prompt, "test_mode": True,
                                     "session_id": store.create_session()})
        events = []
        async for item in response.body_iterator:
            events.append(json.loads(item.removeprefix("data: ").strip()))
        return events

    events = asyncio.run(request())
    route_event = next(event for event in events if event.get("type") == "routed")
    assert route_event["route_source"] == "super_model_cloud"
    assert route_event["needs_tools"] is False
    assert route_event["model"] == target.model
    assert routed.tool_subset == []
    assert not any(event.get("type") == "error" for event in events)


def test_explicit_secrets_and_local_override_stay_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya", lambda prompt: (0.0, 0.0, 0.0))
    for prompt in (
        "My API key is sk-example and I need help.",
        "Read /Users/person/private.txt and explain it.",
        "Use only the local model for this request.",
        "Keep this on my machine.",
        "Stay local.",
        "Keep local.",
        "Local only, please.",
        "Review ghp_abcdefghijklmnopqrstuvwxyz123456.",
        "Explain /tmp/private-notes.txt.",
        "Open ./private-notes.txt.",
        "Read \"/Users/person/private.txt\" and explain it.",
        "Open `./private-notes.txt`.",
        "Never send this to the cloud.",
    ):
        assert classify(prompt)[0] is False


def test_laya_loader_pins_revision_and_uses_cpu_gpu(monkeypatch):
    calls = []
    fake_agent = object()
    fake_laya = SimpleNamespace(load=lambda model_id, **kwargs: (
        calls.append((model_id, kwargs)) or fake_agent))
    monkeypatch.setitem(sys.modules, "laya_coreml", fake_laya)
    monkeypatch.setattr(super_model, "_agent", None)
    monkeypatch.setattr(super_model, "_load_failed", False)

    assert super_model._load_agent() is fake_agent
    assert calls == [(super_model.LAYA_MODEL_ID, {
        "revision": super_model.LAYA_MODEL_REVISION,
        "compute_units": "cpu_gpu",
    })]


def test_missing_or_malformed_laya_fails_local(monkeypatch):
    monkeypatch.setattr(super_model, "_predict_with_laya",
                        lambda prompt: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(super_model, "start_laya_warmup", lambda: None)
    allowed, reason = classify("Explain a public scientific concept.")
    assert allowed is False
    assert "unavailable or uncertain" in reason
