"""Cloud-everything scale-ups (remote_policy.cloud_everything_active): each limit
is lifted only under the gate, and the on-device values stay exactly as they were
with it off. Synthetic only: no network, no Keychain, no real Wisp state."""
import asyncio
import sqlite3

import pytest

from service.inference import remote_policy as policy

GEMINI = "google/gemini-3.8-flash"
URL = "https://openrouter.ai"


def everything_profile(**changes):
    from service.config import _EVERYTHING_LIMITS
    raw = {"base_url": URL, "api_prefix": "/api/v1", "model_id": GEMINI,
           "credential_ref": "keychain:cloud-test", "context_window": 1_000_000,
           "readiness_timeout": 15, "enabled": True, **_EVERYTHING_LIMITS, **changes}
    return raw


@pytest.fixture
def gate(monkeypatch):
    """gate(True) turns cloud-everything on (a valid profile plus parity)."""
    def set_gate(on: bool):
        profile = policy.profile_from_config(everything_profile()) if on else None
        monkeypatch.setattr(policy, "remote_only_profile", lambda: profile)
        monkeypatch.setattr(policy, "remote_parity", lambda: on)
    set_gate(False)
    return set_gate


# --- the gate and its helpers ------------------------------------------------

def test_gate_needs_both_the_profile_and_parity(monkeypatch):
    profile = policy.profile_from_config(everything_profile())
    for has_profile, parity, want in ((False, False, False), (False, True, False),
                                      (True, False, False), (True, True, True)):
        monkeypatch.setattr(policy, "remote_only_profile", lambda p=has_profile: profile if p else None)
        monkeypatch.setattr(policy, "remote_parity", lambda v=parity: v)
        assert policy.cloud_everything_active() is want


def test_output_budget_and_timeout(gate):
    assert policy.cloud_output_budget(512) == 512
    assert policy.cloud_call_timeout(12.0, 4096) == 12.0
    gate(True)
    assert policy.cloud_output_budget(512) == policy.REMOTE_MEDIA_MIN_TOKENS
    assert policy.cloud_output_budget(30_000) == 16384           # the profile's max_tokens
    assert policy.cloud_call_timeout(12.0, 4096) == pytest.approx(4096 / 150 + 15)
    assert policy.cloud_call_timeout(90.0, 400) == 90.0


def test_context_table_is_one_million_for_every_offered_model():
    assert set(policy.REMOTE_CONTEXT_WINDOWS) == set(policy.REMOTE_INPUT_MODALITIES)
    assert set(policy.REMOTE_CONTEXT_WINDOWS.values()) == {1_000_000}
    assert all(v <= policy.LIMITS["context_window"][1] for v in policy.REMOTE_CONTEXT_WINDOWS.values())
    assert policy.remote_context_window("vendor/unlisted") is None


def test_gemini_steps_reason_low_and_haiku_keeps_medium(monkeypatch):
    # The launcher path without remote_parity sends no OpenRouter Gemini reasoning (NOTE 1).
    monkeypatch.setattr(policy, "cloud_everything_active", lambda: False)
    assert policy.reasoning_option(GEMINI) is None
    assert policy.reasoning_option("anthropic/claude-haiku-5.5") == {"effort": "medium"}
    monkeypatch.setattr(policy, "cloud_everything_active", lambda: True)
    assert policy.reasoning_option(GEMINI) == {"effort": "low"}
    assert policy.reasoning_option("anthropic/claude-haiku-5.5") == {"effort": "medium"}
    assert policy.reasoning_option("openai/gpt-6-luna") is None
    profile = policy.profile_from_config(everything_profile())
    body = {"model": GEMINI, "messages": [{"role": "user", "content": "x"}], "max_tokens": 4096,
            "stream": False}
    profile.validate_payload({**body, "reasoning": {"effort": "low"}})
    with pytest.raises(Exception):
        profile.validate_payload({**body, "reasoning": {"effort": "medium"}})


