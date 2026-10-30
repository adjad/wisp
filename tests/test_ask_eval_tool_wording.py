"""Tool wording the 2026-10-29 Ask run showed the model misreading.

"show me what's pending" went to list_scheduled_sends (the send queue), and
"what are my open tasks" / "what are my school tasks" never reached the
reminders, ending in "I don't have that". The model picks tools by their
descriptions, so to-dos and tasks are named where they live.
"""
from service.tools import short_descriptions
from service.tools.registry import REGISTRY


def test_reminders_own_todos_tasks_and_pending():
    tool = REGISTRY["search_reminders"]
    for word in ("to-dos", "tasks", "pending", "overdue"):
        assert word in tool.description
    assert "query" not in tool.parameters.get("required", [])
    assert "empty query lists all" in short_descriptions.SHORT["search_reminders"]


def test_the_send_queue_points_to_dos_elsewhere():
    tool = REGISTRY["list_scheduled_sends"]
    assert tool.description.startswith("Only emails/texts the user scheduled")
    assert "search_reminders" in tool.description
    assert "not to-dos" in short_descriptions.SHORT["list_scheduled_sends"]


def test_a_catch_all_query_lists_every_reminder(monkeypatch):
    from service.tools import assistant_tools as A
    rows = [{"title": "Pay rent", "source": "reminders", "when_ts": 1},
            {"title": "Call the dentist", "source": "reminders", "when_ts": 2}]
    monkeypatch.setattr(A.assistant_store, "active_between", lambda *a, **k: list(rows))
    for q in ("all", "*", "everything", "tasks", " ALL "):
        assert [r["title"] for r in A.reminders_matching("all", q)] == ["Pay rent", "Call the dentist"]
    assert [r["title"] for r in A.reminders_matching("all", "rent")] == ["Pay rent"]


def test_voice_questions_point_to_wisps_own_shortcut():
    from service.model_only import ASK_CAPABILITY_HINT
    assert "⌥⇧Space" in ASK_CAPABILITY_HINT and "never send the user to Siri" in ASK_CAPABILITY_HINT


import pytest  # noqa: E402

from service import model_only as mo  # noqa: E402


@pytest.mark.parametrize("prompt", ["how much free disk space?", "how much battery is left",
                                    "how much storage do I have left?"])
def test_the_macs_own_state_is_never_answered_from_knowledge(prompt):
    """"how much free disk space?" was offered no tools and answered generically."""
    assert not mo.knowledge_question(prompt)


@pytest.mark.parametrize("prompt", ["how many hours of sleep should I get?",
                                    "how much water should I drink a day?"])
def test_general_advice_is_knowledge_not_the_users_data(prompt):
    """Re-prompted for a personal tool, this ended in "I don't have that"."""
    assert mo.knowledge_question(prompt) and not mo.asks_personal_data(prompt)


@pytest.mark.parametrize("prompt", ["what should I wear today?", "how much should I study for my midterm?",
                                    "How do I get to the airport from here?"])
def test_advice_that_needs_the_users_day_or_place_keeps_its_tools(prompt):
    assert not mo.knowledge_question(prompt)


def test_do_you_know_my_birthday_asks_memory_not_capability():
    assert not mo.capability_question("do you know my birthday?")
    assert mo.asks_personal_data("do you know my birthday?")
    assert mo.capability_question("can you read my email?")


def test_a_summary_asked_for_words_becomes_a_keyword_search(monkeypatch):
    """summarize_emails(query="bank") failed on an unexpected argument six times
    in the Ask run; it now hands the search to view_emails with the same scope."""
    import asyncio

    from service.tools import email_tools as E
    seen = []

    async def fake_view(query, day, count, account, period, unread, strict_match=False):
        seen.append((query, day, count, account, period, unread))
        return "2 emails matched 'bank'."

    async def no_digest(*a, **k):
        raise AssertionError("the digest must not run for a keyword search")

    monkeypatch.setattr(E, "view_emails_impl", fake_view)
    monkeypatch.setattr(E, "_ensure_email_cache", no_digest)
    out = asyncio.run(E.summarize_emails(query=" bank ", period="this week"))
    assert out == "2 emails matched 'bank'."
    assert seen == [("bank", None, 20, None, "this week", False)]
    assert "query" in REGISTRY["summarize_emails"].parameters["properties"]


