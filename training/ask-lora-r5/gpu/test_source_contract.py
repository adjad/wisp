"""The answer checks of the 2026-11-07 audit, with the failures it found as negative tests. No model, no service.

    python -m pytest --noconftest --rootdir=. -q test_source_contract.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import source_contract as S  # noqa: E402

CLOCK = "The current date and time is Wednesday, February 17, 2027 at 10:30 AM. Resolve relative dates/times against this."
RESULTS = ["Next 30 days: 1 event\n\n**Monday · Feb 22**\n- **3:30 PM** — Dentist cleaning · Brightside Dental",
           "Subject: Dentist cleaning reminder\nHi Sam, your dentist visit with Dr. Ahn is Monday at 3:30 PM. Please bring your insurance card. The fee is $40.",
           "Title: Health stuff [Notes]\nDentist: ask about the night guard.",
           "No messages matching 'dentist' were found in your recent messages."]
GOLD = {"kind": "all_sources", "shape": "topic", "hit": {"calendar": ["Dentist cleaning", "3:30 PM"], "mail": ["Dr. Ahn"], "notes": ["night guard"]},
        "empty": ["messages"], "read": ["calendar", "mail", "notes", "messages"], "events": [{"title": "Dentist cleaning", "day": "Monday", "time": "3:30 PM"}],
        "failed": {}, "conflict": None, "prohibit": ["bring a book"], "all_of": [], "must_say_absent": False}
GOOD = ("Your **dentist cleaning** is Monday at 3:30 PM at Brightside Dental with Dr. Ahn, and the email says to bring your insurance card "
        "and that the fee is $40. Your notes also say to ask about the night guard. Nothing about it in your texts.")


def msgs(results=RESULTS, prompt="anything about the dentist"):
    out = [{"role": "system", "content": CLOCK}, {"role": "user", "content": prompt},
           {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "x", "arguments": {}}} for i in range(len(results))]}]
    return out + [{"role": "tool", "tool_call_id": f"c{i}", "content": r} for i, r in enumerate(results)]


def keys(text, gold=GOLD, m=None, meta=None):
    return {k for k, _ in S.final_problems(text, {"gold": gold, **(meta or {})}, m or msgs())}


def test_correct_answer_passes():
    assert keys(GOOD) == set()


def test_paraphrase_passes():
    text = ("You're seeing Dr. Ahn for a cleaning on Monday, at 3:30pm. Don't forget your insurance card! You also wrote down a question "
            "about a night guard in your notes.")
    assert keys(text) == set()


def test_the_audit_probe_fails():
    got = keys("Everything looks good. You are all set.")
    assert {"source:missing:calendar", "source:missing:mail", "source:missing:notes"} <= got
    assert "source:generic" in got and "fact:invented_action" in got


def test_missing_one_source_fails():
    text = "Your dentist cleaning is Monday at 3:30 PM with Dr. Ahn."
    assert keys(text) == {"source:missing:notes"}


def test_wrong_amount_fails():
    assert "fact:wrong_amount" in keys(GOOD.replace("$40", "$75"))


def test_derived_amount_passes():
    assert "fact:wrong_amount" not in keys(GOOD + " For two visits that's $80.")


def test_wrong_time_fails():
    got = keys(GOOD.replace("3:30 PM", "4:30 PM"))
    assert "fact:wrong_time" in got or "source:wrong_event_time" in got


def test_wrong_weekday_fails():
    assert "fact:wrong_weekday" in keys(GOOD.replace("Monday", "Tuesday"))


def test_wrong_day_that_exists_elsewhere_fails():
    # tomorrow is Thursday, so the weekday is a known one, but it isn't this event's day
    assert "source:wrong_event_day" in keys(GOOD.replace("Monday", "Thursday"))


def test_wrong_date_fails():
    assert "fact:wrong_date" in keys(GOOD.replace("Monday at", "March 30 at"))


def test_mismatched_weekday_and_date_fails():
    assert "fact:weekday_date_mismatch" in keys(GOOD.replace("Monday at", "Monday, February 23, at"))


def test_right_worked_out_date_passes():
    assert keys(GOOD.replace("Monday at", "Monday, February 22, at")) == set()


def test_invented_action_fails():
    assert "fact:invented_action" in keys(GOOD + " I've added it to your reminders.")
    assert "fact:invented_action" in keys(GOOD + " I went ahead and emailed them.")


def test_real_write_may_report_it():
    assert "fact:invented_action" not in keys(GOOD + " I've added it to your reminders.", meta={"turn_writes": ["add_reminder"]})


def test_absence_that_contradicts_a_hit_fails():
    assert "source:absence_contradicts_hit" in keys(GOOD + " There are no emails about it.")


def test_blanket_absence_with_hits_fails():
    assert "source:blanket_absence" in keys(GOOD + " Otherwise I couldn't find anything.")


def test_honest_empty_source_passes():
    assert keys(GOOD) == set()      # "Nothing about it in your texts": texts were read and were empty


def test_absence_about_an_unread_source_fails():
    gold = dict(GOLD, read=["calendar", "mail", "notes"], hit=GOLD["hit"])
    assert "source:absence_unread" in keys(GOOD.replace("in your texts", "in your texts"), gold=gold)


def test_nothing_shape_must_say_so():
    gold = dict(GOLD, hit={}, empty=["mail", "messages"], read=["mail", "messages"], events=[], must_say_absent=True, shape="nothing")
    m = msgs(["No emails matching 'x' were found.", "No messages matching 'x' were found."])
    assert keys("I looked in your email and texts and didn't find anything on that.", gold, m) == set()
    assert "source:absence_not_stated" in keys("Sure, happy to help with that whenever you like.", gold, m)


def test_invented_fact_on_a_miss_fails():
    gold = dict(GOLD, hit={}, empty=["mail"], read=["mail"], events=[], must_say_absent=True)
    m = msgs(["No emails matching 'x' were found."])
    assert "fact:wrong_amount" in keys("I didn't find it in your email, but it's about $40 on Monday.", gold, m)


def test_fact_from_an_unread_source_fails():
    assert "source:unread_fact" in keys(GOOD + " Theo says to bring a book.")


def test_conflict_needs_both_and_the_new_one():
    res = ["Next 30 days: 1 event\n\n**Monday · Feb 22**\n- **3:30 PM** — Dentist cleaning", "Quick update: your dentist on Monday has moved from 3:30 PM to 5:00 PM."]
    gold = dict(GOLD, shape="conflict", hit={"calendar": ["3:30 PM"], "mail": ["5:00 PM"]}, empty=[], read=["calendar", "mail"],
                conflict={"old": "3:30 PM", "new": "5:00 PM"}, prohibit=[])
    m = msgs(res)
    assert keys("Your calendar still shows Monday at 3:30 PM, but an email says it moved to 5:00 PM.", gold, m) == set()
    assert "source:conflict_missing_new" in keys("Your dentist cleaning is Monday at 3:30 PM.", gold, m)
    assert "source:conflict_missing_old" in keys("Your dentist cleaning moved to Monday at 5:00 PM.", gold, m)
    assert "source:conflict_stale" in keys("It's Monday at 3:30 PM. An email mentions 5:00 PM, but there's no change.", gold, m)


def test_failed_read_has_to_be_said():
    res = ["Wisp could not check Calendar. Turn on Calendar access in System Settings.", "Subject: x\nYour dentist visit with Dr. Ahn."]
    gold = dict(GOLD, shape="failed", hit={"mail": ["Dr. Ahn"]}, empty=[], read=["mail"], failed={"calendar": "Calendar access is off"}, events=[], prohibit=[])
    m = msgs(res)
    assert keys("Dr. Ahn emailed about the visit. I couldn't check your calendar because its access is off.", gold, m) == set()
    assert "source:failed_hidden:calendar" in keys("Dr. Ahn emailed about the visit.", gold, m)
    assert "source:failed_as_absence" in keys("Dr. Ahn emailed about the visit. There are no events on your calendar.", gold, m)


def test_all_of_needs_each_item():
    gold = dict(GOLD, hit={}, read=["calendar"], empty=[], all_of=["Statistics review session", "Campus tour shift"], events=[], prohibit=[])
    m = msgs(["Friday: 2 events\n- 1:00 PM — Statistics review session\n- 4:00 PM — Campus tour shift"])
    assert keys("You have a statistics review session at 1:00 PM and a campus tour shift at 4:00 PM.", gold, m) == set()
    assert "source:missing_item" in keys("You have a statistics review session at 1:00 PM.", gold, m)


# ---- chat, memory
CHAT = {"kind": "chat_contrast", "forbid": r"\b(?:nothing (?:is )?saved|our memory)\b"}
MEM = lambda mode, **kw: {"kind": "memory_contrast", "mode": mode, "facts": [], **kw}  # noqa: E731
PLAIN = [{"role": "system", "content": CLOCK}, {"role": "user", "content": "rough one today"}]


def test_chat_must_not_become_a_memory_lookup():
    assert keys("I'm sorry, that sounds exhausting. What happened?", CHAT, PLAIN) == set()
    assert "chat:memory_talk" in keys("Our memory has nothing saved for that.", CHAT, PLAIN)
    assert "memory:talk_in_chat" in keys("Nothing is saved about your new hobby yet.", MEM("chat"), PLAIN)
    assert "memory:talk_in_chat" in keys("We haven't talked about that in this chat.", MEM("chat"), PLAIN)


def test_fact_in_the_chat_is_answered_from_the_chat():
    gold = MEM("in_chat", facts=["Mudroom"])
    assert keys("That was Mudroom, the one on Alder street.", gold, PLAIN) == set()
    assert "memory:missing_fact" in keys("I'm not sure which one you mean.", gold, PLAIN)
    assert "memory:talk_in_chat" in keys("Mudroom, though nothing is saved about it.", gold, PLAIN)


def test_real_memory_questions():
    hit = MEM("saved_hit", facts=["Thursday"])
    miss = MEM("saved_miss", prohibit=[])
    m = msgs(["No matching memory or user conversation passage."], "do you have anything saved about my kiln rental")
    assert keys("Your class meets on Thursday evenings.", hit, PLAIN) == set()
    assert "memory:missing_fact" in keys("Yes, there's a note about your class.", hit, PLAIN)
    assert keys("I don't have anything saved about a kiln rental.", miss, m) == set()
    assert "memory:absence_not_stated" in keys("Your kiln rental sounds fun!", miss, m)


def test_no_contract_means_only_the_generic_layer_runs():
    assert S.final_problems("Rough day, I hear you.", {"family": "x"}, PLAIN) == []
    m = msgs(["Subject: y\nYour balance is $12."])
    assert [k for k, _ in S.final_problems("Your balance is $99.", {"family": "x"}, m)] == ["fact:wrong_amount"]


def test_echo_measure():
    assert S.echo_score("the dentist is on monday", "the dentist is on monday") == 1.0
    assert S.echo_score("thinking about times", "unrelated zebra words") < 0.5
    assert S.trace_shape("", "x")["empty"] is True


def test_a_plural_weekday_in_a_result_supports_the_day():
    m = msgs(['[memory 11] "Sam plays chess at the Linden club on Tuesdays."'])
    assert "fact:wrong_weekday" not in {k for k, _ in S.final_problems("That's Tuesday.", {"family": "x"}, m)}
    assert "fact:wrong_weekday" in {k for k, _ in S.final_problems("That's Friday.", {"family": "x"}, m)}


def test_an_abbreviated_title_stays_with_its_day():
    events = [{"title": "Mt. Tabor hike", "day": "Tuesday", "time": "9:00 AM"}]
    probs = S._event_problems("On Wednesday you have Mt. Tabor hike at 9:00 AM.", events, None, False)
    assert [k for k, _ in probs] == ["source:wrong_event_day"]
    assert S._event_problems("On Tuesday you have Mt. Tabor hike at 9:00 AM.", events, None, False) == []


def _wc(mode, facts=()):
    return {"kind": "write_contrast", "mode": mode, "facts": list(facts)}


def test_nothing_may_be_claimed_on_the_other_side_of_a_write():
    for mode in ("word", "read", "clarify", "remark"):
        assert "write:invented_action" in keys("Sure thing. I've sent it to Maya?", _wc(mode), PLAIN), mode
    assert keys("You could say: hey, the group moved rooms.", _wc("word"), PLAIN) == set()
    assert keys("That sounds like a lot to keep track of.", _wc("remark"), PLAIN) == set()


def test_a_draft_is_called_a_draft_and_never_sent():
    assert keys("Your draft is ready, it hasn't gone out.", _wc("draft"), PLAIN) == set()
    assert "write:claims_sent" in keys("Your draft has been sent to Nadia.", _wc("draft"), PLAIN)
    assert "write:draft_not_stated" in keys("Here it is for you to look over.", _wc("draft"), PLAIN)


def test_a_clarifying_turn_asks():
    assert keys("Who should I send that to?", _wc("clarify"), PLAIN) == set()
    assert "write:no_question" in keys("Tell me who that's for.", _wc("clarify"), PLAIN)


def test_a_read_states_what_it_found():
    assert keys("Your slot moved to room 214.", _wc("read", ["214"]), PLAIN) == set()
    assert "write:missing_fact" in keys("Yes, there was a text about it.", _wc("read", ["214"]), PLAIN)
