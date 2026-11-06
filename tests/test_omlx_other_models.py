"""Other models on the user's oMLX.app: a turn never unloads there, the idle timer only
releases a model Wisp itself used, and the notch's X leaves a model another app is using.
The bundled engine behaves exactly as before. Fake clients and httpx.MockTransport only;
no engine is contacted."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from service import idle, idle_unloader
from service import memory_saver as M
from service.inference.inference_errors import ModelMemoryLimitError

GEMMA = "gemma-4-E4B-it-r4-step-01840-oQ4e-mtp"
QWEN = "Qwen3.6-27B-oQ3.5e"
EMBED = "bge-embedder"
FOREIGN = "someone-elses-model"
CHAT = "chat-model"            # a keep-warm model that is not a Gemma: Save memory leaves it be
MIN = 60.0
WALL = 1_000_000.0
KINDS = {GEMMA: "gemma4", QWEN: "qwen3_5", EMBED: "bert", FOREIGN: "llama"}


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "SETTINGS_PATH", tmp_path / "memory_saver" / "settings.json")
    monkeypatch.setattr(M, "_state", M._State(started=0.0))
    monkeypatch.setattr(M, "_prewarm_task", None)
    monkeypatch.setattr(M, "_holds", 0)
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_turn", None)
    monkeypatch.setattr(idle, "_in_flight", {})
    monkeypatch.setattr(idle, "_last_used", {})
    monkeypatch.setattr(idle, "_own_access", {})
    monkeypatch.setattr(idle, "_own_loads", {})
    monkeypatch.setattr(idle, "_idle_minutes", 20.0)
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: CHAT)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)


@pytest.fixture
def on_omlx(monkeypatch):
    """Wisp on the user's oMLX.app."""
    import service.config
    import service.config.endpoints
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: False)
    target = SimpleNamespace(endpoint=SimpleNamespace(managed=True, provider="omlx"), model=GEMMA)
    monkeypatch.setattr(service.config.endpoints, "role_target", lambda role: target)
    M._state.started_wall = WALL - 7200       # long before every stamp used below
    idle._last_turn = 0.0


# --- fakes ------------------------------------------------------------------------

class Rows:
    """oMLX.app's model table; a client's status() and a MockTransport share it."""

    def __init__(self, loaded=()):
        self.loaded = set(loaded)
        self.loading = set()
        self.pinned = set()
        self.access = {}                    # model -> last_access
        self.active = 0
        self.waiting = 0

    def rows(self):
        out = []
        for model, kind in KINDS.items():
            out.append({"id": model, "config_model_type": kind, "is_helper": False,
                        "loaded": model in self.loaded, "is_loading": model in self.loading,
                        "pinned": model in self.pinned,
                        "last_access": self.access.get(model, WALL - 3600) if model in self.loaded else None})
        return out


