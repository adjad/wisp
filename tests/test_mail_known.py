"""mail_known: a sender is known only from the person's own Contacts or earlier Sent mail."""
import asyncio

from service.proactive import mail_known as K

T = 1_700_000_000.0
MINE = {"me@school.edu"}
FRIEND = "maya@friend.com"


def judge(address=FRIEND, *, rows=(), fresh=True, contacts=frozenset(), mine=MINE, before=T):
    return K.known(address, before_ts=before, sent_rows=None if rows is None else list(rows),
                   sent_fresh=fresh, contact_addresses=contacts, mine=mine)


def sent(to, ts=T - 3600):
    return {"ts": ts, "to": to, "subject": "Hi"}


def test_a_saved_contact_is_known():
    assert judge(contacts={FRIEND}) == (True, "contact")
    assert judge(contacts=lambda a: a == FRIEND) == (True, "contact")


def test_sent_mail_before_the_message_proves_a_correspondent():
    assert judge(rows=[sent(f"Maya <{FRIEND}>")]) == (True, "sent")       # Gmail: raw To header
    assert judge(rows=[sent([f"Maya Chen <{FRIEND}>", "x@y.com"])]) == (True, "sent")  # Apple Mail list


def test_sent_mail_after_or_at_the_message_does_not_count():
    assert judge(rows=[sent(FRIEND, ts=T + 1)]) == (False, K.UNKNOWN)
    assert judge(rows=[sent(FRIEND, ts=T)]) == (False, K.UNKNOWN)
    assert judge(rows=[sent(FRIEND, ts="soon"), {"to": FRIEND}]) == (False, K.UNKNOWN)


def test_a_cc_recipient_counts_when_the_row_exposes_it():
    # Gmail rows carry only the To header; Apple Mail gives Cc in "to" when there is no To.
    assert judge(rows=[sent(f"boss@work.com, {FRIEND}")]) == (True, "sent")
    assert judge(rows=[sent(["Boss <boss@work.com>", FRIEND])]) == (True, "sent")


def test_the_persons_own_address_never_counts():
    assert judge("me@school.edu", rows=[sent("me@school.edu")], contacts={"me@school.edu"}) == (False, K.UNKNOWN)
    assert judge("ME@School.edu", contacts={"me@school.edu"}) == (False, K.UNKNOWN)


def test_names_domains_and_local_parts_never_match():
    rows = [sent("Maya Chen <maya.chen@other.org>"), sent("someone@friend.com"),
            sent("maya@elsewhere.com"), sent('"maya@friend.com" <x@evil.com>')]
    assert judge(rows=rows, contacts={"maya", "friend.com", "@friend.com", "Maya Chen"}) == (False, K.UNKNOWN)
    assert judge("Maya Chen", rows=[sent(f"Maya Chen <{FRIEND}>")]) == (False, K.NO_ADDRESS)
    assert judge("friend.com", contacts={"friend.com"}) == (False, K.NO_ADDRESS)


def test_addresses_compare_without_case():
    assert judge("Maya@Friend.COM", contacts={"MAYA@friend.com"}) == (True, "contact")
    assert judge("MAYA@FRIEND.COM", rows=[sent("Maya <maya@Friend.com>")]) == (True, "sent")


def test_an_empty_or_malformed_address_is_never_known():
    for bad in ("", None, "   ", "maya", "a@b@c.com", f"Maya <{FRIEND}>", f"{FRIEND} x@y.com", "@friend.com"):
        assert judge(bad, rows=[sent(FRIEND)], contacts={FRIEND}) == (False, K.NO_ADDRESS), bad


def test_stale_sent_without_a_contact_could_not_be_checked():
    assert judge(rows=[sent(FRIEND)], fresh=False) == (False, K.SENT_UNCHECKED)
    assert judge(rows=None, fresh=True) == (False, K.SENT_UNCHECKED)
    assert judge(rows=[sent(FRIEND)], before="later") == (False, K.SENT_UNCHECKED)


def test_stale_sent_with_a_contact_is_known():
    assert judge(rows=None, fresh=False, contacts={FRIEND}) == (True, "contact")


