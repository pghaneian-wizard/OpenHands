# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

`vyvcode` is a rebranded fork of **OpenHands Agent Canvas** (`pghaneian-wizard/OpenHands`, itself a fork of the upstream `OpenHands/agent-canvas`). It is the _frontend_ of the OpenHands stack: a React Router 7 / Vite SPA plus a set of Node launcher scripts and an Electron desktop shell. It is **not** the agent itself.

The agent lives in a separate Python service — the **agent-server**, shipped by upstream as the `openhands-agent-server` PyPI package and the `ghcr.io/openhands/agent-server` image. This repo talks to it over HTTP and never reimplements it.

`AGENTS.md` (634 lines, upstream-maintained) is the authoritative architecture document: tracking/analytics, runtime-service discovery, the live and mock-LLM E2E frameworks, API access rules, and the no-magic-strings rule. **Read it before non-trivial work.** This file deliberately does not duplicate it; it covers only what is specific to the fork.

## The second product: VyvCode (Python overlay on `vyvcode-build`)

The branch `vyvcode-build` carries an independent second product: **VyvCode**, a multi-model CLI coding agent built 2026-08-10 from PJ's build runbook (phases B0–B16, one commit per phase, `vyvcode: B<N> <slug> — summary`). It lives entirely in `vyvcode/` — a standalone uv project (Python 3.12, `openhands-sdk==1.41.0` pinned from PyPI) — plus `docs/vyvcode/` (RECON, DEVIATIONS, ARCHITECTURE, AUTORESEARCH) and vendored skills in `third_party/`. It shares nothing with the TS app except the repository. Start with `vyvcode/README.md` and `vyvcode/INSTALL.md`; design and trust boundaries are in `docs/vyvcode/ARCHITECTURE.md`.

Rules specific to working on it:

- **Always commit with `git commit --only <paths>`.** The git index intentionally carries the staged rebrand (8 renames) and the tree holds ~700 modified rebrand files. A bare `git commit`, a bare `--amend`, or `git add -A` sweeps the rebrand into your commit — this happened twice during the build and both times needed history surgery. New (untracked) files must be `git add`ed first, then still committed via `--only`.
- Backticks inside `git commit -m "…"` are shell-substituted; use `-F <file>` for any message containing them.
- Tests: `cd vyvcode && uv run pytest tests -q` (130 tests, no network, no GPU, ~15 s). `uv run` fails from the repo root — there is no pyproject there.
- Secrets: `vyvcode/.env` is gitignored and holds real keys; `vyvcode/.env.example` carries placeholders only. Journals/logs redact known secrets — preserve that invariant.
- Only `MOONSHOT_API_KEY` is live on this box; `vyvcode --probe` shows coder OK and communicator/planner/reviewer FAIL until OPENAI/ANTHROPIC keys are added to `.env`.
- Deployed globally via `uv tool install --force --from vyvcode vyvcode` → `~/.local/bin/vyvcode`. Re-run after changes; it does not track the checkout.
- `/vyvecode:` is an intentional silent alias of `/vyvcode:` — do not "fix" it.
- Autoresearch (`/vyvcode:autoresearch`) contracts live in `docs/vyvcode/AUTORESEARCH.md`. `~/.cache/autoresearch/` belongs to that tool's own ecosystem and must never be touched by cleanup. Spec deviations (deferred live GPU baselines, memsearch ONNX forcing, …) are in `docs/vyvcode/DEVIATIONS.md`.

## The rebrand contract — read this before renaming anything

The fork was rebranded OpenHands → Vyvcode by a masking script: protect external tokens, rename everything else. The distinction is not cosmetic; getting it wrong breaks the build or silently breaks the wire protocol. Two categories:

**Renamed (ours):** user-visible strings, i18n namespace (`vyvcode`, so locales generate to `public/locales/<lng>/vyvcode.json`), file and identifier names, localStorage keys (`vyvcode-backends`, `vyvcode-onboarded`, `vyvcode-telemetry-consent`, `vyvcode-agent-server-config`, …), env constants (`FREE_VYVCODE_MODELS`), CSS/theme names, the npm package `@vyvcode/agent-canvas`, and `VYVCODE_AUTOMATION_API_KEY` (injected into the sandbox by our own launcher).

**Kept as `openhands` / `all-hands` (external — never rename):**

