# HANDOFF

**Date:** 2026-08-10 (end of the hardening + UX session)
**Repo state:** two independent workstreams live here —

| Workstream | Where | State |
|---|---|---|
| 1. OpenHands → Vyvcode rebrand of the TS app | working tree + index on `main` | complete, verified, **still uncommitted** |
| 2. VyvCode Python CLI agent | branch `vyvcode-build`, dir `vyvcode/` | built, hardened, live status UI, **pushed to `origin/vyvcode-build`**, deployed, all four roles live |

The two must not be mixed in a commit. The rebrand sits as ~700 modified files plus 8 staged renames in the index; every VyvCode commit was made with `git commit --only <paths>` to step around it. Keep doing that (details in `CLAUDE.md`).

---

# Part 1 — TS app rebrand (uncommitted, on `main`)

Cloned `https://github.com/pghaneian-wizard/OpenHands.git` into `/home/pj/vyvcode` with full history (7991 commits), then rebranded OpenHands → Vyvcode.

Scope was chosen deliberately: brand text and internal identifiers renamed; external references (npm packages, API hosts, container images, PyPI package names, agent-server wire values) left untouched so the project still builds and talks to the real backend. The full contract is documented in `CLAUDE.md` — read that section before touching any remaining `openhands` string.

Casing map applied: `OpenHands`→`Vyvcode`, `openhands`→`vyvcode`, `OPENHANDS`→`VYVCODE`, `open-hands`/`all-hands`/`All Hands`→`vyvcode`/`Vyvcode`.

**703 files changed** — 695 edits, 8 path renames:

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

Node 22 required (`export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22`); the system Node 20 dies in jsdom's undici.

| Check | Result |
|---|---|
| `npm run typecheck` | clean |
| `npm run lint` | clean |
| `npm test` | 4,533 passed |
| `npm run build` | clean |
| `npm run build:lib` | clean |

## Regressions found and reverted (post-rename audit)

The masking script renamed strings that had to stay `openhands`. Each was caught by a failing test or a build error and reverted by hand:

- backslash-escaped forms inside regex literals (`/ghcr\.io\/openhands\//`, `/https:\/\/app\.all-hands\.dev\//`) — a plain-text protect pattern does not match these;
- `agent_kind: "openhands"` and `openhands_version` on the wire;
- LiteLLM provider key `openhands` and its model ids;
- PyPI package names and the `openhands.automation.app` module path;
- `~/.openhands/` and `.openhands/` runtime config dirs.

If you write another bulk rename, cover the escaped variants and re-run the suite.

## Reproducing the rename

`scripts/` holds no rename tooling — it was a one-shot masking pass. To redo it, protect the "kept as openhands" column in `CLAUDE.md` first (including backslash-escaped forms), then apply the casing map.

---

# Part 2 — VyvCode Python CLI agent (`vyvcode-build`, pushed)

27 commits on `main..vyvcode-build`. The build itself was B0–B16 (one commit per runbook phase); the rest are this session's hardening and UX work:

```
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

## What it is

`vyvcode/` — standalone uv project (Python 3.12, `openhands-sdk==1.41.0` + `openhands-tools==1.41.0` from PyPI). Pipeline: Communicator (gpt-5.6-sol, token optimizer + stop-slop) → grill → MasterPlanner (claude-fable-5) → parallel kimi-k3 coder swarm in git worktrees → reviewer loop (claude-opus-5) → report + memsearch memory. Plus `/vyvcode:autoresearch`: harness-owned overnight research loop on karpathy/autoresearch (researcher kimi-k3 edits `train.py`; strategist claude-fable-5 rewrites a delimited `program.md` block; keep only strictly-lower `val_bpb`).

Docs: `vyvcode/README.md`, `vyvcode/INSTALL.md`, `docs/vyvcode/{ARCHITECTURE,RECON,DEVIATIONS,AUTORESEARCH}.md`.

## Using it

```bash
cd <any project>
vyvcode
```

At the `vyvcode>` prompt, typing `/` opens a completion menu of every command with its one-line help. Plain text runs the fast path (one coder, no pipeline). `exit`, `quit`, `q` or Ctrl-D leaves; Ctrl-D at the `answers>` prompt aborts the current run rather than the session.

| Command | Effect |
|---|---|
| `/vyvcode:brainstorm <idea>` | full pipeline, unbounded grill + design spec |
| `/plan <task>` | full pipeline, grill capped at 4 rounds |
| `/goal <goal>` | full pipeline, grill capped at 2 rounds |
| `/vyvcode:grill <topic>` | standalone interview, no handoff |
| `/memory-recall <query>` | search project memory |
| `/vyvcode:status` / `/vyvcode:stop` | run state / graceful abort |
| `/vyvcode:autoresearch …` | overnight ML-research loop |
| `!raw <text>` | bypass the optimizer verbatim |

### What the screen shows

At the prompt, the bottom toolbar carries the project directory and this session's spend per model (`communicator gpt-5.6-sol 5.2k · coder kimi-k3 31.2k | 36.4k tok $0.36`), refreshed once a second so it ticks while you sit idle. Before anything has been spent it lists the configured model per role instead.

During any run — a pipeline or a plain fast-path message — a panel stays pinned below the scrolling agent output:

```
✧ Percolating… (2m14s · ↓ 36.4k tokens · coder kimi-k3 at max effort)
▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱  4/7 EXECUTING
  ⠹  P1 core   Burnishing…    2m14s   12.3k tok
  ✔  P2 auth   merged         1m02s    8.1k tok
  ⠹  P3 api    Levitating…      47s    3.4k tok
