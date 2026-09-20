# Laya Core ML models for Wisp routing — benchmark report

## Recommendation

**Keep Ling and the current Wisp routing architecture. Do not replace Ling with
any of the three Laya exports, do not add any of them to candidate retrieval,
and leave all three unused in production.**

This conclusion is not based on latency alone. On 67 in-capacity synthetic
cases, the two multilingual exports recognized only 2 of 50 tool-needing
requests at the predeclared `noul >= 0.5` rule; typed-decisions recognized 0 of
50. Their tool-needed scores did not even provide useful ranking for a different
threshold: AUROC was 0.471, 0.467, and 0.436 respectively. All three produced a
constant `act_probability=1.0`, so the action head supplied no usable abstention
signal on this corpus.

The best raw domain accuracy was 46.3%. That is not enough for candidate
retrieval: the multilingual models made the required tool group reachable in
only 11/50 tool cases, while typed-decisions reached 25/50. Confidence gating
did not rescue this result. At a domain-confidence threshold of 0.9,
multilingual domain accuracy reached only 56.7% at 44.8% coverage;
typed-decisions reached 52.0% at 74.6% coverage.

Finally, these are typed classifiers, not generative tool callers. They cannot
produce Wisp's `RouteDecision` contract, split a compound request into component
domains, order dependent calls, resolve a contextual follow-up into grounded
arguments, or generate tool arguments. Even perfect raw labels would therefore
not replace Ling's role in the agent loop.

## Systems and pinned artifacts

The repository base was `411516dfb47a1ce637362f992ad1f5f8800c97a4`.
Hub metadata, shapes, hashes, and byte totals were recorded in
`preflight.json` before weight download.

| Model | Hub revision | Export | Repository bytes |
|---|---|---|---:|
| `aac6fef/laya-multilingual-coreml-ane` | `39d6a9b3d0f67f06da74fbade6121ea134cbdb21` | CPU+ANE, fixed B1/L96/K32 | 679,922,219 |
| `aac6fef/laya-multilingual-coreml` | `8139e9089273319512c730218903784074133187` | CPU+GPU, B1, enumerated L16–1024, K32 | 679,893,226 |
| `aac6fef/laya-typed-decisions-coreml` | `28d24fa8d67a3264556b23391ec6c3fd98573056` | CPU+GPU, B1, enumerated L16–1024, K32 | 848,151,894 |

The predeclared upper bound was 2,207,967,339 bytes (2.056 GiB), below the
3.5 GB download ceiling. The final cache occupies more disk space because
`laya-coreml` materializes Core ML packages whose Hub files are symlinks; those
local copies are not additional downloads and remain ignored by Git.

The runtime was `laya-coreml==0.1.0` on Python 3.11.15 and
`coremltools==9.0`. Native measurements ran on an Apple M5 Pro (`Mac17,8`),
18 CPU cores, 24 GiB unified memory, and macOS 27.0. The ANE model reported
`cpu_ne`; the general exports reported `cpu_gpu`.

## Evaluation protocol

The corpus contains 70 synthetic cases: 67 ordinary cases and three explicit
capacity probes. It includes English, 12 multilingual prompts, paraphrases,
11 no-tool prompts, six negations, 14 tagged safety cases, six unknown or
unsupported requests, six explicit compound requests, and five contextual or
follow-up cases. No personal data, live account content, real effect, installed
app replacement, or deployment was used. No Wisp tool body was called.

Each Laya model received the same five independent questions:

| ID | Type | Labels/rule |
|---|---|---|
| `tool_needed` | `noul` | `>= 0.5` means a Wisp tool is needed |
| `domain` | `choice` | none, mail, messages, calendar, reminders, notes, contacts, files, device, web, finance, shell, media, memory, compute, multi, unknown |
| `operation` | `choice` | answer, read, write, delete, execute, compound, clarify, unknown |
| `risk` | `choice` | none, sensitive read, reversible write, outbound write, destructive, shell, unknown |
| `disposition` | `choice` | route, clarify, unsupported, no tool, unknown |

