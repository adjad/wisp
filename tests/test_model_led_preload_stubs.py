"""Model-led preload + stub schemas: a call to a stub never executes, it is an
implicit discovery. Synthetic tools and scripted clients only."""
import json

from service.router.model_led import DISCOVERY_TOOL
from service.router.model_led import MAX_DISCOVERIES
from service.tools import registry
from tests.test_model_led_integration import Approval, ScriptedClient, call, discover, names, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

ALL = {"get_upcoming", "view_emails", "view_messages", "write_file", "run_shell"}


def stub(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def by_name(request):
    return {s["function"]["name"]: s for s in request["tools"]}


def receipts(request):
    return [json.loads(m["content"]) for m in request["messages"]
            if m.get("role") == "tool" and m["content"].startswith("{")]


def discoveries(events):
    return [e for e in events if e["type"] == "routing_discovery"]


def test_round_zero_offers_discovery_preload_real_and_exact_stubs(synthetic):
    client, _, _ = run(["Synthetic answer."], model_led_preload=["get_upcoming"])
    offered = by_name(client.requests[0])
    assert set(offered) == {DISCOVERY_TOOL} | ALL
    assert offered["get_upcoming"] == registry.get_tool("get_upcoming").schema()
    for name in ALL - {"get_upcoming"}:
        assert offered[name] == stub(name)
        assert "description" not in offered[name]["function"]
        assert "properties" not in offered[name]["function"]["parameters"]


def test_preloaded_tool_executes_on_round_zero_without_discovery(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[call("get_upcoming", days=1)], "Synthetic agenda."],
                            model_led_preload=["get_upcoming"])
    assert effects == [("get_upcoming", {"days": 1})]
    assert discoveries(events) == []
    # Preload spent no attempt: discovery is still offered next round.
    assert DISCOVERY_TOOL in names(client.requests[1])


def test_preload_does_not_consume_a_discovery_attempt(synthetic):
    client, events, _ = run([[discover("mail", "view_emails")], [discover("messages_contacts", "view_messages")],
                             "Synthetic answer."], model_led_preload=["get_upcoming"])
    assert [e["status"] for e in discoveries(events)] == ["ready_next_step", "ready_next_step"]
    assert DISCOVERY_TOOL in names(client.requests[1])
    assert DISCOVERY_TOOL not in names(client.requests[2])


def test_stub_call_never_executes_and_returns_receipt(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[call("view_emails")], "Synthetic answer."], model_led_preload=["get_upcoming"])
    assert effects == []
    assert not any(e["type"] == "tool_call" and e.get("name") == "view_emails" for e in events)
    [receipt] = receipts(client.requests[1])
    assert receipt["schema_discovery"] == "ready_next_step"
    assert receipt["source_tools_executed"] is False
    assert len(discoveries(events)) == 1


def test_next_round_offers_real_schema_for_stubbed_tool(synthetic):
    effects, _ = synthetic
    client, _, _ = run([[call("view_emails")], [call("view_emails")], "Synthetic answer."],
                       model_led_preload=["get_upcoming"])
    offered = by_name(client.requests[1])
    assert offered["view_emails"] == registry.get_tool("view_emails").schema()
    assert offered["get_upcoming"] == registry.get_tool("get_upcoming").schema()
    assert offered["view_messages"] == stub("view_messages")
    assert DISCOVERY_TOOL in offered
    assert effects == [("view_emails", {})]  # executes only once its schema is loaded


def test_two_stub_calls_in_one_response_are_one_attempt_with_two_receipts(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[call("view_emails"), call("view_messages")], "Synthetic answer."])
    assert effects == []
    assert len(discoveries(events)) == 1
    assert set(discoveries(events)[0]["tools"]) == {"view_emails", "view_messages"}
    assert set(discoveries(events)[0]["families"]) == {"mail", "messages_contacts"}
    assert len(receipts(client.requests[1])) == 2
    assert all(r["schema_discovery"] == "ready_next_step" for r in receipts(client.requests[1]))
    offered = by_name(client.requests[1])
    assert offered["view_emails"]["function"].get("description")
    assert offered["view_messages"]["function"].get("description")
    assert DISCOVERY_TOOL in offered  # one attempt used, one left


def test_budget_exhaustion_removes_stubs_and_discovery(synthetic):
    assert MAX_DISCOVERIES == 2
    client, events, _ = run([[call("view_emails")], [discover("calendar", "get_upcoming")], "Synthetic answer."])
    assert [e["status"] for e in discoveries(events)] == ["ready_next_step", "ready_next_step"]
    assert DISCOVERY_TOOL in names(client.requests[1]) and "view_messages" in names(client.requests[1])
    assert names(client.requests[2]) == {"view_emails", "get_upcoming"}


