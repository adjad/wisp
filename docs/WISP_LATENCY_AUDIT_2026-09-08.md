Wisp has useful latency reductions available without shortening the answers users receive. The best targets are duplicate outgoing-message generation, unnecessary escalation of simple acknowledgments, unnecessary model startup, and prompt-cache layout. Broadly deleting system instructions or tool descriptions is a weaker first move.

This audit covers the current working tree, the installed Ling tokenizer/chat template, September debug exports, and recent replay reports. Production code and model settings were not changed. Offline probes used isolated Wisp state and a fake inference client. No live sends or model benchmarks were performed. Relevant files in the packaged app differ from the working tree, so these findings describe the inspected source, not a verified running build.

Shorter input reduces prompt processing and memory pressure; fewer generated tokens or fewer generations reduce decode work directly. A shorter context can also improve decode throughput, but that effect needs measurement on the actual model/runtime. Historical latency ratios in the backlog describe older configurations and should not be treated as current Ling measurements.

| Priority | Opportunity | Benefit | Quality-preserving scope |
|---|---|---|---|
| 1 | Generate outgoing message content once | Direct decode reduction | Keep complete tool arguments and the existing full approval preview |
| 2 | Preserve the fast path for unmistakable greetings/thanks in pinned sessions | Avoid unnecessary thinking and broad tool menus | Preserve task continuations, approvals, corrections, and cancellation handling |
| 3 | Defer inference startup until inference is needed | Cold-start and end-to-end reduction | Keep the same deterministic tool result and execution checks |
| 4 | Put the changing clock after the serialized tool schemas | More reusable prompt prefix | Preserve exact time, instruction order, and context-window protection |
| 5 | Consolidate repeated time-range descriptions | Smaller input | Keep all range semantics; accept only after tool-argument quality comparisons |
| 6 | Stop repeated attempts that cannot make progress | Lower worst-case decode work | Distinguish terminal failures from recoverable failures; do not just lower retry limits |
| 7 | Evaluate thinking suppression on narrow Ling selection steps | Potential direct decode reduction | Experimental, per-model and per-task; not a blanket switch |

**1. Stop generating the full outbound draft twice.**

[The system prompt](/Users/adijain/Desktop/MOE_Project/service/agent/loop.py:263) requires the full recipient, subject, and body in assistant prose before the tool call. Its stated reason is that the confirmation card shows only a one-line summary. That premise is obsolete for `send_email`, `send_message`, and `reply_to_email`.

[confirm_preview](/Users/adijain/Desktop/MOE_Project/service/tools/action_tools.py:51) constructs the complete preview from tool arguments; [the agent passes it to the approver](/Users/adijain/Desktop/MOE_Project/service/agent/loop.py:2304), and [the Swift view renders it](/Users/adijain/Desktop/MOE_Project/app/Sources/WispApp/OverlayView.swift:439). Moreover, the current agent buffers all generation with `stream=False`, so prose preceding a tool call is normally generated and retained in model context without being displayed to the user.

Remove the duplicate-prose mandate from SYSTEM, the matching send-tool descriptions, and their retry instructions. Have the model generate the complete payload once in the tool arguments. Keep the approval preview, recipient resolution, complete-body requirements, send-versus-draft distinction, and factual outcome reporting. If a transcript copy is wanted, render/store the exact arguments without regenerating them.

This saves approximately one draft's worth of output tokens whenever the model currently follows that mandate. For a draft of B tokens and measured decode rate r tokens/second, the avoided decode work is approximately B/r seconds. This is conditional on actual duplicate generation; it is not a measured average saving across sends.

Initially scope this to the three tools with full previews. `schedule_send` does not yet have matching support in `confirm_preview`; add its complete recipient/body/time preview before removing any equivalent prose requirement. Draft-only tools must continue showing the actual draft. Verify approved, denied, failed, and long-body cases, including full-access mode; outbound-send categories still require confirmation under that mode.

**2. Prevent session pinning from turning “thanks” into a full agent request.**

