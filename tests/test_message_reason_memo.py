"""summary_message_rows reuses the previous snapshot's classifications.

/assistant/today reads the important texts twice per call. Classifying every
cached text each time held the GIL for about a second per read on a real
cache, which stalled the event loop long enough for the app's per-request
identity check to time out and the Brief to show "Wisp's service isn't
running yet." Synthetic only: no server or native Messages access.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from service.tools import imessage_tools as M


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from service.memory import identity
    from service.assistant import sync_status
    monkeypatch.setattr(identity, "user_name", lambda: "Test User")
    monkeypatch.setattr(M, "_contacts", {})
    monkeypatch.setattr(M, "_lines", "")
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    monkeypatch.setattr(M, "_reason_memo", (None, {}))
    monkeypatch.setattr(sync_status, "ensure_sources", AsyncMock())


LINES = [
    'V2 | 1 | R | chat:10 | Alex | Alex: A routine read update.',
    'V2 | 2 | R | chat:11 | Casey | Casey: Can you send the report by Friday?',
    'V2 | 3 | R | chat:12 | Family | Mom: The appointment was moved to tomorrow.',
]


def counting(monkeypatch):
    seen: list[str] = []
    real = M.important_message_reason

    def classify(text):
        seen.append(text)
        return real(text)

    monkeypatch.setattr(M, "important_message_reason", classify)
    return seen


def test_rows_match_an_unmemoized_read(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join(LINES))
    first = M.summary_message_rows()
    monkeypatch.setattr(M, "_reason_memo", (None, {}))
    assert M.summary_message_rows() == first
    bodies = [text for _ts, _context, text in first]
    assert any("send the report" in text for text in bodies)
    assert any("appointment was moved" in text for text in bodies)
    assert not any("routine read" in text for text in bodies)


def test_a_repeat_read_classifies_nothing_again(monkeypatch):
    seen = counting(monkeypatch)
    monkeypatch.setattr(M, "_lines", "\n".join(LINES))
    first = M.summary_message_rows()
    classified = len(seen)
    assert classified >= len(LINES)
    assert M.summary_message_rows() == first
    assert M.summary_message_rows(require_read_state=True) == first
    assert len(seen) == classified, "unchanged texts must not be classified again"


def test_a_new_sync_classifies_only_its_new_texts(monkeypatch):
    seen = counting(monkeypatch)
    monkeypatch.setattr(M, "_lines", "\n".join(LINES))
    M.summary_message_rows()
    seen.clear()
    added = 'V2 | 4 | R | chat:13 | Sam | Sam: Please sign the lease today.'
    monkeypatch.setattr(M, "_lines", "\n".join(LINES + [added]))
    rows = M.summary_message_rows()
    assert any("sign the lease" in text for _ts, _context, text in rows)
    assert [text for text in seen if "Alex" in text or "Casey" in text or "Mom" in text] == []
    assert any("sign the lease" in text for text in seen)


def test_the_memo_keeps_only_the_latest_snapshot(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join(LINES))
    M.summary_message_rows()
    monkeypatch.setattr(M, "_lines", LINES[1])
    M.summary_message_rows()
    _owner, memo = M._reason_memo
    assert not any("routine read" in text for text in memo), "a text gone from the cache is forgotten"
    assert any("send the report" in text for text in memo)


def test_a_replaced_classifier_is_never_answered_from_the_old_memo(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join(LINES))
    assert M.summary_message_rows()
    monkeypatch.setattr(M, "important_message_reason", lambda text: None)
    assert M.summary_message_rows() == []
