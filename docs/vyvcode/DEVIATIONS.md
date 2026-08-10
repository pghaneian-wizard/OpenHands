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

## D-004 — memsearch defaults and live-embedding smoke

**What:** memsearch 0.4.17's resolved default embedding provider is `openai` (needs a key), while
the runbook states local ONNX is the no-key default. VyvCode therefore passes
`-p onnx -m gpahal/bge-m3-onnx-int8` plus a project-local `--milvus-uri
<project>/.memsearch/milvus.db` on every memsearch call. Override with
`VYVCODE_MEMSEARCH_PROVIDER` / `VYVCODE_MEMSEARCH_MODEL` (empty value = defer to memsearch's own
config). Additionally, B9 tests mock the memsearch subprocess layer: the live index/search path
needs the ONNX model download (hundreds of MB from HF on first use), which is left to PJ's manual
smoke.
**Why:** Upstream default changed relative to the runbook's assumption; model download is too heavy
and network-dependent for the build gates.
**Impact:** CLI verbs, flags, and JSON shapes were verified against the installed memsearch 0.4.17
(`index`/`search -j`/`expand`, fields score/source/heading/content/chunk_hash); only the embedding
inference itself is unexercised until first live use.