[Sticky routing](/Users/adijain/Desktop/MOE_Project/service/main.py:697) replaces a fast decision with the pinned agent/coding/reasoning role when no tool subset is present. A pinned agent also changes `needs_tools` to true, while the trivial request's `tool_subset` remains `None`. That offers the whole registry to the agent, subject to subsequent context fitting. A pinned coding/reasoning role instead bypasses [fast-only thinking suppression](/Users/adijain/Desktop/MOE_Project/service/main.py:851).

An offline probe executed the actual sticky block from the current source: `thanks` and `hello` both changed from `fast / needs_tools=False` to `agent / needs_tools=True / tool_subset=None` under an agent pin. All text roles already point to Ling, so no different model is needed to preserve the fast path.

Use a narrow, complete match for unmistakable social acknowledgments after contextual/task routing. Do not exempt every request currently labeled `trivial chit-chat`: the same probe showed that `okay shorter` receives that label, although it can be a real revision request. Test greetings after agent/coding turns alongside “yes, send it,” “okay shorter,” cancellation, pending reminders, and multi-turn corrections.

**3. Avoid starting/loading a model for routes that return deterministic results.**

[main.py starts/checks oMLX](/Users/adijain/Desktop/MOE_Project/service/main.py:660) and [ensures the model is loaded](/Users/adijain/Desktop/MOE_Project/service/main.py:724) before the agent executes its direct calls. Yet [the direct-result completion gate](/Users/adijain/Desktop/MOE_Project/service/agent/loop.py:1586) can return the answer without any generation.

For example, `summarize my messages` has a direct-dispatch route and [its current summarizer](/Users/adijain/Desktop/MOE_Project/service/tools/imessage_tools.py:588) calls deterministic `source_digest`. Old comments still describing a nested summary-model call are stale. Capability/coverage tools can also supply final results directly.

Move engine readiness to paths that actually need inference. The loop already has an `ensure_only` immediately before its model steps. Preserve engine startup for those steps, any genuinely model-backed tools, and optional semantic/reranker routing; the configured lexical provider does not require that model work. Preserve policy, approval, audit, completion checks, and fallback behavior. This is particularly useful after idle unloading and changes no successful deterministic answer text.

**4. Correct the timestamp's position in the rendered prompt.**

