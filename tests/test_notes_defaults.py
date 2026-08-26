"""search_notes' default breadth — browse wide, search narrow.

`count` defaulted to 5 for every call. That is right for a targeted search
("search my notes for latte" — the caller already narrowed it) and far too thin
for a browse.

Measured 2026-08-09 on "What's in my notes?": the model called search_notes({}),
got exactly 5 notes back, listed 5, and closed with "would you like more details
on any specific note?" — with ~100 notes sitting in the cache. An earlier run of
the identical prompt listed 12, because that time the model happened to reason
its way to passing a bigger `count`. Same question, threefold difference in the
answer, decided by a coin flip.

Fixed in the TOOL rather than by asking the model to pass a count — the same
principle tools/timeranges.py states for dates: Wisp resolves it, not the model.

    .venv/bin/python tests/test_notes_defaults.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent.loop import _MAX_TOOL_RESULT_CHARS  # noqa: E402
from service.tools import notes_tools  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def call(**kw) -> str:
    return asyncio.run(notes_tools.search_notes_impl(**kw))


def n_notes(out: str) -> int:
    return out.count("Title:")


CACHED = bool(notes_tools._parse()) if hasattr(notes_tools, "_parse") else False


def test_browse_is_wider_than_search() -> None:
    print("\nbrowse returns more than a targeted search")
    check("the browse default is bigger than the search default",
          notes_tools._BROWSE_COUNT > notes_tools._SEARCH_COUNT,
          f"{notes_tools._BROWSE_COUNT} vs {notes_tools._SEARCH_COUNT}")
    if not CACHED:
        check("(no notes cached on this machine — live checks skipped)", True)
        return
    browse = call()
    check(f"a no-query browse returns up to {notes_tools._BROWSE_COUNT} notes",
          n_notes(browse) > notes_tools._SEARCH_COUNT, f"got {n_notes(browse)}")


def test_explicit_count_still_wins() -> None:
    print("\nan explicit count from the model is always respected")
    if not CACHED:
        check("(no notes cached — skipped)", True)
        return
    check("count=3 returns 3", n_notes(call(count=3)) == 3, f"{n_notes(call(count=3))}")
    check("count=1 returns 1", n_notes(call(count=1)) == 1)


def test_browse_fits_the_tool_result_cap() -> None:
    print("\nthe wider browse still fits the loop's tool-result cap")
    # Over _MAX_TOOL_RESULT_CHARS the result is truncated by _fit_tool_result,
    # which would trade one thin answer for a different thin answer.
    if not CACHED:
        check("(no notes cached — skipped)", True)
        return
    out = call()
    check(f"browse result ({len(out)} chars) is under the {_MAX_TOOL_RESULT_CHARS} cap",
          len(out) <= _MAX_TOOL_RESULT_CHARS, f"{len(out)} chars would be truncated")


def test_no_match_fallback_still_widens() -> None:
    print("\na query with no match still falls back to recent notes")
    if not CACHED:
        check("(no notes cached — skipped)", True)
        return
    out = call(query="zzz_no_such_note_zzz")
    check("says it found no exact match", "no exact match" in out, out[:120])
    check("still shows something to look through", n_notes(out) > 0, out[:120])


if __name__ == "__main__":
    test_browse_is_wider_than_search()
    test_explicit_count_still_wins()
    test_browse_fits_the_tool_result_cap()
    test_no_match_fallback_still_widens()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
