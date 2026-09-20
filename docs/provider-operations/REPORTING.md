# Secret-safe reporting runbook

This is an implementable manual/export workflow, not a collector. No authenticated API was called and no account-wide metric was verified here. Prefer provider APIs after approved connection; otherwise use sanitized exports. Do not run inference to test reporting.

## Sources and units

| Service/source | Measure | Interpretation |
| --- | --- | --- |
| OpenRouter `GET https://openrouter.ai/api/v1/key` | Dedicated-key usage, period counters, remaining limit, reset policy | Key scope only; overlapping day/week/month/lifetime counters are not additive. Export numeric counters and controlled reset enum only. Drop the response's label, which can include key fragments, and identity fields. |
| OpenRouter `GET https://openrouter.ai/api/v1/credits` | Account total purchased/used credits | Requires management key. Prefer a sanitized manual account export if that access is not already approved. Balance is account-wide, not Wisp attribution. |
| OpenRouter completion usage / generation lookup | Requests, prompt/completion tokens, charge in credits; reasoning/cache details | Use already authorized workload records. Generation lookup reconciles the same request. Reasoning/cache subtotals must not be added to parent token totals. Missing final streaming usage stays unknown. |
| OpenRouter Activity export | Period requests, tokens, spend by model/key | Filter Wisp's dedicated key and exact period. Activity spend can include estimated BYOK spend; keep that estimate separate from OpenRouter account charges and actual upstream invoice. |
| Orchestra private evaluation export | Requested/effective model, decision success, latency, tokens if present, errors | Filter to numeric aggregates and public model labels. Do not export prompt/completion captures. No stable public billing API/price table was verified; use an account export or invoice with documented units. |
| EVO local exported experiment evidence | Accepted experiments, attempts, checks, failures, score, elapsed seconds | Checks are not consumed attempts. Scores and local elapsed seconds are not billed tokens or dollars. Provider invoices/usage supply external costs. |

Official references checked 2026-09-20 UTC: [current-key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key), [credits API](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits), [usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting), [Activity exports](https://openrouter.ai/docs/cookbook/administration/activity-export), [EVO documentation](https://evo-hq.com/docs/). These are schemas and vendor statements, not measurements of this user's account.

## Update procedure

1. Choose one closed UTC interval `[start, end)` and a neutral scope alias. Record the observation timestamp separately. Read source-task updates first; do not overwrite a newer result with this historical snapshot.
2. Obtain approved read-only metrics or an export. An eventual API collector should load a secret directly into memory from approved storage, use fixed HTTPS origins, disable redirects, enforce timeouts and avoid request/response debug logging. Do not put authorization values on command lines or in shell tracing. A 401/403 means connection/access unresolved, not zero usage; do not expand privileges automatically.
3. Keep raw exports outside Git in access-controlled local storage. Treat API labels, account IDs, errors, paths and trace payloads as sensitive. Build a new allowlisted record; do not rely on regex redaction of an entire response. Log only a fixed error category and timestamp, never raw exceptions/headers/bodies.
4. Validate numeric fields: finite, nonnegative counts/costs; whole-number counts; explicit units; valid UTC interval; no missing value coerced to zero. Preserve null for unknown. Refunds/adjustments belong to a separate signed adjustment record, not a negative inference count.
5. Deduplicate records by local stable record ID and source revision. Keep request-level events, interval aggregates and cumulative snapshots in distinct classes. Never sum a snapshot with the events it covers. Reject or reconcile overlapping interval exports for the same scope instead of adding them.
6. Reconcile dedicated-key events to provider period totals. Keep Wisp, key and account totals separate. Mark incomplete coverage if calls are missing, use multiple keys, or cannot be attributed. Polling deltas across a key reset/replacement are invalid; start a new series. Provider totals can lag; retain pending reconciliation.
7. Update DASHBOARD.md with the interval, source, freshness, known subtotal, missing-cost request count and any unresolved discrepancy. Preserve prior sanitized reports as dated Markdown outside raw-data storage when history is needed. Review the diff before committing only these sanitized aggregates.

Update on demand and after an approved experiment or billing export. No schedule is enabled. If reporting later becomes scheduled, notify only on meaningful changes, failures, allowance concerns or needed user action; stale/failed collection must remain visibly stale.

## Ledger record template

Copy into a private local reporting file, then publish only reviewed aggregates. This template is deliberately unpopulated; it is not a measured account record.

```json
{
  "record_id": "local-neutral-id",
  "service": "openrouter",
  "record_kind": "interval_aggregate",
  "scope": "wisp-dedicated-key",
  "observed_at_utc": null,
  "period_start_utc": null,
  "period_end_utc": null,
  "evidence_class": "unknown",
  "source_kind": "sanitized_provider_export",
  "source_revision": null,
  "requests": null,
  "prompt_tokens": null,
  "completion_tokens": null,
  "failed_requests": null,
  "successful_tasks": null,
  "local_elapsed_seconds": null,
  "provider_charge": null,
  "charge_unit": "provider_credits",
  "estimated_external_usd": null,
  "missing_cost_requests": null,
  "reconciliation": "not_connected"
}
```

Controlled values: service = orchestra/openrouter/evo; kind = event/interval_aggregate/cumulative_snapshot/adjustment; evidence = verified_provider/source_reported/estimate/unknown. `verified_provider` means observed provider output for that scope, not an independent billing audit. `source_reported` covers historical task handoffs. Keep model/provider labels public and controlled if added. Source revision is a local neutral identifier, never an account/request secret or a private file path. Keep actual generation IDs privately only if needed for reconciliation.

Approved budget records are separate: approval reference, service/scope, currency, period, maximum amount, request/output-token/wall-time caps, provider-enforced setting verified at timestamp, and reserved in-flight amount. No such approval is recorded by this workspace. Provider rate limits, remaining credit, model unit-price filters and EVO attempt limits are different controls.

## Calculation rules

- Known billed subtotal = sum of unique observed charges in one scope/interval/unit. Display missing-cost count beside it; never label an incomplete subtotal as the total.
- Estimated token charge = input tokens × input unit rate + output tokens × output unit rate, with denominators stated (per token versus per million). Account for cache, reasoning inclusion, request charges and other modalities only from documented rates. Label as an estimate until reconciled.
- Cost per successful task = attributed cost of all attempts (including failures/retries) divided by successful completed tasks; unknown when cost coverage is incomplete or successes are zero. A tool follow-up is another request, not necessarily another user task.
- Credits, cash paid for credits, taxes, provider BYOK charges and training/hosting invoices stay separate. Convert credits to USD only with verified account semantics; do not count credit purchases and credit consumption as two operating charges.
- Orchestra/EVO experiment labels may refer to the same OpenRouter request. Attribute the charge once to the billing provider, with experiment tags for analysis. Do not add upstream inference detail to an already inclusive billed charge.
- Latency percentiles require per-request durations and a sample count. Do not average percentiles or treat CLI fixture runtime as model latency.

## Privacy and retention

Exclude prompt/completion text, tool arguments/results, personal names/emails, cookies, authorization headers, key fragments, account/workspace IDs, raw errors and private filesystem paths. A local-only record can still contain secrets. Export aggregate counts rather than traces. Account reports and even cost patterns may be private; only share reviewed summaries.

Provider storage policy is not proof of local processing. Orchestra training/capture and OpenRouter downstream providers need their own retention review. Keep private raw evidence only for an agreed reconciliation period; record deletion decisions without deleting another task's artifacts. Do not commit `.understudy`, `.evo`, auth files or raw exports. No secret-store or privacy-control implementation is included here.
