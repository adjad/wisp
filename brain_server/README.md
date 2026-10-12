# brain_server

The brain server is optional. It hosts one `service.behavior.BehaviorEngine`
behind a small HTTP API, so the GX10 can run the behavior engine for a Mac in
`brain.mode: remote`. The Mac talks to it through
`service.behavior.client.RemoteEngine`. That client has the same methods and
types as the in-process engine, and the runtime falls back to the local engine
(for 120 s at a time) whenever the brain server is unreachable. By default the
Mac runs the engine in-process (`brain.mode: local`), so nothing here is needed
for the demo. How the engine works: [docs/BEHAVIOR_ENGINE.md](../docs/BEHAVIOR_ENGINE.md).

On the Mac, remote mode is `WISP_BRAIN_MODE=remote` plus
`WISP_BRAIN_URL=https://6wjp.tail4cc204.ts.net:8443` (no trailing slash), with the
bearer token in Keychain (service `com.wisp.inference`, account
`wispbrain:<sha256(URL)>`) or in `WISP_BRAIN_TOKEN`. See `docs/gx10/RUNBOOK.md`.

## Run

```sh
python -m brain_server --db ~/.local/share/wisp-brain/brain.db \
  --token-file ~/.config/wisp-brain/token --host 127.0.0.1 --port 8790 \
  --ollama http://127.0.0.1:11434 --model qwen3.6:35b --tz America/Los_Angeles
```

| Option | Default | Notes |
| --- | --- | --- |
| `--host`, `--port` | `127.0.0.1`, `8790` | Loopback only. Any other address is refused unless you pass `--allow-public`. Expose it privately with `tailscale serve --bg --https=8443 http://127.0.0.1:8790`. |
| `--db` | `~/.local/share/wisp-brain/brain.db` | SQLite file for the engine. |
| `--token-file` | `~/.config/wisp-brain/token` | Must be mode `0600` and at least 32 printable characters. Compared in constant time. |
| `--ollama`, `--model` | not set, `qwen3.6:35b` | Enables the optional `OpenAIPhraser`, using Ollama's OpenAI-compatible `/v1` with no token. Leave `--ollama` out for template text only. Use the gateway's model, so Ollama keeps one model resident. |
| `--phraser-timeout` | 8 s | Per phrasing call. On a timeout the nudge keeps its code-composed text. |
| `--tz` | process timezone | Must match the user's (Mac's) timezone, because patterns use local weekdays and minutes. `install-brain.sh` passes `America/Los_Angeles` unless you give `--tz`. |
| `--mine-interval`, `--nudge-interval` | 900 s, 60 s | Background mining and nudge generation. `--no-background` turns both off. |
| `--log-level` | `info` | `critical`, `error`, `warning`, `info` or `debug`. |

In production `deploy/gx10/install-brain.sh` sets all of this up: venv, token,
the `wisp-brain` systemd user unit, and with `--serve` Tailscale Serve on
`:8443` (it never touches the `:443` gateway mount). See `docs/gx10/RUNBOOK.md`.

## API

All endpoints except `/healthz` require `Authorization: Bearer <token>`. Request
bodies are limited to 2 MB (`413`). JSON shapes are the `to_dict()` forms from
`service/behavior/types.py`.

| Method and path | Body or query | Response |
| --- | --- | --- |
| `GET /healthz` | | `{"ok":true,"engine":"ready"}`, or `503` if the engine is unavailable |
| `POST /v1/observe` | `{"observations":[Observation]}` | `{"count":n}` |
| `POST /v1/snapshot` | `{"source","observations","window_start","window_end","taken_at"}` | `{"upserted","cancelled","moved"}` |
| `POST /v1/mine` | `{"now"?}` | `{"patterns":[Pattern]}` |
| `GET /v1/patterns` | `?status=&kind=` | `{"patterns":[Pattern]}` |
| `POST /v1/patterns/{id}/status` | `{"status":"active\|confirmed\|rejected"}` | `{"pattern":Pattern}`; `404` if the id is unknown, `400` if the status is invalid |
| `GET /v1/predict` | `?now=&horizon_s=` (up to 14 days; omitted = the engine's nudge horizon: at least 24 h and through the end of tomorrow) | `{"predictions":[Prediction]}` |
| `POST /v1/nudges/generate` | `{"now"?}` | `{"nudges":[Nudge]}` (newly created only) |
| `GET /v1/nudges` | `?cursor=&limit=` (up to 500) | `{"nudges":[Nudge],"cursor":n}` |
| `POST /v1/feedback` | `{"nudge_id","verdict","ts"?}`; verdict is `useful`, `not_now`, `wrong` or `undo` | `{"ok":true}` |
| `GET /v1/profile` | `?now=&max_bytes=` (default 700, up to 16384) | `{"text":str}` |
| `GET /v1/describe` | `?subject=&now=` | `{"patterns","predictions","summary"}` |
| `GET /v1/status` | | the engine's `status()` plus `"brain_server": {background, last_mined_at, last_nudged_at, last_error, phraser}` |

Engine exceptions map to HTTP status codes as follows. `RemoteEngine` turns
them back into Python exceptions on the client.

| Engine raises | HTTP status | `RemoteEngine` raises |
| --- | --- | --- |
| `KeyError` | `404` | `KeyError` |
| `ValueError` | `400` | `ValueError` |
| `NotImplementedError` | `503` | `BrainUnavailable` |
| any other error | `500` | `BrainUnavailable` |

Malformed bodies get `422`; more than 20,000 observations in one request get
`413`. A request that waits more than 30 s for the busy engine also gets `503`.
`RemoteEngine` turns a `401` or `403` into `BrainAuthError`, which is a
subclass of `BrainUnavailable`. The client waits 5 s per call (60 s for
snapshot, mine and nudge generation). Transport errors and redirects also raise
`BrainUnavailable`. Redirects are never followed, so the token can't leave the
origin.

## Threading

Every engine call runs on one dedicated worker thread, the same thread that
constructed the engine. That includes HTTP requests and the background
miner/nudger. This serializes access to the engine's SQLite connection
whatever the engine does internally.

## Tests

```sh
WISP_HOME=$(mktemp -d) python -m pytest -q tests/test_brain_server.py tests/test_behavior_client.py tests/test_behavior_phraser.py
```

The full behavior suite is listed in `docs/BEHAVIOR_ENGINE.md`.
