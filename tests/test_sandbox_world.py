"""sandbox/outbound.py — outbound action handlers against an in-memory
World, regression tests. No real HTTP: a FakeClient records every POST
instead of sending it.

What must keep holding (see outbound.py's module docstring for why):
  * every result-bearing action posts EXACTLY ONE result, ever — including
    when the handler raises internally, since the alternative is the agent
    turn silently hanging for outbox.DEFAULT_TIMEOUT_S (45s) with no
    diagnostic;
  * the result shape is always exactly {action_id, ok, error} — nothing
    extra, per action_result's actual reader (service/main.py);
  * a `send_message` lands in the SAME thread a later message to the same
    handle would (the marquee "text Mom, then ask Wisp what I said" loop
    depends on this);
  * `archive_email`/`mark_email_read`/`reply_to_email` against an unknown
    message_id fail with the documented not-found error, not a crash;
  * `delete_calendar_event` with a `when_ts` removes only the matching
    OCCURRENCE of a recurring series, not every occurrence sharing the
    source_id (see persona.commitment_rows' seed_standup_recurring case);
  * fire-and-forget actions (create/delete calendar/reminder, sync_emails_now)
    never post a result at all.

    .venv/bin/python tests/test_sandbox_world.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRATCH = tempfile.mkdtemp(prefix="wisp-sandbox-world-test-")
os.environ["WISP_HOME"] = str(Path(SCRATCH) / "moe")
os.environ["WISP_SANDBOX_HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sandbox.outbound import FIRE_AND_FORGET, RESULT_ACTIONS, OutboundConsumer  # noqa: E402
from sandbox.world import World  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


class FakeResponse:
    def raise_for_status(self) -> None:
        pass


class FakeClient:
    """Records every action_result POST instead of sending it."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def post(self, path: str, json: dict | None = None) -> FakeResponse:
        self.posts.append({"path": path, "body": json})
        return FakeResponse()


class FakeSync:
    def __init__(self) -> None:
        self.forced: list[str] = []
        self.kicked: list[str] = []

    async def force_sync(self, source: str) -> None:
        self.forced.append(source)

    def kick(self, *sources: str) -> None:
        self.kicked.extend(sources)


def fresh() -> tuple[World, FakeClient, FakeSync, OutboundConsumer]:
    world = World(path=Path(tempfile.mkdtemp(prefix="wisp-world-")) / "world.json", seed=7)
    client = FakeClient()
    sync = FakeSync()
    outbound = OutboundConsumer(world, client, sync=sync)
    return world, client, outbound, sync


def result_for(client: FakeClient, action_id: str) -> dict | None:
    for p in client.posts:
        if p["path"] == "/assistant/action_result" and p["body"]["action_id"] == action_id:
            return p["body"]
    return None


async def test_send_message_reuses_existing_thread() -> None:
    print("\nsend_message: lands in the SAME thread a seeded conversation used")
    world, client, outbound, _ = fresh()
    before = sum(len(t["messages"]) for t in world.state["threads"].values())
    mom_handle = next(h for h in world.state["contacts"].values()
                      if h["name"] == "Mom")["handles"][0]
    await outbound._dispatch({"type": "send_message", "action_id": "a1",
                              "to": mom_handle, "text": "running late!"})
    after_threads = [t for t in world.state["threads"].values()
                     if t.get("context_label") == "Mom"]
    check("still exactly one 'Mom' thread (no fork)", len(after_threads) == 1,
          str(len(after_threads)))
    after = sum(len(t["messages"]) for t in world.state["threads"].values())
    check("exactly one message added", after == before + 1, f"{before} -> {after}")
    if after_threads:
        check("the new message is in Mom's thread",
              any(m["text"] == "running late!" for m in after_threads[0]["messages"]),
              str(after_threads[0]["messages"][-1]))
    res = result_for(client, "a1")
    check("posted exactly the {action_id, ok, error} shape",
          res == {"action_id": "a1", "ok": True, "error": ""}, str(res))


