#!/usr/bin/env python3
"""Ling V3 dataset generator: Wisp MODEL-LED tool-calling protocol (discovery -> schemas -> calls -> answer).

Deterministic, standard-library only, fixed seed. Reads only ./assets (copied from the Ling model-led eval
artifacts by build_assets.py). Executes no Wisp tool, contacts no network, loads no model.

    python3 generate.py [--output-dir DIR]

Outputs: train.jsonl dev.jsonl (one row per assistant step), train.provenance.jsonl dev.provenance.jsonl
(one record per conversation), conversations.jsonl (one full conversation per line).
"""
import argparse
import datetime as dt
import hashlib
import copy
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
GLOBAL_SEED = 20261012
VERSION = "v3-20261012"

SP = json.loads((ASSETS / "system_parts.json").read_text())
_TOOLS = json.loads((ASSETS / "tools.json").read_text())
REG = {s["function"]["name"]: s for s in _TOOLS["full_registry_schemas"]}
DISC = _TOOLS["discovery_schema_step0"][0]
DISCOVERY = "get_tool_schemas"
WEB_TOOLS = ("web_search", "web_fetch", "http_request")
FAMILY_ORDER = SP["catalog"]["family_order"]
FAMILY_TOOLS_WEB = {f: list(t) for f, t in SP["catalog"]["tools"].items()}
FAMILY_TOOLS_NOWEB = {f: [x for x in t if x not in WEB_TOOLS] for f, t in FAMILY_TOOLS_WEB.items()}
TOOL_FAMILY = {}
for _f, _ts in FAMILY_TOOLS_WEB.items():
    for _t in _ts:
        TOOL_FAMILY[_t] = _f
for _t in REG:
    assert _t in TOOL_FAMILY, _t

# ---------------------------------------------------------------- randomness

def rng_for(*parts):
    h = hashlib.sha256(("|".join(str(p) for p in (GLOBAL_SEED,) + parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def cid_for(r):
    return "call_" + "".join(r.choice("0123456789abcdef") for _ in range(8))

# ---------------------------------------------------------------- time helpers

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def now_line(now):
    h = now.hour % 12 or 12
    ap = "AM" if now.hour < 12 else "PM"
    return (f"The current date and time is {WEEKDAYS[now.weekday()]}, {MONTHS[now.month - 1]} {now.day}, {now.year} "
            f"at {h}:{now.minute:02d} {ap}. Resolve relative dates/times against this.")


def t12(h, m=0):
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}" if m else f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def iso(d, h, m=0):
    return f"{d.isoformat()}T{h:02d}:{m:02d}"


def dlabel(d, now):
    """Human date label used inside tool results."""
    t = now.date()
    if d == t:
        return "Today"
    if d == t + dt.timedelta(days=1):
        return "Tomorrow"
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1][:3]}"


def dshort(d):
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


class Day:
    """A spoken day reference resolved against `now`."""

    def __init__(self, spoken, date, kind, now):
        self.spoken, self.date, self.kind, self.now = spoken, date, kind, now

    @property
    def period(self):
        return {"today": "today", "tomorrow": "tomorrow"}.get(self.kind, self.date.isoformat())

    @property
    def label(self):
        return dlabel(self.date, self.now)

    @property
    def long(self):
        return dshort(self.date)


def next_weekday(now, wd, allow_today=False):
    d = now.date()
    delta = (wd - d.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return d + dt.timedelta(days=delta)


def pick_day(r, now, kinds=("today", "tomorrow", "weekday"), exclude_today_wd=True):
    kind = r.choice(kinds)
    d0 = now.date()
    if kind == "today":
        return Day("today", d0, "today", now)
    if kind == "tomorrow":
        return Day(r.choice(["tomorrow", "tomorrow", "tmrw", "tomorrow"]), d0 + dt.timedelta(days=1), "tomorrow", now)
    if kind == "weekday":
        for _ in range(40):
            wd = r.randrange(7)
            if wd == now.weekday():
                continue
            d = next_weekday(now, wd)
            if (d - d0).days >= 2:
                break
        name = WEEKDAYS[d.weekday()]
        sp = r.choice([name, name.lower(), name[:3] if r.random() < 0.15 else name,
                       f"this {name}" if (d - d0).days <= 5 else f"next {name}"])
        return Day(sp, d, "date", now)
    raise ValueError(kind)


class Tm:
    def __init__(self, h, m, spoken):
        self.h, self.m, self.spoken = h, m, spoken

    @property
    def long(self):
        return t12(self.h, self.m)


def pick_time(r, lo=8, hi=20, minutes=(0, 0, 0, 30, 15, 45)):
    h = r.randint(lo, hi)
    m = r.choice(minutes)
    if h == 12 and m == 0 and r.random() < 0.4:
        return Tm(12, 0, "noon")
    ap = "am" if h < 12 else "pm"
    hh = h % 12 or 12
    base = f"{hh}:{m:02d}" if m else f"{hh}"
    forms = [f"{base}{ap}", f"{base} {ap}", f"{base}{ap}"]
    if r.random() < 0.12:
        forms.append(f"{h:02d}:{m:02d}")
    return Tm(h, m, r.choice(forms))


# ---------------------------------------------------------------- noise (typos / casing)

_ABBR = [("tomorrow", "tmrw"), ("tomorrow", "tmw"), ("please", "pls"), ("you", "u"), ("message", "msg"),
         ("appointment", "appt"), ("because", "bc"), ("thanks", "thx"), ("reminder", "reminder"), ("tonight", "tnight")]


NOISE = {"clean": None, "noisy": None}


def messy(r, text, protect=()):
    """Deterministic surface noise for ~38% of prompts. Quoted spans and protected words stay intact."""
    out = _messy(r, text, protect)
    NOISE["clean"], NOISE["noisy"] = text, out
    return out


def _messy(r, text, protect=()):
    p = r.random()
    if p < 0.62:
        return text
    quotes = re.findall(r'"[^"]*"|“[^”]*”', text)
    holder = {}
    for i, q in enumerate(quotes):
        key = f"\x00{i}\x00"
        holder[key] = q
        text = text.replace(q, key, 1)
    prot = {}
    for i, w in enumerate(protect):
        key = f"\x01{i}\x01"
        prot[key] = w
        text = text.replace(w, key)
    mode = r.choice(["lower", "lower_nopunct", "typo", "abbr", "lower_typo", "terse_punct"])
    if mode in ("lower", "lower_nopunct", "lower_typo"):
        text = text.lower()
    if mode in ("lower_nopunct", "terse_punct"):
        text = re.sub(r"[.!?]+$", "", text)
        if mode == "lower_nopunct":
            text = text.replace(",", "")
    if mode in ("typo", "lower_typo"):
        words = text.split(" ")
        idx = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha() and "\x00" not in w and "\x01" not in w]
        if idx:
            i = r.choice(idx)
            w = words[i]
            j = r.randrange(1, len(w) - 2)
            kind = r.choice(["swap", "drop", "dup"])
            if kind == "swap":
                w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
            elif kind == "drop":
                w = w[:j] + w[j + 1:]
            else:
                w = w[:j] + w[j] + w[j:]
            words[i] = w
            text = " ".join(words)
    if mode == "abbr":
        abbr = list(_ABBR)
        r.shuffle(abbr)
        n = 0
        for a, b in abbr:
            if re.search(r"\b" + a + r"\b", text, re.I) and n < 2:
                text = re.sub(r"\b" + a + r"\b", b, text, count=1, flags=re.I)
                n += 1
        text = text.lower() if r.random() < 0.5 else text
    for k, w in prot.items():
        text = text.replace(k, w)
    for k, q in holder.items():
        text = text.replace(k, q)
    return text


# ---------------------------------------------------------------- system prompt assembly

def _sys0(loaded):
    base = SP["base_lines"]
    lines = list(base[:10])
    for rule in SP["rules"]:
        if set(rule["triggers"]) & set(loaded):
            lines.extend(rule["text"].split("\n"))
    lines.extend(base[10:])
    return "\n".join(lines)


def _sys1(now, web):
    fam = FAMILY_TOOLS_WEB if web else FAMILY_TOOLS_NOWEB
    lines = ["", now_line(now), SP["discovery_preamble"], SP["capabilities_header"]]
    for f in FAMILY_ORDER:
        if fam.get(f):
            lines.append(f"{f}: {SP['catalog']['descriptions'][f]} Tools: {', '.join(fam[f])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- conversation recorder

SP_P = 0.30     # share of tool-using conversations that start with preloaded real schemas
DECOY_FAMILIES = {"messages_contacts": ["notes", "mail"], "mail": ["messages_contacts", "notes"], "calendar": ["notes", "mail"],
                  "notes": ["calendar", "files"], "public_web": ["files", "device"], "files": ["notes", "public_web"],
                  "device": ["automation", "apps_media"], "automation": ["device", "calendar"], "compute": ["public_web", "device"],
                  "memory": ["notes", "calendar"], "apps_media": ["device", "automation"], "wisp_activity": ["calendar", "mail"]}
STUB_P = 0.72   # tuned so ~70% of tool-using conversations open with a stub call (see DATA_CARD)


MESSY_RESULT_LIMIT = 1200   # messy-result families only; everything else stays under 400
# Verbatim from Wisp's repeated-read guard (service/agent/loop.py on main after #195).
GUARD_TEXT = ("({name} already ran with these exact arguments this turn and its result was returned earlier. It was "
              "NOT run again. Answer from that result, or call it with different arguments if you need different data.)")
# Add-on family modules (one owner each). Each defines register(G) and registers families with G.family(...).
EXTENSIONS = ("ext_chains", "ext_followups", "ext_chat", "ext_messy", "ext_guard")


def load_extensions(names=EXTENSIONS):
    import importlib
    sys.path.insert(0, str(HERE))
    me = sys.modules[__name__]
    for name in names:
        if not name:
            continue
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:
                continue
            raise
        mod.register(me)


def stub_schema(name):
    """Round-0 name-only stub: no description, no properties."""
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def catalog_tool_names(web):
    fam = FAMILY_TOOLS_WEB if web else FAMILY_TOOLS_NOWEB
    return sorted({t for f in FAMILY_ORDER for t in fam.get(f, [])})


def canon(args):
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


class Conv:
    """Records one model-led conversation and emits one training row per assistant step."""

    def __init__(self, cid, now, rng, web=False):
        self.cid, self.now, self.r, self.web = cid, now, rng, web
        self.history = []           # prior turns: [(role, text)] (assistant -> training False)
        self.turn = []              # current-turn messages after the user message
        self.user_text = None
        self.user_clean = None
        self.loaded = []            # ordered tool names
        self.attempts = 0
        self.steps = []             # (index into self.turn of assistant msg, offered tool names, loaded snapshot)
        self.gold = []              # [{"name","arguments"}]
        self.seen = set()
        self.facts = {}
        self.short_receipts = rng.random() < 0.35
        self.force_explicit = False
        self.stub_calls = {}        # tool -> (tool_call dict, gold entry) for stub calls whose args may be patched
        self.modes = []             # per discovery event: "stub" | "explicit" (rejected ones included)
        self.catalog_tools = catalog_tool_names(web)
        self.preload = []           # SP: real schemas preloaded at round 0 (do not consume a discovery attempt)
        self.sp_kind = None         # None (pure S) | "hit" | "decoy"
        self.sp_need = []
        self.result_limit = 400     # tool-result length cap; messy-result families raise it (max MESSY_RESULT_LIMIT)

    # -- setup
    def hist(self, role, text):
        self.history.append((role, text))
        return self

    def user(self, text):
        self.user_text = text
        self.user_clean = NOISE["clean"] if NOISE["noisy"] == text else text
        return self

    def _offered(self):
        """Protocol S: loaded REAL schemas, then (while discovery budget remains) get_tool_schemas + a name-only stub
        for every other admitted catalog tool."""
        names = list(self.loaded)
        if self.attempts < 2:
            names.append(DISCOVERY)
            names.extend(t for t in self.catalog_tools if t not in self.loaded)
        return names

    def _assistant(self, calls, content=""):
        msg = {"role": "assistant", "content": content, "training": False}
        if calls:
            msg["tool_calls"] = [{"id": cid_for(self.r), "type": "function",
                                  "function": {"name": n, "arguments": copy.deepcopy(a)}}
                                 for n, a in calls]
        self.turn.append(msg)
        self.steps.append((len(self.turn) - 1, self._offered(), list(self.loaded), True))
        return msg

    def _tool_result(self, call_id, text):
        assert self.result_limit <= MESSY_RESULT_LIMIT
        assert len(text) < self.result_limit, (len(text), text)
        self.turn.append({"role": "tool", "tool_call_id": call_id, "content": text})

    # -- steps
    def discover(self, families, tools=None, mode=None):
        """Load schemas. mode 'stub' = call the needed tool names via their stubs (implicit discovery, several at once
        for a chain); 'explicit' = get_tool_schemas. Default: ~STUB_P stub-first when <=3 new tools are needed."""
        if not self.steps and not self.loaded and self.attempts == 0 and tools and not self.force_explicit and mode is None:
            self._maybe_preload(tools)
        new = [t for t in (tools or []) if t not in self.loaded]
        if tools and not new:
            return self                       # SP hit: everything needed is already loaded, no loading step
        if self.sp_kind == "decoy" and not self.modes and 1 <= len(new) <= 3:
            mode = "stub"
        if mode is None:
            ok = tools is not None and 1 <= len(new) <= 3 and not self.force_explicit
            mode = "stub" if ok and self.r.random() < STUB_P else "explicit"
        if mode == "stub":
            return self._stub_discover(new)
        return self._explicit_discover(families, tools)

    def _maybe_preload(self, tools):
        """SP (Wisp 1.3.2): ~SP_P of tool-using conversations start with 6-12 PRELOADED real schemas. Half contain the
        needed tool(s) (call directly on round 0); half are plausible-but-wrong decoys (ignore them, call the stub)."""
        if len(tools) > 8 or self.r.random() >= SP_P:
            return
        k = self.r.randint(6, 12)
        need = list(dict.fromkeys(tools))
        fams = list(dict.fromkeys(TOOL_FAMILY[t] for t in need))
        pool_fams = list(dict.fromkeys(f for fa in fams for f in DECOY_FAMILIES.get(fa, ["notes", "files"])))
        fam = FAMILY_TOOLS_WEB if self.web else FAMILY_TOOLS_NOWEB
        decoys = [t for f in pool_fams for t in fam.get(f, []) if t not in need]
        if self.r.random() < 0.5:                                   # hit
            extra = self.r.sample(decoys, min(len(decoys), max(0, k - len(need))))
            self.preload = list(dict.fromkeys(need + extra))
            self.sp_kind = "hit"
        else:                                                       # decoy
            self.preload = self.r.sample(decoys, min(len(decoys), k))
            self.sp_kind = "decoy"
        self.preload = [t for t in self.catalog_tools if t in set(self.preload)] if self.sp_kind else []
        assert 6 <= len(self.preload) <= 12 or (self.sp_kind == 'hit' and len(self.preload) <= 12), self.preload
        self.loaded = list(self.preload)
        self.sp_need = need

    def _stub_discover(self, new):
        assert new and self.attempts < 2
        msg = self._assistant([(t, {}) for t in new])
        self.attempts += 1
        self.modes.append("stub")
        fams = list(dict.fromkeys(TOOL_FAMILY[t] for t in new))
        names = list(dict.fromkeys([*self.loaded, *new]))
        assert len(names) <= 24 and len(fams) <= 3
        self.loaded = names
        receipt = json.dumps({"schema_discovery": "ready_next_step", "families": fams, "available_tools": names,
                              "source_tools_executed": False})
        for tc, t in zip(msg["tool_calls"], new):
            g = {"name": t, "arguments": {}, "stub": True}
            self.gold.append(g)
            self.stub_calls[t] = (tc, g)
            self._tool_result(tc["id"], receipt)
        return self

    def _explicit_discover(self, families, tools=None):
        args = {"families": list(families)}
        if tools is not None:
            args["tools"] = list(tools)
        assert self.attempts < 2
        msg = self._assistant([(DISCOVERY, args)])
        self.gold.append({"name": DISCOVERY, "arguments": args})
        cid = msg["tool_calls"][0]["id"]
        self.attempts += 1
        self.modes.append("explicit")
        fam = FAMILY_TOOLS_WEB if self.web else FAMILY_TOOLS_NOWEB
        allowed = {n for f in families for n in fam[f]}
        req = list(tools) if tools is not None else sorted(allowed)
        assert set(req) <= allowed, (families, req)
        names = list(dict.fromkeys([*self.loaded, *req]))
        assert len(names) <= 24
        self.loaded = names
        self._tool_result(cid, json.dumps({"schema_discovery": "ready_next_step", "families": list(families),
                                           "available_tools": names, "source_tools_executed": False}))
        return self

    def reject_then(self, bad_families, bad_tools):
        """Record a discovery the host rejects (tools outside the selected families). Not a training target."""
        args = {"families": list(bad_families), "tools": list(bad_tools)}
        fam = FAMILY_TOOLS_WEB if self.web else FAMILY_TOOLS_NOWEB
        allowed = {n for f in bad_families for n in fam[f]}
        assert not set(bad_tools) <= allowed
        msg = self._assistant([(DISCOVERY, args)])
        self.gold.append({"name": DISCOVERY, "arguments": args, "rejected": True})
        self.modes.append("explicit")
        self.force_explicit = True
        i, o, l, _ = self.steps[-1]
        self.steps[-1] = (i, o, l, False)
        self.attempts += 1
        self._tool_result(msg["tool_calls"][0]["id"], "Schema selection rejected: Requested tools must belong to the "
                          "selected admitted families. No source was read or action performed.")
        return self

    def repeat_read(self, name, args):
        """Context only (never a target): the assistant re-issues a read it already made with identical arguments and
        Wisp's repeated-read guard (service/agent/loop.py, 1.3.2) refuses it. The next step is the target."""
        assert (name, canon(args)) in self.seen, f"repeat_read of a call never made: {name}"
        msg = self._assistant([(name, args)])
        i, o, l, _ = self.steps[-1]
        self.steps[-1] = (i, o, l, False)
        self.gold.append({"name": name, "arguments": args, "guard_rejected": True})
        self._tool_result(msg["tool_calls"][0]["id"], GUARD_TEXT.format(name=name))
        return self

    def call(self, name, args, result):
        return self.calls([(name, args, result)])

    def calls(self, specs):
        assert specs
        for n, a, _ in specs:
            assert n in self.loaded, f"{n} not loaded ({self.loaded})"
            key = (n, canon(a))
            assert key not in self.seen, f"identical repeated call {key}"
            self.seen.add(key)
            self.gold.append({"name": n, "arguments": a})
            if n in self.stub_calls:
                tc, g = self.stub_calls.pop(n)
                if len(a) >= 2 and self.r.random() < 0.35:       # "best guess": a proper, non-empty subset of the real arguments
                    keys = list(a)
                    keep = sorted(self.r.sample(range(len(keys)), self.r.randint(1, len(keys) - 1)))
                    guess = {keys[i]: a[keys[i]] for i in keep}
                    tc["function"]["arguments"] = copy.deepcopy(guess)
                    g["arguments"] = guess
        msg = self._assistant([(n, a) for n, a, _ in specs])
        for tc, (_, _, res) in zip(msg["tool_calls"], specs):
            self._tool_result(tc["id"], res)
        return self

    def final(self, text):
        assert text and text.strip()
        self._assistant([], content=text)
        self.turn[-1]["_final"] = True
        return self

    # -- emission
    def _sys_msgs(self, loaded):
        return [{"role": "system", "content": _sys0(loaded)},
                {"role": "system", "content": _sys1(self.now, self.web)},
                {"role": "system", "content": SP["context_note"]}]

    def _prefix(self):
        out = []
        for role, text in self.history:
            m = {"role": role, "content": text}
            if role == "assistant":
                m["training"] = False
            out.append(m)
        out.append({"role": "user", "content": self.user_text})
        return out

    def rows(self, row_id_base):
        rows = []
        prefix = self._prefix()
        k = -1
        for idx, offered, loaded, trainable in self.steps:
            if not trainable:
                continue
            k += 1
            msgs = self._sys_msgs(loaded) + prefix + [_clean(m) for m in self.turn[:idx]]
            tgt = _clean(self.turn[idx])
            tgt["training"] = True
            # earlier assistant messages in the turn are context only
            for m in msgs:
                if m["role"] == "assistant":
                    m["training"] = False
            rows.append({"id": f"{row_id_base}-s{k}", "conversation_id": self.cid,
                         "tools": [DISC if n == DISCOVERY else (REG[n] if n in loaded else stub_schema(n)) for n in offered],
                         "messages": msgs + [tgt]})
        return rows

    def conversation(self):
        loaded_final = self.loaded
        trainable_idx = {i for i, _, _, ok in self.steps if ok}
        turn = []
        for i, m in enumerate(self.turn):
            m = _clean(m, keep_flag=True)
            if m["role"] == "assistant":
                m["training"] = i in trainable_idx
            turn.append(m)
        msgs = self._sys_msgs(loaded_final) + self._prefix() + turn
        return {"conversation_id": self.cid, "now": now_line(self.now), "messages": msgs,
                "tools_per_round": [{"real_schemas": l, "get_tool_schemas": DISCOVERY in o, "name_only_stubs": sum(1 for n in o if n not in l and n != DISCOVERY)}
                                    for _, o, l, _ in self.steps]}


def _clean(m, keep_flag=False):
    m = {k: v for k, v in m.items() if not k.startswith("_")}
    if m["role"] == "assistant" and not keep_flag:
        m["training"] = False
    if m["role"] == "assistant" and keep_flag:
        m["training"] = m.get("training", False)
    return m


# ---------------------------------------------------------------- shared world (all synthetic)

PEOPLE = [
    "Maya Lindqvist", "Theo Okafor", "Priya Raman", "Jonas Weber", "Camila Reyes", "Dev Malhotra", "Noor Haddad",
    "Elena Petrov", "Marcus Bell", "Hana Sato", "Luca Romano", "Aisha Karimi", "Owen Pritchard", "Zoe Tanaka",
    "Felix Armand", "Ines Duarte", "Rohan Gill", "Tessa Brandt", "Idris Mbeki", "Wren Calloway", "Nadia Sorensen",
    "Caleb Frost", "Mina Park", "Arjun Nair", "Lena Vogel", "Bram Visser", "Sana Qureshi", "Gideon Hart",
    "Odette Marsh", "Kofi Mensah", "Ruth Abernathy", "Paolo Ferri", "Yara Nasser", "Quinn Halloran", "Soren Dahl",
    "Imani Brooks", "Leif Eriksen", "Talia Brenner", "Anil Kapoor", "Greta Lund"]
RELATIVES = ["Mom", "Dad", "Grandma", "Aunt Rosa", "Uncle Dev", "Sis"]


_PHONE_CACHE = {}


def _build_phones(names):
    nums = list(range(101, 199))
    random.Random(GLOBAL_SEED + 7).shuffle(nums)
    for n, x in zip(sorted(names), nums):
        _PHONE_CACHE[n] = f"+1 555 0{x}"


def phone_for(name):
    if name not in _PHONE_CACHE:
        n = int(hashlib.sha256(("ph" + name).encode()).hexdigest()[:8], 16) % 98 + 101
        return f"+1 555 0{n}"
    return _PHONE_CACHE[name]


def email_for(name):
    return name.lower().replace(" ", ".") + "@example.invalid"


def first(name):
    return name.split()[0]


def pick_people(r, k, with_relative=0.0):
    pool = [p for p in PEOPLE]
    r.shuffle(pool)
    out = pool[:k]
    return out


def lookup_result(name, via="phone"):
    ph, em = phone_for(name), email_for(name)
    if via == "phone":
        return f"{name}: {ph} (also on {em})"
    return f"{name}: {em} (also on {ph})"


def ambiguous_lookup(query, names):
    listed = "\n".join(f"- {n}: {phone_for(n)}" for n in names)
    return (f"Several contacts match {query!r} — ask the user which one they mean before sending anything:\n{listed}")


def no_contact(query):
    return f"(No saved contact matches {query!r}. Ask the user for the number or email — do not guess one.)"


def pick(r, seq):
    return seq[r.randrange(len(seq))]


def cap(s):
    return s[:1].upper() + s[1:]


def lc_first(s):
    """Lower-case the first letter of an event title when used mid-sentence."""
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() and not s[0].isdigit() else s


def sent_msg(c, who):
    """Send-tool success receipt: Wisp's own shape, or (per conversation) the shorter 'Sent to ...' form."""
    return f"Sent to {who}." if c.short_receipts else f"Message sent to {who}."


def sent_email(c, addr):
    return f"Sent to {addr}." if c.short_receipts else f"Email sent to {addr}."


# ---------------------------------------------------------------- family registry

FAMILIES = {}


def family(cat, fid, split="train", weight=1, failure=("protocol",), web=False):
    def deco(fn):
        assert fid not in FAMILIES, fid
        FAMILIES[fid] = {"cat": cat, "split": split, "weight": weight, "failure": list(failure), "web": web, "fn": fn}
        return fn
    return deco


# ---------------------------------------------------------------- content banks (synthetic)

CUR = {"split": "train"}   # set by build_one; dev families draw message/email topics train never uses
DEV_TEXT_TOPICS = {"parking", "got_it", "running_early", "lunch", "homework", "safe", "traffic", "dinner_move", "feed_cat"}
DEV_EMAIL_TOPICS = {"agenda", "intro", "ask_ref", "feedback", "pay"}


def split_bank(bank, dev_topics):
    dev = CUR["split"] == "dev"
    out = [b for b in bank if (b["topic"] in dev_topics) == dev]
    assert out
    return out

_WD_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
_WD_ABBR = {n[:3].lower(): n for n in WEEKDAYS}


def fix_caps(s):
    """Capitalize the sentence start and weekday names (no other change)."""
    s = _WD_RE.sub(lambda m: m.group(1).capitalize(), s)
    return s[:1].upper() + s[1:]


def clean_day(d):
    """Day phrase safe to repeat inside a message: no 'tmrw' / 3-letter weekday abbreviations."""
    if d.spoken == "tmrw":
        d.spoken = "tomorrow"
    elif d.spoken.lower() in _WD_ABBR:
        d.spoken = _WD_ABBR[d.spoken.lower()]
    return d


def text_intent(r):
    """-> dict(clause, quote, body, topic). clause is what the user says after 'that'; body is ONLY that content,
    converted to the recipient's point of view (them/their -> you/your), capitalized and punctuated. quote is dictated
    text (sent verbatim)."""
    n = r.choice([5, 10, 10, 15, 20, 25, 30, 45])
    tm = pick_time(r, 9, 21)
    place = r.choice(["the corner cafe", "Luigi's", "the east entrance", "the lobby", "my place", "the park gate",
                      "the train station", "the front desk", "the bakery on Elm", "the parking garage"])
    dayw = clean_day(pick_day(r, NOW_HOLDER["now"], kinds=("tomorrow", "weekday")))
    item = r.choice(["your charger", "the book", "the casserole dish", "your umbrella", "the tickets", "the spare camera", "the board game"])
    C = fix_caps

    def it(topic, clause, body, quote):
        return dict(topic=topic, clause=clause, body=C(body), quote=quote)

    bank = [
        it("late", f"I'm running about {n} minutes late", f"I'm running about {n} minutes late.", f"Running {n} min late, sorry!"),
        it("eta", f"I'll be there by {tm.long}", f"I'll be there by {tm.long}.", f"I'll be there by {tm.long}"),
        it("have_item", f"I have {item} and can drop it off", f"I have {item} and can drop it off.", f"I've got {item}, can drop it by"),
        it("dinner", f"dinner is at {tm.long} at {place}", f"dinner is at {tm.long} at {place}.", f"Dinner's at {tm.long}, {place}"),
        it("cant_make", "I can't make it tonight because something came up", "I can't make it tonight because something came up.",
           "Can't make it tonight, something came up. So sorry!"),
        it("birthday", "happy birthday", "happy birthday!", "Happy birthday!! Hope it's a great one"),
        it("thanks_dinner", "I appreciate them having us over last night", "I appreciate you having us over last night.",
           "Thanks so much for having us over last night!"),
        it("call_me", "I'd love a call when they get a chance", "I'd love a call when you get a chance.", "Call me when you get a chance?"),
        it("landed", "I just landed", "I just landed.", "Just landed!"),
        it("outside", "I'm outside", "I'm outside.", "I'm outside"),
        it("store", "I'm at the store and can pick something up", "I'm at the store and can pick something up.", "At the store, need anything?"),
        it("moved", f"the meeting moved to {tm.long}", f"the meeting moved to {tm.long}.", f"Meeting moved to {tm.long}"),
        it("luck", "I'm wishing them luck with the interview today", "I'm wishing you luck with the interview today.",
           "Good luck with the interview today!"),
        it("pickup", f"I'll be picking up the kids at {tm.long}", f"I'll be picking up the kids at {tm.long}.", f"I'll pick up the kids at {tm.long}"),
        it("push_call", f"we should push our call to {dayw.spoken}", f"we should push our call to {dayw.spoken}.",
           f"Can we push our call to {dayw.spoken}?"),
        it("congrats", "I'm thrilled about their new job", "I'm thrilled about your new job.", "Congrats on the new job!!"),
        it("feel_better", "I'm feeling better and appreciate them checking in", "I'm feeling better and appreciate you checking in.",
           "Feeling better, thanks for checking in"),
        it("jacket", "I left my jacket at their place and want to grab it this weekend",
           "I left my jacket at your place and want to grab it this weekend.", "Left my jacket at yours, can I grab it this weekend?"),
        it("dessert", "I'm bringing dessert tomorrow", "I'm bringing dessert tomorrow.", "Bringing dessert tomorrow!"),
        it("still_on", f"I want to confirm we're still on for {dayw.spoken}", f"I want to confirm we're still on for {dayw.spoken}.",
           f"Are we still on for {dayw.spoken}?"),
        it("tickets", "I got the tickets", "I got the tickets.", "Got the tickets!"),
        it("after_work", "I'll call after work", "I'll call after work.", "Will call after work"),
        it("traffic", "traffic is bad and I'll be a bit behind", "traffic is bad and I'll be a bit behind.", "Stuck in traffic, a bit behind"),
        it("miss_you", "I miss them and we should catch up soon", "I miss you and we should catch up soon.", "Miss you! Let's catch up soon"),
        it("parking", f"I'm parked near {place}", f"I'm parked near {place}.", f"Parked near {place}"),
        it("got_it", "I got their message and will reply properly tonight", "I got your message and will reply properly tonight.",
           "Got your message, will reply properly tonight"),
        it("running_early", f"I'm early and waiting at {place}", f"I'm early and waiting at {place}.", f"I'm early, waiting at {place}"),
        it("lunch", f"I can do lunch {dayw.spoken}", f"I can do lunch {dayw.spoken}.", f"Lunch {dayw.spoken} works for me"),
        it("homework", "the homework is due Friday", "the homework is due Friday.", "Reminder: homework is due Friday"),
        it("safe", "I got home safe", "I got home safe.", "Home safe!"),
        it("dinner_move", f"we need to move dinner to {dayw.spoken}", f"we need to move dinner to {dayw.spoken}.",
           f"Can we move dinner to {dayw.spoken}?"),
        it("feed_cat", "I can feed their cat while they're away", "I can feed your cat while you're away.",
           "Happy to feed the cat while you're away"),
    ]
    return r.choice(split_bank(bank, DEV_TEXT_TOPICS))


EMAIL_FRAME = "Hi {first},\n\n{content}\n\nTest User"


def email_intent(r, to_first):
    """-> dict(topic, subject, clause, content, body). clause follows 'email X ...'; content is ONLY what the user said,
    converted to the recipient's point of view; body = the allowed frame around it."""
    dayw = clean_day(pick_day(r, NOW_HOLDER["now"], kinds=("tomorrow", "weekday")))
    tm = pick_time(r, 9, 17)
    topic = r.choice(["Q4 budget", "kitchen repair", "garden fence", "website redesign", "vendor contract",
                      "team offsite", "book club", "move-out inspection"])
    C = fix_caps

    def it(tp, subject, clause, content):
        return dict(topic=tp, subject=subject, clause=clause, core=C(content))

    bank = [
        it("proposal", "Revised proposal", f"that I'll send the revised proposal by {dayw.spoken}", f"I'll send the revised proposal by {dayw.spoken}."),
        it("confirm_meet", "Meeting confirmation", f"that our meeting on {dayw.spoken} at {tm.long} is confirmed", f"our meeting on {dayw.spoken} at {tm.long} is confirmed."),
        it("thanks_interview", "Thank you", "that I appreciate them taking the time to talk with me yesterday", "I appreciate you taking the time to talk with me yesterday."),
        it("move_call", "Moving our call", f"asking if we can move our call to {dayw.spoken}", f"can we move our call to {dayw.spoken}?"),
        it("ooo", "Out of office next week", "that I'll be out of the office next week", "I'll be out of the office next week."),
        it("invoice", "October invoice", "asking if they can send the October invoice", "can you send the October invoice?"),
        it("rsvp", "RSVP", "to say yes, I'll be at the party on Saturday", "yes, I'll be at the party on Saturday."),
        it("quote", f"Quote request: {topic}", f"asking if they can send a quote for the {topic}", f"can you send a quote for the {topic}?"),
        it("followup", "Following up", "following up on my last message", "following up on my last message."),
        it("decline", "Invitation", "that I can't make it and thank them for the invitation", "I can't make it. Thank you for the invitation."),
        it("agenda", f"Agenda for {topic}", f"asking if they can share the agenda for the {topic}", f"can you share the agenda for the {topic}?"),
        it("intro", "Introduction", "that I'm the new neighbor in 4B", "I'm the new neighbor in 4B."),
        it("late_report", "Report timing", "that the report will be a day late", "the report will be a day late."),
        it("ask_ref", "Reference request", "asking if they would be a reference for my application", "would you be a reference for my application?"),
        it("feedback", f"Feedback on {topic}", f"asking if they can give feedback on the {topic}", f"can you give feedback on the {topic}?"),
        it("pay", "Payment sent", "that I sent the payment today", "I sent the payment today."),
    ]
    b = dict(r.choice(split_bank(bank, DEV_EMAIL_TOPICS)))
    b["body"] = EMAIL_FRAME.format(first=to_first, content=b["core"])
    return b


NOW_HOLDER = {"now": None}


def set_now(now):
    NOW_HOLDER["now"] = now


def name_forms(r, name):
    """How the user refers to a contact: first name (most often), full name, lowercase or relative term."""
    if name in RELATIVES:
        return name, name
    p = r.random()
    if p < 0.62:
        return first(name), first(name)
    if p < 0.82:
        return name, name
    return first(name).lower(), first(name)


def pick_contact(r, relative_p=0.18):
    if r.random() < relative_p:
        rel = r.choice(RELATIVES)
        return rel
    return r.choice(PEOPLE)


# ================================================================ OUTBOUND (sends & drafts)

DECLINED = "The user declined; nothing was sent."


def Q(text):
    return "\u201c" + text + "\u201d"


def _fin_sent(r, kind, first_name, text):
    """Final answer after a successful send: quotes the exact body that was sent."""
    if kind == "message":
        return r.choice([f"Sent to {first_name}: {Q(text)}", f"Texted {first_name}: {Q(text)}", f"Done - your message to {first_name} went out: {Q(text)}"])
    return r.choice([f"Emailed {first_name}. The body was:\n{Q(text)}", f"Sent the email to {first_name}:\n{Q(text)}", f"Done - this went to {first_name}:\n{Q(text)}"])


def _fin_declined(r, first_name, kind="message"):
    thing = "text" if kind == "message" else "email"
    return r.choice([
        f"Okay, I didn't send it — you declined, so nothing went to {first_name}.",
        f"Understood. The {thing} to {first_name} was not sent.",
        f"No problem — I held off. Nothing was sent to {first_name}.",
        f"Got it, you declined the send, so the {thing} to {first_name} wasn't sent."])


def _text_flow(r, c, now, tpls, deny=False, number=False, fam_note=None):
    nm = pick_contact(r)
    said, canon_first = name_forms(r, nm)
    it = text_intent(r)
    mode, tpl = r.choice(tpls)
    phone = phone_for(nm)
    if number:
        said = phone
    if mode == "q":
        body = it["quote"]
        prompt = tpl.format(n=said, q=body)
    else:
        body = it["body"]
        prompt = tpl.format(n=said, c=it["clause"])
    c.user(messy(r, prompt, protect=(said,)))
    if number:
        c.discover(["messages_contacts"], ["send_message"])
        target = phone
        who = phone
    else:
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": canon_first if nm not in RELATIVES else nm}, lookup_result(nm))
        target = phone
        who = nm
    res = DECLINED if deny else sent_msg(c, who)
    c.call("send_message", {"to": target, "text": body}, res)
    fn = who if number else (nm if nm in RELATIVES else first(nm))
    c.final(_fin_declined(r, fn) if deny else _fin_sent(r, "message", fn, body))
    return {"template": tpls.index((mode, tpl)), "slots": {"contact": nm, "topic": it["topic"], "deny": deny}}


TXT_DIRECT = [
    ("c", "text {n} that {c}"), ("c", "can you text {n} that {c}"), ("c", "please text {n} and say {c}"),
    ("c", "text {n}, tell them {c}"), ("c", "message {n} - {c}"), ("c", "send {n} a text saying {c}"),
    ("c", "I need you to text {n}: {c}"), ("c", "text {n} to say {c}"), ("q", 'text {n} "{q}"'),
    ("q", 'send {n} a text: "{q}"'), ("q", 'text {n} saying "{q}"'), ("c", "Text {n} - {c}.")]
TXT_LETKNOW = [
    ("c", "let {n} know {c}"), ("c", "tell {n} that {c}"), ("c", "can you let {n} know {c}"),
    ("c", "drop {n} a line - {c}"), ("c", "I want {n} to know {c}"), ("c", "get word to {n} that {c}"),
    ("c", "give {n} a heads up that {c}"), ("c", "tell {n} {c}"), ("c", "make sure {n} knows {c}"),
    ("q", 'tell {n}: "{q}"')]
TXT_SLANG = [
    ("c", "shoot {n} a quick text saying {c}"), ("c", "hit {n} up and say {c}"), ("c", "ping {n}: {c}"),
    ("c", "msg {n} {c}"), ("c", "iMessage {n} that {c}"), ("c", "fire a text over to {n} - {c}"),
    ("c", "send {n} a lil message, {c}"), ("q", 'ping {n} "{q}"'), ("q", 'shoot {n} this: "{q}"'),
    ("c", "text {n} real quick, {c}")]
TXT_POLITE = [
    ("c", "Could you please send a message to {n} saying {c}?"), ("c", "I'd like to message {n} and say {c}."),
    ("c", "Would you mind texting {n}? {c}"), ("c", "Please let {n} know by text that {c}."),
    ("c", "Can you shoot a message to {n}? Say {c}."), ("c", "I should text {n} - could you tell them {c}?"),
    ("q", 'Could you text {n} the following: "{q}"'), ("c", "Kindly send {n} a text to say {c}.")]
TXT_DEV = [
    ("c", "Compose and send an iMessage to {n}: {c}"), ("c", "{n} needs a text from me - {c}"),
    ("c", "go ahead and message {n}, {c}"), ("c", "I'd like {n} to get a text saying {c}"),
    ("c", "send word to {n} by text that {c}"), ("q", 'Text over to {n}: "{q}"'),
    ("c", "{n}: text them {c}"), ("c", "reach out to {n} by text - {c}")]
TXT_NUMBER = [
    ("c", "text {n} that {c}"), ("c", "send a text to {n} saying {c}"), ("q", 'message {n} "{q}"'),
    ("c", "can you message {n} - {c}")]


@family("outbound", "out_text_direct", weight=15, failure=("direct_real_tool_without_discovery", "outbound_paraphrase_miss"))
def f_out_text_direct(r, c, now):
    return _text_flow(r, c, now, TXT_DIRECT)


@family("outbound", "out_text_letknow", weight=13, failure=("direct_real_tool_without_discovery", "outbound_paraphrase_miss"))
def f_out_text_letknow(r, c, now):
    return _text_flow(r, c, now, TXT_LETKNOW)


@family("outbound", "out_text_slang", weight=12, failure=("outbound_paraphrase_miss", "direct_real_tool_without_discovery"))
def f_out_text_slang(r, c, now):
    return _text_flow(r, c, now, TXT_SLANG)


@family("outbound", "out_text_polite", weight=9, failure=("outbound_paraphrase_miss",))
def f_out_text_polite(r, c, now):
    return _text_flow(r, c, now, TXT_POLITE)


@family("outbound", "out_text_dev", split="dev", weight=6, failure=("outbound_paraphrase_miss", "direct_real_tool_without_discovery"))
def f_out_text_dev(r, c, now):
    return _text_flow(r, c, now, TXT_DEV)


@family("outbound", "out_text_deny", weight=14, failure=("retry_after_denial", "false_sent_claim"))
def f_out_text_deny(r, c, now):
    return _text_flow(r, c, now, TXT_DIRECT + TXT_LETKNOW + TXT_SLANG, deny=True)


@family("outbound", "out_text_deny_dev", split="dev", weight=3, failure=("retry_after_denial", "false_sent_claim"))
def f_out_text_deny_dev(r, c, now):
    return _text_flow(r, c, now, TXT_DEV, deny=True)


@family("outbound", "out_text_number", weight=5, failure=("unneeded_lookup", "direct_real_tool_without_discovery"))
def f_out_text_number(r, c, now):
    return _text_flow(r, c, now, TXT_NUMBER, number=True)


# ---------------------------------------------------------------- email sends

EM_NAME = [
    "email {n} {c}", "send {n} an email {c}", "write an email to {n} {c}", "shoot {n} an email {c}",
    "mail {n} {c}", "can you email {n} {c}", "drop {n} an email {c}", "please email {n} {c}",
    "I need to email {n} {c}", "send an email to {n} {c}"]
EM_DEV = [
    "I need to get an email out to {n} {c}", "shoot an email over to {n} {c}", "send mail to {n} {c}",
    "compose and send an email to {n} {c}", "{n} should get an email from me {c}", "put an email together for {n} and send it {c}"]


def _email_flow(r, c, now, tpls, deny=False, address=False):
    nm = r.choice(PEOPLE)
    said, _ = name_forms(r, nm)
    it = email_intent(r, first(nm))
    tpl = r.choice(tpls)
    addr = email_for(nm)
    if address:
        said = addr
    c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
    if address:
        c.discover(["mail"], ["send_email"])
    else:
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
        c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm, via="email"))
    args = {"to": addr, "subject": it["subject"], "body": it["body"]}
    res = DECLINED if deny else sent_email(c, addr)
    c.call("send_email", args, res)
    f = first(nm) if not address else addr
    c.final(_fin_declined(r, f, "email") if deny else _fin_sent(r, "email", f, it["body"]))
    return {"template": tpls.index(tpl), "slots": {"contact": nm, "topic": it["topic"], "deny": deny, "address": address}}