The complete machine-readable schema is in `questions.json`. Because each
export has batch size one, one routing classification means five serial Core ML
calls. Warm latency uses 25 repetitions of 12 representative prompts (300 full
five-question samples per model), after one warm-up of every representative
case. Model loading and runs were serialized.

The unchanged Wisp comparison calls `service.router.router.route()` directly
against the current 155 routable-tool registry. Its `needs_tools` field means
tools are available to Ling; it does not always mean a tool is forced. For that
reason the report retains two Wisp views:

- availability (`needs_tools`): 96.0% recall and 77.4% precision;
- confident tool intent (`expect_tool_first` or a concrete execution contract):
  70.0% recall and 94.6% precision.

This is a router-only baseline, not an end-to-end remeasurement of Ling's
argument generation. The full-route grade is strict: every required tool group
must be reachable, every fixture-forbidden tool must be absent, and no-tool
requests must not receive tools. It exposes real weaknesses in the current
router but should not be mistaken for overall Wisp task-success accuracy.

## Accuracy and safety results

| System | Tool recall | Tool precision | Tool AUROC | Domain | Operation | Risk | Disposition | Candidate recall | Safety errors | Full `RouteDecision` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Wisp source router (`needs_tools`) | **96.0%** | 77.4% | n/a | 32.8% | **37.3%** | **47.8%** | **70.1%** | **70.0%** | **14/31** | **32/67 (47.8%)** |
| Multilingual ANE | 4.0% | 100% | 0.471 | **46.3%** | 19.4% | 31.3% | 7.5% | 22.0% | 31/31 | 0/67 |
| Multilingual general | 4.0% | 100% | 0.467 | **46.3%** | 19.4% | 31.3% | 9.0% | 22.0% | 31/31 | 0/67 |
| Typed-decisions | 0.0% | n/a | 0.436 | 43.3% | 29.9% | 29.9% | 1.5% | 50.0% | 31/31 | 0/67 |

The apparent 100% tool precision for the multilingual exports is not a
positive result: they predicted tools for only two cases. They classified all
11 no-tool cases correctly on the boolean dimension largely because they said
"no tool" for 65/67 cases overall.

Safety errors count a tool/disposition miss, wrong risk class, or failure to
make the required candidate group reachable; for Wisp they use the stricter
full-route rule. The multilingual exports underclassified 30/31 safety cases;
typed-decisions underclassified all 31. A classifier with this behavior cannot
be placed before Wisp's safety gates.

The two multilingual ports were effectively the same classifier: all 67 cases
had the same tool-needed, domain, and operation labels; 66/67 had the same risk
label and 65/67 the same disposition. The ANE speedup therefore did not trade
away meaningful task accuracy relative to the general port—the underlying
checkpoint itself was the problem for this schema.

### Cohorts that matter

| System | English domain | Multilingual domain | Compound domain | Context domain | No-tool boolean |
|---|---:|---:|---:|---:|---:|
| Wisp source router | 37.5% | 9.1% | **57.1%** | 40.0% | 27.3% |
| Multilingual ANE/general | **51.8%** | 18.2% | 0.0% | 40.0% | **100%** |
| Typed-decisions | 44.6% | **36.4%** | 0.0% | **60.0%** | **100%** |

These isolated domain wins do not justify augmentation. Compound requests are
especially important: a single `multi` label cannot tell retrieval which two or
three menus to union, so every Laya model received zero candidate credit on
that shape. The current Wisp baseline is also weak on multilingual, no-tool, and
unknown prompts; those are follow-up work for its own router and Ling loop, not
a reason to insert a worse classifier.

## Latency and memory

| System | Warm p50 | Warm p95 | Fresh-process load | RSS load delta | Observed RSS peak delta |
|---|---:|---:|---:|---:|---:|
| Wisp source router | **0.65 ms** | **16.37 ms** | 0.23 s | 33.1 MiB | not sampled separately |
| Multilingual ANE | 18.88 ms | 19.09 ms | 15.74 s | 666.8 MiB | 701.7 MiB |
| Multilingual general | 130.41 ms | 701.87 ms | **3.26 s** | 1,131.3 MiB | 1,131.3 MiB |
| Typed-decisions | 34.04 ms | 1,275.45 ms | 3.43 s | 987.6 MiB | 1,048.2 MiB |

