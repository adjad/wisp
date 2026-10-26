"""Synthetic checks for the guided inference setup. No network, no real Wisp state."""
import asyncio
import ast
import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from service.inference.inference_errors import ModelLoadError
from service.setup import catalog, guide
from service.setup.engines import EngineProfile, EngineState, detect_external_engines
from service.setup.hardware import Hardware, current_hardware, detect_hardware

Q6, Q5, Q4 = "Ling-3.0-tiny-oQ6e", "Ling-3.0-tiny-oQ5e", "Ling-3.0-tiny-oQ4e"
GEMMA = "gemma-4-E4B-it-oQ4e-mtp"
G = GEMMA
MELLUM = "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e"
EMBED = "Qwen3-Embedding-0.6B-4bit-DWQ"


@pytest.fixture(autouse=True)
def _modern_host(monkeypatch):
    monkeypatch.setattr(guide, "host_macos_major", lambda: 26)


def _roles(model, endpoint="local"):
    return {r: {"model": model, "endpoint": endpoint} for r in guide.TEXT_ROLES}


def _status(*, ram=24, installed=(), running=True, is_installed=True, roles=None, externals=()):
    return guide.build_status(
        hardware=Hardware("Apple M5 Pro", ram), omlx=guide.OmlxState(is_installed, running, "/models"),
        installed=list(installed), models_source="live" if running else "saved",
        roles=roles or _roles(Q4), externals=list(externals), tool_capable=[])


def _check(status, check_id):
    return next(c for c in status["checks"] if c["id"] == check_id)


# --- hardware ---------------------------------------------------------------

@pytest.mark.parametrize("gib,tier", [(64, "roomy"), (32, "roomy"), (24, "comfortable"),
                                      (16, "standard"), (8, "tight"), (0, "tight")])
def test_ram_tiers(gib, tier):
    assert Hardware("x", gib).tier == tier


def test_hardware_rounds_real_memory_and_survives_failure():
    assert detect_hardware(lambda a: {"hw.memsize": "25769803776"}.get(a[0], "Apple M5 Pro")).ram_gb == 24
    assert detect_hardware(lambda a: {"hw.memsize": "17179869184"}.get(a[0], "")).ram_gb == 16

    def broken(_args):
        raise OSError("no sysctl")
    hw = detect_hardware(broken)
    assert hw.ram_gb == 0 and hw.tier == "tight"
    assert detect_hardware(lambda a: "not a number").ram_gb == 0


# --- recommendations --------------------------------------------------------

def test_fresh_mac_downloads_gemma_on_every_memory_size():
    missing = _status(ram=32, installed=[], roles=_roles(Q4))
    assert missing["models"]["recommended"] == GEMMA
    assert missing["models"]["download"]["model"] == GEMMA
    assert missing["models"]["download"]["repo"] == "Jundot/gemma-4-E4B-it-oQ4e-mtp"
    assert _check(missing, "model")["action"] == "download_model"
    assert not missing["ready"]
    small = _status(ram=16, installed=[], roles=_roles(MELLUM))
    assert small["models"]["recommended"] == GEMMA
    assert small["models"]["download"]["model"] == GEMMA


def test_gemma_repo_name_is_the_model_id_and_its_profile_is_set():
    model = catalog.by_id(GEMMA)
    assert model.repo.split("/")[1] == GEMMA
    p = model.engine_profile
    assert (p["temperature"], p["top_p"], p["top_k"]) == (0.8, 0.95, 64)
    # Thinking off returns zero tokens after a tool result on this model.
    assert p["enable_thinking"] is True
    assert p["mtp_enabled"] is True and p["max_context_window"] == 16384
    # TurboQuant 6-bit KV by default; the bundled oMLX 0.7.0 has no scheme key.
    assert p["turboquant_kv_enabled"] is True and p["turboquant_kv_bits"] == 6
    assert "turboquant_kv_scheme" not in p


def test_mellum_repo_name_is_the_model_id():
    # The engine serves a download under its repo name; roles name the model id.
    repo = catalog.by_id(MELLUM).repo
    assert repo and repo.split("/")[1] == MELLUM


def test_configured_mellum_is_kept_when_another_model_is_recommended():
    roles = _roles(MELLUM)
    found = _status(ram=32, installed=[Q6, MELLUM, EMBED], roles=roles)
    assert found["models"]["use"] == Q6  # Best installed; Gemma is offered, not assumed.
    assert all(role["model"] == MELLUM for role in found["roles"])
    assert roles == _roles(MELLUM)
    entry = next(m for m in found["models"]["installed"] if m["id"] == MELLUM)
    assert entry["tested"] is False
    assert entry["fit"] == "good"
    assert found["models"]["recommended"] == GEMMA
    assert found["ready"]  # Discovery/readiness is separate from inference qualification.
    assert guide.download_info(MELLUM, "/models")["repo"] == catalog.by_id(MELLUM).repo


def test_an_installed_ling_keeps_working_and_gemma_is_offered():
    result = catalog.recommend(32, [Q4])
    assert result["first_choice"] == GEMMA
    assert result["use"] == Q4
    assert result["get"] == GEMMA
    assert catalog.fit(catalog.by_id(MELLUM), 16) == "heavy"
    assert catalog.recommend(16, [Q4, MELLUM])["use"] == Q4


