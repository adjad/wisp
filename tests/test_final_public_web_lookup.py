"""Public questions may search the web in a chat that holds private data, using only the
user's own typed words; a denied tool's line is never the answer."""
from pathlib import Path

from service.model_only import public_fact_query_problem as problem

LOOP = (Path(__file__).resolve().parents[1] / "service" / "agent" / "loop.py").read_text()


def test_user_words_and_weekday_pass():
    assert problem("Costco hours", "What are Costco's hours?") is None
    assert problem("Costco Friday hours", "What are Costco's hours?") is None
    assert problem("game results Sunday", "Who won Sunday's game?") is None


def test_follow_up_uses_earlier_typed_question():
    both = "What are Costco's hours? And what about Monday?"
    assert problem("Costco Monday hours", both) is None


def test_term_not_typed_by_user_is_still_blocked():
    # "jordan" came from a tool result (an email), not from the user's words.
    msg = problem("Costco hours Jordan", "What are Costco's hours?")
    assert msg and "jordan" in msg
    assert problem("Costco hours 415 555 2019", "What are Costco's hours?")


def test_dates_the_user_did_not_type_are_still_refused():
    assert problem("game results Sunday November 8 2026", "Who won Sunday's game?")


def test_denied_line_never_final_and_fixable_query_not_latched():
    assert 'if "BLOCKED by safety policy" in (text or "")' in LOOP
    assert "Ask it in a new chat and I'll search." in LOOP
    assert 'startswith(\n                            "Private data is in context, so a public lookup may only search"' in LOOP
