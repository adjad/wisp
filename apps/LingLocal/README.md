# Ling Local

Ling Local is a small native macOS control panel for the local Ling inference engine. It can inspect a checkpoint, start and stop the engine process it owns, check readiness, and send a local test chat request. It does not change Wisp or oMLX settings and does not install or download anything.

## Build

Run `apps/LingLocal/Scripts/build-app.sh` from any directory. The script requires the repository's `tools/ling_engine/run.sh`, `cli.py`, and `engine.py`, plus the system Swift compiler and macOS SDK. It creates `apps/LingLocal/dist/Ling Local.app` and refuses to overwrite an existing app. The engine's Python runtime remains the installed oMLX runtime; the app bundles only the project engine source files.

## Run

Open Ling Local, choose a local checkpoint folder, and select **Inspect** to read its model and quantization metadata. **Start** launches the app's own engine process bound to `127.0.0.1:8767`; **Stop** terminates only that child process. If the port is occupied, the app refuses to start and leaves the existing process alone. If startup fails, check that the oMLX Python runtime is installed and that the selected checkpoint is readable.

The default checkpoint is `/Users/adijain/Desktop/OMLX_Model_Files/TheWirelessPhoenix/Ling-3.0-tiny-oQ4e`.

## Client addresses

- API root: `http://127.0.0.1:8767/v1`
- Wisp base URL: `http://127.0.0.1:8767` — Wisp adds `/v1` itself, so leave that suffix off the base URL.

The server binds to loopback and is intended for local clients. No API key is required. The chat keeps up to 32 complete exchanges (64 messages) and drops the oldest exchange before sending when the limit is reached. Failed sends are removed from history and restored to the input for retry; **New chat** clears the conversation. The chat test displays timing and memory values only when the engine returns them; it does not synthesize metrics. The current chat request uses a bounded, non-streaming response.
