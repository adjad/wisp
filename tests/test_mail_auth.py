"""Mail authentication evidence: the receiving provider's Authentication-Results, read strictly.

Pure strings and synthetic Gmail API payloads only: no mail, network, subprocess or sleep."""
import base64

import pytest

from service import gmail_source as G
from service import mail_auth as A
from service import mail_sender
from service.tools import email_tools as E

GM = "mx.google.com"


def ok(headers, sender="maya@friend.com", provider="gmail"):
    return A.authenticated(headers, from_address=sender, provider=provider)


def gh(rest):
    return f"{GM}; {rest}"


# --- passes ------------------------------------------------------------------------------

def test_dmarc_pass_aligned_passes():
    h = gh("dkim=pass header.i=@friend.com header.s=s1 header.b=Ab+/Cd==; "
           "spf=pass (google.com: domain of maya@friend.com designates 192.0.2.1 as permitted sender) "
           "smtp.mailfrom=maya@friend.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=friend.com")
    assert ok([h]) == (True, "dmarc pass")


def test_gmail_to_gmail_passes():
    h = gh("dkim=pass header.i=@gmail.com header.s=20230601 header.b=XyZ12345; "
           "spf=pass smtp.mailfrom=maya@gmail.com; dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com")
    assert ok([h], "maya@gmail.com")[0] is True


def test_dkim_pass_aligned_without_dmarc_passes():
    assert ok([gh("dkim=pass header.d=friend.com header.s=s1")]) == (True, "dkim pass aligned")
    assert ok([gh("dkim=pass header.i=@friend.com")]) == (True, "dkim pass aligned")


def test_spf_pass_aligned_alone_passes():
    assert ok([gh("spf=pass smtp.mailfrom=bounce@friend.com")]) == (True, "spf pass aligned")
    assert ok([gh("spf=pass smtp.mailfrom=friend.com")]) == (True, "spf pass aligned")


def test_any_aligned_dkim_among_several_suffices():
    h = gh("dkim=fail header.d=friend.com; dkim=pass header.d=sendgrid.net; dkim=pass header.d=friend.com")
    assert ok([h]) == (True, "dkim pass aligned")


# --- refusals ----------------------------------------------------------------------------

def test_spf_pass_for_an_unrelated_bulk_sender_is_refused():
    h = gh("spf=pass smtp.mailfrom=bounces+123@em1234.sendgrid.net")
    assert ok([h], "ceo@bank.com") == (False, "no aligned pass")


def test_dkim_pass_for_an_unrelated_domain_is_refused():
    assert ok([gh("dkim=pass header.d=mailchimpapp.net header.i=@friend.com")]) == (False, "no aligned pass")


def test_dmarc_fail_wins_over_spf_and_dkim_passes():
    h = gh("dkim=pass header.d=friend.com; spf=pass smtp.mailfrom=friend.com; dmarc=fail header.from=friend.com")
    assert ok([h]) == (False, "dmarc fail")
    # Order of the clauses does not matter.
    h = gh("dmarc=fail header.from=friend.com; dkim=pass header.d=friend.com")
    assert ok([h]) == (False, "dmarc fail")


def test_dmarc_none_needs_an_aligned_dkim_or_spf_pass():
    assert ok([gh("dmarc=none header.from=friend.com; dkim=pass header.d=friend.com")]) == (True, "dkim pass aligned")
    for weak in ("none", "temperror", "permerror"):
        h = gh(f"dmarc={weak} header.from=friend.com; dkim=pass header.d=other.net; spf=softfail smtp.mailfrom=friend.com")
        assert ok([h]) == (False, "no aligned pass"), weak


def test_dmarc_pass_for_another_from_domain_or_an_unknown_result_is_refused():
    assert ok([gh("dmarc=pass header.from=evil.com; dkim=pass header.d=friend.com")]) == (False, "dmarc for another domain")
    assert ok([gh("dmarc=bestguesspass header.from=friend.com")]) == (False, "dmarc unknown result")
    # A dmarc pass that names no domain is not a pass on its own.
    assert ok([gh("dmarc=pass")]) == (False, "no aligned pass")


def test_no_header_at_all_is_refused():
    for headers in (None, [], ()):
        assert ok(headers)[0] is False
    assert ok("mx.google.com; dmarc=pass header.from=friend.com")[0] is False   # a bare string, not a list
    assert ok([""])[0] is False


def test_a_result_from_another_authserv_id_is_not_the_providers():
    assert ok(["mail.friend.com; dmarc=pass header.from=friend.com"]) == (False, "not checked by your mail provider")
    assert ok(["mx.google.com.evil.com; dmarc=pass header.from=friend.com"])[0] is False
    assert ok([gh("dmarc=pass header.from=friend.com")], provider="icloud") == (False, "unknown provider")


