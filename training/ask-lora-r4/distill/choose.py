#!/usr/bin/env python3
"""Pick one self-distilled reply per prompt from gen.py's samples, rejecting only real defects.

The base model's own chat style (length, emoji, warmth, the user's name) is what we want to keep, so nothing here
scores style: a sample is rejected only for a defect the Ask model must never learn - a tool call where chat was
wanted, an empty or cut-off reply, a "couldn't find / can't look up" non-reply, an internal leaking into the text
(tool or skill name, JSON, call syntax, the untrusted-content banner), or, for rows that carry ``meta.recall``, a reply
that drops a fact it was given. A sample can have several reasons; all are recorded. Per prompt the FIRST passing
sample by k order is chosen (no best-of ranking, so the choice does not drift from the model's own distribution).

Writes ``chosen.jsonl`` (the raw record of each chosen sample with ``reasoning`` stripped of surrounding whitespace,
None when empty; sorted in partial order) and ``stats.json`` (per family and overall: counts, ids with no passing
sample and why, reply shape of the chosen replies next to the scripted targets', rejects by reason).

    choose.py --partial $TMPDIR/r4/partial.jsonl --raw raw/chat.jsonl --out-chosen chosen.jsonl \\
        --out-stats stats.json [--raw raw/more.jsonl ...] [--k 2] [--no-hash-check]

Stdlib only; reads the files, calls no service and loads no model.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen import request_hash  # noqa: E402  (the sampler's own hash: object/string tool args hash alike)

RECALL_KEY = "recall"        # meta field (str or list of str) of facts a reply must carry; may be renamed
USER_NAME = "Sam"            # the synthetic user's name in the prompts

# --- copied from training/ask-lora-r2/builder/validate.py (FINAL_LEAK, CHAT_RECEIPT); not imported, validate.py loads
# --- the whole service. Keep in step by hand if validate.py changes.
FINAL_LEAK_CALL = re.compile(r"\{\"|\bUNTRUSTED\b|haven['’]t checked (?:a |any )?current|current personal source|"
                             r"\b[a-z]+_[a-z_]+\(", re.I)
FINAL_LEAK_STALL = re.compile(r"haven['’]t checked (?:a |any )?current|current personal source", re.I)  # source-stall part
CHAT_RECEIPT = re.compile(r"\b(?:didn['’]t|did not|won['’]t|haven['’]t) (?:send|sent|save|saved|add|added|set|"
                          r"schedule|search|look)\w*\b|\bnothing (?:was|got) (?:sent|saved|added)\b|"
                          r"couldn['’]t find any (?:web )?results", re.I)
# --- end copy

# "I couldn't find / can't look up / don't have access ..." non-replies, with straight or curly apostrophes.
# validate.py FINAL_GLITCH: template glitches a final must never carry
FINAL_GLITCH = re.compile(r"\b(?:to|for) on\b|\b\d+ (?:today|yesterday) emails?\b", re.I)
_AP = "['’]"
_VERB = r"(?:find|look\s*up|access|locate|retrieve)"
STALL = re.compile(
    rf"\bI(?:\s+(?:could\s*not|couldn{_AP}t|can\s*not|cannot|can{_AP}t|wasn{_AP}t\s+able\s+to|was\s+not\s+able\s+to"
    rf"|am\s+(?:not\s+able|unable)\s+to|{_AP}m\s+(?:not\s+able|unable)\s+to)|{_AP}m\s+(?:not\s+able|unable)\s+to)"
    rf"\s+{_VERB}\b"
    rf"|\bI\s+(?:do\s+not|don{_AP}t)\s+have\s+(?:any\s+)?access\b|\bI\s+have\s+no\s+access\b"
    # capability refusals of a chat request ("I don't have the ability to play games", "I can't play games")
    rf"|\bI\s+(?:do\s+not|don{_AP}t)\s+have\s+the\s+(?:ability|capability)\s+to\b"
    rf"|\bI(?:\s+(?:can\s*not|cannot|can{_AP}t|am\s+(?:not\s+able|unable)\s+to)|{_AP}m\s+(?:not\s+able|unable)\s+to)"
    rf"\s+(?:play|browse|search|check|tell\s+jokes|do\s+that)\b", re.I)
# a single-turn chat reply that invents an earlier exchange: a running score, "as I said", "last time" (audit
# chatstyle-3: prompts like "rock. your move" or "thanks, needed that" with no history)
ANTECEDENT = re.compile(r"\bthe\s+score\s+(?:is|stands)\b|\b(?:one|two|three|\d)\s*(?:to|-)\s*(?:nothing|zero|one|two|\d)\b"
                        r"\s+(?:to\s+(?:me|you)|lead)|\bas\s+(?:I|we)\s+(?:said|mentioned|discussed)\b|"
                        r"\byou\s+(?:mentioned|said|told\s+me)\s+(?:earlier|before)\b|\blast\s+time\s+(?:we|you)\b|"
                        r"\bour\s+(?:last|previous|earlier)\s+(?:chat|conversation|game|round)\b", re.I)

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U00002B00-\U00002BFF"
                   "\U00002190-\U000021FF\U0000FE0F\U0000200D\U000020E3\U00002300-\U000023FF\U000025A0-\U000025FF"
                   "\U00003030\U0000303D\U00002122\U000000A9\U000000AE]")
SKILL_LINE = re.compile(r"^- ([a-z0-9][a-z0-9-]*): ", re.M)
NAME_RE = re.compile(rf"\b{USER_NAME}\b")


def system_text(row: dict) -> str:
    return "\n".join(m["content"] for m in row["messages"][:3]
                     if m.get("role") == "system" and isinstance(m.get("content"), str))


def skill_names(row: dict) -> list[str]:
    """Names from the 'Installed skills' listing ('- idea-refine: ...'), not every hyphenated word of the prompt."""
    sys_text = system_text(row)
    if "Installed skills" not in sys_text:
        return []
    return sorted(set(SKILL_LINE.findall(sys_text.split("Installed skills", 1)[1])))


EXTRA_TOOL_NAMES: set[str] = set()   # --tool-names: the whole live registry, as validate.py's final:leak checks


def tool_names(row: dict) -> list[str]:
    offered = {(t.get("function") or {}).get("name") for t in row.get("tools") or []} - {None}
    return sorted(offered | {n for n in EXTRA_TOOL_NAMES if "_" in n})


def tool_leak(content: str, names: list[str]) -> list[str]:
    """Tool names in the reply. Names with an underscore are code identifiers: any whole-word hit counts. Plain
    words (calculate, recall, remember, forget) are ordinary English, so they count only quoted/backticked or as a
    call ``name(``."""
    hits = []
    for n in names:
        if "_" in n:
            if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", content):
                hits.append(n)
        elif re.search(rf"[`\"'“]{re.escape(n)}[`\"'”]|(?<![\w-]){re.escape(n)}\(", content):
            hits.append(n)
    return hits


def skill_leak(content: str, names: list[str]) -> list[str]:
    return [n for n in names if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", content)]


def json_leak(content: str) -> bool:
    if '{"' in content or re.search(r"```\s*json", content, re.I):
        return True
    for line in content.splitlines():
        s = line.strip()
        if s[:1] in "{[" and s[-1:] in "}]":
            try:
                json.loads(s)
                return True
            except ValueError:
                pass
    return False


def squash(s: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed - what the recall check compares."""
    return " ".join(re.sub(r"[\W_]+", " ", s.lower()).split())


_NUMWORD = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
_RSTOP = {"the", "a", "an", "is", "of", "to", "and", "on", "at", "in", "for", "my", "your"}


def recall_tokens(s: str) -> set[str]:
    """Tokens for the recall match: number words as digits, "2:00 PM" ~ "2pm", "64°F" ~ "64F", plus the split
    pieces of a merged number+unit ("9am" also gives "9")."""
    s = s.lower().replace("°", "")
    s = re.sub(r"(\d+):00\s*", r"\1", s)
    s = re.sub(r"(\d+)\s*(am|pm|f|c|km|kg|g)\b", r"\1\2", s)
    out = set()
    for t in re.findall(r"[a-z0-9/]+", s):
        t = _NUMWORD.get(t, t)
        out.add(t)
        m = re.fullmatch(r"(\d+)([a-z]+)", t)
        if m:
            out.add(m.group(1))
    return out


def ref_present(ref: str, have: set[str]) -> bool:
    need = [t for t in recall_tokens(ref) if t not in _RSTOP and not (t.isdigit() and any(
        x != t and x.startswith(t) and not x.isdigit() for x in recall_tokens(ref)))]
    return all(t in have or t.rstrip("s") in have or t + "s" in have for t in need)


def recall_missing(content: str, recall) -> list[str]:
    """The refs a reply misses. A ref counts when all its tokens appear (order-free, so "a weekly limit for food
    delivery" carries "weekly delivery limit"). The reply passes (returns []) when it carries the FIRST ref, the
    detail the question asks for, and at least half of all refs; the rest are context (the starter's name) that a
    correct answer may leave out."""
    need = [recall] if isinstance(recall, str) else list(recall or [])
    have = recall_tokens(content)
    hits = [ref_present(r, have) for r in need]
    if not need or (hits[0] and sum(hits) * 2 >= len(need)):
        return []
    return [r for r, h in zip(need, hits) if not h]


def reject_reasons(rec: dict, row: dict) -> list[str]:
    """Every defect of one sample, in a fixed order; empty list = passes."""
    if rec.get("error"):
        return ["error"]
    out = []
    if rec.get("no_choices"):
        out.append("no_choices")
    content = rec.get("content") or ""
    if rec.get("tool_calls") or "<|tool_call>" in content or "<tool_call" in content:
        out.append("tool_call")       # the second form: an unparsed call left in the text (validate.py markup_in_content)
    if not content.strip():
        out.append("empty")
    if rec.get("finish_reason") == "length":
        out.append("truncated")
    if not (rec.get("reasoning") or "").strip():
        out.append("no_thought")      # every distilled row keeps the base's own thinking (r3's thinking collapsed)
    if FINAL_GLITCH.search(content):
        out.append("glitch")
    if STALL.search(content) or CHAT_RECEIPT.search(content) or FINAL_LEAK_STALL.search(content):
        out.append("stall")
    if (tool_leak(content, tool_names(row)) or skill_leak(content, skill_names(row)) or "UNTRUSTED" in content
            or json_leak(content) or FINAL_LEAK_CALL.search(content)):
        out.append("leak")
    if sum(m["role"] == "user" for m in row["messages"][:-1]) == 1 and ANTECEDENT.search(content):
        out.append("antecedent")
    rc = (row["meta"] or {}).get(RECALL_KEY)
    if rc and recall_missing(content, rc):
        out.append("recall")
    none = (row["meta"] or {}).get(NONE_KEY)
    if none is not None and not NONE_ACK.search(content) and invented(content, history_text(row), none):
        out.append("invented")
    return out


# session_chat_r4 (refs.py): rec_user / rec_asst / rec_both must state every ref; rec_none must not invent the detail.
RECALL_KINDS = {"rec_user", "rec_asst", "rec_both"}
NONE_KEY = "_rec_none_ok"     # set by apply_refs: the scenario's whitelisted words (a list, possibly empty)
WORD = re.compile(r"[A-Za-zÀ-ɏ0-9]+(?:['’][A-Za-z]+)*")
SEGMENT = re.compile(r"(?<=[.!?:;*])\s+|\n+|\s[—–-]\s|[\"“”(]")
LEAD = re.compile(r"^[\s*#>•\-\d.)]+")
ALWAYS_OK = {"i", "wisp", "sam"}
# rec_none: a reply that first says the detail never came up may go on to suggest options from general knowledge
# ("We haven't picked a dessert wine yet, but a Sauternes would suit the tart"); only an unacknowledged answer is
# checked for invented names and numbers.
NONE_ACK = re.compile(r"\b(?:haven['’]t|have\s+not|didn['’]t|did\s+not|don['’]t\s+(?:think|recall|remember|have|see)|"
                      r"do\s+not\s+(?:think|recall|remember|have|see)|never|not\s+yet|no\s+one)\b[^.!?]{0,60}?"
                      r"\b(?:mention\w*|pick\w*|select\w*|settl\w*|decid\w*|chose|chosen|said|say|talk\w*|"
                      r"discuss\w*|come\s+up|came\s+up|share\w*|detail|specific|yet|told)\b", re.I)


def history_text(row: dict) -> str:
    """What the conversation holds: every user and assistant message before the target."""
    return "\n".join(m.get("content") or "" for m in row["messages"][:-1] if m["role"] in ("user", "assistant"))


def invented(content: str, history: str, ok) -> list[str]:
    """Names (capitalised mid-sentence words) and numbers in the reply that the history does not contain."""
    hay = " " + squash(history) + " "
    allowed = ALWAYS_OK | {squash(o) for o in ok}
    found = []
    for seg in SEGMENT.split(content):
        toks = WORD.findall(LEAD.sub("", seg))
        for j, t in enumerate(toks):
            base = re.sub(r"['’].*$", "", t)
            is_num = any(ch.isdigit() for ch in base)
            is_name = j > 0 and base[:1].isupper() and base != "I"
            if (is_num or is_name) and squash(base) not in allowed and f" {squash(base)} " not in hay:
                found.append(t)
    return found


def apply_refs(rows: list[dict], refs: dict) -> None:
    for r in rows:
        q = refs.get(r["meta"]["id"])
        if not q:
            continue
        r["meta"] = dict(r["meta"])
        if q["kind"] in RECALL_KINDS:
            r["meta"][RECALL_KEY] = q["ref"]
        elif q["kind"] == "rec_none":
            r["meta"][NONE_KEY] = q["ok"]


# ------------------------------------------------------------------ stats
def pct(xs: list[float], p: float):
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * p / 100
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (i - lo), 1)


