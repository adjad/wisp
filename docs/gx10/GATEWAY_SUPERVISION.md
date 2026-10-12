# GX10 gateway supervision

**Problem.** The Wisp GX10 gateway (`gateway.py`, `127.0.0.1:8767`) was started
by hand. After a reboot nothing restarted it. Tailscale Serve still accepted
HTTPS on `:443`, but its backend was gone, so Send on the Intel Mac did
nothing.

**Fix.** Run the exact same command under systemd with `Restart=on-failure`,
and let it start at boot.

```
Mac ──Tailscale──> https://6wjp.tail4cc204.ts.net:443  (tailscaled, Serve config persisted; NOT changed)
                      └─> http://127.0.0.1:8767         wisp-gx10-gateway.service (user unit, NEW)
                             └─> http://127.0.0.1:11434 ollama.service (system unit; unchanged), model qwen3.6:35b
```

Files are in `deploy/gx10/`:

| File | Purpose |
| --- | --- |
| `wisp-gx10-gateway.service` | Unit template. The installer fills in the `@...@` placeholders. |
| `wait-for-ollama.sh` | `ExecStartPre` helper. It refuses to start if the port is taken, then polls Ollama's `/api/version` for up to 180 s. |
| `install-gateway-service.sh` | Idempotent installer. Supports `--dry-run`, `--takeover` and `--enable-linger`. |
| `rollback-gateway-service.sh` | Removes the supervision and relaunches the recorded manual command. |
| `healthcheck.sh` | Read-only status check on the GX10. |
| `mac-check-gateway.sh` | Read-only check run from the Intel Mac. It reads the token from Keychain and never prints it. |

## User unit or system unit

| | User unit (`~/.config/systemd/user`) — **recommended** | System unit (`/etc/systemd/system`, `User=asus`) |
| --- | --- | --- |
| Needs root | Only once, for `loginctl enable-linger asus`. That may need no sudo, depending on polkit. | Yes, to install, edit and remove it. |
| Starts at boot without login | Only with linger enabled. | Yes. |
| Can order after `ollama.service` | No. A user manager can't see system units, so `ExecStartPre` polls Ollama instead. | Yes, with `After=`/`Wants=ollama.service`. |
| `network-online.target` | Doesn't exist in the user manager. The `After=` line is a no-op there, which is harmless because the gateway binds loopback. | Works. |
| Footprint and rollback | Everything stays under `/home/asus`, owned by `asus`, next to the gateway's own files. | Touches `/etc` on a shared machine. |
| Logs | `journalctl --user -u wisp-gx10-gateway` | `journalctl -u wisp-gx10-gateway` |

**Recommendation: a user unit plus linger.** Nothing system-wide changes
except the linger flag, and rollback is a single script. Waiting on Ollama's
HTTP endpoint is also a better readiness signal than `After=ollama.service`.
`After=` only orders the start of the process. It doesn't wait until Ollama
answers.

Check linger:

```sh
loginctl show-user asus -p Linger   # Linger=yes is required for unattended boot start
loginctl enable-linger asus         # or: sudo loginctl enable-linger asus
```

Without linger the unit still works. It starts when `asus` first logs in
(SSH counts) and keeps running only while asus has a session. That is not
enough for an unattended reboot.

### System-unit alternative (not installed by the scripts)

Use this only if linger is not allowed on the shared host. Don't run it next to
the user unit: pick one.

```ini
# /etc/systemd/system/wisp-gx10-gateway.service   (sudo systemctl daemon-reload && sudo systemctl enable --now wisp-gx10-gateway)
[Unit]
Description=Wisp GX10 inference gateway
Wants=network-online.target ollama.service
After=network-online.target ollama.service
StartLimitIntervalSec=600
StartLimitBurst=20
[Service]
User=asus
WorkingDirectory=/home/asus/.local/share/wisp-gx10-demo/20261010-full-tools-preview-v2
ExecStartPre=/home/asus/.local/share/wisp-gx10-supervision/wait-for-ollama.sh --require-free-port 8767
ExecStartPre=/home/asus/.local/share/wisp-gx10-supervision/wait-for-ollama.sh --timeout 180
ExecStart=/usr/bin/python3 -B /home/asus/.local/share/wisp-gx10-demo/20261010-full-tools-preview-v2/gateway.py --tokens /home/asus/.config/wisp-gx10-demo/20261010-v1/tokens.json --model qwen3.6:35b
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
```

Replace `/usr/bin/python3` with the interpreter the manual process uses. The
installer prints it on the line "Using the running gateway's interpreter".

## What the installer does

1. **Preflight.** It checks for `systemctl --user`, `ss` and `curl`. It
   checks that `gateway.py` exists and that its sha256 starts with
   `eb7080b3`; if not, it refuses unless you pass `--allow-hash-mismatch`. It
   checks that `tokens.json` exists and reports its mode, but never reads,
   changes or prints it.
2. **Finds the running gateway.** It looks for a listener on
   `127.0.0.1:8767` (`ss -ltnp`) and for python processes whose command line
   contains the gateway path. It then:
   - reuses that process's interpreter path, keeping venv paths unresolved;
   - warns if its arguments differ from the unit's;
   - warns with the names only (never the values) of `OLLAMA*`, `WISP*`,
     `GX10*`, `PYTHON*` and proxy environment variables. The unit won't
     inherit them.
3. **Renders the unit.** It renders `~/.config/systemd/user/wisp-gx10-gateway.service`,
   backs up any previous version to `~/.local/state/wisp-gx10-supervision/`,
   copies `wait-for-ollama.sh` to `~/.local/share/wisp-gx10-supervision/`, then
   runs `daemon-reload` and `enable`.
