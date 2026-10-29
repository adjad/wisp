"""Tool wording the 2026-10-29 Ask run showed the model misreading.

"show me what's pending" went to list_scheduled_sends (the send queue), and
"what are my open tasks" / "what are my school tasks" never reached the
reminders, ending in "I don't have that". The model picks tools by their
descriptions, so to-dos and tasks are named where they live.
"""
from service.tools import short_descriptions
from service.tools.registry import REGISTRY


def test_reminders_own_todos_tasks_and_pending():
    tool = REGISTRY["search_reminders"]
    for word in ("to-dos", "tasks", "pending", "overdue"):
        assert word in tool.description
    assert "query" not in tool.parameters.get("required", [])
    assert "empty query lists all" in short_descriptions.SHORT["search_reminders"]


def test_the_send_queue_points_to_dos_elsewhere():
    tool = REGISTRY["list_scheduled_sends"]
    assert tool.description.startswith("Only emails/texts the user scheduled")
    assert "search_reminders" in tool.description
    assert "not to-dos" in short_descriptions.SHORT["list_scheduled_sends"]
