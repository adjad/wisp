# Debugging Wisp

Click **Report a problem** under a completed chat reply, in **Wisp Today**, or
choose **Report a Problem…** from Wisp's menu. Add what you expected, review the
JSON preview, then save it. Nothing is uploaded or sent to anyone. The menu also
covers a dropped connection with no completed reply.

Reports include the app build, backend source fingerprint, trace ID, elapsed
stages, client outcome, source-readiness enums, and native receipt outcomes when
available. A missing journal is explicitly reported; it is never reconstructed
from a different request. A failed Today edit is reported separately from the
subsequent refresh that displays the current plan. The client terminal event is
recorded separately from runner cleanup: a reply can finish before cancellation
of remaining memory-summary work. A source fingerprint identifies the local
service source tree when the first trace is written; packaged builds also carry
their source commit when available. Development metadata never invents a commit.

## Privacy and retention

The service keeps only metadata in `~/.moe/diagnostics` (or
`$WISP_HOME/diagnostics`). It retains at most 100 traces, removes traces older
than seven days during subsequent writes, and retains at most 128 events per
trace. Frequent Today refreshes count toward the same limit, so history may
cover substantially less than seven days. The files and directory are private
to the current user. Delete the directory to clear the history while Wisp is
stopped; it is recreated on the next operation. There is no diagnostic HTTP
reader, network reporting service, or account setup.

Metadata journals exclude prompts, titles, messages, tool arguments/results,
route explanations, raw model I/O, arbitrary error strings and source reasons.
Token events do not cause journal writes. `test_mode` creates no journal, even
when a nested source or native operation is traced. Journal write failures do
not interrupt the request.

**Include conversation or planner details** is off in each report window.
Selecting it includes private content for that specific operation. Chat reports
can include the prompt, answer, errors, tool evidence and model I/O already
captured in memory; raw model I/O requires enabling Debug Mode before the turn.
Known credential keys and common credential patterns are scrubbed before the
preview and save. This cannot recognize every possible secret or private fact;
review the preview before sharing. The existing **Export Chat Debug Log…** is a
separate detailed conversation export.

To diagnose planning, enable **Capture planner details for a problem report**
in Today before refreshing. Wisp requests the exact planner inputs, source
snapshot, fixed clock and expected plan. These remain in memory for that
refresh; they are never added to the metadata journal. Selecting detailed
content in the report window is still required to save them. Turning capture
off, changing the selected day, or a failed refresh clears the captured details.

## Offline replay

Run from the matching source checkout:

```sh
python scripts/replay_diagnostic.py /path/to/Wisp-problem-123.json
```

A detailed Today report runs only the deterministic planner with its captured
clock, timezone and inputs. It compares the complete result with the captured
plan. Exit codes: `0` matches (or metadata playback), `1` planner mismatch,
`2` invalid capture or incompatible planner version. The output contains the
captured private content when replaying a detailed report.

Reports without planner inputs provide metadata timeline playback only. Replay
never starts the service, opens Wisp's database, calls a model, runs agent tools,
or invokes native actions. It loads the pure planner file directly to avoid
service initialization. Inputs are limited to 2 MB and 1,000 tasks/commitments;
a planner fingerprint mismatch requires checking out the matching source.
Redaction can change detailed input values, so this is a replay of the saved,
reviewed capture rather than a promise to reproduce unexported secrets.

Today explains unscheduled work using the scheduler's actual constraints. For
insufficient time it reports the required uninterrupted minutes and the largest
gap before the deadline or working-hours end. Waiting on incomplete sources or
an all-day reservation uses a separate reason code. These diagnostics do not
change slot selection or authorize calendar writes.

## Validation

Synthetic checks cover metadata privacy, journal permissions/retention/failure,
concurrent contexts, cancellation and unconsumed streams, approval correlation,
source and native failure states, planner explanations, capture opt-in,
credential scrubbing, native file reading and offline replay. They operate in
isolated temporary state and never send communications or touch real native
applications. The complete Simulation QA gate includes the new Python and
native report contracts. Native app compilation verifies the report controls.
