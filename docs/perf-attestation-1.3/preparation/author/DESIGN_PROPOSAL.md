# Proposed F1–F5 repair of the Wisp 1.3 desktop attestation design

Status: **author proposal for fresh independent review; no finding is closed and no enabled candidate is approved.** Actor `/root/attestation_design_analysis`; parent `01a112be-3e4e-7833-99e8-eda89e84c53b`. Exact source main `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`; original design `1449ab162c2f92e73162950240054964af636cdf`. `INPUT_HASHES.json` identifies the immutable inputs. `RELEASE.json` records the explicit parent release following acceptance.

This proposal supersedes the original design's lease, native parity, refresh and D2 claims for consideration. It changes no repository file. It neither transfers Claude's product ownership nor releases the pending builder. Final 1.2 reconciliation, the explicit 1.3 NO-MERGE hold, and all later activation/deployment holds remain.

## 1. Preserved behavior and boundaries

The preparation policy is `legacy`: new Desktop authority at each existing load, full `binding()` at the existing connect and peer fences, two complete production lsof established inventories, existing parser, reasons and retry ordering. Reuse, replacement fences and native authorization are **default-off** independently. No optimized fact, clock, store or decoder result authorizes a request before its enabling stage and human decisions are admitted. Absence of `WISP_ATTESTATION_LEGACY=1` grants no permission to enable anything. Future enabling policy is startup-only and explicitly bound to the admitted candidate/profile; an atomic one-way runtime rollback may disable it by revoking all old optimized leases/permits. No runtime re-enable or environment toggle renews a lease.

Managed admission, its manifest format and `runtime_manifest`, gateway, keepalive setting, credential lookup, readiness/model-residency reuse and `engine_epoch` stay outside the change. No modification of `process_identity(pid, uid)` or its `(pid, uid, start_seconds, start_microseconds)` result. Preserve the constructor's exact tree rules, two passes, limits, group rule and named blind spots. Preserve instrumentation seams by their current signatures and module/class lookups. No change to transient reason set, three-attempt retry maximum, fixed command environment, timeout or output cap. New permanent lease/revocation refusals must not be reclassified as transient inspection failures.

PR #161's `_close_inspection_pool()` stays synchronous and non-waiting, and transport cleanup gains no await or worker join for attestation teardown. Running inspection work is allowed to finish; finishing conveys no right to publish or write. PR #162 readiness reuse is neither an attestation oracle nor a dependency to edit.

## 2. F1: immutable qualification lease at every write

### Lease data and validity

For the proposed enabled reuse path, publish an immutable `QualificationLease` containing: cache key; unique qualification ID; global revocation generation; per-key revocation generation; clock-domain ID; process-lifetime ID; full server/parent facts and boundary fingerprints; authority reference; absolute qualification start and absolute expiry. Capture the start **before** every qualification input is read, including group lookup and first tree pass. Expiry is `qualification_start + W_hard`; constructor completion, publication, connect, refresh and successful use never move it. Require finite, ordered timestamps and the same sleep-inclusive domain; valid means `start <= now < expiry`. Equality with expiry refuses. A build whose qualification consumed its lease is never published.

`W_hard` remains unchosen. There is no implicit connection grace or `+ write phase`. A new refresh creates another immutable lease. An old stream retains exactly its original lease and absolute expiry; it cannot borrow a refresh's timestamp or replace its authority mid-request. Expiry closes/refuses that stream; a new connection can perform fresh full qualification. No automatic request replay occurs after a header or partial body may have been sent.

Use an explicit injectable Darwin sleep-inclusive clock, checked for support during qualification. Existing Python monotonic timeout facilities cannot certify this lease domain. Clock read errors, unsupported domain, nonfinite values or backwards readings disable optimized admission; before connect use the unchanged legacy path, while an optimized stream already exists refuse and close it. A failure never resets the start or promotes an expired lease.

### Write and connect ordering

Every nonempty `CheckedStream.write` on an optimized stream must perform these named steps; empty writes send no bytes and grant no freshness credit:

1. `PRE_INSPECTION`: verify backend epoch, exact stream/socket object and endpoint, process-lifetime ID, both revocation generations, policy/clock domain, quarantine state and immutable lease time. No passing snapshot may be memoized across writes.
2. `PEER_INSPECTION`: await the existing complete peer/retry path in a worker. The worker brackets its owner inspection with qualified facts/manifest/fingerprints and its own generation/lease checks. In the default policy these are additional observations, while today's binding/lsof decision remains authoritative.
3. `POST_INSPECTION`: on the event-loop side, reread lease time and global/key generations after the await; compare actual owner with the stream's owner, backend epoch, stream/socket and quarantine/policy. A worker-side pass cannot survive a cross-client invalidation during the await. Crossing hard expiry during a retry or fact read refuses before dispatch.
4. `WRITE_DISPATCH`: validate the same immutable lease and revocation token immediately before releasing a nonempty buffer to the lower transport. Any intervening suspension, including inside the lower stream, must invalidate a reusable dispatch permit and require a fresh time/revocation check before its next emission. Capture no credentials in diagnostics.
5. `POST_WRITE`: after the await returns, check revocation/expiry again; failure closes and stops later body/chunk writes. This check cannot recall bytes already submitted and is not evidence that the preceding bytes were prevented.

Connect has the same post-await checks after load, binding, identity and peer inspection and immediately before returning a stream. A connect-time pass never substitutes for a write check. A close, manifest supersession, quarantine or another client's invalidation can revoke a lease even when this backend's epoch stayed unchanged.

**Linearization point LP-W:** authorization for each nonempty emission is the final lease/generation comparison immediately before submission to the underlying OS write, under a dispatch contract whose no-unchecked-suspension property is proven. Revocation comparison and nonblocking emission submission must be serialized against LP-R by a shared, short dispatch gate or an independently proven equivalent; it must never hold a lock across an await or blocking write. A comparison performed only before releasing that gate is insufficient. LP-R revocation wins if its generation increment precedes LP-W. Bytes already submitted before LP-R cannot be recalled, and kernel delivery can occur later. This is a sampled authorization boundary, not atomic proof of uninterrupted peer/process exclusivity.

**Unmeasured dependency:** pinned main awaits `self.stream.write` after `check`; it does not establish LP-W inside an asynchronously suspending lower writer. Before enabling reuse, independently qualify the concrete bundled lower transport or add a separately owned, reviewed dispatch seam that checks deadline/revocation before every resumed emission. A fake proving only wrapper call order is insufficient. An arbitrary thread preemption/sleep between the sample and syscall also cannot be ruled out by Python checks; do not claim a strict physical-delivery deadline. If the reviewer/human requires such a deadline, keep reuse disabled pending a stronger enforceable transport design. No lease timeout or `wait_for` alone proves that a canceled write emitted zero bytes.

**Corrected R1:** at each qualifying LP-W, the reused tree/group permission audit started less than W ago. This bounds admitted reuse age, not tampering residence in memory or delivery time. The two-pass walk is a permission audit, not an atomic snapshot or loaded-code measurement. A transient writable interval can plant lazily imported code whose effects persist after permissions/files are restored; W does not bound that consequence. Group and tree changes discovered by a valid refresh revoke immediately; otherwise reuse expires without requiring another `load`.

## 3. F4: short locks, revocation generations and publication tokens

### Records and named states

Separate revocation generations from successful replacement revisions. The store has one short-held metadata lock, a global revocation generation, process-lifetime ID and policy revision. Cache key dimensions are `(uid, port, app_root, app_executable, python_root, server_entry, manifest_path, manifest_absent, qualification_rule_revision, policy_revision)` with the expected signing predicates bound to the qualification-rule revision. No path alias normalization can manufacture an equivalent key. Each key has a revocation generation, replacement revision, zero/one published lease, zero/one current publication token and metadata state `EMPTY`, `BUILDING`, `READY`, `REFRESHING`, `EXPIRED`, `REVOKED` or `CLOSED`. Active running jobs are tracked separately; an abandoned job still consumes its real worker budget.