[Agent prompt assembly](/Users/adijain/Desktop/MOE_Project/service/agent/loop.py:1229) places the clock late in the first system message, but [Ling's actual chat template](/Users/adijain/Desktop/OMLX_Model_Files/Ling-3.0-tiny-oQ4e/chat_template.jinja:26) serializes all tool schemas after that message. A minute change therefore breaks the common prefix before every schema. The template supports subsequent system messages, so the exact clock can instead be placed after the stable system-plus-tool block.

Offline rendering of two otherwise identical requests one minute apart produced these exact common-prefix counts with the installed Ling tokenizer. The probes excluded private identity/memory/skill content.

| Offered tools | Current reusable prefix | Clock in later system message | Additional reusable tokens |
|---|---:|---:|---:|
| Email-read pair | 1,157 | 2,623 | 1,466 |
| Core 15 tools | 3,504 | 8,490 | 4,986 |

The additional message boundary costs four tokens. These are reusable-prefix measurements, not measured speedups. Savings depend on actual cache hits, the same tool set/order, and runtime cache behavior. oMLX documents its prefix-sharing cache mechanism in [its repository](https://github.com/jundot/omlx).

An essential implementation detail: [the context fitter](/Users/adijain/Desktop/MOE_Project/service/agent/loop.py:884) protects only the first system message by position. It must also protect the new required clock block, or context pressure could remove the time instruction. Keep exact minute precision and style precedence. [Plain chat](/Users/adijain/Desktop/MOE_Project/service/main.py:836) has a similar opportunity because its clock currently precedes memory and skills.

**5. Reduce repeated schema prose only where semantics remain available.**

The complete static SYSTEM is 5,392 Ling tokens, but Wisp already scopes it to the offered tools. Representative base-prompt sizes, before identity/memory/style/time, are 1,131 tokens for the email-read pair, 1,374 for `get_upcoming` alone, and 3,478 for the core 15-tool menu. Removing the full 5,392-token prompt is therefore not a saving available on every request.

The largest individual static schemas, measured after the real template's JSON serialization, are:

| Tool | Tokens |
|---|---:|
| `view_emails` | 683 |
| `summarize_emails` | 624 |
| `search_browser_history` | 557 |
| `get_stock_price` | 552 |
| `search_notes` | 547 |
| `schedule_send` | 544 |
| `view_messages` | 514 |

The best specific duplication candidate is [PERIOD_ARG](/Users/adijain/Desktop/MOE_Project/service/tools/timeranges.py:276): its description costs 261 tokens and is repeated in six schemas. Put its unchanged vocabulary and time-scoping rules in one shared visible block whenever those tools are offered, with a short field reference. Savings scale with how many of those tools are offered together, not with the whole registry: two offered copies can remove approximately one repeated description, less the reference/header overhead. Preserve the rules about unnamed periods, exact relative spans, and month names versus text queries. Those protect against documented wrong-range answers, so this requires an A/B comparison before adoption.

Whitespace-only compaction is smaller: the email pair goes from 1,307 to 1,240 input tokens; the core set from 4,814 to 4,517. Compacting the HTTP request alone does not achieve these savings if the chat template serializes the parsed schemas with its usual spacing. Do not change tool names, argument names, or Ling's tool-call delimiters merely to shorten the wire format.

**6. Treat retry waste as a recovery-design issue.**

The current loop allows three attempts within each of eight steps. After exhausting an inner retry sequence, an unmet execution obligation can start another outer step with another forced request. An isolated fake-client probe of the actual `run_agent` reproduced **24 model calls with no tool call and no progress**.

An older September 7 trace also records 24 attempts/31.16 seconds for organizing debug files. The later September 8 replay shows that particular route improved, so it is historical evidence of the failure shape, not a claim that this exact prompt still fails.

Do not solve this by blindly reducing retry limits. Nudges change the request, empty-response recovery can resample, and later calls can succeed. First target validated terminal failures or identical effective requests with deterministic sampling. A broader no-progress policy should track obligations, rejected calls/arguments, errors, and new evidence, preserving useful corrective retries. Require success/receipt equivalence on known recovery cases before accepting a latency reduction.

**7. Revisit thinking only as a narrow, measured Ling experiment.**

The blanket tool-selection warning in [no_thinking_kwargs](/Users/adijain/Desktop/MOE_Project/service/config/__init__.py:195) cites older Agents-A1 failures. [Current model configuration](/Users/adijain/Desktop/MOE_Project/service/config/models.yaml:96) records Ling succeeding at greedy tool calling with thinking disabled. That supports a per-model experiment for simple, forced selection steps. It does not establish equivalence for ambiguous tasks or multi-step recovery. Ling's own [model card](https://huggingface.co/inclusionAI/Ling-3.0-tiny) recommends thinking for better performance, reinforcing the need to keep this task-specific.

**Measure these changes with accurate boundaries.**

[stream_events](/Users/adijain/Desktop/MOE_Project/service/inference/omlx_client.py:341) currently exposes content/reasoning but not live tool-argument deltas, usage, or the finish reason in its final event. UI first-text time also includes the agent's intentional verification buffering. Existing wall times can include tools, confirmations, loading, and queueing; they are not decode times.

Add per-request model-ready/queue time, first model token across every channel, prompt/cache counts where supported, generated content/reasoning/tool tokens, finish reason, and tool/approval time. If enabling streamed usage, handle empty `choices` arrays first; current parsing indexes element zero. Benchmark warm/cold and new/pinned sessions with fixed source fixtures and no side effects. Compare action selection, arguments, source grounding, attribution, dates, coverage, approval previews, receipts, and recovery success. Retain the existing full answer and memory quality.

Direct dispatch, scoped prompts, lexical retrieval, deterministic digests, narration/fast thinking suppression, corrected retry nudges, and several typed workflows already exist. Their presence should not be counted again as new optimization savings. Lower model quantization, blanket thinking suppression, tighter answer caps, arbitrary history truncation, and parallel generations on the same local model do not meet the requested quality-preservation bar on the available evidence.
