# Design: attributing Wisp's bundled oMLX engine

Status: design for review, no code yet. Part of [the bundled-engine plan](2026-10-13-bundled-omlx-engine.md), step 3. Release Auditor review required (security boundary); Simulation QA required for startup/recovery.

## 1. What attribution protects

Wisp sends a bearer key and the user's prompts, mail and messages to whatever answers on the engine port. Attribution makes sure that is the intended engine, checked before request bytes leave and again before every write (`CheckedStream.write` → `check()`). The failure it prevents is an impostor taking the port: a process that binds 8791 before the engine starts, or after it dies, and reads the key and the data.

Trust boundary (unchanged from `DesktopOmlx`): root and the user's own login UID are trusted; other local accounts and sandboxed or unprivileged processes are not. A same-UID attacker who can already edit Wisp's files is out of scope.

## 2. Why the current authority cannot be reused

`CredentialTransport(managed=True)` always builds `RuntimeAuthority()`, which loads `DesktopOmlx`: port 8000, `/Applications/oMLX.app` paths, Team ID `PSK5Q5T46L`, server process named `omlx-server`, parent process `app.omlx` whose own parent is launchd (pid 1). `CheckedBackend` also rejects any port other than the authority's. So the bundled engine on 8791 is refused today (`connected_socket_unqualified`, observed in the wiring test). That is correct and stays the behaviour until this lands.

## 3. New authority: `BundledOmlx`

A subclass of `DesktopOmlx` in a new module `service/inference/bundled_peer.py`, so the audited helpers are reused unchanged: `_csops`, `_csops_string`, `_process`, `_executable`, `_qualified_file`, `_trusted_group`, `_qualified_tree`, `connected_peer` (which already takes its port from the authority). It overrides the class constants and `_listener()` only.

| | DesktopOmlx | BundledOmlx |
|---|---|---|
| `port` | 8000 | 8791 (`BUNDLED_PORT`) |
| Who started the engine | launchd, via oMLX.app (parent chain app → pid 1) | this backend process: `ppid == os.getpid()` |
| Expected pid | whoever listens, then qualified | exactly the pid the engine manager spawned |
| Code root | `/Applications/oMLX.app/.../Python` | `<engine root>/py`, `<engine root>/sp`, `<engine root>/omlx` |
| Signer | oMLX Team ID, hard-coded | the backend's own Team ID (section 4) |
| Reuse / lease | yes (`_DesktopReuse`) | no, full check each time (section 7) |

### 3.1 Binding rules (all must hold, else `AuthRefused`)

`BundledOmlx._listener()`:

1. **One loopback listener.** `lsof -iTCP:8791 -sTCP:LISTEN -Fpufn` returns exactly one record for `127.0.0.1:8791` owned by this UID, and `tcp_listeners()` shows no other listener (wildcard, IPv6, second socket) on 8791. Same two checks as `DesktopOmlx`.
2. **Wisp's own child, by pid.** The listener pid equals the pid the engine manager recorded when it spawned the engine. No recorded child (never started, already stopped, or started by an older backend) means refusal `bundled_engine_not_owned`. A listener left behind by a crashed backend is therefore refused, and the manager's start path kills it.
3. **Same incarnation.** `process_identity(pid, uid)` (pid, uid, start time) equals what the manager captured right after spawn. A recycled pid fails here.
4. **Direct child of this backend.** `ps` shows `ppid == os.getpid()` and the right UID. (`start_new_session` does not change the parent.)
5. **Exact executable.** The kernel's `proc_pidpath` equals the resolved `<engine root>/py/bin/python3`, passed through `_qualified_file`: regular file, owned by root or this UID, not group/other-writable, resolved path equal to the expected path.
6. **Signature, strength depends on the backend** (section 4).
7. **Code trees.** `_qualified_tree` on `<root>/py`, `<root>/sp` and `<root>/omlx` (`DesktopOmlx` covers its interpreter and `server.py` directory; the bundled layout puts site-packages beside the interpreter, so it must be listed explicitly). Same rules: owner root or this UID, no world-write, group-write only when no other account is in the group, link count 1 on files, no escaping symlinks, 50,000 entries, two snapshots must match. The upward walk stops at the parent of the enclosing `.app` (so `/Applications` is qualified exactly as it is for oMLX.app), or at the engine root's parent when running from a source checkout.
8. **Established-connection owner.** `ManagedOmlx.connected_peer` runs unchanged with `port = 8791`: two `lsof` ESTABLISHED snapshots must each show exactly one server record (the pinned pid) and one client record (this backend's pid and fd) for the connection, and `binding()` is re-run before and after.

Why pid pinning instead of the oMLX parent-chain rule: the bundled interpreter is a generic `python3`. Path plus signature alone would accept any script run by that interpreter. The pid pin plus "my child" plus a qualified code tree ties the answering process to the exact process Wisp launched with the exact code Wisp shipped.

## 4. Signature strength comes from the backend, never from a flag

The check reads the backend's own code signature (`csops` on `os.getpid()`).

- **Backend validly signed with hardened runtime and a Team ID T** (every shipped build): the engine must also have `CS_VALID | CS_RUNTIME` (`0x00010001`), Team ID T, and signing identifier `ENGINE_SIGNING_ID` (a constant set by the packaging step). Anything else is refused.
- **Backend unsigned or ad hoc** (source checkout, CI, the preview builds): the engine is accepted on rules 1 to 5, 7 and 8 only, and the strength is logged as `unsigned-dev`.

There is no environment variable, config key or CLI option that selects the weaker mode. A shipped, signed backend can never be downgraded, because its own signature is what the check reads. In dev both sides are unsigned, so nothing weaker than the situation already is.

## 5. Selection and wiring

`CredentialTransport.__init__` currently does `authority or RuntimeAuthority()`. Change to a small selector keyed on the origin port: 8791 → `BundledAuthority()` (a `.load()` wrapper returning `BundledOmlx`, mirroring `RuntimeAuthority.load`), anything else → `RuntimeAuthority()` as today. The port is the only input, so config cannot point the bundled authority at 8000 or the desktop authority at 8791; `CheckedBackend` already refuses connections to any other port.

The module is imported lazily and only for 8791. The mini bundle and staged live-QA packages ship `attributed_transport`, `local_peer` and `inference_errors` only; there an import failure or a request to 8791 is a refusal, never a fallback.

`invalidate()` and the epoch counter behave as for the desktop engine; the `_REUSE.invalidate()` call stays specific to `RuntimeAuthority`.

## 6. The manager's side of the contract

`EngineManager` gains `incarnation()`: `(pid, uid, start_seconds, start_microseconds)` read with `process_identity` immediately after `Popen`, plus the resolved interpreter path. It is `None` when no child is running, and cleared in `stop()`. The pid file already exists for orphan reaping and is not an input to attribution (a file on disk must never grant trust).

If the child exits and restarts, the new incarnation replaces the old one and existing connections fail their next `check()` (pid changed), which is the intended behaviour.

## 7. Performance: what to expect and the follow-up

Without a lease, every connection and every write runs the full check: several `lsof` spawns and `csops` calls. The recorded cost on the desktop engine before attestation reuse was about 0.9 s offline per call; a bundled engine skips the two tree walks only if cached, so expect the same order. This is acceptable for a first, correct version and is measured, not guessed, in the validation below.

Follow-up (separate PR, separate audit): extend per-incarnation reuse to `BundledOmlx`. The pid pin makes this simpler than for the desktop engine (one known pid, native `bsdinfo` and `csops` facts bracketed around a full qualification, tree fingerprints as the fence), but it reuses code that was just stabilised in PR #200, so it should not ride along with the first attribution change. Until then `isinstance(authority, RuntimeAuthority)` stays false for `BundledOmlx` and every reuse branch is skipped.

## 8. Errors and copy

`REFUSED_MESSAGE` says "reopen oMLX", which is wrong for the bundled engine. `refused()` becomes mode-aware: "Wisp couldn't verify its built-in AI engine just now. This is usually momentary — try again. If it keeps happening, restart Wisp." The precise reason code (`bundled_engine_not_owned`, `process_identity_changed`, `desktop_signature_unqualified`, …) still goes only to `.reason` for the debug export.

## 9. Tests (all standalone scripts, real processes, no mocks of the kernel)

Positive:
- The manager's real stub engine, signed-or-unsigned dev path: connection accepted, bytes released, key sent only after `check()`.

Negative (each must refuse and must not send the bearer key):
- Impostor listener on 8791 started by the test (not the manager's child).
- Real engine killed, impostor takes the port: pid and incarnation mismatch.
- Engine restarted: old connection refused on next write, new connection accepted.
- Pid recycled / start time differs.
- Child of a different parent (spawned by a helper process, then re-parented).
- Listener also bound on `0.0.0.0:8791` or `[::1]:8791`, or a second listener.
- Interpreter path swapped (symlink, different file) and executable made group- or world-writable.
- Writable `sp/` or `omlx/` tree, hard-linked file, symlink escaping the root.
- Signed backend with an unsigned or foreign-signed engine (needs the signing fixture from the packaging step; run in CI-signed builds only).
- Mode confusion: desktop authority never accepts 8791, bundled authority never accepts 8000, `CheckedBackend` port check.
- Reason codes appear only on `.reason`, never in the user-facing message.

Performance (informational, not a gate): connect and per-write latency of the bundled check against `DesktopOmlx` on the same machine, 30 samples each, using `scripts/bench_transport_overhead.py` extended with a bundled target.

## 10. Decisions needed

1. **Pid pinning plus unsigned-dev rule (section 4).** The proposal above. Alternative: require a signed engine even in dev, which means every developer build and CI run needs a signing identity and makes the test suite depend on it. I recommend the proposal.
2. **Subclass `DesktopOmlx`, do not refactor into a shared base.** Subclassing keeps the audited code untouched and keeps the diff small. A shared base is cleaner but changes the file that the reuse work depends on. I recommend subclassing now and a refactor only if a third engine type appears.
3. **No reuse in the first version.** Correctness first; the lease follows as its own audited change.
4. **Message copy** in section 8.

## 11. Residual risks

- Same-UID attackers who can edit the engine tree or the backend's files are out of scope (as for oMLX.app). The tree check narrows the window, it does not remove it.
- The child's environment (`PYTHONPATH`, `HOME`) is set by the manager and not re-read from the kernel per write (the existing design also excludes argv and environment, decisions R2/D2).
- Between the last check and the kernel delivering bytes there is the same small window the desktop engine has; the pre- and post-`binding()` bracket is the mitigation.
- Hardened-runtime entitlements the engine needs (`allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation`) weaken what a signature proves about loaded libraries; the tree qualification is what compensates, which is why it is mandatory here.

## 12. Files this will touch

New: `service/inference/bundled_peer.py`, `tests/test_bundled_attribution.py`. Edited: `service/inference/attributed_transport.py` (selector, message), `service/engine/manager.py` (`incarnation()`), possibly `service/inference/local_peer.py` (none expected). Owned by this branch; no other active task is known to own them, to be recorded with the Orchestrator before edit.

## 13. Implementation notes (2026-10-13)

Implemented as designed with the recommendations from section 10. Differences and findings:

- **The shipped interpreter must not re-exec.** Attribution pins the executable path read right after spawn. A framework Python's `bin/python3.x` launcher re-execs into `Python.app/Contents/MacOS/Python` after start, so the live path differs from the recorded one and the engine is refused (`desktop_executable_unqualified`). This is the intended fail-closed behaviour. The packaged engine must therefore be a single executable at `py/bin/python3` (the oMLX.app interpreter already is). Tests start the engine from the final binary for the same reason.
- **A manager that vouches for a process is trusted.** The trust root is the incarnation the manager recorded. Tests that hand the authority a hand-built incarnation of an arbitrary child show it is accepted if it is our child, has the same executable and passes the audits; that models a compromised manager, which is out of scope, not an impostor. Impostors are covered by "no incarnation" and "stale incarnation".
- **Manager retries identity capture** for up to 4 s after spawn (a first-run code-signing assessment can leave a new process briefly unreadable) and refuses to run an engine it cannot identify.
- **Hooks for tests:** `BundledOmlx` exposes `signing_id`, `_python_root()` and `_tree_roots()` so a test can run the engine in place with temp trees. Production behaviour is the class defaults.
- **Not verified yet (needs the packaging step):** the audit of a real signed `py/` tree, and the signed path with a Wisp Team ID and `com.wisp.engine.python3`. The signed-engine rules are covered with a faked kernel signature view plus the real python.org interpreter, whose signature put the test run in `signed` strength.
- **Host condition:** while testing, this Mac's code-signing services stalled, so any newly created executable hung in dyld at launch. That is why tests use the interpreter in place, and why an end-to-end run against the trimmed oMLX engine could not be repeated at the end of the session.