def sentences(text: str) -> int:
    t = EMOJI.sub("", text)
    return len([s for s in re.split(r"(?<=[.!?…])[\s\"'”’)*_]+|\n+", t) if re.search(r"\w", s)])


def ends_question(text: str) -> bool:
    return EMOJI.sub("", text).rstrip(" \t\n*_\"'”’)").endswith("?")


def shape(replies: list[dict]) -> dict:
    """Shape of chosen replies; each item is a raw record."""
    n = len(replies)
    texts = [r["content"] for r in replies]
    lens = [len(t) for t in texts]
    thoughts = [len(r["reasoning"].split()) for r in replies if r.get("reasoning")]
    toks = [(r.get("usage") or {}).get("completion_tokens") for r in replies]
    toks = [t for t in toks if isinstance(t, (int, float))]
    share = lambda c: round(c / n, 3) if n else None  # noqa: E731
    return {
        "chars": {"p25": pct(lens, 25), "p50": pct(lens, 50), "p90": pct(lens, 90)},
        "sentences_p50": pct([sentences(t) for t in texts], 50),
        "question_end_share": share(sum(ends_question(t) for t in texts)),
        "question_any_share": share(sum("?" in t for t in texts)),
        "emoji_share": share(sum(bool(EMOJI.search(t)) for t in texts)),
        "name_share": share(sum(bool(NAME_RE.search(t)) for t in texts)),
        "think_share": share(len(thoughts)),
        "thought_words": {"p50": pct(thoughts, 50), "p90": pct(thoughts, 90)},
        "completion_tokens": {"p50": pct(toks, 50), "p90": pct(toks, 90)},
    }


