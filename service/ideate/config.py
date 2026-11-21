"""Ideate's tunables, in one place. README.md says why each has the value it has.

The lean profile exists because of the engine's prefill memory guard (docs/ideate/model-eval.md,
PLAN.md D10): with Inkling 2 Mini oQ3e resident, oMLX refuses a request whose prompt is past
roughly 1.5-2k tokens. Raising that ceiling is the user's engine setting and Ideate never
changes it, so the harness runs small: a short system prompt, four tools, short tool results
and an early compaction.
"""
from __future__ import annotations

# The model (PLAN.md D7). The id is what the engine lists; the label is what the tab shows.
MODEL_ID = "Inkling-2-Mini-oQ3e-mtp"
MODEL_LABEL = "Inkling 2 Mini"
PROVIDER = "omlx"
# Where the engine expects it: <models folder>/<ORG>/<MODEL_ID> (the Hugging Face id's layout).
MODEL_ORG = "mlx-community"
# Places a copy may already be on this Mac, relative to the home folder. Each is searched
# for <MODEL_ID> at most MODEL_SEARCH_DEPTH folders down. The Hugging Face cache is searched
# separately (models--<org>--<id>/snapshots/<rev>).
MODEL_SEARCH_DIRS = ("Desktop/OMLX_Model_Files", "Documents/OMLX_Model_Files", ".omlx/models",
                     ".lmstudio/models", "Models")
MODEL_SEARCH_DEPTH = 2
HF_CACHE_DIR = ".cache/huggingface/hub"

# Pi's tool names. grep/find/ls are left out: bash covers them, and each schema costs prompt tokens.
TOOLS = ("read", "write", "edit", "bash")

# The prompt budget the conversation is kept under, in tokens (the guard trips near 1.5-2k).
PROMPT_BUDGET_TOKENS = 1400
# What Pi is told the model's window is. Pi compacts when the context passes
# CONTEXT_WINDOW - COMPACT_RESERVE_TOKENS, which is PROMPT_BUDGET_TOKENS.
# Pi also sizes each reply from this window: max_tokens = window - input - max(4096, 15% of
# input), floored at 1 (pi-ai). 4096 left room for 1 token, measured 2026-11-21. The window
# must cover the budget + Pi's 4096 safety + MAX_OUTPUT_TOKENS; the engine's real limit is
# the prefill guard, which the budget keeps under.
CONTEXT_WINDOW = 8192
COMPACT_RESERVE_TOKENS = CONTEXT_WINDOW - PROMPT_BUDGET_TOKENS
# Recent tokens a compaction keeps as they are; older turns become a summary.
COMPACT_KEEP_RECENT_TOKENS = 500
# The most one model reply may generate (thinking included).
MAX_OUTPUT_TOKENS = 2048
assert CONTEXT_WINDOW - PROMPT_BUDGET_TOKENS - 4096 >= MAX_OUTPUT_TOKENS
# Reasoning effort sent to the engine (Pi's --thinking level). Most of the model's output is
# thinking (70-95%), so Ideate asks for little. Whether the engine honours it is not yet measured.
THINKING_LEVEL = "low"

# A tool result longer than this reaches the model as its head and tail only.
TOOL_RESULT_CHARS = 600
# When the guard still refuses a step, the run starts over this many times with a fresh
# conversation carrying a short summary and the file list, before it gives up.
FRESH_RESTARTS = 1
# The carried summary: at most this many characters of the agent's last words.
CARRY_SUMMARY_CHARS = 500

# Approvals: how long a card waits for an answer before it counts as "no".
APPROVAL_TIMEOUT_S = 600.0
# Cancel: abort first, then the process group is killed after this long.
CANCEL_GRACE_S = 5.0
# Model swap: how long loading the model may take before the run fails.
LOAD_TIMEOUT_S = 240.0
# After a run ends, Inkling stays loaded this long for a follow-up message, then Ideate unloads
# it (Wisp's bundled engine only). Ask's model loads again on Ask's next turn, not straight away.
RELEASE_AFTER_S = 90.0
# Inkling 2 Mini oQ3e resident (measured 15.1 GB), plus room for macOS and the prompt. A run checks
# free memory right before loading it and refuses below this: on 2026-11-21 a second engine holding
# a 12.5 GB model while Inkling loaded froze a 24 GB Mac until the watchdog restarted it.
MODEL_RESIDENT_BYTES = int(15.1 * 1024**3)
MEMORY_MARGIN_BYTES = 3 * 1024**3
# How long Ideate waits for the models it unloaded to leave memory before checking.
EVICT_SETTLE_S = 30.0
# A run that starts while an Ask turn is answering waits this long for it before taking the engine.
ASK_WAIT_S = 30.0

# Sizes kept on disk and sent to the tab.
EVENT_TEXT_CHARS = 2000
COMMAND_TEXT_CHARS = 400
PROMPT_CHARS = 8000
EVENTS_PER_REPLY = 400
FILES_LISTED = 200
HARNESS_LOG_BYTES = 256 * 1024

# Plain words for the tab.
GUARD_ERROR = ("The model ran out of memory room for this step. Try a smaller step, "
               "or raise oMLX's memory limit.")
