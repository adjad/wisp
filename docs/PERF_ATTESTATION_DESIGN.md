# oMLX attestation: design for perf items 2 and 3 (T1, T2, T3)

Status: **design only, for independent security review before any implementation.** No product code is changed by this document.
Base: `origin/main` `4994caa` (== installed 1.2.0 build 1194). Date: 2026-10-05.
Scope: `service/inference/attributed_transport.py` (AT) and `service/inference/local_peer.py` (LP), Desktop oMLX path only. The Managed path (`ManagedOmlx`, `check_loaded`, `runtime_manifest`) and the mini gateway keep today's behaviour exactly.

Evidence labels: **[M]** measured in this session by a read-only experiment (scripts and raw output in `docs/perf-attestation-evidence/`); **[E]** measured earlier, cited file named; **[S]** static, read from code at `4994caa`; **[D]** arithmetic from measured parts; **[H]** hypothesis, not verified.

Experiment boundary for this document: disposable processes and loopback sockets created by the probe (ephemeral ports), plus read-only `proc_pidinfo`/`proc_pidfdinfo`/`lsof` of the live oMLX server pid. No connection to `:8000` was opened, no byte was sent to the engine, nothing touched `:8765`, `/Applications/Wisp.app`, the Wisp backend, `~/.moe`, or any model.

---

## 0. Summary

| | Today (1.2.0) | After this design |
| --- | --- | --- |
| Attestation per POST (connect + header write + body write) | ~906 ms [E] | **~41 ms** [D] |
| Attestation per GET (connect + header write) | ~775 ms [E] | **~40 ms** [D] |
| "hello" turn (3 GET + 1 POST) | ~3.2 s [D, E] | **~0.17 s** [D] |
| First request after an oMLX restart, after > W_hard of inactivity, or after any eviction | ~906 ms | ~450 ms (one synchronous re-qualification) [D] |
| Idle CPU attributable to tree walks | ~11-13% of a core (eval A5, inferred) | ~1% of a core while requests flow; 0 when idle [D] |
| Tree re-derived | every connection | every new incarnation, and again at most every W_hard of use |

Recommended combination, in order (section 4): **P1** exec-aware incarnation facts + T1 fence; **P2** per-incarnation authority reuse with demand-driven refresh-ahead (T2, residual window W_hard = 60 s); **P3** native socket-owner lookup with lsof fallback, default OFF with a shadow mode; **P4** flip P3 to default ON after audit and shadow evidence. Per-write re-checking is **kept**; coalescing is not needed once the check costs ~0.3 ms.

Two findings change earlier plans and must be read by the reviewer:

1. **`(pid, start time)` is not an incarnation.** `exec` keeps the pid and the kernel start time. Exec into a different binary changes `pbi_comm`, `proc_pidpath`, `csops` flags, cdhash and signing identifier; exec into the **same** binary changes none of those native facts (only argv) [M, section 1.4]. Any "incarnation compare" (T1, the T2 cache key) must therefore include the exec-sensitive facts, and the remaining same-binary-exec gap is stated as a residual (R2).
2. **`time.monotonic()` does not count sleep on macOS.** It is `mach_absolute_time()`; on this machine `CLOCK_MONOTONIC - time.monotonic()` = 336,679 s (~3.9 days of accumulated sleep) [M]. A TTL measured with `time.monotonic()` would survive any sleep. Every freshness bound in this design uses `time.clock_gettime(time.CLOCK_MONOTONIC)`, which the man page defines as continuing "while the system is asleep".

---

## 1. Threat model

### 1.1 Asset and guarantee

Asset: the oMLX bearer credential (Authorization header) and the request body (prompts, tool results, user data), sent to `127.0.0.1:8000`.

Guarantee today (I1, I2): **no request byte or credential is written to a socket unless, immediately before that write, (a) the process owning the server end of that exact established TCP connection is the qualified oMLX server incarnation, (b) the only other owner of the connection is this Wisp process on the descriptor it is writing to, and (c) the backend epoch has not changed since connect.** "Qualified" = the Desktop adapter's predicate: `omlx-server` child (ps uid, argv[0]) of `/Applications/oMLX.app/Contents/MacOS/oMLX` (ppid 1), both kernel-signed (`CS_VALID|CS_RUNTIME`, identifiers `python3`/`app.omlx`, team `PSK5Q5T46L`), executables at the fixed qualified paths with qualified owner/mode, sole TCP listener on port 8000 among visible processes, and an interpreted-code tree with no world-writable or untrusted-group-writable entries, no foreign owners, no hardlinked files and no symlink leaving the tree; the managed manifest absent.

Documented trust boundary (AT `_trusted_group` docstring) [S]: compromise of Wisp's own login uid is **excluded**; root and `_`-prefixed system uids < 500 are platform.

### 1.2 Adversaries and what stops each one

