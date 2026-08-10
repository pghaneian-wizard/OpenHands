# DEVIATIONS

Deviations from the VyvCode build runbook. Format: what / why / impact.

## D-001 — Lineage C base (runbook-anticipated)

**What:** This fork is Agent Canvas (TypeScript SPA), not the `software-agent-sdk` workspace.
`vyvcode/` is a standalone Python project at repo root depending on published
`openhands-sdk==1.41.0` + `openhands-tools==1.41.0` from PyPI; the TS app is ignored.
**Why:** Runbook §2 Lineage C instruction.
**Impact:** None on product spec. Upstream SDK behavior pinned by version instead of workspace
path; bumping the pin is the upgrade path.

## D-002 — SDK examples not available in-tree

**What:** `examples/01_standalone_sdk/` is not shipped in the PyPI wheel. Where the runbook says
"crib from example N", the installed SDK source under `site-packages/openhands/` was consulted
instead.
**Why:** Lineage C has no SDK checkout; wheel omits examples.
**Impact:** None functional; implementation verified against real SDK APIs, not example code.

## D-003 — Live model probe deferred to PJ

**What:** At build time only `MOONSHOT_API_KEY` is present; `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` are absent. B2's "probe with real latencies against live keys" is not run in
CI/build; probe logic is unit-tested with mocked responses, and the live 4-role probe is left to
the manual smoke step (§15, PJ runs it).
**Why:** Missing credentials; runbook rule 1 says a run should not stall on what only PJ can supply.
**Impact:** Probe code path exercised by tests; first live validation happens on PJ's machine via
`vyvcode --probe`.
