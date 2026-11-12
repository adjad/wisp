"""A plain delete of a path with spaces gets a card, not a refusal (rc2 fix).

Nothing here is executed: each string is only handed to the policy. The path
may be written in one pair of quotes or with backslash-space; every other
quoting trick keeps the refusal it had. The card names the decoded paths and
is shown in every mode, whatever the person's own destructive_shell list says.
"""
import pytest

from service.safety import grants, policy
from service.safety.policy import Tier

MODES = (
    (False, True, None),
    (True, True, None),
    (True, False, None),
    (False, True, "allow"),
    (True, False, "allow"),
)

CARDED = {
    "rm -rf ~/Library/Application\\ Support/SomeApp/Cache":
        ["{home}/Library/Application Support/SomeApp/Cache"],
    "rm -rf '{home}/Library/Application Support/SomeApp/Cache'":
        ["{home}/Library/Application Support/SomeApp/Cache"],
    "rm -rf \"{home}/Library/Application Support/SomeApp/Cache\"":
        ["{home}/Library/Application Support/SomeApp/Cache"],
    "rm -rf '/tmp/my build' /tmp/other\\ one \"/tmp/third one/\"":
        ["/tmp/my build", "/tmp/other one", "/tmp/third one"],
    "rm -r -f '/tmp/a b'": ["/tmp/a b"],
}

REFUSED = [
    # The decoded path is not in an ordinary folder.
    "rm -rf '/Users'",
    "rm -rf \"{home}/Library/Application Support\"",
    "rm -rf ~/Library/Application\\ Support",
    "rm -rf '{home}/Documents'",
    "rm -rf '{home}/Documents/old stuff'",
    "rm -rf '/tmp'",
    "rm -rf '/'",
    "rm -rf \"/tmp/a b\" '/Users'",
    "rm -rf '/tmp/a/../../Users'",
    "rm -rf '/tmp/./a'",
    # Tilde is only home when it is unquoted.
    "rm -rf '~/Projects/app/build'",
    "rm -rf \"~/Projects/app/build\"",
    "rm -rf ~'/Projects/app/build'",
    # Quotes glued to other text, nested or mixed.
    "rm -rf '/tmp/a'\"b\"",
    "rm -rf '/tmp/a''b'",
    "rm -rf /tmp/'a b'",
    "rm -rf '/tmp/a b'x",
    "rm -rf \"/tmp/'a b'\"",
    "rm -rf '/tmp/\"a b\"'",
    "rm -rf '/tmp/a b",
    "rm -rf \"/tmp/a b",
    "rm -rf ''",
    "rm -rf \"\"",
    # Anything the shell would expand or treat specially, quoted or not.
    "rm -rf \"/tmp/$HOME\"",
    "rm -rf \"/tmp/`id`\"",
    "rm -rf \"/tmp/$(id)\"",
    "rm -rf \"/tmp/a\\\" b\"",
    "rm -rf \"/tmp/a\\\\b\"",
    "rm -rf \"/tmp/a!b\"",
    "rm -rf '/tmp/a*'",
    "rm -rf '/tmp/a?'",
    "rm -rf '/tmp/[ab]'",
    "rm -rf '/tmp/{a,b}'",
    "rm -rf '/tmp/a;b'",
    "rm -rf '/tmp/a|b'",
    "rm -rf '/tmp/a&b'",
    "rm -rf '/tmp/a>b'",
    "rm -rf '/tmp/a<b'",
    "rm -rf '/tmp/a\nb'",
    "rm -rf '/tmp/a\tb'",
    "rm -rf '/tmp/a=b'",
    "rm -rf '/tmp/büild'",
    # The only escape is backslash-space, outside quotes.
    "rm -rf /tmp/a\\b",
    "rm -rf /tmp/a\\\\ b",
    "rm -rf /tmp/a\\\tb",
    "rm -rf /tmp/a\\\nb",
    "rm -rf \\/tmp/a",
    # Flags stay unquoted and come first.
    "rm '-rf' /tmp/a",
    "rm -rf '/tmp/a b' -f",
    # A trailing backslash-space is cut with the outer spaces: refused, not guessed.
    "rm -rf /tmp/trailing\\ ",
    # Still nothing beside the one command.
    "rm -rf '/tmp/a b'; rm -rf ~",
    "rm -rf '/tmp/a b' && rm -rf ~",
    "rm -rf '/tmp/a b' | cat",
    "sh -c \"rm -rf '/tmp/a b'\"",
    "rm -rf  '/tmp/a b'",
    "rm -rf '/tmp/a b'  /tmp/c",
]


