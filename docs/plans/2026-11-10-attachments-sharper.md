# Email attachments, batch 2: sharper reading

Base: main 4f57adef (PR #500). Branch: `worktree-attachments-sharper`.
The eight promises in `2026-11-09-email-attachments.md` still hold, unchanged:
nothing is run or unpacked, contents are untrusted and fenced, every later
action in the turn is confirmed, reading happens only when asked, only with a
local model, no remote images, no trash/spam/drafts, tiny inline images skipped.

## What this adds

1. **Exact text from pictures and scans.** The app's on-device text recognition
   (Apple Vision) reads pictures and scanned PDF pages. The vision model is kept
   for layout and for pictures that are not text.
2. **Remembered files.** Extracted text is kept on this Mac so a follow-up
   question about the same file needs no fetch and no re-read.
3. **A specific part.** `pages` and `sheet` arguments; answers say which page or
   sheet a passage came from.
4. **Invites and contact cards.** `.ics` and `.vcf` are read as structured data.
5. **A risk verdict.** One plain line when a file looks risky.

## Interfaces (the contract between the pieces)

### A. Text recognition (Swift + bridge)
- New Swift file `app/Sources/WispApp/TextRecognizer.swift`:
  `TextRecognizer.recognize(jpeg: Data) -> (text: String, lines: Int, confidence: Double)?`
  `VNRecognizeTextRequest`, `.accurate`, language correction on, synchronous on the
  caller's queue. Reading order: observations grouped into rows (vertical overlap),
  rows top to bottom, cells left to right joined with a TAB, rows joined with `\n`.
  Output capped at 200,000 characters.
- `MailMemoryReader` gains kind `recognize`:
  request `{type, request_id, kind: "recognize", image_b64}` — strict keys, base64
  of at most 4 MiB of JPEG (must start `FF D8 FF`; nothing else is decoded).
  reply `{ok: true, text, lines, confidence}` or `{ok: false, error}` with error
  in `bad_image | too_large | no_text | failed`. `no_text` = recognition ran and
  found nothing.
- `service/mail_memory/bridge.py`:
  `async def recognize_text(jpeg: bytes, timeout: float = 20.0) -> tuple[str, float]`
  returns `(text, confidence)`, `("", 0.0)` for `no_text`; raises `BridgeError`
  for everything else (including a non-JPEG or over-size input, refused locally).
- Only JPEGs that Wisp's own child process re-encoded from decoded pixels are ever
  sent. The app never decodes the sender's file bytes for this.

### B. Text store (`service/attachment_store.py`, new)
SQLite at `paths.moe_dir() / "cache" / "attachment_text.db"`, file 0600, dir 0700,
WAL off (one small file), opened per call, every function safe to call from
`asyncio.to_thread`. Never raises to callers for a broken/locked db: reads return
None, writes return False.

```python
VERSION = 1                      # bump to invalidate every stored reading
MAX_TEXT_CHARS = 600_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_AGE_S = 30 * 86400           # since last use

def digest(data: bytes) -> str                                   # sha256 hex
@dataclass
class Stored: digest: str; kind: str; text: str; cut: bool; note: str; how: str
def get(digest: str) -> Stored | None                             # touches last_used
def put(digest: str, *, kind: str, text: str, cut: bool, note: str = "", how: str = "",
        account: str = "") -> bool
def locate(locator: str) -> str | None                            # locator -> digest
def remember(locator: str, digest: str, *, account: str = "") -> bool
def forget_account(account: str) -> int                           # rows removed
def forget_all() -> int
def prune(now: float | None = None) -> int                        # age, then size (oldest first)
```
`kind` is the extraction kind (`pdf`, `xlsx`, `txt`, `ocr`, ...). `how` is a short
Wisp-authored label of how the text was got (`text`, `recognized`). `account` is
the mailbox label the file came from, lower-cased, so disconnecting an account
erases its rows. A digest can belong to several accounts (table of
`(digest, account)`); the text row goes when its last account goes. Locators are
opaque strings chosen by the caller, at most 300 characters.

### C. Child process (`service/attachment_child.py`)
- `inspect()` adds `risk`: `{"level": "high" | "caution" | "", "reasons": [str]}`.
  Reasons are Wisp's own fixed sentences only, never text from the file. `warnings`
  stays as it is for compatibility.
  high: a program/installer/disk image/script that runs on double-click; a name
  dressed up as a document (`invoice.pdf.exe`, hidden direction characters, bytes
  that are a program under a document name); a shortcut file (`.lnk`, `.url`,
  `.webloc`, `.inetloc`, `.desktop`); an archive whose listing holds programs.
  caution: Office file with macros; password-protected archive or PDF; an HTML
  file with a password field or a form (scan first 512 KiB of text); a file whose
  bytes match no known type under a document name.
- New job `structured` for `.ics` and `.vcf`, pure Python, bounded:
  `{"kind": "calendar", "method": str, "events": [{summary, start, end, all_day,
  timezone, location, organizer, attendees: [str], attendee_count, repeats,
  status, description}], "event_count": int}` (at most 20 events, attendees 12),
  or `{"kind": "contacts", "contacts": [{name, organization, title, phones,
  emails, addresses, note}], "contact_count": int}` (at most 20). Every string is
  passed through `_pretty` with a sensible limit. Dates are returned as written
  (`2026-11-14 09:30`, with the TZID in `timezone`, `Z` -> `UTC`), not converted.
- New job `ocr_images`: JPEG data URLs for recognition.
  pictures: one image, longest side at most 3000, quality so the JPEG is <= 3 MiB.
  PDFs: the embedded page pictures of up to `MAX_OCR_PAGES = 40` pages, same limits,
  plus `{"pages": total, "read_pages": [page numbers]}`. Result
  `{"images": [...], "pages": int, "read_pages": [...]}`.

### D. Parts of a file (`service/large_document.py`, `service/document_extract.py`)
- PDF text gains `[Page N]` marker lines **in `large` mode only** (other callers
  see the same text as before). `[Sheet: name]` and `[Slide N]` already exist.
- `large_document.select(text, kind, *, pages: str | None, sheet: str | None)
  -> tuple[str, str, str]` = `(selected_text, note, problem)`.
  `pages` accepts `"12"`, `"3-5"`, `"1,4,9-10"` (at most 50 pages); for pptx it
  selects slides. `sheet` matches a sheet name case-insensitively, exact first,
  then unique substring, then a 1-based number. `problem` is a plain sentence when
  nothing matches (and names what exists: "It has 9 pages." / the sheet names,
  which are untrusted and must go through the caller's fence, so return them in a
  separate list: `select(...) -> Selection(text, note, problem, available: list[str])`).
- `retrieve()` and `read_in_parts()` label what they return with the page, slide
  or sheet it came from when markers are present ("[Page 12]" kept at the head of
  each passage), so answers can say where a figure is.

### Integration (integrator only)
`service/mail_attachments.py`, `service/tools/email_attachments.py`,
`service/vision.py`, `service/gmail_source.py`, `service/tools/short_descriptions.py`,
`service/model_only.py`, `scripts/run_simulation_qa.py`, `CHANGELOG.md`, this file.

## Ownership
| Worker | Owns (nobody else edits these) |
| --- | --- |
| A | `TextRecognizer.swift`, `MailMemoryReader.swift`, its regression file, native compile lists, `service/mail_memory/bridge.py`, bridge tests |
| B | `service/attachment_store.py`, `tests/test_attachment_store.py` |
| C | `service/attachment_child.py`, `tests/test_attachment_child.py` |
| D | `service/large_document.py`, `service/document_extract.py`, `tests/test_large_document.py`, `tests/test_documents.py` |
| Integrator | everything under Integration |