def test_unreadable_contacts_with_fresh_sent_is_known():
    assert judge(rows=[sent(FRIEND)], contacts=None) == (True, "sent")
    assert judge(rows=[sent(FRIEND)], contacts=lambda a: None) == (True, "sent")


def test_both_unreadable_is_refused():
    assert judge(rows=None, fresh=False, contacts=None) == (False, K.SENT_UNCHECKED)

    def broken(_address):
        raise RuntimeError("no Contacts")
    assert judge(rows=None, fresh=False, contacts=broken) == (False, K.SENT_UNCHECKED)


def test_read_sent_fails_closed(monkeypatch):
    from service import gmail_source
    from service.mail_memory import bridge

    def boom(*_a, **_k):
        raise RuntimeError("down")

    async def boom_async(*_a, **_k):
        raise bridge.BridgeError("app_away")

    monkeypatch.setattr(K, "_native", {"at": None, "rows": None})
    monkeypatch.setattr(gmail_source, "connected_accounts", boom)
    monkeypatch.setattr(bridge, "read_sent", boom_async)
    assert asyncio.run(K.read_sent(now=T)) == (None, False)

    monkeypatch.setattr(K, "_gmail_rows", boom)
    monkeypatch.setattr(K, "_native_rows", boom)
    assert asyncio.run(K.read_sent(now=T)) == (None, False)


def test_read_sent_keeps_only_current_sources(monkeypatch):
    from service import gmail_source
    from service.mail_memory import bridge
    row_a, row_b = {"ts": T - 9, "to": FRIEND, "account": "a@gmail.com"}, {"ts": T - 9, "to": FRIEND,
                                                                            "account": "b@gmail.com"}

    async def boom_async(*_a, **_k):
        raise bridge.BridgeError("app_away")

    monkeypatch.setattr(K, "_native", {"at": None, "rows": None})
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda: {"a@gmail.com": {}, "b@gmail.com": {}})
    monkeypatch.setattr(gmail_source, "_last", {"a@gmail.com": {"sent": T - 10}, "b@gmail.com": {"sent": T - 9999}})
    monkeypatch.setattr(gmail_source, "sent_rows", lambda: [row_a, row_b])
    monkeypatch.setattr(bridge, "read_sent", boom_async)
    assert asyncio.run(K.read_sent(now=T)) == ([row_a], True)

    # Nothing current: refused even though rows exist.
    monkeypatch.setattr(K, "_native", {"at": None, "rows": None})
    monkeypatch.setattr(gmail_source, "_last", {})
    assert asyncio.run(K.read_sent(now=T)) == (None, False)

    # Apple Mail read now, no Google: fresh.
    async def native(*_a, **_k):
        return [{"ts": T - 5, "to": [FRIEND]}], False
    monkeypatch.setattr(K, "_native", {"at": None, "rows": None})
    monkeypatch.setattr(bridge, "read_sent", native)
    assert asyncio.run(K.read_sent(now=T)) == ([{"ts": T - 5, "to": [FRIEND]}], True)


def test_read_contact_addresses_fails_closed(monkeypatch):
    from service.tools import imessage_tools as im
    monkeypatch.setattr(im, "_contacts", {"maya@friend.com": "Maya", "9255550100": "Sam", "MAYA@x.org": "M"})
    monkeypatch.setattr(im, "_contacts_privacy_current", True)
    monkeypatch.setattr(im, "_contacts_not_allowed", False)
    assert K.read_contact_addresses() == {"maya@friend.com", "maya@x.org"}

    monkeypatch.setattr(im, "_contacts_not_allowed", True)
    assert K.read_contact_addresses() is None
    monkeypatch.setattr(im, "_contacts_not_allowed", False)
    monkeypatch.setattr(im, "_contacts_privacy_current", False)
    assert K.read_contact_addresses() is None

    class Broken:
        def __iter__(self):
            raise RuntimeError("torn")
    monkeypatch.setattr(im, "_contacts_privacy_current", True)
    monkeypatch.setattr(im, "_contacts", Broken())
    assert K.read_contact_addresses() is None


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_mail_known.py"' in manifest
