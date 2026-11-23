"""Writing profile, send-timing / reminder habits and usage counters of service.behavior.

Everything is synthetic and deterministic (fixed clock, invented names, example.test
addresses). The privacy tests read the whole behavior.db (every table plus the raw
file bytes) to prove that no text ever reaches disk. Run with a throwaway WISP_HOME:

    WISP_HOME=<fresh dir> python -m pytest -q tests/test_behavior_writing_profile.py
"""
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from service.behavior import habits, usage, writing
from service.behavior import nudges as nudge_mod, predict as predict_mod, profile as profile_mod
from service.behavior.engine import BehaviorEngine
from service.behavior.miners import pattern_id
from service.behavior.types import PROFILE_ONLY_KINDS, Observation
from tests.fixtures.behavior_persona import generate_persona

TZ = ZoneInfo("America/Los_Angeles")
H = 3600.0
D = 86400.0
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=TZ).timestamp()        # Tuesday 8am (same as the engine tests)
TODAY = datetime.fromtimestamp(NOW, TZ).date()


def local_ts(days_back: int, hour: int, minute: int = 0) -> float:
    d = TODAY - timedelta(days=days_back)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "behavior.db"


@pytest.fixture
def eng(db_path):
    e = BehaviorEngine(db_path, clock=lambda: NOW, tz="America/Los_Angeles")
    yield e
    e.close()


@pytest.fixture
def on(eng):
    eng.configure_writing(True)
    return eng


# ------------------------------------------------------------------ helpers
def dump_everything(path: Path) -> str:
    """Every table of the database, serialized, plus the raw bytes of the db / wal / shm files."""
    con = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        parts = [f"{t}: {con.execute(f'SELECT * FROM \"{t}\"').fetchall()!r}" for t in tables]
    finally:
        con.close()
    for raw in sorted(path.parent.glob(path.name + "*")):
        parts.append(raw.read_bytes().decode("latin-1"))
    return "\n".join(parts).lower()


def count(eng, sql: str, *args) -> int:
    with eng._store._lock:
        return eng._store._conn.execute(sql, args).fetchone()[0]


def weekday_sends(n: int, hours=(9, 10, 11)) -> list[float]:
    """``n`` send times on distinct recent weekdays (newest first), cycling through ``hours``."""
    out, d = [], 1
    while len(out) < n:
        if (TODAY - timedelta(days=d)).weekday() < 5:
            out.append(local_ts(d, hours[len(out) % len(hours)], 15))
        d += 1
    return out


def sends(channel_kind: str, times, source: str) -> list[Observation]:
    return [Observation(id=f"{channel_kind}-{i}", kind=channel_kind, ts=t, subject="", source=source)
            for i, t in enumerate(times)]


def email_text(sentinel: str, greeting="Hi", signoff="Thanks") -> str:
    return (f"{greeting} Quillfeather,\n\nThe {sentinel} report is ready! I can't wait to hear what you think.\n\n"
            f"{signoff},\nJordan Example\nCTO, Example Inc\n555-123-4567\n\n"
            f"On Mon, Oct 12, 2026 at 3:00 PM Pat Example <pat@example.test> wrote:\n> {sentinel}quoted words")


def items(prefix: str, n: int, text_for, *, start_hours_back: int = 1):
    return [(f"{prefix}-{i:03d}", NOW - (start_hours_back + i) * H, text_for(i)) for i in range(n)]


def stats_row(**over):
    row = {"words": 10, "sentences": 2, "caps": 2, "lows": 0, "emoji": 0, "all_lower": 0, "excl": 0,
           "quest": 0, "ellipsis": 0, "contr": 0, "expanded": 0, "greeting": "", "signoff": "", "chars": 50}
    row.update(over)
    return row


# ====================================================================== 1. writing.measure
def test_greeting_and_signoff_come_only_from_the_closed_lexicon_and_names_are_dropped():
    m = writing.measure("email", "Hi Quillfeather,\n\nSee you at the review.\n\nThanks,\nJordan Example")
    assert m["greeting"] == "Hi" and m["signoff"] == "Thanks"
    assert "quillfeather" not in repr(m).lower() and "jordan" not in repr(m).lower()


@pytest.mark.parametrize("text, greeting", [
    ("Hey Quillfeather\nok", "Hey"),
    ("Good morning team,\nok", "Good morning team"),
    ("hello there\nok", "hello there"),
    ("Salutations Quillfeather,\nok", ""),            # not in the lexicon: nothing is kept
    ("Quillfeather,\nok", ""),                        # a bare name is never a greeting
    ("Heyyy there\nok", ""),                          # must end at a word boundary
])
def test_greeting_lexicon(text, greeting):
    assert writing.measure("email", text)["greeting"] == greeting


@pytest.mark.parametrize("text, signoff", [
    ("Done for now.\n\nThanks,\nJordan Example", "Thanks"),
    ("Done for now.\n\nBest regards,\nJordan", "Best regards"),
    ("Done for now.\n\nCheers!", "Cheers"),
    ("Done for now.\nThanks for the help", ""),        # a sentence starting with a sign-off word is not one
    ("Done for now.\nThanks, Jordan Example Of Something Long", ""),   # too many words after the comma
    ("Done for now.\nSincerely, Jordan Example.", ""),                 # name ending in a period: not a sign-off line
])
def test_signoff_lexicon(text, signoff):
    assert writing.measure("email", text)["signoff"] == signoff


def test_signoff_with_name_and_signature_block_are_dropped_from_the_counts():
    with_sig = writing.measure("email", "Hi Sam,\n\nWe cannot go. I will call.\n\nThanks,\nJordan Example\n"
                                        "CTO, Example Inc\n555-123-4567")
    bare = writing.measure("email", "Hi Sam,\n\nWe cannot go. I will call.")
    assert with_sig["signoff"] == "Thanks" and bare["signoff"] == ""
    for key in ("chars", "words", "sentences", "caps", "expanded"):
        assert with_sig[key] == bare[key], key


def test_a_text_of_only_a_signoff_measures_to_nothing():
    assert writing.measure("email", "Thanks so much!") is None


@pytest.mark.parametrize("text", [
    "Typed words here.\n\nOn Mon, Oct 12, 2026 at 3:00 PM Pat Example <pat@example.test> wrote:\n> secretquoted words\n> more",
    "Typed words here.\n\nOn Mon, Oct 12, 2026 at 3:00 PM Pat Example <pat@example.test>\nwrote:\n> secretquoted words",
    "Typed words here.\n> secretquoted words\n> more quoted",
    "Typed words here.\n\n-- \nJordan Example\nsecretquoted words",
    "Typed words here.\n\nSent from my iPhone\nsecretquoted words",
    "Typed words here.\n\n-----Original Message-----\nFrom: Pat Example\nsecretquoted words",
])
def test_quoted_reply_and_signature_do_not_count(text):
    assert writing.strip_quoted(text) == "Typed words here."
    assert writing.measure("email", text) == writing.measure("email", "Typed words here.")
    assert writing.measure("message", text) == writing.measure("message", "Typed words here.")


def test_quote_markers_are_kept_for_notes_and_reminders():
    # Only emails and messages are replies; a note that starts a line with ">" is the user's own text.
    note = writing.measure("note", "> a quoted idea I wrote down\nsecond line here")
    assert note["words"] == 9


def test_contractions_versus_expanded_forms():
    m = writing.measure("email", "I can't. It's fine. I will go. We are here.")
    assert (m["contr"], m["expanded"]) == (2, 2)
    m = writing.measure("message", "We don't know and they won't say. I do not care. It is late.")
    assert m["contr"] == 2 and m["expanded"] == 2


def test_emoji_counts_each_symbol():
    assert writing.measure("message", "Great 😀 job 🎉 ❤")["emoji"] == 3
    assert writing.measure("message", "No pictures here")["emoji"] == 0


def test_exclamation_question_and_ellipsis_flags_are_zero_or_one():
    many = writing.measure("message", "Wow!!! Really??? Hmm... ok… really!")
    assert (many["excl"], many["quest"], many["ellipsis"]) == (1, 1, 1)
    none = writing.measure("message", "Plain statement.")
    assert (none["excl"], none["quest"], none["ellipsis"]) == (0, 0, 0)
    assert writing.measure("message", "Really?")["quest"] == 1
    assert writing.measure("message", "ok…")["ellipsis"] == 1


