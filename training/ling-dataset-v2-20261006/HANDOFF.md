# Ling dataset V2 handoff

Outcome: refined synthetic routing and fixture-overview demonstrations for a future Wisp 1.3.0 training experiment. No production settings, router, performance harness or release code changed; no model was loaded, trained, merged, exported or activated.

Authored from base `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`, frozen router contract `c313a459f6ab259e55981ffcb1bbe3df11fe9307`. Task branch `codex/ling-dataset-v2` was reconciled onto `ebe82735d699a88c05c94fa41fd0fee0e35fdd68` before the candidate commit. All changed files are under `training/ling-dataset-v2-20261006/`.

Three GPT-6.1 Sol/high authors owned distinct argument, context and grounded shards. A fourth GPT-6.1 Sol/high reviewer worked read-only from frozen snapshots. Parent retained integration/QA/docs/packaging ownership. Initial review evidence remains preserved: 98 summaries needed explicit natural source labels (37 strongest ambiguities), while 379 other strict attribution failures were validator false positives. The same recorded owners made the bounded repairs. Exact repair comparison preserves every prompt/fixture/provenance record and all other targets. All helpers are quiescent after handoff.

Data: 4,000 train (3,400 routing / 600 grounded), 400 development (340 routing / 60 grounded), balanced exact 2,000-row pilot subset. Train/dev scenario families were assigned before slot expansion. This is deterministic expansion of authored specifications and surfaces, not 4,000 independent human requests. No old train/dev/final, original evaluation prompts or private user logs were read; no final payload is included.

Validation: standard-library CPU schema/capability/semantic/literal/date/exclusion/masking-annotation/dedup/fixture checks pass all 4,400 rows. All 880 follow-up state replays pass. Nineteen corruption controls reject and two attribution positive controls accept from passing baselines. Six assembled outputs reproduce byte-for-byte in fresh temporary state. Packaging binds validation to data/validator/contract hashes and rejects stale payload/code, checks an explicit 15-member allowlist and CRC/member hashes. A failed wrong-association control during repair exposed a closing-quote clause split; it was fixed before the final frozen snapshot. A sandbox pycache-write failure in an earlier compilation attempt was environmental; cache-free source compilation passed.

Pending: pinned-tokenizer length/prefix checks and actual prepared Axolotl final-response label audit must pass in the user's Brev environment before training. JSON annotations do not establish trainer masking. No V2 model-quality uplift, production routing behavior, Mac export compatibility or globally optimal recipe is claimed. Development data is never a training input; the old final remains sealed. Use either pilot-train or full train, never concatenate them.

Integration: no shared-file overlap with Wisp 1.2.0, PR160 routing work, the comparison harness or attestation performance work. The user's no-merge/no-activation hold remains intact. Draft PR and normal required CI do not authorize production delivery. Review/CI status and exact candidate commit are recorded separately at delivery.

Final independent advisory verdict: `PASS_WITH_NOTES`, bound to snapshot manifest `4fe27d9ec60788a234f857437af244c3c6ed2c935a5080049a126a7e39c2e391`. No further dataset repair remains. Final review and hash-bound receipt are saved under `evidence/final-review/`.

The initial staged whitespace check found two trailing spaces in the argument generator. Its retained author removed exactly those spaces; AST and compiled bytecode are unchanged, and every dataset/provenance/QA/ZIP hash is unchanged. Original frozen evidence is preserved; fresh bounded independent binding verification follows.

Final mechanical advisory verification passed with notes, bound to snapshot3 manifest `402e67d52f0547551d4f1b2f246ca80c296cc98dc592f71c1f6c58efa8b76383`: exactly two spaces removed, AST/bytecode identical, all other prior inputs and ZIP unchanged. The staged whitespace check now passes. Evidence is under `evidence/mechanical-review/`.
