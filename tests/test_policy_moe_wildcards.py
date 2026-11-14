"""A shell wildcard that can reach a protected file in Wisp's own folder (policy._moe_glob_hit)
asks first, even under Full access; one that cannot reach the folder is left alone.

Case does not matter (APFS is case-insensitive), braces are expanded over the whole word, and,
as in the shell, `*`/`?`/`[...]` do not match a leading dot unless dotglob/GLOB_DOTS is on."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from service import paths
from service.safety import policy


@pytest.fixture(autouse=True)
def full_access(monkeypatch):
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    # The default folder, whatever WISP_HOME the test run has; nothing here touches the disk.
    monkeypatch.setattr(paths, "MOE_DIR", Path.home() / ".moe")


def _tier(cmd: str) -> str:
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier.name


PROTECTED = [
    # Different case reaches the same folder and files on APFS.
    "cp /tmp/forged ~/.MOE/act_for*", "echo x > ~/.Moe/*.json", "echo x > ~/.MOE/ACT_FOR*.JSON",
    "cd ~/.MOE && cat act*", "echo x > ~/.moe/GR?NTS.json",
    # Braces anywhere in the word, the folder included.
    "echo x > ~/.{moe,x}/act*", "echo x > ~/.mo{e,}/act*", "echo x > ~/.{l..n}oe/act*",
    "echo x > ~/{.moe,x}/gra*", "echo x > ~/.moe/{grants,act_for_me}.json",
    # Wildcards in the folder name that start with a dot, as the shell needs.
    "cat ~/.m?e/gra*", "cat ~/.*/access_mode*", "cat ~/.[m]oe/gmail*",
    # A bare wildcard after the folder was named.
    "cd ~/.moe; echo x > acc*.json", "cd ~/.moe && rm .reset*",
    # dotglob / GLOB_DOTS: `*` matches a leading dot.
    "shopt -s dotglob; cat ~/*/act_for*", "setopt globdots && cat ~/*/gra*", "setopt GLOB_DOTS; cat ~/*/mcp*",
    "GLOBIGNORE=x; cat ~/*/act*",
    # A hidden protected file needs a pattern that starts with a dot.
    "echo x > ~/.moe/.reset-p*", "echo x > ~/.moe/.*",
    # The folder's sub-folder google_workspace.
    "cat ~/.moe/goog*/token.json", "cat ~/.moe/*/token.json",
    # Every case in tests/test_acting.py's list.
    "echo x > ~/.moe/act_for*.json", "echo x > ~/.moe/act_for_me.jso?", "cp /tmp/x ~/.moe/a[c]t_for_me.json",
    "echo x > ~/.moe/*.json", "rm ~/.moe/*", "sed -i '' s/false/true/ ~/.moe/acc*", "echo x > ~/.moe/gr?nts.json",
    "echo x > ~/.m?e/act*", "cp /tmp/x ~/.*/gra*", "echo x > ~/.[m]oe/act_for_me.jso?",
    "echo x > ~/.moe/act_for_me.{json,bak}", "cd ~/.moe && cat act*",
]

# Spelled out in a different case: caught by the literal _SHELL_SECRET patterns (compiled
# case-insensitively), so these never needed a wildcard rule.
LITERAL = ["echo x > ~/.MOE/act_for_me.json", "echo x > ~/.moe/ACT_FOR_ME.JSON", "echo x > ~/.Moe/Grants.json",
           "echo x > ~/.moe/Access_Mode.json", "cp /tmp/x ~/.MOE/GMAIL.JSON", "cp /tmp/x ~/.moe/MCP.json",
           "touch ~/.MOE/.RESET-PENDING", "rm -r ~/.Moe/Google_Workspace"]

UNTOUCHED = [
    "cat */*.json", "jq . data/*/*.json", 'prettier --check "src/**/*.json"', "rg -g '**/*.json' foo",
    "rm -rf ~/.cache/*/*", "ls *.json", "cat ~/Documents/act_for*.json", "cd ~/Documents && cat act*",
    "cd ~/x.moe-old && ls *.json", "echo x > ~/.moe/notes*.txt", "echo x > ~/.moe/meetings/{a,b}.json",
    "ls ~/.config/*/*.json", "echo x > ~/.moe/meetings/*.json", "cat ~/?moe/act*", "cat ~/[.]moe/act*",
    "ls ~/.moe/*.txt", "cat ~/.moe/*-pending",
]


@pytest.mark.parametrize("cmd", PROTECTED)
def test_a_wildcard_that_can_reach_a_protected_file_asks_first(cmd):
    assert policy._moe_glob_hit(cmd) == "a protected Wisp file (wildcard)", cmd
    assert _tier(cmd) in ("CONFIRM", "DENY"), cmd


# Routes a review found past the first version of this check. Each wrote the gate's file with no card.
REVIEW_ROUTES = [
    # `..` was never resolved.
    "cp /tmp/forged ~/.moe/cache/../act_for_me.js?n",
    "tee ~/.moe/skills/../act_for_me.js{o,}n",
    "cd ~/.moe/cache && cp /tmp/f ../act_for_m?.json",
    "cp /tmp/f ~/Documents/../.moe/gra*",
    "cd ~/.moe && cat sub/../act*",
    # Brackets that fnmatch reads differently from the shell.
    "cp /tmp/f ~/.moe/act_for_me[^x]json",
    "cp /tmp/f ~/.moe/act_for_me[!x]json",
    "cp /tmp/f ~/.moe/act_for_me[[:punct:]]json",
    "cp /tmp/f ~/.moe/[[:alpha:]]ct_for_me.json",
    "cp /tmp/f ~/.moe/act_for_me[[.x.]]json",
    "cp /tmp/f ~/.moe/act_for_me[[=x=]]json",
    "cp /tmp/f ~/.[^x]oe/act*",
    "cp /tmp/f ~/.moe/act_for_me[]x.]json",
    # A quoted or escaped separator inside braces cut the word before expansion.
    'tee ~/.moe/act_for_me.{json,"a b"} </tmp/f',
    "tee ~/.moe/act_for_me.{json,a\\ b} </tmp/f",
    'tee ~/.moe/act_for_me.{json,"("} </tmp/f',
    'tee ~/.{moe,"x y"}/act* </tmp/f',
    'cd ~/.moe && tee act_for_me.{json,"a b"} </tmp/f',
    # An expansion over the cap in a word with no dot, the folder named elsewhere.
    "cd ~/.{moe,x} && tee act_for_me?json{,{0..300}} </tmp/f",
    "cd ~/.moe && tee act_for_me?json{,{0..300}} </tmp/f",
    # A relative wildcard was judged only by its last segment.
    "cd ~/.moe/cache && cp x ../*/token.json",
    "cd ~/.moe/cache && cp x ../goog*/token.json",
    "cd ~/.moe/cache; cd ..; cp x */token.json",
    "pushd ~/.moe/cache; cp x ../g*/token.json",
    "cd ~/.moe && cat g*/token.json",
    # `~+` (the current folder) and `~-` (the previous one) were taken for absolute paths.
    "cd ~/.moe/cache && cd .. && cp x ~+/act*",
    "cd ~/.moe/cache && cd .. && cd /tmp && cp x ~-/act*",
    "cd ~/.moe && cp x ~+/gr?nts.json",
    # zsh reads (a|b) as alternatives with no option set; the word break used to cut it out.
    "zsh -c 'echo x > ~/.moe/(act_for_me|x).json'",
    "zsh -c 'echo x > ~/.moe/act_for_me.js(o)n'",
    "zsh -c 'echo x > ~/.moe/(gmail).json'",
    "zsh -c 'echo x > ~/.(moe|x)/act*'",
    "zsh -c 'echo x > ~/.moe/((act_for_me|y)|x).json'",
    # Reading a group as "any text" must not hide a wildcard inside a subshell or `$(…)`.
    "({cp,/tmp/f,~/.moe/act_for_me.js?n})",
    "zsh -c '(cat>~/.moe/act_for_me.js?n)</tmp/f'",
    "zsh -c '(tee>~/.moe/gr?nts.json)</tmp/f'",
    "zsh -c '(cp</tmp/f>~/.moe/goog*/token.json)'",
    "echo $(<~/.moe/act*)",
    # A zsh group with a space or a quote in it.
    "zsh -c 'echo x > ~/.moe/(act_for_me| x).json'",
    "zsh -c 'echo x > ~/.moe/(x| act_for_me).json'",
    "zsh -c 'echo x > ~/.moe/(act_for_me|x\\ y).json'",
    """zsh -c 'echo x > ~/.moe/(act_for_me|"x y").json'""",
    "zsh -c 'echo x > ~/.moe/(gmail| x).json'",
    "zsh -c 'echo x > ~/.moe/(grants| a).json'",
    # "Not now" sits with the gate.
    "echo x > ~/.moe/act_for_me_asked.json",
    "echo x > ~/.moe/act_for_me_ask*",
]

# Ordinary commands that the repairs above must not start catching.
STILL_UNTOUCHED = [
    "awk '{print $1}' notes.txt",
    "awk '{print $1.x}' data/*.json",
    """curl -s -d '{"path": "a.b", "n": 1.5}' https://example.com""",
    "ls ../*.json",
    "cat ../act*",
    "cp a[^x]b.json /tmp/",
    "echo x{,{0..300}}",
    "echo x > ~/.moe/meetings/../notes*.txt",
    "ls ~/Documents/[[:alpha:]]*.json",
    "cp x ../*/token.json",
    "cp x ~+/act*",
    "echo $(date) > out/(a|b).txt",
    "(ls) && cat sub/*/notes.json",
    "zsh -c 'ls ~/Documents/(a|b).json'",
]