| # | Adversary / event | Capability | What stops it today | After this design |
| --- | --- | --- | --- | --- |
| A1 | Same-user process squats `:8000` before oMLX, or after oMLX exits (port rebind race) | binds 127.0.0.1:8000 or a wildcard | `_listener`: listener pid must be a signed `omlx-server` child of the signed app; `tcp_listeners()` refuses a second listener; owner check refuses a connection whose server end is not the qualified pid | Same. Connect-time `binding()` (full `_listener`) is kept per connection; a cached authority is served only after its incarnation revalidates (dead or replaced pid fails) |
| A2 | oMLX quits/crashes/restarts between checks; process replacement | new pid (or reused pid) | new `DesktopOmlx` per connection + `_identity` compare + `process_identity` (start time) at each peer check | Revalidation on every `load()` and every write compares pid, start time, ppid, uid triple, comm, path, cs flags, cdhash, identifier, team of server **and** parent; any difference evicts and refuses or rebuilds. Connect-time `process_identity` must equal the cached incarnation |
| A3 | PID reuse | different process gets the old pid | `process_identity` start-time compare at peer checks (not in `_identity`) | Start time (µs) is part of every compare, plus path/cdhash/comm/ppid. Dead pid: libproc returns failure, refusal [M: `bsd(dead_pid)` = None] |
| A4 | A qualified process `exec`s into another binary (keeps pid and start time) | process image changes in place | per-write `binding()`: ps comm, `proc_pidpath`, `csops` re-read | Exec-aware facts per use: comm, path, flags, cdhash, identifier change on exec into another binary [M]. Exec into the **same** binary is invisible natively (residual R2) |
| A5 | fd passing / `dup` / `fork` of either end of the connection | a second process holds the socket | lsof: exactly one server and one client record among **visible** processes | lsof path unchanged. Native path accepts only when neither end has `PROC_FP_SHARED` (fileglob referenced more than once), which also covers holders invisible to lsof (other uids). Any sharing falls back to lsof [M, section 3.1] |
| A6 | Socket hijack on loopback | inject into or take over an established TCP connection | not possible for an unprivileged user (no raw sockets, 4-tuple unique per established pcb) [H, general kernel behaviour]; owner check is the backstop | Same |
| A7 | File tampering in the oMLX tree **by another local account** (or untrusted group member) before qualification | write code that oMLX imports | tree walk refuses writable-by-others entries, foreign owners, hardlinks, external symlinks | Same rules, re-derived per incarnation and at most every W_hard of use (residual R1) |
| A8 | Same tamper **after** qualification | as A7, after the walk | next connection's walk (~3.5 s with pollers) | next refresh, at most W_hard after the walk started (R1) |
| A9 | Same-user tampering with the tree | user owns the files | **Not stopped today**: uid == user passes `qualify`, and the Desktop path hashes no content. Out of scope by the trust boundary. macOS App Management protection of signed bundles may limit it [H] | Unchanged (no weakening, no claim) |
| A10 | Managed manifest appears or changes | switches the adapter | `_manifest_absent()` around every `_listener`; `load()` reads it per connection | Same, and the store is cleared whenever `load()` sees a manifest; key includes manifest state |
| A11 | Group membership change (an account joins gid 20) | makes group-writable files writable by others | `_trusted_group` per instance (per connection) | Re-derived with every refresh (fresh group cache per instance); residual R1 |
| A12 | Epoch change / invalidation between writes | another task invalidates | `CheckedStream.check` compares epoch before and after the peer check | Same, plus a module-level generation compare |
| A13 | Wisp's own process forks (subprocess spawns) while a client socket is open | child briefly holds the CLOEXEC fd until exec | lsof may see two client records -> refusal (transient) | Native path sees `FP_SHARED` -> lsof fallback -> today's verdict. No new acceptance |
| A14 | Machine sleeps with a warm cache | wall time passes without `time.monotonic()` advancing | n/a (no cache) | `CLOCK_MONOTONIC` counts sleep; after wake the cache is stale and is rebuilt synchronously |

Out of scope (unchanged): root/kernel compromise, compromise of the user's uid (A9), compromise of the oMLX signing identity, oMLX's own behaviour after it receives bytes.

### 1.3 Which facts can change during a running incarnation

| Fact | Changes only at (re)start | Can change while the process runs | Cheap native detector |
| --- | --- | --- | --- |
| pid, kernel start time (s, µs) | yes | no (exec keeps both) [M] | `proc_pidinfo(PROC_PIDTBSDINFO)` |
| uid / ruid / svuid | | yes (setuid) | same struct |
| ppid | | yes (parent exits -> 1) | same struct (`pbi_ppid`, currently discarded) |
| `pbi_comm`/`pbi_name`, executable path, cs flags, cdhash, identifier, team | | yes, by exec into another binary [M] | `proc_pidinfo`, `proc_pidpath`, `csops` 0/5/11/14 |
| argv[0] (`ps -o comm=` = `omlx-server`) | | yes, by exec into the same binary or argv rewrite (self-reported) | **none** (needs `KERN_PROCARGS2`, F2) |
| `CS_VALID` | | yes in principle; observed flags include `CS_KILL` on the probes, which kills instead [M, H] | `csops` 0 |
| listener ownership / uniqueness on :8000 | | yes (close/rebind, new binder) | none natively for other processes (`tcp_listeners()` = lsof) |
| owner of an established connection | | only via fd passing/fork/dup by a holder, or death | `PROC_PIDFDSOCKETINFO` + `PROC_FP_SHARED` [M] |
| executable/`server_entry`/boundary-dir metadata | | yes (replacement on disk) | `lstat` fingerprint |
| tree permissions/owners/links | | yes (oMLX itself mutates its Python resources; admin chmod; installers) | **none cheap and sound** (section 2.2) |
| group membership | | yes (admin) | none cheap (`pwd.getpwall()` 3.7 ms [E STAGE0]) |
| manifest presence | | yes | one `open()` |

### 1.4 Experiment: what `exec` changes [M] (`probe_exec.py`)

Disposable `/bin/sh` children, facts read before and after an `exec`:

- exec into another binary (`exec /bin/sleep`): pid, ppid, uid triple and **start time unchanged**; `comm`/`name`, `pbi_flags`, path, cs flags, cdhash, signing identifier changed.
- exec into the same binary (`exec /bin/bash -c "sleep 30; true"`): **no native fact changed** (pid, start, comm, flags, path, cs flags, cdhash, identifier all equal).

So: native facts detect a change of image, not a change of program within one interpreter. Today's per-write `ps -o comm=` sees argv[0], which the process sets itself, so it does not reliably detect that either.

---

## 2. Item 2: per-connection authority (`load()` ~463 ms, 51%)

### 2.1 Where the cost is [E: `wisp-1.2.0-performance-eval.md` A1, STAGE0]

