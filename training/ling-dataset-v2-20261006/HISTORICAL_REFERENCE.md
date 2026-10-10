# Historical reference: Ling dataset V2

This package preserves the October 6, 2026 synthetic dataset and its original evidence as an inert, reproducible reference for Wisp's frozen read-intent routing experiment. It does not update Wisp's router, train a model, activate an adapter, or qualify a model for use in the app.

Read this note before the original README and handoff. Those documents, reports and instructions describe the experiment at their recorded snapshots; their pending checks, advisory verdicts and delivery holds are historical statements. They do not establish today's ownership, merge authorization, CI status or training readiness. Current delivery decisions require separately recorded evidence for the exact candidate commit.

## Frozen provenance

- Original dataset candidate: `da67bb008bddd0f5213f8b37da84a1f8e599676a` on `codex/ling-dataset-v2`.
- Authored repository base: `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`; original reconciled base: `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`.
- Frozen router contract: `c313a459f6ab259e55981ffcb1bbe3df11fe9307`, with the bundled `router-system.txt` and `intent.schema.v1.json`.
- Recorded composition: 4,000 training rows, 400 development rows and a balanced 2,000-row pilot that is an exact subset of training. The pilot and full training set are alternatives, not inputs to concatenate. Development data is not training data. The separate original final test is not included.
- Three authored shards cover arguments, context/follow-ups and grounded fixture summaries. Deterministic scenario expansion means these are not 4,000 independent human requests.

This reference qualification preserves all 73 previously tracked files byte-for-byte, including the upload ZIP, data and provenance sidecars, generators, validators, manifests and prior review records. Only this note is added. Preservation is checked by opaque file hashes and Git bytes; dataset payloads and held-out data are not inspected or regenerated during this qualification. No dataset validator, tokenizer, training or model inference is executed in this lane.

## Limits when comparing with later routing

The routing targets belong to the frozen structured read-intent contract above. PR #174 proposes a different model-led capability-discovery and tool-selection path; that proposal is pending and is not shipped functionality established by this package.

A target that passes the old static intent schema is not automatically a valid proposal for the pending tool-selection path. Reuse would require an explicitly reviewed mapping of available capabilities, exact tool schemas and arguments, conversation context and clock, source exclusions, clarification, and execution/approval boundaries. Existing targets must not be silently treated as current tool calls or used to bypass host validation.

The prior static QA and advisory reviews remain evidence about their recorded inputs and checks only. This note claims no new routing accuracy, assistant-quality improvement, latency result, trainer-label correctness, export compatibility or activation readiness. Any future training or evaluation requires its own approved contract, data split and independent measurements. The preserved training instructions are historical reference material, not authorization to start a run.