async def test_send_message_unknown_handle_creates_thread() -> None:
    print("\nsend_message: an unresolved handle gets its own thread")
    world, client, outbound, _ = fresh()
    await outbound._dispatch({"type": "send_message", "action_id": "a2",
                              "to": "+19998887777", "text": "hi"})
    matches = [t for t in world.state["threads"].values()
              if t.get("context_label") == "+19998887777"]
    check("exactly one thread for the unresolved handle", len(matches) == 1, str(len(matches)))


async def test_send_email() -> None:
    print("\nsend_email: lands in Sent, not synced into inbox")
    world, client, outbound, _ = fresh()
    before = len(world.state["emails"])
    await outbound._dispatch({"type": "send_email", "action_id": "a3",
                              "to": ["someone@example.test"], "subject": "Hi",
                              "body": "body text"})
    check("one new email", len(world.state["emails"]) == before + 1,
          str(len(world.state["emails"])))
    sent = [e for e in world.state["emails"].values() if e["mailbox"] == "sent"]
    check("exactly one Sent email", len(sent) == 1, str(len(sent)))
    if sent:
        check("subject/body preserved", sent[0]["subject"] == "Hi" and
              sent[0]["body"] == "body text", str(sent[0]))
    res = result_for(client, "a3")
    check("result shape correct", res == {"action_id": "a3", "ok": True, "error": ""}, str(res))


async def test_reply_to_email_not_found() -> None:
    print("\nreply_to_email: unknown message_id -> documented not-found error")
    world, client, outbound, _ = fresh()
    await outbound._dispatch({"type": "reply_to_email", "action_id": "a4",
                              "message_id": "<nonexistent@sandbox.wisp.test>",
                              "body": "reply text", "reply_all": False})
    res = result_for(client, "a4")
    check("ok is False", res is not None and res["ok"] is False, str(res))
    check("documented error text", res is not None and
          "couldn't find that message" in res["error"], str(res))


async def test_reply_to_email_marks_original_read() -> None:
    print("\nreply_to_email: succeeds against a real message_id, marks it read")
    world, client, outbound, _ = fresh()
    target = next(iter(world.state["emails"].values()))
    target["unread"] = True
    mid = target["message_id"]
    await outbound._dispatch({"type": "reply_to_email", "action_id": "a5",
                              "message_id": mid, "body": "on it", "reply_all": False})
    res = result_for(client, "a5")
    check("ok is True", res is not None and res["ok"] is True, str(res))
    check("original marked read", target["unread"] is False, str(target["unread"]))
    check("subject gets Re: prefix",
          any(e["mailbox"] == "sent" and e["in_reply_to"] == mid and
              e["subject"].startswith("Re:") for e in world.state["emails"].values()),
          "no matching Sent row found")


async def test_mark_email_read_and_archive_not_found() -> None:
    print("\nmark_email_read / archive_email: unknown message_id -> not-found")
    world, client, outbound, _ = fresh()
    await outbound._dispatch({"type": "mark_email_read", "action_id": "a6",
                              "message_id": "<nope@sandbox.wisp.test>", "read": True})
    res = result_for(client, "a6")
    check("mark_email_read not-found", res is not None and res["ok"] is False, str(res))

    await outbound._dispatch({"type": "archive_email", "action_id": "a7",
                              "message_id": "<nope@sandbox.wisp.test>"})
    res = result_for(client, "a7")
    check("archive_email not-found", res is not None and res["ok"] is False, str(res))


async def test_archive_email_moves_mailbox() -> None:
    print("\narchive_email: moves to the archive mailbox")
    world, client, outbound, _ = fresh()
    target = next(iter(world.state["emails"].values()))
    mid = target["message_id"]
    await outbound._dispatch({"type": "archive_email", "action_id": "a8",
                              "message_id": mid})
    res = result_for(client, "a8")
    check("ok is True", res is not None and res["ok"] is True, str(res))
    check("mailbox is now archive", target["mailbox"] == "archive", target["mailbox"])


