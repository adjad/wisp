"""User profile builder — Wisp's own long-term "who is this person" model.

Reads what Wisp has ALREADY synced from Mail, Messages, Notes, and Calendar (no
new OS permission, no new data path — see email_tools.py/imessage_tools.py/
notes_tools.py's caches and assistant/store.py's commitments) and asks the
LOCAL model to distill durable facts about the user: identity, people in their
life, work/school, interests, routines, preferences. Same local-only pattern as
summarize_emails/summarize_messages — the raw personal content never leaves
this process, let alone the machine.

Persisted at ~/.moe/profile.md (the profile itself, under fixed section
headers) plus ~/.moe/profile.json (bookkeeping: last-scanned time/size per
source, for status reporting). Re-running build_profile MERGES into the
existing profile rather than starting over — see _fold's system prompt.

Coverage note: this only ever sees what's currently in each source's cache —
~50 most-recent raw emails (email_tools._raw_emails) plus up to a YEAR of
email headers (email_tools._history) and calendar commitments (past + future,
see AssistantStore.history/active_future — CalendarReader.swift now syncs a
year back), and recent iMessage/SMS history and the ~100 most-recently-
modified notes (still recent-only — Messages/Notes weren't part of the
history-extension request). Not a full-disk or iCloud crawl; extending
coverage to arbitrary files/cloud storage would need a separate indexing pass
(see service/search/) and is out of scope here.
"""
from __future__ import annotations

import json
import re
import time

from service.config import role_to_model
from service.inference.omlx_client import OMLXClient
from service.paths import STATE_DIR

PROFILE_MD = STATE_DIR / "profile.md"
PROFILE_META = STATE_DIR / "profile.json"

SECTIONS = [
    "Identity & basics",
    "People in their life",
    "Work / school",
    "Interests & hobbies",
    "Routines & commitments",
    "Preferences & communication style",
    "Other notable facts",
]

# ~6000 tokens per extraction call. Headers/calendar lines are compact but a
# full year of them adds up (a year of email headers alone can be 100K+
# chars) — a bigger batch keeps the total number of model calls (and thus
# build_profile's wall-clock time) reasonable.
#
# Raised from 16000 now that the map phase runs on gemma (~6.3GB resident)
# instead of gpt-oss (~11.8GB) — see models.yaml's profile_map role. gemma's
# smaller footprint leaves more wired-memory headroom for a bigger batch's
# prefill/KV-cache, the same tradeoff _REDUCE_CHARS documents for gpt-oss.
# This number is a conservative first step, not a re-measured ceiling for
# gemma specifically — raise it further only after checking actual memory
# pressure (`sysctl iogpu.wired_limit_mb` against oMLX's reported footprint)
# on a real build, the way 16000 itself was originally tuned against gpt-oss.
_BATCH_CHARS = 24000
# Ceiling on extraction calls PER SOURCE. Raised from 20 to 60: at 20, a
# year of email (436K chars = 28 batches) had ~26% of its batches sampled
# away, which directly cost detail the user asked for. 60 covers every current
# source in full — the cap now only exists so a pathologically large source
# can't run unbounded, not as a routine constraint. When it does bind, batches
# are sampled evenly across the whole range, never truncated to the newest
# (see _sample_batches).
_MAX_BATCHES_PER_SOURCE = 60
# Max characters of extracted facts fed to one reduce call.
#
# 90000 was WRONG on this hardware and had to come back down. gpt-oss's context
# window (131K tokens) is not the binding constraint — Metal memory is. A 90K
# char fact list is a ~23,400-token prefill, and measured against the 20GB
# ceiling with 11.25GB of weights resident, those calls drove gpt-oss's
# footprint to 11.89GB and produced a hard
# `POST /v1/models/gpt-oss/load -> 409: [METAL] Command buffer` failure, with
# system memory pressure severe enough that the user had to quit oMLX. Fitting
# in the context window is necessary but not sufficient; the KV cache for that
# prefill has to fit in what's left of the wired limit too.
#
# 30000 (~7,500 tokens) keeps each merge call well inside the headroom. The
# hierarchical rounds in _reduce_all already handle a fact list larger than
# this without losing anything, so the only cost is a few more (cheap) calls.
_REDUCE_CHARS = 30000
# A system-prompt-sized slice for the agent loop's every-turn injection (see
# agent/loop.py's run_agent) — capped so it can't crowd out the rest of the
# context budget. The full profile is always available via show_profile.
_DIGEST_CHARS = 2400

_client: OMLXClient | None = None

# Single-flight guard. A build is ~50 sequential gpt-oss calls holding the model
# exclusively for 20+ minutes, so two at once means two large prefills competing
# for the same wired-memory budget — enough on a 24GB machine to fail a model
# load outright with a Metal command-buffer error and put the whole system under
# memory pressure. Now genuinely reachable, since the 4am scheduled refresh
# (scheduler._maybe_daily_profile) can coincide with the menu-bar "Build My
# Profile" button or the build_profile agent tool. A second caller returns
# immediately instead of queueing: waiting 20 minutes to then redo work that
# just finished is never what the caller wanted.
_build_lock: object | None = None
_build_running = False


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


def _load_meta() -> dict:
    if PROFILE_META.exists():
        try:
            return json.loads(PROFILE_META.read_text())
        except Exception:  # noqa: BLE001 — a corrupt sidecar must not break a rebuild
            return {}
    return {}


def _save_meta(meta: dict) -> None:
    PROFILE_META.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_META.write_text(json.dumps(meta, indent=2))


def get_profile_text() -> str:
    if PROFILE_MD.exists():
        try:
            return PROFILE_MD.read_text().strip()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def get_profile_meta() -> dict:
    return _load_meta()


