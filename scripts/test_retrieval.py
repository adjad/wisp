#!/usr/bin/env python3
"""Measure semantic tool retrieval — does the right tool make the menu?

    .venv/bin/python scripts/test_retrieval.py            # full eval
    .venv/bin/python scripts/test_retrieval.py -v         # show every miss
    .venv/bin/python scripts/test_retrieval.py "some query"   # ad-hoc probe

WHAT THIS MEASURES, AND WHY IT IS THE RIGHT NUMBER
--------------------------------------------------
RECALL, not precision. The model picks from whatever menu it is handed, and this
roster measures 10/10 correct selection off a 16-tool menu — so a few irrelevant
neighbors in the candidate set are affordable. A tool that never makes the menu
is not: the model cannot call what it was not offered, and the observed failure
mode is not an error but a confident answer explaining the user's request is
impossible (see the `_write_continuation_subset` incident in router.py).

So the pass bar is "the expected tool is in the candidate set", and the headline
number is recall@k. Precision shows up only as MENU SIZE, reported alongside,
because every extra candidate costs prompt tokens and a little selection risk.

THE HELD-OUT SET MATTERS
------------------------
Queries drawn from `tool_aliases.ALIASES` are the tools' own training text —
scoring them measures nothing except that cosine similarity works. They are
still run, separately labelled, as a sanity floor: a miss there means something
is broken, not that retrieval is hard. The number that means something is the
HELD-OUT set below, whose phrasings deliberately avoid the vocabulary in both
the alias table and the tool descriptions.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import service.tools  # noqa: E402,F401  (registers every tool module)
from service.router import semantic  # noqa: E402
from service.router.router import has_write_intent  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

# (query, expected tool, is_write_intent)
#
# Phrasings a real person would use that do NOT reuse the tool's own words. Each
# one is a request whose correct answer is unambiguous — if a query could
# legitimately be served by two tools, both are listed and either counts.
HELD_OUT: list[tuple[str, tuple[str, ...], bool]] = [
    # calendar / reminders
    ("am i busy thursday", ("get_upcoming",), False),
    ("do i have anything on tonight", ("get_upcoming",), False),
    ("stick a haircut in for saturday morning", ("add_calendar_event",), True),
    ("i need to be reminded about the gas bill", ("add_reminder",), True),
    ("scratch the thing on wednesday", ("cancel_event",), True),
    ("what was i doing this time last month", ("get_past_events",), False),
    # mail
    ("anything worth reading in my mail", ("summarize_emails",), False),
    ("write back to her and say it's fine", ("reply_to_email",), True),
    ("fire off a note to the accountant", ("send_email", "draft_email"), True),
    ("what were the exact words in that message from the bank",
     ("view_emails", "view_messages"), False),
    ("stop showing me the newsletter in my inbox", ("archive_email",), True),
    # messages
    ("let her know i'm on my way", ("send_message",), True),
    ("did anyone text me", ("summarize_messages",), False),
    ("what number do i have for the plumber", ("lookup_contact",), False),
    # notes / memory
    ("i scribbled the wifi password down somewhere", ("search_notes",), False),
    ("keep in mind i hate mushrooms", ("remember",), True),
    ("what do you know about my apartment", ("recall",), False),
    # files
    ("where'd that invoice pdf end up", ("read_file", "list_dir", "run_shell"), False),
    ("my downloads folder is a disaster", ("move_path", "list_dir"), True),
    ("what's sitting on my desktop right now", ("list_dir",), False),
    ("pull the key dates out of this lease", ("read_file",), False),
    # system
    ("it's way too loud", ("set_volume",), True),
    ("how much juice have i got", ("get_battery_status",), False),
    ("i'm stepping away from the desk", ("lock_screen",), True),
    ("the internet feels sluggish", ("run_speed_test",), False),
    ("are my headphones hooked up", ("list_bluetooth_devices",), False),
    # apps / media
    # NOT "put something on in the background" — that phrasing is now in the
    # alias table (it was the probe that exposed the gap), so scoring it here
    # would be scoring training text and quietly inflate this set.
    ("queue up my workout mix", ("music", "spotify"), False),
    ("kill chrome for me", ("quit_app",), True),
    ("fire up xcode", ("open_app",), True),
    ("i'm sick of this song", ("music", "spotify"), False),
    # web / data
    ("should i bring a jacket", ("get_weather",), False),
    ("how'd the market treat nvidia today", ("get_stock_price",), False),
    ("summarize whatever is at this link", ("web_fetch",), False),
    ("what was that site i was looking at last week",
     ("search_browser_history",), False),
    # activity
    ("bring me up to speed", ("get_recent_activity",), False),
    ("what did i just copy", ("clipboard_read",), False),
    # --- Capability Atlas Phase 1 (T1) ---
    # file search: the diagnosis PDF's #1 gap. run_shell counts as a miss here
    # on purpose — falling back to `find` is the behavior find_files replaces.
    ("which folder did i leave the lease agreement in", ("find_files",), False),
    ("hunt down anything with 'invoice' in the name", ("find_files",), False),
    ("i can't remember where the screenshots go", ("find_files",), False),
    # timers — must not collapse into add_reminder
    ("buzz me in twenty minutes", ("set_timer",), True),
    ("wake me at quarter past six", ("set_alarm",), True),
    ("how long till the pasta is done", ("manage_timers",), False),
    ("kill the countdown", ("manage_timers",), True),
    ("cut the music off in an hour", ("set_sleep_timer",), True),
    ("begin counting up from zero", ("stopwatch",), False),
    # arithmetic — must not be answered from the model's own head
    ("knock 30 percent off 89.99", ("calculate",), False),
    ("what do i owe if we split 212 between five of us", ("calculate",), False),
    ("how many kilos is 180 pounds", ("convert_units",), False),
    ("is 350 fahrenheit hot in celsius", ("convert_units",), False),
    # notes: writing, not just searching
    ("scribble this down so I don't lose it", ("create_note",), True),
    ("stick eggs on the grocery list", ("append_note", "manage_lists"), True),
    # the clock — the model has none of its own
    ("what's the date today", ("world_time",), False),
    ("is it morning yet in singapore", ("world_time",), False),
    # system + windows
    ("this screen is searing my retinas", ("set_display",), True),
    ("grab a picture of what i'm looking at", ("screen_capture",), True),
    ("silence my notifications for a bit", ("toggle_setting",), True),
    ("am i about to run out of disk", ("system_status",), False),
    ("shove this window to the left half", ("window_control",), True),
    ("bring my browser forward", ("switch_app",), True),
    ("what have i got open right now", ("list_running_apps",), False),
    # maps
    ("somewhere nearby for a flat white", ("find_place",), False),
    ("how long is the drive to sacramento", ("travel_time",), False),
    ("navigate me to the nearest hospital", ("get_directions", "find_place"), True),
    # composites
    ("give me the rundown for today", ("daily_brief",), False),
    ("i've misplaced my phone again", ("find_my_device",), False),
    # scheduling
    # NOT "cross it off" — that phrasing is now in the alias table (it was the
    # probe that exposed the gap), so scoring it here would be scoring training
    # text. This is the same shape of request in words the index has not seen.
    ("already took care of the dentist thing", ("complete_reminder",), True),
    ("when have i got a spare hour", ("find_free_time",), False),
    # the rest of T1
    ("is a tornado headed my way", ("weather_alerts",), False),
    ("what's the word for when something happens by lucky accident",
     ("define_word",), False),
    ("get my mother on the line", ("place_call",), True),
    ("which model is answering me right now", ("wisp_status",), False),
    # search phrasings must reach view_emails, not a duplicate search tool
    ("dig up the email about the apartment", ("view_emails", "summarize_emails"), False),
]


async def _score(cases: list[tuple[str, tuple[str, ...], bool]], k: int,
                 verbose: bool) -> tuple[int, int, float, list[str]]:
    hits = 0
    sizes = 0
    misses: list[str] = []
    for query, expected, writing in cases:
        got = await semantic.candidates(query, writing=writing, k=k)
        sizes += len(got)
        if any(e in got for e in expected):
            hits += 1
        else:
            rank = await semantic.index().rank(query)
            pos = {n: i for i, (n, _) in enumerate(rank)}
            where = ", ".join(f"{e}@{pos.get(e, '?')}" for e in expected)
            misses.append(f"    {query!r}\n      wanted {where}  got {got}")
    if verbose and misses:
        print("\n".join(misses))
    return hits, len(cases), sizes / max(len(cases), 1), misses


async def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv

    n = await semantic.warm()
    idx = semantic.index()
    print(f"index: {len(set(idx._owner))} tools / {len(idx._rows)} vectors "
          f"({n} embedded this run)\n")

    if argv:  # ad-hoc probe
        query = " ".join(argv)
        ranked = await idx.rank(query)
        print(f"{query!r}\n")
        for name, score in ranked[:12]:
            print(f"   {score:.4f}  {name}")
        print(f"\n  offered (read):  {await semantic.candidates(query, writing=False)}")
        print(f"  offered (write): {await semantic.candidates(query, writing=True)}")
        return 0

    # Sanity floor: the tools' own alias text. A miss here is a broken index.
    #
    # Read off the REGISTRY, not the alias table — new tools declare aliases
    # inline in their own register() call, and only the registry sees both
    # sources. Reading the table alone silently skipped every tool added after
    # retrieval shipped, which is exactly the set most in need of checking.
    #
    # Write intent is DERIVED, not hardcoded False. Getting this wrong made the
    # first run of this harness read 124/160 with no scoring problem at all:
    # every one of the 36 "misses" was a gated tool (send_email, move_path,
    # add_calendar_event, …) that `writing=False` makes unreachable by
    # construction. An eval that mislabels its own inputs measures the labelling.
    seeded = [(a, (name,), has_write_intent(a))
              for name, tool in sorted(REGISTRY.items()) for a in tool.aliases]
    s_hit, s_tot, s_size, _ = await _score(seeded, semantic.DEFAULT_K, False)
    print(f"seeded  (alias text, sanity floor): {s_hit}/{s_tot} "
          f"= {100 * s_hit / s_tot:.1f}%   mean menu {s_size:.1f}")

    h_hit, h_tot, h_size, misses = await _score(HELD_OUT, semantic.DEFAULT_K, verbose)

    # Confirmations retrieve against the ASSISTANT'S OFFER, not the user's
    # "yes". Checked here rather than in tests/test_phase1_tools.py because it
    # needs the real embedder: under the offline word-overlap stub, which tool
    # lands in the top 10 is a property of the stub.
    print("\nconfirmations (retrieved from the assistant's offer)")
    from service.router.router import route as _route
    for offer, want in [
        ("I found 3 emails from the landlord. Want me to archive them?", "archive_email"),
        ("Shall I set a timer for 20 minutes?", "set_timer"),
        # trash_file, not delete_path — casual "delete" language is exactly
        # what trash_file's own description claims ("ordinary delete/remove
        # requests; it's the safer default"), and it correctly displaced
        # delete_path from this confirmation once it existed. Not a
        # regression: recoverable-by-default is the right answer here.
        ("I can delete those old screenshots for you. Want me to?", "trash_file"),
    ]:
        d = await _route("yes go ahead", last_assistant=offer)
        got = d.tool_subset or []
        ok = want in got
        print(f"  {'ok  ' if ok else 'MISS'} {want:14} <- {offer[:46]!r}")
        if not ok and verbose:
            print(f"       got {got}")
    print(f"held-out (unseen phrasings)       : {h_hit}/{h_tot} "
          f"= {100 * h_hit / h_tot:.1f}%   mean menu {h_size:.1f}")

    if misses and not verbose:
        print(f"\n  ({len(misses)} misses — rerun with -v to see them)")

    # 95% on held-out is the bar from the Phase 0 plan.
    ok = h_hit / h_tot >= 0.95
    print("\nPASS" if ok else "\nBELOW BAR (want >=95% held-out)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
