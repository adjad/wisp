# PR50 managed summary QA artifact

This source-only artifact stages a separate `Wisp Summary QA.app` identity on
loopback port 18765. It does not modify or launch production Wisp. Assembly
retains the reviewed credential-pipe and attributed-transport sources, imports
the original Mail, Messages and Daily Summary modules only after installing
synthetic dependency adapters, and emits an allowlisted report from an
irreversible one-shot state machine.

Build the source staging tree with `./scripts/wisp-build qa-assemble --output
dist/qa-source --allow-dirty`. The output is deliberately rejected by every
production verify/release path. Offline tests require no Keychain, signing
identity, QA user, app launch, backend, oMLX process, native source, personal
data, or network access.

An eventual managed-live run remains **BLOCKED** until a separately authorized
operator provisions a dedicated QA reader identity and Keychain item, proves
the configured Ling model and credential target, and establishes an exclusive
server session with positive idle evidence. Missing or ambiguous readiness is
BLOCK, never PASS. This repository command neither performs nor authorizes any
of those actions.