@pytest.mark.parametrize("custom", [False, True])
def test_gemma_fresh_defaults_preserve_existing_user_overrides(tmp_path, monkeypatch, custom):
    import service.config as config
    overlay = tmp_path / "config.yaml"
    value = {"roles": {"general": MELLUM, "agent": "chosen-agent",
                       "embedding": "chosen-embedding", "reranker": "chosen-reranker"},
             "inference": {"bindings": {"reasoning": {
                 "endpoint": "local_provider", "model_id": "chosen-reasoning"}}}}
    if custom:
        overlay.write_text(json.dumps(value))
    before = overlay.read_bytes() if custom else None
    monkeypatch.setattr(config, "USER_CONFIG", overlay)
    config._packaged_config.cache_clear()
    config.models_config.cache_clear()
    try:
        result = config.models_config()
        for role in ("router", "fast", "agent", "general", "coding", "reasoning"):
            assert result["roles"][role] == (value["roles"].get(role, GEMMA) if custom else GEMMA)
        assert result["roles"]["embedding"] == ("chosen-embedding" if custom else EMBED)
        assert result["roles"]["reranker"] == ("chosen-reranker" if custom else "Qwen3-Reranker-0.6B-mlx-6bit")
        if custom:
            assert result["inference"]["bindings"]["reasoning"] == value["inference"]["bindings"]["reasoning"]
            assert overlay.read_bytes() == before
        else:
            assert not overlay.exists()
    finally:
        config.models_config.cache_clear()

@pytest.mark.parametrize("ram", [64, 24, 16, 12, 8, 0])
def test_first_choice_is_gemma_on_every_mac(ram):
    assert catalog.recommend(ram, [])["first_choice"] == GEMMA
    assert catalog.recommend(ram, [Q4])["first_choice"] == GEMMA


@pytest.mark.parametrize("ram,second", [(64, Q6), (24, Q6), (16, Q5), (12, Q4)])
def test_ling_keeps_its_memory_order_behind_gemma(ram, second):
    assert catalog.ranked_for(ram, [Q4])[1].id == second


def test_unpublished_ling_build_is_never_offered_on_a_fresh_mac():
    assert catalog.by_id(Q5).repo is None
    assert Q5 not in [m.id for m in catalog.ranked_for(16, [])]
    assert catalog.recommend(16, [Q5])["get"] == GEMMA


def test_24gb_with_only_oq4e_keeps_it_and_offers_gemma():
    rec = catalog.recommend(24, [Q4])
    assert rec["use"] == Q4 and rec["get"] == GEMMA


def test_best_installed_model_wins_and_nothing_is_offered_when_optimal():
    rec = catalog.recommend(24, [Q4, Q6, MELLUM, GEMMA])
    assert rec["use"] == GEMMA and rec["get"] is None
    rec = catalog.recommend(24, [Q4, Q6, MELLUM])
    assert rec["use"] == Q6 and rec["get"] == GEMMA


def test_heavy_model_on_16gb_is_flagged_but_kept():
    assert catalog.fit(catalog.by_id(Q6), 16) == "heavy"
    assert catalog.fit(catalog.by_id(Q6), 24) == "good"
    assert catalog.fit(catalog.by_id(GEMMA), 8) == "recommended"
    assert catalog.recommend(16, [Q6])["use"] == Q6


def test_small_memory_warns_and_unknown_memory_is_cautious():
    assert "less than 16 GB" in catalog.recommend(8, [])["warning"]
    assert "could not read" in catalog.recommend(0, [])["warning"]
    assert catalog.recommend(24, [])["warning"] is None


# --- checklist --------------------------------------------------------------

def test_fresh_mac_needs_oMLX_first():
    s = _status(installed=(), running=False, is_installed=False)
    assert s["ready"] is False
    assert _check(s, "engine")["state"] == "todo" and _check(s, "engine")["action"] == "install_omlx"
    assert "compatible with this Mac" in _check(s, "engine")["detail"]
    assert s["omlx"]["min_macos"] == 26


def test_old_macos_is_not_sent_to_download_omlx(monkeypatch):
    monkeypatch.setattr(guide, "host_macos_major", lambda: 15)
    s = _status(installed=(), running=False, is_installed=False)
    check = _check(s, "engine")
    assert check["action"] == "unsupported_macos" and "url" not in check
    assert "Ollama" in check["detail"] and "remote-only" in check["detail"]
    # An oMLX the user already has keeps working whatever the host says.
    assert _check(_status(running=False, is_installed=True), "engine")["action"] == "start_engine"
    # An unreadable version never blocks the normal install path.
    monkeypatch.setattr(guide, "host_macos_major", lambda: None)
    assert _check(_status(installed=(), running=False, is_installed=False), "engine")["action"] == "install_omlx"


def test_fresh_mac_recommendation_is_public_and_labelled():
    s = _status(installed=(), running=True, roles=_roles(MELLUM))
    assert _check(s, "model")["action"] == "download_model"
    rec = catalog.by_id(s["models"]["recommended"])
    assert rec.repo and s["models"]["recommended_label"] == f"{rec.label} · {rec.quant}"


@pytest.mark.parametrize("macos,available", [("14.0", False), ("26.1", False),
                                             ("26.2", True), ("27.0", True), ("", False)])
