# Ideate model evaluation: Inkling 2 Mini oQ3e (measured)

Scope change from the coordinator: only `Inkling-2-Mini-oQ3e-mtp` was measured. The oQ4e build was never loaded, so there is no oQ3e vs oQ4e comparison. Engine: the user's oMLX at 127.0.0.1:8000, one model resident throughout, oQ3e loaded before and after. Host: M5 Pro, 24 GB, already swapping (2.3-3.0 GB swap used while measuring). Raw data: `docs/ideate/bench-results.json`.

## Methodology
Non-streaming `/v1/chat/completions`, reasoning at the model default, numbers taken from the response `usage` block (`time_to_first_token`, `generation_tokens_per_second`, `reasoning_tokens`). Tool test: 10 requests with a `tools` array (read_file, write_file, run_shell). Code test: 3 invented tasks, output executed against hidden asserts. A sampler logged `vm_stat` swapouts/swapins, free-memory percent, swap used, oMLX RSS and the engine's `current_model_memory` every 10 s. Single run per item; no repeats.

## Speed (single request, warm)
| Prompt | Prompt tokens | Output tokens (reasoning) | TTFT | Decode tok/s | Wall |
|---|---|---|---|---|---|
| short | 22 | 69 (61) | 0.2 s | 56.0 | 1.5 s |
| ~2.3k-token log summary | 2339 | 1471 (1388) | 4.9 s | 48.8 (prefill 478 tok/s) | 35.1 s |
| code, ~300-token ask | 55 | 836 (591) | 0.6 s | 56.6 | 15.4 s |

Decode in the tool and code runs was 50-70 tok/s. Reasoning is 70-95% of every reply, so wall time is dominated by thinking tokens, not by the answer. Take the earlier 18.9 tok/s first-test figure as a cold start.

## Tool-call reliability (API level)
| Result | Count |
|---|---|
| Valid structured `tool_calls` with parseable JSON args | 10 / 10 |
| First call was the expected tool with correct args | 7 / 10 |
| Valid but exploratory first call (`ls -la`, `pwd && ls` before acting) | 3 / 10 |
| Malformed JSON / no tool call / tool call inlined in content | 0 / 10 |

The three "wrong" ones (run pytest, write fib.py, make notes/todo.md) each issued a look-around shell call first, which is acceptable agent behaviour. Prefix caching was active (311 cached tokens) and did not corrupt results.

## Code quality
| Task | Result |
|---|---|
| `parse_duration` with 12 asserts incl. error cases | pass (1698 tokens, 34 s) |
| HTML counter page (+/-/reset, never below 0), run in a fake DOM under node | pass (23 s) |
| Fix three planted bugs in a 30-line script | pass (8 s) |

## Memory
Engine status reports the model resident at 15.1 GB (`current_model_memory`, 15,096,463,178 bytes). The oMLX server process RSS peaked at 6.8 GB (API stage) and 7.1 GB (harness stage); the rest of the weights are not in RSS. Host free memory fell to 11% and then 6% (harness stage). Swap used peaked near 3.0 GB; swapouts rose by 142k pages (about 2.3 GB) in the API stage and 84k pages (about 1.4 GB) in the harness stage. No stage was stopped for pressure.

## The blocking finding: oMLX prefill memory guard
oMLX rejects any request whose estimated prefill peak exceeds its limit: 19.95 GB here (dynamic ceiling pinned at 21.00 GB minus a 1.05 GB margin). With oQ3e resident at about 13.3-13.8 GB, the KV+SDPA budget is about 6.6 GB. The estimate for this model starts near 6.5 GB for tiny prompts and rises about 0.2 GB per 1k tokens (measured: 1.8k tokens passed; 2.0k-12k tokens were rejected, with estimates of 7.97 to 9.91 GB). The limit moves a little with free memory: a 2.3k-token prompt passed during the first speed run and a 2.0k one was rejected minutes later.

Consequence: a conversation of roughly 1.5-2k tokens is the practical ceiling right now, regardless of the advertised 1M window. That is smaller than any agent's system prompt plus tool schemas plus a couple of tool results. Both harnesses were cut off by this guard (see `harness-live.md`). The fix is on the engine side (raise `custom_ceiling_bytes` in oMLX Memory settings, or free memory), which is the user's setting and was not changed. A larger model build (oQ4e, 16 GB) can only make the budget tighter.

## Recommendation
- Model build: oQ3e is the only build that has a chance of fitting a notch harness on a 24 GB Mac, and on quality it is good enough: 10/10 valid structured tool calls, 3/3 code tasks, 50-70 tok/s. oQ4e is unmeasured; do not choose it without a measurement, and expect it to have a smaller context budget.
- Ship gate: do not ship Ideate on this model until the prefill ceiling is dealt with. Either (a) set Ideate to use a lean harness prompt of about 1k tokens and compact aggressively, accepting 3-4 tool turns per task, or (b) have Ideate require/raise the oMLX memory ceiling, or (c) pick a smaller model for Ideate.
- Set reasoning to low for Ideate; most output tokens are thinking.