A `BuildToken` is immutable `(process_lifetime, key, global_generation, key_generation, policy_revision, expected_revision, unique_nonce, initiating_transport_lifetime, absolute_build_deadline)`. This identifies one job's publication authority, not the process incarnation. Neither constructors, process/native inspection, filesystem/group work, retries, future waits nor logging run under the metadata lock. Snapshot/reserve/CAS/revoke/detach only; no awaits under it.

| Transition | Condition and operation | Linearization point |
| --- | --- | --- |
| `EMPTY/EXPIRED → BUILDING` | Reserve current token if worker budget admits it; other callers become bounded waiters. | LP-B: token installed under metadata lock. |
| `READY → REFRESHING` | Demand only, after soft threshold and when no current token; old lease remains usable only to its own expiry. | LP-B for refresh token. |
| `BUILDING/REFRESHING → READY` | Complete fresh qualification and post-facts; CAS exact token, generations, process lifetime, expected revision and issuer lifetime; clock/lease/manifest/policy still valid. Replace lease, increment replacement revision, clear token. | LP-P: successful CAS. |
| `* → REVOKED` | Current-token qualified finding/error, explicit invalidate, quarantine, manifest transition, policy rollback or detected process/fingerprint mismatch increments applicable revocation generation, clears lease and current token. | LP-R: generation increment. |
| `READY/REFRESHING → EXPIRED` | `now >= lease.expiry`; admission refuses. Token timeout handling is independent. No renewal or grace. | First invalid-time check; later dispatch always checks time. |
| `* → CLOSED` | Mark initiating transport lifetime closed and detach its publication authority synchronously; cancel queued owned work where safe; never wait on running work. | LP-C: lifetime tombstone under short lock. |
| timed-out token → no publication authority | Retire current token by CAS if still current; keep running job in outstanding budget until it actually ends. A timeout revokes that current generation conservatively. | LP-T: token retirement/generation increment. |

**Stale completion is a strict no-op:** a job whose token no longer owns the slot may only settle its private future/release its outstanding-worker accounting. Stale success, failure, `AuthRefused`, timeout or cancellation does not evict, bump any generation, replace a lease, submit another job or publish a diagnostic implying current rejection. A newer independently qualified lease survives. Thus G0 job R cannot evict a G1 publication B. The same rule applies to failure from an old stream: CAS its captured generations before attempting a key revocation; a stale stream can close itself but cannot destroy a new generation.

A current token's refusal/error revokes its key's lease and all same-key connections, with new qualification needed. Successful same-incarnation refresh changes the replacement revision only, so old leases may finish only until their own expiry. Successful refresh with changed process facts is **not** same-incarnation replacement: revoke the prior generation, discard this token, and let a new-generation full build reserve independently. No accidental migration of old streams.

### Bounded wait and hung work

Proposed initial optimized-store budget: at most **two running qualification jobs per process**, with at most one current job per key; one refresh may run and one reserve slot is retained for foreground recovery. Submit no unbounded queue: reserve a real slot before enqueueing; callers waiting for an existing build do not each create jobs. Waiters have absolute sleep-aware deadlines bounded by their caller deadline and the token deadline. Proposed build timeout `T_build` must be an explicit finite policy value qualified on healthy supported installs before enablement; no numeric value is justified by this static analysis. Its acceptance evidence must include cold/warm two-pass tree costs, contention and worst ordinary command timeout sequences.

If a refresh hangs, its token loses publication authority at `T_build`; the running thread is not called canceled and continues consuming one slot. At hard lease expiry, foreground may reserve the other slot with a fresh token without acquiring a lock held by the hung constructor. If both real jobs hang, new optimized builds refuse/degrade for availability until a job truly ends or the process restarts; they never create replacement executors/threads indefinitely. Default-off legacy is unchanged; using legacy cannot extend an existing optimized lease. A restart/close storm must not reset the outstanding-worker budget for threads that still exist. Bounded refusal is intentional; an unconditional promise of prompt successful rebuild is removed.

