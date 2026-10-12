# Wisp Catch: local commitment detection

Catch notices a firm plan in a synthetic incoming text, adds it to Wisp's own
schedule, and displays the source sentence through the existing reminder
notification. It never writes to Apple Calendar or Reminders.

Rules run first. For phrasing they miss, such as **“I can make it to the
Quad tomorrow at 6PM”**, a resident local Ling model served by oMLX may copy one
sentence. Code checks the full source and exact quote, parses time, and checks
existing commitments before saving. Tomorrow at 6PM qualifies only when it is
within the next 24 hours.

## Rehearse in the app

Build a candidate from the repository; this installs nothing:

```sh
./scripts/wisp-build all --allow-dirty
```

Start oMLX and load the Ling model selected for Wisp's local **fast** role before
the demo. Catch never loads or switches models. Unavailable inference, foreground
work, or invalid output produces no alert. Rehearse once to warm the JSON grammar.

Quit Wisp, then run:

```sh
.venv/bin/python demo/wisp_demo.py preflight
.venv/bin/python demo/wisp_demo.py setup
.venv/bin/python demo/wisp_demo.py launch --app dist/<candidate>/Wisp.app
```

The launcher sets `WISP_QA_SEED=1`, `WISP_ATTENTION_DEMO=1`, and
`WISP_ATTENTION_SYNTHETIC_ONLY=1`. Catch reads the synthetic overlay instead of
calling its live Messages reader. Other app features can still sync native
sources and display real data. `WISP_HOME` isolates Wisp's storage; it does not
disable those other native sync features. Review the app before screen sharing.

In another terminal with the same `WISP_HOME`, if set:

```sh
.venv/bin/python demo/wisp_demo.py drop                       # local model path
.venv/bin/python demo/wisp_demo.py drop --preset quad         # rule fast path
.venv/bin/python demo/wisp_demo.py drop --preset question     # silent
.venv/bin/python demo/wisp_demo.py drop --preset hedge        # silent
.venv/bin/python demo/wisp_demo.py drop --preset outgoing     # silent
.venv/bin/python demo/wisp_demo.py drop --preset ambiguous    # silent
.venv/bin/python demo/wisp_demo.py drop --preset unsupported  # silent
.venv/bin/python demo/wisp_demo.py drop --preset distant      # silent
.venv/bin/python demo/wisp_demo.py drop --preset promo        # silent
.venv/bin/python demo/wisp_demo.py drop --preset scam         # silent
.venv/bin/python demo/wisp_demo.py drop --preset known        # already on file
```

The default fixture puts the plan about two hours ahead, including “tomorrow”
when the date rolls over. Allow a scheduler tick (~30 seconds) while Wisp is
idle. Plans within 15 minutes of an existing commitment are suppressed, so clear
between positive cases or choose different times with `--minutes`.

The alert says **“No matching item in Wisp's synced schedule. Added to Wisp's
schedule.”** This describes the data Wisp has. Alert payloads and titles retain
the exact quote; macOS controls visual truncation and notification permissions.

## Backend-only synthetic rehearsal

This starts no native source readers and publishes through Wisp's durable hub,
without opening the app or showing a macOS notification:

```sh
export WISP_HOME="$(mktemp -d -t wisp-catch)"
# Select the already-loaded local Ling model in this isolated config.yaml:
# roles: {fast: <installed-model-id>}
.venv/bin/python demo/wisp_demo.py drop
.venv/bin/python demo/wisp_demo.py replay
.venv/bin/python demo/wisp_demo.py status
.venv/bin/python demo/wisp_demo.py clear
```

Use a fresh isolated directory. The model ID must match a resident oMLX model;
the packaged role may differ from your normal Wisp settings. Credentials need
no copying: the managed local client uses the existing oMLX configuration.

For the offline stage rehearsal, disconnect Wi-Fi after oMLX and weights are
ready, then drop the natural fixture and confirm the app notification contains
its exact sentence. This is a manual native notification check; automated tests
separately enforce that no live Messages reader is called.

## Bounds and cleanup

- One local inference attempt per tick, one generation at a time, at most five
  seconds, cancelled when foreground work starts.
