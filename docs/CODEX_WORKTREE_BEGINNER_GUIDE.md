# Wisp + Codex Worktrees: A Beginner Guide

You do not need to learn or run Git commands. Use the pinned **Wisp Control Center** for the whole workflow:

```text
Delegate: <what you want built>
Status
Ship: <finished task title>
```

The Control Center creates a separate Worktree, chooses the model, monitors progress, asks the worker to test and save its work, and pushes it to GitHub. A separate **Wisp Release Auditor** critiques the exact change, then **Wisp Live QA** executes it safely with test data. **Wisp Repository Maintainer** can autonomously repair findings and GitHub failures on its own branches. When both gates pass and you say `Ship`, the Control Center reconciles the latest code, creates or updates the pull request, waits for checks, merges into `main`, verifies GitHub, and archives the completed task.

## The basic idea

Think of the Wisp repository as a shared workshop:

| Term | Plain-English meaning |
| --- | --- |
| Repository | The Wisp project and its saved history. |
| Local | The main desk. Use it for integration and final testing. |
| Worktree | A separate temporary desk with its own copy of the files. |
| Branch | A named line of work, such as `codex/memory-polish`. |
| Commit | A labeled snapshot of changed files. |
| Diff | A view of what changed between two snapshots. |
| Merge | Bringing a finished branch into the main line of work. |
| Conflict | Two pieces of work changed the same code and need reconciliation. |

A Worktree prevents one Codex task from overwriting another task's files. Each implementation outcome should have its own Worktree.

## Your Wisp control surfaces

### Wisp Control Center

This is the one place you normally use. Use it to:

- assign new implementation tasks;
- ask for a summary of all active work;
- identify tasks that need your approval or input;
- review or ship completed work;
- archive completed tasks.

The Control Center coordinates other tasks and keeps Local stable; feature code remains in separate Worktrees.

### MOE_Project

You only need to open this project when you want deeper detail, such as:

- every individual task;
- a task's detailed conversation;
- changed files and diffs;
- test results;
- Worktree and branch controls.

### Scheduled

The **Wisp task progress monitor** posts a compact dashboard to the Control Center every 15 minutes while work is active. It stays quiet when there is nothing active or awaiting review.

## The automated lifecycle

| State | What happens automatically | What you do |
| --- | --- | --- |
| Active | The Worktree task implements and tests the outcome. | Nothing. |
| Needs input | The Control Center reports the exact decision or approval needed. | Answer in the Control Center. |
| Auditing | An independent read-only agent critiques the complete committed diff. | Nothing. |
| Changes requested | The original builder fixes blocking audit findings and returns the new commit for re-review. | Nothing unless a product decision is needed. |
| Live testing | A separate agent executes the exact candidate in isolated staging without touching real mail, files, or system settings. | Nothing unless a macOS permission is genuinely required. |
| Ready for review | Both independent gates passed; the Control Center summarizes changes, findings, live results, tests, and risks. | Review the summary or ask `Review: <task>`. |
| Shipping | The Control Center refreshes GitHub state, reconciles `main`, reruns affected checks, and creates or updates the PR. | Nothing unless a real conflict or failed check needs a decision. |
| Merged | The Control Center verifies the remote `main` commit and archives the task. | Nothing. |

`Ship: <task>` is the only approval you provide for the normal Git delivery path, but the Control Center will accept it only after the independent audit and Live QA pass. It applies only to the named task and never permits force pushes, bypassing the gates or required checks, or bundling unrelated work.

## What the Release Auditor checks

The Release Auditor is separate from the builder and cannot change code or approve its own fixes. For every committed candidate it checks:

- correctness and regressions;
- security, privacy, permissions, and possible data loss;
- concurrency and performance risks;
- confusing or broken user experience;
- weak, missing, or misleading tests;
- unrelated files or incomplete validation.

It reports prioritized findings and one verdict: `PASS`, `PASS_WITH_NOTES`, or `BLOCK`. A blocking finding goes to the Repository Maintainer or original builder automatically. Any new commit must be audited again, so a prior pass cannot accidentally cover later changes.

## The three standing agents

| Agent | Purpose | Allowed to change code? |
| --- | --- | --- |
| Wisp Release Auditor | Skeptically reviews every exact candidate diff and blocks consequential defects. | No. |
| Wisp Live QA | Runs the real candidate in isolated staging and checks startup and changed workflows. | No. |
| Wisp Repository Maintainer | Fixes eligible findings, failed checks, review feedback, and `autofix` issues on a branch or draft PR. | Yes, but never directly on `main`. |