Caller cancellation detaches its waiter; a shared current job may remain only if its token has a live initiating lifetime and bounded ownership. Closing that initiating transport retires its token before awaitable HTTP-pool cleanup and detaches executor cleanup synchronously in `finally`, using `shutdown(wait=False)` semantics. Closing another transport can increment the optimized global generation by policy, but cannot cancel/join unrelated main inspection tasks. This conservatively invalidates other optimized streams at their next LP-W; the availability effect requires later tests. A closed transport can never be reused.

Fork resets the child's lock, process lifetime, store, token/publication references and worker accounting; inherited streams/leases fail their process-lifetime check. The child has no parent worker threads and cannot wait on them. Parent bookkeeping remains independent. The reset handler must do no native work or blocking joins.

Manifest appearance or any unsafe/non-absence outcome takes the existing manifest/refusal branch and revokes Desktop generations before any optimized admission. Disappearance never revives an old lease: a new full build is required. Keep the underlying manifest format/parser and Managed behavior untouched.

### Store/write invariants

- L1: an immutable lease's start/expiry and qualification inputs never change after publication.
- L2: every nonempty optimized emission passes LP-W with that connection's own unexpired lease; no old connection inherits refresh credit.
- L3: post-await checks read global and key revocation generations in addition to backend epoch.
- C1: LP-P requires the exact currently owning BuildToken and all generation/lifetime conditions; stale completions are no-ops.
- C2: replacement revision is not revocation generation; successful same-incarnation refresh alone does not rewrite old leases.
- C3: no slow work or await holds the metadata lock; real outstanding work remains accounted after logical cancellation.
- C4: the two-job budget and finite waiter/token deadlines survive close/restart-of-executor storms; exhaustion refuses.
- C5: LP-C prevents publication by its jobs; cleanup adds no suspension point or join, preserving #161.
- C6: fork, quarantine, manifest and rollback cannot revive or extend a previous-generation lease.

## 4. F2: native profiles cover every authorization field

Native owner lookup remains `off`. A dark pure decoder may be proposed separately. A runtime `shadow` mode needs a later explicit execution scope and uses the **complete production legacy decision** for acceptance/refusal and reason. Unsupported ABI/profile, decoder uncertainty or missing qualification cannot authorize; the full existing legacy path decides or refuses exactly as before.

Use an allowlist of individually qualified profiles, bound to observed OS/Darwin build, kernel release, architecture, process ABI/translation mode, native interface/flavor and layout-evidence digest. Do not promote a whole OS major, a newer patch, SDK equality or same returned size without evidence. Future supported profiles need their own record; unknown ones are legacy. Do not derive accepted offsets from the runtime payload itself. Separate socket ABI, process-facts ABI, csops encoding and private exec-version profiles; qualification of one does not imply qualification of another.

The profile must name widths, signedness, alignment, offsets, endian rules and constants for **all consumed fields**, and the candidate must reject or conservatively fall back for unrecognized combinations. At minimum:

| Fields / operation | Required role and qualification |
| --- | --- |
| `PROC_PIDLISTFDS`, `proc_fdinfo.proc_fd/proc_fdtype`, `PROX_FDTYPE_SOCKET` | Qualified constants/layout; nonnegative plain fd; bounded list size; exact multiple, positive return, no full-buffer truncation or duplicate ambiguity. Bound descriptor iteration and memory. |
| `PROC_PIDFDSOCKETINFO`, `proc_fileinfo.fi_status`, `PROC_FP_SHARED` | Exact-size return and mask/width semantics; status read at actual status offset. Sharing clear on **both** ends, including unknown status semantics fallback. Endpoint oracle cannot attest this. |
| `soi_type`, `soi_protocol`, `soi_family`, `soi_kind`, `insi_vflag`, `tcpsi_state` | Stream/TCP/IPv4/TCP-union/ESTABLISHED semantics; exact profile constants, union and offsets. Wrong state/kind cannot be treated as established. |
| `insi_fport/lport`, IPv4 foreign/local addresses | Qualified width, low-bit/endian handling and unsupported high bits; server endpoints reverse client endpoints. Per-use own-end comparison is a consistency check only. |
| `soi_so`, `insi_gencnt` for **each** end; selected server fd and client fd | Equality across snapshots and with this stream's recorded native identity where admitted; refuse/fallback for ambiguous zero/redacted values unless profile explicitly qualifies them. Prevent stream-level fd/endpoint ABA from being ignored by a legacy-shaped return tuple. Do not log raw pointers. |
| Process facts and signature fields | Exact PID, UID triple, valid start time, ppid, comm, path, cs flags, cdhash, signing identifier/team and separately qualified exec identifiers for both server and parent. Qualify parsing and required expected predicates, not only equality to a captured value. |

