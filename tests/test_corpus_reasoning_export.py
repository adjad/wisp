"""Corpus exports carry each turn's reasoning (synthetic lines only; nothing is read from ~/.moe)."""
import csv
import io

from service import corpus


def _bounded_line():
    record = corpus.CaseRecord({"id": "LIVE-1", "category": "messages",
                                "conversation": [{"role": "user", "content": "What did Mom say?"}]}, "run-x")
    for event in ({"type": "reasoning", "text": "Step 1:\nRead Mom's thread first."},
                  {"type": "reasoning", "text": "  "},
                  {"type": "text", "text": "Mom asked about dinner."},
                  {"type": "reasoning", "text": "Step 2:\nAnswer from it."},
                  {"type": "done"}):
        record.observe(event)
    record.finish()
    return record.line


def _prompts_line():
    """A prompts-run line as saved before lines had a `reasoning` field: reasoning lives in turn events."""
    turn = lambda prompt, answer, events: {"prompt": prompt, "answer": answer, "events": events}
    return {"run_id": "prompts-x", "case_id": "P-3", "category": "1. Reading", "status": "done",
            "prompt": "Who is Ana? / And when did we last talk?", "answer": "Last week.", "tools": [],
            "turns": [turn("Who is Ana?", "A friend.", [{"type": "routed"}, {"type": "reasoning", "text": "Look her up."}]),
                      turn("And when did we last talk?", "Last week.", [{"type": "tool_call"}])]}


def test_the_recorder_keeps_every_nonempty_reasoning_event_in_order():
    line = _bounded_line()
    assert line["reasoning"] == "Step 1:\nRead Mom's thread first.\n\nStep 2:\nAnswer from it."
    assert line["answer"] == "Mom asked about dinner."


def test_markdown_shows_reasoning_before_each_answer():
    markdown = corpus.export_markdown([_bounded_line()])
    assert "**Reasoning:**\n\n> Step 1:\n> Read Mom's thread first.\n>\n> Step 2:\n> Answer from it.\n\n**Answer:**" in markdown
    multi = corpus.export_markdown([_prompts_line()])
    assert "**Turn 1:** Who is Ana?\n\n**Reasoning:**\n\n> Look her up.\n\n**Answer:**" in multi
    assert "**Turn 2:** And when did we last talk?\n\n**Reasoning:**\n\n> (none)\n\n**Answer:**" in multi


def test_csv_has_a_reasoning_column_next_to_the_answer():
    rows = list(csv.reader(io.StringIO(corpus.export_csv([_bounded_line(), _prompts_line()]))))
    assert rows[0] == corpus.CSV_COLUMNS and corpus.CSV_COLUMNS.index("reasoning") == corpus.CSV_COLUMNS.index("answer") + 1
    column = {name: i for i, name in enumerate(rows[0])}
    assert rows[1][column["reasoning"]] == "Step 1:\nRead Mom's thread first.\n\nStep 2:\nAnswer from it."
    assert rows[2][column["reasoning"]] == "Turn 1: Look her up.\n\nTurn 2: "


def test_an_old_line_without_reasoning_still_exports():
    line = _bounded_line()
    del line["reasoning"]
    assert "> (none)" in corpus.export_markdown([line])
    rows = list(csv.reader(io.StringIO(corpus.export_csv([line]))))
    assert rows[1][rows[0].index("reasoning")] == ""