communicator gpt-5.6-sol 5.2k · coder kimi-k3 31.2k  |  36.4k tok $0.36
```

The activity word rotates every ~6 s out of 64 gerunds, seeded per phase so parallel coders never share one, with a shimmer band sweeping its letters. The bar's leading cell breathes, so a long stage never reads as stalled. Everything degrades to plain lines when stdout is not a terminal, which is what the tests and any piped run see.

Accounting hangs off `models.llm_for` — every model in the app is built there, so the optimizer, grill, planner, all parallel coders and the reviewer are captured without threading a counter through the stages (`vyvcode/usage.py`). Snapshots re-read each instance's live metrics, so totals climb while an agent works.

## Verification

- `cd vyvcode && uv run pytest tests -q` — **195 passed**, no network, no GPU, ~17 s.
- `vyvcode --probe` from a project directory: **all four roles OK** (communicator 1.9 s, planner 8.4 s, coder 4.7 s, reviewer 9.8 s).
- pip-audit on the installed venv: no known vulnerabilities (setuptools forced ≥83 via `[tool.uv] override-dependencies` for PYSEC-2026-3447).

## Keys and deployment (this box, absolem)

- Provider keys live in **`~/.config/vyvcode.env`** (chmod 600, sourced from `.bashrc`, same pattern as `supabase.env`). This exists because VyvCode reads `.env` from the **current directory only** — without it, running from any project other than the checkout finds no keys. The file also pins `VYVCODE_COMMUNICATOR_MODEL`/`_EFFORT` so a stale export in an old shell cannot override them.
- **When rotating a key, update both** `vyvcode/.env` and `~/.config/vyvcode.env`, and keep the key on its own line with no trailing `# comment` — an inline comment on an empty value is parsed *as* the value by python-dotenv.
- `uv tool install --force --from vyvcode vyvcode` → `~/.local/bin/vyvcode`, v0.1.0. It does **not** track the checkout — re-run after every change.
- Autoresearch: real 5-min GPU baselines still not run (a vLLM server holds ~55 GB of the 96 GB VRAM — DEVIATIONS D-005). `~/.cache/autoresearch` is already prepared (963 MB, from the Aug 9 run), so `setup` skips `prepare.py` here.

## This session's work

Two threads: fixing what was broken, then making a run legible while it happens.

**Audit.** Five parallel auditors read all 4.9k lines; ~60 findings came back, each verified against the source before any fix. 32 bugs fixed, 65 tests added (130 → 195). Highlights by class:

**Silent corruption.** The optimizer's ALL-CAPS protect pattern matched its own placeholders from the tenth span on, so a message naming ten files reached the grill, planner and coders with the literal text `V10` where the paths should be — with all four guard checks passing.

**Runs that could not start or finish.** Work before the first state transition ran outside the try (stranded IDLE run blocking every later command); one truncated `state.json` broke every command permanently (now write-then-rename, unreadable runs skipped); a phase update rewrote the whole document and silently reverted a `/vyvcode:stop` from another terminal; a recycled pid after a reboot read as a live owner (liveness now checks the boot id); two runs started in the same second shared an id; the wheel shipped without `agents/` and `skills/`; `glob`/`grep` were never registered so the grill explorer died at the first NEEDS-FACT; EOF at the `answers>` prompt killed the process; any command exception killed the REPL.