@family("outbound", "out_email_name", weight=17, failure=("outbound_paraphrase_miss", "direct_real_tool_without_discovery"))
def f_out_email_name(r, c, now):
    return _email_flow(r, c, now, EM_NAME)


@family("outbound", "out_email_address", weight=10, failure=("unneeded_lookup", "direct_real_tool_without_discovery"))
def f_out_email_address(r, c, now):
    return _email_flow(r, c, now, EM_NAME, address=True)


@family("outbound", "out_email_deny", weight=7, failure=("retry_after_denial", "false_sent_claim"))
def f_out_email_deny(r, c, now):
    return _email_flow(r, c, now, EM_NAME, deny=True)


@family("outbound", "out_email_dev", split="dev", weight=6, failure=("outbound_paraphrase_miss",))
def f_out_email_dev(r, c, now):
    return _email_flow(r, c, now, EM_DEV)


# ---------------------------------------------------------------- drafts

DR_EMAIL = [
    "draft an email to {n} {c}", "write up an email for {n} {c}, I'll look it over before sending",
    "prepare an email to {n} {c} but don't send it", "put together a draft email to {n} {c}",
    "start an email to {n} {c} - I want to review it first", "can you draft something to {n} by email {c}? just a draft",
    "make me a draft of an email to {n} {c}"]
DR_MSG = [
    "draft a text to {n} saying {c}", "write a message to {n} saying {c} but let me look at it first",
    "queue up a text for {n}: {c}, don't send yet", "prep a message to {n} saying {c}",
    "draft an iMessage for {n} - {c} - I'll send it myself", "set up a text to {n} that {c} but hold off on sending",
    "just draft a message to {n}, {c}"]
DR_DEV = [
    ("email", "put together a note to {n} {c} for me to review"), ("email", "I'd like a draft email to {n} {c}, nothing sent yet"),
    ("message", "write out a text for {n} - {c} - and leave it as a draft"),
    ("message", "pull together a message to {n} saying {c}; I want to check it before it goes"),
    ("email", "compose, but don't send, an email to {n} {c}")]


def _draft_flow(r, c, now, kind, tpl, tpl_idx):
    nm = r.choice(PEOPLE)
    said, _ = name_forms(r, nm)
    if kind == "email":
        it = email_intent(r, first(nm))
        c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
        c.discover(["mail", "messages_contacts"], ["draft_email", "lookup_contact"])
        c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm, via="email"))
        c.call("draft_email", {"to": email_for(nm), "subject": it["subject"], "body": it["body"]},
               f"Draft opened in Mail to {email_for(nm)} — nothing has been sent.")
        c.final(r.choice([f"I opened a draft email to {first(nm)} in Mail. Nothing has been sent - review it and send when you're ready. The draft says:\n{Q(it['body'])}",
                          f"Draft is ready in Mail for {first(nm)} (subject: {it['subject']}). It hasn't been sent. Body:\n{Q(it['body'])}",
                          f"Your draft to {first(nm)} is open in Mail, unsent:\n{Q(it['body'])}"]))
    else:
        it = text_intent(r)
        c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
        c.discover(["messages_contacts"], ["draft_message", "lookup_contact"])
        c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm))
        c.call("draft_message", {"to": phone_for(nm), "text": it["body"]},
               f"Message draft prepared in Wisp for {nm} — nothing has been sent.")
        c.final(r.choice([f"Drafted a message to {first(nm)}: “{it['body']}” It hasn't been sent.",
                          f"I prepared the text for {first(nm)} but didn't send it: “{it['body']}”",
                          f"Draft saved for {first(nm)} — not sent yet. It says: “{it['body']}”"]))
    return {"template": tpl_idx, "slots": {"contact": nm, "kind": kind}}


@family("outbound", "out_draft_email", weight=10, failure=("draft_vs_send_confusion", "outbound_paraphrase_miss"))
def f_out_draft_email(r, c, now):
    i = r.randrange(len(DR_EMAIL))
    return _draft_flow(r, c, now, "email", DR_EMAIL[i], i)


@family("outbound", "out_draft_message", weight=9, failure=("draft_vs_send_confusion", "outbound_paraphrase_miss"))
def f_out_draft_message(r, c, now):
    i = r.randrange(len(DR_MSG))
    return _draft_flow(r, c, now, "message", DR_MSG[i], i)


@family("outbound", "out_draft_dev", split="dev", weight=4, failure=("draft_vs_send_confusion",))
def f_out_draft_dev(r, c, now):
    i = r.randrange(len(DR_DEV))
    return _draft_flow(r, c, now, DR_DEV[i][0], DR_DEV[i][1], i)


# ---------------------------------------------------------------- reply / forward

REPLY_BODIES = [
    ("yes, that time works for me", "Yes, that time works for me."),
    ("thanks, I'll review it tonight", "Thanks, I'll review it tonight."),
    ("that sounds good, please go ahead", "That sounds good, please go ahead."),
    ("I'm not available that week", "I'm not available that week."),
    ("can we do 4pm instead", "Can we do 4pm instead?"),
    ("I got it, thank you", "I got it, thank you.")]
MAIL_SUBJECTS = ["Design review notes", "Lease renewal", "Workshop schedule", "Conference travel", "Invoice 2048",
                 "Dentist reschedule", "Team lunch", "Photos from the trip", "Contract draft", "Parent-teacher night",
                 "Quarterly numbers", "Gym membership", "Garage quote"]


def _mail_listing(sender, subject, msgid, when, body_snip):
    return (f"1 email:\nFrom: {sender} <{email_for(sender)}>\nDate: {when}\nSubject: {subject}\nMessage-ID: {msgid}\n"
            f"Body: {body_snip}")


def _msgid(r):
    return "<" + "".join(r.choice("0123456789abcdef") for _ in range(10)) + "@mail.example.invalid>"


REPLY_TPL = [
    "reply to {s}'s email about {subj} and say {c}", "answer {s}'s email on {subj} - {c}",
    "write back to {s} about {subj}, tell them {c}", "respond to the {subj} email from {s}: {c}",
    "reply to that {subj} email from {s} saying {c}", "hit reply on {s}'s {subj} message and say {c}"]
REPLY_DEV = [
    "{s} sent me something about {subj} - reply and say {c}", "get back to {s} on the {subj} thread: {c}",
    "send a reply to {s} regarding {subj}, saying {c}"]


def _reply_flow(r, c, now, tpls):
    s = r.choice(PEOPLE)
    subj = r.choice(MAIL_SUBJECTS)
    tpl = r.choice(tpls)
    phrase, content = r.choice(REPLY_BODIES)
    body = EMAIL_FRAME.format(first=first(s), content=content)
    sf = first(s) if r.random() < 0.7 else s
    c.user(messy(r, tpl.format(s=sf, subj=subj, c=phrase), protect=(sf, subj)))
    c.discover(["mail"], ["reply_to_email", "view_emails"])
    mid = _msgid(r)
    day = now.date() - dt.timedelta(days=r.randint(0, 2))
    c.call("view_emails", {"query": subj, "count": 3, "strict_match": True},
           _mail_listing(s, subj, mid, f"{dshort(day)}, {t12(r.randint(7, 17), r.choice([0, 15, 30, 45]))}",
                         "Could you let me know when you have a moment?"))
    c.call("reply_to_email", {"message_id": mid, "body": body}, f"Reply sent to {s} <{email_for(s)}>.")
    c.final(r.choice([f"Replied to {first(s)}'s email about {subj}:\n{Q(body)}", f"Done - your reply to {first(s)} ({subj}) is sent:\n{Q(body)}",
                      f"Sent your reply to {first(s)}:\n{Q(body)}"]))
    return {"template": tpls.index(tpl), "slots": {"sender": s, "subject": subj}}


@family("outbound", "out_reply", weight=13, failure=("outbound_paraphrase_miss", "direct_real_tool_without_discovery"))
def f_out_reply(r, c, now):
    return _reply_flow(r, c, now, REPLY_TPL)


@family("outbound", "out_reply_dev", split="dev", weight=2, failure=("outbound_paraphrase_miss",))
def f_out_reply_dev(r, c, now):
    return _reply_flow(r, c, now, REPLY_DEV)


FWD_TPL = ["forward the email about {subj} to {n}", "send {n} the {subj} email", "can you forward {n} that {subj} email",
           "pass the {subj} email along to {n}", "forward {n} the message from {s} about {subj}",
           "forward {s}'s {subj} email to {n}"]


