# HANDOFF — `/home/pj/vyvcode`

**Date:** 2026-08-11

One clone of `pghaneian-wizard/OpenHands` carries two unrelated products. Knowing which one you are touching is the first thing to get right, because they share a working tree and can be mixed into a commit by accident.

| | Workstream | Where | State |
|---|---|---|---|
| **A** | **VyvCode** — multi-model CLI coding agent (Python) | branch `vyvcode-build`, directory `vyvcode/` | built, hardened, live status UI, 197 tests green, pushed, deployed, all four roles live |
| **B** | **Vyvcode Agent Canvas** — OpenHands → Vyvcode rebrand of the TS app | working tree + index on `main` | complete and verified, **still uncommitted** |

**The one rule:** never mix them in a commit. B sits as ~700 modified files plus 8 staged renames in the index, so a bare `git commit`, a bare `--amend`, or `git add -A` sweeps the whole rebrand into whatever you were committing. Every A commit was made with `git commit --only <paths>`. Keep doing that — details in `CLAUDE.md`.

## Where to pick up

1. **Drive one real `/goal` end to end.** Every VyvCode stage is unit- and integration-tested on scripted fakes and all four roles probe live, but no run has taken a real project from goal to merged integration branch. That is the highest-value next exercise and the only untested thing left that matters.
2. **Commit workstream B.** Decide squash vs. series, then commit it on `main`, separately from anything on `vyvcode-build`.
3. **Give the repo its own home.** `origin` still points at `pghaneian-wizard/OpenHands.git`; `vyvcode-build` is pushed there.
4. Everything else is in [Open items](#open-items).

---

# A — VyvCode Python CLI agent (`vyvcode-build`, pushed)

## What it is

VyvCode is a multi-model CLI coding agent. It lives entirely in `vyvcode/` — a standalone uv project (Python 3.12, `openhands-sdk==1.41.0` + `openhands-tools==1.41.0` pinned from PyPI) — and shares nothing with the TS app except this repository. You run it from inside whatever project you want it to work on.

One run is a relay of four models, each doing the thing it is best at:

| Role | Model | Key | Job |
|---|---|---|---|
| communicator | `gpt-5.6-sol` | `OPENAI_API_KEY` | rewrites your message into a dense brief; strips slop; guards against meaning drift |
| planner | `anthropic/claude-fable-5` | `ANTHROPIC_API_KEY` | interviews you, then emits a MasterPlan of phases with dependencies and acceptance commands |
| coder | `openai/kimi-k3` (Moonshot) | `MOONSHOT_API_KEY` | one instance per phase, each in its own git worktree, running in parallel |
| reviewer | `anthropic/claude-opus-5` | `ANTHROPIC_API_KEY` | reviews the merged result, clusters fixes, loops until clean or escalates |

`/vyvcode:autoresearch` is a second, self-contained product on the same runtime: an overnight ML-research loop against `karpathy/autoresearch`, where a researcher (kimi-k3) edits `train.py`, a strategist (claude-fable-5) rewrites a delimited block of `program.md`, and only strictly-lower `val_bpb` is kept.

Docs: `vyvcode/README.md` (tour), `vyvcode/INSTALL.md` (setup), `docs/vyvcode/ARCHITECTURE.md` (design + trust boundaries), `docs/vyvcode/AUTORESEARCH.md` (research contracts), `docs/vyvcode/DEVIATIONS.md` (where the build departs from the runbook, and why), `docs/vyvcode/RECON.md` (SDK anchors).

## Ground rules for working on it

- **Commit with `git commit --only <paths>`** — see the one rule above. New files: `git add` them, then still commit with `--only`.
- Backticks in `-m "…"` are shell-substituted. Use `git commit -F <file>` for any message containing them.
- `uv run` only works from `vyvcode/` — there is no pyproject at the repo root.
- The installed tool does **not** track the checkout. After any change: `uv tool install --force --from ./vyvcode vyvcode` (the `./` matters — a bare `vyvcode` resolves as a registry package and fails).
- `vyvcode/.env` holds real keys and is gitignored; `vyvcode/.env.example` carries placeholders only. Never copy one over the other — that is what tripped GitHub push protection during this build.
- Journals, logs and `report.md` run through `redact()`. Preserve that.
- `/vyvecode:` is an intentional silent alias of `/vyvcode:`. Do not "fix" it.

## Running it

```bash
cd <any project>
vyvcode                     # REPL
vyvcode --probe             # 1-token liveness probe per role, then exit
vyvcode --install-assets    # copy agent definitions + skills into this project
vyvcode --no-probe          # skip the probe (also disables role gating)
vyvcode --project <dir>     # target another directory
```

At the `vyvcode>` prompt, typing `/` opens a completion menu of every command with its one-line help. Plain text runs the fast path — a single coder in the project directory, no pipeline. `exit`, `quit`, `q` or Ctrl-D leaves; Ctrl-D at the `answers>` prompt aborts the current run rather than the session.

| Command | Effect |
|---|---|
| `/vyvcode:brainstorm <idea>` | full pipeline, unbounded grill + design spec |
| `/plan <task>` | full pipeline, grill capped at 4 rounds |
| `/goal <goal>` | full pipeline, grill capped at 2 rounds |
| `/vyvcode:grill <topic>` | standalone interview, no handoff |
| `/memory-recall <query>` | search project memory |
| `/vyvcode:status` | inspect run state |
| `/vyvcode:stop` | graceful abort; worktrees preserved |
| `/vyvcode:autoresearch …` | overnight ML-research loop |
| `!raw <text>` | bypass the optimizer verbatim |

A command whose roles failed the startup probe is refused with the reason, rather than failing halfway through a run. Only one pipeline run may be active at a time; a second is refused with the active run's id.

## What the screen shows

At the prompt, the bottom toolbar carries the project directory and this session's spend per model, refreshed once a second so it ticks while you sit idle:

```
communicator gpt-5.6-sol 5.2k · coder kimi-k3 31.2k  |  36.4k tok $0.36
```

Before anything has been spent it lists the configured model per role instead. During any run — pipeline or fast path — a panel stays pinned below the scrolling agent output:

```
✧ Percolating… (2m14s · ↓ 36.4k tokens · coder kimi-k3 at max effort)
▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱  4/7 EXECUTING
  ⠹  P1 core   Burnishing…    2m14s   12.3k tok
  ✔  P2 auth   merged         1m02s    8.1k tok
  ⠹  P3 api    Levitating…      47s    3.4k tok
communicator gpt-5.6-sol 5.2k · coder kimi-k3 31.2k  |  36.4k tok $0.36
```

The activity word rotates every ~6 s out of 64 gerunds, seeded per phase so parallel coders never share one, with a shimmer band sweeping its letters. The bar's leading cell breathes, so a long stage never reads as stalled. Everything degrades to plain lines when stdout is not a terminal — which is what the tests and any piped run see.

## How a run works

The lifecycle, in the order the modules run:

1. **`cli.py`** — parses flags, then calls `quiet.silence_sdk_noise()` *before* importing anything from the SDK (`LOG_LEVEL` is read at import time, so the order is load-bearing), then `config.load_config()`.
2. **`config.py`** — resolves settings with precedence `process env > <project>/.env > <project>/vyvcode.toml > defaults`, validates every numeric knob, and collects `secret_values` for redaction. Bad input fails at startup with a readable message instead of a stack trace mid-run.
3. **`models.py`** — the single factory for every LLM in the app (`llm_for(role, cfg)`), plus the startup probe. Because every model is built here, `usage.py` registers each instance for accounting without any stage having to thread a counter through.
4. **`repl.py` / `tui.py`** — prompt session, slash completion, the toolbar. Every command runs inside a catch-all: no exception, and no EOF at a nested prompt, can kill the session.
5. **`commands.py`** — `parse_line()` produces a `Parsed` (kind + arg + mode); `execute()` dispatches. Handles exit words, `!raw`, the `/vyvecode:` alias, per-command role gating, and the single-active-run refusal. Plain chat takes the fast path here.
6. **`optimizer.py`** — the communicator rewrites the message with stop-slop fusion, protecting literals behind placeholders and checking four guards before returning. Below `OPTIMIZER_MIN_TOKENS` it passes through.
7. **`pipeline.py`** — creates the `Run`, opens the `RunMonitor` for the whole run, and walks the state machine, calling `monitor.stage()` at each transition: `IDLE → GRILL → BRIEF → PLANNING → [PLAN_GATE] → EXECUTING → MERGING → REVIEWING → [FIXING] → REPORTING → DONE`. Any exception, including Ctrl-C and EOF, marks the run `ABORTED` before propagating.
8. **`grill.py`** — the interview. Unanswered facts become `NEEDS-FACT` requests handled by a coder-backed explorer with `glob`/`grep`/read access to the project; its findings are redacted before they reach any provider or disk.
9. **`planner.py`** — MasterPlanner output is parsed into phases, then validated: DAG (no cycles, repeated `depends_on` entries are not cycles), phase ids restricted to `[A-Za-z0-9_-]{1,40}` so no phase can escape the run directory, and acceptance commands harvested only from the structured ``- run: `cmd` `` list, never from prose.
10. **`swarm.py`** — the executor. One git worktree per phase, waves scheduled by dependency indegree, at most `MAX_PARALLEL_CODERS` at once. Each phase must pass its acceptance commands (in its own process group, killed as a group on timeout) and a completion gate that ignores untracked build artifacts. Merges go through a queue; a conflict dispatches an integration coder. A phase that lies about being done gets one retry with the failure quoted back, then `RETRY_EXHAUSTED`; dependents are `SKIPPED`, and the run stays alive.
11. **`reviewer.py`** — reviews the integration branch, parses verdicts defensively, clusters fixes and dispatches them serially into one shared worktree (concurrent fix coders contend on `.git/index.lock`), and loops up to `MAX_REVIEW_CYCLES` before escalating.
12. **`report.py` / `memory.py`** — the final report is rendered only from artifacts that exist on disk, redacted; the digest is written to memsearch memory for later `/memory-recall`.

Cross-cutting: **`usage.py`** (session ledger; snapshots re-read each LLM's live metrics, so totals climb while an agent works), **`live.py`** (`RunMonitor`, activity words, progress bar, per-phase table), **`quiet.py`** (SDK noise suppression + the one shared rich console), **`assets.py` / `installer.py`** (agent definitions and vendored skills, resolved for both wheel and checkout layouts), **`subagents.py`**, **`research.py`** (the autoresearch loop, 1.4k lines — the largest module).

On disk, per project:

```
.vyvcode/runs/<run_id>/state.json     run state, phases, review cycle, owner pid + boot id
.vyvcode/runs/<run_id>/phases/        per-phase reports
.vyvcode/runs/<run_id>/review/        review artifacts
.memsearch/memory/                    digests for /memory-recall
```

## Configuration

Everything is a `VYVCODE_*` env var, with a `vyvcode.toml` equivalent for the non-secret ones (provider API keys are env/`.env` only, never toml). The knobs that change behavior most: `MAX_PARALLEL_CODERS` (4), `MAX_REVIEW_CYCLES` (5), `GRILL_ROUNDS_PLAN` (4), `GRILL_ROUNDS_GOAL` (2), `CODER_MAX_ITER` (200), `CODER_MAX_BUDGET` (unset), `PLAN_GATE` (off), `OPTIMIZER_MIN_TOKENS` (60), `DEFAULT_MODE` (`fast`). Per role, `VYVCODE_<ROLE>_MODEL` / `_EFFORT` / `_BASE_URL` override the table above.

## Verification

- `cd vyvcode && uv run pytest tests -q` — **197 passed**, no network, no GPU, ~17 s. 3.7k lines of tests against 5.8k lines of source.
- `vyvcode --probe` from a project directory — **all four roles OK** on this box (communicator 1.6 s, planner 8.7 s, coder 4.4 s, reviewer 10.2 s on the last run).
- pip-audit on the installed venv: no known vulnerabilities (setuptools forced ≥83 via `[tool.uv] override-dependencies` for PYSEC-2026-3447).

## Keys and deployment (this box, absolem)

- Provider keys live in **`~/.config/vyvcode.env`** (chmod 600, sourced from `.bashrc`, same pattern as `supabase.env`). This exists because VyvCode reads `.env` from the **current directory only** — without it, running from any project other than the checkout finds no keys. The file also pins `VYVCODE_COMMUNICATOR_MODEL`/`_EFFORT` so a stale export in an old shell cannot override them. A non-interactive shell does not source `.bashrc`, so scripts and CI-style invocations must source it themselves.
- **When rotating a key, update both** `vyvcode/.env` and `~/.config/vyvcode.env`, and keep the value on its own line with no trailing `# comment` — python-dotenv parses an inline comment on an empty value *as* the value.
- Deploy: `uv tool install --force --from ./vyvcode vyvcode` → `~/.local/bin/vyvcode`, v0.1.0.
- Autoresearch: `~/.cache/autoresearch` is already prepared (963 MB, from the Aug 9 run), so `setup` skips `prepare.py` here. That directory belongs to autoresearch's own ecosystem — never touch it during cleanup.

## Invariants worth not breaking

Each of these looks like a candidate for simplification and is not. All were bugs, found by audit or in use, and are now covered by tests.

| Invariant | What breaks without it |
|---|---|
| `silence_sdk_noise()` runs before any SDK import | `LOG_LEVEL` is read at import time; the REPL goes verbose again |
| The optimizer's placeholder regex excludes its own `⟦…⟧` markers | From the tenth protected span on, the optimizer corrupts its own output — with all four guards still passing |
| `state.json` is written to a temp file and `os.replace`d | A truncated write bricks every later command in that project |
| A phase update re-reads disk state, and `ABORTED` from another process wins | A `/vyvcode:stop` in another terminal is silently reverted |
| Owner liveness checks the boot id, not just the pid | A recycled pid after a reboot reads as a live owner, blocking every run |
| The completion gate passes `--untracked-files=no` | A coder that runs its own tests (its prompt orders it to) fails every phase on any repo without `__pycache__` ignored |
| `suite_green` requires a non-empty suite | An empty suite counts as green; a run where every phase died reports clean success |
| Acceptance commands come only from the structured `- run:` list | A command written inside prose gets executed under `shell=True` |
| Phase ids are sanitized before use as paths | `../` in a plan escapes the run directory |
| Acceptance runs in its own process group, killed as a group | A backgrounded child outlives the timeout and keeps writing to the worktree |
| Fix coders run serially in one worktree | Concurrent fixers contend on `.git/index.lock` |
| Explorer findings, typed answers and `report.md` go through `redact()` | The explorer reads `.env` when asked about configuration; the contents reach the provider and disk |
| Whoever opens a `RunMonitor` closes it | Nested monitors either double-close or leak the live panel |
| No panel style goes below `grey70` (`grey50` for the bar's tail) | `dim` and `bright_black` render near-invisible on most terminal themes |
| Run ids are made unique against the runs directory | Two runs started in the same second share an id |

## How it got here

30 commits on `main..vyvcode-build`. B0–B16 was the original build from PJ's runbook (one commit per phase, 2026-08-10); the rest is the hardening and UX session that followed.

The hardening pass was an audit: five parallel auditors read all 4.9k lines of source as it stood, ~60 findings came back, each verified against the source before any fix. 32 bugs fixed, 65 tests added (130 → 195) — the classes were silent corruption (the optimizer placeholder bug), runs that could not start or finish (stranded IDLE runs, truncated state, cross-terminal abort loss, pid recycling, id collisions, missing wheel assets, unregistered `glob`/`grep` killing the explorer, EOF and exceptions killing the REPL), wrong results (untracked-file gate, empty suite green, malformed reviewer JSON, repeated `depends_on` read as a cycle, autoresearch `parse_summary` matching researcher-added logging), and safety (prose-harvested commands, path escape, unredacted findings and report, orphaned acceptance children, `.git/index.lock` contention). The invariants table above is that audit's residue.

The UX pass answered a plainer problem: the REPL was silent between "you pressed enter" and "the agent spoke", which on a multi-minute pipeline is indistinguishable from a hang. That produced `usage.py`, the toolbar spend line, and `live.py`.

```
bba948072 vyvcode: readable panel palette — nothing below grey70
11c75b240 vyvcode: rewrite HANDOFF Part 2 for a fresh reader
ae4a9511a vyvcode: refresh HANDOFF — status UI, 195 tests, module map
d6c0adfc1 vyvcode: activity words, animated progress, per-model token usage
df601451e vyvcode: fresh HANDOFF — hardening pass, live keys, live swarm panel, open items
96268c155 vyvcode: live swarm panel + 14 more audit fixes
67d3e8ec9 vyvcode: audit pass — 18 bugs fixed across optimizer, run state, planner, reviewer, swarm, grill
c836e1f25 vyvcode: survive answers> EOF and command exceptions — REPL never dies mid-session
d9dacd447 vyvcode: register glob/grep tools — grill explorer crashed at first NEEDS-FACT
f4e32c2a3 vyvcode: crash-safe run state — dead-pid runs auto-abort, pipeline aborts on crash
4f8fd932f vyvcode: repl-ux + packaging — quiet output, exit words, slash completion, wheel assets, dotenv hardening
10698d313 vyvcode: docs — CLAUDE.md overlay section + HANDOFF rewrite
e6a4ce031 vyvcode: docs — README rewrite + INSTALL guide
bf59e8cda vyvcode: B16 research-hardening — e2e suite, guard smuggle test, docs, D-005
63268af51 vyvcode: B15 strategist, report, progress plot, memory, resume
df2e8ba4f vyvcode: B14 autoresearch experiment loop
7d9faae0c vyvcode: B13 autoresearch config, agent defs, alias, setup flow
e1c5c3048 vyvcode: B12 security sweep, ARCHITECTURE.md, README
91ea77c4a vyvcode: B11 test matrix — no-network integration pipeline + REPL smoke
853375682 vyvcode: B10 final report — artifact-grounded rendering
4c00bbe0e vyvcode: B9 memory — memsearch write/recall/context wiring
4fab9d0dc vyvcode: B8 reviewer loop — verdicts, fix clusters, escalation
c9fff8c1c vyvcode: B7 coder swarm — worktrees, wave scheduling, merge queue
cfbc5f43c vyvcode: B6 grill engine, MasterPlanner, plan validator, pipeline glue
574fc8b84 vyvcode: B5 vendored skills, agent definitions, asset installer
83fc37e0f vyvcode: B4 REPL, dispatch, run state, fast path
5a5effc2c vyvcode: B3 optimizer — inbound token optimizer with stop-slop fusion
ff4a53d84 vyvcode: B2 model layer — role LLM factory, registry, startup probe
1ce1ebf44 vyvcode: B1 scaffold — package, config loader, .env.example
64448c476 vyvcode: B0 recon — Lineage C confirmed, SDK anchors verified
```

---

# B — TS app rebrand (uncommitted, on `main`)

## What was done

`https://github.com/pghaneian-wizard/OpenHands.git` cloned into `/home/pj/vyvcode` with full history (7,991 commits), then rebranded OpenHands → Vyvcode by a masking script: protect external tokens, rename everything else.

The scope was chosen deliberately. Brand text and internal identifiers are renamed; external references — npm packages, API hosts, container images, PyPI package names, agent-server wire values — are left untouched, so the project still builds and still talks to the real backend. **The full contract is in `CLAUDE.md`; read it before touching any remaining `openhands` string.** A rename that crosses into the "kept as openhands" column breaks the wire protocol silently rather than at build time.

Casing map: `OpenHands`→`Vyvcode`, `openhands`→`vyvcode`, `OPENHANDS`→`VYVCODE`, `open-hands`/`all-hands`/`All Hands`→`vyvcode`/`Vyvcode`.

**703 files changed** — 695 edits and 8 path renames:

```
src/api/open-hands.types.ts                             -> src/api/vyvcode.types.ts
src/types/agent-server/core/openhands-event.ts          -> vyvcode-event.ts
src/components/shared/buttons/openhands-logo-button.tsx -> vyvcode-logo-button.tsx
src/hooks/query/use-openhands-verified-models.ts        -> use-vyvcode-verified-models.ts
src/assets/branding/openhands-logo.svg                  -> vyvcode-logo.svg
src/assets/branding/openhands-logo-spark.svg            -> vyvcode-logo-spark.svg
.github/workflows/qa-changes-by-openhands.yml           -> qa-changes-by-vyvcode.yml
public/locales/*/openhands.json                         -> vyvcode.json (generated)
```

## Verification

Node 22 is required (`export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22`); the system Node 20 dies inside jsdom's bundled undici, which is an environment fault and not a code fault.

| Check | Result |
|---|---|
| `npm run typecheck` | clean |
| `npm run lint` | clean |
| `npm test` | 4,533 passed |
| `npm run build` | clean |
| `npm run build:lib` | clean |

## Regressions found and reverted

The masking script renamed several strings that had to stay `openhands`. Each was caught by a failing test or a build error and reverted by hand:

- backslash-escaped forms inside regex literals (`/ghcr\.io\/openhands\//`, `/https:\/\/app\.all-hands\.dev\//`) — a plain-text protect pattern does not match these;
- `agent_kind: "openhands"` and `openhands_version` on the wire;
- the LiteLLM provider key `openhands` and its model ids;
- PyPI package names and the `openhands.automation.app` module path;
- the `~/.openhands/` and `.openhands/` runtime config directories.

If you write another bulk rename, cover the escaped variants and re-run the suite.

## Reproducing it

`scripts/` holds no rename tooling — it was a one-shot masking pass. To redo it, protect the "kept as openhands" column in `CLAUDE.md` first (including backslash-escaped forms), then apply the casing map.

---

# Open items

**Workstream A (VyvCode)**

1. **No end-to-end pipeline run against a real project yet** — see [Where to pick up](#where-to-pick-up).
2. **Autoresearch's first live run** needs the GPU free of the vLLM server (~55 GB of 96 GB held). Real 5-minute baselines are still deferred — DEVIATIONS D-005.
3. **Autoresearch design items left deliberately unfixed.** They change documented behavior rather than fix a defect, so they are PJ's call: `setup` cuts the session branch from current HEAD instead of the default branch, so a re-run branches off the previous night's work; `_reset_to_best` runs `git clean -fd`, deleting untracked scratch files in the checkout; re-prompts (`REASK_HYPOTHESIS`, `GUARD_REPROMPT`, `STRATEGIST_RETRY`) go to fresh context-free conversations, so they cannot see what they are being asked to re-emit; `run.log` is untracked and not ignored upstream, so it is flagged as a guard offender after every keep.
4. **tmux is not installed** — the SDK falls back to a subprocess terminal (the warning is now suppressed). `sudo apt-get install tmux` gives the coders a stabler terminal.
5. `/home/pj/CLAUDE.md`'s subproject table does not list `vyvcode/` yet.

**Workstream B (TS app)**

6. **The rebrand is uncommitted** — decide squash vs. series and commit it on `main`.
7. **`origin` still points at `pghaneian-wizard/OpenHands.git`.** Repoint, or add a `vyvcode` remote, when the repo gets its own home.
8. `@vyvcode/agent-canvas` is unpublished, so the update check 404s; the container image is still `ghcr.io/openhands/agent-canvas`; `OH_*` env vars and `--oh-*` CSS vars were left alone; the logo SVGs are still upstream artwork; the backend is still upstream's.