| Thing                              | Examples                                                                                                                                            |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| npm dependencies                   | `@openhands/typescript-client`, `@openhands/extensions`, and re-exported symbols such as `isOpenHandsCloudHost`                                     |
| Wire values sent to agent-server   | `agent_kind: "openhands"`, `openhands_version`                                                                                                      |
| LiteLLM provider key and model IDs | provider `openhands`; `openhands/glm-5.2`, `openhands/claude-opus-4-5-20251101`, …                                                                  |
| PyPI packages                      | `openhands-agent-server`, `openhands-automation`, `openhands-sdk`, `openhands-tools`, `openhands-workspace`; module path `openhands.automation.app` |
| Container images and OS user       | `ghcr.io/openhands/agent-server`, `ghcr.io/openhands/agent-canvas`; `USER openhands`, `/home/openhands`                                             |
| Runtime config dirs                | `~/.openhands/`, `.openhands/` in a workspace repo, the `openhands-config` repo-name convention for GitLab/Azure DevOps microagents                 |
| Hosts and docs                     | `app.all-hands.dev`, `docs.openhands.dev`, `www.all-hands.ai`                                                                                       |
| Upstream GitHub                    | `github.com/OpenHands/*`, `OpenHands/extensions/plugins/*` actions and their inputs (`openhands-api-key`), `OPENHANDS_*` CI secrets                 |
| Legal / historical                 | `LICENSE`, `CHANGELOG.md`, the `All Hands AI` copyright in `electron-builder.config.mjs`                                                            |

The display label for the provider is mapped, not renamed: `src/utils/map-provider.ts` has `openhands: "Vyvcode"`. Same shape applies elsewhere — rename the label, keep the key.

Escaped forms are a known trap. A regex literal like `/ghcr\.io\/openhands\/agent-canvas/` or `/https:\/\/app\.all-hands\.dev\//` does not match a plain-text protect pattern; several test regexes had to be reverted by hand. If you write a bulk rename, cover the backslash-escaped variants and re-run the suite.

## Environment

