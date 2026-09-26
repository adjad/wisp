"""Synthetic-only contract tests for the standalone MIME parser."""
import base64
from dataclasses import replace
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

import pytest

from service.mail_mime import MailLink, ParseLimits, parse_message


FIXTURES = Path(__file__).resolve().parents[1] / "test_fixtures" / "mail_mime"
IDENTITY = {"source_id": "synthetic-1", "account": "fixture-account"}


def parse(raw, **kwargs):
    return parse_message(raw, identity=IDENTITY, **kwargs)


def codes(result):
    return {issue.code for issue in result.issues}


def text_message(body, kind="plain", charset="utf-8", encoding="8bit"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return (f"Content-Type: text/{kind}; charset={charset}\r\n"
            f"Content-Transfer-Encoding: {encoding}\r\n\r\n").encode() + body


def multipart(children, subtype="mixed", extra=""):
    # Unique boundaries also allow nesting generated fixtures.
    boundary = "boundary-" + str(max((len(child) for child in children), default=0))
    return (f'Content-Type: multipart/{subtype}; boundary="{boundary}"{extra}\n\n'.encode()
            + b"".join(b"--" + boundary.encode() + b"\n" + child + b"\n" for child in children)
            + b"--" + boundary.encode() + b"--\n")


def test_alternative_fixture_preserves_real_href_and_ignores_active_resources():
    result = parse((FIXTURES / "alternative.eml").read_bytes())
    assert result.status == "complete"
    assert result.display_text == "Café & tea\n\nhttps://label.example.invalid details"
    assert result.links == (MailLink("https://destination.example.invalid/path?a=1&b=2",
                                   "https://label.example.invalid details", "1.2"),)


def test_mixed_fixture_orders_body_and_excludes_attachments_and_nested_messages():
    result = parse((FIXTURES / "mixed.eml").read_bytes())
    assert result.status == "complete"
    assert result.display_text == "Café\n\nSecond part."
    assert result.links == (MailLink("https://example.invalid/second", "part", "1.2"),)


def test_truncation_keeps_useful_text_with_explicit_damage():
    result = parse((FIXTURES / "truncated.eml").read_bytes())
    assert result.status == "partial"
    assert result.display_text == "Recovered body"
    assert {"mime_CloseBoundaryNotFoundDefect", "invalid_base64"} <= codes(result)


@pytest.mark.parametrize("charset,data,expected", [
    ("utf-8", "Hello café 🌍".encode(), "Hello café 🌍"),
    ("iso-8859-1", b"caf\xe9", "café"),
    ("windows-1252", b"\x93hello\x94", "“hello”"),
    ("utf-16", "Hello 🌍".encode("utf-16"), "Hello 🌍"),
])
@pytest.mark.parametrize("encoding", ["8bit", "base64", "quoted-printable"])
def test_charsets_and_transfer_encodings(charset, data, expected, encoding):
    import quopri
    encoded = base64.b64encode(data) if encoding == "base64" else (
        quopri.encodestring(data) if encoding == "quoted-printable" else data)
    result = parse(text_message(encoded, charset=charset, encoding=encoding))
    assert (result.status, result.display_text, result.issues) == ("complete", expected, ())


@pytest.mark.parametrize("charset,data", [
    ("unknown-charset", b"hello"), ("utf-8", b"a\xffb"),
    ("base64_codec", b"SGVsbG8="), ("utf-16", b"x"),
])
def test_bad_charset_or_bytes_are_partial(charset, data):
    result = parse(text_message(data, charset=charset))
    assert result.status == "partial"
    assert "invalid_charset_or_text" in codes(result)


def test_missing_charset_is_ascii_with_explicit_utf8_recovery():
    assert parse(b"\nASCII body").status == "complete"
    result = parse(b"Content-Transfer-Encoding: 8bit\n\ncaf\xc3\xa9")
    assert result.status == "partial"
    assert result.display_text == "café"


@pytest.mark.parametrize("data", [b"%%%", b"YQ", b"YQ===", b"YQ==trailing", b"\xff"])
def test_bad_base64_never_leaks_encoded_or_guessed_body(data):
    result = parse(text_message(data, encoding="base64"))
    assert result.status == "malformed"
    assert result.display_text == ""
    assert result.links == ()
    assert codes(result) & {"invalid_base64", "invalid_transfer_bytes"}


def test_qp_soft_lines_and_invalid_escape():
    assert parse(text_message(b"one=\r\ntwo=20three", encoding="quoted-printable")).display_text == "onetwo three"
    result = parse(text_message(b"bad=GG escape=", encoding="quoted-printable"))
    assert result.status == "partial"
    assert "invalid_quoted_printable" in codes(result)


def test_invalid_7bit_and_unknown_transfer_encoding():
    assert "invalid_7bit" in codes(parse(text_message("café", encoding="7bit")))
    result = parse(text_message("not safely decoded", encoding="x-rot13"))
    assert result.status == "malformed"
    assert "unsupported_transfer_encoding" in codes(result)


def test_alternative_chooses_last_html_without_duplicate_plain_text():
    result = parse(multipart([text_message("fallback"), text_message("<p>first</p>", "html"),
                              text_message("<p>last</p>", "html")], "alternative"))
    assert result.display_text == "last"
    assert result.status == "complete"


def test_alternative_uses_plain_when_html_is_broken():
    result = parse(multipart([text_message("fallback"),
                              text_message("%%%", "html", encoding="base64")], "alternative"))
    assert (result.status, result.display_text) == ("partial", "fallback")


@pytest.mark.parametrize("html", [
    '<a href="https://example.invalid"></a>',
    '<a href="https://example.invalid"> \n&nbsp;\u200b</a>',
    '<a href="https://example.invalid"><img src="https://tracker.invalid/pixel"></a>',
])
def test_empty_html_anchor_preserves_readable_plain_alternative(html):
    result = parse(multipart([text_message("Plain warning: read this message"),
                              text_message(html, "html")], "alternative"))
    assert result.status == "complete"
    assert result.display_text == "Plain warning: read this message"
    assert result.links == ()
    assert result.issues == ()


def test_related_root_start_ignores_other_text_resources():
    resource = b"Content-ID: <resource>\n" + text_message("not body")
    root = b"Content-ID: <root>\n" + text_message('<a href="https://example.invalid">root</a>', "html")
    result = parse(multipart([resource, root], "related", '; start="<root>"'))
    assert result.display_text == "root"
    assert result.links[0].part == "1.2"
    missing = parse(multipart([resource, root], "related", '; start="<missing>"'))
    assert missing.status == "malformed"
    assert "invalid_related_root" in codes(missing)


def test_nested_alternative_in_mixed_and_related_default_root():
    alt = multipart([text_message("plain"), text_message("<p>HTML</p>", "html")], "alternative")
    related = multipart([alt, text_message("resource")], "related")
    result = parse(multipart([related, text_message("footer")]))
    assert (result.status, result.display_text) == ("complete", "HTML\n\nfooter")


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "data:text/html,hi", "file:///tmp/a", "mailto:a@example.invalid",
    "tel:123", "cid:part", "/relative", "relative", "//example.invalid", "#anchor",
    "java&#x73;cript:alert(1)", "https://user:pass@example.invalid", "https://example.invalid:99999",
    "https://example.invalid:", "https://example.invalid:0", "https://", "https:///path",
    "https://bad_host.invalid", "https://-bad.invalid", "https://[invalid]",
    "https://example.invalid\\@evil.invalid", "https://example.invalid/%0aX",
    "https://example.invalid/%5cX", "https://example.invalid/%broken",
    "https://example.invalid/a b", "https://example.invalid/&#9;x", "https://example.invalid/\u202ex",
    "https://éxample.invalid", "https://example.invalid/%7f", "https://exa%mple.invalid",
    "https://[::1]evil.invalid", "https://example.invalid..",
])
def test_unsafe_or_ambiguous_destinations_filtered_without_losing_label(href):
    result = parse(text_message(f'<a href="{href}">Visible label</a>', "html"))
    assert result.status == "partial"
    assert result.display_text == "Visible label"
    assert result.links == ()
    assert "filtered_href" in codes(result)