@family("outbound", "out_forward", weight=8, failure=("compound_order", "direct_real_tool_without_discovery"))
def f_out_forward(r, c, now):
    s, nm = r.sample(PEOPLE, 2)
    subj = r.choice(MAIL_SUBJECTS)
    tpl = r.choice(FWD_TPL)
    said, _ = name_forms(r, nm)
    c.user(messy(r, tpl.format(subj=subj, n=said, s=first(s)), protect=(said, subj)))
    c.discover(["mail", "messages_contacts"], ["forward_email", "lookup_contact", "view_emails"])
    mid = _msgid(r)
    day = now.date() - dt.timedelta(days=r.randint(1, 4))
    c.calls([("view_emails", {"query": subj, "count": 3, "strict_match": True},
              _mail_listing(s, subj, mid, f"{dshort(day)}, {t12(r.randint(7, 17), 0)}", "Please see the details below.")),
             ("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm, via="email"))])
    c.call("forward_email", {"message_id": mid, "to": email_for(nm)}, f"Forwarded to {email_for(nm)}.")
    c.final(r.choice([f"Forwarded the “{subj}” email to {first(nm)}.", f"Done — {first(nm)} now has the {subj} email.",
                      f"Sent. I forwarded {first(s)}'s email about {subj} to {first(nm)}."]))
    return {"template": FWD_TPL.index(tpl), "slots": {"sender": s, "to": nm, "subject": subj}}


# ---------------------------------------------------------------- scheduled sends

def _sched_flow(r, c, now, tpls):
    nm = r.choice(PEOPLE)
    said, _ = name_forms(r, nm)
    chan = r.choice(["message", "message", "email"])
    tm = pick_time(r, 6, 21)
    day = pick_day(r, now, kinds=("today", "tomorrow", "tomorrow"))
    mins = r.choice([10, 20, 30, 45])
    whens = [(f"at {tm.spoken}", f"at {tm.spoken}"), (f"in {mins} minutes", f"in {mins} minutes"),
             (f"{day.spoken} at {tm.spoken}" if day.kind != "today" else f"at {tm.spoken}",
              f"{day.kind if day.kind != 'date' else day.spoken} at {tm.spoken}" if day.kind != "today" else f"at {tm.spoken}")]
    spoken, wa = r.choice(whens)
    if chan == "message":
        it = text_intent(r)
        body, clause = it["body"], it["clause"]
    else:
        it = email_intent(r, first(nm))
        body, clause = it["body"], it["clause"]
    tpl = r.choice(tpls[chan])
    c.user(messy(r, tpl.format(n=said, c=clause, w=spoken), protect=(said,)))
    fams = ["mail", "messages_contacts"] if chan == "email" else ["mail", "messages_contacts"]
    c.discover(fams, ["lookup_contact", "schedule_send"])
    c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm, via="email" if chan == "email" else "phone"))
    args = {"channel": chan, "to": email_for(nm) if chan == "email" else phone_for(nm), "body": body, "when": wa}
    if chan == "email":
        args["subject"] = it["subject"]
    sid = r.randint(3, 60)
    hh = tm.long
    res = f"Scheduled {chan} to {nm} for {wa} (id {sid})."
    c.call("schedule_send", args, res)
    c.final(r.choice([f"Scheduled - your {chan} to {first(nm)} will go out {wa}: {Q(body)}", f"Queued for {first(nm)}, sending {wa} (id {sid}): {Q(body)}",
                      f"Done. It's scheduled to send to {first(nm)} {wa}: {Q(body)}"]))
    return {"template": 0, "slots": {"contact": nm, "channel": chan, "when": wa}}


SCHED_TPL = {"message": ["text {n} {w} that {c}", "schedule a text to {n} for {w} saying {c}", "send {n} a message {w}: {c}",
                         "remind {n} by text {w} that {c}", "{w} text {n} that {c}"],
             "email": ["email {n} {w} {c}", "schedule an email to {n} for {w} {c}", "send {n} an email {w} {c}",
                       "set up an email for {n} to go out {w} {c}"]}
SCHED_DEV = {"message": ["queue a text for {n}, to go out {w}: {c}", "have a message go to {n} {w} saying {c}"],
             "email": ["I want an email to land with {n} {w} {c}", "delay-send an email to {n} until {w} {c}"]}


@family("outbound", "out_schedule", weight=9, failure=("scheduled_send_vs_send", "direct_real_tool_without_discovery"))
def f_out_schedule(r, c, now):
    return _sched_flow(r, c, now, SCHED_TPL)


@family("outbound", "out_schedule_dev", split="dev", weight=2, failure=("scheduled_send_vs_send",))
def f_out_schedule_dev(r, c, now):
    return _sched_flow(r, c, now, SCHED_DEV)


# ---------------------------------------------------------------- two recipients

MULTI_TPL = ["text {a} and {b} that {c}", "let {a} and {b} know {c}", "send {a} and {b} each a text saying {c}",
             "message both {a} and {b}: {c}", "tell {a} and also {b} that {c}"]


@family("outbound", "out_multi", weight=8, failure=("compound_never_completes", "outbound_paraphrase_miss"))
def f_out_multi(r, c, now):
    a, b = r.sample(PEOPLE, 2)
    it = text_intent(r)
    tpl = r.choice(MULTI_TPL)
    c.user(messy(r, tpl.format(a=first(a), b=first(b), c=it["clause"]), protect=(first(a), first(b))))
    c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
    c.calls([("lookup_contact", {"name": first(a)}, lookup_result(a)), ("lookup_contact", {"name": first(b)}, lookup_result(b))])
    deny_b = r.random() < 0.2
    c.calls([("send_message", {"to": phone_for(a), "text": it["body"]}, sent_msg(c, a)),
             ("send_message", {"to": phone_for(b), "text": it["body"]}, DECLINED if deny_b else sent_msg(c, b))])
    if deny_b:
        c.final(f"I texted {first(a)}: {Q(it['body'])} The text to {first(b)} wasn't sent because you declined it.")
    else:
        c.final(r.choice([f"Sent to both {first(a)} and {first(b)}: {Q(it['body'])}", f"Done - texted {first(a)} and {first(b)}: {Q(it['body'])}",
                          f"Both messages went out ({first(a)} and {first(b)}): {Q(it['body'])}"]))
    return {"template": MULTI_TPL.index(tpl), "slots": {"a": a, "b": b, "deny_second": deny_b}}


# ================================================================ calendar / mail / notes world (synthetic)

EVENT_BANK = [
    # (title template, location, keyword the user says, aliases the user may say)
    ("Team standup", None, "standup", ["standup", "team standup", "daily standup"]),
    ("Dentist cleaning", "Brightside Dental", "dentist", ["dentist", "dentist appointment", "teeth cleaning"]),
    ("Lunch with {p}", "Cafe Verde", "lunch", ["lunch with {p}", "lunch date with {p}"]),
    ("Design review", "Room 4B", "design review", ["design review", "design critique"]),
    ("Haircut", "Clip & Co", "haircut", ["haircut", "barber appointment"]),
    ("Yoga class", "Lotus Studio", "yoga", ["yoga", "yoga class"]),
    ("Physio", "Harbor Clinic", "physio", ["physio", "physio session", "physiotherapy"]),
    ("Book club", None, "book club", ["book club"]),
    ("Vet appointment", "Paws Vet", "vet", ["vet", "vet appointment", "the vet"]),
    ("1:1 with {p}", None, "1:1", ["1:1 with {p}", "one on one with {p}"]),
    ("Project kickoff", "Zoom", "kickoff", ["kickoff", "project kickoff"]),
    ("Car service", "Garage on 5th", "car service", ["car service", "car appointment", "garage appointment"]),
    ("Coffee with {p}", None, "coffee", ["coffee with {p}", "coffee chat with {p}"]),
    ("Budget review", "Room 2A", "budget review", ["budget review", "budget meeting"]),
    ("Piano lesson", None, "piano", ["piano lesson", "piano"]),
    ("Sprint planning", None, "sprint planning", ["sprint planning", "planning meeting"]),
    ("Parent-teacher meeting", "Lincoln Elementary", "parent-teacher", ["parent-teacher meeting", "parent teacher night"]),
    ("Client call", "Zoom", "client call", ["client call", "call with the client"]),
    ("Dinner reservation", "Osteria Blu", "dinner", ["dinner reservation", "dinner plans"]),
]


class Ev:
    def __init__(self, date, h, m, dur, title, loc, key, alias):
        self.date, self.h, self.m, self.dur, self.title, self.loc, self.key, self.alias = date, h, m, dur, title, loc, key, alias

    @property
    def start(self):
        return self.h * 60 + self.m

    @property
    def end(self):
        return self.start + self.dur

    @property
    def tstr(self):
        return t12(self.h, self.m)


HOUR_RANGE = {"dinner": (17, 20), "lunch": (11, 13), "standup": (9, 10), "coffee": (8, 15), "yoga": (7, 19), "kickoff": (9, 15),
              "haircut": (9, 17), "dentist": (8, 16), "parent-teacher": (16, 19), "book club": (17, 20), "sprint planning": (9, 15)}


def make_event(r, date, h=None, m=None, dur=None, avoid=(), used_keys=()):
    for _ in range(80):
        t, loc, key, aliases = r.choice(EVENT_BANK)
        if key in used_keys:
            continue
        if h is not None and key in HOUR_RANGE and not (HOUR_RANGE[key][0] <= h <= HOUR_RANGE[key][1]):
            continue
        p = first(r.choice(PEOPLE))
        title = t.format(p=p)
        alias = r.choice(aliases).format(p=p)
        lo, hi = HOUR_RANGE.get(key, (8, 18))
        hh = h if h is not None else r.randint(lo, hi)
        mm = m if m is not None else r.choice([0, 0, 30, 15, 45])
        d = dur if dur is not None else r.choice([30, 45, 60, 60, 90])
        ev = Ev(date, hh, mm, d, title, loc, key, alias)
        if all(ev.end <= o.start or ev.start >= o.end for o in avoid):
            return ev
    raise RuntimeError("could not place event")


def make_day(r, date, n, hmin=8, hmax=18):
    evs = []
    for _ in range(n):
        evs.append(make_event(r, date, h=r.randint(hmin, hmax), avoid=evs, used_keys=[e.key for e in evs]))
    return sorted(evs, key=lambda e: e.start)


def _item_line(ev):
    return f"  {ev.tstr} - {ev.title}" + (f" ({ev.loc})" if ev.loc else "")


def agenda_result(now, window, events, reminders=()):
    """get_upcoming-style result. events: [Ev]; reminders: [(date,h,m,title)]."""
    total = len(events) + len(reminders)
    if total == 0:
        return f"Today is {dshort(now.date())}. Nothing scheduled for {window}."
    out = [f"Schedule - {window} ({total} item(s))"]
    days = sorted({e.date for e in events} | {x[0] for x in reminders})
    for d in days:
        out.append(f"{dlabel(d, now)}, {dshort(d)}" if dlabel(d, now) in ("Today", "Tomorrow") else dshort(d))
        es = [e for e in events if e.date == d]
        rs = [x for x in reminders if x[0] == d]
        if es:
            out.append(" Calendar events")
            out.extend(_item_line(e) for e in sorted(es, key=lambda e: e.start))
        if rs:
            out.append(" Reminders")
            out.extend(f"  {t12(h, m)} - {title}" for _, h, m, title in sorted(rs, key=lambda x: (x[1], x[2])))
    return "\n".join(out)


def free_result(minutes, ds, de, windows, label):
    if not windows:
        return f"No gaps of {minutes}+ minutes in the next 1 day(s) between {ds}:00 and {de}:00."
    spans = ", ".join(f"{t12(a // 60, a % 60)}-{t12(b // 60, b % 60)}" for a, b in windows)
    return f"Free for {minutes}+ minutes, {ds}:00-{de}:00:\n  {label}: {spans}\n(Timed items are assumed to run an hour.)"


def free_windows(events, ds, de, minutes):
    """Gaps within [ds,de) hours, given events (assumed 1h per the tool's note)."""
    cur, out = ds * 60, []
    for e in sorted(events, key=lambda e: e.start):
        s, en = e.start, e.start + 60
        if s > cur and s - cur >= minutes:
            out.append((cur, min(s, de * 60)))
        cur = max(cur, en)
    if de * 60 - cur >= minutes:
        out.append((cur, de * 60))
    return [(a, b) for a, b in out if b > a and b - a >= minutes]


def ev_when(now, ev):
    return f"{dlabel(ev.date, now).lower() if dlabel(ev.date, now) in ('Today', 'Tomorrow') else 'on ' + WEEKDAYS[ev.date.weekday()]} at {ev.tstr}"


def sender_line(i, sender, subject, unread=True, when=None):
    tag = " (unread)" if unread else ""
    return f"{i}) {sender} - {subject}{tag}"


EMAIL_SUBJ_BANK = [
    ("Priya Raman", "Design review notes"), ("Jonas Weber", "Lease renewal"), ("Camila Reyes", "Workshop schedule"),
    ("Dev Malhotra", "Conference travel"), ("Noor Haddad", "Invoice 2048"), ("Elena Petrov", "Team lunch"),
    ("Marcus Bell", "Contract draft"), ("Hana Sato", "Photos from the trip"), ("Luca Romano", "Quarterly numbers"),
    ("Aisha Karimi", "Garage quote"), ("Owen Pritchard", "Gym membership"), ("Zoe Tanaka", "Parent-teacher night")]


def email_digest(r, n, period_label, unread_only=False):
    picks = r.sample(EMAIL_SUBJ_BANK, n)
    lines = [f"{n} email(s) {period_label}:"]
    items = []
    for i, (s, subj) in enumerate(picks, 1):
        un = True if unread_only else r.random() < 0.5
        lines.append(sender_line(i, s, subj, un))
        items.append((s, subj, un))
    return "\n".join(lines), items


NOTE_BANK = [
    ("Gift ideas for Mom", "scarf, tea sampler, a framed photo", "gift"),
    ("Packing list - Lisbon trip", "passport, charger, rain jacket, sunscreen", "packing"),
    ("Book ideas", "The Overstory; Piranesi; a cookbook for beginners", "book"),
    ("Directions to Dana's place", "Take Route 9 to Alder Lane, blue door, parking in back", "directions"),
    ("Birthday party plan", "Saturday 6 PM at 14 Alder Lane, bring cake", "party"),
    ("Project Falcon notes", "Kickoff done. Next: vendor shortlist by Friday", "falcon"),
    ("Recipe - lentil soup", "1 cup lentils, 2 carrots, cumin, lemon", "soup"),
    ("Garden to-do", "prune roses, order mulch, fix the gate latch", "garden"),
    ("Apartment search", "Shortlist: Maple St 2BR, Harbor View studio", "apartment"),
    ("Workout plan", "Mon legs, Wed back, Fri shoulders", "workout"),
    ("Movie list", "Past Lives; Anatomy of a Fall; Perfect Days", "movie"),
    ("Moving checklist", "box books, cancel internet, forward mail", "moving"),
]
REMINDER_BANK = [
    "call the plumber", "pick up dry cleaning", "pay the electricity bill", "water the plants", "renew passport",
    "buy flowers for Dana's birthday", "send the invoice", "take out the recycling", "submit expense report",
    "refill prescription", "book flights", "return library books", "charge the camera", "email the landlord",
    "back up photos", "order printer ink", "call the bank", "buy a birthday card"]
CITIES = [("Portland,OR", "Portland"), ("Austin,TX", "Austin"), ("Denver,CO", "Denver"), ("Seattle,WA", "Seattle"),
          ("Boston,MA", "Boston"), ("Chicago,IL", "Chicago"), ("London", "London"), ("Lisbon", "Lisbon"),
          ("Toronto", "Toronto"), ("Dublin,CA", "Dublin"), ("Sydney", "Sydney"), ("Madrid", "Madrid")]


# ================================================================ COMPOUND (read -> act, multi-source)

def weather_result(r, city_full, d, rainy=None):
    rain = r.random() < 0.5 if rainy is None else rainy
    hi = r.randint(48, 82)
    lo = hi - r.randint(8, 16)
    cond = r.choice(["Light rain", "Showers", "Steady rain"]) if rain else r.choice(["Sunny", "Partly cloudy", "Clear", "Mostly sunny"])
    pct = r.randint(60, 90) if rain else r.randint(0, 15)
    area = city_full.split(",")[0]
    return (f"{area} - {d.label if d.kind != 'date' else dshort(d.date)}: {cond}, high {hi}F / low {lo}F, "
            f"{pct}% chance of rain."), rain, (hi, lo, cond, pct)


def messages_result(r, name, n=2, today=True):
    f = first(name) if name not in RELATIVES else name
    lines = [f"Messages with {name}:"]
    opts = [("them", "Are we still on for dinner?"), ("me", "Yes, 7 works."), ("them", "Perfect, see you there"),
            ("them", "Did you see the photos I sent?"), ("me", "Not yet, will look tonight"),
            ("them", "Can you call me later?"), ("them", "Running 5 min behind")]
    r.shuffle(opts)
    picks = opts[:n]
    t0 = r.randint(8, 16)
    items = []
    for i, (who, txt) in enumerate(picks):
        sp = f if who == "them" else "Me"
        lines.append(f"{sp} ({t12(t0, i * 7 + 3)}): {txt}")
        items.append((sp, txt))
    return "\n".join(lines), items


def _name_arg(said, nm):
    return nm if nm in RELATIVES else (nm if said == nm else first(nm))


# ---- C1 calendar -> text

CT_TPL = ["text {n} what time my {a} is {d}", "tell {n} what time I've got {a} {d}", "let {n} know when my {a} is {d}",
          "check my calendar for {a} {d} and text {n} the time", "message {n} the start time of my {a} {d}",
          "look up when my {a} is {d} and send that to {n}", "find my {a} {d}, then text {n} when it starts"]
CT_DEV = ["{n} wants to know when my {a} is {d} - find it and text them", "grab the time of my {a} {d} from the calendar and pass it on to {n} by text",
          "send {n} a text with the time for my {a} {d}", "{n} asked about my {a} {d}; look it up and tell them"]


def _cal_text(r, c, now, tpls, deny_p=0.1):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    day = pick_day(r, now, kinds=("today", "tomorrow", "weekday"))
    ev = make_event(r, day.date, h=r.randint(9, 18) if day.kind != "today" else min(21, max(now.hour + 2, 9)), m=r.choice([0, 30]))
    tpl = r.choice(tpls)
    c.user(messy(r, tpl.format(n=said, a=ev.alias, d=day.spoken), protect=(said, ev.alias)))
    c.discover(["calendar", "messages_contacts"], ["get_upcoming", "lookup_contact", "send_message"])
    res_ev = agenda_result(now, day.period if day.kind == "date" else day.kind, [ev])
    if r.random() < 0.5:
        c.calls([("get_upcoming", {"period": day.period, "query": ev.key}, res_ev),
                 ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))])
    else:
        c.call("get_upcoming", {"period": day.period, "query": ev.key}, res_ev)
        c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
    body = f"My {lc_first(ev.title)} is {ev_when(now, ev)}."
    deny = r.random() < deny_p
    c.call("send_message", {"to": phone_for(nm), "text": body}, DECLINED if deny else sent_msg(c, nm))
    f = nm if nm in RELATIVES else first(nm)
    c.final(_fin_declined(r, f) if deny else
            r.choice([f"{ev.title} is {ev_when(now, ev)}. I texted {f}: {Q(body)}",
                      f"Found it - {ev.title}, {ev_when(now, ev)} - and texted {f}: {Q(body)}",
                      f"Done. Your {ev.title} is {ev_when(now, ev)}, and {f} got: {Q(body)}"]))
    return {"template": tpls.index(tpl), "slots": {"event": ev.key, "contact": nm, "denied": deny}}


@family("compound", "cmp_cal_text", weight=14, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_cal_text(r, c, now):
    return _cal_text(r, c, now, CT_TPL)


@family("compound", "cmp_cal_text_dev", split="dev", weight=6, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_cal_text_dev(r, c, now):
    return _cal_text(r, c, now, CT_DEV)


# ---- C2 free? -> text

FT_TPL = [("if I'm free {d} at {t}, text {n} that I can make it", "I can make it."),
          ("check whether I'm free {d} around {t} - if so, tell {n} by text that I'm in", "I'm in."),
          ("am I free {d} at {t}? If yes, let {n} know I'm good for it", "I'm good for it."),
          ("see if {d} at {t} is open and if it is, text {n} yes", "Yes."),
          ("text {n} I'm in for {d} at {t} but only if my calendar's clear then", "I'm in for {d} at {t}.")]


@family("compound", "cmp_free_text", weight=14, failure=("compound_never_completes", "conditional_action_ignored", "loop"))
def f_cmp_free_text(r, c, now):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday")))
    tm = pick_time(r, 10, 19, minutes=(0,))
    free = r.random() < 0.62
    tpl, body_t = r.choice(FT_TPL)
    c.user(messy(r, tpl.format(n=said, d=day.spoken, t=tm.spoken), protect=(said,)))
    c.discover(["calendar", "messages_contacts"], ["find_free_time", "lookup_contact", "send_message"])
    wins = [(tm.h * 60, (tm.h + 1) * 60)] if free else []
    c.call("find_free_time", {"period": day.period, "minutes": 60, "day_start": tm.h, "day_end": tm.h + 1},
           free_result(60, tm.h, tm.h + 1, wins, day.label))
    f = nm if nm in RELATIVES else first(nm)
    dd = day.label.lower() if day.kind != 'date' else 'on ' + WEEKDAYS[day.date.weekday()]
    if free:
        c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
        body = body_t.format(d=day.spoken, t=tm.spoken)
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        c.final(r.choice([f"You're free then, so I texted {f}: {Q(body)}", f"Your calendar is clear at {tm.long} {dd}. Texted {f}: {Q(body)}",
                          f"Free at {tm.long} {dd}. I sent {f}: {Q(body)}"]))
    else:
        c.final(r.choice([f"You're not free at {tm.long} {dd} - that slot is taken, so I didn't text {f}.",
                          f"That hour ({tm.long} {dd}) is booked on your calendar, so I held off on texting {f}.",
                          f"No - {tm.long} {dd} is busy. I haven't sent anything to {f}."]))
    return {"template": [t for t, _ in FT_TPL].index(tpl), "slots": {"free": free, "contact": nm}}


# ---- C3 mail + calendar summary

MC_TPL = ["what emails came in {p} and what's on my calendar", "summarize my email from {p} and show my schedule too",
          "give me a rundown of my inbox and calendar for {p}", "any new mail {p}? also what do I have on",
          "catch me up on email and my calendar for {p}", "inbox and agenda for {p}, please", "what's in my inbox {p}, and what meetings do I have",
          "email digest plus my agenda for {p}", "show my emails and my events for {p}", "how's my inbox looking {p} and what's coming up on the calendar",
          "I need mail and meetings for {p}", "recap {p}: emails first, then the calendar"]
MC_UNREAD = ["what's unread in my email {p}, and what's on my calendar", "unread mail + {p}'s schedule please",
             "show me unread emails and my calendar for {p}"]


@family("compound", "cmp_mail_cal", weight=11, failure=("compound_never_completes", "multi_source_partial"))
def f_cmp_mail_cal(r, c, now):
    unread = r.random() < 0.3
    tpl = r.choice(MC_UNREAD if unread else MC_TPL)
    p = "today"
    c.user(messy(r, tpl.format(p=p)))
    c.discover(["calendar", "mail"], ["get_upcoming", "summarize_emails"])
    n = r.randint(2, 4)
    dig, items = email_digest(r, n, "today", unread_only=unread)
    evs = make_day(r, now.date(), r.randint(1, 3), hmin=max(8, min(17, now.hour + 1)), hmax=19)
    earg = {"period": "today", "unread": True} if unread else {"period": "today"}
    c.calls([("summarize_emails", earg, dig), ("get_upcoming", {"period": "today"}, agenda_result(now, "today", evs))])
    mail = "; ".join(f"{s} ({subj})" for s, subj, _ in items)
    cal = ", ".join(f"{e.title} at {e.tstr}" for e in evs)
    c.final(f"Email: {n} message{'s' if n != 1 else ''} {'unread ' if unread else ''}today — {mail}.\nCalendar today: {cal}.")
    return {"template": 0, "slots": {"unread": unread, "n_mail": n, "n_events": len(evs)}}


# ---- C4 messages + calendar

MS_TPL = ["did {n} text me today, and what's my schedule {d}", "check my texts with {n} and tell me what's on {d}",
          "anything from {n} in messages? also what do I have {d}", "show my recent messages with {n} and my calendar for {d}"]


@family("compound", "cmp_msgs_cal", weight=8, failure=("compound_never_completes", "multi_source_partial"))
def f_cmp_msgs_cal(r, c, now):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    day = pick_day(r, now, kinds=("today", "tomorrow"))
    tpl = r.choice(MS_TPL)
    c.user(messy(r, tpl.format(n=said, d=day.spoken), protect=(said,)))
    c.discover(["calendar", "messages_contacts"], ["get_upcoming", "view_messages"])
    mres, items = messages_result(r, nm)
    evs = make_day(r, day.date, r.randint(1, 3), hmin=max(8, min(16, now.hour + 1)) if day.kind == "today" else 8, hmax=19)
    c.calls([("view_messages", {"conversation": _name_arg(said, nm), "count": 5}, mres),
             ("get_upcoming", {"period": day.period}, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))])
    f = nm if nm in RELATIVES else first(nm)
    msgs = " / ".join(f"{s}: “{t}”" for s, t in items)
    c.final(f"Recent messages with {f}: {msgs}.\nOn your calendar {day.label.lower() if day.kind != 'date' else 'on ' + WEEKDAYS[day.date.weekday()]}: " +
            ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".")
    return {"template": MS_TPL.index(tpl), "slots": {"contact": nm}}


# ---- C5 notes + reminders

NR_PAIRS = [
    ("birthday", "Birthday party plan", "Saturday 6 PM at 14 Alder Lane, bring cake", "buy a birthday card", "Fri 5:00 PM"),
    ("trip", "Packing list - Lisbon trip", "passport, charger, rain jacket, sunscreen", "renew passport", "Wed 9:00 AM"),
    ("garden", "Garden to-do", "prune roses, order mulch, fix the gate latch", "order mulch", "Sat 10:00 AM"),
    ("moving", "Moving checklist", "box books, cancel internet, forward mail", "call the bank", "Mon 4:00 PM"),
    ("apartment", "Apartment search", "Shortlist: Maple St 2BR, Harbor View studio", "email the landlord", "Thu 12:00 PM"),
    ("falcon", "Project Falcon notes", "Kickoff done. Next: vendor shortlist by Friday", "send the invoice", "Tue 3:00 PM")]
NR_TPL = ["what do I have in my notes and reminders about {t}", "check notes and reminders for {t}", "anything on {t} in my notes or reminders?",
          "look through my notes and my reminders for {t}", "pull up what I wrote down about {t} and any reminders on it"]


@family("compound", "cmp_notes_rem", weight=6, failure=("multi_source_partial", "compound_never_completes"))
def f_cmp_notes_rem(r, c, now):
    key, ntitle, nbody, rem, due = r.choice(NR_PAIRS[:4])
    tpl = r.choice(NR_TPL)
    c.user(messy(r, tpl.format(t=key)))
    c.discover(["notes", "calendar"], ["search_notes", "search_reminders"])
    c.calls([("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}"),
             ("search_reminders", {"query": key, "scope": "all"}, f"1 active reminder: {rem} (due {due})")])
    c.final(f"Notes: “{ntitle}” — {nbody}.\nReminders: {rem}, due {due}.")
    return {"template": NR_TPL.index(tpl), "slots": {"topic": key}}


# ---- C6 weather + calendar

WC_TPL = ["what's the weather in {city} {d} and do I have anything outdoors on my calendar", "weather {d} in {city}? also what's on my schedule",
          "is it going to rain {d} in {city}, and what am I doing that day?", "{city} forecast for {d} plus my calendar for that day"]


@family("compound", "cmp_weather_cal", weight=8, failure=("multi_source_partial", "compound_never_completes"), web=True)
def f_cmp_weather_cal(r, c, now):
    cq, cname = r.choice(CITIES)
    day = pick_day(r, now, kinds=("today", "tomorrow")) if r.random() < 0.8 else pick_day(r, now, kinds=("weekday",))
    tpl = r.choice(WC_TPL)
    c.user(messy(r, tpl.format(city=cname, d=day.spoken), protect=(cname,)))
    c.discover(["calendar", "public_web"], ["get_upcoming", "get_weather"])
    wres, rain, (hi, lo, cond, pct) = weather_result(r, cq, day)
    evs = make_day(r, day.date, r.randint(1, 3), hmin=max(8, min(16, now.hour + 1)) if day.kind == "today" else 8, hmax=19)
    c.calls([("get_weather", {"location": cq, "period": day.period}, wres),
             ("get_upcoming", {"period": day.period}, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))])
    c.final(f"{cname}: {cond.lower()}, high {hi}F / low {lo}F, {pct}% chance of rain.\nYour calendar: " +
            ", ".join(f"{e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") for e in evs) + ".")
    return {"template": WC_TPL.index(tpl), "slots": {"city": cname, "rain": rain}}


# ---- C7 email -> calendar event

MAIL_EVENTS = [
    ("Dentist reschedule", "Brightside Dental", "Dentist appointment", "Brightside Dental", 45),
    ("Workshop schedule", "Camila Reyes", "Workshop", "Studio 3", 120),
    ("Team lunch", "Elena Petrov", "Team lunch", "Cafe Verde", 60),
    ("Parent-teacher night", "Zoe Tanaka", "Parent-teacher night", "Lincoln Elementary", 60),
    ("Car service confirmation", "Garage on 5th", "Car service", "Garage on 5th", 60),
    ("Book club meeting", "Ruth Abernathy", "Book club", "Ruth's place", 90)]
ME_TPL = ["add the {subj} appointment from my email to my calendar", "my {subj} email has a time in it - put it on the calendar",
          "put the {subj} thing from {s}'s email on my calendar", "create a calendar event from the {subj} email",
          "the {subj} email from {s} has the details; add it to my calendar", "calendar the time from that {subj} email"]


@family("compound", "cmp_mail_event", weight=9, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_mail_event(r, c, now):
    subj, s, title, loc, dur = r.choice(MAIL_EVENTS[:4])
    tpl = r.choice(ME_TPL)
    d = now.date() + dt.timedelta(days=r.randint(2, 9))
    tm = pick_time(r, 9, 17, minutes=(0, 30))
    c.user(messy(r, tpl.format(subj=subj.lower(), s=first(s)), protect=(subj.lower(), first(s))))
    c.discover(["mail", "calendar"], ["add_calendar_event", "view_emails"])
    mid = _msgid(r)
    body = f"Confirmed for {dshort(d)} at {tm.long}. Location: {loc}."
    c.call("view_emails", {"query": subj, "count": 2, "strict_match": True},
           f"1 email:\nFrom: {s} <{email_for(s)}>\nDate: {dshort(now.date())}\nSubject: {subj}\nMessage-ID: {mid}\nBody: {body}")
    c.call("add_calendar_event", {"title": title, "when_iso": iso(d, tm.h, tm.m), "duration_min": dur, "location": loc},
           f"Added “{title}” to your calendar for {dshort(d)}, {tm.long}.")
    c.final(r.choice([f"Added “{title}” to your calendar for {dshort(d)} at {tm.long} ({loc}).",
                      f"Done — {title} is on your calendar, {dshort(d)} at {tm.long}, at {loc}."]))
    return {"template": ME_TPL.index(tpl), "slots": {"subject": subj}}


# ---- C8 email -> reminder

MR_TPL = [("find the email from {s} about {subj} and remind me to reply {d}", "Reply to {fs} about {subj}"),
          ("remind me {d} at {t} to answer {s}'s {subj} email", "Answer {fs}'s {subj} email"),
          ("set a reminder to respond to the {subj} email from {s} {d}", "Respond to the {subj} email from {fs}"),
          ("{s} emailed about {subj} - remind me {d} at {t} to get back to them", "Get back to {fs} about {subj}")]


@family("compound", "cmp_mail_reminder", weight=7, failure=("compound_never_completes",))
def f_cmp_mail_reminder(r, c, now):
    s, subj = r.choice(EMAIL_SUBJ_BANK)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    tm = pick_time(r, 8, 18, minutes=(0, 30))
    tpl, title_t = r.choice(MR_TPL)
    c.user(messy(r, tpl.format(s=first(s), subj=subj.lower(), d=day.spoken, t=tm.spoken), protect=(first(s), subj.lower())))
    c.discover(["mail", "calendar"], ["add_reminder", "view_emails"])
    mid = _msgid(r)
    c.call("view_emails", {"query": subj, "count": 2, "strict_match": True}, _mail_listing(s, subj, mid, dshort(now.date()), "Could you reply when you can?"))
    title = title_t.format(fs=first(s), subj=subj)
    c.call("add_reminder", {"title": title, "when_iso": iso(day.date, tm.h, tm.m)}, f"Reminder set: \u201c{title}\u201d \u2014 {dshort(day.date)}, {tm.long} in Apple Reminders and Wisp.")
    c.final(f"Found {first(s)}'s email and set a reminder: \u201c{title}\u201d for {dshort(day.date)} at {tm.long}.")
    return {"template": [t for t, _ in MR_TPL].index(tpl), "slots": {"subject": subj}}


# ---- C9 weather -> reminder (conditional)

WR_TPL = [("if it's supposed to rain in {city} {d}, remind me to take an umbrella", "Take an umbrella"),
          ("remind me to bring an umbrella {d} only if rain is forecast for {city}", "Bring an umbrella"),
          ("check {city}'s weather for {d} - if rain, set a reminder to pack a rain jacket", "Pack a rain jacket"),
          ("if {city} looks wet {d}, add a reminder for an umbrella", "Umbrella")]


@family("compound", "cmp_weather_remind", weight=6, failure=("conditional_action_ignored", "compound_never_completes"), web=True)
def f_cmp_weather_remind(r, c, now):
    cq, cname = r.choice(CITIES)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    rain = r.random() < 0.55
    tpl, title = r.choice(WR_TPL)
    c.user(messy(r, tpl.format(city=cname, d=day.spoken), protect=(cname,)))
    c.discover(["public_web", "calendar"], ["add_reminder", "get_weather"])
    wres, _, (hi, lo, cond, pct) = weather_result(r, cq, day, rainy=rain)
    c.call("get_weather", {"location": cq, "period": day.period}, wres)
    if rain:
        c.call("add_reminder", {"title": title, "when_iso": iso(day.date, 8, 0)},
               f"Reminder set: \u201c{title}\u201d \u2014 {dshort(day.date)}, 8 AM in Apple Reminders and Wisp.")
        c.final(f"Rain is forecast in {cname} ({cond.lower()}, {pct}% chance), so I set a reminder \u201c{title}\u201d for {dshort(day.date)} at 8 AM.")
    else:
        c.final(f"No rain expected in {cname} {day.label.lower() if day.kind != 'date' else 'on ' + WEEKDAYS[day.date.weekday()]} ({cond.lower()}, {pct}% chance), so I didn't set a reminder.")
    return {"template": [t for t, _ in WR_TPL].index(tpl), "slots": {"city": cname, "rain": rain}}


# ---- C10 notes -> text

NT_THINGS = [("the address for the party", "party", "Birthday party plan", "Saturday 6 PM at 14 Alder Lane, bring cake", "The party is Saturday 6 PM at 14 Alder Lane."),
             ("the shortlist from my apartment search note", "apartment", "Apartment search", "Shortlist: Maple St 2BR, Harbor View studio", "My shortlist: Maple St 2BR and Harbor View studio."),
             ("the movie list", "movie", "Movie list", "Past Lives; Anatomy of a Fall; Perfect Days", "Movie ideas: Past Lives, Anatomy of a Fall, Perfect Days."),
             ("my workout plan", "workout", "Workout plan", "Mon legs, Wed back, Fri shoulders", "Workout plan: Mon legs, Wed back, Fri shoulders."),
             ("the book ideas", "book", "Book ideas", "The Overstory; Piranesi; a cookbook for beginners", "Book ideas: The Overstory, Piranesi, and a beginner cookbook.")]
NT_TPL = ["text {n} {th} from my notes", "send {n} {th} in my notes by text", "grab {th} from my notes and message it to {n}",
          "{n} asked for {th} - find it in my notes and text it over", "look in my notes for {th} and text {n}"]


@family("compound", "cmp_note_text", weight=7, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_note_text(r, c, now):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    th, key, ntitle, nbody, body = r.choice(NT_THINGS)
    tpl = r.choice(NT_TPL)
    c.user(messy(r, tpl.format(n=said, th=th), protect=(said,)))
    c.discover(["notes", "messages_contacts"], ["lookup_contact", "search_notes", "send_message"])
    c.calls([("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}"), ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))])
    body = nbody
    c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
    f = nm if nm in RELATIVES else first(nm)
    c.final(f"Texted {f} from your note \u201c{ntitle}\u201d: {Q(body)}")
    return {"template": NT_TPL.index(tpl), "slots": {"note": ntitle}}


# ---- C11 add event + text

EA_TPL = ["add {a} {d} at {t} to my calendar and text {n} about it", "put {a} on my calendar for {d} at {t}, then let {n} know",
          "schedule {a} {d} {t} and tell {n} by text", "calendar {a} {d} at {t} and send {n} a heads up"]


@family("compound", "cmp_event_text", weight=9, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_event_text(r, c, now):
    nm = pick_contact(r, 0.05)
    said, _ = name_forms(r, nm)
    day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday")))
    tm = pick_time(r, 9, 19, minutes=(0, 30))
    title = r.choice(["Dinner with the Hartleys", "Hike at Ridgeline Park", "Game night", "Lunch at Osteria Blu", "Movie night",
                      "Brunch", "Study session", "Birthday drinks"])
    tpl = r.choice(EA_TPL)
    a_ = title.lower() if r.random() < 0.5 else title
    c.user(messy(r, tpl.format(a=a_, d=day.spoken, t=tm.spoken, n=said), protect=(said,)))
    c.discover(["calendar", "messages_contacts"], ["add_calendar_event", "lookup_contact", "send_message"])
    c.calls([("add_calendar_event", {"title": title, "when_iso": iso(day.date, tm.h, tm.m), "duration_min": 60},
              f"Added \u201c{title}\u201d to your calendar for {dshort(day.date)}, {tm.long}."),
             ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))])
    body = fix_caps(f"{a_} {day.spoken} at {tm.spoken}.")
    c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
    f = nm if nm in RELATIVES else first(nm)
    c.final(f"Added {title} for {dshort(day.date)} at {tm.long} and texted {f}: {Q(body)}")
    return {"template": EA_TPL.index(tpl), "slots": {"title": title, "contact": nm}}


# ---- C12 two writes

TW_TPL = ["remind me to {rem} at {t1} and also put {a} on my calendar {d} at {t2}", "two things: a reminder to {rem} {d} at {t1}, and {a} on the calendar {d} at {t2}",
          "set a reminder to {rem} at {t1} {d} and add {a} to my calendar at {t2} the same day"]


