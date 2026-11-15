"""A key saved under the pre-rename Keychain service is copied across once, on first use,
and a key that can't be found or read says how to reconnect it. Synthetic only: a fake
`security` stands in for Keychain, so no real Keychain item is read or written."""
import asyncio
import subprocess
import threading

import pytest

from service import errors
from service.config import keychain_legacy as legacy
from service.config import provider_credentials as secrets
from service.config.endpoints import (ProviderCredentialBusy, ProviderCredentialCopyPending,
                                      ProviderCredentialError, ProviderCredentialMissing,
                                      ProviderCredentialTimedOut)
from service.study import connector as C

URL = "https://example-provider.test"
# The pre-rename names come from keychain_legacy itself (the only place they are
# written), so these tests plant the old item under the right name in every tree.
OLD, NEW = legacy._PRE_RENAME["inference"], "com.renamed.inference"
OLD_STUDY, NEW_STUDY = legacy._PRE_RENAME["study"], "com.renamed.study"
KEY = "sk-test-INVENTED-0123456789abcdef"


class FakeKeychain:
    """`security` over a dict. `denied` items exist but refuse a -w read."""

    def __init__(self, items=None, denied=()):
        self.items = dict(items or {})
        self.denied = set(denied)
        self.argvs = []
        self.stdins = []

    def __call__(self, argv, input=None, **kwargs):
        self.argvs.append(list(argv))
        args = argv[1:]
        if args == ["-i"]:
            self.stdins.append(input)
            words = input.decode("ascii").split()
            assert words[:2] == ["add-generic-password", "-U"]
            opts = dict(zip(words[2::2], words[3::2]))
            self.items[(opts["-s"], opts["-a"])] = opts["-w"]
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert args[0] == "find-generic-password"
        opts = dict(zip(args[1::2], args[2::2]))
        item = (opts["-s"], opts["-a"])
        if item not in self.items:
            return subprocess.CompletedProcess(argv, 44, b"", b"item could not be found")
        if "-w" not in args:
            return subprocess.CompletedProcess(argv, 0, b"attributes", b"")
        if item in self.denied:
            return subprocess.CompletedProcess(argv, 128, b"", b"user canceled")
        return subprocess.CompletedProcess(argv, 0, self.items[item].encode() + b"\n", b"")

    def read_old(self, service):
        return [a for a in self.argvs if service in a and "-w" in a]


@pytest.fixture(autouse=True)
def renamed(monkeypatch, tmp_path):
    # Every `security` call in these tests, including keychain_legacy's own (through
    # LAUNCH), goes to a FakeKeychain the test installs with use(); the default has
    # nothing in it. Nothing here can reach a real Keychain.
    monkeypatch.setattr(subprocess, "run", FakeKeychain())
    monkeypatch.setattr(legacy, "LAUNCH", lambda argv, **kwargs: subprocess.run(argv, **kwargs))
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(secrets, "SERVICE", NEW)
    monkeypatch.setattr(C, "SERVICE", NEW_STUDY)
    monkeypatch.setattr(secrets, "_RECENT_FAILURE", {})
    monkeypatch.setattr(secrets, "_RECENT_MISSING", set())
    monkeypatch.setattr(secrets, "_RECENT_SUCCESS", {})
    monkeypatch.setattr(secrets, "_READ_LOCK", threading.Lock())
    monkeypatch.setattr(secrets, "_reuse_enabled", lambda: False)
    monkeypatch.setattr(secrets.quarantine, "check", lambda: None)
    C.forget_key()
    monkeypatch.setattr(C, "_cloud_target", lambda: None)
    yield
    C.forget_key()


def use(monkeypatch, keychain):
    monkeypatch.setattr(subprocess, "run", keychain)
    return keychain


def account():
    return secrets.keychain_account("cloud", URL)


def assert_key_never_in_argv(keychain):
    assert not any(KEY in " ".join(a) for a in keychain.argvs)


# --- the cloud key ---------------------------------------------------------------------

def test_an_old_key_is_copied_on_first_use_and_the_old_item_is_kept(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): KEY}))
    assert secrets.resolve_keychain("cloud", URL) == KEY
    assert kc.items[(NEW, account())] == KEY
    assert kc.items[(OLD, account())] == KEY          # left in place
    assert_key_never_in_argv(kc)                      # only stdin carried it
    assert len(kc.stdins) == 1
    kc.argvs.clear()
    assert secrets.resolve_keychain("cloud", URL) == KEY   # second use: the new item only
    assert kc.read_old(OLD) == []


def test_already_copied_never_touches_the_old_item(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): "old-value", (NEW, account()): KEY}))
    assert secrets.resolve_keychain("cloud", URL) == KEY
    assert not any(OLD in a for a in kc.argvs)
    assert kc.stdins == []


