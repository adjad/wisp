"""Second round of repairs for the rc2 policy audit notes.

Nothing here is executed: each string is only handed to the policy.
"""
import time

import pytest

from service.safety import grants, policy
from service.safety.policy import Tier
from tests.test_rc3_dangerous_commands import ORDINARY, REFUSED

MODES = ((False, True), (True, True), (True, False))
# An operator character as a word of its own, quoted or escaped: an argument, never an operator.
EXTRA_WORDS = ("'('", "')'", '"("', '")"', "\\(", "\\)", "'|'", "'&'", "'>'", "'<'", '"|"', "\\|", "\\&", "\\>")


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


def _tier(monkeypatch, cmd, full_access=False, keep_floor=True):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", keep_floor)
    return policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier


def _simple(cmd):
    """One plain command of three or more words: an extra word can go before its last."""
    return len(cmd.split()) >= 3 and not any(c in cmd for c in ";|&<>()$`'\"\\\n{}*?[]~")


def _with(base, extra):
    head, last = base.rsplit(" ", 1)
    return f"{head} {extra} {last}"


# Refused as written and with a plain extra word before the last one, so a
# quoted operator there must change nothing either.
SIMPLE_REFUSED = [c for c in REFUSED if _simple(c) and policy.never_run_hit(c)
                  and policy.never_run_hit(_with(c, "x"))]


# --- 1. a quoted or escaped operator character is a word ----------------------

def test_there_are_plain_refused_commands_to_vary():
    assert len(SIMPLE_REFUSED) >= 8


@pytest.mark.parametrize("extra", EXTRA_WORDS)
def test_an_extra_quoted_operator_word_hides_nothing(monkeypatch, extra):
    for base in SIMPLE_REFUSED:
        cmd = _with(base, extra)
        assert policy.never_run_hit(cmd), cmd
        for full_access, keep_floor in MODES:
            assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, (cmd, full_access, keep_floor)


def test_the_words_after_a_quoted_parenthesis_belong_to_the_same_command():
    segments = policy._nr_segments("printf ')' Documents '(' \\) \"|\" 'a;b' x\\&y")
    assert [s["words"] for s in segments] == [["printf", ")", "Documents", "(", ")", "|", "a;b", "x&y"]]
    # Unquoted, each is still an operator.
    assert [s["words"] for s in policy._nr_segments("a b ; c | d && (e)")] == [["a", "b"], ["c"], ["d"], ["e"]]
    out = policy._nr_segments("echo hi > 'a>b' ")
    assert out[0]["words"] == ["echo", "hi"] and out[0]["out"] == [("a>b", False)]


def test_a_find_delete_from_home_grouped_in_quoted_parentheses_is_refused(monkeypatch):
    for cmd in ("find ~ '(' -true ')' -delete", "find . \\( -true \\) -delete", "find ~ \"(\" -type f \")\" -delete"):
        assert policy.never_run_hit(cmd), cmd
        for full_access, keep_floor in MODES:
            assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, cmd


def test_a_grouped_find_with_a_filter_in_each_branch_keeps_its_card(monkeypatch):
    for cmd in ("cd proj && find . \\( -name '*.pyc' -o -name '*.pyo' \\) -delete",
                "cd proj && find . '(' -name '*.tmp' -o -name '*.bak' ')' -delete"):
        assert policy.never_run_hit(cmd) is None, cmd
        assert _tier(monkeypatch, cmd) is Tier.CONFIRM, cmd


@pytest.mark.parametrize("cmd", [
    "grep '(foo)' notes.txt", "grep -c ')' notes.txt", "echo \"(note)\"", "echo '|' ok",
    "git commit -m \"fix (scope): tidy\"", "find . \\( -name a -o -name b \\) -print",
    "awk '{print $1}' data.csv", "sed 's/(a)/b/' in.txt", "(cd proj && make)", "printf '%s\\n' '&' '>'",
    "ls 'a (copy).txt'", "git log --grep='(wip)'",
])
def test_everyday_quoted_operators_are_not_refused(monkeypatch, cmd):
    assert policy.never_run_hit(cmd) is None, cmd
    assert _tier(monkeypatch, cmd) is not Tier.DENY, cmd


