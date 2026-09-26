"""Synthetic, in-memory documents only; no acquisition or runtime integration."""
from dataclasses import fields
from io import BytesIO
from pathlib import Path
import random
import socket
import struct
import warnings
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from service.linked_documents import DOCX, ExtractionLimits, extract_linked_document


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'


def word(body, namespace=W):
    return f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'.encode()


def docx(xml=None, *, extras=(), compression=ZIP_STORED):
    stream = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(stream, "w", compression=compression) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", word("") if xml is None else xml)
            for name, data in extras:
                archive.writestr(name, data)
    return stream.getvalue()


def extract(content, mime="text/plain", **limits):
    return extract_linked_document(content, mime, limits=ExtractionLimits(**limits))


def assert_evidence(result, texts):
    assert [result.text[s.start:s.end] for s in result.sections] == texts
    assert result.text == "\n".join(texts)
    cursor = 0
    for section, text in zip(result.sections, texts):
        assert section.start == cursor
        assert section.end == cursor + len(text)
        assert section.end <= len(result.text)
        assert section.locator
        cursor = section.end + 1


def test_text_offsets_unicode_repeated_lines_and_normalization():
    result = extract("  Café 😀\r\n\r\nrepeat\rrepeat\n")
    assert result.status == "complete"
    assert_evidence(result, ["Café 😀", "repeat", "repeat"])
    assert [s.locator for s in result.sections] == ["text:line[1]", "text:line[3]", "text:line[4]"]


@pytest.mark.parametrize("data", [b"", " \n\t ", b"\xef\xbb\xbf"])
def test_empty_text(data):
    result = extract(data)
    assert result.status == "empty"
    assert_evidence(result, [])


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_explicit_supported_bom_encodings(encoding):
    result = extract("résumé 😀".encode(encoding))
    assert result.status == "complete"
    assert_evidence(result, ["résumé 😀"])


@pytest.mark.parametrize("data, status, reason", [
    (b"hello\xff", "corrupt", "invalid_text_encoding"),
    ("\ud800", "corrupt", "invalid_text_encoding"),
    (b"a\x00b", "unsupported", "binary_text"),
    ("a\x01b", "unsupported", "binary_text"),
])
def test_invalid_text(data, status, reason):
    result = extract(data)
    assert (result.status, result.reasons) == (status, (reason,))
    assert not result.text


@pytest.mark.parametrize("mime", ["application/pdf", "application/octet-stream", "text/rtf", "", "x" * 257])
def test_unsupported_media_never_guesses(mime):
    result = extract("ordinary words", mime)
    assert (result.status, result.reasons) == ("unsupported", ("media_type",))


def test_mime_parameters_do_not_enable_encoding_guess():
    assert extract("hi", " TEXT/PLAIN; charset=utf-8 ").text == "hi"
    assert extract(b"caf\xe9", "text/plain; charset=windows-1252").status == "corrupt"


@pytest.mark.parametrize("data", [b"abcd", "abcd", "éé"])
def test_input_byte_limit_exact_and_over(data):
    assert extract(data, input_bytes=4).status == "complete"
    assert extract(data, input_bytes=3).status == "limit_exceeded"


@pytest.mark.parametrize("field", [field.name for field in fields(ExtractionLimits)])
def test_limits_cannot_disable_or_expand_hard_caps(field):
    default = getattr(ExtractionLimits(), field)
    for value in (0, -1, True, 1.5, default + 1):
        with pytest.raises(ValueError):
            ExtractionLimits(**{field: value})


@pytest.mark.parametrize("content,mime", [(bytearray(b"a"), "text/plain"), (None, "text/plain"), (b"a", None)])
def test_programmer_type_errors(content, mime):
    with pytest.raises(TypeError):
        extract_linked_document(content, mime)


def test_programmer_limit_type_error():
    with pytest.raises(TypeError):
        extract_linked_document("a", "text/plain", limits={})


def test_output_character_budget_and_separator():
    assert extract("ab\nc", output_chars=4).status == "complete"
    result = extract("ab\ncde", output_chars=4)
    assert (result.status, result.reasons) == ("partial", ("output_limit",))
    assert_evidence(result, ["ab", "c"])
    assert_evidence(extract("ab\nc", output_chars=3), ["ab"])
    assert_evidence(extract("😀éx", output_chars=2), ["😀é"])


