# Reading email attachments, large screens and large files

The contract between the four work slices. Anything named here is a public
interface another slice calls: keep the exact name, arguments and return shape.

## What the person asked for

- Wisp reads attachments in email: pictures (PNG, JPEG, HEIC and anything macOS
  decodes), spreadsheets, PDFs, documents, and it inspects what it can't open.
- Apple Mail and Gmail both work. Apple Mail's saved copy is tried first (instant,
  offline, no 7-day Gmail sign-in), Gmail is used for anything not on disk.
- Large screens and large files read well, not just the first part.
- All of it works across several emails in one request.

Facts about this Mac, measured 2026-11-09: 22,260 messages from two Gmail accounts
in Apple Mail; 904 of 2,723 attachments are saved; every attachment from the past
year is saved. Mail is not running, so its index stops at Nov 6 and newer mail is
only in Gmail: **always search both**. Emails dated 2030 are real scheduled mail:
a date more than 36 hours ahead is left out of "recent" results unless the person
gave a date range.

## Promises this must keep

1. A file is never run. Programs, scripts, installers and archives are only
   inspected as bytes; archives are never unpacked.
2. Attachment contents are untrusted. They are fenced, and after a read every
   action in that turn is confirmed (`mark_picture_turn`), with no Always allow.
3. Only when asked. Nothing reads attachments in the background.
4. Local only. If the Ask role isn't this Mac's engine, nothing is read.
5. Remote images in an email body are never downloaded (tracking pixels).
6. Never claim more than was read. Each reading states its coverage.
7. Trash, spam/junk and drafts are never read.
8. Tiny inline images (logos, signature icons) are skipped unless named.

## Slice A: Swift app

Files: `app/Sources/WispApp/MailMemoryReader.swift`, `MediaAttachment.swift`,
`PageReader.swift`, `OverlayModel.swift`, `tests/MailMemoryReaderRegression.swift`,
`tests/MediaAttachmentChecks.swift`.

`MailMemoryReader` answers two new `mail_memory_read` kinds, strict key sets like
the existing three, read-only as ever:

`kind: "attachments"`, keys `type, request_id, kind, terms, since, until, rfc_ids, limit`:
`terms` at most 5 strings, each of them must match (case-insensitive LIKE, escaped
and bound, never interpolated) subject, sender address, sender name or attachment
name; `since`/`until` epoch doubles (0 = no bound); `rfc_ids` at most 40 strings
(when non-empty only those RFC Message-IDs, bound parameters); `limit` 1...400.
Reply `{ok, capped, rows: [{rowid, ts, account, box, subject, sender, sender_name,
rfc_message_id, attachment_id, name, on_disk, size}]}`, one row per attachment,
newest first, `box` being `sent` or `other`, `account` the mailbox URL host as in
`indexRows`. Only `messages.deleted = 0`, and never a mailbox whose last path
component is trash, deleted messages, deleted items, bin, junk, junk e-mail, junk
email, spam or drafts (new `isHiddenMailbox(url:)`, same shape as `isSentMailbox`).
Handle a schema lacking `attachments` with `unsupported_schema`.

`kind: "attachment"`, keys `type, request_id, kind, rowid, attachment_id`:
`attachment_id` matches `^[0-9A-Za-z._-]{1,40}$`. Re-check at read time that the
row exists and passes the same filters. The file is
`<emlx dir's parent>/Attachments/<rowid>/<attachment_id>/<name>`, sibling of the
`Messages` folder (`on_disk` uses the same lookup, through the `emlx` map). Refuse
anything that isn't a regular file or resolves outside the Mail version folder.
At most `maxAttachmentBytes = 33_554_432`. Reply `{ok, name, size, data_b64}` or
`{ok:false, error}` with error one of `not_found`, `not_on_disk`, `too_large`,
`unreadable`, `no_mail_index`.

Large screens: add `MediaAttachment.maxDetailSide = 4096` and a helper returning a
JPEG at that side, stepping down to 2600 then `maxImageSide` until it is at most
6 MB. Use it at the two capture calls in `PageReader.swift` (also change those
captures from `.nominalResolution` to `.bestResolution`) and the two calls in
`OverlayModel.swift` (`attachPasted`, the file picker). Scanned PDF pages keep
`maxImageSide`. The backend cuts the picture up (Slice B). Still memory only.

## Slice B: vision and large documents

