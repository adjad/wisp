# Wisp assistant acceptance corpus

The Ask runner now evaluates the local agent model using real, bounded source reads. Its test targets live in a private local profile. Answers and source results appear together in Wisp so the person running the test can examine them.

## Live cases in Ask

```text
/corpus list
/corpus batch-1
/corpus smoke
/corpus contacts
/corpus messages
/corpus mail
/corpus all
/corpus LIVE-001
/corpus profile
/corpus help
```

The live catalog contains **100 requests**: 10 Contacts, 45 Messages, and 45 Mail cases. `/corpus list` displays every configured request and its provenance without inference or source reads. `/corpus batch-1` through `/corpus batch-10` run ten stable IDs at a time; `/corpus LIVE-100` runs one. A full run is sequential and can take substantial time. Stop cancels the current case and remaining queue.

`smoke` selects one contact, one Messages conversation, and one mail identifier when configured. `all` runs all 100 when both contact and mail targets are configured. Missing target categories are omitted without renumbering IDs; a batch may therefore have fewer than ten cases. Contact cases show saved contact details; Messages cases read at most five matching messages for the selected conversation; Mail cases search at most three recent matching emails with strict matching. Today, yesterday, this-week and unread cases freeze those filters on the server before inference; the model cannot broaden them. Other cases use the latest bounded records. These excerpts cannot prove a complete day, week or conversation was examined. An incomplete email identifier remains an unverified search term. Missing access, syncing, cache coverage, ambiguity and no matches must be disclosed.

Each live case bypasses deterministic workflow/router shortcuts and calls the local agent model to select the bounded read and synthesize the answer. Cards count completed inference responses, name the model, and expose expandable source results. A route event alone is not evidence that the model ran. Review answers against their actual source records and checklist; there is no fixed correct answer or automatic quality score for changing personal data.

The backend exposes only the server-owned case IDs, binds requests to the current profile version, permits only scoped `lookup_contact`, `view_messages`, and `view_emails` calls, caps record counts, and rejects other tool calls before preparation and again at execution. It ignores Full access for purposes of widening the case scope. Sends, drafts, writes, shell, web, skills and tools for other people are unavailable. Private source results stay on the managed local inference endpoint; there is no remote fallback. A cancelled run cancels inference and releases its client.

The profile is `assistant-corpus-profile.json` under Wisp's state folder (`~/.moe` by default, or `WISP_HOME`). It contains `contacts` and `mail_queries` arrays. It is configured locally and is not a repository asset. For example:

```json
{"contacts": ["Example Person"], "mail_queries": ["example@sender.invalid"]}
```

Live corpus runs do not create backend chat sessions or read saved chat history. Results stay in the current app conversation for inspection until the app closes. Existing source caches and local tool audit records may be refreshed or updated by ordinary source readers. Debug payload capture is disabled. Real source content and the local profile must not be committed or uploaded as test artifacts.

## Live case construction

`service/corpus_cases.json` contains the versioned 100-case template catalog with stable `LIVE-001`–`LIVE-100` IDs, distinct intents, target slots, provenance and fixed date/unread filters. Targets come from the private local profile. Changing target lists can change a case's selected person; its token changes accordingly. The first eight IDs retain the original three-contact/two-mail mapping for that profile shape.

Five request patterns were adapted from nine relevant user requests found in a bounded review of 60 recent saved Wisp chats. They cover checking mail, today's mail, unread mail, catching up on messages, and a text-only message draft. The other 95 requests are newly authored realistic requests. This is **not 100 verbatim historical conversations** or a representative sample of user traffic. No raw private chat transcript or source record is committed. Provenance appears in each case's setup and in `/corpus list`.

Requests test summaries, exact quotations, sender attribution, dates, plans, action extraction, uncertainty, comparisons, visible coverage, and text-only reply drafts. These are single-turn evaluation requests with review rubrics; no assistant completion targets or model weight updates are included.

## Original synthetic corpus

The repository still contains 48 wholly synthetic JSONL cases: 12 smoke, 24 regression, and 12 challenge cases, with setup notes, preferred tools and behavior rubrics. These are evaluation data, not supervised training targets.

```text
/corpus synthetic smoke
/corpus synthetic regression
/corpus synthetic challenge
/corpus synthetic all
/corpus ATC-001
```

These commands retain the original routing-only dry run. They execute no tools, seed no fixtures, and deterministic routes can return without model inference. They cannot verify answers against real or seeded source data. For fixture-backed evaluation, use an isolated test profile/harness and intercept all writes and outbound actions.

Validate the static synthetic rows and their app-bundled copy with:

```sh
python3 training/assistant-task-corpus-v1/validate.py
```

Focused implementation checks include `tests/test_live_corpus.py` and `scripts/test_chat_contract.sh`. The backend checks use synthetic sources and scripted inference; only an explicit live run reads the user's configured data.
