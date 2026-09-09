"""Extractive summaries preserve attribution and dates without a second LLM."""


def source_digest(lines: list[str], label: str, kind: str, *, limit: int = 80) -> str:
    from service import debug_capture
    debug_capture.record("source", label=f"{kind} — {label}", text="\n".join(lines))
    if not lines:
        return f"No {kind} found for {label}."
    selected = lines[:limit]
    # Written to be read by a PERSON as well as by the model. These digests are
    # returned to the user verbatim by design (a generative rewrite is what
    # misattributed messages), so the header is part of the answer: the old one
    # was an instruction addressed to the model — "Source excerpts (not inferred
    # outcomes); names, accounts and relative dates below are quoted from the
    # original messages." — and it opened every mail and message answer. It still
    # states the same grounding, as a sentence.
    header = (f"{kind.title()} — {label}. Quoted from the source; the names, "
              "accounts and dates are the sender's own, not conclusions.")
    omitted = (f"\nShowing {len(selected)} of {len(lines)} supplied entries; "
               "this is not the complete set.") if len(lines) > limit else ""
    return header + "\n" + "\n".join("- " + line for line in selected) + omitted
