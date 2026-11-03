"""recall_dev_r4 and session_style_dev (scen_r4_devsets.py): counts, fields, gating and non-overlap.

Run from this directory (about a minute: the overlap tests regenerate session_recall_r4 and instructions_r4 in memory):

    ASK_LORA_ROUND=4 ~/Desktop/MOE_Project/.venv/bin/python -m pytest -q test_devsets.py

DEVSETS_FAST=1 skips those two regenerations (the other reference sets are still checked).

Non-overlap means, against session_chat_r4, session_recall_r4, instructions_r4 (every user turn of every history and prompt,
and every assistant sentence), the toolbench cases of every split (all history user turns, bound over world/fact seeds
0-49, so the `session` cases included), the chat eval (`chateval_prompts.yaml` and `chateval/prompts_r4.yaml`):
  - no folded user turn is equal, no char-trigram Jaccard is 0.5 or more (the check_overlap hit bar is 0.7, its review bar 0.5);
  - no shared toolbench 6-gram, no toolbench fact value;
  - no shared 8-word run in any assistant sentence;
  - no thread id, and no distinctive name, number or topic word the user states in a dev thread appears in a reference thread.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

os.environ["ASK_LORA_ROUND"] = "4"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytest  # noqa: E402

import scen  # noqa: E402,F401  (must come before the scen_r4 modules)
import leak  # noqa: E402
import scen_r4_devsets as D  # noqa: E402
import scen_r4_session_chat as S  # noqa: E402

REPO = HERE.parents[2]
PY = sys.executable
FAST = os.environ.get("DEVSETS_FAST") == "1"


# ------------------------------------------------------------------ the scenarios, built once
@lru_cache(maxsize=1)
def built():
    import random
    return {fam: D._build(fam, random.Random(f"{fam}-r3")) for fam in (D.RECALL, D.STYLE)}


def _turns(sc):
    """(user texts, assistant texts) of a scenario: every history turn, plus the prompt and the scripted final."""
    users = [t["user"] for t in sc.history] + [sc.prompt]
    assts = [t["assistant"] for t in sc.history] + [sc.steps[-1].text]
    return users, assts


# ------------------------------------------------------------------ authoring rules
def test_authoring_checks_pass():
    assert D.check_all() == (24, 36)


def test_counts_and_kinds():
    r, s = D.QS[D.RECALL], D.QS[D.STYLE]
    assert len(r) == 24 and len(s) == 12
    assert Counter(q["kind"] for q in r) == {"rec_user": 7, "rec_asst": 7, "rec_both": 3, "rec_none": 7}
    assert sum(q["kind"] != "rec_none" for q in r) == 17           # "about 17 recall"
    assert Counter(q["kind"] for q in s) == {"emo": 3, "close": 2, "build": 4, "fact": 3}
    rt = {t for t, th in D.THREADS.items() if th["fam"] == D.RECALL}
    st = {t for t, th in D.THREADS.items() if th["fam"] == D.STYLE}
    assert len(rt) == 12 and len(st) == 12 and not rt & st
    assert Counter(q["thread"] for q in r) == {t: 2 for t in rt}   # 2 questions per recall thread
    assert Counter(q["thread"] for q in s) == {t: 1 for t in st}   # 1 per style thread
    assert all(t.startswith("rdev_") for t in rt) and all(t.startswith("sdev_") for t in st)


def test_threads_cover_digest_and_long_histories():
    modes = Counter(th["mode"] for th in D.THREADS.values() if th["fam"] == D.RECALL)
    assert modes["digest"] >= 3 and modes["long"] >= 3
    assert Counter(th["mode"] for th in D.THREADS.values() if th["fam"] == D.STYLE)["long"] >= 2
    for tid, th in D.THREADS.items():
        n = len(D.history_of(tid))
        if th["mode"] == "digest":
            assert 22 <= n <= 24, (tid, n)                          # past 20 exchanges: the host condenses the oldest turns
        if th["mode"] == "long":
            assert sum(len(u) + len(a) for u, a in D.history_of(tid)) >= 2200, tid


# ------------------------------------------------------------------ the scenarios and their fields
def test_scenario_fields():
    sc_r, sc_s = built()[D.RECALL], built()[D.STYLE]
    assert len(sc_r) == 24 and len(sc_s) == 12
    for sc in sc_r:
        assert sc.family == D.RECALL
        assert sc.thread and sc.thread.startswith("rdev_") and sc.thread in D.THREADS
        assert sc.qkind in D.KINDS[D.RECALL]
        if sc.qkind == "rec_none":
            assert sc.recall_refs == () and sc.recall_absent and isinstance(sc.recall_ok, tuple)
        else:
            assert sc.qkind in D.RECALL_KINDS and sc.recall_refs and not sc.recall_absent and sc.recall_ok == ()
    assert Counter(sc.qkind for sc in sc_r) == {"rec_user": 7, "rec_asst": 7, "rec_both": 3, "rec_none": 7}
    for sc in sc_s:
        assert sc.family == D.STYLE
        assert sc.thread and sc.thread.startswith("sdev_") and sc.qkind in D.KINDS[D.STYLE]
        assert sc.recall_refs == () and sc.recall_absent is None and sc.recall_ok == ()
    for sc in sc_r + sc_s:
        assert len(sc.steps) == 1 and sc.steps[0].text                # one scripted final, no tool call
        assert all(set(t) == {"user", "assistant", "tools", "calls"} and not t["tools"] and not t["calls"]
                   for t in sc.history)                              # as production stores a turn: user + final text
    # refs are in the history (recall) and in the scripted final, exactly as written
    for sc in sc_r:
        hist = "\n".join(t["user"] + "\n" + t["assistant"] for t in sc.history).lower()
        for ref in sc.recall_refs:
            assert ref.lower() in sc.steps[-1].text.lower() and ref.lower() in hist
        if sc.qkind == "rec_none":
            assert sc.recall_absent.lower() not in hist


def test_threads_differ_per_question_and_prompts_are_unique():
    prompts = [sc.prompt for sc in built()[D.RECALL] + built()[D.STYLE]]
    assert len(set(p.lower() for p in prompts)) == 36
    ids = [q["thread"] for q in D.QS[D.RECALL] + D.QS[D.STYLE]]
    assert len(set(ids)) == 24


def test_no_isos_no_emoji_no_names_in_finals():
    for sc in built()[D.RECALL] + built()[D.STYLE]:
        f = sc.steps[-1].text
        assert not re.search(r"\d{4}-\d{2}-\d{2}|T\d{2}:\d{2}", f)
        assert not D.EMOJI.search(f) and not re.search(r"\bSam\b", f)


# ------------------------------------------------------------------ gating (ASK_LORA_DEVSETS)
def _child_env():
    """A clean environment for a child interpreter: no dev-set flag, round 4, and no WISP_HOME / MOE_DIR of this process
    (boot.py makes each interpreter its own scratch home)."""
    skip = {"ASK_LORA_DEVSETS", "WISP_HOME", "MOE_DIR"}
    return {k: v for k, v in os.environ.items() if k not in skip} | {"ASK_LORA_ROUND": "4"}


def _families(env):
    code = ("import scen, scen_r4_devsets as D; "
            "print(sorted(f for f in scen.FAMILIES if f in ('recall_dev_r4','session_style_dev'))); "
            "print(sorted(D.FAMILIES_R4), sorted(D.WRITE_ALLOW_R4))")
    e = _child_env() | env
    out = subprocess.run([PY, "-c", code], cwd=HERE, env=e, capture_output=True, text=True, check=True,
                         stdin=subprocess.DEVNULL, timeout=300).stdout.strip().splitlines()
    return out[-2:]


def test_families_load_only_with_the_env_flag():
    assert _families({}) == ["[]", "[] []"]
    assert _families({"ASK_LORA_DEVSETS": "0"}) == ["[]", "[] []"]
    on = _families({"ASK_LORA_DEVSETS": "1"})
    assert on[0] == "['recall_dev_r4', 'session_style_dev']"
    assert on[1] == "['recall_dev_r4', 'session_style_dev'] ['recall_dev_r4', 'session_style_dev']"


# ------------------------------------------------------------------ non-overlap
_DUMP = """
import json, random, sys
import scen
import importlib
m = importlib.import_module(sys.argv[1])
users, assts = [], []
for sc in m.FAMILIES_R4[m.FAMILY](random.Random(f"{m.FAMILY}-r3")):
    hist = sc.history or []
    users += [t["user"] for t in hist] + [sc.prompt]
    assts += [t["assistant"] for t in hist] + [sc.steps[-1].text if hasattr(sc.steps[-1], "text") else ""]
