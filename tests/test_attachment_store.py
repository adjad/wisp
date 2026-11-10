import os
import stat

import pytest

from service import attachment_store as store

D1 = "a" * 64
D2 = "b" * 64
D3 = "c" * 64


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_HOME", str(tmp_path))
    return tmp_path


def _db(home):
    return home / "cache" / "attachment_text.db"


def put(d=D1, text="hello world", **kw):
    kw.setdefault("kind", "pdf")
    kw.setdefault("cut", False)
    return store.put(d, text=text, **kw)


def test_digest_and_round_trip():
    assert store.digest(b"x") == (
        "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    )
    assert put(note="n", how="text", account="A@x.com")
    got = store.get(D1)
    assert (got.kind, got.text, got.cut, got.note, got.how) == (
        "pdf", "hello world", False, "n", "text",
    )
    assert store.get(D2) is None


def test_bad_input_never_raises():
    assert store.get("nope") is None
    assert store.get(None) is None
    assert not store.put("X", kind="pdf", text="t", cut=False)
    assert not put(text="   \n\t")
    assert not put(text="")
    assert store.locate("") is None
    assert store.locate("x" * 301) is None
    assert not store.remember("", D1)
    assert not store.remember("k", "short")
    assert not store.remember("x" * 301, D1)


def test_version_invalidation(monkeypatch):
    put()
    monkeypatch.setattr(store, "VERSION", 2)
    assert store.get(D1) is None
    monkeypatch.setattr(store, "VERSION", 1)
    assert store.get(D1) is None  # the stale row was deleted


def test_truncation(monkeypatch):
    monkeypatch.setattr(store, "MAX_TEXT_CHARS", 10)
    assert put(text="abcdefghijklmnop")
    got = store.get(D1)
    assert got.text == "abcdefghij" and got.cut is True


def test_two_owners_last_one_removes_text():
    put(account="One@X.com")
    put(account="two@x.com")
    assert store.forget_account("one@x.com") == 0
    assert store.get(D1) is not None
    assert store.forget_account("two@x.com") == 1
    assert store.get(D1) is None


def test_forget_account_case_and_whitespace():
    put(account="Person@Example.COM")
    assert store.forget_account("  person@example.com ") == 1
    assert store.get(D1) is None


def test_unknown_account_only_forget_all():
    put(account="")
    assert store.forget_account("") == 0
    assert store.get(D1) is not None
    assert store.forget_all() == 1
    assert store.get(D1) is None


def test_locator_round_trip_and_removal():
    put(account="a@x.com")
    assert store.remember("loc-1", D1, account="A@x.com")
    assert store.locate("loc-1") == D1
    assert store.locate("other") is None
    store.forget_account("a@x.com")
    assert store.locate("loc-1") is None
    assert store.remember("loc-2", D2)
    store.forget_all()
    assert store.locate("loc-2") is None


def test_prune_by_age(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: t[0])
    put(D1)
    store.remember("l", D1)
    t[0] += 10
    put(D2)
    assert store.prune(t[0] + store.MAX_AGE_S - 5) == 1  # only D1 is stale
    assert store.get(D1) is None and store.get(D2) is not None
    assert store.locate("l") is None


def test_prune_by_size_least_recent_first(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: t[0])
    for d in (D1, D2, D3):
        t[0] += 1
        put(d, text="x" * 100)
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 250)
    assert store.prune(t[0]) == 1
    assert store.get(D1) is None
    assert store.get(D2) is not None and store.get(D3) is not None


def test_put_prunes_at_most_hourly(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: t[0])
    calls = []
    real = store.prune
    monkeypatch.setattr(store, "prune", lambda now=None: calls.append(now) or real(now))
    put(D1)
    t[0] += 60
    put(D2)
    assert len(calls) == 1
    t[0] += 3600
    put(D3)
    assert len(calls) == 2


