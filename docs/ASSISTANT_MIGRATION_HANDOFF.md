# C-2 AssistantStore migration repair

## Trigger and ownership

Release Auditor task `01a08523-47c7-76e3-be42-aee801a314fd`, turn
`01a0853c-66f3-7300-af90-570d3ed65243`, independently reproduced C-2:
14-column legacy `UNIQUE(source, source_id)` databases fail with a 16-versus-14
column error, leaving an empty live table and populated
`commitments_old_migrating`; a later startup exposes an empty schedule.
Sixteen-column legacy tables with organizer/account appended instead silently
misalign values and discard the original table.

The Orchestrator assigned this as an exclusive P1 data-corruption repair.
The candidate report is [PR #9](https://github.com/adjad/wisp/pull/9), commit
`1050cf932069136d5cd0237e9fd772cc2c204636`, audited against source
`c3b5afe26a1744dd7927586262be0533322443af`.

- Initial implementation base: `f20fb800fddc714d7d3b8d08a489dae4c81ef483`.
- Reconciled review base: `51fa3ec937df7a19961fbb2103a7654bb058ec74` (`origin/main`).
- Original repair commit: `60d8f829d81437d11aeede752676bec983e1d7d6`.
- Draft PR: [#13](https://github.com/adjad/wisp/pull/13).
- Branch: `codex/maintainer-c2-assistant-migration`.
- Worktree: `035d/MOE_Project`.
- Owner: Wisp Repository Maintainer, task `01a08523-1944-7570-825f-adac9874b7d3`.
- Final commit SHA and draft PR URL accompany the Orchestrator handoff.

## Delivered behavior

Schema setup, additive columns, uniqueness rebuilding, recovery, and index
creation run inside one `BEGIN IMMEDIATE` transaction. Schema inspection occurs
after taking SQLite's write reservation, so concurrent constructors serialize.
Individual statements replace `executescript`, whose implicit commit previously
left partial migrations durable. Initialization errors roll back and close the
connection.

Migration copies explicitly named columns, preserving IDs, every existing
field, NULL values, and active/done/dismissed states. Missing organizer/account
default to NULL. Notification records are untouched. The old table is dropped
only after source rows match the destination across all columns and row counts
are checked. `idx_commit_when(status, when_ts)` is created after dropping the old
table, so it belongs to the live table.

Recovery supports an old table alone, an empty replacement, partially or fully
copied identical rows, and disjoint rows added by a later startup. Identical
copies are accepted. Conflicting IDs or unique keys fail the transaction and
retain both tables and notifications. Unknown migration columns and a second
legacy live table also fail without choosing a data version. The uniqueness
check requires the full non-partial recurring-event key and detects a leftover
two-column key, including reversed key order. Overlapping NULL IDs fail safely
because they cannot identify which rows were already copied; disjoint NULL-ID
rows preserve their multiplicity.

## Changed files

- `service/assistant/store.py`: schema initialization and migration/recovery only.
- `tests/test_assistant_migrations.py`: independent historical fixtures and fault injection.
- `docs/ASSISTANT_MIGRATION_HANDOFF.md`: this handoff.

H-9/H-10 sync behavior, H-11 deduplication, C-1 shell policy, and queued routing
repairs are outside this change. No other production methods were edited.

## Validation

All Python checks used a fresh temporary `WISP_HOME` before service imports.
Only fixture databases and in-process mocks were used; no app launch, real
`~/.moe`, real notifications, external account mutations, or live model calls.
Runtime: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python`.

| Check | Result |
| --- | --- |
| New migration fixtures against the original source | Failed as expected: 14/15-column startup errors, 16-column value corruption, missing index, and unrecovered old rows |
| `tests/test_assistant_migrations.py -v` | 12 tests passed, including parameterized layout/recovery/failure cases |
| `pytest -q -p no:cacheprovider tests/test_typed_reminder_operations.py tests/test_reminder_creation.py` | 25 passed, 1 skipped |
| `tests/test_assistant_dedupe.py` | 32 checks passed |
| `tests/test_reminder_update.py` | 18 checks passed |
| `tests/test_reminder_bulk_clear.py` | 6 checks passed |
| `git diff --check` | Passed |

The single skip is the existing opt-in local Ling integration guarded by
`WISP_LIVE_REMINDER_TEST=1`. There are no unexpected failures in the final scoped
checks. The broader known routing failures were not used to expand this repair.

After the Orchestrator requested reconciliation, main at `51fa3ec` was merged
into this branch without conflicts or rewritten history. It brings the already
merged Research Library, design documentation, and bug report. None overlapped
the three C-2 paths. The migration code and fixtures are unchanged from
`60d8f82`; every successful check in the table above was rerun on the reconciled
tree with the same results. The candidate diff against the reconciled review
base still contains only the three files listed above.

The migration suite covers seven 14/15/16-column layouts (including appended,
inline, and reversed optional-column order), legacy and current uniqueness,
three startups for normal upgrades, and five interrupted states with repeated
startup. It compares complete row dictionaries and notification timestamps,
checks reminder suppression and schedule reads, SQLite integrity, index
ownership, and recurring occurrence insertion. It also verifies unchanged
logical database snapshots after conflicting recovery, unrecognized schemas,
failure before copying, failure after copying/dropping/index creation, denied
commit, and additive-column failure; retries succeed. Four concurrent
constructors are exercised for normal upgrades and interrupted recovery.
NULL-ID fixtures cover duplicate-row preservation and rollback on ambiguous
partial copies.

To reproduce the new fixture suite directly:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python tests/test_assistant_migrations.py -v
```

The test module creates its temporary `WISP_HOME` before importing the store.
For the existing suites, set a fresh temporary `WISP_HOME` in the launching
environment as well.

## Limits and integration

If the previous positional migration already scrambled fields and dropped the
original table, this repair cannot reliably reconstruct that lost information;
recovery would need a trusted backup or separately reviewed source data.
Ambiguous interrupted copies intentionally prevent startup while preserving
both versions for a subsequent explicit recovery decision.

No known overlap with the active research, production-support, or Simulation QA
writer scopes. Changes are compared with the reconciled review base above; any subsequent edits to
the store's initialization require reconciliation. A bounded read-only helper
review informed implementation and is not release approval.

The exact final commit must pass independent Release Audit, Simulation QA, and
Live QA. This handoff does not authorize merge, deployment, or live repair.
