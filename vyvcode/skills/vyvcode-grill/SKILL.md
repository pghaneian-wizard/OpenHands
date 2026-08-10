---
name: vyvcode-grill
description: Frontier-based design-tree interview. Runs standalone via /vyvcode:grill and automatically at the start of every /vyvcode:brainstorm, /plan, and /goal pipeline — never skipped.
---

Interview the user relentlessly until you reach a shared understanding. Map
this as a **design tree**: every decision branches into the decisions that
hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose
prerequisites are already settled — the questions you can ask _now_ without
guessing at answers you haven't heard yet. Ask the whole frontier in one
round: number each question and give your recommended answer. Then wait for
the user's answers before the next round.

Each question is formatted like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree — settled decisions push the
frontier outward and unblock questions that depended on them. Recompute the
frontier and ask the next round. A question whose answer depends on another
question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is never the user's job. When a frontier question needs a
fact from the environment (filesystem, git history, dependencies), do not ask
the user: mark the question with a line

```
NEEDS-FACT: <exactly what to look up>
```

and the harness dispatches a throwaway read-only explorer (kimi-k3, tools:
read/grep/ls only, never_confirm) whose finding is injected into your context
before the next round. Don't block on it: a running exploration is an
unsettled prerequisite, so only the questions downstream of it wait — ask the
rest of the frontier now. The _decisions_ are the user's — put each to them
and wait. The user may answer "all recommended" to accept every ➡️ in the
round.

The session is done when the frontier is empty: every branch of the design
tree visited, nothing left silently assumed. In pipeline mode the harness may
also stop you at a round cap; any question still open at the cap is resolved
by your own ➡️ recommendation and recorded under `assumed:` in BRIEF.md —
assumptions are named, never silent.
