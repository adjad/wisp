"""The notch Brief parses the composed Daily Summary (app/Sources/WispApp/BriefSummary.swift).

It finds sections by their headers and stops at the closing offer, so those
strings are a contract: if one changes here, the Brief's cut-down summary or
its MAIL/TEXTS details would quietly change with it. Source text only; nothing
is composed, synced or sent.
"""
import inspect

from service.assistant import brief
from service.memory import profile


def test_closing_line_matches_the_brief_parser():
    assert brief._CLOSING_LINE.startswith("Let me know if you'd like me to dig")


def test_section_headers_match_the_brief_parser():
    source = inspect.getsource(brief)
    for header in ('"**🌱 How Wisp works**', '"**📅 Today**"', '"**✅ Reminders due today**', '**📧 Inbox',
                   '"**💬 Messages**'):
        assert header in source, header
    assert '"**🧭 Keeping in mind**' in inspect.getsource(profile.keeping_in_mind)


def test_render_brief_order_keeps_messages_last_before_the_closing_line():
    render = inspect.getsource(brief._render_brief)
    order = [render.index(name) for name in ("_greeting_line", "_first_day_section", "_keeping_in_mind",
                                             "_schedule_section",
                                             "_email_section", "Messages", "_CLOSING_LINE")]
    assert order == sorted(order)
