"""Study connector: choice, key handling, request shape, origin pinning, errors."""
import asyncio
import json
import os
import stat
import subprocess

import httpx
import pytest

from service.study import connector as C

KEY = "sk-or-test-SECRET-1234567890"
REAL_CLOUD_TARGET = C._cloud_target


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    C.forget_key()
    monkeypatch.setattr(C, "_cloud_cache", None)
    monkeypatch.setattr(C, "_cloud_target", lambda: None)

    def no_keychain(args):  # tests never reach the real Keychain: nothing is saved
        return subprocess.CompletedProcess(args, 44, b"", b"not found")
    monkeypatch.setattr(C, "_security", no_keychain)
    yield
    C.forget_key()


def reply(content, status=200):
    return httpx.Response(status, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


def call(server, reader=lambda p: KEY, **kw):
    return asyncio.run(C.complete_json("sys", "usr", key_reader=reader, transport=server.transport, **kw))


# --- settings ------------------------------------------------------------------------

def test_default_choice_is_openrouter_and_its_default_model():
    assert C.choice() == ("openrouter", "google/gemini-3.8-flash")


def test_set_choice_round_trips_private_and_atomic(tmp_path):
    C.set_choice("gemini", "gemini-3.8-flash")
    assert C.choice() == ("gemini", "gemini-3.8-flash")
    path = tmp_path / "home" / "study" / "settings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"provider": "gemini", "model_id": "gemini-3.8-flash"}
    assert not list(path.parent.glob("*.tmp"))
    C.set_choice("openrouter", "")
    assert C.choice() == ("openrouter", "google/gemini-3.8-flash")


@pytest.mark.parametrize("provider,model", [("openai", "gpt"), ("openrouter", "bad id"), ("gemini", "x" * 300)])
def test_set_choice_refuses_bad_values(provider, model):
    with pytest.raises(ValueError):
        C.set_choice(provider, model)


def test_unreadable_settings_fall_back_to_default(tmp_path):
    path = tmp_path / "home" / "study" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"provider": "evil", "model_id": "a b"}')
    assert C.choice() == ("openrouter", "google/gemini-3.8-flash")
    path.write_text("not json")
    assert C.choice() == ("openrouter", "google/gemini-3.8-flash")


def test_set_choice_clears_the_key_cache(monkeypatch):
    C._key_cache["openrouter"] = (KEY, 1e18)
    C._exists_cache["openrouter"] = (True, 1e18)
    monkeypatch.setattr(C, "_key_exists", lambda p: False)
    C.set_choice("openrouter", "google/gemini-3.8-flash")
    assert C._key_cache == {} and C._exists_cache == {}


# --- status --------------------------------------------------------------------------

def test_status_shape_without_a_key():
    C._exists_cache["openrouter"] = (False, 1e18)
    s = C.status()
    assert s["provider"] == "openrouter" and s["model_id"] == "google/gemini-3.8-flash"
    assert s["ready"] is False and s["source"] == "none" and s["active_model_id"] is None
    ids = [p["id"] for p in s["providers"]]
    assert ids == ["openrouter", "gemini"]
    for p in s["providers"]:
        assert set(p) == {"id", "label", "default_model", "models", "key_url", "key_hint"}
        assert p["models"][0]["id"] == p["default_model"]
    assert "origin" not in json.dumps(s)


