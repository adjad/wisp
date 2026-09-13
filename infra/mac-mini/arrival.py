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
from pathlib import Path
import sys


def _runtime_imports():
    # Resolve only the repository layout or the signed artifact's sibling
    # runtime. Never use the invoking directory or an input-supplied module path.
    source = Path(__file__).resolve()
    root = source.parents[1] / "runtime" if source.parent.name == "preparation" else source.parents[2]
    _require((root / "mini/resources.py").is_file(), "MINI_RUNTIME_REQUIRED")
    sys.path.insert(0, str(root))

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
          trusted_adapter_sha256=None, authorize=False, now=None, simulate=False):
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
    # A prepared synthetic adapter is never a live capability, even with a valid
    # approval digest. Live adapters remain independently qualified integrations.
    _require(type(simulate) is bool, "INVALID_EXECUTION_MODE")
    _require(bool(getattr(adapter, "simulation_only", False)) == simulate,
             "ADAPTER_EXECUTION_MODE_MISMATCH")
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
    except BaseException as interruption:
        restored = True
        for action, receipt in reversed(receipts):
            try:
                adapter.restore(action, receipt)
                restored = (adapter.verify_restored(action, receipt) is True) and restored
            except BaseException:
                restored = False
        if restored and not isinstance(interruption, Exception):
            raise
        raise ArrivalError("ARRIVAL_REFUSED_ROLLED_BACK" if restored else "ARRIVAL_RECOVERY_REQUIRED") from None
    return {"schema_version": 1, "status": "ARRIVAL_SIMULATED" if simulate else "ARRIVAL_STAGED",
            "role_migration": "disabled"}


def prepare_integrated(arrival_plan, configuration, evidence, *, trusted_evidence_sha256,
                       now=None, monotonic_ns=None):
    """Validate an explicitly synthetic arrival rehearsal against shipped mini code.

    Evidence is supplied through an independent digest pin. It is not host
    measurement or permission to operate a real service. This pure preparation
    boundary reads no host state and cannot qualify the live adapter interface.
    """
    _runtime_imports()
    from mini.resources import ResourceGuard, contract, canonical, integer, GB
    now = time.time() if now is None else now
    monotonic_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    _require(arrival_plan == plan(arrival_plan["binding"]), "INVALID_PLAN")
    _require(type(trusted_evidence_sha256) is str and SHA.fullmatch(trusted_evidence_sha256)
             and digest(evidence) == trusted_evidence_sha256, "UNTRUSTED_PREPARATION_EVIDENCE")
    _keys(evidence, ("schema_version", "mode", "preflight", "identity", "artifact", "models",
                     "telemetry", "storage", "backup", "qualification", "jobs_enabled", "providers_enabled"))
    _require(type(evidence["schema_version"]) is int and evidence["schema_version"] == 1
             and evidence["mode"] == "synthetic", "SYNTHETIC_PREPARATION_REQUIRED")
    _require(evidence["jobs_enabled"] is False and evidence["providers_enabled"] is False,
             "JOBS_MUST_REMAIN_DISABLED")
    binding = arrival_plan["binding"]
    validate_preflight(evidence["preflight"], binding, now)
    identity = evidence["identity"]
    _keys(identity, ("node_id", "account", "host_key_sha256", "tailnet_policy_sha256",
                     "primary_ip", "device_approved", "magic_dns", "https", "serve"))
    _require(identity["node_id"] == binding["node_id"] and identity["account"] == binding["account"]
             and identity["primary_ip"] == "100.94.211.115"
             and all(identity[k] is True for k in ("device_approved", "magic_dns", "https"))
             and identity["serve"] == arrival_plan["routes"], "IDENTITY_NETWORK_MISMATCH")
    for key in ("host_key_sha256", "tailnet_policy_sha256"):
        _require(type(identity[key]) is str and SHA.fullmatch(identity[key]), "IDENTITY_NETWORK_MISMATCH")
    artifact = evidence["artifact"]
    _keys(artifact, ("source_sha256", "archive_sha256", "signature_verified", "publisher_key_sha256"))
    _require(artifact["source_sha256"] == binding["source_sha256"]
             and artifact["archive_sha256"] == binding["artifact_sha256"]
             and artifact["signature_verified"] is True
             and type(artifact["publisher_key_sha256"]) is str
             and SHA.fullmatch(artifact["publisher_key_sha256"]), "ARTIFACT_NOT_QUALIFIED")
    try:
        configuration = contract(configuration)
        _require(hashlib.sha256(canonical(configuration)).hexdigest() == binding["resource_contract_sha256"],
                 "RESOURCE_BINDING_MISMATCH")
        _require(evidence["models"] == configuration["models"]
                 and digest(evidence["models"]) == binding["model_sha256"], "MODEL_BINDING_MISMATCH")
        storage = evidence["storage"]
        _keys(storage, ("devices", "free_bytes", "reserved_bytes", "growth_bytes", "quota_enforced", "sole_backend"))
        _keys(storage["devices"], ("model", "cache", "telemetry", "state", "backup"))
        devices = [integer(v, 1) for v in storage["devices"].values()]
        _require(len(set(devices)) == 1 and storage["quota_enforced"] is True
                 and storage["sole_backend"] is True, "STORAGE_NOT_QUALIFIED")
        _require(integer(storage["reserved_bytes"]) == 50*GB
                 and integer(storage["free_bytes"]) >= max(150*GB, 50*GB + integer(storage["growth_bytes"])),
                 "STORAGE_CAPACITY_REFUSED")
        guard = ResourceGuard(configuration, lambda: evidence["telemetry"],
                              lambda: storage["free_bytes"], clock=lambda: monotonic_ns)
        guard.check(preflight=True)
        for model in configuration["models"]:
            guard.check(model=model)
    except ArrivalError:
        raise
    except Exception:
        raise ArrivalError("MINI_RESOURCE_REFUSED") from None
    backup = evidence["backup"]
    _keys(backup, ("schema_version", "sqlite_online", "integrity_verified", "restore_new_directory",
                   "cursor_identity", "restore_test_sha256"))
    _require(type(backup["schema_version"]) is int and backup["schema_version"] == 2
             and all(backup[k] is True for k in ("sqlite_online", "integrity_verified", "restore_new_directory"))
             and backup["cursor_identity"] == "rotate"
             and type(backup["restore_test_sha256"]) is str and SHA.fullmatch(backup["restore_test_sha256"]),
             "BACKUP_NOT_QUALIFIED")
    qualifications = evidence["qualification"]
    _require(type(qualifications) is list and len(qualifications) == len(configuration["models"]),
             "CONTEXT_NOT_QUALIFIED")
    for model, qualification in zip(configuration["models"], qualifications):
        _keys(qualification, ("model_id", "profile_sha256", "steps"))
        _require(qualification["model_id"] == model["model_id"]
                 and qualification["profile_sha256"] == model["profile_sha256"], "CONTEXT_NOT_QUALIFIED")
        steps = qualification["steps"]
        _require(type(steps) is list and len(steps) == len(model["qualified_contexts"]), "CONTEXT_NOT_QUALIFIED")
        previous = -1
        for context, step in zip(model["qualified_contexts"], steps):
            _keys(step, ("context_tokens", "completed_at", "passed", "report_sha256"))
            _require(type(step["context_tokens"]) is int and step["context_tokens"] == context
                     and type(step["completed_at"]) is int and previous < step["completed_at"] <= now
                     and step["passed"] is True and type(step["report_sha256"]) is str
                     and SHA.fullmatch(step["report_sha256"]), "CONTEXT_NOT_QUALIFIED")
            previous = step["completed_at"]
    return {"schema_version": 1, "status": "SYNTHETIC_PREPARATION_VERIFIED", "live_apply": False,
            "plan_sha256": digest(arrival_plan), "evidence_sha256": trusted_evidence_sha256,
            "resource_contract_sha256": binding["resource_contract_sha256"],
            "jobs_enabled": False, "providers_enabled": False}