Files: `service/document_extract.py`, `service/documents.py`, `service/vision.py`,
`service/tools/builtin.py`, `service/attachment_child.py`, `service/large_document.py`,
`tests/test_documents.py`, `tests/test_vision.py`, new `tests/test_attachment_child.py`,
new `tests/test_large_document.py`.

`attachment_child.py` and `large_document.py` are written; test and fix them.

`document_extract.py`: argv[2] `large` makes the child use `LARGE_TEXT_CHARS =
600_000`, `LARGE_PDF_PAGES = 1500`, `LARGE_DOCUMENT_BYTES = 32 MiB`. `_Text`'s
default limit must read the current value at call time.

`documents.py`:
- `async run_child(code: str, argv: list[str], data: bytes, *, timeout: float) -> dict`:
  the spawn, footprint watch, deadline and kill from `extract_async`, sharing
  `_ONE_AT_A_TIME`; a child `{"error": ...}` raises `ValueError(plain sentence)`.
  `extract_async` is rebuilt on it and keeps its thread fallback.
- `async run_attachment_child(job: str, name: str, data: bytes, *, timeout: float) -> dict`:
  starts `service.attachment_child`; for `tiles` it makes a 0700 temp directory,
  passes it as argv[3] and always removes it afterwards.
- `async extract_async(name, data, *, timeout=..., large: bool = False)`; large mode
  returns up to `LARGE_TEXT_CHARS`, not `MAX_TEXT_CHARS`.
- `local_model()`: public `_local()`, raises `vision.VisionUnavailable`.
- `async local_chat(client, model, messages: list[dict], max_tokens: int, deadline: float) -> str`:
  the foreground gate, busy wait, power tier and no-thinking call from `answer_text`,
  returning the reply text.
- `answer_request` extracts in large mode. Up to `MAX_TEXT_CHARS` behaves as today.
  Longer text goes through `large_document.condense(...)` with a `PassBudget`, then
  one `answer_text`, with the deadline extended to `_LARGE_TIMEOUT_S = 150`. The
  reply ends with the coverage note instead of `TRUNCATED`. Update tests deliberately.

`vision.py`:
- `async expand_picture(url: str) -> list[str]`: a single picture larger than
  `TILE_WHEN_SIDE` on its long side goes to the `tiles` job; any failure returns `[url]`.
- `answer()` and `read_picture()` expand a single still (not video, not scanned pages).
- Several images from one picture: the prompt says the first is the whole picture
  and the rest are overlapping zoomed sections in reading order. Tokens and deadline
  scale: `_MAX_TOKENS` to 700, `READ_MAX_TOKENS` to 1400, deadline plus 5 s per section.
- `async read_images(prompt: str, images: list[str], *, what: str, sections: bool = False,
  client=None, model=None) -> str`: the `read_picture` call for images that are already
  prepared (at most 9), where `what` replaces the "a picture the person attached" text.

`builtin.py`: `read_file` no longer claims Wisp has no vision model; tell the model
the person can attach a picture in Ask. Don't touch `router.py` or `misc_t1.py`.

## Slice C: finding and reading attachments

Files: `service/mail_memory/bridge.py`, `service/gmail_source.py`, new
`service/mail_attachments.py`, new `tests/test_mail_attachments.py`.

`bridge.py`:
- `async read_attachment_index(terms, since, until, rfc_ids, limit=300, timeout=10.0) -> tuple[list[dict], bool]`
  (raises `BridgeError`).
- `async read_attachment(rowid, attachment_id, timeout=45.0) -> tuple[bytes | None, str, str]`:
  `(data, name, why_not)`.

`gmail_source.py` (read-only, use `bound_account` and `_get`):
- `MAX_ATTACHMENT_BYTES = 32 MiB`
- `async search_attachments(account, terms, start, end, limit=15) -> list[dict]`:
  `q = has:attachment` plus terms and `after:`/`before:` epochs, across all mail except
  spam and trash. Each `{account, message_id, rfc_message_id, ts, sender, subject,
  attachments: [{name, mime, size, attachment_id, inline, data}]}`.
- `async find_by_rfc_id(account, rfc_id) -> dict | None` (`rfc822msgid:`), same shape.
- `async attachment_bytes(account, message_id, attachment_id) -> bytes`.
- `raw_record` puts `[Attached: name (size), ...]` first in the body when a message
  has attachments, so `view_emails` shows it. Unchanged when it has none.