def test_ordinary_commands_stay_ordinary(monkeypatch):
    for cmd in ORDINARY:
        assert policy.never_run_hit(cmd) is None, cmd


# --- 2. an echo with an option is not known output ----------------------------

def test_only_a_plain_echo_has_known_output():
    assert policy._nr_literal_output("echo ls -la") == "ls -la"
    for body in ("echo -e ls", "echo -n ls", "echo -ne ls", "echo -E ls", "echo =ls", "echo ls =x", "echo -"):
        assert policy._nr_literal_output(body) is None, body


@pytest.mark.parametrize("option", ["-e", "-ne", "-E", "-n"])
def test_a_shell_fed_an_echo_with_an_option_is_refused(monkeypatch, option):
    for cmd in (f"bash -c \"$(echo {option} reboot)\"", f"bash -c 'sh -c \"$(echo {option} reboot)\"'",
                f"bash <<< \"$(echo {option} reboot)\"", "zsh -c \"$(echo =reboot)\""):
        assert policy.never_run_hit(cmd), cmd
        for full_access, keep_floor in MODES:
            assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, (cmd, full_access, keep_floor)


# --- 3. unknown output inside a shell program ---------------------------------

@pytest.mark.parametrize("cmd", [
    "sh -c \"ls $(printf x)\"", "sh -c \"echo $(cat f)\"", "sh -c \"echo $(date +x)\"",
    "bash -c \"find . -exec $(cat f) \;\"", "bash -c \"trap $(cat f) EXIT\"", "bash -c \"alias x=$(cat f)\"",
    "bash -c \"awk $(cat f) data\"", "bash -c \"git -c core.pager=$(cat f) log\"",
    "bash -c \"BASH_ENV=$(cat f) true\"", "bash -c \"tar --to-command=$(cat f) -xf a.tar\"",
    "bash -c \"rsync -e $(cat f) a b\"", "bash -c \"vim -c $(cat f) x\"", "bash -c \"echo hi > $(cat f)\"",
    "sh -c \"make -C $(curl -s example.com)\"", "bash <<< \"echo $(head -1 f)\"",
])
def test_a_shell_program_holding_opaque_output_is_refused(monkeypatch, cmd):
    assert policy.never_run_hit(cmd), cmd
    for full_access, keep_floor in MODES:
        assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, (cmd, full_access, keep_floor)


@pytest.mark.parametrize("cmd", [
    "sh -c \"make -C $(git rev-parse --show-toplevel)\"", "bash -c \"cd $(pwd) && make\"",
    "zsh -c \"ls $(brew --prefix)/bin\"", "bash -c \"echo $(date)\"", "sh -c \"make -j$(nproc)\"",
])
def test_a_place_the_machine_picks_keeps_the_card_in_every_mode(monkeypatch, cmd):
    assert policy.never_run_hit(cmd) is None, cmd
    for full_access, keep_floor in MODES:
        assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.CONFIRM, (cmd, full_access, keep_floor)
    assert policy.decide("shell", {"cmd": cmd}, tool="run_shell").reason == policy.UNKNOWN_OUTPUT_CARD


def test_a_place_output_in_a_command_position_is_still_refused():
    for cmd in ("bash -c \"$(pwd)\"", "bash -c \"eval $(git rev-parse --show-toplevel)\"", "sh -c \"$(mktemp) x\""):
        assert policy.never_run_hit(cmd), cmd


# --- timing -------------------------------------------------------------------

