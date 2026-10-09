"""One pass of the live path: new messages in, at most a few verified reminders out.

All effects are injected (`create`, `publish`, `fresh_commitments`), so this module is
tested with fakes and never imports the live assistant store. The thin wiring that
supplies the real ones is `service/assistant/attention_runner.py`.

The order of operations is the safety argument:

  1. Mode "off" does nothing. Mode "shadow" decides and records, and does nothing else.
  2. A baseline is set on the first pass and again every time the mode is switched INTO
     live. Nothing older than the latest baseline, and nothing older than the lookback, is
     ever acted on, so turning this on (or back on) cannot flood the user with reminders
     for messages that arrived while it was off.
  3. A message is CLAIMED in the ledger before anything happens. A claim succeeds once, so
     a restart or a re-sync cannot produce a second action for the same message.
  4. The daily cap and a fresh "is it on the calendar now?" check run immediately before
     the effect, not at decision time. The cap counts every attempt whose outcome is not a
     verified failure (created, unknown, in flight): an uncertain write may well exist.
     At most one reminder is written per pass, so a slow or stuck app cannot be hit with a
     burst of writes.
  5. The effect's outcome is recorded whatever it is. Unknown and failed outcomes are
     final: nothing retries, because the reminder may exist.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from service.attention.corpus import Snapshot
from service.attention.detectors import UncapturedCommitment
from service.attention.ledger import Ledger
from service.attention.live import (LOOKBACK_S, Plan, Settings, in_quiet_hours, items_from_records,
                                    local_midnight, plan_reminder, who_label)
from service.attention.matching import find_on_file
from pathlib import Path

MAX_EFFECTS_PER_PASS = 1
CAP_STATES = ("created", "unknown", "claimed")        # an unknown write may exist: it counts

class Outcomes(list):
    """The outcomes of one pass. `limited` is True when it stopped at the per-pass effect limit
    with eligible messages still waiting, so the caller should look again soon."""
    limited: bool = False


Create = Callable[[Plan], Awaitable[dict]]            # -> {"ok": bool, "status": str, "error": str}
Publish = Callable[[dict], Awaitable[None]]
Commitments = Callable[[], list[dict]]
NameFor = Callable[[str], "str | None"]               # a phone/email handle -> contact name


@dataclass(frozen=True)
class Outcome:
    source_id: str
    state: str
    title: str | None = None
    detail: dict | None = None


async def process(*, records: list[dict], commitments: list[dict], now: float, tz: str,
                  settings: Settings, ledger: Ledger, create: Create, publish: Publish,
                  fresh_commitments: Commitments, name_for: NameFor | None = None) -> Outcomes:
    outcomes: Outcomes = Outcomes()
    if settings.mode == "off":
        # Remember that it was off. Resuming live afterwards must re-baseline, or the messages
        # that arrived during the pause would be swept up. (Unreadable settings count as off.)
        ledger.set_meta("last_mode", "off")
        return outcomes
    ledger.recover(now)
    if ledger.meta("enabled_at") is None or (settings.mode == "live"
                                             and ledger.meta("last_mode") != "live"):
        ledger.set_meta("enabled_at", repr(now))          # baseline: never act on older messages
    ledger.set_meta("last_mode", settings.mode)
    floor = max(now - LOOKBACK_S, float(ledger.meta("enabled_at")))

    items = items_from_records(records)
    fresh = [i for i in items if i.direction == "incoming" and floor <= i.ts <= now]
    unseen_ids = {i.id for i in fresh} - ledger.known([i.id for i in fresh])
    if not unseen_ids:
        return outcomes

    snapshot = Snapshot(Path("."), {}, sorted(items, key=lambda i: i.ts), list(commitments))
    detector = UncapturedCommitment(snapshot, tz=tz)
    effects = 0

    for item in sorted((i for i in fresh if i.id in unseen_ids), key=lambda i: i.ts):
        try:
            decision = detector.decide(item, as_of=datetime.fromtimestamp(now, ZoneInfo(tz)))
        except Exception:                                 # noqa: BLE001 — one odd message must not
            continue                                      # stop the others; nothing is claimed
        if not decision.alert:
            continue
        # The label is always the OTHER person (the incoming message's sender), even when the
        # words quoted are the user's own proposal.
        who = (name_for(item.sender) if name_for else None) or who_label(item.sender)
        plan = plan_reminder(decision.source or item, decision.resolved, now, settings, tz, who=who)
        if plan is None:                                  # too late to be useful; not recorded
            continue
        if settings.mode == "live" and effects >= MAX_EFFECTS_PER_PASS:
            outcomes.limited = True
            break                                         # the rest wait for the next pass, unclaimed
        if not ledger.claim(item.id, title=plan.title, due_ts=plan.due_ts,
                            event_ts=plan.event_ts, now=now):
            continue
        detail = {"quote": plan.quote, "when": plan.when_label, "why": list(decision.why)}

        if settings.mode == "shadow":
            ledger.finish(item.id, "shadow", detail, now)
            outcomes.append(Outcome(item.id, "shadow", plan.title, detail))
            continue

        if ledger.count_since(local_midnight(now, tz), CAP_STATES, exclude=item.id) >= settings.daily_cap:
            ledger.finish(item.id, "capped", detail, now)
            outcomes.append(Outcome(item.id, "capped", plan.title, detail))
            continue

        # The calendar may have changed since the tick began (the user may have just added it).
        context = f"{decision.source.text} {item.text}" if decision.source else item.text
        if find_on_file(decision.resolved, context, fresh_commitments(), tz):
            ledger.finish(item.id, "already_on_file", detail, now)
            outcomes.append(Outcome(item.id, "already_on_file", plan.title, detail))
            continue

        effects += 1
        try:
            result = await create(plan)
        except Exception as exc:                          # noqa: BLE001 — outcome unknowable
            result = {"ok": False, "status": "unknown", "error": f"{type(exc).__name__}: {exc}"}
        if result.get("ok") is True:
            state = "created"
        elif result.get("status") == "unknown":
            state = "unknown"
        else:
            state = "failed"
        detail = {**detail, "result": {k: result.get(k) for k in ("ok", "status", "error", "where")}}
        ledger.finish(item.id, state, detail, now)
        outcomes.append(Outcome(item.id, state, plan.title, detail))

        if state == "created" and not in_quiet_hours(datetime.fromtimestamp(now, ZoneInfo(tz)), settings):
            try:
                await publish({"type": "attention_added", "source_id": item.id, "title": plan.title,
                               "when_label": plan.when_label, "quote": plan.quote,
                               "sender": item.sender})
            except Exception:                             # noqa: BLE001 — the reminder exists regardless
                pass
    return outcomes
