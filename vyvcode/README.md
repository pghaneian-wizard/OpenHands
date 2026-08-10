# VyvCode

Multi-model CLI coding agent on the OpenHands SDK: a token-optimizing front
agent (gpt-5.6-sol), grill-driven planning (claude-fable-5), a parallel
kimi-k3 coder swarm in git worktrees, a review-until-clean loop
(claude-opus-5), and persistent semantic memory (memsearch).

## Setup

```bash
cd vyvcode
uv sync
cp .env.example .env        # fill in OPENAI_API_KEY, ANTHROPIC_API_KEY, MOONSHOT_API_KEY
uv run vyvcode --probe      # 4-role liveness table; fails loud with fix hints
```

## Use

Run inside the project you want built or changed:

```bash
cd /path/to/target-project
uv run --project /path/to/vyvcode/vyvcode vyvcode
```

First run installs `.agents/agents/` + `.agents/skills/` into the project
(`--install-assets` does it explicitly).

| Input | What happens |
|---|---|
| plain message | fast path: optimizer → one kimi-k3 coder, full auto |
| `/vyvcode:brainstorm <idea>` | full pipeline, unbounded grill + design spec |
| `/plan <task>` | full pipeline, grill capped at 4 rounds |
| `/goal <goal>` | full pipeline, grill capped at 2 rounds |
| `/vyvcode:grill <topic>` | standalone interview, no handoff |
| `/memory-recall <query>` | search project memory, answer with dates |
| `/vyvcode:status` / `/vyvcode:stop` | run state / graceful abort |
| `!raw <text>` | bypass the optimizer verbatim |

Everything downstream of the Communicator runs full auto (`NeverConfirm`).
The only user gates: grill answers, and the optional plan gate
(`VYVCODE_PLAN_GATE=true`).

Knobs live in `.env.example`; every role's model/effort/base_url is
overridable without touching code. Run artifacts land in
`.vyvcode/runs/<run_id>/`; memory in `.memsearch/memory/YYYY-MM-DD.md`.

## Development

```bash
uv run pytest tests -q        # 89 tests, no network, <15s
```

Docs: `../docs/vyvcode/ARCHITECTURE.md` (design + trust boundaries),
`RECON.md` (SDK anchor audit), `DEVIATIONS.md` (spec deviations).