**Wrong results.** The swarm's completion gate counted untracked files, so a coder that ran its own tests — which its prompt orders it to do — failed every phase on any repo without a `.gitignore` for `__pycache__`. An empty suite counted as green, reporting a run where every phase died as a clean success. Malformed reviewer JSON crashed instead of taking the retry path. A repeated `depends_on` entry was reported as a dependency cycle. Autoresearch's `parse_summary` took the first match anywhere in the log, so researcher-added interim logging silently poisoned keep/discard decisions.

**Safety.** A `- run: \`cmd\`` written inside prose was harvested as an acceptance command and executed under `shell=True`; a plan's phase id could contain `../` and escape the run directory; explorer findings and typed answers reached the provider and disk unredacted (the explorer reads `.env` when asked about configuration); `report.md` was the only artifact written without `redact()`; an acceptance timeout killed only the shell, leaving its children running against the worktree; fix coders ran concurrently in one shared worktree, contending on `.git/index.lock`.

**Status UI.** The REPL was silent between "you pressed enter" and "the agent spoke", which on a multi-minute pipeline is indistinguishable from a hang. Added the session ledger (`usage.py`), the toolbar spend line, and `RunMonitor` (`live.py`) — activity words, progress bar, per-phase table, per-model spend. Rendering is exercised by tests through the plain-text path; the animated path was checked by hand in a pty.

## Where the code lives

| Concern | File |
|---|---|
| Config, precedence, redaction | `vyvcode/config.py` |
| Role LLMs, startup probe | `vyvcode/models.py` |
| Session token ledger | `vyvcode/usage.py` |
| Live panel, activity words, animation | `vyvcode/live.py` |
| REPL loop, prompt, completion | `vyvcode/repl.py`, `vyvcode/tui.py` |
| Command dispatch, fast path | `vyvcode/commands.py` |
| Pipeline orchestration | `vyvcode/pipeline.py` |
| Grill interview | `vyvcode/grill.py` |
| Plan generation + validation | `vyvcode/planner.py` |
| Coder swarm, worktrees, merge queue | `vyvcode/swarm.py` |
| Review-until-clean loop | `vyvcode/reviewer.py` |
| Run state machine, persistence | `vyvcode/run_state.py` |
| Autoresearch loop | `vyvcode/research.py` |
| SDK noise suppression, shared console | `vyvcode/quiet.py` |

## Open items

1. **The rebrand (Part 1) is still uncommitted on `main`.** Decide squash vs. series; commit it separately from `vyvcode-build` work.
2. **`origin` still points at `pghaneian-wizard/OpenHands.git`** — `vyvcode-build` is pushed there. Repoint or add a `vyvcode` remote when the repo gets its own home.
3. **No end-to-end pipeline run against a real project yet.** Every stage is unit- and integration-tested on scripted fakes and all four roles probe live, but no `/goal` has driven a real swarm to a merged integration branch. That is the next real exercise.
4. **Autoresearch first live run** still needs the GPU free of the vLLM server.
5. **Autoresearch design items left deliberately unfixed** (they change documented behavior rather than fix a defect — PJ's call): `setup` cuts the session branch from current HEAD instead of the default branch, so a re-run branches off the previous night's work; `_reset_to_best` runs `git clean -fd`, which deletes untracked scratch files in the checkout; re-prompts (`REASK_HYPOTHESIS`, `GUARD_REPROMPT`, `STRATEGIST_RETRY`) go to fresh context-free conversations, so they cannot see what they are being asked to re-emit; `run.log` is untracked and not ignored upstream, so it is flagged as a guard offender after every keep.
6. **tmux is not installed** — the SDK falls back to a subprocess terminal and warns (the warning is now suppressed). `sudo apt-get install tmux` gives the coders a stabler terminal.
7. TS-app items from the original handoff still stand: `@vyvcode/agent-canvas` unpublished (update-check 404s), container image still `ghcr.io/openhands/agent-canvas`, `OH_*` env vars / `--oh-*` CSS vars left alone, logo SVGs still upstream artwork, backend still upstream's.
8. `/home/pj/CLAUDE.md` subproject table does not yet list `vyvcode/`.
