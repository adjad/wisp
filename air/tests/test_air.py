"""End-to-end exercise of the Air node without any of the Air's hardware.

Runs against a stub model server, so it verifies the parts that are actually
easy to get wrong — wire-format parsing, dedupe, cursor monotonicity, rollover,
reminder dedupe, ack semantics, and the sleep-aware scheduler — on any machine.

    WISPAIR_HOME=/tmp/wispair-test python3 tests/test_air.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

# Scratch state dir before importing the package (config reads it at import).
SCRATCH = tempfile.mkdtemp(prefix="wispair-test-")
os.environ["WISPAIR_HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wispair import config, jobs, model, readers, scheduler, store  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# --------------------------------------------------------------------------

def test_email_parsing() -> None:
    print("\nemail header parsing")
    # Scientific notation is what AppleScript actually emits.
    text = (
        "1.783977044E+9 | Gmail | Sarah Chen | Invoice #4021 is due Friday\n"
        "1783970000.0 | Gmail | Canvas | Assignment posted: PS4 | part two\n"
        "garbage line without pipes\n"
        # An empty account: AppleScript concatenates " | " + "" + " | ", which
        # really does emit two spaces between the pipes.
        "1783960000 |  | No Account Person | Hello\n"
        "\n"
    )
    items = readers.parse_email_headers(text)
    check("parses 3 valid lines, drops garbage", len(items) == 3, f"got {len(items)}")
    check("scientific notation → epoch", abs(items[0]["ts"] - 1783977044) < 1)
    check("pipe inside subject preserved",
          items[1]["payload"].endswith("PS4 | part two"), items[1]["payload"])
    check("blank account renders cleanly",
          items[2]["payload"] == "From No Account Person: Hello", items[2]["payload"])

    bad = readers.parse_email_headers("999 | a | b | c\n99999999999 | a | b | c")
    check("rejects out-of-range timestamps", bad == [], str(bad))

    # Both spacings must land in the same fields.
    tight = readers.parse_email_headers("1783960000|Gmail|Sender|Subject here")
    check("tolerates no spaces around separators",
          len(tight) == 1 and tight[0]["payload"] == "From Sender [Gmail]: Subject here",
          str(tight))


def test_message_parsing() -> None:
    print("\nmessage line parsing")
    text = (
        "1783977044 | Group \"Climbing\" | Me: on my way\n"
        "1783977000 | +15551234567 | +15551234567: can you send the | thing\n"
    )
    items = readers.parse_message_lines(text)
    check("parses both lines", len(items) == 2, f"got {len(items)}")
    check("pipe inside body preserved",
          items[1]["payload"].endswith("send the | thing"), items[1]["payload"])
    check("context bracketed", items[0]["payload"].startswith("[Group \"Climbing\"]"))


def test_dedupe_and_cursor() -> None:
    print("\nitem dedupe + cursor monotonicity")
    store.init()
    now = time.time()
    batch = [
        {"ts": now - 100, "dedupe_key": "a", "payload": "one"},
        {"ts": now - 50, "dedupe_key": "b", "payload": "two"},
    ]
    check("first push inserts 2", store.add_items("email", batch) == 2)
    check("re-push inserts 0 (the 24h overlap case)",
          store.add_items("email", batch) == 0)

    store.set_cursor("email", now)
    store.set_cursor("email", now - 5000)
    check("cursor never rewinds", store.get_cursor("email") == now,
          str(store.get_cursor("email")))


def test_todo_parsing() -> None:
    print("\nmodel response parsing")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    resp = f"""SUMMARY:
Sarah sent an invoice due Friday. Canvas posted a new assignment.
Nothing else needed attention.

