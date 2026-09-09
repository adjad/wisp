# Wisp + Codex Worktrees: A Beginner Guide

You do not need to learn Git commands to use this setup. Your normal workflow is:

1. Ask **Wisp Control Center** to create a separate Worktree task.
2. Let that task work in isolation.
3. Ask **Wisp Control Center** for status or to integrate the finished task.
4. Review the result in **MOE_Project**.

The Control Center should handle the Git details for you.

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

Use this task to:

- assign new implementation tasks;
- ask for a summary of all active work;
- identify tasks that need your approval or input;
- integrate completed work;
- archive completed tasks.

Do not use it as the place where feature code is written. It should coordinate the other tasks and keep Local stable.

### MOE_Project

Open this project when you want to inspect:

- every individual task;
- a task's detailed conversation;
- changed files and diffs;
- test results;
- Worktree and branch controls.

### Scheduled

The **Wisp task progress monitor** checks every 15 minutes. It should stay quiet unless a task completes, fails, becomes blocked, or needs your input.

## Recommended workflow: no terminal required

### Step 1: Assign one clear outcome

Open **Wisp Control Center** and use this template:

> Create a separate top-level Worktree task in MOE_Project to [describe one outcome]. Start from the current clean integration branch. Keep Local untouched. Have the task run relevant tests, preserve unrelated work, and provide a final handoff. Monitor it and notify me when it completes or needs input.

Example:

> Create a separate top-level Worktree task in MOE_Project to add tests and polish the conversation-memory review UI. Start from the current clean integration branch. Keep Local untouched. Have the task run relevant tests, preserve unrelated work, and provide a final handoff. Monitor it and notify me when it completes or needs input.

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

If that information is missing, send:

> Before integration, provide a handoff with the outcome, files changed, tests run, known risks, and the commit or branch containing the work.

You can inspect the task's diff in Codex if you want to see the exact code changes. You do not need to understand every line; look for unexpected files or obviously unrelated changes.

### Step 6: Integrate the result

Return to **Wisp Control Center** and say:

> Integrate the completed [task title] work into Local. Preserve unrelated changes, run the relevant combined tests, report the resulting commit, and archive the task only after the integration is verified.

Let the Control Center handle the branch, commit, merge, or cherry-pick details. Do not manually transfer several Worktree tasks to Local at the same time.

### Step 7: Confirm the result

The Control Center should report:

- that Local is clean;
- the integration commit;
- tests that passed or failed;
- remaining work;
- whether the source task was archived.

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