@family("compound", "cmp_two_writes", weight=6, failure=("compound_never_completes",))
def f_cmp_two_writes(r, c, now):
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    t1 = pick_time(r, 8, 11, minutes=(0, 30))
    t2 = pick_time(r, 13, 19, minutes=(0, 30))
    rem = r.choice(REMINDER_BANK)
    ev = r.choice([x for x in EVENT_BANK if x[1] is not None or True])
    p = first(r.choice(PEOPLE))
    title = ev[0].format(p=p)
    tpl = r.choice(TW_TPL)
    c.user(messy(r, tpl.format(rem=rem, a=title.lower(), d=day.spoken, t1=t1.spoken, t2=t2.spoken), protect=(rem,)))
    c.discover(["calendar"], ["add_calendar_event", "add_reminder"])
    c.calls([("add_reminder", {"title": rem[0].upper() + rem[1:], "when_iso": iso(day.date, t1.h, t1.m)},
              f"Reminder set: “{rem[0].upper() + rem[1:]}” — {dshort(day.date)}, {t1.long} in Apple Reminders and Wisp."),
             ("add_calendar_event", {"title": title, "when_iso": iso(day.date, t2.h, t2.m), "duration_min": 60},
              f"Added “{title}” to your calendar for {dshort(day.date)}, {t2.long}.")])
    c.final(f"Both done for {dshort(day.date)}: a reminder to {rem} at {t1.long}, and {title} on your calendar at {t2.long}.")
    return {"template": TW_TPL.index(tpl), "slots": {"reminder": rem, "event": title}}


# ---- C13 progressive discovery (second discovery call)

PG_TPL = [("check my calendar {d} and text {n} that I'm free in the afternoon if it's open", "I'm free in the afternoon."),
          ("look at {d} on my calendar - if the afternoon is open, message {n} that I'm around", "I'm around."),
          ("see what I have {d}; if I've got nothing after lunch, text {n} asking if they want to grab coffee", "Want to grab coffee?")]
PG_DEV = [("if {d} is light on my calendar, send {n} a text saying I'm free for coffee - take a look at the day first", "I'm free for coffee."),
          ("peek at my schedule for {d}; if the afternoon's free text {n} that I'm around", "I'm around.")]


def _progressive(r, c, now, tpls):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    tpl, body = r.choice(tpls)
    c.user(messy(r, tpl.format(n=said, d=day.spoken), protect=(said,)))
    c.discover(["calendar"], ["get_upcoming"])
    morning = r.random() < 0.8
    evs = [make_event(r, day.date, h=r.randint(9, 11), m=0, dur=60)] if morning else make_day(r, day.date, 2, hmin=13, hmax=17)
    c.call("get_upcoming", {"period": day.period}, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))
    f = nm if nm in RELATIVES else first(nm)
    if morning:
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        c.final(f"Your afternoon is open (only {evs[0].title} at {evs[0].tstr}), so I texted {f}: {Q(body)}")
    else:
        c.final(f"Your afternoon isn't open: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + f". I didn't text {f}.")
    return {"template": tpls.index((tpl, body)), "slots": {"open": morning, "contact": nm}}


@family("compound", "cmp_progressive", weight=7, failure=("second_discovery_misuse", "compound_never_completes"))
def f_cmp_progressive(r, c, now):
    return _progressive(r, c, now, PG_TPL)


@family("compound", "cmp_progressive_dev", split="dev", weight=3, failure=("second_discovery_misuse", "compound_never_completes"))
def f_cmp_progressive_dev(r, c, now):
    return _progressive(r, c, now, PG_DEV)


# ---- dev: parallel read pairs in a different register

DV_PAIR = [("mail_cal", "walk me through today's inbox and agenda"), ("mail_cal", "I want the mail digest and the day's schedule together"),
           ("notes_rem", "what have I jotted down and set reminders for about {t}?"), ("notes_rem", "{t}: notes plus reminders, show me both")]


@family("compound", "cmp_dev_pair", split="dev", weight=6, failure=("multi_source_partial", "compound_never_completes"))
def f_cmp_dev_pair(r, c, now):
    kind, tpl = r.choice(DV_PAIR)
    if kind == "mail_cal":
        c.user(messy(r, tpl))
        c.discover(["calendar", "mail"], ["get_upcoming", "summarize_emails"])
        n = r.randint(2, 3)
        dig, items = email_digest(r, n, "today")
        evs = make_day(r, now.date(), r.randint(1, 3), hmin=max(8, min(17, now.hour + 1)), hmax=19)
        c.calls([("get_upcoming", {"period": "today"}, agenda_result(now, "today", evs)), ("summarize_emails", {"period": "today"}, dig)])
        c.final("Agenda today: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".\nMail: " + "; ".join(f"{s} ({subj})" for s, subj, _ in items) + ".")
    else:
        key, ntitle, nbody, rem, due = r.choice(NR_PAIRS[4:])
        c.user(messy(r, tpl.format(t=key)))
        c.discover(["notes", "calendar"], ["search_notes", "search_reminders"])
        c.calls([("search_reminders", {"query": key, "scope": "all"}, f"1 active reminder: {rem} (due {due})"),
                 ("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}")])
        c.final(f"Reminder: {rem}, due {due}.\nNote “{ntitle}”: {nbody}.")
    return {"template": DV_PAIR.index((kind, tpl)), "slots": {"kind": kind}}


DV_MW = ["{s} sent details in the {subj} email - turn that into a calendar entry", "use the {subj} message from {s} to make a calendar event",
         "make an event out of the time in {s}'s {subj} email"]


@family("compound", "cmp_dev_mail_write", split="dev", weight=5, failure=("compound_never_completes", "wrong_chain_order"))
def f_cmp_dev_mail_write(r, c, now):
    subj, s, title, loc, dur = r.choice(MAIL_EVENTS[4:])
    tpl = r.choice(DV_MW)
    d = now.date() + dt.timedelta(days=r.randint(2, 9))
    tm = pick_time(r, 9, 17, minutes=(0, 30))
    c.user(messy(r, tpl.format(subj=subj.lower(), s=first(s)), protect=(subj.lower(), first(s))))
    c.discover(["mail", "calendar"], ["add_calendar_event", "view_emails"])
    mid = _msgid(r)
    c.call("view_emails", {"query": subj, "count": 2, "strict_match": True},
           f"1 email:\nFrom: {s} <{email_for(s)}>\nDate: {dshort(now.date())}\nSubject: {subj}\nMessage-ID: {mid}\nBody: Confirmed for {dshort(d)} at {tm.long}. Location: {loc}.")
    c.call("add_calendar_event", {"title": title, "when_iso": iso(d, tm.h, tm.m), "duration_min": dur, "location": loc},
           f"Added “{title}” to your calendar for {dshort(d)}, {tm.long}.")
    c.final(f"Created “{title}” for {dshort(d)} at {tm.long} at {loc}, based on {first(s)}'s email.")
    return {"template": DV_MW.index(tpl), "slots": {"subject": subj}}


# ---- C14 three sources, three families

TR_TPL = ["weather in {city} today, what's on my calendar, and do I have unread email", "give me a morning rundown: {city} weather, my schedule and unread mail",
          "I'm heading out - {city} forecast, today's calendar and any unread email please", "check three things: the weather in {city}, my agenda today, and unread emails"]


@family("compound", "cmp_triple", weight=5, failure=("multi_source_partial", "compound_never_completes"), web=True)
def f_cmp_triple(r, c, now):
    cq, cname = r.choice(CITIES)
    tpl = r.choice(TR_TPL)
    c.user(messy(r, tpl.format(city=cname), protect=(cname,)))
    c.discover(["calendar", "mail", "public_web"], ["get_upcoming", "get_weather", "summarize_emails"])
    day = Day("today", now.date(), "today", now)
    wres, rain, (hi, lo, cond, pct) = weather_result(r, cq, day)
    evs = make_day(r, now.date(), r.randint(1, 3), hmin=max(8, min(17, now.hour + 1)), hmax=19)
    n = r.randint(1, 3)
    dig, items = email_digest(r, n, "unread", unread_only=True)
    c.calls([("get_weather", {"location": cq, "period": "today"}, wres), ("get_upcoming", {"period": "today"}, agenda_result(now, "today", evs)),
             ("summarize_emails", {"unread": True}, dig)])
    c.final(f"{cname}: {cond.lower()}, high {hi}F / low {lo}F, {pct}% chance of rain.\nCalendar: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) +
            f".\nUnread email ({n}): " + "; ".join(f"{s} - {subj}" for s, subj, _ in items) + ".")
    return {"template": TR_TPL.index(tpl), "slots": {"city": cname, "n_mail": n}}


# ================================================================ STOP-AFTER-SUCCESS / NO-LOOP

def dw(day):
    """Spoken-result day phrase."""
    if day.kind in ("today", "tomorrow"):
        return day.kind
    return "on " + WEEKDAYS[day.date.weekday()]


def _bare_hour(t):
    return t.h if t.h > 12 else t.h


AV_TPL = [  # (uses_day, template)
    (True, "am i free at {t} {d}"), (True, "am i busy at {t} {d}"), (True, "do i have anything going on {d} at {t}"),
    (True, "is {t} {d} open on my calendar"), (True, "can I fit something in {d} at {t}"), (True, "is my calendar clear {d} around {t}"),
    (True, "free {d} at {t}?"), (False, "am i busy at {t}"), (False, "am i free at {t}"), (False, "do I have time at {t} today"),
    (True, "got any conflicts {d} at {t}"), (True, "would {t} {d} clash with anything")]
AV_DEV = [
    (True, "is there room in my day {d} for something at {t}"), (True, "any meeting overlapping {t} {d}"),
    (False, "is {t} taken today"), (True, "check if I'm booked {d} at {t}")]


def _avail(r, c, now, tpls):
    uses_day, tpl = r.choice(tpls)
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")) if uses_day else pick_day(r, now, kinds=("today",))
    lo = 9
    if day.kind == "today":
        lo = min(20, now.hour + 1)
    tm = pick_time(r, lo, 20, minutes=(0,))
    free = r.random() < 0.5
    c.user(messy(r, tpl.format(t=tm.spoken, d=day.spoken)))
    c.discover(["calendar"], ["find_free_time"])
    wins = [(tm.h * 60, (tm.h + 1) * 60)] if free else []
    c.call("find_free_time", {"period": day.period, "minutes": 60, "day_start": tm.h, "day_end": tm.h + 1},
           free_result(60, tm.h, tm.h + 1, wins, day.label))
    if free:
        c.final(r.choice([f"Yes, you're free at {tm.long} {dw(day)}.", f"You're clear at {tm.long} {dw(day)} - nothing scheduled then.",
                          f"Free - {tm.long} {dw(day)} is open."]))
    else:
        c.final(r.choice([f"No, you're busy at {tm.long} {dw(day)} - there's no free hour then.", f"That slot is taken: {tm.long} {dw(day)} isn't open.",
                          f"You've got something at {tm.long} {dw(day)}, so you're not free."]))
    return {"template": tpls.index((uses_day, tpl)), "slots": {"free": free}}


@family("stop", "stp_avail_time", weight=16, failure=("loop_exact", "loop_tool", "availability_wrong_tool"))
def f_stp_avail_time(r, c, now):
    return _avail(r, c, now, AV_TPL)


@family("stop", "stp_avail_time_dev", split="dev", weight=6, failure=("loop_exact", "loop_tool", "availability_wrong_tool"))
def f_stp_avail_time_dev(r, c, now):
    return _avail(r, c, now, AV_DEV)


WIN_TPL = [("am i free {d} {part}", None), ("what's open {d} {part}", None), ("any gaps in my {part} {d}", None),
           ("do I have time for a {m} minute call {d}", 1), ("when could I fit a {m} minute meeting {d}", 1),
           ("find me an open slot {d} {part}", None), ("got a free hour {d} {part}", 60)]
PARTS = {"morning": (9, 12), "afternoon": (12, 17), "evening": (17, 21)}


@family("stop", "stp_avail_window", weight=12, failure=("loop_exact", "loop_tool", "availability_wrong_tool"))
def f_stp_avail_window(r, c, now):
    tpl, mode = r.choice(WIN_TPL)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    part = r.choice(list(PARTS))
    ds, de = PARTS[part] if mode != 1 else (9, 18)
    m = r.choice([15, 30, 45, 60]) if mode == 1 else (60 if mode == 60 else r.choice([30, 60]))
    c.user(messy(r, tpl.format(d=day.spoken, part=part, m=m)))
    c.discover(["calendar"], ["find_free_time"])
    evs = make_day(r, day.date, r.randint(1, 3), hmin=ds, hmax=max(ds, de - 1))
    wins = free_windows(evs, ds, de, m)
    c.call("find_free_time", {"period": day.period, "minutes": m, "day_start": ds, "day_end": de}, free_result(m, ds, de, wins, day.label))
    if wins:
        spans = ", ".join(f"{t12(a // 60, a % 60)} to {t12(b // 60, b % 60)}" for a, b in wins)
        c.final(f"{cap(dw(day))} you're free {spans}.")
    else:
        c.final(f"No - there's no {m}+ minute gap between {t12(ds)} and {t12(de)} {dw(day)}.")
    return {"template": 0, "slots": {"part": part, "minutes": m, "n_windows": len(wins)}}


FN_TPL = ["what's my first meeting {d}", "when does my day start {d}", "what's the first thing on my calendar {d}", "what's my last meeting {d}",
          "when am I done {d}", "what do I have first thing {d}", "what's up first {d} on the calendar", "what's on the schedule at the end of my day {d}"]
FN_DEV = ["what kicks off my day {d}", "which meeting wraps up my day {d}", "what's the earliest thing I'm doing {d}"]


def _first_last(r, c, now, tpls):
    tpl = r.choice(tpls)
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
    evs = make_day(r, day.date, r.randint(2, 4))
    last = ("last" in tpl or "done" in tpl or "end" in tpl or "wraps" in tpl)
    c.user(messy(r, tpl.format(d=day.spoken)))
    c.discover(["calendar"], ["get_upcoming"])
    c.call("get_upcoming", {"period": day.period}, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))
    e = evs[-1] if last else evs[0]
    if last:
        c.final(f"Your last item {dw(day)} is {e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") + f", ending around {t12((e.start + e.dur) // 60, (e.start + e.dur) % 60)}.")
    else:
        c.final(f"{cap(dw(day))} you start with {e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") + ".")
    return {"template": tpls.index(tpl), "slots": {"last": last, "n_events": len(evs)}}


@family("stop", "stp_first_last", weight=12, failure=("loop_exact", "loop_tool"))
def f_stp_first_last(r, c, now):
    return _first_last(r, c, now, FN_TPL)


@family("stop", "stp_first_last_dev", split="dev", weight=4, failure=("loop_exact", "loop_tool"))
def f_stp_first_last_dev(r, c, now):
    return _first_last(r, c, now, FN_DEV)


CNT_TPL = ["how many meetings do I have {d}", "how busy am I {d}", "how many things are on my calendar {d}", "is {d} packed or light for me",
           "count my events {d}", "how loaded is my {d}"]


@family("stop", "stp_count", weight=9, failure=("loop_exact", "derived_answer_dumped_raw"))
def f_stp_count(r, c, now):
    tpl = r.choice(CNT_TPL)
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "today"))
    n = r.randint(0, 4)
    evs = make_day(r, day.date, n, hmin=8 if day.kind != "today" else min(17, now.hour + 1))
    c.user(messy(r, tpl.format(d=day.spoken)))
    c.discover(["calendar"], ["get_upcoming"])
    c.call("get_upcoming", {"period": day.period}, agenda_result(now, day.kind if day.kind != "date" else day.period, evs))
    if n == 0:
        c.final(f"Nothing - your calendar is empty {dw(day)}.")
    else:
        c.final(f"You have {n} item{'s' if n != 1 else ''} {dw(day)}: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".")
    return {"template": CNT_TPL.index(tpl), "slots": {"n": n}}


ET_TPL = ["what time is my {a}", "when's my {a}", "when is the {a}", "what time do I have the {a}", "remind me when my {a} is", "when am I seeing the {a}",
          "do you know what time my {a} is", "what day is my {a}"]


@family("stop", "stp_event_time", weight=12, failure=("loop_exact", "loop_tool"))
def f_stp_event_time(r, c, now):
    tpl = r.choice(ET_TPL)
    d = now.date() + dt.timedelta(days=r.randint(1, 12))
    ev = make_event(r, d, h=r.randint(9, 17), m=r.choice([0, 30]))
    c.user(messy(r, tpl.format(a=ev.alias), protect=(ev.alias,)))
    c.discover(["calendar"], ["get_upcoming"])
    c.call("get_upcoming", {"days": 14, "query": ev.key}, agenda_result(now, "next 14 day(s)", [ev]))
    c.final(f"Your {ev.alias} is {WEEKDAYS[d.weekday()]}, {dshort(d).split(', ')[1]} at {ev.tstr}" + (f" ({ev.loc})." if ev.loc else "."))
    return {"template": ET_TPL.index(tpl), "slots": {"event": ev.key}}


EMP_TPL = [("cal", "anything on my calendar {d}"), ("cal", "what do I have {d}"), ("cal", "is there anything scheduled {d}"),
           ("mail", "any unread emails"), ("mail", "do I have unread mail"), ("mail", "got any new email from {s}"),
           ("mail", "did {s} email me about {subj}"), ("notes", "do I have a note about {k}"), ("notes", "find my {k} note"),
           ("rem", "do I have a reminder about {k}"), ("rem", "any reminder to {k}")]


@family("stop", "stp_empty", weight=12, failure=("loop_tool", "empty_result_retry_loop"))
def f_stp_empty(r, c, now):
    kind, tpl = r.choice(EMP_TPL)
    s, subj = r.choice(EMAIL_SUBJ_BANK)
    k = r.choice(["passport", "taxes", "gym", "invoice", "garage", "plumber", "lease", "visa", "flight", "warranty"])
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "today"))
    c.user(messy(r, tpl.format(d=day.spoken, s=first(s), subj=subj.lower(), k=k)))
    if kind == "cal":
        c.discover(["calendar"], ["get_upcoming"])
        c.call("get_upcoming", {"period": day.period}, f"Today is {dshort(now.date())}. Nothing scheduled for {day.period if day.kind == 'date' else day.kind}.")
        c.final(f"Nothing - your calendar is clear {dw(day)}.")
    elif kind == "mail":
        if "{s}" in tpl:
            c.discover(["mail"], ["view_emails"])
            c.call("view_emails", {"query": first(s), "count": 5, "strict_match": True}, f"No emails matched {first(s)!r}.")
            c.final(f"I didn't find any emails matching {first(s)}.")
        else:
            c.discover(["mail"], ["view_emails"])
            c.call("view_emails", {"unread": True, "count": 5}, "No unread emails.")
            c.final("You have no unread emails.")
    elif kind == "notes":
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": k}, f"No notes matched {k!r}.")
        c.final(f"I couldn't find a note about {k}.")
    else:
        c.discover(["calendar"], ["search_reminders"])
        c.call("search_reminders", {"query": k, "scope": "all"}, f"No active reminders match {k!r}.")
        c.final(f"No - you don't have an active reminder about {k}.")
    return {"template": EMP_TPL.index((kind, tpl)), "slots": {"kind": kind}}


EMP_DEV = [("cal", "anything booked {d}"), ("mail", "is there fresh mail I haven't read"), ("notes", "did I save anything about {k}")]


@family("stop", "stp_empty_dev", split="dev", weight=4, failure=("loop_tool", "empty_result_retry_loop"))
def f_stp_empty_dev(r, c, now):
    kind, tpl = r.choice(EMP_DEV)
    k = r.choice(["warranty", "visa", "plumber", "taxes"])
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "today"))
    c.user(messy(r, tpl.format(d=day.spoken, k=k)))
    if kind == "cal":
        c.discover(["calendar"], ["get_upcoming"])
        c.call("get_upcoming", {"period": day.period}, f"Today is {dshort(now.date())}. Nothing scheduled for {day.period if day.kind == 'date' else day.kind}.")
        c.final(f"Nothing is booked {dw(day)}.")
    elif kind == "mail":
        c.discover(["mail"], ["view_emails"])
        c.call("view_emails", {"unread": True, "count": 5}, "No unread emails.")
        c.final("No fresh mail - everything is read.")
    else:
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": k}, f"No notes matched {k!r}.")
        c.final(f"Nothing saved about {k} in your notes.")
    return {"template": EMP_DEV.index((kind, tpl)), "slots": {"kind": kind}}


ML_TPL = ["any unread emails from {s}?", "has {s} emailed me lately", "when did {s} last email me", "what did {s}'s last email say",
          "show me the latest email from {s}", "did {s} send anything today", "check if {s} replied about {subj}"]


@family("stop", "stp_mail_one", weight=11, failure=("loop_exact", "loop_tool", "derived_answer_dumped_raw"))
def f_stp_mail_one(r, c, now):
    s, subj = r.choice(EMAIL_SUBJ_BANK)
    tpl = r.choice(ML_TPL)
    c.user(messy(r, tpl.format(s=first(s), subj=subj.lower()), protect=(first(s),)))
    c.discover(["mail"], ["view_emails"])
    d = now.date() - dt.timedelta(days=r.randint(0, 3))
    body = r.choice(["Can you confirm Thursday works?", "The files are in the shared folder now.", "Thanks for yesterday - talk soon.",
                     "Reminder that the deadline is next Wednesday.", "Just checking you saw my last note."])
    args = {"query": first(s), "count": 1 if ("last" in tpl or "latest" in tpl) else 3}
    if "unread" in tpl:
        args["unread"] = True
    c.call("view_emails", args, _mail_listing(s, subj, _msgid(r), f"{dshort(d)}, {t12(r.randint(7, 17), r.choice([0, 20, 40]))}", body))
    if "when" in tpl:
        c.final(f"{first(s)}'s last email was on {dshort(d)}, subject “{subj}”.")
    elif "unread" in tpl or "replied" in tpl or "lately" in tpl or "anything today" in tpl:
        c.final(f"Yes - {first(s)} sent “{subj}” on {dshort(d)}: “{body}”")
    else:
        c.final(f"{first(s)}'s latest email, “{subj}” ({dshort(d)}), says: “{body}”")
    return {"template": ML_TPL.index(tpl), "slots": {"sender": s}}


# ================================================================ SINGLE-TOOL READS

# ---- notes
NOTES_TPL = ["find my note about {k}", "what did I write down about {k}", "search my notes for {k}", "pull up the {k} note", "check notes for {k}",
             "what's in my {k} note", "did I jot anything down about {k}", "look in notes: {k}"]
NOTES_DEV = ["can you dig up whatever I saved on {k}", "notes on {k} - what do they say"]


NOTE_DEV_KEYS = {"soup", "workout", "movie"}


def _note_read(r, c, now, tpls):
    ntitle, nbody, key = r.choice([n for n in NOTE_BANK if (n[2] in NOTE_DEV_KEYS) == (CUR["split"] == "dev")])
    tpl = r.choice(tpls)
    c.user(messy(r, tpl.format(k=key)))
    c.discover(["notes"], ["search_notes"])
    c.call("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}")
    c.final(r.choice([f"Your note “{ntitle}” says: {nbody}.", f"Found “{ntitle}”: {nbody}.", f"“{ntitle}” - {nbody}."]))
    return {"template": tpls.index(tpl), "slots": {"note": ntitle}}


@family("reads", "rd_notes", weight=10, failure=("direct_real_tool_without_discovery",))
def f_rd_notes(r, c, now):
    return _note_read(r, c, now, NOTES_TPL)


# ---- reminders
RM_TPL = [("search", "do I have a reminder about {k}"), ("search", "when's my reminder to {k}"), ("search", "is there a reminder for {k}"),
          ("search", "check my reminders for {k}"), ("today", "what reminders do I have today"), ("today", "any reminders for today"),
          ("today", "what's on my reminder list today"), ("tomorrow", "what reminders are set for tomorrow"), ("today", "reminders today?")]


@family("reads", "rd_reminders", weight=9, failure=("direct_real_tool_without_discovery", "loop_tool"))
def f_rd_reminders(r, c, now):
    kind, tpl = r.choice(RM_TPL)
    rem = r.choice(REMINDER_BANK)
    key = rem.split()[-1] if len(rem.split()) > 1 else rem
    c.user(messy(r, tpl.format(k=rem if "to {k}" in tpl or "for {k}" in tpl else key)))
    c.discover(["calendar"], ["get_upcoming"] if kind != "search" else ["search_reminders"])
    if kind == "search":
        when = (now.date() + dt.timedelta(days=r.randint(0, 5)))
        h = r.randint(8, 18)
        arg = rem if ("to {k}" in tpl or "for {k}" in tpl) else key
        c.call("search_reminders", {"query": arg, "scope": "all"}, f"1 active reminder: {rem} (due {dshort(when)}, {t12(h, 0)})")
        c.final(f"Yes - “{rem}” is due {dshort(when)} at {t12(h, 0)}.")
    else:
        d = now.date() + dt.timedelta(days=1 if kind == "tomorrow" else 0)
        rems = [(d, r.randint(max(8, now.hour + 1) if kind == "today" else 8, 20), 0, x) for x in r.sample(REMINDER_BANK, r.randint(1, 3))]
        c.call("get_upcoming", {"period": kind}, agenda_result(now, kind, [], rems))
        c.final(f"Reminders {kind}: " + "; ".join(f"{x[3]} at {t12(x[1], x[2])}" for x in sorted(rems, key=lambda x: x[1])) + ".")
    return {"template": RM_TPL.index((kind, tpl)), "slots": {"kind": kind}}


# ---- mail summaries / find
MS_TPLS = [("today", "summarize my email today"), ("today", "what's in my inbox today"), ("week", "give me the week's emails in a nutshell"),
           ("unread", "what unread email do I have"), ("unread", "anything unread in my inbox"), ("yesterday", "what came in yesterday by email"),
           ("triage", "what actually matters in my inbox"), ("triage", "which emails need my attention"), ("today", "inbox rundown please")]


@family("reads", "rd_mail_summary", weight=10, failure=("direct_real_tool_without_discovery", "derived_answer_dumped_raw"))
def f_rd_mail_summary(r, c, now):
    kind, tpl = r.choice(MS_TPLS)
    c.user(messy(r, tpl))
    n = r.randint(2, 4)
    if kind == "triage":
        c.discover(["mail"], ["triage_inbox"])
        picks = r.sample(EMAIL_SUBJ_BANK, 3)
        c.call("triage_inbox", {}, "Ranked senders (header-only):\n" + "\n".join(f"{i}) {s}: {subj}" for i, (s, subj) in enumerate(picks, 1)))
        c.final("Top of your inbox by priority: " + "; ".join(f"{s} ({subj})" for s, subj in picks) + ". That ranking uses subjects and senders only, not message bodies.")
    else:
        c.discover(["mail"], ["summarize_emails"])
        arg = {"today": {"period": "today"}, "week": {"period": "this week"}, "unread": {"unread": True}, "yesterday": {"day": "yesterday"}}[kind]
        lab = {"today": "today", "week": "this week", "unread": "unread", "yesterday": "yesterday"}[kind]
        dig, items = email_digest(r, n, lab, unread_only=(kind == "unread"))
        c.call("summarize_emails", arg, dig)
        c.final(f"{n} email{'s' if n != 1 else ''} {lab}: " + "; ".join(f"{s} - {subj}" for s, subj, _ in items) + ".")
    return {"template": MS_TPLS.index((kind, tpl)), "slots": {"kind": kind}}


MF_TPL = ["find the email about {subj}", "pull up the {subj} email", "search my mail for {subj}", "which email mentioned {subj}", "show me that {subj} email",
          "look for an email on {subj}"]


@family("reads", "rd_mail_find", weight=8, failure=("direct_real_tool_without_discovery",))
def f_rd_mail_find(r, c, now):
    s, subj = r.choice(EMAIL_SUBJ_BANK)
    tpl = r.choice(MF_TPL)
    c.user(messy(r, tpl.format(subj=subj.lower())))
    c.discover(["mail"], ["view_emails"])
    d = now.date() - dt.timedelta(days=r.randint(1, 6))
    body = r.choice(["Please review before Friday.", "Here are the numbers we discussed.", "Can you confirm the time?", "Sharing the draft for your thoughts."])
    c.call("view_emails", {"query": subj, "count": 3, "strict_match": True}, _mail_listing(s, subj, _msgid(r), dshort(d), body))
    c.final(f"Found it: {first(s)} sent “{subj}” on {dshort(d)}. It says: “{body}”")
    return {"template": MF_TPL.index(tpl), "slots": {"subject": subj}}


# ---- messages
MG_TPL = [("view", "what did {n} text me"), ("view", "show me my recent messages with {n}"), ("view", "did {n} message me"),
          ("view", "check my texts from {n}"), ("sum", "summarize my texts today"), ("sum", "catch me up on my messages today"),
          ("unread", "any unread texts"), ("unread", "what unread messages do I have"), ("view", "what was the last thing {n} said")]


@family("reads", "rd_messages", weight=10, failure=("direct_real_tool_without_discovery",))
def f_rd_messages(r, c, now):
    kind, tpl = r.choice(MG_TPL)
    nm = pick_contact(r, 0.15)
    said, _ = name_forms(r, nm)
    c.user(messy(r, tpl.format(n=said), protect=(said,)))
    c.discover(["messages_contacts"], ["view_messages"] if kind == "view" else ["summarize_messages"])
    f = nm if nm in RELATIVES else first(nm)
    if kind == "view":
        mres, items = messages_result(r, nm, n=r.randint(1, 3))
        cnt = 1 if "last thing" in tpl else 5
        c.call("view_messages", {"conversation": _name_arg(said, nm), "count": cnt}, mres)
        c.final(f"{f}'s recent messages: " + " / ".join(f"{s}: “{t}”" for s, t in items) + ".")
    elif kind == "sum":
        c.call("summarize_messages", {"period": "today"}, f"3 conversations today:\n- {first(r.choice(PEOPLE))}: plans for Saturday\n- Group chat: photo swap\n- {f}: asked to call later")
        c.final("Today in Messages: a Saturday plans chat, a group photo swap, and " + f"{f} asking you to call later.")
    else:
        c.call("summarize_messages", {"unread": True}, f"2 unread conversations:\n- {f}: asked about dinner\n- {first(r.choice(PEOPLE))}: sent a photo")
        c.final(f"Two unread conversations: {f} asked about dinner, and another person sent a photo.")
    return {"template": MG_TPL.index((kind, tpl)), "slots": {"kind": kind}}


