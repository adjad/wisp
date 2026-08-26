"""move_path, and the summary paths that silently under-reported.

Three findings from the 2026-08-16 debug export, all "the model was never given
what it needed and nobody said so":

  * REORGANIZING had no primitive. The file route offered read_file / list_dir /
    write_file / run_shell — nothing that moves anything — so "reorganize the
    wisp debug logs" fell back to shell and `rm -rf`'d the sources. move_path
    exists so the job has a tool that CANNOT delete.
  * MESSAGES were sliced newest-30 across all chats combined. Measured on the
    real export: 21 of 30 rows were one group chat, so a quieter conversation
    with genuinely recent activity was never shown to the model at all.
  * EMAILS were sliced newest-20 and summarized as "your recent inbox", wording
    that asserts a completeness it does not have.

    .venv/bin/python tests/test_move_and_coverage.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.tools.builtin import move_path  # noqa: E402
from service.tools.imessage_tools import _recent_rows  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_move_path_basics() -> None:
    print("\nmove_path moves, renames, and creates the destination folder")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "log.json"
        src.write_text("x")

        # Into a folder that does not exist yet — no mkdir step needed.
        out = move_path(str(src), str(root / "Logs" / "August") + "/")
        check("moved into a freshly created folder", "moved" in out, out)
        check("the file is there", (root / "Logs" / "August" / "log.json").exists())
        check("the source is gone", not src.exists())

        # Rename to an exact path.
        a = root / "a.txt"
        a.write_text("y")
        out = move_path(str(a), str(root / "b.txt"))
        check("renamed to an exact path", (root / "b.txt").exists(), out)

        # A whole folder moves too — that is what reorganizing usually needs.
        d = root / "DebugLogs"
        d.mkdir()
        (d / "inner.txt").write_text("z")
        out = move_path(str(d), str(root / "Archive") + "/")
        check("a folder moves with its contents",
              (root / "Archive" / "DebugLogs" / "inner.txt").exists(), out)


def test_move_path_never_destroys() -> None:
    print("\nmove_path refuses the cases that would lose data")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "one.txt"
        src.write_text("keep me")
        victim = root / "two.txt"
        victim.write_text("do not clobber")

        out = move_path(str(src), str(victim))
        check("refuses to overwrite an existing destination", "refusing" in out, out)
        check("the destination is untouched", victim.read_text() == "do not clobber")
        check("the source is still there", src.exists())

        out = move_path(str(root / "nope.txt"), str(root / "x.txt"))
        check("a missing source reports plainly", "no such path" in out, out)


def test_recent_messages_are_sampled_per_conversation() -> None:
    print("\none busy chat can no longer hide every other conversation")
    # Shaped like the real export: a dominant group chat plus quieter threads.
    rows = []
    ts = 10_000
    for i in range(40):                      # the loud one, newest
        rows.append((ts - i, 'Group "Grad GC"', f"loud {i}"))
    ts -= 40
    for name in ('Trishe', 'Group "Comp"', 'Group "Muted GC"', 'Mom'):
        for i in range(3):
            ts -= 1
            rows.append((ts, name, f"{name} {i}"))
    rows.sort(key=lambda r: -r[0])

    old = rows[:30]                          # the previous behaviour
    new, dropped = _recent_rows(rows, 30)

    check("the OLD flat slice showed only the loud chat",
          {r[1] for r in old} == {'Group "Grad GC"'}, f"{ {r[1] for r in old} }")
    check("the NEW sample shows every recent conversation",
          {r[1] for r in new} == {'Group "Grad GC"', 'Trishe', 'Group "Comp"',
                                  'Group "Muted GC"', 'Mom'},
          f"{ {r[1] for r in new} }")
    check("it still respects the count budget", len(new) <= 30, str(len(new)))
    check("the loud chat still dominates (it is genuinely most active)",
          sum(1 for r in new if r[1] == 'Group "Grad GC"') >= 4)
    check("nothing was dropped, so nothing is disclosed", dropped == [], f"{dropped}")


def test_dropped_conversations_are_disclosed() -> None:
    print("\nconversations that genuinely don't fit are NAMED, not silently cut")
    rows = []
    ts = 10_000
    for c in range(15):                      # more conversations than any budget
        for i in range(4):
            ts -= 1
            rows.append((ts, f"Chat {c}", f"m{i}"))
    rows.sort(key=lambda r: -r[0])
    new, dropped = _recent_rows(rows, 12)
    check("the budget is respected", len(new) <= 12, str(len(new)))
    check("something was dropped", len(dropped) > 0)
    shown = {r[1] for r in new}
    check("dropped names are exactly the ones NOT shown",
          all(d not in shown for d in dropped), f"{dropped} vs {shown}")


if __name__ == "__main__":
    test_move_path_basics()
    test_move_path_never_destroys()
    test_recent_messages_are_sampled_per_conversation()
    test_dropped_conversations_are_disclosed()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