def test_all_lowercase_flag_and_sentence_starts():
    low = writing.measure("message", "just a quick note. see you soon")
    assert low["all_lower"] == 1 and low["lows"] == 2 and low["caps"] == 0
    cap = writing.measure("message", "Just a quick note. See you soon")
    assert cap["all_lower"] == 0 and cap["caps"] == 2 and cap["lows"] == 0
    mixed = writing.measure("message", "just a quick note. See you soon")
    assert mixed["all_lower"] == 0 and (mixed["caps"], mixed["lows"]) == (1, 1)


@pytest.mark.parametrize("channel, text", [
    ("email", ""), ("email", "   \n\t "), ("message", ""), ("note", "\n\n"), ("reminder", ""),
    ("email", "😀"), ("email", ">>> only quoted\n> lines"),
])
def test_measure_returns_none_when_nothing_is_there(channel, text):
    assert writing.measure(channel, text) is None


def test_measure_rejects_unknown_channels_and_non_text():
    assert writing.measure("tweet", "hello there") is None
    assert writing.measure("email", None) is None
    assert writing.measure("email", 42) is None
    assert writing.measure("email", b"bytes") is None


def test_note_and_reminder_channels_never_keep_a_phrase():
    for channel in ("note", "reminder"):
        m = writing.measure(channel, "Hi Quillfeather, call the garage.\nThanks,\nJordan")
        assert m["greeting"] == "" and m["signoff"] == ""


def test_long_text_is_measured_on_its_first_part_only():
    m = writing.measure("note", "word " * 10_000)
    assert m["chars"] <= writing.MAX_TEXT_CHARS


@pytest.mark.parametrize("channel", writing.CHANNELS)
def test_measure_output_holds_no_text_beyond_two_short_lexicon_phrases(channel):
    text = ("Good morning team,\n\nQuillfeather's budget is at https://example.test/q?secret=1 and "
            "jordan@example.test can't wait! 😀 Do you know?\n\nBest regards,\nJordan Example\n"
            "On Mon, Oct 12, 2026 at 3:00 PM Pat Example <pat@example.test> wrote:\n> zyxquoted")
    m = writing.measure(channel, text)
    assert m is not None
    strings = {k: v for k, v in m.items() if isinstance(v, str)}
    assert set(strings) == {"greeting", "signoff"}
    for k, v in m.items():
        if k not in strings:
            assert type(v) is int, (k, v)
    greetings = {w.split()[0] for w in writing._GREETING_WORDS}
    for phrase in strings.values():
        assert len(phrase) <= writing.MAX_PHRASE_CHARS
        assert phrase == "" or phrase.lower().split()[0] in greetings | set(writing._SIGNOFF_WORDS) \
            | {w.split()[0] for w in writing._SIGNOFF_WORDS}
    blob = repr(m).lower()
    for leaked in ("quillfeather", "example", "secret", "zyxquoted", "budget"):
        assert leaked not in blob
    if channel in writing.PHRASE_CHANNELS:
        assert m["greeting"] == "Good morning team" and m["signoff"] == "Best regards"


# ====================================================================== 2. summarize / statement / card
def test_summarize_needs_twenty_samples():
    rows = [stats_row() for _ in range(writing.MIN_SAMPLES)]
    assert writing.MIN_SAMPLES == 20
    assert writing.summarize("email", rows[:19]) is None
    assert writing.summarize("email", []) is None
    got = writing.summarize("email", rows)
    assert got is not None and got["samples"] == 20 and got["channel"] == "email"


def test_summarize_ignores_skipped_and_junk_rows():
    good = [stats_row() for _ in range(19)]
    junk = [{"skip": 1}, {"skip": 1}, "text", None, {"words": "7"}, {"words": 3.5}]
    assert writing.summarize("email", good + junk) is None       # still 19 real samples
    assert writing.summarize("email", good + junk + [stats_row()])["samples"] == 20


def test_summarize_numbers():
    rows = [stats_row(emoji=1 if i < 5 else 0, excl=1 if i < 10 else 0, quest=1 if i < 2 else 0,
                      all_lower=1 if i < 3 else 0, ellipsis=1 if i == 0 else 0,
                      contr=1 if i < 6 else 0, expanded=1 if i < 2 else 0) for i in range(20)]
    s = writing.summarize("message", rows)
    assert s["avg_words"] == 10.0 and s["avg_sentences"] == 2.0 and s["avg_sentence_words"] == 5.0
    assert s["emoji_pct"] == 25 and s["excl_pct"] == 50 and s["quest_pct"] == 10
    assert s["all_lower_pct"] == 15 and s["ellipsis_pct"] == 5 and s["caps_pct"] == 100
    assert s["contraction_pct"] == 75            # 6 of 8 verbs
    rows_few = [stats_row(contr=1 if i == 0 else 0, expanded=1 if i == 1 else 0) for i in range(20)]
    assert writing.summarize("message", rows_few)["contraction_pct"] is None   # under 5 verbs: no claim


def _with_phrases(greetings: dict[str, int], total: int):
    rows, pool = [], [g for g, n in greetings.items() for _ in range(n)]
    for i in range(total):
        rows.append(stats_row(greeting=pool[i] if i < len(pool) else ""))
    return rows


def test_a_phrase_needs_three_uses_and_the_top_three_win():
    two = writing.summarize("email", _with_phrases({"Hi": 2, "Hello": 2}, 20))
    assert two["greetings"] == []                                    # 2 uses is not a habit
    s = writing.summarize("email", _with_phrases({"Hi": 3, "Hello": 5, "Hey": 4, "Yo": 3}, 20))
    assert [g["phrase"] for g in s["greetings"]] == ["Hello", "Hey", "Hi"]    # Yo is the fourth
    assert [g["count"] for g in s["greetings"]] == [5, 4, 3]
    # Three uses in 100 messages is under 1 in 20: not a habit either.
    assert writing.summarize("email", _with_phrases({"Hi": 3}, 100))["greetings"] == []
    assert writing.summarize("email", _with_phrases({"Hi": 5}, 100))["greetings"][0]["count"] == 5


def test_notes_and_reminders_never_report_phrases():
    rows = _with_phrases({"Hi": 10}, 20)
    for channel in ("note", "reminder"):
        s = writing.summarize(channel, rows)
        assert s["greetings"] == [] and s["signoffs"] == []


def test_statement_contains_the_numbers():
    rows = [stats_row(emoji=1 if i < 5 else 0, excl=1 if i < 10 else 0, contr=3, expanded=1,
                      greeting="Hi" if i < 4 else "", signoff="Thanks" if i < 3 else "") for i in range(20)]
    text = writing.statement(writing.summarize("email", rows))
    assert text.startswith("Your sent emails (20 emails): ")
    for piece in ("About 10 words in 2 sentences each", "100% of sentences start with a capital",
                  "50% have an exclamation mark", "0% a question mark", "emoji in 25%",
                  "75% of verbs contracted", "you open with “Hi” (4×)", "you close with “Thanks” (3×)"):
        assert piece in text, piece
    assert text.endswith(".")


def test_statement_variants():
    lower = writing.statement(writing.summarize("message", [stats_row(all_lower=1, caps=0, lows=2, ellipsis=1)
                                                            for _ in range(20)]))
    assert "100% are written all in lowercase" in lower and "start with a capital" not in lower
    assert "100% an ellipsis" in lower
    one = writing.statement(writing.summarize("note", [stats_row(words=3, sentences=1, caps=1) for _ in range(20)]))
    assert "About 3 words in 1 sentence each" in one and "Your notes (20 notes)" in one
    assert "contracted" not in one


def _style_stats(channel, **kw):
    rows = [stats_row(greeting="Hi", signoff="Thanks", contr=2, expanded=0, **kw) for _ in range(20)]
    return writing.summarize(channel, rows)


def test_style_card_is_short_whole_lines_and_byte_capped():
    stats = {"email": _style_stats("email"), "message": _style_stats("message", excl=1)}
    card = writing.style_card(stats)
    assert card and len(card.encode("utf-8")) <= 300
    lines = card.split("\n")
    assert lines[0] == writing.STYLE_HEADER
    assert [ln.split(":")[0] for ln in lines[1:]] == ["Emails", "Messages"]
    for cap in (0, 40, 60, 90, 150, 200, 300, 1000):
        out = writing.style_card(stats, cap)
        assert len(out.encode("utf-8")) <= cap
        if out:
            got = out.split("\n")
            assert got[0] == writing.STYLE_HEADER and len(got) >= 2
            for line in got[1:]:                       # a line is either whole or cut at a clause boundary
                full = writing.style_line(stats["email" if line.startswith("Emails") else "message"])
                assert line == full or full.startswith(line + ", "), line


