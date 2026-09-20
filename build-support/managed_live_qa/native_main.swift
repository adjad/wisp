import Foundation

// Build-only entry point. It owns only the backend process it launches and the
// private report directory it creates. Production readers and PortGuard are absent.
let qaPort = 18765
let qaBundleIdentifier = "com.wisp.app.summary-qa"
let reportKeys = ["schema_version", "manifest_sha256", "candidate_sha", "model",
                  "status", "reason_codes", "call_count", "selected_ids",
                  "predicates", "elapsed_ms"]

guard CommandLine.arguments.count == 1 else { exit(64) }
fputs("Wisp Summary QA is a separately provisioned one-shot artifact; source build only.\n", stderr)
exit(78)