class FakeClient(Rows):
    """What idle_unloader and the notch route need. `bundled` gives the bundled port."""
    managed = True

    def __init__(self, loaded=(), bundled=False, keep=(CHAT,)):
        super().__init__(loaded)
        from service.engine import BUNDLED_PORT
        from service.inference.providers import provider
        self.provider = provider("omlx")
        self.base_url = f"http://127.0.0.1:{BUNDLED_PORT if bundled else 8000}"
        self.keep = set(keep)
        self.calls = []
        self.activity_fails = False

    def keep_warm(self):
        return set(self.keep)

    async def status(self):
        return {"models": self.rows()}

    async def engine_activity(self):
        if self.activity_fails:
            raise OSError("no /api/status")
        return {"active_requests": self.active, "waiting_requests": self.waiting}

    async def loaded_models(self):
        return sorted(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        self.loaded.discard(model)

    async def load(self, model):
        self.calls.append(("load", model))
        self.loaded.add(model)

    @property
    def unloads(self):
        return [m for op, m in self.calls if op == "unload"]


class Engine(Rows):
    """oMLX's HTTP routes behind a MockTransport. Every POST is recorded."""

    def __init__(self, loaded=(), load_status=200):
        super().__init__(loaded)
        self.posts = []
        self.load_status = load_status

    def __call__(self, request):
        path = request.url.path
        if request.method == "GET":
            if path == "/v1/models/status":
                return httpx.Response(200, json={"models": self.rows()})
            if path == "/api/status":
                return httpx.Response(200, json={"status": "ok", "active_requests": self.active,
                                                 "waiting_requests": self.waiting})
            return httpx.Response(404)
        self.posts.append(path)
        model, verb = path.split("/")[3:5]
        if verb == "load":
            if self.load_status != 200:
                return httpx.Response(self.load_status, json={"error": "no room"})
            self.loaded.add(model)
        elif verb == "unload":
            self.loaded.discard(model)
        return httpx.Response(200, json={"status": "ok"})

    @property
    def unloads(self):
        return [p for p in self.posts if p.endswith("/unload")]

    @property
    def loads(self):
        return [p for p in self.posts if p.endswith("/load")]


def _real_client(handler, bundled=False, keep=()):
    from service.engine import BUNDLED_PORT
    from service.inference.omlx_client import OMLXClient
    port = BUNDLED_PORT if bundled else 8000
    c = OMLXClient(base_url=f"http://127.0.0.1:{port}", api_key="test")
    c._client = httpx.AsyncClient(base_url=c.base_url, transport=httpx.MockTransport(handler))
    c.set_keep_warm(set(keep))
    return c


def _load(model):
    return f"/v1/models/{model}/load"


def _unload(model):
    return f"/v1/models/{model}/unload"


# --- the new idle API ---------------------------------------------------------------

def test_own_request_stamps_like_own_load():
    assert idle.own_access(GEMMA) is None
    with idle.own_request(GEMMA):
        assert idle.own_access_active(GEMMA)
        assert idle.own_access(GEMMA) is not None
    assert not idle.own_access_active(GEMMA)
    assert time.time() - idle.own_access(GEMMA) < 5


# --- a turn on oMLX.app never unloads ---------------------------------------------

def test_a_turn_unloads_nothing_for_a_foreign_model(on_omlx):
    engine = Engine(loaded={FOREIGN})
    c = _real_client(engine)
    asyncio.run(c._ensure_managed(GEMMA))
    assert engine.unloads == []
    assert engine.loads == [_load(GEMMA)]


def test_a_turn_unloads_nothing_for_a_model_wisp_used_earlier(on_omlx):
    engine = Engine(loaded={QWEN})
    idle._own_access[QWEN] = WALL - 3600
    idle._last_used[QWEN] = 0.0
    c = _real_client(engine)
    asyncio.run(c._ensure_managed(GEMMA))
    assert engine.unloads == [] and engine.loads == [_load(GEMMA)]


def test_an_exclusive_turn_unloads_nothing(on_omlx):
    engine = Engine(loaded={GEMMA, FOREIGN})
    c = _real_client(engine, keep={GEMMA})
    asyncio.run(c._ensure_managed(QWEN, exclusive=True))
    assert engine.unloads == []
    assert engine.loads == [_load(QWEN)]


def test_a_turn_with_the_target_already_resident_posts_nothing(on_omlx):
    engine = Engine(loaded={GEMMA, FOREIGN})
    c = _real_client(engine, keep={GEMMA})
    asyncio.run(c._ensure_managed(GEMMA))
    assert engine.posts == []


# --- the keep-warm reload ------------------------------------------------------------

def test_a_missing_keep_warm_model_is_not_reloaded_beside_another_model(on_omlx):
    engine = Engine(loaded={FOREIGN})
    c = _real_client(engine, keep={GEMMA})
    asyncio.run(c._ensure_managed(QWEN))
    assert engine.loads == [_load(QWEN)]         # a load of Gemma could evict FOREIGN
    assert engine.unloads == []


def test_a_missing_keep_warm_model_is_reloaded_when_nothing_else_is_resident(on_omlx):
    engine = Engine()
    c = _real_client(engine, keep={GEMMA})
    asyncio.run(c._ensure_managed(QWEN))
    assert engine.loads == [_load(QWEN), _load(GEMMA)]
    assert engine.unloads == []


# --- 507 -------------------------------------------------------------------------------

def test_a_507_beside_another_model_names_oMLX_and_other_models(on_omlx):
    engine = Engine(loaded={FOREIGN}, load_status=507)
    c = _real_client(engine)
    with pytest.raises(ModelMemoryLimitError) as err:
        asyncio.run(c._ensure_managed(QWEN))
    assert err.value.reason == "model_memory_limit"
    text = str(err.value)
    assert "oMLX" in text and "other models" in text
    assert text != str(ModelMemoryLimitError())
    assert engine.unloads == []


def test_a_507_with_nothing_else_resident_keeps_the_original_message(on_omlx):
    engine = Engine(load_status=507)
    c = _real_client(engine)
    with pytest.raises(ModelMemoryLimitError) as err:
        asyncio.run(c._ensure_managed(QWEN))
    assert str(err.value) == str(ModelMemoryLimitError())
    assert err.value.reason == "model_memory_limit"


def test_the_memory_limit_error_with_no_args_keeps_its_text():
    text = str(ModelMemoryLimitError())
    assert "Quit some apps" in text and "Settings" in text
    assert ModelMemoryLimitError.reason == "model_memory_limit"


# --- the bundled engine is unchanged ------------------------------------------------

def test_a_bundled_turn_still_unloads_everything_outside_the_keep_set():
    engine = Engine(loaded={FOREIGN, QWEN, GEMMA})
    c = _real_client(engine, bundled=True, keep={GEMMA})
    assert M.omlx_app(c) is False
    asyncio.run(c._ensure_managed(EMBED))
    assert sorted(engine.unloads) == sorted([_unload(FOREIGN), _unload(QWEN)])
    assert engine.loads == [_load(EMBED)]


def test_a_bundled_exclusive_turn_still_unloads_the_keep_warm_model():
    engine = Engine(loaded={GEMMA})
    c = _real_client(engine, bundled=True, keep={GEMMA})
    asyncio.run(c._ensure_managed(QWEN, exclusive=True))
    assert engine.unloads == [_unload(GEMMA)]


# --- note_omlx_rows / wisp_may_release / engine_idle ----------------------------------

def _row(model, access=WALL - 3600, loaded=True, loading=False, pinned=False):
    return {"id": model, "loaded": loaded, "is_loading": loading, "pinned": pinned,
            "last_access": access}


def test_a_resident_model_with_no_own_access_is_not_ours(on_omlx):
    M.note_omlx_rows([_row(FOREIGN)])
    assert FOREIGN in M._state.not_ours
    assert M.wisp_may_release(_row(FOREIGN)) is False


def test_a_loading_model_with_no_own_access_is_not_ours(on_omlx):
    M.note_omlx_rows([_row(FOREIGN, access=None, loaded=False, loading=True)])
    assert FOREIGN in M._state.not_ours


def test_a_model_wisp_used_is_ours_and_releasable(on_omlx):
    idle._own_access[EMBED] = WALL
    row = _row(EMBED, access=WALL + 1)                    # oMLX stamps a moment later
    M.note_omlx_rows([row])
    assert EMBED not in M._state.not_ours
    assert M.wisp_may_release(row) is True


def test_a_stamp_later_than_wisps_by_more_than_the_slop_is_not_ours(on_omlx):
    idle._own_access[EMBED] = WALL
    M.note_omlx_rows([_row(EMBED, access=WALL + M.OWN_ACCESS_SLOP_S + 1)])
    assert EMBED in M._state.not_ours
    assert M.wisp_may_release(_row(EMBED, access=WALL + M.OWN_ACCESS_SLOP_S + 1)) is False


def test_a_model_wisp_is_mid_request_on_is_never_added(on_omlx):
    with idle.own_request(EMBED):
        M.note_omlx_rows([_row(EMBED, access=WALL)])
    assert EMBED not in M._state.not_ours


def test_not_ours_sticks_until_the_model_leaves(on_omlx):
    M.note_omlx_rows([_row(FOREIGN)])
    idle._own_access[FOREIGN] = WALL                      # Wisp uses it afterwards
    M.note_omlx_rows([_row(FOREIGN, access=WALL)])
    assert FOREIGN in M._state.not_ours
    assert M.wisp_may_release(_row(FOREIGN, access=WALL)) is False
    M.note_omlx_rows([_row(FOREIGN, access=None, loaded=False)])
    assert FOREIGN not in M._state.not_ours


def test_a_pinned_model_is_not_releasable(on_omlx):
    idle._own_access[EMBED] = WALL
    assert M.wisp_may_release(_row(EMBED, access=WALL, pinned=True)) is False


def test_a_model_without_own_access_is_not_releasable(on_omlx):
    assert M.wisp_may_release(_row(EMBED)) is False


@pytest.mark.parametrize("counts,expected", [
    ({"active_requests": 0, "waiting_requests": 0}, True),
    ({"active_requests": 1, "waiting_requests": 0}, False),
    ({"active_requests": 0, "waiting_requests": 2}, False),
    ({"active_requests": 0}, False),
    ({"active_requests": "0", "waiting_requests": 0}, False),
    ({"active_requests": False, "waiting_requests": 0}, False),
    ({"active_requests": 0.0, "waiting_requests": 0}, False),
])
def test_engine_idle_wants_real_zero_ints(on_omlx, counts, expected):
    class C:
        async def engine_activity(self):
            return counts
    assert asyncio.run(M.engine_idle(C())) is expected


def test_engine_idle_is_false_when_the_read_fails(on_omlx):
    c = FakeClient()
    c.activity_fails = True
    assert asyncio.run(M.engine_idle(c)) is False


# --- the idle timer --------------------------------------------------------------------

class _Stop(Exception):
    pass


def passes(client, n, monkeypatch):
    """Run idle_unloader.run for n polling passes."""
    left = [n]

    async def fake_sleep(_seconds):
        if left[0] <= 0:
            raise _Stop
        left[0] -= 1
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(idle_unloader.run(client, poll_seconds=0))


def _wisp_used(model, minutes_ago=25):
    """Wisp used `model` this run, `minutes_ago` minutes back; nothing since."""
    idle._last_used[model] = time.monotonic() - minutes_ago * MIN
    idle._own_access[model] = time.time() - minutes_ago * MIN


def test_omlx_never_unloads_or_clocks_a_foreign_model(on_omlx, monkeypatch):
    c = FakeClient(loaded={FOREIGN})
    passes(c, 3, monkeypatch)
    assert c.unloads == []
    assert idle.last_used(FOREIGN) is None


def test_omlx_unloads_a_model_wisp_used_after_the_idle_minutes(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    _wisp_used(EMBED)
    c.access[EMBED] = idle.own_access(EMBED) + 1
    passes(c, 1, monkeypatch)
    assert c.unloads == [EMBED]


def test_omlx_keeps_a_model_wisp_used_recently(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    _wisp_used(EMBED, minutes_ago=2)
    c.access[EMBED] = idle.own_access(EMBED)
    passes(c, 1, monkeypatch)
    assert c.unloads == []


def test_omlx_keeps_a_model_another_app_used_since(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    _wisp_used(EMBED)
    c.access[EMBED] = idle.own_access(EMBED) + M.OWN_ACCESS_SLOP_S + 60
    passes(c, 2, monkeypatch)
    assert c.unloads == []
    assert EMBED in M._state.not_ours


def test_omlx_keeps_a_pinned_model(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    c.pinned.add(EMBED)
    _wisp_used(EMBED)
    c.access[EMBED] = idle.own_access(EMBED)
    passes(c, 1, monkeypatch)
    assert c.unloads == []


@pytest.mark.parametrize("field", ["active", "waiting"])
def test_omlx_unloads_nothing_while_oMLX_is_running_a_request(on_omlx, monkeypatch, field):
    c = FakeClient(loaded={EMBED})
    setattr(c, field, 1)
    _wisp_used(EMBED)
    c.access[EMBED] = idle.own_access(EMBED)
    passes(c, 1, monkeypatch)
    assert c.unloads == []


def test_omlx_unloads_nothing_when_activity_is_unreadable(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    c.activity_fails = True
    _wisp_used(EMBED)
    c.access[EMBED] = idle.own_access(EMBED)
    passes(c, 1, monkeypatch)
    assert c.unloads == []


def test_omlx_a_model_seen_before_wisps_first_use_stays_not_ours(on_omlx, monkeypatch):
    c = FakeClient(loaded={EMBED})
    passes(c, 1, monkeypatch)                              # resident, Wisp hasn't used it
    assert EMBED in M._state.not_ours
    _wisp_used(EMBED)                                      # Wisp uses it afterwards
    c.access[EMBED] = idle.own_access(EMBED)
    passes(c, 2, monkeypatch)
    assert c.unloads == []
    assert EMBED in M._state.not_ours


def test_omlx_never_unloads_the_keep_warm_chat_model_on_the_timer(on_omlx, monkeypatch):
    c = FakeClient(loaded={CHAT})
    _wisp_used(CHAT)
    passes(c, 2, monkeypatch)
    assert c.unloads == []


def test_bundled_still_clocks_on_first_sight_then_unloads(monkeypatch):
    c = FakeClient(loaded={FOREIGN}, bundled=True)
    passes(c, 1, monkeypatch)
    assert c.unloads == [] and idle.last_used(FOREIGN) is not None
    idle._last_used[FOREIGN] = time.monotonic() - 25 * MIN
    passes(c, 1, monkeypatch)
    assert c.unloads == [FOREIGN]
    assert idle.last_used(FOREIGN) is None


def test_bundled_still_unloads_while_requests_are_counted_elsewhere(monkeypatch):
    # The bundled engine never consults oMLX's activity counters or not_ours.
    c = FakeClient(loaded={FOREIGN}, bundled=True)
    c.active = 3
    idle._last_used[FOREIGN] = time.monotonic() - 25 * MIN
    passes(c, 1, monkeypatch)
    assert c.unloads == [FOREIGN]
    assert M._state.not_ours == set()


# --- the notch's X ---------------------------------------------------------------------

def hold(c, model=GEMMA, wall=WALL):
    return asyncio.run(M.omlx_x_hold(c, model, wall=wall))


def test_x_may_unload_a_quiet_gemma_nobody_else_used(on_omlx):
    assert hold(FakeClient(loaded={GEMMA})) is None


def test_x_holds_a_model_that_is_not_a_gemma(on_omlx):
    assert hold(FakeClient(loaded={QWEN}), QWEN) == M.NOT_GEMMA


def test_x_holds_a_pinned_gemma(on_omlx):
    c = FakeClient(loaded={GEMMA})
    c.pinned.add(GEMMA)
    assert hold(c) == M.PINNED


def test_x_holds_a_gemma_oMLX_does_not_describe(on_omlx):
    c = FakeClient(loaded=set())

    async def status():
        return {"models": []}
    c.status = status
    assert hold(c) == M.NOT_REPORTED


def test_x_holds_for_another_apps_recent_use(on_omlx):
    c = FakeClient(loaded={GEMMA})
    idle._own_access[GEMMA] = WALL - 3600
    c.access[GEMMA] = WALL - 60
    assert hold(c) == "other_app"


def test_x_ignores_another_apps_use_older_than_the_park_window(on_omlx):
    c = FakeClient(loaded={GEMMA})
    idle._own_access[GEMMA] = WALL - 4 * 3600
    c.access[GEMMA] = WALL - 2 * 3600
    assert hold(c) is None


def test_x_ignores_wisps_own_recent_use(on_omlx):
    c = FakeClient(loaded={GEMMA})
    idle._own_access[GEMMA] = WALL - 60
    c.access[GEMMA] = WALL - 59
    assert hold(c) is None


@pytest.mark.parametrize("field", ["active", "waiting"])
def test_x_is_busy_while_anything_runs(on_omlx, field):
    c = FakeClient(loaded={GEMMA})
    setattr(c, field, 1)
    assert hold(c) == "busy"


def test_x_is_busy_when_api_status_is_unreadable(on_omlx):
    c = FakeClient(loaded={GEMMA})
    c.activity_fails = True
    assert hold(c) == "busy"


def test_x_is_busy_when_last_access_moves_between_reads(on_omlx):
    c = FakeClient(loaded={GEMMA})
    reads = []
    real = c.status

    async def moving():
        reads.append(1)
        if len(reads) > 1:
            c.access[GEMMA] = WALL - 1
        return await real()
    c.status = moving
    assert hold(c) == "busy"


# --- POST /unload_agent ------------------------------------------------------------------

@pytest.fixture
def main_module(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "wisp-home"))
    try:
        import service.main as main
    except Exception as exc:  # noqa: BLE001 — heavy import; the hold itself is covered above
        pytest.skip(f"service.main won't import here ({type(exc).__name__}); covered via omlx_x_hold")
    return main


def _route(main, monkeypatch, client):
    target = SimpleNamespace(endpoint=SimpleNamespace(managed=True, provider="omlx"), model=GEMMA)
    monkeypatch.setattr(main, "client", client, raising=False)
    monkeypatch.setattr(main, "role_target", lambda role: target)
    monkeypatch.setattr(main, "role_to_model", lambda role: GEMMA)
    return asyncio.run(main.unload_agent())


def test_the_route_holds_for_another_apps_use_and_posts_no_unload(on_omlx, main_module, monkeypatch):
    c = FakeClient(loaded={GEMMA})
    idle._own_access[GEMMA] = time.time() - 3600
    c.access[GEMMA] = time.time() - 60
    result = _route(main_module, monkeypatch, c)
    assert result == {"unloaded": [], "held": "other_app"}
    assert c.calls == [] and not M._state.parked


def test_the_route_holds_a_model_that_is_not_a_gemma(on_omlx, main_module, monkeypatch):
    c = FakeClient(loaded={QWEN})
    target = SimpleNamespace(endpoint=SimpleNamespace(managed=True, provider="omlx"), model=QWEN)
    monkeypatch.setattr(main_module, "client", c, raising=False)
    monkeypatch.setattr(main_module, "role_target", lambda role: target)
    monkeypatch.setattr(main_module, "role_to_model", lambda role: QWEN)
    assert asyncio.run(main_module.unload_agent()) == {"unloaded": [], "held": M.NOT_GEMMA}
    assert c.calls == []


def test_the_route_unloads_and_parks_when_clear(on_omlx, main_module, monkeypatch):
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: GEMMA)
    M._state.omlx_model, M._state.omlx_block = GEMMA, None     # a tick has already looked
    c = FakeClient(loaded={GEMMA})
    c.access[GEMMA] = time.time() - 3600
    result = _route(main_module, monkeypatch, c)
    assert result == {"unloaded": [GEMMA]}
    assert c.unloads == [GEMMA]
    assert M._state.parked


def test_the_route_on_the_bundled_engine_never_asks_for_a_hold(main_module, monkeypatch):
    c = FakeClient(loaded={GEMMA}, bundled=True)
    c.active = 5                                          # would be "busy" on oMLX.app
    result = _route(main_module, monkeypatch, c)
    assert result == {"unloaded": [GEMMA]}
    assert c.unloads == [GEMMA]
