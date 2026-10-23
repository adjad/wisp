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

- **Apple Events cannot be limited by verb.** The live profile can only name
  the destinations (`com.apple.mail`, `com.apple.Notes`). So every
  AppleScript a reader runs first goes through `NotchEvaluationScriptPolicy`,
  an allowlist built from the readers' own scripts. Raw event codes (`« »`),
  `current application`, possessive (`'s`) calls other than AppleScript's
  text item delimiters, and words like send, delete, make, move, use, load,
  write, display, do and perform are refused. Every word must be one the
  readers use, every `tell` must name Mail or Notes, and every `set` must
  assign one of the readers' own locals. A script that fails is refused,
  recorded as `script_refused`, and not run. The check is lexical: it vets
  the readers' fixed scripts, and it is not a parser for arbitrary
  AppleScript. `tests/test_notch_script_policy.py` collects every
  multi-line and single-line script literal in the readers and requires the
  allowlists to match them exactly.
- **No other AppleScript is compiled in.** `OutboundSender` is replaced by a
  stub in the evaluation build; any call records `outbound-*` and stops the
  app with `EVALUATION_BLOCKED`. `ScriptRunner` stops before any script or
  launch.
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