def test_no_old_item_is_missing_and_says_reconnect(monkeypatch):
    kc = use(monkeypatch, FakeKeychain())
    with pytest.raises(ProviderCredentialMissing, match="^Provider credential unavailable$"):
        secrets.resolve_keychain("cloud", URL)
    assert kc.stdins == []
    assert not any("-w" in a and OLD in a for a in kc.argvs)   # nothing to ask about
    # Held briefly, still as missing.
    with pytest.raises(ProviderCredentialMissing):
        secrets.resolve_keychain("cloud", URL)


def test_an_old_item_macos_refuses_is_unreadable_not_missing(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): KEY}, denied={(OLD, account())}))
    with pytest.raises(ProviderCredentialError) as caught:
        secrets.resolve_keychain("cloud", URL)
    assert not isinstance(caught.value, ProviderCredentialMissing)
    assert (NEW, account()) not in kc.items
    assert KEY not in str(caught.value)
    message, detail = errors.translate(caught.value)
    assert message == errors.UNREADABLE_KEY_MESSAGE and KEY not in detail


def test_an_old_value_that_is_not_a_plain_key_is_not_copied(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): "two words"}))
    with pytest.raises(ProviderCredentialError):
        secrets.resolve_keychain("cloud", URL)
    assert kc.stdins == []


def test_where_the_name_never_changed_nothing_is_copied(monkeypatch):
    monkeypatch.setattr(secrets, "SERVICE", OLD)
    kc = use(monkeypatch, FakeKeychain())
    with pytest.raises(ProviderCredentialMissing):
        secrets.resolve_keychain("cloud", URL)
    assert len(kc.argvs) == 1 and kc.stdins == []


def test_the_startup_check_never_copies_and_holds_nothing(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): KEY}))
    with legacy.no_copy():
        with pytest.raises(ProviderCredentialCopyPending) as caught:
            secrets.resolve_keychain("cloud", URL)
    assert kc.read_old(OLD) == [] and kc.stdins == []
    assert secrets._RECENT_FAILURE == {} and secrets._RECENT_MISSING == set()
    assert errors.startup_credential_hint(caught.value) is None      # no "Reconnect" in the log
    # The first Ask right after launch copies it (no hold, no wait=True needed).
    assert secrets.resolve_keychain("cloud", URL) == KEY


def test_a_startup_check_with_no_key_anywhere_logs_reconnect(monkeypatch):
    use(monkeypatch, FakeKeychain())
    with legacy.no_copy():
        with pytest.raises(ProviderCredentialMissing) as caught:
            secrets.resolve_keychain("cloud", URL)
    assert errors.startup_credential_hint(caught.value) == errors.RECONNECT_KEY_MESSAGE


def test_on_the_event_loop_the_copy_never_runs(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): KEY}))

    async def on_loop():
        return secrets.resolve_keychain("cloud", URL)
    with pytest.raises(ProviderCredentialCopyPending):
        asyncio.run(on_loop())
    assert kc.read_old(OLD) == [] and secrets._RECENT_FAILURE == {}