# ---- files
FILE_BANK = [("~/Downloads", ["invoice_2048.pdf", "trip-photos.zip", "resume_v3.docx", "Screenshot 2026-10-09.png"]),
             ("~/Desktop", ["notes.txt", "budget.xlsx", "sketch.png"]),
             ("~/Documents", ["lease_2026.pdf", "taxes", "Recipes", "meeting-minutes.docx"])]
FL_TPL = [("list", "what's in my {f} folder"), ("list", "list the files in {f}"), ("list", "show me what's sitting in {f}"),
          ("find", "find the pdf called {q}"), ("find", "where's my {q} file"), ("find", "locate {q} on my mac"),
          ("read", "open {p} and tell me what it says"), ("read", "read {p} for me")]


@family("reads", "rd_files", weight=9, failure=("direct_real_tool_without_discovery",))
def f_rd_files(r, c, now):
    kind, tpl = r.choice(FL_TPL)
    folder, names = r.choice(FILE_BANK)
    fname = folder.split("/")[-1]
    q = r.choice(["lease", "invoice", "resume", "budget", "itinerary"])
    c.user(messy(r, tpl.format(f=fname if r.random() < 0.5 else folder, q=q, p=f"~/Documents/{q}-notes.txt")))
    c.discover(["files"], {"list": ["list_dir"], "find": ["find_files"], "read": ["read_file"]}[kind])
    if kind == "list":
        c.call("list_dir", {"path": folder}, f"{folder} ({len(names)} items):\n" + "\n".join(f"- {n}" for n in names))
        c.final(f"{folder} has {len(names)} items: " + ", ".join(names) + ".")
    elif kind == "find":
        path = f"~/Documents/{q}_2026.pdf"
        c.call("find_files", {"query": q, "kind": "pdf"}, f"1 match:\n{path} (modified {dshort(now.date() - dt.timedelta(days=9))})")
        c.final(f"Found it: {path}.")
    else:
        path = f"~/Documents/{q}-notes.txt"
        content = r.choice(["Call supplier Monday. Confirm delivery window. Ask about the discount.", "Draft outline: intro, scope, timeline, open questions."])
        c.call("read_file", {"path": path}, content)
        c.final(f"{path} says: {content}")
    return {"template": FL_TPL.index((kind, tpl)), "slots": {"kind": kind}}


# ---- web & reference
WEB_BANK = [
    ("how to remove a red wine stain from carpet", "remove red wine stain carpet",
     [("How to Remove Red Wine From Carpet", "homecare.example.invalid", "Blot immediately, then cover with salt and rinse with cold water and dish soap.")],
     "Blot the spill first, then cover it with salt and rinse with cold water and a little dish soap, according to the top result."),
    ("the best time to visit lisbon", "best time to visit Lisbon",
     [("When to Visit Lisbon", "travelnotes.example.invalid", "Spring (April-June) and early autumn have mild weather and fewer crowds.")],
     "The top result suggests spring (April-June) and early autumn for mild weather and smaller crowds."),
    ("how long to boil an egg for a jammy yolk", "jammy egg boiling time",
     [("Perfect Jammy Eggs", "kitchenlab.example.invalid", "Boil for about 6.5 to 7 minutes, then cool in ice water.")],
     "About 6.5 to 7 minutes, then into ice water, per the top result."),
    ("what's a good beginner houseplant", "easy houseplants for beginners",
     [("Easy Houseplants", "leafy.example.invalid", "Pothos, snake plant and ZZ plant tolerate low light and irregular watering.")],
     "Pothos, snake plant and ZZ plant - they tolerate low light and irregular watering."),
    ("how to reset a router", "how to reset a home router",
     [("Resetting Your Router", "netguide.example.invalid", "Hold the reset pin for 10 seconds; settings return to factory defaults.")],
     "Hold the reset pin for about 10 seconds - that returns the router to factory defaults."),
    ("what to pack for a weekend hike", "weekend hike packing list",
     [("Day Hike Checklist", "trailhead.example.invalid", "Water, layers, snacks, map, first-aid kit, headlamp and sun protection.")],
     "The top checklist: water, layers, snacks, a map, a first-aid kit, a headlamp and sun protection."),
    ("how many cups are in a quart", "cups in a quart",
     [("Kitchen Conversions", "cookwise.example.invalid", "1 quart equals 4 cups.")], "One quart is 4 cups, according to the search result."),
    ("how to start a sourdough starter", "sourdough starter day by day",
     [("Sourdough Starter Basics", "breadhub.example.invalid", "Mix equal parts flour and water, feed daily; it's ready in about 7 days.")],
     "Mix equal parts flour and water and feed it daily - it should be ready in around 7 days."),
    ("how to get a pen stain out of a shirt", "remove ink stain from fabric",
     [("Removing Ink From Clothes", "laundrylab.example.invalid", "Dab with rubbing alcohol on a clean cloth, then wash as usual.")],
     "Dab the stain with rubbing alcohol on a clean cloth, then wash as usual."),
    ("how long to roast a whole chicken", "roast whole chicken time",
     [("Roast Chicken Basics", "ovenguide.example.invalid", "Roast at 425F for about 20 minutes per pound until it reaches 165F inside.")],
     "Roughly 20 minutes per pound at 425F, until it reaches 165F inside."),
    ("what's a good stretch for lower back pain", "lower back stretches",
     [("Gentle Back Stretches", "movewell.example.invalid", "Try knee-to-chest and child's pose, holding each for 30 seconds.")],
     "The top result suggests knee-to-chest and child's pose, holding each for about 30 seconds."),
]
WB_TPL = ["search the web for {q}", "google {q}", "look up {q} online", "can you search for {q}", "web search: {q}", "look online - {q}", "find out {q}"]


@family("reads", "rd_web", weight=11, failure=("direct_real_tool_without_discovery", "loop_tool"), web=True)
def f_rd_web(r, c, now):
    q, sq, results, ans = r.choice(WEB_BANK[:8])
    tpl = r.choice(WB_TPL)
    c.user(messy(r, tpl.format(q=q)))
    c.discover(["public_web"], ["web_search"])
    res = "\n".join(f"{i}. {t} - {d}\n   {sn}" for i, (t, d, sn) in enumerate(results, 1))
    c.call("web_search", {"query": sq, "limit": 3}, res)
    c.final(ans)
    return {"template": WB_TPL.index(tpl), "slots": {"query": sq}}


WX_TPL = ["what's the weather in {city}", "weather in {city} {d}?", "how's the weather looking in {city} {d}", "is it going to rain in {city} {d}",
          "do I need a jacket in {city} {d}", "temperature in {city} {d}"]


@family("reads", "rd_weather", weight=9, failure=("direct_real_tool_without_discovery",), web=True)
def f_rd_weather(r, c, now):
    cq, cname = r.choice(CITIES)
    day = pick_day(r, now, kinds=("today", "tomorrow", "weekday"))
    tpl = r.choice(WX_TPL)
    c.user(messy(r, tpl.format(city=cname, d=day.spoken if "{d}" in tpl else ""), protect=(cname,)))
    c.discover(["public_web"], ["get_weather"])
    wres, rain, (hi, lo, cond, pct) = weather_result(r, cq, day)
    args = {"location": cq}
    if "{d}" in tpl:
        args["period"] = day.period
    c.call("get_weather", args, wres)
    label = dw(day) if "{d}" in tpl else "today"
    c.final(f"{cname} {label}: {cond.lower()}, high {hi}F and low {lo}F, with a {pct}% chance of rain.")
    return {"template": WX_TPL.index(tpl), "slots": {"city": cname}}


REF_BANK = [
    ("wiki", "photosynthesis", "Photosynthesis is the process by which plants, algae and some bacteria convert light energy into chemical energy stored in sugars.",
     "Photosynthesis is how plants, algae and some bacteria turn light into chemical energy stored as sugars."),
    ("wiki", "the Great Barrier Reef", "The Great Barrier Reef is the world's largest coral reef system, off the coast of Queensland, Australia.",
     "It's the world's largest coral reef system, off the coast of Queensland, Australia."),
    ("wiki", "Ada Lovelace", "Ada Lovelace (1815-1852) was an English mathematician known for her work on Charles Babbage's Analytical Engine.",
     "Ada Lovelace was a 19th-century English mathematician known for her work on Babbage's Analytical Engine."),
    ("recipe", "carbonara", "Spaghetti carbonara - 25 min. Eggs, pecorino, guanciale, black pepper, spaghetti. Toss hot pasta with egg-cheese mix off the heat.",
     "Spaghetti carbonara takes about 25 minutes: eggs, pecorino, guanciale, black pepper and spaghetti, tossing the hot pasta with the egg-cheese mix off the heat."),
    ("recipe", "pancakes", "Fluffy pancakes - 20 min. Flour, milk, egg, baking powder, butter. Rest the batter 5 minutes; cook on medium heat.",
     "Pancakes in about 20 minutes: flour, milk, egg, baking powder and butter. Rest the batter 5 minutes and cook on medium heat."),
    ("time", "Tokyo", "Tokyo: Tuesday 8:15 AM (JST), 13 hours ahead of local time.", "It's Tuesday 8:15 AM in Tokyo, 13 hours ahead of you."),
    ("time", "London", "London: Monday 11:40 PM (GMT+1), 5 hours ahead of local time.", "London is 5 hours ahead - Monday 11:40 PM there."),
]
RF_TPL = {"wiki": ["tell me about {t}", "what is {t}", "give me a quick summary of {t}", "who/what is {t}"],
          "recipe": ["how do I make {t}", "recipe for {t}?", "quick {t} recipe"],
          "time": ["what time is it in {t}", "current time in {t}?", "time in {t} right now"]}


@family("reads", "rd_reference", weight=9, failure=("direct_real_tool_without_discovery", "loop_tool"), web=True)
def f_rd_reference(r, c, now):
    kind, topic, res, ans = r.choice(REF_BANK)
    tpl = r.choice(RF_TPL[kind])
    c.user(messy(r, tpl.format(t=topic), protect=(topic,)))
    if kind == "wiki":
        c.discover(["public_web"], ["wikipedia_summary"])
        c.call("wikipedia_summary", {"topic": topic}, res)
    elif kind == "recipe":
        c.discover(["public_web"], ["recipe_lookup"])
        c.call("recipe_lookup", {"dish": topic}, res)
    else:
        c.discover(["public_web"], ["world_time"])
        c.call("world_time", {"place": topic}, res)
    c.final(ans)
    return {"template": 0, "slots": {"kind": kind, "topic": topic}}


# ---- device / compute / memory / timers
DEV_BANK = [
    ("get_battery_status", {}, "Battery 64%, on battery power, about 3 h 40 min remaining. Cycles: 212. Health: 92% of design capacity.",
     ["how much battery do i have", "what's my battery at", "battery status", "is my battery healthy", "how long until my mac dies"],
     "Your battery is at 64% on battery power, with roughly 3 h 40 min left. Health is 92% of design capacity after 212 cycles."),
    ("get_volume", {}, "Output volume is 35%.", ["what's the volume at", "how loud is my mac right now", "volume level?"], "The volume is at 35%."),
    ("network_info", {}, "Wi-Fi: connected to 'HarborNet', IP 192.168.1.23, signal good.", ["what wifi am i on", "am I connected to wifi", "what's my ip address"],
     "You're connected to Wi-Fi network 'HarborNet' with IP 192.168.1.23 and a good signal."),
    ("system_status", {}, "Disk: 118 GB free of 460 GB. Memory pressure: normal. CPU load 14%. Uptime 3 days.",
     ["how much disk space do I have left", "is my mac running low on storage", "how's my mac doing", "why is my mac slow"],
     "You have 118 GB free of 460 GB. Memory pressure is normal, CPU load is 14%, and the Mac has been up 3 days."),
    ("list_bluetooth_devices", {}, "Connected: AirPods Pro. Paired: Magic Keyboard, Desk Speaker.", ["what bluetooth devices are connected", "are my airpods connected"],
     "AirPods Pro are connected. Also paired: Magic Keyboard and Desk Speaker."),
]


@family("reads", "rd_device", weight=9, failure=("direct_real_tool_without_discovery", "loop_tool"))
def f_rd_device(r, c, now):
    tool, args, res, prompts, ans = r.choice(DEV_BANK)
    p = r.choice(prompts)
    c.user(messy(r, p))
    c.discover(["device"], [tool])
    c.call(tool, args, res)
    c.final(ans)
    return {"template": prompts.index(p), "slots": {"tool": tool}}


CMP_BANK = [
    ("calculate", lambda r: (lambda a, p: ({"expression": f"{p}% of {a}"}, f"{p}% of {a} = {round(a * p / 100, 2)}",
                                           f"{p}% of {a} is {round(a * p / 100, 2)}.", [f"what's {p}% of {a}", f"{p} percent of {a}", f"how much is {p}% of {a}"]))(r.choice([64.5, 82, 120, 48.2, 215]), r.choice([15, 18, 20, 12]))),
    ("convert_units", lambda r: (lambda v: ({"value": v, "from_unit": "miles", "to_unit": "km"}, f"{v} miles = {round(v * 1.609344, 2)} km",
                                           f"{v} miles is about {round(v * 1.609344, 2)} km.", [f"convert {v} miles to km", f"{v} miles in kilometers", f"how many km is {v} miles"]))(r.choice([5, 10, 13.1, 26.2, 50]))),
    ("convert_currency", lambda r: (lambda a: ({"amount": a, "from_currency": "EUR", "to_currency": "USD"}, f"{a} EUR = {round(a * 1.08, 2)} USD (rate 1.08)",
                                               f"{a} EUR is about {round(a * 1.08, 2)} USD at today's rate of 1.08.", [f"how much is {a} euros in dollars", f"convert {a} EUR to USD", f"{a} euro to usd"]))(r.choice([50, 100, 250, 80]))),
]


@family("reads", "rd_compute", weight=7, failure=("direct_real_tool_without_discovery", "loop_tool"))
def f_rd_compute(r, c, now):
    tool, fn = r.choice(CMP_BANK)
    args, res, ans, prompts = fn(r)
    p = r.choice(prompts)
    c.user(messy(r, p))
    c.discover(["compute"], [tool])
    c.call(tool, args, res)
    c.final(ans)
    return {"template": prompts.index(p), "slots": {"tool": tool}}


MEM_BANK = [("what do you remember about my sister", "sister", "Memory 12: User's sister Nadia lives in Seattle.", "You told me your sister Nadia lives in Seattle."),
            ("what do you know about my car", "car", "Memory 7: User drives a green hatchback.", "I have that you drive a green hatchback."),
            ("do you remember my allergies", "allerg", "Memory 3: User is allergic to shellfish.", "Yes - you're allergic to shellfish."),
            ("remind me what I said regarding my commute", "commute", "Memory 19: User bikes to work on Tuesdays and Thursdays.", "You said you bike to work on Tuesdays and Thursdays."),
            ("remind me what you have on file about my gym schedule", "gym", "Memory 21: User goes to the gym Monday, Wednesday and Friday mornings.", "You go to the gym Monday, Wednesday and Friday mornings.")]


@family("reads", "rd_memory", weight=6, failure=("direct_real_tool_without_discovery",))
def f_rd_memory(r, c, now):
    p, q, res, ans = r.choice(MEM_BANK)
    c.user(messy(r, p))
    c.discover(["memory"], ["recall"])
    c.call("recall", {"query": q}, res)
    c.final(ans)
    return {"template": 0, "slots": {"query": q}}


TIM_TPL = ["how long is left on my timer", "check my timers", "what timers are running", "is my pasta timer still going", "how much time is left on the timer", "any timers going?", "time left on my pasta timer"]


@family("reads", "rd_timers", weight=4, failure=("direct_real_tool_without_discovery",))
def f_rd_timers(r, c, now):
    p = r.choice(TIM_TPL)
    c.user(messy(r, p))
    c.discover(["automation"], ["manage_timers"])
    left = r.choice([4, 7, 12, 18])
    c.call("manage_timers", {"action": "list"}, f"1 timer running: 'pasta' - {left} min left.")
    c.final(f"Your pasta timer has {left} minutes left.")
    return {"template": TIM_TPL.index(p), "slots": {}}


# ---- dev reads in a distinct register
DEV_RD = [
    ("notes", "could you surface what I stored under {k}"), ("mail", "I'm hunting for the message about {subj}"),
    ("web", "do a quick web lookup: {q}"), ("device", "give me the current charge level on this laptop"),
    ("compute", "work out {p} percent of {a}"), ("messages", "recent chat history with {n}, please"), ("files", "enumerate what's in {f}")]


@family("reads", "rd_dev", split="dev", weight=14, failure=("direct_real_tool_without_discovery",), web=True)
def f_rd_dev(r, c, now):
    kind, tpl = r.choice(DEV_RD)
    if kind == "notes":
        ntitle, nbody, key = r.choice([n for n in NOTE_BANK if n[2] in NOTE_DEV_KEYS])
        c.user(messy(r, tpl.format(k=key)))
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}")
        c.final(f"“{ntitle}” says: {nbody}.")
    elif kind == "mail":
        s, subj = r.choice(EMAIL_SUBJ_BANK)
        c.user(messy(r, tpl.format(subj=subj.lower())))
        c.discover(["mail"], ["view_emails"])
        d = now.date() - dt.timedelta(days=r.randint(1, 5))
        c.call("view_emails", {"query": subj, "count": 3, "strict_match": True}, _mail_listing(s, subj, _msgid(r), dshort(d), "Let me know what you think."))
        c.final(f"That's {first(s)}'s email “{subj}” from {dshort(d)}: “Let me know what you think.”")
    elif kind == "web":
        q, sq, results, ans = r.choice(WEB_BANK[8:])
        c.user(messy(r, tpl.format(q=q)))
        c.discover(["public_web"], ["web_search"])
        c.call("web_search", {"query": sq, "limit": 3}, "\n".join(f"{i}. {t} - {d}\n   {sn}" for i, (t, d, sn) in enumerate(results, 1)))
        c.final(ans)
    elif kind == "device":
        c.user(messy(r, tpl))
        c.discover(["device"], ["get_battery_status"])
        c.call("get_battery_status", {}, DEV_BANK[0][2])
        c.final(DEV_BANK[0][4])
    elif kind == "compute":
        a, p = r.choice([64.5, 82, 120, 215]), r.choice([15, 18, 20])
        c.user(messy(r, tpl.format(p=p, a=a)))
        c.discover(["compute"], ["calculate"])
        c.call("calculate", {"expression": f"{p}% of {a}"}, f"{p}% of {a} = {round(a * p / 100, 2)}")
        c.final(f"{p}% of {a} is {round(a * p / 100, 2)}.")
    elif kind == "messages":
        nm = pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        c.user(messy(r, tpl.format(n=said), protect=(said,)))
        c.discover(["messages_contacts"], ["view_messages"])
        mres, items = messages_result(r, nm, n=2)
        c.call("view_messages", {"conversation": _name_arg(said, nm), "count": 5}, mres)
        f = nm if nm in RELATIVES else first(nm)
        c.final(f"Latest with {f}: " + " / ".join(f"{s}: “{t}”" for s, t in items) + ".")
    else:
        folder, names = r.choice(FILE_BANK)
        c.user(messy(r, tpl.format(f=folder)))
        c.discover(["files"], ["list_dir"])
        c.call("list_dir", {"path": folder}, f"{folder} ({len(names)} items):\n" + "\n".join(f"- {n}" for n in names))
        c.final(f"{folder} contains: " + ", ".join(names) + ".")
    return {"template": DEV_RD.index((kind, tpl)), "slots": {"kind": kind}}


# ================================================================ WRITES

def long_when(d, h, m=0):
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day} at {t12(h, m)}"


def reminder_title(task):
    return task[:1].upper() + task[1:]


def rel_time(r, now):
    """-> (spoken, datetime) for 'in N minutes/hours'."""
    if r.random() < 0.5:
        n = r.choice([20, 30, 45, 90])
        t = now + dt.timedelta(minutes=n)
        spoken = f"in {n} minutes" if n < 60 else "in an hour and a half"
    else:
        n = r.choice([1, 2, 3])
        t = now + dt.timedelta(hours=n)
        spoken = f"in {n} hour{'s' if n > 1 else ''}"
    t = t.replace(second=0, microsecond=0)
    t = t - dt.timedelta(minutes=t.minute % 5)
    return spoken, t


RW_TPL = ["remind me to {task} {w}", "set a reminder: {task} {w}", "add a reminder to {task} {w}", "can you remind me {w} to {task}",
          "reminder {w} - {task}", "don't let me forget to {task} {w}", "I need a reminder to {task} {w}", "make a reminder to {task} {w}"]
RW_DEV = ["nudge me {w} about this: {task}", "put in a reminder for {w}: {task}", "have a reminder go off {w} so I {task}"]


def _reminder_write(r, c, now, tpls):
    task = r.choice(REMINDER_BANK)
    mode = r.choice(["abs", "abs", "rel", "tonight"])
    if mode == "rel":
        w, t = rel_time(r, now)
        d, h, m = t.date(), t.hour, t.minute
    elif mode == "tonight" and now.hour < 19:
        h = r.choice([19, 20, 21])
        w, d, m = f"tonight at {t12(h).lower()}", now.date(), 0
    else:
        day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
        tm = pick_time(r, 7, 20, minutes=(0, 0, 30))
        w, d, h, m = f"{day.spoken} at {tm.spoken}", day.date, tm.h, tm.m
    tpl = r.choice(tpls)
    c.user(messy(r, tpl.format(task=task, w=w), protect=(task,)))
    c.discover(["calendar"], ["add_reminder"])
    title = reminder_title(task)
    c.call("add_reminder", {"title": title, "when_iso": iso(d, h, m)}, f"Reminder set: “{title}” — {long_when(d, h, m)} in Apple Reminders and Wisp.")
    c.final(r.choice([f"Done - I'll remind you to {task} on {long_when(d, h, m)}.", f"Reminder set: {task}, {long_when(d, h, m)}.",
                      f"Got it. You'll get a reminder to {task} at {t12(h, m)} on {WEEKDAYS[d.weekday()]}."]))
    return {"template": tpls.index(tpl), "slots": {"task": task, "mode": mode}}


@family("writes", "wr_reminder", weight=14, failure=("direct_real_tool_without_discovery", "write_then_verify_loop"))
def f_wr_reminder(r, c, now):
    return _reminder_write(r, c, now, RW_TPL)


@family("writes", "wr_reminder_dev", split="dev", weight=4, failure=("direct_real_tool_without_discovery",))
def f_wr_reminder_dev(r, c, now):
    return _reminder_write(r, c, now, RW_DEV)


EW_TPL = ["put {a} on my calendar {d} at {t}", "schedule {a} for {d} {t}", "add {a} {d} {t} to my calendar", "block off {t} {d} for {a}",
          "book {a} in my calendar for {d} at {t}", "calendar: {a}, {d}, {t}", "can you add {a} to my calendar, {d} at {t}"]
EW_DEV = ["I've got {a} {d} at {t} - get it into my calendar", "make a calendar entry for {a} {d} at {t}", "create an event: {a}, {d}, {t}"]
DURS = [("", None), (" for 30 minutes", 30), (" for an hour", 60), (" for 2 hours", 120), (" for 45 minutes", 45)]


def _event_write(r, c, now, tpls):
    p = first(r.choice(PEOPLE))
    ev = r.choice(EVENT_BANK)
    title = ev[0].format(p=p)
    day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
    tm = pick_time(r, 8, 19, minutes=(0, 30))
    durphrase, dur = r.choice(DURS)
    loc = r.choice([None, None, ev[1]])
    tpl = r.choice(tpls)
    txt = tpl.format(a=title.lower() if r.random() < 0.4 else title, d=day.spoken, t=tm.spoken) + durphrase
    if loc:
        txt += f" at {loc}"
    c.user(messy(r, txt, protect=(title,)))
    c.discover(["calendar"], ["add_calendar_event"])
    args = {"title": title, "when_iso": iso(day.date, tm.h, tm.m)}
    if dur:
        args["duration_min"] = dur
    if loc:
        args["location"] = loc
    endm = tm.h * 60 + tm.m + (dur or 60)
    c.call("add_calendar_event", args, f"Added “{title}” to your calendar for {long_when(day.date, tm.h, tm.m)} - {t12(endm // 60, endm % 60)}.")
    c.final(r.choice([f"Added “{title}” to your calendar for {long_when(day.date, tm.h, tm.m)}.", f"Done - {title} is on your calendar, {WEEKDAYS[day.date.weekday()]} at {tm.long}.",
                      f"It's on the calendar: {title}, {long_when(day.date, tm.h, tm.m)}."]))
    return {"template": tpls.index(tpl), "slots": {"title": title, "duration": dur, "location": loc}}


@family("writes", "wr_event", weight=12, failure=("direct_real_tool_without_discovery", "write_then_verify_loop"))
def f_wr_event(r, c, now):
    return _event_write(r, c, now, EW_TPL)


@family("writes", "wr_event_dev", split="dev", weight=4, failure=("direct_real_tool_without_discovery",))
def f_wr_event_dev(r, c, now):
    return _event_write(r, c, now, EW_DEV)


NOTE_NEW = [("Packing for Denver", "jacket\nboots\nlaptop charger", True, "jacket, boots, laptop charger"),
            ("Ideas for dad's birthday", "fishing reel, a good pair of socks, dinner out", False, "fishing reel, a good pair of socks, dinner out"),
            ("Weekend chores", "laundry\nclean the garage\ncall the landlord", True, "laundry, clean the garage, call the landlord"),
            ("Meeting takeaways", "ship the beta Friday; Priya owns the docs", False, "ship the beta Friday; Priya owns the docs"),
            ("Books to read", "Piranesi\nThe Overstory", True, "Piranesi, The Overstory"),
            ("Dinner party menu", "lentil soup, roast carrots, lemon tart", False, "lentil soup, roast carrots, lemon tart")]
NN_TPL = ["make a note called {t} with {b}", "new note: {t} - {b}", "jot down {b} in a note titled {t}", "create a note named {t}: {b}",
          "start a note '{t}' and put {b} in it"]
NN_CHK = ["start a checklist called {t}: {b}", "make me a checklist titled {t} with {b}", "new checklist {t}: {b}"]


@family("writes", "wr_note_create", weight=8, failure=("direct_real_tool_without_discovery",))
def f_wr_note_create(r, c, now):
    t, body, chk, spoken = r.choice(NOTE_NEW)
    tpl = r.choice(NN_CHK if chk else NN_TPL)
    c.user(messy(r, tpl.format(t=t, b=spoken), protect=(t,)))
    c.discover(["notes"], ["create_note"])
    args = {"title": t, "body": body}
    if chk:
        args["checklist"] = True
    c.call("create_note", args, f"Created the {'checklist' if chk else 'note'} \"{t}\" in Notes.")
    c.final(r.choice([f"Created the {'checklist' if chk else 'note'} “{t}” in Notes.", f"Done - “{t}” is in your Notes."]))
    return {"template": 0, "slots": {"title": t, "checklist": chk}}


AN_BANK = [("Grocery list", "grocery", "oat milk"), ("Grocery list", "grocery", "two lemons and basil"), ("Hardware store", "hardware", "picture hooks"),
           ("Gift ideas", "gift", "a leather notebook"), ("Packing list", "packing", "phone charger"), ("Books to read", "book", "Piranesi")]
AN_TPL = ["add {x} to my {k} list", "put {x} on my {k} note", "append {x} to the {k} note", "tack {x} onto my {k} list", "stick {x} on the {k} list"]


@family("writes", "wr_note_append", weight=6, failure=("direct_real_tool_without_discovery",))
def f_wr_note_append(r, c, now):
    title, k, x = r.choice(AN_BANK)
    tpl = r.choice(AN_TPL)
    c.user(messy(r, tpl.format(x=x, k=k), protect=(x,)))
    c.discover(["notes"], ["append_note"])
    c.call("append_note", {"title": k, "text": x}, f"Added it to \"{title}\".")
    c.final(r.choice([f"Added “{x}” to your {title}.", f"Done - {x} is on the list now."]))
    return {"template": AN_TPL.index(tpl), "slots": {"note": title}}


FACTS = [("my favorite coffee order is a flat white", "preference"), ("my sister's birthday is on March 4", "person"),
         ("I'm allergic to shellfish", "fact"), ("I usually go to the gym on Monday, Wednesday and Friday mornings", "routine"),
         ("my car is a green hatchback", "fact"), ("I prefer short answers in the morning", "preference"),
         ("my landlord's name is Mr. Abernathy", "person"), ("I'm training for a half marathon in April", "project")]
RMB_TPL = ["remember that {f}", "keep in mind that {f}", "please remember {f}", "save this to memory: {f}", "for future reference, {f} - remember that"]


@family("writes", "wr_remember", weight=6, failure=("direct_real_tool_without_discovery", "false_saved_claim"))
def f_wr_remember(r, c, now):
    f, cat = r.choice(FACTS)
    tpl = r.choice(RMB_TPL)
    c.user(messy(r, tpl.format(f=f), protect=(f,)))
    c.discover(["memory"], ["remember"])
    saved = f[0].upper() + f[1:]
    mid = r.randint(20, 90)
    c.call("remember", {"fact": f, "category": cat}, f"Saved memory {mid}: {saved}")
    c.final(r.choice([f"Saved - I'll remember that {f}.", f"Got it, saved: {f}."]))
    return {"template": RMB_TPL.index(tpl), "slots": {"category": cat}}


UR_TPL = ["move my {k} reminder to {d} at {t}", "push the reminder about {k} to {d} {t}", "change my {k} reminder to {d} at {t}", "reschedule the {k} reminder for {d} {t}"]
UR_DAY = ["push my {k} reminder to tomorrow", "move the {k} reminder to tomorrow"]


