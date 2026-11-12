"""Repairs for the rc2 policy audit notes.

Nothing here is executed: each string is only handed to the policy, and the
only files made are links and notes inside pytest's tmp_path (the test home).
"""
import os
import time

import pytest

from service.safety import grants, policy
from service.safety.policy import Tier

MODES = ((False, True, None), (True, True, None), (True, False, None), (False, True, "allow"),
         (True, True, "allow"))


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", True)
    monkeypatch.setattr(grants, "check", lambda *a, **k: None)
    return folder


def _decide(monkeypatch, cmd, *, full_access=False, keep_floor=True, granted=None):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    monkeypatch.setattr(grants, "check", lambda *a, **k: granted)
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell")


# --- 1. the notes file of a data-only shape ----------------------------------

# A step before the data shape that can make a link, write a file or move the
# working folder: the whole line is read by the floor (and "reboot" refuses it).
LEADS = ["ln -s .zshrc n.txt", "ln -s Library/LaunchAgents/x.plist n.txt", "cp a n.txt",
         "mv a n.txt", "cd notes", "pushd notes", "install a n.txt", "rsync a n.txt",
         "ditto a n.txt", "touch n.txt", "git checkout x", "git diff --output=n.txt",
         "mkdir -p ../x", "mkdir -p .config", "mkdir -m 777 x", "true", "echo hi"]


@pytest.mark.parametrize("lead", LEADS)
def test_a_step_that_can_redirect_the_notes_file_drops_the_data_shape(monkeypatch, lead):
    for tail in ("echo 'reboot' >> n.txt", "echo 'reboot' > n.txt",
                 "cat > n.txt <<'EOF'\nreboot\nEOF"):
        cmd = f"{lead} && {tail}"
        assert policy.inert_text_form(cmd) is None, cmd
        for full_access in (False, True):
            assert _decide(monkeypatch, cmd, full_access=full_access).tier is Tier.DENY, cmd


def test_the_harmless_steps_keep_the_data_shape(monkeypatch):
    for cmd in ("git add -A && git commit -m 'reboot fix'",
                "cd proj && git add -A && git commit -m 'reboot fix'",
                "cd ~/Projects/app && git commit -m 'stop the halt'",
                "git status && git diff && echo 'reboot later' >> notes.txt",
                "mkdir -p notes/today && echo 'reboot later' >> notes/today/a.md",
                "mkdir notes && cat > notes/a.md <<'EOF'\nreboot\nEOF"):
        assert policy.inert_text_form(cmd) is not None, cmd
        assert _decide(monkeypatch, cmd).tier is Tier.CONFIRM, cmd
        assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.ALLOW, cmd


def test_a_notes_file_that_is_a_link_drops_the_data_shape(monkeypatch, home, tmp_path):
    (home / ".zshrc").write_text("")
    (home / "n.txt").symlink_to(home / ".zshrc")
    (home / "agents").symlink_to(tmp_path)           # a folder link leaving home
    (home / "real").mkdir()
    (home / "real" / "inner").symlink_to(home / "real")
    (home / "plain.txt").write_text("x")
    os.link(home / "plain.txt", home / "hard.txt")   # a second name for one file
    (home / "file").write_text("")
    for target in ("n.txt", "agents/n.txt", "real/inner/n.txt", "hard.txt", "plain.txt",
                   "file/n.txt", "Library/LaunchAgents/n.txt", "library/launchagents/x.md",
                   "Library/Keychains/n.txt"):
        for cmd in (f"echo 'reboot' >> {target}", f"cat > {target} <<'EOF'\nreboot\nEOF"):
            assert policy.inert_text_form(cmd) is None, cmd
            for full_access in (False, True):
                assert _decide(monkeypatch, cmd, full_access=full_access).tier is Tier.DENY, cmd


def test_an_ordinary_notes_file_keeps_the_data_shape(monkeypatch, home):
    (home / "notes").mkdir()
    (home / "notes" / "old.txt").write_text("x")
    for target in ("notes/old.txt", "notes/new.txt", "new.md", "missing/folder/new.txt"):
        cmd = f"echo 'remember the reboot' >> {target}"
        assert policy.inert_text_form(cmd) is not None, cmd
        assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.ALLOW, cmd


# --- 2a. a find with a real filter and an exclusion ---------------------------

