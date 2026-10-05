# Router update implementation handoff

## User outcome

Flexible personal read requests can be interpreted by the configured resident Ling router, then validated and compiled into registered read tools. The new planner is disabled by default and never loads a model. Existing exact read shortcuts and deliberate action workflows remain authoritative. Calendar, email and message overviews are compact and preserve source, date, count, actor and partial-coverage information. Compiled message searches cannot broaden on a miss.

This branch implements the code and synthetic evaluation foundation. It does not establish the 95% routing target, a fine-tuning improvement, production activation, or a deployment.

## Ownership and integration

Base: `4994caa15533c0cf84c07208c9097e4197f2815b` (`origin/main`). Integration branch: `codex/router-update-1-3`. The parent owns this handoff and the integration artifacts; source changes are integrated from the frozen single-writer core, evaluation and presentation branches. The authoritative Orchestrator record is linked in `progress.json`.

Use the PR head and external gate receipt for the final exact SHA. Worker commits and incremental checks are listed in `progress.json`; they do not substitute for final candidate gates.

## Validation contract

Run `scripts/test_replay_failure_fixes.py` with the existing project Python in an outer environment that permits its existing restrictive native sandbox to start. Inside the Codex outer sandbox, the native peer gate fails before collection with sandbox_apply Operation not permitted; use approved escalation to launch the unchanged isolated runner, not a weakened inner sandbox. It discovers every test module and uses isolated temporary homes. Required PR checks are `python-regressions` and `Verified macOS artifact`. An independent Release Auditor and separate synthetic Simulation QA must cover the same final head because this change affects source boundaries, outbound classification, native read contracts and cross-component behavior. Failed, absent or inconclusive checks are not passing evidence.

The production-path harness runs actual application orchestration with scripted model responses and synthetic tool fixtures. Its development replay is an integration check, not model accuracy or latency. Both initial and post-repair raw outcomes must be preserved. The sealed 320-case test corpus has not been opened for candidate tuning. Eight development source-failure cases intentionally return honest errors but the original annotation also requires success literals; keep the original scores and disclose this annotation limitation separately.

## Outstanding experiments

Actual resident-Ling inference needs an explicit serialized resource window acknowledged by the existing Wisp 1.2 performance owner. The inactive/rate-limited coordinator is not a resource receipt. No production configuration, live model residency, native source or outbound effect is changed by this branch.

Training feasibility is documented in `TRAINING_FEASIBILITY.md`. The installed checkpoint and training runtime do not yet have a qualified loader/backward/reload path. A separately scoped compatibility check, bounded fresh-synthetic smoke run, saved-and-reloaded adapter, and frozen-code original-versus-adapted comparison remain necessary. No private session harvesting, held-out-derived training, or claimed uplift is permitted. Laya is not promoted by this branch; Jev remains unmeasured.

## Risks and prior failures

- Synthetic tests cannot establish real model validity, resident endpoint support for strict JSON schema, latency, training fit or actual-model generalization. Keep structured routing disabled until those gates pass.
- Some source tools cannot faithfully represent requested combinations of dates, counts or filters. The compiler must return an honest limitation instead of silently dropping them.
- Compact rendering has deterministic fixture coverage; installed-app visual and live native-source behavior are not verified here.
- Automatic approval review rejected a diagnostic allowlist expansion twice; no diagnostic files were edited. The implementation preserves the existing external route-source vocabulary and adds a finite debug-only intent disposition within the original router scope.
- An initial presentation test invocation omitted the repository safety bootstrap and failed with SQLite readonly errors; corrected isolated checks passed. An unintended mechanical test wording change was found and restored before the presentation commit.
- Repairs or reconciliation after a frozen candidate require fresh exact-head gate evidence. Only the designated Local integration coordinator can merge through the protected path. Production deployment and installed-app replacement are outside this implementation.

## First audit and repairs

The first candidate `3f86187` was blocked; its complete evidence is preserved in `first-candidate-gates/`. Core repair `b0ece931` and evaluation repairs `06691d13`, `44285c24`, `e9e51cd2` are integrated. Combined cheap checks on `d02c03d`:401 passed, one original-control-only skip; unchanged80-case scripted replay80/80 exact calls and72/80 original end-to-end. This is not a final gate receipt. The new pushed head must pass complete mechanical validation, required CI, independent re-audit and separate synthetic QA before code-only integration. Activation and training remain unqualified.

The second frozen candidate `40ee88a` passed179/179 local regression modules and the same-head Python CI. Independent re-audit remained BLOCK on repeated-read sender/date pairing and stale query reuse after a source-free replacement. The audit, raw reproductions and gate receipts are preserved in `second-candidate-gates`. Core repair `2892db7` (parent `dfa0c916`) added whole-clause bindings and replacement precedence; its first unchanged DEV replay caught an over-abstention regression for ordinary calendar source aliases. This stage and its original scores remain preserved. The candidate must regain supported DEV behavior before a new freeze; old passing receipts do not transfer.

Core alias followup `de44c4b7` is integrated at `ddc70d2`; bounded source-reference phrases coalesce while separately constrained reads retain full tuple validation. The unchanged development replay recovered80/80 exact/source and72/80 original end-to-end;454 combined tests passed with one existing skip. Exact worker checks passed836 tests plus4640 subtests. See `development-after-alias-repair`. These are pre-freeze checks; the subsequent pushed candidate still needs fresh full mechanical/CI/independent audit/separate QA receipts.