The original SDK's 792-byte socket/136-byte BSD/8-byte fd sizes and offset table are **one retained observation**, not runtime qualification. If later adding another consumed field (for example `soi_pcb`, `fi_type` or guard flags), amend the profile, fixtures, mutants and review before using it. Merely listing but ignoring a field cannot count as coverage. Equality of two native samples is not an atomic ownership lock through the eventual write.

Required profile evidence: exact header/SDK provenance and complete layout comparison; kernel implementation semantics mapped to each field; independently built full-field binary fixtures; endpoint/size-preserving mutations of every authorization field; and later admitted disposable native fixtures exercising both endpoints, sharing, close/reuse and stale process identities. The private-process profile needs its own availability/permission/layout evidence. No ordinary clean shadow count replaces these proofs.

Latch **all optimized native authorization** off process-wide after confirmed profile/size/oracle/decoder uncertainty. Record a reason class only. In shadow the legacy decision/reason remains untouched. In a hypothetical enabled native stream, latch transition revokes native dispatch permits globally; subsequent writes use the complete legacy path only after a new valid check, or close. Never continue with previously cached native acceptance. Process restart does not automatically qualify the formerly unknown profile.

## 5. F3: exact legacy predicate and intentional changes

Pinned `ManagedOmlx.connected_peer`, used by Desktop through its class seam, reads the full `-iTCP:<port> -sTCP:ESTABLISHED` inventory twice. `connection_owners` strictly validates **every** process/file record before selecting the connection pair. It compares only the selected pair between snapshots after complete strict parsing. Thus unrelated valid IPv4 connection churn may pass while unrelated IPv6 or malformed input refuses. The old per-pid microprobe parser is not the oracle.

| Existing production refusal class | Shadow/unchanged behavior | Native-per-pair coverage |
| --- | --- | --- |
| Command failure, stderr, timeout, >1 MiB output; unavailable inspection | Existing refusal/retry/reason ordering. | Cannot infer global success from qualified server fd lookup. |
| Nonbytes, empty/oversized inventory, non-ASCII, empty/short row | `connection_inventory_unqualified` in the existing parser. | No per-pair substitute. |
| Missing/duplicate/out-of-order/unknown process or file fields; incomplete process/item; wrong exact field set | Same strict refusal, including unrelated records. | No per-pair substitute. |
| IPv6/non-IPv4, non-TCP, non-ESTABLISHED record | Same strict refusal anywhere in inventory. | This pair's IPv4/TCP evidence misses unrelated records. |
| Noncanonical/invalid PID, UID, fd, host or port; PID <=0, multiple/missing arrows; out-of-range port | Same strict refusal anywhere. | Need complete global oracle or explicit changed predicate. |
| Missing/extra/duplicate server or client endpoint owner; wrong server PID/UID or Wisp PID/UID/fd | `connected_peer_unqualified`. | Native sharing and exact-endpoint samples are partial evidence, separately qualified. |
| Second complete parsed snapshot changes selected records | `connection_kernel_unqualified`; unrelated **valid** pair churn is allowed. | Native identity/generation evidence needs independent ABA/race qualification. |
| Binding, identity or socket changes across the bracket | Existing binding reason or `connected_peer_changed`; no retry of findings. | Native/facts candidate does not remove this obligation. |

For the proposed preparation and shadow stages, the decision is `LEGACY(raw1, raw2, bindings, identities, socket)` unchanged. Native disagreement is metadata with no response/body/path/PID recording. In particular a stable native pair plus an unrelated IPv6 record must still refuse via legacy. A native uncertainty cannot convert any legacy refusal to acceptance, including transient exhaustion.

