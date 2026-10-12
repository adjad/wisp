"""An identical read repeated in one turn is not run again (small models loop on
reads they already have); different arguments, or a read after a write, still run."""
from tests.test_model_led_integration import Approval, call, run
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)


def results(events):
    return [e["result"] for e in events if e["type"] == "tool_result"]


def test_identical_read_is_not_run_twice(synthetic):
    effects, _ = synthetic
    _, events, _ = run([[call("get_upcoming", days=1)], [call("get_upcoming", days=1)],
                        [call("get_upcoming", days=1)], "Synthetic agenda."],
                       model_led_discovery=False, tools=["get_upcoming"])
    assert effects == [("get_upcoming", {"days": 1})]
    assert all("NOT run again" in r for r in results(events)[1:])


def test_read_with_different_arguments_still_runs(synthetic):
    effects, _ = synthetic
    run([[call("get_upcoming", days=1)], [call("get_upcoming", days=2)], "Synthetic agenda."],
        model_led_discovery=False, tools=["get_upcoming"])
    assert effects == [("get_upcoming", {"days": 1}), ("get_upcoming", {"days": 2})]


def test_read_after_a_successful_write_runs_again(synthetic):
    effects, _ = synthetic
    run([[call("get_upcoming", days=1)], [call("write_file", path="/tmp/x.txt", content="hi")],
         [call("get_upcoming", days=1)], "Synthetic agenda."],
        model_led_discovery=False, tools=["get_upcoming", "write_file"], approval=Approval(allowed=True))
    assert [name for name, _ in effects] == ["get_upcoming", "write_file", "get_upcoming"]


def test_identical_read_guard_applies_under_model_led_discovery(synthetic):
    from tests.test_model_led_integration import discover
    effects, _ = synthetic
    run([[discover("calendar", "get_upcoming")], [call("get_upcoming", period="this week")],
         [call("get_upcoming", period="this week")], "Synthetic agenda."])
    assert effects == [("get_upcoming", {"period": "this week"})]


def test_read_after_a_write_outside_the_effect_table_runs_again(synthetic):
    # set_volume-style tools default to effect "read" in the outcome table; the
    # guard must still treat them as writes because their category isn't a read.
    effects, register = synthetic
    register("set_volume", "device_tools", "system_write", parameters={"type": "object", "properties": {
        "level": {"type": "integer"}}, "additionalProperties": False})
    run([[call("get_upcoming", days=1)], [call("set_volume", level=50)],
         [call("get_upcoming", days=1)], "Synthetic answer."],
        model_led_discovery=False, tools=["get_upcoming", "set_volume"], approval=Approval(allowed=True))
    assert [name for name, _ in effects] == ["get_upcoming", "set_volume", "get_upcoming"]


def test_a_parenthesized_notice_does_not_block_an_identical_retry(synthetic, monkeypatch):
    from service.tools import registry
    calls = []

    async def empty_search(**args):
        calls.append(args)
        return "(no public web results found; do not answer from memory.)"
    empty_search.__module__ = "service.tools.web_tools"
    monkeypatch.setitem(registry.REGISTRY, "web_search", registry.Tool(
        "web_search", "Synthetic search", {"type": "object", "properties": {"query": {"type": "string"}}},
        "web_read", empty_search))
    run([[call("web_search", query="x")], [call("web_search", query="x")], "Synthetic answer."],
        model_led_discovery=False, tools=["web_search"])
    assert len(calls) == 2


def test_volatile_status_reads_can_be_checked_again(synthetic):
    effects, register = synthetic
    register("get_battery_status", "system_tools", "system_read")
    run([[call("get_battery_status")], [call("get_battery_status")], "Synthetic answer."],
        model_led_discovery=False, tools=["get_battery_status"])
    assert [name for name, _ in effects] == ["get_battery_status", "get_battery_status"]