def test_style_card_worst_case_stays_under_300_bytes():
    rows = [stats_row(greeting="Good morning everyone", signoff="Thank you so much", contr=3, emoji=1, excl=1,
                      all_lower=1, caps=0, lows=2) for _ in range(20)]
    stats = {c: writing.summarize(c, rows) for c in ("email", "message")}
    assert len(writing.style_card(stats).encode("utf-8")) <= 300


def test_style_card_is_empty_when_no_channel_qualifies_and_ignores_notes_and_reminders():
    assert writing.style_card({}) == ""
    assert writing.style_card({"email": None, "message": None}) == ""
    only_other = {"note": _style_stats("note"), "reminder": _style_stats("reminder")}
    assert writing.style_card(only_other) == ""
    mixed = writing.style_card({**only_other, "email": _style_stats("email")})
    assert mixed.count("\n") == 1 and "Emails:" in mixed and "otes" not in mixed and "eminder" not in mixed


# ====================================================================== 3. engine privacy
def test_switch_off_by_default_stores_nothing(eng, db_path):
    assert eng.writing_enabled() is False
    assert eng.observe_writing("email", items("srcid", 25, lambda i: email_text("zyxoff"))) == 0
    assert eng.observe_email_sent([(f"srcid-{i}", NOW - i * H) for i in range(5)]) == 0
    assert eng.observe_note_edits([("n1", NOW - H, "Quillfeather journal")]) == 0
    assert eng.writing_unseen("email", ["a", "b"]) == []
    assert eng.writing_stats() == {} and eng.style_card() == ""
    assert count(eng, "SELECT COUNT(*) FROM writing_samples") == 0
    assert count(eng, "SELECT COUNT(*) FROM observations") == 0
    assert "zyxoff" not in dump_everything(db_path)


def test_unknown_channel_and_non_bool_switch_are_refused(eng):
    with pytest.raises(ValueError):
        eng.observe_writing("tweet", [("a", NOW, "hello there")])
    for bad in ("yes", 1, 0, None):
        with pytest.raises(ValueError):
            eng.configure_writing(bad)
    assert eng.writing_enabled() is False


def test_turning_the_switch_on_and_off_erases_everything_it_stored(on):
    eng = on
    stamps = weekday_sends(25)
    assert eng.observe_writing("email", items("e", 25, lambda i: email_text("zyxmail"))) == 25
    assert eng.observe_writing("message", items("m", 25, lambda i: email_text("zyxmsg"))) == 25
    assert eng.observe_email_sent([(f"e-{i}", t) for i, t in enumerate(stamps)]) == 25
    assert eng.observe_note_edits([(f"n{i}", t, f"Quillfeather journal 2026-10-{i + 1:02d}")
                                   for i, t in enumerate(stamps)]) == 25
    # Controls that must survive: message sends (an ordinary kind), a note from another source, an event.
    eng.observe([Observation(id=f"mo{i}", kind="message_out", ts=t, subject="Sam Example", source="messages")
                 for i, t in enumerate(stamps)])
    eng.observe([Observation(id="n-other", kind="note", ts=NOW - H, subject="Elsewhere", source="other"),
                 Observation(id="ev", kind="event", ts=NOW - H, end_ts=NOW - H / 2, subject="Standup", source="calendar")])
    eng.mine(NOW)
    before = {(p.kind, p.subject) for p in eng.patterns()}
    assert {("writing_style", "email"), ("writing_style", "message"), ("send_timing", "email"),
            ("send_timing", "note"), ("send_timing", "message")} <= before
    assert eng._store.meta_get("writing_salt")

    assert eng.configure_writing(False) == {"writing_profile": False}
    assert eng.writing_enabled() is False
    assert count(eng, "SELECT COUNT(*) FROM writing_samples") == 0
    assert count(eng, "SELECT COUNT(*) FROM observations WHERE kind='email_out'") == 0
    assert count(eng, "SELECT COUNT(*) FROM observations WHERE kind='note' AND source='notes'") == 0
    assert count(eng, "SELECT COUNT(*) FROM observations WHERE kind='message_out'") == 25
    assert count(eng, "SELECT COUNT(*) FROM observations WHERE id IN ('n-other','ev')") == 2
    after = {(p.kind, p.subject) for p in eng.patterns()}
    assert not [k for k in after if k[0] == "writing_style"]
    assert ("send_timing", "email") not in after and ("send_timing", "note") not in after
    assert ("send_timing", "message") in after                   # not writing-profile data
    assert eng._store.meta_get("writing_salt") is None
    assert eng.writing_stats() == {} and eng.style_card() == ""
    assert eng.observe_writing("email", items("e", 3, lambda i: email_text("zyxmail"))) == 0

    # A later mine() cannot bring them back, and turning it on again starts from nothing.
    eng.mine(NOW)
    assert not [p for p in eng.patterns() if p.kind == "writing_style"
                or (p.kind == "send_timing" and p.subject in ("email", "note"))]
    eng.configure_writing(True)
    assert eng.writing_unseen("email", [f"e-{i:03d}" for i in range(3)]) == [f"e-{i:03d}" for i in range(3)]


def _channel_feed(eng, channel: str, sentinel: str):
    """Feed one channel the way the runtime does, with sentinel words in every text."""
    stamps = weekday_sends(25)
    if channel == "email":
        eng.observe_email_sent([(f"srcid-zq-{i:03d}", t) for i, t in enumerate(stamps)])
        return eng.observe_writing("email", [(f"srcid-zq-{i:03d}", t, email_text(sentinel))
                                             for i, t in enumerate(stamps)])
    if channel == "message":
        return eng.observe_writing("message", [(f"srcid-zq-{i:03d}", t, email_text(sentinel))
                                               for i, t in enumerate(stamps)])
    if channel == "note":
        eng.observe_note_edits([(f"srcid-zq-{i:03d}", t, f"Quillfeather {sentinel} journal 2026-10-{i + 1:02d}")
                                for i, t in enumerate(stamps)])
        return eng.observe_writing("note", [(f"srcid-zq-{i:03d}", t, f"Quillfeather {sentinel} journal\n"
                                                                    f"{sentinel} body. It's done.")
                                            for i, t in enumerate(stamps)])
    rows = [{"id": f"srcid-zq-{i:03d}", "created_ts": t - 2 * H, "due_ts": t, "completed_ts": t + 1800 if i % 2 else None,
             "repeating": i % 5 == 0, "title": f"Call Quillfeather {sentinel}", "notes": f"{sentinel} secret"}
            for i, t in enumerate(stamps)]
    eng.observe_reminder_facts(rows, snapshot_at=NOW)
    return eng.observe_writing("reminder", [(f"srcid-zq-{i:03d}", t, f"Call Quillfeather {sentinel} tomorrow")
                                            for i, t in enumerate(stamps)])


@pytest.mark.parametrize("channel", ["email", "message", "note", "reminder"])
def test_no_text_ever_reaches_the_database(on, db_path, channel):
    sentinel = f"zyx{channel}sentinel"
    assert _channel_feed(on, channel, sentinel) == 25
    on.mine(NOW)
    on.writing_stats(), on.style_card(), on.weekly(NOW), on.describe(), on.profile_card(NOW)
    blob = dump_everything(db_path)
    for leaked in (sentinel, "quillfeather", "jordan example", "pat@example.test", "srcid-zq", "secret",
                   "555-123-4567", "cto, example"):
        assert leaked not in blob, leaked
    # What is there: the measured integers.
    assert count(on, "SELECT COUNT(*) FROM writing_samples WHERE channel=?", channel) == 25


def test_stored_sample_rows_are_integers_and_two_lexicon_phrases(on):
    _channel_feed(on, "email", "zyxrow")
    rows = on._store.writing_rows("email")
    assert len(rows) == 25
    for row in rows:
        assert set(row) == {"ts", *writing.measure("email", "Hi there.\nok").keys()}
        for k, v in row.items():
            assert isinstance(v, (int, float)) or (k in ("greeting", "signoff") and len(v) <= 40)
    keys = {r[0] for r in on._store._conn.execute("SELECT key FROM writing_samples")}
    assert all(len(k) == 24 and set(k) <= set("0123456789abcdef") for k in keys)