def test_setup_preserves_packaged_requirement_after_external_fallback(tmp_path, monkeypatch, macos, available):
    from service.engine import mode
    (tmp_path / "py/bin").mkdir(parents=True)
    (tmp_path / "py/bin/python3").write_text("synthetic interpreter placeholder")
    (tmp_path / "ENGINE.json").write_text(json.dumps({"minimum_macos": "26.2"}))
    monkeypatch.setattr(mode, "engine_code_root", lambda: tmp_path)
    monkeypatch.setattr(mode.platform, "mac_ver", lambda: (macos, (), ""))
    monkeypatch.setattr(guide, "main_engine_profile", lambda: guide.OMLX)

    missing = _status(running=False, is_installed=False)
    support = missing["bundled_engine"]
    assert support["available"] is available
    assert support["minimum_macos"] == "26.2"
    assert (support["unavailable_reason"] is None) is available
    assert not missing["ready"] and not missing["omlx"]["bundled"]
    if not available:
        assert "26.2" in support["unavailable_reason"]

    # This bundled build's floor must not block an independently working engine.
    working = _status(installed=[Q4], roles=_roles(Q4))
    assert working["ready"] and not working["omlx"]["bundled"]
    assert working["bundled_engine"] == support


@pytest.mark.parametrize("document", [{}] + [{"minimum_macos": value} for value in
    (None, "", False, 0, 26.2, "26.2.1.5", "invalid", {"secret": "opaque"})])
def test_existing_manifest_cannot_omit_or_corrupt_its_requirement(tmp_path, document):
    from service.engine import mode
    (tmp_path / "ENGINE.json").write_text(json.dumps(document))
    assert mode.unsupported_reason(tmp_path, "27.0") is not None
    support = mode.bundled_compatibility(tmp_path, "27.0")
    assert support["available"] is False and support["minimum_macos"] is None
    assert support["unavailable_reason"] == "The bundled engine's macOS requirement is invalid"
    assert "opaque" not in json.dumps(support)


def test_source_checkout_remains_usable_without_packaged_engine_metadata(tmp_path):
    from service.engine import mode
    assert mode.unsupported_reason(tmp_path, "14.0") is None
    assert mode.bundled_compatibility(tmp_path, "14.0") == {
        "available": False, "minimum_macos": None,
        "unavailable_reason": "The built-in engine is not included in this build."}


def test_installed_but_stopped_engine_offers_start():
    s = _status(installed=(), running=False, is_installed=True)
    assert _check(s, "engine")["state"] == "warn" and _check(s, "engine")["action"] == "start_engine"


def test_running_with_no_chat_model_points_at_download():
    s = _status(ram=16, installed=[EMBED], running=True)
    assert _check(s, "model")["action"] == "download_model"
    assert s["models"]["download"]["model"] == GEMMA
    assert s["ready"] is False


def test_roles_pointing_at_an_uninstalled_model_are_caught():
    # A user's saved oQ4e selection is absent; they only have oQ6e.
    s = _status(installed=[Q6], roles=_roles(Q4))
    assert _check(s, "model")["state"] == "todo" and _check(s, "model")["action"] == "apply_model"
    assert Q4 in _check(s, "model")["detail"]
    assert s["ready"] is False


def test_ready_when_engine_runs_and_roles_resolve():
    s = _status(installed=[Q6, EMBED], roles=_roles(Q6))
    assert s["ready"] is True
    assert _check(s, "search")["state"] == "ok"


def test_bundled_engine_needs_live_readiness_and_always_offers_retry(monkeypatch):
    monkeypatch.setattr(guide, "main_engine_profile", lambda: guide.BUNDLED)
    fresh = _status(installed=[], running=False, is_installed=False)
    cached = _status(installed=[Q4], running=False, roles=_roles(Q4))
    for status in (fresh, cached):
        assert not status["ready"] and status["omlx"]["bundled"] is True
        assert _check(status, "engine")["state"] == "warn"
        assert _check(status, "engine")["action"] == "start_engine"
    recovered = _status(installed=[Q4], running=True, roles=_roles(Q4))
    assert recovered["ready"] and _check(recovered, "engine")["state"] == "ok"


def test_roles_bound_to_another_endpoint_are_not_reported_missing():
    roles = _roles(Q6)
    roles["reasoning"] = {"model": "gemma:latest", "endpoint": "local_provider"}
    assert _check(_status(installed=[Q6], roles=roles), "model")["state"] == "ok"


def test_missing_embedder_is_optional():
    s = _status(installed=[Q6], roles=_roles(Q6))
    assert _check(s, "search")["state"] == "warn"
    assert s["ready"] is True
    assert s["models"]["embedding_download"]["repo"] == "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ"


def test_model_list_is_ranked_and_excludes_embedding_and_reranker():
    s = _status(ram=24, installed=["zzz-custom", Q4, "Qwen3-Reranker-0.6B-mlx-6bit", EMBED, Q6, G])
    ids = [m["id"] for m in s["models"]["installed"]]
    assert ids == [G, Q6, Q4, "zzz-custom"]
    by_id = {m["id"]: m for m in s["models"]["installed"]}
    assert by_id[Q6]["fit"] == "good" and by_id[Q6]["tested"] is True
    assert by_id["zzz-custom"]["fit"] == "unknown" and by_id["zzz-custom"]["tested"] is False


def test_download_command_targets_the_omlx_model_folder():
    info = guide.download_info(Q4, "/Users/x/Models/")
    assert info["command"] == "hf download mlx-works/Ling-3.0-tiny-oQ4e --local-dir /Users/x/Models/mlx-works/Ling-3.0-tiny-oQ4e"
    assert info["url"] == "https://huggingface.co/mlx-works/Ling-3.0-tiny-oQ4e"
    assert guide.download_info(Q5, "/m") is None            # nothing published
    assert guide.download_info(None, "/m") is None
    assert guide.download_info(Q4, None)["command"] == "hf download mlx-works/Ling-3.0-tiny-oQ4e"


