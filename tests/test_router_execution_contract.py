"""Structural router obligations for adversarial boundary prompts.

    .venv/bin/python tests/test_router_execution_contract.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.router.router import route  # noqa: E402

PASS = FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def routed(prompt: str):
    return asyncio.run(route(prompt))


def tools(d) -> set[str]:
    return set(d.tool_subset or ()) | {name for name, _ in d.direct_calls}


def test_exact_router_fixes() -> None:
    print("\nexact routing and arguments")
    d = routed("check my calender")
    check("bare calendar is 60 days", d.direct_calls == [("get_upcoming", {"days": 60})],
          str(d.direct_calls))

    d = routed("send a message to mom with the share price of nvidia and amd "
               "from today and from two weeks ago. Be descriptive")
    stock = [args for name, args in d.direct_calls if name == "get_stock_price"]
    check("exact stock span is pre-resolved", bool(stock) and stock[0]["period"] == "2 weeks",
          str(stock))
    check("stock payload never forces calendar", "get_upcoming" not in tools(d), str(tools(d)))
    check("source and send are obligations",
          frozenset({"get_stock_price"}) in d.required_tool_groups
          and frozenset({"send_message"}) in d.required_tool_groups)


def test_compound_claims_and_negatives() -> None:
    print("\ncompound claims and hard negative constraints")
    d = routed("Find the largest PDF in Downloads, summarize it, and draft an email about it to Sarah.")
    check("PDF/email has both halves",
          {"find_files", "read_file", "lookup_contact", "draft_email"} <= tools(d),
          str(tools(d)))

    d = routed("Find the Wi-Fi password in my notes and email it to me for my records.")
    check("self delivery is draft-only", {"search_notes", "draft_email"} <= tools(d)
          and "send_email" not in tools(d), str(tools(d)))

    d = routed("Summarize the text of this email without opening my inbox.")
    check("inbox prohibition is enforced", not ({"view_emails", "summarize_emails"} & tools(d)),
          str(tools(d)))

    d = routed("Create a plain-text file containing my notes.")
    check("Notes-to-file exposes write_file", {"search_notes", "write_file"} <= tools(d))
    check("Notes-to-file forbids Notes.app creation", "create_note" not in tools(d))


def test_conditions_completion_and_inventory() -> None:
    print("\nconditional, completion, and inventory routes")
    d = routed("If my battery is below 20%, turn on Low Power Mode; otherwise tell me the percentage.")
    check("battery condition has read and action", tools(d) == {"get_battery_status", "toggle_setting"})
    check("battery condition is machine-readable",
          d.conditional_tools == (("get_battery_status", "toggle_setting", "percent_below", 20),))

    d = routed("Complete the laundry reminder, not the calendar event with the same name.")
    check("completion resolves complete_reminder",
          d.direct_calls == [("complete_reminder", {"title": "laundry"})],
          str(d.direct_calls))
    check("calendar mutation is forbidden", not ({"cancel_event", "update_event"} & tools(d)))

    d = routed("Can you send text messages, create reminders, and read browser history?")
    check("capability answer is registry-backed", d.direct_calls
          and d.direct_calls[0][0] == "wisp_capabilities", str(d.direct_calls))
    check("capability question cannot perform actions",
          not ({"send_message", "add_reminder"} & tools(d)))


def test_bulk_reminder_delete_scopes() -> None:
    print("\nbulk reminder deletion scopes")
    d = routed("delete my reminders for today")
    check("today is a scope, not title text",
          d.direct_calls == [("clear_reminders", {"scope": "today"})],
          str(d.direct_calls))
    check("today forbids past-due and title matching",
          not ({"clear_past_reminders", "cancel_event"} & tools(d)), str(tools(d)))

    d = routed("clear all of my reminders")
    check("all reminders resolves to the true all scope",
          d.direct_calls == [("clear_reminders", {"scope": "all"})],
          str(d.direct_calls))
    check("all does not mean past-due only",
          "clear_past_reminders" not in tools(d), str(tools(d)))

    d = routed("delete my reminder")
    check("unnamed singular reminder is not widened to all",
          not any(name == "clear_reminders" for name, _ in d.direct_calls),
          str(d.direct_calls))


if __name__ == "__main__":
    test_exact_router_fixes()
    test_compound_claims_and_negatives()
    test_conditions_completion_and_inventory()
    test_bulk_reminder_delete_scopes()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
