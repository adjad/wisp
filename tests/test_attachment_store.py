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
