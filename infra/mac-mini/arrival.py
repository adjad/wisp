"""Versioned arrival preparation; no shell, service, network or credential side effects.

Upstream command reference: https://github.com/jundot/omlx/blob/main/README.md
The candidate argv must be qualified against the pinned oMLX version before loading.

Apply is a library interface for a separately qualified adapter. No live adapter is
shipped here. Approval digests must arrive from the independent approval channel,
never from the untrusted arrival document. Fixed action IDs prevent command injection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import time

GATES = ("identity", "hardware", "model", "resource", "network", "credential", "task")
ACTIONS = ("load-qualified-user-services", "serve-443-loopback-8765",
           "serve-8443-loopback-8766", "bind-qualified-model-resources",
           "stage-role-migration")
SHA = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")


class ArrivalError(ValueError):
    """Messages are fixed codes; never include supplied values."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _require(condition, code):
    if not condition:
        raise ArrivalError(code)


def _keys(value, expected):
    _require(type(value) is dict and set(value) == set(expected), "INVALID_SCHEMA")


def _token(value):
    return type(value) is str and bool(TOKEN.fullmatch(value))


def validate_template(template):
    """Accept only the distributed template, including disabled supervision."""
    _require(digest(template) == "4325eca112e898292901006739a5b0035d144b17784aa8378526296e51884d4e", "INVALID_OMLX_TEMPLATE")
    return {"schema_version": 1, "status": "DISABLED_TEMPLATE_VALID"}



def validate_preflight(evidence, binding, now):
    """Strict fresh adapter observation, separate from approval assertions."""
    _keys(evidence, ("schema_version", "binding", "observed_at", "uid", "administrator",
                     "hardware_qualified", "model_sha256", "resource_contract_sha256",
                     "resource_qualified", "concurrency", "credential_authenticated",
                     "firewall_enabled", "inbound_exceptions", "primary_only_network",
                     "funnel", "loopback_only"))
    _require(type(evidence["schema_version"]) is int and evidence["schema_version"] == 1
             and evidence["binding"] == binding, "PREFLIGHT_BINDING_MISMATCH")
    _require(type(evidence["observed_at"]) is int and 0 <= now - evidence["observed_at"] <= 60,
             "STALE_PREFLIGHT")
    _require(type(evidence["uid"]) is int and evidence["uid"] > 0
             and evidence["administrator"] is True, "ACCOUNT_NOT_QUALIFIED")
    for key in ("hardware_qualified", "resource_qualified", "credential_authenticated",
                "firewall_enabled", "primary_only_network", "loopback_only"):
        _require(evidence[key] is True, "PREFLIGHT_NOT_QUALIFIED")
    for key in ("model_sha256", "resource_contract_sha256"):
        _require(type(evidence[key]) is str and bool(SHA.fullmatch(evidence[key])),
                 "RESOURCE_NOT_QUALIFIED")
        _require(evidence[key] == binding[key], "RESOURCE_BINDING_MISMATCH")
    _require(type(evidence["concurrency"]) is int and evidence["concurrency"] == 1
             and evidence["inbound_exceptions"] == [] and evidence["funnel"] is False,
             "PREFLIGHT_UNSAFE_POSTURE")


def prepare_omlx(template, *, version, artifact_sha256, verified_artifact_sha256):
    """Produce a disabled install plan only after publisher verification upstream.

    The caller supplies verified_artifact_sha256 from the signature verifier, not
    the manifest. No archive extraction or installation occurs in this function.
    """
    validate_template(template)
    _require(type(version) is str and bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)),
             "INVALID_OMLX_VERSION")
    _require(type(artifact_sha256) is str and bool(SHA.fullmatch(artifact_sha256))
             and artifact_sha256 == verified_artifact_sha256, "UNVERIFIED_OMLX_ARTIFACT")
    result = json.loads(json.dumps(template))
    result["install"]["version"] = version
    result["install"]["sha256"] = artifact_sha256
    return result


