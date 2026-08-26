#!/usr/bin/env bash
# Fetch a GGUF model into the Llama.app-managed cache so its router server
# (already running on 127.0.0.1:9931 with --models-max/--fit-target/--sleep-idle
# guardrails) can load it on demand, instead of spawning a second raw
# unmanaged `llama serve` process. A raw, unmanaged load of the 27B model
# froze this machine once already -- see scripts/pull_llama_router_model.sh
# git history / chat log for details. Don't reintroduce that path.
set -euo pipefail

ROUTER_URL="http://127.0.0.1:9931"
MODEL_REPO="${1:-unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS}"
WIRED_LIMIT_MB="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)"

if ! curl -fsS "$ROUTER_URL/v1/models" >/dev/null 2>&1; then
    echo "Llama.app's router isn't reachable at $ROUTER_URL."
    echo "Launch the Llama app (menu bar) first -- do not start a second"
    echo "standalone 'llama serve' process, that's what froze the machine."
    exit 1
fi

echo "Wired memory ceiling on this machine: ${WIRED_LIMIT_MB} MB"
echo "Qwen3.8-27B at UD-IQ4_XS is roughly 15-16GB of weights -- close to that"
echo "ceiling on its own. Close Wisp/anything else holding a resident model"
echo "before loading it for the first time."
echo
echo "load-mode = none is set for this model in models.user.ini -- the default"
echo "mmap load on 2026-08-20 caused a page-fault/page-in storm severe enough"
echo "to stall the system past the 92s watchdog timeout and trigger a kernel"
echo "panic (panic-full-2026-08-20-011336.0002.panic). Don't remove that key."
echo

echo "Downloading ${MODEL_REPO} into the app's model cache..."
llama download -hf "$MODEL_REPO"

echo
echo "Done. The router will pick it up on next refresh (relaunch the app if"
echo "it doesn't show up within a few seconds). Query it at:"
echo "  ${ROUTER_URL}/v1/chat/completions   (model: \"${MODEL_REPO}\")"
echo "It loads on demand and sleeps after 300s idle -- no manual start/stop needed."
