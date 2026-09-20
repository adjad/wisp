# Usage and setup dashboard

Observed 2026-09-20 UTC. Update manually using [REPORTING.md](REPORTING.md). Status becomes stale when source tasks or provider state change. This report does not poll or enforce budgets.

| Service | Verified artifact / source-reported result | Connection and setup | Usage scope | External cost evidence |
| --- | --- | --- | --- | --- |
| Orchestra | Local CLI pilot and newer routing harness exist; hosted run not verified | Account/key/project/entitlement unknown; pinned 0.6.41 toolkit described in source setup | Original synthetic preparation pilot: 9 CLI commands, 0 provider calls | Source-reported $0 API spend for original pilot only; newer harness and account totals unknown |
| OpenRouter | Offline prototype: 22 synthetic tests passed | Account, dedicated key, model/provider selection and balances unknown; Wisp adapter not delivered by this workspace | Original offline pilot: 0 inference requests | Source-reported $0 provider spend for original pilot only; account totals unknown |
| EVO | Local CLI 0.8.0 pilot: 12 scoring / 6 gate cases; headless worked | No account required for demonstrated local CLI workflow; optional hosted backends not verified | One restored accepted baseline plus check runs; not an optimization win | Source-reported $0 external service spend for pilot; host-agent usage and local compute unmeasured |

## Budgets and readiness

| Service | Proposed/approved allowance for this workspace | Provider-enforced limit | Next dependency |
| --- | --- | --- | --- |
| Orchestra | No paid-workload authorization | Unknown; harness does not enforce a dollar cap | User sign-in/approved connection, entitlement and written pricing; separate bounded-run approval |
| OpenRouter | No paid-workload authorization. Source proposal: at most 12 attempts / $0.25 inference, **not approved** | Unknown; no key inspected or changed | User-approved dedicated key connection, selected model/provider and funded-account confirmation without buying credits |
| EVO | Reporting only; no new experiment authorized | Attempt limits are not dollar limits | Before a future run: scope, protected benchmark, wall-time/attempt cap and provider-cost bound |

No common account total can be calculated. The three pilot zeroes exclude ordinary Codex sessions, reviewer work, electricity and development time. They are not lifetime spend, available balance, or proof of free hosted service.

## Current reporting coverage

| Measurement | Orchestra | OpenRouter | EVO |
| --- | --- | --- | --- |
| Account balance / billing period spend | Unknown | Unknown | No consolidated billing account established |
| Dedicated Wisp provider usage | Unknown | Unknown | Provider charges must come from actual providers |
| Runtime Wisp success rate / latency / cost per success | Unmeasured | Unmeasured | Unmeasured |
| Local pilot evidence | Available in source branch | Available in source branch | Available in source branch |
| Live account reconciliation | Not connected | Not connected | Not applicable to local CLI alone |

## Next user action

For live usage visibility, sign in to the desired provider and approve a dedicated connection used only for reporting (verify the credential’s actual permissions), or supply a manually sanitized usage export through the source task. Never paste a key into chat. Orchestra additionally needs current entitlement/pricing confirmed; OpenRouter needs the intended model/provider before any live evaluation. No user action is required to read and maintain this offline workspace.
