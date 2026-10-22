# Phone gateway protocol (v2)

The iPhone app's **Mac** mode talks to Wisp on the user's Mac through a small,
separate gateway process. The Wisp backend itself stays on `127.0.0.1:8765` and
is never exposed; the gateway is the only thing the phone can reach.

```
iPhone ──(Tailscale / WireGuard)──> gateway  100.x.y.z:8799 ──> 127.0.0.1:8765 backend
```

## Process

- Run with `python -m service.phone_gateway serve [--port 8799]`.
- Binds **only** to this Mac's Tailscale IPv4 (`tailscale ip -4`). It refuses to
  start when Tailscale has no address, and never binds `0.0.0.0`, `::` or a LAN
  address. `--host 127.0.0.1` exists for tests and local debugging only.
- State lives in `$MOE_DIR/phone_gateway/` (default `~/.moe/phone_gateway/`),
  directory `0700`, files `0600`.
- Upstream is fixed at `http://127.0.0.1:8765` (`--backend` overrides it, for
  tests and the isolated 8775 backend only).

## Pairing

`python -m service.phone_gateway pair [--name "Adi's iPhone"]` creates one device:

- token = 32 random bytes, base64url without padding; only its SHA-256 is stored
  in `devices.json` as `{id, name, token_sha256, created_at, last_seen}`;
- prints the pairing link and renders it as a QR code in the terminal:

```
wisp-pair://v1?host=<magicdns name>&ip=<100.x.y.z>&port=8799&token=<token>&mac=<computer name>
```

`host` is the MagicDNS name (`tailscale status --json` → `Self.DNSName`, trailing
dot removed). The phone prefers `host`, falls back to `ip`.

`devices` lists paired devices (never tokens). `revoke <id>` deletes one.

## Auth

Every request except `GET /phone/v1/ping` needs `Authorization: Bearer <token>`.
The gateway hashes the presented token and compares it to each stored hash in
constant time. Failures return `401 {"detail":"unauthorized"}`. More than 10
failures from one peer address in 60 s answer `429` for the rest of that window.
Tokens never appear in logs.

### Ping challenge (v2)

The phone never sends its token to an address that hasn't proved it is the
paired Mac. Tailscale addresses come from 100.64.0.0/10, which carriers also
use for CGNAT. With Tailscale off, a stranger at the Mac's `ip` could otherwise
answer the ping and then collect the bearer token.

1. The phone sends `GET /phone/v1/ping?nonce=<n>`, where `n` is 32 fresh random
   bytes as lowercase hex. The gateway accepts 32..128 lowercase hex characters
   and answers `422` to anything else.
2. The gateway answers `{"wisp_gateway": 2, "proofs": [p, ...]}`, with one proof per
   paired device, sorted:
   `p = hex(HMAC-SHA256(key = SHA-256(token) as raw 32 bytes, msg = "wisp-phone-ping-v1:" + n))`.
   The key is the stored `token_sha256`, so the gateway never needs the token.
3. The phone uses a base URL only when one of the proofs matches its own token,
   checked in constant time. If something answers but no proof matches, the app
   shows "couldn't prove it's your Mac" and sends nothing.

A verified address is trusted for at most 60 s, and only until a request to it
fails. A clock that moves backwards ends the trust at once. After that the phone proves the Mac again before it sends the token.
Model-file downloads never use that cache: every attempt, retries included,
proves the Mac with a fresh ping first. A retry resumes with a `Range` request
rebuilt for that base, and only when the base is unchanged; otherwise it
starts the file over. URLSession's opaque resume data is never reused.

The proof binds the pairing, not the channel. Something on the path that can
reach the real gateway could relay a nonce and its proof. Only a tailnet peer
can reach the gateway, so a CGNAT stranger can't, and the token itself is
protected by WireGuard.

A ping without `nonce` still returns `{"wisp_gateway": 2}`, so older phones keep
working. A v2 phone refuses a v1 gateway, because a v1 gateway can't prove the
pairing. Update the Mac first.

Test vector: token `"A" * 43`, nonce `00112233445566778899aabbccddeeff` →
`2dd110115354b568036ae2d48dec99d6f038e6398e9e184e8a1c7c381c7f32ea`.

## Routes

All bodies are JSON. Anything not listed is `404`. Request bodies are
re-built from the allowed fields only. Unknown fields are dropped, never forwarded.

| Method & path | Upstream | Notes |
| --- | --- | --- |
| `GET /phone/v1/ping[?nonce=<hex>]` | — | No auth. `{"wisp_gateway": 2}`, plus `proofs` when a nonce is given (see Ping challenge). Nothing identifying. A proof only shows that a token is paired, and the device count is visible only to tailnet peers. |
| `GET /phone/v1/hello` | `GET /mode` | `{"gateway_version":2,"mac_name":str,"device":{"id","name"},"backend":{"reachable":bool,"mode":any}}`. Backend down → `reachable:false`, still `200`. |
| `POST /phone/v1/agent` | `POST /agent` | Body `{prompt:str (1..16000 chars), session_id?:str}`. Gateway always sends `debug:false`. Response is the upstream SSE stream (`text/event-stream`), passed through unbuffered, line for line. |
| `POST /phone/v1/agent/approve` | `POST /agent/approve` | Body `{session_id, action_id, approved:bool, scope:"once"\|"always"\|"never", request_id?}`. |
| `GET /phone/v1/sessions` | `GET /sessions` | Pass-through JSON. |
| `GET /phone/v1/sessions/{sid}` | `GET /sessions/{sid}` | `sid` must match `[A-Za-z0-9_-]{1,64}`. |
| `DELETE /phone/v1/sessions/{sid}` | `DELETE /sessions/{sid}` | Same `sid` rule. |
| `GET /phone/v1/today` | `GET /assistant/today` | Pass-through JSON (Today/Day plan). |
| `GET /phone/v1/models` | — | On-device model bundles this Mac offers the phone (below). |
| `GET /phone/v1/models/{id}/files/{name}` | — | Streams one file. Supports `Range: bytes=a-b` (`206`) and `HEAD`. |

Upstream unreachable → `502 {"detail":"wisp_backend_unreachable"}`. Upstream
status codes otherwise pass through.

### SSE events the phone handles (from `/agent`)

`session {id}`, `status {text}`, `routed`, `tool_call {id,name,...}`,
`tool_result {id,result}`, `confirm {id,request_id,tool,reason,preview,grantable,scope_hint}`,
`confirm_timeout {id}`, `message_draft {to,text}`, `delta {text}`, `text {text}`,
`clear_answer`, `reasoning`, `heartbeat`, `error {message}`, `done`. Unknown types are ignored.

### Model bundles

The Mac can hand the phone the on-device Ling weights, so the phone never needs
a HuggingFace download. Bundles are configured with
`serve --model <dir>` (repeatable) or `models.json` in the state directory
(`[{"id": "...", "path": "/abs/dir"}]`).

`GET /phone/v1/models` →

```json
{"models": [{"id": "ling-3.0-tiny-wisp-v3-oq4e", "model_type": "bailing_hybrid",
  "total_bytes": 4612345678,
  "files": [{"name": "model.safetensors", "size": 4600000000, "sha256": "..."}]}]}
```

- `id` = lowercased directory name, `[a-z0-9._-]` only.
- Only regular files directly inside the directory, with an allowed extension
  (`.safetensors .json .jinja .txt .model`), no symlinks, no subdirectories, no
  `.py` files (the phone never executes downloaded code).
- `sha256` is computed once per `(size, mtime)` and cached in the state
  directory, so a 4 GB file is hashed only when it changes.
- The phone verifies every file's SHA-256 after download before loading.