def test_permissions(home):
    put()
    assert stat.S_IMODE(os.stat(_db(home)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(_db(home).parent).st_mode) == 0o700


def test_symlinked_path_refused(home):
    (home / "cache").mkdir()
    target = home / "elsewhere.db"
    target.write_bytes(b"")
    os.symlink(target, _db(home))
    assert not put()
    assert store.get(D1) is None
    assert target.read_bytes() == b""


def test_symlinked_directory_refused(home):
    real = home / "real"
    real.mkdir()
    os.symlink(real, home / "cache")
    assert not put()
    assert list(real.iterdir()) == []


def test_corrupt_db_returns_nothing(home):
    (home / "cache").mkdir()
    _db(home).write_bytes(b"this is not a sqlite database" * 200)
    assert store.get(D1) is None
    assert not put()
    assert store.locate("x") is None
    assert not store.remember("x", D1)
    assert store.forget_account("a") == 0
    assert store.forget_all() == 0
    assert store.prune() == 0


def test_erased_text_not_in_raw_bytes(home):
    marker = "ZEBRA-MARKER-7731-" + "q" * 400
    put(D1, text=marker, account="a@x.com")
    put(D2, text="keep me", account="b@x.com")
    assert marker.encode() in _db(home).read_bytes()
    store.forget_all()
    raw = b"".join(p.read_bytes() for p in _db(home).parent.iterdir())
    assert b"ZEBRA-MARKER-7731" not in raw
    assert b"keep me" not in raw


def test_forget_account_scrubs_bytes(home):
    put(D1, text="SECRET-ACCOUNT-TEXT-91", account="a@x.com")
    put(D2, text="other", account="b@x.com")
    store.forget_account("a@x.com")
    raw = _db(home).read_bytes()
    assert b"SECRET-ACCOUNT-TEXT-91" not in raw
    assert store.get(D2) is not None


def _clock(monkeypatch, start=1_000_000.0):
    t = [start]
    monkeypatch.setattr(store.time, "time", lambda: t[0])
    return t


def test_get_refuses_and_deletes_stale_row_without_any_prune(monkeypatch, home):
    t = _clock(monkeypatch)
    put(D1, text="STALE-TEXT-5521", account="a@x.com")
    store.remember("loc", D1, account="a@x.com")
    # Stop the opportunistic prune so only get's own age check can act.
    monkeypatch.setattr(store, "prune", lambda now=None: 0)
    t[0] += store.MAX_AGE_S + 5
    assert store.get(D1) is None
    assert store.locate("loc") is None
    t[0] += 1
    assert store.get(D1) is None
    import sqlite3
    with sqlite3.connect(str(_db(home))) as c:
        assert c.execute("SELECT COUNT(*) FROM texts").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM owners").fetchone()[0] == 0
    assert b"STALE-TEXT-5521" not in _db(home).read_bytes()


def test_get_within_age_still_serves_and_refreshes(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)
    t[0] += store.MAX_AGE_S - 10
    assert store.get(D1) is not None  # touches last_used
    t[0] += store.MAX_AGE_S - 10
    assert store.get(D1) is not None


def test_get_and_locate_never_prune(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)
    store.remember("l", D1)

    def boom(now=None):
        raise AssertionError("a read must not prune")

    monkeypatch.setattr(store, "prune", boom)
    monkeypatch.setattr(store, "prune_if_due", boom)
    for _ in range(3):
        t[0] += 7200  # an hourly prune would be due every time
        assert store.get(D1) is not None
        assert store.locate("l") == D1


def test_failed_prune_does_not_consume_the_hour(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)  # its prune succeeds and records the hour
    t[0] += store.MAX_AGE_S + 100
    real_connect = store._connect
    n = [0]

    def flaky():
        n[0] += 1
        return None if n[0] == 2 else real_connect()  # 1: claim, 2: the prune

    monkeypatch.setattr(store, "_connect", flaky)
    assert store.prune_if_due() == 0  # the prune failed
    monkeypatch.setattr(store, "_connect", real_connect)
    t[0] += 1  # no hour wait: the failure released the claim
    assert store.prune_if_due() == 1


def test_running_prune_blocks_a_second_claim_until_the_lease_ends(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)
    t[0] += store._PRUNE_EVERY_S + 1
    monkeypatch.setattr(store, "prune", lambda now=None: 0)  # claimer "still pruning"
    assert store.prune_if_due() == 0
    calls = []
    monkeypatch.setattr(store, "prune", lambda now=None: calls.append(now) or 0)
    t[0] += 5
    assert store.prune_if_due() == 0 and calls == []  # lease held
    t[0] += store._LEASE_S
    store.prune_if_due()
    assert len(calls) == 1  # a dead pruner's lease expires


def test_put_still_prunes_when_due(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)
    t[0] += store.MAX_AGE_S + 100
    put(D2)  # due: put prunes the stale D1
    assert store.get(D1) is None
    assert store.get(D2) is not None


def test_prune_if_due_from_a_thread(monkeypatch):
    import threading

    t = _clock(monkeypatch)
    put(D1)
    t[0] += store.MAX_AGE_S + 100
    out = []
    th = threading.Thread(target=lambda: out.append(store.prune_if_due()))
    th.start()
    th.join(10)
    assert out == [1]


def test_lifespan_does_not_await_the_prune():
    import inspect

    from service import main

    src = inspect.getsource(main.lifespan)
    helper = src.index("async def _prune_attachment_store")
    # The only call sits inside the background helper, which is started, not awaited.
    assert src.count("prune_if_due") == 1 and src.index("prune_if_due") > helper
    assert "create_task(_prune_attachment_store())" in src
    assert "attachment_prune_task.cancel()" in src


def test_prune_if_due_runs_once_per_hour_and_ages_out(monkeypatch):
    t = _clock(monkeypatch)
    put(D1)
    t[0] += store.MAX_AGE_S + 100
    assert store.prune_if_due() == 1  # an hour passed since put's prune claim
    assert store.prune_if_due() == 0  # not due again yet
    t[0] += 3600
    assert store.prune_if_due() == 0


def test_stale_row_deleted_by_get_is_vacuumed_by_next_prune(monkeypatch, home):
    t = _clock(monkeypatch)
    put(D1, text="VAC-" + "z" * 5000)
    t[0] += store.MAX_AGE_S + 5
    size_before = _db(home).stat().st_size
    real = store.prune
    monkeypatch.setattr(store, "prune", lambda now=None: 0)  # the get is not an hourly one
    assert store.get(D1) is None
    assert _db(home).stat().st_size == size_before  # no vacuum on a read
    monkeypatch.setattr(store, "prune", real)
    store.prune()
    assert _db(home).stat().st_size < size_before


def test_unknown_owner_does_not_keep_text_after_known_owner_erased():
    put(D1, account="x@x.com")
    put(D1, account="")
    store.remember("loc", D1, account="")
    assert store.forget_account("x@x.com") == 1
    assert store.get(D1) is None
    assert store.locate("loc") is None


def test_shared_known_owners_keep_text_until_both_go():
    put(D1, account="x@x.com")
    put(D1, account="y@x.com")
    put(D1, account="")
    assert store.forget_account("x@x.com") == 0
    assert store.get(D1) is not None
    assert store.forget_account("y@x.com") == 1
    assert store.get(D1) is None


def test_only_unknown_owner_untouched_by_forget_account(monkeypatch):
    t = _clock(monkeypatch)
    put(D1, account="")
    put(D2, account="x@x.com")
    assert store.forget_account("x@x.com") == 1
    assert store.forget_account("") == 0
    assert store.get(D1) is not None
    t[0] += store.MAX_AGE_S + 5
    assert store.get(D1) is None  # ages out
    put(D3, account="")
    assert store.forget_all() == 1
    assert store.get(D3) is None


def test_every_connection_has_secure_delete(monkeypatch):
    seen = []
    real = store.sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            seen.append(sql)
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(store.sqlite3, "connect", lambda *a, **k: Spy(real(*a, **k)))
    put(D1, account="a@x.com")
    store.forget_all()
    assert seen.count("PRAGMA secure_delete=ON") >= 2


async def test_forget_facts_erases_attachment_text_even_if_facts_erase_raises(monkeypatch):
    from service.mail_memory import api, profile_facts, runner

    put(D1, text="ATTACH-TEXT", account="a@x.com")

    def boom(*a, **k):
        raise RuntimeError("facts failed")

    async def quiet():
        return None

    monkeypatch.setattr(profile_facts, "forget_all", boom)
    monkeypatch.setattr(runner, "set_facts", lambda v: None)
    monkeypatch.setattr(api, "_changed", quiet)
    with pytest.raises(RuntimeError, match="facts failed"):
        await api.forget_facts(api.Nothing())
    assert store.get(D1) is None


# ---------------------------------------------------------------------------
# note_seen, search, count, facts, tried
# ---------------------------------------------------------------------------

import json  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402


def seen(d=D1, **kw):
    kw.setdefault("source", "mail")
    return store.note_seen(d, **kw)


def _rows(home, table):
    with sqlite3.connect(str(_db(home))) as c:
        return c.execute(f"SELECT * FROM {table}").fetchall()


def _raw(home):
    return b"".join(p.read_bytes() for p in sorted(_db(home).parent.iterdir())).lower()


def test_note_seen_refused_without_text_or_with_bad_source():
    assert not seen()  # no text row yet
    put()
    assert seen(name="a.pdf")
    assert not seen(source="sms")
    assert not seen(source="")
    assert not store.note_seen("nope", source="mail")
    assert not store.note_seen(None, source="mail")
    for src in ("mail", "gmail", "messages"):
        assert seen(source=src, name=src)


def test_note_seen_cleans_and_cuts(home):
    put()
    bidi = "‮exe.fdp‬"
    assert seen(
        name="in\x00voice\n" + bidi + "x" * 300,
        sender="Bo\x07b ⁦<b@x.com>⁩" + "y" * 300,
        subject="Sub\tject\r\n" + "z" * 400,
        account=" A@X.com ",
    )
    (row,) = _rows(home, "seen")
    _id, dig, src, acct, name, sender, subject, ts = row
    assert acct == "a@x.com"
    assert len(name) == 200 and len(sender) == 200 and len(subject) == 300
    for s in (name, sender, subject):
        assert all(c.isprintable() for c in s)
        assert "‮" not in s and "⁦" not in s
    assert name.startswith("invoice exe.fdp")
    assert subject.startswith("Sub ject")


def test_note_seen_keeps_newest_eight_and_updates_same_row(home):
    put()
    for i in range(12):
        assert seen(name=f"f{i}", ts=100.0 + i)
    names = sorted(r[4] for r in _rows(home, "seen"))
    assert len(names) == 8
    assert names == sorted(f"f{i}" for i in range(4, 12))
    assert seen(name="f11", ts=500.0)  # same row: updated, not duplicated
    assert len(_rows(home, "seen")) == 8
    assert seen(name="f0", ts=1.0)  # older than all eight: itself trimmed
    assert "f0" not in [r[4] for r in _rows(home, "seen")]


def test_note_seen_bad_ts_is_zero(home):
    put()
    for bad in (float("nan"), float("inf"), -5, "x", None):
        assert seen(name=str(bad), ts=bad)
    assert {r[7] for r in _rows(home, "seen")} == {0.0}


def test_search_finds_by_words():
    put(D1, text="The lease ends on 31 August 2027 for the flat.")
    put(D2, text="Quarterly invoice from Acme, total 240.00")
    assert [h.digest for h in store.search("lease august")] == [D1]
    assert [h.digest for h in store.search("ACME invoice")] == [D2]
    assert store.search("") == []
    assert store.search("a") == []  # one-letter words are not words
    assert store.search(None) == []
    assert store.search("nothing here at all zzzz") == []


def test_search_unseen_text_has_empty_fields():
    put(D1, text="lease terms", account="A@x.com")
    (hit,) = store.search("lease")
    assert (hit.name, hit.sender, hit.subject, hit.ts, hit.source) == ("", "", "", 0.0, "")
    assert hit.account == "a@x.com"
    assert hit.all_words is True and hit.kind == "pdf" and hit.cut is False


def test_search_all_words_first_then_any():
    put(D1, text="alpha beta gamma")
    put(D2, text="alpha only here")
    hits = store.search("alpha beta")
    assert [h.digest for h in hits] == [D1] and hits[0].all_words
    hits = store.search("alpha zzzzz")
    assert {h.digest for h in hits} == {D1, D2}
    assert not any(h.all_words for h in hits)


def test_search_matches_file_name_and_text_together():
    put(D1, text="the lease runs to 2027")
    seen(D1, name="Flat-Agreement.pdf", sender="Land Lord <l@x.com>", subject="Sign this", ts=50.0)
    put(D2, text="nothing relevant")
    seen(D2, name="agreement-copy.pdf", ts=10.0)
    hits = store.search("agreement lease")
    assert [h.digest for h in hits] == [D1] and hits[0].all_words
    assert hits[0].name == "Flat-Agreement.pdf" and hits[0].sender.startswith("Land")
    assert hits[0].subject == "Sign this" and hits[0].ts == 50.0
    assert [h.digest for h in store.search("agreement")] == [D1, D2]  # newest first


def test_search_filters():
    put(D1, text="invoice one", account="a@x.com")
    put(D2, text="invoice two", account="b@x.com")
    put(D3, text="invoice three", account="a@x.com")
    seen(D1, name="One.PDF", ts=100.0, account="a@x.com")
    seen(D2, name="two.pdf", ts=200.0, account="b@x.com", source="gmail")
    # D3 has no seen row (ts 0)

    def got(**kw):
        return sorted(h.digest for h in store.search("invoice", **kw))

    assert got() == sorted([D1, D2, D3])
    assert got(account=" B@x.com") == [D2]
    assert got(account="a@x.com") == sorted([D1, D3])
    assert got(source="gmail") == [D2]
    assert got(source="mail") == [D1]
    assert got(since=150.0) == [D2]
    assert got(until=150.0) == sorted([D1, D3])  # an unseen text has ts 0
    assert got(since=50.0, until=150.0) == [D1]
    assert got(name="one.p") == [D1]
    assert got(name="PDF") == sorted([D1, D2])
    assert got(name="zzz") == []


def test_search_filters_apply_to_the_same_seen_row():
    put(D1, text="invoice")
    seen(D1, name="old.pdf", ts=10.0, source="mail")
    seen(D1, name="new.pdf", ts=900.0, source="gmail")
    assert store.search("invoice", source="mail", since=500.0) == []
    (hit,) = store.search("invoice", since=500.0)
    assert hit.name == "new.pdf" and hit.source == "gmail"
    (hit,) = store.search("invoice", until=500.0)
    assert hit.name == "old.pdf"


def test_search_limit_and_order():
    for i, d in enumerate((D1, D2, D3)):
        put(d, text="common word")
        seen(d, name=f"n{i}", ts=10.0 + i)
    assert [h.name for h in store.search("common", limit=2)] == ["n2", "n1"]
    assert len(store.search("common", limit=1)) == 1
    assert len(store.search("common", limit=0)) == 1
    assert len(store.search("common", limit="junk")) == 3


def test_search_snippets():
    filler = "lorem ipsum " * 60
    text = f"{filler} the LEASE ends soon {filler} later the deposit is refunded {filler}"
    put(D1, text=text)
    (hit,) = store.search("lease deposit")
    assert 1 <= len(hit.snippets) <= 2
    assert all(len(s) <= 160 for s in hit.snippets)
    assert any("LEASE" in s for s in hit.snippets)
    assert any("deposit" in s for s in hit.snippets)
    assert all("  " not in s and "\n" not in s for s in hit.snippets)
    put(D2, text="a\n\n\n   tabbed\t\tlease   words")
    (hit,) = store.search("tabbed")
    assert hit.snippets == ("a tabbed lease words",)


def test_search_snippets_are_cleaned_of_control_characters():
    put(D1, text="before \x07‮ lease \x00 after")
    (hit,) = store.search("lease")
    assert all(c.isprintable() for c in hit.snippets[0])


def test_search_does_not_touch_last_used(monkeypatch, home):
    t = _clock(monkeypatch)
    put(D1, text="lease")
    before = _rows(home, "texts")[0][-1]
    t[0] += 1000
    assert store.search("lease")
    assert _rows(home, "texts")[0][-1] == before


def test_search_skips_stale_and_old_version(monkeypatch):
    t = _clock(monkeypatch)
    put(D1, text="lease")
    t[0] += store.MAX_AGE_S + 10
    assert store.search("lease") == []
    t[0] = 1_000_000.0
    monkeypatch.setattr(store, "VERSION", 2)
    assert store.search("lease") == []
    assert store.count() == 0


def test_search_query_syntax_is_data():
    put(D1, text="alpha NEAR beta OR gamma a:b -x star* \"quoted\" AND not")
    put(D2, text="plain words only")
    nasty = [
        '"', '""', "*", "NEAR", "OR", "AND", "NOT", "a:b", "-x", "x*", '"alpha" OR "plain"',
        "NEAR(alpha beta)", "body:alpha", "alpha AND", "(", ")", "^alpha", "{a b}", "'", "--",
        "alpha OR plain", "\x00", "ａｌｐｈａ", "%", "_", "\\",
    ]
    for q in nasty:
        assert isinstance(store.search(q), list)  # never raises
    assert [h.digest for h in store.search("near")] == [D1]
    # "alpha OR plain" is three plain words, not an operator.
    assert not any(h.all_words for h in store.search("alpha plain"))
    assert [h.digest for h in store.search('"alpha" beta')] == [D1]
    assert [h.digest for h in store.search("body:alpha")] == [D1]  # "body" is not a column
    assert store.search("body:zzzz") == []
    assert [h.digest for h in store.search("a:b")] == []  # one-letter words are dropped


def test_search_up_to_eight_words():
    put(D1, text="w1 w2 w3 w4 w5 w6 w7 w8 w9")
    hits = store.search("w1 w2 w3 w4 w5 w6 w7 w8 zz9999")  # ninth word ignored
    assert hits and hits[0].all_words


def test_count():
    assert store.count() == 0
    put(D1)
    put(D2)
    assert store.count() == 2
    store.forget_all()
    assert store.count() == 0


def test_new_functions_never_raise_on_corrupt_db(home):
    (home / "cache").mkdir()
    _db(home).write_bytes(b"this is not a sqlite database" * 200)
    assert store.count() == 0
    assert store.search("lease") == []
    assert not store.note_seen(D1, source="mail")
    assert store.get_facts(D1, 1) is None
    assert not store.put_facts(D1, 1, {"a": 1})
    assert not store.tried("k")
    assert not store.mark_tried("k", "ok")


def test_facts_round_trip_and_rules():
    assert not store.put_facts(D1, 1, {"a": 1})  # no text row
    put()
    assert store.put_facts(D1, 1, {"what": "lease", "total": {"value": "2.00"}, "n": [1, 2]})
    assert store.get_facts(D1, 1) == {"what": "lease", "total": {"value": "2.00"}, "n": [1, 2]}
    assert store.get_facts(D1, 2) is None  # another version reads as None
    assert store.get_facts(D2, 1) is None
    assert store.get_facts("bad", 1) is None
    assert store.put_facts(D1, 2, {"what": "new"})
    assert store.get_facts(D1, 2) == {"what": "new"} and store.get_facts(D1, 1) is None
    assert not store.put_facts(D1, 1, ["list"])
    assert not store.put_facts(D1, 1, "text")
    assert not store.put_facts(D1, 1, {"bad": object()})
    assert not store.put_facts(D1, 1, {"nan": float("nan")})
    assert not store.put_facts(D1, True, {"a": 1})
    assert not store.put_facts(D1, "1", {"a": 1})
    assert store.get_facts(D1, True) is None
    big = {"k": "x" * 3990}
    assert len(json.dumps(big)) <= 4000
    assert store.put_facts(D1, 3, big)
    assert not store.put_facts(D1, 3, {"k": "x" * 4000})
    assert store.get_facts(D1, 3) == big


def test_facts_dropped_with_the_text():
    put(D1)
    store.put_facts(D1, 1, {"a": 1})
    put(D2)
    store.put_facts(D2, 1, {"b": 1})
    store.forget_all()
    assert store.get_facts(D1, 1) is None and store.get_facts(D2, 1) is None


def test_tried_round_trip_and_age(monkeypatch):
    t = _clock(monkeypatch)
    assert not store.tried("msg-1")
    assert store.mark_tried("msg-1", "unreadable")
    assert store.tried("msg-1") and not store.tried("msg-2")
    assert not store.mark_tried("", "ok")
    assert not store.mark_tried("k" * 301, "ok")
    assert store.mark_tried("k" * 300, "ok") and store.tried("k" * 300)
    assert not store.tried("")
    assert not store.tried(None)
    t[0] += 30 * 86400 - 10
    assert store.tried("msg-1")
    t[0] += 20
    assert not store.tried("msg-1")  # ignored once 30 days old


def test_tried_outcome_token(home):
    for outcome in ("kept", "too_big", "x" * 25, "Bad Token", "", None, 5):
        assert store.mark_tried(f"k-{outcome}", outcome)
    with sqlite3.connect(str(_db(home))) as c:
        got = {r[0] for r in c.execute("SELECT outcome FROM tried")}
    assert got == {"kept", "too_big", "other"}


def test_tried_removed_by_prune_after_thirty_days(monkeypatch, home):
    t = _clock(monkeypatch)
    store.mark_tried("old-key", "ok")
    t[0] += 31 * 86400
    store.mark_tried("new-key", "ok")
    store.prune(t[0])
    assert len(_rows(home, "tried")) == 1
    assert store.tried("new-key") and not store.tried("old-key")


def test_forget_all_clears_tried_and_seen():
    put(D1, text="fileword")
    seen(D1, name="nameword")
    store.mark_tried("k", "ok")
    store.forget_all()
    assert not store.tried("k")
    assert store.search("nameword") == []


def test_forget_account_removes_its_seen_rows_and_orphans(home):
    put(D1, text="shared lease", account="a@x.com")
    put(D2, text="mine lease", account="b@x.com")
    seen(D1, name="a.pdf", account="a@x.com")
    seen(D2, name="b.pdf", account="b@x.com")
    store.put_facts(D1, 1, {"x": 1})
    store.forget_account("a@x.com")
    assert store.get_facts(D1, 1) is None
    assert [h.digest for h in store.search("lease")] == [D2]
    assert [r[4] for r in _rows(home, "seen")] == ["b.pdf"]


def test_dropped_digest_removes_extras(monkeypatch, home):
    t = _clock(monkeypatch)
    put(D1, text="lease")
    seen(D1, name="n")
    store.put_facts(D1, 1, {"a": 1})
    monkeypatch.setattr(store, "prune", lambda now=None: 0)
    t[0] += store.MAX_AGE_S + 5
    assert store.get(D1) is None
    assert _rows(home, "seen") == [] and _rows(home, "facts") == []
    assert _rows(home, "indexed") == []
    assert store.search("lease") == []


def test_replacing_a_text_reindexes_it():
    put(D1, text="oldword content")
    assert store.search("oldword")
    put(D1, text="newword content")
    assert store.search("oldword") == []
    assert [h.digest for h in store.search("newword")] == [D1]


# --- the index: upgrade of an old database, no FTS5 -------------------------

_OLD_SCHEMA = """
CREATE TABLE texts(
    digest TEXT PRIMARY KEY, version INTEGER NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, cut INTEGER NOT NULL, note TEXT NOT NULL,
    how TEXT NOT NULL, bytes INTEGER NOT NULL, created REAL NOT NULL,
    last_used REAL NOT NULL);
CREATE TABLE owners(digest TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(digest, account));
CREATE TABLE locators(locator TEXT PRIMARY KEY, digest TEXT NOT NULL, account TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value REAL NOT NULL);
"""


def _old_db(home, rows):
    (home / "cache").mkdir(mode=0o700, exist_ok=True)
    now = time.time()
    with sqlite3.connect(str(_db(home))) as c:
        c.executescript(_OLD_SCHEMA)
        for dig, text in rows:
            c.execute(
                "INSERT INTO texts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dig, store.VERSION, "pdf", text, 0, "", "text", len(text), now, now),
            )
            c.execute("INSERT INTO owners VALUES(?,?)", (dig, "a@x.com"))
    os.chmod(_db(home), 0o600)


def test_old_database_gains_tables_and_old_rows_become_searchable(home):
    _old_db(home, [(D1, "ancient lease text"), (D2, "other old words")])
    assert store.get(D1).text == "ancient lease text"  # existing path still works
    assert [h.digest for h in store.search("ancient")] == [D1]
    assert [h.digest for h in store.search("words")] == [D2]
    assert seen(D1, name="x.pdf")
    assert store.put_facts(D2, 1, {"a": 1})
    assert store.count() == 2
    assert len(_rows(home, "indexed")) == 2  # filled lazily by search


def test_get_does_not_fill_the_index(home):
    _old_db(home, [(D1, "ancient lease text")])
    assert store.get(D1) is not None
    assert _rows(home, "indexed") == []
    store.search("zzzz")
    assert len(_rows(home, "indexed")) == 1


def test_unindexed_rows_found_even_when_fill_is_capped(monkeypatch, home):
    monkeypatch.setattr(store, "_FILL_ROWS", 1)
    _old_db(home, [(D1, "alpha one"), (D2, "alpha two"), (D3, "alpha three")])
    assert len(store.search("alpha")) == 3
    assert len(_rows(home, "indexed")) == 1
    assert len(store.search("alpha")) == 3  # more of them indexed on the next call
    assert len(_rows(home, "indexed")) == 2


def test_old_database_without_fts5_still_works(monkeypatch, home):
    monkeypatch.setattr(store, "FTS5", False)
    _old_db(home, [(D1, "ancient lease text")])
    assert [h.digest for h in store.search("lease")] == [D1]


def test_search_without_fts5(monkeypatch, home):
    monkeypatch.setattr(store, "FTS5", False)
    put(D1, text="The lease ends 31 August", account="a@x.com")
    put(D2, text="invoice acme")
    seen(D1, name="Flat.pdf", ts=5.0, account="a@x.com")
    assert _rows(home, "indexed") == []
    assert [h.digest for h in store.search("lease august")] == [D1]
    assert [h.digest for h in store.search("flat")] == [D1]  # file name
    assert {h.digest for h in store.search("lease acme")} == {D1, D2}
    assert store.search("invoice", account="a@x.com") == []
    assert [h.digest for h in store.search("lease", since=1.0, name="FLAT")] == [D1]
    for q in ('"', "*", "NEAR", "a:b", "-x", "OR"):
        assert isinstance(store.search(q), list)
    store.forget_all()
    assert store.search("lease") == []


def test_missing_fts5_module_switches_the_index_off(monkeypatch):
    real = store.sqlite3.connect

    class NoFts:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if "USING fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(store, "FTS5", True)
    monkeypatch.setattr(store.sqlite3, "connect", lambda *a, **k: NoFts(real(*a, **k)))
    assert put(D1, text="lease terms")
    assert store.FTS5 is False
    assert [h.digest for h in store.search("lease")] == [D1]


def test_search_is_fast_over_many_large_texts():
    chunk = "The quick brown fox jumps over the lazy dog while reading page text. "
    for i in range(200):
        dig = f"{i:064x}"
        body = (chunk * 300) + f" unique{i:04d} "
        if i == 137:
            body += " needleword7 "
        assert store.put(dig, kind="pdf", text=body, cut=False, account="a@x.com")
        assert store.note_seen(dig, source="mail", name=f"file{i}.pdf", ts=float(i))
    t0 = time.perf_counter()
    hits = store.search("needleword7")
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    many = store.search("quick brown", limit=8)
    second = time.perf_counter() - t0
    assert [h.digest for h in hits] == [f"{137:064x}"]
    assert len(many) == 8
    assert first < 2.0 and second < 2.0


def test_search_is_fast_without_fts5_too(monkeypatch):
    monkeypatch.setattr(store, "FTS5", False)
    chunk = "The quick brown fox jumps over the lazy dog while reading page text. "
    for i in range(200):
        store.put(f"{i:064x}", kind="pdf", text=(chunk * 300) + f" u{i:04d}", cut=False)
    t0 = time.perf_counter()
    assert len(store.search("lazy dog")) == 8
    assert time.perf_counter() - t0 < 2.0


def test_search_over_one_maximum_size_text():
    store.put(D1, kind="pdf", text=("word " * 100_000) + "finalneedle", cut=False)
    t0 = time.perf_counter()
    assert [h.digest for h in store.search("finalneedle")] == [D1]
    assert time.perf_counter() - t0 < 2.0


# --- erase guarantees --------------------------------------------------------

_TOKENS = {
    "text": "zebratextq7731",
    "name": "zebranamer8842",
    "sender": "zebrasenderw9953",
    "subject": "zebrasubjectp1164",
    "facts": "zebrafactsk2275",
}
_KEYS = tuple(_TOKENS)


def _plant(dig, account="a@x.com", tag="zebra"):
    t = {k: v.replace("zebra", tag) for k, v in _TOKENS.items()}
    assert put(dig, text=f"filler words {t['text']} " + "padding " * 300, account=account)
    assert store.note_seen(
        dig, source="mail", account=account, name=f"{t['name']}.pdf",
        sender=f"{t['sender']} <s@x.com>", subject=f"re {t['subject']}", ts=1.0,
    )
    assert store.put_facts(dig, 1, {"what": t["facts"], "party": t["facts"]})
    return t


def _present(home, tokens):
    raw = _raw(home)
    return {k: tok.encode() in raw for k, tok in tokens.items()}


def test_everything_planted_is_present_before_erasing(home):
    t = _plant(D1)
    assert all(_present(home, t).values())
    assert store.search(t["text"]) and store.search(t["name"])


def test_forget_all_leaves_nothing_in_the_file(home):
    t = _plant(D1)
    u = _plant(D2, account="b@x.com", tag="quokka")
    store.mark_tried("zebratriedm3386", "kept")
    assert store.forget_all() == 2
    assert not any(_present(home, t).values())
    assert not any(_present(home, u).values())
    assert store.search(t["text"]) == [] and store.search(t["name"]) == []


def test_forget_account_leaves_nothing_of_that_account(home):
    t = _plant(D1, account="a@x.com")
    keep = _plant(D2, account="b@x.com", tag="quokka")
    store.forget_account("a@x.com")
    assert not any(_present(home, t).values())
    assert all(_present(home, keep).values())
    assert store.search(t["text"]) == []
    assert [h.digest for h in store.search(keep["text"])] == [D2]
    assert store.get_facts(D2, 1) is not None


def test_prune_by_age_leaves_nothing_in_the_file(monkeypatch, home):
    t = _clock(monkeypatch)
    old = _plant(D1)
    t[0] += 10
    keep = _plant(D2, tag="quokka")
    assert store.prune(t[0] + store.MAX_AGE_S - 5) == 1
    assert not any(_present(home, old).values())
    assert all(_present(home, keep).values())
    assert store.search(old["text"]) == []


def test_prune_by_size_leaves_nothing_in_the_file(monkeypatch, home):
    t = _clock(monkeypatch)
    old = _plant(D1)
    t[0] += 1
    _plant(D2, tag="quokka")
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 3000)
    assert store.prune(t[0]) == 1
    assert not any(_present(home, old).values())