def test_same_ids_twice_add_nothing_and_changed_text_is_not_read_again(on):
    batch = items("m", 6, lambda i: "Hi there. Thanks for the note!")
    assert on.observe_writing("message", batch) == 6
    assert on.observe_writing("message", batch) == 0
    edited = [(sid, ts, "A completely different text now") for sid, ts, _ in batch]
    assert on.observe_writing("message", edited) == 0
    assert all(r["words"] == 6 for r in on._store.writing_rows("message"))
    # The same ids on another channel are different samples.
    assert on.observe_writing("email", batch) == 6
    # Times-only sources and notes upsert without growing.
    assert on.observe_email_sent([("a", NOW - H), ("b", NOW - 2 * H)]) == 2
    assert on.observe_email_sent([("a", NOW - H), ("b", NOW - 2 * H)]) == 0
    notes = [("n1", NOW - H, "Quillfeather journal 1")]
    assert on.observe_note_edits(notes) == 1 and on.observe_note_edits(notes) == 0


def test_unmeasurable_text_is_remembered_so_it_is_not_read_again(on):
    assert on.observe_writing("email", [("empty", NOW - H, ""), ("sig", NOW - 2 * H, "Thanks so much!")]) == 2
    assert on.writing_unseen("email", ["empty", "sig", "new"]) == ["new"]
    assert on.writing_stats() == {}                               # skip rows are not samples


def test_writing_unseen_skips_known_ids_and_is_per_channel(on):
    on.observe_writing("email", [(i, NOW - i * H, "A short sentence.") for i in range(1, 6)])
    assert on.writing_unseen("email", [1, 2, 3, 6, 7, 8]) == [6, 7, 8]
    assert on.writing_unseen("message", [1, 2]) == [1, 2]
    assert on.writing_unseen("tweet", [1, 2]) == []
    assert on.writing_unseen("email", []) == []
    on.configure_writing(False)
    assert on.writing_unseen("email", [6]) == []


def test_non_finite_and_non_numeric_times_are_skipped(on):
    got = on.observe_writing("message", [("a", float("nan"), "Hello there."), ("b", float("inf"), "Hello there."),
                                         ("c", None, "Hello there."), ("d", "yesterday", "Hello there."),
                                         ("e", NOW - H, "Hello there.")])
    assert got == 1
    assert on.observe_email_sent([("x", float("nan")), ("y", None), ("z", NOW - H)]) == 1
    assert on.observe_note_edits([("n", float("-inf"), "Quill"), ("o", NOW - H, "Quill")]) == 1


def test_stored_cap_keeps_the_newest(on, monkeypatch):
    monkeypatch.setattr(writing, "MAX_STORED_PER_CHANNEL", 25)
    batch = items("c", 40, lambda i: "A short sentence here.")
    on.observe_writing("message", batch)
    rows = on._store.writing_rows("message")
    assert len(rows) == 25
    newest = sorted((ts for _, ts, _ in batch), reverse=True)[:25]
    assert sorted((r["ts"] for r in rows), reverse=True) == newest
    on.observe_writing("email", items("c", 3, lambda i: "A short sentence here."))
    assert len(on._store.writing_rows("message")) == 25            # another channel does not trim it


def test_summaries_read_only_the_newest_samples_and_resending_reports_nothing_new(on):
    """The summary window (MAX_SAMPLES_PER_CHANNEL) is smaller than what is stored, so an item
    outside it is still known and is never read again."""
    batch = items("c", 60, lambda i: "A short sentence here.")
    assert on.observe_writing("message", batch) == 60
    assert on.observe_writing("message", batch) == 0
    assert len(on._store.writing_rows("message", limit=25)) == 25
    newest = sorted((ts for _, ts, _ in batch), reverse=True)[:25]
    assert sorted((r["ts"] for r in on._store.writing_rows("message", limit=25)), reverse=True) == newest


# ====================================================================== 4. mine(): writing patterns and verdicts
def _feed_messages(eng, n: int, prefix: str = "m"):
    return eng.observe_writing("message", items(prefix, n, lambda i: "hey, can't talk now! call you later"))


def test_writing_style_patterns_need_twenty_samples(on):
    _feed_messages(on, 19)
    on.mine(NOW)
    assert not [p for p in on.patterns() if p.kind == "writing_style"]
    assert on.writing_stats() == {}
    _feed_messages(on, 1, prefix="extra")
    on.mine(NOW)
    pats = [p for p in on.patterns() if p.kind == "writing_style"]
    assert [p.subject for p in pats] == ["message"]
    p = pats[0]
    assert p.id == pattern_id("writing_style", "message")
    assert p.statement == writing.statement(on.writing_stats()["message"])
    assert p.support == p.opportunities == 20
    assert p.stats["basis"] == "observed" and "sent messages" in p.stats["evidence_label"]


def test_a_rejected_channel_leaves_stats_and_card_but_keeps_its_verdict_across_mining(on):
    _feed_messages(on, 25)
    on.observe_writing("email", items("e", 25, lambda i: email_text("zyxreject")))
    on.mine(NOW)
    assert set(on.writing_stats()) == {"email", "message"}
    assert "Emails:" in on.style_card() and "Messages:" in on.style_card()
    pid = pattern_id("writing_style", "email")
    on.set_pattern_status(pid, "rejected")

    assert set(on.writing_stats()) == {"message"}
    assert set(on.writing_stats(include_rejected=True)) == {"email", "message"}
    card = on.style_card()
    assert "Messages:" in card and "Emails:" not in card

    on.mine(NOW + H)
    assert on.patterns(status="rejected")[0].id == pid            # the verdict survived the re-mine
    assert set(on.writing_stats()) == {"message"}
    on.observe_writing("email", items("e2", 10, lambda i: email_text("zyxreject")))
    on.mine(NOW + 2 * H)
    assert set(on.writing_stats()) == {"message"} and on._store.get_pattern(pid).status == "rejected"
    on.set_pattern_status(pid, "active")
    assert set(on.writing_stats()) == {"email", "message"}


def _everything_engine(eng):
    """Persona calendar + every profile-only kind present."""
    eng.configure_writing(True)
    persona = generate_persona(NOW)
    eng.observe(persona.observations + persona.upcoming)
    eng.observe_writing("email", items("e", 25, lambda i: email_text("zyxall")))
    eng.observe_writing("message", items("m", 25, lambda i: email_text("zyxall")))
    stamps = weekday_sends(25)
    eng.observe_email_sent([(f"e-{i}", t) for i, t in enumerate(stamps)])
    eng.observe_note_edits([(f"n{i}", t, f"Quillfeather journal 2026-10-{i + 1:02d}") for i, t in enumerate(stamps)])
    eng.observe_reminder_facts([{"id": f"r{i}", "created_ts": t - 2 * H, "due_ts": t,
                                 "completed_ts": t + 600 if i % 2 else None, "repeating": i % 4 == 0}
                                for i, t in enumerate(stamps)], snapshot_at=NOW)
    for i in range(35):
        assert eng.record_usage("tab_opened", "study" if i % 2 else "brief", now=local_ts(1 + i % 5, 9 + i % 2))
    eng.mine(NOW)
    return {p.kind: p for p in eng.patterns()}


