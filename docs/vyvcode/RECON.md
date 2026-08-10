# VyvCode B0 Recon

Date: 2026-08-10. Checkout: `/home/pj/vyvcode`, branch `vyvcode-build` (cut from `main`).

## Lineage

**Lineage C — Agent Canvas.** `package.json` name is `@vyvcode/agent-canvas` (rebranded fork of
`OpenHands/agent-canvas`), a React Router 7 / Vite SPA. No Python agent in-tree.

Consequence (per runbook §2): VyvCode is a standalone Python project at repo root (`vyvcode/`)
that depends on the **published** `openhands-sdk` + `openhands-tools` from PyPI. The TS app is
untouched. Recorded in `DEVIATIONS.md`.

Installed for recon and development:

- `openhands-sdk==1.41.0`, `openhands-tools==1.41.0` into `vyvcode/.venv` (Python 3.12.3, uv 0.12.3)
- `memsearch` available on PyPI at 0.4.17 (installed in B9)

All anchor paths below are relative to
`vyvcode/.venv/lib/python3.12/site-packages/` (abbreviated `SP/`).

## Anchor verification

| Capability | Path in this checkout | Status |
|---|---|---|
| LLM object (`model`, `api_key`, `base_url`, `reasoning_effort`, `extended_thinking_budget`, `usage_id`) | `SP/openhands/sdk/llm/llm.py` | OK — all six fields present |
| LLM registry / profiles / routing / fallback | `SP/openhands/sdk/llm/llm_registry.py`, `llm_profile_store.py`, `router/`, `fallback_strategy.py` | OK |
| Agent, Conversation, AgentContext | `SP/openhands/sdk/agent/`, `SP/openhands/sdk/conversation/`; all exported from `openhands.sdk` top level (`LLM`, `Agent`, `Conversation`, `AgentContext`, `LLMRegistry`) | OK |
| Markdown subagent definitions | `SP/openhands/sdk/subagent/{schema.py,load.py,registry.py}`; schema fields confirmed: `name, description, model, tools, skills, permission_mode, max_iteration_per_run, max_budget_per_run, condenser` | OK |
| Subagent discovery dirs | `.agents/agents/` (primary) and `.openhands/agents/` (legacy) at project and user level, per `subagent/load.py` docstring; priority: programmatic > plugin > project `.agents` > project `.openhands` | OK |
| Delegation + parallel task fan-out | `SP/openhands/tools/task/definition.py` (`TaskToolSet`), `SP/openhands/tools/task/manager.py`, `SP/openhands/tools/delegate/visualizer.py` (`DelegationVisualizer`), `register_builtins_agents` in `SP/openhands/tools/__init__.py` | OK |
| Skills | `SP/openhands/sdk/skills/`; `load_project_skills(work_dir)` searches `{work_dir}/.agents/skills/` then `.openhands/skills/` (`.agents` wins on name clash); `load_user_skills()` searches `~/.agents/skills/`, `~/.openhands/skills/` + installed `~/.openhands/skills/installed/`; cache at `~/.openhands/cache/skills` | OK — runbook's expected `.agents/skills/` path is correct |
| Confirmation policies | `SP/openhands/sdk/security/confirmation_policy.py` — `AlwaysConfirm`, `NeverConfirm`, `ConfirmRisky` all present | OK |
| Worked examples (`examples/01_standalone_sdk/`) | **Not shipped in the PyPI wheel.** Reference only: `github.com/OpenHands/software-agent-sdk` @ main. Where the runbook says "crib from example N", the installed SDK source under `SP/openhands/` is the ground truth consulted instead | MISSING in-tree (equivalent chosen) |

## Environment notes

- Node repo files untouched; Python work confined to `vyvcode/`, `third_party/`, `docs/vyvcode/`.
- API keys at recon time: `MOONSHOT_API_KEY` set; `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` absent.
  Live probe (`vyvcode --probe`) against all four roles is PJ's manual smoke step (§15); build
  gates use mocked LLMs. Recorded in `DEVIATIONS.md`.
- Working tree carried ~705 modified files from the uncommitted rebrand at branch time. VyvCode
  phase commits add only VyvCode-owned paths; rebrand files stay untouched and uncommitted.
