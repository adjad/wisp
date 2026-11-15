"""A key saved under the pre-rename Keychain service is copied across once, on first use,
and a key that can't be found or read says how to reconnect it. Synthetic only: a fake
`security` stands in for Keychain, so no real Keychain item is read or written."""
import subprocess
import threading

import pytest

from service import errors
from service.config import keychain_legacy as legacy
from service.config import provider_credentials as secrets
from service.config.endpoints import ProviderCredentialError, ProviderCredentialMissing
from service.study import connector as C

URL = "https://example-provider.test"
OLD, NEW = "com.wisp.inference", "com.renamed.inference"
OLD_STUDY, NEW_STUDY = "com.wisp.study", "com.renamed.study"
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


def test_the_startup_check_never_copies(monkeypatch):
    kc = use(monkeypatch, FakeKeychain({(OLD, account()): KEY}))
    with legacy.no_copy():
        with pytest.raises(ProviderCredentialMissing):
            secrets.resolve_keychain("cloud", URL)
    assert kc.read_old(OLD) == [] and kc.stdins == []
    assert secrets.resolve_keychain("cloud", URL, wait=True) == KEY   # first real use


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
