#!/usr/bin/env python3
"""One-off helper: derive assets/* from the read-only Ling model-led eval artifacts.

generate.py and validate.py need only assets/ (not the eval directory). This script is
provided for provenance; it reads the eval directory and never executes any Wisp tool.
Usage: python3 build_assets.py [EVAL_DIR]
"""
import hashlib, json, re, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "/private/tmp/claude-501/-Users-adijain-Desktop-MOE-Project/fe2cf8d4-3c1b-4cf8-8d0b-a4dbe8db37dc/scratchpad/ling-eval")
OUT = HERE / "assets"
OUT.mkdir(exist_ok=True)

# trigger map: group head prefix -> tools that bring the rule into the scoped system prompt
TRIGGERS = [
    ("- For volume, call", ["get_volume", "set_volume"]),
    ("- For battery charge", ["get_battery_status"]),
    ("- For FILES AND FOLDERS", ["list_dir", "read_file"]),
    ("- For anything about the user's calendar", ["get_upcoming", "get_past_events"]),
    ("- If more than one Mail or Calendar account", ["get_upcoming", "get_past_events", "summarize_emails", "view_emails"]),
    ("- To add something with a time", ["add_calendar_event", "add_reminder"]),
    ("- To correct or reschedule a reminder", ["update_reminder"]),
    ("- To cancel/delete/remove something from the schedule", ["cancel_event"]),
    ("- Reminder deletion has separate bulk scopes", ["clear_reminders", "clear_past_reminders"]),
    ("- MESSAGES MEANS THE MESSAGES APP", ["summarize_messages", "view_messages"]),
    ("- AGGREGATE QUESTIONS", ["get_upcoming", "search_notes", "summarize_messages", "summarize_emails"]),
    ("- You CAN send things", ["send_email", "send_message", "reply_to_email", "draft_email", "draft_message", "schedule_send"]),
    ("- `get_upcoming` already includes reminders", ["get_upcoming"]),
    ("- For an OVERVIEW of the user's email", ["summarize_emails", "view_emails"]),
    ("- Mailbox upkeep", ["archive_email", "mark_email_read"]),
    ("- For anything the user wrote down or saved in Notes", ["search_notes"]),
    ("- MEMORY", ["remember", "recall", "forget"]),
    ("- For 'what do you know about me", ["recall"]),
    ("- For live external facts", ["get_weather", "get_stock_price", "get_sports_scores", "web_search", "web_fetch", "http_request"]),
    ("- To write, fix, or refactor code", ["write_file", "run_shell"]),
    ("- To change something on a remote service", ["http_request"]),
    ("- THINGS YOU CANNOT DO IN YOUR HEAD", ["run_shell", "create_tool"]),
    ("- `create_tool` writes", ["create_tool"]),
    ("- The moment `create_tool` succeeds", ["create_tool"]),
    ("- If `create_tool` reports", ["create_tool"]),
    ("- To REORGANIZE, TIDY", ["move_path"]),
    ("- For music:", ["spotify", "music"]),
]


def groups_of(extra_lines):
    groups = []
    for ln in extra_lines:
        if ln.startswith("- "):
            groups.append([ln])
        elif groups:
            groups[-1].append(ln)
    return ["\n".join(g) for g in groups]


sp = [json.loads(l) for l in open(EVAL / "system_prompts.jsonl")]
base = sp[0]["system"][0].replace("test.user@example.com", "test.user@example.invalid").splitlines()
variants = sorted({x["system"][0] for x in sp}, key=len)
order, seen = [], set()
for v in variants:
    v = v.replace("test.user@example.com", "test.user@example.invalid")
    lines = v.splitlines()
    extra = [l for l in lines if l not in base]
    gs = groups_of(extra)
    prev = None
    for g in gs:
        if g not in seen:
            seen.add(g)
            if prev is None:
                order.insert(0, g)
            else:
                order.insert(order.index(prev) + 1, g)
        prev = g
rules = []
for g in order:
    head = g.splitlines()[0]
    trig = None
    for pref, tools in TRIGGERS:
        if head.startswith(pref):
            trig = tools
            break
    if not trig:
        print("UNMATCHED", head[:110]); trig = []
    rules.append({"text": g, "triggers": trig})
# catalog (union over captured variants)
fam_tools, fam_desc, fam_order = {}, {}, []
for x in sp:
    for l in x["system"][1].splitlines():
        m = re.match(r"^([a-z_]+): (.*?) Tools: (.*)$", l)
        if m:
            if m.group(1) not in fam_order:
                fam_order.append(m.group(1))
            fam_desc[m.group(1)] = m.group(2)
            fam_tools.setdefault(m.group(1), set()).update(m.group(3).split(", "))
catalog = {"family_order": fam_order, "descriptions": fam_desc,
           "tools": {f: sorted(t) for f, t in fam_tools.items()}}
sys1 = sp[0]["system"][1]
head = sys1[:sys1.index("Available capabilities")]
head = head[head.index("MODEL-LED CAPABILITY DISCOVERY:"):]
tail_marker = "Available capabilities (only currently admitted tools are listed):"
txt = (EVAL / "system_prompt.txt").read_text()
ctx_note = txt[txt.index("=== system[2]"):].split("\n", 1)[1].strip()
# host wording for protocol S / SP (replaces the four captured discovery lines)
_OLD = """Before using a real tool, call get_tool_schemas with its family and,
preferably, only the tools you need. Those schemas arrive NEXT step.
A schema request does not read a source or perform an action. You may
request another family later, within two discovery calls and 24 schemas."""
_NEW = """Some tools arrive with full schemas; every other listed tool is offered
by name only. Call any tool by name: if its schema is not loaded yet,
nothing runs, its schema loads, and you call it again NEXT step with full
arguments. Or call get_tool_schemas with a family to load several at once.
Loading does not read a source or perform an action. You may load tools
within two loading steps and 24 schemas."""
assert _OLD in head
head = head.replace(_OLD, _NEW)
parts = {"base_lines": base, "rules": rules, "catalog": catalog,
         "discovery_preamble": head.rstrip("\n"), "capabilities_header": tail_marker, "context_note": ctx_note}
(OUT / "system_parts.json").write_text(json.dumps(parts, indent=1, ensure_ascii=False) + "\n")
shutil.copy(EVAL / "tools.json", OUT / "tools.json")

# held-out shingles (hashed; prompts themselves are NOT stored)
def norm(s):
    return re.findall(r"[a-z0-9']+", s.lower())

def h(tokens):
    return hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:12]

prompts = set()
for f in sorted(EVAL.glob("transcripts*.jsonl")):
    for l in open(f):
        r = json.loads(l)
        for m in r["messages"]:
            if m["role"] == "user":
                prompts.add(m["content"])
for f in sorted(EVAL.glob("results*.jsonl")):
    for l in open(f):
        r = json.loads(l)
        for k in ("prompt", "last_user"):
            if r.get(k):
                prompts.add(r[k])
sh = []
for p in sorted(prompts):
    t = norm(p)
    if not t:
        continue
    entry = {str(n): sorted({h(t[i:i + n]) for i in range(len(t) - n + 1)}) for n in range(1, 7) if len(t) >= n}
    sh.append({"ntok": len(t), "full": h(t), "sh": entry})
(OUT / "heldout_shingles.json").write_text(json.dumps({"n_prompts": len(sh), "items": sh}, separators=(",", ":")) + "\n")
print("rules", len(rules), "prompts", len(sh))
for r in rules:
    print(r["triggers"][:2], r["text"].splitlines()[0][:70])
