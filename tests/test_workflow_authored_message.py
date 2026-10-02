"""Authored messages that mention a topic are not data deliveries.

Regression: "email Sam asking to move our meeting" compiled into a summary
delivery of the user's calendar and was mailed under the subject "Wisp report".
"""
import pytest

from service.workflows.compiler import compile_new

AUTHORED = [
    "send jane.doe@example.com an email asking to move our meeting to Friday",
    "email Sam asking if he can reschedule our meeting",
    "text Mom asking what time dinner is",
    "email my professor asking for an extension on the essay",
    "send Alex a message asking about the calendar invite",
    "text Dad telling him my schedule changed",
    "email jane@example.com asking her to send the meeting notes",
    "message Priya letting her know I'm running late to the meeting",
    "email Finance requesting my email receipts",
    "email boss requesting time off next week",
    "email Sam thanking him for the meeting",
    "text Mom to ask about the reminders she set",
    "Email Sam and ask him about the calendar invite",
]

DELIVERIES = [
    "summarize my emails and text it to Mom",
    "email me my calendar for tomorrow",
    "send my schedule to Sam",
    "text Mom my meetings this week",
    "email Sam a summary of my unread emails",
    "send the daily brief to Dad",
    "forward my reminders to Alex by email",
    "email my boss my calendar for today",
    "send Sam my calendar asking if he is free",
    "text Mom my schedule and let her know I'm busy",
]


@pytest.mark.parametrize("prompt", AUTHORED)
def test_authored_message_is_not_compiled_into_a_delivery(prompt):
    assert compile_new(prompt) is None


@pytest.mark.parametrize("prompt", DELIVERIES)
def test_real_data_deliveries_still_compile(prompt):
    plan = compile_new(prompt)
    assert plan is not None and plan.sources


@pytest.mark.parametrize("prompt", AUTHORED)
def test_router_does_not_force_source_reads_for_authored_messages(prompt):
    from service.router.router import _source_outbound_subset
    assert _source_outbound_subset(prompt) is None


@pytest.mark.parametrize("prompt", [
    "email me my calendar for tomorrow",
    "send my schedule to Sam",
])
def test_router_still_grounds_real_deliveries(prompt):
    from service.router.router import _source_outbound_subset
    assert _source_outbound_subset(prompt) is not None
