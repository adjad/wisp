# Fresh demo of Wisp on your own Mac

Show Wisp as if it had just been installed (empty Brief and Day, no caches, no
"Before you ask" cards, no memory facts, no know-you profile, first-run onboarding
again) while it reads your **current real** Calendar, Reminders, Mail, Messages,
Notes and Contacts. When you finish, your normal Wisp comes back exactly as it was.

## Start a demo

```bash
python3 scripts/wisp_demo.py start --quit-app --launch
```

- `--quit-app` quits Wisp first. Without it the command refuses while Wisp, its
  backend (port 8765) or its engine (port 8000) is running.
- `--launch` opens Wisp when the demo home is ready.

Wisp then starts from nothing. The know-you onboarding, setup guide, notch tip and
permission prompts appear again. The Brief, Day cards, proactive cards and
summaries are rebuilt from your real sources as they sync, which takes a few
minutes, as on a new install.

## End the demo

```bash
python3 scripts/wisp_demo.py stop --quit-app --launch
```

Your own `~/.moe` and Wisp's settings are put back as they were. The demo's state
moves to `~/.moe-demo-last` so you can look at it afterwards. The next demo deletes
it. Pass `--discard` to delete it straight away.

`python3 scripts/wisp_demo.py status` says whether a demo is running.

## What it does

| | Start | Stop |
| --- | --- | --- |
| `~/.moe` (all of Wisp's own state) | renamed to `~/.moe-demo-saved`; a new empty `~/.moe` takes its place | demo home moves to `~/.moe-demo-last`; `~/.moe-demo-saved` is renamed back to `~/.moe` |
| App settings (`com.wisp.assistant` UserDefaults: `wisp.setupGuide.shown`, `wisp.notchTip.shown`, `wisp.didRequestPermissions`, window frames, …) | exported to a backup inside the saved home, then cleared | cleared, then imported from the backup; the result is checked against the backup |
| Bundled engine (`~/.moe/engine`, ~9 GB) | APFS clone into the demo home: instant, no extra disk, no re-download; the original is not touched | discarded with the demo home |
| `config.yaml`, `gmail.json`, `google_workspace/`, `mcp.json` | copied in, so the demo uses the same model roster, engine mode (16k context, MTP, thinking settings live in the engine's `base/model_settings.json`) and connections | discarded with the demo home |
| macOS privacy permissions (TCC) | kept: they belong to the app, not to `~/.moe` | kept |

Both commands move whole directories with `rename`, so nothing in your real
`~/.moe` is copied or rewritten. Start writes only two records into it before it
moves it (the settings backup and `.wisp-demo.json`), and stop removes both.

## Sends during a demo

The demo is a normal Wisp, not a sandbox. The app hard-codes `~/.moe`, and a
backend whose home is anything else counts as a sandbox. The app refuses to act on
a sandbox backend, and a sandbox backend refuses port 8765. Reads from your real
sources work as usual. Every email, message and other outbound action still needs
your on-screen confirmation, exactly as in normal use. Deny any send you do not
want to happen in front of an audience.

## If something goes wrong

- **`stop` says a file is open or a port is in use.** Wisp is still running.
  Quit it from the menu bar (or pass `--quit-app`) and run `stop` again.
- **`start` failed partway.** If `~/.moe-demo-saved` exists, your real home is
  in it. `status` shows whether the demo record is there, and `stop` restores it.
- **Restoring by hand.** Quit Wisp, move `~/.moe` aside, rename
  `~/.moe-demo-saved` to `~/.moe`, then run
  `defaults import com.wisp.assistant ~/.moe/.wisp-demo-defaults.plist` and delete
  the two `.wisp-demo*` files from `~/.moe`.