def test_profile_only_kinds_are_mined_but_never_reach_the_card_predictions_nudges_or_describe(eng, monkeypatch):
    by_kind = _everything_engine(eng)
    assert PROFILE_ONLY_KINDS <= set(by_kind), PROFILE_ONLY_KINDS - set(by_kind)
    hidden = [p for p in eng.patterns() if p.kind in PROFILE_ONLY_KINDS]
    assert len(hidden) >= 6
    assert any(p.kind not in PROFILE_ONLY_KINDS for p in eng.patterns())     # the persona's own patterns remain

    seen: dict[str, set] = {"predict": set(), "nudges": set(), "card": set()}
    real_predict, real_candidates, real_card = predict_mod.predict, nudge_mod.build_candidates, profile_mod.profile_card

    def spy_predict(patterns, *a, **k):
        seen["predict"] |= {p.kind for p in patterns}
        return real_predict(patterns, *a, **k)

    def spy_candidates(inputs):
        seen["nudges"] |= {p.kind for p in inputs.patterns}
        return real_candidates(inputs)

    def spy_card(patterns, *a, **k):
        seen["card"] |= {p.kind for p in patterns}
        return real_card(patterns, *a, **k)

    monkeypatch.setattr(predict_mod, "predict", spy_predict)
    monkeypatch.setattr(nudge_mod, "build_candidates", spy_candidates)
    monkeypatch.setattr(profile_mod, "profile_card", spy_card)

    card = eng.profile_card(NOW, max_bytes=5000)
    preds = eng.predict(NOW)
    eng.generate_nudges(NOW)
    described = eng.describe(now=NOW)
    for name, kinds in seen.items():
        assert kinds, f"{name} was never exercised"
        assert not kinds & PROFILE_ONLY_KINDS, (name, kinds & PROFILE_ONLY_KINDS)
    assert not {p["kind"] for p in described["patterns"]} & PROFILE_ONLY_KINDS
    statements = [p.statement for p in hidden]
    for text in statements:
        assert text not in card and text not in described["summary"]
        assert all(text != pr.because for pr in preds)
    hidden_ids = {p.id for p in hidden}
    assert not hidden_ids & {pr.pattern_id for pr in preds}
    for subject in ("email", "message", "note"):                    # a filter on a subject cannot surface them
        assert not {p["kind"] for p in eng.describe(subject, NOW)["patterns"]} & PROFILE_ONLY_KINDS
    assert {p.kind for p in eng.mine(NOW)} & PROFILE_ONLY_KINDS == set()    # mine() returns the live, non-profile set


def test_profile_only_patterns_are_still_listed_for_the_learned_window(eng):
    _everything_engine(eng)
    listed = {p.kind for p in eng.patterns()}
    assert PROFILE_ONLY_KINDS <= listed


# ====================================================================== 5. habits
def test_send_timing_needs_twenty_events_over_fourteen_days():
    times = weekday_sends(24)
    assert habits.SEND_MIN_EVENTS == 20 and habits.SEND_MIN_DAYS == 14
    assert habits.mine_send_timing("email", sends("email_out", times[:19], "mail"), TZ) is None
    spread = habits.mine_send_timing("email", sends("email_out", times[:20], "mail"), TZ)
    assert spread is not None
    short = [local_ts(d, 10, 5) for d in range(1, 13)] + [local_ts(d, 10, 6) for d in range(1, 13)]
    assert len(short) == 24                                           # plenty of sends, but only 12 days
    assert max(short) - min(short) < 14 * D
    assert habits.mine_send_timing("email", sends("email_out", short, "mail"), TZ) is None


def test_send_timing_boundary_is_exactly_fourteen_days():
    newest = local_ts(1, 11)                         # a Monday; 19 sends in the three hours before it
    base = [newest - k * 600 for k in range(19)]
    exact = base + [newest - 14 * D]
    assert max(exact) - min(exact) == 14 * D
    assert habits.mine_send_timing("note", sends("note", exact, "notes"), TZ) is not None
    near = base + [newest - 14 * D + 60]
    assert max(near) - min(near) < 14 * D
    assert habits.mine_send_timing("note", sends("note", near, "notes"), TZ) is None


def _planned(counts: dict[int, int]) -> list[float]:
    """weekday send times: ``counts`` maps hour -> number of sends (one per weekday, newest first)."""
    plan = [h for h, n in counts.items() for _ in range(n)]
    out, d = [], 1
    while len(out) < len(plan):
        if (TODAY - timedelta(days=d)).weekday() < 5:
            out.append(local_ts(d, plan[len(out)], 20))
        d += 1
    return out


def test_send_timing_needs_forty_percent_of_weekday_sends_in_one_three_hour_window():
    plan = {13: 2, 16: 2, 19: 2, 22: 2, 1: 2, 5: 2}
    at_threshold = _planned({9: 8, **plan})               # 8 of 20 = 40%
    p = habits.mine_send_timing("email", sends("email_out", at_threshold, "mail"), TZ)
    assert p is not None and p.support == 8 and p.opportunities == 20
    assert "40% of 20 on weekdays" in p.statement
    below = _planned({9: 7, **plan, 14: 1})                # 7 of 20 = 35%
    assert len(below) == 20
    assert habits.mine_send_timing("email", sends("email_out", below, "mail"), TZ) is None


def _timing_fixture():
    """24 weekday sends (20 inside 9am-12pm, 4 at 4pm, all of those on Thursdays) plus 7 on weekends."""
    weekday = weekday_sends(20)
    thursdays = [local_ts(d, 16, 10) for d in range(1, 29) if (TODAY - timedelta(days=d)).weekday() == 3][:4]
    weekend = [local_ts(d, 14, 0) for d in range(1, 29) if (TODAY - timedelta(days=d)).weekday() >= 5][:7]
    assert len(thursdays) == 4 and len(weekend) == 7
    return weekday + thursdays + weekend


def test_send_timing_statement_names_weekday_window_and_weekend_share():
    times = _timing_fixture()
    p = habits.mine_send_timing("email", sends("email_out", times, "mail"), TZ)
    assert p is not None and p.kind == "send_timing" and p.subject == "email"
    stmt = p.statement
    assert stmt.startswith("Your sent emails cluster on weekdays between 9am–12pm (")
    assert "(83% of 24 on weekdays)" in stmt
    assert "Thursday is the busiest day (26%)" in stmt
    assert stmt.endswith("23% are on weekends.")
    assert p.window.weekdays == (0, 1, 2, 3, 4) and (p.window.start_minute, p.window.end_minute) == (540, 720)
    assert p.support == 20 and p.opportunities == 24
    assert p.stats["top_weekday"] == 3 and p.stats["weekend_pct"] == 23 and p.stats["samples"] == 31
    assert p.stats["evidence_label"] == "20 of 24 weekday emails in that window"
    assert len(p.evidence) == 5 and p.last_seen == max(times)
    assert 0 < p.confidence < 1


def test_send_timing_weekend_clause_needs_five_sends_and_a_fifth_of_all():
    base = weekday_sends(24)
    few = [local_ts(d, 14) for d in range(1, 29) if (TODAY - timedelta(days=d)).weekday() >= 5][:4]
    assert "weekends" not in habits.mine_send_timing("email", sends("email_out", base + few, "mail"), TZ).statement
    seven = [local_ts(d, 14) for d in range(1, 29) if (TODAY - timedelta(days=d)).weekday() >= 5][:7]
    assert "weekends" in habits.mine_send_timing("email", sends("email_out", base + seven, "mail"), TZ).statement
    huge = weekday_sends(20) + seven[:5]      # 5 of 25 = 20%
    assert "20% are on weekends" in habits.mine_send_timing("email", sends("email_out", huge, "mail"), TZ).statement


def test_send_timing_channel_wording_and_stable_id():
    times = weekday_sends(24)
    words = {"email": "sent emails", "message": "sent messages", "note": "notes last edited"}
    ids = set()
    for channel, noun in words.items():
        p = habits.mine_send_timing(channel, sends("x", times, "notes"), TZ)
        assert f"Your {noun} cluster" in p.statement
        assert p.id == pattern_id("send_timing", channel)
        ids.add(p.id)
    assert len(ids) == 3
    first = habits.mine_send_timing("email", sends("email_out", times, "mail"), TZ)
    shifted = habits.mine_send_timing("email", sends("email_out", [t - 60 for t in times[:-2]] + times[-2:], "mail"), TZ)
    assert first.id == shifted.id


def test_send_timing_with_only_weekend_sends_says_nothing():
    weekend = [local_ts(d, 10) for d in range(1, 90) if (TODAY - timedelta(days=d)).weekday() >= 5]
    assert len(weekend) >= 20
    assert habits.mine_send_timing("email", sends("email_out", weekend, "mail"), TZ) is None


def test_send_timing_id_is_stable_across_engine_remines(on):
    times = weekday_sends(24)
    on.observe_email_sent([(f"e{i}", t) for i, t in enumerate(times)])
    on.mine(NOW)
    first = [p for p in on.patterns() if p.kind == "send_timing"]
    assert [p.subject for p in first] == ["email"]
    on.observe_email_sent([(f"extra{i}", local_ts(2 + i, 10, 40)) for i in range(2)])
    on.mine(NOW + H)
    second = [p for p in on.patterns() if p.kind == "send_timing"]
    assert [p.id for p in second] == [first[0].id] == [pattern_id("send_timing", "email")]
    assert second[0].stats["samples"] == first[0].stats["samples"] + 2