@pytest.mark.parametrize("href", [
    "https://example.invalid/path?a=1&amp;b=2", "HTTP://example.invalid:8080/p#anchor",
    "https://[2001:db8::1]/p", "https://xn--bcher-kva.example/a%20b", " https://example.invalid/p ",
])
def test_safe_absolute_destinations(href):
    result = parse(text_message(f'<a href="{href}">Open <b>link</b></a>', "html"))
    assert result.status == "complete"
    assert result.links[0].href == href.strip().replace("&amp;", "&")
    assert result.links[0].anchor_text == "Open link"


def test_no_inferred_links_and_no_base_resolution():
    result = parse(text_message('<base href="https://example.invalid"><p>https://visible.invalid</p>'
                                '<a href="/path">Relative</a><a name="local">Named</a>', "html"))
    assert result.links == ()
    assert parse(text_message("https://visible.invalid")).links == ()


def test_entities_whitespace_img_alt_and_controls():
    result = parse(text_message('<p>One&nbsp; &amp; two</p><p><a href="https://example.invalid">'
                                'Three<br>four<img alt=" five" src="https://tracker.invalid"></a></p>\x00\u202e', "html"))
    assert result.display_text == "One & two\n\nThree\nfour five"
    assert result.links[0].anchor_text == "Three\nfour five"


