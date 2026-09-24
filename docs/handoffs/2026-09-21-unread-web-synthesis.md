# PR #67: unread message and web summaries

## Reconciled outcome

Broad Messages digests and Daily Summary select only authoritative unread messages from the inclusive rolling previous 72 hours, excluding future timestamps and unknown legacy read state. Named person/group summaries and explicit day/period selectors retain main's behavior, including critical read messages where previously supported. Stock and web tool routes receive evidence-synthesis, descriptive-link, uncertainty, and untrusted-content guidance even when selected through direct calls.

When a quote-only request names a known stock company or ticker, synthesis uses quote guidance without web-link instructions even if web tools are present only as fallback options. Explicit requests for news, web research, or a named source retain web evidence guidance.

## Base, history, and ownership

- Original continuation base: `6c7bae346e26b6a593ecd7f038bb3e40dddf0afa` (`origin/main` at task start).
- Original prior PR head: `888457f64f36e8c26c1140a8f37299ec3f400364`.
- Reconciliation base: `377af1cb50c207defd95489daedb08df8943af59` (`main`, including PR #76).
- Latest implementation candidate: `917753bac7e4d0bea172c1cf2814d75cae378e2e` (quote-only fallback guidance and tests; PR branch `codex/unread-web-synthesis`). The PR description records the exact current branch tip after this handoff refresh.
- PR: https://github.com/adjad/wisp/pull/67; remote branch `codex/unread-web-synthesis`.
- Local continuation: `codex/pr67-continuation-529a`, in the clean Codex worktree `529a`; the historical `/private/tmp/wisp-unread-web-synthesis` checkout is preserved.
- Reconciliation used a normal merge of main into the prior PR head, followed by non-force pushes to the existing remote branch. The merge commit is `7223e19f9acecae80c44f3f050ee74af90bc42d2`; later implementation commits address findings from the independent audit.
- The Orchestrator acknowledged sole ownership of the ten original PR files before edits: this handoff, `service/tools/imessage_tools.py`, `service/assistant/brief.py`, `service/tools/registry.py`, `service/tools/web_tools.py`, `service/agent/loop.py`, `service/main.py`, `service/inference/attributed_transport.py`, `tests/test_message_digest.py`, and `tests/test_web_response_followup.py`.
- Net changed files against the continuation base: this handoff, `service/tools/imessage_tools.py`, `service/main.py`, `tests/test_message_digest.py`, and `tests/test_web_response_followup.py`.

## Deliberate conflict resolutions

Main already contains stronger official-oMLX signature, process, runtime-tree, and established-socket attribution plus cloud evidence sanitization, source links, local news display-only isolation, and richer web synthesis. Those implementations and tests are retained; the stale PR's weaker replacements are not restored. The Orchestrator explicitly confirmed unread-only behavior supersedes main's critical-read inclusion only for broad digests and Daily Summary. Other message selectors remain unchanged. No Settings or PR #76 fixes are included; changes from main enter only through the merge.

## Validation and limits

- On implementation candidate `917753bac7e4d0bea172c1cf2814d75cae378e2e`, focused suites passed: `tests/test_message_digest.py` 703 passed and `tests/test_web_response_followup.py` 179 passed.
- The full replay regression gate covered 117 modules: 116 passed in the sandbox; `tests/test_runtime_peer.py` could not start its disposable loopback server there. Running that module with loopback permission passed all 9 tests, covering all 117 modules. No live inference endpoint, Messages database, installed app, or real communications were used.
- Added tests cover inclusive 72-hour and now boundaries, expired/future/read/legacy exclusions, all Daily Summary selectors, empty-cache no-fallback behavior, retained explicit historical/named reads, stock/direct-web synthesis guidance, and quote-only NVIDIA/AAPL prompts alongside explicit news/source requests.
- Local runtime: Python 3.14.3 from the existing project environment. Full isolated command: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python scripts/test_replay_failure_fixes.py`. Final exact-commit results are attached to the PR handoff; local Python is not the pinned CI Python 3.13 environment.
- Required remote evidence for the final pushed head: `python-regressions` and `Verified macOS artifact`. Older-head checks do not qualify. Independent Release Auditor review and Live QA remain separate gates; builder peer review is not release approval.
- Integration dependencies: native Messages U/R metadata and main's existing evidence/attribution protections. PR #76 shares `service/main.py`/`service/agent/loop.py`; its branch was not edited. Revalidate on any later reconciliation. This task does not merge or deploy.