def test_dropped_digest_is_gone_after_the_next_prune(monkeypatch, home):
    t = _clock(monkeypatch)
    old = _plant(D1)
    real = store.prune
    monkeypatch.setattr(store, "prune", lambda now=None: 0)
    t[0] += store.MAX_AGE_S + 5
    assert store.get(D1) is None  # dropped by a read; the file is rewritten later
    monkeypatch.setattr(store, "prune", real)
    store.prune(t[0])
    assert not any(_present(home, old).values())


def test_version_drop_is_gone_after_the_next_prune(monkeypatch, home):
    old = _plant(D1)
    monkeypatch.setattr(store, "VERSION", 2)
    assert store.get(D1) is None
    store.prune()
    assert not any(_present(home, old).values())


def test_tried_keys_are_never_stored_readable(home):
    store.mark_tried("<message-id-secret-5521@mail.example>", "kept")
    assert b"secret-5521" not in _raw(home)
    assert store.tried("<message-id-secret-5521@mail.example>")


def test_erase_works_without_fts5(monkeypatch, home):
    monkeypatch.setattr(store, "FTS5", False)
    t = _plant(D1)
    store.forget_all()
    assert not any(_present(home, t).values())
    t2 = _plant(D2, tag="wombat")
    store.forget_account("a@x.com")
    assert not any(_present(home, t2).values())