The Control Center coordinates all three. You continue using only `Delegate`, `Status`, `Review`, and `Ship` in the pinned Control Center task.

## Recommended workflow: no terminal required

### Step 1: Assign one clear outcome

Open **Wisp Control Center** and write:

> Delegate: [describe one outcome]

Example:

> Delegate: add tests and polish the conversation-memory review UI.

### Step 2: Confirm that a separate task appeared

The new task should:

- appear as a separate task under **MOE_Project**;
- have its own title;
- show **Worktree** as its environment;
- contain the implementation conversation outside the Control Center.

If the implementation starts inside the Control Center, send this correction:

> Stop implementation here. Create a separate top-level Worktree task in MOE_Project for this outcome and return the new task to me.

### Step 3: Let the task run

You can leave the task and continue using the app. The task may ask you to:

- approve a command;
- make a product decision;
- provide a missing credential or account selection;
- clarify expected behavior.

Approve only actions you recognize as part of the task. When uncertain, ask the task to explain what the action changes and whether it is reversible.

### Step 4: Check progress

Ask **Wisp Control Center**:

> Give me a concise status report for every active Wisp task. Show what is running, what is complete, what is blocked, and anything that needs my input.

For full detail, open the individual task under **MOE_Project**.

Status labels generally mean:

| Status | Meaning |
| --- | --- |
| Active | The task is currently working. |
| Idle | The last turn finished; it may be complete or waiting for another message. |
| Needs input | The task cannot continue without you. |
| Blocked | The task encountered a problem it cannot resolve safely. |

An **Idle** task is not automatically finished. Read its latest message or ask the Control Center.

### Step 5: Review the finished task

A good completion message should include:

- what was implemented;
- files changed;
- tests run and their results;
- incomplete work or known risks;
- a commit or branch containing the work.

If you want more detail, send:

> Review: [task title]

You can inspect the task's diff in Codex if you want to see the exact code changes. You do not need to understand every line; look for unexpected files or obviously unrelated changes.

### Step 6: Ship the result

When you are happy, return to **Wisp Control Center** and say:

> Ship: [task title]

The Control Center handles fetch, safe pull, reconciliation, tests, commits, pushes, the pull request, required checks, merge, remote verification, and archival. It stops only for a real conflict, failed check, expired login, or product decision.

### Step 7: Confirm the result

The Control Center reports:

- that Local is clean;
- the pull request and merged commit;
- tests that passed or failed;
- remaining work;
- whether GitHub `main` was verified and the source task was archived.

If Local is not clean, ask:

> Explain every uncommitted Local change and its owner. Do not discard anything.

## Creating a Worktree manually in Codex

Use this only when you do not want the Control Center to dispatch the task.

1. Open **MOE_Project**.
2. Start a new task.
3. Select **Worktree** below the prompt box instead of **Local**.
4. Choose the clean branch that the Control Center identifies as the current integration branch.
5. Enter one specific outcome and submit the task.

Codex creates an isolated Worktree for that task. It initially may use a detached `HEAD`, which simply means the work is not yet attached to a named branch.

When the task is ready to keep, the task header may offer **Create branch here**. Use a short branch name with the `codex/` prefix, such as:

```text
codex/memory-review-polish
```

If you are unsure, do not press branch or transfer controls. Ask the Control Center to integrate the task.

## When to use Local and Worktree

| Situation | Use |
| --- | --- |
| Building or changing a feature | Worktree |
| Fixing an independent bug | Worktree |
| Exploring a risky idea | Worktree |
| Reviewing code without edits | Either; Worktree is safer if edits may follow |
| Combining finished tasks | Local through Wisp Control Center |
| Running final whole-app validation | Local through Wisp Control Center |
| Small integration-only correction | Local through Wisp Control Center |

## Worktrees versus subagents

They solve different problems:

- A **Worktree task** is a separate top-level Codex task with isolated files. Use it for an independent implementation outcome.
- A **subagent** is a helper nested inside one task. Use it for bounded research, review, or test analysis.

If you want a separate task you can monitor and message directly, explicitly ask for a **separate top-level Worktree task**.

## Automatic model selection

Wisp Control Center chooses the model and reasoning effort when it creates each task. The policy favors quality because your Pro usage allowance is generous.

