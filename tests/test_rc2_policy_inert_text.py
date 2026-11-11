"""Text that only mentions a refused command is not refused by the floor (rc2 fix).

Nothing here is executed: each string is only handed to the policy. A few
whole-command shapes whose quoted text is only ever data (a commit message, a
line echoed into a notes file, a quoted heredoc into a notes file) have that
text blanked before the configurable floor reads the rest. Every way of
running text still gets the refusal it had.
"""
import time

import pytest

from service.safety import grants, policy
from service.safety.policy import Tier

# Refused by the old floor only because the quoted data names a blocked word.
DATA_ONLY = [
    "git commit -m \"Stop the reboot loop when the Mac wakes\"",
    "git commit -m 'Do not halt the timer; never run sudo here'",
    "git commit -am 'Fix the shutdown banner'",
    "git commit -a -m \"Handle killall\" -m 'Second paragraph about nvram'",
    "git commit --no-verify -m 'Explain why rm -rf / is refused'",
    "git commit -m 'Line one about reboot\n\nLine two about halt'",
    "cd proj && git add -A && git commit -m 'reboot fix'",
    "echo 'remember to reboot after the update' >> notes.txt",
    "echo \"sudo is not needed for this\" > notes/todo.md",
    "echo -n 'halt the timer' >> log/today.log",
    "cat > notes.md <<'EOF'\nreboot after the update\nrun sudo softwareupdate later\nEOF",
    "cat <<'EOF' >> notes/ideas.txt\nhalt and catch fire\nEOF\n",
    "git commit -F - <<'EOF'\nStop the reboot loop\n\nIt did not halt.\nEOF",
]

# Still refused in the default mode: the text can run, or the shape is not one
# of the narrow data-only ones.
STILL_REFUSED = [
    "sh -c 'reboot'",
    "bash -c \"sudo ls\"",
    "eval 'reboot'",
    "echo reboot | sh",
    "echo 'reboot' | sh",
    "echo 'reboot' | xargs sudo",
    "python3 -c \"import os; os.system('reboot')\"",
    "osascript -e 'do shell script \"reboot\"'",
    "git commit -m \"$(reboot)\"",
    "git commit -m \"`reboot`\"",
    "git commit -m 'x' && reboot",
    "git commit -m 'x'; reboot",
    "git commit -m 'x' | sudo tee y",
    "sudo git commit -m 'x'",
    "git -c core.editor=reboot commit -m 'x'",
    "git commit --template=x -m 'reboot'",
    "git commit -m 'reboot' -- reboot",
    "git commit 'reboot'",
    "git commit -m reboot",
    "git commit -m \"reboot $HOME\"",
    "git commit -m \"reboot\\\" ; reboot\"",
    "git commit -m 'a'\"reboot\"",
    "echo 'reboot' >> run.sh",
    "echo 'reboot' >> notes",
    "echo 'reboot' >> .zshrc",
    "echo 'reboot' >> .config/notes.txt",
    "echo 'reboot' >> ../notes.txt",
    "echo 'reboot' >> /etc/notes.txt",
    "echo 'reboot' >> ~/notes.txt",
    "echo 'reboot' >> Library/LaunchAgents/x.plist",
    "echo 'reboot' 'halt' >> notes.txt",
    "echo -x 'reboot' >> notes.txt",
    "echo 'reboot' >> notes.txt && reboot",
    "echo 'reboot' >> notes.txt\nreboot",
    "echo reboot >> notes.txt",
    "echo 'reboot' > notes.txt 2>&1",
    "cat > notes.md <<EOF\nreboot\nEOF",
    "cat > notes.md << 'EOF'\nreboot\nEOF",
    "cat > notes.md <<-'EOF'\nreboot\nEOF",
    "cat > run.sh <<'EOF'\nreboot\nEOF",
    "bash <<'EOF'\nreboot\nEOF",
    "sh > notes.md <<'EOF'\nreboot\nEOF",
    "cat <<'EOF' | sh\nreboot\nEOF",
    "cat > notes.md <<'EOF'\nhello\nEOF\nreboot",
    "cat > notes.md <<'EOF'\nreboot\n",
    "cat > notes.md <<'EOF' && reboot\nhi\nEOF",
    "git commit -F - <<'EOF' && reboot\nmsg\nEOF",
]

MODES = ((False, True, None), (True, True, None), (False, True, "allow"))


@pytest.fixture(autouse=True)
def _modes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(policy, "_READ_ONLY", False)


def _decide(monkeypatch, cmd, *, full_access=False, keep_floor=True, granted=None):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    monkeypatch.setattr(grants, "check", lambda *a, **k: granted)
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell")


@pytest.mark.parametrize("cmd", DATA_ONLY)
def test_text_that_only_mentions_a_command_is_not_refused(monkeypatch, cmd):
    # The old floor refused it: the raw text names a blocked word.
    assert policy._any(policy._SHELL_DENY, cmd)
    assert policy.inert_text_form(cmd) is not None, cmd
    assert policy.never_run_hit(cmd) is None
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, cmd, full_access=full_access, keep_floor=keep_floor,
                           granted=granted)
        assert decision.tier is not Tier.DENY, (cmd, full_access, granted, decision.reason)
    # The default mode still asks before it runs.
    assert _decide(monkeypatch, cmd).tier is Tier.CONFIRM


@pytest.mark.parametrize("cmd", STILL_REFUSED)
def test_text_that_can_run_is_still_refused(monkeypatch, cmd):
    assert policy.inert_text_form(cmd) is None, cmd
    assert _decide(monkeypatch, cmd).tier is Tier.DENY, cmd


def test_the_rest_of_the_line_is_still_read_by_the_floor(monkeypatch):
    # The prefix steps are plain words the floor reads as before.
    for cmd in ("sudo true && git commit -m 'x'", "killall Dock && echo 'x' >> notes.txt",
                "nvram boot-args && cat > notes.md <<'EOF'\nx\nEOF",
                "reboot && cat > notes.md <<'EOF'\nhi\nEOF", "xargs reboot && echo 'x' >> notes.txt"):
        assert policy.inert_text_form(cmd) is not None, cmd
        assert _decide(monkeypatch, cmd).tier is Tier.DENY, cmd


def test_the_never_run_check_still_reads_the_original_text(monkeypatch):
    # A data shape after a step the never-run check refuses.
    cmd = "cd / && rm -rf usr && git commit -m 'x'"
    assert _decide(monkeypatch, cmd, full_access=True, keep_floor=False).tier is Tier.DENY


def test_a_long_tricky_line_is_judged_quickly():
    for text in ("git commit " + "-m 'x' " * 3000, "echo " + "'a' " * 5000 + ">> n.txt",
                 "cat > n.md <<'EOF'\n" + "reboot\n" * 2500 + "EOF",
                 "git commit -m '" + "a" * 15000, "echo \"" + "a" * 15000,
                 "cd x && " * 2000 + "git commit -m 'x'"):
        text = text[:policy.SHELL_TEXT_LIMIT]
        start = time.thread_time()
        policy.inert_text_form(text)
        policy.decide("shell", {"cmd": text}, tool="run_shell")
        assert time.thread_time() - start < 1.0, text[:40]
