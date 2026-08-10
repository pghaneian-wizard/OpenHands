# HANDOFF

**Date:** 2026-08-10 (updated same day, end of VyvCode build)
**Repo state:** two independent workstreams live here —

| Workstream | Where | State |
|---|---|---|
| 1. OpenHands → Vyvcode rebrand of the TS app | working tree + index on `main` | complete, verified, **still uncommitted** |
| 2. VyvCode Python CLI agent (runbook B0–B16) | branch `vyvcode-build`, dir `vyvcode/` | complete, committed, **pushed to `origin/vyvcode-build`**, deployed locally |

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
src/assets/branding/openhands-logo-white.svg            -> vyvcode-logo-white.svg
__tests__/.../model-selector-openhands.test.tsx         -> model-selector-vyvcode.test.tsx
.github/workflows/qa-changes-by-openhands.yml           -> qa-changes-by-vyvcode.yml
```

Notable non-mechanical changes:

- npm package is now `@vyvcode/agent-canvas` (root `package.json` and both root entries in `package-lock.json`).
- i18n namespace `openhands` → `vyvcode`. `src/i18n/index.ts` exports `VYVCODE_I18N_NAMESPACE`; `scripts/make-i18n-translations.cjs` now emits `public/locales/<lng>/vyvcode.json`. Self-consistent — locales are generated, not committed.
- localStorage keys renamed (`vyvcode-backends`, `vyvcode-onboarded`, `vyvcode-telemetry-consent`, `vyvcode-active-backend`, `vyvcode-agent-server-config`, `vyvcode-automations-view`, …). Any existing local state under the old keys is orphaned; fresh installs are unaffected.
- `src/utils/map-provider.ts` keeps the provider key `openhands` and maps it to the label `"Vyvcode"`.

## Verification

Run on Node v22.23.2 installed via nvm (repo requires ≥22.12; system node is 20.20.2 and cannot run the suite — jsdom/undici throws `webidl.util.markAsUncloneable is not a function`).

| Check | Result |
|---|---|
| `npm run typecheck` | clean |
| `npm run lint` | 0 errors; 1 pre-existing warning (`src/hooks/query/use-local-git-info.ts:136`, unused eslint-disable) |
| `npx vitest run` | 561 files passed, 1 skipped; 4497 tests passed, 5 skipped, 9 todo, **0 failures** |
| `npm run build` | built |

A pristine `origin/main` worktree was used as a baseline to confirm the 13 failures encountered mid-rebrand were rename-induced rather than pre-existing. All were fixed; the baseline worktree has been removed.

E2E suites (`test:e2e`, `test:e2e:mock-llm`) were **not** run — they need a live agent-server or Docker. Worth doing before any release.

## Regressions found and reverted (post-rename audit)

These were over-reach by the rename — external names that had been renamed and would have broken at runtime rather than at build time. All are fixed; listed so the same traps are recognisable if the rename is ever redone or extended:

- `openhands-automation` PyPI package and `openhands.automation.app` module (`config/defaults.json`, `docker/Dockerfile`, `scripts/check-sdk-version-sync.mjs`, `.github/workflows/mock-llm-e2e.yml`).
- `docker/Dockerfile`: `USER openhands` and `chown -R openhands:openhands` — the OS user comes from the upstream base image.
- `src/utils/utils.ts`: GitLab/Azure DevOps microagent repo suffix `openhands-config`.
- `.github/workflows/issue-duplicate-checker.yml`: the third-party action input `openhands-api-key`, plus `OPENHANDS_API_KEY` / `OPENHANDS_BOT_GITHUB_PAT_PUBLIC` secret names and the `openhands-bot` commit identity.
- `helm/agent-canvas/README.md`: the `www.all-hands.ai/enterprise` link and the "OpenHands Enterprise" product name (a real third-party product; renaming it was factually wrong).
- `electron/main.mjs`: a line-wrapped `openhands-agent-server` in a comment.
- Escaped regex literals in tests — `/ghcr\.io\/openhands\/…/`, `/https:\/\/app\.all-hands\.dev\/…/` — that the plain-text protect patterns missed.
- SDK-exported symbol `isOpenHandsCloudHost` and the `openhands_version` wire field.

## Reproducing the rename

The rename script was session-scoped and is gone. If the rename needs redoing (for example after a large upstream merge), reimplement it from the contract table in `CLAUDE.md` rather than trying to recover the script, and add the escaped-regex and line-wrap cases listed above from the start.

---

# Part 2 — VyvCode Python CLI agent (`vyvcode-build`, committed + pushed)

Built from PJ's "VyvCode Build Runbook" (B0–B12) and "Autoresearch Addendum" (B13–B16), one commit per phase plus a docs commit — 19 commits, `main..vyvcode-build`:

```
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

## Verification

- `cd vyvcode && uv run pytest tests -q` — **130 passed**, no network, no GPU, ~15 s.
- Live: kimi-k3 probe OK against api.moonshot.ai (2.5 s); autoresearch setup validated against a real clone at `~/.cache/vyvcode-ar-smoke` (branch `autoresearch/aug10` @ `69fc9ac`, uv sync, strategy block committed, tsv initialized).
- pip-audit on the installed venv: no known vulnerabilities (setuptools forced ≥83 via `[tool.uv] override-dependencies` for PYSEC-2026-3447).

## Deployment state (this box, absolem)

- `uv tool install --force --from vyvcode vyvcode` → `~/.local/bin/vyvcode`, v0.1.0. Not tracking the checkout — re-run after changes.
- `vyvcode --probe`: coder OK; communicator/planner/reviewer FAIL — only `MOONSHOT_API_KEY` exists on this box. Add `OPENAI_API_KEY` + `ANTHROPIC_API_KEY` to `vyvcode/.env` to go fully green.
- Autoresearch: real 5-min GPU baselines **not** run at build time — a vLLM server held ~55 GB of the 96 GB VRAM (DEVIATIONS D-005). `~/.cache/autoresearch` is already prepared (963 MB, from PJ's Aug 9 run), so `setup` skips `prepare.py` here. First real run: free the GPU, then the smoke block in `docs/vyvcode/AUTORESEARCH.md` §Live smoke.

## Open items

1. **The rebrand (Part 1) is still uncommitted on `main`.** Decide squash vs. series; commit it separately from `vyvcode-build` work.
2. **`origin` still points at `pghaneian-wizard/OpenHands.git`** — `vyvcode-build` is pushed there. Repoint or add a `vyvcode` remote when the repo gets its own home.
3. **VyvCode live keys**: OPENAI + ANTHROPIC missing; 3 of 4 pipeline roles gated off until added.
4. **Autoresearch first live run** is PJ's smoke (needs the GPU free of the vLLM server).
5. TS-app items from the original handoff still stand: `@vyvcode/agent-canvas` unpublished (update-check 404s), container image still `ghcr.io/openhands/agent-canvas`, `OH_*` env vars / `--oh-*` CSS vars left alone, logo SVGs still upstream artwork, backend still upstream's.
6. `/home/pj/CLAUDE.md` subproject table does not yet list `vyvcode/`.
