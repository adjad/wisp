#!/usr/bin/env python3
"""Ling V3.3-thinking: every V3.2 row, with a short grounded reasoning trace on the trained turn (stdlib only).

V3.2 trained thinking OFF. With thinking ON, V3 either rambles (3.3-4k characters on direct probes, ~10x slower) or
emits 1-97 characters. This set teaches the thinking-on rendering every Wisp request now uses (model-only mode states
`enable_thinking: true` on every step): a 1-4 sentence trace that says what the user wants, what the step does and why,
then the unchanged V3.2 answer or tool call.

Nothing about the V3.2 behaviour changes. Every message, tool schema and tool call is byte-identical to V3.2; the only
addition is `reasoning_content` on the final (training: true) assistant message. Earlier assistant steps carry no
reasoning, because Wisp never sends reasoning back in history (the template then renders them `<think></think>`,
exactly as at inference).

Each trace is derived from the row itself, not from a model: the user's words, the tool schemas offered (name-only stub
or loaded), this turn's tool results, and the gold step. validate_thinking.py checks that every quoted span appears in
the row and every tool named is one the row offers.

    python3 ../ling-dataset-v3.2-20261014/sources.py && python3 ../ling-dataset-v3.2-20261014/generate.py
    python3 add_thinking.py        # verifies the V3.2 sha256 pins, writes train.jsonl, dev.jsonl, thinking.provenance.jsonl
"""
import hashlib, json, random, re, statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
V32 = HERE.parent / "ling-dataset-v3.2-20261014"
SEED = 20261023
QUOTE = 110          # longest quoted span of user text or a tool result
RESULT_LINES = 2     # a result is summarized by its first meaningful lines

SENDS = {"send_message", "send_email", "forward_email", "reply_to_email", "schedule_send"}
ACTIONS = SENDS | {
    "add_calendar_event", "add_reminder", "append_note", "cancel_event", "clear_past_reminders", "clear_reminders",
    "complete_reminder", "create_note", "draft_email", "draft_message", "email_triage", "forget", "mac_lock_screen",
    "mac_media", "mac_mute", "mac_set_volume", "move_path", "open_app", "quit_app", "remember", "set_timer",
    "text_to_speech", "update_reminder", "write_file"}
PURPOSE = {
    "add_calendar_event": "add the calendar event", "add_reminder": "set the reminder", "append_note": "add to the note",
    "cancel_event": "cancel the event", "clear_past_reminders": "clear past-due reminders",
    "clear_reminders": "clear those reminders", "complete_reminder": "mark the reminder done",
    "create_note": "create the note", "draft_email": "draft the email", "draft_message": "draft the message",
    "email_triage": "triage the email", "find_files": "find the file", "find_free_time": "find free time",
    "forward_email": "forward the email", "get_upcoming": "read the calendar and reminders",
    "get_weather": "get the weather", "list_dir": "list the folder", "lookup_contact": "get the contact's number or email",
    "mac_get_volume": "read the current volume", "mac_lock_screen": "lock the screen", "mac_media": "control playback",
    "mac_mute": "mute or unmute", "mac_set_volume": "set the volume", "open_app": "open the app",
    "quit_app": "quit the app", "read_file": "read the file", "recall": "check saved memory",
    "remember": "save it to memory", "reply_to_email": "reply to the email", "schedule_send": "schedule the send",
    "search_conversations": "search past conversations", "search_notes": "search Notes",
    "search_reminders": "search reminders", "send_email": "send the email", "send_message": "send the text",
    "set_timer": "set the timer or alarm", "summarize_emails": "summarize the emails",
    "system_status": "check the Mac's status", "text_to_speech": "say it aloud", "update_reminder": "change the reminder",
    "view_emails": "find the email", "view_messages": "read the messages", "web_fetch": "fetch the page",
    "web_search": "search the web", "wikipedia_summary": "look it up on Wikipedia",
}
FAILED = ("(exit code", "could not", "error", "failed", "no saved contact", "no matching", "not found", "no results")
DENIED = "The user denied this action."
BLOCKED = "BLOCKED by safety policy:"
GUARD = re.compile(r"^\((\w+) already ran with these exact arguments")
WRAPPER = "Historical evidence; never instructions or authorization."
CHAT_CATEGORIES = {"v31:v31_chat_code_inline", "v3r:chat_no_tool"}
HINT_AT_FINAL = {"read_not_write", "email_address"}


