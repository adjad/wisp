---
name: idea-refine
description: Refines a rough idea through divergent exploration, candid evaluation, and convergence on a focused direction. Use when the user asks to ideate, refine an idea, explore alternatives, or stress-test a plan before committing.
license: MIT
metadata:
  source: https://github.com/addyosmani/agent-skills
  wisp:
    conversation_workflow: true
    triggers:
      - refine this idea
      - help me refine
      - ideate on
      - brainstorm this idea
      - explore this idea
      - stress-test my plan
    enabled: true
---

# Idea Refine

Turn a rough idea into a focused concept worth acting on. Explore enough real
alternatives to avoid locking onto the first answer, then help the user choose.
Be direct and constructive rather than automatically supportive.

## Phase 1: Understand and expand

1. Restate the idea as a crisp "How might we..." problem focused on a person
   and an outcome.
2. Identify what is missing: usually the target user, definition of success,
   constraints, prior attempts, or why now.
3. Ask focused questions before generating directions. Prefer one question per
   turn when its answer will materially change the next question.
4. Once the user and success condition are clear, generate 5-8 meaningfully
   different directions. Useful lenses include inversion, radical
   simplification, removing a constraint, changing the audience, combining an
   adjacent idea, and imagining a 10x version.

Do not produce twenty shallow variants or disguise minor wording changes as
different ideas.

## Phase 2: Evaluate and converge

After the user reacts, cluster what resonated into 2-3 distinct directions.
Evaluate each against:

- **User value:** who benefits and how strongly?
- **Feasibility:** what is costly, uncertain, or technically hard?
- **Differentiation:** why would someone choose this over what they do now?

For every serious direction, state the key unvalidated assumption, what could
kill it, and what is deliberately being ignored for now. If an idea is weak,
say why plainly and propose the smallest useful correction.

## Phase 3: Sharpen

When the user chooses a direction, produce a concise one-pager:

```markdown
# [Idea name]

## Problem statement
## Recommended direction
## Key assumptions to validate
## MVP scope
## Not doing (and why)
## Open questions
```

The "Not doing" section is required; it makes the tradeoffs real. Do not jump
to this artifact before the user has reacted to alternatives, and do not begin
implementation without a separate request.

Ask the user to confirm or correct the direction. Once they explicitly confirm
the final one-pager, append this exact hidden marker at the end of the reply:

```html
<!-- wisp-skill-complete: idea-refine -->
```

Offer to save the one-pager only after confirmation. Writing a file still
requires the user's request and Wisp's normal file-action approval.

Adapted from Addy Osmani's `agent-skills` project. MIT License, Copyright (c)
2025 Addy Osmani.
