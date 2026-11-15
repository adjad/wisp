"""A reminder request with its time and title is created as given (2026-11-15).

"Set a reminder at 9:00 a.m. tomorrow for Mom's birthday." was read as "on the
birthday's date": the model looked the birthday up in Contacts, found none and
refused. The typed compiler resolves it (the stated time is the alert, what
follows for/about/to is the title); the Ask turn binds that, leads with
add_reminder and offers no contact-date lookup. A request with no time ("remind
me on Mom's birthday") is left to look the date up or ask.

Synthetic only: no Contacts, no Reminders, no model.
"""
import asyncio
import copy
from datetime import datetime, timedelta

import pytest

from service import model_only as mo
from service.agent import loop
from service.tasks.compiler import compile_reminder_create
from service.tools import registry
from tests.test_final_ask_card_and_referents import OWNER
from tests.test_model_led_entrypoint import endpoint  # noqa: F401  (fixture)
from tests.test_model_led_integration import Approval, call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 15, 11, 30)          # a Sunday


@pytest.mark.parametrize("prompt, title, when", [
    ("Set a reminder at 9:00 a.m. tomorrow for Mom's birthday.", "Mom's birthday", "2026-11-16T09:00"),
    ("remind me tomorrow at 9 about Mom's birthday", "Mom's birthday", "2026-11-16T09:00"),
    ("set a reminder for Dad's anniversary at 5pm Friday", "Dad's anniversary", "2026-11-20T17:00"),
    ("set a reminder for 9am tomorrow about the dentist", "the dentist", "2026-11-16T09:00"),
    ("remind me at 6 p.m. to call the bank", "call the bank", "2026-11-15T18:00"),
    ("set a reminder at 9am tomorrow to buy milk", "buy milk", "2026-11-16T09:00"),
])
def test_the_stated_time_is_the_alert_and_the_rest_the_title(prompt, title, when):
    plan = compile_reminder_create(prompt, now=NOW)
    assert plan is not None and plan.status == "ready"
    assert (plan.subject.value, plan.temporal.absolute_iso) == (title, when)
    assert mo.typed_reminder(prompt, now=NOW) == {"title": title, "when_iso": when, "kind": "reminder"}


@pytest.mark.parametrize("prompt", [
    "remind me on Mom's birthday",        # no time: look the date up or ask
    "set a reminder for Mom's birthday",
    "remind me tomorrow",                 # no title
    "set a reminder at 9 tomorrow to call Mom and text Dad at 5",
])
def test_a_request_without_both_is_left_to_the_model(prompt):
    assert mo.typed_reminder(prompt, now=NOW) is None


def test_the_ask_turn_binds_the_reminder_and_offers_no_birthday_lookup(endpoint, monkeypatch):
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    for name, module, category in (("add_reminder", "assistant_tools", "assistant_write"),
                                   ("contact_dates", "contacts_tools", "assistant_read"),
                                   ("lookup_contact", "imessage_tools", "contacts_read")):
        async def fake(**args):
            return "unused"
        fake.__module__ = "service.tools." + module
        registry.REGISTRY[name] = registry.Tool(name, "Synthetic", {"type": "object", "properties": {}},
                                                category, fake)
    sid = endpoint.store.create_session()
    endpoint.request("Set a reminder at 9:00 a.m. tomorrow for Mom's birthday.", sid)
    sent = endpoint.agent_calls[-1]
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert sent["tool_argument_bindings"]["add_reminder"] == {
        "title": "Mom's birthday", "when_iso": f"{tomorrow}T09:00", "kind": "reminder"}
    assert "contact_dates" in sent["forbidden_tools"]
    assert "THIS REMINDER" in (sent.get("style_hint") or "")

    # No time given: nothing bound, and the birthday lookup stays offered.
    endpoint.request("remind me on Mom's birthday", endpoint.store.create_session())
    sent = endpoint.agent_calls[-1]
    assert "add_reminder" not in (sent.get("tool_argument_bindings") or {})
    assert "contact_dates" not in sent["forbidden_tools"]


def test_the_model_cannot_change_the_bound_title_or_time(synthetic):
    effects, register = synthetic
    register("add_reminder", "assistant_tools", "assistant_write", parameters={
        "type": "object", "properties": {"title": {"type": "string"}, "when_iso": {"type": "string"},
                                         "kind": {"type": "string"}}, "required": ["title", "when_iso"]})
    register("contact_dates", "contacts_tools", "assistant_read",
             parameters={"type": "object", "properties": {"days": {"type": "integer"}}})
    bound = mo.typed_reminder("Set a reminder at 9:00 a.m. tomorrow for Mom's birthday.", now=NOW)
    run([[call("contact_dates", days=30)],
         [call("add_reminder", title="Mom birthday day", when_iso="2027-03-02T00:00")], "Set."],
        prompt="Set a reminder at 9:00 a.m. tomorrow for Mom's birthday.", approval=Approval(True),
        tool_argument_bindings={"add_reminder": bound}, forbidden_tools=frozenset({"contact_dates"}), **OWNER)
    assert effects == [("add_reminder", bound)]          # no lookup ran; the user's words won
