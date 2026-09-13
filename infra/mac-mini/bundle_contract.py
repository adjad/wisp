"""Version 1 mini contract. Kept independent of application dependencies."""
BASE = "c23e9c9e8860222a8aa4b070364a7e17236b3ec8"
FILES = {"mini/" + name for name in (
    "__init__.py", "__main__.py", "http.py", "gateway.py", "node.py", "protocol.py", "store.py", "requirements.txt", "README.md")}


def validate_contract(manifest):
    if (manifest.get("schema_version") != 1 or manifest.get("kind") != "wisp-mini-runtime"
            or manifest.get("base_commit") != BASE or manifest.get("jobs_enabled") is not False
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
    if len(rows) != len(FILES) or {row["path"] for row in rows} != FILES:
        raise ValueError("unsupported_file_contract")