def test_status_with_a_study_key_is_ready_and_never_reads_the_secret(monkeypatch):
    calls = []

    def fake_security(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(C, "_security", fake_security)
    for _ in range(5):
        s = C.status()
    assert s["ready"] is True and s["source"] == "study"
    assert s["active_model_id"] == s["model_id"] == "google/gemini-3.8-flash"
    assert len(calls) == 1, "existence is cached"
    assert "-w" not in calls[0] and calls[0][-2:] == ["-a", "openrouter"]
    assert KEY not in json.dumps(s)


def test_keychain_read_is_hardened_and_cached(monkeypatch):
    calls = []

    def fake_security(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, (KEY + "\n").encode(), b"")
    monkeypatch.setattr(C, "_security", fake_security)
    assert C._keychain_key("openrouter") == KEY
    assert C._keychain_key("openrouter") == KEY
    assert len(calls) == 1
    assert calls[0] == ["find-generic-password", "-s", "com.wisp.study", "-a", "openrouter", "-w"]
    assert C._keychain_key("nope") is None


@pytest.mark.parametrize("out,code,err", [(b"", 0, b""), (b"a" * 5000, 0, b""), (b"has space", 0, b""),
                                           (b"ok", 44, b""), (b"ok", 0, b"warning")])
def test_keychain_rejects_bad_values(monkeypatch, out, code, err):
    monkeypatch.setattr(C, "_security", lambda args: subprocess.CompletedProcess(args, code, out, err))
    assert C._keychain_key("gemini") is None


def test_status_cloud_fallback_on_remote_only_mac(monkeypatch):
    C._exists_cache["openrouter"] = (False, 1e18)
    monkeypatch.setattr(C, "_cloud_target", lambda: {"origin": "https://openrouter.ai", "prefix": "/api/v1",
                                                     "model": "google/gemini-3.9-pro", "key": lambda: KEY})
    s = C.status()
    assert s["ready"] is True and s["source"] == "cloud"
    # The cloud profile's model answers; the Study choice is kept as model_id.
    assert s["active_model_id"] == "google/gemini-3.9-pro" and s["model_id"] == "google/gemini-3.8-flash"


def _profile_modules(monkeypatch, base_url, prefix="/api/v1", model="google/gemini-3.8-flash", profile=True):
    from service.inference import remote_policy

    class EP:
        def __init__(self):
            self.base_url, self.api_prefix = base_url, prefix

        def api_key(self):
            return KEY

    class Target:
        endpoint = EP()

    Target.model = model
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object() if profile else None)
    monkeypatch.setattr(remote_policy, "remote_default_target", lambda role="fast": Target())


def test_real_cloud_target_requires_remote_profile_and_study_origin(monkeypatch):
    _profile_modules(monkeypatch, "https://openrouter.ai")
    target = REAL_CLOUD_TARGET()
    assert target["origin"] == "https://openrouter.ai" and target["model"] == "google/gemini-3.8-flash"
    _profile_modules(monkeypatch, "https://evil.example")
    assert REAL_CLOUD_TARGET() is None
    _profile_modules(monkeypatch, "https://openrouter.ai", prefix="/v1")
    assert REAL_CLOUD_TARGET() is None
    _profile_modules(monkeypatch, "https://openrouter.ai", profile=False)
    assert REAL_CLOUD_TARGET() is None


def test_cloud_target_survives_broken_modules(monkeypatch):
    from service.inference import remote_policy

    def boom():
        raise RuntimeError("different branch")
    monkeypatch.setattr(remote_policy, "remote_only_profile", boom)
    assert REAL_CLOUD_TARGET() is None