def plan(binding):
    _keys(binding, ("source_sha256", "artifact_sha256", "model_sha256", "resource_contract_sha256", "node_id", "task_id", "account"))
    for key in ("source_sha256", "artifact_sha256", "model_sha256", "resource_contract_sha256"):
        _require(type(binding[key]) is str and bool(SHA.fullmatch(binding[key])), "INVALID_BINDING")
    for key in ("node_id", "task_id", "account"):
        _require(_token(binding[key]), "INVALID_BINDING")
    _require(binding["account"] != "root", "ROOT_PROHIBITED")
    return {"schema_version": 1, "binding": dict(binding), "gates": list(GATES),
            "actions": list(ACTIONS), "routes": {"443": "http://127.0.0.1:8765",
            "8443": "http://127.0.0.1:8766"}, "funnel": False,
            "network_scope": "approved-single-primary", "role_migration": "staged-disabled"}


def validate_approval(arrival_plan, approval, trusted_approval_sha256, now=None):
    _require(type(arrival_plan) is dict and "binding" in arrival_plan, "INVALID_PLAN")
    _require(arrival_plan == plan(arrival_plan["binding"]), "INVALID_PLAN")
    _require(type(trusted_approval_sha256) is str and bool(SHA.fullmatch(trusted_approval_sha256))
             and digest(approval) == trusted_approval_sha256, "UNTRUSTED_APPROVAL")
    _keys(approval, ("schema_version", "plan_sha256", "binding", "nonce", "issued_at",
                     "expires_at", "gates"))
    _require(type(approval["schema_version"]) is int and approval["schema_version"] == 1,
             "INVALID_APPROVAL_VERSION")
    _require(approval["plan_sha256"] == digest(arrival_plan)
             and approval["binding"] == arrival_plan["binding"], "APPROVAL_BINDING_MISMATCH")
    _require(_token(approval["nonce"]), "INVALID_NONCE")
    now = time.time() if now is None else now
    _require(type(approval["issued_at"]) is int and type(approval["expires_at"]) is int
             and approval["issued_at"] <= now < approval["expires_at"]
             and 0 < approval["expires_at"] - approval["issued_at"] <= 3600, "STALE_APPROVAL")
    _keys(approval["gates"], GATES)
    for gate in GATES:
        evidence = approval["gates"][gate]
        _keys(evidence, ("status", "evidence_sha256"))
        _require(evidence["status"] == "PASS" and type(evidence["evidence_sha256"]) is str
                 and bool(SHA.fullmatch(evidence["evidence_sha256"])), "GATE_NOT_QUALIFIED")


def apply(arrival_plan, approval, *, trusted_approval_sha256, adapter=None,
          trusted_adapter_sha256=None, authorize=False, now=None):
    """Execute only with independent pins and explicit authorization.

    Adapter contract: qualification_sha256 is independently attested; claim(nonce,
    plan_digest) durably rejects reuse before mutations; preflight(plan) freshly
    verifies ALL identity/admin/hardware/model/resource/network/credential gates;
    capture(action) returns an opaque prior-state receipt; perform/verify implement
    exactly the fixed action; restore/verify_restored restore that receipt. A nonce
    stays consumed after rollback. The adapter must never revive stale credentials.
    Exceptions and receipts never leave this interface. No adapter is auto-selected.
    """
    now = time.time() if now is None else now
    validate_approval(arrival_plan, approval, trusted_approval_sha256, now)
    _require(authorize is True, "EXPLICIT_AUTHORIZATION_REQUIRED")
    _require(adapter is not None and type(trusted_adapter_sha256) is str
             and bool(SHA.fullmatch(trusted_adapter_sha256))
             and getattr(adapter, "qualification_sha256", None) == trusted_adapter_sha256,
             "QUALIFIED_ADAPTER_REQUIRED")
    receipts = []
    try:
        _require(adapter.claim(approval["nonce"], digest(arrival_plan)) is True, "REPLAY_REFUSED")
        validate_preflight(adapter.preflight(arrival_plan), arrival_plan["binding"], now)
        for action in ACTIONS:
            receipt = adapter.capture(action)
            # Record before mutation: even a partially failed perform is undone.
            receipts.append((action, receipt))
            adapter.perform(action, arrival_plan)
            _require(adapter.verify(action, arrival_plan) is True, "ACTION_VERIFICATION_FAILED")
    except Exception:
        restored = True
        for action, receipt in reversed(receipts):
            try:
                adapter.restore(action, receipt)
                restored = (adapter.verify_restored(action, receipt) is True) and restored
            except Exception:
                restored = False
        raise ArrivalError("ARRIVAL_REFUSED_ROLLED_BACK" if restored else "ARRIVAL_RECOVERY_REQUIRED") from None
    return {"schema_version": 1, "status": "ARRIVAL_STAGED", "role_migration": "disabled"}