There are only two honest paths to future native authority:

1. **Preserve strict parity:** implement/qualify the complete global legacy predicate, including every unrelated-record refusal and inspection visibility/error condition. Running legacy inventories each time proves this directly but does not remove their cost. A hypothetical native global census still needs its own complete visibility and failure proof; it is not specified/qualified here.
2. **Intentionally change the predicate:** propose a separate security change that replaces global-inventory restrictions with a per-connection predicate. Specify each newly accepted and newly refused class, threat analysis, reason behavior and availability impacts. Obtain independent review and owning-human acceptance **in addition to D3**. Do not describe it as I4 parity or infer acceptance from performance goals.

This revision selects path 1 for all currently contemplated authoritative stages and defers any lsof-eliminating enablement. Accordingly the original 40 ms arithmetic is not a forecast for this revised design. Sharing fallback preserves legacy's visibility limit; it does not newly prove that invisible holders are absent. Choosing to refuse on known sharing would be a separately reviewed stricter verdict change, not an unnoticed fallback tweak.

## 6. F5: exec-version qualification distinct from argv

The retained probes sampled start time, BSD/path/signing facts. They did not sample every native exec-sensitive mechanism. Upstream [Apple's private proc-info header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info_private.h) defines a 56-byte unique-identifier record with process/parent identifiers and `p_idversion`, exposed by flavor 17. [Apple's proc-info implementation](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/kern/proc_info.c) fills the version; [the exec implementation](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/kern/kern_exec.c) updates it during exec. This supports investigating detection of same-binary and A→B→A exec. It does **not** attest the deployed kernel, permissions, ABI stability or wraparound behavior.

Propose a separate `exec_identity` helper returning a typed internal record rather than altering `process_identity`. Establish the server and current parent through non-authorizing discovery using the existing listener/process parsing rules before collecting pre-qualification facts; malformed discovery refuses. The constructor's full listener result must agree with these selected PIDs, executable paths and both pre/post fact samples before publication. Under a qualified profile, bracket each server and parent multi-call fact collection with unique-ID/version reads; require equal before/after IDs/version and agreement of the server's current ppid with the selected parent's independently sampled identity. `p_orig_ppidversion` describes the original parent and cannot be substituted for current-parent validation. Obtain coherent pre/post samples around the constructor's full qualification too. Treat UUID as executable identity evidence, never as proof of Python module/argv/loaded code.

If the private API is unavailable, returns wrong length, fails permissions, changes profile, produces malformed/ambiguous identity, or changes during collection, it cannot grant optimized trust. Before admission use today's full binding/lsof path; an already optimized stream must refuse or complete a fresh entire legacy check before any next emission under its unexpired lease. No partial mixture of old native facts and a later legacy accept. Separate API-error fallback from a detected real identity/version change: the latter revokes that lease and closes/refuses; do not hide it in fallback.

Required future evidence covers both server and parent: same-binary exec, other-binary exec, A→B→A between ordinary facts, exec/version change during multi-call collection, process replacement/reparenting, unavailable flavors/permissions, translated/native ABIs, exact return layout and bounded version-reuse concerns. One upstream increment mechanism does not prove no possible ABA on every deployed build.

**Separate argv decision:** even a qualified exec-version does not detect in-place argv rewriting and cannot reproduce `ps -o comm=`'s `omlx-server`/parent-command predicate. Retain today's argv and listener binding checks at every existing fence unless a separate D2b predicate-change proposal is explicitly accepted. No claim that only an already compromised process can exploit this, or that an external attacker gains nothing, substitutes for review. Same-binary exec must stop being named an unavoidable native residual before this mechanism is qualified or rejected with evidence.

## 7. Human choices and safe current settings

These are decisions to present later with exact scope and evidence; this artifact records **no acceptance**.

