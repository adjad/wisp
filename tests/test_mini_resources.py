"""Synthetic capacity/qualification evidence; no real models or telemetry."""
import asyncio
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
import pytest

from mini.resources import GB, POLICY, ResourceGuard, ResourceRefusal, canonical, contract, strict_json


@pytest.fixture(autouse=True)
def synthetic_capacity(monkeypatch, tmp_path):
    # Qualification tests never assume real CI/developer disk capacity and
    # never share their administrative lock directory with a deployed runtime.
    import mini.resources as resources
    monkeypatch.setattr(os, "statvfs", lambda _: os.statvfs_result(
        (4096, 4096, 100000000, 75000000, 75000000, 1000000, 999999, 999999, 0, 255)))
    monkeypatch.setattr(resources, "LOCK_ROOT", tmp_path.resolve() / "test-volume-leases")


def synthetic_process(code, root):
    capacity = "import os\nos.statvfs = lambda _: os.statvfs_result((4096,4096,100000000,75000000,75000000,1000000,999999,999999,0,255))\n"
    lock = "import mini.resources\nmini.resources.LOCK_ROOT = __import__('pathlib').Path(" + repr(str(root.resolve() / "test-volume-leases")) + ")\n"
    marker = "sys.path.insert(0, sys.argv[1])"
    if marker in code:  # extracted-bundle -I interpreter gains only its own package
        return capacity + code.replace(marker, marker + "\n" + lock)
    return capacity + lock + code


def fixture_configuration():
    return {**POLICY, "models": [{"model_id": "fixture-model", "revision": "a"*40,
        "tokenizer_revision": "b"*40, "runtime_revision": "c"*40, "profile_sha256": "d"*64,
        "quantization_bits": 4, "context_tokens": 8192, "qualified_contexts": [8192],
        "request_memory_bytes": GB, "request_paged_bytes": GB}]}


def fixture_guard(configuration=None, **changes):
    configuration = configuration or fixture_configuration()
    sample = dict(schema_version=1, contract_sha256=hashlib.sha256(canonical(configuration)).hexdigest(),
                  sampled_at_ns=time.monotonic_ns(), memory_used_bytes=10*GB,
                  hot_cache_used_bytes=GB, paged_kv_used_bytes=GB, engine_limits_enforced=True)
    sample.update(changes)
    return ResourceGuard(configuration, lambda: {**sample, "sampled_at_ns": time.monotonic_ns()}, lambda: 200*GB)


@pytest.mark.parametrize("field", list(POLICY))
def test_policy_is_exact(field):
    data = fixture_configuration()
    data[field] = 2 if type(POLICY[field]) is int else True
    with pytest.raises(ResourceRefusal):
        contract(data)


@pytest.mark.parametrize("value", [True, None, -1, 1.5, 2**64, float('nan'), float('inf'), "60000000000"])
def test_invalid_telemetry_refuses(value):
    guard = fixture_guard(memory_used_bytes=value)
    with pytest.raises(ResourceRefusal):
        guard.check()


@pytest.mark.parametrize("field,limit", [("memory_used_bytes",60*GB), ("hot_cache_used_bytes",2*GB), ("paged_kv_used_bytes",20*GB)])
def test_exact_resource_boundary(field, limit):
    fixture_guard(**{field:limit}).check()
    with pytest.raises(ResourceRefusal, match="capacity_exceeded"):
        fixture_guard(**{field:limit+1}).check()


def test_free_space_preflight_reserve_and_prediction():
    guard = fixture_guard()
    for boundary, preflight in ((150*GB, True), (50*GB, False)):
        guard.free_bytes = lambda: boundary
        guard.check(preflight=preflight)
        guard.free_bytes = lambda: boundary-1
        with pytest.raises(ResourceRefusal, match="storage_capacity"):
            guard.check(preflight=preflight)
    guard.free_bytes = lambda: 50*GB
    with pytest.raises(ResourceRefusal):
        with guard.admission({"model":"fixture-model"}):
            pass


@pytest.mark.parametrize("change", [
    {"revision":"main"}, {"tokenizer_revision":"latest"}, {"runtime_revision":""},
    {"profile_sha256":"bad"}, {"quantization_bits":3}, {"quantization_bits":True},
    {"context_tokens":16384}, {"context_tokens":16384,"qualified_contexts":[16384]},
    {"qualified_contexts":[8192,8192]}, {"request_memory_bytes":2**64},
])
def test_model_refusal(change):
    data = fixture_configuration()
    data["models"][0].update(change)
    with pytest.raises(ResourceRefusal):
        contract(data)