def test_section_and_plain_parser_budgets():
    assert extract("a\nb", sections=2).status == "complete"
    result = extract("a\nb", sections=1)
    assert result.reasons == ("section_limit",)
    assert_evidence(result, ["a"])
    assert extract("a\nb", parser_events=2).status == "complete"
    result = extract("a\nb", parser_events=1)
    assert result.reasons == ("parser_event_limit",)
    assert_evidence(result, ["a"])


def test_html_headings_lists_tables_inline_entities_and_unicode():
    html = '<!DOCTYPE html><html><head><title>Ignore</title><style>x{}</style></head><body><h1>A &amp; B</h1><p>Café <b>😀</b>!</p><ul><li>One</li><li>Two</li></ul><table><tr><td>A</td><td>B</td></tr></table></body></html>'
    result = extract(html, "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["A & B", "Café 😀!", "One", "Two", "A", "B"])
    assert result.sections[0].kind == "heading"


def test_html_suppression_preserves_prose_and_does_not_read_attributes():
    result = extract('<p>A<script>fetch("secret")</script>B<template><div>hidden</div></template>C<span hidden>no</span>D</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["A", "B", "C", "D"])


def test_html_void_breaks_comments_and_self_closing_elements():
    result = extract('<p>a<br/>b<!-- ignored -->c<hr>d</p><div/></div>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["a bc d"])


def test_html_nonvoid_slash_does_not_close_hidden_subtree():
    result = extract('<div hidden/>Secret', "text/html")
    assert result.status == "partial"
    assert result.reasons == ("unclosed_html",)
    assert_evidence(result, [])
    result = extract('<div hidden/><p>Secret</p></div><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["Visible"])


@pytest.mark.parametrize("fragment,status,reasons", [
    ('<svg/>', 'partial', ('image_content_omitted',)),
    ('<svg hidden/>', 'complete', ()),
    ('<svg><circle/></svg>', 'partial', ('image_content_omitted',)),
    ('<svg><g><circle/><path/></g></svg>', 'partial', ('image_content_omitted',)),
    ('<math/>', 'complete', ()),
    ('<math hidden/>', 'complete', ()),
    ('<math><mrow><mi/><mo/></mrow></math>', 'complete', ()),
])
def test_html_foreign_slash_keeps_following_evidence(fragment, status, reasons):
    result = extract(fragment + '<p>Visible 😀</p>', 'text/html')
    assert (result.status, result.reasons) == (status, reasons)
    assert_evidence(result, ['Visible 😀'])


@pytest.mark.parametrize("tag", ['title', 'script', 'style', 'plaintext', 'input'])
@pytest.mark.parametrize("slash", ['', '/'])
def test_html_foreign_descendants_do_not_enter_html_text_modes(tag, slash):
    closing = '' if slash else f'</{tag}>'
    result = extract(f'<svg><{tag}{slash}>{closing}<circle/></svg><p>Visible</p>', 'text/html')
    assert (result.status, result.reasons) == ('partial', ('image_content_omitted',))
    assert_evidence(result, ['Visible'])


@pytest.mark.parametrize("opening,closing", [
    ('<svg><foreignObject>', '</foreignObject></svg>'),
    ('<svg><desc>', '</desc></svg>'),
    ('<svg><title>', '</title></svg>'),
    ('<math><mtext>', '</mtext></math>'),
    ('<math><annotation-xml encoding="text/html">', '</annotation-xml></math>'),
    ('<math><annotation-xml encoding="application/xhtml+xml">', '</annotation-xml></math>'),
])
def test_html_in_foreign_integration_points_keeps_nonvoid_slash_open(opening, closing):
    result = extract(opening + '<div hidden/>Secret</div>' + closing + '<p>Visible</p>', 'text/html')
    assert 'malformed_html' not in result.reasons
    assert 'unclosed_html' not in result.reasons
    assert_evidence(result, ['Visible'])


def test_html_foreign_slash_still_counts_depth_and_events():
    result = extract('<svg><circle/></svg>', 'text/html', depth=1)
    assert result.status == 'limit_exceeded'
    assert 'parser_depth_limit' in result.reasons
    result = extract('<math/>', 'text/html', parser_events=1)
    assert (result.status, result.reasons) == ('limit_exceeded', ('parser_event_limit',))


@pytest.mark.parametrize("fragment", [
    '<svg><title/><script/><plaintext/></svg>',
    '<math><title/><script/><plaintext/></math>',
    '<svg><plaintext>ignored</plaintext></svg>',
    '<svg><foreignObject/></svg>',
    '<math><annotation-xml encoding="text/html"/></math>',
    '<math><mtext><mglyph/></mtext></math>',
    '<math><annotation-xml><svg/></annotation-xml></math>',
    '<math><annotation-xml encoding="application/xml" encoding="text/html"><mrow/></annotation-xml></math>',
])
def test_html_foreign_integration_element_itself_stays_foreign(fragment):
    result = extract(fragment + '<p>Visible</p>', 'text/html')
    assert 'malformed_html' not in result.reasons
    assert 'unclosed_html' not in result.reasons
    assert_evidence(result, ['Visible'])


def test_html_raw_text_at_foreign_integration_point_cannot_expose_ancestors():
    html = '<svg><foreignObject><script/>"</foreignObject></svg><p>LEAK</p>"</script></foreignObject></svg><p>Visible</p>'
    result = extract(html, 'text/html')
    assert (result.status, result.reasons) == ('partial', ('image_content_omitted',))
    assert_evidence(result, ['Visible'])


@pytest.mark.parametrize('root', ['svg', 'math'])
@pytest.mark.parametrize('tag', 'b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 h5 h6 head hr i img li listing menu meta nobr ol p pre ruby s small span strong strike sub sup table tt u ul var'.split())
def test_html_foreign_start_breakout_has_explicit_handoff(root, tag):
    html = f'<{root}><{tag}>Visible</{tag}>'
    result = extract(html, 'text/html')
    assert result.status == 'unsupported'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, [])
    result = extract('<p>Retained 😀</p>' + html, 'text/html')
    assert result.status == 'partial'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, ['Retained 😀'])


@pytest.mark.parametrize('root', ['svg', 'math'])
@pytest.mark.parametrize('attribute', ['color="red"', 'face="serif"', 'SIZE', 'COLOR=""'])
def test_html_foreign_font_attribute_breakout_has_handoff(root, attribute):
    result = extract(f'<{root}><font {attribute}>Visible</font>', 'text/html')
    assert result.status == 'unsupported'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, [])


@pytest.mark.parametrize('html', [
    '<svg></p><p>Visible</p>',
    '<svg></br>Visible',
    '<math></p><p>Visible</p>',
    '<math></br>Visible',
    '<svg><g></p>Visible',
    '<math><mrow></br>Visible',
    '<div><svg></div><p>Visible</p>',
    '<section><math></section><p>Visible</p>',
    '<svg><foreignObject><div><math></svg><p>Visible</p>',
    '<svg></unknown><p>Visible</p>',
])
def test_html_foreign_end_breakout_has_explicit_handoff(html):
    result = extract(html, 'text/html')
    assert result.status == 'unsupported'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, [])
    result = extract('<p>Retained</p>' + html, 'text/html')
    assert result.status == 'partial'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, ['Retained'])