def test_latest_texts_go_to_view_messages_not_the_important_digest():
    """"who texted me today?" got "1 message in 3 days" from the important-only
    digest while dozens of texts had arrived (2026-10-29 Ask run)."""
    tool = REGISTRY["summarize_messages"]
    assert "use view_messages instead" in tool.description
    assert "view_messages" in short_descriptions.SHORT["summarize_messages"]


@pytest.mark.parametrize("prompt,lead", [
    ("when is my midterm? pls", "get_upcoming"),
    ("do I have class tomorrow?", "get_upcoming"),
    ("what r my open tasks", "search_reminders"),
    ("what are my school tasks?", "search_reminders"),
])
def test_dated_events_and_todos_lead_with_their_source(prompt, lead):
    """2026-10-29 Ask run: "when is my midterm" was offered no calendar tool and
    "my school tasks" lost Reminders to Google Tasks; both ended in "I don't have that"."""
    from service import main
    assert main._ask_tool_rank(prompt)[0][0] == lead


def test_general_and_write_requests_keep_their_head_start():
    from service import main
    assert not mo.calendar_first("When is the next solar eclipse?")
    assert not mo.reminders_first("what is a to-do list app?")
    assert main._ask_tool_rank("remind me to do homework")[0][0] == "add_reminder"


def test_overdue_and_due_soon_filter_by_due_date(monkeypatch):
    """dev8 audit: "overdue" as the query listed every reminder, and the model
    presented the whole list as overdue."""
    import asyncio, time
    from service.tools import assistant_tools as A
    now = time.time()
    rows = [{"title": "Pay rent", "source": "reminders", "when_ts": now - 86400},
            {"title": "Call the dentist", "source": "reminders", "when_ts": now + 2 * 86400},
            {"title": "Renew passport", "source": "reminders", "when_ts": now + 40 * 86400}]

    def between(start, end, include_stale=False):
        return [r for r in rows if (start is None or r["when_ts"] >= start)
                and (end is None or r["when_ts"] < end)]

    async def ready(*a, **k):
        return {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": True}
    monkeypatch.setattr(A.assistant_store, "active_between", between)
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", ready)
    monkeypatch.setattr(A, "_undated_report", lambda q, s: ("", ""))
    overdue = asyncio.run(A.search_reminders("overdue"))
    assert "Pay rent" in overdue and "dentist" not in overdue and "passport" not in overdue
    assert "Only reminders already overdue" in overdue
    soon = asyncio.run(A.search_reminders("due soon"))
    assert "dentist" in soon and "Pay rent" not in soon and "passport" not in soon
    assert "next 7 days" in soon
    pending = asyncio.run(A.search_reminders("pending"))
    assert all(t in pending for t in ("Pay rent", "dentist", "passport"))
    rows[:] = rows[1:]
    assert "no overdue reminders" in asyncio.run(A.search_reminders("overdue"))


@pytest.mark.parametrize("prompt", ["What is the RAM in a computer for?", "How do I take a screenshot on macOS?",
                                    "What is a CPU?", "how do I change the volume on a Mac?",
                                    "why does battery capacity drop over time?"])
def test_general_device_questions_stay_knowledge(prompt):
    """dev8 audit: any device word sent these down the tool path."""
    assert mo.knowledge_question(prompt)


@pytest.mark.parametrize("prompt", ["how much RAM is free", "what's my disk space",
                                    "how much battery do I have", "what macOS version am I on"])
def test_device_state_phrasing_keeps_tools(prompt):
    assert not mo.knowledge_question(prompt)


def test_reminder_reads_carry_no_internal_verification_note():
    """Ask eval 2026-10-29: "Deletion status is not independently verified" was
    read back to the user verbatim. The tools' own text no longer carries it."""
    import inspect
    from service.tools import assistant_tools
    assert "not independently verified" not in inspect.getsource(assistant_tools)
