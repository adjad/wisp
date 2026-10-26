"""run_shell / run_applescript may not reach Wisp's own service.

The human-only send routes (email_reply, send_message_draft, card fire,
done/clear) used to be matched against the raw command text, so a
percent-encoded path or one built from a shell variable got past the deny
while uvicorn decoded the path and served it. The text is now percent-decoded
until stable and lowercased first, and any command that names the service port
on a loopback host is refused outright. Both hold under full access with the
floor lifted. Pure policy checks: nothing is run, nothing is contacted.
"""
import pytest

from service.safety import policy


@pytest.fixture
def full_access(monkeypatch):
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", False)       # not liftable by full access


def tier(tool, text):
    args = {"script": text} if tool == "run_applescript" else {"cmd": text}
    return policy.decide("shell", args, tool=tool).tier


SEND = "curl -X POST -H 'Content-Type: application/json' -d '{}' "


@pytest.mark.parametrize("cmd", [
    SEND + "http://127.0.0.1:8765/assistant/email_reply/send",                  # plain
    SEND + "http://127.0.0.1:8765/assistant/%65mail_reply/send",                # percent-encoded
    SEND + "http://127.0.0.1:8765/assistant/%2565mail_reply/send",              # double-encoded
    SEND + "http://127.0.0.1:8765/assistant/%252565mail_reply/send",            # triple-encoded
    SEND + "http://127.0.0.1:8765/%61ssistant/send_message_draft",
    SEND + "http://127.0.0.1:8765/assistant/proactive/cards/card:abc/%66ire",
    "CURL -X POST HTTP://LocalHost:8765/Assistant/Email_Reply/Send -d '{}'",    # mixed case
    "u=email_reply; curl -X POST http://127.0.0.1:8765/assistant/$u/send -d '{}'",   # variable-built
    "u=send_message_draft; curl -X POST \"http://localhost:8765/assistant/${u}\" -d x",
])
def test_send_routes_are_denied_however_the_path_is_spelled(cmd, full_access):
    assert tier("run_shell", cmd) is policy.Tier.DENY


@pytest.mark.parametrize("cmd", [
    "curl http://wisp.example/assistant/%65mail_reply/send",
    "curl http://wisp.example/assistant/%2565mail_reply/send",
    "curl http://wisp.example/ASSISTANT/EMAIL_REPLY/send",
    "curl http://wisp.example/assistant/needs_you/r:1/%64one",
])
def test_the_send_deny_reads_the_decoded_lowercased_path(cmd):
    assert policy.local_send_endpoint_hit(cmd)


@pytest.mark.parametrize("host", [
    "127.0.0.1:8765", "localhost:8765", "[::1]:8765", "0.0.0.0:8765",
    "LOCALHOST:8765", "[0:0:0:0:0:0:0:1]:8765", "127.1:8765", "0:8765",
    "127.0.0.1%3A8765", "localhost%253a8765", "wisp.localhost:8765", "[::ffff:127.0.0.1]:8765",
])
def test_any_command_aimed_at_the_service_port_is_denied(host, full_access):
    cmd = f"curl -s http://{host}/assistant/today"
    assert policy.local_send_endpoint_hit(cmd) is None
    assert policy.local_service_hit(cmd)
    assert tier("run_shell", cmd) is policy.Tier.DENY


@pytest.mark.parametrize("cmd", [
    "u=email_reply; p=send; curl -X POST http://127.0.0.1:8765/assistant/$u/$p -d '{}'",
    "curl http://'local''host':87'65'/assistant/today",
    "curl http://local\\host:87\\65/assistant/today",
    "printf 'POST /assistant/x HTTP/1.0\\r\\n\\r\\n' | nc localhost 8765",
    "python3 -c \"import socket; socket.create_connection(('127.0.0.1', 8765))\"",
])
def test_built_or_quoted_commands_still_name_the_port(cmd, full_access):
    assert policy.local_service_hit(cmd)
    assert tier("run_shell", cmd) is policy.Tier.DENY


@pytest.mark.parametrize("script", [
    'do shell script "u=email_reply; curl -X POST http://localhost:8765/assistant/$u/send"',
    'do shell script "curl http://127.0.0.1:" & "8765/assistant/%65mail_reply/send"',
    'do shell script "curl -s http://[::1]:8765/assistant/today"',
])
def test_applescript_is_held_to_the_same_rule(script, full_access):
    assert tier("run_applescript", script) is policy.Tier.DENY