`CheckedBackend.connect_tcp` calls `RuntimeAuthority.load()` per connection; with no manifest it constructs `DesktopOmlx`, whose `__init__` runs `_listener()` including `_qualified_tree` on `python_root` (47,319 entries, two passes, ~397 ms) and `server_entry.parent` (1,390 entries, ~12 ms), plus `_trusted_group` (~4 ms). Keep-alive is off, so this is per request. Per pass: `scandir` names alone ~113 ms; `scandir`+`stat`+`qualify` ~210 ms [E STAGE0]; `lstat` is 18% of a legacy pass; the rest is Python per-entry work.

### 2.2 Options

**(a) Reuse the qualified authority per oMLX incarnation, bounded by W_hard, revalidated cheaply on every use.** Saving ~461 ms/request [D]. Security argument in 2.3. **Chosen**, combined with (c).

**(b) Invalidation signal for tree tampering without re-walking.**
- *Directory mtime/ctime snapshots*: unsound. `chmod`, `chown` and `link()` of a file change only that file's ctime (and nlink), not its directory's mtime. Detecting the A7/A8 classes needs every entry's ctime, i.e. a `stat` per entry, which is the walk.
- *A cheaper sound fingerprint*: none found. APFS exposes no per-subtree change counter to user space. `getattrlistbulk` could fetch attributes per directory in one call and might cut a pass from ~210 ms toward the 113 ms listing floor, but it is new native parsing in a security path (T3 class) and still O(entries); deferred.
- *FSEvents* (`CoreServices` loads and `FSEventStreamCreate`, `FSEventStreamSetDispatchQueue`, `FSEventStreamFlushSync`, `FSEventsGetCurrentEventId` resolve via ctypes [M]): delivery is asynchronous (a change and a request inside the delivery latency race), events coalesce and can be dropped (`MustScanSubDirs`, `UserDropped`/`KernelDropped`), and it needs a native callback thread. It can only ever **shorten** a window, never replace the walk. Rejected for now; a later accelerator may use it **only to invalidate** (any event under the roots, or any drop flag, evicts), never to extend freshness.
- *kqueue per directory*: thousands of descriptors; impractical.

**(c) Keep the full walk at incarnation start and at most every W seconds, off the request path; stale or failed refresh fails closed.** **Chosen** as the refresh mechanism for (a); details in 2.4.

**(d) Lazy/incremental walk with a persisted inventory.** Comparing to a persisted inventory still requires a `stat` per entry (no saving), and a persisted inventory is a new file the user uid can edit. **Rejected.**

### 2.3 What the tree walk actually proves, and why a bounded window is acceptable

- The Desktop path hashes no content [S]. The walk proves only that, **at the moment of the walk**, nothing in the tree is writable or replaceable by a principal other than root/system, the user, or a trusted group, and no link leaves the tree. A same-user writer (A9) passes it today.
- It is a **point-in-time permission audit, not a proof of loaded code**. CPython imports lazily, so code planted in the tree can be imported later; code imported earlier stays loaded after the file is reverted. A cross-account attacker who gets a writable window can plant, wait for an import, and revert; the walk never detected that class even per connection, it only shortened the opportunity to the inter-request gap (~3.5 s with the pollers).
- What the window changes: a persistent misconfiguration (an entry becoming writable by another account) is detected at most **W_hard** after the walk that preceded it started, instead of at the next connection. The attacker still needs a writable entry created by root, an admin, an installer or the user, and attacker-created files carry the attacker's uid and are refused at the next walk unless deleted first.
- Today's verdict is already reused for the whole connection's write phase without re-walking (`_qualified_roots` per instance) [S]. This design extends that reuse from one connection to at most W_hard seconds of one incarnation.