def test_status_points_at_omlx_downloader_and_model_folder_defaults(monkeypatch):
    s = _status(installed=[], running=True)
    assert s["omlx"]["admin_url"] == "http://127.0.0.1:8000/admin"
    import service.main as main
    import service.config as config
    monkeypatch.setattr(config, "omlx_settings", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    assert main._omlx_model_dir().endswith("/.omlx/models")
    monkeypatch.setattr(config, "omlx_settings", lambda: {"model": {"model_dirs": ["/Volumes/Big/Models"]}})
    assert main._omlx_model_dir() == "/Volumes/Big/Models"
    monkeypatch.setattr(config, "omlx_settings", lambda: {"model": {"model_dir": "/single"}})
    assert main._omlx_model_dir() == "/single"


def test_download_command_is_shell_safe_for_hostile_folder_names():
    import shlex
    for folder in ('/Users/x/My "Models"', "/Users/x/$(touch pwned)", "/Users/x/`id`/m", "/Users/x/a b;rm -rf ~"):
        command = guide.download_info(Q4, folder)["command"]
        tokens = shlex.split(command)
        assert tokens[:3] == ["hf", "download", "mlx-works/Ling-3.0-tiny-oQ4e"]
        assert tokens[4] == f"{folder}/mlx-works/Ling-3.0-tiny-oQ4e"     # one literal argument
        assert len(tokens) == 5


def test_unknown_memory_does_not_mark_the_smallest_build_heavy():
    assert catalog.fit(catalog.by_id(GEMMA), 0) == "recommended"
    assert catalog.fit(catalog.by_id(Q4), 0) == "good"
    assert catalog.fit(catalog.by_id(Q6), 0) == "heavy"


def test_partial_role_mapping_does_not_crash_the_checklist():
    s = guide.build_status(
        hardware=Hardware("x", 24), omlx=guide.OmlxState(True, True, "/m"), installed=[Q6],
        models_source="live", roles={"fast": {"model": Q6, "endpoint": "local"}},
        externals=[], tool_capable=[])
    assert _check(s, "model")["state"] == "todo" and "(none)" in _check(s, "model")["detail"]


def test_admin_url_follows_the_configured_omlx_port(monkeypatch):
    import service.main as main
    import service.config as config
    monkeypatch.setattr(config, "omlx_base_url", lambda: "http://127.0.0.1:8123")
    assert main._omlx_admin_url() == "http://127.0.0.1:8123/admin"
    monkeypatch.setattr(config, "omlx_base_url", lambda: (_ for _ in ()).throw(KeyError("omlx")))
    assert main._omlx_admin_url() == "http://127.0.0.1:8000/admin"
    s = guide.build_status(
        hardware=Hardware("x", 24), omlx=guide.OmlxState(True, True, "/m", "http://127.0.0.1:8123/admin"),
        installed=[Q6], models_source="live", roles=_roles(Q6), externals=[], tool_capable=[])
    assert s["omlx"]["admin_url"] == "http://127.0.0.1:8123/admin"


def test_hardware_is_detected_once_but_failures_are_not_cached(monkeypatch):
    import service.setup.hardware as hw
    monkeypatch.setattr(hw, "_cached", None)
    calls = []
    results = iter([Hardware("x", 0), Hardware("Apple M5 Pro", 24), Hardware("never", 8)])
    monkeypatch.setattr(hw, "detect_hardware", lambda: calls.append(1) or next(results))
    assert current_hardware().ram_gb == 0            # failed read: not cached
    assert current_hardware().ram_gb == 24           # retried, now cached
    assert current_hardware().ram_gb == 24
    assert len(calls) == 2


def test_setting_roles_is_one_overlay_write_with_coupled_roles(monkeypatch):
    import service.config as config
    writes = []
    monkeypatch.setattr(config, "_save_overlay", writes.append)
    config.set_roles({r: Q6 for r in guide.TEXT_ROLES})
    assert len(writes) == 1
    assert set(writes[0]["roles"]) == {"fast", "router", "general", "agent", "coding", "reasoning"}
    assert all(b["endpoint"] == "local" and b["model_id"] == Q6
               for b in writes[0]["inference"]["bindings"].values())
    writes.clear()
    config.set_role("general", Q4)                   # the old single-role API is unchanged
    assert writes[0]["roles"] == {"general": Q4, "agent": Q4}


def test_engine_cards_say_what_each_can_do():
    detected = [EngineState(EngineProfile("ollama", "Ollama", 11434, False, "s", "h", None),
                            11434, True, ["llama3:8b"])]
    s = _status(installed=[Q6], roles=_roles(Q6), externals=detected)
    cards = {e["id"]: e for e in s["engines"]}
    assert cards["omlx"]["runs_tools"] is True and cards["omlx"]["running"] is True
    assert cards["ollama"]["running"] is True and cards["ollama"]["runs_tools"] is False
    assert cards["ollama"]["model_ids"] == ["llama3:8b"]
    assert cards["lmstudio"]["running"] is False
    assert "port 8000" in cards["mtplx"]["summary"]        # its default port collides with oMLX
    assert len([e for e in s["engines"] if e["id"] == "ollama"]) == 1


# --- external engine detection ---------------------------------------------

def _transport(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port not in routes:
            raise httpx.ConnectError("refused")
        status, body = routes[port]
        return httpx.Response(status, json=body) if isinstance(body, (dict, list)) else httpx.Response(status, content=body)
    return httpx.MockTransport(handler)


def test_detects_running_apps_and_lists_their_models():
    found = asyncio.run(detect_external_engines(transport=_transport({
        11434: (200, {"data": [{"id": "llama3:8b"}, {"id": "qwen2.5:7b"}]}),
        1234: (200, {"data": []}),
        8767: (200, {"data": [{"id": "Ling"}]}),
    })))
    by_port = {e.port: e for e in found}
    assert set(by_port) == {11434, 1234, 8767}
    assert by_port[11434].profile.id == "ollama" and by_port[11434].models == ["llama3:8b", "qwen2.5:7b"]
    assert by_port[8767].profile.id == "custom"


def test_detection_ignores_errors_garbage_and_oversize():
    found = asyncio.run(detect_external_engines(transport=_transport({
        11434: (500, {"error": "x"}),
        1234: (200, b"<html>not json</html>"),
        8080: (200, b"x" * (300 * 1024)),
        8767: (200, {"data": "nope"}),
    })))
    assert [e.port for e in found if e.port != 8767] == []
    assert next(e for e in found if e.port == 8767).models == []


def test_detection_never_probes_wisp_or_omlx_ports_or_other_hosts():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.port))
        raise httpx.ConnectError("refused")
    asyncio.run(detect_external_engines(transport=httpx.MockTransport(handler)))
    assert seen and all(host == "127.0.0.1" for host, _ in seen)
    assert not {8000, 8765} & {port for _, port in seen}


