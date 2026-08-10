# Installing VyvCode

VyvCode is a standalone Python 3.12 uv project living in the `vyvcode/`
directory of the vyvcode repository. Nothing here touches the TypeScript app
at the repo root.

## Prerequisites

- **Python ≥ 3.12** (uv will fetch one if missing)
- **[uv](https://docs.astral.sh/uv/)** — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **git ≥ 2.30** — worktree-based coder swarm and research loop depend on it
- **tmux** (recommended) — the SDK's terminal tool prefers it; without it you
  get a subprocess fallback and a startup warning
- API keys for the roles you'll use (see below); only the roles you exercise
  need live keys
- **Autoresearch mode only:** an NVIDIA GPU with recent drivers
  (`nvidia-smi` must work), ~20 GB free disk, and ~1 GB for the
  `~/.cache/autoresearch` dataset. AMD/ROCm boxes: point
  `VYVCODE_AR_REPO` at the AMD fork (andyluo7/autoresearch).

## 1. Clone and sync

```bash
git clone https://github.com/pghaneian-wizard/OpenHands.git vyvcode
cd vyvcode/vyvcode
uv sync
```

`uv sync` creates `.venv/` with the pinned `openhands-sdk==1.41.0` stack.

## 2. Configure keys

```bash
cp .env.example .env
$EDITOR .env
```

| Key | Powers |
|---|---|
| `OPENAI_API_KEY` | Communicator (gpt-5.6-sol) |
| `ANTHROPIC_API_KEY` | MasterPlanner + Reviewer + Strategist (claude-fable-5 / claude-opus-5) |
| `MOONSHOT_API_KEY` | Coder swarm + Researcher (kimi-k3 via api.moonshot.ai) |

`.env` is gitignored — real keys never enter the repository. Every role's
model, key variable, base URL and effort is overridable in `.env` without
code changes; an empty value clears a baked-in default.

## 3. Install the command

```bash
uv tool install --from . vyvcode
```

This puts `vyvcode` on your PATH (typically `~/.local/bin` — ensure it is in
`$PATH`). Alternative without installing:
`uv run --project /path/to/vyvcode/vyvcode vyvcode`.

## 4. Verify

```bash
vyvcode --version   # vyvcode 0.1.0
vyvcode --probe     # per-role liveness table with latencies
```

The probe hits each configured model with a minimal request and prints
OK/FAIL per role with a fix hint. Roles without keys show FAIL and are gated
off at runtime; the rest of the app still works.

Run the test suite (no network, no GPU, ~15 s):

```bash
uv run pytest tests -q      # 130 passed
```

## 5. First run

```bash
cd /path/to/target-project
vyvcode
```

First run installs `.agents/agents/` and `.agents/skills/` into the project.
First memory write downloads the ONNX embedding model
(`gpahal/bge-m3-onnx-int8`, a few hundred MB from Hugging Face, one-time).

## Autoresearch quick start (GPU box)

```bash
cd ~ && vyvcode                      # in tmux
/vyvcode:autoresearch setup          # clone + uv sync + prepare + 2× baseline (~10 min)
/vyvcode:autoresearch start --hours 1
/vyvcode:autoresearch stop           # graceful → report + progress.png + memory
```

Full contract, hardware notes and troubleshooting:
[`../docs/vyvcode/AUTORESEARCH.md`](../docs/vyvcode/AUTORESEARCH.md).

## Upgrading

```bash
cd vyvcode/vyvcode && git pull
uv sync
uv tool install --force --from . vyvcode
```

## Uninstall

```bash
uv tool uninstall vyvcode
```

Per-project artifacts (`.vyvcode/`, `.agents/`, `.memsearch/`) live inside
each target project — delete them there if unwanted. `~/.cache/autoresearch`
belongs to autoresearch's own tooling and is safe to keep.

## Troubleshooting

- **`vyvcode: command not found`** — `~/.local/bin` not in `$PATH`, or the
  tool install was skipped; use the `uv run --project … vyvcode` form.
- **Probe FAIL on a role** — key missing/invalid in `.env`, or the provider
  rejects the model ID; the probe's hint names the env var to fix. Reviewer
  fallback: set `VYVCODE_REVIEWER_MODEL=anthropic/claude-fable-5`.
- **tmux warning at startup** — cosmetic; install tmux for a stabler agent
  terminal.
- **Autoresearch setup refuses to run** — it requires a clean checkout that
  fingerprints as autoresearch (`prepare.py` + `train.py` + `program.md`) and
  a working `nvidia-smi`; the refusal message states which check failed.
