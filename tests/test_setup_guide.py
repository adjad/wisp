"""Synthetic checks for the guided inference setup. No network, no real Wisp state."""
import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from service.setup import catalog, guide
from service.setup.engines import EngineProfile, EngineState, detect_external_engines
from service.setup.hardware import Hardware, current_hardware, detect_hardware

Q6, Q5, Q4 = "Ling-3.0-tiny-oQ6e", "Ling-3.0-tiny-oQ5e", "Ling-3.0-tiny-oQ4e"
EMBED = "Qwen3-Embedding-0.6B-4bit-DWQ"


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

@pytest.mark.parametrize("ram,first", [(64, Q6), (24, Q6), (16, Q5), (12, Q4), (8, Q4), (0, Q4)])
def test_first_choice_follows_memory(ram, first):
    assert catalog.recommend(ram, [])["first_choice"] == first


def test_16gb_is_offered_a_download_it_can_actually_get():
    # oQ5e is the ideal 16 GB build but is not published, so offer oQ4e.
    assert catalog.recommend(16, [])["get"] == Q4
    assert catalog.by_id(Q5).repo is None
    assert catalog.recommend(16, [Q5])["get"] is None


def test_24gb_with_only_oq4e_is_offered_the_upgrade():
    rec = catalog.recommend(24, [Q4])
    assert rec["use"] == Q4 and rec["get"] == Q6


def test_best_installed_model_wins_and_nothing_is_offered_when_optimal():
    rec = catalog.recommend(24, [Q4, Q6])
    assert rec["use"] == Q6 and rec["get"] is None


def test_heavy_model_on_16gb_is_flagged_but_kept():
    assert catalog.fit(catalog.by_id(Q6), 16) == "heavy"
    assert catalog.fit(catalog.by_id(Q6), 24) == "recommended"
    assert catalog.fit(catalog.by_id(Q4), 24) == "good"
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
    assert "macOS 15" in _check(s, "engine")["detail"]
    assert s["omlx"]["min_macos"] == 15


def test_installed_but_stopped_engine_offers_start():
    s = _status(installed=(), running=False, is_installed=True)
    assert _check(s, "engine")["state"] == "warn" and _check(s, "engine")["action"] == "start_engine"


def test_running_with_no_chat_model_points_at_download():
    s = _status(installed=[EMBED], running=True)
    assert _check(s, "model")["action"] == "download_model"
    assert s["models"]["download"]["model"] == Q6
    assert s["ready"] is False


def test_roles_pointing_at_an_uninstalled_model_are_caught():
    # The default roster names oQ4e; the user only has oQ6e.
    s = _status(installed=[Q6], roles=_roles(Q4))
    assert _check(s, "model")["state"] == "todo" and _check(s, "model")["action"] == "apply_model"
    assert Q4 in _check(s, "model")["detail"]
    assert s["ready"] is False


def test_ready_when_engine_runs_and_roles_resolve():
    s = _status(installed=[Q6, EMBED], roles=_roles(Q6))
    assert s["ready"] is True
    assert _check(s, "search")["state"] == "ok"


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
    s = _status(ram=24, installed=["zzz-custom", Q4, "Qwen3-Reranker-0.6B-mlx-6bit", EMBED, Q6])
    ids = [m["id"] for m in s["models"]["installed"]]
    assert ids == [Q6, Q4, "zzz-custom"]
    by_id = {m["id"]: m for m in s["models"]["installed"]}
    assert by_id[Q6]["fit"] == "recommended" and by_id[Q6]["tested"] is True
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
    assert catalog.fit(catalog.by_id(Q4), 0) == "recommended"
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
    assert s["hardware"]["tier"] == "standard" and s["models"]["recommended"] == Q5
    assert _check(s, "engine")["action"] == "start_engine"
