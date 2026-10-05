# Wisp 1.3 production-path evaluation

This lane exercises `service.main.agent` and its real shortcuts, typed tasks,
workflows, read dispatcher, router, agent loop, policies, verification, SSE,
and workflow presentation. Every registered tool callable is replaced before
execution with a fixture callable. Native contact resolution and reply
preparation have separate synthetic seams. No real tool body executes.
The fake client emits authored responses; **scripted results are not measured
model interpretation, tool-selection, narration, latency, or energy accuracy**.
An oracle intent response tests schema/compiler/integration correctness only.

## Frozen corpus

`seal.json` was created before candidate comparisons. There are 80 development
variants in 10 families and 320 heldout variants in 40 separate families. Families
are the unit of independence; these are not 320 independent linguistic templates.
Eight parameter variants per family provide time, identity, count, and boundary
coverage without claiming eight independent families. Original product taxonomy
supplied the cases; no historical raw prompt corpus or failure-log corpus was
imported. Existing routing corpora remain separate regression assets.

The test bytes, expected annotations, and authoring source remain frozen. Do not
regenerate or tune on test failures. The original annotations are conservative
mechanical call contracts, not an expert-adjudicated model benchmark. Some
requested scopes exceed existing tool capabilities. `runtime_constraints`
explicitly rejects known ignored date/count filters and elapsed-calendar reads,
even when gold arguments match. Unsupported/clarification outcomes may therefore
fail the strict original call annotation; adjudicate them independently after
candidate freeze, retain the original score and denominator, and record any
supplemental adjudication rather than rewriting sealed examples.

Current coverage includes exact dates, days, weeks, months, rolling horizons,
DST/year/leap transitions, typos, context, multiple sources, unread filters,
drafts/sends, negation, quoted instructions, public/private boundaries, partial
failure, unavailable tools, identity literals, ordinary conversation, and
ambiguity. Groundtruth label/value checks cover count, direction and coverage;
reference tokens check literal preservation. They cannot detect every possible
hallucination or prove narrative quality. Fixture data tests orchestration rather
than native source synchronization, tool-internal querying, or device permissions.
Pure presentation helpers also need their owner’s focused fixture tests.

## Run development replay

```sh
/Users/adijain/Desktop/MOE_Project/.venv/bin/python scripts/eval_router_update.py \
  --split dev --output /private/tmp/wisp-router-dev
```

The script creates temporary `WISP_HOME`, redirects `Path.home()` before service
imports, and freezes the local clock with `America/Los_Angeles` zone rules. It
imports lazy candidate clock modules before freezing their `datetime` references.
Service state never uses real `~/.moe` or oMLX settings. The dedicated process
blocks network, subprocess/exec/system calls, personal-home reads, and writes or
SQLite connections outside temporary state and the explicit output directory.
It does not use ordinary `test_mode`, which returns plans rather than successful
fixture execution. No HTTP endpoint or FastAPI lifespan is started.

`raw.jsonl` retains exact requests, schemas with runtime defaults, fake responses,
SSE events, actual fixture calls, execution module, context, and answer. The
manifest hashes the raw output, corpus, harness, every production Python file,
and records the checkout HEAD, Python version, model/checkpoint nulls, metric
numerators/denominators, and each family’s results. Zero denominators are N/A.
Source/menu/first-call/end-to-end metrics have different denominators; compare
them separately. Menu is N/A when a deterministic path needs no model menu.
First-call N/A means no call was requested. Exact argument comparison preserves
types, every non-default constraint, and literal spelling. Date equivalence is
accepted only when the actual runtime resolver produces identical epoch bounds.

The checked-in `baseline-scripted/` contains the development control raw output
compressed losslessly, its original manifest, and hashes of both representations.
Its production base is `4994caa15533c0cf84c07208c9097e4197f2815b` and the manifest
records the original harness file hash from worker commit `0a71781`. This control was captured before the worker
commit, so its HEAD is the clean production base rather than claiming commit-bound
model evidence. Model revision/checkpoint are explicitly absent.

## Frozen scorer amendment (2026-10-05 19:20:05 UTC ACK)

The original `exact_arguments`, `first_call`, and `end_to_end` calculations remain
unchanged. Added `request_argument` permits exactly one implementation safeguard:
`view_messages(strict_match=True)` when the raw gold call omitted that field,
the compared tool registration advertises a Boolean guard accepting true, and
the query and every other argument match under the existing runtime-default/date
normalization. JSON value types remain distinct, including Boolean/integer and
integer/float. This rule applies only to `view_messages`; it does not normalize
`view_emails` or arbitrary tool flags. Explicit gold guard values remain
binding. Query-free reads, wrong literals/types/scopes/counts, and extra arguments
cannot use the amendment.

