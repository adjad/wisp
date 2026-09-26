# Standalone linked-document extraction (A15a)

All fixtures are generated in memory in `tests/test_linked_documents.py` from
synthetic text and minimal ZIP/XML packages. No downloaded or user documents are
needed. Some fixtures deliberately contain fake URLs, malformed ZIP metadata,
or encrypted-container signatures; none initiate acquisition or encryption.

The new `service.linked_documents.extract_linked_document(content, media_type,
limits=...)` accepts caller-provided bytes or text. Supported labels are
`text/plain`, `text/html`, and the standard DOCX MIME type. It does not guess
formats. Text bytes must be UTF-8 or BOM-marked UTF-16; MIME charset parameters
do not change decoding. Invalid argument types/limits raise programmer errors.

`Extraction.text` contains normalized text. Each `EvidenceSection` supplies a
half-open `start:end` span in **Python characters in that output**, a block kind,
and a logical locator. These are not offsets into source bytes, XML, or rendered
pages. Whitespace-only blocks are omitted; retained blocks have one newline
between them. Plain-text lines and DOCX paragraphs retain internal whitespace;
HTML block whitespace is collapsed. DOCX table paragraphs retain document order.

Outcomes are `complete`, `empty`, `partial`, `encrypted`, `image_only`, `corrupt`,
`unsupported`, and `limit_exceeded`. Reasons are fixed codes, never source text
or exception messages. If completed sections survive a parser/output limit or
corruption, the outcome is `partial` with the relevant reason. Unfinished DOCX
paragraphs are discarded. Images are never OCRed. Encryption detection is a
signature-based handoff, not validation that a package can be decrypted.

Budgets cap input bytes, ZIP entries, each expanded part, total declared ZIP
expansion, compression ratio, parser events/depth, output characters, and section
count. Caller overrides can lower but not increase the built-in hard ceilings.
ZIP central-directory entries are counted before ZipFile allocates its index;
the actual main-part read is capped and checked by ZipFile's CRC validation.
ZIP64, split archives, and unsupported compression have no extraction fallback.
XML DTDs and entities are rejected. Archive contents are never written to disk.

This is a static text projection. HTML does not execute scripts, load CSS,
resolve links, or promise browser-equivalent visibility. Common non-content
elements and `hidden` subtrees are skipped. Tolerated unclosed/mismatched tags
produce partial results. DOCX extraction covers the main WordprocessingML body;
ancillary headers/footers/notes/comments, revisions, images, and embedded content
are explicitly omitted when detected. It does not validate every unopened ZIP
part or the entire OPC package. Full layout fidelity is outside this slice.
Word markup-compatibility AlternateContent triggers an explicit handoff instead
of combining mutually exclusive Choice/Fallback representations. HTML marked
sections (including legacy conditional declarations) are conservatively
unsupported even inside comments or scripts.
Foreign SVG/MathML start/end tags that require HTML tree reconstruction produce
`foreign_html_breakout`: `unsupported` without completed evidence, or `partial`
with earlier completed sections. Attribute-dependent `font` breakouts and
unmatched foreign end tags also receive this conservative handoff. The extractor
does not silently classify the remaining visible HTML as image-only content.
MathML `mphantom`, annotation subtrees, and element children after the first
`semantics` or `maction` child are omitted with `mathml_content_omitted`. Annotation
subtrees are conservatively omitted even outside `semantics`. Visible preceding
and following sections keep their output offsets; suppression remains active
through HTML integration points without disabling parser depth/event limits.

The existing `service/tools/builtin.py::_read_docx` remains the path-based
`python-docx` reader for read_file. This separate bytes-to-evidence API has no
runtime consumers yet. Reader, browser, mail, bridge, and runtime integration
require their own ownership ACK; full A15 depends on A08/A14.

Focused check: `python -m pytest -q tests/test_linked_documents.py`.
Required repository CI remains a separate exact-SHA gate. Untrusted parser and
resource-boundary review is required before shipping; this fixture note is not
an independent release or specialist-QA verdict.
