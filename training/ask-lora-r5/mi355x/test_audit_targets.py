"""Offline tests for audit_targets.py (no tokenizer, no torch). Run: .venv/bin/python -m pytest test_audit_targets.py
or directly: .venv/bin/python test_audit_targets.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_targets as A  # noqa: E402


def call(name, **args):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": args}}


U = {"role": "user", "content": "hi"}
TOOL = {"role": "tool", "tool_call_id": "c", "content": "result"}


def ex(*msgs):
    return {"messages": list(msgs), "tools": []}


class StepTypes(unittest.TestCase):
    def test_all_types(self):
        asst = lambda *c: {"role": "assistant", "content": "", "tool_calls": list(c)}  # noqa: E731
        fin = {"role": "assistant", "content": "ok"}
        self.assertEqual(A.step_type(ex(U, fin), {}), "conversation_final")
        self.assertEqual(A.step_type(ex(U, asst(call("a")), TOOL, fin), {}), "final_after_tool")
        self.assertEqual(A.step_type(ex(U, asst(call("a"))), {}), "call_single")
        self.assertEqual(A.step_type(ex(U, asst(call("a"), call("b"))), {}), "call_parallel")
        self.assertEqual(A.step_type(ex(U, asst(call("a")), TOOL, asst(call("b"))), {}), "chain_call_single")
        self.assertEqual(A.step_type(ex(U, asst(call("a")), TOOL, asst(call("b"), call("c"))), {}), "chain_call_parallel")
        self.assertEqual(A.step_type(ex(U, asst(call("get_tool_schemas"))), {"target_kind": "load"}), "load")
        self.assertEqual(A.step_type(ex(U, asst(call("a")), TOOL, asst(call("get_tool_schemas"))), {}), "load")

    def test_thought_class(self):
        self.assertEqual([A.thought_class(x) for x in ("base", "scripted", "none")], ["base_written", "scripted", "absent"])


class Closure(unittest.TestCase):
    F = "<|channel>thought\n"

    def test_forced_prompt_closes_at_once(self):
        self.assertTrue(A.closes_immediately("...<|turn>model\n" + self.F, "\n<channel|>It is done.<turn|>"))
        self.assertTrue(A.closes_immediately("x" + self.F, "<channel|>It is done.<turn|>"))

    def test_forced_prompt_with_a_thought(self):
        self.assertFalse(A.closes_immediately("x" + self.F, "The user asked.\n<channel|>Done.<turn|>"))

    def test_first_step_opens_and_closes(self):
        self.assertTrue(A.closes_immediately("x<|turn>model\n", self.F + "\n<channel|><|tool_call>call:a{}<tool_call|><|tool_response>"))
        self.assertFalse(A.closes_immediately("x<|turn>model\n", self.F + "plan\n<channel|><|tool_call>call:a{}<tool_call|><|tool_response>"))

    def test_no_thought_at_all(self):
        self.assertFalse(A.closes_immediately("x<|turn>model\n", "<|tool_call>call:a{}<tool_call|><|tool_response>"))

    def test_follows_tool_result(self):
        self.assertTrue(A.follows_tool_result(ex(U, TOOL, {"role": "assistant", "content": "a"})))
        self.assertFalse(A.follows_tool_result(ex(U, {"role": "assistant", "content": "a"})))


class Dates(unittest.TestCase):
    def test_terms(self):
        t = A.relative_terms("Next month, today and on Friday; this_week too")
        self.assertEqual(t, {"month": {"next"}, "day": {"today"}, "weekday": {"friday"}, "week": {"this"}})

    def test_known_next_month_vs_this_month(self):
        calls = [("summarize_emails", '{"period": "this month"}'), ("get_upcoming", '{"period": "next month"}')]
        hits = A.date_conflicts("Pull the email digest and look up the calendar (next month).", calls, "")
        self.assertEqual([h["source"] for h in hits], ["call_args:summarize_emails"])
        self.assertEqual(hits[0]["thought_words"], ["next"])
        self.assertEqual(hits[0]["evidence_words"], ["this"])

    def test_no_conflict_when_words_overlap_or_group_absent(self):
        self.assertEqual(A.date_conflicts("today or tomorrow?", [("get_upcoming", '{"period": "tomorrow"}')], ""), [])
        self.assertEqual(A.date_conflicts("this month", [("a", '{"period": "this_month"}')], "No events this month."), [])
        self.assertEqual(A.date_conflicts("no dates here", [("a", '{"period": "this month"}')], "Friday"), [])

    def test_answer_conflict(self):
        hits = A.date_conflicts("The meeting is on Thursday.", [], "Your meeting is on Wednesday.")
        self.assertEqual((hits[0]["group"], hits[0]["source"]), ("weekday", "answer"))

    def test_call_arguments_json_string(self):
        calls = A._call_texts([{"function": {"name": "n", "arguments": '{"period": "tomorrow"}'}}])
        self.assertEqual(calls, [("n", '{"period": "tomorrow"}')])
        self.assertEqual(len(A.date_conflicts("What is on today", calls, "")), 1)

    def test_current_turn_calls(self):
        a1 = {"role": "assistant", "content": "", "tool_calls": [call("x")]}
        a0 = {"role": "assistant", "content": "", "tool_calls": [call("old")]}
        msgs = [U, a0, TOOL, U, a1, TOOL, {"role": "assistant", "content": "r"}]
        self.assertEqual([c["function"]["name"] for c in A.current_turn_calls(msgs)], ["x"])


class Markup(unittest.TestCase):
    def test_mark_and_elide(self):
        self.assertEqual(A.mark_span("P", "C"), "P⟦C⟧")
        text, cut = A.elide_tools("a<|tool>declaration:x{...}<tool|>b")
        self.assertEqual(text, "a<|tool>[…tool declaration elided…]<tool|>b")
        self.assertGreater(cut, 0)


class TrainerParity(unittest.TestCase):
    """The imported trainer pieces behave as the audit assumes (labels are -100 over the prompt, all completion tokens labelled)."""

    def test_sequence_labels_and_render_constants(self):
        render, train, stats = A.trainer_modules()
        ids, labels, _ = train.sequence([1, 2, 3], [4, 5])
        self.assertEqual(labels, [-100, -100, -100, 4, 5])
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(render.FORCED, "<|channel>thought\n")
        self.assertEqual(render.EMPTY_THOUGHT, "\n<channel|>")
        # row_shape (trainer) agrees with closes_immediately on forced / unforced rows
        forced = {"prompt": "x" + render.FORCED, "completion": render.EMPTY_THOUGHT + "ok<turn|>"}
        self.assertEqual(render.row_shape(forced), "forced_empty")
        self.assertTrue(A.closes_immediately(forced["prompt"], forced["completion"], render.FORCED))
        think = {"prompt": "x" + render.FORCED, "completion": "plan\n<channel|>ok<turn|>"}
        self.assertEqual(render.row_shape(think), "forced_think")
        self.assertFalse(A.closes_immediately(think["prompt"], think["completion"], render.FORCED))
        # r5_stats.is_forced mirrors the render's forced prompt
        e = ex(U, {"role": "assistant", "content": "", "tool_calls": [call("a")]}, TOOL, {"role": "assistant", "content": "r"})
        self.assertTrue(stats.is_forced(e, {"chat_template_kwargs": {"enable_thinking": True}}))
        self.assertFalse(stats.is_forced(e, {"chat_template_kwargs": {"enable_thinking": False}}))


if __name__ == "__main__":
    unittest.main()