**Node ≥ 22.12 is required** (`engines` in `package.json`). The system `node` on this machine is v20.20.2, on which the suite dies with `TypeError: webidl.util.markAsUncloneable is not a function` from jsdom's bundled undici — this is an environment fault, not a code fault. Select Node 22 first:

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 22
```

## Commands

```bash
npm ci                  # install
npm run dev             # full stack: agent-server + automation + ingress + Vite
npm run dev:minimal     # agent-server + Vite only (scripts/dev-safe.mjs)
npm run dev:static      # static build behind the ingress proxy
npm run dev:mock        # Vite with VITE_MOCK_API=true (MSW handlers, no backend)
npm run typecheck       # react-router typegen && tsc
npm run lint            # typecheck + eslint src + prettier --check
npm run lint:fix
npm test                # make-i18n && vitest run  (~4.5k tests, ~30 s on Node 22)
npm run test:coverage
npm run test:mutation:diff   # Stryker on the diff only; full run is `test:mutation`
npm run test:e2e:mock-llm
npm run test:e2e:live        # needs a real LLM key; see AGENTS.md "Live End-to-End Test Framework"
npm run build           # = build:app — make-i18n && react-router build
npm run build:lib       # library build consumed via the subpath exports
npm run desktop         # Electron shell against a local build
npm run check-translation-completeness
```

**Running one test.** Vitest resolves aliased imports through the generated `src/i18n/declaration.ts`, so generate i18n once per checkout before invoking `vitest` directly:

```bash
npm run make-i18n
npx vitest run __tests__/api/config-service.test.ts          # one file
npx vitest run __tests__/api -t "<test name substring>"      # one case by name
npx vitest __tests__/api/config-service.test.ts              # watch mode
npx playwright test --config=playwright.mock-llm.config.ts -g "conversation"
```

Ports come from `config/defaults.json`, the single source of truth for version pins, ports, paths and package names — agent-server `18000`, automation `18001`, ingress/proxy `8000`, Vite `3001`. Read it rather than hardcoding; `scripts/dev-*.mjs`, `docker/entrypoint.sh` and `.github/workflows/docker.yml` all consume it.

## Architecture

The SPA is a thin, opinionated client over the agent-server. Almost everything interesting is one of five pipelines.

### 1. Backend registry → client options → typed clients

Every request is scoped to an _active backend_. `src/api/backend-registry/` owns that: `types.ts` (`Backend` = id/name/host/apiKey/`kind: "local" | "cloud"`/`authMode`/`connectionRevision`), `storage.ts` (persisted under `vyvcode-backends`), `active-store.ts`, `default-backend.ts`, `health-store.ts`, `auth.ts`. `connectionRevision` bumps whenever credentials change and is part of query keys, so switching or re-authenticating a backend invalidates cached data instead of serving another host's results.

`src/api/agent-server-client-options.ts` turns the active backend into the options object every `@openhands/typescript-client` class takes; `src/api/agent-server-config.ts` resolves host, session key (`VITE_SESSION_API_KEY`, else the injected `window.__AGENT_CANVAS_SESSION_API_KEY__`), working dir (`DEFAULT_WORKING_DIR = "workspace/project"`), and the locked-cloud-host / cookie-auth modes. Cloud backends do not use these clients at all — they go through `src/api/cloud/*` and `callCloudProxy` (`src/api/cloud/proxy.ts`).

Boot order lives in `src/root.tsx`: config query → auth-missing screen → first-run onboarding → Manage Backends modal → app. A bug in backend seeding surfaces as the user being trapped on the Manage Backends screen (see AGENTS.md "Published binary auth fix").

### 2. Service layer → query/mutation hooks → components

`src/api/README.md` is the contract and is short enough to read in full. Shape: one directory per service, `feature-service.api.ts` + `feature.types.ts`, exporting a plain object of async methods. **Components never call a service directly** — wrap it in `src/hooks/query/` (~70 files, `useQuery`) or `src/hooks/mutation/` (~43 files, `useMutation`). TanStack Query holds _server_ state; the ~25 zustand stores in `src/stores/` hold _ephemeral UI and conversation_ state (`conversation-store`, `use-event-store`, `agent-store`, `status-store`, `metrics-store`, …). Do not duplicate server state into a store.

If an endpoint is missing, add it to `@openhands/typescript-client` and release that first — not a raw fetch here.

### 3. Conversation lifecycle

`src/api/agent-server-adapter.ts` (~1.3k lines) is the single hub and the file most changes touch. It assembles the start request (`buildStartConversationRequest`, plus the `…WithEncryptedSettings` variant): agent tools from settings, bundled skills, agent context, confirmation policy, security analyzer, and either Vyvcode-agent or ACP-agent settings. It also owns conversation-tag semantics (`ACP_SERVER_TAG_KEY`, the `AUTOMATION_*` tag keys, `RESERVED_CONVERSATION_TAG_KEYS`), `toAppConversation` / `toConversationPage` normalization, and runtime-services parsing.

Two agent kinds exist (`AgentKind = "openhands" | "acp"`): the Vyvcode/OpenHands SDK agent, and any ACP agent (Claude Code, Codex, Gemini) launched through the Agent-Client Protocol — see `docs/ACP_AGENTS.md`, `src/constants/acp-providers.ts`, `src/api/acp-service/`.

### 4. Event stream

`src/contexts/conversation-websocket-context.tsx` (~1.1k lines) is the live pipeline and is deliberately ordered: REST backfill of events completes _first_, then the socket subscribes with `resend_mode='since'` and an `after_timestamp` baked into the URL. Opening the socket early degrades to `resend_mode='all'` and replays the entire backlog — the `isFetching` gate in that file is load-bearing, so preserve it when refactoring. `src/hooks/use-websocket.ts` keeps options in a ref and re-connects on URL change only. Events flow into `useEventStore` and are typed by `src/types/agent-server/core/` (`events/*`, `base/*`, `type-guards.ts`). A second socket exists for the planning agent.

### 5. Automations, manifests, and the Canvas UI tool

Automations are a **separate Python backend** (`openhands-automation`, port `18001`) reached at `/api/automation/*` through the ingress — `src/api/automation-service/`. `src/manifests/` is the host-owned half of extension-authored setup experiences: a catalog entry from `@openhands/extensions` declares _what to ask_, and this repo generates the form, request, route, review screen, and analytics. Adding a field type means touching `manifest-validation.ts` / `manifest-local-validation.ts` / `manifest-actions.ts` together.

`canvas_ui_control` (`src/api/canvas-ui-client-tool.ts`, name in `src/constants/canvas-ui.ts`, Python mirror in `tools/canvas_ui_tool.py`) is a _client-defined_ tool: the agent calls it to drive the right-hand panel — switch tabs, open a file, show a preview. The SDK generates the action discriminator `ClientAction_canvas_ui_control`; keep that convention isolated in the constants file.

### Dev topology

`scripts/ingress.mjs` listens on `8000` and routes by longest prefix: `/api/automation` → `18001`, `/api` and `/sockets` → agent-server `18000`, everything else → Vite `3001`. It also augments `/server_info` with a `runtime_services` block that the frontend parses (`parseRuntimeServicesInfo`, `buildRuntimeServicesSystemSuffix`) to tell the agent what services exist. `dev-safe.mjs` is the same minus automation and ingress; `dev-with-automation.mjs` also seeds the session key into agent-server secrets as `VYVCODE_AUTOMATION_API_KEY` and derives `AUTOMATION_KV_SECRET`. `OH_AGENT_SERVER_LOCAL_PATH` points the launcher at a local `software-agent-sdk` checkout (editable installs); `OH_AGENT_SERVER_GIT_REF` / `OH_AUTOMATION_GIT_REF` pick git refs.

### Settings

Settings are **schema-driven, not hardcoded**: `SettingsService` reads `/api/settings/agent-schema` and `/api/settings/conversation-schema`, renders from `SettingsSchema` (`src/types/settings.ts`), saves with PATCH diffs, and requests `X-Expose-Secrets: encrypted` only for conversation-start payloads. Adding a backend-owned setting is usually an agent-server change plus, at most, presentation here.

### Component tiers

`src/components/` has ~580 files in three tiers, and putting a component in the wrong one is the most common structural mistake:

- `src/ui/` — primitives with no app knowledge (`card`, `context-menu`, `dropdown`, `typography`, `toggle-switch`).
- `src/components/shared/` — app-aware but feature-agnostic (modals, buttons, tooltips, brand icons).
- `src/components/features/<feature>/` — the actual features (`automations`, `backends`, `chat`, `conversation`, `files`, `settings`, `skills`, `plugins`, `mcp-page`, `onboarding`, `terminal`, `browser`, `home`, `launch`, `manifest`, …). Top-level `src/components/{conversation,conversation-events,files,browser,settings,sidebar,terminal}/` are the library-exported groupings — changes there ripple into the published subpath exports.

`src/utils/` (~100 files) is small pure helpers, one concern per file, most with a colocated test. Look there before writing a formatter, a path/git helper, or a mapper — `map-provider.ts`, `extract-model-and-provider.ts`, `git-status-mapper.ts`, `file-tree.ts`, `handle-event-for-ui.ts`, and `base-path.ts` already exist and are widely imported.

### Library build

`src/index.ts` re-exports `src/lib`, and `package.json#exports` publishes subpaths (`./browser`, `./conversation`, `./files`, `./settings`, `./sidebar`, `./terminal`, `./i18n`, …) built by `npm run build:lib`. `__tests__/library-entrypoints.test.ts` and `__tests__/package-library.test.ts` guard that surface — a moved component can break the published package without touching the app.

## Where to make a change

| Change                            | Touch                                                                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| New backend data                  | `src/api/<feature>-service/` → a hook in `src/hooks/query` or `mutation` → component                               |
| New page                          | `src/routes.ts` (flat config under `routes/root-layout.tsx`) + `src/routes/<name>.tsx`                             |
| New visible string                | `src/i18n/translation.json`, then `npm run make-i18n`, then `t(I18nKey.…)`                                         |
| New conversation/agent capability | `agent-server-adapter.ts` request builders, plus `src/types/agent-server/core/events/` if a new event type arrives |
| New UI state                      | a store in `src/stores/` — only if it is not server state                                                          |
| Shared constant                   | `src/constants/` for app values, `config/defaults.json` for ports/versions/images/packages                         |

Path alias: `#/*` → `src/*` (tsconfig `paths`, honored by Vite via `tsconfigPaths: true`). Use it instead of deep relative imports.

## Layout

- `src/api/` — all backend access. `agent-server-adapter.ts` is the hub; `no-direct-agent-server-calls.test.ts` enforces that components do not bypass it.
- `src/routes/`, `src/components/`, `src/hooks/`, `src/stores/` — the SPA.
- `src/i18n/` — `translation.json` is hand-edited; `declaration.ts` and `public/locales/**` are generated by `npm run make-i18n`. Never hand-edit the generated files. Every visible string goes through `t()` keyed by an `I18nKey` member.
- `src/mocks/` — MSW handlers shared by `dev:mock` and the unit tests.
- `scripts/` — dev launchers, ingress and static server, Docker build, icon and i18n generation.
- `electron/`, `docker/`, `helm/`, `bin/agent-canvas.mjs` — the four distribution paths.
- `__tests__/` — ~530 unit test files mirroring `src/` directory-for-directory, plus `__tests__/MSW.md` and `__tests__/router.md`. A further ~30 tests are colocated in `src/` (mostly the rule-enforcing ones, e.g. `src/api/no-direct-agent-server-calls.test.ts`). `test-utils.tsx` at the repo root provides the render wrapper (QueryClient + i18n + `NavigationProvider`) and mocks `react-router`'s `useParams`.
- `tests/e2e/` — Playwright: `mock-llm/` (deterministic, runs in CI), `live/` and `live-acp/` (real LLM), `support/` shared fixtures. Vitest excludes `tests/`, so unit and E2E never collide.
- `docs/` — `architecture.md`, `DEVELOPMENT.md`, `TESTING_MATRIX.md`, `SELF_HOSTING.md`, `ACP_AGENTS.md`.
- `specs/` — feature specs (`backend-management`, `llm-defaults`, `mcp-settings`, `workspace-upload-path`). Check here before designing a change to those areas.
- `.agents/skills/` — `release.md` (release process) and `custom-codereview-guide.md`.
- `HANDOFF.md` — the fork's rebrand log: exact file renames, the verification matrix, and what is still uncommitted.

## Rules enforced by CI

`AGENTS.md` states these in full; they fail the build, so they are worth knowing up front.

- **Agent-server calls go through `@openhands/typescript-client`.** Never raw `axios`/`fetch`/the legacy shared `openHands` axios instance for `/api/*`, `/server_info`, `/sockets`. Cloud backend routes go through `callCloudProxy`. `src/api/no-direct-agent-server-calls.test.ts` enforces both.
- **No magic strings** — user-facing copy via `t()` / `I18nKey`; non-UI identifiers in named constants; discriminated-union tags as string-literal types.
- **The `HUMAN:` section of a PR description is off-limits to agents.** Do not add, edit, move, or remove it. If the PR-description check fails on it, report the validator error and ask the user to write it.
- **Tests**: TDD, AAA structure, extend existing test files rather than adding new ones, mock the service under a hook rather than the hook itself, and keep the case count minimal.
- Dependencies are exact-pinned (no caret ranges). Change versions with npm so `package.json` and `package-lock.json` move together; targeted transitive fixes belong in `overrides`, scoped to the vulnerable consumer.

## Working with upstream

`origin` still points at `https://github.com/pghaneian-wizard/OpenHands.git`. Full history is present, so rebasing onto upstream is possible — but every upstream file touching brand strings will conflict. When merging upstream work, resolve conflicts by re-applying the rebrand contract above rather than by taking either side wholesale.

`AGENTS.md`, `README.md`, `CHANGELOG.md`, `docs/` and the GitHub workflows are upstream-maintained; edits there are extra merge surface. Prefer putting fork-specific guidance in this file.

### Large fork-specific changes

Divergence is cheapest at the edges and most expensive in the hub files. In rough order of merge pain:

- **Cheap**: new routes, new components, new stores, new `src/api/<feature>-service/` directories, new i18n keys, new constants.
- **Moderate**: new query/mutation hooks, `src/manifests/` extensions, theme and style changes.
- **Expensive**: `agent-server-adapter.ts`, `conversation-websocket-context.tsx`, `settings-service.api.ts`, `vite.config.ts`, `config/defaults.json` — upstream edits these constantly. When changing them, prefer adding a separate module the hub calls into over rewriting hub logic in place.
- **Requires a coordinated backend change**: anything that alters the wire protocol, the settings schema, or the agent's toolset. This repo cannot add an endpoint on its own — the change belongs in the agent-server and `@openhands/typescript-client` first.

Before any bulk edit, re-read the rebrand contract above: a rename that crosses into the "kept as `openhands`" column breaks the wire protocol silently rather than at build time.

## Conventions

- No magic strings: user-facing copy goes through i18n; ports, versions, image names and package names go through `config/defaults.json`.
- The app must never bind `0.0.0.0:80` if deployed to the shared host described in `/home/pj/CLAUDE.md` — a host-level Nginx owns that port.
- Husky `lint-staged` runs eslint --fix, prettier, a staged-file typecheck, and `check-translation-completeness` on commit. An incomplete translation key blocks the commit, not just CI.
- `npm run make-i18n` is a prerequisite of `test`, `lint`, and every `build` script. Run it manually before any direct `vitest`/`tsc` invocation.