@pytest.mark.parametrize("cmd", PROTECTED)
def test_what_asked_before_groups_were_read_still_asks_on_the_first_reading_alone(cmd):
    """The second reading (groups as "any text") is only ever added to the first. Every command in
    PROTECTED asked before that reading existed, and must still be caught without it."""
    dotglob = bool(policy._SHELL_DOTGLOB.search(cmd))
    assert policy._moe_glob_pass(cmd.casefold(), dotglob) is not None, cmd
    assert _tier(cmd) in ("CONFIRM", "DENY"), cmd


def test_the_group_reading_never_replaces_the_plain_one(monkeypatch):
    # With the second reading taken away, a subshell's wildcard still asks; with only the
    # second reading, it would not. So the first can never be dropped for the second.
    cmd = "zsh -c '(cat>~/.moe/act_for_me.js?n)</tmp/f'"
    assert policy._moe_glob_pass(cmd, False) is not None
    assert policy._moe_glob_pass(policy._GLOB_GROUP.sub(policy._GLOB_ANY, cmd), False) is None
    assert policy._moe_glob_hit(cmd) is not None


@pytest.mark.parametrize("cmd", REVIEW_ROUTES)
def test_the_routes_a_review_found_all_ask_first(cmd):
    assert _tier(cmd) in ("CONFIRM", "DENY"), (cmd, _tier(cmd))


