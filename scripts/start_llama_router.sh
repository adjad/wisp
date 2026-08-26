#!/usr/bin/env bash
# Start the llama.cpp router directly (the GUI app's translocated bundle
# doesn't survive a reboot, so this is the reliable path). Loads models
# on-demand per scripts/llama_router_models.ini -- load-mode=none and
# no-mmproj avoid the mmap page-fault storm that panicked the machine
# on 2026-08-20 (see memory: llama-cpp-mmap-panic).
set -euo pipefail
cd "$(dirname "$0")"

if curl -fsS http://127.0.0.1:9931/v1/models >/dev/null 2>&1; then
    echo "Router already running on 127.0.0.1:9931."
    exit 0
fi

nohup /Users/adijain/.llama-app/llama serve \
    --models-preset "$(pwd)/llama_router_models.ini" \
    --host 127.0.0.1 --port 9931 \
    --models-max 1 --fit-target 2048 --sleep-idle-seconds 300 --jinja \
    --log-file /tmp/llama-server.log \
    > /tmp/llama-router-stdout.log 2>&1 &
disown

sleep 1
echo "Router starting on 127.0.0.1:9931 (log: /tmp/llama-server.log)."
echo "Models available: curl -s http://127.0.0.1:9931/v1/models"
echo "Chat: curl -s http://127.0.0.1:9931/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"unsloth/Qwen3.8-27B-GGUF:IQ4_XS\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
