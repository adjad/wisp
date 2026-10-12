"""Test fixture: eight weeks of a fictional person's behavior, plus two weeks ahead.

Used only by the behavior-engine tests (miners, predictions, nudges, phraser).
Every name, company, message and event here is made up; every id starts with
``demo:`` so fixture data can never be mistaken for real observations.

The history is generated with planted routines whose ground truth is returned
in ``Persona.expected`` so tests can check that the miners recover them:

* "Morning lift" Mon/Wed/Fri 6:45-7:45 (~85% adherence, the odd Tuesday);
* weekday "Focus block" 9:00-11:00 (she protects focus time daily);
* afternoon messages with Dev Malik most weekdays, plus a Thursday 3pm 1:1;
* Sunday-evening calls and texts with Mom; biweekly investor calls with Nora Keene;
* Tue/Thu afternoon runs of 3-4 back-to-back internal meetings where she
  cancels the LAST one ~60% of the time;
* meetings booked over gym time get moved 4 times out of 5;
* she asks Wisp for her brief around 8:15 on weekdays.

Planted rates use an error-diffusion counter (not coin flips) so the realized
rate is the intended one within one occurrence whatever ``now`` is; timing
jitter and which customer/variant comes up use a seeded RNG. Deterministic for
a given (now, tz, weeks, seed).

The upcoming two weeks always contain a meeting over the next gym morning and
a 4-meeting back-to-back run on the next Tue/Thu, so the engine fires
``habit_conflict`` and ``cancel_likely`` nudges.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from service.behavior.timeutil import at_minute, daterange, local_dt, resolve_tz
from service.behavior.types import Observation

USER = "Aria Solano"
CALENDAR = "Wisp Demo"
GYM = "Morning lift"
GYM_START, GYM_LEN = 6 * 60 + 45, 60
BRIEF_MINUTE = 8 * 60 + 15
B2B_LAST_CANCEL_RATE = 0.6


@dataclass
class Persona:
    now: float
    tz: str
    observations: list[Observation]          # history (ts < now), incl. lifecycle observations
    upcoming: list[Observation]              # planned events with start >= now
    calendar_events: list[dict[str, Any]]    # past (kept) + upcoming, for seeding a real calendar
    reminders: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    expected: dict[str, Any] = field(default_factory=dict)


class _Rate:
    """Error-diffusion 'coin': fires so the running share tracks ``rate`` exactly."""

    def __init__(self, rate: float, phase: float) -> None:
        self.rate, self.acc = rate, phase

    def __call__(self) -> bool:
        self.acc += self.rate
        if self.acc >= 1.0:
            self.acc -= 1.0
            return True
        return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_DEV_IN = [
    "Tenant-isolation suite is green on staging — want the summary before Priya's review?",
    "Lena's export spike works end to end; one edge case with archived sites left.",
    "Can we keep Thursday's delivery slot for SSO? Auto-dispatch can wait a week.",
    "Priya sent two exceptions back. Neither blocks, both need a written disposition.",
    "Jonah's take-home is in. Strong on infra, light on incident write-ups.",
    "Heads up: Harborline asked about SCIM again. I'd rather not promise it this quarter.",
    "Pushed the audit-log schema. Need your call on retention defaults.",
    "Rivet's offline recovery repro is fixed on main. Walkthrough-ready Friday.",
]
_DEV_OUT = [
    "Yes please — send it over and I'll read before 4.",
    "Agreed, SSO first. I'll tell Tessa we're on track for the evidence packet.",
    "Let's default to 13 months and make it configurable per tenant.",
    "Good. Write up the dispositions and I'll forward to Priya tonight.",
    "Thanks — let's discuss Jonah at our 1:1.",
    "Perfect, I'll book the Rivet walkthrough.",
]
_MOM_IN = ["Still on for our call at 6?", "Did you eat something today that wasn't a protein bar?",
           "Dad says hi. Call when you can!", "Love you — talk next week."]
_MOM_OUT = ["Yes! Talk soon ❤️", "Ha, yes. Salad and everything.", "Calling at 6!", "Love you too."]
_SAMIRA_IN = ["Security packet v3 is in the drive — needs your sign-off on the SOC2 wording.",
              "Harborline wants the onboarding plan by Friday. Draft is ready.",
              "Field-tech training deck updated with the offline flow."]
_SAMIRA_OUT = ["Looks good, approved.", "Send it — cc me.", "Thanks Samira, will review after lunch."]
_NORA_IN = ["Ready when you are.", "Partners liked the NRR trend. Can you send the cohort table?"]
_NORA_OUT = ["Dialing in now.", "Sending the cohort table this afternoon."]
_OWEN_IN = ["Metrics deck is updated for Friday's review.", "Runway model refreshed: 12.0 months base case."]
_OWEN_OUT = ["Thanks Owen.", "Great — let's walk through the downside case Friday."]

_CUSTOMERS = [("Harborline HVAC", "Tessa Cole"), ("Rivet Fleet Services", "Marcus Bell"),
              ("Foundry Facilities", "Elise Hart")]
_COLLISIONS = [("Harborline SSO check-in", "Tessa Cole"), ("Rivet offline-recovery prep", "Marcus Bell"),
               ("Foundry procurement call", "Elise Hart"), ("Security packet review — Priya Sen", "Priya Sen"),
               ("Candidate call — Jonah West", "Jonah West")]


class _Builder:
    def __init__(self, now: float, tz, rng: random.Random) -> None:
        self.now, self.tz, self.rng = now, tz, rng
        self.history: list[Observation] = []
        self.upcoming: list[Observation] = []
        self.calendar: list[dict[str, Any]] = []
        self.reminders: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    def ts(self, day: date, minute: float) -> float:
        return at_minute(day, minute, self.tz)

    def event(self, day: date, minute: int, dur: int, title: str, series: str, *, location: str = "",
              notes: str = "", organizer: str = USER, attendees: tuple[str, ...] = (),
              outcome: str = "kept", moved_to: int | None = None) -> Observation:
        """Plan one occurrence. History events may be cancelled or moved (lifecycle
        observations are emitted exactly as observe_snapshot would)."""
        start = self.ts(day, minute)
        end = start + dur * 60
        stamp = local_dt(start, self.tz).strftime("%Y%m%d%H%M")
        attrs = {"calendar": CALENDAR, "location": location, "organizer": organizer,
                 "organizer_is_user": organizer == USER,
                 "attendees": list(attendees) or [USER]}
        obs = Observation(id=f"demo:evt:{_slug(series)}:{stamp}", kind="event", ts=start, subject=title,
                          source="demo", end_ts=end, source_id=f"demo:series:{_slug(series)}", attrs=attrs)
        if start >= self.now:
            self.upcoming.append(obs)
            self._cal(obs, notes)
            return obs
        if outcome == "cancelled":
            self.history.append(Observation(
                id=f"demo:cancel:{obs.id}", kind="event_cancelled", ts=start, subject=title, source="demo",
                end_ts=end, source_id=obs.source_id,
                attrs={**attrs, "start_ts": start, "end_ts": end, "subject": title,
                       "source_id": obs.source_id, "detected_at": start - 86400}))
            return obs
        if outcome == "moved" and moved_to is not None:
            new = self.event(day, moved_to, dur, title, series + "-moved", location=location, notes=notes,
                             organizer=organizer, attendees=attendees)
            new = replace(new, source_id=obs.source_id)
            self.history[-1] = new
            self.history.append(Observation(
                id=f"demo:move:{obs.id}", kind="event_moved", ts=start, subject=title, source="demo",
                end_ts=end, source_id=obs.source_id,
                attrs={**attrs, "from_ts": start, "to_ts": new.ts, "from_end_ts": end,
                       "to_end_ts": new.end_ts, "subject": title, "source_id": obs.source_id,
                       "new_id": new.id}))
            return obs
        self.history.append(obs)
        self._cal(obs, notes)
        return obs

    def _cal(self, obs: Observation, notes: str) -> None:
        self.calendar.append({"demo_id": obs.id, "title": obs.subject, "start_ts": obs.ts,
                              "end_ts": obs.end_ts, "location": obs.attrs.get("location", ""),
                              "notes": notes, "calendar": CALENDAR})

    def message(self, ts: float, contact: str, direction: str, text: str) -> None:
        if ts >= self.now:
            return
        stamp = local_dt(ts, self.tz).strftime("%Y%m%d%H%M")
        self.history.append(Observation(
            id=f"demo:msg:{stamp}:{_slug(contact)}:{direction}", kind=f"message_{direction}", ts=ts,
            subject=contact, source="demo", attrs={"text": text, "service": "iMessage"}))

    def exchange(self, day: date, minute: int, contact: str, ins: list[str], outs: list[str],
                 *, reply_after: tuple[int, int] = (5, 35)) -> None:
        t = self.ts(day, minute)
        self.message(t, contact, "in", self.rng.choice(ins))
        self.message(t + 60 * self.rng.randint(*reply_after), contact, "out", self.rng.choice(outs))

    def ask(self, ts: float, subject: str, text: str) -> None:
        if ts < self.now:
            stamp = local_dt(ts, self.tz).strftime("%Y%m%d%H%M")
            self.history.append(Observation(id=f"demo:ask:{stamp}:{_slug(subject)}", kind="wisp_ask",
                                            ts=ts, subject=subject, source="demo", attrs={"text": text}))

    def reminder(self, due: float, title: str, notes: str = "", *, done_after_min: int | None = None) -> None:
        stamp = local_dt(due, self.tz).strftime("%Y%m%d%H%M")
        rid = f"demo:rem:{stamp}:{_slug(title)}"
        completed = due < self.now and done_after_min is not None
        if due < self.now:
            self.history.append(Observation(id=rid, kind="reminder", ts=due, subject=title, source="demo",
                                            attrs={"list": CALENDAR, "status": "completed" if completed else "open"}))
            if completed and due + done_after_min * 60 < self.now:
                self.history.append(Observation(id=f"demo:remdone:{stamp}:{_slug(title)}", kind="reminder_done",
                                                ts=due + done_after_min * 60, subject=title, source="demo",
                                                attrs={"list": CALENDAR, "reminder_id": rid}))
        self.reminders.append({"demo_id": rid, "title": title, "due_ts": due, "completed": completed,
                               "list": CALENDAR, "notes": notes})

    def note(self, ts: float, title: str, body: str) -> None:
        stamp = local_dt(ts, self.tz).strftime("%Y%m%d%H%M")
        nid = f"demo:note:{stamp}:{_slug(title)[:40]}"
        if ts < self.now:
            self.history.append(Observation(id=nid, kind="note", ts=ts, subject=title, source="demo",
                                            attrs={"folder": CALENDAR}))
        self.notes.append({"demo_id": nid, "title": title, "body": body, "folder": CALENDAR})


def generate_persona(now: float, *, tz: str = "America/Los_Angeles", weeks_back: int = 8,
                     weeks_ahead: int = 2, seed: int = 11) -> Persona:
    zone = resolve_tz(tz)
    rng = random.Random(seed)
    b = _Builder(now, zone, rng)
    today = local_dt(now, zone).date()
    first = today - timedelta(days=7 * weeks_back)
    last = today + timedelta(days=7 * weeks_ahead - 1)

    gym, focus, brief = _Rate(0.85, rng.random()), _Rate(0.9, rng.random()), _Rate(0.85, rng.random())
    b2b_cancel = _Rate(B2B_LAST_CANCEL_RATE, rng.random())
    mom, dev = _Rate(0.85, rng.random()), _Rate(0.8, rng.random())

    # Five past gym mornings get a meeting booked over them: four are moved, one kept.
    eligible = [d for d in daterange(first + timedelta(days=7), today - timedelta(days=2)) if d.weekday() in (0, 2, 4)]
    collision_days: dict[date, str] = {}
    if len(eligible) >= 5:
        picks = [eligible[round(i * (len(eligible) - 1) / 4)] for i in range(5)]
        kept_idx = rng.randrange(5)
        collision_days = {d: ("kept" if i == kept_idx else "moved") for i, d in enumerate(picks)}

    # Next gym morning strictly after today gets an upcoming conflict.
    conflict_day = next(d for d in daterange(today + timedelta(days=1), today + timedelta(days=8))
                        if d.weekday() in (0, 2, 4))
    expected: dict[str, Any] = {"b2b_last": {"opportunities": 0, "cancelled": 0}}
    forced_b2b_done = False
    b2b_upcoming: dict[str, Any] = {}
    collision_n = 0

    for day in daterange(first, last):
        wd = day.weekday()
        past_day = b.ts(day, 24 * 60) <= now
        day_start_future = b.ts(day, 0) >= now

        # --- gym ---------------------------------------------------------
        go = gym() if wd in (0, 2, 4) else (wd == 1 and rng.random() < 0.15 and not day_start_future)
        if wd in (0, 2, 4) and (b.ts(day, GYM_START) >= now):
            go = True                                     # a recurring plan is always on the calendar
        if day in collision_days:
            go = collision_days[day] == "moved"
        if go:
            jitter = rng.choice([0, 0, 0, 0, 0, -5, 5, 0])
            b.event(day, GYM_START + jitter, GYM_LEN, GYM, "gym", location="Ironwood Strength Club",
                    notes="Strength block: squat, press, pull. Phone in the locker.")
        if day in collision_days:
            title, who = _COLLISIONS[collision_n % len(_COLLISIONS)]
            collision_n += 1
            moved_to = {0: 13 * 60, 2: 11 * 60 + 15, 4: 11 * 60 + 15}[wd]
            b.event(day, 7 * 60, 30, title, f"collision-{collision_n}", location="Zoom", organizer=who,
                    attendees=(who, USER), outcome=collision_days[day], moved_to=moved_to)
        if day == conflict_day:
            ev = b.event(day, 7 * 60, 30, "Harborline SSO check-in", "harborline-sso-upcoming", location="Zoom",
                         organizer="Tessa Cole", attendees=("Tessa Cole", USER, "Samira Vale"),
                         notes="Tessa wants a date for the SSO evidence packet before procurement.")
            expected["conflict_event_id"] = ev.id
            expected["conflict_day"] = day.isoformat()

        if wd < 5:
            # --- focus + brief ------------------------------------------------
            if focus() or b.ts(day, 9 * 60) >= now:
                b.event(day, 9 * 60, 120, "Focus block", "focus",
                        notes="Protected — no meetings. Pricing memo and investor narrative.")
            if brief():
                b.ask(b.ts(day, BRIEF_MINUTE + rng.randint(-10, 10)), "brief", "What's on my plate today?")
            if rng.random() < 0.25:
                topic = rng.choice(["draft_email", "calendar", "research"])
                t = b.ts(day, rng.randint(10 * 60, 18 * 60))
                b.ask(t, topic, {"draft_email": "Draft a reply to Tessa about the SSO timeline",
                                 "calendar": "When am I free on Thursday?",
                                 "research": "Summarize Foundry's procurement requirements"}[topic])
            # --- Dev afternoons ----------------------------------------------
            if past_day or b.ts(day, 14 * 60) < now:
                if dev():
                    for _ in range(rng.choice([1, 1, 2])):
                        b.exchange(day, rng.randint(14 * 60, 16 * 60 + 30), "Dev Malik", _DEV_IN, _DEV_OUT)
                if rng.random() < 0.1:
                    b.exchange(day, rng.randint(11 * 60, 11 * 60 + 40), "Dev Malik", _DEV_IN, _DEV_OUT)

        if wd == 0:
            b.event(day, 11 * 60 + 30, 45, "Leadership sync", "leadership-sync", location="Cedarloop office",
                    attendees=(USER, "Dev Malik", "Samira Vale", "Owen Reed"),
                    notes="Agenda: pilots, security path, hiring, cash.")
            if b.ts(day, 8 * 60 + 30) < now:
                b.note(b.ts(day, 8 * 60 + 30), f"Weekly priorities — week of {day.strftime('%b')} {day.day}",
                       "1. Harborline SSO evidence packet (Samira + Priya)\n2. Foundry export proof (Lena)\n"
                       "3. Rivet offline walkthrough\n4. Investor update Friday\nProtect: focus 9–11 daily, gym MWF.")
        if wd in (1, 3):
            # --- back-to-back run --------------------------------------------
            four = rng.random() < 0.5
            base = [("Pipeline review", 30), ("Hiring sync", 30), ("Pricing review", 45 if wd == 1 else 30)]
            if wd == 1:
                run, start = base + ([("Ops standup", 30)] if four else []), 14 * 60
            else:
                run, start = ([("Ops standup", 30)] if four else []) + base, 13 * 60 - (30 if four else 0)
            run_future = b.ts(day, start) >= now
            if run_future and not forced_b2b_done and not four:
                run = run + [("Ops standup", 30)] if wd == 1 else [("Ops standup", 30)] + run
                start = start if wd == 1 else start - 30
            cancel_last = (not run_future) and b2b_cancel()
            m = start
            for i, (title, dur) in enumerate(run):
                is_last = i == len(run) - 1
                ev = b.event(day, m, dur, title, title, location="Cedarloop office · Juniper room",
                             attendees=(USER, "Dev Malik", "Samira Vale", "Theo Park"),
                             outcome="cancelled" if (is_last and cancel_last) else "kept")
                if is_last and not run_future:
                    expected["b2b_last"]["opportunities"] += 1
                    expected["b2b_last"]["cancelled"] += int(cancel_last)
                if is_last and run_future and not forced_b2b_done:
                    b2b_upcoming = {"b2b_upcoming_day": day.isoformat(), "b2b_upcoming_last_event_id": ev.id}
                m += dur
            if run_future:
                forced_b2b_done = True
        if wd == 3:
            b.event(day, 15 * 60, 30, "Dev / Aria 1:1", "dev-aria-1-1", location="Cedarloop office",
                    organizer="Dev Malik", attendees=("Dev Malik", USER),
                    notes="Delivery slots, SSO evidence, Jonah hiring loop.")
        if wd == 2:
            company, contact = _CUSTOMERS[day.isocalendar()[1] % len(_CUSTOMERS)]
            b.event(day, 13 * 60, 45, f"Customer call — {company}", f"customer-{company}", location="Zoom",
                    attendees=(USER, contact, "Imani Flores"),
                    outcome="cancelled" if rng.random() < 0.12 else "kept")
            if day.isocalendar()[1] % 2 == 0:
                b.event(day, 16 * 60, 30, "Investor call — Nora Keene", "investor-nora", location="Phone",
                        organizer="Nora Keene", attendees=("Nora Keene", USER),
                        notes="Seed extension: NRR cohort table, data-room gaps.")
                b.exchange(day, 15 * 60 + 30, "Nora Keene", _NORA_IN, _NORA_OUT, reply_after=(2, 10))
        if wd == 1 and rng.random() < 0.6:
            b.exchange(day, rng.randint(11 * 60, 12 * 60 + 15), "Samira Vale", _SAMIRA_IN, _SAMIRA_OUT)
        if wd == 4:
            b.event(day, 12 * 60, 60, "Team lunch", "team-lunch", location="Cedarloop office")
            b.event(day, 15 * 60, 45, "Weekly metrics review", "metrics-review", location="Cedarloop office",
                    attendees=(USER, "Owen Reed"), notes="MRR, pipeline, burn, runway.")
            b.reminder(b.ts(day, 16 * 60), "Send weekly investor update",
                       "Numbers from the metrics review; flag SSO timeline risk.",
                       done_after_min=rng.randint(10, 40))
            if rng.random() < 0.6:
                b.exchange(day, rng.randint(14 * 60, 14 * 60 + 40), "Owen Reed", _OWEN_IN, _OWEN_OUT)
        if wd == 5 and rng.random() < 0.3 and past_day:
            b.event(day, 10 * 60, 60, "Farmers market", "farmers-market", location="Ferry Building")
        if wd == 6:
            call = mom() or b.ts(day, 18 * 60) >= now
            if call:
                b.exchange(day, 17 * 60 + rng.randint(0, 40), "Mom", _MOM_IN, _MOM_OUT, reply_after=(3, 15))
                b.event(day, 18 * 60, 30, "Call Mom", "call-mom", notes="Ask about Dad's knee.")
                if rng.random() < 0.5:
                    b.exchange(day, 19 * 60 + rng.randint(0, 30), "Mom", _MOM_IN, _MOM_OUT, reply_after=(2, 20))
        elif rng.random() < 0.03:
            b.exchange(day, 12 * 60 + rng.randint(0, 50), "Mom", _MOM_IN, _MOM_OUT)

    # One-off reminders and notes for the same fictional storyline.
    b.reminder(b.ts(today + timedelta(days=1), 9 * 60 + 30), "Send tenant-isolation evidence to Priya",
               "Attach synthetic test evidence and unresolved exceptions.")
    b.reminder(b.ts(today + timedelta(days=2), 10 * 60), "Protect Lena’s four-hour export block",
               "Confirm 10:00–14:00 with no demo interruptions.")
    b.reminder(b.ts(conflict_day - timedelta(days=1), 17 * 60), "Prep Harborline SSO status for Tessa")
    b.note(now - 3 * 86400, "Meeting hygiene — Tue/Thu afternoons",
           "Tue/Thu afternoons stack 3–4 reviews back to back. The last one is usually the first to go. "
           "Consider merging Pricing review into Pipeline review.")
    b.note(now - 5 * 86400, "Investor call prep — Nora Keene",
           "NRR 108% trailing three months. $350k non-binding interest. Data-room gaps: SOC2 letter, cohort table.")

    opp = expected["b2b_last"]["opportunities"]
    expected["b2b_last"]["rate"] = expected["b2b_last"]["cancelled"] / opp if opp else 0.0
    expected.update(b2b_upcoming)
    expected.update({
        "gym": {"subject": "fitness", "label": GYM, "weekdays": [0, 2, 4],
                "start_minute": GYM_START, "end_minute": GYM_START + GYM_LEN, "adherence": 0.85},
        "focus": {"subject": "focus", "weekdays": [0, 1, 2, 3, 4], "start_minute": 540, "end_minute": 660},
        "dev": {"contact": "Dev Malik", "daypart": "afternoon", "weekdays": [0, 1, 2, 3, 4]},
        "mom": {"contact": "Mom", "weekday": 6, "daypart": "evening"},
        "brief_minute": BRIEF_MINUTE,
        "gym_collisions": {"moved": sum(1 for v in collision_days.values() if v == "moved"),
                           "kept": sum(1 for v in collision_days.values() if v == "kept")},
    })
    history = sorted(b.history, key=lambda o: (o.ts, o.id))
    upcoming = sorted(b.upcoming, key=lambda o: (o.ts, o.id))
    calendar = sorted(b.calendar, key=lambda e: (e["start_ts"], e["demo_id"]))
    return Persona(now=now, tz=tz, observations=history, upcoming=upcoming, calendar_events=calendar,
                   reminders=sorted(b.reminders, key=lambda r: (r["due_ts"], r["demo_id"])),
                   notes=b.notes, expected=expected)
