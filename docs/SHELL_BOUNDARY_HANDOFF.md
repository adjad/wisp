# C-1 view-only shell boundary repair

## Trigger, base and ownership

The Release Auditor independently executed temporary marker writes through
view-only policy-approved shell strings. Its final review of [report PR #9](https://github.com/adjad/wisp/pull/9)
at `1050cf932069136d5cd0237e9fd772cc2c204636` confirmed C-1 as urgent, with P1
priority acknowledged by the Orchestrator. Evidence is in Release Auditor task
`01a08523-47c7-76e3-be42-aee801a314fd`, turn
`01a0853c-66f3-7300-af90-570d3ed65243`.

The Orchestrator explicitly activated this as the Maintainer's sole production
repair after freezing C-2. This branch does not contain the C-2 repair.

- Clean base and final refreshed `origin/main`: `51fa3ec937df7a19961fbb2103a7654bb058ec74`.
- Branch: `codex/maintainer-c1-shell-boundary`.
- Worktree: `035d/MOE_Project`.
- Maintainer task: `01a08523-1944-7570-825f-adac9874b7d3`.
- Final SHA and draft PR URL accompany the Orchestrator handoff.

## Outcome

An allowlisted command-name prefix no longer establishes read-only permission.
Policy validates the complete argument vector for a small set of simple system
utilities and attaches that vector to `Decision.shell_argv`. `run_shell` checks
policy again immediately before execution and passes this exact vector to
`subprocess.run(shell=False)`. The executable is a fixed OS path, standard input
is disconnected with `DEVNULL`, and the child receives only a fixed system PATH
and C locale. PATH replacements, shell startup files and loader environment
variables cannot change implicit execution.

The implicit subset is intentionally narrow:

| Utility | Validated arguments |
| --- | --- |
| `pwd` | No arguments or combinations of `-L`, `-P` |
| `whoami` | No arguments |
| `uname` | No arguments or combinations of `-a`, `-m`, `-n`, `-p`, `-r`, `-s`, `-v` |
| `echo` | Literal argument text within the syntax restrictions below |
| `ls` | File operands and combinations of `-a`, `-A`, `-h`, `-l`, `-1`, `-d`, `-F` |
| `cat` | File operands and combinations of `-b`, `-e`, `-n`, `-s`, `-t`, `-u`, `-v` |
| `wc` | File operands and combinations of `-c`, `-l`, `-m`, `-w` |
| `head`, `tail` | File operands and `-n`/`-c` counts of 1–6 ASCII digits, separate or attached; no follow mode |

Only canonical names and their explicitly mapped system paths are accepted.
Shell composition, redirection, comments, control characters, variable/command
substitution, globbing and tilde expansion are refused even when quoted.
Quotes and backslashes can group otherwise literal arguments. Relative file
operands are anchored to the same home directory the tool already uses as its
working directory. Empty operands are rejected. `--` allows dash-prefixed
literal filenames without letting them become options. `cat -` and `wc -`
read empty standard input; bare `-` for `head`/`tail` is refused because BSD/GNU
interpret it differently (use `./-` for the file). Unknown options are rejected
even after an earlier filename, until `--` terminates options.

Utilities with broader execution or mutation surfaces, including git, find,
rg, env, interpreters, pagers and date, get no implicit allowance. Existing
`shell_allow`/`shell_mutate` configuration can restrict the validated subset
but cannot expand it.

Explicit confirmed actions, full access, and existing standing grants retain
their original shell semantics and environment. A standing grant remains an
explicit mode override under the existing grant policy; changing that authority
is outside C-1. The caller still owns confirmation before invoking a CONFIRM
action. The executor rechecks DENY, including mode changes and explicit denial,
but this is not a redesign of approval tokens or a general OS sandbox.

## Changed files

- `service/safety/policy.py`: shell argument validation and the decision's execution contract.
- `service/tools/builtin.py`: `run_shell` description, denial recheck, and direct-argv execution.
- `tests/test_shell_boundary.py`: new fixture regressions.
- `docs/SHELL_BOUNDARY_HANDOFF.md`: this handoff.

H-1 grants, H-2 protected paths, MCP policy, routing and migrations are unchanged.
No overlap with other active writer scopes was found. The frozen C-2 branch
remains at `8c1759559b3630f2d0111a7dcbc0ec7dd5b06a00`.

## Validation

All checks used fresh temporary `WISP_HOME` before service imports. New shell
tests additionally replace `Path.home()` with a disposable fixture directory.
Mutating test payloads write only temporary markers; system-mutating examples
are policy-only. Agent execution uses a scripted client and mocked approval;
there are no live models, external communications, real account mutations,
credential operations or installed-app changes.

Runtime: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python`.

| Check | Result |
| --- | --- |
| Original source with new `test_auditor_marker_bypasses_cannot_execute` | All five subcases reproduced unauthorized marker writes: env delegation, version chain, echo chain, substitution, find-exec |
| `tests/test_shell_boundary.py -v` | 12 tests passed, including adversarial input matrices, real accepted reads, PATH/environment attack fixtures and actual agent confirmation dispatch |
| `tests/test_destructive_shell.py` | 29 checks passed |
| `tests/test_tool_dispatch.py` | 8 checks passed |
| `tests/test_tool_test_mode.py` | 14 checks passed |
| `tests/test_tool_outcomes.py` | 6 checks passed |
| `pytest -q -rs -p no:cacheprovider tests/test_replay_failure_fixes.py` | 51 tests passed |
| `git diff --check` | Passed |

No unexpected failures or skips remain in final checks. During fixture
development, the host demonstrated that BSD head/tail treat `-` as a filename;
the final contract rejects that ambiguous operand and tests the explicit
literal-file spelling. A bounded read-only helper reviewed the implementation;
that guidance does not substitute for independent release review.

Run the new suite directly; it sets its own fresh temporary `WISP_HOME` before
imports. Set a fresh `WISP_HOME` in the launching environment for every existing
suite above, especially the destructive-shell script, which writes fixture
grants. Run the four script-style suites as scripts because their manual
failure counters are not pytest assertions.

The candidate must pass independent Release Audit, Simulation QA and Live QA
for its exact final SHA. No local test result or empty GitHub check list is
release approval. No merge, deployment, or activation of another repair is
authorized by this handoff.