# ---- reminder habits ---------------------------------------------------------
def _reminder_rows(n=20, done=14, repeating=4, lead_hours=2.0):
    rows = []
    for i in range(n):
        created = local_ts(2 + i, 9, 0)
        due = created + lead_hours * H
        row = {"id": f"rem-{i}", "created_ts": created, "due_ts": due, "repeating": i < repeating}
        if i < done:
            row["completed_ts"] = due + (1800 if i >= 2 else -600)
        rows.append(row)
    return rows


def _facts(eng):
    return eng._store.observations(kinds=["reminder_fact"])


def test_reminder_habits_from_stored_facts(eng):
    assert eng.observe_reminder_facts(_reminder_rows(), snapshot_at=NOW) == 20
    out = {p.subject: p for p in habits.mine_reminder_habits(_facts(eng), [], NOW, TZ)}
    assert set(out) == {"lead_time", "completion_lag", "overdue", "repeating"}
    assert all(p.kind == "reminder_habit" and p.id == pattern_id("reminder_habit", p.subject) for p in out.values())

    assert out["lead_time"].statement == ("You usually set a reminder about 2 hours before it is due, "
                                          "mostly in the morning.")
    assert out["lead_time"].stats["median_hours"] == 2.0 and out["lead_time"].stats["samples"] == 20

    lag = out["completion_lag"]
    assert lag.statement == "You usually finish a reminder about 30 minutes after it is due; 14% are done by their due time."
    assert (lag.support, lag.opportunities) == (2, 14) and lag.stats["on_time_pct"] == 14

    over = out["overdue"]
    assert over.statement == ("30% of the reminders that came due in the last eight weeks are still open (6 of 20).")
    assert over.stats["open_pct"] == 30

    rep = out["repeating"]
    assert rep.statement == "4 of your 20 reminders repeat."
    assert (rep.support, rep.opportunities) == (4, 20)


def test_reminder_habits_need_ten_reminders(eng):
    eng.observe_reminder_facts(_reminder_rows(n=9, done=9, repeating=3), snapshot_at=NOW)
    assert habits.mine_reminder_habits(_facts(eng), [], NOW, TZ) == []
    eng.observe_reminder_facts(_reminder_rows(n=10, done=10, repeating=3), snapshot_at=NOW)
    subjects = {p.subject for p in habits.mine_reminder_habits(_facts(eng), [], NOW, TZ)}
    assert {"lead_time", "completion_lag", "repeating"} <= subjects


def test_repeating_notes_join_the_repeating_statement():
    notes = [Observation(id=f"n{i}", kind="note", ts=NOW - i * H, subject="", source="notes",
                         attrs={"series": i < 3, "edited": True}) for i in range(12)]
    out = habits.mine_reminder_habits([], notes, NOW, TZ)
    assert [p.subject for p in out] == ["repeating"]
    assert out[0].statement == "3 of your 12 notes belong to a repeating series."
    fewer = habits.mine_reminder_habits([], notes[:9], NOW, TZ)
    assert fewer == []


def test_completion_lag_on_time_and_early_wording():
    facts = [Observation(id=f"f{i}", kind="reminder_fact", ts=NOW - (i + 3) * D, subject="", source="reminders",
                         attrs={"created_ts": NOW - (i + 3) * D - H, "due_ts": NOW - (i + 3) * D,
                                "completed_ts": NOW - (i + 3) * D - 7200, "repeating": False}) for i in range(12)]
    lag = next(p for p in habits.mine_reminder_habits(facts, [], NOW, TZ) if p.subject == "completion_lag")
    assert lag.statement == "You usually finish a reminder about 2 hours before it is due; 100% are done by their due time."
    exact = [Observation(id=f"g{i}", kind="reminder_fact", ts=NOW - (i + 3) * D, subject="", source="reminders",
                         attrs={"due_ts": NOW - (i + 3) * D, "completed_ts": NOW - (i + 3) * D}) for i in range(12)]
    lag = next(p for p in habits.mine_reminder_habits(exact, [], NOW, TZ) if p.subject == "completion_lag")
    assert lag.statement.startswith("You usually finish a reminder right when it is due")


def test_overdue_ignores_old_and_very_recent_reminders(eng):
    old = [{"id": f"o{i}", "created_ts": NOW - 70 * D, "due_ts": NOW - 69 * D - i} for i in range(12)]
    just = [{"id": f"j{i}", "created_ts": NOW - 2 * H, "due_ts": NOW - 600 - i} for i in range(12)]
    eng.observe_reminder_facts(old + just, snapshot_at=NOW)
    subjects = {p.subject for p in habits.mine_reminder_habits(_facts(eng), [], NOW, TZ)}
    assert "overdue" not in subjects


def test_open_reminders_that_vanish_are_dropped_but_completed_ones_are_kept(eng):
    first = [{"id": "open-keep", "created_ts": NOW - 3 * D, "due_ts": NOW - D},
             {"id": "open-gone", "created_ts": NOW - 3 * D, "due_ts": NOW - D},
             {"id": "done-gone", "created_ts": NOW - 3 * D, "due_ts": NOW - D, "completed_ts": NOW - D + 60}]
    assert eng.observe_reminder_facts(first, snapshot_at=NOW) == 3
    assert len(_facts(eng)) == 3
    eng.observe_reminder_facts([first[0]], snapshot_at=NOW + H)
    left = _facts(eng)
    assert len(left) == 2
    done = [o for o in left if o.attrs["completed_ts"] is not None]
    still_open = [o for o in left if o.attrs["completed_ts"] is None]
    assert len(done) == 1 and len(still_open) == 1
    # An open reminder that is later completed becomes a kept completed fact.
    eng.observe_reminder_facts([{**first[0], "completed_ts": NOW + 60}], snapshot_at=NOW + 2 * H)
    eng.observe_reminder_facts([], snapshot_at=NOW + 3 * H)
    assert len(_facts(eng)) == 2 and all(o.attrs["completed_ts"] is not None for o in _facts(eng))


def test_invalid_reminder_rows_are_ignored(eng):
    ok = {"id": "ok", "created_ts": NOW - D, "due_ts": NOW - D + H, "completed_ts": NOW - D + 2 * H, "repeating": True}
    junk = [
        {"id": "bool", "created_ts": True, "due_ts": False},
        {"id": "nan", "created_ts": float("nan"), "due_ts": float("inf")},
        {"id": "str", "created_ts": "1760000000", "due_ts": "tomorrow"},
        {"id": "neg", "created_ts": -5, "due_ts": 0},
        {"id": "huge", "created_ts": 5e9},
        {"id": "future", "created_ts": NOW + 3 * D},
        {"id": "", "created_ts": NOW - D},
        {"created_ts": NOW - D},
        {"id": 7, "created_ts": NOW - D},
        {"id": "nothing"},
        "not a dict", None, 12,
    ]
    assert eng.observe_reminder_facts(junk + [ok], snapshot_at=NOW) == 1
    only = _facts(eng)
    assert len(only) == 1 and only[0].attrs["repeating"] is True
    # A valid row with some bad fields keeps the good ones and nulls the rest; "yes"/1 are not "repeating".
    mixed = {"id": "mix", "created_ts": NOW - D, "due_ts": "tomorrow", "completed_ts": float("nan"), "repeating": "yes"}
    one = {"id": "one", "created_ts": NOW - D, "repeating": 1}
    assert eng.observe_reminder_facts([ok, mixed, one], snapshot_at=NOW) == 2
    for o in _facts(eng):
        if o.attrs["due_ts"] is None:
            assert o.attrs["completed_ts"] is None and o.attrs["repeating"] is False


