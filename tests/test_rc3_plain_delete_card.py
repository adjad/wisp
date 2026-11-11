"""One plain delete of an absolute path gets a card (owner decision 2026-11-10).

Nothing here is executed: each string is only handed to the policy. A card is
given only when the whole command is `rm`, its flags and plainly written paths
in ordinary folders. Every other spelling keeps the refusal it had, in every
mode, so a delete cannot ride in beside a harmless-looking one.
"""
import os

import pytest

from service.safety import grants, policy
from service.safety.policy import Tier

MODES = (
    (False, True, None),       # the default
    (True, True, None),        # Full access
    (True, False, None),       # Full access with the configurable floor off
    (False, True, "allow"),    # a saved Always allow for the shell
    (True, False, "allow"),
)

CARDED = [
    "rm -rf /tmp/build",
    "rm -rf /tmp/build/",
    "rm -r /private/tmp/wisp-test",
    "rm -fr ~/Projects/app/node_modules",
    "rm -r -f ~/proj",
    "rm -Rf ~/Projects/app/build ~/Projects/app/dist /tmp/x",
    "rm -rf {home}/Projects/app/.venv",
    "rm -rf ~/Documents/work/old-drafts",
    "rm -rf ~/Library/Caches/com.example.app",
    "rm -rf /Volumes/USB/proj/build",
    "rm -rf /Volumes/Data/Users/u/Projects/app/build",
    "rm -rf /Users/Shared/scratch",
    "rm -rf /opt/homebrew/var/thing",
    "rm -rf /usr/local/share/thing",
    "rm -rf /var/folders/ab/cdef/T/job-1",
    "  rm -rf /tmp/build  ",
]

REFUSED = [
    # A path that is not in an ordinary folder.
    "rm -rf /",
    "rm -rf ~",
    "rm -rf ~/",
    "rm -rf {home}",
    "rm -rf /tmp",
    "rm -rf /private/tmp/",
    "rm -rf ~/Documents",
    "rm -rf ~/Documents/old",
    "rm -rf ~/Desktop/thing",
    "rm -rf ~/Library/Caches",
    "rm -rf ~/.config/app",
    "rm -rf ~/.ssh/old",
    "rm -rf ~/.moe/cache",
    "rm -rf ~/Library/Keychains/login.keychain-db",
    "rm -rf /usr/local/bin",
    "rm -rf /opt/homebrew",
    "rm -rf /Volumes/USB",
    "rm -rf /Volumes/USB/old",
    "rm -rf /Volumes/Data/Users/u",
    "rm -rf /Volumes/Data/Users/u/Documents/old",
    "rm -rf /Volumes/Data/Users/u/Library/x",
    "rm -rf /Volumes/Data/etc/x",
    "rm -rf /Volumes/Data/usr/bin/x",
    "rm -rf /Volumes/Data/System/Library/x",
    "rm -rf /Volumes/Ext/Backups.backupdb/mac/2026",
    "rm -rf /Volumes/Ext/x/.timemachine/y",
    "rm -rf /opt/homebrew/Cellar",
    "rm -rf /Volumes/Recovery/a/b",
    "rm -rf /Volumes/Preboot/a/b",
    "rm -rf /Volumes",
    "rm -rf /Users/someone-else/Projects/app/build",
    "rm -rf /Users/Shared",
    "rm -rf /Applications/Thing.app",
    "rm -rf /Library/Thing/cache",
    "rm -rf /System/Volumes/Data/tmp/x",
    "rm -rf /var/folders/ab/cdef/T",
    "rm -rf /private/var/db/thing/x",
    "rm -rf /etc/thing/x",
    "rm -rf /.vol/1/2",
    "rm -rf /dev/fd/3",
    "rm -rf /home/x/y",
    "rm -rf ~root/x/y",
    "rm -rf ~+/x",
    # One good path does not carry a bad one.
    "rm -rf /tmp/build ~/Documents",
    "rm -rf /tmp/build /",
    "rm -rf ~/Documents /tmp/build",
    # Dots and upward steps.
    "rm -rf /tmp/x/../../Users",
    "rm -rf /tmp/./x",
    "rm -rf ~/Projects/..",
    # Anything beside the one plain command.
    "rm -rf /tmp/build; rm -rf ~",
    "rm -rf /tmp/build && rm -rf ~",
    "rm -rf /tmp/build || true",
    "rm -rf /tmp/build | cat",
    "rm -rf /tmp/build &",
    "rm -rf /tmp/build\nrm -rf ~",
    "rm -rf /tmp/build\n",
    "rm -rf /tmp/build\\",
    "rm -rf /tmp/build #",
    "rm -rf /tmp/build > /dev/null",
    "rm -rf /tmp/build 2>&1",
    "rm -rf /tmp/build $HOME",
    "rm -rf /tmp/build $X",
    "rm -rf /tmp/build `pwd`",
    "rm -rf /tmp/build $(pwd)",
    "rm -rf /tmp/build \"/Users\"",
    "rm -rf '/tmp/build'",
    "rm -rf /tmp/my\\ build",
    "rm -rf /tmp/*",
    "rm -rf /tmp/build?",
    "rm -rf /tmp/{a,b}",
    "rm -rf /tmp/a=b",
    "rm -rf /tmp/build\t/Users",
    "rm -rf  /tmp/build",
    "rm  -rf /tmp/build",
    "rm -rf -- /tmp/build",
    "rm -rfv /tmp/build",
    "rm --recursive /tmp/build",
    "rm -rf -P /tmp/build",
    "X=1 rm -rf /tmp/build",
    "x=/tmp/build; rm -rf /tmp/build $x",
    "alias ls='rm -rf ~'; rm -rf /tmp/build",
    "/bin/rm -rf /tmp/build",
    "\\rm -rf /tmp/build",
    "command rm -rf /tmp/build",
    "env rm -rf /tmp/build",
    "sudo rm -rf /tmp/build",
    "sh -c 'rm -rf /tmp/build'",
    "eval rm -rf /tmp/build",
    "xargs rm -rf /tmp/build",
    "time rm -rf /tmp/build",
    "(rm -rf /tmp/build)",
    "{ rm -rf /tmp/build; }",
    "rm -rf /tmp/build <<EOF\nEOF",
    "rm -rf /tmp/b\u00fcild",
    "rm -rf /tmp/build\u00a0/Users",
    "rm -rf /tmp/build\x00",
    "RM -rf /tmp/build",
    "srm -rf /tmp/build",
]