def test_duplicate_nested_unclosed_anchors_and_malformed_html():
    duplicate = parse(text_message('<a href="https://one.invalid" href="https://two.invalid">label</a>', "html"))
    assert duplicate.links == ()
    nested = parse(text_message('<a href="https://one.invalid">one<a href="https://two.invalid">two', "html"))
    assert [link.anchor_text for link in nested.links] == ["one", "two"]
    assert "malformed_html" in codes(nested)
    truncated_script = parse(text_message("<script>body", "html"))
    assert truncated_script.status == "malformed"
    assert "malformed_html" in codes(truncated_script)


def test_suppressed_html_never_exposes_links_or_script_text():
    result = parse(text_message('<script>bad()</script><style>css</style><template><a href="https://bad.invalid">'
                                'hidden</a></template><iframe src="https://bad.invalid">frame</iframe>'
                                '<svg><a href="https://bad.invalid">vector</a></svg><p>Body</p>', "html"))
    assert (result.display_text, result.links) == ("Body", ())


@pytest.mark.parametrize("hidden_html", [
    '<div hidden><a href="https://hidden.example.invalid">Secret</a></div>',
    '<a hidden href="https://hidden.example.invalid">Secret</a>',
    '<a hidden="false" href="https://hidden.example.invalid">Secret</a>',
    '<a HIDDEN="until-found" href="https://hidden.example.invalid">Secret</a>',
    '<div hidden><div>Inner</div><a href="https://hidden.example.invalid">Secret</a></div>',
    '<div hidden><span>Secret</span><img alt="Hidden image"><br></div>',
    '<div hidden><p>Secret</div>',
    '<img hidden alt="Secret" src="https://hidden.example.invalid/pixel">',
    '<img hidden alt="Secret" src="https://hidden.example.invalid/pixel"/>',
    '<a hidden href="https://hidden.example.invalid"/>',
])
def test_hidden_attribute_suppresses_text_links_and_preserves_visible_sibling(hidden_html):
    result = parse(text_message(hidden_html + '<p>Visible</p>', "html"))
    assert result.status == "complete"
    assert result.display_text == "Visible"
    assert result.links == ()
    assert result.issues == ()


def test_hidden_descendant_does_not_enter_visible_anchor_label():
    result = parse(text_message('<a href="https://visible.example.invalid">Open '
                                '<span hidden>Secret</span>link</a>', "html"))
    assert result.display_text == "Open link"
    assert result.links == (MailLink("https://visible.example.invalid", "Open link", "1"),)


def test_hidden_html_alternative_preserves_readable_plain_fallback():
    result = parse(multipart([text_message("Readable fallback"), text_message(
        '<div hidden><a href="https://hidden.example.invalid">Secret</a></div>', "html")],
        "alternative"))
    assert (result.status, result.display_text, result.links) == ("complete", "Readable fallback", ())


def test_escaped_markup_remains_literal_plain_text_for_text_only_consumers():
    result = parse(text_message("<p>&lt;script&gt;literal&lt;/script&gt;</p>", "html"))
    assert result.display_text == "<script>literal</script>"
    assert result.links == ()


def test_identity_is_copied_and_never_taken_from_headers():
    identity = dict(IDENTITY)
    result = parse_message((FIXTURES / "alternative.eml").read_bytes(), identity=identity)
    identity["source_id"] = "mutated"
    assert dict(result.identity) == IDENTITY
    with pytest.raises(TypeError):
        result.identity["source_id"] = "changed"


def test_deterministic_results_including_issue_and_link_order():
    raw = (FIXTURES / "truncated.eml").read_bytes()
    assert parse(raw) == parse(raw)
    raw = text_message('<a href="https://b.invalid">b</a><a href="https://a.invalid">a</a>'
                       '<a href="/bad">x</a><a href="/bad">y</a>', "html")
    result = parse(raw)
    assert result == parse(raw)
    assert [link.anchor_text for link in result.links] == ["b", "a"]
    assert sum(issue.code == "filtered_href" for issue in result.issues) == 1