def test_complete_uses_cloud_fallback_when_no_study_key(monkeypatch):
    monkeypatch.setattr(C, "_keychain_key", lambda p: None)
    monkeypatch.setattr(C, "_cloud_target", lambda: {"origin": "https://generativelanguage.googleapis.com",
                                                     "prefix": "/v1beta/openai", "model": "gemini-3.8-flash",
                                                     "key": lambda: KEY})
    server = Server(reply('{"ok": 1}'))
    assert asyncio.run(C.complete_json("s", "u", transport=server.transport)) == {"ok": 1}
    req = server.requests[0]
    assert str(req.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert server.body()["model"] == "gemini-3.8-flash"
    assert "x-title" not in req.headers


def test_missing_when_no_key_anywhere(monkeypatch):
    monkeypatch.setattr(C, "_keychain_key", lambda p: None)
    with pytest.raises(C.StudyConnectorError) as err:
        asyncio.run(C.complete_json("s", "u", transport=Server().transport))
    assert err.value.code == "missing"


# --- requests ------------------------------------------------------------------------

def test_request_shape_and_headers():
    server = Server(reply('{"title": "x"}'))
    assert call(server, max_tokens=1234) == {"title": "x"}
    req = server.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-title"] == "Wisp Study"
    assert "http-referer" not in req.headers and "referer" not in req.headers
    body = server.body()
    assert body["model"] == "google/gemini-3.8-flash"
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 1234 and body["stream"] is False
    assert 0 <= body["temperature"] <= 1


def test_gemini_provider_url():
    C.set_choice("gemini", "gemini-3.8-flash")
    server = Server(reply('{"a": 1}'))
    call(server)
    assert str(server.requests[0].url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert "x-title" not in server.requests[0].headers


def test_origin_pinning_refuses_other_urls(monkeypatch):
    monkeypatch.setitem(C.PROVIDERS, "openrouter", dict(C.PROVIDERS["openrouter"], origin="https://evil.example"))
    server = Server(reply('{"a": 1}'))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "unreachable" and server.requests == []


def test_redirects_are_not_followed():
    server = Server(httpx.Response(302, headers={"location": "https://evil.example/x"}))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "bad_reply" and len(server.requests) == 1


def test_fenced_and_prose_wrapped_json():
    assert call(Server(reply('```json\n{"a": [1, 2]}\n```'))) == {"a": [1, 2]}
    assert call(Server(reply('Sure! Here it is: {"a": {"b": 1}} Hope that helps.'))) == {"a": {"b": 1}}
    parts = [{"type": "text", "text": '{"a": '}, {"type": "text", "text": "3}"}]
    assert call(Server(reply(parts))) == {"a": 3}


def test_retry_once_on_bad_json_then_bad_reply():
    server = Server(reply("not json"), reply('{"ok": true}'))
    assert call(server) == {"ok": True} and len(server.requests) == 2
    server = Server(reply("nope"), reply("[1, 2]"))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "bad_reply" and len(server.requests) == 2


def test_retry_once_on_5xx_and_timeout():
    server = Server(httpx.Response(503), reply('{"ok": 1}'))
    assert call(server) == {"ok": 1}
    server = Server(httpx.ReadTimeout("slow"), httpx.Response(500))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "unreachable" and len(server.requests) == 2
    server = Server(httpx.ConnectError("down"), httpx.ConnectError("down"))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "unreachable"


@pytest.mark.parametrize("status,code", [(401, "rejected"), (403, "rejected"), (429, "busy"), (404, "bad_reply")])
def test_error_mapping(status, code):
    server = Server(httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == code and len(server.requests) == 1
    assert KEY not in str(err.value) and KEY not in err.value.message and KEY not in repr(err.value.args)


def test_rejected_clears_cached_key():
    C._key_cache["openrouter"] = (KEY, 1e18)
    with pytest.raises(C.StudyConnectorError):
        call(Server(httpx.Response(401)))
    assert "openrouter" not in C._key_cache


def test_400_field_fallback_drops_the_named_field():
    server = Server(httpx.Response(400, json={"error": {"message": "reasoning_effort is not supported"}}),
                    reply('{"ok": 1}'))
    assert call(server) == {"ok": 1}
    first, second = server.body(0), server.body(1)
    assert "reasoning_effort" in first and "reasoning_effort" not in second
    assert second["response_format"] == {"type": "json_object"}


def test_400_unnamed_drops_both_once_then_bad_reply():
    server = Server(httpx.Response(400, text="Invalid request"), httpx.Response(400, text="Invalid request"))
    with pytest.raises(C.StudyConnectorError) as err:
        call(server)
    assert err.value.code == "bad_reply" and len(server.requests) == 2
    assert "reasoning_effort" not in server.body(1) and "response_format" not in server.body(1)


def test_key_reader_errors_never_leak_the_key():
    def reader(provider):
        raise RuntimeError(f"cannot read {KEY}")
    with pytest.raises(C.StudyConnectorError) as err:
        call(Server(), reader=reader)
    assert err.value.code == "missing" and KEY not in str(err.value)
    assert err.value.__cause__ is None or KEY not in str(err.value.__cause__)
    with pytest.raises(C.StudyConnectorError):
        call(Server(), reader=lambda p: "")


def test_key_never_logged(caplog):
    caplog.set_level("DEBUG")
    server = Server(httpx.Response(400, text="response_format unsupported"), httpx.Response(503),
                    reply("junk"))
    with pytest.raises(C.StudyConnectorError):
        call(server)
    assert KEY not in caplog.text


def test_unknown_error_code_becomes_bad_reply():
    assert C.StudyConnectorError("weird").code == "bad_reply"
    assert C.StudyConnectorError("busy").message


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_study_connector.py"' in manifest