def test_two_results_claiming_the_provider_are_no_proof_in_either_order():
    real = gh("dmarc=fail header.from=friend.com")
    forged = gh("dmarc=pass header.from=friend.com")
    for headers in ([real, forged], [forged, real], [forged, forged]):
        assert ok(headers) == (False, "more than one result claims to be your provider's")


def test_a_senders_own_header_with_a_foreign_id_is_ignored_whatever_its_position():
    real = gh("dmarc=pass header.from=friend.com")
    foreign_pass = "friend.com; dmarc=pass header.from=friend.com"
    assert ok([real, foreign_pass]) == (True, "dmarc pass")
    assert ok([foreign_pass, real]) == (True, "dmarc pass")
    # And a foreign pass never rescues the provider's failure.
    assert ok([foreign_pass, gh("dmarc=fail header.from=friend.com")]) == (False, "dmarc fail")
    # Microsoft's id-less form is set aside, not mistaken for the provider's.
    ms = "spf=pass (sender IP is 192.0.2.9) smtp.mailfrom=friend.com; dkim=pass header.d=friend.com; dmarc=pass header.from=friend.com"
    assert ok([ms]) == (False, "not checked by your mail provider")
    assert ok([ms, real]) == (True, "dmarc pass")


def test_comments_and_quoted_strings_never_count_as_results():
    assert ok([gh("dkim=fail (dmarc=pass header.from=friend.com) header.d=friend.com")]) == (False, "no aligned pass")
    assert ok([gh('dkim=fail reason="dmarc=pass header.from=friend.com" header.d=friend.com')]) == (False, "no aligned pass")
    assert ok([gh("dkim=fail (nested (dkim=pass header.d=friend.com) \\) still) header.d=friend.com")])[0] is False
    assert ok([gh('spf=fail smtp.mailfrom="dkim=pass header.d=friend.com"')])[0] is False
    # Unquoted reason text that runs into an "=" stays inside the reason value.
    assert ok([gh("dkim=fail reason=dmarc=pass header.d=other.net")])[0] is False
    # A quoted property value is read as that value.
    assert ok([gh('dkim=pass header.d="friend.com"')]) == (True, "dkim pass aligned")
    # A comment between a provider id and its results is fine.
    assert ok([f"{GM} (via Gmail); dmarc=pass header.from=friend.com"]) == (True, "dmarc pass")


def test_unterminated_comment_or_quote_or_broken_clause_is_unparseable():
    for bad in (gh("dmarc=pass (header.from=friend.com"), gh('dmarc=pass header.from="friend.com'),
                gh("dmarc pass header.from=friend.com"), gh("dmarc=pass header.from="), gh("=pass"),
                gh("dmarc=pass) header.from=friend.com")):
        assert ok([bad]) == (False, "unparseable"), bad


def test_folded_multiline_header_and_mixed_case():
    h = ("MX.Google.COM;\r\n       dkim=PASS header.i=@Friend.COM header.s=s1\r\n"
         "\theader.b=QWERTY12;\r\n       spf=Pass (google.com: domain of\r\n maya@friend.com designates\r\n"
         " 192.0.2.1 as permitted sender) smtp.mailfrom=Maya@Friend.com;\r\n"
         "       DMARC=Pass (p=NONE sp=NONE dis=NONE) Header.From=FRIEND.com")
    assert ok([h], "Maya@Friend.com") == (True, "dmarc pass")
    assert ok([gh("dkim/1=pass header.d=friend.com")]) == (True, "dkim pass aligned")
    assert ok([f"{GM} 1; dmarc=pass header.from=friend.com"]) == (True, "dmarc pass")
    assert ok([gh("dkim = pass header.d = friend.com")]) == (True, "dkim pass aligned")
    assert ok([f"{GM}; none"]) == (False, "no aligned pass")


def test_subdomain_alignment_and_public_suffix_refusals():
    assert A.aligned("news.shop.com", "shop.com") and A.aligned("shop.com", "a.b.shop.com")
    assert ok([gh("dkim=pass header.d=shop.com")], "deals@news.shop.com") == (True, "dkim pass aligned")
    assert ok([gh("spf=pass smtp.mailfrom=bounce@mail.shop.com")], "deals@shop.com") == (True, "spf pass aligned")
    assert A.aligned("example.co.uk", "mail.example.co.uk")
    assert A.aligned("github.io", "github.io")                          # exact match always aligns
    for parent, child in (("co.uk", "evil.co.uk"), ("com.au", "bank.com.au"), ("co.jp", "x.co.jp"),
                          ("ac.nz", "uni.ac.nz"), ("github.io", "victim.github.io"),
                          ("s3.amazonaws.com", "bucket.s3.amazonaws.com"), ("herokuapp.com", "a.herokuapp.com"),
                          ("vercel.app", "x.vercel.app"), ("com", "shop.com")):
        assert not A.aligned(parent, child), (parent, child)
        assert ok([gh(f"dkim=pass header.d={parent}")], f"me@{child}")[0] is False
    # Siblings are not aligned; neither are look-alike suffixes that are not a label boundary.
    assert not A.aligned("a.shop.com", "b.shop.com")
    assert not A.aligned("evilshop.com", "shop.com")