def test_index_keeps_no_second_copy_of_the_text(home):
    t = _plant(D1)
    with sqlite3.connect(str(_db(home))) as c:
        assert [r[1] for r in c.execute("PRAGMA table_info(ft)")] == ["body"]
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "ft_content" not in names
    assert store.search(t["text"])


# --- erase and search under a lock held elsewhere ----------------------------

import threading  # noqa: E402


def _hold(home, mode="IMMEDIATE"):
    c = sqlite3.connect(str(_db(home)), isolation_level=None, check_same_thread=False)
    c.execute(f"BEGIN {mode}")
    return c


def test_forget_all_reports_failure_while_another_connection_holds_the_lock(monkeypatch, home):
    t = _plant(D1)
    monkeypatch.setattr(store, "_ERASE_WAIT_S", 0.2)
    c = _hold(home)
    try:
        assert store.forget_all_ok() is False
        assert store.forget_all() == 0 and store.last_erase_failed() is True
        assert store.forget_account_ok("a@x.com") is False
        assert store.forget_account("a@x.com") == 0 and store.last_erase_failed() is True
        assert store.forget_source_ok("mail") is False
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert all(_present(home, t).values())            # nothing was erased, and nothing said it was
    assert store.forget_all_ok() is True and store.last_erase_failed() is False
    assert not any(_present(home, t).values())