def scripted_chars(rows: list[dict]) -> dict:
    lens = []
    for r in rows:
        c = r["messages"][-1].get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if isinstance(c, str) and c.strip():
            lens.append(len(c))
    return {"p25": pct(lens, 25), "p50": pct(lens, 50), "p90": pct(lens, 90), "n": len(lens)}


def group_stats(rows: list[dict], samples: dict, chosen: dict) -> dict:
    ids = [r["meta"]["id"] for r in rows]
    recs = [s for i in ids for s in samples.get(i, [])]
    reasons = Counter(x for s in recs for x in s["_reasons"])
    firsts = [samples[i][0] for i in ids if samples.get(i)]
    no_pass = {i: [{"k": s["k"], "reasons": s["_reasons"]} for s in samples[i]] for i in ids
               if i not in chosen and samples.get(i)}
    return {
        "prompts": len(ids), "with_samples": sum(1 for i in ids if samples.get(i)), "samples": len(recs),
        "chosen": sum(1 for i in ids if i in chosen),
        "no_samples": [i for i in ids if not samples.get(i)],
        "no_pass": no_pass,
        # rows that keep their scripted reply (think=False) because no base sample passed
        "scripted_kept": {"count": len(no_pass), "ids": sorted(no_pass)},
        "rejects_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "first_sample_pass_rate": round(sum(not s["_reasons"] for s in firsts) / len(firsts), 3) if firsts else None,
        "chosen_shape": shape([chosen[i] for i in ids if i in chosen]),
        # every base sample that wrote a text reply (rejected ones included): the style the targets must keep
        "base_text_samples_shape": shape([s for s in recs if (s.get("content") or "").strip() and not s.get("error")]),
        "scripted_chars": scripted_chars([r for r in rows if r["meta"]["id"] in chosen]),
    }