def test_reminder_facts_carry_no_title_anywhere(eng, db_path):
    row = {"id": "srcid-zq-title", "created_ts": NOW - D, "due_ts": NOW - D + H, "repeating": False,
           "title": "Call Quillfeather about zyxtitle", "notes": "zyxtitle body", "name": "zyxtitle"}
    assert eng.observe_reminder_facts([row] * 1 + [{**row, "id": f"x{i}"} for i in range(11)], snapshot_at=NOW) == 12
    for o in _facts(eng):
        assert o.subject == "" and o.source == "reminders"
        assert set(o.attrs) == {"created_ts", "due_ts", "completed_ts", "repeating"}
        assert o.id.startswith("rf:") and "srcid-zq" not in o.id
    for p in habits.mine_reminder_habits(_facts(eng), [], NOW, TZ):
        assert "title" not in p.stats and "zyxtitle" not in repr(p).lower()
    eng.mine(NOW)
    blob = dump_everything(db_path)
    assert "zyxtitle" not in blob and "quillfeather" not in blob and "srcid-zq" not in blob


def test_reminder_habits_are_mined_by_the_engine_and_ids_are_stable(eng):
    eng.observe_reminder_facts(_reminder_rows(), snapshot_at=NOW)
    eng.mine(NOW)
    first = {p.subject: p.id for p in eng.patterns(kind="reminder_habit")}
    assert set(first) == {"lead_time", "completion_lag", "overdue", "repeating"}
    eng.observe_reminder_facts(_reminder_rows(n=21), snapshot_at=NOW + H)
    eng.mine(NOW + H)
    second = {p.subject: p.id for p in eng.patterns(kind="reminder_habit")}
    assert second == first


def test_measure_confidence_and_span_helpers():
    assert habits.measure_confidence(0) == 0.0 and habits.measure_confidence(20) == 0.5
    assert habits._span(0.5) == "30 minutes" and habits._span(1) == "1 hour" and habits._span(2) == "2 hours"
    assert habits._span(48) == "2 days" and habits._span(-3) == "3 hours"


# ====================================================================== 6. usage
@pytest.mark.parametrize("event, detail, expected", [
    ("ask", "local", "local"), ("ask", "Cloud", "cloud"), ("ask", " local ", "local"),
    ("ask", "remote", None), ("ask", "", None), ("ask", None, None), ("ask", "local cloud", None),
    ("tab_opened", "Study Tab", "study_tab"), ("tab_opened", "brief", "brief"),
    ("tool_used", "web.search", "web.search"), ("tool_used", "mail:read-latest", "mail:read-latest"),
    ("approval", "send_email", "send_email"), ("denial", "x" * 48, "x" * 48),
    ("denial", "x" * 49, None), ("tool_used", "", None), ("tool_used", None, None),
    ("tool_used", "a/b", None), ("tool_used", "Résumé", None), ("tool_used", "~/Documents/Secret", None),
    ("tool_used", "line\nbreak", None), ("tab_opened", "what is my next meeting?", None),
    ("typed", "local", None), ("wisp_ask", "local", None), ("", "local", None), (None, "local", None),
])
def test_clean_detail_accepts_only_closed_events_and_validates_labels(event, detail, expected):
    assert usage.clean_detail(event, detail) == expected


def test_events_and_ask_details_are_closed_sets():
    assert usage.EVENTS == ("ask", "tab_opened", "tool_used", "approval", "denial")
    assert usage.ASK_DETAILS == ("local", "cloud")


def test_record_usage_upserts_counters(eng):
    assert eng.record_usage("ask", "local", ms=1500, now=NOW) is True
    assert eng.record_usage("ask", "local", ms=2500, now=NOW + 60) is True
    assert eng.record_usage("ask", "cloud", ms=900, now=NOW) is True
    rows = {(r["event"], r["detail"]): r for r in eng._store.usage_rows("2000-01-01")}
    local = rows[("ask", "local")]
    assert (local["day"], local["hour"], local["n"], local["total_ms"]) == (TODAY.isoformat(), 8, 2, 4000)
    assert rows[("ask", "cloud")]["n"] == 1 and rows[("ask", "cloud")]["total_ms"] == 900
    # A different hour or day is a different row.
    eng.record_usage("ask", "local", now=NOW + 2 * H)
    eng.record_usage("ask", "local", now=NOW - D)
    assert len(eng._store.usage_rows("2000-01-01")) == 4
    assert [r["day"] for r in eng._store.usage_rows(TODAY.isoformat())] == [TODAY.isoformat()] * 3


def test_record_usage_clamps_time_and_refuses_unknown_events(eng):
    assert eng.record_usage("tool_used", "calendar_read", ms=-50, now=NOW)
    assert eng.record_usage("tool_used", "calendar_read", ms=10_000_000, now=NOW)
    row = eng._store.usage_rows("2000-01-01")[0]
    assert row["n"] == 2 and row["total_ms"] == 600_000
    for event, detail in [("ask", "remote"), ("typed", "x"), ("tool_used", "a b/c"), ("tab_opened", "")]:
        assert eng.record_usage(event, detail, now=NOW) is False
    assert count(eng, "SELECT COUNT(*) FROM usage_counters") == 1


def test_usage_rows_hold_no_text(eng, db_path):
    eng.record_usage("tool_used", "Study Tab zyxprompt?", now=NOW)
    eng.record_usage("tool_used", "study_tab", now=NOW)
    blob = dump_everything(db_path)
    assert "zyxprompt" not in blob and "study_tab" in blob


def urow(day, event, detail="", n=1, hour=9, total_ms=0):
    return {"day": day.isoformat() if not isinstance(day, str) else day, "hour": hour, "event": event,
            "detail": detail, "n": n, "total_ms": total_ms}


def _kinds(patterns):
    return {p.subject: p for p in patterns}


def test_mine_usage_busiest_hours_thresholds():
    day = TODAY - timedelta(days=1)
    assert usage.MIN_USES == 30
    rows = [urow(day, "ask", "local", n=15, hour=9), urow(day, "tab_opened", "brief", n=14, hour=10)]
    assert "busiest_hours" not in _kinds(usage.mine_usage(rows, NOW, TZ))                       # 29 uses
    rows = [urow(day, "ask", "local", n=15, hour=9), urow(day, "tab_opened", "brief", n=15, hour=10)]
    p = _kinds(usage.mine_usage(rows, NOW, TZ))["busiest_hours"]
    assert p.statement == "You use Wisp most between 9–11am (100% of 30 uses in the last four weeks)."
    assert (p.support, p.opportunities) == (30, 30) and p.kind == "usage_stat"
    assert p.window.start_minute == 540 and p.window.end_minute == 660
    assert p.id == pattern_id("usage_stat", "busiest_hours")
    # Spread so thin that no two-hour window reaches 30%: no claim.
    flat = [urow(day, "tab_opened", "brief", n=3, hour=h) for h in range(0, 24)]
    assert "busiest_hours" not in _kinds(usage.mine_usage(flat, NOW, TZ))


def test_mine_usage_lists_need_ten_events():
    day = TODAY - timedelta(days=2)
    nine = [urow(day, "tab_opened", "study", n=5), urow(day, "tab_opened", "brief", n=4)]
    assert "tabs" not in _kinds(usage.mine_usage(nine, NOW, TZ))
    ten = [urow(day, "tab_opened", "study", n=6), urow(day, "tab_opened", "my_day", n=3), urow(day, "tab_opened", "brief", n=1)]
    tabs = _kinds(usage.mine_usage(ten, NOW, TZ))["tabs"]
    assert tabs.statement == "The tabs you open most: Study (6), My day (3), Brief (1)."
    tools = [urow(day, "tool_used", "calendar_read", n=7), urow(day, "tool_used", "mail.search", n=3)]
    assert _kinds(usage.mine_usage(tools, NOW, TZ))["tools"].statement == \
        "The tools Wisp uses most for you: calendar read (7), mail.search (3)."
    assert "tools" not in _kinds(usage.mine_usage([urow(day, "tool_used", "calendar_read", n=9)], NOW, TZ))


def test_mine_usage_ties_sort_by_label_and_top_is_three():
    day = TODAY - timedelta(days=1)
    rows = [urow(day, "tab_opened", t, n=3) for t in ("zeta", "alpha", "mid", "beta")]
    stmt = _kinds(usage.mine_usage(rows, NOW, TZ))["tabs"].statement
    assert stmt == "The tabs you open most: Alpha (3), Beta (3), Mid (3)."


