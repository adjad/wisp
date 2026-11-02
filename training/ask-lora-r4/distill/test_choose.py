#!/usr/bin/env python3
"""Unit checks for choose.py's reject rules. Run: python3 test_choose.py  (or pytest)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import choose  # noqa: E402

SYS = ("You are Wisp.\nA one-liner and a part-time job are fine words.\n"
       "Installed skills — procedures the user has set up. Call use_skill(name) to load one:\n"
       "- idea-refine: Refines a rough idea.\n- interview-me: Clarifies intent.\n- mac-controls: Mac volume.\n")
TOOLS = [{"type": "function", "function": {"name": n}} for n in
         ("daily_brief", "search_email", "recall", "calculate", "use_skill")]


def row(**meta):
    return {"meta": {"id": "x-1", "family": "chat_voice_r4", **meta},
            "messages": [{"role": "system", "content": SYS}, {"role": "system", "content": "b"},
                         {"role": "system", "content": "c"}, {"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "scripted"}], "tools": TOOLS}


def why(content, **kw):
    rec = {"id": "x-1", "k": 0, "content": content, "tool_calls": [], "finish_reason": "stop", **kw}
    return choose.reject_reasons(rec, row(**({"recall": kw.pop("recall")} if "recall" in kw else {})))


def check(content, expect, **kw):
    got = why(content, **kw)
    assert expect in got, (content, expect, got)


def ok(content):
    assert why(content) == [], (content, why(content))


def test_passes_warm_replies():
    ok("Aw, Sam, that sounds like a lovely evening! 😊 Want me to remember it?")
    ok("Haha, Sam, seven half-read books and an eighth? I recall you said you love a fresh start 📚")
    ok("Sure! Let me calculate: it's about 12 minutes. I'd say a one-liner and a part-time plan is plenty.")
    ok("Here's a thought: forget the deadline for a bit and take a breath.")
    ok("Okay Sam! I can't wait to hear how it goes. What are you cooking?")


def test_basic_rules():
    assert choose.reject_reasons({"error": "boom"}, row()) == ["error"]
    check("hi", "no_choices", no_choices=True)
    check("hi", "tool_call", tool_calls=[{"id": "1"}])
    check("   \n", "empty")
    check("a long reply that got cut", "truncated", finish_reason="length")


def test_stalls():
    for s in ["I couldn't find anything about that.", "I couldn’t find it", "I could not find your note",
              "I can't look up your calendar.", "I can’t look up that", "I cannot look up that", "I can not look up that",
              "I don't have access to your mail.", "I do not have access to that.", "I’m unable to access your files.",
              "I'm unable to access that", "I wasn't able to find it.", "I wasn’t able to find a thing",
              "Sorry, I couldn't find any web results for that.", "I didn't send anything.",
              "I haven't checked a current personal source."]:
        check(s, "stall")


def test_leaks():
    check("I used daily_brief to check.", "leak")
    check("Calling `search_email` now", "leak")
    check("I'll use the `calculate` tool", "leak")
    check("recall(\"x\")", "leak")
    check("Loading the idea-refine skill!", "leak")
    check("Try interview-me or mac-controls.", "leak")
    check("This is UNTRUSTED data", "leak")
    check('{"a": 1}', "leak")
    check("Here:\n```json\n{}\n```", "leak")
    check("Result:\n[1, 2, 3]\nDone", "leak")
    check("then run get_weather(city) for you", "leak")


def test_no_false_leaks():
    ok("Idea refine sounds fun; interview me is a game. A one-liner, a part-time gig.")
    ok("I jotted [a note] for you and {curly} bits are fine.")


def test_recall():
    r = row(recall=["Friday 3 pm", "Dr. Lee"])
    rec = {"id": "x-1", "k": 0, "tool_calls": [], "finish_reason": "stop"}
    assert choose.reject_reasons({**rec, "content": "You see dr lee on friday, 3 pm."}, r) == []
    assert "recall" in choose.reject_reasons({**rec, "content": "You see Dr. Lee soon."}, r)
    assert "recall" in choose.reject_reasons({**rec, "content": "Friday 3 pm works."}, row(recall="Dr. Lee"))
    assert choose.reject_reasons({**rec, "content": "anything"}, row()) == []


def test_shape_helpers():
    assert choose.ends_question("Which one? 😊") and not choose.ends_question("Sure! 😊 Fine.")
    assert choose.EMOJI.search("hi 😂") and choose.EMOJI.search("ok ❤️") and not choose.EMOJI.search("plain, text.")
    assert choose.sentences("One. Two! Three? 😊") == 3
    assert choose.skill_names(row()) == ["idea-refine", "interview-me", "mac-controls"]


def test_end_to_end():
    r1 = row()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "partial.jsonl")
        raw = os.path.join(d, "raw.jsonl")
        open(p, "w").write(json.dumps(r1) + "\n")
        h = choose.request_hash(r1["messages"][:-1], r1["tools"])
        recs = [{"id": "x-1", "family": "chat_voice_r4", "k": 0, "request_hash": h, "content": "I couldn't find it",
                 "reasoning": " t ", "tool_calls": [], "finish_reason": "stop", "usage": {"completion_tokens": 9}},
                {"id": "x-1", "family": "chat_voice_r4", "k": 1, "request_hash": h, "content": "Hi Sam! 😊",
                 "reasoning": " two words ", "tool_calls": [], "finish_reason": "stop", "usage": {"completion_tokens": 7}}]
        open(raw, "w").write("\n".join(json.dumps(x) for x in recs) + "\n{cut")
        rows, chosen, stats = choose.run(p, [raw])
        assert chosen["x-1"]["k"] == 1 and chosen["x-1"]["reasoning"] == "two words"
        g = stats["overall"]
        assert g["first_sample_pass_rate"] == 0.0 and g["rejects_by_reason"] == {"stall": 1}
        assert g["chosen_shape"]["emoji_share"] == 1.0 and g["chosen_shape"]["name_share"] == 1.0
        _, chosen, _ = choose.run(p, [raw], k=1)
        assert not chosen
        recs[1]["request_hash"] = "deadbeef"
        open(raw, "w").write("\n".join(json.dumps(x) for x in recs) + "\n")
        assert not choose.run(p, [raw])[1] and choose.run(p, [raw], hash_check=False)[1]



def test_session_refs():
    hist = {"meta": {"id": "session_chat_r4-001-s0", "family": "session_chat_r4"},
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": "ivy turns 7, party at cedar park"},
                         {"role": "assistant", "content": "A fossil dig suits a dinosaur fan."},
                         {"role": "user", "content": "what color did i say?"}, {"role": "assistant", "content": "s"}],
            "tools": TOOLS}
    rec = {"id": hist["meta"]["id"], "k": 0, "tool_calls": [], "finish_reason": "stop"}
    rows = [json.loads(json.dumps(hist)) for _ in range(2)]
    rows[1]["meta"]["id"] = "session_chat_r4-002-s0"
    choose.apply_refs(rows, {"session_chat_r4-001-s0": {"kind": "rec_none", "ref": [], "absent": "color", "ok": []},
                             "session_chat_r4-002-s0": {"kind": "rec_asst", "ref": ["fossil dig"], "absent": None,
                                                        "ok": []}})
    none, recall = rows
    assert choose.reject_reasons({**rec, "content": "You didn't mention a color for Ivy, Sam. Ask her! 😊"}, none) == []
    assert "invented" in choose.reject_reasons({**rec, "content": "You said Teal, for her 8th."}, none)
    assert choose.reject_reasons({**rec, "content": "It was a Fossil-dig party!"}, recall) == []
    assert "recall" in choose.reject_reasons({**rec, "content": "A dinosaur party."}, recall)

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
