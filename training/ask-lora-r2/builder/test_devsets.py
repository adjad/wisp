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
  - no thread id, and no distinctive name, number or topic word the user states in a dev thread appears in a reference thread
    (names compared case-insensitively with the possessive stripped; each thread declares its central topic nouns in TOPICS,
    and none of them, in either number, may occur anywhere in a reference source);
  - the same sources through check_overlap.run_checks (the RL gate's own checker) over the dev build's all.jsonl: clean.
The dev build itself (`scen_r4_devsets.py build` / `validate`) is tested end to end: the file set, the labels, the validator.
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
    texts = [t for tid in D.THREADS for pair in D.history_of(tid) for t in pair] + [q["final"] for q in D.QS[D.RECALL] + D.QS[D.STYLE]]
    corpus = "\n".join(texts)
    owned = "\n".join([a for tid in D.THREADS for _u, a in D.history_of(tid)] + [q["final"] for q in D.QS[D.RECALL] + D.QS[D.STYLE]])
    lower_anywhere = {_w(t) for t in S.TOK.findall(owned) if t[0].islower()}    # the assistant's text is correctly cased
    terms = set()
    for text in texts:
        terms |= set(S._names(text, ""))          # capitalised mid-sentence ...
    terms = {t for t in terms if t not in lower_anywhere}        # ... and never written lowercase anywhere in the dev data
    terms |= {t.lower() for t in S.TOK.findall(corpus) if t.isdigit() and len(t) >= 4}
    for q in D.QS[D.RECALL] + D.QS[D.STYLE]:
        terms |= {t.lower() for r in (*q["ref"], *q["ctx"], *q["ok"]) for t in S.TOK.findall(r)
                  if t[0].isupper() and t.lower() not in lower_anywhere}
    terms -= set(re.findall(r"[a-z]+", leak._DAYS + leak._MONTHS))
    ref_words = _ref_words()
    shared = sorted(t for t in terms if ref_words[t])
    assert len(terms) > 40, len(terms)
    assert "philippa" in terms and "bartleby" in terms             # the name detector sees possessives ("Philippa's parade")
    bad = [t for t in shared if t not in _ALLOWED_SHARED]
    assert not bad, " ".join(bad)


def _w(token):
    """A comparison key: lower case, possessive stripped ("Marguerite's" and "marguerite" are the same name)."""
    return re.sub(r"['’]s$", "", token).lower()


@lru_cache(maxsize=1)
def _ref_words():
    c = Counter()
    for ref in references().values():
        for t in ref["users"] + ref["assts"]:
            c.update(_w(w) for w in S.TOK.findall(t))
    return c


def _forms(word):
    """The word, its plural and singular (a crude pair is enough: the topic words are plain nouns)."""
    w = word.lower()
    out = {w, w + "s", w + "es"}
    if w.endswith("ies"):
        out.add(w[:-3] + "y")
    if w.endswith("es"):
        out.add(w[:-2])
    if w.endswith("s"):
        out.add(w[:-1])
    if w.endswith("y"):
        out.add(w[:-1] + "ies")
    return out


# The central nouns of every dev thread (what it is ABOUT, plus its people and products). None may occur in any reference
# source: sharing "octopus" with a trivia turn or "podcast" with another thread's interview is the topic overlap this proves
# absent. Plural / singular forms are compared too; a new thread must declare its own entry.
TOPICS = {
    "rdev_swim": "swim swimming goggles thandiwe sorrel",
    "rdev_shelf": "bookshelf",
    "rdev_car": "honda desmond",
    "rdev_scope": "telescope dobsonian signe",
    "rdev_campaign": "dungeons dragons campaign sunken cistern wizard",
    "rdev_paint": "watercolor watercolors",
    "rdev_bakesale": "pta lincoln",
    "rdev_flat": "avondale irving",
    "rdev_tank": "aquarium betta bartleby aqueon quietflow",
    "rdev_printer": "printer eero maplehouse",
    "rdev_license": "easement covenant quizlet",
    "rdev_costume": "jellyfish tentacles philippa alder",
    "sdev_promo": "mireille management",
    "sdev_house": "oak",
    "sdev_lab": "physics uncertainty propagation",
    "sdev_closet": "closet donate",
    "sdev_juggle": "juggle tennis balls",
    "sdev_bookswap": "donors lunchtime",
    "sdev_gym": "barbell dumbbells bench",
    "sdev_photos": "scanning shoeboxes flatbed",
    "sdev_quilt": "quilt",
    "sdev_poster": "poster volcano nephew mars phobos deimos",
    "sdev_cranes": "origami cranes",
    "sdev_bagel": "bagel bagels",
}
# Words of the topics above that ordinary English also uses; each is listed with the reason it is not a topic overlap.
_TOPIC_OK = {
    "closet": "a grief turn mentions clothes put away in a closet; the dev thread is sorting one out",
    "mars": "the crude singular 'mar' of the planet matches a month abbreviation; the planet itself is in no reference",
    "swim": "'swim trunks' in a gift-packing turn; the dev thread is adult swim lessons",
    "dungeons": "the title of an unrelated manga in a book list; the dev thread is a tabletop campaign",
    "juggle": "an idiom ('much less of a juggle') in a piano turn; the dev thread is learning to juggle",
}


def test_every_thread_declares_topics_and_none_overlaps_a_reference():
    assert set(TOPICS) == set(D.THREADS), sorted(set(TOPICS) ^ set(D.THREADS))
    ref_words = _ref_words()
    hits = {}
    for tid, words in TOPICS.items():
        for w in words.split():
            seen = [f for f in _forms(w) if ref_words[f]]
            if seen and w.lower() not in _TOPIC_OK:
                hits.setdefault(tid, []).append((w, seen))
    assert not hits, hits
    # the declared words are really the thread's (a stale entry proves nothing): at least two (or all, if fewer) occur in it
    for tid, words in TOPICS.items():
        text = " ".join(u + " " + a for u, a in D.history_of(tid)).lower() + " " + " ".join(
            q["prompt"] + " " + q["final"] for fam in D.QS.values() for q in fam if q["thread"] == tid).lower()
        toks = set(re.findall(r"[a-z0-9]+", text))
        present = [w for w in words.split() if toks & _forms(w)]
        assert len(present) >= min(2, len(words.split())), (tid, present)


def test_names_are_unique_across_dev_and_references_case_insensitive_possessive_safe():
    """Every name the dev threads give a person or place is compared with the reference vocabulary: 'Philippa' against
    "Marguerite's"-style possessives, any case. A name shared with a reference source fails, wherever it appears."""
    texts = [t for tid in D.THREADS for pair in D.history_of(tid) for t in pair] + [q["final"] for fam in D.QS.values() for q in fam]
    owned = "\n".join([a for tid in D.THREADS for _u, a in D.history_of(tid)] + [q["final"] for fam in D.QS.values() for q in fam])
    lower_anywhere = {_w(t) for t in S.TOK.findall(owned) if t[0].islower()}      # the assistant's text is correctly cased
    names = set()
    for t in texts:
        names |= {_w(n) for n in S._names(t, "")}
    names -= lower_anywhere | set(re.findall(r"[a-z]+", leak._DAYS + leak._MONTHS))
    assert {"philippa", "bartleby", "desmond", "thandiwe", "signe"} <= names, names
    ref_words = _ref_words()
    shared = sorted(n for n in names if ref_words[n] and n not in _ALLOWED_SHARED)
    assert not shared, shared


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
north atlantic mediterranean sea russia france spain australia volga europe canberra wellington honda
brother celsius fahrenheit everest nile japan italian latin avocado boiling dark living pack park ten twelve twenty id""".split())



# ------------------------------------------------------------------ the dev build: file set, labels, validator, export loaders
@pytest.fixture(scope="module")
def dev_build(tmp_path_factory):
    """`scen_r4_devsets.py build` in a fresh process, into a directory of its own (what the integrator runs)."""
    out = tmp_path_factory.mktemp("devsets")
    env = _child_env() | {"ASK_LORA_OUT": str(out)}
    r = subprocess.run([PY, str(HERE / "scen_r4_devsets.py"), "build"], cwd=HERE, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "examples: 36 failures: 0" in r.stdout, r.stdout[-1500:]
    return out, env


def test_build_refuses_to_run_without_its_own_output_dir():
    env = {k: v for k, v in _child_env().items() if k != "ASK_LORA_OUT"}
    r = subprocess.run([PY, str(HERE / "scen_r4_devsets.py"), "build"], cwd=HERE, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=300)
    assert r.returncode == 2 and "ASK_LORA_OUT is required" in r.stdout


def test_dev_build_writes_the_same_file_set_as_a_normal_build(dev_build):
    import json
    out, _env = dev_build
    names = {p.name for p in out.iterdir()}
    assert {"all.jsonl", "meta.jsonl", "train.jsonl", "train.meta.jsonl", "valid.jsonl", "valid.meta.jsonl", "stats.json",
            "eval_manifest.json"} <= names, names
    ex = [json.loads(l) for l in open(out / "all.jsonl")]
    metas = [json.loads(l) for l in open(out / "meta.jsonl")]
    assert len(ex) == len(metas) == 36
    assert len(list(open(out / "train.jsonl"))) + len(list(open(out / "valid.jsonl"))) == 36
    assert len(list(open(out / "train.meta.jsonl"))) == len(list(open(out / "train.jsonl")))
    assert Counter(m["family"] for m in metas) == {"recall_dev_r4": 24, "session_style_dev": 12}
    assert all(m.get("thread") and m.get("qkind") for m in metas)
    for m in metas:
        if m["family"] == D.RECALL and m["qkind"] != "rec_none":
            assert m["recall_refs"], m["id"]
        if m["qkind"] == "rec_none":
            assert m["recall_absent"] and not m["recall_refs"], m["id"]
    st = json.loads((out / "stats.json").read_text())
    assert st["examples"] == 36 and set(st["families"]) == {"recall_dev_r4", "session_style_dev"}
    # no thread spans train and valid (the build's own assertion, shown on the written files)
    side = {}
    for name in ("train", "valid"):
        for l in open(out / f"{name}.meta.jsonl"):
            side.setdefault(json.loads(l)["thread"], set()).add(name)
    assert all(len(v) == 1 for v in side.values())


def test_validator_reads_the_dev_build_with_no_problems(dev_build):
    out, env = dev_build
    r = subprocess.run([PY, str(HERE / "scen_r4_devsets.py"), "validate"], cwd=HERE, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=900)
    assert r.returncode == 0, r.stdout[-2500:] + r.stderr[-1500:]
    assert "all.jsonl: 36 examples, problems: none" in r.stdout, r.stdout[-1500:]
    assert "extra_user" not in r.stdout


def test_export_prompts_loaders_read_the_dev_build(dev_build):
    out, _env = dev_build
    code = ("import sys; sys.path.insert(0, %r); import export_prompts as E; from pathlib import Path; "
            "p = E.load_pairs(Path(%r)); E.normalise_clarify(p); t, _n = E.index_pairs(p, True); print(len(p), len(t))"
            % (str(REPO / "training" / "ask-lora-r3" / "grpo"), str(out)))
    r = subprocess.run([PY, "-c", code], cwd=HERE, env=_child_env(), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "36 36", r.stdout[-1500:] + r.stderr[-1500:]


def test_check_overlap_run_checks_is_clean_on_the_dev_build(dev_build):
    """The RL gate's own checker (check_overlap.run_checks) over the dev build's all.jsonl, against every training and eval
    source this file compares by hand: no hit, no review item, nothing skipped."""
    out, _env = dev_build
    sys.path.insert(0, str(REPO / "training" / "ask-lora-r3" / "grpo"))
    import check_overlap
    from scripts.toolbench.cases import heldout_shingles, load_cases
    from scripts.toolbench.world import make_facts
    sh = heldout_shingles(load_cases(), [make_facts(None)] + [make_facts(s) for s in range(50)])
    manifest = {"sets": {name: {"prompts": ref["users"], "threads": sorted(ref["threads"])} for name, ref in references().items()},
                "shingles": sh}
    rep = check_overlap.run_checks([str(out / "all.jsonl")], manifest=manifest, facts="toolbench")
    assert rep["rows"] == 36 and rep["by_modality"] == {"text": 36}
    assert rep["hits"] == [] and rep["review"] == [] and rep["skipped"] == [], rep
    assert rep["result"] == "clean" and rep["exit"] == 0
    assert set(rep["excluded_sets"]) >= {"session_chat_r4", "toolbench", "chateval"}


# ------------------------------------------------------------------ the ideals as probe references
def _reward():
    sys.path.insert(0, str(REPO / "training" / "ask-lora-r3" / "grpo"))
    try:
        import reward
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"reward.py not importable here: {exc!r}")
    return reward


def test_recall_refs_survive_reformatting_and_the_ideal_passes_its_own_probe():
    """Refs are what the answer must bring back, so they must not depend on number or phrase formatting ("9,800" vs "9800",
    "3/4 inch" vs "three quarter inch"): at most two words, no number with a comma, slash or decimal point."""
    for q in D.QS[D.RECALL]:
        for r in q["ref"]:
            assert len(r.split()) <= 2, (q["thread"], r)
            assert not re.search(r"\d[,/.]\d", r), (q["thread"], r)
            assert not re.match(r"(?i)(one|two|three|four|five|six|seven|eight|nine|ten)\s+(quarter|half|third)", r), (q["thread"], r)
    R = _reward()
    for sc in built()[D.RECALL]:
        if sc.qkind in D.RECALL_KINDS:
            score, guess = R.recall_score(sc.steps[-1].text, sc.recall_refs)
            assert score == 1.0 and not guess, (sc.thread, sc.recall_refs, score)


def test_no_final_restates_a_stretch_of_the_earlier_conversation():
    R = _reward()
    for sc in built()[D.RECALL] + built()[D.STYLE]:
        lines = [t for h in sc.history for t in (h["user"], h["assistant"])]
        assert not R.history_echo(sc.steps[-1].text, {"history": lines}), (sc.thread, sc.steps[-1].text)


def test_never_said_finals_do_not_all_open_the_same_way():
    rn = [q["final"] for q in D.QS[D.RECALL] if q["kind"] == "rec_none"]
    assert len(rn) == 7
    firsts = [" ".join(re.findall(r"[a-z']+", f.lower())[:3]) for f in rn]
    assert len(set(firsts)) == 7, firsts                            # seven different openings
    assert len({f.split()[0].lower() for f in rn}) >= 5, [f.split()[0] for f in rn]
    assert not any(re.match(r"(?i)(an?|the)\s+\w+\s+(has|have) not come up", f) for f in rn)   # the retired "A color has not come up"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