# ------------------------------------------------------------------ io
def read_jsonl(path: str):
    for line in open(path):
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue          # a line cut off by a kill or still being written


def run(partial: str, raws: list[str], k: int | None = None, hash_check: bool = True, refs: list[str] = ()):
    all_rows = list(read_jsonl(partial))
    for p in refs:
        apply_refs(all_rows, json.load(open(p)))
    by_id = {r["meta"]["id"]: r for r in all_rows}
    cur = {i: request_hash(r["messages"][:-1], r.get("tools")) for i, r in by_id.items()}
    recs = [rec for path in raws for rec in read_jsonl(path)
            if rec.get("id") in by_id and (k is None or rec["k"] < k)]
    # An id regenerated after its prompt changed (a separate raw file, same k values) keeps only the samples drawn
    # for the current prompt; ids with no fresh sample keep theirs, so the stale_hash reject still reports them.
    fresh = {rec["id"] for rec in recs if rec.get("request_hash") == cur[rec["id"]]}
    if hash_check:
        recs = [rec for rec in recs if rec["id"] not in fresh or rec.get("request_hash") == cur[rec["id"]]]
    got = {}                                  # (id, k) -> record; a good record beats an error, then first wins
    for rec in recs:
        key = (rec["id"], rec["k"])
        if key not in got or (got[key].get("error") and not rec.get("error")):
            got[key] = rec
    orphans = sorted({i for i, _ in got} - set(by_id))
    fams = {r["family"] for r in got.values()} | {by_id[i]["meta"]["family"] for i, _ in got}
    rows = [r for r in all_rows if r["meta"]["family"] in fams]
    samples = defaultdict(list)
    for (i, kk), rec in sorted(got.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        row = by_id[i]
        rec = dict(rec)
        rec["_reasons"] = reject_reasons(rec, row)
        if (hash_check and rec.get("request_hash") and not rec.get("error")
                and rec["request_hash"] != cur[i]):
            rec["_reasons"].append("stale_hash")
        samples[i].append(rec)
    chosen = {}
    for i, ss in samples.items():
        ok = next((s for s in ss if not s["_reasons"]), None)
        if ok:
            ok = {k2: v for k2, v in ok.items() if k2 != "_reasons"}
            ok["reasoning"] = (ok.get("reasoning") or "").strip() or None
            chosen[i] = ok
    stats = {"overall": group_stats(rows, samples, chosen), "orphan_ids": orphans,
             "families": {f: group_stats([r for r in rows if r["meta"]["family"] == f], samples, chosen)
                          for f in sorted(fams)}}
    # which sample index won (k >= 2 are topup.sh rounds, run only for ids with no passing sample in k 0-1)
    for f, g in stats["families"].items():
        g["chosen_k"] = dict(sorted(Counter(c["k"] for i, c in chosen.items() if by_id[i]["meta"]["family"] == f).items()))
    stats["overall"]["chosen_k"] = dict(sorted(Counter(c["k"] for c in chosen.values()).items()))
    return rows, chosen, stats


def summary(stats: dict) -> str:
    def line(name, g):
        s, c, b = g["chosen_shape"], g["scripted_chars"], g["base_text_samples_shape"]
        return (f"{name:22} prompts {g['prompts']:4} samples {g['samples']:5} chosen {g['chosen']:4} "
                f"first-pass {g['first_sample_pass_rate']}  chars p50 {s['chars']['p50']} (base {b['chars']['p50']}, "
                f"scripted {c['p50']})  emoji {s['emoji_share']} (base {b['emoji_share']}) "
                f"?end {s['question_end_share']} (base {b['question_end_share']}) think {s['think_share']}")
    out = [line(f, g) for f, g in stats["families"].items()] + [line("ALL", stats["overall"])]
    o = stats["overall"]
    out.append(f"rejects by reason: {o['rejects_by_reason']}")
    out.append(f"no passing sample: {len(o['no_pass'])}; no samples yet: {len(o['no_samples'])}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial", required=True)
    ap.add_argument("--raw", action="append", required=True, help="gen.py output; repeatable")
    ap.add_argument("--out-chosen", required=True)
    ap.add_argument("--out-stats", required=True)
    ap.add_argument("--k", type=int, default=None, help="only samples with k < K")
    ap.add_argument("--tool-names", help="JSON list of every live tool name (refs.py --registry-out)")
    ap.add_argument("--refs", action="append", default=[], help="refs.py output (session_chat recall checks)")
    ap.add_argument("--no-hash-check", action="store_true", help="do not reject samples whose request_hash differs")
    a = ap.parse_args(argv)
    if a.tool_names:
        EXTRA_TOOL_NAMES.update(json.load(open(a.tool_names)))
    rows, chosen, stats = run(a.partial, a.raw, a.k, not a.no_hash_check, a.refs)
    with open(a.out_chosen, "w") as f:
        for r in rows:
            if r["meta"]["id"] in chosen:
                f.write(json.dumps(chosen[r["meta"]["id"]], ensure_ascii=False) + "\n")
    with open(a.out_stats, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(summary(stats))


if __name__ == "__main__":
    sys.exit(main())