@pytest.mark.parametrize('html', [
    '<svg><font/></svg>',
    '<math><font class="ordinary"/></math>',
    '<svg><foreignObject><p>omitted SVG</p></foreignObject></svg>',
    '<math><mtext><span></span></mtext></math>',
])
def test_html_foreign_nonbreakouts_and_integration_points_still_extract(html):
    result = extract(html + '<p>Visible</p>', 'text/html')
    assert 'foreign_html_breakout' not in result.reasons
    assert_evidence(result, ['Visible'])


@pytest.mark.parametrize('html', [
    '<svg><p/>Visible',
    '<math><mtext><mglyph><p>Visible',
    '<math><mtext><malignmark><span>Visible',
])
def test_html_nested_and_slash_foreign_breakouts_have_handoffs(html):
    result = extract(html, 'text/html')
    assert result.status == 'unsupported'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, [])


def test_html_foreign_breakout_retains_flushed_prefix_and_respects_budget():
    result = extract('Retained<svg><p>Visible', 'text/html')
    assert result.status == 'partial'
    assert 'foreign_html_breakout' in result.reasons
    assert_evidence(result, ['Retained'])
    result = extract('<svg><p>Visible', 'text/html', parser_events=1)
    assert result.status == 'limit_exceeded'
    assert 'parser_event_limit' in result.reasons
    assert_evidence(result, [])


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "noembed", "noframes", "noscript"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_html_suppressed_raw_text_cannot_end_an_ancestor(tag, slash):
    result = extract(f'<div hidden><{tag}{slash}></div>Secret</{tag}></div><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["Visible"])
    result = extract(f'<{tag}{slash}>Secret</{tag}><p>Visible</p>', "text/html")
    assert_evidence(result, ["Visible"])


@pytest.mark.parametrize("tag", ["textarea", "title"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_html_rcdata_keeps_literal_tags_and_decodes_entities_once(tag, slash):
    result = extract(f'<{tag}{slash}><b>Literal</b>&amp;lt;</{tag}><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["<b>Literal</b>&lt;", "Visible"])
    result = extract(f'<div hidden><{tag}{slash}></div>Secret</{tag}></div><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["Visible"])


@pytest.mark.parametrize("slash", ["", "/"])
def test_html_xmp_and_plaintext_preserve_literal_markup_and_entities(slash):
    result = extract(f'<xmp{slash}><b>Literal</b>&amp;</xmp><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["<b>Literal</b>&amp;", "Visible"])
    result = extract(f'<plaintext{slash}><b>Literal</b>&amp;</plaintext><p>Still literal</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["<b>Literal</b>&amp;</plaintext><p>Still literal</p>"])
    result = extract(f'<div hidden><plaintext{slash}></div>Secret', "text/html")
    assert "Secret" not in result.text


@pytest.mark.parametrize("tag", ["script", "style", "textarea", "xmp", "plaintext"])
def test_html_nonvoid_slash_modes_remain_bounded(tag):
    result = extract(f'<{tag}/>text</{tag}>', "text/html", parser_events=1)
    assert result.reasons == ("parser_event_limit",)
    result = extract(f'<div hidden><{tag}/>text', "text/html", depth=1)
    assert "parser_depth_limit" in result.reasons


@pytest.mark.parametrize("slash", ["", "/"])
def test_html_raw_text_eof_and_end_tag_boundaries(slash):
    result = extract(f'<p>Visible</p><script{slash}>Secret &amp;', "text/html")
    assert result.status == "partial"
    assert "unclosed_html" in result.reasons
    assert_evidence(result, ["Visible"])
    result = extract(f'<script{slash}>Secret</scriptx>more Secret</SCRIPT ><p>Visible</p>', "text/html")
    assert result.status == "complete"
    assert_evidence(result, ["Visible"])
    result = extract(f'<textarea{slash}>&amp;lt;<b>literal', "text/html")
    assert result.status == "partial"
    assert_evidence(result, ["&lt;<b>literal"])


@pytest.mark.parametrize("html,reason", [
    ("<p>retained", "unclosed_html"),
    ("<div><p>retained</div>", "malformed_html"),
    ("<p>retained</p></aside>", "malformed_html"),
])
def test_html_tolerant_recovery_is_explicit(html, reason):
    result = extract(html, "text/html")
    assert (result.status, result.reasons) == ("partial", (reason,))
    assert_evidence(result, ["retained"])


@pytest.mark.parametrize("html,status", [
    ('<!DOCTYPE html SYSTEM "https://invalid.example/a">', "unsupported"),
    ("<?xml version='1.0'?>", "unsupported"),
    ("<![evil]>", "unsupported"),
    ("<![CDATA[secret]]>", "unsupported"),
])
def test_html_declarations_are_handoffs(html, status):
    assert extract(html, "text/html").status == status


def test_html_image_only_empty_and_mixed():
    assert extract('<img src="https://invalid.example/i">', "text/html").status == "image_only"
    assert extract('<svg><text>vector text</text></svg>', "text/html").status == "image_only"
    assert extract('<img hidden src="/i">', "text/html").status == "empty"
    result = extract('<p>caption</p><img src="/i">', "text/html")
    assert (result.status, result.reasons) == ("partial", ("image_content_omitted",))
    assert_evidence(result, ["caption"])
    assert extract('<iframe src="/other">fallback</iframe>', "text/html").reasons == ("embedded_content_omitted",)


def test_html_parser_limits_include_ignored_content():
    assert extract("<p>x</p>", "text/html", parser_events=3, depth=1).status == "complete"
    result = extract("<p>x</p>", "text/html", parser_events=2)
    assert (result.status, result.reasons) == ("limit_exceeded", ("parser_event_limit",))
    result = extract("<template><div>x</div></template>", "text/html", depth=1)
    assert result.reasons == ("parser_depth_limit",)
    assert extract("<!--a--><!--b-->", "text/html", parser_events=1).status == "limit_exceeded"


def test_docx_body_order_runs_tabs_breaks_headings_and_tables():
    xml = word('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p><w:p><w:r><w:t>Café </w:t></w:r><w:r><w:t>😀</w:t><w:tab/><w:t>X</w:t><w:br/><w:t>Y</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>After</w:t></w:r></w:p>')
    result = extract(docx(xml), DOCX)
    assert result.status == "complete"
    assert_evidence(result, ["Title", "Café 😀\tX\nY", "Cell", "After"])
    assert result.sections[0].kind == "heading"
    assert result.sections[2].locator == "word/document.xml:p[3]"


def test_docx_strict_namespace_and_deflate_supported():
    xml = word('<w:p><w:r><w:t>text</w:t></w:r></w:p>', namespace="http://purl.oclc.org/ooxml/wordprocessingml/main")
    result = extract(docx(xml, compression=ZIP_DEFLATED), DOCX)
    assert result.status == "complete"
    assert_evidence(result, ["text"])


def test_docx_revisions_and_field_instructions_not_evidence():
    result = extract(docx(word('<w:p><w:del><w:r><w:t>removed</w:t></w:r></w:del><w:r><w:instrText>external command</w:instrText><w:t>visible</w:t></w:r></w:p>')), DOCX)
    assert (result.status, result.reasons) == ("partial", ("revision_content_omitted",))
    assert_evidence(result, ["visible"])


def test_docx_empty_image_only_and_omitted_parts():
    assert extract(docx(), DOCX).status == "empty"
    assert extract(docx(word('<w:p><w:r><w:drawing/></w:r></w:p>')), DOCX).status == "image_only"
    result = extract(docx(word('<w:p><w:r><w:t>A</w:t></w:r></w:p><w:altChunk/>'), extras=[("word/header1.xml", b"not parsed")]), DOCX)
    assert result.status == "partial"
    assert result.reasons == ("ancillary_word_parts_omitted", "embedded_content_omitted")
    assert_evidence(result, ["A"])


@pytest.mark.parametrize("tag,reason", [
    ("object", "embedded_content_omitted"),
    ("altChunk", "embedded_content_omitted"),
    ("subDoc", "embedded_content_omitted"),
    ("drawing", "image_content_omitted"),
    ("pict", "image_content_omitted"),
    ("del", "revision_content_omitted"),
    ("moveFrom", "revision_content_omitted"),
])
@pytest.mark.parametrize("nested_paragraph", [False, True])
def test_docx_omitted_subtrees_never_contribute_evidence(tag, reason, nested_paragraph):
    omitted = '<w:t>Secret</w:t><w:br/><w:tab/><w:noBreakHyphen/>'
    if nested_paragraph:
        omitted = f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>{omitted}</w:r></w:p>'
    body = f'<w:p><w:r><w:{tag}>{omitted}</w:{tag}><w:t>Visible</w:t></w:r></w:p>'
    result = extract(docx(word(body)), DOCX)
    assert (result.status, result.reasons) == ("partial", (reason,))
    assert_evidence(result, ["Visible"])
    assert result.sections[0].kind == "paragraph"
    assert result.sections[0].locator == "word/document.xml:p[1]"


def test_docx_omitted_subtree_depth_and_events_still_count():
    payload = docx(word('<w:p><w:object><w:r><w:t>Secret</w:t></w:r></w:object><w:t>Visible</w:t></w:p>'))
    result = extract(payload, DOCX, depth=5)
    assert result.status == "limit_exceeded"
    assert "parser_depth_limit" in result.reasons
    assert_evidence(result, [])
    result = extract(payload, DOCX, parser_events=6)
    assert result.status == "limit_exceeded"
    assert "parser_event_limit" in result.reasons
    assert_evidence(result, [])


@pytest.mark.parametrize("payload,reason", [
    (b"not a zip", "invalid_zip_directory"),
    (docx()[:-1], "invalid_zip_directory"),
    (docx(b"<broken>"), "invalid_word_document"),
    (docx(word("", namespace="urn:impostor")), "invalid_word_document"),
    (docx(f'<w:document xmlns:w="{W}"/>'.encode()), "missing_word_body"),
    (docx(word('<w:body/>')), "invalid_word_body"),
])
def test_corrupt_docx(payload, reason):
    result = extract(payload, DOCX)
    assert result.status == "corrupt"
    assert reason in result.reasons
    assert not result.text


def test_docx_partial_xml_retains_only_completed_paragraphs():
    xml = word('<w:p><w:r><w:t>retained</w:t></w:r></w:p><w:p><w:r><w:t>lost')
    result = extract(docx(xml), DOCX)
    assert (result.status, result.reasons) == ("partial", ("invalid_word_xml",))
    assert_evidence(result, ["retained"])


@pytest.mark.parametrize("encoding", ["wat-no-codec", "UTF-7", "UTF-32"])
def test_docx_unusable_xml_encoding_is_a_handoff(encoding):
    payload = docx(f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + word(""))
    result = extract(payload, DOCX)
    assert (result.status, result.reasons) == ("corrupt", ("invalid_word_xml",))


def test_docx_mutually_exclusive_representations_do_not_become_evidence():
    alternate = '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Choice Requires="w"><w:p><w:r><w:t>Choice</w:t></w:r></w:p></mc:Choice><mc:Fallback><w:p><w:r><w:t>Fallback</w:t></w:r></w:p></mc:Fallback></mc:AlternateContent>'
    result = extract(docx(word(alternate)), DOCX)
    assert (result.status, result.reasons) == ("unsupported", ("alternate_word_content",))
    assert not result.text
    result = extract(docx(word('<w:p><w:r><w:t>before</w:t></w:r></w:p>' + alternate)), DOCX)
    assert result.status == "partial"
    assert_evidence(result, ["before"])


def test_inline_heading_keeps_kind_and_incomplete_html_token_is_partial():
    result = extract('<h1><em>A</em> title</h1>', "text/html")
    assert result.sections[0].kind == "heading"
    assert_evidence(result, ["A title"])
    result = extract('<p>before</p><unfinished', "text/html")
    assert result.status == "partial"
    assert "unfinished_html_token" in result.reasons


def test_docx_hyphens_and_deleted_breaks():
    result = extract(docx(word('<w:p><w:r><w:t>A</w:t><w:noBreakHyphen/><w:t>B</w:t><w:softHyphen/><w:t>C</w:t></w:r><w:del><w:r><w:br/><w:t>deleted</w:t></w:r></w:del><w:r><w:t>D</w:t></w:r></w:p>')), DOCX)
    assert result.status == "partial"
    assert_evidence(result, ["A\u2011B\u00adCD"])


@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE w:document [<!ENTITY x "expansion">]>',
    '<!DOCTYPE w:document SYSTEM "file:///not-opened">',
])
def test_docx_dtd_and_entities_rejected(declaration):
    result = extract(docx(declaration.encode() + word('<w:p><w:r><w:t>&x;</w:t></w:r></w:p>')), DOCX)
    assert (result.status, result.reasons) == ("unsupported", ("xml_declaration_forbidden",))


def test_docx_parser_limits_and_partial_output():
    payload = docx(word('<w:p><w:r><w:t>abc</w:t></w:r></w:p>'))
    assert extract(payload, DOCX, depth=5, parser_events=11).status == "complete"
    assert extract(payload, DOCX, depth=4).reasons == ("parser_depth_limit",)
    assert extract(payload, DOCX, parser_events=1).reasons == ("parser_event_limit",)
    result = extract(payload, DOCX, output_chars=2)
    assert result.status == "partial"
    assert_evidence(result, ["ab"])


def test_docx_entry_and_expansion_boundaries():
    payload = docx()
    total = len(CONTENT_TYPES) + len(word(""))
    largest = max(len(CONTENT_TYPES), len(word("")))
    assert extract(payload, DOCX, zip_entries=2, zip_total_bytes=total, zip_entry_bytes=largest).status == "empty"
    for limits, reason in [({"zip_entries": 1}, "zip_entry_limit"),
                           ({"zip_total_bytes": total - 1}, "zip_expansion_limit"),
                           ({"zip_entry_bytes": largest - 1}, "zip_part_size_limit")]:
        result = extract(payload, DOCX, **limits)
        assert (result.status, result.reasons) == ("limit_exceeded", (reason,))


def test_docx_ratio_applies_even_to_unread_parts():
    payload = docx(extras=[("word/media/image.bin", b"0" * 100_000)], compression=ZIP_DEFLATED)
    assert extract(payload, DOCX).reasons == ("zip_ratio_limit",)


def test_duplicate_parts_and_unsupported_compression():
    assert extract(docx(extras=[("word/document.xml", word(""))]), DOCX).reasons == ("duplicate_zip_part",)
    assert extract(docx(compression=ZIP_BZIP2), DOCX).reasons == ("zip_compression",)


def test_missing_parts_and_text_instead_of_docx_bytes():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("unrelated", "x")
    assert extract(stream.getvalue(), DOCX).reasons == ("missing_docx_part",)
    assert extract("hello", DOCX).reasons == ("docx_requires_bytes",)


def test_encrypted_zip_handoff():
    payload = bytearray(docx())
    central = payload.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", payload, central + 8)[0]
    struct.pack_into("<H", payload, central + 8, flags | 1)
    result = extract(bytes(payload), DOCX)
    assert (result.status, result.reasons) == ("encrypted", ("encrypted_zip_entry",))


def test_encrypted_office_signature_is_distinct_from_generic_ole():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    assert extract(ole, DOCX).status == "unsupported"
    result = extract(ole + "EncryptedPackage\x00EncryptionInfo".encode("utf-16-le"), DOCX)
    assert (result.status, result.reasons) == ("encrypted", ("encrypted_office_container",))


def test_forged_directory_count_is_bounded_before_zipfile(monkeypatch):
    payload = bytearray(docx(extras=[("extra", b"x")]))
    end = payload.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", payload, end + 8, 1, 1)
    def forbidden(*args, **kwargs):
        pytest.fail("ZipFile must not allocate an oversized directory")
    monkeypatch.setattr("service.linked_documents.ZipFile", forbidden)
    result = extract(bytes(payload), DOCX, zip_entries=2)
    assert result.reasons == ("zip_entry_limit",)


def test_docx_crc_corruption_is_not_evidence():
    payload = bytearray(docx(word('<w:p><w:r><w:t>ORIGINAL</w:t></w:r></w:p>')))
    payload[payload.index(b"ORIGINAL")] = ord("X")
    result = extract(bytes(payload), DOCX)
    assert (result.status, result.reasons) == ("corrupt", ("invalid_docx_archive",))
    assert not result.text


def test_no_network_or_filesystem_acquisition(monkeypatch):
    payload = docx(word('<w:p><w:hyperlink><w:r><w:t>label</w:t></w:r></w:hyperlink></w:p>'), extras=[("word/_rels/document.xml.rels", b'<Relationship Target="https://invalid.example/secret"/>')])
    def forbidden(*args, **kwargs):
        pytest.fail("extractor must not acquire anything")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert extract(payload, DOCX).text == "label"
    assert extract('<a href="file:///secret">label</a>', "text/html").text == "label"


def test_deterministic_malformed_zip_corpus_never_escapes_or_breaks_spans():
    rng = random.Random(15)
    base = docx(word('<w:p><w:r><w:t>Synthetic</w:t></w:r></w:p>'))
    for _ in range(500):
        payload = bytearray(base)
        for _ in range(rng.randrange(1, 5)):
            payload[rng.randrange(len(payload))] = rng.randrange(256)
        result = extract(bytes(payload), DOCX)
        for section in result.sections:
            assert 0 <= section.start < section.end <= len(result.text)
        if result.status == "complete":
            assert not result.reasons
        if result.status in {"corrupt", "unsupported", "encrypted", "limit_exceeded"}:
            assert result.reasons and not result.text