def has_profile() -> bool:
    return bool(get_profile_text())


def _split_sections(text: str) -> dict[str, list[str]]:
    """Parse a profile into {section: [bullet, ...]}, preserving order."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out.setdefault(current, [])
        elif current and line.strip().startswith(("-", "*")):
            out[current].append(line.rstrip())
    return out


# How many bullets per section the no-argument overview shows, and the hard
# character ceiling on any single show_profile result.
_OVERVIEW_PER_SECTION = 12
_MAX_TOOL_CHARS = 12000


def render_profile(text: str, section: str | None = None) -> str:
    """A BOUNDED view of the profile, for the show_profile tool.

    Returning the whole profile is what broke "what do you know about me": at
    881 bullets it was an 89,000-character tool result, which put ~27,000
    tokens of undifferentiated bullets into the model's context. Verified from
    a real trace — the model completed normally (finish_reason=stop) and, given
    that wall of text, emitted a 50-token non-answer ("I'm a local assistant,
    so I can't browse the web") instead of using any of it. A tool result has
    to be small enough to actually reason over; the full document is for the
    user to read, not for the context window.
    """
    sections = _split_sections(text)
    if not sections:
        return text[:_MAX_TOOL_CHARS]

    if section:
        # Tolerant match — the model rarely reproduces a section name exactly.
        key = next((k for k in sections if k.lower() == section.lower().strip()), None)
        if key is None:
            want = section.lower().strip()
            key = next((k for k in sections
                        if want in k.lower() or k.lower().split()[0] in want), None)
        if key is None:
            return (f"(no section named {section!r}. Available: "
                    f"{', '.join(sections)})")
        body = "\n".join(sections[key])[:_MAX_TOOL_CHARS]
        return f"## {key}\n{body}"

    parts = ["(Overview — the most important entries per section. For the full "
             "list of any one section, call show_profile with that section name.)"]
    for name, bullets in sections.items():
        if not bullets:
            continue
        shown = bullets[:_OVERVIEW_PER_SECTION]
        extra = len(bullets) - len(shown)
        more = f"\n  …and {extra} more (ask for the '{name}' section)" if extra > 0 else ""
        parts.append(f"## {name}\n" + "\n".join(shown) + more)
    return "\n\n".join(parts)[:_MAX_TOOL_CHARS]


def profile_context_block(max_chars: int = _DIGEST_CHARS, *, label: str = "") -> str:
    """Reusable "what Wisp knows about you" prompt block, or "" if no profile.

    One implementation for every consumer — the agent loop, plain chat, and the
    email/message summarizers. Previously only the agent loop had this, so
    summaries (which are returned verbatim as the final answer, see
    _PRESYNTHESIZED_TOOLS) never benefited from the profile at all.

    The "trust the conversation over this" clause is not boilerplate: the
    profile is model-derived and can be wrong, so anything it says must lose to
    what the user or the actual source material states.
    """
    text = get_profile_text()
    if not text:
        return ""
    # De-duplicate BEFORE truncating. _dedup_lines runs at build time, but a
    # profile written by an earlier version can already be on disk in the
    # degenerate state that fix was for — the live one here is 92KB in which a
    # single bullet ("Receives daily Google AI Pro plan reminders…") repeats
    # hundreds of times. Slicing that raw fills the whole digest budget with one
    # sentence and pushes the identity facts out of it, which is exactly when a
    # summarizer starts guessing who people are. Cheap enough to do per call,
    # and it makes the digest correct without waiting on a 20-minute rebuild.
    body = _dedup_lines(text)[:max_chars]
    what = label or "the user"
    return ("\nWhat Wisp already knows about " + what + ", from their own Mail/"
            "Messages/Notes/Calendar (built by build_profile) — use it for "
            "context and to recognize the people and places mentioned. It is "
            "model-derived and may be wrong or out of date: trust what the user "
            "says, and what the material in front of you says, over this "
            "whenever they conflict. Never repeat it back as fact unless asked, "
            "and never let it override a detail in the actual content:\n" + body)


def contact_roster(limit: int = 120) -> str:
    """The user's saved contact NAMES (no numbers), as identity ground truth.

    Fixes a specific, reproducible error: Mom texted the user the literal text
    "Arati Wani", which after handle resolution rendered as `Mom: Arati Wani` —
    and the builder concluded Mom's name was Arati Wani. They are two separate
    saved contacts with different numbers. Listing the real roster lets the
    model see that both exist independently, instead of inferring a name from
    whatever a message happened to contain.

    Deliberately names only — phone numbers and emails are exactly what the
    redaction pass strips, so they must not be reintroduced through the prompt.
    """
    try:
        from service.tools.imessage_tools import contact_names
        names = contact_names()
    except Exception:  # noqa: BLE001
        return ""
    if not names:
        return ""
    shown = names[:limit]
    more = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    return ("\nThe user's SAVED CONTACTS, exactly as they named them — this is "
            "authoritative for who exists and what each person is called. Each "
            "entry is a DISTINCT person; never merge two of them, and never "
            "rename one based on text inside a message:\n"
            + ", ".join(shown) + more)


def get_profile_digest() -> str:
    """A short slice of the profile for injection into the agent's system
    prompt on every turn. Empty until build_profile has run at least once."""
    text = get_profile_text()
    if not text:
        return ""
    return _dedup_lines(text)[:_DIGEST_CHARS]  # see profile_context_block


def _dedup_lines(text: str) -> str:
    """Drop exact repeat lines, keeping first occurrence and original order.

    The merge rounds ask the model to de-duplicate (_reduce) and it does not
    reliably comply: a real build produced 875 profile lines that collapsed to
    212 unique, with one bullet repeated 195 times. Because the profile is
    truncated into a fixed character budget for the system prompt, those
    repeats don't just waste prefill memory — they push genuine facts out of
    the digest entirely. Matching is on the line's normalized text (case and
    surrounding punctuation/whitespace folded) so trivially-reformatted
    restatements collapse too, but the ORIGINAL line is what's kept.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = " ".join(line.split()).strip("-•* \t").casefold()
        if not key:
            # Keep blank lines/headings structure, but never let them dedup away.
            out.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _batches(text: str, size: int = _BATCH_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def _sample_batches(batches: list[str], cap: int) -> list[str]:
    """At most `cap` batches, sampled EVENLY across the whole list.

    Truncating with `batches[:cap]` would be actively wrong here: every source
    is ordered newest-first, so taking a prefix means "only the most recent
    slice" — precisely the failure this profile had (a year of email on disk,
    a profile that only knew about the last few days). Even sampling keeps
    coverage spread across the entire time range when a source is too big to
    process in full.
    """
    if len(batches) <= cap:
        return batches
    step = len(batches) / cap
    return [batches[min(int(i * step), len(batches) - 1)] for i in range(cap)]


# --- phase 1: map (extract facts from one batch, independently) -------------
#
# Deliberately does NOT see the running profile. The previous design folded
# each batch into a single carried-forward profile with a hard per-section
# bullet cap, which meant every batch could EVICT facts learned from earlier
# ones — with dozens of batches the surviving profile described little beyond
# whatever was processed last. Extracting independently and merging once at
# the end (see _reduce) removes that ordering bias entirely.
_MAP_SYS = (
    "You extract durable facts about a person from their own private data, for "
    "an on-device assistant that uses them to give more personal help.\n"
    "\n"
    "Output a flat list of '- ' bullets. No headers, no preamble, no closing "
    "commentary. Each bullet is ONE specific, self-contained fact.\n"
    "\n"
    "WHAT TO EXTRACT — durable things that would still be true next month:\n"
    "- Identity: name, where they live/study/work, role, affiliations.\n"
    "- People: names of family, friends, colleagues, and HOW they relate to "
    "the user. Always say who someone is, not just that they exist.\n"
    "- Work/school: employer, school, program, courses, projects, "
    "responsibilities.\n"
    "- Interests, hobbies, and recurring activities.\n"
    "- Routines: recurring meetings, standing commitments, regular patterns.\n"
    "- Preferences: tools, communication style, food, scheduling habits.\n"
    "\n"
    "ACCURACY RULES — these matter more than completeness:\n"
    "- Record ONLY what the material actually shows. If you find yourself "
    "writing 'may', 'might', 'possibly', 'seems to', or 'likely', DELETE that "
    "bullet — a guess is worse than a gap.\n"
    "- Do not infer a relationship, employer, or role that isn't stated. Two "
    "people appearing in one thread does not establish how they know each "
    "other.\n"
    "- Copy names, organizations, and dates EXACTLY as they appear. Never "
    "adjust a year or 'correct' a name to something more familiar.\n"
    "- When a fact is tied to a time, include it ('as of March 2026', 'every "
    "Friday') — an undated fact that later goes stale is how a profile turns "
    "wrong.\n"
    "- Skip one-off transient noise: order numbers, confirmation codes, a "
    "single meeting's time/place, marketing email, automated notifications.\n"
    "\n"
    "NEVER RECORD, even though you will see them — this profile is injected "
    "into other prompts and can be exported to a file, so a secret written "
    "here escapes the one message it belonged in:\n"
    "- Verification / 2FA / one-time / security / Steam Guard codes.\n"
    "- Passwords, passcodes, PINs, API keys, or access tokens.\n"
    "- Card numbers, CVVs, bank/account numbers, or purchase payment details.\n"
    "- Phone numbers, full street addresses, SSNs, or government ID numbers.\n"
    "'Lives in Danville, CA' is a good fact; the street address is not. "
    "'Plays games on Steam' is a good fact; the login code is not.\n"
    "\n"
    "TIME — every line you are given is date-stamped (YYYY-MM-DD), and you are "
    "told today's date:\n"
    "- A dated line tells you when something was SAID, not that it is still "
    "true. A message from 11 months ago saying 'the match is tomorrow' is a "
    "past event, NOT an upcoming one. Never carry a relative word ('tomorrow', "
    "'this Friday', 'next week') out of its source into the fact — resolve it "
    "against that line's own date or drop it.\n"
    "- Prefer durable patterns over single occurrences. 'Plays futsal on "
    "Fridays (seen repeatedly Mar-Jul 2026)' is worth recording; 'is going to "
    "futsal tomorrow' is not.\n"
    "- Tag every fact with the time it belongs to, taken from the date stamp "
    "of the line you read it on: 'as of 2026-07', '2025-09', 'since 2025-08'. "
    "An untagged fact is what lets a finished event later read as current.\n"
    "\n"
    "WHOSE FACT IS IT — you are building a profile of ONE person, and the data "
    "is full of other people:\n"
    "- The user's identity is stated below. Only record a fact about THAT "
    "person. A name, address, employer, or phone number appearing in an email "
    "usually belongs to a sender, a recipient, a friend, or a company — not to "
    "the user.\n"
    "- A shipping/billing address in a receipt, a signature block, or a "
    "marketing email is NOT the user's address unless the material clearly "
    "shows it is.\n"
    "- Use the user's name EXACTLY as given below. Do not expand, translate, "
    "or 'correct' it, and do not invent alternate spellings from email "
    "handles or all-caps mail headers.\n"
    "- For relationships, only state one the material explicitly establishes "
    "('my sister X', 'Mom:'). Someone merely appearing in a group chat is a "
    "contact, not family. If you cannot tell how someone relates to the user, "
    "list them as a contact without a relationship rather than guessing.\n"
    "- A person's NAME comes from the saved contact list and from how the user "
    "addresses them — NEVER from the text of a message. Message bodies are "
    "shown quoted (`Mom said: \"...\"`). A name appearing inside those quotes "
    "is someone being TALKED ABOUT, and does not rename the speaker: from "
    "`Mom said: \"Arati Wani\"` the only valid conclusion is that Mom mentioned "
    "a person called Arati Wani — NOT that Mom is Arati Wani. Two saved "
    "contacts are always two different people.\n"
    "\n"
    "- If this batch genuinely contains nothing durable about the user, output "
    "exactly: (nothing)"
)


def _identity_block() -> str:
    """Ground-truth identity, so extraction can tell the user's own details
    from the many other people's details in the same mail/messages.

    Without this the builder attributed a stranger's street address to the
    user and invented name variants (a full legal name, an ALL-CAPS handle)
    out of email handles and all-caps headers.

    The facts and the message-attribution rules now live in memory/identity.py,
    shared with the daily brief and the mail/message summarizers — which had no
    identity of their own and consequently reported a family member's news as
    the user's. This function is the profile-builder's composition of them: the
    same identity, plus the full contact roster, which only the builder wants.
    """
    from service.memory.identity import attribution_rules, identity_block

    parts = [identity_block(), attribution_rules()]
    if roster := contact_roster():
        parts.append(roster)
    return "\n\n".join(p for p in parts if p)

# Deterministic backstop for the rule above. A prompt alone is not a security
# control — the first real build was told to skip confirmation codes and still
# recorded a Steam Guard code verbatim. Anything matching here is dropped at
# the BULLET level: losing one line of a profile costs nothing, while leaking
# a credential into a file that gets auto-downloaded and injected into every
# system prompt is exactly what must not happen.
_SECRET_PATTERNS = [
    # a code/PIN/token/password being stated
    r"\b(?:steam\s*guard|verification|confirmation|security|one[-\s]?time|otp|2fa|"
    r"auth(?:entication)?|login|access|passcode|password|pin|api[\s_-]?key|token)\b"
    r"[^.\n]{0,40}\b(?:code|key|token|is|:)\b",
    r"\b(?:password|passcode|api[\s_-]?key|access token|cvv|cvc)\b",
    # long digit runs — card / account / ID numbers
    r"\b\d{13,19}\b",
    # phone numbers. Matched against a dash-normalized copy of the line (see
    # _redact) because the model writes them with typographic dashes — a real
    # leak got through as "(408) 594‑9423" using U+2011, which an ASCII-only
    # "-" class does not match. Area code may be parenthesized.
    r"\+\d{10,15}\b",
    r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    r"\bphone numbers?\b|\bcontact number\b|\bcell\b.{0,12}\d{3}",
    # street address: number + street-type word
    r"\b\d{2,6}\s+[A-Z][A-Za-z]*\s+(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Blvd|"
    r"Boulevard|Ln|Lane|Ct|Court|Way|Pl|Place|Ter|Terrace|Cir|Circle)\b",
    r"\bssn\b|\bsocial security\b",
]
_SECRET_RE = re.compile("|".join(_SECRET_PATTERNS), re.IGNORECASE)


# Typographic dashes the model uses interchangeably with ASCII "-": figure
# dash, non-breaking hyphen, en/em dash, minus sign. Normalized before matching
# so a phone number written "594‑9423" can't slip past an ASCII-only pattern.
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−"})


def _augment_people_section(profile: str) -> str:
    """Guarantee real, active contacts appear in "People in their life" even
    when the model's extraction missed or emptied the section — see
    memory/entities.py for why that's a real, observed failure mode. Only
    APPENDS rows for contacts the model's own text doesn't already mention by
    name; never removes or rewrites what the model wrote, since a relationship
    description it did capture ("Mom, texts most days about...") is strictly
    more useful than the bare activity line this adds.
    """
    from service.memory.entities import roster_lines

    sections = _split_sections(profile)
    if "People in their life" not in sections:
        return profile
    existing_text = "\n".join(sections["People in their life"]).casefold()
    missing = [line for line in roster_lines()
               if line.split("**")[1].casefold() not in existing_text]
    if not missing:
        return profile

    parts = []
    for name in SECTIONS:
        bullets = sections.get(name, [])
        if name == "People in their life":
            bullets = [b for b in bullets if b.strip() != "- (nothing found yet)"]
            bullets = bullets + missing
        body = "\n".join(bullets) if bullets else "- (nothing found yet)"
        parts.append(f"## {name}\n{body}")
    return "\n\n".join(parts)


def _redact(profile: str) -> str:
    """Drop any bullet carrying a secret or direct identifier (see
    _SECRET_PATTERNS). Section headers and non-bullet lines pass through, so
    the document structure survives even if a whole section empties out."""
    out: list[str] = []
    for line in profile.splitlines():
        if (line.lstrip().startswith(("-", "*"))
                and _SECRET_RE.search(line.translate(_DASHES))):
            continue
        out.append(line)
    return "\n".join(out)


# gpt-oss is a REASONING model: left to its own devices it spends the token
# budget narrating an <|channel|>analysis monologue and the real answer gets
# cut off at the cap. Measured on the previous build — 3 of its 4 completions
# came back finish_reason=length, i.e. the profile text was literally
# truncated mid-thought, which is a large part of why the result read as
# thin and inaccurate. Every other gpt-oss path in this app already pins
# reasoning_effort for exactly this reason (see agent/loop.py, search/synth.py,
# and config.resolve_reasoning_effort); profile building was the one that
# didn't. "low" is right here: extraction and merging are mechanical
# rewriting tasks, not the hard multi-step reasoning "medium" exists for.
_EFFORT = {"reasoning_effort": "low"}
# The MERGE passes get "medium". Extraction (above) is mechanical pattern-
# spotting where low is correct, but merging is genuinely reasoning-shaped:
# categorizing ~90K chars of facts into the right section, resolving conflicts
# between facts from different months, and de-duplicating without losing the
# most specific wording. Safe to raise now that these calls have a 4000-token
# budget — the original truncation problem came from an UNSET effort against a
# 1400-token cap, not from medium itself.
_MERGE_EFFORT = {"reasoning_effort": "medium"}


async def _map_extract(client: OMLXClient, model: str, source: str, batch: str) -> str:
    # reasoning_effort is a gpt-oss/harmony-template concept — gemma (the
    # default profile_map model) doesn't have it and every other gemma call
    # site in this codebase omits it too. Only sent when the map role is
    # actually pointed at a gpt-oss model, so reassigning profile_map back to
    # gpt-oss via Settings doesn't silently reintroduce the truncation bug
    # _EFFORT exists to prevent (see the comment above it).
    kwargs = {"chat_template_kwargs": _EFFORT} if "gpt-oss" in model else {}
    resp = await client.chat(
        model,
        [{"role": "system", "content": f"{_MAP_SYS}\n\n{_identity_block()}"},
         {"role": "user", "content":
             f"The following is the user's own {source} content, oldest first, "
             f"each line date-stamped. Extract durable facts about the user.\n\n{batch}"}],
        temperature=0.1, max_tokens=2000, **kwargs,
    )
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    return "" if text.startswith("(nothing") else text


# --- phase 2: reduce (merge all extracted facts into the profile) -----------
#
# Run ONCE PER SECTION rather than once for the whole profile. A single call
# had to produce all seven sections inside one token budget, which is what
# kept the finished profile down to a few KB no matter how much data went in —
# the model rationed itself across sections. Per-section passes give each
# section the full budget, and a focused pass ("find everything about the
# people in their life") also reads the fact list far more thoroughly than one
# pass trying to do everything at once.
_SECTION_GUIDANCE = {
    # Deliberately the ONLY narrow section. It came back with 216 bullets on a
    # real build — "be exhaustive" beat "the facts you'd need first", and it
    # became a dumping ground duplicating every other section. The cap and the
    # explicit exclusions are what keep it a summary rather than a copy.
    "Identity & basics": "STRICT LIMIT: at most 15 bullets, and this is the one section that must "
        "stay short. ONLY: their name, age/stage of life, city (never the street address), "
        "languages, and the few defining facts you would state in a one-paragraph introduction. "
        "Everything else belongs to another section — do NOT put schools, courses, jobs, people, "
        "hobbies, routines, preferences, or events here even though you can see them.",
    "People in their life": "One bullet PER PERSON. Give the name, how they relate to the user, and "
        "what the user actually does with them. Group family, then close friends, then wider "
        "contacts. Only assert a relationship the facts establish; otherwise say 'contact'.",
    "Work / school": "Employer/school, program, year, specific courses and grades, roles and titles, "
        "projects, responsibilities, and the people involved in each.",
    "Interests & hobbies": "Each distinct interest as its own bullet, with the specifics — which "
        "sports/teams/games/artists, where they do it, who with, how often.",
    "Routines & commitments": "Recurring patterns with their actual cadence and time "
        "('futsal Fridays 7-9pm at X'). Separate what is CURRENT from what has ended.",
    "Preferences & communication style": "How they write and to whom, tools and apps they favor, "
        "food, scheduling habits, and anything about how they like to be helped.",
    "Other notable facts": "ONLY durable facts that genuinely fit NONE of the sections above. "
        "This is a small remainder, not a catch-all — if a fact could plausibly sit in an earlier "
        "section, it belongs there and must be omitted here. Expect few bullets.",
}

# Each fact belongs in exactly ONE section. Without this the sections
# duplicated each other heavily (Identity & basics and Other notable facts came
# back with 216 and 204 bullets on a real build, largely restating Work/school,
# Routines and Preferences), which inflates the file without adding knowledge.
_SECTION_SCOPE = (
    "SCOPE — each fact belongs to exactly ONE section:\n"
    + "\n".join(f"  {s}: {_SECTION_GUIDANCE[s].split('.')[0]}." for s in SECTIONS)
    + "\nWrite ONLY the section you were asked for. If a fact fits an earlier "
      "section in that list better than yours, leave it out — the other pass "
      "records it. Never restate a fact just because it is also true of your "
      "section."
)

_REDUCE_SYS = (
    "You are writing ONE SECTION of an on-device assistant's private profile "
    "of its user, from facts already extracted from that user's own data.\n"
    "\n"
    "Output ONLY the bullets for the section named below — no header, no "
    "preamble, no commentary. Every line starts with '- '.\n"
    "\n"
    "Rules:\n"
    "- Use ONLY the facts provided. Never add anything not present in them.\n"
    "- Include ONLY facts belonging to this section. Ignore the rest.\n"
    "- BE EXHAUSTIVE AND DETAILED **within this section's scope**: if the facts "
    "support forty bullets that genuinely belong here, write forty. Never drop "
    "a real in-scope fact for brevity, and never collapse several distinct "
    "specifics into one vague line. But exhaustive does NOT mean including "
    "out-of-scope facts — a section that restates another section's content is "
    "wrong, not thorough. Respect any stated bullet limit for this section.\n"
    "- MERGE only true duplicates: the same fact stated several ways becomes "
    "ONE bullet keeping the MOST specific version (names, dates, places, "
    "numbers all preserved). Prefer 'Sam Ortiz, their manager at Redpoint, "
    "runs the Tuesday 10am planning sync' over three vaguer bullets about Sam.\n"
    "- EVERY BULLET MUST START WITH A TIME TAG, in one of exactly these forms:\n"
    "    `- [now] ` — true today and ongoing (a current school, job, habit).\n"
    "    `- [since YYYY-MM] ` — ongoing, and you know when it started.\n"
    "    `- [YYYY-MM] ` — a fact or event tied to that single month, now past.\n"
    "    `- [YYYY-MM → YYYY-MM] ` — was true over that span, now finished.\n"
    "    `- [always] ` — timeless (a sibling, a birthplace, a preference).\n"
    "  Derive the month from the date stamps on the source lines the fact came "
    "from — never from today's date, and never guess. If the material genuinely "
    "does not pin a fact to a time, use [always] only when it is truly "
    "timeless; otherwise use the month of the line you read it on.\n"
    "- The tag decides the tense: write [now]/[since]/[always] bullets in the "
    "present tense, and dated/past-span bullets in the past tense. A finished "
    "event must never read as upcoming.\n"
    "- Drop any fact that is hedged, speculative, or self-contradictory. When "
    "two conflict, keep the one with more specific support, or describe the "
    "change over time if both were true at different points.\n"
    "- Order bullets most-important first.\n"
    "- If the facts contain nothing for this section, output exactly: "
    "- (nothing found yet)"
)


async def _reduce_section(client: OMLXClient, model: str, section: str,
                          facts: str, prior: str = "") -> str:
    prior_block = (f"\n\nPREVIOUS VERSION of this section (carry forward anything "
                   f"still supported, but let the new facts correct it):\n{prior}"
                   if prior else "")
    resp = await client.chat(
        model,
        [{"role": "system", "content":
            f"{_REDUCE_SYS}\n\n{_identity_block()}"},
         {"role": "user", "content":
            f"SECTION TO WRITE: {section}\n"
            f"WHAT BELONGS IN IT: {_SECTION_GUIDANCE.get(section, '')}\n\n"
            f"{_SECTION_SCOPE}\n\n"
            f"EXTRACTED FACTS:\n{facts}{prior_block}\n\n"
            f"BULLETS FOR '{section}':"}],
        temperature=0.2, max_tokens=4000, chat_template_kwargs=_MERGE_EFFORT,
    )
    return (resp["choices"][0]["message"].get("content") or "").strip()


async def _reduce(client: OMLXClient, model: str, facts: str, prior: str = "") -> str:
    """Compress a fact list without sectioning — used only by _reduce_all's
    hierarchical rounds, to shrink an oversized fact list before the
    per-section passes run over it."""
    resp = await client.chat(
        model,
        [{"role": "system", "content":
            "You de-duplicate extracted facts about one person. Output a flat "
            "'- ' bullet list preserving EVERY distinct fact, merging only "
            "true duplicates and always keeping the most specific wording "
            "(names, dates, places, numbers). Do not summarize, categorize, or "
            "drop anything for brevity. Output only bullets."},
         {"role": "user", "content": f"FACTS:\n{facts}\n\nDE-DUPLICATED:"}],
        temperature=0.1, max_tokens=4000, chat_template_kwargs=_MERGE_EFFORT,
    )
    return (resp["choices"][0]["message"].get("content") or "").strip()


def _calendar_text() -> str:
    """Past year + upcoming commitments as flat lines (see AssistantStore.history
    /active_future — CalendarReader.swift now syncs a year back, not just
    upcoming)."""
    from service.assistant.store import assistant_store

    now = time.time()
    rows = sorted(assistant_store.history(days=365)
                  + assistant_store.active_future(horizon_days=365),
                  key=lambda r: r.get("when_ts") or 0)
    lines = []
    for r in rows:
        ts = r.get("when_ts")
        when = (time.strftime("%Y-%m-%d %a %-I:%M %p", time.localtime(ts)) if ts else "?")
        # Explicitly labelled PAST/UPCOMING. A bare date left the model to work
        # out tense from an unknown "today", and it guessed wrong — writing
        # finished events as if they were still ahead.
        tense = "UPCOMING" if ts and ts >= now else "PAST"
        loc = f" @ {r['location']}" if r.get("location") else ""
        ctx = f" ({r['context']})" if r.get("context") else ""
        acct = f" [{r['account']}]" if r.get("account") else ""
        lines.append(f"[{tense}] {when} — {r.get('kind', 'event')}: "
                     f"{r.get('title', '(untitled)')}{ctx}{acct}{loc}")
    return "\n".join(lines)


def _gather_sources(only: list[str] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """((source_name, raw_text) for each source with content, [empty sources]).

    The empty list matters: a source silently contributing nothing is exactly
    how a profile ends up confidently describing a person from a quarter of
    their data, so build_profile reports it back rather than quietly omitting
    it. Imported lazily to avoid a hard import-time dependency between memory/
    and tools/.

    `only` restricts which sources are even fetched — used by the scheduler's
    per-source rotation (see scheduler._maybe_daily_profile) so a source that
    simply wasn't THIS run's turn doesn't get reported as "missing" (which
    would misleadingly read as a sync problem rather than a schedule choice).
    """
    from service.tools import email_tools, imessage_tools, notes_tools

    sources: list[tuple[str, str]] = []
    missing: list[str] = []
    want = lambda name: only is None or name in only  # noqa: E731

    # BOTH email views, not either/or. The previous version used raw bodies
    # when present and otherwise fell back to headers — but raw covers only
    # the ~50 newest messages while history reaches back a year, so whenever
    # raw was warm the profile silently lost the entire year of coverage.
    # They're complementary: bodies give depth on recent mail, headers give
    # breadth across the year.
    # exclude_machine=True: strips automated/marketing senders before the model
    # ever sees them (see email_tools.is_machine_sender). This mailbox is
    # dominated by job-alert and notification traffic, and a real build's
    # single most-repeated fact (195 of 868 bullets) was "receives daily Google
    # AI Pro plan reminders" — noise that crowded out actual identity and
    # relationships from the fixed-size digest. summarize_emails/view_emails
    # intentionally do NOT filter this; a user reading their own inbox wants
    # everything, promos included.
    if want("email"):
        email_parts = [p for p in (email_tools.all_header_text(exclude_machine=True),
                                   email_tools.all_raw_email_text(exclude_machine=True)) if p]
        if email_parts:
            sources.append(("email", "\n\n".join(email_parts)))
        else:
            missing.append("email")

    for name, fetch in (("messages", imessage_tools.all_message_text),
                       ("notes", notes_tools.all_notes_text),
                       ("calendar", _calendar_text)):
        if not want(name):
            continue
        text = fetch()
        if text:
            sources.append((name, text))
        else:
            missing.append(name)

    return sources, missing


async def _publish_progress(**fields) -> None:
    """Live build progress over the same /assistant/events SSE channel the
    app already uses for reminders/changed pings — see OverlayModel's
    "profile_progress" case and SyncProgress.swift on the Swift side. Wrapped
    defensively: a notification hiccup must never break the actual build."""
    try:
        from service.assistant.hub import hub
        await hub.publish({"type": "profile_progress", **fields})
    except Exception:  # noqa: BLE001
        pass


def _prior_section(prior: str, section: str) -> str:
    """The bullets under `## <section>` in an existing profile, if present."""
    if not prior:
        return ""
    out, capturing = [], False
    for line in prior.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip() == section
            continue
        if capturing and line.strip():
            out.append(line)
    return "\n".join(out)


async def _reduce_all(client: OMLXClient, model: str, facts: list[str], prior: str,
                      on_step=None) -> str:
    """Merge every extracted fact into the sectioned profile.

    First shrinks the fact list to something one call can see in full
    (hierarchical de-duplication rounds, only when needed), then writes each
    section with its own dedicated pass — see _SECTION_GUIDANCE on why
    per-section beats one all-sections call.
    """
    # Deterministic pass FIRST: the map phase extracts each batch independently,
    # so the same recurring fact ("receives daily X") is re-found by every batch
    # it appears in. Collapsing those here means the expensive model rounds below
    # spend their budget on distinct facts instead of re-reading copies.
    blob = _dedup_lines("\n".join(f for f in facts if f))
    if not blob.strip():
        return prior

    while len(blob) > _REDUCE_CHARS:
        groups = _batches(blob, _REDUCE_CHARS)
        merged = []
        for g in groups:
            merged.append(await _reduce(client, model, g))
            if on_step:
                await on_step()
        # Re-dedup between rounds: each group is reduced in isolation, so two
        # groups can independently emit the same surviving fact.
        new_blob = _dedup_lines("\n".join(merged))
        # Guard against a pathological non-shrinking round (a model echoing its
        # input back) rather than looping forever.
        if len(new_blob) >= len(blob):
            blob = new_blob[:_REDUCE_CHARS]
            break
        blob = new_blob

    parts = []
    for section in SECTIONS:
        bullets = _dedup_lines(await _reduce_section(
            client, model, section, blob, _prior_section(prior, section)))
        parts.append(f"## {section}\n{bullets or '- (nothing found yet)'}")
        if on_step:
            await on_step()
    return "\n\n".join(parts)


async def build_profile(sources: list[str] | None = None) -> dict:
    """Scan every source Wisp already has synced and rebuild the profile.

    Two phases (see _map_extract / _reduce): facts are extracted from each
    batch INDEPENDENTLY, then merged once at the end. The earlier design
    folded every batch into a single carried-forward profile, which let later
    batches evict what earlier ones found — with a year of data that produced
    a profile describing little beyond the most recently processed slice.

    `sources`, if given, restricts the run to those source names (e.g.
    `["messages"]`) — everything else in the existing profile is left
    untouched (each section's reduce pass carries the prior version forward,
    see _prior_section). Used by scheduler._maybe_daily_profile to rotate one
    source per night instead of a single 20+-minute all-sources pass — see
    that function for why. The manual "Build My Profile" button and the
    `build_profile` agent tool both call this with no argument (full build),
    and that's unchanged.

    Returns {"ok": True, "sources": {name: {batches, chars}}, "missing": [...],
    "updated_at": ts}, or {"ok": False, "reason": str} if nothing is synced.
    Runs entirely on the local model — nothing leaves the machine. Safe to
    re-run: the previous profile is carried into the final merge, so re-runs
    refine rather than duplicate.
    """
    global _build_running
    if _build_running:
        return {"ok": False, "reason": (
            "a profile build is already running — it takes 20+ minutes and holds "
            "the model exclusively, so this request was skipped rather than run "
            "a second one alongside it")}
    # CLAIM THE SLOT IMMEDIATELY — before the first `await`, and never after it.
    #
    # This assignment used to sit ~30 lines below the check, with
    # `await client.ensure_only(model, exclusive=True)` in between. That await
    # unloads the resident models and loads gpt-oss (11.81GB), which is tens of
    # seconds; asyncio yields to other requests for all of it, so a second
    # caller arriving in that window read `_build_running` as False and started
    # its own build. The guard was not atomic, and the check-to-set gap was the
    # single longest operation in the function.
    #
    # Observed, not theoretical: on 2026-08-05 two builds ran concurrently and
    # the oMLX log shows every extraction arriving twice with identical prompts
    # (6825, 6735, 6723, 6712, 6641 — each ~0.1s apart). Two gpt-oss builds
    # sharing this machine's memory budget is exactly the case the module
    # docstring and _REDUCE_CHARS warn ends in a Metal failure, and the engine
    # did go down mid-build. Nothing below this point may await before the flag
    # is set.
    _build_running = True

    # Everything after the claim runs inside try/finally so the flag is released
    # on EVERY exit path — including the early "nothing synced" return below,
    # which previously ran before the flag existed and now would strand it.
    done = 0
    total = 0
    profile = ""
    now = time.time()
    counts: dict[str, dict] = {}
    missing: list[str] = []
    try:
        sources, missing = _gather_sources(only=sources)
        if not sources:
            return {"ok": False, "reason": (
                "nothing synced yet — Mail/Messages/Notes/Calendar sync "
                "automatically once their permissions are granted in System "
                "Settings ▸ Privacy & Security; try again in a moment or after "
                "granting access")}

        client = _c()
        # Map and reduce deliberately use DIFFERENT models — see models.yaml's
        # profile_map role. Map is 70+ mechanical extraction calls (most of a
        # build's wall-clock time); reduce is ~7 genuinely reasoning-shaped
        # merge calls. Running map on gemma (~6.3GB) instead of gpt-oss
        # (~11.8GB) is what actually shortens the sustained heavy-compute
        # stretch — the previous single-model version held gpt-oss exclusively
        # for the WHOLE build. One model swap happens between the two phases
        # (skipped if map_model == reduce_model, e.g. if profile_map is ever
        # reassigned to gpt-oss); ensure_only no-ops if the right model is
        # already resident, same as every other call site.
        map_model = role_to_model("profile_map")
        reduce_model = role_to_model("profile")  # pinned to gpt-oss — see models.yaml
        # Exclusive, like agent/loop.py's gpt-oss turns and Super Model: this
        # can run dozens of extraction calls back-to-back, so it gets the full
        # memory budget rather than trying to co-reside with another resident
        # model — which also matches this hardware's tight memory ceiling (see
        # the MOE coding-models notes on 24GB OOMs). Whatever was resident
        # before simply reloads on its own next time something needs it (e.g.
        # the next summarize_emails call) — no separate restore step required.
        await client.ensure_only(map_model, exclusive=True)

        batched: list[tuple[str, list[str]]] = []
        for source, text in sources:
            batches = _sample_batches(_batches(text), _MAX_BATCHES_PER_SOURCE)
            counts[source] = {"batches": len(batches), "chars": len(text)}
            batched.append((source, batches))
        # + one pass per section for the merge phase, so the bar reflects the real
        # remaining work instead of sitting at 100% through the whole merge.
        total = sum(len(b) for _s, b in batched) + len(SECTIONS)

        await _publish_progress(active=True, done=0, total=total, source="")

        facts: list[str] = []
        for source, batches in batched:
            for batch in batches:
                facts.append(await _map_extract(client, map_model, source, batch))
                done += 1
                await _publish_progress(active=True, done=done, total=total, source=source)

        async def _step() -> None:
            nonlocal done, total
            done += 1
            total = max(total, done + 1)   # hierarchical rounds can add passes
            await _publish_progress(active=True, done=done, total=total, source="merging")

        await _publish_progress(active=True, done=done, total=total, source="merging")
        await client.ensure_only(reduce_model, exclusive=True)
        profile = _redact(await _reduce_all(client, reduce_model, facts,
                                            get_profile_text(), on_step=_step))
        # Deterministic floor, applied AFTER redaction: entity rows are joined
        # from Contacts + message counts, not from anything the model wrote, so
        # there's nothing here for _redact's secret patterns to have caught or
        # missed either way.
        profile = _augment_people_section(profile)
        now = time.time()

        if profile:
            PROFILE_MD.parent.mkdir(parents=True, exist_ok=True)
            PROFILE_MD.write_text(profile.strip() + "\n")

            meta = _load_meta()
            for source, _text in sources:
                meta[source] = {"last_scanned": now, "chars": counts[source]["chars"]}
            # Only REFRESH the missing-status of sources actually checked this
            # run (see _gather_sources' `only`) — a per-source scheduled run
            # that only checked "messages" must not overwrite what's known
            # about email/notes/calendar's availability from a previous run.
            checked = {s for s, _ in sources} | set(missing)
            prior_missing = [m for m in (meta.get("missing") or []) if m not in checked]
            meta["missing"] = prior_missing + missing
            meta["updated_at"] = now
            _save_meta(meta)
    finally:
        # Clear the single-flight flag here, not after the return — an exception
        # partway through (oMLX down, a Metal load failure) must not leave the
        # build permanently locked out until the backend restarts.
        _build_running = False
        # Always clears the "in progress" state client-side, even if a call
        # raised partway through — otherwise a failed run leaves the progress
        # bar stuck showing "in progress" forever.
        await _publish_progress(active=False, done=done, total=total, source="")

    if not profile:
        return {"ok": False, "reason": "the model returned an empty profile — try again"}
    return {"ok": True, "sources": counts, "missing": missing, "updated_at": now}
