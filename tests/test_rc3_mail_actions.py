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


def test_only_the_automation_refusal_carries_the_fixed_code():
    # macOS error -1743 stops the Apple Event before Mail or Messages sees it.
    # The backend treats this code, and nothing else from the app, as proof
    # that a send never began (service/assistant/outbox.py).
    from service.assistant import outbox
    assert f'code == -1743 ? ["refusal": "{outbox._AUTOMATION_DENIED}"] : [:]' in SENDER
    assert SENDER.count('"refusal"') == 1
    for name in ("sendEmail", "sendMessage", "run", "runReply"):
        body = _function(SENDER, name)
        assert "extra: refusal(code)" in body, name
        assert "NSAppleScript.errorNumber] as? Int ?? 0" in body, name
    # A success never carries it.
    assert not re.search(r"ok: true[^\n]*refusal", SENDER)


def test_a_short_code_is_texted_by_sms_and_never_by_imessage():
    # Messages accepts an iMessage send to a short code and then marks it Not
    # Delivered, so the old "iMessage, then SMS on error" order never reached SMS.
    body = _function(SENDER, "sendMessage")
    short, rest = body.split('""" : """')
    short = short[short.index("let script"):]
    assert "service type = SMS" in short and "iMessage" not in short and "try" not in short
    assert rest.index("service type = iMessage") < rest.index("service type = SMS")
    assert ": shortCode ? noTextRelay" in body
    assert "(3...6).contains(t.count) && t.allSatisfy(\\.isNumber)" in _function(SENDER, "isShortCode")
    assert "Text Message Forwarding" in re.search(r'static let noTextRelay = "([^"]+)"', SENDER).group(1)


def test_a_sent_text_is_not_reported_as_delivered():
    from service.tools import action_tools
    source = (ROOT / "service/tools/action_tools.py").read_text(encoding="utf-8")
    assert 'return f"Message sent to {display}. {MESSAGE_HANDED_OVER}"' in source
    assert 'if delivery == "failed":' in source and 'if delivery == "no_sms_account":' in source
    assert action_tools.MESSAGE_HANDED_OVER == "Messages accepted it; delivery isn't confirmed here."
    from service.tools.registry import classify_tool_outcome
    receipt = "Message sent to 22821. " + action_tools.MESSAGE_HANDED_OVER
    assert classify_tool_outcome("send_message", receipt).status == "succeeded"


READER = (ROOT / "app/Sources/WispApp/MessagesReader.swift").read_text(encoding="utf-8")


def test_the_app_reads_the_text_back_from_messages_before_answering():
    # tests/MessagesReaderRegression.swift runs the same read against a
    # synthetic store; this pins the query and the wiring.
    check = READER[READER.index("enum MessageDeliveryCheck"):READER.index("enum TypedStreamText")]
    query = check[check.index("static let query"):check.index("/// One look at the store.")]
    assert "WHERE m.is_from_me = 1 AND m.date >= ?" in query
    assert "JOIN handle h ON m.handle_id = h.ROWID" in query
    for column in ("m.is_sent", "m.is_delivered", "m.error", "m.service"):
        assert column in query
    assert "SQLITE_OPEN_READONLY" in check
    # An error on the row wins over any sent or delivered flag.
    assert check.index("!= 0 ? .failed") < check.index("? .delivered") < check.index("? .sent : .pending")
    body = _function(SENDER, "sendMessage")
    since = body.index("MessageDeliveryCheck.storeDate(")
    run = body.index("s.executeAndReturnError(&err)")
    settle = body.index("MessageDeliveryCheck.settle(to: to, text: text, sinceNs: since)")
    assert since < run < settle
    assert '"delivery": seen.state.rawValue, "delivery_service": seen.service' in body
    # With no SMS account the short-code script stops before its send line.
    short = body[body.index("let script"):body.index('""" : """')]
    assert short.index('return "\\(noSMSAccount)"') < short.index("send ")
    assert 'static let noSMSAccount = "no_sms_account"' in SENDER