def test_a_million_token_request_fits_the_everything_profile():
    profile = policy.profile_from_config(everything_profile())
    text = "word " * (2_000_000)          # 10 MB, about 2.5M chars a side of 1M tokens
    profile.validate_payload({"model": GEMINI, "stream": False, "max_tokens": 16384,
                              "messages": [{"role": "user", "content": text}]})


# --- config: limits, Connect and the startup upgrade --------------------------

def test_set_cloud_everything_saves_a_million_token_profile(monkeypatch):
    import service.config as config
    saved = []
    monkeypatch.setattr(config, "_save_overlay", lambda update, **k: saved.append(update))
    endpoint = {"base_url": URL, "api_prefix": "/api/v1", "credential_ref": "keychain:cloud-test"}
    config.set_cloud_everything(endpoint, GEMINI, 1_000_000)
    profile = saved[0]["inference"]["remote_only"]
    assert profile["context_window"] == 1_000_000 and profile["max_tokens"] == 16384
    assert profile["request_bytes"] == 16 * 1024 * 1024 and profile["aggregate_bytes"] == 12 * 1024 * 1024
    assert profile["messages"] == 1000


def legacy_cfg(**changes):
    profile = everything_profile(context_window=32_000, max_tokens=8192,
                                 request_bytes=8 * 1024 * 1024, aggregate_bytes=4 * 1024 * 1024)
    profile.update(changes)
    return {"remote_parity": True, "inference": {"remote_only": profile}}


def test_startup_upgrade_raises_an_old_intel_profile(monkeypatch):
    import service.config as config
    monkeypatch.setattr(config, "_intel_mac", lambda: True)
    from service.config import upgraded_cloud_profile
    up = upgraded_cloud_profile(legacy_cfg())
    assert up["context_window"] == 1_000_000 and up["max_tokens"] == 16384
    assert up["request_bytes"] == 16 * 1024 * 1024 and up["aggregate_bytes"] == 12 * 1024 * 1024
    policy.profile_from_config(up)                     # still a valid profile
    # Only exactly the old values change; a hand-set one is kept.
    assert upgraded_cloud_profile(legacy_cfg(max_tokens=4000))["max_tokens"] == 4000


@pytest.mark.parametrize("cfg", [
    legacy_cfg(model_id="vendor/unlisted"),
    legacy_cfg(context_window=64_000),
    legacy_cfg(base_url="https://inference.example"),
    {**legacy_cfg(), "remote_parity": False},
    {"remote_parity": True, "inference": {}},
])
def test_startup_upgrade_leaves_everything_else_alone(cfg):
    from service.config import upgraded_cloud_profile
    assert upgraded_cloud_profile(cfg) is None


