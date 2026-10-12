#!/usr/bin/env python3
"""Validate the Aria demo dataset (stdlib only). Usage: python3 validate.py [data_dir]"""
import json, os, re, sys
from collections import Counter
from datetime import datetime, timedelta, timezone

HERE = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
CUTOFF = datetime.fromisoformat("2026-10-11T23:59:00-07:00")
EARLIEST = datetime.fromisoformat("2026-08-10T00:00:00-07:00")
BASE_CONTACTS = {"c%02d" % i for i in range(1, 15)}   # founder-demo.json c01..c14
PHONE = re.compile(r"^\+1-(\d{3}-)?555-01\d\d$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
errors, notes = [], []

def err(m): errors.append(m)

def load(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        err("%s: invalid JSON or unreadable (%s)" % (name, e)); return None

def reserved(domain):
    d = domain.lower()
    return d in ("example.com", "example.org") or d.endswith(".example")

def check_email(addr, where):
    m = EMAIL.fullmatch(addr or "")
    if not m or not reserved(m.group(1)):
        err("%s: email not on reserved domain: %r" % (where, addr))

def parse(ts, where):
    try:
        d = datetime.fromisoformat(ts)
    except Exception:
        err("%s: unparseable date %r" % (where, ts)); return None
    if d.tzinfo is None or d.utcoffset() != timedelta(hours=-7):
        err("%s: date lacks -07:00 offset: %r" % (where, ts))
    if d > CUTOFF:
        err("%s: date after cutoff: %r" % (where, ts))
    if d < EARLIEST:
        err("%s: date before window: %r" % (where, ts))
    return d

def uniq(ids, label):
    dup = [k for k, v in Counter(ids).items() if v > 1]
    if dup: err("%s: duplicate ids %s" % (label, dup[:5]))

def scan_text(text, where):
    for m in EMAIL.finditer(text or ""):
        if not reserved(m.group(1)): err("%s: non-reserved email in text: %s" % (where, m.group(0)))
    if re.search(r"lorem|ipsum", text or "", re.I): err("%s: placeholder text" % where)

extra = load("contacts_extra.json"); mail = load("mail.json")
msgs = load("messages.json"); rep = load("reply_templates.json")
if not all([extra, mail, msgs, rep]): 
    print("FAIL: could not load all files"); sys.exit(1)

# ---- contacts
contacts = extra["contacts"]
uniq([c["id"] for c in contacts], "contacts_extra")
known = set(BASE_CONTACTS)
for c in contacts:
    if c["id"] in BASE_CONTACTS: err("contacts_extra: id collides with base contact %s" % c["id"])
    known.add(c["id"])
    check_email(c["email"], "contact " + c["id"])
    if not PHONE.match(c["phone"]): err("contact %s: phone not 555-01xx: %s" % (c["id"], c["phone"]))
    scan_text(c.get("context", ""), "contact " + c["id"])
# optional cross-check against the real fixture file
fixture = os.path.join(HERE, "..", "wisp-behavior-qa", "service", "demo", "founder-demo.json")
if os.path.exists(fixture):
    fx = json.load(open(fixture))
    fx_ids = {c["id"] for c in fx["contacts"]}
    if fx_ids != BASE_CONTACTS: err("founder-demo.json contact ids differ from c01..c14")
    for c in fx["contacts"]:
        check_email(c["email"], "founder-demo " + c["id"])
        if not PHONE.match(c["phone"]): err("founder-demo %s: phone %s" % (c["id"], c["phone"]))
    notes.append("founder-demo.json cross-check: ok (14 contacts)")
else:
    notes.append("founder-demo.json not found beside data dir; used built-in c01..c14 id list")

# ---- mail
M = mail["messages"]; T = mail["threads"]
uniq([m["id"] for m in M], "mail messages"); uniq([t["id"] for t in T], "mail threads")
tids = {t["id"] for t in T}
latest = None
for m in M:
    w = "mail " + m["id"]
    d = parse(m["date"], w)
    if d and (latest is None or d > latest): latest = d
    if m["thread_id"] not in tids: err(w + ": unknown thread")
    if m["mailbox"] not in ("inbox", "sent", "archive"): err(w + ": bad mailbox")
    for p in [m["from"]] + m["to"] + m["cc"]:
        check_email(p["email"], w)
        if p.get("contact_id") and p["contact_id"] not in known: err("%s: unknown contact %s" % (w, p["contact_id"]))
    if not m["to"]: err(w + ": no recipients")
    scan_text(m["body"], w); scan_text(m["subject"], w)
    if m["has_attachment"] != bool(m["attachments"]): err(w + ": has_attachment mismatch")
    if m["unread"] and m["mailbox"] == "sent": err(w + ": sent message marked unread")
    for k in ("unread", "flagged", "has_attachment"):
        if not isinstance(m[k], bool): err("%s: %s not bool" % (w, k))
for t in T:
    ids = t["message_ids"]
    if not 1 <= len(ids) <= 6: err("thread %s: %d messages (want 1-6)" % (t["id"], len(ids)))
mid = {m["id"] for m in M}
for t in T:
    for i in t["message_ids"]:
        if i not in mid: err("thread %s: unknown message %s" % (t["id"], i))
today_unread = [m for m in M if m["unread"] and m["date"].startswith("2026-10-11")]
if not 3 <= len(today_unread) <= 8: err("expected 3-8 unread mails from 2026-10-11, got %d" % len(today_unread))
box = Counter(m["mailbox"] for m in M)
if not 40 <= len(T) <= 55: err("thread count %d outside 40-55" % len(T))

# ---- messages
IM = msgs["messages"]; CH = {c["id"]: c for c in msgs["chats"]}
uniq([m["id"] for m in IM], "messages"); uniq(list(CH), "chats")
for m in IM:
    w = "msg " + m["id"]
    d = parse(m["date"], w)
    if m["chat_id"] not in CH: err(w + ": unknown chat")
    else:
        if m["chat_name"] != CH[m["chat_id"]]["name"]: err(w + ": chat_name mismatch")
        if m["participants"] != CH[m["chat_id"]]["participants"]: err(w + ": participants mismatch")
    for p in m["participants"]:
        if p != "me" and p not in known: err("%s: unknown participant %s" % (w, p))
    if m["sender"] != "me" and m["sender"] not in known: err("%s: unknown sender %s" % (w, m["sender"]))
    if m["sender"] != "me" and m["sender"] not in m["participants"]: err(w + ": sender not in participants")
    if m["is_from_me"] != (m["sender"] == "me"): err(w + ": is_from_me mismatch")
    if m["is_from_me"] and not m["read"]: err(w + ": outgoing marked unread")
    scan_text(m["text"], w)
for c in msgs["chats"]:
    n = sum(1 for m in IM if m["chat_id"] == c["id"])
    if n != c["message_count"]: err("chat %s: message_count %d != %d" % (c["id"], c["message_count"], n))
if not 230 <= len(IM) <= 300: err("message count %d outside 230-300" % len(IM))

# rhythm sanity (informational + hard floors)
def when(m): return datetime.fromisoformat(m["date"])
dev = [m for m in IM if m["chat_id"] == "chat-dev"]
wk = [m for m in dev if when(m).weekday() < 5]
aft = [m for m in wk if 14 <= when(m).hour < 17 or (when(m).hour == 17 and when(m).minute < 30)]
if len(aft) / max(1, len(wk)) < 0.9: err("Dev weekday messages mostly outside afternoons")
mom = [m for m in IM if m["chat_id"] == "chat-mom" and when(m).weekday() == 6]
mom_eve = [m for m in mom if when(m).hour >= 17]
if len(mom_eve) < 15: err("Mom Sunday-evening coverage too thin (%d)" % len(mom_eve))
notes.append("Dev: %d msgs (%d weekday, %d in 2-5:30pm); Mom Sunday evening msgs: %d" % (len(dev), len(wk), len(aft), len(mom_eve)))

# ---- reply templates
ids = []
main = ["c02", "c03", "c04", "c05", "c08", "c15"]
for cid, items in rep["by_contact"].items():
    if cid not in known: err("reply_templates: unknown contact %s" % cid)
    if cid in main and not 3 <= len(items) <= 6: err("reply_templates: %s has %d (want 3-6)" % (cid, len(items)))
    for t in items:
        ids.append(t["id"]); scan_text(t["text"], "template " + t["id"])
        if t["channel"] not in ("message", "mail", "any"): err("template %s: bad channel" % t["id"])
for t in rep["generic"]:
    ids.append(t["id"]); scan_text(t["text"], "template " + t["id"])
uniq(ids, "reply_templates")
for cid in main:
    if cid not in rep["by_contact"]: err("reply_templates: missing main contact %s" % cid)
if len(rep["generic"]) < 3: err("too few generic fallbacks")

# ---- report
print("Aria demo data validation")
print("  contacts_extra: %d contacts (%s)" % (len(contacts), ", ".join(c["id"] for c in contacts)))
print("  mail: %d threads, %d messages (inbox %d, sent %d, archive %d), %d unread (%d from 2026-10-11), %d flagged, %d with attachments"
      % (len(T), len(M), box["inbox"], box["sent"], box["archive"], sum(m["unread"] for m in M),
         len(today_unread), sum(m["flagged"] for m in M), sum(m["has_attachment"] for m in M)))
print("  messages: %d messages in %d chats %s; %d unread incoming"
      % (len(IM), len(CH), {c["id"]: c["message_count"] for c in msgs["chats"]}, sum(not m["read"] for m in IM)))
print("  reply_templates: %d contacts, %d contact templates, %d generic"
      % (len(rep["by_contact"]), sum(len(v) for v in rep["by_contact"].values()), len(rep["generic"])))
print("  latest mail %s; latest message %s" % (latest.isoformat(), max(when(m) for m in IM).isoformat()))
for n in notes: print("  note:", n)
if errors:
    print("FAIL: %d problem(s)" % len(errors))
    for e in errors[:50]: print("  -", e)
    sys.exit(1)
print("PASS: all checks ok")
