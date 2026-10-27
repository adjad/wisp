"""Run Wisp's pure date resolver (service/attention/resolve.py) on mail date phrases.

No model, no I/O. Run from the repo root: PYTHONPATH=. .venv/bin/python scripts/mail_date_resolver_check.py
Measured 2026-10-26: 12/17 sentences; see docs/plans/2026-10-26-email-digest-memory-research.md.
"""
import sys
from datetime import datetime, timezone
from service.temporal_facts import extract_temporal_facts, EvidenceContext
from service.attention.resolve import resolve
TZ = "America/Los_Angeles"
# (email date, phrase/sentence, expected YYYY-MM-DD)
CASES = [
 ("2026-08-05", "Can you send me the Q3 regional numbers by Friday?", "2026-08-07"),
 ("2026-04-12", "please call us at 555-0142 to book it for October 2026.", "2026-10"),
 ("2026-05-20", "written notice must reach our office no later than July 2, 2027", "2027-07-02"),
 ("2026-05-20", "Your current lease ends on August 31, 2027.", "2027-08-31"),
 ("2026-09-01", "conferences are MOVED from November 12 to November 19, same time.", "2026-11-19"),
 ("2026-09-01", "Parent-teacher conferences will be held on November 12 at 4:30 PM", "2026-11-12"),
 ("2026-07-14", "we are getting married on June 6, 2027 in Portland", "2027-06-06"),
 ("2026-03-02", "Coverage expires on March 2, 2027.", "2027-03-02"),
 ("2026-10-01", "The second installment of $3,412.18 is due February 1, 2027", "2027-02-01"),
 ("2026-10-01", "becomes delinquent after April 10, 2027.", "2027-04-10"),
 ("2026-06-10", "before my defense on November 18!", "2026-11-18"),
 ("2026-04-12", "your plan renews every year on January 1", "2027-01-01"),
 ("2026-09-01", "order forms are due back by September 15.", "2026-09-15"),
 ("2026-06-10", "I'll send chapter 3 next week.", "2026-06-15..2026-06-21"),
 ("2026-08-20", "Tickets go on sale 10/3 at 10am", "2026-10-03"),
 ("2026-07-01", "the deposit is due in two weeks", "2026-07-15"),
 ("2026-12-15", "Payment due Jan 5", "2027-01-05"),
]
for d, text, want in CASES:
    arrival = datetime.fromisoformat(d + "T17:00:00+00:00")
    r = resolve(text, arrival, TZ, "x")
    tf = extract_temporal_facts(text, captured_at=arrival, timezone=TZ,
                                evidence=EvidenceContext("email", "x"))
    facts = getattr(tf, "facts", None) or getattr(tf, "candidates", None) or []
    kinds = [f"{getattr(f,'kind','?')}:{getattr(f,'value',None) or getattr(f,'start',None) or getattr(f,'date',None)}" for f in facts][:3]
    got = r.day.isoformat() if r.day else None
    ok = got and (got.startswith(want) if ".." not in want else want.split("..")[0] <= got <= want.split("..")[1])
    print(f"{'OK ' if ok else 'XX '} want {want:22} resolve={got} blocked={r.blocked} inferred={r.inferred} | facts={kinds} | {text[:55]}")