def test_an_erase_waits_for_a_lock_that_is_released_in_time(monkeypatch, home):
    t = _plant(D1)
    monkeypatch.setattr(store, "_ERASE_WAIT_S", 5.0)
    c = _hold(home)
    threading.Timer(0.4, lambda: (c.execute("ROLLBACK"), c.close())).start()
    assert store.forget_account_ok("a@x.com") is True
    assert not any(_present(home, t).values())


def test_zero_removed_is_not_a_failure(home):
    assert store.forget_all_ok() is True and store.forget_account_ok("nobody@x.com") is True
    assert store.forget_all() == 0 and store.last_erase_failed() is False
    assert store.forget_account("") == 0 and store.last_erase_failed() is False


def test_search_while_a_writer_holds_the_lock_still_answers(monkeypatch, home):
    t = _plant(D1)
    monkeypatch.setattr(store, "_WAIT_S", 0.2)
    c = _hold(home)                                   # a writer: readers are not blocked
    try:
        hits, failed = store.search_checked(t["text"])
        assert failed is False and [h.digest for h in hits] == [D1]
        assert store.count_checked() == 1 and store.last_search_failed() is False
    finally:
        c.execute("ROLLBACK")
        c.close()


def test_search_and_count_say_so_when_the_database_cannot_be_read(monkeypatch, home):
    t = _plant(D1)
    monkeypatch.setattr(store, "_WAIT_S", 0.2)
    c = _hold(home, "EXCLUSIVE")
    try:
        assert store.search_checked(t["text"]) == ([], True)
        assert store.search(t["text"]) == [] and store.last_search_failed() is True
        assert store.count_checked() is None
        assert store.count() == 0 and store.last_search_failed() is True
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert store.count() == 1 and store.last_search_failed() is False
    assert store.search_checked("nonsenseword") == ([], False)       # empty is not failed
    assert store.last_search_failed() is False


