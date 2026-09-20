# PR #50 managed live QA

`Wisp Summary QA.app` is a separate one-shot qualification application. It is
not Wisp, and the production verifier rejects it even if its marker is removed.

It stages the exact candidate versions of `email_tools._summarize`,
`imessage_tools._summarize`, and `brief._sections`. Synthetic adapters are
installed before those modules are imported. Two Ling completion calls receive
only fixed fixtures; Daily Summary must remain model-free. Reports contain only
fixture IDs, booleans, counts, hashes, timing, and fixed reason codes.

Security boundaries:

- separate bundle and Keychain service
- one owned child process group, one run, and no relaunch
- production `WISPCP1` pipe; no secret in argv, env, logs, or reports
- closed, independently inventory-pinned Python runtime and Git-blob-pinned source
- atomic owner-only reports
- no production app delegate, native readers, scheduler, memory, sync, or effects

Build without installing or launching:

```bash
python3 -B build-support/pipeline.py qa-assemble \
  --output dist/pr50-summary-qa \
  --qa-runtime /path/to/reviewed/runtime \
  --qa-runtime-inventory-sha256 <canonical-runtime-inventory-sha256> \
  --production-sha <40-character-production-candidate-sha>
```

The command compiles and ad-hoc signs the separate app and records the Python
runtime and source inventories. The runtime digest is an independent reviewed
input; assembly fails if the runtime differs before or after copying.

The shipped manifest declares `exclusive_proof_protocol` as `unavailable`.
Launching the app therefore validates its signed inventories, writes a
sanitized `BLOCK` report with reason `external_exclusivity_required`, and exits
before Keychain access, child launch, or inference. A live qualification run is
not currently enabled. Enabling `server-lease-v1` requires a separately
reviewed exclusivity implementation and fresh security approval.
