"""Compare ext_v4_personal.agenda() with the live get_upcoming formatter in service/tools/assistant_tools.py.

Run from the repo root's venv: TZ=America/Los_Angeles MOE_DIR=<scratch> .venv/bin/python check_formats.py
(The formatter prints the local zone, so the check runs in US Pacific, the zone the rows use.) Writes nothing.
"""
import datetime as dt
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
import ext_v4_personal as X
from service.tools import assistant_tools as A


def live(t, label, items, schedule, display_start):
    rows = [{"when_ts": dt.datetime(d.year, d.month, d.day, h, m).timestamp(), "title": title, "location": place,
             "source": kind} for d, h, m, title, place, kind in items]
    rows.sort(key=lambda row: row["when_ts"])   # get_upcoming hands the formatter time-ordered rows
    return A._format_forward_agenda(rows, now=t.timestamp(), window_label=label, title="Schedule" if schedule else "Upcoming",
                                    display_day_start=display_start)


def main():
    bad = 0
    for i in range(300):
        r = random.Random(i)
        t, _ = X._now(r)
        items = X._items_today(r, t, r.randint(1, 3)) + [X._reminder(r, t, t.date() + dt.timedelta(days=r.randint(0, 2)), 9)]
        start = dt.datetime.combine(t.date(), dt.time()).timestamp()
        for label, schedule in (("today", True), ("next 7 day(s)", False)):
            mine, theirs = X.agenda(t, label, items, schedule=schedule), live(t, label, items, schedule, start)
            if mine != theirs:
                bad += 1
                if bad <= 2:
                    print("MISMATCH\n--- mine\n" + mine + "\n--- live\n" + theirs)
    print("PASS" if not bad else f"FAIL {bad}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