class PreparedArrivalAdapter:
    """In-memory action rehearsal. No OS, provider, credential or service methods.

    The receipt binds the plan and all model/resource/storage observations.
    Revalidating before every action prevents mutable fixture drift. A real
    arrival adapter must separately prove its actual capabilities and identity.
    """
    simulation_only = True

    def __init__(self, arrival_plan, configuration, evidence, *, trusted_evidence_sha256,
                 now, monotonic_ns):
        self.plan = json.loads(json.dumps(arrival_plan))
        self.configuration = json.loads(json.dumps(configuration))
        self.evidence = json.loads(json.dumps(evidence))
        self.evidence_pin, self.now, self.monotonic_ns = trusted_evidence_sha256, now, monotonic_ns
        self.qualification_sha256 = digest(self.validate())
        self.nonces, self.actions = set(), []

    def validate(self):
        return prepare_integrated(self.plan, self.configuration, self.evidence,
                                  trusted_evidence_sha256=self.evidence_pin,
                                  now=self.now, monotonic_ns=self.monotonic_ns)

    def claim(self, nonce, plan_digest):
        if nonce in self.nonces or plan_digest != digest(self.plan):
            return False
        self.nonces.add(nonce)
        return True

    def preflight(self, arrival_plan):
        _require(arrival_plan == self.plan, "INVALID_PLAN")
        self.validate()
        return self.evidence["preflight"]

    def capture(self, action):
        _require(action in ACTIONS, "INVALID_ACTION")
        return tuple(self.actions)

    def perform(self, action, arrival_plan):
        self.preflight(arrival_plan)
        _require(len(self.actions) < len(ACTIONS) and action == ACTIONS[len(self.actions)], "INVALID_ACTION")
        self.actions.append(action)

    def verify(self, action, arrival_plan):
        self.preflight(arrival_plan)
        return bool(self.actions) and self.actions[-1] == action

    def restore(self, action, receipt):
        self.actions = list(receipt)

    def verify_restored(self, action, receipt):
        return self.actions == list(receipt)


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
    parser.add_argument("--resource-contract")
    parser.add_argument("--preparation-evidence")
    parser.add_argument("--trusted-evidence-sha256")
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
            supplied = (args.resource_contract, args.preparation_evidence, args.trusted_evidence_sha256)
            _require(all(supplied) or not any(supplied), "PAIRED_PREPARATION_INPUTS_REQUIRED")
            if all(supplied):
                _runtime_imports()
                from mini.resources import strict_json, private_read
                configuration = strict_json(private_read(args.resource_contract))
                evidence = strict_json(private_read(args.preparation_evidence))
                result = prepare_integrated(arrival_plan, configuration, evidence,
                                            trusted_evidence_sha256=args.trusted_evidence_sha256)
            else:
                result = arrival_plan
            print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except ArrivalError as error:
        print(json.dumps({"schema_version": 1, "status": str(error)}, sort_keys=True))
        return 2
    except Exception:
        print('{"schema_version": 1, "status": "ARRIVAL_INVALID_INPUT"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
