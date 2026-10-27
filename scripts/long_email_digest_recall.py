"""Recall harness for the long-email digest (service/tools/email_digest.py).

NOT a gate: it needs a running oMLX with the model loaded and takes minutes.
It builds synthetic long emails (no real mail) with planted details, many past
the old 6,000-char head-cut, and scores how many details survive:
  - head-cut: today's behavior before the digest (first 6,000 chars, one call)
  - digest:   the production `digest_body` (full cleaned body, thinking off)

    .venv/bin/python scripts/long_email_digest_recall.py \\
        --base-url http://127.0.0.1:8795 --model gemma-4-E4B-it-oQ4e-mtp --reps 3

Quit Wisp first, or point it at an isolated engine: digests wait for the model
to be idle in THIS process only.
Measured 2026-10-26 on Gemma 4 E4B: head-cut 41%, digest 95% (5.6 s mean).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random

random.seed(7)

FILLER = {
    "project": [
        "The team spent most of the sprint on the migration work, and the dashboards now load noticeably faster for the regional offices.",
        "We held two design reviews with the platform group; both were productive and the feedback is captured in the shared tracker.",
        "Onboarding for the two new contractors went smoothly, and they have already closed several small tickets on the reporting module.",
        "There was some discussion about whether to keep the legacy export format, but nobody felt strongly enough to block the change.",
        "QA flagged a handful of cosmetic issues on tablet layouts; none of them are release blockers and they are queued for next sprint.",
        "Vendor support was responsive this month and the open escalation from August was closed without further follow-up from us.",
        "The internal wiki has been reorganized so that runbooks sit next to the services they describe, which people seem to like.",
        "We also cleaned up a long tail of stale feature flags, which should make the configuration screens easier to reason about.",
    ],
    "school": [
        "The autumn assembly was a great success and we want to thank every family that came out to support the performers.",
        "Our library has received a wonderful donation of new picture books, and classes will rotate through to see them over the coming weeks.",
        "Please remember that the car park is very busy at pickup time, so we encourage walking or cycling wherever possible.",
        "The student council has been hard at work organizing lunchtime clubs, including chess, coding, and a new gardening group.",
        "Thank you to the parent volunteers who helped repaint the playground markings during the half-term break.",
        "Reading logs continue to be an important part of homework, and teachers will check them every Friday as usual.",
        "The lost property box is overflowing again, so please label coats, water bottles, and lunch boxes clearly.",
        "We are proud of the progress our older students have made in the regional maths challenge this term.",
    ],
    "trip": [
        "The group has been looking forward to this for months and the planning chat has been lively, to put it mildly.",
        "Weather looks mostly dry, though the mountains can change quickly, so pack a light rain layer just in case.",
        "Several people asked about bringing bikes; there is storage at the lodge but space is limited and first-come.",
        "The town has a good bakery near the square and a couple of decent restaurants if we want a night off cooking.",
        "We tried to keep costs reasonable by sharing rooms and splitting groceries, and the spreadsheet tracks who paid what.",
        "If anyone has dietary needs not already in the sheet, add them so the grocery run covers everyone.",
        "There is no great phone signal on the upper trails, so agree a meeting point before splitting into smaller groups.",
        "Parking at the trailhead fills up early on weekends, so carpooling from the lodge makes the most sense.",
    ],
    "legal": [
        "This letter summarizes the main commercial terms discussed during our recent calls and supersedes the earlier draft outline.",
        "Both parties acknowledge that the definitions section will be harmonized with the master agreement before signature.",
        "Nothing in this summary creates a binding obligation until the definitive agreement has been executed by authorized signatories.",
        "The service levels described in Schedule B remain unchanged from the pilot period except where noted below.",
        "Each party will bear its own costs in connection with the negotiation and preparation of the definitive documents.",
        "Confidentiality obligations from the existing mutual NDA continue to apply to all information exchanged in this process.",
        "We appreciate the constructive approach your team has taken throughout these discussions.",
        "Any notices under the agreement should be sent to the addresses listed in the signature block of the master agreement.",
    ],
}


def para(kind, n=4):
    return " ".join(random.choice(FILLER[kind]) for _ in range(n))


def build(kind, facts, target_chars, positions):
    """Interleave filler paragraphs with fact sentences at fractional positions."""
    paras = []
    while sum(len(p) for p in paras) < target_chars:
        paras.append(para(kind))
    for frac, sentence in sorted(zip(positions, (f[1] for f in facts)), reverse=True):
        idx = min(len(paras), int(frac * len(paras)))
        paras.insert(idx, sentence)
    return "\n\n".join(paras)


# Each fact: (needles that must ALL appear in the summary, sentence planted in the body)
EMAILS = []

f = [
    (["$48,500"], "The revised budget for phase two is $48,500, up from the original estimate."),
    (["November 14"], "Code freeze for the release is November 14, so anything not merged by then slips."),
    (["Priya"], "Priya will own the database cutover and needs read access to the production replica."),
    (["INC-4471"], "The outage last Tuesday is tracked as INC-4471 and the postmortem is still open."),
    (["Thursday", "3"], "Please send me your section of the board deck by Thursday at 3pm."),
    (["Lisbon"], "The offsite has moved from Porto to Lisbon because the venue double-booked us."),
    (["two", "QA"], "We are also hiring two more QA engineers before the end of the quarter."),
    (["Marcus"], "Marcus is leaving the team at the end of the month; his last day is the 31st."),
    (["SSO"], "Correction to my earlier note: SSO is NOT shipping this release, it moves to January."),
    (["(415) 555-0193"], "If anything is urgent while I'm travelling, call my cell at (415) 555-0193."),
]
EMAILS.append(dict(
    id="project_update", subject="Q4 platform update and asks", sender="Dana Whitfield <dana@example.com>",
    body="Hi all,\n\n" + build("project", f, 15000, [0.05, 0.15, 0.3, 0.45, 0.55, 0.62, 0.7, 0.8, 0.9, 0.97])
    + "\n\nThanks,\nDana\n\nDana Whitfield | Director of Platform\nExample Corp | 1 Market St\nThis email and any attachments are confidential and intended solely for the addressee.",
    facts=f))

f = [
    (["October 31"], "Costume parade is on October 31 at 9:15am on the main field."),
    (["$12"], "The class photo package costs $12 and order forms are due back Monday."),
    (["Ms. Okafor"], "Ms. Okafor will be the new Year 4 teacher from the start of next term."),
    (["flu"], "Free flu vaccinations are offered in the school hall on November 6; consent forms are required."),
    (["early"], "School closes early at 1:30pm on Friday November 22 for staff training."),
    (["nut"], "Reminder: we are a nut-free school, including in birthday treats."),
    (["ParentPay"], "Trip payments now go through ParentPay only; cash is no longer accepted."),
    (["Science Museum"], "Year 5 visit the Science Museum on December 3 and need a packed lunch."),
    (["cancelled", "swimming"], "Update: swimming lessons on Wednesdays are cancelled for the rest of term because the pool is closed for repairs."),
]
EMAILS.append(dict(
    id="school_newsletter", subject="Westbrook Primary - Autumn newsletter", sender="Westbrook Primary <office@westbrook.example.sch.uk>",
    body="Dear Parents and Carers,\n\n" + build("school", f, 12000, [0.02, 0.2, 0.35, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98])
    + "\n\nKind regards,\nThe Office Team\n\nYou are receiving this because you are a parent at Westbrook Primary. Manage your email preferences here.",
    facts=f))

f = [
    (["Friday", "4"], "Check-in at the lodge is Friday from 4pm; the door code is 7720#."),
    (["7720"], "Write the door code down: 7720#."),
    (["$185"], "Everyone's share comes to $185 each, which covers the lodge and groceries."),
    (["Jordan"], "Jordan is driving the van and can take five people plus gear from the station."),
    (["Ridge"], "Saturday's main hike is the Eagle Ridge loop, about 14 km with 900 m of climbing."),
    (["Venmo"], "Please Venmo Sam by Wednesday so we can pay the lodge deposit."),
    (["11am"], "Checkout on Sunday is 11am sharp or we lose the cleaning deposit."),
    (["allerg"], "Alex has a severe shellfish allergy, so no shrimp in the shared meals please."),
]
quoted = ("\n\nOn Tue, Oct 21, 2026 at 9:02 AM Sam Lee <sam@example.com> wrote:\n" +
          "\n".join("> " + line for line in para("trip", 30).split(". ")))
EMAILS.append(dict(
    id="trip_thread", subject="Re: Mountain weekend - final plan", sender="Riley Chen <riley@example.com>",
    body="Hey everyone,\n\n" + build("trip", f, 8000, [0.05, 0.25, 0.4, 0.55, 0.7, 0.8, 0.9, 0.97])
    + "\n\nSee you there!\nRiley" + quoted,
    facts=f))

f = [
    (["$2.4", "million"], "The total contract value is $2.4 million over the initial term."),
    (["36"], "The initial term is 36 months from the effective date."),
    (["Net 45"], "Payment terms are Net 45 from receipt of a correct invoice."),
    (["99.9"], "Uptime commitment remains 99.9% measured monthly, with service credits as in Schedule B."),
    (["90 days"], "Either party may terminate for convenience on 90 days' written notice after month 12."),
    (["5%"], "Annual price increases are capped at 5%, applied on each anniversary."),
    (["Delaware"], "Governing law will be the State of Delaware."),
    (["December 1"], "We need your signed redlines by December 1 to hold the pricing."),
    (["liability"], "Liability is capped at twelve months of fees, except for breaches of confidentiality."),
    (["not", "exclusiv"], "To be clear, this arrangement is not exclusive; we may engage other vendors for the same services."),
]
EMAILS.append(dict(
    id="term_sheet", subject="Summary of proposed terms - Master Services Agreement", sender="Elena Marsh <emarsh@lawfirm.example>",
    body="Dear Mr. Patel,\n\n" + build("legal", f, 20000, [0.03, 0.12, 0.25, 0.38, 0.5, 0.6, 0.72, 0.82, 0.9, 0.98])
    + "\n\nSincerely,\nElena Marsh\nPartner\n\nCONFIDENTIALITY NOTICE: This message may contain privileged information.",
    facts=f))



class HTTPClient:
    """The two OMLXClient methods digest_body uses, over plain HTTP."""

    def __init__(self, base_url: str, api_key: str | None):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key

    def activity_key(self, model: str) -> str:
        return f"recall:{self.base_url}:{model}"

    async def chat(self, model, messages, *, max_tokens=900, temperature=None, **extra):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "stream": False, **extra}
        if temperature is not None:
            body["temperature"] = temperature
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/v1/chat/completions",
                                     json.dumps(body).encode(), headers)
        return await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(req, timeout=300)))


def recall(text: str, facts) -> tuple[int, list]:
    low = text.lower()
    missed = [needles for needles, _ in facts if not all(n.lower() in low for n in needles)]
    return len(facts) - len(missed), missed


async def run(args) -> None:
    from service.tools import email_digest as D
    D._TIMEOUT_S = args.timeout
    client = HTTPClient(args.base_url, args.api_key)
    totals: dict[str, list[float]] = {}
    for rep in range(args.reps):
        for e in EMAILS:
            row = {"sender": e["sender"], "subject": e["subject"], "body": e["body"]}
            t = time.monotonic()
            head = e["body"][:6000]
            resp = await client.chat(args.model, [{"role": "user", "content": (
                "Summarize this email for the person who received it.\n\n<email>\n"
                f"From: {e['sender']}\nSubject: {e['subject']}\n\n{head}\n</email>")}],
                max_tokens=900, chat_template_kwargs={"enable_thinking": False})
            cases = [("head-cut", resp["choices"][0]["message"].get("content") or "",
                      time.monotonic() - t)]
            D._cache.clear()  # measure the model, not the repeat-lookup cache
            t = time.monotonic()
            cases.append(("digest", await D.digest_body(row, client=client, model=args.model),
                          time.monotonic() - t))
            for name, text, secs in cases:
                got, missed = recall(text, e["facts"])
                agg = totals.setdefault(name, [0, 0, 0.0, 0])
                agg[0] += got; agg[1] += len(e["facts"]); agg[2] += secs; agg[3] += 1
                print(f"{rep} {e['id']:18} {name:9} {got}/{len(e['facts'])} {secs:5.1f}s missed={missed}",
                      flush=True)
    for name, (got, of, secs, n) in totals.items():
        print(f"{name:9} recall {got}/{of} = {100 * got / of:.0f}%  mean {secs / n:.1f}s")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", default="http://127.0.0.1:8795")
    p.add_argument("--model", default="gemma-4-E4B-it-oQ4e-mtp")
    p.add_argument("--api-key", default=os.environ.get("OMLX_API_KEY"))
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--timeout", type=float, default=60.0)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
