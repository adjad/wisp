# A08a captured-source temporal facts

All examples in this directory are invented. No fixture was copied from user data.
The extraction module uses Python's standard library and the timezone database;
it does not import Wisp task resolution, memory, providers, or application state.

```python
from datetime import datetime, timezone
from service.temporal_facts import EvidenceContext, extract_temporal_facts

result = extract_temporal_facts(
    "Report due 2026-10-03.",
    captured_at=datetime(2026, 9, 25, 19, 17, tzinfo=timezone.utc),
    timezone="America/Los_Angeles",
    evidence=EvidenceContext("synthetic", "fixture-001"),
)
```

The immutable result preserves input ordering, exact Python character spans,
clause context, capture metadata, source identity, and extractor version. A date
without a stated time stays a date. Capture timestamps must be aware; their zone
never implicitly supplies the source timezone. A caller-supplied timezone is a
fallback; an explicit source zone takes precedence. Callers must retain both the
fact's uncertainties and the result's issues. A resolved candidate expresses a
unique reading at its stated precision, not independent verification of truth.

Supported English forms include ISO dates, full or three-letter month names,
slash dates, today/tomorrow/yesterday, numeric relative days/weeks/hours/minutes,
AM/PM and 24-hour clocks, noon/midnight, and bounded date/clock ranges. Relative
hours/minutes are elapsed durations; relative days/weeks are local calendar dates.
Missing years, ambiguous slash order, weekdays, bare clock hours, and unsupported
dayparts stay uncertain. A DST fold keeps both UTC candidates; a gap has none.
Clock ranges preserve per-endpoint zones; trailing date-range zones apply to both
endpoints. `until` retains an exclusive end, `through` an inclusive end, and
`to`/dash ranges retain an unspecified end boundary. Unspaced negative offsets
outside ISO date-time notation can also be range endpoints, so they remain
ambiguous. Full date-time ranges outside this grammar retain separate endpoints
with `unsupported_range`; corrections retain `alternatives` without a winner.
No end-of-day deadline, implicit meridiem, overnight rollover, source read,
persistence, model call, Calendar action, or Today integration is performed.

This is deliberately a bounded parser, not general language understanding.
Arbitrary prose and unsupported languages may yield no temporal expression.
Kind classification uses local English cues. Conditional/negated mentions are
retained with uncertainty. Correction/alternative language within a clause is
conservative; separate sentences or source documents are not reconciled. This
slice does not establish the A01 public contract; later integration needs its
own ownership and A01/A02 readiness.

Malformed metadata has explicit result/fact diagnostics. Oversized text is
rejected above 100,000 characters; more than 256 facts produces an explicitly
partial result. Malformed clock/zone suffixes do not resolve using a supported
prefix or a fallback zone. Timezone resolution depends on installed IANA tzdata;
no locale, system clock, or machine-local timezone is consulted.

Run the focused regression suite with:

```sh
python -m pytest -q tests/test_temporal_facts.py
```