TODO: Pay invoice #4021 | {tomorrow}T14:00:00
TODO: Submit PS4 | none
TODO: Call the dentist
TODO:    | 2026-01-01T00:00:00
"""
    summary, todos = jobs.parse_response(resp)
    check("summary text extracted", summary.startswith("Sarah sent an invoice"), summary[:60])
    check("TODO lines stripped from summary", "TODO:" not in summary)
    check("3 valid todos (blank title dropped)", len(todos) == 3, str(todos))
    check("ISO datetime parsed", todos[0][1] is not None and todos[0][1] > time.time())
    check("'none' → no due date", todos[1][1] is None)
    check("missing separator keeps the todo", todos[2][0] == "Call the dentist")

    # A past date must not become a live alarm.
    _, past = jobs.parse_response("TODO: Old thing | 2024-01-01T09:00:00")
    check("past due date rejected", past[0][1] is None, str(past))

    # Date-only gets a 9am default rather than a midnight alarm.
    _, dateonly = jobs.parse_response(f"TODO: Thing | {tomorrow}")
    ts = dateonly[0][1]
    check("date-only defaults to 09:00",
          ts is not None and datetime.fromtimestamp(ts).hour == 9,
          str(datetime.fromtimestamp(ts)) if ts else "None")

    # No format at all — must still yield a usable summary, not nothing.
    s2, t2 = jobs.parse_response("Just some prose with no headers at all.")
    check("unformatted response still yields summary", s2.startswith("Just some prose"))
    check("unformatted response yields no todos", t2 == [])


def test_reminder_dedupe() -> None:
    print("\nreminder dedupe ledger")
    # Anchor at local noon so the one-hour variant remains on the same calendar
    # day even when CI happens to run near midnight in its configured timezone.
    due = (datetime.now() + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0,
    ).timestamp()
    check("first enqueue succeeds", store.enqueue_reminder("Pay invoice #4021", due))
    check("exact repeat suppressed", not store.enqueue_reminder("Pay invoice #4021", due))
    check("case/space variant suppressed",
          not store.enqueue_reminder("  pay   INVOICE #4021 ", due))
    check("same day, different time suppressed",
          not store.enqueue_reminder("Pay invoice #4021", due + 3600))
    check("different day allowed",
          store.enqueue_reminder("Pay invoice #4021", due + 86400 * 2))

    pending = store.pending_reminders()
    check("2 pending", len(pending) == 2, str(len(pending)))
    store.ack_reminders([pending[0]["id"]])
    check("ack removes from pending", len(store.pending_reminders()) == 1)
    check("acked reminder still blocks a duplicate",
          not store.enqueue_reminder("Pay invoice #4021", due))


def test_scheduler_slots() -> None:
    print("\nscheduler slot logic (sleep-aware)")
    cfg = dict(config.DEFAULTS)          # run_hours 5,8,11,14,17,20,23
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    at = lambda h, m=0: (day + timedelta(hours=h, minutes=m)).timestamp()  # noqa: E731
    slot_of = lambda ts: datetime.fromtimestamp(                            # noqa: E731
        scheduler.most_recent_slot(cfg, ts)).hour

    check("12:30 → 11:00 slot", slot_of(at(12, 30)) == 11)
    check("05:00 exactly → 05:00 slot", slot_of(at(5)) == 5)
    check("04:59 (dark window) → yesterday 23:00", slot_of(at(4, 59)) == 23)
    check("06:40 after dark night → 05:00 slot (catch-up)", slot_of(at(6, 40)) == 5)

    nxt = datetime.fromtimestamp(jobs.next_run_ts(cfg, at(12, 30))).hour
    check("next run after 12:30 is 14:00", nxt == 14)
    nxt2 = datetime.fromtimestamp(jobs.next_run_ts(cfg, at(23, 30)))
    check("next run after 23:30 is tomorrow 05:00",
          nxt2.hour == 5 and nxt2.day != day.day, str(nxt2))


def test_run_rollover_and_summarize() -> None:
    print("\nrun: rollover, then summarize (stubbed model)")
    cfg = dict(config.DEFAULTS)
    cfg["min_items_to_summarize"] = 3

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    calls: list[tuple[str, str]] = []

    async def fake_complete(_cfg, system, user):
        calls.append((system, user))
        return (f"SUMMARY:\nTwo things happened.\n\n"
                f"TODO: Reply to Sarah | {tomorrow}T09:00:00\n")

    real = model.complete
    model.complete = fake_complete
    jobs.model.complete = fake_complete
    try:
        now = time.time()
        store.add_items("messages", [
            {"ts": now - 600, "dedupe_key": "m1", "payload": "[Alice] hi"},
        ])
        res = asyncio.run(jobs.run_source(cfg, "messages"))
        check("1 item under threshold → rolled over", res["status"] == "rolled_over", str(res))
        check("no model call made for a rolled-over window", len(calls) == 0)

        store.add_items("messages", [
            {"ts": now - 500, "dedupe_key": "m2", "payload": "[Alice] you around?"},
            {"ts": now - 400, "dedupe_key": "m3", "payload": "[Bob] sending the file"},
        ])
        res = asyncio.run(jobs.run_source(cfg, "messages"))
        check("3 items → summarized", res["status"] == "summarized", str(res))
        check("all 3 items in one call", res["items"] == 3, str(res))
        check("exactly one model call", len(calls) == 1, str(len(calls)))
        check("reminder queued", res["reminders_queued"] == 1, str(res))

        system, user = calls[0]
        check("current date injected into system prompt",
              datetime.now().strftime("%Y-%m-%d") in system)
        check("all item payloads in one user message",
              all(p in user for p in ("hi", "you around?", "sending the file")))

        res2 = asyncio.run(jobs.run_source(cfg, "messages"))
        check("nothing pending after summarize", res2["status"] == "empty", str(res2))

        sums = store.summaries_since(0)
        check("summary stored", len(sums) == 1 and "Two things" in sums[0]["text"])
        check("summary starts unacked", sums[0]["acked"] == 0)
        store.ack_summaries(time.time())
        check("ack marks it", store.summaries_since(0) == [])
        check("acked summary still retrievable with include_acked",
              len(store.summaries_since(0, include_acked=True)) == 1)
    finally:
        model.complete = real
        jobs.model.complete = real


def test_model_error_keeps_items() -> None:
    print("\nrun: model failure must not lose items")
    cfg = dict(config.DEFAULTS)
    cfg["min_items_to_summarize"] = 1

    async def boom(_cfg, _s, _u):
        raise model.ModelError("connection refused")

    real = model.complete
    jobs.model.complete = boom
    try:
        now = time.time()
        store.add_items("email", [
            {"ts": now, "dedupe_key": "e-fail", "payload": "From X: important"},
        ])
        before = store.counts()["items_pending"]
        res = asyncio.run(jobs.run_source(cfg, "email"))
        check("reports model_error", res["status"] == "model_error", str(res))
        check("items still pending after failure",
              store.counts()["items_pending"] == before,
              f"{store.counts()['items_pending']} vs {before}")
        check("no summary written", not any(
            s["source"] == "email" for s in store.summaries_since(0, include_acked=True)))
    finally:
        jobs.model.complete = real


def test_stale_rollover_forced() -> None:
    print("\nrun: stale content summarizes even below threshold")
    cfg = dict(config.DEFAULTS)
    cfg["min_items_to_summarize"] = 10
    cfg["max_rollover_hours"] = 24

    async def fake(_cfg, _s, _u):
        return "SUMMARY:\nQuiet day.\n"

    real = jobs.model.complete
    jobs.model.complete = fake
    try:
        old = time.time() - 30 * 3600      # older than max_rollover_hours
        store.add_items("email", [
            {"ts": old, "dedupe_key": "e-old", "payload": "From Y: something"},
        ])
        res = asyncio.run(jobs.run_source(cfg, "email"))
        check("stale single item summarized anyway",
              res["status"] == "summarized", str(res))
    finally:
        jobs.model.complete = real


def main() -> int:
    print(f"state dir: {SCRATCH}")
    test_email_parsing()
    test_message_parsing()
    test_dedupe_and_cursor()
    test_todo_parsing()
    test_reminder_dedupe()
    test_scheduler_slots()
    test_run_rollover_and_summarize()
    test_model_error_keeps_items()
    test_stale_rollover_forced()
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
