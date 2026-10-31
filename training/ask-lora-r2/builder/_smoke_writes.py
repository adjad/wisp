"""Throwaway round-2 write smoke: drives /agent in-process (no model, no sockets).

Checks: add_calendar_event raises a card, is approved, returns its receipt and the
turn ends on the scripted final; a denied send_message puts "The user denied this
action." in the next tool result; add_reminder raises no card; update_event moves
a verified event (card raised, receipt accepted).
"""
from __future__ import annotations

import datetime as dt
import random

import harness as H
from harness import Call, Final
from world import World, Event

NOW = dt.datetime(2026, 11, 3, 10, 0)


def world() -> World:
    w = World(now=NOW)
    w.contacts = {"Maya Chen": ("+1 503-555-0142", "maya.chen@example.com")}
    w.events = [Event("Study group", NOW + dt.timedelta(days=1, hours=4), 60, "Library 2F")]
    return w


def run(label, prompt, steps, w, approve=None):
    H.set_clock(NOW)
    policy, events, final = H.run_scenario(prompt, steps, w, NOW, approve=approve)
    want = steps[-1].text if isinstance(steps[-1], Final) else None
    errs = [e for e in events if e.get("type") == "error"]
    print(f"== {label}")
    print("  cards_seen:", H.CURRENT["cards"], " ran:", [n for n, _ in H.CURRENT["calls"]],
          " effects:", [t for t, _ in w.effects])
    print("  steps done:", f"{policy.idx}/{len(policy.steps)}", " model requests:", len(policy.requests),
          " errors:", errs[:1])
    results = [str(e.get("result"))[:160] for e in events if e.get("type") == "tool_result"]
    for text in results:
        print("  tool result:", text)
    print("  scripted final:", want)
    print("  shown final:   ", final if final != want else "(same)")
    return policy, final, results


def main():
    tmw = (NOW + dt.timedelta(days=1)).date()
    # 1. calendar add -> card -> approved -> receipt -> final
    w = world()
    _p, final, _r = run("add_calendar_event approved",
                        "add a dentist appointment tomorrow at 3pm",
                        [[Call("add_calendar_event", title="Dentist", when_iso=f"{tmw}T15:00")],
                         Final("Added Dentist to your calendar for tomorrow at 3 PM.")], w, approve=[True])
    assert H.CURRENT["cards"] == ["add_calendar_event"], H.CURRENT["cards"]
    assert w.effects and w.effects[0][0] == "add_calendar_event"
    assert final == "Added Dentist to your calendar for tomorrow at 3 PM.", final

    # 1b. same, but the prompt says "my calendar": the host's fresh-personal guard
    # replaces the final (a production behaviour scen authors must know about).
    w = world()
    run("add_calendar_event, prompt says 'my calendar'",
        "put dentist on my calendar tomorrow at 3pm",
        [[Call("add_calendar_event", title="Dentist", when_iso=f"{tmw}T15:00")],
         Final("Added Dentist to your calendar for tomorrow at 3 PM.")], w, approve=[True])

    # 2. denied send: production ends the turn itself after a denied write (no
    # further model request), so the script stops at the call step.
    w = world()
    p, final, results = run("send_message denied",
                            "text Maya that I'm running 10 minutes late",
                            [[Call("send_message", to="Maya Chen", text="Running 10 minutes late, sorry!")]],
                            w, approve=[False])
    assert H.CURRENT["cards"] == ["send_message"], H.CURRENT["cards"]
    assert not w.effects
    assert any("The user denied this action." in t for t in results), results
    assert len(p.requests) == 1

    # 2b. denied calendar write: host text, no further model request
    w = world()
    p, final, results = run("add_calendar_event denied",
                            "add a dentist appointment tomorrow at 3pm",
                            [[Call("add_calendar_event", title="Dentist", when_iso=f"{tmw}T15:00")]],
                            w, approve=[False])
    assert H.CURRENT["cards"] == ["add_calendar_event"] and not w.effects and len(p.requests) == 1

    # 3. add_reminder: no card
    w = world()
    run("add_reminder (no card)",
        "remind me to call the pharmacy tomorrow at 9am",
        [[Call("add_reminder", title="Call the pharmacy", when_iso=f"{tmw}T09:00")],
         Final("Done — I'll remind you tomorrow at 9 AM to call the pharmacy.")], w)
    assert H.CURRENT["cards"] == [], H.CURRENT["cards"]
    assert w.effects and w.effects[0][0] == "add_reminder"

    # 4. update_event move, verified
    w = world()
    run("update_event move approved",
        "move my study group tomorrow to 4pm",
        [[Call("update_event", title="Study group", when_iso=f"{tmw}T16:00")],
         Final("Moved your study group to 4 PM tomorrow.")], w, approve=[True])
    assert H.CURRENT["cards"] == ["update_event"], H.CURRENT["cards"]
    assert w.effects and w.effects[0][0] == "update_event", w.effects
    assert w.events[0].start.hour == 16
    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    random.seed(0)
    main()