**Residual R1 (needs reviewer acceptance):** a tree permission/owner/link/group-membership change becomes refusing at most **W_hard + write phase** after the start of the last successful walk (write phase = connect to last request write, milliseconds for Wisp's in-memory bodies; bounded by the client write timeout, as today). Recommended W_hard = **60 s** (decision D1).

### 2.4 Chosen mechanism: per-incarnation authority store with demand-driven refresh-ahead

**Store.** Module-level in AT (so all 13 `OMLXClient` construction sites and ad hoc clients share it [E 02 report]), keyed by `(uid, port, app_root, python_root, server_entry, manifest_absent)`, holding at most one `DesktopOmlx` instance plus `generation` (module int), `walk_started_at` (`CLOCK_MONOTONIC`, read **before** the constructor's `_listener`), a build/refresh lock (single flight) and one in-flight refresh future.

**The instance is built only by the existing constructor.** `DesktopOmlx.__init__` keeps running `_listener()` with both tree walks (two passes each). A refresh constructs a **new** instance off the request path and swaps it in; no tree or group state on a published instance is ever mutated. I3 therefore holds verbatim for every instance (section 5).

**Incarnation facts (new, exec-aware), captured around the constructor's `_listener`:** for the server pid and the parent pid: `(pid, start_s, start_us, ppid, uid, ruid, svuid, pbi_comm, proc_pidpath, cs_flags, cdhash, cs_identifier, cs_team)`; plus `lstat` fingerprints `(dev, ino, mode, uid, gid, nlink, size, mtime_ns, ctime_ns)` of the server executable, the parent executable, `server_entry`, and every boundary directory from `python_root` and `server_entry.parent` up to `/Applications`. Facts are read **before and after** `_listener()` and must be equal (bracket), and the after-facts must agree with `_listener`'s result (`pid`, `executable`, `parent_pid`, `parent_executable`); otherwise construction refuses (`desktop_listener_changed`). This closes the "exec between `_listener` and fact capture" race. New helper `local_peer.process_facts(pid)` with the same discipline as `process_identity`: `sizeof == 136` guard, exact return length, `proc_pidpath` via the existing `process_path`, `csops` via the existing parsing; any failure is `AuthRefused`. `process_identity` itself is unchanged (F6).

**`RuntimeAuthority.load()` (still called once per connection; benchmark seam kept):**
1. Read the manifest as today. Present -> clear the Desktop store, bump generation, return `ManagedOmlx` (unchanged path).
2. Absent -> under the store lock: if an instance exists, `revalidate()` it: generation unchanged; `now - walk_started_at <= W_hard` using `CLOCK_MONOTONIC`; current server and parent facts equal the captured ones (all 13 fields each); fingerprints equal. Pass -> return it. Fail -> evict, bump generation, fall through.
3. No valid instance -> build synchronously (single flight; concurrent callers wait for the same build and then revalidate it; a build that observes a generation change since it started is discarded and not published) and publish with its `walk_started_at`.
4. If `now - walk_started_at >= W_soft` and no refresh is in flight, submit one refresh to a dedicated single-worker executor (`wisp-attest-refresh`), never awaited by the request.

Measured revalidation cost: 2 processes x (bsdinfo, path, cs flags, cdhash) + 12 `lstat` + one failed `open()` = **0.063 ms p50, 0.079 p95, 0.81 max** [M].

**Connect path (`CheckedBackend.connect_tcp`) after `load()`:** `binding()` runs the full `_listener()` as today (listener lsof, `tcp_listeners()`, ps, path, signatures, files, manifest before/after, `_identity` compare; trees are the instance's cached verdict), then `process_identity(pid)` must equal the instance's captured server `(pid, uid, start_s, start_us)`; a mismatch evicts and refuses (`process_identity_changed`). This binds the cached tree verdict to the incarnation it was derived for.

**Refresh task:** constructs a fresh `DesktopOmlx` (full qualification, fresh `_group_cache`, two passes per tree). Outcome:
- qualifies and the generation is unchanged -> publish (replacing the old instance; connections already holding the old instance finish with it, their writes still pass the fence).
- `AuthRefused` (any reason), or any other exception (class name recorded only), or generation changed -> evict the current instance, bump generation. The next `load()` builds synchronously and refuses if the condition persists. **A failed refresh never extends freshness.**
- hung -> the instance ages past W_hard and requests rebuild synchronously in `to_thread` (not on the refresh executor); a late result is discarded by the generation compare.

**Eviction (bump generation) on:** any `AuthRefused` raised while using a cached instance (connect, binding, peer check), `CredentialTransport.invalidate()` (network error, close), the quarantine latch (register on `quarantine._gate.callbacks`, as `credentials.py:20` does), manifest appearance, refresh failure.

**Restart, sleep/wake.** oMLX restart: the old pid is dead or its start time differs -> revalidation fails -> rebuild. Sleep: `CLOCK_MONOTONIC` advances during sleep, so after a sleep longer than W_hard the first request rebuilds synchronously (and any oMLX restart during sleep is caught by the facts anyway). Wisp restart: the store is in memory only; nothing is persisted.

**Concurrency and fork.** Instances are immutable after publication (I3); the store holds one reference swapped under its lock; the refresh executor is created lazily, shut down in `_close_inspection_pool()`, and reset in a forked child via `os.register_at_fork` like `_POOL`. The refresh walk competes for the GIL with the event loop for ~0.4 s every ~W_soft while requests flow (the walk already runs in `to_thread` today, on every request).

**Logging (metadata only):** counters and debug events `attest_build`, `attest_reuse`, `attest_evict(reason)`, `attest_refresh(ok|refused(reason)|error(ExceptionClass), duration_ms, entries)`, `attest_age_ms` at use. No paths, ports, pids, tokens, or file names. Exposed through an in-module accessor (export to the debug bundle is a later change; `omlx_client.py` is not touched).

**Expected saving** [D]: `load()` 463 ms -> ~0.5 ms per connection (manifest read + 0.06 ms revalidation): **-~461 ms per request**. Idle CPU from walks: one ~0.4 s walk per W_soft while traffic flows (~1% of a core at W_soft = 40 s), none when idle.

### 2.5 Reconciliation with I3

Old I3: "a new authority instance still re-derives the tree twice before first use". **It still holds for every instance.** What changes is that an instance is no longer new per connection: it is reused across connections of the same incarnation while (i) the walk it was built with started at most W_hard ago by `CLOCK_MONOTONIC`, (ii) the server and parent exec-aware facts and file fingerprints equal those captured around its own `_listener`, (iii) the generation is unchanged, and (iv) the connection's full `binding()` and `process_identity` match it. New I3' (section 5) states this. The pinned tests that encode "a new authority per load" change deliberately (section 5.3).

---

## 3. Item 3: peer checks (3 x ~137 ms per POST)

Each `connected_peer` [S, LP:427]: `socket_identity`; `binding(pid)` (~39 ms, 4 spawns); `process_identity`; lsof ESTABLISHED snapshot 1 (~30 ms); strict parse; snapshot 2 (~30 ms); per-connection record compare; `binding(pid)` (~39 ms); `process_identity`; `socket_identity`. It runs at connect and before every non-empty write (GET: 2 per request, POST: 3).

### 3.1 (a) Native socket-owner lookup (T3) — verified on Darwin 27.0 [M]

Layout (compiled against the CLT SDK 26.0, `layout.c`): `sizeof(struct socket_fdinfo)` = 792, `proc_fileinfo` 24 (`fi_status` at 4), `soi_so` 160, `soi_type`/`soi_protocol`/`soi_family` 176/180/184, `soi_kind` 256, TCP union at 264: `insi_fport` 264, `insi_lport` 268, `insi_gencnt` 272, `insi_vflag` 288, foreign IPv4 308, local IPv4 324, `tcpsi_state` 344; `proc_fdinfo` 8; `proc_bsdinfo` 136. These match report 02's hand arithmetic (foreign +44, local +60 into `in_sockinfo`).

On the running Darwin 27.0 kernel (`probe_sockets.py`, disposable server child + client in the probe):
- `proc_pidfdinfo(PROC_PIDFDSOCKETINFO)` returns exactly **792** bytes into an 1,048-byte buffer (kernel struct == SDK struct).
- Ports are in network byte order in the low 16 bits of the `int` fields (`ntohs` required); addresses, `vflag` = `INI_IPV4`, family 2, protocol 6, kind `SOCKINFO_TCP`, state 4 (ESTABLISHED) / 1 (LISTEN) all parse correctly and **match lsof** (fd, 4-tuple, state) for both ends.
- `fi_status & PROC_FP_SHARED`: clear at baseline (status 2 = CLOEXEC only); **set after `dup()` in the server, set after the server `fork()`s**, cleared after the dup is closed; the same on the client end.
- Cost of one native owner lookup (list the server pid's fds, socket info for each socket fd, plus the own fd): **0.027 ms p50, 0.036 p95, 0.19 max** (200 runs), against ~30 ms for one whole-port lsof ESTABLISHED snapshot [E] (13.6 ms for a per-pid lsof on the tiny probe process [M]).
- Other-uid process: `PROC_PIDLISTFDS` on pid 1 fails with `EPERM`: an owner in another uid is invisible, so the native path cannot find the server record and falls back (fail closed).
- Live oMLX server pid, read-only (`probe_live.py`, no connection opened): its 12 TCP sockets parsed natively equal lsof's fd + 4-tuple + state set in 3 of 3 rounds; the established loopback server sockets show `fi_status` = CLOEXEC only (not shared); the listener is not shared. Raw output not committed (it contains a non-loopback address unrelated to this work).

**Native snapshot** (new `local_peer.native_connection_owners(pid, client_fd, local, remote)`), strictly:
1. `PROC_PIDLISTFDS` on the qualified server pid; buffer sized from a size query plus headroom; refuse to use the result (anomaly) if the return is <= 0, not a multiple of 8, or fills the buffer (possible truncation).
2. For each `PROX_FDTYPE_SOCKET` fd: `PROC_PIDFDSOCKETINFO`, return must be exactly 792 (anomaly otherwise; a vanished fd is skipped only if `errno == EBADF`, anything else is an anomaly). Select entries with kind TCP, family `AF_INET`, protocol TCP, `vflag == INI_IPV4`, state ESTABLISHED, local == `remote` (127.0.0.1:8000) and foreign == `local` (client ephemeral). Require **exactly one** match and `PROC_FP_SHARED` clear.
3. Own end: `PROC_PIDFDSOCKETINFO(getpid(), client_fd)`: same field checks with local == `local`, foreign == `remote`; `PROC_FP_SHARED` clear.
4. **Per-use layout oracle:** the parsed own-end ports and addresses must equal `getsockname()`/`getpeername()` of the same socket (already read by `socket_identity`). This re-proves the offsets and byte order on every check for free; a mismatch is an anomaly and latches native lookup off for the process lifetime.
5. Result: `(server_fd, server_gencnt, server_so, client_gencnt)`.

**Use inside `connected_peer`:** two native snapshots (the bracket structure is kept), which must be equal including `gencnt` (inpcb generation) and `soi_so`; the returned owner tuple stays `(incarnation, server_fd, endpoint)` so the `actual != self.owner` equality in `CheckedStream.check` keeps its meaning.

**Fallback, never allow.** Any anomaly at any step (non-exact return size, errno other than the listed one, no match, more than one match, `FP_SHARED` on either end, oracle mismatch, any exception) runs **today's lsof path verbatim** (both snapshots, same parser, same reasons), and lsof's verdict stands. The native path itself never refuses and never accepts on an anomaly; it can only accept the unambiguous case. Native-accept is a subset of lsof-accept: lsof requires exactly one server record `(pid, uid)` and one client record `(our pid, uid, fd)` among visible processes; the native accept requires the same 4-tuple in exactly one fd of the qualified pid and our fd, both unshared, while the uid triple is already pinned by the incarnation compare. Holders that lsof can see (A5) set `FP_SHARED`; holders lsof cannot see (other uids) also set it, so they now cause a fallback instead of a silent lsof accept. Verdicts are therefore identical to today (I4); only the time differs.

A13 note: while Wisp forks a subprocess, the child holds the client fd until exec, so `FP_SHARED` can be transiently set on the client end [H, from the fork experiment]. The fallback makes this today's behaviour (lsof may also see two client records and refuse, a retry-free finding as today). No new transient reason is introduced (I6).

**Flag:** module constant `NATIVE_OWNER_LOOKUP` with states `off` (default in P3), `shadow` (run native and lsof, use lsof's verdict, count disagreements as metadata), `on` (P4, after audit and shadow evidence). A process-lifetime latch turns native off after any oracle mismatch or size mismatch.

**Saving** [D]: 2 x ~30 ms -> 2 x ~0.03 ms per peer check: **-~60 ms per check, -~180 ms per POST** (-~120 per GET).

**Tests:** differential native vs lsof on disposable loopback servers (plain, dup in server, fork in server, dup in client, server closed -> CLOSE_WAIT, two connections to the same server, listener only); fault injection on every anomaly class (return 791/793/0/-1, EPERM, ESRCH, truncated list, odd length, wrong family/vflag/state/kind, port byte-swapped, oracle mismatch, `FP_SHARED`) each asserting **lsof was run and its verdict returned**; a `sizeof`/offset guard test (ctypes struct size 792 and field offsets equal the table above); a test that native never returns acceptance after an anomaly (mutation: replace fallback with `return owner`).

### 3.2 (b) Replace the two `binding()` calls inside each peer check with an exec-aware incarnation compare (T1)

Replace `self.binding(pid)` (both, LP:431 and LP:459) for **Desktop only** with `self.peer_fence(pid, incarnation)` = `revalidate()` from 2.4 minus the age check (facts of server and parent, fingerprints, manifest absence, generation), then `process_identity == incarnation` as today. Managed keeps `binding(pid)` via its own `peer_fence` that calls `self.binding(pid)` (behaviour unchanged).

What the dropped per-write `_listener` proved, and what replaces it:

| Fact re-proved per write today | After T1 |
| --- | --- |
| pid is still the listener of 127.0.0.1:8000; sole listener on 8000 | not re-proved per write. Irrelevant to an already-established 4-tuple: a new or changed listener cannot take over an established connection; the owner check proves the actual peer. Still proved per connection by the connect-time `binding()` |
| ps uid, argv[0] == `omlx-server`, parent ppid 1 and argv | uid triple and ppid per write (native); parent facts per write; argv[0] only per connection (residual R2) |
| path, file qualification, signatures of server and parent | path, cs flags, cdhash, identifier, team per write (exec-sensitive); file metadata by `lstat` equality (stricter than the predicate) |
| manifest absent | per write (one `open()`) |
| tree | cached per instance today, cached per instance after (no change within a connection) |

**Residual R2 (needs reviewer acceptance):** between the connect-time `binding()` and a write (milliseconds), an exec of the qualified server into the **same** interpreter binary, or an argv rewrite, is not re-detected per write. Only code already executing inside the qualified, signed process can do either, and today's per-write check only reads argv[0], which that same code controls. No external adversary gains anything.

**Saving** [D]: -2 x ~39 ms per check = **-~78 ms per check, -~233 ms per POST** (-~155 per GET), plus 8 fewer spawns per check. Fence cost ~0.06 ms [M].

**Tests:** existing per-write tests unchanged in verdict; new: exec into another binary between connect and header write (disposable child process whose facts are read natively) refuses; parent exit (ppid -> 1) refuses; pid reuse fake refuses; manifest appears between writes refuses; fingerprint changes refuse; R2 pinned as a named `PINNED_RESIDUAL` test.

### 3.3 (c) Coalescing per-write re-checks

The connect check and the header-write check run back-to-back with no bytes between them; the header-write and body-write checks fence the race "the epoch or the peer changes after the credential was sent and before the prompt is sent" (`test_epoch_change_between_header_and_body_write_sends_only_the_header`). Dropping the **connect-time** owner check while keeping a full check before every write would keep I1/I2 exactly (every byte still follows a fresh check), saving one check per request. Dropping or caching the **body** check would lose the header-to-body fence and is rejected.

**Decision: do not coalesce.** With T1 + T3 a check costs ~0.3 ms; the connect-time check saves < 1 ms and is the early refusal that closes the stream before httpcore writes anything. Revisit only if T3 is rejected (then dropping the connect-time owner check alone saves ~61 ms with no change to the write guarantee).

### 3.4 (d) Caching lsof/proc results within one write sequence

A cached owner result is not a fence; it is coalescing with extra steps. **Rejected.**

---

## 4. Recommendation, order and totals

| Step | Change | Per POST after step [D] | Residual added |
| --- | --- | --- | --- |
| today | | ~913 (load 463 + binding 39 + 3 x 137) | |
| P1 | `process_facts` + T1 fence (Desktop) | ~680 (463 + 39 + 3 x ~60) | R2 |
| P2 | per-incarnation store, refresh-ahead, W_hard | ~221 (0.5 + 39 + 3 x ~60) | R1 |
| P3 | native owner lookup, `off`/`shadow` | ~221 (`off`) | none (verdict parity) |
| P4 | native owner lookup default `on` | **~41** (0.5 + 39 + 3 x ~0.3) | none |

GET: ~40 ms. "hello" turn: ~3.2 s -> ~0.17 s of attestation. First request after restart/idle > W_hard: ~450 ms.

Order rationale: P1 builds the exec-aware facts that P2's key and revalidation need, and is the smallest guarantee change. P2 is the largest saving and the only one with a time window, so it gets its own review. P3 lands dark, so the struct code can be audited and shadow-compared on real traffic before it decides anything.

**Deliberately not done:**
- The connect-time `binding()` (~39 ms, 4 spawns) stays per connection. It is now the dominant remaining cost. Amortizing it would drop per-connection listener uniqueness and argv[0] checks (decision D4, recommended: not now).
- No coalescing of write checks; no result caching within a write sequence.
- No FSEvents, `getattrlistbulk`, persisted inventory, or `KERN_PROCARGS2` parsing.
- No change to Managed, `runtime_manifest`, the mini gateway, keep-alive (`max_keepalive_connections=0`), readiness call counts, `omlx_client.py`, `process_identity`.
- No fix of the pinned blind spots (unreadable directories, ACLs, setuid bits, symlink chains); they remain pinned and unchanged.

---

## 5. Invariants (extending I1-I10)

I1, I2, I4-I10 of the T0 plan stand unchanged. I3 is replaced by I3'. Each line names the proving test (new tests go in `tests/test_runtime_attestation_t1t3.py`; existing names are kept).

- **I1** No request byte or credential before connect-time owner verification. *`test_nothing_is_written_before_verification_and_every_write_is_rechecked`, `test_rogue_runtime_connection_gets_zero_bytes` (rerun with a warm store).*
- **I2** Every non-empty write re-runs `connected_peer_with_retry` on the connection's authority instance; no skipping, coalescing or caching. *Existing write-discipline tests, unchanged.*
- **I3'** Every `DesktopOmlx` instance runs the full `_listener` and re-derives each tree twice (same predicate, fields, limit, equality) before first use; a refresh builds a new instance the same way; an instance is served to a connection only if its walk started <= W_hard ago by `CLOCK_MONOTONIC`, its exec-aware facts and fingerprints equal the bracketed capture, the generation is unchanged, and the connection's `binding()` and `process_identity` match it. *`test_every_new_authority_walks_each_tree_twice` (unchanged), new `test_store_serves_only_fresh_matching_instances`, `test_refresh_builds_a_new_instance_and_never_mutates_a_published_one`, `test_connect_incarnation_must_equal_cached_incarnation`.*
- **I11** Freshness uses `CLOCK_MONOTONIC` (counts sleep). *`test_freshness_clock_counts_sleep`: inject a clock jump without `time.monotonic` moving -> rebuild; mutation swapping to `time.monotonic` is killed.*
- **I12** A failed, refused, errored, hung or generation-stale refresh never extends freshness and evicts. *`test_refresh_failure_evicts_and_next_load_requalifies`, `test_hung_refresh_ages_out_to_synchronous_rebuild`, `test_late_refresh_result_is_discarded_after_invalidate`.*
- **I13** Incarnation facts are exec-aware: pid, start, ppid, uid triple, comm, path, cs flags, cdhash, identifier, team for server and parent, read before and after `_listener` and equal. *`test_exec_into_other_binary_is_refused_at_next_use` (disposable process), `test_facts_bracket_rejects_change_during_listener`, `test_ppid_change_refuses`, `test_pid_reuse_refuses`.*
- **I14** Any `AuthRefused` while using a cached instance, `invalidate()`, the quarantine latch and manifest appearance evict and bump the generation. *`test_eviction_triggers` (parametrized).*
- **I15** The store is module-level and keyed; another port, app root or manifest state never reuses an entry. *`test_store_key_isolation`.*
- **I16** Native owner lookup never accepts on an anomaly and never refuses on its own: every anomaly runs today's lsof path verbatim and returns its verdict. *`test_native_anomaly_falls_back_to_lsof` (parametrized over every anomaly class), mutation "fallback -> accept" killed.*
- **I17** Native accept implies lsof accept (differential on disposable sockets, both modes). *`test_native_equals_lsof_on_disposable_connections`, shadow-mode counters.*
- **I18** Native parsing asserts return size 792 and re-proves offsets/byte order on every use against `getsockname`/`getpeername`; a mismatch latches native off. *`test_socket_fdinfo_layout_guard`, `test_own_end_oracle_mismatch_latches_off`.*
- **I19** Desktop peer fence keeps the bracket: fence, two owner snapshots, fence, `process_identity`, `socket_identity`, in that order. *`test_peer_check_event_order` (records the call sequence).*
- **I20** Logged diagnostics contain no paths, ports, pids, file names or credentials. *`test_attestation_diagnostics_are_metadata_only`.*

### 5.1 Negative controls (each must refuse or rebuild, never send bytes to a non-qualified peer)

Synthetic only: fakes, disposable processes and spare loopback ports; never `:8000`, `:8765`, the installed apps or user data.

| Control | Setup | Expected |
| --- | --- | --- |
| Rogue listener | warm store, rogue on the spare port | zero bytes (`test_rogue_runtime_connection_gets_zero_bytes` with warm store) |
| Replaced process mid-session | kill spare server, start another on the same port | revalidation fails, evict, rebuild refuses the rogue |
| PID reuse | fake facts: same pid, different start | evict, refuse |
| Exec into another binary mid-session | disposable child execs between connect and header write | fence refuses before the header |
| Exec into same binary | disposable child | not detected per write (PINNED_RESIDUAL R2); detected per connection only through argv checks |
| Tree tamper after qualification | add world-writable entry / hardlink / external symlink / foreign owner after warm-up | accepted until refresh or W_hard (PINNED_RESIDUAL R1); refresh refuses and evicts; next load refuses |
| Group membership change | fake `grp`/`pwd` after warm-up | caught at next refresh only (PINNED_RESIDUAL R1) |
| Epoch change between header and body | existing test | header only; plus generation bump variant |
| Port rebind | spare server exits, rogue binds the same port inside W_hard | rebuild refuses |
| Stale cache after restart / sleep-wake | new incarnation; clock jump > W_hard | rebuild; never served stale |
| Background check failure | refresh raises `AuthRefused`, `OSError`, `RuntimeError`, hangs | evict / age out; next load requalifies |
| Manifest appears / disappears | create, then remove | `desktop_authority_superseded`; fresh instance after removal |
| File fingerprint change | exe inode, `server_entry` mode, boundary dir owner | evict at next use |
| Fork/dup of either end | disposable server forks / dups | native fallback; lsof verdict |
| Concurrency | 20 connections racing a build; `invalidate()` during build | one build; discarded build not published |

### 5.2 Red / green / mutation

- **Red first:** each new test is committed and shown failing on `4994caa` where it encodes new behaviour (store, fence, native), or passing where it pins existing behaviour; record both lists.
- **Green:** full transport suites (`test_runtime_attestation_t0.py`, `test_runtime_proc_path.py`, `test_runtime_peer.py`, `test_web_response_followup.py`) plus the new file, under Python 3.13 (bundled class) and 3.14, `TZ=UTC` and `Pacific/Kiritimati`, and the CI-like sandbox (no sockets, no exec of chmod), via `scripts/test_replay_failure_fixes.py`-style runners (bare `pytest tests/` runs zero tests).
- **Mutation (minimum set, every survivor killed or listed as a gap):** clock source; `<=` vs `<` on W_hard and W_soft; generation compare removed; each of the 13 fact fields dropped from the compare (26 mutants); bracket removed; fingerprint field dropped; refresh failure path publishing instead of evicting; native: `FP_SHARED` mask, `ntohs` removed, state constant, family/vflag check, size compare, exactly-one -> at-least-one, oracle compare removed, fallback -> accept; fence order swapped.

### 5.3 Pinned tests that change deliberately (listed for the reviewer)

- `test_runtime_authority_builds_a_new_desktop_authority_for_every_load` -> becomes "returns the same instance while it revalidates; a new one after eviction or W_hard".
- `test_PINNED_BLIND_SPOT_tree_verdict_is_cached_for_the_instance_lifetime` and `..._group_verdict_is_cached_per_instance...` -> renamed `PINNED_RESIDUAL_R1_*`, asserting the bound W_hard.
- `test_packaged_desktop_peer_path_uses_stable_lsof_without_netstat` -> its fake patches `binding`; under T1 it patches `peer_fence` instead. With native `off` it still sees exactly two lsof calls; a new sibling asserts zero lsof calls with native `on` and an unshared fake owner, and two with any anomaly.
- `test_binding_repeats_every_spawn_but_walks_no_tree_on_the_same_instance`, `test_every_new_authority_walks_each_tree_twice`, `test_desktop_runtime_cache_is_scoped_to_one_authority`, `test_a_new_authority_is_loaded_for_every_connection`: **unchanged** (constructor and per-connection `load()` behaviour are kept).

### 5.4 Benchmark seams to preserve (F7)

`CredentialTransport.handle_async_request`, `RuntimeAuthority.load` (still once per connection; cost drops), `attributed_transport.inspect_command` and `tcp_listeners` (looked up at call time), `DesktopOmlx.binding` (stays in `vars(DesktopOmlx)`; count drops from 7-8 to 1 per request), `ManagedOmlx.connected_peer` (still the shared entry that `DesktopOmlx.connected_peer` delegates to by class lookup at call time). Lower `process_check`/`binding_check` counts are the intended improvement, not lost instrumentation. `test_benchmark_instrumentation_seams_exist_with_current_signatures` stays green. The background refresh is not a request-path surface; its walks appear in the new attestation counters, not in `authority_load`.

---

## 6. Failure and rollback

- **Kill switch:** `WISP_ATTESTATION_LEGACY=1` forces today's behaviour (new instance per `load()`, `binding()` inside each peer check, lsof only). It can only make attestation stricter and slower, so reading it from the environment adds no trust (I7's concern is trust *granted* by environment values). Default unset.
- **Defaults:** P1 and P2 on when merged (they are the reviewed design); native owner lookup `off` in P3, `shadow` possible by constant, `on` only in P4.
- **Native call failures:** any failure, unexpected errno, size or oracle mismatch -> lsof path; size/oracle mismatch also latches native off for the process lifetime and records one metadata event. Never an accept.
- **Store failures:** any exception inside the store or revalidation is `AuthRefused` (I5) and evicts; the next `load()` requalifies from scratch.
- **Rollback:** each PR is independently revertible; P4 reverts by flipping the constant.
- **Diagnostics:** the counters in 2.4 and 3.1 (`attest_*`, `native_owner_{accept,fallback(anomaly_class),latched_off}`, `shadow_disagreement`), metadata only.

---

## 7. Open decisions for the user

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| D1 | Residual window R1 for tree/group changes | W_hard = 30 s / **60 s** / 300 s (W_soft = 2/3 of it) | **60 s.** ~1% CPU while active; a misconfiguration that lets another account write oMLX code is refused within a minute instead of a few seconds |
| D2 | Accept residual R2 (argv[0]/same-binary exec not re-checked per write) | accept / keep `binding()` per write | **Accept.** Only code inside the signed process can trigger it; today's check reads a value that code controls |
| D3 | Native owner lookup rollout | keep `off` / `shadow` then `on` | **`shadow` then `on`** after Auditor PASS and >= 500 shadow checks with zero disagreements |
| D4 | Amortize the connect-time `binding()` (~39 ms) | per connection / per W_hard | **Per connection (not now).** Revisit after P4 is measured |

---

## 8. PR-sized implementation plan with gates

Owned paths: `service/inference/attributed_transport.py`, `service/inference/local_peer.py`, new `tests/test_runtime_attestation_t1t3.py`, the four pinned test files only where 5.3 says, `scripts/bench_transport_overhead.py`. One writer, own worktree from fresh `origin/main`. Every PR: frozen exact SHA, mechanical evidence, required CI, **Release Auditor** (security boundary + native integration + concurrency), **Simulation QA** with disposable processes and spare ports only. No Live QA against the installed app. No rebuild or relaunch of Wisp without separate user authorization.

1. **P0, pin (tests only).** Add harness pieces (fake facts provider, injectable `CLOCK_MONOTONIC`, disposable exec/fork/dup socket fixtures), the R1/R2 residual pins against today's behaviour, and the native differential harness (skipped while the code is absent). Gate: green on base; mutation of the harness killed.
2. **P1, facts + T1.** `local_peer.process_facts`, Desktop `peer_fence`, Managed unchanged. Gate: I13, I19, existing suites unchanged; R2 pinned; measured peer check ~137 -> ~60 ms (bench script, >= 21 alternating runs).
3. **P2, store + refresh-ahead.** Gate: I3', I11, I12, I14, I15, the negative-control table, concurrency soak (200 sequential + 20 concurrent requests on a spare-port fake, zero refusals, no leaked threads or children), measured `load()` p50 < 5 ms warm, idle CPU sample. D1 must be decided before this PR's frozen SHA.
4. **P3, native lookup (`off`/`shadow`).** Gate: I16-I18, layout and differential tests, anomaly matrix, mutation set; shadow run on a disposable server; Auditor confirms struct reading.
5. **P4, default `on`.** Gate: D3 evidence (shadow counters, zero disagreements), Auditor on the one-line flip, end-to-end A/B with the release benchmark harness (or the bench script if no approved baseline, stated plainly).

Kill criterion: if P1 + P2 do not reduce the measured per-request attestation by at least 50%, stop and report before P3.

---

## Appendix: evidence files (`docs/perf-attestation-evidence/`)

- `layout.c` + `layout.out`: SDK struct sizes and offsets.
- `probe_sockets.py` + `probe_sockets.out`: disposable loopback differential, `FP_SHARED`, timing, EPERM on another uid.
- `probe_exec.py`, `probe_exec_same.py` + outputs: exec detection, revalidation cost, clock comparison.
- `probe_live.py` (read-only, live oMLX pid; output summarized in 3.1, not committed).
- Cited: `/private/tmp/wisp-1.2.0-performance-eval.md` (A1, A5), `t0-evidence/STAGE0.md`, coordination reports 02 and 05.