`mail_attachments.py`:
- `ATTACHMENT_TOOL = "read_email_attachments"`, `MAX_EMAILS = 10`, `MAX_READS = 8`.
- `async run(query=None, name=None, start=None, end=None, label="", count=5, account=None,
  question=None, list_only=False) -> str`: find, merge, select, then list or read.
- `async annotate_rows(rows: list[dict], timeout: float = 1.5) -> list[dict]`: best
  effort, adds `[Attached: ...]` to the start of `body` for rows whose `message_id` is in
  Apple Mail's index, skipping rows that already have it; returns rows unchanged when
  the app isn't connected or anything fails.

Finding: strip words like attachment, email, from, my, latest from `query` for `terms`.
Search Apple Mail and every connected Gmail account (or the one matching `account`),
tolerating either failing and saying so. Merge by RFC Message-ID (no brackets, casefolded,
send both forms to Swift): the local copy is preferred, the Gmail reference kept. Sort
newest first, apply the 36-hour future rule, take `count` emails (default 5, at most
`MAX_EMAILS`), filter by `name` when given, and skip images under 20 KB unless named,
counting what was skipped.

Fetching: local first. If it isn't on disk or fails, use the Gmail reference; if there is
none, try `find_by_rfc_id` on connected accounts. Otherwise a plain reason: not downloaded,
open it in Mail, or sign in to Gmail again.

Reading, at most `MAX_READS` per call, 150 s in all, one shared `large_document.PassBudget`:
call `documents.local_model()` first and stop with a plain sentence if unavailable
(`list_only` skips this). Every file goes to `run_attachment_child("inspect", ...)` first.
- `family` program, or a program/script/archive name: inspect only. Report real type,
  warnings, URLs, ZIP entries. Scripts and text-like names (sh, py, js, ics, vcf, vtt, svg
  and so on) are read as text, saying the script was not run, passing `<stem>.txt` to the
  extractor.
- Picture: `tiles` job, then `vision.read_images`, `sections=True` when tiled.
- Document: `extract_async(large=True)`, mapping docm/xlsm/pptm to docx/xlsx/pptx by name.
  A PDF with no text goes to `pdf_pages` then `read_images`. Then `large_document.condense`
  with a `chat` built on `documents.local_chat`.
- Old xls/ppt and unknown types: the plain reason.

Output must fit `email_digest.RESULT_BUDGET_CHARS` (6000) in total, sharing the budget as
it goes (what one file leaves unused flows to the next). Group by email: a Wisp-written line
with sender, subject, date and source, then each file's name, type, size, coverage note, and
its content inside `untrusted.fence(text, untrusted.EMAIL_ATTACHMENT)`. Names go through the
same cleaning as the child's `_pretty`. State what was left out and why.

## Slice D: tool and wiring

Files: new `service/tools/email_attachments.py`, `service/tools/__init__.py`,
`service/tools/short_descriptions.py`, `service/tools/untrusted.py`,
`service/tools/email_tools.py`, `service/agent/loop.py`, `service/model_only.py`,
`service/main.py`, `tests/test_tool_consolidation.py`, new `tests/test_email_attachment_tool.py`,
`CHANGELOG.md`, plus any other test that pins the tool list.

- Register `read_email_attachments` with category `email_read`: arguments `query`, `name`,
  `period` (`PERIOD_ARG`), `day`, `count`, `account`, `question` (what the person wants to
  know, passed through), `list_only`. Resolve dates as `view_emails` does, reject
  `_CORPUS_MAIL_IDENTIFIER` runs, call `mail_attachments.run`. Write the description the way
  `view_emails`' is written, stating reach: it matters for tool choice.
- `untrusted.py`: `EMAIL_ATTACHMENT = "email attachment"`, added to `_KINDS`.
- `email_tools.py`: `view_emails_impl` calls `annotate_rows` before digesting, not on corpus
  runs; its description mentions `[Attached: ...]` and the new tool.
- `loop.py`: mark the turn with `mark_picture_turn()` right before running this tool unless
  `list_only` is true, next to `turn_reads_untrusted`; add a `_SOURCE_LABELS` entry.
- `model_only.py`: add to `ASK_CORE_STUBS`; new `attachment_first(prompt)` for prompts that
  mention an attachment, or a file kind in or from an email. `main.py` puts the tool first in
  `head` beside `mail_first` when not writing.
- `tests/test_tool_consolidation.py`: the count goes from 93 to 94.
- `CHANGELOG.md`: one Added entry under Unreleased, plain language.