| Work type | Model | Typical reasoning |
| --- | --- | --- |
| Hard architecture, multi-system integration, security, migrations, concurrency, or difficult performance work | GPT-6 Astra | High or Extra High |
| Complex implementation, debugging, refactoring, production review, or careful research | GPT-5.6 Sol | High; Extra High for unusual risk or difficulty |
| Ordinary well-scoped features, bug fixes, tests, or repository documentation | GPT-5.6 Terra | Medium or High |
| Mechanical formatting, extraction, approved fixture generation, or bulk transformation | GPT-5.6 Luna | Low or Medium |
| Near-instant coding iteration where speed matters more than depth | GPT-5.3 Codex Spark | Only when you explicitly request it |

The Control Center reports the selected model, reasoning level, and rationale whenever it creates a task. If the task is difficult to classify, it chooses the stronger option.

Ultra reasoning is not selected automatically. Ultra can create nested subagents, which would make the top-level task structure less predictable. Ask for Ultra explicitly when you actually want nested parallel agents.

You can override the router at any time:

> Create this as a separate Worktree task using GPT-6 Astra with Extra High reasoning.

Or let the Control Center decide:

> Create this as a separate Worktree task. Choose the best model and reasoning effort for quality, and tell me why.

## Safety rules for a Git beginner

Follow these rules and your work remains recoverable:

1. Keep only the Control Center writing to Local.
2. Use one Worktree per independent outcome.
3. Never discard changes whose owner is unclear.
4. Never move several active tasks into Local together.
5. Do not delete a Worktree until its useful changes are committed and integrated.
6. Do not force a branch checkout when Codex says the branch is already used by another Worktree.
7. Ask the Control Center to resolve conflicts instead of choosing one side yourself.
8. Treat passwords, API keys, `.env` files, and account data as secrets; do not commit them.

Avoid these destructive Git commands unless an experienced operator has verified the exact target and recovery plan:

```text
git reset --hard
git clean -fd
git checkout -- <file>
git push --force
```

## Safe optional Git commands

You do not need these commands, but they are safe ways to inspect state in a terminal:

```bash
# Show changed files and the current branch.
git status

# Show the current branch name.
git branch --show-current

# Show recent saved snapshots.
git log --oneline -10

# Show unstaged code changes without modifying them.
git diff

# Show staged code changes without modifying them.
git diff --staged
```

These commands only read repository state.

## Common problems

### The new work happened inside Wisp Control Center

Send:

> Stop implementation here. Dispatch this as a separate top-level Worktree task and return the new task to me.

### I cannot find the task

Open **MOE_Project** and look through its tasks. You can also use Codex task search with the outcome name.

### The task is Idle but I do not know whether it finished

Ask:

> Is this task complete? If yes, provide the standard integration handoff. If not, state the next unfinished step.

### Codex reports a conflict

Do not choose “ours,” “theirs,” overwrite, clean, or reset. Ask:

> Pause without discarding anything. Send the conflict, affected files, and intended behavior to Wisp Control Center for reconciliation.

### Codex says a branch is already checked out

Do not force it. A Git branch cannot normally be checked out in two Worktrees simultaneously. Ask the Control Center whether to use **Transfer**, integrate the commit, or create a different branch.

### A Worktree is missing a local configuration file

Git-ignored files do not always move with a Worktree. Do not copy secrets into source control. Ask the Control Center to configure `.worktreeinclude` or provide a safe setup step.

## A complete example

You want to improve Wisp's memory review experience.

1. Tell the Control Center to create a separate Worktree task.
2. Confirm a task such as **Polish Wisp memory review** appears under MOE_Project.
3. Let it implement and test the change.
4. The progress monitor notifies you when it completes or needs input.
5. Ask the Control Center for the task's handoff.
6. Ask the Control Center to integrate it into Local and run combined tests.
7. Review the coordinator's summary.
8. Archive the completed task.

At no point do you need to run a Git command yourself.

## Copy-paste commands for Wisp Control Center

Create a task:

> Create a separate top-level Worktree task in MOE_Project to [outcome]. Start from the current clean integration branch, leave Local untouched, run relevant tests, provide a complete handoff, and monitor it.

Check everything:

> Give me a concise status report for all Wisp tasks, including running, idle, blocked, complete, and anything needing my input.

Integrate a result:

> Integrate [task title] into Local, preserve unrelated work, run combined tests, report the integration commit, and archive the source task only after verification.

Handle a conflict:

> Preserve both sides, identify the intended behavior of each change, resolve the conflict in Local, run affected tests, and explain the resolution.

Pause safely:

> Pause this task without reverting, cleaning, overwriting, or discarding anything. Provide a handoff to Wisp Control Center.

## Official documentation

- [Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
- [Codex Projects and chats](https://learn.chatgpt.com/docs/projects)
- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
