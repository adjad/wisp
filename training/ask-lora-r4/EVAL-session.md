# Ask LoRA r4: session eval (multi-turn recall, persistent preferences, instruction following)

**Why:** r3's gate had no way to see the multi-turn failures it shipped with (garbled empathy, repeating the old answer on "really?", opening the wrong item for "the second one", "nothing has been said in our chats before", a preference lasting one turn). Toolbench histories were one turn long and the scorer could only check `contains_all`, `contains_any` and `not_contains`. This adds 55 gate cases (plus 1 dev probe) and the answer-shape checks they need.

## What exists now

- `scripts/toolbench/cases/session.yaml`: 55 `gate` cases and 1 `dev` probe. 47 have a short history (3 to 9 earlier exchanges, at most about 600 characters). 8 `_long` cases carry 7 to 10 exchanges with realistic 40 to 150 word assistant replies (about 2,500 to 4,500 characters), the volume r3 failed recall and preferences under; the production budget keeps roughly the last 20 exchanges inside about 9,000 characters, so none is near the cap. All are seeded through production's own session store (`runner._seed_session`, oldest first). Earlier assistant turns are written as ideal replies: they already obey any preference set above them, and a test enforces that.
- `scripts/toolbench/scorer.py`: new `answer:` checks, all optional and additive: `max_words`, `min_words`, `max_sentences`, `min_sentences`, `starts_with_any` (case-insensitive, whole word, after leading quotes or markdown), `regex_all`, `regex_none` (case-insensitive; `(?-i:...)` makes a part case-sensitive), `no_bullets`, `all_lines_bulleted`, `language_es` (Spanish stopwords clearly outnumber English ones; numbers ignored). `contains_*` now fold curly apostrophes and "6:40 PM" / "6:40 p.m." to "6:40pm"; the regex checks fold curly apostrophes. A failed check is `ungrounded_answer`, as for the existing checks, so `answer_ok` and the reports pick it up with no other change.
- `scripts/toolbench/cases.py`: validates the new keys at load time (types, regexes compile, no contradictory pairs) and `--ids` now accepts shell-style wildcards (`--ids '*_pref_*'`). The held-out shingle export already included history user turns (`heldout_shingles`); a test pins it.
- `tests/test_toolbench.py`: 12 new tests, including one that runs the real specs against known-wrong answers (invented phone, confirmation number or time; "26.2" for "6.2"; the wrong first question; "Nothing..." as an opener of No).

## What the cases measure

