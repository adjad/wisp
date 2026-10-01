"""A credential typed into chat is never stored, logged or sent to a model."""
import json

import pytest

import importlib
from service.safety.redaction import (HANDOFF_NOTICE, PLACEHOLDER, is_key_handoff,
                                      scrub, scrub_obj)

audit_module = importlib.import_module("service.safety.audit")  # the package re-exports a function of the same name

# Built at runtime so no literal in this file looks like a real secret.
OPENROUTER = "sk-or-v1-" + "a1b2c3d4" * 6
OPENAI = "sk-proj-" + "Zx9" * 14
ANTHROPIC = "sk-ant-api03-" + "Qw7_" * 10
GITHUB = "ghp_" + "A1b2C3" * 7
AWS = "AKIA" + "ABCDEFGH12345678"
GOOGLE = "AIza" + "Sy" * 17 + "x"
HF = "hf_" + "abCD12" * 7
SLACK = "xoxb-" + "1234567890-" * 3
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".abcdefghij_KLMNOP"
OPAQUE = "syntheticLocal" + "Credential0123456789"


@pytest.mark.parametrize("label", ["api_key", "secret-key", "access_token", "auth-token", "password"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_json_and_yaml_assignments_preserve_labels(label, quote):
    text = f"{quote}{label}{quote}: {quote}{OPAQUE}{quote}"
    expected = f"{quote}{label}{quote}: {quote}{PLACEHOLDER}{quote}"
    assert scrub(text) == (expected, 1)
    assert scrub(expected) == (expected, 0)


@pytest.mark.parametrize("depth", [8, 9, 10])
@pytest.mark.parametrize("container", [dict, list, tuple])
def test_audit_depth_budget_never_returns_an_unscrubbed_subtree(depth, container):
    value = OPENROUTER
    for _ in range(depth):
        value = {"nested": value} if container is dict else container([value])
    original = json.dumps(value)
    assert OPENROUTER not in json.dumps(scrub_obj(value))
    assert PLACEHOLDER in json.dumps(scrub_obj(value))
    assert json.dumps(value) == original


def test_actual_audit_writer_scrubs_quoted_fields_and_deep_subtrees(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_module, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit_module, "AUDIT_LOG", tmp_path / "audit.jsonl")
    deep = OPENROUTER
    for _ in range(10):
        deep = {"nested": [deep]}
    audit_module.audit("allow", tool="fixture", args={"json": json.dumps({"api_key": OPAQUE}),
                                                     "deep": deep, "safe": "kept"})
    text = (tmp_path / "audit.jsonl").read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert len(records) == 1
    assert records[0]["args"]["safe"] == "kept"
    assert OPAQUE not in text and OPENROUTER not in text and PLACEHOLDER in text


def test_actual_turn_store_and_nonempty_fts_scrub_quoted_json(tmp_path):
    from service.memory.store import SessionStore
    sessions = SessionStore(tmp_path / "quoted-sessions.db")
    sid = sessions.create_session()
    for role in ("user", "assistant"):
        sessions.add_turn(sid, role, json.dumps({"api_key": OPAQUE}))
    rows = sessions._db.execute("SELECT content FROM turns").fetchall()
    fts = sessions._db.execute("SELECT text FROM memory_turn_fts").fetchall()
    assert len(rows) == len(fts) == 2
    assert all(OPAQUE not in row[0] and PLACEHOLDER in row[0] for row in rows + fts)


@pytest.mark.parametrize("secret", [OPENROUTER, OPENAI, ANTHROPIC, GITHUB, AWS, GOOGLE, HF, SLACK, JWT])
def test_provider_key_shapes_are_redacted(secret):
    clean, count = scrub(f"here you go {secret} thanks")
    assert secret not in clean and PLACEHOLDER in clean and count == 1


def test_pem_private_key_block_is_redacted():
    body = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    clean, count = scrub("my key:\n" + body + "\nplease store")
    assert "MIIEvQ" not in clean and count == 1 and "please store" in clean


def test_bearer_header_and_assignments_are_redacted_keeping_the_label():
    clean, _ = scrub("curl -H 'Authorization: Bearer " + "x" * 30 + "' https://example.test")
    assert "x" * 30 not in clean
    clean, count = scrub('api_key = "abcdef0123456789abcdef"')
    assert clean == f'api_key = "{PLACEHOLDER}"' and count == 1
    assert scrub("token: " + "9" * 20)[0] == "token: " + PLACEHOLDER


@pytest.mark.parametrize("text", [
    "what's on my calendar tomorrow",
    "commit 9fceb02d0ae598e95dc970b74767f19372d61724 broke the build",
    "meeting id 3f2b8a1c-5d6e-4f70-8a9b-0c1d2e3f4a5b at noon",
    "read https://example.test/a/very/long/path/with/many/segments/and-a-slug-0123456789",
    "my token of appreciation",
    "the password is required to log in",
    "sk-short",
    "call me at +1 650 555 0134",
    "",
])
def test_ordinary_text_is_untouched(text):
    assert scrub(text) == (text, 0)


def test_scrub_is_idempotent_and_never_raises():
    once, _ = scrub("use " + OPENROUTER)
    assert scrub(once) == (once, 0)
    assert scrub(None) == (None, 0)
    assert scrub(12345) == (12345, 0)


def test_scrub_obj_redacts_nested_audit_fields():
    cleaned = scrub_obj({"cmd": f"export K={OPENROUTER}", "args": [{"x": f"Bearer {'y' * 30}"}], "n": 3})
    assert OPENROUTER not in json.dumps(cleaned) and "y" * 30 not in json.dumps(cleaned)
    assert cleaned["n"] == 3


@pytest.mark.parametrize("prompt,expected", [
    (f"/connect openrouter {OPENROUTER}", True),
    (f"here is my key {OPENROUTER}", True),
    (f"{OPENROUTER}", True),
    (f"summarize the attached notes and then tell me whether the key {OPENROUTER} in this log looks rotated", False),
])
def test_key_handoff_detection(prompt, expected):
    assert is_key_handoff(scrub(prompt)[0]) is expected


def test_audit_log_never_records_a_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_module, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit_module, "AUDIT_LOG", tmp_path / "audit.jsonl")
    audit_module.audit("allow", tool="run_shell", args={"cmd": f"curl -H 'Authorization: Bearer {'z' * 30}'"})
    audit_module.audit("allow", tool="write_file", args={"path": "/tmp/x", "content": OPENROUTER})
    text = (tmp_path / "audit.jsonl").read_text()
    assert "z" * 30 not in text and OPENROUTER not in text and PLACEHOLDER in text


def test_turn_store_never_persists_a_credential(tmp_path):
    from service.memory.store import SessionStore
    sessions = SessionStore(tmp_path / "sessions.db")
    sid = sessions.create_session()
    sessions.add_turn(sid, "user", f"/connect openrouter {OPENROUTER}")
    sessions.add_turn(sid, "assistant", f"Sure, using {OPENAI}")
    rows = sessions._db.execute("SELECT content FROM turns").fetchall()
    assert rows and all(OPENROUTER not in r[0] and OPENAI not in r[0] for r in rows)
    fts = sessions._db.execute("SELECT * FROM memory_turn_fts").fetchall()
    assert all(OPENROUTER not in " ".join(map(str, r)) for r in fts)


@pytest.mark.parametrize("prompt,secret", [(f"/connect openrouter {OPENROUTER}", OPENROUTER),
                                          (json.dumps({"api_key": OPAQUE}), OPAQUE)])
def test_agent_answers_a_key_handoff_without_a_model_and_stores_nothing(monkeypatch, tmp_path, prompt, secret):
    from fastapi.testclient import TestClient
    from service import main
    from service.memory.store import SessionStore
    import types

    def no_model(*_a, **_k):
        raise AssertionError("a key handoff must not reach routing or a model")
    monkeypatch.setattr(main, "run_agent", no_model, raising=False)
    monkeypatch.setattr(main, "route", no_model)
    monkeypatch.setattr(main, "client", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(main, "store", SessionStore(tmp_path / "handoff.db"))
    with TestClient(main.app).stream("POST", "/agent",
                                     json={"prompt": prompt}) as response:
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds == ["session", "text", "done"]
    assert events[1]["text"] == HANDOFF_NOTICE and secret not in json.dumps(events)
    sid = events[0]["id"]
    stored = " ".join(t["content"] for t in main.store.turns_from(sid, 0))
    assert secret not in stored and PLACEHOLDER in stored
    fts = main.store._db.execute("SELECT text FROM memory_turn_fts").fetchall()
    assert len(fts) == 2 and secret not in json.dumps([tuple(row) for row in fts])
