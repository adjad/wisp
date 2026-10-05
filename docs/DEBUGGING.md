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
`$WISP_HOME/diagnostics`), at most 128 events per trace. Retention is applied
per kind of trace, on each write, so background polling can never push a chat
turn out of the history:

- chat turns and native operation receipts: the newest 100;
- Today refreshes, source syncs and the daily brief: the newest 20 of each kind.
  Today refreshes about every 30 seconds while its window is open, so that is
  roughly the last 10 minutes of Today plans, enough to explain the plan on
  screen;
- any trace older than seven days is removed on a later write, whichever cap
  applies. A trace that is still being written always keeps its own journal.

The journal therefore holds at most 160 files. The files and directory are
private to the current user. Delete the directory to clear the history while
Wisp is stopped; it is recreated on the next operation. There is no diagnostic
HTTP reader, network reporting service, or account setup.

Metadata journals exclude prompts, titles, messages, tool arguments/results,
route explanations, raw model I/O, arbitrary error strings and source reasons.
Token events do not cause journal writes. `test_mode` creates no journal, even
when a nested source or native operation is traced. Journal write failures do
not interrupt the request.

**Include conversation or planner details** is off in each report window.
Selecting it includes private content for that specific operation. Chat reports
can include the prompt, answer, errors, tool evidence and model I/O already
captured in memory; raw model I/O requires enabling Debug Mode before the turn.
Automatic redaction is best effort and the preview is the final authority:
read it before you save or share. Wisp redacts, wherever they appear:

- values of keys whose name contains password, passwd, passphrase, secret,
  token, authorization, cookie, credential, api key, access/private/signing key,
  session id, bearer or oauth (matched as a substring, ignoring case, separators
  and invisible characters, so `x-api-key` and `openrouterApiKey` count; counters
  such as `max_tokens` are kept), and secrets used as a dictionary key;
- `label: value`, `label=value` and `"label": "value"` for the same labels in
  prose or embedded JSON, `password is …`, `Authorization`/`Cookie` headers
  including Basic and Bearer schemes, and URL passwords;
- AWS access keys, Google API keys, Slack, GitHub and Stripe tokens, `sk-` style
  keys (also upper case, full-width and `%2D`-encoded), JWTs and PEM key blocks;
- labels split by whitespace, newlines or zero-width characters.

It cannot recognise every secret or private fact. Known gaps: a secret with no
label and no recognisable format, multi-word passphrases (only the first word
after `passphrase is` is removed), secrets in images or attachments, and personal
details such as names, addresses or message text. Text that is redacted is
rewritten without invisible characters and with compatibility normalization.
A saved report is created private to your user (mode 600) from its first byte
and replaced atomically; if that cannot be done, saving fails and no file is
left behind. The existing **Export Chat Debug Log…** is a separate detailed
conversation export.

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
applications. They also check that polling does not evict chat traces, that
saved reports are created with private permissions from the first byte, and that
non-finite numbers in captured content cannot crash report creation. The complete Simulation QA gate includes the new Python and
native report contracts. Native app compilation verifies the report controls.