@family("writes", "wr_update_reminder", weight=6, failure=("unrequested_change", "direct_real_tool_without_discovery"))
def f_wr_update_reminder(r, c, now):
    task = r.choice(REMINDER_BANK)
    key = task.split()[-1] if len(task.split()) > 1 else task
    if r.random() < 0.25:
        tpl = r.choice(UR_DAY)
        c.user(messy(r, tpl.format(k=key), protect=(key,)))
        c.discover(["calendar"], ["update_reminder"])
        c.call("update_reminder", {"title": key, "day": "tomorrow"}, f"Updated “{reminder_title(task)}” - now tomorrow, same time.")
        c.final(f"Moved your “{task}” reminder to tomorrow.")
        return {"template": 100, "slots": {"task": task}}
    tpl = r.choice(UR_TPL)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    tm = pick_time(r, 8, 19, minutes=(0, 30))
    c.user(messy(r, tpl.format(k=key, d=day.spoken, t=tm.spoken), protect=(key,)))
    c.discover(["calendar"], ["update_reminder"])
    c.call("update_reminder", {"title": key, "when_iso": iso(day.date, tm.h, tm.m)}, f"Updated “{reminder_title(task)}” - now {long_when(day.date, tm.h, tm.m)}.")
    c.final(f"Done - “{task}” is now set for {long_when(day.date, tm.h, tm.m)}.")
    return {"template": UR_TPL.index(tpl), "slots": {"task": task}}


CR_TPL = ["mark {k} as done", "I finished the {k} thing, check off the reminder", "tick off my {k} reminder", "complete the reminder about {k}", "{k} reminder - done, mark it complete"]


@family("writes", "wr_complete_reminder", weight=5, failure=("unrequested_change", "direct_real_tool_without_discovery"))
def f_wr_complete_reminder(r, c, now):
    task = r.choice(REMINDER_BANK)
    key = task.split()[-1] if len(task.split()) > 1 else task
    tpl = r.choice(CR_TPL)
    c.user(messy(r, tpl.format(k=key), protect=(key,)))
    c.discover(["calendar"], ["complete_reminder"])
    c.call("complete_reminder", {"title": key}, f"Marked “{reminder_title(task)}” as done.")
    c.final(r.choice([f"Marked “{task}” as done.", f"Checked off: {task}."]))
    return {"template": CR_TPL.index(tpl), "slots": {"task": task}}


CE_TPL = ["cancel my {a}", "delete the {a} from my calendar", "take {a} off my calendar", "remove my {a} from the calendar", "cancel {a} {d}", "get rid of the {a} on my calendar"]


@family("writes", "wr_cancel_event", weight=5, failure=("unrequested_change", "direct_real_tool_without_discovery"))
def f_wr_cancel_event(r, c, now):
    ev = make_event(r, now.date() + dt.timedelta(days=r.randint(1, 6)))
    tpl = r.choice(CE_TPL)
    day = pick_day(r, now, kinds=("tomorrow", "weekday"))
    c.user(messy(r, tpl.format(a=ev.alias, d=day.spoken), protect=(ev.alias,)))
    c.discover(["calendar"], ["cancel_event"])
    c.call("cancel_event", {"title": ev.key}, f"Cancelled “{ev.title}”.")
    c.final(r.choice([f"Cancelled “{ev.title}”.", f"Done - {ev.title} is off your calendar."]))
    return {"template": CE_TPL.index(tpl), "slots": {"event": ev.key}}


TA_BANK = [("timer", "set a timer for {x} minutes", lambda x: ({"duration": f"{x} minutes"}, f"Timer set for {x} minutes.", f"Timer set for {x} minutes.")),
           ("timer", "{x} minute timer please", lambda x: ({"duration": f"{x} minutes"}, f"Timer set for {x} minutes.", f"Your {x}-minute timer is running.")),
           ("timer", "start a {x} min timer called pasta", lambda x: ({"duration": f"{x} minutes", "label": "pasta"}, f"Timer 'pasta' set for {x} minutes.", f"Started a {x}-minute timer named pasta.")),
           ("alarm", "wake me up at {h}", None), ("alarm", "set an alarm for {h} tomorrow", None)]


@family("writes", "wr_timer_alarm", weight=6, failure=("direct_real_tool_without_discovery",))
def f_wr_timer_alarm(r, c, now):
    kind, tpl, fn = r.choice(TA_BANK)
    if kind == "timer":
        x = r.choice([5, 10, 12, 15, 20, 25])
        args, res, ans = fn(x)
        c.user(messy(r, tpl.format(x=x)))
        c.discover(["automation"], ["set_timer"])
        c.call("set_timer", args, res)
        c.final(ans)
    else:
        tm = pick_time(r, 5, 9, minutes=(0, 30))
        spoken = f"{tm.h % 12 or 12}:{tm.m:02d}am" if tm.m else f"{tm.h % 12 or 12}am"
        c.user(messy(r, tpl.format(h=spoken)))
        c.discover(["automation"], ["set_alarm"])
        c.call("set_alarm", {"time": spoken}, f"Alarm set for {tm.long}.")
        c.final(f"Alarm set for {tm.long}.")
    return {"template": TA_BANK.index((kind, tpl, fn)), "slots": {"kind": kind}}


SET_BANK = [("set_volume", lambda r: (lambda v: ({"level": v}, f"Volume set to {v}%.", f"Volume is now {v}%.", [f"set the volume to {v}", f"turn the volume to {v} percent", f"volume {v}"]))(r.choice([10, 20, 30, 50, 70]))),
            ("toggle_setting", lambda r: ({"setting": "do_not_disturb", "on": True}, "Do Not Disturb is on.", "Do Not Disturb is on.", ["turn on do not disturb", "enable DND", "silence notifications with do not disturb"])),
            ("toggle_setting", lambda r: ({"setting": "bluetooth", "on": False}, "Bluetooth is off.", "Bluetooth is off.", ["turn off bluetooth", "disable bluetooth please"])),
            ("set_appearance", lambda r: ({"mode": "dark"}, "Appearance set to Dark.", "Switched to dark mode.", ["switch to dark mode", "make my mac dark mode", "go dark mode"]))]


@family("writes", "wr_settings", weight=5, failure=("direct_real_tool_without_discovery",))
def f_wr_settings(r, c, now):
    tool, fn = r.choice(SET_BANK)
    args, res, ans, prompts = fn(r)
    p = r.choice(prompts)
    c.user(messy(r, p))
    c.discover(["device"], [tool])
    c.call(tool, args, res)
    c.final(ans)
    return {"template": prompts.index(p), "slots": {"tool": tool}}


@family("writes", "wr_clear_today", weight=2, failure=("unrequested_change",))
def f_wr_clear_today(r, c, now):
    p = r.choice(["delete all my reminders for today", "clear every reminder due today", "wipe today's reminders"])
    n = r.randint(2, 5)
    c.user(messy(r, p))
    c.discover(["calendar"], ["clear_reminders"])
    c.call("clear_reminders", {"scope": "today"}, f"Cleared {n} reminders due today.")
    c.final(f"Cleared {n} reminders due today.")
    return {"template": 0, "slots": {"n": n}}


@family("writes", "wr_trash", weight=2, failure=("unrequested_change",))
def f_wr_trash(r, c, now):
    name = r.choice(["old-installer.dmg", "draft-v1.docx", "scratch.txt", "backup-2024.zip"])
    folder = r.choice(["~/Downloads", "~/Desktop"])
    p = r.choice([f"move {folder}/{name} to the trash", f"trash {folder}/{name}", f"throw away {folder}/{name}"])
    c.user(messy(r, p))
    c.discover(["files"], ["trash_file"])
    c.call("trash_file", {"path": f"{folder}/{name}"}, f"Moved {name} to the Trash.")
    c.final(f"Moved {name} to the Trash.")
    return {"template": 0, "slots": {"file": name}}


DVB = ["could you log a to-do for me: {task}, due {d} at {t}", "I need an alert {d} at {t} so I {task}"]


@family("writes", "wr_dev_misc", split="dev", weight=6, failure=("direct_real_tool_without_discovery", "unrequested_change"))
def f_wr_dev_misc(r, c, now):
    kind = r.choice(["note", "update", "cancel", "reminder2"])
    if kind == "note":
        title, k, x = r.choice(AN_BANK)
        c.user(messy(r, f"throw {x} onto the {k} list in Notes", protect=(x,)))
        c.discover(["notes"], ["append_note"])
        c.call("append_note", {"title": k, "text": x}, f"Added it to \"{title}\".")
        c.final(f"{x} is on the {title} now.")
    elif kind == "update":
        task = r.choice(REMINDER_BANK)
        key = task.split()[-1] if len(task.split()) > 1 else task
        day = pick_day(r, now, kinds=("tomorrow", "weekday"))
        tm = pick_time(r, 8, 19, minutes=(0, 30))
        c.user(messy(r, f"shift the {key} reminder over to {day.spoken}, {tm.spoken}", protect=(key,)))
        c.discover(["calendar"], ["update_reminder"])
        c.call("update_reminder", {"title": key, "when_iso": iso(day.date, tm.h, tm.m)}, f"Updated “{reminder_title(task)}” - now {long_when(day.date, tm.h, tm.m)}.")
        c.final(f"The “{task}” reminder is now {long_when(day.date, tm.h, tm.m)}.")
    elif kind == "cancel":
        ev = make_event(r, now.date() + dt.timedelta(days=r.randint(1, 6)))
        c.user(messy(r, f"I won't make the {ev.alias} anymore, please drop it from my calendar", protect=(ev.alias,)))
        c.discover(["calendar"], ["cancel_event"])
        c.call("cancel_event", {"title": ev.key}, f"Cancelled “{ev.title}”.")
        c.final(f"Dropped “{ev.title}” from your calendar.")
    else:
        task = r.choice(REMINDER_BANK)
        day = pick_day(r, now, kinds=("tomorrow", "weekday"))
        tm = pick_time(r, 8, 19, minutes=(0, 30))
        c.user(messy(r, f"I need an alert {day.spoken} at {tm.spoken} so I {task}", protect=(task,)))
        c.discover(["calendar"], ["add_reminder"])
        title = reminder_title(task)
        c.call("add_reminder", {"title": title, "when_iso": iso(day.date, tm.h, tm.m)}, f"Reminder set: “{title}” — {long_when(day.date, tm.h, tm.m)} in Apple Reminders and Wisp.")
        c.final(f"Alert set: {task}, {long_when(day.date, tm.h, tm.m)}.")
    return {"template": 0, "slots": {"kind": kind}}


# ================================================================ ERROR HANDLING

PERM = [
    ("notes", ["notes"], "search_notes", lambda r: {"query": r.choice(["passport", "recipe", "project", "trip"])},
     "(Notes automation isn't permitted yet. Grant it in System Settings → Privacy & Security → Automation.)",
     "I couldn't read your notes - Wisp doesn't have Notes permission yet. Grant it in System Settings → Privacy & Security → Automation, then ask again.",
     ["search my notes for {k}", "find my note on {k}", "what do my notes say about {k}"]),
    ("calendar", ["calendar"], "get_upcoming", lambda r: {"period": "tomorrow"},
     "Wisp could not check Calendar. Check Calendar access in Settings, then try again.",
     "I couldn't check your calendar - Wisp can't access Calendar right now. Check Calendar access in Settings and try again.",
     ["what's on my calendar tomorrow", "anything scheduled tomorrow", "what do I have tomorrow"]),
    ("messages", ["messages_contacts"], "view_messages", lambda r: {"unread": True, "count": 5},
     "(Messages access is not enabled. Grant Full Disk Access to Wisp in System Settings → Privacy & Security.)",
     "I couldn't read your messages - Wisp doesn't have Full Disk Access yet. Enable it in System Settings → Privacy & Security, then ask again.",
     ["any unread texts", "show me my unread messages", "do I have new texts"]),
    ("mail", ["mail"], "summarize_emails", lambda r: {"period": "today"},
     "(Mail isn't permitted yet. Allow Wisp under System Settings → Privacy & Security → Automation → Mail.)",
     "I couldn't read your email - Mail hasn't been allowed for Wisp yet. Enable it under System Settings → Privacy & Security → Automation → Mail.",
     ["summarize my email today", "what's in my inbox today", "email rundown for today"]),
    ("free", ["calendar"], "find_free_time", lambda r: {"period": "tomorrow", "minutes": 60, "day_start": 14, "day_end": 15},
     "(error: Wisp could not check current Calendar and Reminders availability. Nothing was verified.)",
     "I couldn't verify your availability - Wisp wasn't able to read Calendar and Reminders, so I can't say whether 2 PM tomorrow is free.",
     ["am i free tomorrow at 2pm", "is 2 pm tomorrow open", "do I have anything tomorrow at 2"]),
    ("remind", ["calendar"], "add_reminder", lambda r: {"title": "Call the plumber", "when_iso": iso(NOW_HOLDER["now"].date() + dt.timedelta(days=1), 9, 0)},
     "(error: the Wisp app is not connected and an earlier Apple Reminders write was not verified; nothing was added.)",
     "I couldn't add that reminder - Wisp isn't connected to Reminders right now, so nothing was added. Try again once the Wisp app is running.",
     ["remind me to call the plumber tomorrow at 9am", "set a reminder for 9 tomorrow: call the plumber", "reminder tomorrow 9am - call the plumber"]),
    ("note_create", ["notes"], "create_note", lambda r: {"title": "Trip ideas", "body": "Lisbon, Porto, Madeira"},
     "(error talking to Notes: Not authorized to send Apple events to Notes.)",
     "I couldn't create the note - Notes won't let Wisp write to it yet, so nothing was saved. You can allow it under System Settings → Privacy & Security → Automation.",
     ["make a note called Trip ideas with Lisbon, Porto, Madeira", "new note: Trip ideas - Lisbon, Porto, Madeira"]),
]
PERM_DEV = [
    ("calendar", ["calendar"], "get_upcoming", lambda r: {"period": "today"},
     "Wisp could not check Calendar. Check Calendar access in Settings, then try again.",
     "I wasn't able to read your calendar just now - Wisp's Calendar access looks blocked. Check the permission in Settings and ask me again.",
     ["any meetings left today", "what's still on today"]),
    ("notes", ["notes"], "search_notes", lambda r: {"query": "recipe"},
     "(Notes automation isn't permitted yet. Grant it in System Settings → Privacy & Security → Automation.)",
     "Notes isn't accessible to Wisp yet, so I couldn't look up the recipe. Allow it under Privacy & Security → Automation and I'll try again when you ask.",
     ["dig out my recipe note", "where's that recipe I saved in notes"]),
]


def _blocked(r, c, now, table):
    kind, fams, tool, argf, err, ans, prompts = r.choice(table)
    k = r.choice(["passport", "recipe", "project", "trip"])
    p = r.choice(prompts)
    c.user(messy(r, p.format(k=k)))
    c.discover(fams, [tool])
    args = argf(r)
    if tool == "search_notes" and "{k}" in p:
        args = {"query": k}
    c.call(tool, args, err)
    c.final(ans)
    return {"template": prompts.index(p), "slots": {"kind": kind}}


@family("errors", "err_blocked", weight=20, failure=("silent_on_error", "identical_retry", "false_success_claim"))
def f_err_blocked(r, c, now):
    return _blocked(r, c, now, PERM)


@family("errors", "err_blocked_dev", split="dev", weight=4, failure=("silent_on_error", "identical_retry"))
def f_err_blocked_dev(r, c, now):
    return _blocked(r, c, now, PERM_DEV)


SEND_FAIL = [("message", "(the message was NOT sent: Messages is not signed in.)", "Messages isn't signed in"),
             ("message", "(the message was NOT sent: no network connection.)", "there was no network connection"),
             ("email", "(the email was NOT sent: Mail could not reach the server.)", "Mail couldn't reach the server")]


@family("errors", "err_send_failed", weight=10, failure=("false_sent_claim", "identical_retry", "silent_on_error"))
def f_err_send_failed(r, c, now):
    kind, err, why = r.choice(SEND_FAIL)
    nm = r.choice(PEOPLE)
    said, _ = name_forms(r, nm)
    if kind == "message":
        it = text_intent(r)
        tpl = r.choice(["text {n} that {c}", "let {n} know {c}", "message {n} - {c}", "shoot {n} a text saying {c}"])
        c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm))
        c.call("send_message", {"to": phone_for(nm), "text": it["body"]}, err)
    else:
        it = email_intent(r, first(nm))
        tpl = r.choice(["email {n} {c}", "send {n} an email {c}", "write {n} an email {c}"])
        c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
        c.call("lookup_contact", {"name": first(nm) if said != nm else nm}, lookup_result(nm, via="email"))
        c.call("send_email", {"to": email_for(nm), "subject": it["subject"], "body": it["body"]}, err)
    c.final(r.choice([f"That didn't go through - {why}, so the {kind} to {first(nm)} was not sent. Try again once that's sorted.",
                      f"I couldn't send it: {why}. Nothing was delivered to {first(nm)}.",
                      f"The {kind} to {first(nm)} failed ({why}) and was not sent."]))
    return {"template": 0, "slots": {"kind": kind}}


BROADEN = [
    # (kind, user prompt, literal first query, keyword for second query, sender, subject, body/ntitle, nbody)
    ("mail", "find the email about the garage estimate", "garage estimate", "garage", "Aisha Karimi", "Garage quote", "Attached is the quote you asked for.", None),
    ("mail", "look for the email on the lease paperwork", "lease paperwork", "lease", "Jonas Weber", "Lease renewal", "Please sign and return the renewal by the 30th.", None),
    ("mail", "pull up the email on last quarter's figures", "last quarter figures", "quarterly", "Luca Romano", "Quarterly numbers", "Sharing the figures as promised.", None),
    ("mail", "find the email about lunch plans", "lunch plans", "lunch", "Elena Petrov", "Team lunch", "Thursday at noon works for everyone.", None),
    ("mail", "which email had the trip pictures", "trip pictures", "trip", "Hana Sato", "Photos from the trip", "Here is the album link.", None),
    ("mail", "look up the email about my gym contract", "gym contract", "gym", "Owen Pritchard", "Gym membership", "Your renewal is due at the end of the month.", None),
    ("notes", "find my note about tax receipts", "tax receipts 2025", "taxes", None, None, "Taxes 2025", "Receipts folder is in the blue binder; filed through March"),
    ("notes", "what did I write about the trip packing", "packing for the trip", "packing", None, None, "Packing list - Lisbon trip", "passport, charger, rain jacket, sunscreen"),
    ("notes", "do I have notes on the apartment hunt", "apartment hunt", "apartment", None, None, "Apartment search", "Shortlist: Maple St 2BR, Harbor View studio"),
    ("files", "find the pdf of my lease agreement final", "lease agreement final", "lease", None, None, None, None),
    ("files", "where's the pdf of my insurance card scan", "insurance card scan", "insurance", None, None, None, None),
]


@family("errors", "err_broaden", weight=11, failure=("identical_retry", "empty_result_retry_loop", "summarize_irrelevant_results"))
def f_err_broaden(r, c, now):
    kind, p, q1, q2, s, subj, body, nbody = r.choice(BROADEN)
    c.user(messy(r, p))
    if kind == "mail":
        c.discover(["mail"], ["view_emails"])
        c.call("view_emails", {"query": q1, "count": 3, "strict_match": True}, f"No emails matched {q1!r}.")
        d = now.date() - dt.timedelta(days=r.randint(1, 5))
        c.call("view_emails", {"query": q2, "count": 3, "strict_match": True}, _mail_listing(s, subj, _msgid(r), dshort(d), body))
        c.final(f"The first search found nothing, but a broader one did: {first(s)} sent \u201c{subj}\u201d on {dshort(d)} - \u201c{body}\u201d")
    elif kind == "notes":
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": q1}, f"No notes matched {q1!r}.")
        c.call("search_notes", {"query": q2}, f"1 note: {body}\n{nbody}")
        c.final(f"Nothing matched the first search, but I found \u201c{body}\u201d: {nbody}.")
    else:
        c.discover(["files"], ["find_files"])
        c.call("find_files", {"query": q1, "kind": "pdf"}, f"No files matched {q1!r}.")
        path = f"~/Documents/{q2}_2026.pdf"
        c.call("find_files", {"query": q2, "kind": "pdf"}, f"1 match:\n{path} (modified {dshort(now.date() - dt.timedelta(days=30))})")
        c.final(f"The exact wording didn't match, but a broader search found {path}.")
    return {"template": 0, "slots": {"kind": kind, "q1": q1}}


BROADEN_DEV = [
    ("can you locate that email regarding the school fundraiser", "fundraiser flyer", "fundraiser", "Wren Calloway", "School fundraiser", "Please bring cookies on Friday."),
    ("track down the message about the moving truck", "moving truck reservation", "truck", "Idris Mbeki", "Moving truck booking", "Truck confirmed for the 14th."),
    ("I need that email on choir practice", "choir practice times", "choir", "Greta Lund", "Choir rehearsal", "We start at 6 PM sharp."),
]


@family("errors", "err_broaden_dev", split="dev", weight=2, failure=("identical_retry", "empty_result_retry_loop"))
def f_err_broaden_dev(r, c, now):
    p, q1, q2, s, subj, body = r.choice(BROADEN_DEV)
    c.user(messy(r, p))
    c.discover(["mail"], ["view_emails"])
    c.call("view_emails", {"query": q1, "count": 3, "strict_match": True}, f"No emails matched {q1!r}.")
    d = now.date() - dt.timedelta(days=r.randint(1, 5))
    c.call("view_emails", {"query": q2, "count": 3, "strict_match": True}, _mail_listing(s, subj, _msgid(r), dshort(d), body))
    c.final(f"After a broader search I found it: {first(s)}'s email \u201c{subj}\u201d from {dshort(d)} - \u201c{body}\u201d")
    return {"template": BROADEN_DEV.index((p, q1, q2, s, subj, body)), "slots": {"q1": q1}}


@family("errors", "err_changed_retry", weight=9, failure=("identical_retry", "silent_on_error"), web=True)
def f_err_changed_retry(r, c, now):
    kind = r.choice(["web", "web", "file", "stock"])
    if kind == "web":
        q, sq, results, ans = r.choice(WEB_BANK[:8])
        c.user(messy(r, r.choice(["search the web for {q}", "look up {q} online", "google {q}"]).format(q=q)))
        c.discover(["public_web"], ["web_search"])
        c.call("web_search", {"query": sq, "limit": 5}, "(timed out reaching the search provider)")
        short = " ".join(sq.split()[:3])
        c.call("web_search", {"query": short, "limit": 2}, "\n".join(f"{i}. {t} - {d}\n   {sn}" for i, (t, d, sn) in enumerate(results, 1)))
        c.final("The first search timed out, so I tried a shorter one. " + ans)
    elif kind == "file":
        c.user(messy(r, r.choice(["read my budget spreadsheet in documents", "open ~/Documents/budget.xlsx and tell me what's in it", "what's in my budget file"])))
        c.discover(["files"], ["find_files", "read_file"])
        c.call("read_file", {"path": "~/Documents/budget.xlsx"}, "(error: no such file: ~/Documents/budget.xlsx)")
        c.call("find_files", {"query": "budget"}, "1 match:\n~/Documents/Finance/budget-2026.xlsx")
        c.call("read_file", {"path": "~/Documents/Finance/budget-2026.xlsx"}, "Sheet1: Rent 1800; Groceries 420; Transport 110; Savings 500")
        c.final("budget.xlsx wasn't at that path, but I found ~/Documents/Finance/budget-2026.xlsx: Rent 1800, Groceries 420, Transport 110, Savings 500.")
    else:
        c.user(messy(r, r.choice(["what's HLCN trading at", "stock price for HLCN", "how is HLCN doing today"])))
        c.discover(["public_web"], ["get_stock_price", "web_search"])
        c.call("get_stock_price", {"symbols": ["HLCN"]}, "(timed out reaching Yahoo Finance for HLCN)")
        c.call("web_search", {"query": "HLCN stock price", "limit": 2}, "1. Halcyon Robotics (HLCN) quote - markets.example.invalid\n   HLCN last traded at 48.20, up 0.6% (delayed 15 min).")
        c.final("The direct price lookup timed out, but a web search shows Halcyon Robotics (HLCN) last traded at 48.20, up 0.6%, delayed by about 15 minutes.")
    return {"template": 0, "slots": {"kind": kind}}


@family("errors", "err_partial_pair", weight=7, failure=("multi_source_partial", "silent_on_error"))
def f_err_partial_pair(r, c, now):
    c.user(messy(r, r.choice(["what's on my calendar today and what's in my inbox", "give me my agenda and my email for today", "calendar and email rundown for today",
                              "tell me my schedule today plus any new email", "what do I have on today and what mail came in", "agenda and inbox for today please",
                              "check my calendar and my email today", "today's meetings and today's emails, please"])))
    c.discover(["calendar", "mail"], ["get_upcoming", "summarize_emails"])
    evs = make_day(r, now.date(), r.randint(1, 3), hmin=max(8, min(17, now.hour + 1)), hmax=19)
    ok_first = r.random() < 0.5
    cal_res = agenda_result(now, "today", evs)
    mail_err = "(Mail isn't permitted yet. Allow Wisp under System Settings → Privacy & Security → Automation → Mail.)"
    c.calls([("get_upcoming", {"period": "today"}, cal_res), ("summarize_emails", {"period": "today"}, mail_err)])
    c.final("Calendar today: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".\nI couldn't read your email - Mail isn't allowed for Wisp yet (System Settings → Privacy & Security → Automation → Mail), so that part is missing.")
    return {"template": 0, "slots": {}}


@family("errors", "err_double_fail", weight=5, failure=("identical_retry", "silent_on_error", "runaway"))
def f_err_double_fail(r, c, now):
    c.user(messy(r, r.choice(["am i free tomorrow at 4pm", "is 4 pm tomorrow open on my calendar", "do I have a conflict tomorrow at 4",
                              "is tomorrow at 4pm clear", "can I take a 4 pm meeting tomorrow", "anything on my calendar tomorrow at 4pm"])))
    c.discover(["calendar"], ["find_free_time", "get_upcoming"])
    c.call("find_free_time", {"period": "tomorrow", "minutes": 60, "day_start": 16, "day_end": 17}, "(error: Wisp could not check current Calendar and Reminders availability. Nothing was verified.)")
    c.call("get_upcoming", {"period": "tomorrow"}, "Wisp could not check Calendar. Check Calendar access in Settings, then try again.")
    c.final("I tried two ways of checking your calendar and both failed, so I can't tell whether 4 PM tomorrow is free. Check Calendar access in Settings and ask again.")
    return {"template": 0, "slots": {}}


@family("errors", "err_reject_recover", weight=8, failure=("discovery_rejected", "family_tool_mismatch"))
def f_err_reject_recover(r, c, now):
    kind = r.choice(["cal_text", "mail_cal", "note_text"])
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    f = nm if nm in RELATIVES else first(nm)
    if kind == "cal_text":
        day = pick_day(r, now, kinds=("tomorrow", "weekday"))
        ev = make_event(r, day.date, h=r.randint(9, 17), m=0)
        c.user(messy(r, f"text {said} when my {ev.alias} is {day.spoken}", protect=(said, ev.alias)))
        c.reject_then(["calendar"], ["get_upcoming", "lookup_contact", "send_message"])
        c.discover(["calendar", "messages_contacts"], ["get_upcoming", "lookup_contact", "send_message"])
        c.call("get_upcoming", {"period": day.period, "query": ev.key}, agenda_result(now, day.period if day.kind == "date" else day.kind, [ev]))
        c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
        body = f"My {lc_first(ev.title)} is {ev_when(now, ev)}."
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        c.final(f"Texted {f}: {Q(body)}")
    elif kind == "mail_cal":
        c.user(messy(r, r.choice(["unread email and today's calendar please", "show unread mail plus what's on today"])))
        c.reject_then(["mail"], ["summarize_emails", "get_upcoming"])
        c.discover(["mail", "calendar"], ["get_upcoming", "summarize_emails"])
        dig, items = email_digest(r, 2, "unread", unread_only=True)
        evs = make_day(r, now.date(), 2, hmin=max(8, min(17, now.hour + 1)), hmax=19)
        c.calls([("summarize_emails", {"unread": True}, dig), ("get_upcoming", {"period": "today"}, agenda_result(now, "today", evs))])
        c.final("Unread: " + "; ".join(f"{s} - {subj}" for s, subj, _ in items) + ".\nToday: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".")
    else:
        th, key, ntitle, nbody, body = r.choice(NT_THINGS)
        body = nbody
        c.user(messy(r, f"text {said} {th} from my notes", protect=(said,)))
        c.reject_then(["notes"], ["search_notes", "lookup_contact"])
        c.discover(["notes", "messages_contacts"], ["lookup_contact", "search_notes", "send_message"])
        c.calls([("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}"), ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))])
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        c.final(f"Texted {f} from \u201c{ntitle}\u201d: {Q(body)}")
    return {"template": 0, "slots": {"kind": kind}}


@family("errors", "err_reject_recover_dev", split="dev", weight=2, failure=("discovery_rejected", "family_tool_mismatch"))
def f_err_reject_recover_dev(r, c, now):
    nm = pick_contact(r, 0.1)
    said, _ = name_forms(r, nm)
    f = nm if nm in RELATIVES else first(nm)
    it = text_intent(r)
    c.user(messy(r, f"reach out to {said} by text - {it['clause']}", protect=(said,)))
    c.reject_then(["messages_contacts"], ["lookup_contact", "send_email"])
    c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
    c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
    c.call("send_message", {"to": phone_for(nm), "text": it["body"]}, sent_msg(c, nm))
    c.final(f"Texted {f}: {Q(it['body'])}")
    return {"template": 0, "slots": {}}


# ---- transient error -> one CHANGED retry (different arguments) -> answer

