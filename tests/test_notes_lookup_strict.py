"""Named-topic Notes/memory lookup: strict all-term matching and honest freshness.

User report: a freshly created order Note was not retrieved and an unrelated old
record surfaced. Four mechanisms, each pinned here with synthetic data only:

R1  memory/FTS keyword search compiled 'amazon order' to `amazon OR order`, so a
    passage containing only 'order' won. Strict (opt-in) mode needs EVERY
    significant term; non-contiguous terms still match.
R2  an exact/multi-term miss in search_notes fell back to UNRELATED recent notes
    as if relevant. A miss is now a truthful 'no matching note' (the fallback
    survives only for explicit browse requests like 'recent notes').
R3  the Notes cache refreshed once a day and a lookup accepted a ready cached
    snapshot without a fresh read. A lookup can now request a fresh, correlated
    native read and the result says whether the data is current, stale (with age),
    timed out or unavailable. A publish that STARTED before the request is never
    accepted as fresh.
R4  an empty result is a complete answer for that source and says so, so the model
    does not silently fall through to other private tools.

Nothing here touches the real app, Notes, the network, or user data.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-notes-lookup-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ["HOME"] = _scratch.name

from service.assistant import sync_status  # noqa: E402
from service.tools import cache_store, notes_tools  # noqa: E402

FS, RS = notes_tools._FS, notes_tools._RS
NOW = time.time()


def raw(*notes: tuple[float, str, str, str]) -> str:
    return "".join(f"{ts}{FS}{title}{FS}{folder}{FS}{body}{RS}"
                   for ts, title, folder, body in notes)


OLD_ORDER = (NOW - 90 * 86400, "Presentation order", "Work",
             "Speaker order for the offsite: Dana, then Lee, then Priya.")
RECENT_GROCERY = (NOW - 3600, "Groceries", "Home", "milk, eggs, rice")
RECENT_TRIP = (NOW - 7200, "Trip ideas", "Home", "Lisbon in spring")
NEW_AMAZON = (NOW - 60, "Amazon", "Home",
              "Pending delivery. The standing order was placed Friday for the desk lamp.")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sync_status, "NOTES_REFRESH_TIMEOUT_S", 0.4)
    monkeypatch.setattr(cache_store, "save", lambda name, text: None)
    monkeypatch.setattr(notes_tools, "_notes", "")
    monkeypatch.setattr(notes_tools, "_notes_at", 0.0)
    monkeypatch.setattr(notes_tools, "_available", None)
    monkeypatch.setattr(notes_tools, "_reason", "")
    for name, value in (("_snapshot_at", 0.0), ("_snapshot_started_at", 0.0),
                        ("_received_at", 0.0), ("_receipt_started", 0.0),
                        ("_receipt_available", None),
                        ("_receipt_reason", "")):
        monkeypatch.setattr(notes_tools, name, value, raising=False)


def load(*notes, started=None):
    notes_tools.cache_notes(raw(*notes), snapshot_started_at=started)


def search(**kw) -> str:
    return asyncio.run(notes_tools.search_notes_impl(**kw))


class FakeApp:
    """Plays the Swift app: reacts to a published sync request."""

    def __init__(self, monkeypatch, behaviour):
        from service.assistant.hub import hub
        self.requests: list[float] = []
        self.behaviour = behaviour
        self._sub = hub.subscribe()

        async def publish(event, **kw):
            if event.get("type") == "sync_assistant_sources_now" and "notes" in event["sources"]:
                self.requests.append(time.time())
                await self.behaviour(self)
            return {}

        monkeypatch.setattr(hub, "publish", publish)
        self.hub = hub

    def close(self):
        self.hub.unsubscribe(self._sub)


@pytest.fixture
def app(monkeypatch):
    made: list[FakeApp] = []

    def make(behaviour):
        a = FakeApp(monkeypatch, behaviour)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


# --------------------------------------------------------------------- R2 / R1
def test_strict_miss_returns_truthful_no_match_and_no_unrelated_fallback():
    load(OLD_ORDER, RECENT_GROCERY, RECENT_TRIP)
    out = search(query="amazon order", strict=True)
    assert "Groceries" not in out and "Trip ideas" not in out
    assert "Presentation order" not in out, "a note with only 'order' is not an Amazon order"
    assert "no" in out.lower() and "amazon" in out.lower()
    assert "most recent notes" not in out


def test_default_mode_keeps_the_legacy_recent_notes_fallback():
    load(OLD_ORDER, RECENT_GROCERY)
    out = search(query="amazon order")
    assert "no exact match" in out and "Groceries" in out


def test_non_contiguous_terms_still_match():
    load(OLD_ORDER, NEW_AMAZON, RECENT_GROCERY)
    out = search(query="amazon order", strict=True)
    assert "Title: Amazon" in out and "desk lamp" in out
    assert "Presentation order" not in out and "Groceries" not in out


def test_stopwords_are_dropped_from_the_topic():
    load(OLD_ORDER, NEW_AMAZON)
    out = search(query="what's the order for Amazon", strict=True)
    assert "Title: Amazon" in out and "Presentation order" not in out


def test_terms_may_span_title_folder_and_body():
    load((NOW, "Amazon", "Orders", "lamp"), RECENT_GROCERY)
    assert "Title: Amazon" in search(query="amazon orders lamp", strict=True)


def test_strict_does_not_match_a_term_inside_another_word():
    load((NOW, "Border notes", "Home", "crossing the border"), RECENT_GROCERY)
    assert "Border notes" not in search(query="order", strict=True)


def test_vague_browse_request_still_lists_recent_notes():
    load(OLD_ORDER, RECENT_GROCERY, RECENT_TRIP)
    out = search(query="recent notes", strict=True)
    assert "Groceries" in out and "Trip ideas" in out


def test_only_stopword_query_is_a_phrase_lookup_not_a_browse():
    load(RECENT_GROCERY)
    assert "Groceries" not in search(query="the of", strict=True)


# ------------------------------------------------------------------------ R3
def test_fresh_read_finds_a_note_created_after_the_last_snapshot(app):
    load(OLD_ORDER, RECENT_GROCERY, started=NOW - 86400)

    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER, RECENT_GROCERY, NEW_AMAZON),
                                snapshot_started_at=time.time())

    fake = app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert len(fake.requests) == 1, "a lookup must ask the app for a fresh read"
    assert "Title: Amazon" in out
    assert "may be stale" not in out.lower()


def test_old_in_flight_publish_is_not_accepted_as_fresh(app):
    started_before = NOW - 30

    async def behaviour(a):
        # the read that was already running when the request arrived finishes now
        notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=started_before)

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=0.4))
    assert result["status"] != "fresh"
    assert result["status"] in {"stale", "timeout"}


def test_unstamped_publish_is_never_proof_of_freshness(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER))

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=0.3))["status"] != "fresh"


def test_in_flight_publish_then_real_one_is_accepted(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=NOW - 30)

        async def later():
            await asyncio.sleep(0.15)
            notes_tools.cache_notes(raw(OLD_ORDER, NEW_AMAZON),
                                    snapshot_started_at=time.time())

        a.task = asyncio.get_running_loop().create_task(later())

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=2.0))
    assert result["status"] == "fresh"


def test_stale_result_reports_age_and_refuses_to_claim_absence(app):
    load(OLD_ORDER, RECENT_GROCERY, started=time.time() - 3 * 3600)
    notes_tools._snapshot_at = time.time() - 3 * 3600

    async def behaviour(a):
        return None  # the app never answers

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    low = out.lower()
    assert "3 hours" in low
    assert "can't confirm" in low or "cannot confirm" in low
    assert "groceries" not in low


def test_stale_hit_is_labelled_with_its_age(app):
    load(NEW_AMAZON, started=time.time() - 2 * 3600)
    notes_tools._snapshot_at = time.time() - 2 * 3600

    async def behaviour(a):
        return None

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "Title: Amazon" in out and "2 hours" in out


def test_timeout_without_any_snapshot_does_not_say_no_notes(app):
    async def behaviour(a):
        return None

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "no note" not in out.lower().replace("no notes found", "")
    assert "could not" in out.lower() or "couldn't" in out.lower()


def test_unavailable_receipt_is_reported_not_treated_as_empty(app):
    load(OLD_ORDER, started=NOW - 86400)

    async def behaviour(a):
        notes_tools.cache_notes("", available=False, reason="Notes access was denied.",
                                snapshot_started_at=time.time())

    app(behaviour)
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=1.0))
    assert result["status"] == "unavailable"
    out = search(query="amazon order", strict=True, refresh=True)
    assert "Notes access was denied" in out
    assert "no note" not in out.lower()


def test_no_connected_app_is_unavailable_immediately(monkeypatch):
    from service.assistant.hub import hub
    monkeypatch.setattr(hub, "_subs", set())
    t0 = time.monotonic()
    result = asyncio.run(sync_status.refresh_notes(timeout_seconds=5.0))
    assert time.monotonic() - t0 < 1.0
    assert result["status"] == "unavailable"


def test_older_snapshot_arriving_late_does_not_replace_a_newer_one():
    load(NEW_AMAZON, started=NOW)
    notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=NOW - 500)
    assert "Title: Amazon" in search(query="amazon", strict=True)


def test_fresh_miss_is_a_complete_answer_that_forbids_fallthrough(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER, RECENT_GROCERY),
                                snapshot_started_at=time.time())

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "just now" in out.lower() or "current" in out.lower()
    # scope the claim to what was searched; never order the model off other sources
    assert "do not search" not in out.lower() and "unless the user asks" not in out.lower()
    assert "only" in out.lower() and "notes" in out.lower()


def test_default_search_does_not_request_a_refresh(monkeypatch):
    load(NEW_AMAZON)
    from service.assistant.hub import hub

    async def boom(event, **kw):  # pragma: no cover - must not be hit
        raise AssertionError("refresh requested")

    monkeypatch.setattr(hub, "publish", boom)
    monkeypatch.setattr(notes_tools, "_available", True)
    assert "Title: Amazon" in search(query="amazon")


# ---------------------------------------------------------------- R1: memory
@pytest.fixture
def world(tmp_path, monkeypatch):
    from service.memory.facts import FactStore
    from service.memory.store import SessionStore
    facts = FactStore(tmp_path / "facts.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    import sys
    # `service.memory.store` the module is shadowed by the package attribute `store`.
    monkeypatch.setattr(sys.modules["service.memory.store"], "store", sessions)
    yield facts, sessions
    facts._db.close()
    sessions._db.close()


def _say(sessions, text, sid=None):
    sid = sid or sessions.create_session()
    sessions.add_turn(sid, "user", text)
    return sid


def test_memory_default_retrieval_is_still_or(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "Presentation order matters for the offsite.")
    texts = [p["text"] for p in retrieve("amazon order", facts=facts, sessions=sessions)["passages"]]
    assert texts, "legacy OR behaviour must be unchanged unless strict is requested"


def test_memory_strict_requires_every_term(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "Presentation order matters for the offsite.")
    _say(sessions, "My Amazon delivery will arrive in the late afternoon, and the order is the desk lamp.")
    result = retrieve("amazon order", facts=facts, sessions=sessions, strict=True)
    texts = [p["text"] for p in result["passages"]]
    assert len(texts) == 1 and "Amazon" in texts[0]


def test_memory_strict_filters_facts_too(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    facts.add("My presentation order is Dana then Lee.", "fact")
    facts.add("My Amazon order is a desk lamp.", "fact")
    got = [f["text"] for f in retrieve("amazon order", facts=facts, sessions=sessions,
                                       strict=True, include_passages=False)["facts"]]
    assert got == ["My Amazon order is a desk lamp."]


def test_memory_strict_drops_stopwords_and_keeps_non_contiguous(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "The Amazon thing I need to track is the order for the lamp.")
    got = retrieve("what's the order for Amazon", facts=facts, sessions=sessions, strict=True)
    assert len(got["passages"]) == 1


def test_recall_tool_is_strict_and_states_the_miss_is_complete(world, monkeypatch):
    from service.tools import memory_tools
    facts, sessions = world
    monkeypatch.setattr(memory_tools, "store", facts)
    _say(sessions, "Presentation order matters for the offsite.")
    out = asyncio.run(memory_tools.recall("amazon order"))
    assert "Presentation order" not in out
    assert out.startswith("No matching memory")
    assert "do not search" not in out.lower() and "unless the user asks" not in out.lower()
    assert "Notes and Mail were not searched" in out


def test_search_conversations_tool_is_strict(world, monkeypatch):
    from service.tools import memory_tools
    facts, sessions = world
    monkeypatch.setattr(memory_tools, "store", facts)
    _say(sessions, "Presentation order matters for the offsite.")
    out = asyncio.run(memory_tools.search_conversations("amazon order"))
    assert "Presentation order" not in out and out.startswith("No matching")


# ------------------------------------------------------- native reader contract
def test_native_notes_reader_stamps_each_read_and_queues_a_request_behind_one_in_flight():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/Sources/WispApp/NotesReader.swift").read_text()
    # the backend can only trust a post whose read began after its request
    assert '"snapshot_started_at": snapshotStartedAt' in src
    assert "Date().timeIntervalSince1970" in src
    # a request during a running read is queued, not dropped, so a post that
    # started after it exists to be accepted; warm-up/daily reads stay coalesced
    assert "rerunRequested = true" in src and "func syncIfIdle()" in src


# --------------------------------------------------- the HTTP endpoint carries the stamp
def _post_notes(body):
    from fastapi.testclient import TestClient
    import service.main as main
    return TestClient(main.app).post("/assistant/sync/notes", json=body)


def test_endpoint_passes_the_read_start_stamp_to_the_cache():
    response = _post_notes({"raw": raw(OLD_ORDER),
                            "diagnostics": {"available": True, "snapshot_started_at": NOW - 5}})
    assert response.status_code == 200
    assert notes_tools._receipt_started == pytest.approx(NOW - 5)


def test_endpoint_without_a_stamp_is_still_accepted_but_never_fresh(app):
    async def behaviour(a):
        await asyncio.to_thread(_post_notes, {"raw": raw(OLD_ORDER),
                                              "diagnostics": {"available": True}})

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=0.3))["status"] != "fresh"


def test_freshness_works_end_to_end_through_the_endpoint(app):
    async def behaviour(a):
        await asyncio.to_thread(
            _post_notes, {"raw": raw(OLD_ORDER, NEW_AMAZON),
                          "diagnostics": {"available": True,
                                          "snapshot_started_at": time.time()}})

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=3.0))["status"] == "fresh"


# ============================================================ audit round 2
import math  # noqa: E402


@pytest.mark.parametrize("bogus", [1e308, float("inf"), float("nan"), -5.0,
                                   time.time() + 1e9, 10 ** 400, "later", True])
def test_bogus_start_stamp_cannot_freeze_the_cache(bogus):
    load(OLD_ORDER, started=NOW - 100)
    notes_tools.cache_notes(raw(RECENT_GROCERY), snapshot_started_at=bogus)  # must not raise
    before = notes_tools.notes_receipt()["started_at"]
    assert math.isfinite(before) and before <= time.time() + 61
    notes_tools.cache_notes(raw(NEW_AMAZON), snapshot_started_at=time.time())
    assert "Title: Amazon" in search(query="amazon", strict=True), "later real publish was dropped"


def test_bogus_stamp_is_data_but_never_proof_of_freshness(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(NEW_AMAZON), snapshot_started_at=1e308)

    app(behaviour)
    assert asyncio.run(sync_status.refresh_notes(timeout_seconds=0.3))["status"] != "fresh"
    assert "Title: Amazon" in search(query="amazon", strict=True)


def test_endpoint_survives_bogus_stamps():
    from fastapi.testclient import TestClient
    from service.main import app as fastapi_app
    client = TestClient(fastapi_app)
    for bogus in (1e308, 10 ** 400, "x", None):
        r = client.post("/assistant/sync/notes",
                        json={"raw": raw(NEW_AMAZON),
                              "diagnostics": {"available": True, "snapshot_started_at": bogus}})
        assert r.status_code == 200


def _crowd(sessions, n_order, n_amazon):
    for i in range(n_order):
        _say(sessions, f"the order of things {i} and another order list order")
    for i in range(n_amazon):
        _say(sessions, f"amazon amazon amazon parcel {i}")


def test_memory_strict_finds_match_hidden_behind_a_large_or_pool(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _crowd(sessions, 300, 80)
    _say(sessions, " ".join(f"filler{j}" for j in range(200)) + " my amazon order is a desk lamp")
    got = retrieve("amazon order", facts=facts, sessions=sessions, strict=True)
    assert any("desk lamp" in p["text"] for p in got["passages"])


def test_memory_strict_facts_not_hidden_behind_a_large_or_pool(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    for i in range(120):
        facts.add(f"The order of presentation number {i} is fixed.", "fact")
        facts.add(f"Amazon amazon parcel number {i} arrived.", "fact")
    facts.add("My Amazon order is a desk lamp.", "fact")
    got = retrieve("amazon order", facts=facts, sessions=sessions, strict=True,
                   include_passages=False)
    assert [f["text"] for f in got["facts"]] == ["My Amazon order is a desk lamp."]


INFLECTIONS = [("families", "family"), ("family", "families"), ("ordering", "ordered"),
               ("ordered", "ordering"), ("boxes", "box"), ("box", "boxes"),
               ("running", "run"), ("run", "running"), ("story", "stories"),
               ("stories", "story"), ("orders", "order"), ("order", "orders")]
NEGATIVES = [("order", "border"), ("ord", "order"), ("order", "ordinary"),
             ("amaz", "amazon"), ("cat", "category")]


@pytest.mark.parametrize("term,word", INFLECTIONS)
def test_inflections_match_in_notes(term, word):
    load((NOW, "T", "F", f"something about {word} today"))
    assert "Title: T" in search(query=term, strict=True)


@pytest.mark.parametrize("term,word", NEGATIVES)
def test_short_prefix_and_substring_overmatch_is_rejected_in_notes(term, word):
    load((NOW, "T", "F", f"something about {word} today"))
    assert "Title: T" not in search(query=term, strict=True)


@pytest.mark.parametrize("term,word", INFLECTIONS)
def test_inflections_match_in_memory(term, word, world):
    from service.memory.retrieval import retrieve, all_terms_match
    assert all_terms_match(term, f"something about {word} today")
    facts, sessions = world
    _say(sessions, f"remember my thing about {word} today")
    got = retrieve(f"{term} thing", facts=facts, sessions=sessions, strict=True)
    assert len(got["passages"]) == 1


@pytest.mark.parametrize("term,word", NEGATIVES)
def test_overmatch_is_rejected_in_memory(term, word):
    from service.memory.retrieval import all_terms_match
    assert not all_terms_match(term, f"something about {word} today")


def test_complete_answer_mentions_the_500_note_read_cap(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw(OLD_ORDER, RECENT_GROCERY), snapshot_started_at=time.time())

    app(behaviour)
    assert "at most 500" in search(query="amazon order", strict=True, refresh=True)


def test_zero_notes_read_does_not_claim_no_note_matches(app):
    async def behaviour(a):
        notes_tools.cache_notes("", snapshot_started_at=time.time())

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True)
    assert "No note matches" not in out and "0 notes" not in out
    assert "no notes" in out.lower()


@pytest.mark.parametrize("q", ["to do list", "find my list", "show list"])
def test_list_words_are_a_lookup_not_an_unfiltered_browse(q):
    load((NOW, "Groceries", "Home", "milk"), (NOW - 5, "To do list", "Home", "call dentist"))
    out = search(query=q, strict=True)
    assert "Groceries" not in out and "call dentist" in out


def test_cjk_terms_keep_substring_matching():
    load((NOW, "订单", "", "我的订单号 12345"), RECENT_GROCERY)
    assert "我的订单号" in search(query="订单", strict=True)
    assert "我的订单号" in search(query="订单号", strict=True)


# ============================================================ audit round 3
@pytest.mark.parametrize("q", ["what is", "???", "the", "what do i", "  ?  "])
def test_term_less_strict_queries_never_raise(q, world, monkeypatch):
    from service.memory.retrieval import retrieve
    from service.tools import memory_tools
    facts, sessions = world
    monkeypatch.setattr(memory_tools, "store", facts)
    _say(sessions, "Something the user said once.")
    got = retrieve(q, facts=facts, sessions=sessions, strict=True)
    assert got["facts"] == [] and got["passages"] == []
    for out in (asyncio.run(memory_tools.recall(q)),
                asyncio.run(memory_tools.search_conversations(q))):
        assert "significant search terms" in out.lower()


def test_term_less_strict_query_survives_the_no_fts_fallback(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "Something the user said once.")
    sessions._db.execute("DROP TRIGGER IF EXISTS memory_turn_insert")
    sessions._db.execute("DROP TABLE memory_turn_fts")
    assert retrieve("what is", facts=facts, sessions=sessions, strict=True)["passages"] == []


@pytest.mark.parametrize("q", ["what's in my notes?", "whats in my notes", "show recent notes",
                               "my notes?", "what's in my notes", "recent notes"])
def test_vague_browse_phrasings_are_still_browses(q):
    assert notes_tools.is_browse_query(q)
    load(OLD_ORDER, RECENT_GROCERY, RECENT_TRIP)
    out = search(query=q, strict=True)
    assert "Groceries" in out and "Trip ideas" in out and "found nothing" not in out


@pytest.mark.parametrize("q", ["to do list", "find my list", "show list", "amazon order"])
def test_topic_phrasings_are_not_browses(q):
    assert not notes_tools.is_browse_query(q)


def test_memory_strict_rejects_a_porter_stem_collision(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "general election results were announced")
    assert retrieve("generate election", facts=facts, sessions=sessions, strict=True)["passages"] == []
    assert retrieve("general election", facts=facts, sessions=sessions, strict=True)["passages"]
    facts.add("The university opens in May.", "fact")
    assert retrieve("universe", facts=facts, sessions=sessions, strict=True,
                    include_passages=False)["facts"] == []
    _say(sessions, "the latest new thing")
    assert retrieve("news", facts=facts, sessions=sessions, strict=True)["passages"] == []


SAME = [("use", "used"), ("use", "using"), ("used", "using"), ("study", "studied"),
        ("copy", "copied"), ("reply", "replied"), ("try", "tried"), ("tie", "tied"),
        ("tie", "tying"), ("travel", "travelling"), ("travel", "traveled"),
        ("movie", "movies"), ("cookie", "cookies"), ("agree", "agreed"),
        ("deliver", "delivery"), ("pay", "paid"), ("child", "children"), ("wife", "wives"),
        ("families", "family"), ("ordering", "ordered"), ("boxes", "box"),
        ("running", "run"), ("story", "stories"), ("house", "houses"), ("make", "making"),
        ("stop", "stopped"), ("potato", "potatoes"), ("class", "classes"), ("bus", "buses"),
        ("toy", "toys"), ("stay", "stayed"), ("plan", "planning")]
DIFFERENT = [("order", "border"), ("order", "ordinary"), ("order", "orderly"), ("ord", "order"),
             ("amaz", "amazon"), ("cat", "category"), ("car", "care"), ("plan", "plane"),
             ("tim", "time"), ("can", "cane"), ("hat", "hate"), ("win", "wine"),
             ("wedding", "wed"), ("evening", "even"), ("news", "new"), ("bed", "red"),
             ("red", "shed"), ("king", "ring"), ("thing", "string"), ("is", "as"),
             ("glass", "class"), ("car", "cares"), ("fee", "feed"), ("ne", "need")]


@pytest.mark.parametrize("a,b", SAME)
def test_regular_inflections_meet(a, b):
    assert notes_tools.all_terms_match([a], b) and notes_tools.all_terms_match([b], a)


@pytest.mark.parametrize("a,b", DIFFERENT)
def test_look_alike_words_do_not_meet(a, b):
    assert not notes_tools.all_terms_match([a], b) and not notes_tools.all_terms_match([b], a)


def test_no_fts_fallback_prefilter_keeps_inflections(world):
    from service.memory.retrieval import retrieve
    facts, sessions = world
    _say(sessions, "my families and the way they are ordering stories")
    sessions._db.execute("DROP TRIGGER IF EXISTS memory_turn_insert")
    sessions._db.execute("DROP TABLE memory_turn_fts")
    got = retrieve("family order story", facts=facts, sessions=sessions, strict=True)
    assert len(got["passages"]) == 1
    assert retrieve("family border", facts=facts, sessions=sessions, strict=True)["passages"] == []


@pytest.mark.parametrize("offset", [30, 59])
def test_stamp_beyond_a_few_seconds_ahead_is_unstamped(offset):
    notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=time.time() + offset)
    assert notes_tools.notes_receipt()["started_at"] == 0.0
    notes_tools.cache_notes(raw(NEW_AMAZON), snapshot_started_at=time.time())
    assert "Title: Amazon" in search(query="amazon", strict=True)


@pytest.mark.parametrize("old", [1.0, 100.0, 1e-300, time.time() - 3 * 86400])
def test_ancient_stamp_is_unstamped(old):
    notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=old)
    assert notes_tools.notes_receipt()["started_at"] == 0.0
    assert (notes_tools.snapshot_age_seconds() or 0) < 100


def test_a_slightly_future_stamp_from_clock_skew_is_accepted():
    notes_tools.cache_notes(raw(OLD_ORDER), snapshot_started_at=time.time() + 2)
    assert notes_tools.notes_receipt()["started_at"] > 0


def test_tool_description_states_the_500_note_cap():
    from service.tools.registry import get_tool
    text = get_tool("search_notes").description
    assert "100 most recent" not in text and "500" in text


def test_scoped_miss_reports_filtered_and_total_counts(app):
    async def behaviour(a):
        notes_tools.cache_notes(raw((NOW - 60, "Today note", "", "hello"), OLD_ORDER, RECENT_TRIP),
                                snapshot_started_at=time.time())

    app(behaviour)
    out = search(query="amazon order", strict=True, refresh=True, day="today")
    assert "of 3" in out and "today" in out.lower()
