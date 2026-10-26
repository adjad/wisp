""""Any emails I need to reply to?" is a question, so it routes as a read.

Before rc1 it reached the email read+write route, which offers send_email and
reply_to_email for a question that asked for nothing to be sent. These
phrasings now join "Which emails should I reply to?" on the source-grounded
attention read (collect_task_evidence only). Anything that adds a filter the
read cannot honor (a sender) or an action (draft, reply) keeps its old owner.
Routing only; nothing here executes a tool.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_scratch = tempfile.TemporaryDirectory()
os.environ["WISP_HOME"] = _scratch.name

import pytest  # noqa: E402

import service.tools  # noqa: E402,F401
from service.router.router import route  # noqa: E402
from service.workflows.workflow20 import attention_read  # noqa: E402

OUTBOUND = {"send_email", "send_message", "reply_to_email", "forward_email",
            "draft_email", "draft_message"}

READS = {
    "Any emails I need to reply to?": ("email",),
    "any emails i need to respond to": ("email",),
    "Do I have any emails I should reply to?": ("email",),
    "Are there any emails or texts I should respond to?": ("email", "messages"),
    "Any texts I need to respond to?": ("messages",),
    "Do I have any messages that need a reply?": ("messages",),
    "any mail that needs a response": ("email",),
    "Any emails waiting for my reply?": ("email",),
    "Are there any emails still waiting on a response?": ("email",),
    "Do I have any texts I have to get back to?": ("messages",),
    "Anything I need to reply to?": ("email", "messages"),
    "Is there anything I should respond to?": ("email", "messages"),
    # Already covered before rc1; must keep working.
    "Which emails should I reply to first?": ("email",),
    "What should I reply to first?": ("email", "messages"),
}


@pytest.mark.parametrize("prompt,sources", sorted(READS.items()))
def test_reply_needed_question_is_a_read(prompt, sources):
    read = attention_read(prompt)
    assert read is not None and read.sources == sources
    decision = asyncio.run(route(prompt))
    assert decision.reason == "source-grounded personal priorities"
    assert set(decision.tool_subset) == {"collect_task_evidence"}
    assert not (set(decision.tool_subset) & OUTBOUND)
    assert [name for name, _ in decision.direct_calls] == ["collect_task_evidence"]


@pytest.mark.parametrize("prompt", [
    "Any emails from Sam I need to reply to?",            # sender filter the read can't honor
    "Any emails I need to reply to? Draft replies for them.",
    "Reply to any emails that need a reply",
    "Any calendar events I need to reply to?",             # not a communication source
])
def test_actions_and_filters_keep_their_owner(prompt):
    assert attention_read(prompt) is None
