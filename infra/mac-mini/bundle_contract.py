"""Version 1 mini contract. Kept independent of application dependencies."""
import re
FILES = {"mini/" + name for name in (
    "__init__.py", "__main__.py", "http.py", "gateway.py", "node.py", "protocol.py", "store.py", "requirements.txt", "README.md",
    "resources.py", "runtime.py", "adapters.py", "acquisition.py", "backup.py", "resource-contract.json", "RUNBOOK.md")}

# Preparation is shipped as data/code, never automatically executed by staging.
PREPARATION_FILES = {"mini/payload/preparation/" + name for name in (
    "arrival.py", "omlx-v1.json", "omlx-launchagent-v1.plist")}


def validate_contract(manifest):
    if (manifest.get("schema_version") != 1 or manifest.get("kind") != "wisp-mini-runtime"
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")) or manifest.get("jobs_enabled") is not False
            or manifest.get("python") != ">=3.13,<3.15" or manifest.get("requirements") != "mini/requirements.txt"):
        raise ValueError("unsupported_bundle_contract")
    services = manifest.get("services", {})
    if set(services) != {"gateway", "node"}:
        raise ValueError("unsupported_services")
    expected = {
        "gateway": {"argv": ["python", "-m", "mini", "gateway"], "listen": "127.0.0.1:8765", "upstream": "http://127.0.0.1:8000",
                    "serve_https_port": 443, "environment_keys": ["WISP_MINI_INFERENCE_KEY", "WISP_LOCAL_OMLX_KEY"],
                    "routes": ["GET /health", "GET /v1/models", "POST /v1/chat/completions", "POST /v1/embeddings", "POST /v1/rerank"]},
        "node": {"argv": ["python", "-m", "mini", "node", "--state-dir", "{state_dir}", "--node-id", "{node_id}"],
                 "listen": "127.0.0.1:8766", "serve_https_port": 8443, "environment_keys": ["WISP_MINI_NODE_KEY"],
                 "routes": ["GET /healthz", "GET /v1/status", "GET /v1/results"]},
    }
    if services != expected:
        raise ValueError("unsupported_service_contract")
    rows = manifest.get("files", [])
    paths = {row["path"] for row in rows}
    required_files = FILES
    extra = paths - required_files
    if manifest.get("artifact_type") == "offline-runtime":
        required = {"mini/payload/keychain-helper", "mini/payload/mini-launcher", "mini/payload/venv/bin/python3", "mini/payload/runtime-health.py", "mini/payload/provisioning/receiver.py"}
        required |= PREPARATION_FILES
        provenance = manifest.get("provenance", {})
        if (not required <= extra or any(not p.startswith("mini/payload/") for p in extra)
                or provenance.get("source_commit") != manifest["source_commit"]
                or not isinstance(provenance.get("strict_toolchain"), bool)):
            raise ValueError("unsupported_runtime_contract")
    elif manifest.get("artifact_type") != "source" or extra:
        raise ValueError("unsupported_artifact_type")
    if len(rows) != len(paths) or not required_files <= paths:
        raise ValueError("unsupported_file_contract")
