# Autoresearch report — ar_20260810_220000_test

- branch: autoresearch/test (best sha beef123, exp 4)
- val_bpb: baseline 0.993100 → best 0.988000 (Δ-0.005100, -0.51%); noise spread 0.001000
- experiments: 6 run / 2 kept / 3 discarded / 1 crashed
- strategist passes: 1

## Keeps

| exp | hypothesis | val_bpb | Δ |
|---|---|---|---|
| 001 | untie embeddings | 0.991000 | -0.002100 |
| 004 | lr 0.05 warmup 2x | 0.988000 | -0.003000 |

## Drift (first → best)

- params_M: 561.2 → 561.2
- peak_vram_mb: 18000.0 → 19000.0
- mfu_%: 41.0 → 43.5

## Crashes

- exp 003: depth 12 — OOM

## Spend

- researcher tokens (SDK metrics): 5100

## Resume

- /vyvcode:autoresearch start in this checkout continues the session on autoresearch/test from exp 7
- artifacts: ar_20260810_220000_test/ (state.json, journal.md, experiments.jsonl, logs/, progress.png)
