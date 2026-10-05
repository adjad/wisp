# Read-only digest redaction diagnosis

Orchestrator authorization: `EXISTING_PRESENTATION_OWNER_READ_ONLY_DIGEST_TEST_DIAGNOSIS`, acknowledged 2026-10-05 21:14:43. Frozen worker inspected: `ae8efb960965a20660297f86d5fffd6e741bdcc8`; no repository file changed. Parent gate candidate `27490ae` reported the same unchanged assertion at `tests/test_message_digest.py:2961`.

The failure is a numeric timestamp collision, not a reproduced text leak. `SummaryRow` retains a real numeric timestamp as the first tuple field. `str(rows)` converts that timestamp into a decimal string, so an unrelated sequence of four digits can equal a fixture OTP.

| Frozen local clock | Epoch | Unchanged original test | Where forbidden digits occur |
| --- | --- | --- | --- |
| Oct 5, 2026 10:40:00 | 1791222000.0 | PASS | Nowhere |
| Oct 5, 2026 14:09:27 | 1791234567.0 | FAIL at original row-repr assertion | `1234` only in numeric epochs 1791234565.0 and 1791234566.0 |
| Oct 5, 2026 20:19:42 | 1791256782.0 | FAIL at original row-repr assertion | `5678` only in numeric epochs 1791256780.0 and 1791256781.0 |

Each case retained exactly two source occurrences, identical redacted bodies, and source-position metadata `(0, 1)` / `(1, 2)`. Both bodies were `Alex: Please review the original request with a stated deadline (private details omitted).` Repeated filtering and fresh read-state selection retained both occurrences.

Before running the original unchanged assertion, the diagnostic replay exercised the same four public summary paths: recent, today, this week, and named conversation. It froze Messages and digest `datetime.now`, froze the freshness `time.time` clock, and passed the same frozen datetime to the real period resolver. Every path returned two messages with the action category. All four forbidden strings (`1234`, `5678`, `report`, `proposal`) were absent from every rendered output, all string-valued row fields, the complete debug records, and the complete fake model-call arguments. The fake model interface retained the existing synthetic offline failure. Network and native-subprocess guards recorded zero attempts. Repository root conftest supplied disposable Wisp state.

Evidence: `/private/tmp/WISP_DIGEST_DIAG_clean.json`, `/private/tmp/WISP_DIGEST_DIAG_otp1234.json`, `/private/tmp/WISP_DIGEST_DIAG_otp5678.json` and matching `.log` files. Diagnostic plugin: `/private/tmp/wisp_digest_metadata_diag.py`. The original test was neither edited nor skipped; the two adversarial executions intentionally reproduced its failure.

Exact narrow command template, run separately for the three rows above:

```sh
ROUTER_DIGEST_DIAG_CASE=<clean|otp1234|otp5678> ROUTER_DIGEST_DIAG_EPOCH=<epoch> PYTHONPATH=/private/tmp:/Users/adijain/.codex/worktrees/router-overviews/MOE_Project PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -p pytest_asyncio.plugin -p wisp_digest_metadata_diag -q tests/test_message_digest.py::test_distinct_redacted_requests_keep_both_source_occurrences
```

Bounded durable proposal, requiring a new edit ACK for **only** `tests/test_message_digest.py`:

1. Parameterize this existing test with all three fixed epochs, explicitly including `1234` and `5678` timestamp collisions. Keep the OTP bodies and protected words exactly unchanged.
2. Freeze the freshness clock and Messages/digest datetime for the parameterized epoch; bind the period resolver to that datetime. This keeps recent/today/week/conversation semantics stable across future dates.
3. Preserve the real timestamps with an explicit equality assertion against `[now - 2, now - 1]`; assert the adversarial epochs actually contain the intended OTP digits. Retain both-occurrence, equal-body, repeat-filtering, selection-count, public-output, and complete model/debug privacy assertions.
4. Replace the initial generic `str(rows)` privacy assertion with the same forbidden-string check over **every string-valued tuple field**, preserving both conversation and message-body coverage while excluding numeric metadata. Add the same string-field assertion for the selected rows. Keep the existing complete debug/model-call assertion unchanged.
5. Narrowly validate the parameterized test and the existing message digest module with the safety bootstrap. No product-code repair, changed OTP fixture, skip, or reduced forbidden-string set is justified by the evidence.

This diagnosis establishes correctness for this synthetic redaction regression, not a general privacy audit. Parent mechanical/review evidence remains invalid until the test repair is separately acknowledged, committed, and validated at its new SHA.