FIND_CARDED = [
    "find . -name '*.pyc' -not -path './keep/*' -delete",
    "find . -not -path './keep/*' -name '*.tmp' -delete",
    "find . -type f -name '*.o' ! -name keep.o -delete",
    "find ~/Projects -name '*.log' ! -name keep.log -delete",
    "find . -mtime +30 -not -name '*.keep' -exec rm {} \\;",
    "find . -depth -name '*.tmp' -delete",
    "find . -path ./keep -prune -o -name '*.tmp' -delete",
    "find . \\( -name '*.pyc' -o -name '*.pyo' \\) -not -path './keep/*' -delete",
]
FIND_REFUSED = [
    "find . -not -name x -o -name y -delete",        # the first alternative has no filter
    "find . -name a -o -not -path x -delete",
    "find . ! -fstype -name -delete",                # `-name` is -fstype's operand
    "find . -not -name -name -delete",
    "find . ! ! -name x -delete",
    "find . -not \\( -name a \\) -delete",
    "find . -not \\( -name a -o -name b \\) -delete",
    "find . -type f -not -name '*.keep' -delete",
]


@pytest.mark.parametrize("cmd", FIND_CARDED)
def test_a_real_filter_with_an_exclusion_keeps_its_card(monkeypatch, cmd):
    assert policy.never_run_hit(cmd) is None, cmd
    assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.CONFIRM, cmd


@pytest.mark.parametrize("cmd", FIND_REFUSED)
def test_a_find_with_only_negated_filters_is_still_refused(monkeypatch, cmd):
    assert policy.never_run_hit(cmd), cmd
    for full_access, keep_floor, granted in MODES:
        assert _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                       granted=granted).tier is Tier.DENY, (cmd, full_access, granted)


# --- 2b. a shell fed a command substitution -----------------------------------

# The program is known; only an argument comes from another command.
SUBST_CARDED = [
    "sh -c \"make -C $(git rev-parse --show-toplevel)\"",
    "bash -c \"echo $(date)\"",
    "bash -c \"cd $(pwd) && make\"",
    "bash -c \"$(date)\"",                           # output no text can choose
    "echo \"$(date)\" | sh",
    "zsh -c \"ls $(brew --prefix)/bin\"",
]
# The output can become a command.
SUBST_STILL_REFUSED = [
    "bash -c \"$(printf x)\"",
    "echo \"$(cat f)\" | sh",
    "bash <<< \"$(cat f)\"",
    "bash -c \"p=$(printf x); \\$p\"",
    "bash -c \"eval $(cat f)\"",
    "bash <<< \"echo $(cat notes.txt)\"",          # a file's text is pasted into the program
    "bash -c \"sh -c '$(cat f)'\"",
    "bash -c \"python3 -c $(cat f)\"",
    "bash -c \"sh $(cat f)\"",
    "bash -c \"echo hi; $(cat f) -x\"",
    "bash -c \"echo $(cat f) | sh\"",
    "bash -c \"$(echo rm) -rf /\"",                  # known output, judged: a delete of /
    "bash -c \"date +$(cat f)\" && bash -c \"$(date +x)\"",
]


@pytest.mark.parametrize("cmd", SUBST_CARDED)
def test_a_shell_with_an_unknown_argument_gets_a_card_in_every_mode(monkeypatch, cmd):
    assert policy.never_run_hit(cmd) is None, cmd
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                           granted=granted)
        assert decision.tier is Tier.CONFIRM, (cmd, full_access, granted)
        assert decision.reason == policy.UNKNOWN_OUTPUT_CARD
    monkeypatch.setattr(policy, "_READ_ONLY", True)
    assert _decide(monkeypatch, cmd).tier is Tier.DENY


@pytest.mark.parametrize("cmd", SUBST_STILL_REFUSED)
def test_a_shell_whose_command_comes_from_output_is_still_refused(monkeypatch, cmd):
    assert policy.never_run_hit(cmd), cmd
    for full_access, keep_floor, granted in MODES:
        assert _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                       granted=granted).tier is Tier.DENY, (cmd, full_access, granted)