print(json.dumps({"users": users, "assts": assts}))
"""


def _scenarios_of(module_name):
    """{"users", "assts"} of every scenario a family generator makes, from a FRESH process: the generators keep clock and
    world state, so running them after this file's own builds in one process would change what they make. Cached in the
    temp dir, keyed by the generator's file (size and mtime) and this builder's code, so a rerun costs nothing."""
    import hashlib
    import json
    import tempfile
    src = HERE / f"{module_name}.py"
    key = hashlib.sha1(f"{module_name}|{src.stat().st_size}|{src.stat().st_mtime_ns}|{_DUMP}".encode()).hexdigest()[:16]
    cache = Path(tempfile.gettempdir()) / f"devsets_refs_{module_name}_{key}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    out = subprocess.run([PY, "-c", _DUMP, module_name], cwd=HERE, env=_child_env(), capture_output=True, text=True,
                         check=True, stdin=subprocess.DEVNULL, timeout=600)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    cache.write_text(json.dumps(got))
    return got


@lru_cache(maxsize=1)
def references():
    """{set name: {"users": [..], "assts": [..], "threads": set}} of every training / eval source that exists at authoring time."""
    refs = {}
    users, assts = [], []
    for q in S.QS:
        ex = S.THREADS[q["thread"]][:q["cut"] or len(S.THREADS[q["thread"]])]
        users += [u for u, _a in ex] + [q["prompt"]]
        assts += [a for _u, a in ex] + [q["final"]]
    for t in S.THREADS.values():
        users += [u for u, _a in t]
        assts += [a for _u, a in t]
    refs["session_chat_r4"] = dict(users=users, assts=assts, threads=set(S.THREADS))
    if not FAST:
        for name in ("scen_r4_session_recall", "scen_r4_instructions"):
            got = _scenarios_of(name)
            refs[name] = dict(users=got["users"], assts=got["assts"], threads=set())
    refs["toolbench"] = dict(users=[e["prompt"] for e in leak.load_toolbench()], assts=[], threads=set())
    refs["chateval"] = dict(users=[e["prompt"] for e in leak.load_chat()] + _chateval_r4_turns(), assts=[], threads=set())
    return refs


def _chateval_r4_turns():
    import yaml
    path = HERE.parents[1] / "ask-lora-r4" / "chateval" / "prompts_r4.yaml"
    out = []
    for u in yaml.safe_load(path.read_text())["units"]:
        out += [str(t) for t in (u.get("turns") or u.get("prompts") or ([u["prompt"]] if "prompt" in u else []))]
    return out


@lru_cache(maxsize=1)
def dev_corpus():
    users, assts = [], []
    for sc in built()[D.RECALL] + built()[D.STYLE]:
        u, a = _turns(sc)
        users += u
        assts += a
    return sorted(set(users)), sorted(set(assts))


def test_reference_sets_are_what_they_should_be():
    refs = references()
    assert len(refs["toolbench"]["users"]) > 1000                    # all splits, bound over seeds 0-49
    assert any("sourdough starter" in u.lower() for u in refs["toolbench"]["users"])    # the session cases are in
    assert len(refs["chateval"]["users"]) > 100
    assert any("fiddle-leaf" in u.lower() for u in refs["chateval"]["users"])           # prompts_r4.yaml is in
    assert len(refs["session_chat_r4"]["users"]) > 500
    if not FAST:
        assert len(refs["scen_r4_session_recall"]["users"]) > 1000 and len(refs["scen_r4_instructions"]["users"]) > 500


def test_user_turns_do_not_overlap_any_reference_user_turn():
    users, _ = dev_corpus()
    hits, near = [], []
    for name, ref in references().items():
        index = leak.LeakIndex(questions=[leak._entry(f"{name}#{i}", u) for i, u in enumerate(ref["users"]) if u.strip()],
                               toolbench=False, chat=False)
        folds = {leak.fold(u) for u in ref["users"]}
        for u in users:
            if leak.fold(u) in folds:
                hits.append((name, u))
                continue
            score, q = index.nearest(u)
            if score >= 0.5:
                near.append((round(score, 2), name, u, q["prompt"]))
    assert not hits, hits[:5]
    assert not near, near[:8]                                       # >= 0.7 is a hit, 0.5-0.7 needs a person: none either


def test_no_toolbench_fact_values_and_no_shared_toolbench_6gram():
    users, assts = dev_corpus()
    text = "\n".join(users + assts)
    assert leak.fact_hits(text) == []
    from scripts.toolbench.cases import heldout_shingles, load_cases
    from scripts.toolbench.world import make_facts
    sh = heldout_shingles(load_cases(), [make_facts(None)] + [make_facts(s) for s in range(50)])
    full, six = set(), set()
    for it in sh["items"]:
        full.add(it["full"])
        six |= set((it["sh"] or {}).get("6") or ())
    import hashlib

    def toks(t):
        return re.findall(r"[a-z0-9']+", t.lower())

    def h(ts):
        return hashlib.sha1(" ".join(ts).encode()).hexdigest()[:12]
    bad = []
    for t in users + assts:
        w = toks(t)
        if w and h(w) in full:
            bad.append(("full", t[:70]))
        if any(h(w[i:i + 6]) in six for i in range(len(w) - 5)):
            bad.append(("6-gram", t[:70]))
    assert not bad, bad[:6]


def _grams(text, n):
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def test_assistant_text_shares_no_8_word_run_with_any_reference():
    _, assts = dev_corpus()
    mine = set().union(*[_grams(a, 8) for a in assts])
    shared = []
    for name, ref in references().items():
        theirs = set().union(*[_grams(a, 8) for a in ref["assts"]]) if ref["assts"] else set()
        theirs |= set().union(*[_grams(u, 8) for u in ref["users"]])
        shared += [(name, g) for g in sorted(mine & theirs)[:3]]
    assert not shared, shared[:6]


def test_thread_ids_and_distinctive_terms_do_not_overlap():
    refs = references()
    all_threads = set().union(*[r["threads"] for r in refs.values()])
    assert not all_threads & set(D.THREADS)
    # distinctive terms of the dev threads: proper names (capitalised mid-sentence and never written lowercase in the
    # assistant's own text, as session_chat_r4 defines them), numbers of four digits or more, and every ref / ctx / ok word
    texts = [u + "\n" + a for tid in D.THREADS for u, a in D.history_of(tid)] + [q["final"] for q in D.QS[D.RECALL] + D.QS[D.STYLE]]
    corpus = "\n".join(texts)
    lower_anywhere = {t for t in S.TOK.findall(corpus) if t[0].islower()}
    terms = set()
    for text in texts:
        terms |= set(S._names(text, ""))          # capitalised mid-sentence ...
    terms = {t for t in terms if t not in lower_anywhere}        # ... and never written lowercase anywhere in the dev data
    terms |= {t.lower() for t in S.TOK.findall(corpus) if t.isdigit() and len(t) >= 4}
    for q in D.QS[D.RECALL] + D.QS[D.STYLE]:
        terms |= {t.lower() for r in (*q["ref"], *q["ctx"], *q["ok"]) for t in S.TOK.findall(r)
                  if t[0].isupper() and t.lower() not in lower_anywhere}
    terms -= set(re.findall(r"[a-z]+", leak._DAYS + leak._MONTHS))
    ref_words = Counter()
    for name, ref in refs.items():
        for t in ref["users"] + ref["assts"]:
            ref_words.update(w.lower() for w in S.TOK.findall(t))
    shared = sorted(t for t in terms if ref_words[t])
    assert len(terms) > 40, len(terms)
    bad = [t for t in shared if t not in _ALLOWED_SHARED]
    assert not bad, " ".join(bad)


# Words a dev thread uses that are ordinary English or places/things any chat may name; the check above is about
# the distinctive ones (people, products, courses of action). Kept short and explicit so a new overlap is a decision.
_COMMON = set("""the a an and or but is are was were be been to of in on at for with from by as it its this that these those i you he she
we they his her their our your my me him them there here what when where who which how why not no yes ok okay good great well also
just really very too one two three four five six seven eight nine ten first second third last next today tomorrow tonight start
keep put use take make get let if then so than more most some any each every many much few little lot only even still again
once start stop begin ask pick try check find see look mark write read put set run add cut fold press wipe test open close
mix pair plan list ready simple small short long big large new old high low full light hard soft cold hot warm dry wet
rubber concrete cotton flannel glass steel paper wood pine oak plastic jersey fleece silicone nylon wool leather""".split())
_ALLOWED_SHARED = set("""saturday sunday monday tuesday wednesday thursday friday saturdays tuesdays english japanese chicago
saturn jupiter mars earth moon sun venus mercury neptune uranus apple spotify google twitter youtube android iphone
north atlantic mediterranean russia france spain australia volga europe canberra wellington honda
brother celsius fahrenheit everest nile japan italian latin avocado boiling dark living pack park ten twelve twenty id""".split())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
