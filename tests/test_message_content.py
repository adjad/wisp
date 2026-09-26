"""Synthetic-only checks for the internal, I/O-free A12a normalizer."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import random
import subprocess
import sys

import pytest

from service.message_content import (
    MAX_ID_CHARS, MAX_LINKS, MAX_TEXT_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS,
    canonicalize_url, normalize_message_content,
)

FIXTURES = Path(__file__).resolve().parents[1] / "test_fixtures/message_content/records.json"


def record(**changes):
    return {
        "guid": "synthetic-guid", "conversation": "synthetic-conversation",
        "sender": "synthetic-sender", "direction": "incoming",
        "timestamp": "2026-09-25T17:30:00Z", "text": "Synthetic message", "links": [],
        **changes,
    }


def issue_codes(result, field):
    return [issue.code for issue in result.issues if issue.field == field]


@pytest.mark.parametrize("fixture", json.loads(FIXTURES.read_text()), ids=lambda f: f["name"])
def test_synthetic_fixture(fixture):
    result = normalize_message_content(fixture["input"])
    assert result.status == fixture["status"]
    if result.status == "invalid":
        assert result.message is None
    else:
        assert result.message.text == fixture["text"]
        assert result.message.timestamp == fixture["timestamp"]
        assert [link.url for link in result.message.links] == fixture["urls"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("HTTPS://EXAMPLE.TEST:443", "https://example.test/"),
    ("http://Example.Test:80/a", "http://example.test/a"),
    ("https://example.test:8443/a", "https://example.test:8443/a"),
    ("https://example.test:080/a", "https://example.test:80/a"),
    ("https://bücher.test/café", "https://xn--bcher-kva.test/caf%C3%A9"),
    ("https://xn--bcher-kva.test/", "https://xn--bcher-kva.test/"),
    ("http://[2001:0DB8:0:0::1]:80/a", "http://[2001:db8::1]/a"),
    ("https://127.0.0.1/a", "https://127.0.0.1/a"),
    ("https://example.test/A%2fb?x=%aa&x=2+b#Part", "https://example.test/A%2Fb?x=%AA&x=2+b#Part"),
    ("https://example.test/a/../%2e/b", "https://example.test/a/../%2E/b"),
    ("https://example.test/?", "https://example.test/?"),
    ("https://example.test/#", "https://example.test/#"),
    ("https://example.test/?#", "https://example.test/?#"),
    ("https://example.test/?b=2&a=1&a=3&utm_source=x#Part", "https://example.test/?b=2&a=1&a=3&utm_source=x#Part"),
])
def test_url_canonicalization_preserves_meaning(raw, expected):
    assert canonicalize_url(raw) == expected
    assert canonicalize_url(expected) == expected


@pytest.mark.parametrize("raw", [
    None, 42, True, [], {}, b"https://example.test", "", "example.test", "//example.test",
    "javascript:alert(1)", "data:text/plain,hello", "file:///tmp/a", "mailto:a@example.test",
    "ftp://example.test/a", "sms:+123", "http:/example.test", "https:///a",
    " https://example.test", "https://example.test ", "https://example.test/a b",
    "https://exam\nple.test", "https://example.test/\tfoo", "https://example.test/\x7f",
    "https://example.test/\u202ehidden", "https://example.test/\ud800",
    "https://example.test/\\evil", "https://example.test\\@evil.test",
    "https://user:password@example.test", "https://@example.test", "https://example.test@evil.test",
    "https://example.test:", "https://example.test:0", "https://example.test:65536",
    "https://example.test:-1", "https://example.test:nan", "https://example.test:１２",
    "https://[::1", "https://[::1]junk", "https://[::1]:", "https://[fe80::1%25en0]/",
    "https://[v1.fe]/", "https://%65xample.test", "https://a..test", "https://-a.test",
    "https://a_.test", "https://example.test.", "https://ß.test", "https://xn--.test",
    "https://127.1", "https://2130706433", "https://0177.0.0.1", "https://0x7f000001",
    "https://example.123", "https://example.test/%", "https://example.test/%xz",
    "https://example.test/%0A", "https://example.test/?a=%00", "https://example.test/#%7f",
    "https://example.test/<script>", "https://example.test/\"x", "https://example.test/{x}",
    "https://" + "a" * 64 + ".test", "https://example.test/" + "a" * MAX_URL_CHARS,
])
def test_rejects_unsafe_or_ambiguous_url(raw):
    assert canonicalize_url(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [
    (0, "1970-01-01T00:00:00.000000Z"),
    (-0.5, "1969-12-31T23:59:59.500000Z"),
    ("2026-09-25T10:30:00-07:00", "2026-09-25T17:30:00.000000Z"),
    ("2026-09-25T23:00:00+05:30", "2026-09-25T17:30:00.000000Z"),
    ("2024-02-29T00:00:00.123456Z", "2024-02-29T00:00:00.123456Z"),
])
def test_timestamp_known_epoch_and_timezone(raw, expected):
    result = normalize_message_content(record(timestamp=raw))
    assert result.status == "complete"
    assert result.message.timestamp == expected


@pytest.mark.parametrize("raw", [
    True, False, [], {}, float("nan"), float("inf"), -float("inf"), 10 ** 1000,
    10 ** 18, "0", "2026-09-25", "2026-09-25T17:30:00", "2026-09-25 17:30:00Z",
    "2026-09-25T17:30:00-00:00", "2026-09-25T17:30:00+00:99",
    "2026-09-25T17:30:00+24:00", "2023-02-29T00:00:00Z",
    "2026-09-25T17:30:60Z", "2026-09-25T17:30:00.1234567Z",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
def test_bad_timestamp_is_partial_not_guessed(raw):
    result = normalize_message_content(record(timestamp=raw))
    assert result.status == "partial"
    assert result.message.timestamp is None
    assert issue_codes(result, "timestamp") == ["invalid"]


@pytest.mark.parametrize("field", ["guid", "conversation", "sender", "direction", "timestamp", "text", "links"])
@pytest.mark.parametrize("mode", ["missing", "null", "wrong_type"])
def test_field_coverage_distinguishes_missing_from_invalid(field, mode):
    raw = record()
    if mode == "missing":
        raw.pop(field)
    else:
        raw[field] = None if mode == "null" else {}
    result = normalize_message_content(raw)
    assert result.status == ("invalid" if field == "text" else "partial")
    assert (field in result.coverage.supplied_fields) == (mode != "missing")
    assert field not in result.coverage.valid_metadata
    assert issue_codes(result, field) == ["invalid" if mode == "wrong_type" else "missing"]


@pytest.mark.parametrize("field", ["guid", "conversation", "sender"])
@pytest.mark.parametrize("bad", ["", " ", " x", "x ", "x\n", "x\x00", "\ud800", "a\u202eb", "x" * (MAX_ID_CHARS + 1)])
def test_opaque_ids_are_not_silently_rewritten(field, bad):
    result = normalize_message_content(record(**{field: bad}))
    assert getattr(result.message, field) is None
    assert issue_codes(result, field) == ["invalid"]


def test_attribution_is_caller_supplied_and_ids_are_case_sensitive():
    result = normalize_message_content(record(sender="Me", conversation="Someone Else", direction=" OUTGOING ", text="Someone Else: hello"))
    assert result.message.sender == "Me"
    assert result.message.conversation == "Someone Else"
    assert result.message.direction == "outgoing"
    assert normalize_message_content(record(guid="ID")).message.identity != normalize_message_content(record(guid="id")).message.identity
    for direction in ("unknown", "sent", "received", True, 1):
        result = normalize_message_content(record(sender="Me", direction=direction))
        assert result.message.direction is None
        assert result.status == "partial"


def test_unicode_text_is_data_and_repairs_are_reported():
    raw = "  Cafe\u0301\r\n👩‍💻\rمرحبا\t<em>data</em>\u202e\x00\ud800  "
    result = normalize_message_content(record(text=raw))
    assert result.message.text == "  Café\n👩‍💻\nمرحبا\t<em>data</em>\u202e\ufffd\ufffd  "
    assert result.coverage.text == "partial"
    assert issue_codes(result, "text") == ["replaced_control_or_surrogate"]


@pytest.mark.parametrize("raw", [None, [], 4, False, "not a dict", {}, {"text": " \n\t", "links": []}])
def test_unusable_records_are_explicitly_invalid(raw):
    result = normalize_message_content(raw)
    assert result.status == "invalid"
    assert result.message is None
    assert "no_usable_content" in issue_codes(result, "record")


def test_links_are_metadata_only_and_empty_differs_from_unknown():
    text = "https://example.test/not-extracted"
    assert normalize_message_content(record(text=text)).message.links == ()
    empty = normalize_message_content(record(text="", links=[{"url": "https://example.test"}]))
    unknown = normalize_message_content(record(text=None, links=[{"url": "https://example.test"}]))
    assert empty.status == "complete" and empty.coverage.text == "empty"
    assert unknown.status == "partial" and unknown.coverage.text == "missing"
    assert normalize_message_content(record()).coverage.links_supplied == 0
    assert normalize_message_content(record(links=None)).coverage.links_supplied is None


def test_mixed_links_have_conserved_counts_and_deterministic_labels():
    links = [
        {"url": "HTTPS://EXAMPLE.TEST:443", "title": "Z"},
        {"url": "https://example.test/", "title": "A"},
        {"url": "https://example.test/", "title": "A"},
        {"url": "https://example.test/other", "title": 5},
        {"url": "file:///synthetic"}, None, {}, "https://example.test/plain",
    ]
    result = normalize_message_content(record(links=links))
    c = result.coverage
    assert (c.links_supplied, c.links_examined, c.links_accepted, c.links_rejected, c.links_duplicate, c.links_skipped) == (8, 8, 4, 4, 2, 0)
    assert c.links_accepted - c.links_duplicate == len(result.message.links)
    assert result.message.links[0].titles == ("A", "Z")
    assert result.status == "partial"
    assert [(i.code, i.index) for i in result.issues] == [("invalid_title", 3), ("invalid_url", 4), ("invalid_url", 5), ("invalid_url", 6), ("invalid_url", 7)]
    assert normalize_message_content(record(links=list(reversed(links)))).message.links == result.message.links


def test_fragments_queries_and_case_sensitive_paths_remain_distinct():
    urls = ["https://example.test/Path", "https://example.test/path", "https://example.test/path#A", "https://example.test/path#B", "https://example.test/path?a=1&b=2", "https://example.test/path?b=2&a=1"]
    result = normalize_message_content(record(links=[{"url": u} for u in urls]))
    assert len(result.message.links) == len(urls)


def test_resource_limits_report_exact_losses():
    result = normalize_message_content(record(
        text="x" * (MAX_TEXT_CHARS + 5),
        links=[{"url": "https://example.test/", "title": "t" * (MAX_TITLE_CHARS + 1)}] * (MAX_LINKS + 3),
    ))
    assert len(result.message.text) == MAX_TEXT_CHARS
    assert len(result.message.links[0].titles[0]) == MAX_TITLE_CHARS
    assert result.coverage.text == result.coverage.links == "partial"
    assert result.coverage.links_skipped == 3
    assert result.coverage.links_examined == result.coverage.links_accepted == MAX_LINKS
    assert result.coverage.links_duplicate == MAX_LINKS - 1
    assert issue_codes(result, "text") == ["truncated"]


def test_unicode_expansion_cannot_bypass_output_limits():
    result = normalize_message_content(record(
        text="\u0344" * MAX_TEXT_CHARS,
        links=[{"url": "https://example.test", "title": "\u0344" * MAX_TITLE_CHARS}],
    ))
    assert len(result.message.text) == MAX_TEXT_CHARS
    assert len(result.message.links[0].titles[0]) == MAX_TITLE_CHARS
    assert result.coverage.text == result.coverage.links == "partial"
    assert issue_codes(result, "text") == ["truncated"]


def test_guid_identity_survives_enrichment_fallback_is_content_bound():
    original = normalize_message_content(record()).message
    enriched = normalize_message_content(record(text="Edited text", links=[{"url": "https://example.test/"}])).message
    assert original.identity_kind == "guid"
    assert original.identity == enriched.identity
    fallback = normalize_message_content(record(guid=None)).message
    assert fallback.identity_kind == "content_fingerprint"
    for changes in ({"text": "different"}, {"sender": "different"}, {"conversation": "different"}, {"direction": "outgoing"}, {"timestamp": 0}, {"links": [{"url": "https://example.test"}]}):
        assert normalize_message_content(record(guid=None, **changes)).message.identity != fallback.identity
    assert normalize_message_content(record(guid=None, sender="a|b", conversation="c")).message.identity != normalize_message_content(record(guid=None, sender="a", conversation="b|c")).message.identity


def test_identity_is_independent_of_order_and_equivalent_normalizations():
    a = record(guid=None, text="Cafe\u0301\r\n", links=[{"url": "HTTPS://EXAMPLE.TEST:443", "title": "B"}, {"url": "https://example.test/", "title": "A"}])
    b = dict(reversed(list(a.items())))
    b.update(text="Café\n", timestamp="2026-09-25T10:30:00-07:00", links=list(reversed(a["links"])))
    assert normalize_message_content(a).message.identity == normalize_message_content(b).message.identity


def test_output_is_immutable_input_untouched_and_issues_do_not_echo_values():
    raw = record(links=[{"url": "https://example.test", "title": "label"}, {"url": "https://user:synthetic-secret@example.test"}])
    saved = deepcopy(raw)
    result = normalize_message_content(raw)
    assert raw == saved
    assert "synthetic-secret" not in repr(result)
    raw["links"][0]["title"] = "changed"
    assert result.message.links[0].titles == ("label",)
    with pytest.raises(FrozenInstanceError):
        result.message.text = "changed"


def test_seeded_malformed_values_never_raise_and_counts_conserve():
    rng = random.Random(12)
    values = [None, True, False, 0, 1.5, [], {}, "", "\ud800", "\x00", "x", "https://example.test"]
    for _ in range(250):
        raw = {key: deepcopy(rng.choice(values)) for key in record()}
        if rng.choice([True, False]):
            raw["links"] = [deepcopy(rng.choice(values)) for _ in range(rng.randrange(10))]
        result = normalize_message_content(raw)
        c = result.coverage
        assert c.links_examined == c.links_accepted + c.links_rejected
        if c.links_supplied is not None:
            assert c.links_supplied == c.links_examined + c.links_skipped


def test_standalone_import_and_normalization_need_no_io_or_clock():
    # A fresh interpreter verifies import purity as well as execution purity.
    # Preload stdlib dependencies so the audit hook can forbid file access.
    script = '''
import __future__, dataclasses, datetime, hashlib, ipaddress, json, math, re, unicodedata
import urllib.parse, encodings.idna, encodings.punycode, sys, time
import service
source = open("service/message_content.py", encoding="utf-8").read()
module = type(sys)("isolated_message_content")
sys.modules[module.__name__] = module
def no_clock(*args, **kwargs):
    raise AssertionError("clock access")
class NoClock(datetime.datetime):
    now = utcnow = today = no_clock
datetime.datetime = NoClock
time.time = time.time_ns = no_clock
def audit(event, args):
    if event == "open" or event.startswith(("socket.", "subprocess.", "os.system", "sqlite3.")):
        raise AssertionError(event)
sys.addaudithook(audit)
exec(compile(source, "message_content.py", "exec"), module.__dict__)
value = {"text": "Synthetic", "links": [{"url": "https://bücher.test"}]}
a = module.normalize_message_content(value)
b = module.normalize_message_content(value)
assert a == b and a.status == "partial"
assert a.message.links[0].url == "https://xn--bcher-kva.test/"
assert not any(name.startswith(("service.tools", "service.config", "sqlite3")) for name in sys.modules)
print(a.message.identity)
'''
    first = subprocess.run([sys.executable, "-B", "-c", script], cwd=FIXTURES.parents[2], capture_output=True, text=True)
    second = subprocess.run([sys.executable, "-B", "-c", script], cwd=FIXTURES.parents[2], capture_output=True, text=True)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout == second.stdout