class _Drip(httpx.AsyncByteStream):
    """Answers 200 and then sends one byte at a time, forever. httpx read timeouts
    are per chunk, so this never trips them."""
    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.02)
            yield b" "

    async def aclose(self):
        pass


def _stalling_transport():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 1234:
            return httpx.Response(200, stream=_Drip())
        if request.url.port == 8080:
            await asyncio.sleep(30)                   # never answers at all
        if request.url.port == 11434:
            return httpx.Response(200, json={"data": [{"id": "llama3:8b"}]})
        raise httpx.ConnectError("refused")
    return httpx.MockTransport(handler)


def test_an_endlessly_streaming_or_silent_app_cannot_stall_detection():
    started = time.monotonic()
    found = asyncio.run(detect_external_engines(timeout=10, deadline=0.3, transport=_stalling_transport()))
    assert time.monotonic() - started < 2.0
    assert [e.port for e in found] == [11434]        # the healthy app is still found


def test_default_deadline_is_a_short_wall_clock_bound():
    from service.setup import engines
    assert 0 < engines._PROBE_DEADLINE <= 2


def test_status_route_still_loads_when_a_provider_drips(monkeypatch):
    import service.main as main

    async def detect():
        return await detect_external_engines(timeout=10, deadline=0.3, transport=_stalling_transport())
    monkeypatch.setattr(main, "client", _FakeClient([Q6]), raising=False)
    monkeypatch.setattr(main, "detect_external_engines", detect)
    monkeypatch.setattr(main, "_omlx_installed", lambda: True)
    monkeypatch.setattr(main, "_omlx_model_dir", lambda: "/m")
    monkeypatch.setattr(main, "current_hardware", lambda: Hardware("Apple M5 Pro", 24))
    monkeypatch.setattr(main, "_setup_roles", lambda: _roles(Q6))
    started = time.monotonic()
    s = asyncio.run(main.setup_status())
    assert time.monotonic() - started < 2.0
    assert _check(s, "engine")["state"] == "ok" and s["ready"] is True    # rest of the checklist loads
    assert {e["id"] for e in s["engines"] if e["running"]} >= {"omlx", "ollama"}
    assert next(e for e in s["engines"] if e["id"] == "lmstudio")["running"] is False


def test_external_app_disclosure_does_not_claim_more_than_loopback_proves():
    s = _status(installed=[Q6], roles=_roles(Q6))
    text = s["disclosure"]
    assert text == guide.EXTERNAL_ENGINE_DISCLOSURE
    assert "not identity-verified" in text and "connect only one you trust" in text
    assert "Reasoning prompts are sent to the app you connect" in text
    assert "leaves this Mac" not in text and "stay on this Mac" not in text


def test_the_guide_shows_the_disclosure_and_never_the_old_claim():
    app = Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp"
    view = (app / "SetupGuideView.swift").read_text()
    assert "Nothing leaves this Mac" not in view and "leaves this Mac" not in view
    assert "Text(s.disclosure)" in view                  # rendered from the backend text
    assert "let disclosure: String" in view
    # Settings' own panel carries the same identity warning, so the two cannot drift apart.
    settings = (app / "SettingsView.swift").read_text()
    assert "not identity-verified; connect only one you trust" in settings
    assert "not identity-verified; connect only one you trust" in guide.EXTERNAL_ENGINE_DISCLOSURE


def test_detection_caps_model_ids_and_drops_malformed_rows():
    rows = [{"id": f"m{i}"} for i in range(500)] + [{"id": 3}, {"name": "x"}, "bad", {"id": ""}]
    found = asyncio.run(detect_external_engines(transport=_transport({11434: (200, {"data": rows})})))
    assert len(found[0].models) == 200


# --- routes -----------------------------------------------------------------

