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


def test_accounts_lists_known_owners_and_locator_accounts():
    assert store.accounts() == set()
    put(D1, account="One@X.com")
    put(D2, account="")                                  # the unknown owner is never listed
    put(D3, account="two@x.com")
    store.remember("loc", D2, account=" Three@X.com ")
    assert store.accounts() == {"one@x.com", "two@x.com", "three@x.com"}
    assert store.account_key(" One@X.com ") == "one@x.com" and store.account_key(None) == ""
    assert store.get(D1) is not None and store.get(D3) is not None   # listing erased nothing
    assert store.forget_account("ONE@x.com") == 1
    assert store.accounts() == {"two@x.com", "three@x.com"}          # a forgotten account is gone from it
    assert store.forget_account("three@x.com") == 0
    assert store.accounts() == {"two@x.com"}


def test_accounts_is_empty_when_the_store_is_unusable(home):
    (home / "cache").mkdir()
    _db(home).write_bytes(b"this is not a sqlite database" * 200)
    assert store.accounts() == set()
    _db(home).unlink()
    target = home / "elsewhere.db"
    target.write_bytes(b"")
    os.symlink(target, _db(home))
    assert store.accounts() == set() and target.read_bytes() == b""


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
