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
