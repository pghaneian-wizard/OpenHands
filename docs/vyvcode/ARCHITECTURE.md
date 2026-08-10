# VyvCode Architecture

VyvCode is a Python overlay (`vyvcode/`) on the published OpenHands SDK
(`openhands-sdk` + `openhands-tools` 1.41.0, exact-pinned). It never touches
upstream internals; everything is wiring.

## Pipeline

```
user ◄──► Communicator (gpt-5.6-sol, xhigh)        cli.py / repl.py / commands.py
              │  optimizer.py (protect→rewrite→verify, stop-slop fused)
              │  grill.py (frontier rounds, NEEDS-FACT explorer)
              ▼  BRIEF.md
        MasterPlanner (claude-fable-5, max)         planner.py — read-only agent,
              ▼  MasterPlan.md (validated: topo,     strict parse, one retry
                 D5 disjoint est_files, acceptance)
        Coder swarm (kimi-k3, max)                   swarm.py — worktrees, waves,
              ▼  integration branch                  completion gate, merge queue
        Reviewer (claude-opus-5, xhigh)              reviewer.py — verdict json,
              ▼  APPROVED + suite green              fix clusters, escalation
        report.py → report.md → memory.py digest
```

Orchestration state lives in `run_state.py`
(`.vyvcode/runs/<run_id>/state.json`, §13 layout). `pipeline.py` is the only
module that sequences stages; every stage is injectable, which is how the
no-network integration test drives the whole machine.

## Module map

| Module | Owns |
|---|---|
| `config.py` | the only reader of the environment; precedence env > .env > vyvcode.toml > defaults; `redact()` |
| `models.py` | role→LLM factory, LLMRegistry, keyless fallback profiles, startup probe |
| `optimizer.py` | inbound rewrite pipeline + token metrics |
| `commands.py` | dispatch table, probe gating, fast path (single coder) |
| `repl.py` / `cli.py` | prompt loop, flags (`--probe`, `--install-assets`, `--no-probe`) |
| `grill.py` | frontier protocol, explorer dispatch, BRIEF extraction |
| `planner.py` | planner invocation, MasterPlan parse/validate, regeneration |
| `swarm.py` | branch topology, scheduler, completion gate, merge queue, retry policy |
| `reviewer.py` | verdict contract, fix waves, escalation |
| `report.py` | §14 report from artifacts only |
| `memory.py` | memsearch CLI wrapper: digest write, search/expand/recall, context injection |
| `subagents.py` | one-shot SDK agent runner + packaged agent-prompt loading |
| `installer.py` | `.agents/agents` + `.agents/skills` install with stamping and backups |

Beyond the runbook's §3.1 layout, three helper modules exist: `repl.py`
(loop split out of `cli.py`), `subagents.py`, `installer.py`, plus
`pipeline.py` as the stage sequencer. Same contracts, more seams.

## Trust boundaries (§16)

1. **Acceptance commands run under a shell.** Planner-authored `- run:` lines
   and their re-runs (completion gate, post-merge, review suite) are
   `shell=True` by design — the MasterPlan is the trusted artifact that
   authorizes them. Everything else (`git`, memsearch) is argv, `shell=False`.
2. **Secrets.** Keys enter only via env/.env; `vyvcode.toml` cannot carry
   them. `redact()` (known key values + `sk-` pattern + key=value pattern)
   runs on: user input before any model call, probe/provider errors, grill
   transcripts (via input redaction), memory digests, and the goal lines in
   memory anchors. Fallback LLM profiles are persisted keyless; LiteLLM
   resolves provider keys from the environment at call time.
3. **Path confinement.** `est_files` that are absolute or contain `..` are
   rejected at plan validation; worktrees live under
   `.vyvcode/worktrees/<run_id>/` inside the project.
4. **Model claims are never trusted.** Acceptance re-runs, clean-worktree
   checks, MERGE_HEAD checks, verdict-vs-suite cross-check.

## Security sweep results (2026-08-10)

| Check | Result |
|---|---|
| `.env` gitignored; `vyvcode/.env` matches root `.env` pattern | ✅ (`git check-ignore` verified) |
| `sk-` in branch history | ✅ no real keys — every hit is a test fixture, `.env.example` placeholder, or a word-substring in vendored docs (`ta`**sk-brief**, `a`**sk-matt**) |
| Redaction coverage (logs, metrics, artifacts, memory, cross-provider prompts) | ✅ tested (`test_config`, `test_models`, `test_memory`, `test_commands::TestSecretBoundary`) |
| subprocess argv `shell=False` except acceptance lines | ✅ single `shell=True` site: `swarm._run_shell` |
| Worktree/est_files confinement | ✅ validator test |
| Probe errors verbatim minus keys | ✅ tested |
| Dirty-tree refusal | ✅ tested |
| `pip-audit` on the venv | ✅ "No known vulnerabilities found" after overriding transitive `setuptools>=83` (PYSEC-2026-3447; memsearch caps `<81`, override smoke-checked) |

## Config surface beyond §3.2

- `VYVCODE_MEMSEARCH_PROVIDER` / `VYVCODE_MEMSEARCH_MODEL` — default
  `onnx` / `gpahal/bge-m3-onnx-int8` (see DEVIATIONS D-004); empty = defer to
  memsearch's own config.
- `VYVCODE_<ROLE>_API_KEY_ENV` — explicit key-env override; otherwise inferred
  from base_url/model prefix, falling back to the §1.2 role default.
- `vyvcode --no-probe` — skip startup probe (headless/test use).

## Known limits

- Resume (`§13`) is thin: state.json + worktrees persist and `/vyvcode:stop`
  aborts cleanly, but there is no `resume <run_id>` re-entry command yet.
- Fast-path memory digests are single-line (no outcome introspection).
- Live model probe, live grill, and ONNX embedding downloads are PJ's manual
  smoke (DEVIATIONS D-003/D-004).
