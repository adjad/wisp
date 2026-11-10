# Attachments batch 2: repairs after the independent review

Candidate reviewed: e8bb7cbc (BLOCK). Each finding has one owner. Nobody edits a file
they don't own; anything needed elsewhere is reported to the integrator.

## Shared contract (so the owners can work at the same time)

**Page facts in extracted text (owner R2, used by R1).** In `large` mode a PDF's text is:

```
[Pages: N]            <- first line, the PDF's true page count, always present in large mode
[Page 1]
text of page 1
[Page 4]              <- pages with no text have no block
text of page 4
```

- Any line of *file* text that could be mistaken for a marker (`[Pages: ...]`, `[Page N]`,
  `[Slide N]`, `[Sheet: ...]`, matched case-insensitively with optional surrounding
  space) is neutralised by the extractor by inserting a space after the bracket
  (`[ Page 9]`), in every large-mode extractor (PDF, pptx, xlsx cell rows), so markers in
  the text are only ever the extractor's own.
- `large_document.select(text, kind, *, pages=None, sheet=None, cut=False) -> Selection`
  uses `[Pages: N]` as the count when present (never `max(blocks)`), clamps asked ranges
  to it before building anything, and words what is missing truthfully:
  - beyond N: "Page 12 doesn't exist: it has 9 pages."
  - inside N, no block, `cut` false: "Page 3 has no text Wisp can read."
  - inside N, no block, past the last block, `cut` true: "Wisp didn't read as far as page 400."
  - no `[Pages: N]` line (slides, old stored text): never say "doesn't exist" or "It has
    N pages"; say "Wisp found no text for page N." instead.
- `large_document.page_facts(text) -> tuple[int | None, int]` = (declared page count or
  None, number of `[Page N]` blocks). R1 uses it for an honest "N of M pages had text".
- The `[Pages: N]` line is never shown as content: `retrieve`, `read_in_parts`,
  `head_and_spread` and `condense` drop it, and BM25 ignores it.
- R1 builds recognised scanned-PDF text the same way: `[Pages: total]` first, then a
  `[Page n]` block per recognised page.

## Owners

| Owner | Files | Findings |
| --- | --- | --- |
| R1 wiring | `service/mail_attachments.py`, `tests/test_attachment_reading_sharper.py`, `tests/test_mail_attachments.py` | 1, 3, 4, 5, 6, and the caller side of 2 |
| R2 pages | `service/large_document.py`, `service/document_extract.py`, `tests/test_large_document.py`, `tests/test_documents.py` | 2, 8, 12 (markers for other callers) |
| R3 child | `service/attachment_child.py`, `tests/test_attachment_child.py` | 7, 11 (child side), 40-page time bound |
| R4 store | `service/attachment_store.py`, `tests/test_attachment_store.py`, `service/mail_memory/api.py` | 9 |
| R5 app link | `app/Sources/WispApp/TextRecognizer.swift`, `app/Sources/WispApp/MailMemoryReader.swift`, `tests/MailMemoryReaderRegression.swift`, `service/mail_memory/bridge.py`, new `tests/test_recognize_bridge.py` | 10 |