async def test_draft_message_sets_thread_draft_not_a_message() -> None:
    print("\ndraft_message: sets thread.draft, does NOT append a sent message")
    world, client, outbound, _ = fresh()
    mom_handle = next(h for h in world.state["contacts"].values()
                      if h["name"] == "Mom")["handles"][0]
    before = sum(len(t["messages"]) for t in world.state["threads"].values())
    await outbound._dispatch({"type": "draft_message", "action_id": "a9",
                              "to": mom_handle, "text": "draft text"})
    after = sum(len(t["messages"]) for t in world.state["threads"].values())
    check("no message appended", after == before, f"{before} -> {after}")
    mom_thread = next(t for t in world.state["threads"].values()
                      if t.get("context_label") == "Mom")
    check("draft set on the thread", mom_thread["draft"] == "draft text",
          str(mom_thread["draft"]))
    res = result_for(client, "a9")
    check("result posted", res == {"action_id": "a9", "ok": True, "error": ""}, str(res))


async def test_delete_calendar_event_matches_only_the_right_occurrence() -> None:
    print("\ndelete_calendar_event: when_ts disambiguates a recurring series")
    world, client, outbound, _ = fresh()
    standups = {k: v for k, v in world.state["calendar"].items()
               if v["source_id"] == "seed_standup_recurring"}
    check("seed data has 2 standup occurrences", len(standups) == 2, str(len(standups)))
    victim_key, victim = next(iter(standups.items()))
    survivor_key = next(k for k in standups if k != victim_key)
    when_ts = world.abs_ts(victim["ts_off"])
    await outbound._dispatch({"type": "delete_calendar_event", "action_id": None,
                              "source_id": "seed_standup_recurring", "when_ts": when_ts})
    remaining = {k: v for k, v in world.state["calendar"].items()
                if v["source_id"] == "seed_standup_recurring"}
    check("exactly one occurrence removed", len(remaining) == 1, str(len(remaining)))
    check("the deleted occurrence is gone", victim_key not in remaining, str(remaining))
    check("the OTHER occurrence survives, untouched", survivor_key in remaining,
          f"expected {survivor_key} in {list(remaining)}")


async def test_delete_calendar_event_without_when_ts_removes_all() -> None:
    print("\ndelete_calendar_event: no when_ts removes every occurrence for that source_id")
    world, client, outbound, _ = fresh()
    await outbound._dispatch({"type": "delete_calendar_event", "action_id": None,
                              "source_id": "seed_standup_recurring"})
    remaining = [v for v in world.state["calendar"].values()
                if v["source_id"] == "seed_standup_recurring"]
    check("both occurrences removed", len(remaining) == 0, str(len(remaining)))


async def test_create_and_delete_reminder() -> None:
    print("\ncreate_apple_reminder / delete_apple_reminder")
    world, client, outbound, _ = fresh()
    before = len(world.state["reminders"])
    await outbound._dispatch({"type": "create_apple_reminder", "action_id": None,
                              "title": "water the plants", "when_ts": world.now() + 3600})
    check("one reminder added", len(world.state["reminders"]) == before + 1,
          str(len(world.state["reminders"])))
    new_id = next(rid for rid, r in world.state["reminders"].items()
                 if r["title"] == "water the plants")
    await outbound._dispatch({"type": "delete_apple_reminder", "action_id": None,
                              "source_id": new_id})
    check("reminder removed", new_id not in world.state["reminders"], "")


async def test_sync_emails_now_bypasses_debounce() -> None:
    print("\nsync_emails_now: forces email_headers + email_raw immediately, no debounce")
    world, client, outbound, sync = fresh()
    await outbound._dispatch({"type": "sync_emails_now", "action_id": None})
    check("forced email_headers", "email_headers" in sync.forced, str(sync.forced))
    check("forced email_raw", "email_raw" in sync.forced, str(sync.forced))