def test_the_copy_read_waits_long_enough_to_type_a_password(monkeypatch):
    kc = FakeKeychain({(OLD, account()): KEY})
    timeouts = {}

    def run(argv, input=None, timeout=None, **kwargs):
        if OLD in argv and "-w" in argv:
            timeouts["read"] = timeout
        return kc(argv, input=input, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    assert secrets.resolve_keychain("cloud", URL) == KEY
    assert timeouts["read"] >= 60


def test_an_unanswered_question_is_not_a_refusal(monkeypatch):
    kc = FakeKeychain({(OLD, account()): KEY})
    answered = {"now": False}

    def run(argv, input=None, **kwargs):
        if OLD in argv and "-w" in argv and not answered["now"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return kc(argv, input=input, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ProviderCredentialTimedOut) as caught:
        secrets.resolve_keychain("cloud", URL)
    assert errors.translate(caught.value)[0] == errors.WAITED_KEY_MESSAGE
    assert secrets._RECENT_FAILURE == {}            # no hold: the next use asks again
    answered["now"] = True
    assert secrets.resolve_keychain("cloud", URL) == KEY


def test_ask_is_told_while_macos_asks(monkeypatch):
    kc = FakeKeychain({(OLD, account()): KEY})
    order = []

    def run(argv, input=None, **kwargs):
        if OLD in argv and "-w" in argv:
            order.append("read")
        return kc(argv, input=input, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    token = legacy.WAIT_NOTICE.set(lambda: order.append("notice"))
    try:
        assert secrets.resolve_keychain("cloud", URL) == KEY
    finally:
        legacy.WAIT_NOTICE.reset(token)
    assert order == ["notice", "read"]


def test_each_turn_installs_the_waiting_line():
    source = open("service/main.py", encoding="utf-8").read()
    body = source[source.index("    async def runner_body():"):][:2500]
    assert "WAIT_NOTICE.set(keychain_wait_notice)" in body
    assert "KEYCHAIN_WAIT_STATUS" in body and "call_soon_threadsafe" in body
    assert errors.KEYCHAIN_WAIT_STATUS == "Waiting for you to allow Keychain access…"


def test_a_busy_read_lock_is_not_the_always_allow_sentence(monkeypatch):
    use(monkeypatch, FakeKeychain({(NEW, account()): KEY}))
    monkeypatch.setattr(secrets, "LOCK_WAIT_S", 0.05)
    secrets._READ_LOCK.acquire()
    try:
        with pytest.raises(ProviderCredentialBusy) as caught:
            secrets.resolve_keychain("cloud", URL)
    finally:
        secrets._READ_LOCK.release()
    message = errors.translate(caught.value)[0]
    assert message == errors.BUSY_KEY_MESSAGE and "Always Allow" not in message


def test_a_failed_write_still_answers_this_use(monkeypatch):
    kc = FakeKeychain({(OLD, account()): KEY})

    def run(argv, input=None, **kwargs):
        if argv[1:] == ["-i"]:
            raise subprocess.TimeoutExpired(argv, 10)
        return kc(argv, input=input, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    assert secrets.resolve_keychain("cloud", URL) == KEY
    assert (NEW, account()) not in kc.items


# --- the reconnect message -------------------------------------------------------------

def test_ask_says_reconnect_instead_of_something_went_wrong():
    message, _ = errors.translate(ProviderCredentialMissing("Provider credential unavailable"))
    assert message == errors.RECONNECT_KEY_MESSAGE
    assert "Settings › Models" in message and "Test & Save" in message
    assert "Something went wrong" not in message


def test_a_wrapped_missing_key_is_still_found():
    try:
        try:
            raise ProviderCredentialMissing("Provider credential unavailable")
        except ProviderCredentialMissing as inner:
            raise RuntimeError("transport failed") from inner
    except RuntimeError as outer:
        assert errors.translate(outer)[0] == errors.RECONNECT_KEY_MESSAGE


def test_other_failures_keep_their_own_messages():
    assert errors.credential_message(ValueError("x")) is None
    assert errors.translate(ValueError("x"))[0].startswith("Something went wrong")


# --- the Study key ---------------------------------------------------------------------

def test_an_old_study_key_counts_as_saved_and_is_copied_on_use(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD_STUDY, "openrouter"): KEY}))
    assert C._key_exists("openrouter") is True
    assert kc.read_old(OLD_STUDY) == []              # existence asks nothing
    assert C._keychain_key("openrouter") == KEY
    assert kc.items[(NEW_STUDY, "openrouter")] == KEY
    assert kc.items[(OLD_STUDY, "openrouter")] == KEY
    assert_key_never_in_argv(kc)


def test_a_refused_old_study_key_is_held_and_says_keychain(monkeypatch):
    use(monkeypatch, FakeKeychain({(OLD_STUDY, "openrouter"): KEY}, denied={(OLD_STUDY, "openrouter")}))
    monkeypatch.setattr(C, "choice", lambda: ("openrouter", "model-x"))
    with pytest.raises(C.StudyConnectorError) as caught:
        C._resolve(None)
    assert caught.value.code == "missing"
    assert "Keychain" in str(caught.value.message) and "Study key" in str(caught.value.message)
    assert KEY not in str(caught.value.message)


def test_no_study_key_anywhere_is_plain_missing(monkeypatch):
    kc = use(monkeypatch, FakeKeychain())
    assert C._key_exists("openrouter") is False
    assert C._keychain_key("openrouter") is None
    assert kc.stdins == []


def test_study_on_a_cloud_connection_with_no_key_says_reconnect(monkeypatch):
    use(monkeypatch, FakeKeychain())
    monkeypatch.setattr(C, "choice", lambda: ("openrouter", "model-x"))

    def missing():
        raise ProviderCredentialMissing("Provider credential unavailable")
    monkeypatch.setattr(C, "_cloud_target", lambda: {"origin": "https://openrouter.ai", "prefix": "/api/v1",
                                                     "model": "model-x", "key": missing})
    with pytest.raises(C.StudyConnectorError) as caught:
        C._resolve(None)
    assert caught.value.message == errors.RECONNECT_KEY_MESSAGE


def test_an_unanswered_study_question_says_so_and_is_not_held(monkeypatch):
    kc = FakeKeychain({(OLD_STUDY, "openrouter"): KEY})
    answered = {"now": False}

    def run(argv, input=None, **kwargs):
        if OLD_STUDY in argv and "-w" in argv and not answered["now"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return kc(argv, input=input, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(C, "choice", lambda: ("openrouter", "model-x"))
    with pytest.raises(C.StudyConnectorError) as caught:
        C._resolve(None)
    assert caught.value.message == C.KEYCHAIN_WAIT_MESSAGE and "Keychain" in C.KEYCHAIN_WAIT_MESSAGE
    assert not C._read_held("openrouter")
    answered["now"] = True
    assert C._keychain_key("openrouter") == KEY


def test_study_never_copies_on_the_event_loop(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD_STUDY, "openrouter"): KEY}))

    async def on_loop():
        return C._keychain_key("openrouter")
    assert asyncio.run(on_loop()) is None
    assert kc.read_old(OLD_STUDY) == [] and not C._read_held("openrouter")
