"""Author disposable specification-derived smoke examples; never reads an eval corpus."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def intent(sources=(), kind="read", excluded=(), unsupported=()):
    return dict(version=1, kind=kind, sources=list(sources),
                excluded_sources=list(excluded), unsupported_constraints=list(unsupported))


def source(domain, operation="overview", **fields):
    return dict(domain=domain, operation=operation, **fields)


def create():
    system = (ROOT / "router-system.txt").read_text() + "\nLocal clock: 2026-11-09T09:00:00-08:00. Prior completed tools (source metadata only): []"
    rows = []

    def route(split, family, prompt, expected, history=()):
        messages = [{"role": "system", "content": system}]
        messages += [dict(m, training=False) for m in history]
        messages += [{"role": "user", "content": prompt},
                     {"role": "assistant", "content": json.dumps(expected, separators=(",", ":")), "training": True}]
        rows.append(dict(id=family, family=family, split=split, category="routing", messages=messages, expected=expected))

    def text(split, family, category, prompt, answer):
        rows.append(dict(id=family, family=family, split=split, category=category,
                         messages=[{"role": "system", "content": "Answer using only the supplied facts. Be concise, preserve exact values, and never claim to have used tools."},
                                   {"role": "user", "content": prompt},
                                   {"role": "assistant", "content": answer, "training": True}], expected=answer))

    route("train", "smoke_calendar_week", "whats on my calender this week?", intent([source("calendar", time={"named": "this week"})]))
    route("train", "smoke_calendar_date", "Show my calendar for 2026-11-17.", intent([source("calendar", time={"date": "2026-11-17"})]))
    route("train", "smoke_email_unread", "Give me an overview of unread email from yesterday.", intent([source("email", unread=True, time={"named": "yesterday"})]))
    route("train", "smoke_email_literal", 'Find emails matching "Harbor prototype".', intent([source("email", "records", query="Harbor prototype")]))
    route("train", "smoke_message_person", "Summarize my messages with Mira from this month.", intent([source("messages", conversation="Mira", time={"named": "this month"})]))
    route("train", "smoke_excluded_messages", "Do not access messages. Show only my calendar tomorrow.", intent([source("calendar", time={"named": "tomorrow"})], excluded=["messages"]))
    route("train", "smoke_two_sources", "Summarize email and messages from last week.", intent([source("email", time={"named": "last week"}), source("messages", time={"named": "last week"})]))
    route("train", "smoke_reminder_overdue", "What reminders are overdue?", intent([source("reminders", scope="overdue")]))
    route("train", "smoke_draft_inline", 'Draft a reply here saying "I can meet at 3"; do not send it.', intent(kind="inline"))
    route("train", "smoke_send_unsupported", 'Send Mira a message saying "Running late".', intent(kind="unsupported"))
    route("train", "smoke_prohibition", "Do not read my email or messages.", intent(kind="none", excluded=["email", "messages"]))
    route("train", "smoke_context_source_switch", "Actually, show my calendar for tomorrow instead.", intent([source("calendar", time={"named": "tomorrow"})]), history=[{"role": "user", "content": "Show unread email from yesterday."}, {"role": "assistant", "content": "You asked for unread email from yesterday."}])
    text("train", "smoke_ground_calendar", "grounded", "Synthetic calendar results: Tuesday 2026-11-10, 09:15 Design check-in; Tuesday 2026-11-10, 14:00 Workshop. Summarize briefly.", "Tuesday, November 10\n- 9:15 AM: Design check-in\n- 2:00 PM: Workshop")
    text("train", "smoke_ground_partial", "grounded", "Synthetic results: calendar succeeded with 1 event, Thursday 11:00 Review. Reminders failed with timeout. Summarize the available results and coverage.", "Your calendar shows Review on Thursday at 11:00 AM. Reminders could not be checked because the request timed out.")
    text("train", "smoke_ground_empty", "grounded", "Synthetic email result: status=success, query=unread email, items=[]. Give a one-sentence summary.", "No unread emails were found in the checked results.")
    text("train", "smoke_ground_direction", "grounded", 'Synthetic conversation: Mira said "Can we meet at 2?" You replied "3 works better." Summarize who proposed each time.', "Mira proposed 2, and you suggested 3 instead.")
    text("train", "smoke_math", "retention", "What is 18 times 7? Give only the number.", "126")
    text("train", "smoke_extract", "retention", 'Extract the reference exactly: "Your reference is +Harbor-008." Return only the reference.', "+Harbor-008")
    text("train", "smoke_rewrite", "retention", 'Rewrite politely: "Give me the draft today."', "Could you please send me the draft today?")
    text("train", "smoke_spanish", "retention", 'Translate "See you tomorrow" into Spanish.', "Hasta mañana.")
    # Disposable development probes, not the future sealed quality benchmark.
    route("dev", "probe_calendar_month_exclusion", "Show my calendar for December 2026, but do not read my email.", intent([source("calendar", time={"month": "2026-12"})], excluded=["email"]))
    route("dev", "probe_notes_literal", 'Search my notes for "Cobalt invoice".', intent([source("notes", "records", query="Cobalt invoice")]))
    route("dev", "probe_draft_new", 'Write a possible text here: "Thanks for the update."', intent(kind="inline"))
    route("dev", "probe_calendar_range", "Give me a calendar overview from 2026-12-03 through 2026-12-05.", intent([source("calendar", time={"start": "2026-12-03", "end": "2026-12-05"})]))
    text("dev", "probe_ground_denied", "grounded", "Synthetic messages result: permission denied. Are there any messages?", "I could not check messages because access was denied.")
    text("dev", "probe_math_new", "retention", "What is 23 plus 19? Give only the number.", "42")
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    manifest = {"purpose": "disposable compatibility probes, not evidence of routing quality", "source": "new synthetic specifications; no personal data, old evals, or teacher model outputs", "pilot_generated": False, "final_quality_set_generated": False, "files": {}}
    for split in ("train", "dev"):
        selected = [r for r in rows if r["split"] == split]
        raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in selected).encode()
        path = out / (split + ".jsonl")
        path.write_bytes(raw)
        manifest["files"][path.name] = {"count": len(selected), "families": [r["family"] for r in selected], "sha256": hashlib.sha256(raw).hexdigest()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    create()