def test_mine_usage_approvals_need_ten_decisions():
    day = TODAY - timedelta(days=1)
    assert "approvals" not in _kinds(usage.mine_usage([urow(day, "approval", "t", n=5), urow(day, "denial", "t", n=4)], NOW, TZ))
    p = _kinds(usage.mine_usage([urow(day, "approval", "t", n=8), urow(day, "denial", "t", n=2)], NOW, TZ))["approvals"]
    assert p.statement == "You approve 80% of the actions Wisp asks about (8 of 10)."
    assert (p.support, p.opportunities) == (8, 10)


def test_mine_usage_local_cloud_and_response_time():
    day = TODAY - timedelta(days=1)
    assert usage.MIN_ASKS == 20
    assert "local_cloud" not in _kinds(usage.mine_usage([urow(day, "ask", "local", n=19, total_ms=19_000)], NOW, TZ))
    only_local = _kinds(usage.mine_usage([urow(day, "ask", "local", n=20, total_ms=40_000)], NOW, TZ))
    assert only_local["local_cloud"].statement == "All 20 of your recent questions were answered on this Mac."
    assert only_local["response_time"].statement == "Answers take about 2 seconds on this Mac on average."
    both = _kinds(usage.mine_usage([urow(day, "ask", "local", n=15, total_ms=30_000),
                                    urow(day, "ask", "cloud", n=25, total_ms=100_000)], NOW, TZ))
    assert both["local_cloud"].statement == ("38% of your recent questions were answered on this Mac "
                                             "and 62% by a cloud model.")
    assert both["local_cloud"].stats["local"] == 15 and both["local_cloud"].stats["cloud"] == 25
    # Only the mode with 20+ timed answers is quoted.
    assert both["response_time"].statement == "Answers take about 4 seconds from a cloud model on average."
    assert "local_seconds" not in both["response_time"].stats and both["response_time"].stats["cloud_seconds"] == 4.0
    untimed = _kinds(usage.mine_usage([urow(day, "ask", "local", n=30)], NOW, TZ))
    assert "response_time" not in untimed and "local_cloud" in untimed


def test_mine_usage_looks_back_only_four_weeks():
    old = TODAY - timedelta(days=usage.LOOKBACK_DAYS + 1)
    edge = TODAY - timedelta(days=usage.LOOKBACK_DAYS)
    assert usage.mine_usage([urow(old, "tab_opened", "study", n=40)], NOW, TZ) == []
    assert "tabs" in _kinds(usage.mine_usage([urow(edge, "tab_opened", "study", n=40)], NOW, TZ))
    assert usage.mine_usage([], NOW, TZ) == []


def test_usage_patterns_flow_through_engine_mine_with_stable_ids(eng):
    for i in range(12):
        eng.record_usage("tab_opened", "study" if i % 3 else "brief", now=local_ts(1 + i % 3, 9))
    eng.mine(NOW)
    first = _kinds(eng.patterns(kind="usage_stat"))
    assert "tabs" in first
    eng.record_usage("tab_opened", "study", now=local_ts(1, 9))
    eng.mine(NOW + H)
    second = _kinds(eng.patterns(kind="usage_stat"))
    assert second["tabs"].id == first["tabs"].id == pattern_id("usage_stat", "tabs")
    assert second["tabs"].stats["samples"] == 13


# ---- weekly report -----------------------------------------------------------
def test_usage_report_covers_seven_completed_local_days():
    start = TODAY - timedelta(days=7)
    inside = [urow(start + timedelta(days=i), "ask", "local", n=1) for i in range(7)]
    outside = [urow(TODAY, "ask", "local", n=50), urow(start - timedelta(days=1), "ask", "local", n=50)]
    rep = usage.usage_report(inside + outside, NOW, TZ)
    assert rep["available"] is True
    assert rep["start_date"] == start.isoformat() and rep["end_date_exclusive"] == TODAY.isoformat()
    assert rep["lines"][0].startswith("Questions asked: 7")        # today (partial) and day -8 are excluded
    assert rep["lines"][1] == "Answered on this Mac: 7 of 7"


def test_usage_report_has_baseline_only_when_counting_started_before_the_baseline_window():
    start = TODAY - timedelta(days=7)
    base_start = start - timedelta(days=28)
    week = [urow(start + timedelta(days=1), "ask", "local", n=5)]
    base = [urow(base_start, "ask", "local", n=8)]
    with_base = usage.usage_report(base + week, NOW, TZ)
    assert with_base["lines"][0] == "Questions asked: 5 (previous four-week average 2; change +3)"
    assert not any(line.startswith("Earlier weeks are not compared") for line in with_base["lines"])
    # Counting began a day after the baseline window opened: no comparison, and it says why.
    late = [urow(base_start + timedelta(days=1), "ask", "local", n=8)]
    without = usage.usage_report(late + week, NOW, TZ)
    assert without["lines"][0] == "Questions asked: 5"
    assert without["lines"][-1] == f"Earlier weeks are not compared: counting began on {(base_start + timedelta(days=1)).isoformat()}."
    # The only data being from this week also has no baseline.
    only = usage.usage_report(week, NOW, TZ)
    assert only["lines"][-1] == f"Earlier weeks are not compared: counting began on {week[0]['day']}."


def test_usage_report_not_available_without_week_data():
    for rows in ([], [urow(TODAY, "ask", "local")], [urow(TODAY - timedelta(days=8), "ask", "local")]):
        rep = usage.usage_report(rows, NOW, TZ)
        assert rep["available"] is False and rep["lines"] == [] and "first full day of use" in rep["note"]
        assert usage.render_lines(rep) == [rep["note"]]


def test_usage_report_lines_and_render():
    start = TODAY - timedelta(days=7)
    d = start + timedelta(days=2)
    rows = [urow(d, "ask", "local", n=3, hour=9, total_ms=6000), urow(d, "ask", "cloud", n=1, hour=10, total_ms=3000),
            urow(d, "tab_opened", "study", n=4, hour=9), urow(d, "tool_used", "calendar_read", n=2, hour=9),
            urow(d, "approval", "send", n=2), urow(d, "denial", "send", n=1)]
    rep = usage.usage_report(rows, NOW, TZ)
    assert rep["lines"][:3] == ["Questions asked: 4", "Answered on this Mac: 3 of 4; by a cloud model: 1",
                                "Average response time: 2.2 seconds"]
    assert "Tabs opened: 4 (Study 4)" in rep["lines"] and "Tool uses: 2 (calendar read 2)" in rep["lines"]
    assert "Actions approved: 2; declined: 1" in rep["lines"]
    assert any(line.startswith("Busiest hours: 9–10am (7)") for line in rep["lines"])
    out = usage.render_lines(rep)
    assert out[0] == "Wisp usage (counts only):" and all(line.startswith("- ") for line in out[1:])


def test_weekly_includes_usage_and_the_text_mentions_it(eng):
    start = TODAY - timedelta(days=7)
    for i in range(3):
        eng.record_usage("ask", "local", ms=1000, now=local_ts(3, 9, i))
    eng.record_usage("tab_opened", "study", now=local_ts(3, 9))
    eng.record_usage("ask", "local", now=NOW)                     # today: not a completed day
    report = eng.weekly(NOW)
    assert "usage" in report and report["usage"]["available"] is True
    assert report["usage"]["start_date"] == start.isoformat()
    assert report["usage"]["lines"][0].startswith("Questions asked: 3")
    assert "Wisp usage (counts only):" in report["summary"] and "Questions asked: 3" in report["summary"]


def test_weekly_without_any_usage_still_has_the_key_and_a_note(eng):
    report = eng.weekly(NOW)
    assert report["usage"]["available"] is False
    assert report["usage"]["note"] in report["summary"]


def test_weekly_report_without_usage_rows_has_no_usage_key():
    from service.behavior.weekly import render_report, weekly_report
    report = weekly_report([], [], NOW, TZ)
    assert "usage" not in report
    assert "Wisp usage" not in render_report(report)


# ====================================================================== boundary
@pytest.mark.parametrize("module", ["writing", "habits", "usage"])
def test_new_modules_import_nothing_live(module):
    from tests.test_behavior_boundary import BANNED, PACKAGE, _assert_portable_import, _imports
    path = PACKAGE / f"{module}.py"
    assert path.is_file()
    names = list(_imports(path))
    assert names, "the module imports nothing at all?"
    for name in names:
        assert not any(name == b or name.startswith(b + ".") for b in BANNED), f"{module}.py imports {name}"
        _assert_portable_import(name)
        assert name.split(".")[0] != "httpx"