@pytest.mark.parametrize("cmd", [
    "curl -s https://api.github.com/repos/example/example",
    "lsof -nP -iTCP:8765 -sTCP:LISTEN",
    "lsof -i :8765",
    "seq 0 8765 | tail -1",
    "date +%Y-%m-%d",
    "printf '%20s|' hi",
    "ls ~/Desktop",
])
def test_unrelated_shell_use_is_untouched(cmd, full_access):
    assert policy.local_send_endpoint_hit(cmd) is None
    assert policy.local_service_hit(cmd) is None
    assert tier("run_shell", cmd) is policy.Tier.ALLOW


@pytest.mark.parametrize("cmd", [
    "curl -s http://127.0.0.1:8000/v1/models",       # another local service: a card, not a refusal
    "curl -s http://localhost:87650/",
    "curl -s http://example.com:8765/status",        # a name could resolve to 127.0.0.1
    "curl -s http://192.168.1.20:8765/",
    "curl -s http://localtest.me:8765/assistant/today",
])
def test_other_hosts_and_ports_get_a_card_not_a_refusal(cmd, full_access):
    assert policy.local_service_hit(cmd) is None
    assert tier("run_shell", cmd) is policy.Tier.CONFIRM


# Spellings the first version of the deny missed (dev1 hardening review).
@pytest.mark.parametrize("cmd", [
    "curl -s http://127.0.0.1:08765/assistant/today",                 # leading zeros
    "curl -s http://localhost:0008765/assistant/today",
    "exec 3<>/dev/tcp/127.0.0.1/8765; printf 'GET /assistant/today' >&3",
    "cat < /dev/tcp/localhost/8765",
    "curl --connect-to ::127.0.0.1:8765 http://wisp.example/assistant/today",   # ':' before the host
    "curl --resolve wisp.example:8765:127.0.0.1 http://wisp.example:8765/",
    "curl http://0x7f.0.0.1:8765/", "curl http://0x7f.1:8765/",        # dotted hex
    "curl http://0177.0.0.1:8765/", "curl http://0177.1:8765/",        # dotted octal
    "curl http://017700000001:8765/", "curl http://0x7F000001:8765/",  # whole-number forms
    "curl http://localhost.:8765/",                                    # trailing dot
    "curl http://[::]:8765/", "curl http://[0:0:0:0:0:ffff:127.0.0.1]:8765/",
])
def test_cheap_bypass_spellings_are_denied(cmd, full_access):
    assert policy.local_service_hit(cmd)
    assert tier("run_shell", cmd) is policy.Tier.DENY


@pytest.mark.parametrize("cmd", [
    "grep -rn 'localhost:8765' docs/",
    "grep -rn localhost:8765 README.md docs",
    "/usr/bin/grep -n 127.0.0.1:8765 service/main.py",
    "grep -rn assistant/email_reply service/",
    "echo http://127.0.0.1:8765/assistant/today",       # a validated read-only command
])
def test_searching_text_that_mentions_the_port_is_allowed(cmd, full_access):
    assert tier("run_shell", cmd) is policy.Tier.ALLOW


@pytest.mark.parametrize("cmd", [
    "grep -rn localhost:8765 docs/ | head",              # a pipeline is not a plain search
    "grep -rn localhost:8765 docs/ $(curl -s http://localhost:8765/x)",
    "rg --pre ./x localhost:8765 docs/",                 # rg --pre runs a command
])
def test_only_a_plain_grep_is_exempt(cmd, full_access):
    assert tier("run_shell", cmd) is policy.Tier.DENY


# Spellings the text deny cannot see. The deny is best-effort; these must
# still never run without a card under full access.
@pytest.mark.parametrize("cmd", [
    "p=8765; curl -s http://localhost:$p/assistant/today",
    "curl -s http://127.0.0.1:$((8000+765))/assistant/today",
    "nc localhost $((8000+765))",
    "socat - TCP:127.1:8765",
    "h=127.0.0.1; curl -s http://$h:8765/assistant/today",
    "curl -s http://wisp.example:8765/assistant/today",    # a name that could resolve to loopback
])
def test_what_the_deny_misses_still_gets_a_card(cmd, full_access):
    assert tier("run_shell", cmd) is not policy.Tier.ALLOW