@pytest.mark.parametrize("cmd", STILL_UNTOUCHED)
def test_ordinary_commands_stay_untouched_after_the_repairs(cmd):
    assert policy._moe_glob_hit(cmd) is None, cmd


def test_a_bracket_is_read_as_any_one_character():
    assert policy._glob_brackets("a[^x]b[[:alpha:]]c[]x]d[!]]e") == "a?b?c?d?e"
    assert policy._glob_brackets("a[b") == "a[[]b" and policy._glob_brackets("[") == "[[]"
    assert policy._glob_segment_matches("act_for_me.json", "act_for_me[^x]json", False)
    assert not policy._glob_segment_matches(".moe", "[.]moe", False)          # no leading dot, as in the shell
    assert policy._glob_segment_matches(".moe", ".[^x]oe", False)


def test_the_gates_asked_file_is_a_protected_root():
    assert any(str(root).endswith("/act_for_me_asked.json") for root in policy._credential_roots())


# Names built at run time: no reading of the command's text can catch these. What stands between
# them and the gate's file is not this check.
BUILT_AT_RUN_TIME = [
    "f=act_for_me; echo '{}' > ~/.moe/$f.json",
    'python3 -c "import os;open(os.path.expanduser(\'~/.moe/act_for_m\'+\'e.json\'),\'w\').write(\'{}\')"',
    "echo x > ~/.moe/act_for_me$'\\x2e'json",
    "find ~ -name 'act_for*' -exec cp /tmp/f {} \\;",
]


