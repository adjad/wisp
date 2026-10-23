# Contained Notch evaluation

Runs prompts through the real notch Ask surface (`OverlayPanel` + `OverlayView`
+ `OverlayModel`) in a separately marked development app, "Wisp Notch
Evaluation (DEV)" (`com.wisp.notch-evaluation.dev`), against a guarded
backend. It never installs, signs as, opens or replaces Wisp.app, never uses
port 8765, and never reads or writes `~/.moe`.

- `scripts/build_notch_evaluation.sh` builds, launches and stops a run.
- `lifecycle.py` validates scratch roots and supervises both processes; no
  command signals a pid read from a file.
- `host.py` is the guarded backend: the process-latched boundary
  (`service/evaluation_boundary.py`), an endpoint allowlist, and completions
  only to a resident, reviewed model.
- `driver.py` types each case into the app's evaluation-only input adapter and
  writes resumable results; `corpus.py` parses the corpus; `snapshots.py` and
  `capture.py` make verified, read-only source copies.

## Source modes

**Snapshot** (default): sources come only from a verified snapshot installed
in the run's own root. The app runs under `sandbox-exec` with `app.sb`.

**Live** (`launch --live-sources`): the user approves it and grants this
bundle's own permissions (Full Disk Access, Mail and Notes Automation,
Calendars, Reminders, Contacts) in System Settings. Scripts never change
those settings. The app's own readers read Mail, Messages, Notes, Calendar,
Reminders and Contacts read-only; Documents and Desktop come from a
read-only `capture.py` copy. The app is launched through Launch Services
(`open -n -W`) so macOS attributes permissions to it, applies `app.sb` to
itself with `sandbox_init` before anything else, and refuses to run if that
fails. It is stopped by a token-checked quit, and exits on its own when the
backend stops proving the run's identity.

The backend admits one live-only hub event, a request to re-read named
sources, and nothing else; every effect stays blocked at its sink.

## Known limits

- **Engine attribution is pinned, not re-run per connection.** The production
  attribution of the desktop oMLX runs `lsof` and the setuid `ps`, which the
  sandboxed backend cannot run (macOS never executes a setuid binary inside a
  sandbox). So the unconfined supervisor runs it once before launch, unchanged
  (`engine_attest.py`: `RuntimeAuthority` → `DesktopOmlx` on 127.0.0.1:8000),
  and pins the listener's pid, start time, uid, executable, parent, signature
  and loaded model in the host config. It checks
  `~/.moe/omlx-runtime-authorization.json` only to confirm the file is absent.
  The backend never execs and never reads `~/.moe`. Its engine authority
  (`pinned_engine.py`, importable only under the latch) re-checks the pin on
  every request write with kernel reads only: the same incarnation and
  executable (proc_pidinfo, proc_pidpath), and this connection's far end held
  by that pid (proc_pidfdinfo). Any mismatch stops the run as
  `ENGINE_NOT_READY`, with no fallback. **Gap:** per-connection peer
  attribution in a run uses this pinned identity, not the production lsof/ps
  path; `service/inference/attributed_transport.py` is unchanged.
- **Reviewed reads** are listed in `host.py` `REVIEWED_READS`. `plan_meeting`
  (it can request an on-demand native sync), `system_status` and
  `list_running_apps` (processes, AppleScript), web, clipboard, contacts and
  browser-history tools stay blocked.

- **Apple Events cannot be limited by verb.** The live profile can only name
  the destinations (`com.apple.mail`, `com.apple.Notes`). So every
  AppleScript a reader runs first goes through `NotchEvaluationScriptPolicy`,
  an allowlist built from the readers' own scripts. Raw event codes (`« »`),
  `current application`, possessive (`'s`) calls other than AppleScript's
  text item delimiters, and words like send, delete, make, move, use, load,
  write, display, do and perform are refused. Every word must be one the
  readers use, every `application "X"` (in a `tell` or not) must name Mail or
  Notes, and every `set` must assign one of the readers' own locals. Comments
  end at CR or LF, and any non-ASCII character outside a string literal
  (Unicode line breaks, zero-width characters, homoglyphs) is refused. A script that fails is refused,
  recorded as `script_refused`, and not run. The check is lexical: it vets
  the readers' fixed scripts, and it is not a parser for arbitrary
  AppleScript. `tests/test_notch_script_policy.py` collects every
  multi-line and single-line script literal in the readers and requires the
  allowlists to match them exactly.
- **No other AppleScript or process runs.** `OutboundSender` is replaced by a
  stub in the evaluation build; any call records `outbound-*` and stops the
  app with `EVALUATION_BLOCKED`. `ScriptRunner`, `BackendManager` and
  `PortGuard` stop before any script or process. The test scans every app
  source for NSAppleScript, OSAScript, user script tasks, `Process(` and
  `osascript`.
- **Reminders and Calendar are read only.** Every EventKit save, remove and
  commit (`RemindersWriter`, `CalendarReader`) stops the evaluation build
  with `EVALUATION_BLOCKED`; only the readers' fetches run.
- **The one socket bind** the profile allows is the input adapter's
  `app-input.sock`, by literal path in the validated owned root.
- **Launch Services lookups are allowed** in both modes, because AppKit
  cannot start without them. That would also let the app ask Launch Services
  to open URLs, files or apps, so `NotchEvaluationWorkspaceGuard` replaces
  every NSWorkspace open and launch entry point before NSApplication starts.
  Each one records the call and refuses it, and the app will not start
  without the guard. The native checks call each one, including the
  deprecated and file-changing ones (recycle, duplicate, setIcon,
  performFileOperation).
- **Confinement is proven, not assumed.** At startup, in both modes, the app
  tries to create a file outside its owned root. That write must be denied,
  or the app stops. The input socket must be a 0600 socket owned by the user,
  or the adapter refuses to start.
- A wedged main thread cannot handle the quit request. Stop then reports
  `APP_STILL_RUNNING`; the off-main identity watchdog exits the app within
  about 30 seconds of the backend being retired.