- Only synthetic incoming Messages reach the model. No mail or sender learning.
- Explicit supported clock times within 24 hours; questions, hedges, promotions,
  scam shapes, competing times and unsupported dates are silent.
- The model fallback is intentionally limited to the whole sentence
  `I can make it to the Quad [day] at [clock] [day]`, with optional supported
  day words. This wording stays a rule miss. The model copies its sentence;
  code validates it. This demo does not support arbitrary natural phrasing.
- Surrounding text is limited to an optional `Hey!`, `Hi!`, or `Hello!` greeting
  and an optional `Bring the blue folder` sentence. Unknown context abstains.
- Dayparts use bounded local hours: morning before noon, afternoon from noon
  until 5PM, evening/tonight from 5PM onward. Contradictory clocks abstain.
- At most three Catch alerts per local day, with durable dedupe per message.
- Bounded cached quotes and abstentions are rechecked against current state.
- Commitment and alert persistence is atomic. The app uses normal durable replay.

After quitting the demo app:

```sh
.venv/bin/python demo/wisp_demo.py clear
```

This removes QA seed commitments, their alerts, and synthetic Catch receipts,
preserving live-message receipts. Relaunching normally disables Catch.

## Automated checks

Use separate isolated processes; Wisp resolves its data directory on import:

```sh
WISP_HOME="$(mktemp -d -t wisp-catch-tests)" WISP_QA_SEED=1 \
  .venv/bin/python -m pytest -q tests/test_attention_detector.py
WISP_HOME="$(mktemp -d -t wisp-attention-tests)" \
  .venv/bin/python -m pytest -q tests/test_attention_slice0.py
```

Mocks cover invalid output, quote grounding, local endpoint enforcement, busy
inference, dedupe, caps and persistence. Actual model rehearsal remains necessary:
mocks do not measure extraction quality or prove native notification delivery.

## Behavior engine demo (fictional persona)

A separate demo shows Wisp learning routines, predicting tomorrow and sending
nudges, some with an Undo for what it already did. The full script, with exact
prompts, talking points and a Plan B, is in
[docs/BEHAVIOR_DEMO.md](../docs/BEHAVIOR_DEMO.md). How the engine works is in
[docs/BEHAVIOR_ENGINE.md](../docs/BEHAVIOR_ENGINE.md).

Unlike Catch, this demo **does write to the Mac's real Calendar, Reminders and
Notes**, but only into containers named **Wisp Demo**. Those containers hold
fictional data for Aria Solano / Cedarloop, and each item is tagged with
`[wisp-demo-id:…]`.

```sh
python3 demo/seed_mac_persona.py seed --dry-run      # print the AppleScript, write nothing
python3 demo/seed_mac_persona.py seed                # or: seed --calendar / --reminders / --notes
.venv/bin/python demo/wisp_demo.py behavior reset --wait   # reset first: removes rehearsal drops from
                                                          # "Wisp Demo", waits ~75 s for a Calendar sync,
                                                          # then POST /behavior/clear (full nudge budget)
.venv/bin/python demo/wisp_demo.py behavior status   # also: run, patterns, predict, profile, nudges
.venv/bin/python demo/wisp_demo.py drop --preset gym-conflict   # meeting on the next gym morning
.venv/bin/python demo/wisp_demo.py drop --preset backtoback     # 4 meetings tomorrow 14:00-16:00
.venv/bin/python demo/wisp_demo.py drop --preset dev            # no Mac change; Dev's window
python3 demo/seed_mac_persona.py remove              # quit Wisp first; deletes only "Wisp Demo"
```

Launch with `WISP_BEHAVIOR=1 WISP_BEHAVIOR_SYNTHETIC_ONLY=1` by starting the app
binary from Terminal. The founder/Intel build turns both on automatically.
`behavior clear` (alias `reset`; `--dry-run` prints the cleanup AppleScript)
works only in synthetic mode. A re-`drop` of an event that is already there
still runs the engine and prints that event's existing nudge. Seeding runs on the macOS system
Python 3.9+ and needs only the standard library; `wisp_demo.py` needs the repo
venv. Re-seeding skips items that already exist.

Tests (Linux-safe, no osascript or backend):

```sh
WISP_HOME=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_seed_mac_persona.py tests/test_wisp_demo_behavior.py
```