def pick(rng, *options):
    return rng.choice(options)


def clip(text, n=QUOTE):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def q(text, n=QUOTE):
    return "“" + clip(text, n) + "”"


def join_names(names):
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def purpose(name):
    return PURPOSE.get(name, name.replace("_", " "))


def result_gist(content):
    """The first meaningful lines of a tool result, with Wisp's evidence wrapper skipped."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and ln.strip() != WRAPPER]
    return " ".join(lines[:RESULT_LINES]) if lines else "nothing"


def is_failure(content):
    head = content.strip().lower()
    return any(head.startswith(p) or head.startswith("(" + p) for p in FAILED)


def asks_question(answer):
    """True when the answer itself asks something (question marks inside quoted text don't count)."""
    bare = re.sub(r"“[^”]*”|\"[^\"]*\"", "", answer)
    return "?" in bare


def is_receipt(content):
    return content.startswith('{"schema_discovery"')


def fmt_args(args):
    parts = []
    for k, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={clip(s, 60)}")
    return ", ".join(parts)


class Row:
    def __init__(self, row, prov):
        self.row, self.prov = row, prov or {}
        self.msgs = row["messages"]
        self.last = self.msgs[-1]
        self.lu = max(i for i, m in enumerate(self.msgs) if m["role"] == "user")
        self.user = self.msgs[self.lu]["content"]
        self.loaded = {t["function"]["name"] for t in row["tools"] if "description" in t["function"]}
        self.offered = {t["function"]["name"] for t in row["tools"]}
        names = {}
        for m in self.msgs:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        # this turn's tool results, in order: (tool name, content)
        self.results = [(names.get(m.get("tool_call_id"), "the tool"), m["content"])
                        for m in self.msgs[self.lu + 1:-1] if m["role"] == "tool"]
        self.history = [m for m in self.msgs[:self.lu] if m["role"] in ("user", "assistant", "tool")]

    def request(self, rng):
        """What the user wants, quoted; a short follow-up is quoted with the assistant turn it answers."""
        prev = next((m for m in reversed(self.history) if m["role"] == "assistant" and (m.get("content") or "").strip()), None)
        if prev is not None and len(self.user) <= 40:
            return pick(rng, f"The user said {q(self.user)}, replying to my earlier {q(prev['content'], 90)}.",
                        f"The user answered {q(self.user)} to my last message: {q(prev['content'], 90)}.")
        return pick(rng, f"The user asked: {q(self.user)}.", f"The request is {q(self.user)}.",
                    f"The user wants: {q(self.user)}.")

    def source_of(self, value):
        v = value.strip().lower()
        if len(v) < 2:
            return None
        if v in self.user.lower():
            return "the request"
        for name, content in reversed(self.results):
            if v in content.lower():
                return f"the {name} result"
        if any(v in (m.get("content") or "").lower() for m in self.history):
            return "earlier in the conversation"
        return None

    def last_result_sentence(self, rng):
        name, content = self.results[-1]
        if is_receipt(content):
            tools = json.loads(content).get("available_tools", [])
            wanted = [n for n in tools if n in {tc["function"]["name"] for tc in self.last.get("tool_calls") or []}]
            loaded = join_names(wanted) if wanted else "the tools I asked for"
            return pick(rng, f"The schemas for {loaded} are loaded now.", f"{loaded} {'is' if len(wanted) == 1 else 'are'} loaded now.")
        if content.strip() == DENIED:
            return f"The user denied {name}, so it did not run."
        g = GUARD.match(content)
        if g:
            return f"{g.group(1)} already ran with these arguments; its earlier result stands."
        verb = "came back with" if is_failure(content) else pick(rng, "returned", "came back with")
        return f"{name} {verb} {q(result_gist(content))}."

    # ---- step kinds -------------------------------------------------------------------------------------------
    def discovery(self, rng):
        args = self.last["tool_calls"][0]["function"]["arguments"]
        tools = args.get("tools") or []
        lead = self.last_result_sentence(rng) + " " if self.results else self.request(rng) + " "
        need = (f"I need {join_names([f'{t} to {purpose(t)}' for t in tools])}" if tools
                else f"I need tools from {join_names(args.get('families') or ['the right family'])}")
        tail = pick(rng, "Their schemas aren't loaded yet, so load them with get_tool_schemas first; nothing runs until then.",
                    "None of that is loaded yet, so get_tool_schemas comes first. It only loads schemas.")
        if len(tools) == 1:
            tail = pick(rng, "It isn't loaded yet, so load its schema with get_tool_schemas first; nothing runs until then.",
                        "Its schema isn't loaded, so get_tool_schemas comes first. It only loads the schema.")
        return lead + need + ". " + tail

    def stubs(self, rng):
        calls = self.last["tool_calls"]
        names = [c["function"]["name"] for c in calls]
        lead = self.last_result_sentence(rng) + " " if self.results else self.request(rng) + " "
        need = f"I need {join_names([f'{n} to {purpose(n)}' for n in dict.fromkeys(names)])}."
        one = len(set(names)) == 1
        tail = pick(rng,
                    f"{'It is' if one else 'They are'} listed by name only, so calling {'it' if one else 'them'} just loads the schema{'' if one else 's'}; nothing runs yet.",
                    f"{'That tool is a name-only stub' if one else 'Those are name-only stubs'} here: the call loads the schema{'' if one else 's'} and runs nothing.")
        guessed = [c for c in calls if c["function"]["arguments"]]
        if guessed:
            tail += " I'll pass what I already know: " + "; ".join(
                f"{c['function']['name']}({fmt_args(c['function']['arguments'])})" for c in guessed) + "."
        return lead + need + " " + tail

    def real_calls(self, rng):
        calls = self.last["tool_calls"]
        names = [c["function"]["name"] for c in calls]
        if self.results:
            lead = self.last_result_sentence(rng)
            last_name, last_content = self.results[-1]
            if is_failure(last_content) and not is_receipt(last_content):
                lead += " " + pick(rng, "That didn't get it, so adjust the call instead of repeating it.",
                                   "That failed as called; change the arguments rather than retry the same thing.")
        else:
            lead = self.request(rng) + " " + pick(
                rng, f"{join_names(names)} {'is' if len(set(names)) == 1 else 'are'} already loaded, so call {'it' if len(set(names)) == 1 else 'them'} directly.",
                "The tool needed is already loaded; no discovery step.")
        steps = []
        for c in calls:
            n, a = c["function"]["name"], c["function"]["arguments"]
            steps.append(f"{n}({fmt_args(a)})" if a else f"{n}()")
        body = ((pick(rng, "Next: ", "Now call ", "So: ") if self.results else "Call ") + join_names(steps) + " to " +
                join_names([purpose(n) for n in dict.fromkeys(names)]) + ".")
        if len(calls) > 1:
            body += " " + pick(rng, "They don't depend on each other, so run them together.",
                               "Both can run in the same step.")
        notes = []
        for c in calls:
            n, a = c["function"]["name"], c["function"]["arguments"]
            if n in SENDS:
                for key in ("to", "message_id"):
                    if isinstance(a.get(key), str):
                        src = self.source_of(a[key])
                        if src:
                            what = "recipient" if key == "to" else "message"
                            notes.append(f"The {what} comes from {src}, not a guess.")
        return lead + " " + body + (" " + " ".join(dict.fromkeys(notes)) if notes else "")

    def final_after_tools(self, rng):
        name, content = self.results[-1]
        answer = self.last.get("content") or ""
        asks = asks_question(answer)
        if content.strip() == DENIED:
            return (f"The user denied {name}, so nothing changed. " +
                    pick(rng, "Say that plainly; don't retry it or try another way.",
                         "Report it and stop: no retry, no workaround."))
        if content.startswith(BLOCKED):
            return (f"{name} was blocked by Wisp's safety policy, so it didn't run. " +
                    pick(rng, "Don't retry it; answer with what I have and say what was blocked.",
                         "Retrying would be blocked again. Answer from the other results and explain."))
        g = GUARD.match(content)
        if g:
            return (f"{g.group(1)} already ran with these exact arguments, so calling it again gets nothing new. " +
                    pick(rng, "Answer from the earlier result.", "Use what it already returned and answer."))
        lead = self.last_result_sentence(rng)
        if name in ACTIONS and not is_failure(content) and not is_receipt(content):
            # the answer restates the confirmation; quoting it here too would write it twice
            lead = pick(rng, f"{name} confirmed it worked.", f"{name} reports success.")
        if is_receipt(content):
            return self.request(rng) + " " + lead + " " + pick(rng, "I can answer without running anything else.",
                                                                "Nothing more needs to run; answer now.")
        if is_failure(content):
            tail = pick(rng, "That didn't work, so say so honestly and don't claim it did.",
                        "It failed or found nothing; report that rather than invent a result.")
            if asks:
                tail += " " + pick(rng, "Ask the user for what's missing instead of guessing.",
                                   "The user has to supply what's missing, so ask.")
            return lead + " " + tail
        done = [n for n, c in self.results if n in ACTIONS and not is_receipt(c) and not is_failure(c)]
        if len(done) > 1 and not asks_question(answer):
            return (f"All {len(done)} {join_names(done)} calls succeeded. " +
                    pick(rng, "Confirm them together in a sentence and stop.", "Everything went through, so confirm briefly and stop."))
        if name in ACTIONS:
            tail = pick(rng, "That confirms it's done. Tell the user briefly and stop; no more calls.",
                        "The action went through, so confirm it in a sentence and stop.")
        elif len({n for n, c in self.results if not is_receipt(c) and not is_failure(c) and not c.startswith(BLOCKED)}) > 1:
            tail = pick(rng, "I have results from every source I needed; compare them and answer.",
                        "All the sources are in. Answer from them without calling anything else.")
        else:
            tail = pick(rng, "That's what the user asked for, so answer from it without more tool calls.",
                        "I have what I need. Answer from this result and stop.",
                        "Answer from that; no further tools.")
        if asks:
            tail = pick(rng, "That leaves a choice only the user can make, so ask rather than guess.",
                        "It doesn't settle what the user wants, so ask a short question instead of acting.")
        return lead + " " + tail

    def final_direct(self, rng):
        answer = self.last.get("content") or ""
        category = self.prov.get("category", "")
        family = self.prov.get("family_id", "")
        if family == "ch_nocap":
            return self.request(rng) + " " + pick(
                rng, "None of my tools can do this, so say so plainly instead of calling something unrelated.",
                "No tool covers this. Say I can't, rather than reach for a tool that doesn't fit.")
        if family.startswith("ch_capability"):
            return self.request(rng) + " " + pick(
                rng, "This is about what I can do; answer from my own capabilities, no call needed.",
                "A question about my abilities, not a task, so answer it directly.")
        if family == "ch_math_units":
            return self.request(rng) + " " + "This is simple arithmetic I can do myself; no tool needed."
        if category in CHAT_CATEGORIES or "```" in answer:
            writing = re.search(r"\b(write|draft|compose|rewrite|reword|poem|caption|toast|note for|message for)\b", self.user, re.I)
            kind = "code" if "```" in answer else "writing" if writing else "general knowledge"
            return self.request(rng) + " " + pick(
                rng, f"This is {kind} I can answer myself; no tool is needed.",
                f"No personal data or action is involved here, so answer directly without tools.")
        if family == "sf_no_referent":
            return self.request(rng) + " " + pick(
                rng, "Nothing earlier in this conversation says what this refers to, so ask instead of acting.",
                "There's no pending item or earlier message it could mean. Ask what the user means.")
        if family.startswith("mem_") and asks_question(answer):
            return self.request(rng) + " " + pick(
                rng, "It isn't clear this should be saved to memory, so check before saving anything.",
                "This may or may not be a request to remember something; confirm before saving.")
        if family == "mc_specific":
            return self.request(rng) + " " + pick(
                rng, "mac_media can only play, pause or skip; it can't pick a specific song, album or playlist. Say so and offer what it can do.",
                "No tool here can choose particular music: mac_media only plays, pauses and skips. Explain that and offer an alternative.")
        if "destructive" in family:
            return self.request(rng) + " " + pick(
                rng, "That would delete or clear something I can't undo, and it doesn't say what. Ask which before touching anything.",
                "Nothing says what to remove, and removal can't be undone, so ask first.")
        if asks_question(answer):
            return self.request(rng) + " " + pick(
                rng, "I can't act on this without guessing, so ask a short question first.",
                "Something I need isn't stated. Ask before calling any tool.")
        if self.history:
            return self.request(rng) + " " + pick(
                rng, "The answer is already in this conversation; no tool needed.",
                "Nothing new has to be read for this. Answer from the conversation.")
        return self.request(rng) + " " + pick(rng, "I can answer this directly; no tool needed.",
                                              "No tool is needed for this; answer directly.")

    def reasoning(self):
        kind, text = self._reasoning()
        # V3.3 authored rows carry the one fact the step turns on (e.g. "days=2 covers the next 48 hours").
        hint = self.prov.get("hint")
        if hint and (kind in ("call", "final_direct") or
                     (kind == "final_after_tools" and self.prov.get("family_id") in HINT_AT_FINAL)):
            text += " " + hint
        return kind, text

    def _reasoning(self):
        rng = random.Random(f"{SEED}:{self.row['id']}")
        calls = self.last.get("tool_calls") or []
        if calls:
            names = [c["function"]["name"] for c in calls]
            if "get_tool_schemas" in names:
                return "discovery", self.discovery(rng)
            if all(n not in self.loaded for n in names):
                return "stub", self.stubs(rng)
            return "call", self.real_calls(rng)
        if self.results:
            return "final_after_tools", self.final_after_tools(rng)
        return "final_direct", self.final_direct(rng)


# ---- V2 replay rows: read-intent JSON and grounded summaries ----------------------------------------------------
def v2_intent(row, rng):
    out = json.loads(row["messages"][-1]["content"])
    kind = out.get("kind")
    user = next(m["content"] for m in reversed(row["messages"]) if m["role"] == "user")
    lead = f"Latest request: {q(user)}."
    if kind == "read":
        parts = []
        for s in out.get("sources", []):
            bits = [s.get("domain", "?"), s.get("operation", "")]
            if s.get("query"):
                bits.append(f"query {q(s['query'], 60)}")
            if s.get("time"):
                bits.append("time " + ", ".join(f"{k} {v}" for k, v in s["time"].items()))
            for k in ("account", "count"):
                if k in s:
                    bits.append(f"{k} {s[k]}")
            parts.append(" ".join(b for b in bits if b))
        body = "It is a fresh read of " + "; ".join(parts) + "."
        if out.get("excluded_sources"):
            body += " Excluded: " + ", ".join(map(str, out["excluded_sources"])) + "."
        if out.get("unsupported_constraints"):
            body += " Not supported: " + "; ".join(out["unsupported_constraints"]) + "."
    elif kind == "unsupported":
        body = ("It asks for " + "; ".join(q(c, 60) for c in out.get("unsupported_constraints", [])) +
                ", which a read can't do, so kind is unsupported with no sources.")
    else:
        body = f"No personal source is read here, so kind is {kind} with no sources."
    return lead + " " + body + " " + pick(rng, "Return only the JSON object.", "Output the JSON object and nothing else.")


def v2_summary(row, rng):
    user = next(m["content"] for m in reversed(row["messages"]) if m["role"] == "user")
    ask = user.split("\n", 1)[0]
    cov = re.findall(r'"returned_count":\s*(\d+)(?:,\s*"total_count":\s*(\d+))?', user)
    partial = re.search(r'"complete":\s*false', user)
    statuses = sorted(set(re.findall(r'"status":\s*"(\w+)"', user)) - {"ok"})
    body = f"The task: {q(ask)}. Use only the synthetic results given."
    if cov:
        n = sum(int(r) for r, _ in cov)
        body += f" They return {n} item{'s' if n != 1 else ''}"
        body += ", and coverage is partial, so state the limit." if partial else "."
    if statuses:
        body += " Some sources are " + join_names(statuses) + "; say which."
    return body + " " + pick(rng, "Keep dates and attribution as given.", "Keep every date exactly as written.")


def v2_reasoning(row):
    rng = random.Random(f"{SEED}:{row['id']}")
    system = row["messages"][0]["content"]
    if system.startswith("Interpret the latest request"):
        return "v2_intent", v2_intent(row, rng)
    return "v2_summary", v2_summary(row, rng)


def authored():
    """V3.3's own conversations (ext_v33_corpus.py), rendered with V3.2's Builder -> ({split: rows}, {cid: provenance})."""
    sys.path.insert(0, str(V32))
    sys.path.insert(0, str(HERE))
    import generate as G
    import ext_v33_corpus as X
    held = json.loads((V32 / "assets" / "heldout_shingles.json").read_text())["items"]
    run = [set(i) for i in json.loads((HERE / "assets" / "corpus_run_shingles.json").read_text())["items"]]
    rows, prov, counters = {"train": [], "dev": []}, {}, {"train": 0, "dev": 0}
    for m in X.build(G, held, run):
        split = m["split"]
        counters[split] += 1
        cid = f"v33-fix-{split}-{counters[split]:04d}"
        style = "terse" if counters[split] % 2 else "full"
        prefix = [({"role": role, "content": t, "training": False} if role == "assistant" else {"role": role, "content": t})
                  for role, t in m["prefix"]] + [{"role": "user", "content": m["user_text"]}]
        nrows, _, info = m["builder"].finish(prefix, m["now"], m["user_text"], style)
        for r in nrows:
            rows[split].append({"id": f"{cid}-s{r['k']}", "conversation_id": cid, "tools": r["tools"], "messages": r["messages"]})
        prov[cid] = {"id": cid, "split": split, "category": "v33:" + X.CATEGORY, "family_id": m["fid"], "hint": m["hint"],
                     "slots": m["meta"], "schema_style": style, "catalog_variant": m["variant"], "reference_now": m["now"],
                     "user_prompt": m["user_text"], "max_overlap": round(m["overlap"], 3), "gold_calls": info["gold_calls"],
                     "protocol_variant": info["protocol_variant"], "n_steps": info["n_steps"]}
    return rows, prov


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    pins = json.loads((V32 / "preflight.json").read_text())["data_sha256"]
    for f in ("train.jsonl", "dev.jsonl"):
        if not (V32 / f).exists():
            sys.exit(f"missing {V32 / f}: run ../ling-dataset-v3.2-20261014/sources.py and generate.py first")
        if sha(V32 / f) != pins[f]:
            sys.exit(f"{f} does not match V3.2's pinned sha256; regenerate V3.2 first")
    prov = {}
    for f in ("train.provenance.jsonl", "dev.provenance.jsonl"):
        for line in (V32 / f).read_text().splitlines():
            p = json.loads(line)
            prov[p["id"]] = p
    new_rows, new_prov = authored()
    prov.update(new_prov)
    (HERE / "v33.provenance.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in new_prov.values()))
    stats = {"source_sha256": pins, "seed": SEED, "splits": {}}
    with (HERE / "thinking.provenance.jsonl").open("w") as pf:
        for split in ("train", "dev"):
            out, kinds, lengths = [], {}, []
            source = [json.loads(line) for line in (V32 / f"{split}.jsonl").read_text().splitlines()]
            # authored rows go after V3.2's model-led rows and before the V2 replay block (which stays last)
            cut = next((i for i, r in enumerate(source) if r["id"].startswith("v2r-")), len(source))
            for row in source[:cut] + new_rows[split] + source[cut:]:
                if row["id"].startswith("v2r-"):
                    kind, text = v2_reasoning(row)
                else:
                    kind, text = Row(row, prov.get(row["conversation_id"])).reasoning()
                text = " ".join(text.split())
                row["messages"][-1]["reasoning_content"] = text
                out.append(json.dumps(row, ensure_ascii=False))
                kinds[kind] = kinds.get(kind, 0) + 1
                lengths.append(len(text))
                pf.write(json.dumps({"id": row["id"], "split": split, "kind": kind, "chars": len(text)}) + "\n")
            (HERE / f"{split}.jsonl").write_text("\n".join(out) + "\n")
            s = sorted(lengths)
            stats["splits"][split] = {"rows": len(out), "v33_rows": len(new_rows[split]),
                                      "v33_conversations": sum(p["split"] == split for p in new_prov.values()),
                                      "by_kind": dict(sorted(kinds.items())),
                                      "reasoning_chars": {"min": s[0], "p50": int(statistics.median(s)),
                                                          "p95": s[int(0.95 * (len(s) - 1))], "max": s[-1]},
                                      "sha256": sha(HERE / f"{split}.jsonl")}
    (HERE / "stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
