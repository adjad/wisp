"""The quoted-history patterns every mail body passes through each Brief cycle.

Security review findings 4 and 5: on a crafted body the old patterns took tens of
seconds (`^\\s*` under re.M ran across newlines at every line start, and the
multi-line attribution forms backtracked), and the widened "On … wrote:" and
underscore markers cut ordinary prose. Timing is CPU time, never wall clock, with
a bound far above what a linear pattern needs, so only a return to super-linear
behaviour fails.
"""
from __future__ import annotations

import time

import pytest

from service.assistant.daily_workspace import _QUOTED_HISTORY
from service.proactive import sources

TOP = "Can you send the notes by Friday?\n\n"
OLD = "\nThe recital is Sat Oct 31 at 2pm.\n> older"


def cut(body: str) -> str:
    return _QUOTED_HISTORY.split(body)[0]


# --- Finding 4: linear on hostile bodies -------------------------------------------------------

_PARA = "Hi Maya, thanks for the notes on the recital program. I think the order works.\n" * 3 + "\n"
HOSTILE = {
    "space_newline": " \n" * 16_000,
    "newlines": "\n" * 50_000,
    "on": "On " * 20_000,
    "from_lines": "From: a@b\n" * 5_000,
    "underscores": "_" * 20_000,
    "quote_marks": ">" * 40_000,
    "spaces": " " * 100_000,
    "dashes": "-" * 100_000,
    "from_at": "From: " + "@" * 100_000,
    "schrieb": "Am 2026 " + "schrieb " * 12_000,
    "on_lines": "On 2026 x\n" * 10_000,
    "ordinary_200k": (TOP + _PARA * 900)[:200_000] + "\nOn Mon, Nov 9, 2026 at 10:05 AM Maya <m@x.com> wrote:\n> a",
}
PATTERNS = {
    "quoted_history": lambda text: (_QUOTED_HISTORY.split(text), _QUOTED_HISTORY.search(text)),
    "history_hint": sources._HISTORY_HINT.search,
    "header_line": sources._HEADER_LINE.findall,
    "quoting": sources._QUOTING.search,
}
# Measured at most ~0.01 s of CPU for any pair here (2026-11-12, M5 Pro); the old
# patterns took 2-107 s on several of them.
CPU_BOUND = 0.5


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("name", HOSTILE)
def test_each_history_pattern_is_linear_on_a_hostile_body(pattern, name):
    run, text = PATTERNS[pattern], HOSTILE[name]
    started = time.process_time()
    run(text)
    spent = time.process_time() - started
    assert spent < CPU_BOUND, f"{pattern} on {name}: {spent:.3f} s of CPU"


def test_the_whole_body_timing_check_is_fast_on_a_hostile_body():
    newest = {"ts": 1.0, "body": " \n" * 16_000 + "From: a@b\n" * 5_000, "subject": "Hi"}
    started = time.process_time()
    sources._body_timing([newest], newest)
    sources._excerpt(newest)
    assert time.process_time() - started < CPU_BOUND


# --- Finding 5: real attribution lines are cut, prose is not ------------------------------------

REAL = {
    "gmail": "On Mon, Nov 9, 2026 at 10:05 AM Maya Chen <maya@friend.com> wrote:",
    "gmail_two_lines": "On Mon, Nov 9, 2026 at 10:05 AM Maya Chen\n<maya@friend.com> wrote:",
    "gmail_three_lines": "On Mon, Nov 9, 2026 at\n10:05 AM Maya Chen\n<maya@friend.com> wrote:",
    "gmail_wrote_alone": "On Mon, Nov 9, 2026 at 10:05 AM Maya Chen <maya@friend.com>\nwrote:",
    "apple": "On Nov 9, 2026, at 10:05 AM, Maya Chen <maya@friend.com> wrote:",
    "outlook": ("From: Maya Chen <maya@friend.com>\nSent: Monday, November 9, 2026 10:05 AM\n"
                "To: Me <me@school.edu>\nSubject: Recital"),
    "outlook_address_date": "From: Maya Chen <maya@friend.com>\nDate: Mon, Nov 9, 2026\nSubject: Recital",
    "outlook_rule": "________________________________",
    "original_message": "-----Original Message-----",
    "original_message_spaced": "----- Original Message -----",
    "gmail_forward": "---------- Forwarded message ---------",
    "apple_forward": "Begin forwarded message:",
    "quoted": "> The recital is Sat Oct 31.",
    "german": "Am 09.11.2026 um 10:05 schrieb Maya Chen:",
    "german_gmail": "Am Mo., 9. Nov. 2026 um 10:05 Uhr schrieb Maya Chen <maya@friend.com>:",
    "french": "Le 9 nov. 2026 à 10:05, Maya Chen a écrit :",
    "spanish": "El 9 nov 2026, a las 10:05, Maya Chen escribió:",
    # Short single-line forms the old pattern accepted (and existing tests rely on).
    "weekday_only": "On Monday Taylor wrote:",
    "short_weekday": "On Mon, Bob wrote:",
}


@pytest.mark.parametrize("name", REAL)
def test_a_real_attribution_or_separator_is_cut(name):
    body = TOP + REAL[name] + OLD
    assert cut(body).strip() == TOP.strip(), name
    assert _QUOTED_HISTORY.search(body)


PROSE = {
    "on_friday_sarah_wrote": "On Friday the team met.\nHere is what Sarah wrote:\nthe plan is fine.",
    "fill_in_line": "Please fill this in.\nName: ____________________\nSigned: ____________________",
    "on_without_wrote": "On Mon, Nov 9 we meet at 10:05 in the lab.",
    "wrote_mid_line": "On Monday Sarah wrote: the plan is fine.",
    "from_without_sent": "Hello\nFrom: the desk of the dean\nThe plan is fine.",
}


@pytest.mark.parametrize("name", PROSE)
def test_ordinary_prose_is_not_cut(name):
    body = TOP + PROSE[name]
    assert cut(body) == body
    assert _QUOTED_HISTORY.search(body) is None


def test_the_cut_is_at_the_first_marker():
    body = TOP + "On Mon, Bob wrote:\n> one\n-----Original Message-----\ntwo"
    assert cut(body) == TOP


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_mail_history_regex.py"' in manifest