class _FakeClient:
    def __init__(self, models=None, fail=False):
        self._models, self._fail = models or [], fail

    async def models(self):
        if self._fail:
            raise httpx.ConnectError("down")
        return list(self._models)


def test_apply_sets_every_text_role_and_rejects_bad_models(monkeypatch):
    import service.main as main

    calls, kept = [], []
    monkeypatch.setattr(main, "client", _FakeClient([Q6, Q4, EMBED]), raising=False)
    monkeypatch.setattr(main, "set_roles", lambda roles: calls.extend(roles.items()))
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: kept.append(True))

    async def status():
        return {"ok": True}
    monkeypatch.setattr(main, "setup_status", status)

    assert asyncio.run(main.setup_apply({"model": f" {Q6} "})) == {"ok": True}
    assert calls == [(r, Q6) for r in guide.TEXT_ROLES] and kept == [True]

    for bad in ({"model": "not-installed"}, {"model": EMBED}, {"model": ""}, {"model": 5}, {}):
        with pytest.raises(HTTPException) as e:
            asyncio.run(main.setup_apply(bad))
        assert e.value.status_code == 400
    assert calls == [(r, Q6) for r in guide.TEXT_ROLES]      # nothing else was written


def test_apply_reports_a_stopped_engine_plainly(monkeypatch):
    import service.main as main
    monkeypatch.setattr(main, "client", _FakeClient(fail=True), raising=False)
    monkeypatch.setattr(main, "set_roles", lambda *a: pytest.fail("must not write"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.setup_apply({"model": Q6}))
    assert e.value.status_code == 503 and "isn't running" in e.value.detail


@pytest.fixture
def automatic_apply_state(monkeypatch, tmp_path):
    from service import config, main
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "config.yaml")
    config.models_config.cache_clear()
    config.set_roles({r: "missing-fixture-model" for r in (*guide.TEXT_ROLES, "router", "agent")})
    monkeypatch.setattr(main, "set_roles", config.set_roles)
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)
    async def status():
        return {"ok": True}
    monkeypatch.setattr(main, "setup_status", status)
    yield config, main
    config.models_config.cache_clear()


@pytest.mark.parametrize("new_choice", ["external", "working_local"])
def test_automatic_apply_rechecks_after_actual_async_model_lookup(automatic_apply_state, monkeypatch, new_choice):
    config, main = automatic_apply_state
    expected = config.setup_configuration_token()
    async def interleave():
        started, release = asyncio.Event(), asyncio.Event()
        class DelayedClient:
            async def models(self):
                started.set()
                await release.wait()
                return [Q6, Q4]
        monkeypatch.setattr(main, "client", DelayedClient(), raising=False)
        pending = asyncio.create_task(main.setup_apply({"model": Q6, "automatic": True,
                                                        "expected_configuration": expected}))
        await started.wait()
        # A real configuration writer runs during the endpoint's suspended lookup.
        if new_choice == "external":
            config._save_overlay({"inference": {"bindings": {"reasoning": {
                "endpoint": "local_provider", "model_id": "chosen-fixture-model"}}}})
        else:
            config.set_roles({"reasoning": Q4})
        chosen = copy.deepcopy(config.models_config())
        release.set()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 409
        assert config.models_config() == chosen
        # Manual Apply is still an explicit override of the current choice.
        assert await main.setup_apply({"model": Q6}) == {"ok": True}
        assert all(config.models_config()["roles"][r] == Q6 for r in guide.TEXT_ROLES)
    asyncio.run(interleave())


def test_automatic_apply_condition_runs_inside_atomic_overlay_write(automatic_apply_state, monkeypatch):
    config, main = automatic_apply_state
    expected = config.setup_configuration_token()
    writer = config._write_overlay
    def changed_at_persist(update, **kwargs):
        if kwargs.get("precondition") is not None:
            writer({"inference": {"bindings": {"reasoning": {
                "endpoint": "local_provider", "model_id": "late-fixture-choice"}}}})
        return writer(update, **kwargs)
    monkeypatch.setattr(config, "_write_overlay", changed_at_persist)
    monkeypatch.setattr(main, "client", _FakeClient([Q6]), raising=False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.setup_apply({"model": Q6, "automatic": True, "expected_configuration": expected}))
    assert error.value.status_code == 409
    assert config.models_config()["inference"]["bindings"]["reasoning"]["model_id"] == "late-fixture-choice"


@pytest.mark.parametrize("existing", [None, "external", "working_local"])
def test_automatic_apply_requires_unconfigured_roles_even_with_current_token(automatic_apply_state, monkeypatch, existing):
    config, main = automatic_apply_state
    if existing == "external":
        config._save_overlay({"inference": {"bindings": {"agent": {
            "endpoint": "local_provider", "model_id": "fixture-choice"}}}})
    elif existing == "working_local":
        config.set_roles({"reasoning": Q4})
    monkeypatch.setattr(main, "client", _FakeClient([Q6, Q4]), raising=False)
    before = copy.deepcopy(config.models_config())
    body = {"model": Q6, "automatic": True, "expected_configuration": config.setup_configuration_token()}
    if existing:
        with pytest.raises(HTTPException) as error:
            asyncio.run(main.setup_apply(body))
        assert error.value.status_code == 409
        assert config.models_config() == before
    else:
        assert asyncio.run(main.setup_apply(body)) == {"ok": True}
        assert all(config.models_config()["roles"][r] == Q6 for r in guide.TEXT_ROLES)