def test_garbage_and_very_long_input_are_refused():
    for junk in ([b"mx.google.com; dmarc=pass"], [42], [None], [{"v": 1}], ["\x00\x01;;;=="], [";"], ["()"],
                 [gh("dmarc=pass header.from=friend.com"), 7]):
        assert ok(junk)[0] is False, junk
    long = gh("dkim=pass header.d=friend.com (" + "x" * (A.MAX_HEADER_CHARS + 10) + ")")
    assert ok([long]) == (False, "unparseable")
    assert ok([gh("dkim=pass header.d=friend.com")] + ["a.example; none"] * (A.MAX_HEADERS + 1)) == (False, "too many headers")
    assert ok([gh("; ".join(["dkim=fail header.d=x.com"] * (A.MAX_RESULTS + 1)))]) == (False, "unparseable")


def test_from_address_must_be_one_clean_address():
    h = [gh("dmarc=pass header.from=friend.com")]
    for sender in ("", None, "friend.com", "@friend.com", "a@b@friend.com", "Maya <maya@friend.com>", "maya@", 5):
        assert A.authenticated(h, from_address=sender, provider="gmail") == (False, "no from address"), sender


# --- Gmail: parse_metadata and the side store ----------------------------------------------

def _message(gid="g1", sender="Maya Chen <maya@friend.com>", mid="<m1@friend.com>", auth=()):
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "me@gmail.com"},
               {"name": "Subject", "value": "Hello"}, {"name": "Message-ID", "value": mid}]
    headers[1:1] = [{"name": "Authentication-Results", "value": a} for a in auth]
    return {"id": gid, "internalDate": "1790000000000", "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": headers,
                        "body": {"data": base64.urlsafe_b64encode(b"Synthetic").decode()}}}


def test_parse_metadata_keeps_every_auth_header_in_api_order():
    first, second = gh("dmarc=pass header.from=friend.com"), "friend.com; dkim=pass header.d=friend.com"
    assert G.parse_metadata(_message(auth=(first, second)))["auth_results"] == [first, second]
    assert G.parse_metadata(_message(auth=(second, first)))["auth_results"] == [second, first]
    assert G.parse_metadata(_message())["auth_results"] == []
    # Case-insensitive name; folding kept for mail_auth to unfold.
    m = _message()
    m["payload"]["headers"].append({"name": "AUTHENTICATION-RESULTS", "value": "mx.google.com;\r\n none"})
    assert G.parse_metadata(m)["auth_results"] == ["mx.google.com;\r\n none"]


def test_parse_metadata_tolerates_junk_and_keeps_its_place():
    m = _message(auth=(gh("dmarc=pass header.from=friend.com"),))
    m["payload"]["headers"] += [{"name": "Authentication-Results", "value": None},
                                {"name": "Authentication-Results"}, "junk", 7, {"name": None, "value": "x"}]
    meta = G.parse_metadata(m)
    assert meta["auth_results"] == [gh("dmarc=pass header.from=friend.com"), "", ""]
    assert meta["sender_address"] == "maya@friend.com"
    assert ok(meta["auth_results"]) == (False, "unparseable")
    assert G.parse_metadata({"id": "x", "payload": {"headers": "nope"}})["auth_results"] == []
    many = _message(auth=[gh("dmarc=pass header.from=friend.com")] * (G._AUTH_KEEP + 3))
    kept = G.parse_metadata(many)["auth_results"]
    assert len(kept) == G._AUTH_KEEP + 1 and kept[-1] == "" and ok(kept)[0] is False


def test_persisted_rows_keep_their_field_counts():
    meta = G.parse_metadata(_message(auth=(gh("dmarc=pass header.from=friend.com"),)))
    assert len(G.header_line(meta, "me@gmail.com", False).split("\x01")) == 10
    assert len(G.raw_record(_message(auth=(gh("dmarc=pass"),)), "me@gmail.com").split("\x01")) == 9


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(G, "_auth", {})
    return G._auth