async def test_fire_and_forget_never_posts_a_result() -> None:
    print("\nfire-and-forget actions never POST /assistant/action_result")
    world, client, outbound, _ = fresh()
    for etype in FIRE_AND_FORGET:
        client.posts.clear()
        payload = {"type": etype, "action_id": None, "title": "x",
                  "when_ts": world.now() + 60, "source_id": "whatever"}
        await outbound._dispatch(payload)
        check(f"{etype}: no action_result posted",
              not any(p["path"] == "/assistant/action_result" for p in client.posts),
              str(client.posts))


async def test_handler_exception_still_posts_exactly_one_result() -> None:
    print("\na handler bug must still post exactly one result (never hang the turn)")
    world, client, outbound, _ = fresh()

    async def _boom(ev: dict) -> tuple[bool, str]:
        raise RuntimeError("boom")

    outbound._h_send_message = _boom  # type: ignore[method-assign]
    await outbound._dispatch({"type": "send_message", "action_id": "a10",
                              "to": "+19998887777", "text": "hi"})
    matching = [p for p in client.posts if p["path"] == "/assistant/action_result"
               and p["body"]["action_id"] == "a10"]
    check("exactly one result posted", len(matching) == 1, str(matching))
    if matching:
        check("ok is False with the sandbox error surfaced",
              matching[0]["body"]["ok"] is False and "boom" in matching[0]["body"]["error"],
              str(matching[0]))


async def test_stall_ms_and_drop_result_injection() -> None:
    print("\ninjection: stall_ms delays the result, drop_result suppresses it entirely")
    world, client, outbound, _ = fresh()
    world.state["injection"]["stall_ms"]["send_message"] = 50
    import time
    t0 = time.monotonic()
    await outbound._dispatch({"type": "send_message", "action_id": "a11",
                              "to": "+19998887777", "text": "hi"})
    elapsed = time.monotonic() - t0
    check("stall_ms actually delayed the POST", elapsed >= 0.045, f"{elapsed:.3f}s")
    check("result still posted after the stall", result_for(client, "a11") is not None, "")

    world.state["injection"]["drop_result"] = ["send_message"]
    await outbound._dispatch({"type": "send_message", "action_id": "a12",
                              "to": "+19998887777", "text": "hi2"})
    check("dropped result never posted", result_for(client, "a12") is None, "")


async def test_fail_injection_short_circuits_without_mutating() -> None:
    print("\ninjection: fail[type] posts ok:false WITHOUT mutating world")
    world, client, outbound, _ = fresh()
    before = sum(len(t["messages"]) for t in world.state["threads"].values())
    world.state["injection"]["fail"]["send_message"] = "Mail.app is not running"
    await outbound._dispatch({"type": "send_message", "action_id": "a13",
                              "to": "+19998887777", "text": "hi"})
    after = sum(len(t["messages"]) for t in world.state["threads"].values())
    check("no message was actually added", after == before, f"{before} -> {after}")
    res = result_for(client, "a13")
    check("forced error surfaced verbatim",
          res == {"action_id": "a13", "ok": False, "error": "Mail.app is not running"}, str(res))


async def main() -> int:
    print(f"scratch: {SCRATCH}")
    print(f"result-bearing actions covered: {sorted(RESULT_ACTIONS)}")
    await test_send_message_reuses_existing_thread()
    await test_send_message_unknown_handle_creates_thread()
    await test_send_email()
    await test_reply_to_email_not_found()
    await test_reply_to_email_marks_original_read()
    await test_mark_email_read_and_archive_not_found()
    await test_archive_email_moves_mailbox()
    await test_draft_message_sets_thread_draft_not_a_message()
    await test_delete_calendar_event_matches_only_the_right_occurrence()
    await test_delete_calendar_event_without_when_ts_removes_all()
    await test_create_and_delete_reminder()
    await test_sync_emails_now_bypasses_debounce()
    await test_fire_and_forget_never_posts_a_result()
    await test_handler_exception_still_posts_exactly_one_result()
    await test_stall_ms_and_drop_result_injection()
    await test_fail_injection_short_circuits_without_mutating()
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
