# Model-led routing preview — implementation in progress

This is an isolated, default-off experiment at current-main base
`6ee9e8b76990fb22dbbc46b0be583fa9481da713`, branch
`codex/ling-led-routing-test`. The current checkout is separate from Local,
old router work, model comparisons and datasets. No version metadata changes.

## Intended behavior

On admitted ordinary turns, Ling sees a compact capability index covering every
currently available tool family. It can answer, clarify, or call the virtual
`get_tool_schemas` operation with families and optional tool names. Wisp loads
those tools' authoritative schemas on the **next** model step. Ling then chooses
calls and arguments; the existing executor checks schemas, exclusions,
permissions, confirmations and actual results. A bounded second schema request
can recover a missed family. Discovery reads no user data and performs no action.

Existing covered typed task/workflow execution and pending confirmations retain
their controlled path, including durable effect claims. This first experiment
does not replace those paths. Managed local inference only; cloud and external
provider settings remain unchanged. Broad menus cannot remove user exclusions.

## Current state

The capability catalog, negative-only guard, actual agent loop, and opt-in
`service/main.py` entrypoint connection are implemented in the isolated
worktree. The entrypoint captures an admitted managed local Ling target,
preserves the original request, and uses the shared persistence and client
cleanup path. The flag remains off by default. Short replies to an unresolved
question, active skills, and typed task/workflow ownership keep their existing
path. No Swift, configuration or release version changes are included.

Sixteen pure discovery tests passed and twenty-two synthetic actual-loop,
validation, permission and fake-executor tests passed. The loop tests used
scripted inference, not Ling, and ran before the entrypoint connection. They
are not proof of model accuracy or entrypoint correctness. Twenty-one actual-entrypoint
regressions passed in the first run, with no audit violations and verified owned
SQLite/client/request cleanup. That attempt was rejected by a count guard whose
metadata incorrectly expected twenty-two. It is preserved as failed qualification.
The fresh reviewed run passes all twenty-one cases, with twenty-one unique actual
test identities exactly matching the frozen inventory, no audit violations and
verified cleanup. That run tested commit
`f8d9797d250237294ec2c8d62b8c4e859a30a8fd`; this later documentation-only update
does not change the tested product or test code. The original fifteen cases are
retained; six additions cover stateless mode, target changes, inference failure,
disconnection, baseline direct reads and dry-run discovery. Fixture teardown
closes each owned SQLite connection; the closed runner also accounts for the
import-time stores displaced during the tests. Source syntax and whitespace
checks pass for the exact reviewed entrypoint patch.

Historical controller startup failures and their independent reviews remain
preserved under the private experiment-planning artifacts. The later managed-v8
run completed all twenty-two loop tests with zero audit violations. Its trusted
native dependency imports are not hostile-native containment or live-tool QA.

**There is no verified runnable preview artifact yet.** Connected entrypoint
validation has passed with scripted inference. Actual Ling synthetic inference, latency/memory measurements,
packaging and app QA remain incomplete. No installed app replacement, production
settings change, real-user tool execution, merge or deployment occurred.

## Planned validation and user comparison

Before delivery, use fake tools/clients to verify actual loop admission,
next-step schema expansion, unavailable/excluded tools, malformed selections,
source coverage, permissions, denial, pending ownership, cancellation and the
unchanged default-off path. Then run the standard clean-source preview build,
exact-head checks and independent safety review. Actual Ling synthetic smoke
must be distinguished from scripted control-flow tests.

Representative user checks: “what is up this weej,” “and tomorrow?” after an
agenda, “what did Mom say,” unread summaries, “check messages but don't read
email,” a draft versus a send, ambiguous source/channel, and a partial source
failure. Use a new conversation when comparing baseline and experiment so the
previous arm's answer does not become the next arm's context.

Once a verified artifact is linked here, quit existing Wisp and use:

```bash
scripts/wisp-model-led-preview /absolute/path/to/preview/Wisp.app
# Quit the preview before switching:
scripts/wisp-model-led-preview --baseline /absolute/path/to/preview/Wisp.app
```

The launcher never installs or replaces Wisp and does not edit saved settings.
It uses the chat model already selected in Wisp. Debug Mode can capture offered
schemas and responses for review; tool family decisions are structured metadata,
not private chain-of-thought. A successful synthetic test is not a release gate.

## Planning helpers and ownership

Three GPT-6.1 Sol/high read-only helpers reviewed architecture, boundaries and
test/build design. Each delivered a fresh ACCEPT and waited for GO before reads;
no helper wrote source or executed tests/builds/models. Parent is sole writer.
The initial source admission covered `service/router/model_led.py`,
`tests/test_model_led_routing.py`, `scripts/wisp-model-led-preview`, and this file. Later acknowledgments added `service/agent/loop.py`,
`service/router/router.py` and `tests/test_model_led_integration.py`. The user
explicitly assigned the isolated `service/main.py` copy on October 8; the
Orchestrator recorded that assignment and the new
`tests/test_model_led_entrypoint.py` path. Claude’s Local and PR ownership was
not transferred.
The shared inference client, Swift, configuration, version and old artifacts
remain unchanged. No merge, deploy, publication or installed-app replacement.