# --- the index mark and the insert go together -------------------------------

def test_a_failed_word_insert_leaves_the_text_to_be_indexed_later(home):
    assert put(D2, text="first words")
    with sqlite3.connect(str(_db(home)), isolation_level=None) as c:
        c.execute("DROP TABLE ft")                    # a stand-in table that refuses one text
        c.execute("CREATE TABLE ft(body TEXT CHECK(body NOT LIKE '%poison%'))")
    assert put(D1, text="poison words kept anyway")
    with sqlite3.connect(str(_db(home))) as c:
        marked = {r[0] for r in c.execute("SELECT digest FROM indexed")}
    assert D1 not in marked                           # not marked as indexed: the lazy fill retries it
    assert store.get(D1).text == "poison words kept anyway"


# --- "tried" keys are salted --------------------------------------------------

def test_tried_keys_are_salted_and_forgotten_with_the_salt(home):
    import hashlib

    key = "mail:1:1.2:50000:report.pdf"
    assert store.mark_tried(key, "kept") and store.tried(key)
    plain = hashlib.sha256(key.encode()).hexdigest()
    assert plain.encode() not in _raw(home)           # a guessed locator cannot be confirmed
    with sqlite3.connect(str(_db(home))) as c:
        salt = c.execute("SELECT value FROM meta WHERE key='tried_salt'").fetchone()[0]
    assert isinstance(salt, bytes) and len(salt) == 32
    assert store.forget_all_ok()
    assert salt not in _raw(home)
    assert store.tried(key) is False
    assert store.mark_tried(key, "kept")
    with sqlite3.connect(str(_db(home))) as c:
        assert c.execute("SELECT value FROM meta WHERE key='tried_salt'").fetchone()[0] != salt