This is request-contract scoring, not runtime equivalence: a guarded query miss
returns a bounded cache no-match, while the legacy path may broaden the result
set and increase its count. Separate `query_scope_guard` is required for gold
query-bearing `view_messages` calls whose guard is omitted or true. It requires
the registered capability, the same literal query, and actual Boolean true at
execution. Missing/false/string/numeric flags or unavailable registrations fail.
An explicit gold false selects the legacy contract; guard is N/A, never credited
as a guarded success. Other tools and query-free cases are N/A. Each metric keeps
its own numerator/denominator. The aggregate guard denominator counts scenarios
with at least one applicable call; a passing scenario must guard every applicable
call.

`guarded_end_to_end` uses the full typed `request_argument` sequence (which also
proves the first-call order), this applicable guard, and every original gate
except the old exact-argument and first-call comparisons. The old end-to-end
result remains separately available and is never relabeled. The amendment was
written from generic runtime behavior and synthetic cases before any candidate
or heldout comparison. Sealed data, original corpus hashes, and saved baseline
artifacts are immutable; the baseline manifest intentionally retains its original
harness hash rather than being overwritten by this repair.

Evaluation path metadata accepts only `intent_disposition` values `compiled`,
`clarify`, and `declined`, yielding `intent_compiled`, `intent_clarify`, and
`intent_declined`. Raw SSE `route_source` stays unchanged. These paths do not imply
that a model was called; clarification can precede generation, and oracle replay
remains scripted. Unknown or non-string dispositions are ignored.

The scripted client now captures the synthetic configuration's exact router
`Target` and supplies coherent base URL, provider, API prefix, endpoint name,
model/status, and inert credential-origin/backend metadata. Its backend is an
ordinary `object()`, not a credential loader or HTTP transport. Candidate replay
supplies that same snapshot to the planner's router-role resolver. No credentials
are resolved and no networking, model loads, or model calls are enabled by this
fixture identity. The separately bounded adapter repair below borrows the supplied
client's existing identity only after matching its frozen grant and isolated
router configuration. Resource authorization for measured inference remains a
separate prerequisite and has not been granted or exercised here.

## Candidate and heldout

After the core owner’s committed API is integrated, `--candidate` opts into
`models_config()['intent_router']`; an absent API fails rather than silently
pretending to test a candidate. Development replay may then compare integration
behavior. Tests for the candidate planner’s actual local-clock prompt are skipped
on the original control and must run after core integration.

Do not evaluate heldout until the combined candidate is frozen and explicitly
authorized. The CLI requires both `--allow-heldout` and `--candidate-sha` matching
its checkout HEAD. This flag is an explicit operator gate, not a security boundary:
keep heldout prompts/results away from builder and tuning output until freeze.

## Future measured inference (disabled)

`ResidentInferenceAdapter` is an injected interface, separate from fixture tool
execution. The CLI has no option to enable it. `InferenceGrant` defaults to
disabled and requires an exact loopback host/port, frozen Ling model, revision,
checkpoint SHA256, and resource/approval receipt. Only that endpoint may be allowed
by `install_guard(..., inference_grant=...)`; native execution remains blocked.
Before status or generation, the adapter requires the installed isolation guard,
checks that service paths still point to temporary evaluation state, resolves
`role_target('router')` there, and uses the strict core `_client_matches_target`
validator on the supplied client. It fails closed if that validator is unavailable.
The frozen grant must match the configured model and exact loopback origin; an
explicit configured revision must also match. Only six read-only properties are
borrowed: `target`, `base_url`, `endpoint_name`, `provider`, `api_prefix`, and
`_credential_transport`. These return the supplied references; the adapter does
not create transport, resolve keys, copy credentials, or expose arbitrary client
methods. Identity is checked again after awaited residency status, before chat
or streaming begins.

Status must report the exact selected model already resident. The adapter never
invokes start, ensure/load/swap/unload on its underlying client. A grant's revision,
checkpoint hash, and approval receipt are caller provenance, not independent proof
of the loaded weights. Such proof must be established separately for any measured
lane. No model was loaded or called in this implementation. The parent must reserve
resources and supply the client/grant before using this interface. Results then use
a different mode and retain actual response events and model provenance; never
combine them with oracle replay as measured model successes.

Synthetic adapter tests use fake status/chat/stream callbacks with all sockets
still denied. They verify exact borrowed references, mismatches rejected before
I/O, unavailable-model rejection without loading, and identity changes during
status rejected before generation. The strict-core integration check is skipped
on the original baseline and must pass on the combined candidate.

## Focused verification

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest \
  -p pytest_asyncio.plugin tests/test_router_update_eval.py -q
```

Tests cover strict scoring, runtime ignored constraints, DST boundaries, corpus
integrity without parsing heldout prompts, disabled inference, network/process/
write isolation, actual endpoint digest success, workflow presentation and denied
synthetic delivery, and rejection of historically lossy list-of-lists context.
