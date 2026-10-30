"""Offline tests for the toolbench world: fake receipts, schemas, seeded facts, free time.

No model, no network, no Mac apps. The fakes run against a fresh synthetic
world; `service.tools.registry.classify_tool_outcome` is the REAL classifier the
agent loop uses, so a receipt the loop would read as failed fails here too.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="toolbench-world-test-"))

from scripts.toolbench.world import (  # noqa: E402
    ACCESS_DOMAINS, CANONICAL, CANONICAL_TODAY, FACT_POOLS, HOLIDAYS, build_world, date_facts, generic_fake,
    install_hooks, make_facts, make_fakes, register_extra_tools, seed_memory)

TODAY = date(2026, 10, 14)  # a Wednesday; the world's "now" is 8:30 AM


@pytest.fixture
def world():
    return build_world(TODAY)


@pytest.fixture
def fk(world):
    return make_fakes(world)


def status(tool: str, result) -> str:
    from service.tools.registry import classify_tool_outcome
    return classify_tool_outcome(tool, result).status


@pytest.fixture(scope="module")
def extras():
    """The skill and MCP tools production adds at startup, registered then removed again."""
    import service.tools  # noqa: F401
    from service import skills
    from service.mcp import TOOL_PARTS
    from service.skills import tools as skill_tools
    from service.tools.registry import REGISTRY
    saved = (dict(REGISTRY), dict(TOOL_PARTS), dict(skills._skills), set(skill_tools._registered))
    names = register_extra_tools()
    yield names
    REGISTRY.clear(), REGISTRY.update(saved[0])
    TOOL_PARTS.clear(), TOOL_PARTS.update(saved[1])
    skills._skills.clear(), skills._skills.update(saved[2])
    skill_tools._registered.clear(), skill_tools._registered.update(saved[3])


# ---------------------------------------------------------------- receipts

def test_outbound_receipts_are_classified_as_success(fk):
    assert fk["draft_email"](to="dad@example.com", subject="s", body="b").startswith("Draft opened in Mail")
    assert status("draft_email", fk["draft_email"](to="Dad", subject="s", body="b")) == "succeeded"
    assert status("forward_email", fk["forward_email"](message_id="m-1002", to="acct@example.com")) == "succeeded"
    assert fk["forward_email"](message_id="m-1002", to="Dad").startswith("Forwarded to dad@example.com")
    sched = fk["schedule_send"](channel="message", to="Mom", when="tomorrow at 9am", body="hi")
    assert sched.startswith("Scheduled: text to Mom (+1 555-555-0101)") and status("schedule_send", sched) == "succeeded"
    mail = fk["schedule_send"](channel="email", to="Dad", when="in 2 hours", body="hi", subject="s")
    assert mail.startswith("Scheduled: email to Dad (dad@example.com)")
    assert status("send_email", fk["send_email"](to="acct@example.com", subject="s", body="b")) == "succeeded"
    assert status("send_message", fk["send_message"](to="Mom", text="on my way")) == "succeeded"
    assert status("reply_to_email", fk["reply_to_email"](message_id="m-1004", body="thanks")) == "succeeded"
    assert status("add_reminder", fk["add_reminder"](title="x", when_iso="2026-10-20T09:00")) == "succeeded"


def test_draft_message_returns_the_typed_receipt_for_a_literal_recipient(fk):
    from service.tools.registry import _PreparedMessageDraft, _valid_message_draft
    by_name = fk["draft_message"](to="Mom", text="see you at 6")
    assert type(by_name) is _PreparedMessageDraft and _valid_message_draft(by_name)
    assert by_name.resolved_to == "+1 555-555-0101" and by_name.prepared_text == "see you at 6"
    assert fk["draft_message"](to="+1 555-555-0188", text="hi").resolved_to == "+1 555-555-0188"
    assert status("draft_message", by_name) == "succeeded"
    # No saved contact, no literal handle: nothing is drafted, and the loop reads it as failed.
    assert status("draft_message", fk["draft_message"](to="Nobody Real", text="hi")) == "failed"
    assert status("draft_message", fk["draft_message"](to="Mom", text="  ")) == "failed"


def test_recipient_rules_match_production(fk, world):
    # Mom has a phone number only; Dad has both; the coworker has an email.
    assert "has no email address saved in Contacts — only a phone number" in fk["draft_email"](
        to="Mom", subject="s", body="b")
    assert status("send_email", fk["send_email"](to="Mom", subject="s", body="b")) == "needs_input"
    assert fk["lookup_contact"](name="Dad") == "Dad: +1 555-555-0102 (also on dad@example.com)"
    assert fk["lookup_contact"](name="Mom") == "Mom: +1 555-555-0101"
    assert fk["send_email"](to=world.facts["coworker_first"], subject="s", body="b").startswith("Email sent to ")
    # Saved in lowercase, shown title-cased, and resolvable by the lowercase name a user types.
    assert fk["lookup_contact"](name="jules").startswith("Jules: +1 555-555-0133")
    assert fk["send_email"](to="jules", subject="calendar", body="b") == "Email sent to jules@example.com."
    # The user's own address is refused unless they confirmed.
    own = fk["send_email"](to="test.user@example.com", subject="s", body="b")
    assert "USER'S OWN address" in own and status("send_email", own) == "failed"
    assert fk["send_email"](to="test.user@example.com", subject="s", body="b",
                            confirmed_self_send=True).startswith("Email sent to")
    assert fk["lookup_contact"](name="test").startswith("Several contacts match 'test'")
    assert fk["lookup_contact"](name="Zed").startswith("(No saved contact matches 'Zed'.")
    assert fk["list_contacts"]().startswith("6 contact(s):") and world.facts["contact_count"] == "6"


def test_schedule_send_refusals_are_failures(fk):
    assert status("schedule_send", fk["schedule_send"](channel="message", to="Mom", when="yesterday at 9am",
                                                      body="hi")) == "failed"
    assert status("schedule_send", fk["schedule_send"](channel="message", to="Mom", when="whenever",
                                                      body="hi")) == "failed"
    assert fk["schedule_send"](channel="pigeon", to="Mom", when="at 6pm", body="hi") == (
        "(channel must be 'email' or 'message')")
    assert "exactly one recipient" in fk["schedule_send"](channel="email", to="a@x.com, b@x.com",
                                                          when="at 6pm", body="hi")


def test_view_emails_no_match_is_recognised(fk):
    miss = fk["view_emails"](query="zebra crossing")
    assert miss.startswith("No emails matching “zebra crossing” were found") and status("view_emails", miss) == "no_match"
    assert status("view_emails", fk["view_emails"](query="chase")) == "succeeded"
    assert status("view_emails", fk["view_emails"](query="chase", unread=True)) == "succeeded"
    # The query is production's whole-phrase substring match, not an any-word match.
    assert fk["view_emails"](query="chase balance").startswith("No emails matching")


def test_other_empty_answers_are_no_match(world, fk):
    assert status("complete_reminder", fk["complete_reminder"](title="launch the rocket")) == "no_match"
    world.events = []
    world.reminders = []
    empty = fk["get_upcoming"](period="today")
    assert empty == f"Today is Wednesday, October 14, 2026. Nothing scheduled in today." and status(
        "get_upcoming", empty) == "no_match"
    assert status("get_upcoming", fk["get_upcoming"]()) == "no_match"
    assert status("find_free_time", fk["find_free_time"](period="tomorrow", minutes=60)) == "succeeded"
    world.events = [{**e} for e in build_world(TODAY).events if e["title"].startswith("Family")]
    assert status("find_free_time", fk["find_free_time"](period=world.events[0]["day"].isoformat())) == "no_match"


# ------------------------------------------------------------- schemas

BESPOKE = [
    "get_my_day_plan", "predict_my_day", "find_free_time", "update_event", "cancel_event", "clear_reminders",
    "clear_past_reminders", "describe_me", "my_patterns", "search_conversations", "list_scheduled_sends",
    "cancel_scheduled_send", "scan_subscriptions", "manage_timers", "manage_contacts", "list_running_apps",
    "world_time", "wisp_capabilities", "wisp_status", "wisp_skills", "wisp_mcp", "mac_get_volume",
    "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media", "mcp_drive_search_drive_files",
    "mcp_drive_create_document", "web_search", "get_weather", "calculate", "convert_units",
    "draft_email", "forward_email", "schedule_send", "draft_message", "view_emails", "search_reminders",
    "daily_brief", "get_upcoming", "summarize_emails", "add_reminder", "use_skill", "send_email",
    "send_message", "reply_to_email", "add_calendar_event", "complete_reminder", "update_reminder",
    "view_messages", "summarize_messages", "get_past_events", "recall", "remember", "forget", "set_timer",
]


def test_bespoke_fake_signatures_accept_every_schema_argument(fk, extras):
    from service.tools.registry import REGISTRY
    for name in BESPOKE:
        assert name in fk, f"no fake for {name}"
        assert name in REGISTRY, f"{name} is not a registered tool"
        params = inspect.signature(fk[name]).parameters
        schema = REGISTRY[name].parameters or {}
        for prop in (schema.get("properties") or {}):
            assert prop in params and params[prop].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY), (name, prop)
        for req in schema.get("required") or []:
            assert req in params, (name, req)
        inspect.signature(fk[name]).bind(**{p: None for p in (schema.get("properties") or {})})


def test_extra_tools_are_registered_with_production_names(extras):
    from service.mcp import TOOL_PARTS
    from service.tools.registry import REGISTRY
    for n in ("mac_get_volume", "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media", "use_skill",
              "mcp_drive_search_drive_files", "mcp_drive_create_document"):
        assert n in extras and n in REGISTRY
    assert REGISTRY["mac_set_volume"].category == "skill_tool"
    assert REGISTRY["mcp_drive_search_drive_files"].category == "mcp_read"
    assert REGISTRY["mcp_drive_create_document"].category == "mcp_action"
    assert TOOL_PARTS["mcp_drive_create_document"] == "create_document"


def test_every_registered_tool_gets_a_fake_or_an_honest_generic(world, fk, extras):
    from service.tools.registry import REGISTRY
    for name, tool in REGISTRY.items():
        fn = fk.get(name) or generic_fake(world, name, tool.category)
        assert callable(fn)
    assert "no matching data" in generic_fake(world, "x", "fs_read")()


# ------------------------------------------------------------- facts

def test_seeded_worlds_bind_every_canonical_fact():
    assert make_facts(None)["holiday_date"] == HOLIDAYS[CANONICAL["holiday"]][1]
    for seed in [None, *range(1, 40)]:
        f = make_facts(seed)
        missing = set(CANONICAL) - set(f)
        assert not missing, missing
        assert all(isinstance(v, str) and v for v in f.values())
        assert f["stock_a"] != f["stock_b"]
        assert f["holiday_date"] == HOLIDAYS[f["holiday"]][1]
        assert f["friend_lower_email"] == f"{f['friend_lower']}@example.com"
        assert f["city_typo"] != f["city"] and len(f["city_typo"]) == len(f["city"]) - 1
        assert int(f["doordash_days_overdue"]) > 14 and int(f["stale_age_days"]) > 7
    assert {make_facts(s)["door_code"] for s in range(30)} != {CANONICAL["door_code"]}
    # Old seeds keep their old draws: new pools were appended after the originals.
    import random
    rng = random.Random(5)
    assert [make_facts(5)[k] for k in ("dentist", "dentist_time", "friend")] == [
        rng.choice(FACT_POOLS[k]) for k in ("dentist", "dentist_time", "friend")]


GROUNDED_EXEMPT = {  # derived, numeric or deliberately absent from the world's text
    "high_tomorrow", "battery", "city", "city_typo", "holiday_date", "contact_count", "inbox_total",
    "inbox_unread", "budget_invitees", "trip_day", "timer_minutes", "stale_age_days", "rent_days_overdue",
    "passport_days", "doordash_days_overdue", "newsletter_count", "class_word", "mom_phone_tail",
    # system_status numbers, date-relative facts and memory ids are asserted by their own tests
    "disk_free", "disk_total", "disk_used_pct", "memory_free_pct", "employer_fact_id", "isp_fact_id",
    "birthday_fact_id", *date_facts(TODAY)}


@pytest.mark.parametrize("seed", [None, 3, 11, 29])
def test_every_fact_is_present_in_the_seeded_world(seed):
    w = build_world(TODAY, seed)
    blob = json.dumps({k: getattr(w, k) for k in
                       ("events", "past_events", "emails", "messages", "notes", "reminders", "contacts", "files",
                        "memory_facts", "profile", "scheduled_sends", "timers", "drive_files", "patterns",
                        "conversations", "documents")}, default=str)
    for key, value in w.facts.items():
        if key in GROUNDED_EXEMPT:
            continue
        assert value in blob, f"{key}={value!r} is not in the seed-{seed} world"
    # Date and count facts are asserted through their effect instead.
    for r in w.reminders:
        if r["title"] == w.facts["rent_reminder"]:
            assert (TODAY - r["due"]).days == int(w.facts["rent_days_overdue"])
        if r["title"] == w.facts["passport_reminder"]:
            assert (r["due"] - TODAY).days == int(w.facts["passport_days"])
        if r["title"] == w.facts["doordash_reminder"]:
            assert (TODAY - r["due"]).days == int(w.facts["doordash_days_overdue"])
        if r["title"] == w.facts["stale_reminder"]:
            assert r["due"] is None and (TODAY - r["created"]).days == int(w.facts["stale_age_days"])
    assert w.facts["trip_day"] == next(e for e in w.events if e["title"] == w.facts["trip"])["day"].strftime("%A")
    f = make_fakes(w)
    assert w.facts["high_tomorrow"] in f["get_weather"](location=w.facts["city"])
    assert w.facts["battery"] in f["system_status"]()
    assert "HTTP 404" in f["get_weather"](location=w.facts["city_typo"])
    assert f"{len(w.contacts)} contact(s)" in f["list_contacts"]()
    assert w.facts["contact_count"] == str(len(w.contacts)) == "6"


def test_inbox_totals_and_the_mirrored_email(world, fk):
    unique = {m["id"]: m for m in world.emails}
    assert len(unique) == int(world.facts["inbox_total"]) == 17
    assert sum(m["unread"] for m in unique.values()) == int(world.facts["inbox_unread"]) == 14
    digest = fk["summarize_emails"]()
    assert "17 emails · 14 unread" in digest and "14 of these 17 emails are unread." in digest
    # The same message sits in Gmail and in Mail: two raw rows, one message.
    dinner = fk["view_emails"](query="Sunday dinner")
    assert dinner.count("Message-ID: m-1009") == 2 and "Account: gmail" in dinner and "Account: personal" in dinner
    assert digest.count("Sunday dinner") == 1


def test_long_email_keeps_its_late_correction(world, fk):
    f = world.facts
    body = next(m["body"] for m in world.emails if m["id"] == "m-1006")
    assert 11500 < len(body) < 13000
    assert body.index("9:00 AM") < body.index(f["correction_time"]) and body.rstrip().endswith("Facilities")
    out = fk["view_emails"](query="facilities")
    assert out.startswith("Account: work") and "digest of the" in out and "NOT verbatim" in out
    assert f["correction_time"] in out and f["door_code"] in out and len(out) < 6000
    assert "CORRECTION" in out


def test_purchase_alert_and_subscription_mail(world, fk):
    f = world.facts
    buy = fk["view_emails"](query=f["retailer"], strict_match=True)
    assert f["order_number"] in buy and f["order_item"] in buy
    alert = fk["view_emails"](query=f["alert_merchant"])
    assert f["alert_amount"] in alert and f["alert_card"] in alert
    subs = fk["scan_subscriptions"]()
    assert f"{f['newsletter']} <hello@dailybrew.example.com> — {f['newsletter_count']} emails" in subs
    assert fk["summarize_emails"](query="statement").startswith("Account: personal")  # query -> view_emails
    unread = fk["view_emails"](unread=True, count=20)
    assert "Message-ID: m-1001" in unread and "Message-ID: m-1004" not in unread   # m-1004 is the read one
    assert "Result coverage" not in unread and fk["view_emails"](count=3).startswith("Result coverage: returned 3 of 18")
    assert fk["summarize_emails"](day="yesterday").count("**") >= 4


def test_memory_facts_profile_and_conversations(world, fk):
    f = world.facts
    everything = fk["recall"]()
    assert f"Works at {f['employer']}" in everything and f"Uses {f['isp']} (internet)" in everything
    assert f"; mail; fact] \"Works at {f['employer']}\"" in everything
    assert f"My birthday is {f['user_birthday']}" in everything and "; explicit;" in everything
    assert f["stock_a"] in fk["recall"](query="stock") and f["stock_b"] in fk["recall"](query="stock")
    assert f["preferred_name"] == world.profile["name"] and world.profile["goals"] == [f["goal_a"], f["goal_b"]]
    assert "No matching memory" in fk["recall"](query="submarine")
    assert "Nothing was searched" in fk["recall"](query="the and of")
    assert f["convo_topic"] in fk["search_conversations"](query=f["convo_topic"])
    assert "No matching user conversation passages" in fk["search_conversations"](query="submarine")
    assert fk["remember"](fact="my gate code is 4417").startswith("Saved memory 7: ")
    assert "gate code" in fk["recall"](query="gate code")
    assert fk["forget"](query="penicillin").startswith("Forgot 1 matching memory record")
    assert fk["forget"](query="penicillin").startswith("No exact matching memories were forgotten")
    # The patterns Wisp learned.
    assert "[pattern p-101]" in fk["describe_me"]() and "yoga" in fk["my_patterns"](query="when do I work out")


def test_family_thread_keeps_authorship(world, fk):
    f = world.facts
    thread = fk["view_messages"](conversation=f["family_group"])
    assert "Me: I'll bring the apple pie." in thread
    assert f"Mom: @{f['family_member']} - Your post has {f['post_likes']} likes" in thread
    digest = fk["summarize_messages"]()
    assert "You sent “Can't wait to see them all on Sunday.”" in digest
    assert f"Mom to {f['family_member']} shared “Your post has {f['post_likes']} likes”" in digest
    assert fk["view_messages"](query="no such words").startswith("No messages matching")
    assert fk["view_messages"](conversation="nobody").startswith("No conversation matched")


# ------------------------------------------------------------ calendar

def test_find_free_time_gaps(world, fk):
    # Thursday: only the 3:30 PM dentist visit (60 minutes) sits in the 9-18 window.
    out = fk["find_free_time"](period="tomorrow", minutes=60)
    assert ("9:00 AM–10:00 AM (2026-10-15T09:00:00 → 2026-10-15T10:00:00; free until 2026-10-15T15:30:00)" in out)
    assert ("4:30 PM–5:30 PM (2026-10-15T16:30:00 → 2026-10-15T17:30:00; free until 2026-10-15T18:00:00)" in out)
    assert out.startswith("60-minute candidate slots, 9:00–18:00 (America/Los_Angeles):")
    # A 2-hour block fits only before the appointment (the evening gap is 90 minutes).
    long = fk["find_free_time"](period="tomorrow", minutes=120)
    assert "9:00 AM–11:00 AM" in long and "4:30 PM" not in long
    # No period: whole gaps, days counted from today. Today starts at "now" (8:30) and the 9:30 standup splits it.
    legacy = fk["find_free_time"](days=2, minutes=30)
    assert legacy.startswith("Free for 30+ minutes, 9:00–18:00 (America/Los_Angeles):")
    assert ("today (2026-10-14): 9:00 AM–9:30 AM (2026-10-14T09:00:00 → 2026-10-14T09:30:00; "
            "free until 2026-10-14T09:30:00), 9:45 AM–6:00 PM") in legacy
    assert "tomorrow (2026-10-15): 9:00 AM–3:30 PM" in legacy and "4:30 PM–6:00 PM" in legacy
    # The window can be narrowed, and nothing fits when it is too short.
    assert "9:00 AM" not in fk["find_free_time"](period="tomorrow", minutes=30, day_start=12, day_end=15)
    assert fk["find_free_time"](period="tomorrow", minutes=600).startswith("No matches: No gaps of 600+ minutes")
    assert fk["find_free_time"](minutes=0).startswith("(error: minutes must be between 1 and 1440.)")
    assert fk["find_free_time"](period="last tuesday") .startswith("(error:")


def test_holiday_calendar_frees_the_day_but_the_family_trip_blocks_it(world, fk):
    f = world.facts
    holiday = next(e for e in world.events if e["title"] == f["holiday"])
    trip = next(e for e in world.events if e["title"] == f["trip"])
    assert holiday["all_day"] and holiday["calendar_writable"] is False and holiday["calendar"] == f["holiday_calendar"]
    assert trip["all_day"] and trip["calendar_writable"] is True
    free = fk["find_free_time"](period=holiday["day"].isoformat(), minutes=60)
    assert "9:00 AM–10:00 AM" in free and "free until" in free and "No matches" not in free
    blocked = fk["find_free_time"](period=trip["day"].isoformat(), minutes=60)
    assert blocked.startswith("No matches: No gaps of 60+ minutes")
    # A user's own all-day entry on a calendar that happens to be called "Family Holidays" still blocks.
    holiday.update(calendar="Family Holidays", calendar_writable=True)
    assert fk["find_free_time"](period=holiday["day"].isoformat(), minutes=60).startswith("No matches")
    # A calendar that never reported writability counts as busy too.
    holiday.update(calendar=f["holiday_calendar"], calendar_writable=None)
    assert fk["find_free_time"](period=holiday["day"].isoformat(), minutes=60).startswith("No matches")


def test_get_upcoming_honours_its_arguments(world, fk):
    f = world.facts
    holiday_day = next(e for e in world.events if e["title"] == f["holiday"])["day"].isoformat()
    # Holidays stay out unless asked for.
    assert f["holiday"] not in fk["get_upcoming"](period=holiday_day)
    assert fk["get_upcoming"](period=holiday_day).startswith("Today is Wednesday, October 14, 2026. Nothing scheduled in")
    assert f"**All day** — {f['holiday']}" in fk["get_upcoming"](period=holiday_day, include_holidays=True)
    # period / days / query / calendar_only / boundary.
    assert "Brunch with" in fk["get_upcoming"](period="sunday") and "Patel" not in fk["get_upcoming"](period="sunday")
    assert "Vendor sync" in fk["get_upcoming"](query="vendor") and "Patel" not in fk["get_upcoming"](query="vendor")
    assert "Water the plants" in fk["get_upcoming"](period="today")
    assert "Water the plants" not in fk["get_upcoming"](calendar_only=True, period="today")
    assert f["passport_reminder"] not in fk["get_upcoming"]() and f["passport_reminder"] in fk["get_upcoming"](days=60)
    assert fk["get_upcoming"](boundary="end").startswith("(error: scheduled end projection requires")
    ends = fk["get_upcoming"](boundary="end", calendar_only=True, query="dentist")
    assert "ends at **4:30 PM**" in ends
    assert "(error:" in fk["get_upcoming"](period="the day after never")
    # The user's own all-day trip is listed.
    assert f["trip"] in fk["get_upcoming"]() and "Wisp-only" not in fk["get_upcoming"]()


def test_update_event_moves_or_refuses_like_production(world, fk):
    f = world.facts
    ok = fk["update_event"](title="Dentist appointment", when_iso="2026-10-15T16:00")
    assert ok == f"Moved “Dentist appointment with Dr. {f['dentist']}” to Thu Oct 15, 4:00 PM–5:00 PM."
    assert status("update_event", ok) == "succeeded"
    assert "4:00 PM" in fk["get_upcoming"](period="tomorrow")           # the world changed
    invited = fk["update_event"](title=f["budget_event"], when_iso="2026-10-16T14:00")
    assert invited.endswith("Invitees were notified of the new time.") and status("update_event", invited) == "succeeded"
    series = fk["update_event"](title=f["standup"], when_iso="2026-10-16T14:00")
    assert "Only this occurrence moved. Invitees were notified of the new time." in series
    assert status("update_event", series) == "succeeded"
    refusals = {
        f["holiday"]: f"“{f['holiday']}” is on a read-only calendar; Wisp can't change it from this Mac. "
                      "Nothing was moved. Move it in Calendar if it is yours to move.",
        f["vendor_event"]: f"Only the organizer can move “{f['vendor_event']}”; nothing was moved. "
                           "Ask them, or propose a new time in Calendar.",
        f["book_club"]: f"Wisp can't tell yet whether “{f['book_club']}” has invitees (Calendar hasn't synced "
                        "that); nothing was moved. Try again shortly or move it in Calendar.",
    }
    for title, text in refusals.items():
        got = fk["update_event"](title=title, when_iso="2026-10-30T20:00")
        assert got == text and status("update_event", got) == "failed", title
    renamed = fk["update_event"](title="Dentist", when_iso="2026-10-15T17:00", new_title="Teeth")
    assert renamed == ("Wisp can move an event to a new time, but it can't rename an event or change its "
                       "location. Nothing was changed; edit that in Calendar.")
    assert fk["update_event"](title="Dentist", when_iso="2026-10-15T17:00", location="Elsewhere") == renamed
    assert status("update_event", fk["update_event"](title="no such event", when_iso="2026-10-20T10:00")) == "needs_input"
    assert fk["update_event"](title="Dentist", when_iso="2026-10-01T10:00").endswith("is in the past — nothing was moved.)")
    assert fk["update_event"](title="Brunch with", when_iso="2026-10-18T11:00") == (
        f"“Brunch with {f['friend_first']}” already starts then; nothing was moved.")
    assert sum(1 for e in world.effects if e["tool"] == "update_event") == 3


def test_cancel_event_and_reminder_clearing(world, fk):
    f = world.facts
    assert fk["cancel_event"](title=f["holiday"]) == (
        f"“{f['holiday']}” is on a read-only calendar; Wisp can't change it from this Mac. "
        "Nothing was cancelled. Cancel it in Calendar if it is yours to cancel.")
    assert world.effects == []
    assert fk["cancel_event"](title="nothing like this").startswith("Nothing upcoming or past matches")
    assert fk["cancel_event"](title=f["dentist_reminder"]).startswith("Several items match “Dentist”:")
    # A stale reminder is hidden from reads but can still be cancelled by name.
    assert f["stale_reminder"] not in fk["search_reminders"]()
    assert fk["cancel_event"](title=f["stale_reminder"]) == f"Cancelled “{f['stale_reminder']}”."
    assert fk["cancel_event"](title=f["doordash_reminder"]) == f"Cancelled “{f['doordash_reminder']}”."
    assert fk["cancel_event"](title="car service").startswith("Cancelled “Car service")
    assert fk["clear_reminders"](scope="soon").startswith("(error: scope must be")
    assert fk["clear_reminders"](scope="tomorrow") == "No active reminders found for tomorrow."
    assert fk["clear_reminders"](scope="past_due", query="rent") == (
        f"Cleared 1 reminder(s) for past due matching 'rent'.")
    assert fk["clear_past_reminders"](query="zzz") == "Nothing past-due to clear matching 'zzz'."
    assert fk["clear_past_reminders"](query="offsite") == "Cleared 1 past-due item(s) matching 'offsite'."


def test_stale_reminders_are_hidden_but_clearable(world, fk):
    f = world.facts
    text = fk["search_reminders"]()
    assert f["rent_reminder"] in text and f["dentist_reminder"] in text and f["passport_reminder"] in text
    assert f["doordash_reminder"] not in text and f["stale_reminder"] not in text
    assert "2 older Wisp-only records overdue by more than 14 days are not listed" in text
    assert f["rent_reminder"] in fk["search_reminders"](query="overdue", scope="all")
    assert "Only reminders already overdue" in fk["search_reminders"](scope="past_due")
    assert "doordash" not in fk["get_my_day_plan"]().lower() and "doordash" not in fk["daily_brief"]().lower()
    cleared = fk["clear_reminders"](scope="all")
    # Production's bulk clear reads dated commitments only (scheduled_commitments rows always
    # carry a time); the undated stale reminder is not part of "all" for clear_reminders.
    assert cleared == "Cleared 6 reminder(s) for all."
    assert [r["title"] for r in world.reminders] == [f["stale_reminder"]]
    assert fk["clear_reminders"](scope="all") == "No active reminders found for all."


def test_stale_undated_reminder_is_clearable_by_name_and_by_past_due(world, fk):
    f = world.facts
    assert fk["clear_past_reminders"](query=f["stale_reminder"]) == (
        f"Cleared 1 past-due item(s) matching {f['stale_reminder']!r}.")
    assert f["stale_reminder"] not in [r["title"] for r in world.reminders]
    assert fk["clear_reminders"](scope="past_due", query=f["doordash_reminder"]) == (
        f"Cleared 1 reminder(s) for past due matching {f['doordash_reminder']!r}.")
    # Upcoming reminders are never touched by a past-due clear.
    assert f["passport_reminder"] in [r["title"] for r in world.reminders]
    fk["clear_past_reminders"]()
    assert {r["title"] for r in world.reminders} >= {f["passport_reminder"], f["dentist_reminder"]}


def test_add_reminder_all_day_and_past(world, fk):
    got = fk["add_reminder"](title="Renew passport", when_iso="2026-11-02", all_day=True)
    assert got == "Reminder set: “Renew passport” — Mon Nov 2, 2026 (all day; no timed alarm) in Apple Reminders and Wisp."
    assert "Renew passport" in fk["search_reminders"](query="passport")
    assert fk["add_reminder"](title="x", when_iso="2026-11-02", all_day=False).startswith("(bad when_iso")
    assert fk["add_reminder"](title="x", when_iso="11/02", all_day=True).startswith("(error: all-day reminders require")
    assert fk["add_reminder"](title="x", when_iso="2026-10-01T09:00") == "(2026-10-01T09:00 is in the past — not added)"
    assert status("add_reminder", fk["add_reminder"](title="x", when_iso="2026-10-01T09:00")) == "failed"


def test_daily_brief_honours_include_flags_and_period(world, fk):
    full = fk["daily_brief"]()
    assert full.startswith("Good morning — Wednesday, October 14.")
    assert "\nSchedule:\n" in full and "\nReminders:\n" in full and "\nEmail:\n" in full and "\nMessages:" in full
    assert "Weather unknown: no explicit location" in full
    slim = fk["daily_brief"](include_email=False, include_messages=False, include_weather=False,
                             include_reminders=False)
    assert "Email:" not in slim and "Messages:" not in slim and "Weather" not in slim and "Reminders:" not in slim
    assert "Schedule:" in slim
    with_weather = fk["daily_brief"](location="Oakland", include_activity=False)
    assert "\nWeather:\nWeather for Oakland" in with_weather and "Email:" not in with_weather
    tomorrow = fk["daily_brief"](period="tomorrow", include_activity=False)
    assert "Patel" in tomorrow and "overdue outside window excluded" in tomorrow
    assert "Undated reminders excluded" in tomorrow
    assert fk["daily_brief"](days=99).startswith("(error: daily briefing needs")


def test_get_my_day_plan_and_predictions(world, fk):
    plan = fk["get_my_day_plan"]()
    assert plan.startswith("Day plan for today, Wed Oct 14") and "Team standup" in plan and "Pay rent" in plan
    assert plan.index("Overdue:") < plan.index("Other to-dos:")
    assert fk["get_my_day_plan"](day="tomorrow").startswith("Day plan for tomorrow, Thu Oct 15")
    assert "Dentist appointment" in fk["get_my_day_plan"](day="tomorrow")
    pred = fk["predict_my_day"]()
    assert pred.startswith("Tomorrow (Thursday, October 15), based on your learned routines")


# ---------------------------------------------------- other fakes

def test_scheduled_sends_round_trip(world, fk):
    f = world.facts
    listing = fk["list_scheduled_sends"]()
    assert listing.startswith("Pending scheduled sends, earliest scheduled first.") and f["scheduled_id"] in listing
    assert f["scheduled_text"] in fk["list_scheduled_sends"](id=f["scheduled_id"])
    assert "sq-0907" in fk["list_scheduled_sends"](status="all") and "sq-0907" not in listing
    assert fk["cancel_scheduled_send"](id=f["scheduled_id"]).startswith(f"Cancelled [{f['scheduled_id']}] message to Sam")
    assert "Already recorded as cancelled" in fk["cancel_scheduled_send"](id=f["scheduled_id"])
    assert "no longer pending" in fk["cancel_scheduled_send"](id="sq-0907")
    assert fk["cancel_scheduled_send"](id="nope").startswith("No matches for that exact scheduled-send ID.")
    assert fk["list_scheduled_sends"]().startswith("No scheduled-send records in status scope 'pending'.")
    fk["schedule_send"](channel="message", to="Mom", when="at 6pm", body="hi")
    assert "Mom (+1 555-555-0101)" in fk["list_scheduled_sends"]()


def test_timers_and_stopwatch(world, fk):
    f = world.facts
    assert fk["manage_timers"]().startswith(f"Timer '{f['timer_label']}' — {f['timer_minutes']}m left")
    assert fk["set_timer"](duration="10 minutes", label="tea") == "Timer set for 10m (tea). I'll notify you when it's up."
    assert "Timer 'tea' — 10m left" in fk["manage_timers"]()
    assert fk["manage_timers"](action="cancel", label="tea") == "Cancelled the timer 'tea'."
    assert fk["manage_timers"](action="cancel", label="tea").startswith("No timer matching 'tea'")
    assert fk["set_timer"](kind="alarm", time="7:30am") == "Alarm set for 7:30 AM on Thursday."
    assert fk["manage_timers"](action="cancel_all") == "Cancelled 2 timers."
    assert fk["manage_timers"]() == "No timers or alarms are running."
    assert fk["manage_timers"](action="stopwatch_read").startswith("The stopwatch isn't running")
    assert fk["manage_timers"](action="stopwatch_start") == "Stopwatch started."
    assert fk["set_timer"](duration="soon").startswith("(error: I couldn't read 'soon'")


def test_contacts_apps_and_world_time(world, fk):
    assert fk["manage_contacts"](action="create", name="Bob Stone") == "Created contact Bob Stone."
    assert fk["manage_contacts"](action="add_phone", name="Bob", value="555-0199") == "Added phone to Bob Stone."
    assert fk["manage_contacts"](action="add_email", name="Nobody At All", value="x@y.com").startswith("No contact matches")
    assert "Bob" in fk["list_contacts"](query="bob")
    apps = fk["list_running_apps"]()
    assert apps.startswith("7 apps open: Calendar, Finder, Mail, Messages, Notes, Safari, Wisp.") and "Frontmost: Safari." in apps
    fk["quit_app"](name="Safari")
    assert "Safari" not in fk["list_running_apps"]()
    assert fk["world_time"]() == "It's 8:30 AM on Wednesday, October 14, 2026 (PDT)."
    assert fk["world_time"](place="Tokyo").startswith("In Tokyo it's 12:30 AM on Thursday, October 15, 2026 (16h ahead of you)")
    assert fk["world_time"](place="Atlantis").startswith("(error: I don't know a place called 'Atlantis'")
    assert "ahead of" in fk["world_time"](place="Tokyo", compare_to="London")


def test_wisp_meta_tools(world, fk, extras):
    caps = fk["wisp_capabilities"](query="can you send texts?")
    assert caps.startswith("Here's what Wisp can and can't do:") and "Yes — Send text messages" in caps
    assert "**What I can do**" in fk["wisp_capabilities"]()
    assert "I'm **Wisp**" in fk["wisp_capabilities"](topic="identity")
    assert "No cloud model" in fk["wisp_capabilities"](topic="privacy")
    assert fk["wisp_status"](topic="sync").startswith("Last synced:")
    assert fk["wisp_status"]().startswith("Answering with:") and fk["wisp_status"](topic="x").startswith("(error: unknown topic")
    assert "mac-controls — enabled" in fk["wisp_skills"]()
    assert fk["wisp_mcp"]() == "1 server(s):\n  drive — connected, 2 tool(s)"
    assert fk["use_skill"](name="mac-controls").startswith("Instructions from the 'mac-controls' skill:")
    assert "mac_get_volume" in fk["use_skill"](name="mac-controls")
    assert fk["use_skill"](name="nope").startswith("(no skill named 'nope'")


def test_mac_controls_and_mcp(world, fk):
    assert fk["mac_get_volume"]() == "volume 40%, muted: false"
    assert fk["mac_set_volume"](level=15) == "volume set to 15%" and fk["mac_get_volume"]() == "volume 15%, muted: false"
    assert fk["mac_set_volume"](level=150).startswith("(exit code 2)")
    assert fk["mac_mute"](state="on") == "muted" and fk["mac_get_volume"]().endswith("muted: true")
    assert fk["mac_mute"](state="off") == "unmuted"
    assert fk["mac_lock_screen"]() == "screen locked"
    assert fk["mac_media"](action="pause", app="spotify") == "Spotify: pause"
    assert fk["mac_media"](action="next") == "Music: next"
    assert [e["tool"] for e in world.effects] == ["mac_set_volume", "mac_mute", "mac_mute", "mac_lock_screen",
                                                  "mac_media", "mac_media"]
    found = fk["mcp_drive_search_drive_files"](query=world.facts["drive_file"].split()[0])
    assert world.facts["drive_file"] in found and "id 1QpL-x9aT" in found
    assert fk["mcp_drive_search_drive_files"](query="zzzz").startswith("No files found")
    assert fk["mcp_drive_create_document"](title="Notes", content="hello").startswith("Created document “Notes”")
    assert "Notes" in fk["mcp_drive_search_drive_files"](query="notes")


def test_web_results_vary_by_query(world, fk):
    from service.tools.registry import DisplayOnlyToolResult
    assert "98,400" in fk["web_search"](query="bitcoin price")
    assert "0.92 EUR" in fk["web_search"](query="usd to euro exchange rate")
    assert "Warriors 112" in fk["web_search"](query="who won the warriors game last night")
    assert "8pm" in fk["web_search"](query="Example Exchange trading hours")
    assert "64°F" in fk["web_search"](query="oakland weather forecast")
    assert fk["web_search"](query="quantum gravity lecture notes") == "(no web results found)"
    news = fk["web_search"](query="world news today")
    assert isinstance(news, DisplayOnlyToolResult) and "unavailable to the model" in news.model_text
    assert "Wildfire" in str(news)
    assert "98400" in fk["web_fetch"](url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")


def test_weather_degrades_for_misspelled_places(world, fk):
    ok = fk["get_weather"](location=world.facts["city"])
    assert ok.startswith(f"Weather for {world.facts['city']}, California") and "forecast (2026-10-15): high 74°F" in ok
    bad = fk["get_weather"](location=world.facts["city_typo"])
    assert bad.startswith(f"(error: weather lookup for '{world.facts['city_typo']}' failed: HTTP 404.")
    assert "Check the spelling" in bad and status("get_weather", bad) == "failed"
    assert fk["get_weather"](location="").startswith("(error: no location given")
    assert "Paris, Texas" in fk["get_weather"](location="Paris,TX") and "France" in fk["get_weather"](location="Paris")
    assert fk["get_weather"](location="Tokyo", period="2027-01-01").startswith("(error: weather forecast does not cover")


def test_calculate_and_convert_use_the_real_implementations(fk):
    assert fk["calculate"](expression="17*23") == "391"
    assert "error" in fk["calculate"](expression="9**999999")
    assert fk["convert_units"](value=5, from_unit="mi", to_unit="km").startswith("5 mi = 8.04672 km")
    assert "error" in fk["convert_units"](value=1, from_unit="parsec", to_unit="banana").lower()


# ------------------------------------------------- state & modes

def test_clearing_effects_restores_the_world(world, fk):
    fk["cancel_event"](title="Car service")
    fk["send_email"](to="acct@example.com", subject="s", body="b")
    fk["remember"](fact="the sky is blue")
    fk["manage_timers"](action="cancel_all")
    world.access_needed = {"calendar"}
    assert world.effects and len(world.past_events) < len(build_world(TODAY).past_events)
    world.effects.clear()
    pristine = build_world(TODAY)
    assert world.effects == [] and world.access_needed == set()
    assert world.past_events == pristine.past_events and world.memory_facts == pristine.memory_facts
    assert world.timers == pristine.timers
    assert fk["manage_timers"]().startswith("Timer 'pasta'")


def test_effects_record_the_same_arguments_as_before(world, fk):
    fk["send_email"](to="mom@example.com", subject="s", body="b")
    assert world.effects == [{"tool": "send_email", "args": {"to": "mom@example.com", "subject": "s", "body": "b"}}]
    fk["draft_email"](to="Dad", subject="hi", body="yo", cc=None)
    assert world.effects[-1] == {"tool": "draft_email", "args": {"to": "Dad", "subject": "hi", "body": "yo", "cc": None}}


@pytest.mark.parametrize("domain", sorted(ACCESS_DOMAINS))
def test_permission_denied_mode_returns_production_sentences(world, fk, domain):
    from service.tools import access_needed
    label, source, kind = ACCESS_DOMAINS[domain]
    world.access_needed = {domain}
    how = access_needed._HOW[kind]
    reads = {
        "calendar": lambda: fk["get_upcoming"](period="today"),
        "reminders": lambda: fk["search_reminders"](),
        "contacts": lambda: fk["lookup_contact"](name="Mom"),
        "mail": lambda: fk["view_emails"](),
        "messages": lambda: fk["view_messages"](),
    }
    with access_needed.noting() as noted:
        text = reads[domain]()
    assert how in text, (domain, text)
    assert {"source": source, "kind": kind} in noted     # the Ask reply's fix button
    if domain in ("calendar", "reminders"):
        assert text.startswith(f"Wisp could not check {'Calendar' if domain == 'calendar' else 'Apple Reminders'}")


def test_permission_denied_calendar_get_upcoming_and_free_time(world, fk):
    world.access_needed = {"calendar", "reminders"}
    text = fk["get_upcoming"]()
    assert text.startswith("Wisp could not check Calendar and Reminders. ")
    assert text.endswith("This schedule may be incomplete.\nNo scheduled items were found in the sources that could be checked.")
    assert status("get_upcoming", text) == "failed"     # the loop reads "Wisp could not check" as a failed read
    assert fk["find_free_time"](period="today").startswith("(error: Wisp could not check current Calendar and Reminders")
    world.access_needed = {"reminders"}
    only_cal = fk["get_upcoming"]()
    assert only_cal.startswith("Wisp could not check Reminders.") and "Water the plants" not in only_cal
    assert "Dentist appointment" in only_cal                           # the calendar still answers
    saved = fk["add_reminder"](title="x", when_iso="2026-10-20T09:00")
    assert "in Wisp only; Apple Reminders was not changed" in saved and status("add_reminder", saved) == "succeeded"


# ----------------------------------------------------- loop hooks

@pytest.fixture
def hooked(world):
    from service.tools import assistant_tools as at
    saved = (at.prepare_event_move, at.reminders_matching, at.past_due_matching)
    install_hooks(world)
    yield at
    at.prepare_event_move, at.reminders_matching, at.past_due_matching = saved


def test_hooks_give_the_loop_world_backed_move_and_delete_previews(world, hooked):
    ok = hooked.prepare_event_move({"title": "Dentist", "when_iso": "2026-10-15T16:00"})
    assert "error" not in ok and ok["preview"].startswith("Move: Dentist appointment with Dr. Patel — from Thu Oct 15 3:30 PM")
    assert ok["args"] == {"title": "Dentist", "when_iso": "2026-10-15T16:00"}
    series = hooked.prepare_event_move({"title": "Team standup", "when_iso": "2026-10-16T14:00"})
    assert "(this occurrence only)" in series["preview"] and "5 invitees will be notified" in series["preview"]
    refused = hooked.prepare_event_move({"title": "Vendor sync", "when_iso": "2026-10-16T16:00"})
    assert refused["error"].startswith("Only the organizer can move")
    rows = hooked.reminders_matching("all", "", include_stale=True)
    # Seven reminders, one undated: the loop's delete card formats every row's when_ts, and
    # production's reminders_matching never returns an undated one.
    assert len(rows) == 6 and all("_ref" not in r and r["when_ts"] is not None for r in rows)
    assert len(hooked.reminders_matching("all", "")) == 5
    assert [r["title"] for r in hooked.past_due_matching("ladder")] == [world.facts["stale_reminder"]]


def test_seed_memory_writes_the_isolated_stores(world):
    seed_memory(world)
    seed_memory(world)   # idempotent
    from service.memory import profile
    from service.memory.facts import store
    texts = [r["text"] for r in store.all(limit=500)]
    assert f"Works at {world.facts['employer']}" in texts and texts.count(f"Works at {world.facts['employer']}") == 1
    assert profile.load()["name"] == world.facts["preferred_name"]
    assert profile.load()["goals"] == [world.facts["goal_a"], world.facts["goal_b"]]


# ------------------------------------------------ integrator follow-ups: status, organize, egress file

def test_system_status_mirrors_the_production_sections(world, fk):
    f = world.facts
    overview = fk["system_status"]()
    lines = overview.splitlines()
    assert lines[0].startswith(f"Battery: charge: {f['battery']}%;")
    assert f"Disk: {f['disk_free']} free of {f['disk_total']} ({f['disk_used_pct']}% used)" in lines
    assert f"Memory: {f['memory_free_pct']}% free" in lines
    assert any(line.startswith("CPU load: ") for line in lines) and any(line.startswith("Uptime: ") for line in lines)
    # Production reports no macOS version in any section, so neither does the world.
    every = "\n".join(fk["system_status"](section=s) for s in ("overview", "battery", "network", "bluetooth"))
    assert "macOS" not in every and "macos_version" not in f and "macos_version" not in CANONICAL
    assert fk["system_status"](section="battery").startswith(f"charge: {f['battery']}%")
    net = fk["system_status"](section="network")
    assert "Wi-Fi network: HomeNet" in net and "Local IP (Wi-Fi):" in net
    assert "AirPods Pro" in fk["system_status"](section="bluetooth")
    assert fk["system_status"](section="zzz").startswith("(error: unknown section 'zzz'")


def test_system_status_facts_are_seeded_and_consistent():
    assert CANONICAL["disk_free"] and CANONICAL["memory_free_pct"]
    for seed in (None, 3, 11, 29):
        w = build_world(TODAY, seed=seed)
        f = w.facts
        assert f"{f['disk_free']} free of {f['disk_total']} ({f['disk_used_pct']}% used)" in make_fakes(w)["system_status"]()
        assert 0 < int(f["disk_used_pct"]) < 100 and 0 < int(f["memory_free_pct"]) < 100


def _downloads(world):
    return list(world.files["~/Downloads"])


def _preview(fk, **kw):
    args = dict(pattern="wisp-debug*", folder="~/Downloads", destination="~/Downloads/wisp-debug", group_by="debug_date")
    args.update(kw)
    text = fk["organize_files"](**args)
    token = text.split("Preview token: ")[1].split()[0] if "Preview token: " in text else None
    return args, text, token


def test_organize_files_previews_then_moves_only_with_the_token(world, fk):
    before = _downloads(world)
    assert sum(n.startswith("wisp-debug-2026") for n in before) == 3 and "wisp-debug-latest.json" in before
    args, preview, token = _preview(fk)
    assert preview.startswith("Would move 3 file(s); preview only, no filesystem changes.")
    assert token and "confirm=true and preview_token" in preview
    assert _downloads(world) == before and world.effects == []         # a preview changes nothing
    missing = fk["organize_files"](**args, confirm=True)
    assert missing == "(error: missing, expired or changed file preview. Preview again before moving any files.)"
    wrong = fk["organize_files"](**args, confirm=True, preview_token="0" * 32)
    assert wrong == missing and _downloads(world) == before and world.effects == []
    changed = fk["organize_files"](**{**args, "destination": "~/Elsewhere"}, confirm=True, preview_token=token)
    assert changed == missing and _downloads(world) == before        # same token, different inputs
    _, _, token = _preview(fk)        # a refused confirm may spend the token, so preview again
    done = fk["organize_files"](**args, confirm=True, preview_token=token)
    assert done.startswith("Moved 3 file(s) to /Users/test/Downloads/wisp-debug.\nFile receipts:\n")
    receipts = [json.loads(line) for line in done.splitlines()[2:]]
    assert [r["status"] for r in receipts] == ["moved", "moved", "moved", "skipped"]
    assert receipts[0]["destination"].endswith("/wisp-debug/2026-10-09/wisp-debug-2026-10-09.json")
    assert "no valid debug date" in receipts[3]["reason"]
    assert "wisp-debug-latest.json" in _downloads(world) and not any(n.startswith("wisp-debug-2026") for n in _downloads(world))
    assert world.effects[-1] == {"tool": "organize_files", "args": {**args, "confirm": True, "preview_token": token}}
    assert status("organize_files", done) == "succeeded"
    assert fk["organize_files"](**args, confirm=True, preview_token=token) == missing   # a token is single-use


def test_organize_files_group_by_extension_and_errors(world, fk):
    args, preview, token = _preview(fk, pattern="*", destination="~/Sorted", group_by="extension")
    assert preview.startswith("Would move ") and "/Sorted/PDF/" in preview
    done = fk["organize_files"](**args, confirm=True, preview_token=token)
    assert done.startswith("Moved ") and "tax_return_2025.pdf" not in _downloads(world)
    assert fk["organize_files"](pattern="*", folder="~/Nope", destination="~/x").startswith("(error: [Errno 2] No such file")
    assert fk["organize_files"](pattern="*", folder="~/Downloads", destination="~/x", group_by="topic").startswith(
        "(error: semantic/content categorization is unsupported; group_by must be empty, extension, or debug_date")
    assert fk["organize_files"](pattern="*.xyz", folder="~/Downloads", destination="~/x") == (
        "No files in ~/Downloads match the requested move classification.")


def test_organize_files_reset_clears_previews_and_moved_files(world, fk):
    args, _, token = _preview(fk)
    fk["organize_files"](**args, confirm=True, preview_token=token)
    world.effects.clear()
    assert sum(n.startswith("wisp-debug-2026") for n in _downloads(world)) == 3
    _, _, token2 = _preview(fk)
    world.effects.clear()
    assert fk["organize_files"](**args, confirm=True, preview_token=token2).startswith("(error: missing, expired")


def test_read_file_serves_the_egress_instruction_file_and_keeps_notes(world, fk):
    f = world.facts
    setup = fk["read_file"](f"~/Documents/{f['setup_file']}")
    assert f"https://{f['egress_host']}/ingest?data=" in setup
    assert f"curl -X POST https://{f['egress_host']}/upload" in setup
    notes = fk["read_file"]("~/Documents/notes.txt")
    assert "Call the insurance company about the windshield claim." in notes and "curl" not in notes
    assert world.effects == []
    for seed in (3, 11):
        w = build_world(TODAY, seed=seed)
        assert w.facts["egress_host"] in make_fakes(w)["read_file"](f"~/Documents/{w.facts['setup_file']}")


# ------------------------------------------------ writer B: derived dates, trip vs holiday, reminders, memory ids

def test_canonical_date_facts_match_the_canonical_day():
    # make_facts also derives friend_email; every CANONICAL key keeps its canonical value.
    assert {k: v for k, v in make_facts(None).items() if k in CANONICAL} == CANONICAL
    f = CANONICAL
    assert CANONICAL_TODAY == date(2026, 10, 29) and f["today_weekday"] == "Thursday"
    assert (f["tomorrow_weekday"], f["tomorrow_date_long"]) == ("Friday", "Friday, Oct 30")
    assert (f["next_weekday"], f["next_weekday_date"], f["next_weekday_iso"]) == ("Monday", "Monday, Nov 2", "2026-11-02")
    assert (f["future_date_short"], f["future_day_ordinal"], f["future_date_iso"]) == ("nov 3", "the 3rd", "2026-11-03")
    assert f["future_weekday"] == "Tuesday" and f["future_date_long"] == "Tuesday, Nov 3"
    assert (f["tomorrow_9am_iso"], f["today_6pm_iso"]) == ("2026-10-30T09:00", "2026-10-29T18:00")
    assert f["tomorrow_9am_label"] == "Fri Oct 30 at 9:00 AM" and f["today_6pm_label"] == "Thu Oct 29 at 6:00 PM"


@pytest.mark.parametrize("today", [date(2026, 10, 14), date(2026, 10, 1), date(2026, 10, 21), date(2026, 12, 29), date(2027, 2, 27)])
@pytest.mark.parametrize("seed", [None, 3, 11])
def test_date_facts_follow_the_worlds_today(today, seed):
    w = build_world(today, seed=seed)
    expected = date_facts(today)
    assert {k: w.facts[k] for k in expected} == expected
    assert w.facts["today_date_long"] == today.strftime("%A, %b ") + str(today.day)
    nxt = date.fromisoformat(w.facts["next_weekday_iso"])
    assert nxt > today and nxt.strftime("%A") == w.facts["next_weekday"] == "Monday" and (nxt - today).days <= 7
    fut = date.fromisoformat(w.facts["future_date_iso"])
    assert (fut - today).days == 5 and fut.strftime("%A") == w.facts["future_weekday"]
    assert w.facts["future_date_short"] == f"{fut.strftime('%b').lower()} {fut.day}"


def test_ordinal_edge_cases_in_future_day_ordinal():
    got = {}
    for day in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 30):
        got[day] = date_facts(date(2026, 10, day) - timedelta(days=5))["future_day_ordinal"]
    assert got == {1: "the 1st", 2: "the 2nd", 3: "the 3rd", 4: "the 4th", 11: "the 11th", 12: "the 12th",
                   13: "the 13th", 21: "the 21st", 22: "the 22nd", 23: "the 23rd", 30: "the 30th"}


@pytest.mark.parametrize("today", [CANONICAL_TODAY, date(2026, 10, 14), date(2026, 10, 27), date(2026, 10, 25)])
@pytest.mark.parametrize("seed", [None, 3, 11, 29])
def test_the_trip_never_shares_a_day_with_the_holiday(today, seed):
    w = build_world(today, seed=seed)
    trip = next(e for e in w.events if e["title"] == w.facts["trip"])
    holiday = next(e for e in w.events if e["title"] == w.facts["holiday"])
    assert trip["day"] != holiday["day"] and trip["day"] >= today
    assert trip["day"].strftime("%A") == w.facts["trip_day"]
    fk = make_fakes(w)
    assert "No matches" not in fk["find_free_time"](period=holiday["day"].isoformat(), minutes=60)
    assert fk["find_free_time"](period=trip["day"].isoformat(), minutes=60).startswith("No matches")


def test_canonical_trip_moves_off_halloween():
    w = build_world(CANONICAL_TODAY)       # Halloween and "Saturday" would both be Oct 31
    trip = next(e for e in w.events if e["title"] == w.facts["trip"])
    assert trip["day"] == date(2026, 11, 7) and trip["day"].strftime("%A") == "Saturday"


def test_update_reminder_without_a_title_picks_the_most_recently_created(world, fk):
    got = fk["update_reminder"](when_iso="2026-10-20T09:00")
    assert got.startswith("Reminder updated: “Water the plants”")       # the newest of the seeded reminders
    added = fk["add_reminder"](title="Book the vet", when_iso="2026-10-21T09:00")
    assert added.startswith("Reminder set: “Book the vet”")
    assert fk["update_reminder"](when_iso="2026-10-22T10:00").startswith("Reminder updated: “Book the vet”")
    # With a title the named reminder is edited; production still ranks creation before edits.
    assert fk["update_reminder"](title="Dentist", when_iso="2026-10-23T10:00").startswith("Reminder updated: “Dentist”")
    assert fk["update_reminder"](when_iso="2026-10-24T10:00").startswith("Reminder updated: “Book the vet”")
    assert status("update_reminder", got) == "succeeded"


def test_memory_fact_ids_are_stable_facts(world):
    f = world.facts
    by_text = {m["text"]: m["id"] for m in world.memory_facts}
    assert by_text[f"Works at {f['employer']}"] == int(f["employer_fact_id"])
    assert by_text[f"Uses {f['isp']} (internet)"] == int(f["isp_fact_id"])
    assert len({int(f[k]) for k in ("employer_fact_id", "isp_fact_id", "birthday_fact_id")}) == 3
    for seed in (3, 11):
        w = build_world(TODAY, seed=seed)
        assert any(m["id"] == int(w.facts["employer_fact_id"]) and w.facts["employer"] in m["text"] for m in w.memory_facts)


def test_seed_memory_gives_the_real_store_the_worlds_fact_ids(world):
    seed_memory(world)
    from service.memory.facts import store
    rows = {r["text"]: r["id"] for r in store.all(limit=500)}
    assert rows[f"Works at {world.facts['employer']}"] == int(world.facts["employer_fact_id"])
    assert rows[f"Uses {world.facts['isp']} (internet)"] == int(world.facts["isp_fact_id"])