def test_16k_progression_and_context_guard():
    data = fixture_configuration()
    data["models"][0].update(context_tokens=16384, qualified_contexts=[8192,16384])
    guard = fixture_guard(data)
    with guard.admission({"model":"fixture-model", "max_tokens":15000}):
        pass
    with pytest.raises(ResourceRefusal, match="context_exceeded"):
        with guard.admission({"model":"fixture-model", "max_tokens":16384}):
            pass
    with pytest.raises(ResourceRefusal, match="model_unqualified"):
        with guard.admission({"model":"other"}):
            pass


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{'])
def test_strict_json(raw):
    with pytest.raises(ResourceRefusal):
        strict_json(raw)


def test_attestation_stale_unavailable_and_negative_time():
    guard=fixture_guard()
    sample=guard.sample()
    for age in (2_000_000_001, -1):
        guard.sample=lambda:sample
        guard.clock=lambda:sample['sampled_at_ns']+age
        with pytest.raises(ResourceRefusal, match='stale'):
            guard.check()
    guard.clock=lambda:sample['sampled_at_ns']
    guard.sample=lambda:{**sample,'engine_limits_enforced':False}
    with pytest.raises(ResourceRefusal): guard.check()
    guard.sample=lambda:None
    with pytest.raises(ResourceRefusal): guard.check()
    guard.sample=lambda:sample
    guard.free_bytes=lambda:None
    with pytest.raises(ResourceRefusal): guard.check()


def test_admission_capacity_race_and_release(tmp_path):
    guard=fixture_guard()
    guard.lock_path=tmp_path/'lock'
    other=fixture_guard()
    other.lock_path=guard.lock_path
    with guard.admission({'model':'fixture-model'}):
        with pytest.raises(ResourceRefusal,match='busy'):
            with other.admission({'model':'fixture-model'}): pass
        with pytest.raises(ResourceRefusal,match='busy'):
            with guard.admission({'model':'fixture-model'}): pass
    with other.admission({'model':'fixture-model'}): pass


def test_private_files_unknown_contract_and_runtime_change(tmp_path):
    from mini.resources import private_read
    file=tmp_path/'telemetry'
    file.write_text('{}');file.chmod(0o600)
    assert private_read(file)==b'{}'
    file.chmod(0o644)
    with pytest.raises(ResourceRefusal): private_read(file)
    link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(ResourceRefusal): private_read(link)
    data=fixture_configuration();data['surprise']=1
    with pytest.raises(ResourceRefusal):contract(data)
    guard=fixture_guard()
    with guard.admission({'model':'fixture-model'}):
        guard.free_bytes=lambda:49*GB
        with pytest.raises(ResourceRefusal):guard.check()


async def test_production_default_refuses_and_concurrency_cannot_expand():
    from mini.gateway import Gateway
    from tests.test_mini_http import invoke,TOKEN,UPSTREAM_TOKEN
    calls=[]
    app=Gateway(TOKEN,UPSTREAM_TOKEN,transport=httpx.MockTransport(lambda r:calls.append(r)))
    assert app.concurrency==1
    status,body,_=await invoke(app)
    assert status==503 and b'resource_unavailable' in body and not calls
    for value in (True,0,2,4):
        with pytest.raises(ValueError): Gateway(TOKEN,UPSTREAM_TOKEN,concurrency=value)


async def test_runtime_deterioration_cancels_stalled_inference():
    from mini.gateway import Gateway
    from tests.test_mini_http import invoke,TOKEN,UPSTREAM_TOKEN
    started=asyncio.Event(); cancelled=asyncio.Event()
    async def upstream(request):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    guard=fixture_guard()
    app=Gateway(TOKEN,UPSTREAM_TOKEN,resources=guard,transport=httpx.MockTransport(upstream))
    request=asyncio.create_task(invoke(app))
    await started.wait()
    guard.free_bytes=lambda:0
    status,body,_=await request
    assert status==503 and b'storage_capacity_refused' in body and cancelled.is_set()


async def test_orphan_transport_keeps_single_admission_latched():
    from mini.gateway import Gateway
    from tests.test_mini_http import invoke,TOKEN,UPSTREAM_TOKEN,response
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def upstream(request):
        calls.append(request);entered.set()
        while not release.is_set():
            try:await release.wait()
            except asyncio.CancelledError:pass
        return response({'choices':[{'message':{'content':'fixture'},'finish_reason':'stop'}]})
    app=Gateway(TOKEN,UPSTREAM_TOKEN,resources=fixture_guard(),deadline=0.02,transport=httpx.MockTransport(upstream))
    first=asyncio.create_task(invoke(app));await entered.wait()
    assert (await first)[0]==504
    assert (await invoke(app))[0]==429 and len(calls)==1
    release.set()
    for _ in range(100):
        if not app._executing:break
        await asyncio.sleep(.01)
    assert not app._executing


async def test_cancel_before_transport_coroutine_starts_releases_latch(monkeypatch):
    from mini.gateway import Gateway
    from tests.test_mini_http import invoke,TOKEN,UPSTREAM_TOKEN,response
    app=Gateway(TOKEN,UPSTREAM_TOKEN,resources=fixture_guard(),transport=httpx.MockTransport(
        lambda _:response({'status':'ok'})))
    original=asyncio.create_task
    def cancel_execute(coro,**kwargs):
        task=original(coro,**kwargs)
        if coro.__qualname__.endswith('.execute'):
            task.cancel()
        return task
    monkeypatch.setattr(asyncio,'create_task',cancel_execute)
    with pytest.raises(asyncio.CancelledError):await invoke(app)
    assert not app._executing
    monkeypatch.setattr(asyncio,'create_task',original)
    assert (await invoke(app,'/health',method='GET'))[0]==200


def test_same_volume_lease_excludes_inference_store_and_snapshot(tmp_path):
    from mini.resources import volume_lease
    from mini.backup import backup
    from mini.store import Store
    state=Store(tmp_path.resolve()/'state','fixture-mini')
    parent=tmp_path.resolve()/'backups';parent.mkdir(mode=0o700)
    guard=fixture_guard();guard.storage_path=state.path.parent
    with guard.admission({'model':'fixture-model'}):
        with pytest.raises(ResourceRefusal,match='volume_busy'):
            state.register_job('fixture','study.generate')
        with pytest.raises(ResourceRefusal,match='volume_busy'):
            backup(state,parent/'blocked.sqlite3')
    state.register_job('fixture','study.generate')
    with volume_lease(parent):
        with pytest.raises(ResourceRefusal,match='volume_busy'):
            with guard.admission({'model':'fixture-model'}):pass


def test_predicted_allocation_exact_boundary():
    guard=fixture_guard(memory_used_bytes=59*GB,paged_kv_used_bytes=19*GB)
    guard.free_bytes=lambda:51*GB
    with guard.admission({'model':'fixture-model'}):pass
    guard.free_bytes=lambda:51*GB-1
    with pytest.raises(ResourceRefusal):
        with guard.admission({'model':'fixture-model'}):pass


def test_default_cli_service_is_unqualified_single_request_gateway(monkeypatch):
    import sys,uvicorn
    from mini.__main__ import main
    from mini.resources import Unqualified
    monkeypatch.setattr(sys,'argv',['mini'])
    monkeypatch.setenv('WISP_MINI_INFERENCE_KEY','a'*64)
    monkeypatch.setenv('WISP_LOCAL_OMLX_KEY','b'*64)
    seen=[]
    monkeypatch.setattr(uvicorn,'run',lambda app,**kwargs:seen.append((app,kwargs)))
    assert main()==0
    assert seen[0][0].concurrency==1 and isinstance(seen[0][0].resources,Unqualified)
    assert seen[0][1]['host']=='127.0.0.1' and seen[0][1]['port']==8765


def test_file_configuration_and_fresh_telemetry_are_required_together(tmp_path):
    configuration=fixture_configuration()
    parent=tmp_path.resolve()/'private';parent.mkdir(mode=0o700)
    config=parent/'contract.json';telemetry=parent/'telemetry.json'
    config.write_bytes(canonical(configuration));config.chmod(0o600)
    telemetry.write_bytes(canonical(fixture_guard(configuration).sample()));telemetry.chmod(0o600)
    guard=ResourceGuard.from_files(config,telemetry)
    guard.check(preflight=True)
    telemetry.write_bytes(b'{}')
    with pytest.raises(ResourceRefusal):guard.check()
    config.write_bytes(b'{"schema_version":99}')
    with pytest.raises(ResourceRefusal):ResourceGuard.from_files(config,telemetry)