def test_an_old_unsalted_row_never_matches(home):
    import hashlib

    key = "mail:9:9.9:10:old.pdf"
    assert store.mark_tried("mail:other", "kept")      # makes the tables
    with sqlite3.connect(str(_db(home)), isolation_level=None) as c:
        c.execute("INSERT INTO tried(key, outcome, ts) VALUES(?,?,?)",
                  (hashlib.sha256(key.encode()).hexdigest(), "kept", time.time()))
    assert store.tried(key) is False


def test_tried_many_is_one_answer_for_a_page(home):
    keys = [f"mail:{i}:1.2:5:f{i}.pdf" for i in range(900)]
    for k in keys[::3]:
        store.mark_tried(k, "kept")
    assert store.tried_many(keys + ["", "x" * 301]) == set(keys[::3])
    assert store.tried_many([]) == set()
    assert store.tried(keys[0]) and not store.tried(keys[1])


# --- forget_source ------------------------------------------------------------

def test_forget_source_removes_only_texts_seen_from_that_source(home):
    only = _plant(D1, account="a@x.com", tag="zebra")
    both = _plant(D2, account="a@x.com", tag="quokka")
    other = _plant(D3, account="a@x.com", tag="wombat")
    for d, tag in ((D1, "zebra"), (D2, "quokka")):
        store.note_seen(d, source="messages", account="a@x.com", name=f"{tag}-msg.pdf", sender="s", subject="u", ts=2.0)
    store.note_seen(D3, source="gmail", account="a@x.com", name="w.pdf", ts=3.0)
    with sqlite3.connect(str(_db(home)), isolation_level=None) as c:
        c.execute("DELETE FROM seen WHERE digest=? AND source='mail'", (D1,))     # D1: messages only
    assert store.forget_source("messages") == 1
    assert store.last_erase_failed() is False
    assert store.get(D1) is None and store.get_facts(D1, 1) is None
    assert not any(_present(home, only).values())
    assert store.get(D2) is not None and store.get(D3) is not None
    assert _present(home, both)["text"] and _present(home, other)["text"]
    assert store.search(only["text"]) == []
    hits = store.search(both["text"])
    assert [h.digest for h in hits] == [D2] and hits[0].source == "mail"      # the messages row is gone
    assert b"quokka-msg" not in _raw(home)
    assert store.forget_source("bogus") == 0 and store.forget_source_ok("bogus") is True


