# Ling dataset V3.2: V3.1 migrated to the current Wisp registry, full + terse schemas

V3.2 is the V3.1 training set (`../ling-dataset-v3.1-20261013`, frozen and untouched) replayed against the Wisp tool registry
as it exists on `origin/main` 1bd68e7e (after PR #208's 182 -> 100 consolidation and PR #224's removals), with about half of
the conversations rendered in the opt-in **terse** schema mode, plus one authored category, `v32_mac_controls`, for the
mac-controls skill tools (60 train / 8 dev conversations). Nothing here loads a model, calls oMLX, executes a Wisp tool or
reads `~/.moe`.

## Why

1. V3.1's `assets/tools.json` (165 schemas), system prompts and catalog predate two consolidations. Its rows name tools that
   no longer exist (`set_alarm`, `archive_email`, `air_quality`, `delete_path`, ...), and its rule paragraphs mention retired
   tools ("- For volume, call", "- For music:", `archive_email` mailbox upkeep). Training on it would teach Ling to call
   tools Wisp no longer has.
2. Wisp is gaining `WISP_TERSE_TOOL_SCHEMAS` (one-line descriptions, no parameter descriptions; PR #227). It ships default-off
   and flips only after a model trained for it passes eval, so the model must work with BOTH full and terse schemas.

## What changed from V3.1

| | V3.1 | V3.2 |
|---|---|---|
| registry | 165 schemas copied from an eval run | 97 schemas captured from Wisp source (`capture_assets.py`): the 91 built-ins `tool_schemas()` returns (`update_event` is registered but unavailable), `use_skill`, and the mac-controls skill's `mac_get_volume`, `mac_set_volume`, `mac_mute`, `mac_lock_screen`, `mac_media` (category `skill_tool`, family `skills`, always confirmation-gated). 96 are offered; `run_applescript` is blocked by the private-shell rule the identity block always triggers |
| system message 0 | captured variants + hand-written trigger map | `loop.build_system(loaded)` reproduced from `loop.SYSTEM`'s blocks (gating names from each block's first line, exactly as Wisp computes them) + identity block (synthetic Test User) + installed-skills catalog (idea-refine, interview-me, mac-controls; a skill body is appended when its triggers match the user's text) |
| system message 1 | captured catalog | now line + `CapabilityCatalog.prompt()` (with/without web variant as before) |
| offer order | loaded, `get_tool_schemas`, stubs | loaded, stubs (sorted), `get_tool_schemas` LAST, as `loop.model_led_offer` sends it |
| schemas | full only | `schema_style` per conversation: `full` (tools.json byte-for-byte) or `terse` (short line, parameter descriptions stripped) |
| content | | every user/assistant/tool text identical to V3.1 except where a recorded migration rewrote it |
| new category | | `v32_mac_controls` (ext_v32_mac.py), see below |

The discovery protocol itself (two loading steps, 24 schemas, implicit stub discovery, receipt JSON, rejection text) is
unchanged on `origin/main` and was re-verified against `service/router/model_led.py` and `service/agent/loop.py`.

Tool-call conventions are unchanged: OpenAI-style `tool_calls` with `function.arguments` as JSON objects, one row per
assistant step, only the last message of a row has `training: true`.

### Migration rules (see DATA_CARD.md for the counts and every dropped conversation)

- Merged tools -> the surviving tool with its behaviour argument, from `service/safety/grants.py` (`_MERGED_SCOPES`,
  `_RETIRED_INTO`) and `tests/test_tool_consolidation.py`: e.g. `set_alarm(time)` -> `set_timer(kind="alarm", time)`,
  `archive_email(message_id)` -> `email_triage(message_id, action="archive")`, `mark_email_read(read)` ->
  `email_triage(action="mark_read"|"mark_unread")`, `get_battery_status` / `network_info` -> `system_status(section=...)`,
  `air_quality` / `rain_radar` / `weather_alerts` -> `get_weather(include=[...])`. Stub calls, explicit
  `get_tool_schemas` tool lists, receipts and guard texts are rewritten with them.
- Retired outright, honest successor available: `astronomy` and `recipe_lookup` -> `web_search` with the old figures as the
  top result (synthetic `example.com` listing, the same shape V3.1's web rows use), final answer cites the domain.
  `convert_currency` has no unit-converter successor; its two conversations ran without web access, so they are dropped.
- Retired with no successor: `airdrop_file` (3) and `place_call` (1) conversations are dropped.
- Defined for re-runs but unused by this source set: volume/lock/music -> `mac_*` skill tools, `trash_file`/`delete_path` ->
  `move_path` to `~/.Trash/<name>` (only for an explicit trash/delete request naming the file; vague "delete everything"
  stays a no-tool clarification), grocery evidence -> `search_notes`/`search_reminders`, timers, wifi, flags, `wisp_sync`,
  `switch_app`.
- Retired tools that were only loaded as decoys (SP preloads, explicit discovery lists) map to their survivor or are removed.
  One conversation's stub step disappeared because its tool was already preloaded after the merge (`weather_alerts` decoy ->
  `get_weather`), turning it from SP-decoy into SP-hit.

## New category: `v32_mac_controls`

`ext_v32_mac.py` authors 60 train / 8 dev conversations (12 train families, 4 dev families with their own phrasing
registers; deterministic from seed 20261014; held-out overlap < 0.4; terse/full split like every other category). They use
the same protocol as the migrated rows (`generate.Builder`): stubs or `get_tool_schemas` for the `skills` family (where
`model_led.family_for` puts category `skill_tool`), SP preloads (hit and decoy), receipts, two loading steps. Results are the
skill's real strings (controls.sh through the skill runner; a non-zero exit becomes `(exit code 2)\n<stderr>`), the host's
argument-validation error, and the host's confirmation denial `The user denied this action.` (service/agent/loop.py; V3.1's
sends used a synthetic `The user declined; nothing was sent.`, but the skill tools go through the generic confirm path).

| family | gold behaviour |
|---|---|
| explicit level | `mac_set_volume(level=<int>)`, "50%" -> 50; volume 0 answered as zero, not muted |
| relative ("turn it down", "louder") | `mac_get_volume` first, then `mac_set_volume` about 20 lower/higher, clamped to 0-100 |
| read ("what's my volume", "am I muted") | `mac_get_volume` only |
| mute / unmute | `mac_mute(state="on"/"off")` |
| lock / lock failure | `mac_lock_screen`; on "sent the Lock Screen shortcut, but macOS did not report the screen as locked" or "could not lock the screen: ..." the answer says the screen is NOT locked and why; no retry |
| playback | `mac_media(action)` with `app` omitted (Apple Music), `app="spotify"` only when Spotify is named |
| specific song/album/playlist | no call; says mac_media can't pick music, offers to resume or open the app; accepting in a follow-up -> `open_app` |
| declined confirmation | says nothing changed; no retry; never `run_shell` / `run_applescript` |
| refused argument | `level: "50%"` (host type error) or `level: 110` (skill range error) is a context-only step; the target is the corrected call (or 100 for "max it out") |

validate.py enforces this category's rules: no `run_shell`/`run_applescript` call or stub, executed levels are integers 0-100,
mute is never a volume change, volume questions only read, relative changes read first and move about 20 (or clamp), Spotify
only when named, no `mac_media` for specific music, no call after a denial, and a failed lock is reported as NOT locked.
Refused context-only calls are the only schema-invalid calls allowed, and only in this category.

## Files

| file | purpose |
|---|---|
| `train.jsonl`, `dev.jsonl` | rows (gitignored; regenerate byte-identically). Migrated V3.1/V3 rows first, then V3.1's V2 replay rows unchanged |
| `train.provenance.jsonl`, `dev.provenance.jsonl` | one record per conversation: source + `source_conversation_id`, `schema_style`, `migrated`, `migrations` (`migrated_from`, `migrated_to`, `migration_rule`), recomputed gold calls / preload / variant, plus every V3.1 provenance field |
| `conversations.jsonl` | one full conversation per line with `tools_per_round` |
| `migration_report.json` | dropped conversations with reasons |
| `sources.py` | rebuilds the exact V3.1 source rows into `source_cache/` (gitignored) and proves them byte-identical to V3.1's pinned train/dev hashes |
| `capture_assets.py` | captures `assets/` from read-only Wisp checkouts (Wisp venv; HOME/WISP_HOME redirected to a temp dir) |
| `generate.py` | deterministic migration + rendering (seed 20261014, stdlib only) |
| `ext_v32_mac.py` | the authored `v32_mac_controls` category |
| `validate.py`, `validation.json` | checks below; FAILS if the short-description asset is a placeholder |
| `preflight.py`, `preflight.json` | real Ling tokenizer render + intended label mask (tokenizer files only, no weights) |
| `check_labels.py`, `axolotl-a6000.yaml`, `merge_lora.py`, `package.py` | training-side tools, unchanged from V3.1 except names/counts |
| `assets/` | `tools.json`, `system_parts.json`, `short_descriptions.json`, `capture_verification.json` (reference renderings produced by Wisp's own functions), `heldout_shingles.json` (V3.1's, unchanged) |

Asset provenance: Wisp `origin/main` 1bd68e7eb68f73be5b06dbc7bc4c607c12339a7a; mac-controls skill
55a2b5b142ca816013213e07be36498ae78e6766 (`origin/claude/mac-controls-skill`, PR #226 reviewed head); short descriptions
178ebb3ed19de91efbb8591335bf1aad687a6168 (`origin/claude/terse-tool-schemas`, PR #227, real export, not a placeholder).

## Reproduce and validate

```
cd training/ling-dataset-v3.2-20261014
python3 sources.py                 # V3.1 sources -> source_cache/, verified against V3.1 preflight.json hashes
python3 generate.py                # rows, provenance, conversations, stats.json, DATA_CARD.md, migration_report.json
python3 validate.py --check-repro  # writes validation.json, exits 1 on any problem
/Users/adijain/Desktop/MOE_Project/.venv/bin/python preflight.py --local-files-only \
    --tokenizer-dir /Users/adijain/Downloads/Ling-3.0-tiny-Wisp-V2-merged
python3 package.py                 # refuses unless validation + preflight pass and short descriptions are real
```

**Re-render after the registry, the skill or the short descriptions change** (one command after the read-only archives):

```
git archive <main-sha> service | tar -x -C $S/main
git archive <skill-sha> service/skills/bundled/mac-controls | tar -x -C $S/skill
git archive <terse-sha> service/tools/short_descriptions.py service/tools/registry.py scripts/export_short_descriptions.py | tar -x -C $S/terse
python3 $S/terse/scripts/export_short_descriptions.py $S/short_export.json
.venv/bin/python training/ling-dataset-v3.2-20261014/capture_assets.py --main $S/main --main-sha <main-sha> \
    --skill-root $S/skill --skill-sha <skill-sha> --short-export $S/short_export.json --short-sha <terse-sha> \
    --terse-registry $S/terse/service/tools/registry.py \
  && (cd training/ling-dataset-v3.2-20261014 && python3 generate.py && python3 validate.py --check-repro)
```

`validate.py` checks: short descriptions real and covering all 92 built-ins; generate.py's stdlib mirrors of
`build_system`, `skills_context_block` and the terse renderer reproduce Wisp's own renderings
(`capture_verification.json`); no unknown or retired tool name in any field (offered tools, calls, discovery args, receipts,
catalog, system text, results, finals); per-step offer set and order, receipts, rejections, load budget; schema validity of
every call (types, enums, required, unknown keys, min/max) and stub guesses; full rows byte-equal to tools.json, terse rows
with the short line and no parameter descriptions; one style per conversation and a 40-60% terse share; per-category counts
equal V3.1's minus the recorded drops; unmigrated conversations identical to their source; held-out eval overlap < 0.4;
synthetic PII; V3.1's forbidden-call rules (no-history vague/no-referent requests call no tool, chat/code categories call no
tool, sends/destructive calls need a user cue, send recipients come from the user or an earlier result, no move to the Trash
without an explicit trash/delete request, guarded repeats never targets); V2 replay rows byte-identical and last.

## Train on the A6000

Same recipe as V3.1 (LoRA r16/alpha32 on attention, 1 epoch, LR 1e-4, `sequence_len` 16384, thinking off). Real token counts
(`preflight.json`): train 1,974 rows, max 11,599 / p95 9,517 / median 6,249 (13.0M tokens) + 600 V2 replay rows; dev 242 + 60.
Rows are ~25% shorter than V3.1's because the catalog has 96 tools instead of ~160 and half the rows are terse.
161 optimizer steps at effective batch 16.

### Continued fine-tune base (pick one)

- **V3.1 was trained and passed its gate:** continue from `Ling-3.0-tiny-Wisp-V3.1-merged` (V3 merged + the V3.1 adapter via
  `merge_lora.py --base-dir ...V3-merged --adapter-dir ling-v31-lora-out`). V3.2 then mostly teaches the registry and the terse
  rendering on top of behaviour V3.1 already learned.
- **V3.1 was not trained (or failed):** continue from `Ling-3.0-tiny-Wisp-V3-merged`. V3.2 contains every V3.1 fix
  conversation, so it replaces the V3.1 run rather than stacking on it.

Then: upload the ZIP, verify its sha256 against `upload-zip.json`, extract into a NEW directory, run `preflight.py`
(must print PASS), edit `base_model` in `axolotl-a6000.yaml`, `axolotl preprocess`, gate with `check_labels.py`, timed smoke run
(`max_steps: 20`), full run (adapter in `ling-v32-lora-out`), merge with `merge_lora.py`.

## Eval gate (full model-led eval, S and SP, thinking off, T=0)

Run the eval **twice**, with `WISP_TERSE_TOOL_SCHEMAS` unset and with `WISP_TERSE_TOOL_SCHEMAS=1`, on a Wisp build at or after
the registry above with the mac-controls skill bundled (the training rows assume its tools and skills-catalog line exist).

- Forbidden calls: **0**
- named_item >= 7/10
- Guard-ignored runaways <= 5
- No domain regressions vs the V3 `_stub2` baseline (no domain drops more than one case)
- **Terse-vs-full parity:** the terse run meets every line above, and its action-chain completion is within 2 points of the
  full run with no domain more than one case worse. The terse flag flips on only if this holds.

## Known gaps and risks

- `v32_mac_controls` is template-authored (12 train families, small phrasing banks); it is the only coverage of the mac_*
  tools. The skills block assumes skill triggers as of 55a2b5b1; if PR #226 changes them again, re-capture and re-generate.
- No row fetches the crypto/news endpoints with `web_fetch`; terse mode drops those URL templates from the description, so
  that path is untrained in terse mode. validate.py enforces the exact endpoint forms if such rows are added.
- Merged-tool results keep the single-purpose shape (e.g. `get_weather(include=[air_quality])` returns only the AQI line).
- The skills block assumes the bundled skill set {idea-refine, interview-me, mac-controls} enabled and no user skills. The
  mac-controls body is appended (`[loaded below]`) in the rows whose user text matches its triggers.
- Memory/facts blocks are empty (synthetic user), as in V3.1.