| skill | cases | what passes |
|---|---|---|
| recall of a fact the **user** stated, 3 / 6 / 9 exchanges back, and in the first exchange of an 8-exchange long chat | 4 (+1 "did I say" form) | answer from the conversation, no tool (the "did I say" form may use one lookup) |
| recall of a fact the **assistant** stated, 3 / 6 / 9 back, and in the first long reply of a long chat | 4 | answer from the conversation, no tool |
| fact **never said** (3 / 6 / 9 back, and a long chat) | 4 | an explicit refusal about the fact ("don't have", "no record", "never mentioned"; generic "let me know" does not count) and no invented phone, code, business or time (`regex_none`) |
| **re-read** a detail the earlier reply did not state (amount, merchant, body, "the second one") | 4 | the mail tool runs again; the answer carries the new fact |
| **"really? / are you sure? / check again"** | 4 | the source is re-read, then the answer is confirmed from the fresh read |
| "what was the **first thing** I asked" (5 and 8 back, and a long chat) | 3 | answer from the conversation; no "nothing has been said" |
| **persistent preferences**, honored 3 to 6 exchanges after they were set: one sentence (chat and tool), no questions (chat and a send), no bullets (chat and tool), Spanish (chat and tool), just the number (chat and tool) | 10 | the shape check holds on a turn that would otherwise break it |
| the same four preferences (one sentence, no bullets, no questions, Spanish) set in the first exchange of a **long** chat | 4 | still honored after 7 to 10 exchanges of long replies |
| **yes/no first** set in the session (yes, no) and asked in the sentence | 3 | verdict first, then the detail |
| preference **lifted** ("back to normal", "back to English") | 2 | the full answer returns; Spanish stops |
| **corrections** 2 to 3 turns deep (the place, the unit, a reminder time) | 3 | the earlier request is rebound; nothing is re-asked |
| **multi-part** requests | 3 | every part done, in the order asked |
| **exact-text** sends (the assistant's own draft, a quoted text, "say only") | 3 | the words go out verbatim, through a confirmation card |
| **length caps** (5 words, one line, just the time) | 3 | the cap beats "give full answers" |

Persistent-preference group: the 18 cases whose id contains `_pref_` (16 `sess_pref_*`, 2 `sess_yn_pref_*`).

## Running only these

Wisp must be quit (the harness refuses to start while :8765 or :8791 listens). One gate at a time, `--jobs 2` rules apply to anything that compiles; this does not compile.

```
# no model: load, bind, check every case against the host's routing
.venv/bin/python scripts/run_toolbench.py --dry-run --only session
.venv/bin/python scripts/run_toolbench.py --list --only session

# untuned vs candidate, interleaved, 5 trials, only the session gate
.venv/bin/python scripts/run_toolbench.py --models <untuned>,<candidate> --profile-as <untuned> \
    --only session --split gate --trials 5

# only the persistent-preference cases
.venv/bin/python scripts/run_toolbench.py --models <untuned>,<candidate> --profile-as <untuned> \
    --ids '*_pref_*' --trials 5

# one case, a variant world
.venv/bin/python scripts/run_toolbench.py --ids sess_really_balance --world-seed 7

# rescored offline, and compared with a baseline run
.venv/bin/python scripts/run_toolbench.py --report ~/.moe/evals/toolbench/<run> --compare ~/.moe/evals/toolbench/<baseline>
```

`--only session` selects by file stem; `--split dev` adds the one host-gap probe (`sess_reread_amount_bare`).

## Proposed r4 gate bars

Add to the table in `PLAN.md` (both are `hard`):

| bar | rule |
|---|---|
| **session cases** | `--only session --split gate`, **5 trials** interleaved: candidate pass rate **>= untuned + 10 points** (paired, same seeds). 55 cases x 5 = 275 trials a side. |
| **persistent-preference cases** | `--ids '*_pref_*'`, **5 trials**: candidate pass rate **>= 80%** (72 of 90 trials), and not below untuned. |

Both sit on top of the existing floor: unsafe calls no higher than untuned, `answer_ok` on answer cases no lower than untuned.

**How much noise these bars carry** (binomial, ignoring that trials cluster by case, which only widens it):
- Session bar: the standard error of the candidate-minus-untuned difference is about 4 points at 275 trials a side (a first draft with 47 cases x 3 trials had about 6). A true +10 gain clears the +10 bar about half the time; a true +17 clears it about 95% of the time; a true +3 clears it about 5% of the time. Treat a result within 5 points of the bar as inconclusive and rerun with other `--world-seed` values rather than ship or reject on it.
- Preference bar: 90 trials, so the standard error at 80% is about 4 points. A model whose true rate is 80% passes about half the time; one at 88% passes about 97% of the time; one at 72% about 3%. The same rerun rule applies.
- **Per-skill rates are diagnostic only.** Each skill has 2 to 5 cases, so 10 to 25 trials at 5 trials each; one trial is 4 to 10 points. Use the per-skill table to see where a failure sits, never to pass or fail a skill on a 10-point difference.

## Host limits found while writing the cases (not model failures; the gate cases avoid them)

Both live in `service/` and cap what any tuned model can score on real conversations:

1. **`knowledge_question()` reads the prompt alone.** A follow-up with no personal marker ("how much was that statement for?", "how much was it and where?") is classed general knowledge and the turn runs tool-free (`ask_direct`, zero tools offered, "No tool is needed and none is available"), so the model cannot re-read the mail. This is a likely cause of r3's "I haven't read an amount yet". Gate cases say "my" ("how much was my statement for?") so the model is judged on its own behavior; `sess_reread_amount_bare` (dev) keeps the natural wording and fails for every model until the host looks at the history. A test (`test_gate_session_cases_are_winnable_by_the_host`) fails if a gate case that expects a tool lands on a tool-free turn.
2. **`asks_personal_data()` matches "did I say ...", "should I ...", "have I ..." and forces a tool.** For a recall answered from the conversation alone, `loop.py` re-prompts once, and if no personal source ran it replaces the final with "I don't have that. Nothing I can check ... records it." even though the user stated the fact two turns earlier. The recall cases are phrased without that trigger (a test enforces it); `sess_user_fact_said_4` keeps the "did I say" form and allows one lookup, which measures the path real users hit. A host fix would let a fact found in the recent history count as grounded.
3. A prompt that **opens as a question** and asks for a write ("what time is the dentist again, and remind me ...") forbids every write tool. The multi-part case leads with the action.

## Limits of the runner

- The turn under test is always one prompt after a seeded history of 3 to 10 exchanges (the long cases reach 4,500 characters). A preference given in the same turn as the request, or one that must survive 20 exchanges or more than the production budget keeps (about 9,000 characters), is not covered.
- Earlier tool results are not stored in production and are not seeded here either (only the user text, the assistant text and an optional `calls` entry that feeds the "Current subject" line).
- `language_es` is a stopword ratio: it separates Spanish from English prose, not Spanish from Portuguese or Italian.
- The chat judge (blind preference) still sees only the last message; multi-turn chat quality beyond these cases is not judged.