def test_explicit_discovery_still_works_and_leaves_stubs(synthetic):
    effects, _ = synthetic
    client, _, _ = run([[discover("calendar", "get_upcoming")], [call("get_upcoming", days=1)], "Synthetic agenda."])
    offered = by_name(client.requests[1])
    assert offered["get_upcoming"] == registry.get_tool("get_upcoming").schema()
    assert offered["view_emails"] == stub("view_emails")
    assert DISCOVERY_TOOL in offered
    assert effects == [("get_upcoming", {"days": 1})]


def test_rejected_implicit_selection_executes_nothing(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[call("get_upcoming"), call("view_emails"), call("view_messages"), call("run_shell")],
                             "Synthetic answer."])
    assert effects == []
    results = [m["content"] for m in client.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(results) == 4
    assert all(r.startswith("Schema selection rejected:") for r in results)
    assert [e["status"] for e in discoveries(events)] == ["rejected"]


def test_forbidden_tool_is_neither_stub_nor_preload(synthetic):
    forbidden = frozenset({"view_emails"})
    client, _, _ = run(["Synthetic answer."], forbidden_tools=forbidden,
                       model_led_preload=["view_emails", "get_upcoming"])
    assert names(client.requests[0]) == {DISCOVERY_TOOL} | (ALL - {"view_emails"})


def test_forbidden_stub_call_is_not_offered_or_executed(synthetic):
    effects, _ = synthetic
    run([[call("view_emails")], "Synthetic answer."], forbidden_tools=frozenset({"view_emails"}))
    assert effects == []


def test_unknown_preload_names_are_ignored(synthetic):
    client, _, _ = run(["Synthetic answer."], model_led_preload=["no_such_tool", "get_upcoming", DISCOVERY_TOOL])
    offered = by_name(client.requests[0])
    assert set(offered) == {DISCOVERY_TOOL} | ALL
    assert "description" in offered["get_upcoming"]["function"]
    assert "no_such_tool" not in offered
    assert "description" in offered[DISCOVERY_TOOL]["function"]


def test_unavailable_preload_is_dropped(synthetic):
    _, register = synthetic
    register("unavailable_read", "email_tools", unavailable="No synthetic implementation")
    client, _, _ = run(["Synthetic answer."], model_led_preload=["unavailable_read"])
    assert "unavailable_read" not in names(client.requests[0])


def test_at_most_twelve_tools_are_preloaded(synthetic):
    _, register = synthetic
    extra = [f"read_extra_{i:02d}" for i in range(14)]
    for name in extra:
        register(name, "assistant_tools")
    client, _, _ = run(["Synthetic answer."], model_led_preload=extra)
    offered = by_name(client.requests[0])
    real = [n for n, s in offered.items() if n != DISCOVERY_TOOL and "description" in s["function"]]
    assert len(real) == 12
    assert real == [] or set(real) <= set(extra)


def test_legacy_route_ignores_preload(synthetic):
    effects, _ = synthetic
    client, _, _ = run([[call("get_upcoming", days=1)], "Synthetic agenda."], model_led_discovery=False,
                       tools=["get_upcoming"], model_led_preload=["view_emails"])
    assert names(client.requests[0]) == {"get_upcoming"}
    assert effects == [("get_upcoming", {"days": 1})]


def test_sibling_call_to_not_yet_loaded_tool_does_not_execute(synthetic):
    effects, _ = synthetic
    client, events, _ = run([[call("view_emails"), call("view_messages")], "Synthetic answer."],
                            model_led_preload=["get_upcoming"])
    assert effects == []
    assert not any(e["type"] == "tool_call" for e in events)
    assert len(receipts(client.requests[1])) == 2



def test_entrypoint_preload_is_a_fail_safe_lexical_head_start(monkeypatch):
    from service import main
    from service.router import reranker
    from service.router.model_led import MAX_PRELOAD
    picked = main._model_led_preload("remind me friday to email the landlord")
    assert 0 < len(picked) <= MAX_PRELOAD and "add_reminder" in picked
    def boom(*args, **kwargs):
        raise RuntimeError("ranking failed")
    monkeypatch.setattr(reranker, "ranked_lexical_candidates", boom)
    assert main._model_led_preload("anything") == []