@family("errors", "err_transient_args", weight=7, failure=("identical_retry", "silent_on_error", "runaway"))
def f_err_transient_args(r, c, now):
    kind = r.choice(["mail", "notes", "cal", "msgs"])
    if kind == "mail":
        s, subj = r.choice(EMAIL_SUBJ_BANK)
        c.user(messy(r, r.choice(["did {s} send me anything this week", "has {s} emailed me recently", "check for mail from {s} this week"]).format(s=first(s)), protect=(first(s),)))
        c.discover(["mail"], ["view_emails"])
        c.call("view_emails", {"query": first(s), "count": 5, "strict_match": True}, "(error: Mail search timed out)")
        d = now.date() - dt.timedelta(days=r.randint(0, 4))
        c.call("view_emails", {"query": first(s), "period": "this week", "count": 2}, _mail_listing(s, subj, _msgid(r), dshort(d), "Quick note about next steps."))
        c.final(f"The first search timed out, so I narrowed it to this week: {first(s)} sent “{subj}” on {dshort(d)} - “Quick note about next steps.”")
    elif kind == "notes":
        ntitle, nbody, key = r.choice([n for n in NOTE_BANK if (n[2] in NOTE_DEV_KEYS) == (CUR["split"] == "dev")])
        c.user(messy(r, r.choice(["what's in my {k} note", "pull up what I saved about {k}", "find the note on {k}"]).format(k=key)))
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": key}, "(error talking to Notes: the operation timed out)")
        c.call("search_notes", {"query": key, "count": 2}, f"1 note: {ntitle}\n{nbody}")
        c.final(f"The first lookup timed out, but a smaller retry worked. “{ntitle}” says: {nbody}.")
    elif kind == "cal":
        day = pick_day(r, now, kinds=("tomorrow", "weekday"))
        evs = make_day(r, day.date, r.randint(1, 3))
        c.user(messy(r, r.choice(["what's on {d}", "any plans {d}", "what am I doing {d}"]).format(d=day.spoken)))
        c.discover(["calendar"], ["get_upcoming"])
        c.call("get_upcoming", {"period": day.period}, "(error: Calendar did not respond in time)")
        c.call("get_upcoming", {"days": 3, "calendar_only": True}, agenda_result(now, "next 3 day(s)", [e for e in evs]))
        sel = [e for e in evs if e.date == day.date]
        c.final("The first request timed out, so I re-checked the next three days. " + (f"{cap(dw(day))}: " + ", ".join(f"{e.title} at {e.tstr}" for e in sel) + "." if sel else "Nothing is listed for that day."))
    else:
        nm = pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        c.user(messy(r, r.choice(["what did {n} say to me", "any new texts from {n}", "check my messages from {n}"]).format(n=said), protect=(said,)))
        c.discover(["messages_contacts"], ["view_messages"])
        c.call("view_messages", {"conversation": _name_arg(said, nm), "count": 5}, "(error: Messages database is busy; try again)")
        mres, items = messages_result(r, nm, n=2)
        c.call("view_messages", {"conversation": _name_arg(said, nm), "period": "this week", "count": 3}, mres)
        f = nm if nm in RELATIVES else first(nm)
        c.final(f"The first read failed, so I retried for this week. {f}'s recent messages: " + " / ".join(f"{s_}: “{t_}”" for s_, t_ in items) + ".")
    return {"template": 0, "slots": {"kind": kind}}


# ================================================================ CLARIFY

AMBIG = [("Alex Moreau", "Alex Brandt"), ("Sam Whitfield", "Sam Okonkwo"), ("Chris Dalton", "Chris Navarro"), ("Jamie Fox", "Jamie Lindholm"),
         ("Taylor Quinn", "Taylor Ahmed"), ("Jordan Pike", "Jordan Castellano"), ("Maria Santos", "Maria Kovacs"), ("Dana Whitaker", "Dana Osei")]
_build_phones(list(PEOPLE) + list(RELATIVES) + [x for p in AMBIG for x in p])
for _a, _b in AMBIG:
    assert phone_for(_a) != phone_for(_b), (_a, _b)


def _multi_contact(r, c, now, tpls):
    a, b = r.choice(AMBIG)
    if r.random() < 0.5:
        a, b = b, a
    fn = first(a)
    mode, tpl = r.choice(tpls)
    it = text_intent(r) if mode != "email" else email_intent(r, fn)
    c.user(messy(r, tpl.format(n=fn, c=it["clause"]), protect=(fn,)))
    if mode == "email":
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
    elif mode == "draft":
        c.discover(["messages_contacts"], ["draft_message", "lookup_contact"])
    else:
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
    c.call("lookup_contact", {"name": fn}, ambiguous_lookup(fn, [a, b]))
    verb = {"text": "text", "email": "email", "draft": "draft a message to"}[mode]
    c.final(r.choice([f"Which {fn} do you mean - A) {a} or B) {b}? I haven't sent anything yet.",
                      f"I found two contacts named {fn}: A) {a} and B) {b}. Which one should I {verb}?",
                      f"Quick check before I go further - {fn} matches A) {a} and B) {b}. Which one?"]))
    return {"template": tpls.index((mode, tpl)), "slots": {"pair": [a, b], "mode": mode}}


MC_TPLS = [("text", "text {n} that {c}"), ("text", "let {n} know {c}"), ("email", "email {n} {c}"), ("text", "message {n} - {c}"),
           ("draft", "draft a text to {n} saying {c}"), ("text", "shoot {n} a text saying {c}"), ("email", "send {n} an email {c}")]
MC_DEV = [("text", "{n} should hear from me by text - {c}"), ("email", "put an email together for {n} and send it {c}")]


@family("clarify", "cl_multi_contact", weight=13, failure=("guess_recipient", "send_on_ambiguity"))
def f_cl_multi_contact(r, c, now):
    return _multi_contact(r, c, now, MC_TPLS)


@family("clarify", "cl_multi_contact_dev", split="dev", weight=2, failure=("guess_recipient", "send_on_ambiguity"))
def f_cl_multi_contact_dev(r, c, now):
    return _multi_contact(r, c, now, MC_DEV)


NOC_NAMES = ["Dre", "Beatrix", "Kwame", "Priyanka", "Mr. Oduya", "Coach Lenz", "Nonna Lucia", "Hugo"]


def _no_contact(r, c, now, tpls):
    nm = r.choice(NOC_NAMES)
    mode, tpl = r.choice(tpls)
    it = text_intent(r) if mode != "email" else email_intent(r, nm)
    c.user(messy(r, tpl.format(n=nm, c=it["clause"]), protect=(nm,)))
    if mode == "email":
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
    else:
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
    c.call("lookup_contact", {"name": nm}, no_contact(nm))
    c.final(r.choice([f"I don't have a saved contact called {nm}. What's their {'email address' if mode == 'email' else 'number'}?",
                      f"No contact named {nm} turned up. Can you give me their {'email' if mode == 'email' else 'phone number'} so I can send it?",
                      f"I couldn't find {nm} in your contacts, and I won't guess. What {'address' if mode == 'email' else 'number'} should I use?"]))
    return {"template": tpls.index((mode, tpl)), "slots": {"name": nm, "mode": mode}}


@family("clarify", "cl_no_contact", weight=7, failure=("guess_recipient", "silent_on_error"))
def f_cl_no_contact(r, c, now):
    return _no_contact(r, c, now, MC_TPLS[:4] + MC_TPLS[5:])


EVAMB = [("dentist", [("Dentist cleaning", 1, 15), ("Dentist consult", 4, 10)]), ("lunch", [("Lunch with Theo", 2, 12), ("Lunch with Priya", 5, 13)]),
         ("review", [("Design review", 1, 14), ("Budget review", 3, 11)]), ("call", [("Client call", 2, 10), ("Call with Mom", 3, 18)])]
REMAMB = [("call", ["Call the plumber", "Call the bank"]), ("pick up", ["Pick up dry cleaning", "Pick up the parcel"]), ("renew", ["Renew passport", "Renew gym membership"])]
CEV_TPL = ["cancel my {k}", "delete the {k} from my calendar", "cancel the {k} appointment", "take the {k} off my calendar"]


@family("clarify", "cl_multi_event", weight=8, failure=("destructive_on_ambiguity", "guess_target"))
def f_cl_multi_event(r, c, now):
    if r.random() < 0.7:
        key, opts = r.choice(EVAMB)
        c.user(messy(r, r.choice(CEV_TPL).format(k=key), protect=(key,)))
        c.discover(["calendar"], ["cancel_event"])
        items = []
        for t, off, h in opts:
            d = now.date() + dt.timedelta(days=off)
            items.append(f"{t} ({dshort(d)}, {t12(h)})")
        c.call("cancel_event", {"title": key}, f"Several items match “{key}”: " + "; ".join(items) + ". Which one? Please be more specific.")
        c.final(f"Which one should I cancel - A) {items[0]} or B) {items[1]}? I haven't cancelled anything.")
    else:
        key, opts = r.choice(REMAMB)
        c.user(messy(r, r.choice(["mark {k} as done", "tick off my {k} reminder", "complete the {k} reminder"]).format(k=key), protect=(key,)))
        c.discover(["calendar"], ["complete_reminder"])
        c.call("complete_reminder", {"title": key}, f"Several match {key!r} — which one?\n" + "\n".join(f"- {o}" for o in opts))
        c.final(f"Two reminders match: A) {opts[0]} or B) {opts[1]}. Which one is done?")
    return {"template": 0, "slots": {}}


MISSING = [
    ("remind me to call", "Who should I remind you to call, and when?"), ("set a reminder for tomorrow", "What should the reminder say, and what time tomorrow?"),
    ("text him I'm late", "Who should I text? I don't know who 'him' is."), ("send it", "Send what, and to whom?"),
    ("schedule a meeting", "What's the meeting called, when should it be, and how long?"), ("email my boss about the thing", "Which thing, and who is your boss? Tell me the name and what it should say."),
    ("add it to my calendar", "What should I add, and when?"), ("cancel it", "Cancel what? Tell me which event or reminder."),
    ("remind me in a bit", "How long is a bit - 15 minutes, an hour?"), ("set an alarm", "What time should the alarm go off?"),
    ("let them know", "Who should I tell, and what should I say?"), ("move it to friday", "Move what to Friday? Name the event or reminder."),
    ("book dinner", "I can add it to your calendar - which night, what time, and where?"), ("remind me about the thing tomorrow", "Which thing should I remind you about, and at what time?"),
    ("reply yes", "Reply yes to which email or message?"), ("forward that to her", "Forward which email, and who is 'her'?"),
    ("add a note", "What should the note say, and what should I call it?"), ("set a timer", "How long should the timer run?"),
    ("tell her I said thanks", "Who should I tell, and by text or email?"), ("put the meeting on my calendar", "Which meeting - and what day and time?"),
    ("text mom", "What should I say to Mom?"), ("email Sam", "What should the email say, and which Sam do you mean?")]
MISSING_DEV = [("get that over to them", "Get what over to whom? Tell me the item and the person."), ("snooze it", "Snooze which reminder, and for how long?"),
               ("new event please", "Sure - what's it called, and when is it?")]


def _missing(r, c, now, bank):
    p, q = r.choice(bank)
    c.user(messy(r, p))
    c.final(q)
    return {"template": bank.index((p, q)), "slots": {}}


@family("clarify", "cl_missing_info", weight=18, failure=("guess_missing_arg", "tool_on_underspecified"))
def f_cl_missing_info(r, c, now):
    return _missing(r, c, now, MISSING)


@family("clarify", "cl_missing_info_dev", split="dev", weight=3, failure=("guess_missing_arg", "tool_on_underspecified"))
def f_cl_missing_info_dev(r, c, now):
    return _missing(r, c, now, MISSING_DEV)


CH_TPL = ["get in touch with {n} about {t}", "reach out to {n} about {t}", "follow up with {n} regarding {t}", "contact {n} about {t}", "can you ping {n} about {t}?"]
CH_TOP = ["the schedule change", "dinner on Friday", "the open invoice", "moving the meeting", "next week's plans", "the house keys"]


@family("clarify", "cl_channel", weight=5, failure=("guess_channel",))
def f_cl_channel(r, c, now):
    nm = r.choice(PEOPLE)
    said, _ = name_forms(r, nm)
    tpl = r.choice(CH_TPL)
    c.user(messy(r, tpl.format(n=said, t=r.choice(CH_TOP)), protect=(said,)))
    c.final(r.choice([f"Should I text or email {first(nm)}, and what would you like to say?", f"Do you want that sent as a text or an email - and what should the message say?",
                      f"Happy to - text or email? And what should I tell {first(nm)}?"]))
    return {"template": CH_TPL.index(tpl), "slots": {"contact": nm}}


WL_TPL = ["what's the weather like tomorrow", "will it rain this weekend?", "do I need an umbrella today", "how's the weather", "is it going to be cold tonight",
          "weather forecast for friday?", "should I bring a jacket tomorrow", "how hot will it get on saturday", "is it supposed to snow this week",
          "any rain coming up soon?", "what's it like outside right now", "forecast for the next few days?"]


@family("clarify", "cl_weather_loc", weight=5, failure=("guess_location", "tool_on_underspecified"), web=True)
def f_cl_weather_loc(r, c, now):
    p = r.choice(WL_TPL)
    c.user(messy(r, p))
    c.final(r.choice(["Which city should I check the weather for?", "Where are you - what city or postcode should I use for the forecast?",
                      "I don't know your location yet. What city do you want the weather for?"]))
    return {"template": WL_TPL.index(p), "slots": {}}


# ================================================================ CHAT / NO TOOL

QA = [
    ("why is the sky blue", "Sunlight scatters off air molecules, and shorter blue wavelengths scatter much more than red ones, so the sky looks blue."),
    ("what's the difference between affect and effect", "Affect is usually a verb meaning to influence; effect is usually a noun meaning the result. 'The rain affected the game; the effect was a delay.'"),
    ("explain what an API is in simple terms", "An API is a set of rules that lets one program ask another program for data or actions, like a waiter carrying orders between you and the kitchen."),
    ("what causes the tides", "Mostly the Moon's gravity, with help from the Sun, pulling on the oceans and creating bulges that sweep around the Earth as it rotates."),
    ("how many ounces are in a pound", "There are 16 ounces in a pound."),
    ("what's the capital of australia", "Canberra."),
    ("how do you say good morning in spanish", "Buenos días."),
    ("what does ephemeral mean", "Lasting for a very short time - like morning mist or a fleeting trend."),
    ("is a tomato a fruit or a vegetable", "Botanically it's a fruit, since it develops from a flower and holds seeds; in the kitchen it's treated as a vegetable."),
    ("what's the boiling point of water in fahrenheit", "212°F at sea level."),
    ("how do you say thank you in japanese", "Arigatō (ありがとう), or arigatō gozaimasu to be more polite."),
    ("what's the difference between a virus and a bacterium", "Bacteria are living single-celled organisms that can reproduce on their own; viruses are much smaller and need a host cell to replicate."),
    ("how fast does light travel", "About 299,792 kilometers per second (roughly 186,000 miles per second) in a vacuum."),
    ("who wrote pride and prejudice", "Jane Austen."),
    ("how many days are in a leap year", "366 - February gets a 29th day."),
    ("what's a haiku", "A short Japanese-style poem with three lines of 5, 7 and 5 syllables."),
    ("what is compound interest", "Interest calculated on both your original amount and the interest it has already earned, so growth accelerates over time."),
    ("how does a refrigerator keep things cold", "It circulates a refrigerant that absorbs heat inside the fridge and releases it outside, using a compressor and evaporator."),
    ("what's the difference between weather and climate", "Weather is what's happening in the atmosphere right now or this week; climate is the long-term average pattern over decades."),
    ("explain the pythagorean theorem", "In a right triangle, the square of the hypotenuse equals the sum of the squares of the other two sides: a² + b² = c²."),
    ("why do we have leap years", "A year is about 365.25 days, so adding a day every four years keeps the calendar aligned with Earth's orbit."),
    ("what's the plural of octopus", "Octopuses is the most common plural; octopi is also used informally, though it's technically not Latin-correct."),
]
QA_DEV = [("what does gravity do", "Gravity is the force that pulls masses toward each other - it keeps us on the ground and planets in orbit."),
          ("how many minutes are in a day", "There are 1,440 minutes in a day."), ("what is the largest ocean", "The Pacific Ocean.")]


def _qa(r, c, now, bank):
    q, a = r.choice(bank)
    c.user(messy(r, q))
    c.final(a)
    return {"template": bank.index((q, a)), "slots": {}}


@family("chat", "ch_knowledge", weight=14, failure=("acted_on_chat", "tool_on_chat"))
def f_ch_knowledge(r, c, now):
    return _qa(r, c, now, QA)


@family("chat", "ch_knowledge_dev", split="dev", weight=2, failure=("acted_on_chat", "tool_on_chat"))
def f_ch_knowledge_dev(r, c, now):
    return _qa(r, c, now, QA_DEV)


SMALL = [("hi", "Hi! What can I do for you?"), ("good morning", "Good morning! What's on your mind?"), ("thanks!", "You're welcome!"),
         ("thank you so much", "Anytime!"), ("tell me a joke", "Why don't scientists trust atoms? Because they make up everything."),
         ("how are you", "Doing well, thanks for asking - ready when you are."), ("never mind", "No problem. Let me know if you need anything."),
         ("ok cool", "Great. I'm here if you need anything else."), ("you're awesome", "Thank you - happy to help!"), ("hello there", "Hello! How can I help?"),
         ("lol nice", "Glad you liked it!"), ("hey wisp", "Hey! What can I do for you?")]


@family("chat", "ch_smalltalk", weight=8, failure=("acted_on_chat", "tool_on_chat"))
def f_ch_smalltalk(r, c, now):
    return _qa(r, c, now, SMALL)


def _timeq(r, c, now):
    kind = r.choice(["time", "date", "day", "plus"])
    h = now.hour % 12 or 12
    ap = "AM" if now.hour < 12 else "PM"
    if kind == "time":
        c.user(messy(r, r.choice(["what time is it", "what's the time right now", "time check?", "do you know what time it is"])))
        c.final(f"It's {h}:{now.minute:02d} {ap}.")
    elif kind == "date":
        c.user(messy(r, r.choice(["what's today's date", "what's the date today", "what is today's date?"])))
        c.final(f"Today is {WEEKDAYS[now.weekday()]}, {MONTHS[now.month - 1]} {now.day}, {now.year}.")
    elif kind == "day":
        c.user(messy(r, r.choice(["what day is it", "which day of the week is it today", "what day is today"])))
        c.final(f"It's {WEEKDAYS[now.weekday()]}.")
    else:
        n = r.choice([2, 3, 5, 10])
        d = now.date() + dt.timedelta(days=n)
        c.user(messy(r, f"what's the date {n} days from today"))
        c.final(f"{n} days from today is {WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}, {d.year}.")
    return {"template": 0, "slots": {"kind": kind}}


@family("chat", "ch_time_date", weight=6, failure=("tool_on_chat", "hedging_on_time"))
def f_ch_time_date(r, c, now):
    return _timeq(r, c, now)


COMPOSE = [
    ("write a two sentence thank-you note for a coworker who covered my shift", "Thank you so much for covering my shift - it made a stressful week much easier. I owe you one, and I'm happy to return the favor anytime."),
    ("give me three name ideas for a bakery", "1) Rise & Shine Bakehouse, 2) The Daily Crumb, 3) Golden Whisk."),
    ("write a haiku about coffee", "Steam curls from the cup,\nmorning wakes in bitter warmth,\nthe day finds its pace."),
    ("rewrite this to sound friendlier: send me the report", "Hi! Whenever you get a chance, could you send me the report? Thanks so much!"),
    ("give me a short toast for my friend's wedding", "To the happy couple: may your days be full of laughter, your home full of love, and your hearts always full of each other. Cheers!"),
    ("suggest a caption for a photo of my dog at the beach", "Sand in his paws, zero worries in his head."),
    ("write a polite way to decline a meeting invite", "Thanks for the invite - unfortunately I can't make that time. Could you share notes afterward, or suggest another slot?"),
    ("help me start a cover letter opening line", "I'm excited to apply for the role because it combines the problem-solving I enjoy with work that makes a real difference."),
]
COMPOSE_DEV = [("draft me a quick get-well message, no need to send it", "Thinking of you and hoping you're feeling better soon. Rest up - I'm here if you need anything."),
               ("come up with a slogan for a plant shop", "Grow something good.")]


@family("chat", "ch_compose", weight=8, failure=("tool_on_chat", "draft_vs_send_confusion"))
def f_ch_compose(r, c, now):
    return _qa(r, c, now, COMPOSE)


@family("chat", "ch_compose_dev", split="dev", weight=1, failure=("tool_on_chat", "draft_vs_send_confusion"))
def f_ch_compose_dev(r, c, now):
    return _qa(r, c, now, COMPOSE_DEV)


CAPS = [
    ("what can you help me with", "I can work with your calendar and reminders, email, messages and contacts, notes, files, memory, weather and web lookups, device settings, timers and alarms, and quick calculations. Just tell me what you need."),
    ("can you read my email", "Yes - I can search and summarize your email, draft messages, and send or reply when you ask. Sending always asks for your approval first."),
    ("can you see my calendar", "Yes. I can check your schedule and free time, add or cancel events, and manage reminders."),
    ("are you able to send texts", "Yes - I can text people from your contacts. You'll get a confirmation before anything is actually sent."),
    ("do you have access to my files", "Yes - I can list folders, find files, read documents, and move or trash files when you ask."),
    ("can you set reminders for me", "Yes. I can add reminders with a date and time, change them, and mark them done."),
    ("what can you do with email", "I can search and summarize your inbox, read specific messages, draft and send emails, reply, forward, and archive or flag messages."),
    ("can you look things up online", "Yes - I can search the web, check the weather, stock prices and sports scores, and pull up reference summaries."),
    ("are you able to make phone calls", "I can start a FaceTime or phone call to a contact or number from your Mac. I can't answer or hang up calls."),
    ("do you remember things I tell you", "Yes - I can save facts and preferences you ask me to remember, recall them later, and forget them when you ask."),
    ("what do you do with notes", "I can search your Notes, create new notes or checklists, and append to existing ones."),
    ("can you control my mac settings", "To a degree - things like volume, Wi-Fi, Bluetooth, Do Not Disturb, appearance, and a few more. Tell me which setting."),
]


@family("chat", "ch_capability", weight=8, failure=("tool_on_chat", "capability_question_tool_call"))
def f_ch_capability(r, c, now):
    return _qa(r, c, now, CAPS)


NOCAP = [("order me a pizza", "I can't place orders - I don't have a tool for ordering food. I can set a reminder to order it or find a nearby place if you give me a city."),
         ("book me a flight to denver", "I can't book flights - there's no booking tool available to me. I can add the trip to your calendar or set a reminder to book it."),
         ("post this on instagram", "I can't post to Instagram - I don't have access to social media accounts."),
         ("buy 10 shares of apple", "I can't make trades or purchases. I can look up a price if you like, but you'd need to place the order yourself.")]
NOCAP_DEV = [("reserve a table at a restaurant tonight", "I can't make reservations - I have no booking tool. I can add a dinner event to your calendar once you've booked.")]


@family("chat", "ch_nocap", weight=4, failure=("false_done_claim", "tool_on_chat"))
def f_ch_nocap(r, c, now):
    return _qa(r, c, now, NOCAP)


@family("chat", "ch_nocap_dev", split="dev", weight=1, failure=("false_done_claim", "tool_on_chat"))
def f_ch_nocap_dev(r, c, now):
    return _qa(r, c, now, NOCAP_DEV)


# ================================================================ FOLLOW-UPS (history)

def _agenda_sentence(now, day, evs):
    if not evs:
        return f"Nothing is scheduled {dw(day)}."
    return f"{cap(dw(day))} you have " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + "."


FW_TPL = ["what about {d}", "and {d}?", "how about {d}", "same for {d}", "{d}?", "what's {d} look like"]
FW_DEV = ["now check {d} instead", "ok and {d}", "switch to {d}"]


def _what_about(r, c, now, tpls):
    d1 = pick_day(r, now, kinds=("today", "tomorrow"))
    d2 = pick_day(r, now, kinds=("weekday",))
    ev1 = make_day(r, d1.date, r.randint(1, 3), hmin=8 if d1.kind != "today" else min(17, now.hour + 1))
    first_prompt = r.choice(["run me through my schedule {d}", "what am I up to {d}", "walk me through {d} on the calendar", "give me the agenda for {d}"]).format(d=d1.spoken)
    c.hist("user", first_prompt).hist("assistant", _agenda_sentence(now, d1, ev1))
    tpl = r.choice(tpls)
    c.user(messy(r, tpl.format(d=d2.spoken)))
    c.discover(["calendar"], ["get_upcoming"])
    ev2 = make_day(r, d2.date, r.randint(0, 3))
    c.call("get_upcoming", {"period": d2.period}, agenda_result(now, d2.period, ev2))
    c.final(_agenda_sentence(now, d2, ev2))
    return {"template": tpls.index(tpl), "slots": {"first": d1.kind, "second": d2.period}}


@family("followup", "fu_what_about", weight=9, failure=("followup_context_lost", "loop_tool"))
def f_fu_what_about(r, c, now):
    return _what_about(r, c, now, FW_TPL)


@family("followup", "fu_what_about_dev", split="dev", weight=2, failure=("followup_context_lost", "loop_tool"))
def f_fu_what_about_dev(r, c, now):
    return _what_about(r, c, now, FW_DEV)


FC_ANS = [("email", ["email", "use email instead", "email please", "by email"]), ("text", ["text", "use messages instead", "text is fine", "via text"])]


@family("followup", "fu_channel", weight=6, failure=("followup_context_lost", "guess_channel"))
def f_fu_channel(r, c, now):
    nm = r.choice(PEOPLE)
    it = text_intent(r)
    first_prompt = r.choice(["get in touch with {n} - {c}", "reach out to {n} and say {c}"]).format(n=first(nm), c=it["clause"])
    c.hist("user", first_prompt).hist("assistant", f"Should I text or email {first(nm)}?")
    chan, answers = r.choice(FC_ANS)
    c.user(messy(r, r.choice(answers)))
    if chan == "text":
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": first(nm)}, lookup_result(nm))
        c.call("send_message", {"to": phone_for(nm), "text": it["body"]}, sent_msg(c, nm))
        c.final(_fin_sent(r, "message", first(nm), it["body"]))
    else:
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
        c.call("lookup_contact", {"name": first(nm)}, lookup_result(nm, via="email"))
        body = EMAIL_FRAME.format(first=first(nm), content=it["body"])
        c.call("send_email", {"to": email_for(nm), "subject": "Message", "body": body}, sent_email(c, email_for(nm)))
        c.final(_fin_sent(r, "email", first(nm), body))
    return {"template": 0, "slots": {"channel": chan, "contact": nm}}


CANCEL_OFFERS = [("Here's the text I'd send to {n}: “{b}” Want me to send it?", ["cancel", "no, don't send it", "never mind", "nope scrap that", "no thanks"]),
                 ("I can email {n} the update. Shall I go ahead?", ["actually no", "cancel that", "leave it", "don't"])]


@family("followup", "fu_cancel_offer", weight=4, failure=("send_on_vague_assent", "tool_on_chat"))
def f_fu_cancel_offer(r, c, now):
    nm = r.choice(PEOPLE)
    it = text_intent(r)
    offer, answers = r.choice(CANCEL_OFFERS)
    c.hist("user", f"what should I tell {first(nm)}? {it['clause']}").hist("assistant", offer.format(n=first(nm), b=it["body"]))
    c.user(messy(r, r.choice(answers)))
    c.final(r.choice([f"Okay, I won't send anything to {first(nm)}.", "No problem - nothing was sent.", "Understood, I've dropped it."]))
    return {"template": 0, "slots": {}}


@family("followup", "fu_more", weight=4, failure=("loop_exact", "followup_context_lost"))
def f_fu_more(r, c, now):
    picks3 = r.sample(EMAIL_SUBJ_BANK, 3)
    c.hist("user", "show my latest emails").hist("assistant", "Your 3 latest emails: " + "; ".join(f"{s} - {subj}" for s, subj in picks3) + ".")
    c.user(messy(r, r.choice(["show me more", "more please", "what else came in", "next few"])))
    c.discover(["mail"], ["view_emails"])
    n = 6
    rest = [x for x in EMAIL_SUBJ_BANK if x not in picks3]
    r.shuffle(rest)
    lines = [f"{i}) {s} - {subj}" for i, (s, subj) in enumerate(picks3 + rest[:3], 1)]
    c.call("view_emails", {"count": 6}, f"{n} emails:\n" + "\n".join(lines))
    c.final("The next ones after those three: " + "; ".join(f"{s} - {subj}" for s, subj in rest[:3]) + ".")
    return {"template": 0, "slots": {}}


@family("followup", "fu_weather_next", weight=3, failure=("followup_context_lost", "guess_location"), web=True)
def f_fu_weather_next(r, c, now):
    cq, cname = r.choice(CITIES)
    day = Day("today", now.date(), "today", now)
    wres, rain, (hi, lo, cond, pct) = weather_result(r, cq, day)
    c.hist("user", f"what's the weather in {cname}").hist("assistant", f"{cname} today: {cond.lower()}, high {hi}F, {pct}% chance of rain.")
    c.user(messy(r, r.choice(["and tomorrow?", "what about tomorrow", "how about tomorrow"])))
    c.discover(["public_web"], ["get_weather"])
    tm = Day("tomorrow", now.date() + dt.timedelta(days=1), "tomorrow", now)
    w2, rain2, (hi2, lo2, cond2, pct2) = weather_result(r, cq, tm)
    c.call("get_weather", {"location": cq, "period": "tomorrow"}, w2)
    c.final(f"{cname} tomorrow: {cond2.lower()}, high {hi2}F and low {lo2}F, with a {pct2}% chance of rain.")
    return {"template": 0, "slots": {"city": cname}}


# ================================================================ extra breadth: activity, past events, threads, upkeep, apps, calls, forget

BRIEFS = [
    "give me my morning brief", "daily brief please", "brief me on today", "what does my day look like overall",
    "can I get a quick rundown of my day", "catch me up on everything for today", "morning briefing?"]