@pytest.mark.parametrize("raw,code", [
    (b"", "empty_message"),
    (b'Content-Type: multipart/mixed; boundary="absent"\n\nbody', "mime_StartBoundaryNotFoundDefect"),
    (b"Content-Type: text/plain\nContent-Type: text/html\n\nambiguous", "duplicate_mime_header"),
])
def test_empty_malformed_and_ambiguous_mime(raw, code):
    result = parse(raw)
    assert result.status == "malformed"
    assert code in codes(result)
    assert result.links == ()


@pytest.mark.parametrize("raw", [
    b"Content-Type: text/plain\nContent-Disposition: attachment\n\nattachment",
    b"Content-Type: text/plain\n\n",
    b"Content-Type: application/octet-stream\n\nbytes",
])
def test_valid_message_without_supported_body_is_empty_not_malformed(raw):
    result = parse(raw)
    assert result.status == "empty"
    assert codes(result) == {"no_display_body"}


@pytest.mark.parametrize("field,raw,exact,code", [
    ("max_message_bytes", text_message("body"), len(text_message("body")), "message_byte_limit"),
    ("max_parts", multipart([text_message("one"), text_message("two")]), 3, "part_limit"),
    ("max_depth", multipart([multipart([text_message("body")])]), 3, "depth_limit"),
    ("max_part_bytes", text_message("café"), 5, "part_byte_limit"),
    ("max_decoded_bytes", multipart([text_message("abc"), text_message("def")]), 6, "decoded_byte_limit"),
    ("max_text_chars", text_message("hello"), 5, "text_limit"),
    ("max_links", text_message('<a href="https://a.invalid">a</a><a href="https://b.invalid">b</a>', "html"), 2, "link_limit"),
])
def test_inclusive_resource_budgets_and_fail_closed(field, raw, exact, code):
    assert parse(raw, limits=replace(ParseLimits(), **{field: exact})).status == "complete"
    result = parse(raw, limits=replace(ParseLimits(), **{field: exact - 1}))
    assert result.status == "rejected"
    assert (result.display_text, result.links) == ("", ())
    assert code in codes(result)


def test_input_limit_runs_before_mime_parser_and_part_limit_at_allocation():
    with patch("service.mail_mime.BytesParser", side_effect=AssertionError("must not parse")):
        assert parse(b"xx", limits=ParseLimits(max_message_bytes=1)).status == "rejected"
    from email.message import EmailMessage
    with patch("service.mail_mime.EmailMessage", wraps=EmailMessage) as message:
        result = parse(multipart([text_message("x")] * 50), limits=ParseLimits(max_parts=3))
        assert result.status == "rejected"
        assert message.call_count == 3


def test_unselected_alternative_and_attachment_count_still_consume_budgets():
    result = parse(multipart([text_message("plain"), text_message("html", "html")], "alternative"),
                   limits=ParseLimits(max_decoded_bytes=8))
    assert result.status == "rejected"
    attachment = b"Content-Disposition: attachment\n" + text_message("skip")
    result = parse(multipart([text_message("body"), attachment]), limits=ParseLimits(max_parts=2))
    assert "part_limit" in codes(result)


def test_many_parts_and_deep_messages_reject_without_uncaught_recursion():
    assert "part_limit" in codes(parse(multipart([text_message("x")] * 100)))
    raw = text_message("leaf")
    for _ in range(21):
        raw = multipart([raw])
    assert "depth_limit" in codes(parse(raw))


@pytest.mark.parametrize("kwargs", [{"max_parts": 0}, {"max_parts": True}, {"max_parts": 101}, {"max_message_bytes": -1}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        ParseLimits(**kwargs)


def test_invalid_api_types():
    with pytest.raises(TypeError):
        parse_message("not bytes", identity={})
    with pytest.raises(TypeError):
        parse_message(b"", identity={"id": 1})
    with pytest.raises(TypeError):
        parse_message(b"", identity={}, limits={})


def test_parser_performs_no_file_network_or_process_io():
    raw = (FIXTURES / "alternative.eml").read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("parser attempted I/O")
    with patch("builtins.open", forbidden), patch.object(socket, "socket", forbidden), \
            patch.object(subprocess, "Popen", forbidden):
        assert parse(raw).status == "complete"


def test_standalone_import_has_no_runtime_integration_dependencies():
    script = "import sys; import service.mail_mime; assert not any(n.startswith(('service.main', 'service.tools', 'service.config')) for n in sys.modules)"
    subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, " +
                    repr(str(FIXTURES.parents[1])) + "); " + script], check=True)
