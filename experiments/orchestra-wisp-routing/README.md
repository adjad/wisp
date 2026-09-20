# Orchestra setup for Wisp intent and tool routing

This harness asks candidate Orchestra/Understudy catalog models to classify a Wisp request and select the tools needed to fulfill it. It never executes a Wisp tool. Labels come from Wisp's checked-in 85-case adversarial router corpus, and the compact catalog comes from the 171-tool activation inventory.

## Tool location

The durable worktree-local checkout is:

```text
.understudy/vendor/understudy-agent-tools
```

Its pinned commit is `c70c889e5167a6e7a5f94f901411d42108c4ef2e` (public package version 0.6.41). The executable wrapper is:

```bash
experiments/orchestra-wisp-routing/understudy
```

The earlier `/private/tmp/wisp-orchestra-pilot-tools` checkout is only temporary. No global CLI or Codex plugin is required. `.understudy/` is ignored because it can contain credentials, prompt/completion captures, and evaluation outputs.

## 1. Verify the local setup

```bash
experiments/orchestra-wisp-routing/understudy status --json
python3 experiments/orchestra-wisp-routing/evaluate.py --self-test
python3 experiments/orchestra-wisp-routing/evaluate.py --backend plan --limit 10
```

The last command makes zero network calls and prints corpus/tool-inventory hashes.

## 2. Sign in

Account creation and credential provisioning are external changes. Run these only when ready:

```bash
experiments/orchestra-wisp-routing/understudy login --email YOU@example.com --send-code
experiments/orchestra-wisp-routing/understudy login --code ONE_TIME_CODE
experiments/orchestra-wisp-routing/understudy doctor --hosted
```

The CLI stores the key outside the repository. Do not paste it into chat or commit it. After authentication, disable optional product telemetry if desired:

```bash
export UNDERSTUDY_TELEMETRY=0
```

## 3. Inspect models and create the workload

```bash
experiments/orchestra-wisp-routing/understudy models list --json
experiments/orchestra-wisp-routing/understudy projects list --json
experiments/orchestra-wisp-routing/understudy workloads create \
  --from-card experiments/orchestra-wisp-routing/workload-card.json \
  --project PROJECT_SLUG \
  --capture
```

Use the project slug returned by the account. If a workload with this name already exists, reuse it. Ensure the eval workload has no configured route before a keyless catalog sweep; a 0% route still counts as configured.

## 4. Check Wisp's local routing contract

Use the repository's dependency environment:

```bash
/Users/adijain/Desktop/MOE_Project/.venv/bin/python \
  experiments/orchestra-wisp-routing/evaluate.py \
  --backend wisp --limit 10
```

This calls Wisp's router directly, reads its required/forbidden tool contract, and executes no tools.

## 5. Plan a hosted comparison

No request is sent without `--execute`:

```bash
python3 experiments/orchestra-wisp-routing/evaluate.py \
  --backend understudy \
  --models MODEL_A,MODEL_B \
  --project PROJECT_SLUG \
  --limit 10
```

The plan reports zero calls. For a real run, use the CLI wrapper so the stored key is injected into the child process:

```bash
experiments/orchestra-wisp-routing/understudy run -- \
  python3 experiments/orchestra-wisp-routing/evaluate.py \
  --backend understudy \
  --models MODEL_A,MODEL_B \
  --project PROJECT_SLUG \
  --workload wisp-intent-tool-selection \
  --limit 10 \
  --execute
```

Each request uses streaming, at most 256 output tokens, and runs serially. Results are private files under `.understudy/evals/wisp-intent-tool-selection/`. The runner records the effective model, route, mode, request ID, latency, token usage, strict tool/intent grading, and errors. A requested/effective model mismatch fails the row.

Start with ten cases and two models. Review actual account pricing and usage before expanding to all 85 cases. This harness cannot enforce a dollar cap because Orchestra does not publish a stable price table in the API response.

## Interpretation

A strict pass requires the required tools, one acceptable alternative where applicable, no extra or forbidden tools, compatible intent domains, correct tool/no-tool classification, and the expected channel/target clarification when the corpus specifies one.

This evaluates routing decisions, not task execution or answer quality. It does not send messages, create reminders, change files, or call Wisp tools. Passing is evidence for a candidate model comparison, not authorization to replace Wisp's local router or enable cloud routing in production.