def render_omlx(prepared, *, executable, executable_sha256, verified_executable_sha256):
    """Render disabled candidate bytes; version-specific adapter qualification is required.

    Config is the Wisp contract, not an assertion about an upstream settings schema.
    No launchctl invocation, installation, or credential material is included.
    """
    _require(type(prepared) is dict and type(prepared.get("install")) is dict,
             "INVALID_OMLX_TEMPLATE")
    original = json.loads(json.dumps(prepared))
    version = original["install"].get("version")
    artifact = original["install"].get("sha256")
    original["install"]["version"] = "REQUIRED"
    original["install"]["sha256"] = "REQUIRED"
    _require(prepared == prepare_omlx(original, version=version, artifact_sha256=artifact,
                                     verified_artifact_sha256=artifact), "INVALID_OMLX_TEMPLATE")
    _require(type(executable) is str and bool(re.fullmatch(
        r"/Users/[A-Za-z0-9_-]+/Library/Application Support/Wisp/omlx/[0-9]+\.[0-9]+\.[0-9]+/bin/omlx", executable))
        and "/omlx/" + version + "/bin/omlx" in executable, "INVALID_OMLX_EXECUTABLE")
    _require(type(executable_sha256) is str and bool(SHA.fullmatch(executable_sha256))
             and executable_sha256 == verified_executable_sha256, "UNVERIFIED_OMLX_EXECUTABLE")
    supervision = {"Label": "com.wisp.omlx", "ProgramArguments": [executable, "serve", "--host",
                   "127.0.0.1", "--port", "8000"], "Disabled": True, "RunAtLoad": False,
                   "KeepAlive": False, "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    config = {"schema_version": 1, "kind": "wisp-omlx-qualified-configuration-candidate",
              "install": prepared["install"], "config": prepared["config"],
              "executable_sha256": executable_sha256, "requires_live_qualification": True}
    return {"launchagent_plist": plistlib.dumps(supervision),
            "config_json": (json.dumps(config, sort_keys=True, indent=2) + "\n").encode()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare arrival; live adapter qualification is external.")
    parser.add_argument("--binding", required=True, help="JSON with exact source/artifact/model/resource/node/task/account pins")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--approval")
    parser.add_argument("--trusted-approval-sha256")
    parser.add_argument("--authorize-task", action="store_true")
    args = parser.parse_args(argv)
    try:
        with open(args.binding, encoding="utf-8") as stream:
            arrival_plan = plan(json.load(stream))
        if args.apply:
            _require(args.approval is not None, "APPROVAL_REQUIRED")
            with open(args.approval, encoding="utf-8") as stream:
                approval = json.load(stream)
            # No arbitrary module/plugin import and no unqualified live dispatch.
            apply(arrival_plan, approval, trusted_approval_sha256=args.trusted_approval_sha256,
                  authorize=args.authorize_task)
        else:
            print(json.dumps(arrival_plan, sort_keys=True, indent=2))
        return 0
    except ArrivalError as error:
        print(json.dumps({"schema_version": 1, "status": str(error)}, sort_keys=True))
        return 2
    except Exception:
        print('{"schema_version": 1, "status": "ARRIVAL_INVALID_INPUT"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