def test_the_side_store_answers_only_for_a_fetched_message_and_its_own_sender(store):
    meta = G.parse_metadata(_message(auth=(gh("dmarc=pass header.from=friend.com"),)))
    G._remember_auth("Me@Gmail.com", "g1", meta)
    assert G.auth_results_for("me@gmail.com", "m1@friend.com") == meta["auth_results"]
    assert G.authenticated_for("me@gmail.com", "<M1@friend.com>", "maya@friend.com") == (True, "dmarc pass")
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "x@evil.com") == (False, "sender differs from the fetched message")
    # Unknown message, unknown account, empty id: no evidence at all.
    for account, mid in (("me@gmail.com", "<nope@x.com>"), ("other@gmail.com", "<m1@friend.com>"), ("me@gmail.com", "")):
        assert G.auth_results_for(account, mid) is None
        assert G.authenticated_for(account, mid, "maya@friend.com") is None
    # The same message fetched again keeps its evidence; it is gone once the account is forgotten.
    G._remember_auth("me@gmail.com", "g1", meta)
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "maya@friend.com") == (True, "dmarc pass")
    G._forget_auth("ME@gmail.com")
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "maya@friend.com") is None


def test_a_copied_message_id_takes_the_evidence_away_from_both(store):
    good = G.parse_metadata(_message(auth=(gh("dmarc=pass header.from=friend.com"),)))
    copy = G.parse_metadata(_message(gid="g2", sender="maya@friend.com", auth=(gh("dmarc=fail header.from=friend.com"),)))
    G._remember_auth("me@gmail.com", "g1", good)
    G._remember_auth("me@gmail.com", "g2", copy)
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "maya@friend.com") is None
    G._remember_auth("me@gmail.com", "g1", good)        # stays ambiguous
    assert G.auth_results_for("me@gmail.com", "<m1@friend.com>") is None


def test_a_message_without_auth_headers_is_known_but_unproven(store):
    G._remember_auth("me@gmail.com", "g1", G.parse_metadata(_message()))
    assert G.auth_results_for("me@gmail.com", "<m1@friend.com>") == []
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "maya@friend.com") == (False, "no header")


def test_metadata_fetch_requests_the_header_with_the_read_only_scope(store, monkeypatch):
    import asyncio
    calls = []

    async def fake_get(http, path, params=None):
        calls.append((path, list(params or [])))
        return _message(gid=path.rsplit("/", 1)[-1], auth=(gh("dmarc=pass header.from=friend.com"),))

    monkeypatch.setattr(G, "_get", fake_get)
    monkeypatch.setattr(G, "_meta", {})
    monkeypatch.setattr(G, "_current", lambda: ("me@gmail.com", {}, 0))
    asyncio.run(G._ensure_meta(None, ["g9"]))
    assert ("metadataHeaders", "Authentication-Results") in calls[0][1] and ("format", "metadata") in calls[0][1]
    assert G.authenticated_for("me@gmail.com", "<m1@friend.com>", "maya@friend.com") == (True, "dmarc pass")
    assert G.SCOPE.endswith("/gmail.readonly")


# --- Gmail rows never carry a full From beside a separate address ---------------------------

ORDINARY = ("Maya Chen <maya@friend.com>", "maya@friend.com", "=?utf-8?b?TWF5YSBDaGVu?= <maya@friend.com>")
POSED = ('"Maya <maya@friend.com>" <x@evil.com>', "=?utf-8?q?Maya_=3Cmaya=40friend=2Ecom=3E?= <x@evil.com>")


def _rows(sender):
    message = _message(sender=sender)
    meta = G.parse_metadata(message)
    header = E._parse_header_records(G.header_line(meta, "me@gmail.com", False))[0]
    body = E._parse_raw_text(G.raw_record(message, "me@gmail.com"))[0]
    attached = G._attachment_message("me@gmail.com", message)
    return {"header": header, "body": body, "attachment": attached}


@pytest.mark.parametrize("sender", ORDINARY)
def test_ordinary_senders_read_as_ordinary_on_every_gmail_row(sender):
    for kind, row in _rows(sender).items():
        assert not mail_sender.is_odd(row["sender"], row.get("sender_address")), (kind, row)
        assert mail_sender.real_address(row["sender"], row.get("sender_address")) == "maya@friend.com", kind
        assert not ("sender_address" in row and "<" in row["sender"]), kind


@pytest.mark.parametrize("sender", POSED)
def test_posed_senders_read_as_odd_on_every_gmail_row(sender):
    rows = _rows(sender)
    for kind, row in rows.items():
        if "=?" not in sender:
            assert mail_sender.is_odd(row["sender"], row.get("sender_address")), (kind, row)
        # An encoded posed name parseaddr cannot split loses its name: shown by the real address alone.
        assert mail_sender.real_address(row["sender"], row.get("sender_address")) == "x@evil.com", kind
        assert "friend.com" not in mail_sender.shown(row["sender"], row.get("sender_address")), kind


def test_the_value_cap_tracks_mail_auth():
    assert G._AUTH_VALUE_CAP == A.MAX_HEADER_CHARS + 1


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_mail_auth.py"' in manifest