def test_automatic_apply_cannot_omit_expected_configuration(automatic_apply_state, monkeypatch):
    _, main = automatic_apply_state
    monkeypatch.setattr(main, "client", _FakeClient([Q6]), raising=False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.setup_apply({"model": Q6, "automatic": True}))
    assert error.value.status_code == 400


def test_start_engine_maps_failure_to_a_next_step(monkeypatch):
    import service.main as main
    from service.inference.omlx_client import ModelLoadError

    async def fail():
        raise ModelLoadError("The local oMLX CLI is unavailable")
    monkeypatch.setattr(main, "ensure_omlx", fail)
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.setup_start_engine())
    assert e.value.status_code == 503 and "Open the oMLX app" in e.value.detail


def test_status_route_falls_back_to_saved_models_when_engine_is_down(monkeypatch):
    import service.main as main

    async def none():
        return []
    monkeypatch.setattr(main, "client", _FakeClient(fail=True), raising=False)
    monkeypatch.setattr(main, "detect_external_engines", none)
    monkeypatch.setattr(main, "_omlx_installed", lambda: True)
    monkeypatch.setattr(main, "_omlx_model_dir", lambda: "/models")
    monkeypatch.setattr(main, "current_hardware", lambda: Hardware("Apple M4", 16))
    monkeypatch.setattr(main, "models_config", lambda: {"installed_models": [Q4]})
    monkeypatch.setattr(main, "_setup_roles", lambda: _roles(Q4))
    s = asyncio.run(main.setup_status())
    assert s["omlx"]["running"] is False and s["models"]["source"] == "saved"
    assert s["hardware"]["tier"] == "standard" and s["models"]["recommended"] == GEMMA
    assert _check(s, "engine")["action"] == "start_engine"


# Extract only the actual functions under test: importing service.main starts
# unrelated application machinery. All managed HTTP/loading/activity below is
# synthetic; normalization, metadata validation and the route are production code.
def _functions(path, names, namespace, *, methods=False):
    tree = ast.parse(path.read_text())
    nodes = (next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == "OMLXClient").body if methods else tree.body)
    selected = [copy.deepcopy(n) for n in nodes
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in selected} == set(names)
    for node in selected:
        node.decorator_list = []
    import __future__
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)


def _chat_pipeline(raw):
    root = Path(__file__).resolve().parents[1]
    events = []

    class IncompleteStreamError(RuntimeError):
        pass

    ns = {"Any": Any, "json": json, "httpx": httpx, "IncompleteStreamError": IncompleteStreamError,
          "engine_epoch": SimpleNamespace(begin=lambda: events.append("epoch"), end=lambda: None),
          "idle": SimpleNamespace(begin=lambda *a: None, end=lambda *a: None)}
    path = root / "service/inference/omlx_client.py"
    _functions(path, {"_ensure_choices", "_demote_unclosed_think"}, ns)
    _functions(path, {"chat", "_completion_data", "_check_response"}, ns, methods=True)

    class Managed:
        managed = True
        api_prefix = "/v1"
        provider = SimpleNamespace(name="omlx")
        chat = ns["chat"]
        _completion_data = staticmethod(ns["_completion_data"])
        _check_response = ns["_check_response"]

        def __init__(self):
            self._client = self

        def _fit_request(self, *args):
            return args

        def _payload(self, *args, **kwargs):
            return {}

        def activity_key(self, model):
            return model

        async def ensure_only(self, model):
            events.append("load")

        async def post(self, path, **kwargs):
            events.append("http")
            return httpx.Response(200, json=copy.deepcopy(raw), request=httpx.Request("POST", "http://synthetic.invalid" + path))

    def target(role):
        events.append("target")
        return SimpleNamespace(role=role, model="Synthetic",
                               endpoint=SimpleNamespace(name="local", managed=True))

    async def ensure():
        events.append("ensure")

    route = {"Any": Any, "HTTPException": HTTPException, "StreamingResponse": StreamingResponse, "setup_guide": guide,
             "ModelLoadError": ModelLoadError,
             "role_target": target, "remote_only_profile": lambda: None,
             "ensure_omlx": ensure, "client": Managed()}
    _functions(root / "service/main.py", {"chat"}, route)
    return route["chat"], events, IncompleteStreamError


def _completion(text="OK", *, finish="stop", role="assistant", **extra):
    return {"choices": [{"message": {"role": role, "content": text, **extra}, "finish_reason": finish}]}


COMPLETION_CASES = [
    ("complete_visible", _completion(), True),
    ("closed_reasoning_and_answer", _completion("<think>secret</think> OK"), True),
    ("uppercase_closed_and_answer", _completion("<THINK>secret</THINK> OK"), True),
    ("separate_reasoning_and_answer", _completion(reasoning_content="secret"), True),
    ("closed_reasoning_only", _completion("<think>secret</think>"), False),
    ("uppercase_reasoning_only", _completion("<THINK>secret</THINK>"), False),
    ("orphan_reasoning_close", _completion("secret</think>"), False),
    ("length_with_separate_reasoning", _completion(finish="length", reasoning_content="secret"), False),
    ("finish_error_with_text", _completion(finish="error"), False),
    ("wrong_message_role", _completion(role="user"), False),
    ("unfinished_lowercase", _completion("OK<think>unfinished"), False),
    ("nested_reasoning", _completion("<think>outer<think>inner</think></think> OK"), False),
    ("empty_answer", _completion("  "), False),
    ("error_envelope", {**_completion(), "error": {"message": "synthetic failure"}}, False),
    ("missing_choices", {}, False),
]


