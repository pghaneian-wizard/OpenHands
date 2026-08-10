---
name: vyvcode-brainstorm
description: Turn an idea into a validated design spec through grill rounds, then hand a BRIEF to the MasterPlanner. Runs under /vyvcode:brainstorm.
---

# Brainstorming Ideas Into Designs

Help turn ideas into fully formed designs and specs through collaborative
dialogue, then hand the result to the MasterPlanner.

<HARD-GATE>
Do NOT write any code, scaffold any project, or take any implementation
action until the flow below completes and the BRIEF is handed to the
MasterPlanner. This applies to EVERY project regardless of perceived
simplicity.
</HARD-GATE>

## Anti-Pattern: "This Is Too Simple To Need A Design"

Every project goes through this process. A todo list, a single-function
utility, a config change — all of them. "Simple" projects are where
unexamined assumptions cause the most wasted work. The design can be short
(a few sentences for truly simple projects), but the flow still runs.

## The Flow

1. **Explore project context** — the harness supplies git log, tree, README
   head, and prior project memory; read them before asking anything.
2. **Grill rounds** — use the vyvcode-grill skill: ask the WHOLE frontier
   each round (numbered ❓ questions, each with a ➡️ recommendation), wait
   for answers, recompute the frontier. Brainstorm mode has no round cap:
   continue until the frontier is empty. Mark environment lookups
   `NEEDS-FACT:` instead of asking the user.
3. **Propose 2-3 approaches** — with trade-offs; lead with your
   recommendation and reasoning. YAGNI ruthlessly.
4. **Present the design** — sections scaled to their complexity: purpose,
   architecture, components, data flow, error handling, testing. Get
   approval after each section.
5. **Write the design spec** — save to
   `docs/vyvcode/specs/YYYY-MM-DD-<topic>-design.md`.
6. **Spec self-review** — placeholder scan (no TBD/TODO), internal
   consistency, scope check, ambiguity check. Fix inline.
7. **Hand off** — compile the settled decisions into the BRIEF (schema
   provided by the harness) and hand it to the MasterPlanner. That is the
   terminal state of this skill: you never plan the build yourself and you
   never start implementing.

## Design principles

- Break the system into units with one clear purpose each, communicating
  through well-defined interfaces, testable independently.
- In existing codebases: explore current structure first, follow existing
  patterns, include targeted improvements only where they serve the goal.
- Don't propose unrelated refactoring.