The general models' wide tails are input-shape effects, not unexplained random
spikes. For multilingual general, short English cases were about 17–18 ms,
L96/L128-like cases about 250–480 ms, and Spanish/Hindi representatives about
703/477 ms. Typed-decisions reached roughly 971 ms on Spanish and 1,293 ms on
Hindi. The raw 300-sample series and case IDs are retained in each result file.

Memory is current-process RSS. Apple Core ML accelerator allocations in unified
memory are not guaranteed to be fully attributed, so the numbers are useful for
relative comparison but not a complete system-wide footprint. Fresh-process
load does not purge macOS's global Core ML compilation cache.

## Capacity behavior

The ANE package behaved as documented: the two 96-token probes prepared to 134
and 138 tokens for the `tool_needed` question and raised an explicit capacity
error. The 1,024-token probe also raised. It cannot classify several ordinary
context-rich Wisp requests once state plus question/options crosses 96 total
tokens.

Both general exports accepted the 134- and 138-token probes. For a 3,908-token
synthetic state, the runtime prepared every question at exactly 1,024 tokens and
returned a prediction. That is recorded as `truncate`, not `accept`: content
beyond the checkpoint limit was silently excluded by prompt construction.

## Per-model decision

### `laya-multilingual-coreml-ane`

**Stay unused.** It is the only export with consistently low warm latency, but
4% tool recall, 22% required-group recall, 31/31 safety errors, and the fixed
96-token context make it unsafe even as a preliminary gate. If a future model
is trained directly on Wisp's schema, this export shape is worth revisiting as a
latency target—not this checkpoint.

### `laya-multilingual-coreml`

**Stay unused.** It removes the ANE context ceiling but preserves essentially
the same wrong labels, uses about 1.1 GiB process RSS, and has 130/702 ms
p50/p95 for the five-question decision. It offers no quality reason to pay that
cost.

### `laya-typed-decisions-coreml`

**Stay unused.** Its checkpoint is specialized for different typed-decision
workflows. On Wisp it produced zero tool-needed positives, 1.5% disposition
accuracy, and 31/31 safety errors. Its 50% candidate recall is still below the
unchanged router's 70% and is paired with no usable tool gate or abstention
signal.

### Ling and current retrieval

**Do not replace Ling. Do not augment candidate retrieval with these outputs.**
Laya can at most classify a fixed set of questions; it cannot narrate, generate
arguments, emit structured tool calls, or complete the agent loop. Wisp's
existing router still needs improvement—strict full-route success was only
32/67 on this deliberately broad synthetic set—but the measured Laya variants
would reduce recall and safety rather than fix those gaps.

## Failures and limitations

- The first run tried `~/.cache/laya-coreml` and was blocked by workspace
  permissions before inference. `LAYA_COREML_CACHE` was then set to the isolated
  benchmark cache.
- A sandboxed native run could not create Apple's Core ML compiler working
  directory. The same pinned command was rerun with native execution approval;
  all three models then executed successfully.
- The ANE runtime emitted NumPy divide/overflow/invalid warnings inside its host
  action-head matrix multiply. Public outputs remained finite, but every
  recorded action probability was exactly 1.0; it was not used for scoring.
- The corpus is synthetic and purposely broader than current English regex
  coverage. It measures routing semantics, not end-to-end task success.
- Labels and the 0.5 boolean rule were frozen before native inference. No
  threshold or question was fit to these results.
- The Wisp comparison did not run live Ling generation or real tools. Existing
  argument-generation reliability should be evaluated separately with
  synthetic tool stubs if a future change reaches that layer.

Raw files contain every output probability, per-question token count, case
error, capacity outcome, 300-sample latency series, process memory measurement,
model manifest/checksum verification, and machine fact. `RUN_LOG.md` records
the exact commands and diagnostic failures.