def _run_setup_pipeline(raw):
    chat, events, incomplete = _chat_pipeline(raw)
    try:
        body = asyncio.run(chat({"role": "general", "prompt": "Reply with the single word OK.",
                                 "max_tokens": 512, "setup_test": True}))
        return 200, body, events
    except HTTPException as error:
        return error.status_code, {"detail": error.detail}, events
    except incomplete:
        return 503, {"detail": "Synthetic provider failure"}, events


@pytest.mark.parametrize("name,raw,valid", COMPLETION_CASES, ids=[c[0] for c in COMPLETION_CASES])
def test_setup_certificate_uses_actual_managed_completion_and_route(name, raw, valid):
    status, body, events = _run_setup_pipeline(raw)
    assert "http" in events
    assert (status == 200) == valid
    if valid:
        assert body["setup_test"] == "complete_visible_assistant_v1"
        assert body["content"] == "OK"
    else:
        assert status in (502, 503)
        assert "setup_test" not in body


@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}])
def test_setup_flag_is_strictly_boolean_before_any_effect(flag):
    chat, events, _ = _chat_pipeline(_completion())
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat({"setup_test": flag, "role": "general", "prompt": "OK"}))
    assert error.value.status_code == 422 and events == []


def test_streaming_setup_test_is_rejected_before_any_effect():
    chat, events, _ = _chat_pipeline(_completion())
    with pytest.raises(HTTPException) as error:
        asyncio.run(chat({"setup_test": True, "stream": True, "role": "general", "prompt": "OK"}))
    assert error.value.status_code == 422 and events == []


@pytest.mark.parametrize("flag", [{}, {"setup_test": False}])
def test_ordinary_chat_keeps_its_existing_response_contract(flag):
    chat, _, _ = _chat_pipeline(_completion("ordinary", finish="length", reasoning_content="secret"))
    body = asyncio.run(chat({"role": "general", "prompt": "OK", **flag}))
    assert body == {"model": "Synthetic", "content": "ordinary", "usage": None}


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Requires macOS Swift compiler")
def test_actual_completion_route_to_native_setup_certificate(tmp_path):
    cases = []
    for name, raw, valid in COMPLETION_CASES:
        status, body, _ = _run_setup_pipeline(raw)
        cases.append({"name": name, "status": status, "body": body, "expectedWorks": valid})
    payload = tmp_path / "pipeline.json"
    payload.write_text(json.dumps(cases))
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "SetupGuideChecks"
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path),
                   "WISP_HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    command = ["swiftc", "-swift-version", "5", "-module-cache-path", str(tmp_path / "module-cache"),
               str(root / "tests/SetupGuideChecks.swift"), str(root / "app/Sources/WispApp/SetupGuideView.swift"),
               str(root / "app/Sources/WispApp/Theme.swift"), "-o", str(output)]
    compiled = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)
    assert compiled.returncode == 0, compiled.stderr[-5000:]
    checked = subprocess.run([str(output), str(payload)], capture_output=True, text=True, env=environment, timeout=45)
    assert checked.returncode == 0, checked.stderr[-7000:]
    assert "setup model-state checks passed; all HTTP intercepted" in checked.stdout
    print(checked.stdout.strip())


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"server": "x", "auth": null}'])
def test_missing_or_broken_omlx_settings_never_stops_the_service(tmp_path, monkeypatch, content):
    # A fresh Mac has no ~/.omlx/settings.json (oMLX never opened, or not installed).
    # The backend builds its inference client at startup, so this must not raise.
    import service.config as config
    settings = tmp_path / "settings.json"
    if content is not None:
        settings.write_text(content)
    monkeypatch.setattr(config, "OMLX_SETTINGS", settings)
    monkeypatch.setattr(config, "is_bundled", lambda: False)
    monkeypatch.setattr(config, "_credential", lambda name: "")
    monkeypatch.setitem(config.models_config()["omlx"], "base_url", None)
    try:
        assert config.omlx_api_key() == ""
        assert config.omlx_base_url() == "http://127.0.0.1:8000"
    finally:
        config.models_config.cache_clear()


def test_omlx_settings_written_after_launch_are_picked_up(tmp_path, monkeypatch):
    # Wisp can start before oMLX.app is ever opened; once oMLX writes its
    # settings (key, port), they must apply without restarting the backend.
    import os
    import service.config as config
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config, "OMLX_SETTINGS", settings)
    assert config.omlx_settings() == {}
    settings.write_text('{"auth": {"api_key": "first"}}')
    assert config.omlx_settings() == {"auth": {"api_key": "first"}}
    settings.write_text('{"auth": {"api_key": "second-key"}}')
    os.utime(settings, ns=(1, 10**18))
    assert config.omlx_settings()["auth"]["api_key"] == "second-key"


def test_status_says_when_a_remote_provider_answers(monkeypatch):
    # The app keeps a "not set up" cue up while ready is false; a remote-only Wisp
    # answers without a local model, so it must be able to tell.
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    assert _status()["remote_only"] is False
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    assert _status()["remote_only"] is True

    def invalid():
        from service.config.endpoints import EndpointConfigurationError
        raise EndpointConfigurationError("bad profile")
    monkeypatch.setattr(remote_policy, "remote_only_profile", invalid)
    assert _status()["remote_only"] is False
