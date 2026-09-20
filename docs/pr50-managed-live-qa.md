# PR #50 managed live QA

`Wisp Summary QA.app` is a separate one-shot qualification application. It is
not Wisp, and the production verifier rejects it even if its marker is removed.

It stages the exact candidate versions of `email_tools._summarize`,
`imessage_tools._summarize`, and `brief._sections`. Synthetic adapters are
installed before those modules are imported. Two Ling completion calls receive
only fixed fixtures; Daily Summary must remain model-free. Reports contain only
fixture IDs, booleans, counts, hashes, timing, and fixed reason codes.

Security boundaries:

- separate bundle, Keychain service, and loopback port `18765`
- one owned child, one run, no relaunch, and listener-PID verification
- production `WISPCP1` pipe; no secret in argv, env, logs, or reports
- real attributed oMLX transport; ambiguous busy state is `BLOCK`
- atomic owner-only reports
- no production app delegate, native readers, scheduler, memory, sync, or effects

Build without installing or launching:

```bash
python3 -B build-support/pipeline.py qa-assemble --output dist/pr50-summary-qa
```

The command compiles and ad-hoc signs the separate app and records the Python
interpreter hash. A live run remains separately authorized and requires the
dedicated QA Keychain item, authenticated loopback oMLX, no other clients, and
`Ling-3.0-tiny-oQ4e`. The sanitized report is written under
`~/Library/Application Support/Wisp Summary QA/reports/` and applies only to
its embedded candidate SHA.
