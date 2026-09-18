# Primary Mac / future mini provisioning handoff

Base: PR #42 head `c23e9c9e8860222a8aa4b070364a7e17236b3ec8`.
Branch: `codex/primary-mini-provisioning`. The existing PR #42 branch is untouched.
Exact final commit and post-commit evidence are recorded in the draft PR and task
handoff; this document does not substitute an earlier SHA for that evidence.

Delivered: Security.framework credential bridge and private backend credential
resolution; `wisp-node-prep` init-primary/policy-render/preflight/activate/doctor/
rollback commands; additive restricted Tailnet policy; read-only firewall and
loopback checks; pinned versioned mini-bundle validation; Tailscale SSH streaming
receiver, explicit Keychain readers, resumable inert staging and disabled launchd
templates; focused tests and operational guidance in `infra/mac-mini/README.md`.

Owned changes are limited to BackendManager/BackendCredentials; config init,
credentials and endpoints; provisioning script/infra; focused tests; two reviewed
SAFE_FULL_TESTS registrations; docs. No mini runtime files, dependency lock files,
Local checkout, installed apps, live credentials, Tailnet or system settings were
changed. No deployment, role switch, job enablement, merge or archive occurred.

Three read-only helpers reviewed Keychain/launcher threats, Tailnet policy and
CLI/test gaps. Findings were repaired in the builder's acknowledged scope:
local token preservation, distinct credentials and local-ref isolation, explicit
Keychain ACL readers, true Tailscale JSON fields, sanitized Serve output,
explicit launchd domains/exact services, malformed policy/Serve rejection,
resumable staging/venv setup, repeat credential verification, actual runtime PID
supervision, and truthful rollback restart status.

Validation commands (all synthetic/compile-only):

- `python -m pytest -q tests/test_node_prep.py tests/test_primary_credentials.py`
  — 53 checks at final source preparation.
- `python -m pytest -q tests/test_inference_endpoints.py tests/test_node_inbox.py tests/test_lazy_inference_readiness.py tests/build_pipeline/pipeline_checks.py tests/test_simulation_qa_runner.py`
  — inference/node regressions and explicit manifest/pipeline checks.
- `python scripts/test_replay_failure_fixes.py` — isolated complete Python gate;
  rerun at the final committed SHA for authoritative evidence.
- `swift build --disable-sandbox --package-path app --scratch-path /private/tmp/wisp-primary-native-build`
  — full app compilation; no app launch or installation.
- `swiftc -parse-as-library` with isolated module cache: BackendCredentials plus
  BackendCredentialChecks; BackendCredentials/BackendManager plus
  BackendRecoveryChecks; mini-launcher and keychain-helper compile-only.
- Fixture-only `activate` against mini worker commit
  `172c87d92d5897106af9f450996bc386fac74787`, archive SHA-256
  `e9ff7fc97f79c7bad64e138a2b8726d6289957e6e814515447441b5b32e1c075` — passed,
  roles local, proactive_node false, gateway qualification required.
- Dynamic synthetic leak checks assert values never reach receiver files,
  subprocess arguments, diagnostic output, or inherited backend child env.
  Source/diff checks cover credential literals and unsafe firewall/Funnel writes.

Earlier failures were actionable and repaired: new test manifest drift, an
outdated test that expected remote access to the local key, and a synthetic venv
retry stub that did not model idempotent directory creation. The default SwiftPM
nested sandbox could not start in the desktop sandbox; `--disable-sandbox` for
SwiftPM itself compiled successfully inside the existing desktop sandbox.
Deprecated file-based Keychain ACL API warnings remain documented.

Release blockers / incomplete live qualification:

1. No live Keychain mutation was authorized. Cross-executable trusted-reader
   access, signed app/helper identity, locked/denied behavior, helper updates and
   real mini bootstrap require isolated native specialist qualification before
   release. Source checks and synthetic closures do not establish these facts.
2. No live Tailnet/device approval/HTTPS/firewall/install qualification was run.
   The exported full-policy file cannot prove the current admin-console settings.
   Exact-head independent Release Auditor and applicable specialist gates remain
   required; fixture readiness does not mean deployable or merge-approved.
3. Existing local oMLX auth must already be canonical 256-bit hex for import;
   otherwise initialization refuses before credential writes. Authentication
   migration is a future explicit live task. Mini local auth is separate.
4. Staging leaves all services disabled and gateway qualification required.
   Applied rollback restores persistent primary config and unloads managed remote
   jobs, but returns exit 2/restart_required until backend caches are refreshed.
5. Integration with the mini branch must add these reviewed names to
   scripts/run_simulation_qa.py's manifest **together with the files**:
   tests/test_mini_http.py, tests/test_mini_store.py, tests/test_mini_contract.py.
   They are deliberately absent from this standalone provisioning branch.
   Rerun full combined-candidate checks at its new SHA. Both branches share the
   PR #42 base; the only expected code overlap is the manifest integration seam.

Keychain ACL APIs are deprecated but used explicitly for file-based Keychain
compatibility. A shared signed access-group migration is outside this candidate.
No CI pass is inferred from missing checks; inspect the draft PR's exact head.
