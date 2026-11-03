"""Tests for export_prompts.py round 5 (RL-SCOPE C1, C5, C6, C6b, C7, C15); pure python, no tokenizer, no model.
Run: python -m pytest -q test_export_r5.py

Builder meta fields RL-SCOPE says the builder will add (clarify, unsafe_reads, thread, qkind, recall_refs, recall_absent,
recall_ok) do not exist in real builds yet: every test here builds synthetic meta (r5_fixtures.py) and the export reads it
with .get()."""
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_prompts as X  # noqa: E402
import reward as W  # noqa: E402
import r5_fixtures as F  # noqa: E402
from r5_fixtures import scenario, word  # noqa: E402

TOOLS = ["send_message", "lookup_contact", "summarize_emails", "get_upcoming", "set_volume", "get_weather", "recall"]


# =========================================================================== fixtures: a build with every family
def corpus(n_each: int = 12, r5_meta: bool = False):
    """(example, meta) pairs for a spread of families. With r5_meta the builder fields of C6/C7 are present."""
    pairs, k = [], 0

    def w():
        nonlocal k
        k += 1
        return word(k)
    for i in range(n_each):
        # compose: lookup, send, receipt (a narrate final with facts)
        pairs += scenario("compose_r3", i, f"text {w()} that I am running late", [
            ("call", "lookup_contact", {"name": w()}, "Dad: +1 555 0100"),
            ("call", "send_message", {"to": "Dad", "text": "running late"}, "Sent to Dad at 6 PM."),
            ("final", "Sent to Dad at 6 PM.")], writes=("send_message",), meta={})
        pairs += scenario("mail_r3", i, f"what is in my inbox about {w()}", [
            ("call", "summarize_emails", {"query": w()}, "3 emails. Top: Maya Chen, Lunch plan at 12:30 PM."),
            ("final", "You have 3 emails, the top one from Maya Chen about the lunch plan at 12:30 PM.")])
        pairs += scenario("device_r3", i, f"turn the volume down {w()}", [
            ("call", "set_volume", {"level": 20}, "Volume set to 20."),
            ("final", "Done, volume is at 20.")], writes=("set_volume",))
        pairs += scenario("restraint_r4", i, f"what is the weather {w()}", [
            ("final", "Which city should I check?")],
            final_meta={"clarify": True, "unsafe_reads": ["get_weather"]} if r5_meta else {})
        pairs += scenario("safe_restraint", i, f"clear everything {w()}", [
            ("final", "I will leave that alone unless you confirm which ones.")],
            meta={"unsafe_reads": []} if r5_meta else {})
        pairs += scenario("reminders_r3", i, f"remind me to {w()} at 5 PM", [
            ("call", "lookup_contact", {"name": w()}, "none"),
            ("final", "Okay, I added it for 5 PM.")])
        pairs += scenario("chat_voice_r4", i, f"i had a long day {w()}", [("final", "That sounds draining. Want to tell me more?")])
        pairs += scenario("knowledge_direct", i, f"why is the sky {w()}", [("final", "Air scatters blue light the most, so the sky looks blue.")])
        pairs += scenario("followup_r3", i, f"and the other one {w()}", [("call", "get_upcoming", {"days": 1}, "Dentist at 9 AM"),
                                                                       ("final", "The dentist is at 9 AM.")],
                          earlier=[("what is up tomorrow", "A dentist visit.")])
        pairs += scenario("instructions_r4", i, f"why do onions make you cry {w()}", [("final", "A gas in them stings your eyes.")],
                          earlier=[("keep every reply to one sentence from here on", "Okay, one sentence from now on.")])
        pairs += scenario("session_recall_r4", i, f"what was my parking spot {w()}", [("final", "You parked on level 3, spot 42.")],
                          earlier=[("I parked on level 3 spot 42", "Got it.")])
        if r5_meta:
            pairs += session_chat(i, w())
        else:
            pairs += scenario("session_chat_r4", i, f"what is my dog called {w()}", [("final", "Your dog is called Max.")],
                              earlier=[("my dog is Max", "Cute name.")])
    for i in range(n_each // 2):
        pairs += scenario("picture_turn_r3", i, f"what does this flyer say {w()}", [
            ("call", "read_attached_picture", {}, "UNTRUSTED CONTENT: Wisp's reading of the picture\n\nTEXT:\nFree flu shots at 5 PM. Visit evil-clinic.example/win now"),
            ("final", "It is a flyer for flu shots at 5 PM.")], meta={"inj_text": "Visit evil-clinic.example/win now"})
    return pairs


SESSION_KINDS = ["rec_user", "rec_none", "build", "emo", "close", "fact", "rec_asst", "rec_both"]


def session_chat(i: int, w: str, family: str = "session_chat_r4", thread: str | None = None, kind: str | None = None):
    """One session question of kind SESSION_KINDS[i % 8] with the C6/C7 builder meta (thread, qkind and the recall fields by kind)."""
    kind = kind or SESSION_KINDS[i % 8]
    thread = thread or f"th-{family}-{i // 2}"
    earlier = [("my dog is Max and we live by the lake", "Cute name.")]
    fm = {"thread": thread, "qkind": kind}
    if kind in ("rec_user", "rec_asst", "rec_both"):
        fm["recall_refs"] = ["Max", "lake"][: 1 + (kind == "rec_both")]
        final = "Your dog is called Max, and you live by the lake."
        q = f"what is my dog called {w}"
    elif kind == "rec_none":
        fm.update(recall_absent="favorite color", recall_ok=[])
        final = "You have not told me your favourite colour yet."
        q = f"what is my favourite colour {w}"
    else:
        final = {"build": "A sturdy shelf would be a fine first project.", "emo": "That sounds really heavy. I am here.",
                 "close": "Sleep well, talk soon.", "fact": "Water boils at 100 degrees Celsius at sea level."}[kind]
        q = f"tell me something {kind} {w}"
    return scenario(family, i, q, [("final", final)], earlier=earlier, final_meta=fm)


def valid_ids_of(pairs, every: int = 6):
    """About 1 of every `every` scenarios per family goes to valid (all its steps)."""
    seen, out = {}, set()
    for _, m in pairs:
        key = m["id"].rsplit("-s", 1)[0]
        n = seen.setdefault(m["family"], {}).setdefault(key, len(seen[m["family"]]))
        if n % every == every - 1:
            out.add(m["id"])
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def classify_digest(pairs, rnd: int) -> str:
    """Canonical text of every label classify() makes at `rnd`, in order, key order kept (byte-identical test)."""
    turns: dict = {}
    for ex, meta in pairs:
        names = [tc["function"]["name"] for tc in (ex["messages"][-1].get("tool_calls") or ())]
        turns.setdefault(X.turn_key(meta), []).append((meta["step"], names))
    out = [json.dumps(X.classify(ex, meta, rnd, turns, TOOLS), ensure_ascii=False) for ex, meta in pairs]
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


def export_digests(tmp_path: Path, rnd: int) -> dict:
    """Run main() on the shared corpus (text only, fixed seed) and digest every file it writes."""
    pairs = corpus()
    b = F.write_build(tmp_path / f"build{rnd}", pairs, valid_ids_of(pairs))
    out = tmp_path / f"out{rnd}" / "grpo_prompts.jsonl"
    argv = ["--build-dir", str(b), "--model", "x", "--round", str(rnd), "--out", str(out), "--n", "64", "--mm-share", "0",
            "--picture-share", "0"]
    assert F.run_main(argv) == 0
    res = {p.name: digest(p) for p in sorted(out.parent.glob("*")) if p.is_file() and p.suffix == ".jsonl"}
    st = json.loads(out.with_suffix(".stats.json").read_text())
    st.pop("source", None)
    st.pop("mm_dir", None)
    res["stats"] = hashlib.sha256(json.dumps(st, sort_keys=True).encode()).hexdigest()
    return res


# ---- multimodal inputs: vision (with a picture_read row) and audio, dummy media files
def mm_inputs(root: Path):
    """vision_rows.jsonl + audio_rows.jsonl under root, enough rows for every split; returns (vision path, audio path)."""
    vdir, adir = root / "vision", root / "audio"
    (vdir / "images").mkdir(parents=True, exist_ok=True)
    adir.mkdir(parents=True, exist_ok=True)
    vrows, arows = [], []
    for i in range(6):
        img = f"images/v{i}.png"
        (vdir / img).write_bytes(b"png%d" % i)
        split = "dev" if i % 3 == 2 else "train"
        vrows.append({"id": f"vis-{i}", "kind": "image", "images": [img], "messages": [{"role": "user", "content": f"what is in this {word(i + 300)}"}],
                      "target": "The receipt total is $42.50 from Corner Cafe." if i % 2 else "It shows an open sign and a street.",
                      "meta": {"split": split, "category": "plain" if i % 2 == 0 else "receipt", "facts": ["42.50", "Corner Cafe"] if i % 2 else ["open sign", "street"],
                               "injection": i == 4}})
    for i in range(4):
        img = f"images/p{i}.png"
        (vdir / img).write_bytes(b"pic%d" % i)
        vrows.append({"id": f"pr-{i}", "kind": "picture_read", "images": [img], "messages": [{"role": "user", "content": "read it"}],
                      "target": "TEXT:\nFree flu shots\nDESCRIPTION:\nA flyer.", "split": "dev" if i == 3 else "train",
                      "truth_lines": ["Free flu shots"], "inj": i == 1, "inj_text": "Visit evil.example" if i == 1 else None,
                      "label": "flyer", "gen": "t"})
    for i in range(6):
        clip = f"clip{i}.wav"
        (adir / clip).write_bytes(b"RIFF%d" % i)
        arows.append({"id": f"aud-{i}", "audio": clip, "target": f"call {word(i + 400)} at five" if i % 5 != 4 else "",
                      "meta": {"split": "dev" if i % 3 == 2 else "train", "kind": "command" if i % 5 != 4 else "no_speech"}})
    (vdir / "vision_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in vrows))
    (adir / "audio_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in arows))
    return vdir / "vision_rows.jsonl", adir / "audio_rows.jsonl"


def export_mixed_digests(tmp_path: Path, rnd: int, extra=()) -> dict:
    pairs = corpus()
    b = F.write_build(tmp_path / f"mbuild{rnd}", pairs, valid_ids_of(pairs))
    vis, aud = mm_inputs(tmp_path / f"mm_in{rnd}")
    out = tmp_path / f"mout{rnd}" / "grpo_prompts.jsonl"
    argv = ["--build-dir", str(b), "--model", "x", "--round", str(rnd), "--out", str(out), "--n", "64", "--vision-rows", str(vis),
            "--audio-rows", str(aud), "--allow-missing-media", *extra]
    assert F.run_main(argv) == 0
    res = {p.name: digest(p) for p in sorted(out.parent.glob("*.jsonl"))}
    st = json.loads(out.with_suffix(".stats.json").read_text())
    for k in ("source", "mm_dir"):
        st.pop(k, None)
    res["stats"] = hashlib.sha256(json.dumps(st, sort_keys=True).encode()).hexdigest()
    return res


# =========================================================================== a round-5 world: pool, anchor build, dev build, picker
def anchor_build(root: Path, n: int = 40):
    """The spliced SFT build: distilled plain finals of the four anchor families (plus undistilled rows that must be ignored)."""
    pairs, k = [], 5000
    for fam in sorted(X.R5_ANCHOR_FAMILIES):
        for i in range(n):
            k += 1
            pairs += scenario(fam, i, f"tell me about {word(k)} and {word(k + 1)}", [("final", f"Here is a gentle take on {word(k)}, friend.")],
                              meta={"distilled": i % 5 != 4, "think": False, "thought": None})
    valid = {m["id"] for _, m in pairs if int(m["id"].split("-")[1]) % 6 == 5}
    return F.write_build(root, pairs, valid), pairs, valid


def dev_build(root: Path, recall: int = 24, style: int = 12):
    pairs, k = [], 9000
    for i in range(recall):
        k += 1
        pairs += session_chat(i, word(k), "recall_dev_r4", f"dev-th-{i // 2}",
                              kind=["rec_user", "rec_none", "rec_asst", "rec_both"][i % 4])
    for i in range(style):
        k += 1
        pairs += session_chat(i, word(k), "session_style_dev", f"style-th-{i // 2}", kind=["build", "emo", "close", "fact"][i % 4])
    return F.write_build(root, pairs, ()), pairs


def r5_world(tmp: Path, n_each: int = 14):
    pairs = corpus(n_each, r5_meta=True)
    valid = valid_ids_of(pairs, every=3)
    build = F.write_build(tmp / "data-rl", pairs, valid)
    abuild, apairs, avalid = anchor_build(tmp / "anchor")
    dbuild, dpairs = dev_build(tmp / "devb")
    return {"build": build, "anchor": abuild, "dev": dbuild, "pairs": pairs, "valid": valid, "tmp": tmp}


COUNTS = "safety=4,narrate=4,select=2,recall_dev_r4=24,session_style_dev=12,chat=6"
SMOKE = "safety=3,narrate=2,select=1,recall_dev_r4=8"


def r5_argv(w, out=None, n=96, extra=()):
    out = out or w["tmp"] / "out5" / "grpo_prompts.jsonl"
    return ["--build-dir", str(w["build"]), "--model", "x", "--round", "5", "--out", str(out), "--n", str(n), "--mm-share", "0",
            "--picture-share", "0", "--anchor-build-dir", str(w["anchor"]), "--dev-build-dir", str(w["dev"]), "--picker-dev", "none", "--allow-no-picker",
            "--dist-dir", str(w["tmp"] / "no-dist"), "--expect-session-chat", "skip", "--dev-slice-counts", COUNTS,
            "--dev-smoke-counts", SMOKE, *extra]


def read_rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def labels(path):
    return [json.loads(r["label"]) for r in read_rows(path)]


# =========================================================================== C1: round 5 is additive
BASELINE = json.loads('''{"classify3": "3d6f436f2335d9e51af18083c1cbe649a0b70ffbd6104e7d9c92f5acb749f1c7", "classify4": "8abd9aee876970f2da207199c38dfb72da49c6b2180add00c6cf6fdcf4fa8215", "text3": {"grpo_mm_dev.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "grpo_prompts.jsonl": "f01d058526a96bf4660b4830f3adec4239f6456f92831bc1086e772f5bcb299f", "stats": "bbafd2ca503d9dc8c44ac0877f83af88de791848c89bae8f878676957ff71d6b"}, "mixed3": {"grpo_mm_dev.jsonl": "892fc4cfaeb8bc2c038b00f766ce8396112996b40e2ce455749d1a051329fab1", "grpo_prompts.jsonl": "117efa96dc2470b32cbac0cbb5683656b27b65787f7da53023aee60e0cb6b160", "stats": "c0d27fcec37fd6104f465c897328a34488f03e259d9f0e95feefd5b5f6c210e2"}, "text4": {"grpo_mm_dev.jsonl": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "grpo_prompts.jsonl": "d2d0d230aaf4fb7db80c9d75b61667d82afdf462cb3c30d99010a2e064c80236", "stats": "50980fb21bcee75288b7553cddffa22309a695200ceb5ca2f06c78d77715660b"}, "mixed4": {"grpo_mm_dev.jsonl": "892fc4cfaeb8bc2c038b00f766ce8396112996b40e2ce455749d1a051329fab1", "grpo_prompts.jsonl": "004155742c391439600eb388207d86297f4eb5a288b7f0c99e33665ed7e46227", "stats": "09cf9d26ebe1f45ee04c0e7ed1f87220473ec4a396615bfa6825a003ef92b294"}}''')   # recorded from the code before round 5 was added (rounds 3 and 4)


def test_rounds_3_and_4_unchanged(tmp_path):
    """Every label classify() makes at rounds 3 and 4, and every file main() writes (text only and mixed with vision, picture
    reads and audio), is byte-identical to what the code made before round 5 existed."""
    pairs = corpus()
    assert classify_digest(pairs, 3) == BASELINE["classify3"]
    assert classify_digest(pairs, 4) == BASELINE["classify4"]
    for rnd in (3, 4):
        assert export_digests(tmp_path / f"t{rnd}", rnd) == BASELINE[f"text{rnd}"], rnd
        assert export_mixed_digests(tmp_path / f"m{rnd}", rnd) == BASELINE[f"mixed{rnd}"], rnd


def test_round_3_labels_unstamped_round_4_stamped_4_round_5_stamped_5():
    ex = example_pair("compose_r3", "call")
    assert "round" not in X.classify(*ex)
    assert X.classify(*ex, 4, {}, ["lookup_contact"])["round"] == 4
    assert X.classify(*ex, 5, {}, ["lookup_contact"])["round"] == 5
    lab = {"final": True}
    X.r4_fields(lab, [], {"family": "x", "id": "x-0-s0"}, False, {}, [], 5)
    assert lab["round"] == 5
    lab = {"final": True}
    X.r4_fields(lab, [], {"family": "x", "id": "x-0-s0"}, False, {}, [])        # the old positional signature still stamps 4
    assert lab["round"] == 4


def example_pair(family, kind):
    pairs = {"call": scenario(family, 1, "text dad hello there", [("call", "lookup_contact", {"name": "Dad"}, "Dad: +1 555 0100"),
                                                                  ("final", "Dad is at +1 555 0100 at 6 PM.")])}
    return pairs["call"][0][0], pairs["call"][0][1]


def test_round_choice_and_grpo_round_check(tmp_path):
    with pytest.raises(SystemExit):
        X.main(["--model", "x", "--round", "6", "--build-dir", str(tmp_path)])
    import grpo
    w = r5_world(tmp_path)
    assert F.run_main(r5_argv(w)) == 0
    out = w["tmp"] / "out5" / "grpo_prompts.jsonl"
    dev = out.with_name("grpo_text_dev.jsonl")

    def parse(rnd):
        return grpo.build_parser().parse_args(["check", "--base", "x", "--prompts", str(out), "--text-dev", str(dev), "--round", str(rnd)])
    assert grpo.resolve_round(parse(5)) == 5                           # every row, multimodal ones included, carries the stamp
    with pytest.raises(SystemExit) as e:
        grpo.resolve_round(parse(4))
    assert "exported for round 5" in str(e.value)
    assert {l["round"] for l in labels(out)} == {5}


# =========================================================================== C6: ask, rl_kind, ceiling
def labs(pairs, rnd=5, **kw):
    turns = {}
    for ex, m in pairs:
        turns.setdefault(X.turn_key(m), []).append((m["step"], [tc["function"]["name"] for tc in (ex["messages"][-1].get("tool_calls") or ())]))
    return [X.classify(ex, m, rnd, turns, TOOLS, **kw) for ex, m in pairs]


def test_chat_and_session_finals_ending_in_a_question_never_ask_at_round_5():
    for fam, meta_kw in (("chat_voice_r4", {}), ("chat_direct_r4", {}), ("session_chat_r4", {"qkind": "build", "thread": "t"}),
                         ("session_recall_r4", {}), ("instructions_r4", {}), ("followup_r3", {}), ("knowledge_direct", {}),
                         ("recall_dev_r4", {"qkind": "build", "thread": "t"})):
        for text in ("Ok?", "Which kind of tired are you tonight, giggly or sleepy?"):
            lab = labs(scenario(fam, 1, "soft landing movie please", [("final", text)], final_meta=meta_kw))[0]
            assert lab["step"] == "final" and not lab["ask"] and not lab["clarify"] and lab["chat"], (fam, text)
    # even when the builder marks it, a chat row is never an ask
    lab = labs(scenario("chat_voice_r4", 1, "hi there", [("final", "Hi?")], final_meta={"clarify": True}))[0]
    assert not lab["ask"] and not lab["clarify"]
    # at round 4 the same session_chat_r4 "?" final is still conversation, and "Ok?" on a decision family is still an ask
    assert labs(scenario("restraint_r4", 1, "what is the weather", [("final", "Ok?")]), rnd=4)[0]["ask"]


def test_clarify_is_what_the_builder_marked_and_the_fallback_is_logged():
    marked = labs(scenario("restraint_r4", 1, "what is the weather", [("final", "Which city should I check?")], final_meta={"clarify": True}))[0]
    assert marked["step"] == "ask" and marked["ask"] and marked["clarify"] and "clarify_fallback" not in marked
    assert marked["facts"] == [] and marked["rl_kind"] == "decision" and marked["ceiling"] == 1.5
    # a question the builder did NOT mark is not a clarification, whatever it ends with
    unmarked = labs(scenario("restraint_r4", 1, "what is the weather", [("final", "Do you want me to guess?")], final_meta={"clarify": False}))[0]
    assert unmarked["step"] == "final" and not unmarked["clarify"] and unmarked["ceiling"] == 1.0
    # a build without the key: the trailing-"?" test of round 3, flagged so main() can count the fallback
    old = labs(scenario("restraint_r4", 1, "what is the weather", [("final", "Which city should I check?")]))[0]
    assert old["ask"] and old["clarify"] and old["clarify_fallback"] is True
    # a clarification after a lookup (the which-one question) is an ask too; narrate is off on it
    which = labs(scenario("compose_r3", 1, "text alex", [("call", "lookup_contact", {"name": "Alex"}, "Alex A: 1, Alex B: 2"),
                                                          ("final", "Which Alex do you mean?")], final_meta={"clarify": True}))[1]
    assert which["ask"] and not which["narrate"] and which["ceiling"] == 1.5 and which["rl_kind"] == "decision"
    # build-wide normalisation: a build that has the key on some steps treats the others as clarify False (no fallback)
    pairs = scenario("restraint_r4", 1, "what is the weather", [("final", "Do you want me to guess?")]) + \
        scenario("restraint_r4", 2, "what is the news", [("final", "Which topic?")], final_meta={"clarify": True})
    assert X.normalise_clarify(pairs) is True and pairs[0][1]["clarify"] is False
    assert X.normalise_clarify(scenario("restraint_r4", 3, "x y", [("final", "Z?")])) is False


def test_rl_kind_order_and_ceiling_for_every_kind():
    sess = {"thread": "t1"}
    cases = [
        # (pairs, step index, expected rl_kind, ceiling)
        (scenario("chat_voice_r4", 1, "i had a long day", [("final", "That sounds draining.")], meta={"distilled": True}), 0, "chat_anchor", 1.0),
        (scenario("knowledge_direct", 1, "why is the sky blue", [("final", "Air scatters blue light.")], meta={"distilled": True}), 0, "chat_anchor", 1.0),
        (scenario("session_chat_r4", 1, "my dog", [("final", "Max.")], final_meta={**sess, "qkind": "rec_user", "recall_refs": ["Max"]}), 0, "recall", 2.0),
        (scenario("session_chat_r4", 2, "my cat", [("final", "No idea yet.")], final_meta={**sess, "qkind": "rec_none", "recall_absent": "cat"}),
         0, "recall_none", 1.5),
        (scenario("session_chat_r4", 3, "i am sad", [("final", "I am here.")], final_meta={**sess, "qkind": "emo"}), 0, "session_anchor", 1.0),
        (scenario("instructions_r4", 1, "why do onions make you cry", [("final", "A gas stings.")]), 0, "instruction", 1.0),
        (scenario("mail_r3", 1, "what is in my inbox", [("call", "summarize_emails", {}, "3 emails. Maya Chen at 12:30 PM"),
                                                       ("final", "Maya Chen wrote about lunch at 12:30 PM.")]), 1, "narrate", 2.0),
        (scenario("mail_r3", 2, "what is in my inbox", [("call", "summarize_emails", {}, "no emails"), ("final", "Nothing new.")]), 1, "select", 1.0),
        (scenario("mail_r3", 3, "what is in my inbox", [("call", "summarize_emails", {}, "3 emails"), ("final", "Done.")]), 0, "select", 1.5),
        (scenario("compose_r3", 1, "text dad", [("call", "lookup_contact", {"name": "Dad"}, "Dad: 1"),
                                               ("call", "send_message", {"to": "Dad", "text": "hi"}, "sent"), ("final", "Done.")],
                  writes=("send_message",)), 0, "call", 1.5),
        (scenario("compose_r3", 2, "text dad", [("call", "lookup_contact", {"name": "Dad"}, "Dad: 1"),
                                               ("call", "send_message", {"to": "Dad", "text": "hi"}, "sent"), ("final", "Done.")],
                  writes=("send_message",)), 2, "decision", 1.0),
        (scenario("mail_r3", 4, "what is in my inbox", [("call", "summarize_emails", {}, "3 emails"), ("call", "summarize_emails", {}, "3 emails"),
                                                       ("final", "Done.")]), 1, "call", 1.5),          # a later call step of a selection scenario
        (scenario("picture_turn_r3", 1, "what does this say", [("call", "read_attached_picture", {}, "UNTRUSTED CONTENT: Wisp's reading of x\n\nT"),
                                                              ("final", "A flyer.")]), 1, "select", 1.0),
    ]
    for pairs, i, kind, ceiling in cases:
        lab = labs(pairs)[i]
        assert (lab["rl_kind"], lab["ceiling"]) == (kind, ceiling), (pairs[i][1]["id"], lab["rl_kind"], lab["ceiling"])
        assert X.r5_label_problems(lab) == [], (pairs[i][1]["id"], X.r5_label_problems(lab))
    # the C17 instruction term lifts the instruction ceiling
    assert labs(scenario("instructions_r4", 1, "why do onions make you cry", [("final", "A gas stings.")]), instruction_term=True)[0]["ceiling"] == 1.5
    # the order: an instructions_r4 final that is also a narrate-with-facts final is an instruction (3 before 4)
    both = scenario("instructions_r4", 2, "what is up", [("call", "get_upcoming", {}, "Dentist 9 AM"), ("final", "The dentist is at 9 AM.")])
    assert labs(both)[1]["rl_kind"] == "instruction"


def test_ceiling_for_the_whole_table_and_no_default():
    t = X.ceiling_for
    assert [t("call", "call"), t("call", "load"), t("select", "call"), t("select", "final")] == [1.5, 1.5, 1.5, 1.0]
    assert [t("narrate", "final"), t("recall", "final"), t("recall_none", "final")] == [2.0, 2.0, 1.5]
    assert [t("chat_anchor", "final"), t("session_anchor", "final"), t("decision", "final")] == [1.0, 1.0, 1.0]
    assert t("decision", "ask", clarify=True) == 1.5 and t("select", "ask", clarify=True) == 1.5
    assert t("instruction", "final") == 1.0 and t("instruction", "final", instruction_term=True) == 1.5
    with pytest.raises(KeyError):
        t("mystery", "final")
    with pytest.raises(KeyError):
        t("call", "final")
    assert set(X.TEXT_RL_KINDS) == {"chat_anchor", "session_anchor", "recall", "recall_none", "instruction", "narrate", "decision",
                                    "select", "call"}


def test_main_exits_when_a_text_row_has_no_ceiling_entry(tmp_path, monkeypatch):
    w = r5_world(tmp_path)
    monkeypatch.setattr(X, "assign_rl_kind", lambda *a, **k: "mystery")
    with pytest.raises(SystemExit) as e:
        F.run_main(r5_argv(w))
    assert "mystery" in str(e.value) and "ceiling" in str(e.value)


def test_missing_qkind_and_inconsistent_recall_fields_are_errors():
    final = lambda **fm: scenario("session_chat_r4", 1, "what is my dog called", [("final", "Max.")], final_meta=fm)          # noqa: E731
    with pytest.raises(X.BuilderMetaError, match="qkind"):
        labs(final())
    assert labs(final(), allow_old_build=True)[0]["rl_kind"] == "session_anchor"
    with pytest.raises(X.BuilderMetaError, match="empty meta.recall_refs"):
        labs(final(qkind="rec_user"))
    with pytest.raises(X.BuilderMetaError, match="recall_absent"):
        labs(final(qkind="rec_none"))
    with pytest.raises(X.BuilderMetaError, match="non-empty meta.recall_refs"):
        labs(final(qkind="rec_none", recall_absent="cat", recall_refs=["lemon"]))
    with pytest.raises(X.BuilderMetaError, match="session anchor has none"):
        labs(final(qkind="build", recall_refs=["lemon", "dinosaurs"]))                  # refs.json has refs on anchors too: never copied
    ok = labs(final(qkind="rec_none", recall_absent="favorite color", recall_ok=["blue"]))[0]
    assert (ok["recall_refs"], ok["recall_absent"], ok["recall_ok"]) == ([], "favorite color", ["blue"])
    rec = labs(final(qkind="rec_both", recall_refs=["Max", "lake"], thread="t9"))[0]
    assert rec["recall_refs"] == ["Max", "lake"] and rec["thread"] == "t9" and rec["qkind"] == "rec_both" and "recall_absent" not in rec
    anchor = labs(final(qkind="emo", thread="t9"))[0]
    assert not ({"recall_refs", "recall_absent", "recall_ok"} & set(anchor))


# =========================================================================== facts and the other C6/C7 label keys
def test_facts_only_on_narrate_finals_or_explicit_meta_facts():
    reply = "Your lunch with Maya Chen is at 12:30 PM and a 40% discount."
    narrate = labs(scenario("mail_r3", 1, "what is in my inbox", [("call", "summarize_emails", {}, "x"), ("final", reply)]))[1]
    assert narrate["rl_kind"] == "narrate" and "12:30 PM" in narrate["facts"] and "Maya Chen" in narrate["facts"]
    # the same text as a chat / session / selection / compose answer carries none of the extracted facts
    for fam, fm in (("chat_voice_r4", {}), ("session_recall_r4", {}), ("instructions_r4", {}), ("followup_r3", {}), ("knowledge_direct", {}),
                    ("session_chat_r4", {"qkind": "fact", "thread": "t"}), ("restraint_r4", {})):
        assert labs(scenario(fam, 1, "tell me a thing about it", [("final", reply)], final_meta=fm))[0]["facts"] == [], fam
    # no extraction after a read either when the answer is a chat-family answer; an explicit builder list is used as given
    given = labs(scenario("session_recall_r4", 2, "what was it", [("final", reply)], final_meta={"facts": ["12:30 PM"]}))[0]
    assert given["facts"] == ["12:30 PM"]
    given_narrate = labs(scenario("mail_r3", 2, "what is in my inbox", [("call", "summarize_emails", {}, "x"), ("final", reply)],
                                  final_meta={"facts": ["Maya"]}))[1]
    assert given_narrate["facts"] == ["Maya"]
    # round 4 is unchanged: it extracted on every non-ask final
    assert labs(scenario("chat_voice_r4", 1, "tell me", [("final", reply)]), rnd=4)[0]["facts"]
    # recall rows no longer get recall_facts / the recall flag at round 5
    r5 = labs(scenario("session_recall_r4", 3, "what was my parking spot", [("final", "You parked on level 3, spot 42.")],
                       earlier=[("I parked on level 3 spot 42", "Got it.")]))[0]
    assert "recall" not in r5 and "denial_ok" not in r5 and r5["facts"] == []
    assert labs(scenario("session_recall_r4", 3, "what was my parking spot", [("final", "You parked on level 3, spot 42.")],
                         earlier=[("I parked on level 3 spot 42", "Got it.")]), rnd=4)[0]["recall"] is True


def test_label_keys_unsafe_reads_skill_names_tool_names_ground_and_style():
    pairs = scenario("restraint_r4", 1, "what is the weather", [("call", "recall", {"q": "home city"}, "nothing"),
                                                               ("final", "Which city should I check?")],
                     meta={"unsafe_reads": ["get_weather", "get_weather", "summarize_emails"]}, final_meta={"clarify": True})
    call, final = labs(pairs)
    for lab in (call, final):
        assert lab["unsafe_reads"] == ["get_weather", "summarize_emails"]                 # sorted, deduplicated
        assert lab["skill_names"] == ["idea-refine", "mac-controls", "travel-notes"]
        assert lab["tool_names"] == TOOLS                                                  # on the call row too (round 4 wrote finals only)
    assert "tool_names" not in labs(pairs, rnd=4)[0] and labs(pairs, rnd=4)[1]["tool_names"] == TOOLS
    assert "skill_names" not in labs(pairs, rnd=4)[1] and "unsafe_reads" not in labs(pairs, rnd=4)[1]
    # no listing -> []
    bare = scenario("restraint_r4", 2, "x y", [("final", "Which?")], final_meta={"clarify": True})
    bare[0][0]["messages"][0] = {"role": "system", "content": "Be helpful."}
    assert labs(bare)[0]["skill_names"] == []
    # ground: on narrate and on session finals that are not anchors; never on a session anchor or a chat anchor
    g = labs(scenario("session_chat_r4", 5, "what is my dog", [("final", "Max.")], earlier=[("my dog is Max", "ok")],
                      final_meta={"qkind": "rec_user", "recall_refs": ["Max"], "thread": "t"}))[0]
    assert "my dog is Max" in g["ground"]
    a = labs(scenario("session_chat_r4", 6, "hello there", [("final", "Hello.")], final_meta={"qkind": "emo", "thread": "t"}))[0]
    assert "ground" not in a
    assert "style" not in labs(scenario("chat_voice_r4", 7, "hi hi", [("final", "Hi, friend \U0001F319")], meta={"distilled": True}))[0]
    assert "style" in labs(scenario("chat_voice_r4", 7, "hi hi", [("final", "Hi, friend \U0001F319")], meta={"distilled": True}), rnd=4)[0]
    # banned strings of a text injection row ride along
    assert labs(scenario("ignore_embedded_instructions", 1, "read it", [("final", "ok")], meta={"banned": ["wire the money"]}))[0]["banned"] \
        == ["wire the money"]


def test_skill_names_match_dist_choose():
    import importlib.util
    dist = Path(X.__file__).resolve().parent.parent.parent / "ask-lora-r4" / "distill"
    if not (dist / "choose.py").is_file():
        pytest.skip("distill is not merged here")
    sys.path.insert(0, str(dist))
    sys.modules.pop("gen", None)
    try:
        spec = importlib.util.spec_from_file_location("dist_choose_xcheck", dist / "choose.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(dist))
        sys.modules.pop("gen", None)
    for msgs in (scenario("mail_r3", 1, "x", [("final", "y")])[0][0]["messages"],
                 [{"role": "system", "content": "No listing."}, {"role": "user", "content": "x"}],
                 [{"role": "system", "content": "Installed skills\n- a-b: one\n- c9: two\nnot: a skill\n- Bad: no"}, {"role": "user", "content": "x"}]):
        assert X.skill_names_of(msgs[:-1]) == mod.skill_names({"messages": msgs})


def test_max_tokens_on_chat_and_session_rows_only_from_the_flag():
    chat = labs(scenario("chat_voice_r4", 1, "hi hi", [("final", "Hi.")], meta={"distilled": True}), chat_max_tokens=1024)[0]
    sess = labs(scenario("followup_r3", 1, "and the other", [("call", "get_upcoming", {}, "x"), ("final", "Done.")]), chat_max_tokens=1024)
    other = labs(scenario("mail_r3", 1, "inbox", [("call", "summarize_emails", {}, "x"), ("final", "Done.")]), chat_max_tokens=1024)
    assert chat["max_tokens"] == 1024 and [l["max_tokens"] for l in sess] == [1024, 1024]              # every step of a session turn
    assert all("max_tokens" not in l for l in other)
    assert labs(scenario("chat_voice_r4", 1, "hi hi", [("final", "Hi.")], meta={"distilled": True}), chat_max_tokens=1536)[0]["max_tokens"] == 1536
    assert "max_tokens" not in labs(scenario("chat_voice_r4", 1, "hi hi", [("final", "Hi.")], meta={"distilled": True}))[0]


# =========================================================================== the round-5 mix (unit)
def ritem(i, family, kind, step=None, **lab):
    step = step or ("call" if kind in ("call", "select") else "final")
    return {"id": f"{family}-{kind}-{i}", "family": family, "prompt": "p", "weight": 1.0, "_tokens": 100,
            "label": {"rl_kind": kind, "step": step, "narrate": kind == "narrate", "facts": ["6 PM"] if kind == "narrate" else [], **lab}}


def big_pool():
    items = []
    for fam in ("safe_restraint", "gather_then_send_r4", "restraint_r4", "compose_r3", "reminders_r3", "safe_effects", "no_write_for_reads"):
        items += [ritem(i, fam, "call") for i in range(120)] + [ritem(i + 500, fam, "decision") for i in range(120)]
    for fam in ("mail_r3", "device_r3", "calendar_r3", "compose_r3"):
        items += [ritem(i, fam, "narrate") for i in range(200)]
    for fam, kind, n in (("session_chat_r4", "recall", 54), ("session_chat_r4", "recall_none", 14), ("session_chat_r4", "session_anchor", 99),
                         ("followup_r3", "call", 100), ("followup_r3", "narrate", 100), ("session_recall_r4", "call", 80),
                         ("session_recall_r4", "decision", 70), ("instructions_r4", "instruction", 176)):
        items += [ritem(i, fam, kind) for i in range(n)]
    for fam in ("mail_r3", "device_r3", "calendar_r3", "files_r3", "web_r3"):
        items += [ritem(i + 900, fam, "select") for i in range(60)]
    items += [ritem(i, "mail_r3", "call") for i in range(300, 330)]                 # later steps of selection scenarios: never drawn
    items += [ritem(i, "chat_voice_r4", "decision") for i in range(40)]            # undistilled chat rows in the pool: never drawn
    return items


def anchor_pool(n=(60, 60, 70, 6)):
    out = []
    for fam, k in zip(("chat_voice_r4", "chat_direct_r4", "knowledge_direct", "concise_gentle"), n):
        out += [ritem(i, fam, "chat_anchor") for i in range(k)]
    return out


def test_r5_mix_quotas_follow_the_spec_at_full_size():
    n_all, n_mm, n_pic = 1488, 223, 30
    n_text = n_all - n_mm
    pics = [dict(ritem(i, "picture_turn_r3", "select"), weight=1.0) for i in range(n_pic)]
    rows, st = X.r5_text_rows(big_pool(), pics, anchor_pool(), n_text, random.Random(5))
    assert len(rows) == n_text and st["short"] == {}
    b = Counter(X.r5_bucket(r) for r in rows)
    assert b["select"] == st["quota"]["select"]
    for key, share in X.R5_SHARES.items():
        assert abs(b[key] / n_all - share) < 0.006, (key, b[key] / n_all, share)       # 28 / 28 / 12 / 6 / 11 per cent of all rows
    assert sum(X.apportion(n_text, X.R5_SHARES).values()) == n_text
    got = (st["quota"]["safety"], st["quota"]["narrate"], st["quota"]["session"], st["quota"]["chat_anchor"], st["quota"]["select"])
    assert all(abs(g - want) <= 1 for g, want in zip(got, (417, 416, 179, 89, 164))), got       # the 4.1 table's approximate row counts
    # safety draws only the safety families, upweighted ones more; narrate only labelled-fact narrate finals outside the session families
    assert {r["family"] for r in rows if X.r5_bucket(r) == "safety"} <= X.COMPOSE_SAFETY_FAMILIES
    assert all(r["label"]["rl_kind"] == "narrate" for r in rows if X.r5_bucket(r) == "narrate")
    assert not any(r["family"] in X.R5_SESSION_FAMILIES for r in rows if X.r5_bucket(r) == "narrate")
    sel = [r for r in rows if X.r5_bucket(r) == "select" and r["family"] != "picture_turn_r3"]
    assert {r["label"]["rl_kind"] for r in sel} == {"select"}                       # first-step selection rows only, never later steps
    fams = Counter(r["family"] for r in sel)
    assert max(fams.values()) - min(fams.values()) <= 1                            # stratified: families drawn evenly
    assert sum(r["family"] == "picture_turn_r3" for r in rows) == n_pic            # inside the 11%
    assert not any(r["family"] == "chat_voice_r4" and r["label"]["rl_kind"] != "chat_anchor" for r in rows)
    assert Counter(r["id"] for r in rows if X.r5_bucket(r) != "chat_anchor").most_common(1)[0][1] <= X.MAX_COPIES


def test_r5_mix_session_bucket_caps_copies_and_does_not_let_instructions_crowd_out_recall():
    rows, st = X.r5_text_rows(big_pool(), [], anchor_pool(), 1265, random.Random(6))
    ses = [r for r in rows if X.r5_bucket(r) == "session"]
    assert len(ses) == st["quota"]["session"] and abs(len(ses) - 179) <= 1
    assert Counter(r["id"] for r in ses).most_common(1)[0][1] <= X.R5_SESSION_MAX_COPIES == 2
    src = Counter(f"{r['family']}:{r['label']['rl_kind']}" for r in ses)
    assert dict(src) == st["session_sources"]                                         # per-source counts are recorded
    pg = sum(v for k, v in src.items() if k.endswith(("recall", "recall_none")) or k.startswith(("followup_r3", "session_recall_r4")))
    assert src["instructions_r4:instruction"] < pg / 2                                # 176 instruction rows in the pool, 0.5 weight
    rate = lambda k, pool: src[k] / pool                                               # noqa: E731
    assert rate("session_chat_r4:recall", 54) > rate("instructions_r4:instruction", 176)


def test_r5_mix_chat_anchors_are_proportional_and_short_anchors_exit():
    rows, st = X.r5_text_rows(big_pool(), [], anchor_pool(), 1265, random.Random(7))
    fam = st["chat_anchor_families"]
    assert sum(fam.values()) == 89 == st["got"]["chat_anchor"] and fam["knowledge_direct"] > fam["chat_voice_r4"] > fam["concise_gentle"] >= 1
    assert abs(fam["chat_voice_r4"] / 89 - 60 / 196) < 0.02                          # proportional to family size
    assert len({r["id"] for r in rows if X.r5_bucket(r) == "chat_anchor"}) == 89     # distinct: no copies of an anchor
    with pytest.raises(SystemExit, match="chat anchors: only 50"):
        X.r5_text_rows(big_pool(), [], anchor_pool((20, 20, 10, 0)), 1265, random.Random(7))


def test_r5_mix_thin_bucket_is_topped_up_from_selection_and_reported():
    thin = [it for it in big_pool() if X.r5_bucket(it) != "narrate"] + [ritem(i, "mail_r3", "narrate") for i in range(5)]
    rows, st = X.r5_text_rows(thin, [], anchor_pool(), 1265, random.Random(8))
    assert len(rows) == 1265 and st["short"] == {"narrate": 417 - 15}
    assert st["got"]["select"] == st["quota"]["select"] + 402


def test_r5_tie_reason_only_for_a_degenerate_scripted_target():
    assert X.r5_tie_reason({"step": "final", "ideal_text": "  "}) == "empty scripted final"
    assert X.r5_tie_reason({"step": "final", "ideal_text": "Done."}) is None
    assert X.r5_tie_reason({"step": "call", "ideal_text": ""}) is None


# =========================================================================== main(): the whole round-5 export
def text_rows(path):
    return [(r, json.loads(r["label"])) for r in read_rows(path) if r["modality"] == "text"]


def test_r5_export_end_to_end_invariants(tmp_path):
    w = r5_world(tmp_path)
    assert F.run_main(r5_argv(w)) == 0
    out = w["tmp"] / "out5" / "grpo_prompts.jsonl"
    rows = text_rows(out)
    st = json.loads(out.with_suffix(".stats.json").read_text())
    assert st["round"] == 5 and st["rows"] == len(rows) == 96 and st["unique_ids"] == len({r["id"] for r, _ in rows})
    kinds = Counter(l["rl_kind"] for _, l in rows)
    assert sum(kinds.values()) == len(rows) and set(kinds) <= set(X.TEXT_RL_KINDS)               # exactly one rl_kind per text row
    for r, l in rows:
        assert l["round"] == 5 and l["ceiling"] == X.ceiling_for(l["rl_kind"], l["step"], l["clarify"]), r["id"]
        assert l["tool_names"] == sorted(l["tool_names"]) and "send_message" in l["tool_names"]         # on call rows too
        assert isinstance(l["unsafe_reads"], list) and l["skill_names"] == ["idea-refine", "mac-controls", "travel-notes"]
        assert "style" not in l and X.r5_label_problems(l) == [], (r["id"], X.r5_label_problems(l))
        if l["rl_kind"] != "narrate":
            assert l["step"] in ("call", "load") or l["facts"] == [], r["id"]                           # facts only on narrate finals
        assert ("max_tokens" in l) == (l["rl_kind"] == "chat_anchor" or r["family"] in X.R5_SESSION_FAMILIES), r["id"]
    assert st["r5"]["rl_kind"] == dict(kinds) and st["r5"]["text_rows"] == 96
    # nothing from the valid split, by id or by question fold, and no scripted chat row
    valid_ids = w["valid"]
    valid_folds = {X.leak_mod().fold(m["prompt"]) for _, m in w["pairs"] if m["id"] in valid_ids}
    exported = {r["id"].rsplit("#", 1)[0] for r, _ in rows}
    assert not exported & valid_ids and st["dropped"]["valid split"] == len(valid_ids)
    assert not any(r["family"] in ("chat_voice_r4", "knowledge_direct", "concise_gentle", "chat_direct_r4") and l["rl_kind"] != "chat_anchor"
                   for r, l in rows)
    # anchors: prefixed ids, distilled only, per-family counts recorded, session rows carry the builder's thread / qkind
    anchors = [(r, l) for r, l in rows if l["rl_kind"] == "chat_anchor"]
    assert anchors and all(r["id"].startswith("anchor:") and l["chat"] and not l["ask"] for r, l in anchors)
    assert sum(st["r4_mix"]["chat_anchor_families"].values()) == len(anchors) == st["r4_mix"]["quota"]["chat_anchor"]
    ses = [l for _, l in rows if l["rl_kind"] in ("recall", "recall_none", "session_anchor")]
    assert ses and all(l["thread"] and l["qkind"] for l in ses)
    assert all(l["recall_refs"] for _, l in rows if l["rl_kind"] == "recall")
    assert all(l["recall_absent"] and l["recall_refs"] == [] for _, l in rows if l["rl_kind"] == "recall_none")
    # the sidecar has one record per row, with every user turn and the conversation text
    side = read_rows(out.with_suffix(".overlap.jsonl"))
    assert not any(set(s["folds"]) & valid_folds for s in side)
    assert [s["id"] for s in side] == [r["id"] for r in read_rows(out)] and all(s["users"] and "conv" in s for s in side)
    assert st["r5"]["sidecars"]["prompts_sha256"] == hashlib.sha256(out.with_suffix(".overlap.jsonl").read_bytes()).hexdigest()
    # C5: re-exporting at 1536
    assert F.run_main(r5_argv(w, w["tmp"] / "out5b" / "grpo_prompts.jsonl", extra=("--chat-max-tokens", "1536"))) == 0
    assert {l["max_tokens"] for _, l in text_rows(w["tmp"] / "out5b" / "grpo_prompts.jsonl") if "max_tokens" in l} == {1536}


def test_r5_export_mixed_with_vision_picture_reads_and_audio(tmp_path):
    w = r5_world(tmp_path)
    vis, aud = mm_inputs(tmp_path / "mm_in")
    mixed = ["--vision-rows", str(vis), "--audio-rows", str(aud), "--allow-missing-media"]
    argv = r5_argv(w, extra=mixed)
    argv[argv.index("--mm-share") + 1] = "0.15"
    argv[argv.index("--picture-share") + 1] = "0.1"
    assert F.run_main(argv) == 0
    out = w["tmp"] / "out5" / "grpo_prompts.jsonl"
    rows = read_rows(out)
    mm = [(r, json.loads(r["label"])) for r in rows if r["modality"] != "text"]
    assert {r["family"] for r, _ in mm} >= {"vision_r3", "picture_read_r3", "audio_r3"}
    assert all(l["rl_kind"] == "mm" and l["ceiling"] is None and l["round"] == 5 for _, l in mm)
    dev = [json.loads(r["label"]) for r in read_rows(out.with_name("grpo_mm_dev.jsonl"))]
    assert dev and all(l["rl_kind"] == "mm" and l["ceiling"] is None and l["round"] == 5 for l in dev)
    # the mm labels are today's labels (label.kind and the rest unchanged) plus the three round-5 keys
    r4 = r5_world(tmp_path / "r4w")
    argv4 = ["--build-dir", str(r4["build"]), "--model", "x", "--round", "4", "--out", str(tmp_path / "o4" / "grpo_prompts.jsonl"), "--n", "96",
             "--vision-rows", str(vis), "--audio-rows", str(aud), "--allow-missing-media", "--picture-share", "0.1"]
    assert F.run_main(argv4) == 0
    old = {r["id"]: json.loads(r["label"]) for r in read_rows(tmp_path / "o4" / "grpo_prompts.jsonl") if r["modality"] != "text"}
    old.update({r["id"]: json.loads(r["label"]) for r in read_rows(tmp_path / "o4" / "grpo_mm_dev.jsonl")})
    new = {r["id"]: json.loads(r["label"]) for r, _ in mm}
    new.update({r["id"]: json.loads(r["label"]) for r in read_rows(out.with_name("grpo_mm_dev.jsonl"))})
    shared = set(old) & set(new)
    assert shared
    for i in shared:
        assert {k: v for k, v in new[i].items() if k not in ("round", "rl_kind", "ceiling")} == old[i], i
    assert {l.get("kind") for l in new.values()} >= {"image", "picture_read"}
    # a mixed export still passes the label checks: text rows have ceilings, mm rows none
    assert Counter(l["rl_kind"] == "mm" for _, l in [(r, json.loads(r["label"])) for r in rows]) [True] == len(mm)


def test_r5_export_exits_without_valid_meta_or_with_include_valid(tmp_path):
    w = r5_world(tmp_path)
    (w["build"] / "valid.meta.jsonl").rename(w["build"] / "valid.meta.jsonl.gone")
    with pytest.raises(SystemExit, match="valid.meta.jsonl is missing"):
        F.run_main(r5_argv(w))
    (w["build"] / "valid.meta.jsonl.gone").rename(w["build"] / "valid.meta.jsonl")
    with pytest.raises(SystemExit, match="include-valid"):
        F.run_main(r5_argv(w, extra=("--include-valid",)))
    # rounds 3 and 4 keep skipping a missing valid file silently (byte-identical behaviour)
    (w["build"] / "valid.meta.jsonl").unlink()
    assert F.run_main(["--build-dir", str(w["build"]), "--model", "x", "--round", "4", "--out", str(tmp_path / "o4" / "g.jsonl"), "--n", "32",
                       "--mm-share", "0", "--picture-share", "0"]) == 0


def test_r5_export_requires_the_anchor_dev_and_picker_inputs(tmp_path):
    w = r5_world(tmp_path)
    base = r5_argv(w)
    for flag in ("--anchor-build-dir", "--dev-build-dir", "--picker-dev"):
        argv = list(base)
        i = argv.index(flag)
        del argv[i:i + 2]
        with pytest.raises(SystemExit, match=flag):
            F.run_main(argv)
    argv = list(base)
    for flag in ("--dev-build-dir", "--picker-dev"):
        i = argv.index(flag)
        del argv[i:i + 2]
    assert F.run_main(argv + ["--no-text-dev"]) == 0
    assert not (w["tmp"] / "out5" / "grpo_text_dev.jsonl").exists()
    assert json.loads((w["tmp"] / "out5" / "grpo_prompts.stats.json").read_text())["r5"]["text_dev"] is None


def test_r5_export_session_count_and_builder_field_checks(tmp_path):
    w = r5_world(tmp_path)
    got = X.session_counts([m for _, m in w["pairs"]])
    assert got["recall"] and got["recall_none"] and got["session_anchor"] and not got["no_qkind"]
    argv = r5_argv(w)
    i = argv.index("--expect-session-chat")
    ok = list(argv)
    ok[i + 1] = f"{got['recall']},{got['recall_none']},{got['session_anchor']}"
    assert F.run_main(ok) == 0
    bad = list(argv)
    bad[i + 1] = f"{got['recall'] + 1},{got['recall_none']},{got['session_anchor']}"
    with pytest.raises(SystemExit, match="session_chat_r4 recall: .* in the build, .* expected"):
        F.run_main(bad)
    default = list(argv)
    default[i + 1:i + 2] = ["54,14,99"]                                        # the builder's real counts: this small fixture differs
    with pytest.raises(SystemExit, match="expected"):
        F.run_main(default)
    # a build whose session_chat_r4 finals have no qkind (it predates C6/C7)
    old = r5_world(tmp_path / "old")
    pairs = [(e, {k: v for k, v in m.items() if k not in ("qkind", "thread", "recall_refs", "recall_absent", "recall_ok")}) for e, m in old["pairs"]]
    F.write_build(old["build"], pairs, old["valid"])
    with pytest.raises(SystemExit, match="no meta.qkind"):
        F.run_main(r5_argv(old))
    assert F.run_main(r5_argv(old, old["tmp"] / "oo" / "g.jsonl", extra=("--allow-old-build",))) == 0
    st = json.loads((old["tmp"] / "oo" / "g.stats.json").read_text())["r5"]
    assert st["old_build"] is True and st["session_counts"]["problems"]


def test_r5_export_refs_crosscheck_and_dist_record(tmp_path):
    w = r5_world(tmp_path)
    refs = {m["id"]: {"kind": m["qkind"], "ref": m.get("recall_refs") or ["x"], "absent": m.get("recall_absent"), "ok": m.get("recall_ok") or []}
            for _, m in w["pairs"] if m["family"] == "session_chat_r4"}
    good = tmp_path / "refs.json"
    good.write_text(json.dumps(refs))
    dist = tmp_path / "dist" / "raw" / "v1"
    dist.mkdir(parents=True)
    (tmp_path / "dist" / "choose.py").write_text("# choose\n")
    (dist / "session_chat.refs.json").write_text(json.dumps(refs))
    argv = r5_argv(w, extra=("--refs-crosscheck", str(good)))
    argv[argv.index("--dist-dir") + 1] = str(tmp_path / "dist")
    assert F.run_main(argv) == 0
    st = json.loads((w["tmp"] / "out5" / "grpo_prompts.stats.json").read_text())["r5"]["dist"]
    assert st["choose_sha256"] == hashlib.sha256(b"# choose\n").hexdigest()
    assert st["refs_sha256"] == hashlib.sha256((dist / "session_chat.refs.json").read_bytes()).hexdigest()
    assert st["crosscheck_sha256"] == hashlib.sha256(good.read_bytes()).hexdigest() and st["commit"] is None      # not a git checkout
    # a mismatching refs entry fails the export
    k = next(i for i, r in refs.items() if r["kind"] == "rec_user")
    refs[k]["ref"] = ["different"]
    good.write_text(json.dumps(refs))
    with pytest.raises(SystemExit, match="recall_refs"):
        F.run_main(r5_argv(w, w["tmp"] / "o2" / "g.jsonl", extra=("--refs-crosscheck", str(good))))
    # the real distill dir: its commit is a 40-hex sha (or None when git is unavailable) and its choose.py hash is recorded
    real = X.dist_record(Path(X.__file__).resolve().parent.parent.parent / "ask-lora-r4" / "distill")
    assert real["choose_sha256"] and (real["commit"] is None or len(real["commit"]) == 40)
    if real["refs_file"]:
        assert real["refs_sha256"] == hashlib.sha256(Path(real["refs_file"]).read_bytes()).hexdigest()


def test_session_counts_and_refs_crosscheck_helpers():
    metas = [{"id": "a", "family": "session_chat_r4", "target_kind": "final", "qkind": k} for k in ("rec_user", "rec_asst", "rec_both", "rec_none", "emo", "fact")]
    assert X.session_counts(metas) == {"recall": 3, "recall_none": 1, "session_anchor": 2, "no_qkind": 0}
    assert X.session_count_problems(metas, {"recall": 3, "recall_none": 1, "session_anchor": 2}) == []
    assert X.session_count_problems(metas, {"recall": 54, "recall_none": 14, "session_anchor": 99})
    assert X.session_count_problems([{"id": "x", "family": "mail_r3"}], {"recall": 54}) == []                  # no session_chat_r4 at all
    refs = {"s1": {"kind": "rec_user", "ref": ["Max"], "absent": None, "ok": []}, "s2": {"kind": "emo", "ref": ["lemon"], "absent": None, "ok": []},
            "s3": {"kind": "rec_none", "ref": ["lemon"], "absent": "colour", "ok": ["blue"]}}
    ok = [{"id": "s1", "family": "session_chat_r4", "recall_refs": ["Max"]}, {"id": "s2", "family": "session_chat_r4"},
          {"id": "s3", "family": "session_chat_r4", "recall_absent": "colour", "recall_ok": ["blue"]}]
    assert X.refs_crosscheck(ok, refs) == []
    # refs.json carries refs on anchors and never-said rows: copying them by "non-empty" would be caught
    bad = [dict(ok[0]), dict(ok[1], recall_refs=["lemon"]), dict(ok[2], recall_refs=["lemon"])]
    assert len(X.refs_crosscheck(bad, refs)) == 2


# =========================================================================== C6b: the text dev set
def dev_rows(w):
    return read_rows(w["tmp"] / "out5" / "grpo_text_dev.jsonl")


def test_text_dev_files_counts_samples_smoke_and_stats(tmp_path):
    w = r5_world(tmp_path)
    assert F.run_main(r5_argv(w)) == 0
    out = w["tmp"] / "out5"
    dev = dev_rows(w)
    by_slice = Counter(r["slice"] for r in dev)
    assert by_slice == {"safety": 4, "narrate": 4, "select": 2, "recall_dev_r4": 24, "session_style_dev": 12, "chat": 6}
    samples = {"safety": 4, "recall_dev_r4": 4, "narrate": 2, "select": 2, "session_style_dev": 2, "chat": 2}
    for r in dev:
        lab = json.loads(r["label"])
        assert r["samples"] == lab["samples"] == samples[r["slice"]]
        assert lab["round"] == 5 and lab["rl_kind"] in X.TEXT_RL_KINDS and lab["ceiling"] == X.ceiling_for(lab["rl_kind"], lab["step"], lab["clarify"])
        assert X.r5_label_problems(lab) == [] and r["modality"] == "text"
    kinds = {s: Counter(json.loads(r["label"])["rl_kind"] for r in dev if r["slice"] == s) for s in by_slice}
    assert set(kinds["chat"]) == {"chat_anchor"} and set(kinds["narrate"]) == {"narrate"}
    assert set(kinds["recall_dev_r4"]) == {"recall", "recall_none"} and set(kinds["session_style_dev"]) == {"session_anchor"}
    for r in dev:
        lab = json.loads(r["label"])
        if r["slice"] in ("recall_dev_r4", "session_style_dev"):
            assert lab["thread"] and lab["qkind"] and lab["max_tokens"] == 1024                     # the session cap, as on the train rows
        if lab["rl_kind"] == "recall":
            assert lab["recall_refs"]
        if lab["rl_kind"] == "recall_none":
            assert lab["recall_absent"] and lab["recall_refs"] == []
    smoke = read_rows(out / "grpo_text_dev.smoke30.jsonl")
    assert Counter(r["slice"] for r in smoke) == {"safety": 3, "narrate": 2, "select": 1, "recall_dev_r4": 8}
    assert {r["id"] for r in smoke} <= {r["id"] for r in dev}
    assert {r["id"]: r["samples"] for r in smoke} == {r["id"]: r["samples"] for r in dev if r["id"] in {s["id"] for s in smoke}}
    rep = json.loads((out / "grpo_text_dev.stats.json").read_text())
    assert rep["requested"]["safety"] == 4 and rep["actual"] == dict(by_slice) and rep["seed"] == 20261101 and rep["errors"] == []
    assert rep["picker"] == {"given": False, "rows": 0, "folds": 0, "overlap": 0}
    assert rep["files"]["text_dev_sha256"] == hashlib.sha256((out / "grpo_text_dev.jsonl").read_bytes()).hexdigest()
    assert rep["files"]["smoke30_sha256"] == hashlib.sha256((out / "grpo_text_dev.smoke30.jsonl").read_bytes()).hexdigest()
    assert rep["smoke30"] == {"safety": 3, "narrate": 2, "select": 1, "recall_dev_r4": 8} and len(rep["smoke_ids"]) == 14
    assert set(rep["source_ids"]) == set(by_slice) and rep["source_ids"]["recall_dev_r4"] == sorted(rep["source_ids"]["recall_dev_r4"])
    # disjoint from the exported prompts by id and question fold, from the dev-only families, and chat from the anchor train rows
    prompts = text_rows(out / "grpo_prompts.jsonl")
    p_ids = {r["id"].rsplit("#", 1)[0] for r, _ in prompts}
    assert not {r["id"].rsplit("#", 1)[0] for r in dev if r["slice"] != "chat"} & p_ids
    assert not any(r["family"] in X.DEV_ONLY_FAMILIES for r, _ in prompts)
    assert not {r["id"].rsplit("#", 1)[0] for r in dev if r["slice"] == "chat"} & {i for i in p_ids if i.startswith("anchor:")}
    side = {s["id"]: s for s in read_rows(out / "grpo_text_dev.overlap.jsonl")}
    assert set(side) == {r["id"] for r in dev} and all(s["users"] for s in side.values())
    p_threads = {l["thread"] for _, l in prompts if l.get("thread")}
    assert not {s["thread"] for s in side.values() if s["thread"]} & p_threads
    # same seed, same bytes
    assert F.run_main(r5_argv(w, w["tmp"] / "again" / "grpo_prompts.jsonl")) == 0
    assert (w["tmp"] / "again" / "grpo_text_dev.jsonl").read_bytes() == (out / "grpo_text_dev.jsonl").read_bytes()
    assert (w["tmp"] / "again" / "grpo_prompts.jsonl").read_bytes() == (out / "grpo_prompts.jsonl").read_bytes()


def test_text_dev_at_the_spec_sizes(tmp_path):
    """50 safety/compose + 40 narrate + 10 selection + 24 recall_dev_r4 + 12 session_style_dev + 30 chat, smoke30 = 15/5/2/8."""
    pairs = corpus(70, r5_meta=True)
    w = {"build": F.write_build(tmp_path / "data-rl", pairs, valid_ids_of(pairs, every=3)), "tmp": tmp_path, "pairs": pairs,
         "anchor": anchor_build(tmp_path / "anchor", n=120)[0], "dev": dev_build(tmp_path / "devb")[0], "valid": valid_ids_of(pairs, every=3)}
    argv = r5_argv(w)
    for flag in ("--dev-slice-counts", "--dev-smoke-counts"):
        i = argv.index(flag)
        del argv[i:i + 2]
    argv += ["--n", "160"]
    assert F.run_main(argv) == 0
    dev = dev_rows(w)
    assert Counter(r["slice"] for r in dev) == {"safety": 50, "narrate": 40, "select": 10, "recall_dev_r4": 24, "session_style_dev": 12, "chat": 30}
    assert len(dev) == 166
    smoke = read_rows(w["tmp"] / "out5" / "grpo_text_dev.smoke30.jsonl")
    assert Counter(r["slice"] for r in smoke) == {"safety": 15, "narrate": 5, "select": 2, "recall_dev_r4": 8} and len(smoke) == 30
    assert sum(r["samples"] for r in smoke) == 15 * 4 + 5 * 2 + 2 * 2 + 8 * 4 == 106                 # the 106 decodes C10 prices
    assert len({r["id"] for r in dev}) == len(dev)
    safety = [json.loads(r["label"]) for r in dev if r["slice"] == "safety"]
    assert {l["rl_kind"] for l in safety} <= {"call", "decision"} and len({l["rl_kind"] for l in safety}) == 2     # stratified by rl_kind


def mutate_dev(w, fn):
    pairs = [(e, dict(m)) for e, m in dev_build(w["tmp"] / "scratch")[1]]
    fn(pairs)
    return F.write_build(w["tmp"] / "devb", pairs, ())


def exported_session_row(w):
    argv = r5_argv(w)
    for flag in ("--dev-build-dir", "--picker-dev"):
        i = argv.index(flag)
        del argv[i:i + 2]
    assert F.run_main(argv + ["--no-text-dev"]) == 0                  # the training rows do not depend on the dev set
    rows = text_rows(w["tmp"] / "out5" / "grpo_prompts.jsonl")
    r, l = next((r, l) for r, l in rows if r["family"] == "session_chat_r4")
    meta = next(m for _, m in w["pairs"] if m["id"] == r["id"].rsplit("#", 1)[0])
    return meta


def test_text_dev_exits_when_a_dev_question_or_thread_is_in_grpo_prompts(tmp_path):
    w = r5_world(tmp_path)
    meta = exported_session_row(w)
    # (a) the same question (fold) as a training row
    mutate_dev(w, lambda ps: [m.update(prompt=meta["prompt"]) or e["messages"].__setitem__(-2, {"role": "user", "content": meta["prompt"]})
                              for e, m in ps[:1]])
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(r5_argv(w))
    assert "shares a question fold with a grpo_prompts row" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()
    assert not (w["tmp"] / "out5" / "grpo_text_dev.jsonl").exists()                          # nothing half-written
    # (b) the same thread
    mutate_dev(w, lambda ps: [m.update(thread=meta["thread"]) for e, m in ps[:1]])
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(r5_argv(w))
    assert "also appears in grpo_prompts" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()


def test_text_dev_exits_for_a_dev_only_family_in_the_build_dir(tmp_path):
    w = r5_world(tmp_path)
    extra = session_chat(3, word(777), "recall_dev_r4", "leaked-thread", "rec_user")
    F.write_build(w["build"], w["pairs"] + extra, w["valid"])
    with pytest.raises(SystemExit, match="dev-only family"):
        F.run_main(r5_argv(w))


def test_text_dev_exits_when_a_slice_is_short_and_never_pads(tmp_path):
    w = r5_world(tmp_path)
    argv = r5_argv(w)
    argv[argv.index("--dev-slice-counts") + 1] = COUNTS.replace("safety=4", "safety=9999")
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(argv)
    assert "slice safety: " in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text() and "shrink the picker --limit" in \
        (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()
    F.write_build(w["dev"], dev_build(w["tmp"] / "tiny", recall=6, style=12)[1], ())
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(r5_argv(w))
    assert "slice recall_dev_r4: 6 questions in --dev-build-dir, 24 needed (never padded)" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()
    argv = r5_argv(w)
    argv[argv.index("--dev-slice-counts") + 1] = COUNTS.replace("chat=6", "chat=9999").replace("recall_dev_r4=24", "recall_dev_r4=6")
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(argv)
    assert "slice chat:" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()


def test_text_dev_exits_for_a_recall_dev_row_without_its_refs(tmp_path):
    w = r5_world(tmp_path)
    mutate_dev(w, lambda ps: [m.update(recall_refs=[]) for e, m in ps if m["qkind"] == "rec_user"][:1])
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(r5_argv(w))
    assert "empty meta.recall_refs" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()
    mutate_dev(w, lambda ps: [m.pop("thread") for e, m in ps[:1]])
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(r5_argv(w))
    assert "without meta.thread" in (w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text()


def test_text_dev_is_disjoint_from_the_picker_sample(tmp_path):
    w = r5_world(tmp_path)
    assert F.run_main(r5_argv(w)) == 0
    free = {r["id"].rsplit("#", 1)[0] for r in dev_rows(w) if r["slice"] in ("safety", "narrate", "select")}
    assert free
    picker = tmp_path / "picker.jsonl"
    prompts = {r["id"].rsplit("#", 1)[0]: r["prompt"] for r in dev_rows(w)}
    picker.write_text("".join(json.dumps({"id": i, "family": "x", "prompt": prompts[i], "label": {}}) + "\n" for i in sorted(free)))
    argv = r5_argv(w, w["tmp"] / "outp" / "grpo_prompts.jsonl")
    argv[argv.index("--picker-dev") + 1] = str(picker)
    assert F.run_main(argv) == 0
    again = {r["id"].rsplit("#", 1)[0] for r in read_rows(w["tmp"] / "outp" / "grpo_text_dev.jsonl") if r["slice"] in ("safety", "narrate", "select")}
    assert again and not again & free                                                         # the picker's rows are never dev rows
    rep = json.loads((w["tmp"] / "outp" / "grpo_text_dev.stats.json").read_text())
    assert rep["picker"]["given"] and rep["picker"]["rows"] == len(free) and rep["picker"]["overlap"] == 0 and rep["errors"] == []
    # a picker row with the id of a valid row but another prompt saw a different instance (a re-seed): the id match proves nothing
    first = sorted(free)[0]
    picker.write_text(json.dumps({"id": first, "family": "x", "prompt": "another instance of it", "label": {}}) + "\n")
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(argv)
    assert "different prompt" in (w["tmp"] / "outp" / "grpo_text_dev.stats.json").read_text()
    # a picker row that is not a valid row of this build cannot be proven disjoint
    picker.write_text(json.dumps({"id": "ghost-000-s0"}) + "\n")
    with pytest.raises(SystemExit, match="text dev set failed"):
        F.run_main(argv)
    assert "ghost-000-s0" in (w["tmp"] / "outp" / "grpo_text_dev.stats.json").read_text()


def test_text_dev_chat_probe_is_disjoint_from_the_anchor_train_rows(tmp_path):
    w = r5_world(tmp_path)
    assert F.run_main(r5_argv(w)) == 0
    out = w["tmp"] / "out5"
    chat = [r for r in dev_rows(w) if r["slice"] == "chat"]
    train = {r["id"] for r, l in text_rows(out / "grpo_prompts.jsonl") if l["rl_kind"] == "chat_anchor"}
    assert all(r["id"].startswith("anchor:") for r in chat) and not {r["id"].rsplit("#", 1)[0] for r in chat} & {i for i in train}
    a_valid = {"anchor:" + i for i in anchor_build(tmp_path / "a2")[2]}
    assert {r["id"].rsplit("#", 1)[0] for r in chat} <= a_valid                                 # the distilled VALID side of the anchor build
    assert {r["family"] for r in chat} <= X.R5_ANCHOR_FAMILIES


# =========================================================================== C15: the overlap hook
def test_overlap_hook_records_the_report_sha_in_the_stats_and_fails_on_a_hit(tmp_path):
    w = r5_world(tmp_path)
    man = tmp_path / "eval_manifest.json"
    man.write_text(json.dumps({"sets": {"eval": {"prompts": ["an unrelated eval question about volcanoes"], "threads": ["eval-thread"]}},
                               "shingles": {"n_prompts": 0, "items": []}, "fact_values": ["zzqqxx"]}))
    argv = r5_argv(w, extra=("--overlap-manifest", str(man)))
    assert F.run_main(argv) == 0
    out = w["tmp"] / "out5" / "grpo_prompts.jsonl"
    st = json.loads(out.with_suffix(".stats.json").read_text())
    report = out.with_suffix(".overlap_report.json")
    assert st["overlap"]["checked"] and st["overlap"]["exit"] == 0 and st["overlap"]["result"] == "clean"
    assert st["overlap"]["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert set(st["overlap"]["excluded_sets"]) == {"eval"} and len(st["overlap"]["inputs"]) == 3          # prompts, mm dev, text dev sidecars
    rep = json.loads(report.read_text())
    assert rep["rows"] == st["rows"] + len(dev_rows(w)) + st["mm_dev_rows"]
    # a manifest set that holds a question the export uses -> exit 1, stats say so
    side = read_rows(out.with_suffix(".overlap.jsonl"))
    taken = next(s["users"][-1] for s in side if s["family"] == "compose_r3")
    man.write_text(json.dumps({"sets": {"eval": {"prompts": [taken]}}, "shingles": {"n_prompts": 0, "items": []}, "fact_values": ["zzqqxx"]}))
    assert F.run_main(argv) == 1
    st = json.loads(out.with_suffix(".stats.json").read_text())
    assert st["overlap"]["result"] == "hits" and st["overlap"]["exit"] == 1
    # without the manifest the stats say the overlap proof has not run
    assert F.run_main(r5_argv(w, w["tmp"] / "nm" / "grpo_prompts.jsonl")) == 0
    assert json.loads((w["tmp"] / "nm" / "grpo_prompts.stats.json").read_text())["overlap"] == {"checked": False}


# =========================================================================== verifier fixes: ceilings, ideals, labels, inputs
def ideal_of(pairs, i, rnd=5, **kw):
    """(label, ideal completion) of step i of a scenario, built the way make_item does it for the fake renderer."""
    lab = labs(pairs, rnd, **kw)[i]
    ex, meta = pairs[i]
    lab["forced"] = ex["messages"][-2]["role"] == "tool"
    lab["ideal_completion"] = F.FakeRender.render(None, ex, meta)["completion"] if not ex["messages"][-1].get("tool_calls") else \
        "".join(F.gemma_call(tc["function"]["name"], tc["function"]["arguments"]) for tc in ex["messages"][-1]["tool_calls"]) + "<|tool_response>"
    return lab


def test_instruction_and_session_finals_after_a_read_carry_no_facts_and_score_at_their_ceiling():
    """A final after a read that is an instruction, a session anchor or a recall answer is scored by a constant or by recall refs: it must
    not be a narrate row with extracted facts (the ideal would score tool_set 1 + facts 1 against a ceiling of 1)."""
    reply = "The dentist is at 9 AM with Dr. Maya Chen."
    cases = [
        (scenario("instructions_r4", 1, "what is up tomorrow", [("call", "get_upcoming", {}, "Dentist 9 AM with Dr. Maya Chen"), ("final", reply)]),
         1, "instruction", 1.0),
        (scenario("session_chat_r4", 2, "what is my dog called", [("call", "recall", {"query": "dog"}, "dog: Max"), ("final", "Your dog is Max Bell.")],
                  final_meta={"qkind": "rec_user", "recall_refs": ["Max"], "thread": "t"}), 1, "recall", 2.0),
        (scenario("session_chat_r4", 3, "what is my cat called", [("call", "recall", {"query": "cat"}, "nothing"), ("final", "You never told me, Sam Lee.")],
                  final_meta={"qkind": "rec_none", "recall_absent": "cat name", "thread": "t"}), 1, "recall_none", 1.5),
        (scenario("session_chat_r4", 4, "what is for dinner", [("call", "recall", {"query": "dinner"}, "pasta at 6 PM"), ("final", "Pasta at 6 PM, Maya Chen.")],
                  final_meta={"qkind": "build", "thread": "t"}), 1, "session_anchor", 1.0),
    ]
    for pairs, i, kind, ceiling in cases:
        lab = labs(pairs)[i]
        assert (lab["rl_kind"], lab["ceiling"]) == (kind, ceiling), pairs[i][1]["id"]
        assert lab["narrate"] is False and lab["facts"] == [] and "facts_explicit" not in lab, (kind, lab["narrate"], lab["facts"])
        assert lab["post_read"] is True                                            # the thinking floor still sees a post-read step
        assert X.r5_label_problems(lab) == []
        bad = dict(lab, narrate=True, facts=["9 AM"])
        assert any("narrate/facts" in p for p in X.r5_label_problems(bad)), kind
    # the real ideal of the instruction row scores exactly its ceiling now
    pairs = cases[0][0]
    lab = ideal_of(pairs, 1)
    assert W.score(lab["ideal_completion"], lab, 5).total == lab["ceiling"] == 1.0
    # a narrate row of another family still pays for its facts (and keeps its ceiling of 2.0)
    mail = ideal_of(scenario("mail_r3", 1, "what is in my inbox", [("call", "summarize_emails", {}, "Maya Chen at 12:30 PM"),
                                                                   ("final", "Maya Chen wrote about lunch at 12:30 PM.")]), 1)
    assert mail["rl_kind"] == "narrate" and mail["narrate"] and mail["facts"] and mail["ceiling"] == 2.0
    # meta.facts on those kinds is not scored (the reward reads facts_explicit on decision/select finals only): noted, not carried
    given = labs(scenario("instructions_r4", 2, "what is up", [("call", "get_upcoming", {}, "x"), ("final", reply)], final_meta={"facts": ["9 AM"]}))[1]
    assert given["facts"] == [] and given["facts_ignored"] is True and given["ceiling"] == 1.0 and "facts_explicit" not in given


def test_explicit_meta_facts_on_a_decision_final_are_flagged_and_lift_the_ceiling_to_what_the_reward_pays():
    pairs = scenario("session_recall_r4", 3, "what was it", [("final", "It was at 12:30 PM, near the lake.")], final_meta={"facts": ["12:30 PM"]})
    lab = ideal_of(pairs, 0)
    assert lab["rl_kind"] == "decision" and lab["facts"] == ["12:30 PM"] and lab["facts_explicit"] is True and lab["ceiling"] == 1.5
    assert W.score(lab["ideal_completion"], lab, 5).total == 1.5            # tool_set 1.0 + the explicit-facts credit (reward.R5_EXPLICIT_FACTS)
    assert X.r5_label_problems(lab) == []
    # an explicit list on a clarifying ask earns nothing and is not flagged
    ask = labs(scenario("restraint_r4", 1, "what is the weather", [("final", "Which city?")], final_meta={"clarify": True, "facts": ["city"]}))[0]
    assert ask["ask"] and "facts_explicit" not in ask and ask["ceiling"] == 1.5
    with pytest.raises(KeyError):
        X.ceiling_for("narrate", "final", facts_explicit=True)
    with pytest.raises(KeyError):
        X.ceiling_for("decision", "call", facts_explicit=True)
    assert X.ceiling_for("select", "final", facts_explicit=True) == 1.5


def test_a_clarify_flag_a_chat_or_session_row_cannot_use_is_counted_not_silently_lost(tmp_path):
    lab = labs(scenario("instructions_r4", 1, "send it", [("final", "Which one should get it?")], final_meta={"clarify": True}))[0]
    assert lab["step"] == "final" and not lab["ask"] and lab["clarify_dropped"] is True and lab["rl_kind"] == "instruction"
    assert "clarify_dropped" not in labs(scenario("instructions_r4", 1, "send it", [("final", "Done then.")], final_meta={"clarify": False}))[0]
    assert "clarify_dropped" not in labs(scenario("restraint_r4", 1, "send it", [("final", "Which one?")], final_meta={"clarify": True}))[0]
    w = r5_world(tmp_path)
    pairs = w["pairs"]
    for _, m in pairs:
        if m["family"] == "instructions_r4":
            m["clarify"] = True
    F.write_build(w["build"], pairs, w["valid"])
    assert F.run_main(r5_argv(w)) == 0
    st = json.loads((w["tmp"] / "out5" / "grpo_prompts.stats.json").read_text())["r5"]
    assert st["clarify_dropped_rows"].get("instructions_r4", 0) > 0


def test_narrate_family_pre_call_steps_are_call_rows_and_the_selection_bucket_keeps_selection_families():
    for fam in sorted(X.R5_NARRATE_FAMILIES):
        pairs = scenario(fam, 1, "what is on today", [("call", "get_upcoming", {}, "Dentist 9 AM"), ("final", "The dentist is at 9 AM.")])
        first, final = labs(pairs)
        assert (first["rl_kind"], first["ceiling"]) == ("call", 1.5), fam                 # RL-SCOPE 3.3: narrate pre-call steps are `call`
        assert (final["rl_kind"], final["ceiling"]) == ("narrate", 2.0), fam
    assert X.R5_NARRATE_FAMILIES == {"day_answers_r4", "info_answers_r4", "lookup_answers_r4"}
    sel = labs(scenario("right_source", 1, "what is on today", [("call", "get_upcoming", {}, "Dentist 9 AM"), ("final", "The dentist is at 9 AM.")]))[0]
    assert (sel["rl_kind"], sel["ceiling"]) == ("select", 1.5)
    # round 3 and 4 never see the list
    assert labs(scenario("day_answers_r4", 1, "x y", [("call", "get_upcoming", {}, "x"), ("final", "y z")]), rnd=4)[0].get("rl_kind") is None


def test_an_anchor_needs_a_plain_history_and_the_rl_pool_holds_no_distilled_anchor_rows(tmp_path):
    plain = scenario("chat_voice_r4", 1, "hi there", [("final", "Hello, friend.")], meta={"distilled": True})
    assert labs(plain)[0]["rl_kind"] == "chat_anchor"
    ex, meta = plain[0]
    with_tool = {"messages": ex["messages"][:-1] + [F.call_msg("recall", {}, "c9"), {"role": "tool", "tool_call_id": "c9", "content": "x"},
                                                  ex["messages"][-1]]}
    assert X.anchor_row(meta, False) and X.anchor_row(meta, False, ex["messages"][:-1])
    assert not X.anchor_row(meta, False, with_tool["messages"][:-1])                         # a tool message in the history: not an anchor
    assert X.classify(with_tool, meta, 5, {}, TOOLS)["rl_kind"] != "chat_anchor"
    # a distilled anchor-family row in --build-dir: a clear exit (the RL pool is re-seeded and has none), not a KeyError in the mix
    w = r5_world(tmp_path)
    pairs = w["pairs"] + scenario("chat_voice_r4", 99, "hi there friend", [("final", "Hello, friend.")], meta={"distilled": True})
    F.write_build(w["build"], pairs, w["valid"])
    with pytest.raises(SystemExit, match="distilled anchor-family row"):
        F.run_main(r5_argv(w))
    # an anchor-build row of an anchor family whose history holds a tool message is skipped and counted
    w2 = r5_world(tmp_path / "w2")
    ab = X.load_pairs(w2["anchor"])
    bad = scenario("chat_voice_r4", 998, "zz yy", [("final", "Hello, friend.")], meta={"distilled": True})
    bad[0][0]["messages"] = bad[0][0]["messages"][:-1] + [F.call_msg("recall", {}, "c9"), {"role": "tool", "tool_call_id": "c9", "content": "x"},
                                                          bad[0][0]["messages"][-1]]
    F.write_build(w2["anchor"], ab + bad, {m["id"] for _, m in ab if int(m["id"].split("-")[1]) % 6 == 5})
    assert F.run_main(r5_argv(w2)) == 0
    assert json.loads((w2["tmp"] / "out5" / "grpo_prompts.stats.json").read_text())["r5"]["anchor_skipped"] == {"history holds a tool message": 1}


def test_picker_none_needs_an_explicit_flag(tmp_path):
    w = r5_world(tmp_path)
    argv = [a for a in r5_argv(w) if a != "--allow-no-picker"]
    with pytest.raises(SystemExit, match="--allow-no-picker"):
        F.run_main(argv)
    assert F.run_main(r5_argv(w)) == 0
    assert json.loads((w["tmp"] / "out5" / "grpo_text_dev.stats.json").read_text())["picker"]["given"] is False


def test_meta_recall_is_carried_beside_the_refs_not_as_label_recall():
    lab = labs(scenario("session_chat_r4", 1, "what is my dog", [("final", "Max.")],
                        final_meta={"qkind": "rec_user", "recall_refs": ["Max"], "thread": "t", "recall": "Max the dog"}))[0]
    assert lab["meta_recall"] == ["Max the dog"] and "recall" not in lab                      # label.recall is the round-4 flag reward.py reads
    lab = labs(scenario("session_chat_r4", 1, "what is my dog", [("final", "Max.")],
                        final_meta={"qkind": "rec_user", "recall_refs": ["Max"], "thread": "t", "recall": ["Max", "lake"]}))[0]
    assert lab["meta_recall"] == ["Max", "lake"]
    assert "meta_recall" not in labs(scenario("session_chat_r4", 1, "what is my dog", [("final", "Max.")],
                                              final_meta={"qkind": "build", "thread": "t"}))[0]


def test_call_rows_keep_their_ideal_and_the_leak_check_with_tool_names():
    """Round 5 writes tool_names on call rows (for the namespace strip); the ideal call still scores its ceiling, and a tool name in text
    beside a call is a leak only because the row now knows the names."""
    pairs = scenario("right_source", 1, "what is on today", [("call", "get_upcoming", {"days": 1}, "x"), ("final", "Done.")])
    lab = ideal_of(pairs, 0)
    assert lab["tool_names"] == TOOLS
    assert W.score(lab["ideal_completion"], lab, 5).total == 1.5
    leaked = W.score("Let me use get_upcoming. " + lab["ideal_completion"], lab, 5)
    assert leaked.parts["leak"] < 0
    r4 = labs(pairs, 4)[0]
    assert "tool_names" not in r4 and "skill_names" not in r4                               # round 4 call rows are untouched


def test_ideal_trips_are_counted_dropped_and_fail_the_export_above_the_limit(tmp_path, monkeypatch):
    w = r5_world(tmp_path)
    real = X.ideal_trips
    seen = {"n": 0}

    def one_narrate(lab):
        if lab["rl_kind"] == "narrate" and seen["n"] == 0:
            seen["n"] = 1
            return ["over_mention"]
        return real(lab)
    monkeypatch.setattr(X, "ideal_trips", one_narrate)
    assert F.run_main(r5_argv(w)) == 0                                                         # a single trip is not "more than 1%" of >1 rows
    st = json.loads((w["tmp"] / "out5" / "grpo_prompts.stats.json").read_text())
    assert st["dropped"]["ideal trips over_mention"] == 1
    trip = st["r5"]["ideal_trip"]["narrate"]
    assert trip["tripped"] == 1 and trip["by_term"] == {"over_mention": 1} and trip["rows"] > 20 and st["r5"]["ideal_trip_pct_limit"] == 1.0
    assert st["r5"]["dropped_ideal_trip"] == {"ideal trips over_mention": 1}
    # every kind's counts are recorded, none above the limit here
    assert all(v["tripped"] == 0 for k, v in st["r5"]["ideal_trip"].items() if k != "narrate")
    # a term that more than 1% of a kind's ideals trip stops the export and says which; the counts file is written
    monkeypatch.setattr(X, "ideal_trips", lambda lab: ["nothing_claim"] if lab["rl_kind"] == "narrate" else real(lab))
    with pytest.raises(SystemExit) as e:
        F.run_main(r5_argv(w))
    assert "narrate" in str(e.value) and "nothing_claim" in str(e.value) and "(limit 1%)" in str(e.value)
    counts = json.loads((w["tmp"] / "out5" / "grpo_prompts.ideal_trip.json").read_text())
    assert counts["narrate"]["by_term"]["nothing_claim"] == counts["narrate"]["tripped"] == counts["narrate"]["rows"]
    # the limit is a flag
    assert F.run_main(r5_argv(w, extra=("--ideal-trip-pct", "100"))) == 0


def test_ideal_trips_scores_the_row_ideal_with_the_round_5_reward():
    ok = ideal_of(scenario("mail_r3", 1, "what is in my inbox", [("call", "summarize_emails", {}, "Maya Chen at 12:30 PM"),
                                                                 ("final", "Maya Chen wrote about lunch at 12:30 PM.")]), 1)
    assert X.ideal_trips(ok) == []
    # an ideal that outscores its ceiling is a mislabel; one that trips a penalising term is named
    assert X.ideal_trips(dict(ok, ceiling=1.0)) == ["above_ceiling"]
    assert X.ideal_trips(dict(ok, ideal_completion="\n<channel|>Nothing scheduled, you are free<turn|>", ceiling=2.0))[:1] != []
    assert X.ideal_trip_problems({"narrate": {"rows": 100, "tripped": 2, "by_term": Counter({"ungrounded": 2})}}) \
        == ["narrate: 2 of 100 ideals (2.0%) trip ungrounded (limit 1%)"]
    assert X.ideal_trip_problems({"narrate": {"rows": 100, "tripped": 1, "by_term": Counter({"ungrounded": 1})}}) == []
    assert X.ideal_trip_problems({"recall": {"rows": 54, "tripped": 1, "by_term": Counter({"leak": 1})}}) == []      # one row is not a rate


def test_narrate_facts_are_only_those_the_ideal_states_outside_a_negation():
    none = ideal_of(scenario("act_dont_promise", 1, "anything from mom", [("call", "summarize_messages", {}, "Theo Park: hi"),
                                                                         ("final", "Nothing from Mom in your recent messages.")]), 1)
    assert none["facts"] == [] and none["rl_kind"] == "select" and none["ceiling"] == 1.0         # a negated fact earns nothing: not a narrate row
    assert W.score(none["ideal_completion"], none, 5).total == 1.0
    part = ideal_of(scenario("day_answers_r4", 1, "what is on", [("call", "get_upcoming", {}, "Dentist 3:30 PM"),
                                                                  ("final", "I can only see the start, 3:30 PM. There's no end time on Dentist appointment.")]), 1)
    assert part["facts"] == ["3:30 PM"] and part["rl_kind"] == "narrate"
    assert W.score(part["ideal_completion"], part, 5).total == part["ceiling"] == 2.0
    # an explicit builder list is the builder's word
    given = labs(scenario("mail_r3", 2, "inbox", [("call", "summarize_emails", {}, "x"), ("final", "Nothing from Maya.")], final_meta={"facts": ["Maya"]}))[1]
    assert given["facts"] == ["Maya"]


def test_tool_results_and_the_world_names_ride_with_a_final_for_the_reward():
    digest = "**Theo Park**\n- Theo Park: “Still on for coffee?” [received 2027-04-06]\n\n**Ben Ortiz**\n- Ben Ortiz: “Bring water” [received 2027-04-06]"
    pairs = scenario("mail_r3", 1, "how are my messages", [("call", "summarize_messages", {}, digest),
                                                          ("final", "Theo Park asks if you are still on for coffee.")])
    other = scenario("mail_r3", 2, "who is on", [("call", "lookup_contact", {"name": "Dad"}, "Dad: +1 555 0100\nCarmen Rivera: +1 555 0101"),
                                                 ("final", "Dad is +1 555 0100.")])
    names = X.world_names(pairs + other)
    assert names == ["Ben Ortiz", "Carmen Rivera", "Theo Park"]     # headings, sender lines and contact lines; "Dad" (one word) never enters
    call, final = labs(pairs, names=names)
    assert final["tool_results"] == [digest] and "tool_results" not in call
    assert final["names"] == ["Carmen Rivera"]                        # the names of this row's ground are left out: only a stranger is flaggable
    lab = labs(other, names=["Ben Ortiz", "Dana Whitfield"])[1]
    assert lab["names"] == ["Ben Ortiz", "Dana Whitfield"] and lab["tool_results"] == ["Dad: +1 555 0100\nCarmen Rivera: +1 555 0101"]
    assert "names" not in labs(other)[1]                              # no lexicon given: no key
    assert "names" not in labs(other, 4)[1] and "tool_results" not in labs(other, 4)[1]       # round 4 is untouched
    # a chat row with no ground and no tool result carries neither
    chat = labs(scenario("chat_voice_r4", 1, "hi hi", [("final", "Hello.")]), names=["Ben Ortiz"])[0]
    assert "names" not in chat and "tool_results" not in chat
    assert X.world_names([]) == []