| Choice | Current setting | Concrete future choice and prerequisites |
| --- | --- | --- |
| D1: cross-connection tree/group reuse | Disabled; per-connection legacy qualification. | Keep disabled, or choose an explicit finite W (30/60/300 seconds are prior options, not recommendations). Requires F1 dispatch/lease and F4 proofs, duration/availability evidence, and acceptance of admitted permission-audit age plus the unbounded loaded-code consequence of transient tampering. No grace by default. |
| D2a: exec-sensitive replacement facts | Full existing binding remains. | Keep binding, or admit a separately qualified exec-version profile for server and parent. First qualify or reject private interface on every supported target; unsupported targets retain binding. This is not consent to an exec gap. |
| D2b: mutable argv/listener predicate | Keep argv/binding at existing fences. | Keep it, or approve exact documented per-write predicate removal independently. Qualified exec detection cannot stand in for argv. Full connect binding still remains under D4. |
| D3: native ownership rollout | Off; legacy authoritative. | Off, or later admitted shadow only. Any native-authoritative stage additionally needs complete F2 profiles, sharing/ABA proof and either full F3 parity or an explicit separate predicate-change decision. A threshold of clean ordinary observations alone is insufficient. |
| D4: connect binding | Full binding per connection and connect owner check. | Keep it; no amortization proposal is presently admitted. Any later relaxation requires independent threat analysis and gates. No coalescing of header/body checks. |

## 8. Future implementation stages and dependencies

1. **Coordination prerequisite:** parent resolves actual pending builder registration, precise Claude baton/mailbox acknowledgment and unpublished-work disposition, exact ownership/base/release and final 1.2 dependency. This analysis grants none. Fresh work uses admitted current origin/main, reconciled through protected workflow; obsolete `4994caa` red-test instructions do not bind future builders.
2. **S0 behavior pins:** an explicitly released product builder adds meaningful synthetic oracle/control coverage on the admitted base, preserving existing lifetime/blind-spot tests in legacy policy. Do not rename current tests into proof of a lease they do not implement. Verify #161 cleanup and #162 readiness behavior. No native runtime scope follows merely from a design artifact.
3. **S1 default-off preparation:** pure lease/token/state model and decoder scaffolding with default legacy dispatch; observe only admitted synthetic data. Facts/clock/private API execution require separately stated disposable qualification scope. No production native shadow traffic yet. Ownership stays at one primary writer.
4. **S2 lease/revocation candidate:** implement the proposed state machine and qualify the concrete LP-W dispatch contract, including lower-writer suspension/preemption limits. Default off. Future exact-head audit and synthetic specialist QA must establish bounded jobs, stale no-op, old-stream expiry and cross-client post-await refusal before D1 can enable reuse.
5. **S3 exec/profile qualification:** qualify both process and native-owner ABI profiles independently. Keep binding and lsof authoritative. If private exec support is rejected/unsupported, the fallback is current binding, not implicit R2 acceptance. Only a separately approved D2 candidate can remove a binding predicate.
6. **S4 legacy-authoritative shadow:** only after a fresh execution scope permits specified disposable processes/spare ports, gather full production differential evidence including global refusal cases. Shadow remains off in normal operation unless separately admitted. No installed app, live engine ports, credential/model/cloud/user-data traffic.
7. **S5 authority changes:** separate candidates for cross-connection reuse, any replacement fence, and any native-authoritative decision. Each requires owning-human exact D choices, fresh exact-SHA mechanical evidence, required same-head CI (zero configured checks = unavailable/non-passing), independent Auditor and specialist QA where triggered. F3 currently prevents claiming a lsof-free parity path. A new source edit/reconciliation invalidates old evidence.
8. **S6 release performance:** later admitted release benchmark on exact qualifying candidate and baseline, using an authorized idle window/state. Include full identifier/team/exec fact cost, synchronization, lower-write checks, cold/sleep/restart/refusal cases, contention and idle CPU. Reconcile the prior 25% versus 50% threshold against the authoritative release protocol before freeze. No static estimate or microprobe qualifies ~40 ms or a merge. The 1.3 hold and final 1.2 dependency remain until the authorized integration coordinator explicitly handles them.

`TEST_MATRIX.json` specifies future assertions and mutants; all entries are **NOT_RUN**. This author neither runs those tests nor approves its own fixes. Parent freezes this proposal's digest for a distinct review acceptance/release.