4. **Starts it, depending on the port:**
   - **Port free** (for example after a reboot): starts the unit and checks
     that it is active, listening and answering HTTP. An unauthenticated
     `/v1/models` returning `401` is the expected result.
   - **Port taken by the manual process, no `--takeover`:** doesn't start a
     duplicate. Prints how to switch over. The unit is enabled, so the next
     boot is covered.
   - **`--takeover`:** records `/proc/<pid>/cmdline` and the cwd, sends
     `SIGTERM` to the manual PID, waits until the port is free, then starts
     the unit and verifies it. The Mac sees a gap of a few seconds. It refuses
     if the PID isn't this user's `gateway.py`. If the unit doesn't become
     healthy within `--verify-timeout` (60 s), it rolls back by itself and
     relaunches the recorded manual command.
   - **Already supervised:** does nothing, unless the unit file changed or
     you passed `--restart`.
5. **Checks linger.** It reports the linger state. With `--enable-linger` it
   enables linger.
6. **Prints rollback.**

The unit also refuses to start a duplicate on its own: its first
`ExecStartPre` exits with an error if anything already listens on `:8767`.

## Restart semantics (know this for the failure drill)

- `Restart=on-failure` restarts after a non-zero exit, a crash, `SIGKILL` or
  a watchdog or timeout failure. The restart comes after `RestartSec=5`. The
  limit is 20 starts in 10 minutes. After that the unit stays `failed`; run
  `systemctl --user reset-failed wisp-gx10-gateway` and start it again.
- A clean stop doesn't restart the unit. This covers `systemctl --user stop`
  and a plain `kill <pid>` (`SIGTERM` is a "clean" signal to systemd).
- To simulate a crash, use
  `systemctl --user kill -s SIGKILL wisp-gx10-gateway`.
- If you'd rather have it restart on any exit, run
  `systemctl --user edit wisp-gx10-gateway` and add `[Service]` then
  `Restart=always`. The scripts keep `on-failure` as specified.
- If Ollama is still loading at boot, `ExecStartPre` waits up to 180 s. If
  Ollama never comes up, the start fails and systemd retries.

## Logs and status

```sh
systemctl --user status wisp-gx10-gateway
journalctl --user -u wisp-gx10-gateway -f           # live
journalctl --user -u wisp-gx10-gateway -b           # since boot
bash ~/wisp-src/deploy/gx10/healthcheck.sh          # read-only summary
```

## Troubleshooting

| Symptom (from the Mac) | Meaning | Fix |
| --- | --- | --- |
| `mac-check` shows HTTP `000` | Tailscale is down or the Mac is off the tailnet. | Check the Tailscale menu on the Mac, then `tailscale ping 6wjp`. |
| HTTP `502` or `504` | Serve is up but nothing answers on `:8767`. | `systemctl --user status wisp-gx10-gateway`, then start it. |
| HTTP `401` with the Keychain token | Token mismatch. | Don't rotate tokens. Check that the Keychain item and the base URL match. |
| Unit `failed`, `start-limit-hit` | Too many restarts. | `journalctl --user -u wisp-gx10-gateway -n 80`, then `systemctl --user reset-failed wisp-gx10-gateway` and start it. |
| Unit `failed`, "already in use" | The manual gateway is still running. | Stop that process, or run the installer with `--takeover`. |
| Unit `activating` for a long time | Waiting for Ollama. | `sudo systemctl status ollama`. |
| `systemctl --user` fails over SSH | `XDG_RUNTIME_DIR` is unset. | Run `export XDG_RUNTIME_DIR=/run/user/$(id -u)`. The scripts do this automatically. |

## Rollback

```sh
bash ~/wisp-src/deploy/gx10/rollback-gateway-service.sh --dry-run
bash ~/wisp-src/deploy/gx10/rollback-gateway-service.sh
```

The rollback script:

- stops and disables the unit;
- moves the unit file to `~/.local/state/wisp-gx10-supervision/*.removed.<ts>`;
- relaunches the gateway with the exact recorded command line and cwd,
  detached with `setsid nohup`, logging to
  `~/.local/state/wisp-gx10-supervision/gateway-manual.log`.

That relaunched process won't survive a reboot, which is the state before the
install. The script doesn't change tokens, Serve, Ollama or linger. If the
install enabled linger, it prints the `disable-linger` command.

## What was and wasn't verified

**Verified** in a Linux sandbox. That sandbox had no systemd user manager, so
`systemctl` and `loginctl` were stubbed and a fake `gateway.py` listened on
`:8767`:

- all scripts pass `bash -n` and `shellcheck` cleanly;
- both rendered units pass `systemd-analyze verify`;
- `--dry-run` changes nothing;
- the hash mismatch is refused;
- the installer detects the manual process, reuses its interpreter, and
  refuses to start a duplicate;
- state (cmdline and cwd) is recorded;
- `--takeover` kills only the matching PID;
- when the unit fails its health check, the installer rolls back by itself
  and the recorded manual command relaunches and listens again;
- running rollback while the port is in use doesn't relaunch.

**Not verified:**

- whether the real user manager starts the unit on the GX10;
- linger and polkit behaviour on DGX OS;
- whether `gateway.py` behaves the same under systemd's minimal environment.
  The installer lists any env var names that might matter.
- start at reboot;
- real Ollama, Tailscale and the 35B model.

The runbook steps verify each of these on the real host.
