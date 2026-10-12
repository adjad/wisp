"""brain_server: auth, size limit, healthz, endpoint/engine mapping, config,
and the deploy scripts' syntax. Uses a fake engine; one test exercises the
real BehaviorEngine when it is implemented."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brain_server.app import create_app
from brain_server.config import ConfigError, check_bind, load_token, parse_args
from service.behavior.types import Action, Nudge, Observation, Pattern, Prediction, Window

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeEngine:
    """Same public surface as BehaviorEngine, deterministic, records calls."""

    def __init__(self) -> None:
        self.obs: dict[str, Observation] = {}
        self.calls: list[tuple] = []
        self.thread_ids: set[int] = {threading.get_ident()}
        self.statuses: dict[str, str] = {}
        self.feedbacks = []
        self.nudge_log: list[Nudge] = []

    def _rec(self, *call):
        self.calls.append(call)
        self.thread_ids.add(threading.get_ident())

    def observe(self, observations):
        observations = list(observations)
        self._rec("observe", len(observations))
        for o in observations:
            assert isinstance(o, Observation)
            self.obs[o.id] = o
        return len(observations)

    def observe_snapshot(self, source, observations, *, window_start, window_end, taken_at):
        observations = list(observations)
        self._rec("observe_snapshot", source, window_start, window_end, taken_at)
        for o in observations:
            self.obs[o.id] = o
        return {"upserted": len(observations), "cancelled": 1, "moved": 0}

    def _pattern(self, pid="p1"):
        return Pattern(id=pid, kind="habit", subject="fitness",
                       statement="You go to the gym on weekday mornings",
                       window=Window((0, 1, 2, 3, 4), 390, 480), support=8, opportunities=10,
                       confidence=0.55, evidence=("cal:a:1", "cal:b:2"), stats={"n": 8},
                       status=self.statuses.get(pid, "active"))

    def mine(self, now=None):
        self._rec("mine", now)
        return [self._pattern()]

    def patterns(self, *, status=None, kind=None):
        self._rec("patterns", status, kind)
        return [self._pattern()] if status in (None, self.statuses.get("p1", "active")) else []

    def set_pattern_status(self, pattern_id, status):
        self._rec("set_pattern_status", pattern_id, status)
        if pattern_id != "p1":
            raise KeyError(pattern_id)
        self.statuses[pattern_id] = status
        return self._pattern(pattern_id)

    def predict(self, now=None, horizon_s=86400.0):
        self._rec("predict", now, horizon_s)
        return [Prediction(kind="activity", subject="gym", ts=1000.0, end_ts=2000.0, probability=0.8,
                           because="8 of last 10 weekdays", pattern_id="p1", refs=("cal:a:1",))]

    def generate_nudges(self, now=None):
        self._rec("generate_nudges", now)
        n = Nudge(id=f"n{len(self.nudge_log) + 1}", kind="habit_conflict", title="Gym vs Team sync",
                  body="Team sync overlaps your gym time.", because="8/10 weekdays",
                  created_at=now or 1.0, priority=0.7, pattern_ids=("p1",), evidence=("cal:x:1",),
                  actions=(Action("add_hold", {"title": "Gym", "when_ts": 5.0}, False, "Hold gym"),),
                  seq=len(self.nudge_log) + 1)
        self.nudge_log.append(n)
        return [n]

    def nudges_since(self, cursor=0, limit=50):
        self._rec("nudges_since", cursor, limit)
        items = [n for n in self.nudge_log if n.seq > cursor][:limit]
        return items, (items[-1].seq if items else cursor)

    def feedback(self, fb):
        self._rec("feedback", fb)
        self.feedbacks.append(fb)

    def profile_card(self, now=None, max_bytes=700):
        self._rec("profile_card", now, max_bytes)
        return "Gym weekday mornings 6:30-8:00\nTalks to Sam in the afternoon"[:max_bytes]

    def describe(self, subject=None, now=None):
        self._rec("describe", subject, now)
        return {"patterns": [self._pattern().to_dict()], "predictions": [], "summary": f"about {subject}"}

    def status(self):
        self._rec("status")
        return {"observations": {"event": len(self.obs)}, "patterns": 1, "last_mined_at": None,
                "nudge_cursor": len(self.nudge_log)}


@pytest.fixture
def fake():
    return FakeEngine()


@pytest.fixture
def client(fake, tmp_path):
    app = create_app(tmp_path / "brain.db", TOKEN, engine_factory=lambda: fake, background=False)
    with TestClient(app) as c:
        yield c


def obs_dict(i=1, kind="event"):
    return Observation(id=f"cal:e{i}:{1000 + i}", kind=kind, ts=1000.0 + i, subject="Gym",
                       source="calendar", end_ts=2000.0 + i, attrs={"k": i}).to_dict()


# ---- auth / health / limits -------------------------------------------------
def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "engine": "ready"}


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/observe"), ("POST", "/v1/snapshot"), ("POST", "/v1/mine"), ("GET", "/v1/patterns"),
    ("POST", "/v1/patterns/p1/status"), ("GET", "/v1/predict"), ("POST", "/v1/nudges/generate"),
    ("GET", "/v1/nudges"), ("POST", "/v1/feedback"), ("GET", "/v1/profile"), ("GET", "/v1/describe"),
    ("GET", "/v1/status"),
])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": f"Basic {TOKEN}"}])
def test_every_endpoint_requires_token(client, fake, method, path, headers):
    r = client.request(method, path, headers=headers, json={})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert fake.calls == []


def test_body_size_limit(client, fake):
    big = [obs_dict(i) for i in range(1)]
    big[0]["subject"] = "x" * (2 * 1024 * 1024 + 10)
    r = client.post("/v1/observe", headers=AUTH, json={"observations": big})
    assert r.status_code == 413
    assert fake.calls == []


def test_body_size_limit_without_content_length(fake, tmp_path):
    app = create_app(tmp_path / "b.db", TOKEN, engine_factory=lambda: fake, background=False,
                     max_body_bytes=1000)

    def chunks():
        for _ in range(20):
            yield b"x" * 100

    with TestClient(app) as c:
        r = c.post("/v1/observe", headers=AUTH, content=chunks())
    assert r.status_code == 413


def test_engine_not_implemented_reports_503(tmp_path):
    def factory():
        raise NotImplementedError

    app = create_app(tmp_path / "b.db", TOKEN, engine_factory=factory, background=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 503
        assert c.get("/v1/status", headers=AUTH).status_code == 503


def test_short_token_refused(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path / "b.db", "short", engine_factory=FakeEngine)


# ---- endpoint mapping -------------------------------------------------------
def test_observe_and_validation(client, fake):
    r = client.post("/v1/observe", headers=AUTH, json={"observations": [obs_dict(1), obs_dict(2)]})
    assert r.status_code == 200 and r.json() == {"count": 2}
    assert fake.obs["cal:e1:1001"].attrs == {"k": 1}
    for bad in ([{"id": "x"}], [{"id": "x", "kind": "bogus", "ts": 1}], [{"id": "x", "kind": "event", "ts": "now"}],
                "nope"):
        assert client.post("/v1/observe", headers=AUTH, json={"observations": bad}).status_code == 422


def test_engine_errors_map_to_status(client):
    assert client.post("/v1/patterns/missing/status", headers=AUTH, json={"status": "confirmed"}).status_code == 404
    assert client.post("/v1/patterns/p1/status", headers=AUTH, json={"status": "maybe"}).status_code == 400
    assert client.get("/v1/predict", headers=AUTH, params={"horizon_s": 0}).status_code == 422


def test_engine_runs_on_one_dedicated_thread(fake, tmp_path):
    app = create_app(tmp_path / "b.db", TOKEN, engine_factory=lambda: FakeEngine(), background=False)
    engine = app.state.host.engine
    with TestClient(app) as c:
        for _ in range(5):
            c.get("/v1/status", headers=AUTH)
    # thread_ids = {constructing thread, worker thread}: both are the single engine worker
    assert len(engine.thread_ids) == 1


def test_background_loop_mines_and_nudges(fake, tmp_path):
    app = create_app(tmp_path / "b.db", TOKEN, engine_factory=lambda: fake, background=True,
                     mine_interval_s=0.5, nudge_interval_s=0.5)
    with TestClient(app) as c:
        deadline = time.time() + 15
        while time.time() < deadline:
            names = {call[0] for call in fake.calls}
            if {"mine", "generate_nudges"} <= names:
                break
            time.sleep(0.2)
        st = c.get("/v1/status", headers=AUTH).json()
    assert {"mine", "generate_nudges"} <= {call[0] for call in fake.calls}
    assert st["brain_server"]["background"] is True
    assert st["brain_server"]["last_mined_at"] is not None


# ---- config -----------------------------------------------------------------
def test_bind_refuses_public_without_flag():
    with pytest.raises(ConfigError):
        check_bind("0.0.0.0", False)
    with pytest.raises(ConfigError):
        parse_args(["--host", "0.0.0.0"])
    with pytest.raises(ConfigError):
        parse_args(["--host", "100.111.110.43"])
    check_bind("0.0.0.0", True)
    check_bind("127.0.0.1", False)
    check_bind("::1", False)
    cfg = parse_args(["--db", "~/x.db", "--ollama", "http://127.0.0.1:11434/", "--no-background"])
    assert cfg.host == "127.0.0.1" and cfg.port == 8790 and cfg.ollama == "http://127.0.0.1:11434"
    assert cfg.background is False and str(cfg.db).endswith("/x.db") and "~" not in str(cfg.db)


def test_load_token_checks_mode_and_length(tmp_path):
    p = tmp_path / "token"
    p.write_text("a" * 43 + "\n")
    os.chmod(p, 0o644)
    with pytest.raises(ConfigError, match="0600"):
        load_token(p)
    os.chmod(p, 0o600)
    assert load_token(p) == "a" * 43
    p.write_text("short")
    with pytest.raises(ConfigError):
        load_token(p)
    with pytest.raises(ConfigError):
        load_token(tmp_path / "missing")


def test_main_exits_2_on_bad_config(tmp_path, capsys):
    from brain_server.__main__ import main
    assert main(["--token-file", str(tmp_path / "missing")]) == 2
    assert "not found" in capsys.readouterr().err


# ---- real engine (skips until the engine workstream lands) ------------------
def _real_engine_or_skip(tmp_path):
    from service.behavior import engine as engine_mod
    try:
        eng = engine_mod.BehaviorEngine(tmp_path / "probe.db")
    except NotImplementedError:
        pytest.skip("BehaviorEngine not implemented yet")
    try:
        eng.status()
    except NotImplementedError:
        pytest.skip("BehaviorEngine not implemented yet")
    if callable(getattr(eng, "close", None)):
        eng.close()


def test_real_engine_round_trip(tmp_path):
    """The real BehaviorEngine behind HTTP, fed the fictional persona: what the
    Mac gets through RemoteEngine equals what the engine computes in-process."""
    _real_engine_or_skip(tmp_path)
    from service.behavior.client import RemoteEngine
    from service.behavior.engine import BehaviorEngine
    from service.behavior.persona import generate_persona
    from service.behavior.types import Feedback

    now = 1_791_210_600.0  # Monday 2026-10-05 07:30 America/Los_Angeles (nudges allowed)
    persona = generate_persona(now, tz="America/Los_Angeles")
    tz = "America/Los_Angeles"
    app = create_app(tmp_path / "brain.db", TOKEN, background=False, tz=tz)
    local = BehaviorEngine(tmp_path / "local.db", tz=tz)
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"ok": True, "engine": "ready"}
        remote = RemoteEngine("http://127.0.0.1:8790", TOKEN, client=c)
        for eng in (remote, local):
            assert eng.observe(persona.observations) >= 1
            snap = eng.observe_snapshot("calendar", persona.upcoming, window_start=now - 86400,
                                        window_end=now + 14 * 86400, taken_at=now)
            assert set(snap) >= {"upserted", "cancelled", "moved"}
        mined = remote.mine(now=now)
        assert mined == local.mine(now=now) and mined, "patterns must survive the wire unchanged"
        assert {p.kind for p in mined} >= {"habit"}
        assert remote.predict(now=now) == local.predict(now=now)
        created = remote.generate_nudges(now=now)
        assert created, "a weekday 07:30 with the persona produces at least a day_preview"
        assert [n.to_dict() for n in created] == [n.to_dict() for n in local.generate_nudges(now=now)]
        items, cursor = remote.nudges_since(0)
        assert [n.id for n in items] == [n.id for n in created] and cursor >= len(created)
        remote.feedback(Feedback(nudge_id=created[0].id, verdict="useful", ts=now))
        p = remote.set_pattern_status(mined[0].id, "confirmed")
        assert p.status == "confirmed" and remote.patterns(status="confirmed")[0].id == mined[0].id
        with pytest.raises(KeyError):
            remote.set_pattern_status("no-such-pattern", "confirmed")
        card = remote.profile_card(now=now, max_bytes=300)
        assert isinstance(card, str) and len(card.encode()) <= 300
        d = remote.describe("lift", now=now)
        assert {"patterns", "predictions", "summary"} <= set(d)
        st = remote.status()
        assert st["brain_server"]["background"] is False
    local.close()


# ---- deploy scripts ---------------------------------------------------------
DEPLOY = ROOT / "deploy" / "gx10"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))


def test_deploy_scripts_exist():
    names = {p.name for p in SCRIPTS}
    assert {"install-gateway-service.sh", "rollback-gateway-service.sh", "install-brain.sh",
            "uninstall-brain.sh", "wait-for-ollama.sh", "healthcheck.sh", "mac-check-gateway.sh"} <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_deploy_script_syntax_and_strict_mode(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert os.stat(script).st_mode & stat.S_IXUSR, f"{script.name} is not executable"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_deploy_scripts_shellcheck(script):
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck not installed")
    subprocess.run([sc, "-x", str(script)], check=True)


@pytest.mark.parametrize("script", [p for p in SCRIPTS if p.name.startswith(("install", "rollback", "uninstall"))],
                         ids=lambda p: p.name)
def test_mutating_scripts_support_dry_run_and_help(script):
    text = script.read_text()
    assert "--dry-run" in text
    r = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and "--dry-run" in r.stdout


def test_gateway_unit_runs_exact_command_and_never_touches_serve_or_tokens():
    unit = (DEPLOY / "wisp-gx10-gateway.service").read_text()
    assert "Restart=on-failure" in unit
    assert "ExecStartPre=@WAIT_SCRIPT@ --require-free-port @PORT@" in unit
    assert "ExecStartPre=@WAIT_SCRIPT@ --url http://127.0.0.1:11434/api/version" in unit
    assert "wait-for-ollama.sh" in (DEPLOY / "install-gateway-service.sh").read_text()
    assert "-B @GATEWAY_PY@ --tokens @TOKENS_JSON@ --model @MODEL@" in unit
    install = (DEPLOY / "install-gateway-service.sh").read_text()
    assert "20261010-full-tools-preview-v2/gateway.py" in install
    assert "20261010-v1/tokens.json" in install
    assert "qwen3.6:35b" in install
    for script in SCRIPTS:
        text = script.read_text()
        # no script changes the 443 Serve mount or writes the gateway tokens
        assert "--https=443" not in text and "serve reset" not in text, script.name
        assert not any(w in text for w in ("> \"$TOKENS", ">\"$TOKENS", "chmod 600 \"$TOKENS")), script.name


def test_brain_unit_binds_loopback():
    unit = (DEPLOY / "wisp-brain.service").read_text()
    assert "--host 127.0.0.1" in unit and "--port @PORT@" in unit
    install = (DEPLOY / "install-brain.sh").read_text()
    assert "--https=8443" in install and "umask 077" in install


def test_predict_without_horizon_uses_engine_default(client, fake):
    """Finding 1: an omitted horizon reaches the engine as None so it can use
    its own default (>= 24h, through the end of tomorrow) instead of a fixed 24h."""
    assert client.get("/v1/predict", headers=AUTH).status_code == 200
    assert client.get("/v1/predict", headers=AUTH, params={"horizon_s": 7200}).status_code == 200
    preds = [c for c in fake.calls if c[0] == "predict"]
    assert preds[0][-1] is None and preds[1][-1] == 7200.0


# ---- tailnet :8443 ownership (finding 7) ---------------------------------------
_OURS = {"TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
         "Web": {"gx10.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}},
                 "gx10.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}}}
_FOREIGN = {"TCP": {"8443": {"HTTPS": True}},
            "Web": {"gx10.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
_ABSENT = {"TCP": {"443": {"HTTPS": True}},
           "Web": {"gx10.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}}}


def _fake_tailscale(tmp_path, status):
    """A `tailscale` on PATH that prints ``status`` for `serve status --json`
    and logs every invocation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "status.json").write_text(json.dumps(status))
    ts = bin_dir / "tailscale"
    ts.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{tmp_path}/calls.log"\n'
                  f'if [[ "$*" == "serve status --json" ]]; then cat "{tmp_path}/status.json"; fi\n')
    ts.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home")}