# Refused by the broad floor entry alone on main (a remote or container
# command, an odd space): not plain, so the floor still reads them.
FLOOR_REFUSED = [
    "ssh host rm -rf /tmp/build",
    "docker run -v /:/host x rm -rf /tmp/build",
    "rm\u00a0-rf /tmp/build",
]


@pytest.fixture
def home(monkeypatch, tmp_path):
    folder = tmp_path / "home"
    (folder / "Documents" / "work").mkdir(parents=True)
    (folder / "Projects" / "app").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    return folder


def _decide(monkeypatch, args, *, full_access=False, keep_floor=True, granted=None,
            read_only=False):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_READ_ONLY", read_only)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    monkeypatch.setattr(grants, "check", lambda *a, **k: granted)
    return policy.decide("shell", args, tool="run_shell")


@pytest.mark.parametrize("cmd", CARDED)
def test_one_plain_delete_in_an_ordinary_folder_gets_a_card_in_every_mode(monkeypatch, home, cmd):
    cmd = cmd.format(home=home)
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, {"cmd": cmd}, full_access=full_access,
                           keep_floor=keep_floor, granted=granted)
        # Never refused, and never run without asking: no mode or grant lifts the card.
        assert decision.tier is Tier.CONFIRM, (cmd, full_access, keep_floor, granted)


@pytest.mark.parametrize("cmd", REFUSED)
def test_every_other_spelling_keeps_its_refusal_in_every_mode(monkeypatch, home, cmd):
    cmd = cmd.replace("{home}", str(home))
    assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
    for full_access, keep_floor, granted in MODES:
        decision = _decide(monkeypatch, {"cmd": cmd}, full_access=full_access,
                           keep_floor=keep_floor, granted=granted)
        assert decision.tier is Tier.DENY, (cmd, full_access, keep_floor, granted)


@pytest.mark.parametrize("cmd", FLOOR_REFUSED)
def test_the_floor_still_refuses_what_only_it_refused(monkeypatch, home, cmd):
    assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
    for full_access, granted in ((False, None), (True, None), (False, "allow")):
        decision = _decide(monkeypatch, {"cmd": cmd}, full_access=full_access, granted=granted)
        assert decision.tier is Tier.DENY, (cmd, full_access, granted)