def test_long_runs_of_quoted_operators_are_judged_quickly():
    n = policy.SHELL_TEXT_LIMIT
    for text in ("rm -rf " + "')' " * n, "find . " + "'(' " * n, "echo " + "\\( " * n, "x " + "'|' '&' " * n,
                 "bash -c \"" + "$(echo -e a) " * n, "sh -c \"" + "$(pwd) " * n):
        start = time.thread_time()
        policy.never_run_hit(text[:n])
        assert time.thread_time() - start < 1.0, text[:40]


# --- 4. the stand-in characters, a `;` between quoted words, a redefined echo ---

STAND_INS = [chr(0xF2A0 + k) for k in range(16)]


@pytest.mark.parametrize("mark", STAND_INS)
def test_a_stand_in_character_in_the_input_is_never_an_operator(monkeypatch, mark):
    # The shell reads it as an ordinary character, so the words after it still count.
    for base in SIMPLE_REFUSED:
        plain = _with(base, mark)
        for cmd in (plain, f'sh -c "{plain}"', f'eval "{plain}"', f'bash <<< "{plain}"'):
            assert policy.never_run_hit(cmd), repr(cmd)
            for full_access, keep_floor in MODES:
                assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, repr(cmd)


def test_a_stand_in_character_stays_inside_its_word():
    words = policy._nr_segments(f"printf a{STAND_INS[0]}b {STAND_INS[5]} c")[0]["words"]
    assert words == ["printf", "a_b", "_", "c"]


def test_a_semicolon_between_two_quoted_words_still_ends_the_command(monkeypatch):
    assert [s["words"] for s in policy._nr_segments("echo 'a';'b' c")] == [["echo", "a"], ["b", "c"]]
    for base in SIMPLE_REFUSED:
        first, rest = base.split(" ", 1)
        cmd = f"echo 'a';'{first}' {rest}"
        assert policy.never_run_hit(cmd), cmd
        for full_access, keep_floor in MODES:
            assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, cmd


def test_a_quoted_semicolon_word_is_a_find_root_not_the_end_of_the_roots(monkeypatch):
    for cmd in ("find ';' ~ -delete", "find \; / -delete", "find \";\" ~ -type f -delete"):
        assert policy.never_run_hit(cmd), cmd
        for full_access, keep_floor in MODES:
            assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, cmd
    # The -exec terminator still works in every spelling.
    for cmd in ("cd proj && find . -name '*.o' -exec rm {} \;", "cd proj && find . -name '*.o' -exec rm {} ';'"):
        assert policy.never_run_hit(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "echo(){ printf x; }; bash -c \"$(echo hi)\"", "function echo { printf x; }; bash <<< \"$(echo hi)\"",
    "echo () { printf x; } && sh -c \"$(echo ls)\"", "alias echo='printf x'; zsh -c \"$(echo hi)\"",
    "enable -n echo; bash -c \"$(echo hi)\"", "date(){ printf x; }; bash -c \"$(date)\"",
    "pwd(){ printf x; }; bash -c \"cd $(pwd) && make\"",
])
def test_a_redefined_command_has_no_known_output(monkeypatch, cmd):
    assert policy.never_run_hit(cmd), cmd
    for full_access, keep_floor in MODES:
        assert _tier(monkeypatch, cmd, full_access, keep_floor) is Tier.DENY, cmd


def test_python_code_with_an_escaped_operator_is_judged_as_python_reads_it(monkeypatch):
    for esc in ("\\(", "\\)", "\\|", "\\&"):
        cmd = f"python3 -c \"import os; os.system('rm -rf {esc} ~')\""
        assert policy.never_run_hit(cmd), cmd


def test_git_rev_parse_is_a_place_only_with_its_path_flags(monkeypatch):
    assert policy.never_run_hit("sh -c \"make -C $(git rev-parse --show-toplevel)\"") is None
    for flag in ("--sq-quote", "--parseopt", "--local-env-vars", "--abbrev-ref"):
        assert policy.never_run_hit(f"sh -c \"x $(git rev-parse {flag})\""), flag
