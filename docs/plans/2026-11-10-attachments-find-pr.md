## Summary

- Find a file by what is inside it: `read_email_attachments` can search the readings Wisp has kept and read a few more of the best matches.
- A background switch, off by default: reads Mail's saved copies from the last 30 days while you are away, with no model.
- Questions across many files, with totals and counts added by Wisp's own code, not by the model.
- Save a file to Downloads or another folder (`save_email_attachment`): the card names the exact file, it never overwrites, risky files are refused, and the file is marked as downloaded.
- Files sent in Messages are read like email files (`read_message_attachments`).
- Recordings are transcribed on this Mac (first 10 minutes), and emails attached to emails are read with up to 3 of their files.

Not in this change: attaching a file to a new email (forwarding already carries files), reading Gmail or Messages files in the background, videos, Outlook `.msg` files, and a Settings button for the background switch.

## Rules kept and the one new rule

Kept: files are read only when asked, only with a model on this Mac; nothing is run or unpacked; what a file says is untrusted text, so everything else in that chat still asks first; nothing leaves this Mac; kept readings follow the store's rules (this Mac only, mode 0600, 30 days unused, 200 MB, erased with the account and with Forget).

New: background reading. It is off by default, uses no model, reads only copies Mail already saved on this Mac from the last 30 days, never downloads from Gmail, never reads Messages files, stops for chats, Low Power Mode and a low battery, and reads at most 40 files a day.

## How it was built

- `service/attachment_store.py`: new tables for facts, tried files and file-name search of kept readings.
- `service/attachment_fields.py`: short facts from a reading and totals added by code.
- `service/attachment_index.py`, `service/attachment_api.py`: the background job and its app-only switch.
- `service/attachment_child.py`: recordings (converted to short WAV windows), attached emails, and inspection.
- `service/attachment_audio.py`, `service/voice.py`: transcribing a recording with the local model.
- `service/mail_attachments.py`: finding by contents, many files at once, Messages as a source, files inside emails.
- `service/attachment_save.py`, `service/tools/attachment_actions.py`: prepare, card and save, with a signed one-use token.
- `service/tools/email_attachments.py`, `service/tools/registry.py`, `service/safety/*`: the two new tools and their grants and policy.
- `service/mail_memory/bridge.py`, `app/Sources/WispApp/MailMemoryReader.swift`: the Messages files reader on the Mail bridge.

## Tests

New files: `tests/test_attachment_fields.py` (TBD), `tests/test_attachment_audio.py` (TBD), `tests/test_attachment_sources.py` (TBD), `tests/test_attachment_find.py` (TBD), `tests/test_attachment_index.py` (TBD), `tests/test_attachment_save.py` (TBD), `tests/test_attachment_tools.py` (TBD), `tests/test_attachment_seams.py` (TBD), `tests/test_message_attachment_bridge.py` (TBD). Extended: `tests/test_attachment_child.py`, `tests/test_attachment_store.py`, `tests/test_voice_transcribe.py`, `tests/test_tool_consolidation.py` and a few policy tests. Swift: `tests/MailMemoryReaderRegression.swift`. All are added to the Simulation QA list in `scripts/run_simulation_qa.py`.

Results: Python passed TBD, failed TBD, skipped TBD. Swift regression: TBD. Simulation QA: TBD. CI: TBD.

## What was not tested

- The Swift side was not compiled locally when this was written, because another job held the shared build lock. CI compiles it.
- Nothing was tried in the running app.
- No live Gmail fetch.
- No real Messages database.
- No real recording through the real model.

## Review triggers under AGENTS.md

- Security or privacy boundary: a new action that writes a file to disk, and a new reader of Messages files.
- Native integration: new kinds of request on the Mail bridge.
- Persisted data: new tables in the attachment store.
- A background job that reads files without being asked.

An independent Release Auditor verdict on the exact SHA is needed before merge.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
