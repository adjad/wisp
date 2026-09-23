#!/usr/bin/env python3
"""Build and run the compile-time-only native Settings fixture app.

All synthetic state and the .app stay under a new /private/tmp/wisp-settings-qa-*
directory. This never launches the installed Wisp app or binds port 8765.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import errno
import plistlib
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DISABLED_CLOUD = {
    "enabled": False, "provider": "openrouter", "provider_label": "OpenRouter",
    "base_url": "https://openrouter.ai", "api_prefix": "/api/v1",
    "model_id": "", "context_window": 16384, "credential_name": "cloud",
    "roles": [], "super_model_enabled": False, "super_model_router": "disabled",
}
DISABLED_LOCAL = {
    "enabled": False, "active": False, "authenticated": False,
    "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
    "model_id": "", "context_window": 8192, "roles": [],
}


class Fixture:
    def __init__(self) -> None:
        self.mode = "normal"
        self.cloud = dict(DISABLED_CLOUD)
        self.local = dict(DISABLED_LOCAL)
        self.requests: list[dict[str, str]] = []
        self.lock = threading.Lock()


def handler_for(fixture: Fixture):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def respond(self, value: dict, status: int = 200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}") if size else {}
            path = self.path.split("?", 1)[0]
            with fixture.lock:
                fixture.requests.append({"method": self.command, "path": path, "mode": fixture.mode})
                if path == "/__qa/mode" and self.command == "POST":
                    fixture.mode = body["mode"]
                    if fixture.mode == "reset":
                        fixture.cloud = dict(DISABLED_CLOUD)
                        fixture.local = dict(DISABLED_LOCAL)
                        fixture.mode = "normal"
                    return self.respond({"ok": True})
                if path == "/inference/cloud":
                    if self.command == "GET":
                        if fixture.mode in {"partial_cloud_get", "ambiguous"}:
                            return self.respond({"enabled": True})
                        if fixture.mode == "empty_cloud_identity":
                            return self.respond({**fixture.cloud, "credential_name": ""})
                        return self.respond(fixture.cloud)
                    if self.command == "POST":
                        if fixture.mode == "pre_reject":
                            return self.respond({"detail": "Synthetic pre-save rejection"}, 400)
                        fixture.cloud = {
                            **DISABLED_CLOUD, **body, "enabled": True,
                            "provider_label": "OpenRouter",
                            "super_model_router": "disabled",
                        }
                        if fixture.mode in {"post_500", "ambiguous"}:
                            return self.respond({"detail": "Synthetic post-save warmup failure"}, 500)
                        if fixture.mode == "cloud_partial_post":
                            return self.respond({"enabled": True})
                        return self.respond(fixture.cloud)
                    if self.command == "DELETE":
                        fixture.cloud = dict(DISABLED_CLOUD)
                        return self.respond(fixture.cloud)
                if path == "/inference/local-provider":
                    if self.command == "GET":
                        if fixture.mode == "local_partial_get":
                            return self.respond({"enabled": True})
                        return self.respond(fixture.local)
                    if self.command == "POST":
                        fixture.local = {
                            **DISABLED_LOCAL, **body, "enabled": True,
                            "active": True, "authenticated": False,
                        }
                        if fixture.mode == "local_post_500":
                            return self.respond({"detail": "Synthetic local post-save close"}, 500)
                        if fixture.mode == "local_partial_post":
                            return self.respond({"enabled": True})
                        return self.respond(fixture.local)
                    if self.command == "DELETE":
                        if fixture.mode == "local_bad_delete_2xx":
                            return self.respond(fixture.local)
                        fixture.local = dict(DISABLED_LOCAL)
                        if fixture.mode == "local_delete_500":
                            return self.respond({"detail": "Synthetic local post-disable close"}, 500)
                        return self.respond(fixture.local)
                if path == "/models":
                    return self.respond({"installed": ["qa-local"], "roles": {"reasoning": "qa-local"}})
                if path == "/mode":
                    return self.respond({"read_only": True, "full_access": False})
                if path == "/idle_timeout":
                    return self.respond({"idle_minutes": 5})
                if path == "/skills":
                    return self.respond({"skills": [{"name": "humanizer", "enabled": False}]})
                return self.respond({"detail": "Unexpected QA route"}, 404)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

    return Handler


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="wisp-settings-qa-", dir="/private/tmp"))
    fixture = Fixture()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_for(fixture))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    assert port != 8765
    scratch = home / "swift-build"
    app = home / "Wisp Settings QA.app"
    executable = app / "Contents/MacOS/WispSettingsQA"
    result: dict = {"fixture_home": str(home), "port": port, "app": str(app)}
    run_id = home.name.removeprefix("wisp-settings-qa-")
    suite_name = f"com.wisp.settings-qa.{run_id}"
    suite_file = Path.home() / "Library/Preferences" / f"{suite_name}.plist"
    try:
        build = subprocess.run([
            "/usr/bin/swift", "build", "--disable-sandbox", "--package-path", str(ROOT / "app"),
            "--scratch-path", str(scratch), "-Xswiftc", "-DWISP_SETTINGS_QA", "-q",
        ], cwd=ROOT, capture_output=True, text=True, timeout=180)
        if build.returncode:
            result["build_stderr"] = build.stderr[-6000:]
            raise RuntimeError("Settings QA Swift build failed")
        executable.parent.mkdir(parents=True)
        shutil.copy2(scratch / "debug/WispApp", executable)
        with (app / "Contents/Info.plist").open("wb") as output:
            plistlib.dump({
                "CFBundleIdentifier": "com.wisp.settings-qa",
                "CFBundleName": "Wisp Settings QA",
                "CFBundleExecutable": executable.name,
                "CFBundlePackageType": "APPL",
                "NSPrincipalClass": "NSApplication",
            }, output)
        result["binary_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
        for phase in ("first", "restart"):
            env = os.environ.copy()
            env.update({
                "WISP_HOME": str(home),
                "WISP_SETTINGS_QA_BASE_URL": f"http://127.0.0.1:{port}",
                "WISP_SETTINGS_QA_RUN_ID": run_id,
                "WISP_SETTINGS_QA_PHASE": phase,
            })
            process = subprocess.run([str(executable)], env=env, cwd=home,
                                     capture_output=True, text=True, timeout=45)
            report_path = home / f"qa-{phase}-report.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            result[phase] = {
                "exit_code": process.returncode, "report": report,
                "stderr": process.stderr[-2000:],
            }
            if report.get("status") != "PASS" or process.returncode != 0:
                break
            try:
                os.kill(report["pid"], 0)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    raise
            else:
                raise RuntimeError(f"QA app process {report['pid']} remained alive")
        result["requests"] = fixture.requests
        result["status"] = "PASS" if all(
            result.get(phase, {}).get("report", {}).get("status") == "PASS"
            for phase in ("first", "restart")
        ) else "FAIL"
    except Exception as error:
        result["status"] = "FAIL"
        result["error"] = repr(error)
    finally:
        server.shutdown()
        server.server_close()
        # AppStorage can persist a pane selection in its unique QA suite.
        # Preserve it for audit in WISP_HOME, remove only this run's domain,
        # then prove no QA preferences remain outside the fixture.
        result["qa_suite_was_written"] = suite_file.exists()
        if suite_file.exists():
            shutil.copy2(suite_file, home / "qa-suite-captured.plist")
        subprocess.run(["/usr/bin/defaults", "delete", suite_name],
                       capture_output=True, text=True, check=False)
        if suite_file.exists():
            suite_file.unlink()
        result["qa_suite_clean"] = not suite_file.exists()
        if not result["qa_suite_clean"]:
            result["status"] = "FAIL"
            result["error"] = f"QA defaults remained outside fixture: {suite_file}"
        (home / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps({key: result.get(key) for key in
                          ("status", "fixture_home", "app", "first", "restart", "error", "build_stderr")}, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
