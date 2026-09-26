Synthetic A02 storage fixtures; no real identities, accounts or source data.
`lifecycle.json` extends A01 assignment examples with a separate confirmation
capture so receipt evidence is actually grounded in the matching observation.

APIs: compose `DiscoveryStore(assistant)` and `JobStore(assistant)` using
`assistant.transaction()`. Record envelopes contain `payload`, storage `revision`
(CAS token), and separate `overrides`. Wire item/source revisions remain distinct.
Immutable observation/evidence/receipt retries must match exactly. Item content
changes advance its wire revision; lifecycle-only changes use storage CAS. Only
explicit `set_overrides` replaces manual metadata. Saves never change Today data.

Future workers must reserve conservative action/time slices before work and stop
at the reserved limit; storage cannot enforce runtime elapsed time. Waits consume
no budget. Persist `mark_effect_pending` before any separately authorized effect.
Lease loss/cancellation then preserves `outcome_unknown`; only explicit terminal
reconciliation clears it. Job results grant no approval and complete no obligation.
A03/A14 own trusted approval, execution, receipt authenticity and runtime accounting.
