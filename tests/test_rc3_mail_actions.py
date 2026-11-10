"""Source contracts for the Mail scripts the app runs for triage, send and forward.

The scripts are Swift string templates, so these read the source text. They run
nothing and contact nothing. The native checks in tests/MailReplyScriptChecks.swift
build and compile the same scripts.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SENDER = (ROOT / "app/Sources/WispApp/OutboundSender.swift").read_text(encoding="utf-8")
SCRIPTS = (ROOT / "app/Sources/WispApp/MailReplyScript.swift").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    """The text of one `static func`, up to the next member at the same depth."""
    start = source.index(f"static func {name}(")
    rest = source[start + 1:]
    following = re.search(r"\n    (?:private )?static (?:func|let) |\n    // |\n    /// ", rest)
    return source[start:start + 1 + (following.start() if following else len(rest))]


def test_triage_lookup_names_the_inbox_mailbox_the_way_the_reader_does():
    lookup = _function(SCRIPTS, "findInboxMessage")
    first = lookup.index('set mbx to mailbox "INBOX" of acct')
    retry = lookup.index('set mbx to mailbox "Inbox" of acct')
    assert first < retry
    # Mail's dictionary gives an account mailboxes, not an `inbox` property.
    assert "inbox of acct" not in lookup
    assert lookup.count("whose message id is") == 2
    assert 'if theMsg is missing value then error "\\(messageNotFound)"' in lookup


def test_mark_read_flag_and_archive_use_the_working_lookup():
    for name in ("markEmailRead", "archiveEmail", "flagEmail"):
        body = _function(SENDER, name)
        assert "MailActionScript.findInboxMessage(messageID: messageId)" in body, name
        assert "unverifiedForwardLookup" not in body, name


def test_not_found_text_matches_the_sandbox_mirror():
    declared = re.search(r'static let messageNotFound = "([^"]+)"', SCRIPTS).group(1)
    mirror = (ROOT / "sandbox/outbound.py").read_text(encoding="utf-8")
    assert declared in re.sub(r'"\s*\n\s*"', "", mirror)


def test_forward_stays_held_back_until_it_is_checked_in_mail():
    # The old lookup is kept for forward alone, so a forward still stops before
    # Mail composes anything. Switch forward to the working lookup only after a
    # real forward (with and without a note) has been checked in Mail, and
    # update this test with it.
    assert SENDER.count("unverifiedForwardLookup(") == 2        # its definition and one caller
    assert "unverifiedForwardLookup(messageId)" in _function(SENDER, "forwardEmail")
    assert "MailActionScript.findInboxMessage" not in _function(SENDER, "forwardEmail")


def test_send_and_forward_treat_a_false_answer_from_mail_as_a_failure():
    # Mail's `send` answers a boolean and raises nothing when it refuses.
    guard = 'if (send) is not true then error "\\(sendNotAccepted)"'
    for name, target in (("sendEmail", "tell newMsg"), ("forwardEmail", "tell fwd")):
        script = _function(SCRIPTS, name)
        assert target in script and script.index(target) < script.index(guard), name
        assert not any(line.strip() == "send" for line in script.splitlines()), name
    wording = re.search(r'static let sendNotAccepted = "([^"]+)"', SCRIPTS).group(1)
    assert "check Mail before retrying" in wording


def test_the_app_sends_and_forwards_with_the_guarded_scripts():
    assert "MailActionScript.sendEmail(to: to, cc: cc, subject: subject, body: body)" in _function(SENDER, "sendEmail")
    assert "MailActionScript.forwardEmail(" in _function(SENDER, "forwardEmail")
    # No send in OutboundSender bypasses the guard: the only bare `send` lines
    # left are Messages' `send "…" to participant …`, which has no result.
    bare = [line.strip() for line in SENDER.splitlines() if line.strip() == "send"]
    assert bare == []