@pytest.mark.parametrize("cmd", BUILT_AT_RUN_TIME)
def test_a_name_built_at_run_time_asks_with_full_access_off_and_on_a_model_led_turn(cmd, monkeypatch):
    # Full access OFF (Ask mode; the packaged setting is Full access ON): every shell command that
    # is not a fixed read asks, with the command shown.
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    assert _tier(cmd) == "CONFIRM", cmd
    # Full access, the bare policy layer: allowed. This is the known limit of reading text.
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    allowed = policy.decide("shell", {"cmd": cmd}, tool="run_shell")
    assert allowed.tier.name == "ALLOW", cmd
    # Full access on a model-led turn (agent/loop.py applies this to every call): a shell command
    # the model chose is always confirmed, so these still show a card.
    again = policy.opaque_outbound_decision("run_shell", "shell", {"cmd": cmd}, allowed, reason="model-led",
                                            categories=policy.OPAQUE_ACTION_CATEGORIES)
    assert again.tier.name == "CONFIRM", cmd


@pytest.mark.parametrize("cmd", LITERAL)
def test_a_protected_file_spelled_in_another_case_asks_first(cmd):
    assert policy.shell_secret_hit({"cmd": cmd}) is not None, cmd
    assert _tier(cmd) in ("CONFIRM", "DENY"), cmd


@pytest.mark.parametrize("cmd", UNTOUCHED)
def test_a_wildcard_that_cannot_reach_wisps_folder_is_left_alone(cmd):
    assert policy._moe_glob_hit(cmd) is None, cmd
    assert policy.shell_secret_hit({"cmd": cmd}) is None, cmd


def test_a_custom_wisp_folder_without_a_dot_can_be_matched_by_any_wildcard(monkeypatch):
    # WISP_HOME=/tmp/wisphome: the shell's `*` CAN match "wisphome" (no leading dot), so a
    # wildcard that fits that name reaches its protected files. A wildcard that cannot be the
    # folder's name (`other*`) cannot reach them. The default ~/.moe stays protected as well.
    monkeypatch.setattr(paths, "MOE_DIR", Path("/tmp/wisphome"))
    for cmd in ("cat /tmp/wisph*/act*", "cat /tmp/*/gra*", "cat /tmp/WISPHOME/acc*",
                "cd /tmp/wisphome && cat act*", "cat ~/.moe/act*"):
        assert policy._moe_glob_hit(cmd) is not None, cmd
        assert _tier(cmd) in ("CONFIRM", "DENY"), cmd
    for cmd in ("cat /tmp/other*/act*", "cat /tmp/wisphome-old/act*", "cat /tmp/wisph*/notes*"):
        assert policy._moe_glob_hit(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "echo " + "{a,b}" * 40,
    "echo ~/.m" + "{a,b}" * 40 + "/act*",
    "echo " + "{a}" * 1300,
    "echo " + "a* " * 33_000,
    "cat " + "x/" * 50_000 + "*",
    "cd ~/.moe && ls " + " ".join(f"q{i}*" for i in range(12_000)),
    "echo {1..100000000}/.moe/act*",
    "cat " + "*a" * 50_000,
])
def test_a_pathological_command_is_judged_quickly(cmd):
    started = time.process_time()
    policy._moe_glob_hit(cmd)
    assert time.process_time() - started < 2.0, len(cmd)


def test_an_expansion_too_big_to_follow_that_could_be_the_folder_asks():
    assert policy._moe_glob_hit("echo ~/.m" + "{a,b}" * 40 + "/act*") is not None
    assert policy._moe_glob_hit("echo {1..100000000}/.moe/act*") is not None


def test_this_file_is_in_the_simulation_manifest():
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_policy_moe_wildcards.py"' in manifest