def test_a_link_is_judged_as_the_folder_it_leads_to(monkeypatch, home):
    os.symlink(home / "Documents", home / "Projects" / "docs")
    os.symlink(home, home / "Projects" / "app" / "up")
    os.symlink("/", home / "Projects" / "app" / "disk")
    for cmd in ("rm -rf ~/Projects/docs", "rm -rf ~/Projects/docs/",
                "rm -rf ~/Projects/docs/work", "rm -rf ~/Projects/app/up",
                "rm -rf ~/Projects/app/up/Documents", "rm -rf ~/Projects/app/up/Library/x",
                "rm -rf ~/Projects/app/disk", "rm -rf ~/Projects/app/disk/usr/local/bin",
                "rm -rf ~/Projects/app/disk/Users/someone/x/y"):
        assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
        for full_access, keep_floor, granted in MODES:
            decision = _decide(monkeypatch, {"cmd": cmd}, full_access=full_access,
                               keep_floor=keep_floor, granted=granted)
            assert decision.tier is Tier.DENY, (cmd, full_access, keep_floor, granted)
    # Through the link into an ordinary folder is the same delete as its real path.
    assert _decide(monkeypatch, {"cmd": "rm -rf ~/Projects/app/up/Projects/app/build"}
                   ).tier is Tier.CONFIRM


def test_the_card_is_only_for_a_run_shell_command(monkeypatch, home):
    script = 'do shell script "rm -rf /tmp/build"'
    assert policy.plain_delete_paths({"script": script}) is None
    assert policy.plain_delete_paths({"cmd": "rm -rf /tmp/build", "script": script}) is None
    assert policy.plain_delete_paths({"cmd": ["rm", "-rf", "/tmp/build"]}) is None
    assert policy.plain_delete_paths({}) is None
    for args in ({"script": script}, {"cmd": "rm -rf /tmp/build", "script": script},
                 {"cmd": "rm -rf /tmp/build", "script": 'do shell script "rm -rf ~"'}):
        for full_access, keep_floor, granted in MODES:
            decision = _decide(monkeypatch, args, full_access=full_access,
                               keep_floor=keep_floor, granted=granted)
            assert decision.tier is Tier.DENY, (args, full_access, keep_floor, granted)


def test_a_delete_that_is_not_recursive_is_left_as_it_was(monkeypatch, home):
    assert policy.plain_delete_paths({"cmd": "rm -f /tmp/one.log"}) is None
    assert policy.plain_delete_paths({"cmd": "rm -f -f /tmp/one.log"}) is None
    assert _decide(monkeypatch, {"cmd": "rm -f /tmp/one.log"}).tier is Tier.CONFIRM


def test_home_is_known_by_its_real_path_too(monkeypatch, tmp_path):
    real = tmp_path / "disk" / "me"
    (real / "Documents").mkdir(parents=True)
    os.symlink(real, tmp_path / "me")
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    for cmd in (f"rm -rf {real}", f"rm -rf {real}/Documents", f"rm -rf {real}/Documents/old",
                f"rm -rf {tmp_path}/me", "rm -rf ~/Documents/old"):
        assert policy.plain_delete_paths({"cmd": cmd}) is None, cmd
        for full_access, keep_floor, granted in MODES:
            decision = _decide(monkeypatch, {"cmd": cmd}, full_access=full_access,
                               keep_floor=keep_floor, granted=granted)
            assert decision.tier is Tier.DENY, (cmd, full_access, keep_floor, granted)
    assert _decide(monkeypatch, {"cmd": f"rm -rf {real}/Projects/app/build"}).tier is Tier.CONFIRM


def test_view_only_mode_still_refuses_it(monkeypatch, home):
    assert _decide(monkeypatch, {"cmd": "rm -rf /tmp/build"}, read_only=True).tier is Tier.DENY


def test_the_rest_of_the_floor_still_reads_a_plain_delete(monkeypatch, home):
    # Only the broad recursive-delete entry is skipped; a path that trips
    # another entry (here the word sudo) keeps that refusal while the floor is on.
    assert policy.plain_delete_paths({"cmd": "rm -rf /tmp/sudo"}) is not None
    assert _decide(monkeypatch, {"cmd": "rm -rf /tmp/sudo"}).tier is Tier.DENY
    # A broad entry the person added themselves is not skipped either.
    monkeypatch.setattr(policy, "_rules", lambda key, default: (
        [*default, r"\brm\b"] if key == "shell_deny" else default))
    assert _decide(monkeypatch, {"cmd": "rm -rf /tmp/build"}).tier is Tier.DENY


def test_too_many_paths_or_too_long_is_not_plain(home):
    many = "rm -rf " + " ".join(f"/tmp/x{i}" for i in range(policy._PLAIN_MAX_PATHS + 1))
    assert policy.plain_delete_paths({"cmd": many}) is None
    assert policy.plain_delete_paths({"cmd": "rm -rf /tmp/" + "a" * 5000}) is None
    assert policy.plain_delete_paths({"cmd": "rm -rf /tmp/a /tmp/b"}) == ["/tmp/a", "/tmp/b"]
