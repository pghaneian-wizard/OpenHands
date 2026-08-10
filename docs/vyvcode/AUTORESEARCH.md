# /vyvcode:autoresearch

Autonomous overnight ML-research mode on karpathy/autoresearch: the harness
owns the loop (commits, 5-minute training runs, ground-truth `val_bpb`
parsing, keep/discard, results.tsv/journal); the researcher agent (kimi-k3)
owns train.py; a strategist (claude-fable-5) rewrites one delimited block in
program.md every N experiments. `/vyvecode:` is a silent alias.

## Commands

| Command | Behavior |
|---|---|
| `setup [--dir PATH]` | clone + uv sync + prepare (skipped when `~/.cache/autoresearch` is ready) + double baseline (spread printed = your ε noise floor) + fresh `autoresearch/<tag>` branch + session init + 2-round grill |
| `start [--experiments N] [--hours H] [--freeform]` | run until budget or stop; no flags = NEVER STOP; `--freeform` = upstream style, agent runs its own loop |
| `status` | counts, baseline vs best (Δ, %), crash streak, budget, tsv tail |
| `stop [--now]` | graceful finishes the in-flight run; `--now` SIGKILLs the training process group and discards the in-flight experiment |
| `report` | regenerate report.md + progress.png for the latest session |

Requires the cwd to fingerprint as an AR checkout (prepare.py + train.py +
program.md). One session per checkout (`.vyvcode/research/LOCK`, stale pids
cleared with a notice); parallel GPUs = parallel checkouts via `--dir` (D16).

## Contracts honored exactly

- branch `autoresearch/<tag>` cut fresh from the default branch, dirty tree refused
- `uv run train.py > run.log 2>&1`, 10-minute process-group SIGKILL
- nine-field summary block parsed with anchored regexes; no `val_bpb:` line = crash
- results.tsv: tabs, 5 columns, crash rows `0.000000`/`0.0`, comma-free
  descriptions, untracked via `.git/info/exclude` — `analysis.ipynb` works unmodified
- keep only on strictly-lower (`< best − ε`, ε default 0.0 = upstream-exact; tie = discard)
- train.py is the only editable file: guard reverts anything else, re-prompts
  once, abandons on the second violation
- first run is always the untouched baseline (run twice; spread reported)

## Session artifacts

`.vyvcode/research/<session_id>/`: `state.json`, `best.json`, `journal.md`
(redacted), `experiments.jsonl` (incl. researcher token spend), `logs/*.log.gz`,
`report.md`, `progress.png`. 1 GB session-dir warning; `~/.cache/autoresearch/`
is never touched by anything.

## Live smoke (§7.3 — PJ runs it, GPU box)

```bash
cd ~ && vyvcode                      # in tmux on the CUDA machine
/vyvcode:autoresearch setup          # ~10 min: sync, prepare, 2× baseline
/vyvcode:autoresearch start --hours 1
/vyvcode:autoresearch status         # mid-run
# next morning equivalent:
/vyvcode:autoresearch stop           # graceful → report + progress.png + memory
```

Expected after 1 hour: ~10-12 tsv experiments, ≥1 strategist pass, report
numbers matching `zgrep "^val_bpb:" .vyvcode/research/<id>/logs/*.gz`.

## Hardware notes

- **absolem** (RTX PRO 6000 Blackwell, 96 GB): natural host; upstream selects
  `kernels-community/flash-attn3` for non-Hopper, torch cu128 covers Blackwell.
  Numbers won't match H100 write-ups; the fixed budget keeps the session
  self-consistent. **Caution:** a vLLM server was holding ~55 GB VRAM at build
  time — free the GPU (or accept contention) before `start`.
- **winston** (W7900/ROCm): set `VYVCODE_AR_REPO` to the AMD fork
  (andyluo7/autoresearch); everything here is fork-agnostic as long as the
  summary block prints the same fields — capture one fork log and eyeball
  `parse_summary` against it first.
- Multi-GPU one box: one checkout + one session per GPU with
  `CUDA_VISIBLE_DEVICES` (env passthrough exists on the training Popen).

Decisions D11-D16 in the addendum; deviations in DEVIATIONS.md (D-005: live
baselines deferred, GPU was occupied at build time).