@pytest.fixture
def home(monkeypatch, tmp_path):
    folder = tmp_path / "home"
    (folder / "Library" / "Application Support" / "SomeApp").mkdir(parents=True)
    (folder / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    return folder


def _decide(monkeypatch, cmd, *, full_access=False, keep_floor=True, granted=None, read_only=False):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_READ_ONLY", read_only)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    monkeypatch.setattr(grants, "check", lambda *a, **k: granted)
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell")


@pytest.mark.parametrize("cmd,paths", list(CARDED.items()))
def test_a_quoted_or_escaped_path_gets_a_card_naming_the_decoded_path(monkeypatch, home, cmd, paths):
    cmd = cmd.replace("{home}", str(home))
    paths = [p.replace("{home}", str(home)) for p in paths]
    assert policy.plain_delete_paths({"cmd": cmd}) == paths
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                           granted=granted)
        assert decision.tier is Tier.CONFIRM, (cmd, full_access, keep_floor, granted)
        for path in paths:
            shown = "~" + path[len(str(home)):] if path.startswith(str(home) + "/") else path
            assert f"“{shown}”" in decision.reason, (decision.reason, shown)
        assert decision.delete_targets is not None


@pytest.mark.parametrize("cmd", REFUSED)
def test_every_other_quoting_keeps_its_refusal(monkeypatch, home, cmd):
    cmd = cmd.replace("{home}", str(home))
    assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                           granted=granted)
        assert decision.tier is Tier.DENY, (cmd, full_access, keep_floor, granted)


def test_the_decoded_words_are_what_runs(home):
    argv, targets = policy.plain_delete_argv(
        {"cmd": "rm -rf ~/Library/Application\\ Support/SomeApp/Cache '/tmp/a b/'"})
    assert argv == ("/bin/rm", "-rf", "--",
                    f"{home}/Library/Application Support/SomeApp/Cache", "/tmp/a b")
    assert [full for full, _ in targets] == list(argv[3:])


def test_the_card_does_not_depend_on_the_persons_destructive_list(monkeypatch, home):
    # A narrowed (or emptied) destructive_shell list used to be the only thing
    # giving a plain delete its card under Full access or a saved Always allow.
    real_rules = policy._rules
    monkeypatch.setattr(policy, "_rules", lambda key, default: (
        [] if key == "destructive_shell" else real_rules(key, default)))
    assert policy.is_destructive_shell("rm -rf /tmp/build") is None
    for cmd in ("rm -rf /tmp/build", "rm -rf '/tmp/a b'", "rm -rf ~/Projects/app/build"):
        for full_access, keep_floor, granted in MODES:
            decision = _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                               granted=granted)
            assert decision.tier is Tier.CONFIRM, (cmd, full_access, keep_floor, granted)


def test_view_only_still_refuses_a_quoted_delete(monkeypatch, home):
    assert _decide(monkeypatch, "rm -rf '/tmp/a b'", read_only=True).tier is Tier.DENY
    assert _decide(monkeypatch, "rm -rf '/tmp/a b'", full_access=True, read_only=True).tier is Tier.DENY


def test_a_link_with_spaces_is_judged_as_its_target(monkeypatch, home):
    link = home / "Projects" / "my app"
    link.parent.mkdir()
    link.symlink_to(home / "Documents")
    for cmd in (f"rm -rf '{link}'", "rm -rf ~/Projects/my\\ app"):
        assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
        assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.DENY


def test_a_long_tricky_line_is_judged_quickly(home):
    import time
    for text in ("rm -rf " + "'/tmp/a b' " * 2000, "rm -rf " + "/tmp/a\\ " * 4000,
                 "rm -rf '" + "/a" * 7000, "rm -rf " + "\\ " * 7000):
        text = text[:policy.SHELL_TEXT_LIMIT]
        start = time.thread_time()
        assert policy.plain_delete_paths({"cmd": text}) is None
        policy.decide("shell", {"cmd": text}, tool="run_shell")
        assert time.thread_time() - start < 1.0