@pytest.mark.parametrize("status,port,expected", [
    (_OURS, "8790", "ours"), (_ABSENT, "8790", "absent"), (_FOREIGN, "8790", "foreign"),
    (_OURS, "8791", "foreign"),
])
@pytest.mark.parametrize("script", ["install-brain.sh", "uninstall-brain.sh"])
def test_serve_8443_owner_classifies_the_mount(tmp_path, status, port, expected, script):
    env = _fake_tailscale(tmp_path, status)
    text = (DEPLOY / script).read_text()
    fn = text[text.index("serve_8443_owner() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\nPORT={port}\nPYTHON="{sys.executable}"\n{fn}\n'
                        "serve_8443_owner"], capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[0] == expected


@pytest.mark.parametrize("status,removes", [(_OURS, True), (_FOREIGN, False), (_ABSENT, False)])
def test_uninstall_serve_off_only_removes_our_mount(tmp_path, status, removes):
    env = _fake_tailscale(tmp_path, status)
    r = subprocess.run(["bash", str(DEPLOY / "uninstall-brain.sh"), "--serve-off", "--dry-run",
                        "--python", sys.executable], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert ("tailscale serve --https=8443 off" in r.stdout) is removes, r.stdout + r.stderr
    if status is _FOREIGN:
        assert "NOT removing" in r.stderr
    log = (tmp_path / "calls.log").read_text()
    assert "off" not in log                       # dry run: nothing but status reads hit tailscale


def test_install_refuses_foreign_8443_before_changing_anything():
    text = (DEPLOY / "install-brain.sh").read_text()
    pre = text[:text.index("# ---------------------------------------------------------------- code")]
    assert 'SERVE_STATE="$(serve_8443_owner)"' in pre and "refusing to replace it" in pre
    serve = text[text.index("# ---------------------------------------------------------------- tailscale serve :8443"):]
    # re-checked immediately before the write, and the write is skipped when already ours
    assert serve.index('SERVE_STATE="$(serve_8443_owner)"') < serve.index('elif ! tailscale serve --bg --https=8443')
