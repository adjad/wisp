# Ask LoRA r4 chat eval

Shows whether the candidate's chat **clearly improves** on the untuned base without regressing (PLAN.md, "Gate bars for r4", chat rows). r3's judge saw only the last user message, so it could not judge recall or coherence (multi-turn came out 11-13). This one shows the judge the whole conversation, and scores recall and standing instructions mechanically.

| file | what |
|---|---|
| `prompts_r4.yaml` | the 40 r2/r3 units, byte-for-byte (same ids), plus 12 new multi-turn conversations (`mt_*`, 8-9 user turns each). 52 units, 152 model turns per sample. |
| `chat_eval.py` | runs models through Wisp's real model-led Ask turn, N samples, writes `replies.jsonl` with the full conversation per row. `--dry-run` validates the prompts. |
| `judge_pairs.py` | `build` blind whole-conversation A/B pairs, `mech` the mechanical checks, `score` the PLAN.md chat bars. |
| `chatlib.py` | prompts schema and validation, sentence/word/bullet/question/emoji measures, non-reply and leak detectors, the checks. |
| `test_chateval_r4.py` | 65 tests with fake replies. |

## The 12 new conversations

| skill | ids | what later turns need |
|---|---|---|
| recall (4) | `mt_recall_trip`, `mt_recall_gift`, `mt_recall_move`, `mt_recall_self` | a name, number or object the **user** said 3-8 turns earlier; `mt_recall_self` also checks the model's own "say that again, shorter" (overlap, shorter) and a nickname against the world profile's name |
| standing instruction (4) | `mt_stand_onesentence`, `mt_stand_noquestions`, `mt_stand_nolists`, `mt_stand_lifted` | one sentence / no question at the end / no bullets / under 20 words, then lifted (full answers return). Checked on **every** reply after the instruction |
| emotional thread (2) | `mt_emo_breakup`, `mt_emo_pet` | continue without inventing people or facts (`forbid`), recall the one fact given, no repeated reply |
| casual fact mid-chat (2) | `mt_fact_plant`, `mt_fact_crochet` | answer a passing factual question (212, seven, 16, 100), then go back to the thread and recall what the user said |

Design rules, enforced by `test_chateval_r4.py`:
- The model writes every assistant turn itself and each model continues from its own replies. Recall targets are things the user said; the other checks hold whatever the model answered earlier.
- None of the probed details (names, numbers, objects) appears in `training/ask-lora-r2/builder/*.py` or `scripts/toolbench/cases/*.yaml`, and no user turn is within the leak index's 0.7 trigram similarity of a training or toolbench prompt.
- No user turn trips `service.model_only.asks_personal_data`, so the eval measures the model, not the host quirk in `EVAL-session.md` (limit 2).
- Every check is shown satisfiable (an "ideal" fake model passes all 12) and falsifiable (a "bad" fake model fails all 12).

## Running the gate

Wisp quit, oMLX up, nothing else holding the heavy lock; `gate.sh` does the checks and releases the lock. Chat stage only:

```
cd ~/Desktop/MOE_Project            # the checkout under test
PY=.venv/bin/python
CH=training/ask-lora-r4/chateval

# 0. anywhere, any time: no model, no Wisp
$PY $CH/chat_eval.py --dry-run
$PY -m pytest $CH -q

# 1. the whole gate (chat is one stage). With the gate.sh edit below, chat runs by default:
ONLY=chat training/ask-lora-r3/gate/gate.sh gemma-4-E4B-it-oQ4e-mtp <candidate-model-id> 3 3
#    or without editing gate.sh:
CHATEVAL=$PWD/$CH/chat_eval.py CHAT_PROMPTS=$PWD/$CH/prompts_r4.yaml \
  ONLY=chat training/ask-lora-r3/gate/gate.sh gemma-4-E4B-it-oQ4e-mtp <candidate-model-id> 3 3

# 2. by hand, same thing (A is the baseline, profile-as A gives B A's per-model treatment)
$PY $CH/chat_eval.py --repo . --models A,B --profile-as A --samples 3 --out RUN/chateval
#    --only 'mt_*' for just the new conversations; --resume continues an interrupted run
```

3 samples = 156 pairs; 5 samples = 260 (more power for the p < 0.05 bar). Time: 456 turns per model at 3 samples, roughly 40 to 60 minutes each.

### Judge, then score

```
RUN=<out>/chateval
$PY $CH/judge_pairs.py build --replies $RUN/replies.jsonl --base A --cand B --out $RUN/judge
#   -> pairs_for_judge.jsonl, pairs_key.json (the A/B key), chunk_NN.jsonl (one judge prompt per pair), judge_instructions.md
# Judge every line of every chunk with an independent model (one subagent per chunk, as in r3). Each answers one JSON line:
#   {"pair_id", "overall_winner": "A"|"B"|"tie", "confidence": 1-3, "flags": {"A": [..], "B": [..]}, "rationale"}
# Save them as $RUN/judge/judgments_NN.jsonl.  The judge is blind: the prompts carry no model name.
$PY $CH/judge_pairs.py mech  --replies $RUN/replies.jsonl --base A --cand B --details      # no judge needed
$PY $CH/judge_pairs.py score --replies $RUN/replies.jsonl --key $RUN/judge/pairs_key.json \
    --judgments "$RUN/judge/judgments_*.jsonl" --out $RUN/judge/score                       # exit 0 pass, 1 fail, 3 incomplete
```

`score` refuses a verdict (exit 3) unless every pair is judged.

## What `score` applies

| PLAN.md bar | rule here |
|---|---|
| overall win | candidate wins > base wins on all pairs, exact two-sided sign test p < 0.05 (ties dropped). The unit-level test (samples of a unit are not independent) is printed beside it |
| single-turn categories | per category over single and variety pairs: base wins minus candidate wins <= 2 |
| non-replies | candidate <= base and 0 in multi-turn conversations, by the mechanical detector (empty/error/timeout, a missing turn, a stall such as "I couldn't find any results" or "I don't have your home city yet") **and** by the judge's `not_a_reply` flag |
| leaks | no reply with a snake_case tool name, "the x-y skill", `UNTRUSTED`, tool-call JSON (`--max-leaks`, default 0; `--tool-names FILE` adds exact names) |
| median reply | candidate median chars >= 0.8x base |
| emoji | share of replies with an emoji >= 0.5x base |

Also reported and **advisory** (hard with `--strict-mech`): the candidate must not be below base on mechanical conversation pass rate, recall of user details, and standing instructions obeyed per reply. These are proposed additions to PLAN.md; the table in the report shows each skill's rate.

The non-reply and leak detectors are heuristics (regexes in `chatlib.py`, tested against the r3 examples and the r3 run's real replies); the judge flags are the second opinion, and a surprising count deserves a read of `mech --details`.

## Output format

`replies.jsonl`, one row per model turn: `run, model, unit, kind, category, skill, sample, seed, variant, turn, n_turns, user, history_turns, reply, conversation, turn_seconds, error, timeout, escapes, metrics, steps`. `conversation` is the full transcript up to and including that reply. The keys are those of the r2/r3 rows, so old runs load into `judge_pairs.py` too (their missing `mt_*` turns count as missing).

## Not covered

- Tool calls a past turn made are not in the next turn's history (production keeps a digest). Chat prompts rarely call tools.
- `training/ask-lora-r2/builder/leak.py` indexes only `chateval_prompts.yaml`. The training builder should also deny `prompts_r4.yaml`'s user turns: change `load_chat` to read both files.
- Audio/vision/toolbench stay in `gate.sh`; the toolbench `session` cases (EVAL-session.md) are the other half of the multi-turn bars.