def test_forget_source_works_without_fts5(monkeypatch, home):
    monkeypatch.setattr(store, "FTS5", False)
    t = _plant(D1)
    assert store.forget_source("mail") == 1
    assert not any(_present(home, t).values())


# --- review round 2: salt race, fail-closed tried keys, first source, erase results ------

def test_two_threads_making_the_salt_end_with_one_secret(home):
    mine = store._connect()
    other = sqlite3.connect(str(_db(home)), isolation_level=None)
    rival = os.urandom(32)

    class Racing:
        """After this connection's first look at the salt, a rival writes its own."""

        def __init__(self, conn):
            self._c = conn
            self._raced = False

        def execute(self, sql, *args):
            cur = self._c.execute(sql, *args)
            if not self._raced and sql.startswith("SELECT") and "meta" in sql and args and "tried_salt" in str(args):
                self._raced = True
                rows = cur.fetchall()
                other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('tried_salt', ?)",
                              (sqlite3.Binary(rival),))
                return self._c.execute("SELECT 1 WHERE 0") if not rows else cur
            return cur

    try:
        got = store._salt(Racing(mine))
        stored = other.execute("SELECT value FROM meta WHERE key='tried_salt'").fetchone()[0]
    finally:
        mine.close()
        other.close()
    assert got == rival == bytes(stored)          # the first writer's secret wins; nobody overwrites it


def test_tried_many_fails_closed_while_the_database_is_locked(monkeypatch, home):
    assert store.mark_tried("k1", "kept")
    monkeypatch.setattr(store, "_WAIT_S", 0.2)
    c = _hold(home, "EXCLUSIVE")
    try:
        with pytest.raises(store.StoreBusy):
            store.tried_many(["k1", "k2"])
        assert store.tried("k1") is False         # the one-key form just says no
        assert store.mark_tried("k3", "kept") is False
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert store.tried_many(["k1", "k2"]) == {"k1"}


def test_tried_many_fails_closed_when_the_salt_cannot_be_read(monkeypatch):
    def boom(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_salt", boom)
    with pytest.raises(store.StoreBusy):
        store.tried_many(["k1"])
    assert store.mark_tried("k1", "kept") is False
    assert store.tried_many([]) == set()


def test_erase_results_come_from_the_call_not_the_shared_flag(monkeypatch, home):
    t = _plant(D1)
    real = store._run_erase

    def racing(body):
        out = real(body)
        store._erase_state["failed"] = True       # a concurrent erase failed after ours finished
        return out

    monkeypatch.setattr(store, "_run_erase", racing)
    ok, removed = store.forget_account_result("a@x.com")
    assert (ok, removed) == (True, 1)
    assert not any(_present(home, t).values())
    assert store.forget_all_result() == (True, 0)
    assert store.forget_account_result("") == (True, 0)
    monkeypatch.setattr(store, "_ERASE_WAIT_S", 0.2)
    monkeypatch.setattr(store, "_run_erase", real)
    _plant(D2)
    c = _hold(home)
    try:
        assert store.forget_account_result("a@x.com") == (False, 0)
        assert store.forget_all_result() == (False, 0)
    finally:
        c.execute("ROLLBACK")
        c.close()


def test_forget_source_finds_a_text_whose_seen_rows_are_gone(home):
    t = _plant(D1, tag="zebra")                    # first seen from mail, owned by a@x.com
    with sqlite3.connect(str(_db(home)), isolation_level=None) as c:
        c.execute("INSERT INTO owners(digest, account) VALUES(?, 'b@x.com')", (D1,))
    assert store.forget_account("a@x.com") == 0    # b still owns it: the text stays, its seen rows go
    assert store.get(D1) is not None and _rows(home, "seen") == []
    assert store.forget_source("mail") == 1        # only the durable first source can say it was mail's
    assert store.get(D1) is None
    assert not any(_present(home, t).values())
    assert _rows(home, "first_source") == []


def test_forget_source_keeps_a_text_another_source_still_holds(home):
    t = _plant(D1, tag="zebra")
    assert store.note_seen(D1, source="messages", account="a@x.com", name="m.pdf", ts=5.0)
    assert store.forget_source("mail") == 0
    assert store.get(D1) is not None and _present(home, t)["text"]
    assert store.forget_source("messages") == 1    # now nothing holds it
    assert store.get(D1) is None


def test_first_source_is_backfilled_for_a_text_that_predates_it(home):
    put(D1, text="old reading words")
    with sqlite3.connect(str(_db(home)), isolation_level=None) as c:
        c.execute("INSERT INTO seen(digest, source, account, name, sender, subject, ts)"
                  " VALUES(?, 'gmail', 'a@x.com', 'o.pdf', '', '', 1.0)", (D1,))
    assert store.note_seen(D1, source="mail", account="a@x.com", name="n.pdf", ts=2.0)
    assert [r[1] for r in _rows(home, "first_source")] == ["gmail"]
