---
name: interview-me
description: Clarifies what the user actually wants through a focused, one-question-at-a-time interview. Use when the user explicitly asks to be interviewed or grilled, or asks to clarify or stress-test their underlying intent. Do not activate for clear information requests or mechanical tasks.
license: MIT
metadata:
  source: https://github.com/addyosmani/agent-skills
  wisp:
    conversation_workflow: true
    triggers:
      - interview me
      - grill me
      - ask me questions
      - help me clarify what I want
      - stress-test my thinking
    enabled: true
---

# Interview Me

Discover the outcome the user actually wants before proposing a solution. This
is an interactive conversation, not a questionnaire or a requirements dump.

## Start

In the first reply:

1. State a one-sentence hypothesis about the user's underlying goal.
2. Give an honest confidence percentage. Below roughly 70%, name what is still
   missing.
3. Ask exactly one focused question and include your best guess at its answer,
   with a short reason. Make it easy for the user to correct you.

Use this compact shape when helpful:

```text
HYPOTHESIS: ...
CONFIDENCE: ~N% — missing: ...
Q: ...
GUESS: ...
```

## Continue

After each answer:

- Update your hypothesis or confidence when the answer changes your read.
- Ask only the next question; do not batch a list of questions.
- Prefer questions about the target user, desired outcome, why now, measurable
  success, the binding constraint, and what should remain out of scope.
- Challenge conventional or prestige-sounding answers such as "scalable",
  "modern", or "best practice" by asking what the user would actually want if
  they did not have to justify it.
- Stay willing to be wrong. The attached guess exists to expose your
  assumptions, not to pressure the user into agreeing.

Do not use the interview to delay a request that is already unambiguous. If the
user asks to stop, stop immediately and summarize only what was learned.

## Finish

Stop asking questions once you can reasonably predict the user's reaction to
the next few questions. Restate the shared understanding in this form:

```text
Outcome: ...
User: ...
Why now: ...
Success: ...
Constraint: ...
Out of scope: ...
```

Ask for an explicit confirmation or correction. Do not treat "whatever you
think" as confirmation; offer two concrete interpretations if needed. Once the
user explicitly confirms the restatement, acknowledge completion and append
this exact hidden marker at the very end of the reply:

```html
<!-- wisp-skill-complete: interview-me -->
```

Do not write files, create plans, or begin implementation unless the user asks
for that as a separate next step.

Adapted from Addy Osmani's `agent-skills` project. MIT License, Copyright (c)
2025 Addy Osmani.
