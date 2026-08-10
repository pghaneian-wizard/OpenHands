# VyvCode

Multi-model CLI coding agent on the OpenHands SDK. A token-optimizing front
agent (gpt-5.6-sol) grills you into a real spec, a MasterPlanner
(claude-fable-5) cuts it into parallel work packets, a kimi-k3 coder swarm
executes them in isolated git worktrees, a reviewer (claude-opus-5) loops each
packet until clean, and everything learned lands in persistent semantic memory
(memsearch). It also ships `/vyvcode:autoresearch`, an autonomous overnight
ML-research mode for karpathy/autoresearch.

## Pipeline

```
you ─ Communicator (gpt-5.6-sol: token optimizer + stop-slop)
        │  grill rounds until the spec stops wobbling
        ▼
    MasterPlanner (claude-fable-5) ── plan gate (optional)
        │  work packets with acceptance criteria
        ▼
    Coder swarm (kimi-k3 × N, one git worktree each)
        │  per-packet
        ▼
    Reviewer (claude-opus-5) ── review-until-clean loop
        │
        ▼
    integration branch + run report + memory digest
```

Everything downstream of the Communicator runs full auto (`NeverConfirm`).
The only user gates: grill answers, and the optional plan gate
(`VYVCODE_PLAN_GATE=true`).

## Install

See [INSTALL.md](INSTALL.md). Short version:

```bash
git clone https://github.com/pghaneian-wizard/OpenHands.git vyvcode
cd vyvcode/vyvcode && uv sync
cp .env.example .env          # fill in OPENAI_API_KEY, ANTHROPIC_API_KEY, MOONSHOT_API_KEY
uv tool install --from . vyvcode
vyvcode --probe               # 4-role liveness table; fails loud with fix hints
```

## Use

Run inside the project you want built or changed:

```bash
cd /path/to/target-project
vyvcode
```

First run installs `.agents/agents/` + `.agents/skills/` into the project
(`--install-assets` does it explicitly; `--project DIR` targets another
directory; `--no-probe` skips the startup liveness check).

| Input | What happens |
|---|---|
| plain message | fast path: optimizer → one kimi-k3 coder, full auto |
| `/vyvcode:brainstorm <idea>` | full pipeline, unbounded grill + design spec |
| `/plan <task>` | full pipeline, grill capped at 4 rounds |
| `/goal <goal>` | full pipeline, grill capped at 2 rounds |
| `/vyvcode:grill <topic>` | standalone interview, no handoff |
| `/memory-recall <query>` | search project memory, answer with dates |
| `/vyvcode:status` / `/vyvcode:stop` | run state / graceful abort |
| `/vyvcode:autoresearch …` | overnight ML-research loop — see [AUTORESEARCH](../docs/vyvcode/AUTORESEARCH.md) |
| `!raw <text>` | bypass the optimizer verbatim |

`/vyvecode:` (typo) is a silent alias of `/vyvcode:` everywhere.

## Configuration

Every knob lives in `.env` (template: `.env.example`); nothing requires code
changes. Per role (`COMMUNICATOR`, `PLANNER`, `CODER`, `REVIEWER`,
`RESEARCHER`, `STRATEGIST`): `VYVCODE_<ROLE>_MODEL`, `_API_KEY_VAR`,
`_BASE_URL`, `_EFFORT`. Global: swarm width, grill caps, plan gate, memsearch
provider/model, and the `VYVCODE_AR_*` autoresearch knobs. An empty value
clears a baked-in default (e.g. drop the Moonshot base URL to use a proxy).

Run artifacts land in `.vyvcode/runs/<run_id>/`; memory in
`.memsearch/memory/YYYY-MM-DD.md`; research sessions in
`.vyvcode/research/<session_id>/`.

## Development

```bash
uv run pytest tests -q        # 130 tests, no network, no GPU
```

Docs: [`../docs/vyvcode/ARCHITECTURE.md`](../docs/vyvcode/ARCHITECTURE.md)
(design + trust boundaries), [`RECON.md`](../docs/vyvcode/RECON.md) (SDK
anchor audit), [`DEVIATIONS.md`](../docs/vyvcode/DEVIATIONS.md) (spec
deviations), [`AUTORESEARCH.md`](../docs/vyvcode/AUTORESEARCH.md) (research
mode).

This directory is a standalone uv project inside the vyvcode repository; the
TypeScript Agent Canvas app at the repo root is a separate product and is not
touched by anything here.