def test_startup_upgrade_saves_once(monkeypatch):
    import service.config as config
    monkeypatch.setattr(config, "_intel_mac", lambda: True)
    saved = []
    monkeypatch.setattr(config, "models_config", legacy_cfg)
    monkeypatch.setattr(config, "_save_overlay", lambda update, **k: saved.append(update))
    up = config.upgrade_cloud_profile()
    assert up["context_window"] == 1_000_000
    assert saved == [{"inference": {"remote_only": up, "super_model": {"context_window": 1_000_000}}}]
    # A save that fails leaves the old profile working.
    monkeypatch.setattr(config, "_save_overlay", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert config.upgrade_cloud_profile() is None


def test_connect_takes_a_million_tokens_only_for_everything(monkeypatch):
    from service import main
    from service.config import provider_credentials as secrets
    import service.config as config
    monkeypatch.setattr(secrets, "resolve_keychain", lambda *a, **k: "k" * 32)
    monkeypatch.setattr(main, "remote_only_profile", lambda: object())

    async def no_probe(*_a, **_k):          # the all-cloud switch probe is a network call
        return None
    monkeypatch.setattr(main, "_remote_only_switch_probe", no_probe, raising=False)
    saved = []
    monkeypatch.setattr(config, "set_cloud_everything", lambda *a, **k: saved.append(a))

    async def settings():
        return {"enabled": True}
    monkeypatch.setattr(main, "get_cloud_inference", settings)
    body = {"provider": "openrouter", "base_url": URL, "api_prefix": "/api/v1", "model_id": GEMINI,
            "context_window": 1_000_000, "roles": ["everything"], "super_model_enabled": False,
            "credential_name": "cloud-0123456789abcdef0123456789abcdef"}
    asyncio.run(main.connect_cloud_inference(body))
    assert saved and saved[0][2] == 1_000_000
    with pytest.raises(main.HTTPException) as refused:
        asyncio.run(main.connect_cloud_inference({**body, "roles": ["agent"]}))
    assert refused.value.status_code == 400
    with pytest.raises(main.HTTPException):
        asyncio.run(main.connect_cloud_inference({**body, "context_window": 1_048_577}))


# --- Ask -----------------------------------------------------------------------

def test_ask_budgets_and_menu(gate):
    from service import model_only as mo
    from service.router import model_led as ml
    off = (mo.ask_prompt_budget(), mo.ask_history_reserve(), mo.ask_preload(), mo.ask_stubs(),
           ml.max_discoveries(), ml.max_schemas(), ml.max_preload(), ml.max_schema_bytes())
    assert off == (mo.ASK_PROMPT_BUDGET, mo.ASK_HISTORY_RESERVE, mo.ASK_PRELOAD, mo.ASK_STUBS,
                   ml.MAX_DISCOVERIES, ml.MAX_SCHEMAS, ml.MAX_PRELOAD, ml.MAX_SCHEMA_BYTES)
    assert off == (8000, 1200, 6, 24, 2, 24, 12, 32_000)
    gate(True)
    assert (mo.ask_prompt_budget(), mo.ask_history_reserve()) == (24000, 6000)
    assert (mo.ask_preload(), mo.ask_stubs()) == (16, 40)
    assert (ml.max_discoveries(), ml.max_schemas(), ml.max_preload(), ml.max_schema_bytes()) == (4, 40, 16, 64_000)


def test_ask_results_stay_trimmed_except_whole_mail_and_conversations(gate):
    from service import model_only as mo
    for name in ("view_emails", "view_messages", "web_fetch", "get_upcoming"):
        assert mo.ask_result_limit(name) == mo.ASK_TOOL_RESULT_CHARS == 6000
    gate(True)
    assert mo.ask_result_limit("view_emails") == mo.ask_result_limit("view_messages") == 400_000
    assert mo.ask_result_limit("web_fetch") == mo.ask_result_limit("search_notes") == 6000


def test_email_reads_skip_the_digest_only_under_the_gate(gate):
    from service import model_only as mo
    from service.tools import email_digest
    with mo.tool_result_budget(mo.ask_result_limit("view_emails")):
        assert not email_digest.whole_mail_reads() and email_digest.result_budget_chars() == 6000
    gate(True)
    assert not email_digest.whole_mail_reads()          # no caller budget: the digest stays
    with mo.tool_result_budget(mo.ask_result_limit("view_emails")):
        assert email_digest.whole_mail_reads() and email_digest.result_budget_chars() == 380_000
        body = "Reply text.\n\nOn Mon, Dana wrote:\n> " + "earlier thread line\n> " * 2000
        fitted = email_digest.fit_rows([{"body": body}], budget=email_digest.result_budget_chars())
    assert fitted.rows[0]["body"] == body and fitted.cut == 0       # whole, quoted thread kept


# --- Brief, inbox summary, Messages ---------------------------------------------

def test_inbox_summary_reads_whole_bodies_under_the_gate(gate):
    from service.assistant import mail_gists
    body = "Can you confirm the venue? " * 40 + "\n\nOn Tue, Sam wrote:\n> the old plan"
    short = mail_gists._excerpt({"body": body})
    assert len(short) <= mail_gists._EXCERPT_CHARS + 1 and "old plan" not in short
    gate(True)
    whole = mail_gists._excerpt({"body": body})
    assert "the old plan" in whole and len(whole) > 1000
    assert len(mail_gists._excerpt({"body": "x " * 30_000})) <= mail_gists.CLOUD_BODY_CHARS + 1


def test_conversation_rows_is_one_chat_oldest_first(monkeypatch):
    from service.tools import imessage_tools as im
    records = [(30.0, "id-a", "Ana", "Ana: code 123456 is yours", None),
               (10.0, "id-a", "Ana", "Me: hi", None), (20.0, "id-b", "Bo", "Bo: hey", None)]
    monkeypatch.setattr(im, "_parse_records", lambda: records)
    rows = im.conversation_rows("Ana")
    assert [r[0] for r in rows] == [10.0, 30.0] and all(r[1] == "Ana" for r in rows)
    assert im.conversation_rows("Ana", since=15.0)[0][0] == 30.0


def test_view_messages_reads_a_whole_conversation_under_the_gate(gate, monkeypatch):
    from service.tools import imessage_tools as im
    import service.assistant.sync_status as sync_status

    async def nothing(*_a, **_k):
        return None
    monkeypatch.setattr(sync_status, "ensure_sources", nothing)
    records = [(1_700_000_000.0 + i, "id-a", "Ana", f"Ana: message {i}", None) for i in range(120)]
    monkeypatch.setattr(im, "messages_sync_state", lambda: "ready")
    monkeypatch.setattr(im, "_lines", "synthetic")
    monkeypatch.setattr(im, "_parse_records", lambda: records)
    monkeypatch.setattr(im, "_match_conversation", lambda rows, name: (("id-a", "Ana"), None))
    off = asyncio.run(im.view_messages_impl(conversation="Ana"))
    assert "message 0\n" not in off and off.count("Ana: message") == 20
    gate(True)
    from service import model_only as mo

    async def in_ask():
        with mo.tool_result_budget(mo.ask_result_limit("view_messages")):
            return await im.view_messages_impl(conversation="Ana")
    on = asyncio.run(in_ask())
    assert on.count("Ana: message") == 120


# --- background depth -----------------------------------------------------------

def test_mail_memory_and_gmail_depth(gate):
    from service.mail_memory import runner
    from service import gmail_source
    import base64
    long = base64.urlsafe_b64encode(("x" * 60_000).encode()).decode()
    payload = {"mimeType": "text/plain", "body": {"data": long}}
    assert runner.window_days() == runner.WINDOW_DAYS == 90
    assert len(gmail_source.extract_body(payload)) == gmail_source.BODY_CAP == 20000
    gate(True)
    assert runner.window_days() == 200
    assert len(gmail_source.extract_body(payload)) == 60_000


def test_smart_search_reads_the_whole_document_only_under_the_gate(gate):
    from service.search import engine
    from service.search.chunker import Chunk
    chunks = [Chunk(i, "text", i * 10, i * 10 + 10) for i in range(50)]
    assert engine._whole_document_picks("t" * 500, chunks) is None
    gate(True)
    assert engine._whole_document_picks("t" * 500, chunks) == list(range(50))
    assert engine._whole_document_picks("t" * (engine.CLOUD_WHOLE_DOC_CHARS + 1), chunks) is None


def test_mail_history_of_intel_size_is_accepted(monkeypatch):
    from service import main
    from service.tools import email_tools
    for name in ("_history", "_history_at", "_swift_history", "_native_history_at"):
        monkeypatch.setattr(email_tools, name, getattr(email_tools, name))
    monkeypatch.setattr(email_tools, "_persist_composed_sources", lambda: None)
    rows = "\n".join(f"{1_600_000_000 + i} | Sender {i} <s{i}@example.com> | Subject {i} | acct"
                     for i in range(120_000))
    assert asyncio.run(main.assistant_sync_emails({"history": rows})) == {"ok": True}
    assert email_tools._swift_history == rows and len(email_tools._history) >= len(rows)


# --- research storage -----------------------------------------------------------

def test_research_store_retries_a_transient_open(tmp_path, monkeypatch):
    from service.research import store as research_store
    real = sqlite3.connect
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        db = real(*args, **kwargs)
        if len(calls) < 3:
            db.close()
            raise sqlite3.OperationalError("disk I/O error")
        return db
    monkeypatch.setattr(research_store.sqlite3, "connect", flaky)
    monkeypatch.setattr(research_store.time, "sleep", lambda s: None)
    path = tmp_path / "research.db"
    path.write_bytes(b"")
    s = research_store.ResearchStore(path)
    assert len(calls) == 3 and s.list_jobs() == [] and path.exists()
    s.close()


def test_research_store_gives_up_and_keeps_the_file(tmp_path, monkeypatch):
    from service.research import store as research_store

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return broken()
    monkeypatch.setattr(research_store.sqlite3, "connect", counting)
    monkeypatch.setattr(research_store.time, "sleep", lambda s: None)
    path = tmp_path / "research.db"
    path.write_bytes(b"keep")
    with pytest.raises(sqlite3.OperationalError):
        research_store.ResearchStore(path)
    assert path.read_bytes() == b"keep"


def test_research_store_does_not_retry_other_errors(tmp_path, monkeypatch):
    from service.research import store as research_store
    calls = []

    def other(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: x")
    monkeypatch.setattr(research_store.sqlite3, "connect", other)
    with pytest.raises(sqlite3.OperationalError):
        research_store.ResearchStore(tmp_path / "research.db")
    assert len(calls) == 1


def test_research_unavailable_answers_503_and_the_service_runs(monkeypatch):
    from service import main

    def refuse():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(main, "ResearchManager", refuse)
    stand_in = main._open_research()
    monkeypatch.setattr(main, "research_manager", stand_in)
    for call in (main.research_list(), main.research_get("job-1")):
        with pytest.raises(main.HTTPException) as raised:
            asyncio.run(call)
        assert raised.value.status_code == 503
        assert raised.value.detail == main.RESEARCH_STORAGE_UNAVAILABLE


def test_research_reads_twice_the_chunks_in_one_call_per_source(gate):
    from service.research.orchestrator import ResearchManager

    class Store:
        def __init__(self):
            self.chunks = []

        def sources(self, job_id, statuses=None):
            body = "\n\n".join(f"Paragraph {i} about solar panel efficiency and output. " * 12
                               for i in range(30))
            return [{"source_id": "S1", "body": body, "query": "q", "title": "T", "url": "u",
                     "published_at": ""}]

        def event_payloads(self, job_id, kind):
            return [{"query": "q", "subquestion_id": "q1"}]

        def add_chunk(self, *a, **k):
            self.chunks.append(k)

        def update_source(self, *a, **k):
            pass

        def event(self, *a, **k):
            pass

    def run():
        manager = ResearchManager.__new__(ResearchManager)
        manager.store = Store()
        calls = []

        async def checkpoint(job_id):
            return True

        async def call_json(client, system, user, *, max_tokens, job_id=None):
            calls.append(user)
            return []
        manager._checkpoint = checkpoint
        manager._call_json = call_json
        plan = {"depth": "standard", "objective": "solar panel efficiency",
                "subquestions": [{"id": "q1", "question": "How efficient are solar panels?"}]}
        asyncio.run(manager._extract(None, "job", plan))
        return calls, manager.store.chunks

    calls, chunks = run()
    assert len(calls) == len(chunks) == 2               # standard depth: 2 chunks, a call each
    gate(True)
    calls, chunks = run()
    assert len(chunks) == 4 and len(calls) == 1         # twice the chunks, one call
