"""One strict reading of a mail From value (service/mail_sender.py). Pure strings; no mail is read."""
from __future__ import annotations

import pytest

from service import mail_sender as S

EVIL = "x@evil.com"


@pytest.mark.parametrize("sender, parsed, address, odd", [
    # Ordinary senders.
    ("Maya Chen <maya@friend.com>", "", "maya@friend.com", False),
    ('"Chen, Maya" <maya@friend.com>', "", "maya@friend.com", False),
    ("maya@friend.com", "", "maya@friend.com", False),
    ("<maya@friend.com>", "", "maya@friend.com", False),
    ("Maya Chen", "maya@friend.com", "maya@friend.com", False),          # header-only row: name and address apart
    ("Maya Chen <maya@friend.com>", "maya@friend.com", "maya@friend.com", False),
    ('"maya@friend.com" <maya@friend.com>', "", "maya@friend.com", False),   # the name IS the address: not odd
    ("MAYA@Friend.com", "", "maya@friend.com", False),
    ("Maya Chen", "", "", False),                                         # a name and no address: nothing to trust
    ("", "", "", False),
    # A name dressed up as someone else's address.
    ('"Maya <maya@friend.com>" <x@evil.com>', "", EVIL, True),            # Apple Mail: the raw header
    ("Maya <maya@friend.com> <x@evil.com>", "", EVIL, True),              # Gmail: quotes already dropped
    ('"me@school.edu" <x@evil.com>', "", EVIL, True),
    ("Maya <maya@friend.com>", EVIL, EVIL, True),                         # header-only: the name carries an address
    ('"Maya (maya@friend.com)" <x@evil.com>', "", EVIL, True),
    # A quoted local part, or junk after the address: no address is guessed from the name.
    ('"Dean <dean@school.edu>" <"x>"@evil.com>', "", "", True),
    ('Dean <dean@school.edu> <"x y"@evil.com>', "", "", True),
    ("Maya <maya@friend.com> and friends", "", "", True),
    ("Maya <maya@friend.com>, Sam <sam@friend.com>", "", "sam@friend.com", True),
    ("see maya@friend.com for details", "", "", True),
    ("Maya <maya@friend.com", "", "", True),                              # unterminated
    ("Maya <maya@friend.com> <>", "", "", True),
    ("Maya Chen <maya@friend.com>", "not an address", "", True),          # a parsed field that is not one address
    ("Maya Chen <maya@friend.com>", "a@b.com, c@d.com", "", True),
    # Look-alike and full-width marks, and an encoded word that decodes to an address.
    ("Maya ＜maya@friend.com＞ <x@evil.com>", "", EVIL, True),
    ("Maya ‹maya＠friend.com› <x@evil.com>", "", EVIL, True),
    ("Maya 〈maya@friend.com〉 <x@evil.com>", "", EVIL, True),
    ("=?utf-8?q?Maya_=3Cmaya=40friend=2Ecom=3E?= <x@evil.com>", "", EVIL, True),
    ("=?utf-8?b?TWF5YSBDaGVu?= <maya@friend.com>", "", "maya@friend.com", False),   # "Maya Chen", encoded: fine
])
def test_the_real_address_and_whether_the_name_is_dressed_up(sender, parsed, address, odd):
    assert S.real_address(sender, parsed) == address
    assert S.is_odd(sender, parsed) is odd


def test_an_odd_sender_is_shown_by_its_real_address_never_its_name():
    assert S.shown('"Maya <maya@friend.com>" <x@evil.com>') == EVIL
    assert S.shown("Maya <maya@friend.com>", EVIL) == EVIL
    assert S.shown('"Dean <dean@school.edu>" <"x>"@evil.com>') == "Unknown sender"
    assert S.shown("Maya Chen <maya@friend.com>") == "Maya Chen"
    assert S.shown('"Chen, Maya" <maya@friend.com>') == "Chen, Maya"
    assert S.shown("maya@friend.com") == "maya@friend.com"
    assert S.shown("=?utf-8?b?TWF5YSBDaGVu?= <maya@friend.com>") == "Maya Chen"
    for posed in ('"Maya <maya@friend.com>" <x@evil.com>', "Maya ＜maya@friend.com＞ <x@evil.com>"):
        assert "maya@friend.com" not in S.shown(posed) and "Maya" not in S.shown(posed)


def test_a_row_is_the_users_own_only_when_it_is_really_theirs_and_not_dressed_up():
    mine = {"me@school.edu"}
    assert S.is_own("Me <me@school.edu>", "", mine) and S.is_own("Adi", "ME@school.edu", mine)
    for posed in ('"me@school.edu" <x@evil.com>', "Me <me@school.edu> <x@evil.com>", "Me <me@school.edu>",
                  '"Me <me@school.edu>" <"x>"@evil.com>', "me@school.edu and others"):
        parsed = EVIL if posed == "Me <me@school.edu>" else ""
        assert S.is_own(posed, parsed, mine) is False, posed
    # Their own address with someone else's address in the name is odd, so not trusted as theirs either.
    assert S.is_own("Dean <dean@school.edu> <me@school.edu>", "", mine) is False
    assert S.is_own("Me <me@school.edu>", "", set()) is False


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_mail_sender.py"' in manifest
