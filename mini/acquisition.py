"""Provider-neutral acquisition contracts with synthetic capabilities only.

No HTTP, filesystem, shell, native, credential store or dynamic plugin access.
Real adapters must be implemented and independently qualified later; declaring
an origin or credential role never grants a network or credential capability.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Protocol

from mini.adapters import KINDS, REVISION, SnapshotAdapter

ROLES = {"canvas.sync": "canvas.read", "study.generate": "study.read",
         "stocks.watch": "stocks.read", "research.run": "research.read"}
ERRORS = {"disabled", "unqualified", "schema", "binding", "oversize", "forbidden_content",
          "rate", "busy", "timeout", "provider_refused", "cancelled"}


class AcquisitionRefusal(ValueError):
    def __init__(self, code):
        super().__init__(code if code in ERRORS else "provider_refused")


def require(value, code="schema"):
    if not value:
        raise AcquisitionRefusal(code)


def keys(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def encoded(value, maximum=100000):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise AcquisitionRefusal("schema") from None
    require(len(raw) <= maximum, "oversize")
    return raw


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def token(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value))


def contract(value):
    encoded(value, 10000)
    keys(value, ("schema_version", "connector_id", "version", "kind", "origin", "credential_role",
                 "capabilities", "classification", "privacy", "limits", "enabled"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    token(value["connector_id"])
    require(type(value["version"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["version"]))
    require(type(value["kind"]) is str and value["kind"] in KINDS)
    # Reserved .invalid namespace: synthetic declarations cannot address a host.
    require(type(value["origin"]) is str and re.fullmatch(r"https://[a-z][a-z0-9-]{0,62}\.invalid", value["origin"]), "binding")
    require(value["credential_role"] == ROLES[value["kind"]], "binding")
    require(value["capabilities"] == ["snapshot.acquire", "text.outline"], "unqualified")
    require(value["classification"] == "synthetic" and value["privacy"] == {
        "outbound_private_data": False, "persist": "synthetic-only"}, "unqualified")
    require(value["enabled"] is False, "disabled")
    keys(value["limits"], ("requests_per_minute", "deadline_ms", "max_request_bytes", "max_result_bytes"))
    for key, low, high in (("requests_per_minute",1,60), ("deadline_ms",1,1000),
                           ("max_request_bytes",1024,10000), ("max_result_bytes",1024,100000)):
        v = value["limits"][key]
        require(type(v) is int and low <= v <= high)
    return json.loads(encoded(value))


def qualification(configuration, receipt, trusted_sha256):
    require(type(trusted_sha256) is str and digest(receipt) == trusted_sha256, "unqualified")
    require(receipt == {"schema_version": 1, "contract_sha256": digest(configuration),
                       "scope": "synthetic-acquisition-only", "status": "PASS"}, "unqualified")


def request(configuration, *, node_id, job_id, now, snapshot_id):
    token(node_id); token(job_id); token(snapshot_id)
    require(type(now) is int and 0 <= now <= 2**53)
    value = {"schema_version": 1, **{k:configuration[k] for k in (
        "connector_id", "kind", "origin", "credential_role", "classification")},
        "connector_version": configuration["version"], "operation": "snapshot.acquire",
        "parameters": {"snapshot_id": snapshot_id}}
    value["request_id"] = digest({"node_id":node_id,"job_id":job_id,"now":now,"request":value})
    validate_request(configuration, value)
    return value


def validate_request(configuration, value):
    encoded(value, configuration["limits"]["max_request_bytes"])
    keys(value, ("schema_version", "request_id", "connector_id", "connector_version", "kind",
                 "origin", "credential_role", "classification", "operation", "parameters"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(type(value["request_id"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["request_id"]))
    require(all(value[k] == configuration[k] for k in (
        "connector_id", "kind", "origin", "credential_role", "classification"))
        and value["connector_version"] == configuration["version"], "binding")
    require(value["operation"] == "snapshot.acquire", "unqualified")
    keys(value["parameters"], ("snapshot_id",))
    token(value["parameters"]["snapshot_id"])


def validate_result(configuration, requested, value):
    encoded(value, configuration["limits"]["max_result_bytes"])
    require(type(value) is dict)
    status = value.get("status")
    keys(value, ("schema_version", "request_id", "origin", "credential_role", "classification", "status",
                 "snapshot" if status == "ok" else "error"))
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(all(value[k] == requested[k] for k in ("request_id", "origin", "credential_role", "classification")), "binding")
    if status != "ok":
        require(status == "error" and type(value["error"]) is str and value["error"] in ERRORS)
        raise AcquisitionRefusal(value["error"])
    snapshot = value["snapshot"]
    adapter = SnapshotAdapter(configuration["kind"], enabled=True, qualification={
        "kind": configuration["kind"], "revision": REVISION, "scope": "portable-snapshot-only"})
    try:
        adapter.validate(snapshot)
    except Exception:
        raise AcquisitionRefusal("schema") from None
    # Content is plain presentation text. No executable or credential-bearing
    # representation, resource locator, markup script or provider-private class.
    forbidden = re.compile(r"```|<\s*(?:script|iframe)|(?:https?|file|javascript|data)://|\b(?:Bearer\s+|sk-[A-Za-z0-9]|(?:api[_-]?key|password|token)\s*[:=])|\b[0-9a-f]{64}\b|#!|\$\(|(?:^|\n)\s*(?:sudo|exec|curl|wget|bash|sh)\s", re.I)
    require(not any(forbidden.search(item[k]) for item in snapshot["items"] for k in ("title","text")), "forbidden_content")
    return json.loads(encoded(snapshot))


class AcquisitionAdapter(Protocol):
    async def acquire(self, value: dict) -> dict: ...


class DisabledAdapter:
    async def acquire(self, value):
        raise AcquisitionRefusal("disabled")


class FixtureAdapter:
    """Inert injected response; no callable, transport or endpoint interpretation."""
    def __init__(self, snapshot, *, delay=0):
        require(type(delay) in (int,float) and 0 <= delay <= 1)
        self.snapshot = json.loads(encoded(snapshot))
        self.delay = delay

    async def acquire(self, value):
        await asyncio.sleep(self.delay)
        return {"schema_version":1, **{k:value[k] for k in ("request_id","origin","credential_role","classification")},
                "status":"ok", "snapshot":json.loads(encoded(self.snapshot))}


class Connector:
    def __init__(self, configuration, *, adapter=None, receipt=None, trusted_receipt_sha256=None, simulate=False):
        self.configuration = contract(configuration)
        self._contract_pin = digest(self.configuration)
        self.adapter = DisabledAdapter() if adapter is None else adapter
        require(type(simulate) is bool)
        self.simulate = simulate
        self._receipt = json.loads(encoded(receipt)) if receipt is not None else None
        self._receipt_pin = trusted_receipt_sha256
        if simulate:
            require(type(self.adapter) is FixtureAdapter, "unqualified")
            qualification(self.configuration, receipt, trusted_receipt_sha256)
        else:
            require(type(self.adapter) is DisabledAdapter, "disabled")
        self._active = None
        self._times = []

    async def acquire(self, value):
        require(self.simulate, "disabled")
        configuration = contract(self.configuration)
        require(digest(configuration) == self._contract_pin and type(self.adapter) is FixtureAdapter, "unqualified")
        qualification(configuration, self._receipt, self._receipt_pin)
        validate_request(configuration, value)
        require(self._active is None or self._active.done(), "busy")
        now = time.monotonic()
        self._times = [t for t in self._times if now-t < 60]
        require(len(self._times) < configuration["limits"]["requests_per_minute"], "rate")
        self._times.append(now)
        task = asyncio.create_task(self.adapter.acquire(json.loads(encoded(value))))
        self._active = task
        def consumed(done):
            if not done.cancelled():
                done.exception()  # never emit unhandled task diagnostics
        task.add_done_callback(consumed)
        try:
            done, _ = await asyncio.wait({task}, timeout=configuration["limits"]["deadline_ms"]/1000)
            if not done:
                task.cancel()
                raise AcquisitionRefusal("timeout")
            return validate_result(configuration, value, task.result())
        except asyncio.CancelledError:
            task.cancel()
            raise
        except AcquisitionRefusal:
            raise
        except Exception:
            raise AcquisitionRefusal("provider_refused") from None


def snapshot_receipt(configuration, asked, snapshot, now):
    return {"request_id":asked['request_id'], "contract_sha256":digest(configuration),
            "snapshot_sha256":digest(snapshot), "batch_time":now,
            "configuration":json.loads(encoded(configuration)), "request":json.loads(encoded(asked))}


def validate_receipt(receipt, snapshot, *, node_id, job_id, kind):
    keys(receipt, ("request_id","contract_sha256","snapshot_sha256","batch_time","configuration","request"))
    configuration = contract(receipt['configuration'])
    require(configuration['kind'] == kind, 'binding')
    validate_request(configuration, receipt['request'])
    expected = request(configuration, node_id=node_id, job_id=job_id, now=receipt['batch_time'],
                       snapshot_id=receipt['request']['parameters']['snapshot_id'])
    require(receipt['request'] == expected and receipt['request_id'] == expected['request_id']
            and receipt['contract_sha256'] == digest(configuration)
            and receipt['snapshot_sha256'] == digest(snapshot), 'binding')
    validate_result(configuration, expected, {"schema_version":1,
        **{k:expected[k] for k in ('request_id','origin','credential_role','classification')},
        "status":"ok", "snapshot":snapshot})


async def acquire_tick(runtime, now, connectors, snapshot_ids):
    """Acquire synthetic snapshots then atomically stage receipts/occurrences.

    A failed batch has no newly staged occurrence. Already persisted pending work
    completes before another acquisition, so restart never depends on a provider.
    """
    require(type(connectors) is dict and type(snapshot_ids) is dict)
    if not runtime.enabled_jobs:
        return []
    require(set(connectors) == set(snapshot_ids) == set(runtime.enabled_jobs))
    completed = runtime.complete_pending()
    snapshots, receipts = {}, {}
    with runtime.store.connect() as db:
        kinds = {r['job_id']:r['kind'] for r in db.execute('SELECT job_id,kind FROM jobs')}
    for jid in sorted(connectors):
        connector = connectors[jid]
        require(type(connector) is Connector and connector.configuration['kind'] == kinds.get(jid), "binding")
        asked = request(connector.configuration, node_id=runtime.store.node_id, job_id=jid,
                        now=now, snapshot_id=snapshot_ids[jid])
        snapshots[jid] = await connector.acquire(asked)
        receipts[jid] = snapshot_receipt(connector.configuration, asked, snapshots[jid], now)
    runtime.stage(now, snapshots, acquisition=receipts)
    return completed + runtime.complete_pending()