@family("reads", "rd_activity", weight=5, failure=("direct_real_tool_without_discovery", "derived_answer_dumped_raw"))
def f_rd_activity(r, c, now):
    kind = r.choice(["brief", "brief", "recent", "recent"])
    if kind == "brief":
        p = r.choice(BRIEFS)
        c.user(messy(r, p))
        c.discover(["wisp_activity"], ["daily_brief"])
        evs = make_day(r, now.date(), r.randint(1, 2), hmin=max(8, min(17, now.hour + 1)), hmax=19)
        nrem, nmail, nmsg = r.randint(0, 3), r.randint(1, 6), r.randint(0, 5)
        res = ("Daily brief - " + dshort(now.date()) + "\nCalendar: " + "; ".join(f"{e.tstr} {e.title}" for e in evs) +
               f"\nReminders due: {nrem}\nMail today: {nmail} new\nMessages today: {nmsg} new")
        c.call("daily_brief", {}, res)
        c.final(f"Today: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + f". You have {nrem} reminder{'s' if nrem != 1 else ''} due, {nmail} new email{'s' if nmail != 1 else ''} and {nmsg} new message{'s' if nmsg != 1 else ''}.")
    else:
        p = r.choice(["what's new", "anything I missed", "what happened while I was away", "what's the latest across my stuff", "did I miss anything"])
        c.user(messy(r, p))
        c.discover(["wisp_activity"], ["get_recent_activity"])
        a, b = r.sample(PEOPLE, 2)
        s, subj = r.choice(EMAIL_SUBJ_BANK)
        res = f"Recent activity (last 24 h):\n[Text] {first(a)}: can you call me later?\n[Email] {s} - {subj}\n[Reminder added] {r.choice(REMINDER_BANK)}"
        c.call("get_recent_activity", {}, res)
        c.final(f"In the last 24 hours: a text from {first(a)} asking you to call later, an email from {first(s)} (“{subj}”), and a newly added reminder.")
    return {"template": 0, "slots": {"kind": kind}}


PAST_TPL = [("week", "what did I have on my calendar last week"), ("week", "what meetings did I have in the past week"), ("week", "look back at my last 7 days of events"),
            ("q", "when did I last see the {a}"), ("q", "when was my last {a}"), ("q", "did I have a {a} recently")]


@family("reads", "rd_past_events", weight=4, failure=("direct_real_tool_without_discovery", "wrong_tool_future_vs_past"))
def f_rd_past_events(r, c, now):
    kind, tpl = r.choice(PAST_TPL)
    if kind == "week":
        c.user(messy(r, tpl))
        c.discover(["calendar"], ["get_past_events"])
        evs = [make_event(r, now.date() - dt.timedelta(days=k)) for k in sorted(r.sample(range(1, 7), 3))]
        lines = "\n".join(f"- {dshort(e.date)}: {e.title}" for e in sorted(evs, key=lambda e: e.date, reverse=True))
        c.call("get_past_events", {"days": 7}, f"Today is {dshort(now.date())}. Past 7 day(s), {len(evs)} item(s), most recent first:\n{lines}")
        c.final("Last week you had: " + "; ".join(f"{e.title} ({dshort(e.date)})" for e in sorted(evs, key=lambda e: e.date, reverse=True)) + ".")
    else:
        ev = make_event(r, now.date() - dt.timedelta(days=r.randint(3, 40)))
        c.user(messy(r, tpl.format(a=ev.alias), protect=(ev.alias,)))
        c.discover(["calendar"], ["get_past_events"])
        c.call("get_past_events", {"days": 60, "query": ev.key},
               f"Today is {dshort(now.date())}. Past 60 day(s), 1 item(s) matching '{ev.key}', most recent first:\n- {dshort(ev.date)}: {ev.title}")
        c.final(f"Your last {ev.alias} was on {dshort(ev.date)}.")
    return {"template": PAST_TPL.index((kind, tpl)), "slots": {"kind": kind}}


THREAD_TPL = ["summarize the email thread about {subj}", "catch me up on the {subj} thread", "what's the gist of the {subj} email chain", "sum up the thread on {subj}"]
THREAD_BANK = [("Lease renewal", 4, "Jonas proposed new terms, you asked for a 12-month lease, and he agreed pending sign-off."),
               ("Workshop schedule", 3, "Camila shared two time slots; the thread settled on Thursday afternoon."),
               ("Garage quote", 3, "Aisha sent a quote of 480; you asked about timing and she can start next Tuesday."),
               ("Conference travel", 5, "Dev compared flights; the group picked the Wednesday morning departure.")]


@family("reads", "rd_thread", weight=3, failure=("direct_real_tool_without_discovery",))
def f_rd_thread(r, c, now):
    subj, n, gist = r.choice(THREAD_BANK)
    tpl = r.choice(THREAD_TPL)
    c.user(messy(r, tpl.format(subj=subj.lower())))
    c.discover(["mail"], ["summarize_thread"])
    c.call("summarize_thread", {"subject": subj}, f"Thread '{subj}' ({n} messages): {gist}")
    c.final(f"The “{subj}” thread has {n} messages: {gist}")
    return {"template": THREAD_TPL.index(tpl), "slots": {"subject": subj}}


UP_TPL = [("archive_email", "archive the email from {s} about {subj}"), ("archive_email", "file away {s}'s {subj} email"),
          ("mark_email_read", "mark the {subj} email as read"), ("mark_email_read", "mark {s}'s email about {subj} read"),
          ("flag_email", "flag the email from {s} about {subj}"), ("flag_email", "put a flag on the {subj} email")]


@family("writes", "wr_mail_upkeep", weight=5, failure=("direct_real_tool_without_discovery", "unrequested_change"))
def f_wr_mail_upkeep(r, c, now):
    tool, tpl = r.choice(UP_TPL)
    s, subj = r.choice(EMAIL_SUBJ_BANK)
    c.user(messy(r, tpl.format(s=first(s), subj=subj.lower()), protect=(first(s),)))
    c.discover(["mail"], [tool, "view_emails"])
    mid = _msgid(r)
    d = now.date() - dt.timedelta(days=r.randint(0, 3))
    c.call("view_emails", {"query": subj, "count": 2, "strict_match": True}, _mail_listing(s, subj, mid, dshort(d), "Following up on the details."))
    if tool == "archive_email":
        c.call(tool, {"message_id": mid}, f"Archived “{subj}” (it stays in Archive).")
        c.final(f"Archived {first(s)}'s “{subj}” email.")
    elif tool == "mark_email_read":
        c.call(tool, {"message_id": mid, "read": True}, f"Marked “{subj}” as read.")
        c.final(f"Marked the “{subj}” email as read.")
    else:
        c.call(tool, {"message_id": mid, "flagged": True}, f"Flagged “{subj}”.")
        c.final(f"Flagged {first(s)}'s “{subj}” email.")
    return {"template": UP_TPL.index((tool, tpl)), "slots": {"tool": tool}}


APP_BANK = [
    ("open_app", lambda r: (lambda a: ({"name": a}, f"Opened {a}.", f"Opened {a}.", [f"open {a.lower()}", f"launch {a}", f"bring up {a} for me"]))(r.choice(["Safari", "Spotify", "Notes", "Calendar", "Slack"]))),
    ("music", lambda r: ({"action": "pause"}, "Paused Apple Music.", "Paused the music.", ["pause the music", "pause apple music", "stop the song for a sec"])),
    ("music", lambda r: ({"action": "next"}, "Skipped to the next track.", "Skipped to the next track.", ["skip this song", "next track please", "play the next song"])),
    ("quit_app", lambda r: (lambda a: ({"name": a}, f"Quit {a}.", f"Quit {a}.", [f"quit {a}", f"close {a} completely", f"shut down {a}"]))(r.choice(["Slack", "Safari", "Notes", "Spotify"]))),
]


@family("writes", "wr_apps_media", weight=6, failure=("direct_real_tool_without_discovery", "unrequested_change"))
def f_wr_apps_media(r, c, now):
    tool, fn = r.choice(APP_BANK)
    args, res, ans, prompts = fn(r)
    p = r.choice(prompts)
    c.user(messy(r, p))
    c.discover(["apps_media"], [tool])
    c.call(tool, args, res)
    c.final(ans)
    return {"template": prompts.index(p), "slots": {"tool": tool}}


@family("writes", "wr_clear_past", weight=3, failure=("unrequested_change", "delete_paraphrase_miss"))
def f_wr_clear_past(r, c, now):
    p = r.choice(["get rid of my overdue reminders", "clear out all the old reminders", "dump the past-due reminders", "wipe my expired reminders"])
    n = r.randint(3, 9)
    c.user(messy(r, p))
    c.discover(["calendar"], ["clear_past_reminders"])
    c.call("clear_past_reminders", {}, f"Cleared {n} past-due reminders.")
    c.final(f"Cleared {n} past-due reminders.")
    return {"template": 0, "slots": {"n": n}}


FORGET_BANK = [("forget that I like cilantro", "cilantro"), ("forget what I told you about my old address", "old address"), ("delete the memory about my gym schedule", "gym schedule"),
               ("please forget my coffee order", "coffee order")]


@family("writes", "wr_forget", weight=2, failure=("unrequested_change",))
def f_wr_forget(r, c, now):
    p, q = r.choice(FORGET_BANK)
    c.user(messy(r, p))
    c.discover(["memory"], ["forget"])
    c.call("forget", {"query": q}, "Forgot 1 matching memory record(s), including correction history.")
    c.final(f"Done - I forgot what I had saved about {q}.")
    return {"template": FORGET_BANK.index((p, q)), "slots": {}}


CALL_TPL = ["call {n}", "give {n} a call", "ring {n}", "phone {n}", "can you call {n} for me", "get {n} on the phone", "facetime {n}"]


@family("outbound", "out_call", weight=5, failure=("direct_real_tool_without_discovery", "outbound_paraphrase_miss"))
def f_out_call(r, c, now):
    nm = pick_contact(r, 0.3)
    said, canon_first = name_forms(r, nm)
    tpl = r.choice(CALL_TPL)
    c.user(messy(r, tpl.format(n=said), protect=(said,)))
    c.discover(["device"], ["place_call"])
    args = {"to": canon_first}
    if tpl.startswith("facetime"):
        args["audio_only"] = False
    f = nm if nm in RELATIVES else first(nm)
    c.call("place_call", args, f"Calling {canon_first}...")
    c.final(r.choice([f"Calling {f} now.", f"Started the call to {f}."]))
    return {"template": CALL_TPL.index(tpl), "slots": {"contact": nm}}


# ================================================================ held-out overlap check

def norm_tokens(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def _h(tokens):
    return hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:12]


_HELD = None


def held_items():
    global _HELD
    if _HELD is None:
        d = json.loads((ASSETS / "heldout_shingles.json").read_text())
        _HELD = [(it["ntok"], it["full"], {k: set(v) for k, v in it["sh"].items()}) for it in d["items"]]
    return _HELD


def max_heldout_overlap(text):
    """Return max normalized n-gram Jaccard (n=min(6,len)) vs every held-out eval prompt; exact match -> 1.0."""
    t = norm_tokens(text)
    if not t:
        return 0.0
    full = _h(t)
    best = 0.0
    for ntok, hfull, sh in held_items():
        if hfull == full:
            return 1.0
        m = min(len(t), ntok)
        if m < 3:
            continue
        n = min(6, m)
        a = {_h(t[i:i + n]) for i in range(len(t) - n + 1)}
        b = sh.get(str(n), set())
        if not a or not b:
            continue
        j = len(a & b) / len(a | b)
        if j > best:
            best = j
    return best


OVERLAP_LIMIT = 0.4  # stricter than the 0.5 required by the spec; rejection is ">= OVERLAP_LIMIT"

# ================================================================ allocation

CATEGORIES = [  # (key, name, pct)
    ("outbound", "outbound_sends_drafts", 18), ("compound", "compound_read_then_act", 22), ("stop", "stop_after_success_no_loop", 10),
    ("reads", "single_tool_reads", 11), ("writes", "writes", 8), ("errors", "error_handling", 7), ("clarify", "clarify_ambiguity", 5),
    ("chat", "chat_no_tool", 8), ("followup", "followups_history", 7), ("guard", "repeated_read_guard", 4)]
N_TOTAL = 1600
N_DEV = 160


def largest_remainder(weights, total):
    s = float(sum(weights.values()))
    raw = {k: total * w / s for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in out:
        if out[k] == 0 and total >= len(weights):
            out[k] = 1
    left = total - sum(out.values())
    order = sorted(weights, key=lambda k: (-(raw[k] - int(raw[k])), k))
    i = 0
    while left > 0:
        out[order[i % len(order)]] += 1
        left -= 1
        i += 1
    while left < 0:
        k = max(out, key=lambda x: (out[x], x))
        out[k] -= 1
        left += 1
    return out


def plan():
    """-> list of (split, category_key, family_id, index_in_family)."""
    out = []
    for key, name, pct in CATEGORIES:
        total = N_TOTAL * pct // 100
        dev = N_DEV * pct // 100
        for split, n in (("train", total - dev), ("dev", dev)):
            fams = {fid: f["weight"] for fid, f in FAMILIES.items() if f["cat"] == key and f["split"] == split}
            assert fams, (key, split)
            alloc = largest_remainder(fams, n)
            for fid in sorted(alloc):
                for j in range(alloc[fid]):
                    out.append((split, key, fid, j))
    return out


def pick_now(r):
    d = dt.date(2026, 10, 12) + dt.timedelta(days=r.randint(0, 70))
    h = r.choice([6, 7, 8, 8, 9, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21])
    m = r.choice([0, 3, 7, 12, 18, 24, 31, 37, 42, 48, 55])
    return dt.datetime(d.year, d.month, d.day, h, m)


def _clock(t):
    m = re.match(r"(\d{1,2})(?::(\d{2}))? (AM|PM)", t)
    return (int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)) * 60 + int(m.group(2) or 0)


def stale_today(c):
    """True if a 'today' read lists items/slots that start before the reference time (unrealistic 'upcoming')."""
    now_min = c.now.hour * 60 + c.now.minute
    for m in c.turn:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                a = tc["function"]["arguments"]
                if tc["function"]["name"] == "find_free_time" and a.get("period") == "today" and a.get("day_start", 0) * 60 < now_min:
                    return True
        if m["role"] == "tool" and m["content"].startswith("Schedule - today"):
            for line in m["content"].splitlines():
                mm = re.match(r"\s+(\d{1,2}(?::\d{2})? [AP]M) - ", line)
                if mm and _clock(mm.group(1)) < now_min:
                    return True
    return False


def build_one(split, cat, fid, j, taken_prompts):
    fam = FAMILIES[fid]
    for attempt in range(60):
        r = rng_for("conv", fid, j, attempt)
        now = pick_now(r)
        set_now(now)
        CUR["split"] = split
        web = fam["web"] or (cat == "chat" and r.random() < 0.5)
        c = Conv(f"tmp-{fid}-{j}", now, r, web=web)
        try:
            meta = fam["fn"](r, c, now)
        except (AssertionError, RuntimeError):
            continue
        if stale_today(c):
            continue
        txt = c.user_text
        key = (split, txt.strip().lower())
        if key in taken_prompts:
            continue
        ov = max([max_heldout_overlap(txt)] + [max_heldout_overlap(t) for role, t in c.history if role == "user"])
        if ov >= OVERLAP_LIMIT:
            continue
        taken_prompts.add(key)
        return c, meta, ov, attempt
    raise RuntimeError(f"could not build {fid} #{j}")


def jdump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def est_tokens(obj):
    return len(json.dumps(obj, ensure_ascii=False)) / 4.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=str(HERE))
    ap.add_argument("--ext", default=None, help="comma-separated add-on modules to load (default: all present)")
    args = ap.parse_args()
    load_extensions(EXTENSIONS if args.ext is None else tuple(args.ext.split(",")))
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = plan()
    taken = set()
    built = []
    for split, cat, fid, j in items:
        c, meta, ov, attempt = build_one(split, cat, fid, j, taken)
        built.append({"split": split, "cat": cat, "fid": fid, "j": j, "conv": c, "meta": meta, "overlap": ov, "attempt": attempt})
    # deterministic shuffle within each split so categories interleave
    cat_name = {k: n for k, n, _ in CATEGORIES}
    rows = {"train": [], "dev": []}
    prov = {"train": [], "dev": []}
    convs = {"train": [], "dev": []}
    shuf = rng_for("shuffle")
    for split in ("train", "dev"):
        lst = [b for b in built if b["split"] == split]
        lst.sort(key=lambda b: (b["fid"], b["j"]))
        shuf.shuffle(lst)
        for idx, b in enumerate(lst, 1):
            c = b["conv"]
            base = f"v3-{b['cat']}-{split}-{idx:04d}"
            c.cid = base
            rws = c_rows(c, base)
            rows[split].extend(rws)
            gold = c.gold
            scen = hashlib.sha1((b["fid"] + canon(b["meta"].get("slots", {})) + c.user_text + now_line(c.now)).encode()).hexdigest()[:10]
            prov[split].append({
                "id": base, "conversation_id": base, "split": split, "category": cat_name[b["cat"]], "category_key": b["cat"],
                "family_id": b["fid"], "scenario_id": f"{b['fid']}-{scen}", "template_id": f"{b['fid']}-t{b['meta'].get('template', 0)}",
                "gold_calls": gold, "targeted_failure_class": FAMILIES[b["fid"]]["failure"], "reference_now": now_line(c.now),
                "user_prompt": c.user_text, "user_prompt_clean": c.user_clean, "history_user_prompts": [t for r_, t in c.history if r_ == "user"],
                "discovery_modes": c.modes, "protocol_variant": ("S" if not c.preload else ("SP-hit" if c.sp_kind == "hit" else "SP-decoy")),
                "preload": c.preload, "preload_contains_needed": (set(c.sp_need) <= set(c.preload)) if c.preload else None, "history_turns": len(c.history), "n_steps": len(rws),
                "slots": b["meta"].get("slots", {}), "catalog_variant": "web" if c.web else "personal_no_web",
                "heldout_max_jaccard": round(b["overlap"], 3), "generation_attempt": b["attempt"]})
            cv = c.conversation()
            cv.update({"id": base, "split": split, "category": cat_name[b["cat"]], "family_id": b["fid"]})
            convs[split].append(cv)
    for split in ("train", "dev"):
        # V2 replay rows (v2_replay.py) go last, byte-for-byte, so V3 keeps V2's routing-contract skill.
        replay = (HERE / f"v2_replay.{split}.jsonl").read_text()
        (out / f"{split}.jsonl").write_text("".join(jdump(x) + "\n" for x in rows[split]) + replay)
        (out / f"{split}.provenance.jsonl").write_text("".join(jdump(x) + "\n" for x in prov[split]))
    (out / "conversations.jsonl").write_text("".join(jdump(x) + "\n" for s in ("train", "dev") for x in convs[s]))
    write_stats(out, rows, prov)


def c_rows(c, base):
    rws = c.rows(base)
    return rws


def write_stats(out, rows, prov):
    import statistics
    stats = {"version": VERSION, "seed": GLOBAL_SEED, "splits": {}}
    for split in ("train", "dev"):
        rs, ps = rows[split], prov[split]
        toks = sorted(est_tokens(x) for x in rs)
        tgt = sorted(est_tokens(x["messages"][-1]) for x in rs)
        by_cat, by_step, by_idx = {}, {}, {}
        for x in rs:
            k_ = int(x["id"].rsplit("-s", 1)[1])
            by_idx[f"s{k_}"] = by_idx.get(f"s{k_}", 0) + 1
        for p in ps:
            by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
        for x in rs:
            tcs = x["messages"][-1].get("tool_calls")
            if not tcs:
                k = "final"
            elif tcs[0]["function"]["name"] == DISCOVERY:
                k = "discovery"
            elif "properties" not in next(t_ for t_ in x["tools"] if t_["function"]["name"] == tcs[0]["function"]["name"])["function"]["parameters"]:
                k = "stub_call"
            else:
                k = "tool_call"
            by_step[k] = by_step.get(k, 0) + 1
        stats["splits"][split] = {
            "conversations": len(ps), "rows": len(rs), "rows_per_conversation": round(len(rs) / len(ps), 2),
            "by_category": by_cat, "rows_by_target_type": by_step, "rows_by_step_index": dict(sorted(by_idx.items(), key=lambda kv: int(kv[0][1:]))),
            "families": len({p["family_id"] for p in ps}),
            "variants": {v: sum(1 for p in ps if p["protocol_variant"] == v and p["gold_calls"]) for v in ("S", "SP-hit", "SP-decoy")},
            "first_discovery_stub_first": sum(1 for p in ps if p["discovery_modes"] and p["discovery_modes"][0] == "stub"),
            "first_discovery_explicit": sum(1 for p in ps if p["discovery_modes"] and p["discovery_modes"][0] == "explicit"), "scenarios": len({p["scenario_id"] for p in ps}),
            "templates": len({p["template_id"] for p in ps}),
            "row_tokens_est_chars_div4": {"mean": round(statistics.mean(toks)), "p50": round(toks[len(toks) // 2]),
                                          "p95": round(toks[int(len(toks) * 0.95)]), "max": round(toks[-1])},
            "target_tokens_est_chars_div4": {"mean": round(statistics.mean(tgt), 1), "p95": round(tgt[int(len(tgt) * 0.95)], 1), "max": round(tgt[-1], 1)},
        }
    (out / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n")
    write_card(out, stats, prov)


def write_card(out, stats, prov):
    S = stats["splits"]
    lines = ["# Ling dataset V3 data card (generated by generate.py)", "",
             f"Version `{VERSION}`, seed {GLOBAL_SEED}. Synthetic; no real data. Task: Wisp MODEL-LED tool calling, protocol S (Ling chooses from ALL tools; name-only stubs + get_tool_schemas -> receipt -> real schemas -> calls -> answer).", "",
             "## Counts", "", "| split | conversations | rows (one per assistant step) | rows/conv | families | scenarios | templates |", "|---|---|---|---|---|---|---|"]
    for s in ("train", "dev"):
        d = S[s]
        lines.append(f"| {s} | {d['conversations']} | {d['rows']} | {d['rows_per_conversation']} | {d['families']} | {d['scenarios']} | {d['templates']} |")
    lines += ["", "## Conversations by category", "", "| category | train | dev | target % |", "|---|---|---|---|"]
    pct = {n: p for _, n, p in CATEGORIES}
    for _, n, _p in CATEGORIES:
        lines.append(f"| {n} | {S['train']['by_category'].get(n, 0)} | {S['dev']['by_category'].get(n, 0)} | {pct[n]} |")
    lines += ["", "## Rows by target type", "", "| split | discovery (get_tool_schemas) | stub call (implicit discovery) | executed tool call | final |", "|---|---|---|---|---|"]
    for s in ("train", "dev"):
        b = S[s]["rows_by_target_type"]
        lines.append(f"| {s} | {b.get('discovery', 0)} | {b.get('stub_call', 0)} | {b.get('tool_call', 0)} | {b.get('final', 0)} |")
    lines += ["", "## Length estimates (characters / 4; no tokenizer was run)", "", "| split | row mean | row p50 | row p95 | row max | target mean | target p95 | target max |", "|---|---|---|---|---|---|---|---|"]
    for s in ("train", "dev"):
        a, t = S[s]["row_tokens_est_chars_div4"], S[s]["target_tokens_est_chars_div4"]
        lines.append(f"| {s} | {a['mean']} | {a['p50']} | {a['p95']} | {a['max']} | {t['mean']} | {t['p95']} | {t['max']} |")
    ps = [p for s in ("train", "dev") for p in prov[s]]
    fc = {}
    for p in ps:
        for f in p["targeted_failure_class"]:
            fc[f] = fc.get(f, 0) + 1
    lines += ["", "## Targeted failure classes (conversations tagged; a conversation may carry several)", "", "| failure class | conversations |", "|---|---|"]
    for f, n in sorted(fc.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {f} | {n} |")
    lines += ["", "## Protocol S mix (first discovery of each tool-using conversation)", "", "| split | stub-first | explicit get_tool_schemas first | stub-first share |", "|---|---|---|---|"]
    for s_ in ("train", "dev"):
        a_, e_ = S[s_]["first_discovery_stub_first"], S[s_]["first_discovery_explicit"]
        lines.append(f"| {s_} | {a_} | {e_} | {a_ / max(1, a_ + e_):.0%} |")
    lines += ["", "## S vs SP (tool-using conversations)", "", "| split | pure S (no preload) | SP-hit (needed tool preloaded, called directly at round 0) | SP-decoy (preload wrong, call the stub) | SP share |", "|---|---|---|---|---|"]
    for s_ in ("train", "dev"):
        v_ = S[s_]["variants"]
        tot_ = max(1, sum(v_.values()))
        lines.append(f"| {s_} | {v_['S']} | {v_['SP-hit']} | {v_['SP-decoy']} | {(v_['SP-hit'] + v_['SP-decoy']) / tot_:.0%} |")
    lines += ["", "## Rows per step index (s0 = first assistant step of a conversation)", "", "| split | " + " | ".join(f"s{i}" for i in range(8)) + " |", "|---|" + "---|" * 8]
    for s_ in ("train", "dev"):
        b_ = S[s_]["rows_by_step_index"]
        lines.append(f"| {s_} | " + " | ".join(str(b_.get(f"s{i}", 0)) for i in range(8)) + " |")
    lines += ["", "## Design notes", "",
              "- Protocol S: round 0 offers `get_tool_schemas` plus a name-only stub (`parameters: {type: object}`, no description/properties) for every admitted catalog tool (the with/without-web catalog variant decides which). Gold either calls the needed tool name(s) through their stubs (an implicit discovery: not executed, the host returns the standard receipt as each stub call's result; two or three stubs in one message for a chain) or calls `get_tool_schemas` for a whole family/tool list. After the receipt the next round offers the loaded REAL schemas, then (while fewer than two discoveries, implicit included, have happened) `get_tool_schemas` and stubs for the remaining tools; the assistant must re-issue the call with full arguments, never answer from the receipt. Stub-call arguments are `{}` or a partial best guess. A multi-stub message is ONE implicit discovery, families listed in call order. Executed-call repeats are checked on real calls only.",
              "- One training row per ASSISTANT STEP (discovery call, stub call, real call(s), final answer). Only the last assistant message of a row has `training:true`; every earlier assistant message (in-turn or history) has `training:false`.",
              "- Round 0 offers only `get_tool_schemas`; after a successful discovery the offered tools are the loaded real schemas (registry-exact) plus `get_tool_schemas` while fewer than two discovery calls were made. Discovery receipts follow the host's cumulative expansion; a rejected discovery (tools outside the selected families) uses the host's rejection text and is never a training target.",
              "- The scoped system prompt (message 0) is rebuilt per step from the captured model-led prompt plus the tool-specific rule paragraphs triggered by the loaded tools; messages 1-2 are the captured capability catalog + context note. Identity is synthetic (Test User / test.user@example.invalid).",
              "- Send fidelity: send_message / send_email / schedule_send / reply_to_email / draft bodies contain ONLY the user's stated content (quoted text verbatim; otherwise converted to the recipient's point of view, capitalized and punctuated). Emails use exactly `Hi <First>,\\n\\n<content>\\n\\nTest User`. Facts read earlier in the turn (calendar times, note text) may be relayed. After a successful send the final answer quotes the exact body. Reminder/event/note/memory titles and bodies likewise use the user's words. Enforced by validate.py (`send_body_fidelity`, `write_fidelity`, `final_quotes_body`).",
              "- Split is by family: every family (authored scenario archetype + phrasing register) belongs to exactly one split; dev families use phrasing registers absent from train, and several content banks (message/email topics, web, notes, note+reminder pairs, mail-derived events) are partitioned so dev topics do not appear in train.",
              "- Prompts are checked at generation and again in validate.py against hashed shingles of the Ling eval corpus (459 prompts incl. follow-up context; n=min(6,len); this dataset rejects Jaccard >= 0.4, stricter than the required > 0.5). The eval prompts themselves are not stored.",
              "", "## Limitations", "",
              "- Everything is synthetic and author-written. Tool results follow the shapes of Wisp's real tools but are condensed (<400 chars) and cleaner than live output; a model may over-trust these shapes.",
              "- SP (Wisp 1.3.2): in ~30% of tool-using conversations round 0 also carries 6-12 PRELOADED real schemas from a (fallible) lexical shortlist; they are loaded from the start, consume no discovery attempt and count toward the 24-schema cap (receipts list them in available_tools). Half of those preloads contain the needed tool(s), which gold calls directly with full arguments (no loading step); the other half are plausible-but-wrong decoys from adjacent families, which gold ignores while calling the right tool through its stub. Provenance records `preload`, `preload_contains_needed` and `protocol_variant`.",
              "- The capability preamble uses the host's new wording (tools arrive with full schemas or by name only; call by name, schema loads, call again next step; two loading steps, 24 schemas).",
              "- Round-0 and later rows now carry ~160 name-only stubs, which raises row length by roughly 3-4k estimated tokens versus the shortlist protocol.",
              "- The tool-scoped system prompt is reconstructed from captured variants with a hand-written trigger map; it is not produced by running Wisp's `build_system`. The with/without-web catalog variants approximate the host's blocked-tool logic.",
              "- Phrasing comes from a finite template set with rule-based noise (casing, abbreviations, one-letter typos); it is not real user language. Content banks (messages, events, reminders, contacts, cities) are small and partly shared across splits, so dev is not an independent test of generalisation.",
              "- Gold choices encode policy decisions: casual 'tell/let X know' routes to Messages; channel-less 'get in touch with X' asks a question; sends always call the send tool (host confirmation decides) and a denial ends the turn; availability uses `find_free_time` once; independent reads may be parallel or sequential (both appear).",
              "- `get_upcoming`/`get_weather` are given ISO dates for named weekdays; this assumes the host accepts YYYY-MM-DD there (it does for the shared period resolver).",
              "- Not covered: attachments, voice, MCP/skill tools, shell/AppleScript, non-English, history that contains earlier tool calls (history is plain text), very long tool outputs, or real-world error variety.",
              "- Token numbers are chars/4 estimates, no tokenizer was run. Trainer masking must be verified on prepared labels (final-step-only per row). Counts are exactly 900 train / 100 dev conversations; category percentages are the rounded targets.",
              "- Quality gains from this data are unmeasured until Ling is retrained and re-evaluated on the held-out model-led eval.", ""]
    (out / "DATA_CARD.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