def test_a_substitution_whose_output_is_known_is_judged_as_written(monkeypatch):
    # `$(echo ls)` prints `ls`: the program is `ls`, and nothing is unknown.
    for cmd in ("bash <<< \"$(echo ls)\"", "sh -c \"$(echo ls -la)\""):
        assert policy.never_run_hit(cmd) is None
        assert _decide(monkeypatch, cmd).tier is Tier.CONFIRM
        assert _decide(monkeypatch, cmd).reason != policy.UNKNOWN_OUTPUT_CARD
    assert policy.never_run_hit("bash <<< \"$(echo reboot)\"")


def test_substitutions_outside_a_shell_program_get_no_new_card(monkeypatch):
    for cmd in ("echo \"$(date)\"", "eval \"$(brew shellenv)\"", "ls $(pwd)"):
        assert _decide(monkeypatch, cmd, full_access=True).tier is Tier.ALLOW, cmd


# --- 3. the plain-delete card -------------------------------------------------

def _card(cmd):
    decision = policy.decide("shell", {"cmd": cmd}, tool="run_shell")
    assert decision.tier is Tier.CONFIRM, (cmd, decision.reason)
    return decision.reason


def test_the_macs_own_spellings_are_not_called_links(tmp_path):
    # pytest's tmp_path sits under /var/folders, which is /private/var/folders on disk.
    target = tmp_path / "work" / "build"
    target.mkdir(parents=True)
    reason = _card(f"rm -rf {target}")
    assert "link" not in reason and "everything inside" in reason
    if os.path.exists("/tmp/"):
        assert "link" not in _card("rm -rf /tmp/wisp-audit-not-there/build")


def test_a_path_that_is_a_link_says_only_the_link_goes(tmp_path):
    real = tmp_path / "w" / "real"
    real.mkdir(parents=True)
    link = tmp_path / "w" / "link"
    link.symlink_to(real)
    reason = _card(f"rm -rf {link}")
    assert "removes the link, not what it points to" in reason
    assert "everything inside" not in reason


def test_a_path_reached_through_a_link_shows_where_it_is(tmp_path):
    real = tmp_path / "x" / "real"
    (real / "build").mkdir(parents=True)
    hop = tmp_path / "x" / "hop"
    hop.symlink_to(real)
    reason = _card(f"rm -rf {hop}/build")
    assert "reached through a link" in reason and str(real.resolve() / "build") in reason
    assert "everything inside" in reason


@pytest.mark.parametrize("cmd", [
    "rm -rf \"/tmp/a/ b\"",
    "rm -rf '/tmp/a/b /c'",
    "rm -rf /tmp/a/b\\ /c",
    "rm -rf \"/tmp/a/Docu ments/b\"",
    "rm -rf '/Users/Shared/x/Lib rary/c'",
    "rm -rf '/Volumes/Disk/Data/Applic ations/x'",
])
def test_a_name_with_edge_spaces_or_like_a_known_folder_gets_no_card(monkeypatch, cmd):
    assert policy.plain_delete_targets({"cmd": cmd}) is None, cmd
    for full_access, keep_floor, granted in MODES:
        assert _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                       granted=granted).tier is Tier.DENY, cmd


def test_an_ordinary_name_with_a_space_keeps_its_card():
    assert policy.plain_delete_targets({"cmd": "rm -rf \"/tmp/a/My Notes/b\""}) is not None


# --- time ------------------------------------------------------------------------

def test_the_new_checks_are_quick_on_long_text():
    n = policy.SHELL_TEXT_LIMIT
    texts = ["bash -c \"" + "echo $(date) " * (n // 13) + "\"",
             "bash -c \"" + "$(x)" * (n // 4) + "\"",
             "echo \"" + "$(cat f)" * (n // 8) + "\" | sh",
             "bash -c \"" + "sh -c '" * (n // 7) + "\"",
             "find . " + "-not -name x " * (n // 13) + "-delete",
             "find . \\( " + "! -name x -o " * (n // 13) + "\\) -delete",
             "mkdir -p a && " * (n // 14) + "echo 'x' >> n.txt",
             "git add a && " * (n // 13) + "git commit -m 'x'",
             "echo 'x' >> " + "a/" * (n // 2 - 10) + "n.txt",
             "rm -rf '/tmp/" + "a b/" * 990 + "'"]
    for text in texts:
        text = text[:n]
        start = time.thread_time()
        policy.inert_text_form(text)
        policy.decide("shell", {"cmd": text}, tool="run_shell")
        assert time.thread_time() - start < 1.0, text[:40]
