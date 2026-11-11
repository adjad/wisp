"""Never-run gaps closed in rc2, and the `public` folder pinned (rc2 fixes).

Nothing here is executed: each string is only handed to the policy.
"""
import pytest

from service.safety import grants, policy
from service.safety.policy import Tier

MODES = ((False, True, None), (True, True, None), (True, False, None), (False, True, "allow"))

# A negated or alternative filter picks (nearly) everything, so it is no filter.
FIND_REFUSED = [
    "find . -not -name x -delete",
    "find . ! -name x -delete",
    "find . -type f -not -name '*.keep' -delete",
    "find . -name a -o -type f -delete",
    "find . -type f -o -name a -delete",
    "find . \\( -type f -o -name b \\) -delete",
    "find . ! \\( -name a -name b \\) -delete",
    "find . -name keep -prune -o -delete",
    "find . -name a , -delete",
    "find ~/Projects -not -name x -delete",
    "find /tmp/x -not -name y -delete",
    "find . -not -name x -exec rm {} \\;",
    "find . -type f -exec grep -name {} \\; -delete",
]
FIND_CARDED = [
    "find . -name '*.pyc' -delete",
    "find . -name '*.pyc' -o -name '__pycache__' -delete",
    "find . \\( -name a -o -name b \\) -delete",
    "find . -name '*.log' -mtime +30 -delete",
    "find ~/Projects -name .DS_Store -delete",
    "find build -not -name keep -delete",          # a plain folder of home needs no filter
    "cd proj && find . -not -name x -delete",
]

# A shell whose program text comes from a command's output.
SUBST_REFUSED = [
    "bash -c \"$(printf 'x')\"",
    "sh -c \"$(printf '\\x72\\x6d -rf ~')\"",
    "zsh -c \"$(cat notes.txt)\"",
    "bash -c \"cd $(pwd) && make\"",
    "bash -c `printf x`",
    "sudo bash -c \"$(printf x)\"",
    "bash <<< \"$(printf x)\"",
    "echo \"$(printf x)\" | sh",
    "printf x | xargs -I{} bash -c \"$(printf {})\"",
]
SUBST_LEFT = [
    "eval \"$(brew shellenv)\"",
    "eval \"$(pyenv init -)\"",
    "bash -c 'echo $(date)'",
    "echo \"$(date)\"",
    "sh -c \"$(cat <<'EOF'\necho hi\nEOF\n)\"",
    "bash -c \"echo hi\"",
]


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    return tmp_path


def _decide(monkeypatch, cmd, *, full_access=False, keep_floor=True, granted=None):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    monkeypatch.setattr(grants, "check", lambda *a, **k: granted)
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell")


@pytest.mark.parametrize("cmd", FIND_REFUSED)
def test_a_negated_or_alternative_filter_is_no_filter(monkeypatch, cmd):
    assert policy.never_run_hit(cmd), cmd
    for full_access, keep_floor, granted in MODES:
        assert _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                       granted=granted).tier is Tier.DENY, (cmd, full_access, granted)


@pytest.mark.parametrize("cmd", FIND_CARDED)
def test_a_real_filter_keeps_its_card(monkeypatch, cmd):
    assert policy.never_run_hit(cmd) is None, cmd
    assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.CONFIRM, cmd


@pytest.mark.parametrize("cmd", SUBST_REFUSED)
def test_a_shell_fed_a_command_substitution_is_refused(monkeypatch, cmd):
    hit = policy.never_run_hit(cmd)
    assert hit, cmd
    for full_access, keep_floor, granted in MODES:
        assert _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                       granted=granted).tier is Tier.DENY, (cmd, full_access, granted)


@pytest.mark.parametrize("cmd", SUBST_LEFT)
def test_ordinary_substitutions_are_left_alone(cmd):
    assert policy.never_run_hit(cmd) is None, cmd


def test_a_relative_public_folder_is_home_public_only_when_the_working_folder_is_home(monkeypatch):
    # run_shell runs in home, and the Mac's disk ignores case, so a bare
    # `public` there IS ~/Public: the refusal is right.
    for cmd in ("rm -rf public", "rm -rf Public", "rm -rf public/site", "find public -name x -delete",
                "cd ~ && rm -rf public"):
        assert policy.never_run_hit(cmd), cmd
    # Anywhere else it is an ordinary folder and keeps its card.
    for cmd in ("cd proj && rm -rf public", "cd ~/Projects/site && rm -rf public",
                "rm -rf proj/public", "rm -rf ~/Projects/site/public",
                "cd /tmp/site && rm -rf public", "cd ~/Projects/site && find public -delete"):
        if "~/" not in cmd.split("&&")[-1]:       # an absolute path gets the plain-delete card
            assert policy.never_run_hit(cmd) is None, cmd
        assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.CONFIRM, cmd
    # A working folder only known at run time could be home: still refused.
    assert policy.never_run_hit("cd \"$SITE\" && rm -rf public")
