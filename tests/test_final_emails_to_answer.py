"""Ask "Any emails I need to answer?" reads the Brief's needs-reply items (the day
plan), the same source as the Brief's reply cards. Synthetic text only."""
import pytest

from service import model_only as mo


@pytest.mark.parametrize("prompt", [
    "Any emails I need to answer?",
    "any emails i need to reply to",
    "Which emails need a reply?",
    "Do I owe anyone a reply?",
    "Are there any emails that need a response?",
    "Who is waiting on a reply from me?",
    "What emails do I still need to respond to?",
])
def test_owed_reply_questions(prompt):
    assert mo.emails_to_answer_question(prompt)


@pytest.mark.parametrize("prompt", [
    "Reply to Dana's last email and say yes",
    "Draft a reply to the email from Sam",
    "Any emails from Sam?",
    "What's the weather?",
    "",
])
def test_other_prompts_do_not_match(prompt):
    assert not mo.emails_to_answer_question(prompt)


def test_head_start_leads_with_day_plan():
    from service import main
    from service.tools.registry import REGISTRY
    preload, stubs = main._ask_tool_rank("Any emails I need to answer?", "")
    if mo.DAY_PLAN_TOOL in REGISTRY:
        assert (preload + stubs)[0] == mo.DAY_PLAN_TOOL


def test_hint_names_the_tool():
    assert "get_my_day_plan" in mo.EMAILS_TO_ANSWER_HINT
