"""/vyvcode:autoresearch — autonomous overnight ML-research mode (addendum B13-B16).

Built on karpathy/autoresearch's three-file contract: ``prepare.py`` (fixed),
``train.py`` (the only file the researcher may edit), ``program.md`` (human's
file, plus one VyvCode-managed strategy block). The harness owns the loop:
commits, training runs, summary parsing, keep/discard, results.tsv, journal.
Agents keep creative control of train.py and lose only the clipboard duty.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from vyvcode.config import VyvConfig

AR_FILES = ("prepare.py", "train.py", "program.md")
AR_CACHE = Path("~/.cache/autoresearch").expanduser()
TSV_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription"

SUMMARY_FIELDS = (
    "val_bpb", "training_seconds", "total_seconds", "peak_vram_mb",
    "mfu_percent", "total_tokens_M", "num_steps", "num_params_M", "depth",
)
_INT_FIELDS = ("num_steps", "depth")

STRATEGY_BEGIN = "<!-- vyvcode:strategy:begin (auto-managed — everything outside these markers is human-owned) -->"
STRATEGY_END = "<!-- vyvcode:strategy:end -->"

AMD_POINTER = (
    "CUDA GPU required (nvidia-smi found no device). For AMD/ROCm boxes point "
    "VYVCODE_AR_REPO at the AMD fork upstream links (andyluo7/autoresearch)."
)

SETUP_HINT = (
    "not an autoresearch checkout (need prepare.py + train.py + program.md). "
    "Run /vyvcode:autoresearch setup [--dir PATH] to clone and prepare one."
)


class ResearchError(Exception):
    pass


# ── checkout fingerprint ─────────────────────────────────────────────────────


def is_ar_checkout(path: Path) -> bool:
    return all((Path(path) / name) .is_file() for name in AR_FILES)


# ── git (vyvcode identity, argv only) ────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=vyvcode", "-c", "user.email=vyvcode@local", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise ResearchError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def _head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


# ── session state ────────────────────────────────────────────────────────────


class ResearchSession:
    """One autoresearch session: state.json, journal, experiments.jsonl, logs/."""

    def __init__(self, ar_dir: Path, tag: str = "", session_id: str | None = None):
        self.ar_dir = Path(ar_dir)
        if session_id is None:
            stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d_%H%M%S")
            session_id = f"ar_{stamp}_{tag}"
        self.session_id = session_id
        self.dir = self.ar_dir / ".vyvcode" / "research" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        if self.state_path.is_file():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {
                "session_id": session_id,
                "tag": tag,
                "branch": f"autoresearch/{tag}",
                "started": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
                "baseline": None,   # {sha, val_bpb, spread}
                "best": None,       # {sha, val_bpb, exp_id}
                "counts": {"run": 0, "kept": 0, "discarded": 0, "crashed": 0},
                "crash_streak": 0,
                "strategist_passes": 0,
                "stop": None,       # None | "graceful" | "now"
                "train_pgid": None,
                "assumed": [],
                "decisions": [],
            }
            self.save()

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def tsv_path(self) -> Path:
        return self.ar_dir / "results.tsv"

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    def reload(self) -> "ResearchSession":
        self.state = json.loads(self.state_path.read_text())
        return self

    def journal(self, text: str) -> None:
        stamp = _dt.datetime.now(_dt.UTC).strftime("%H:%M")
        with open(self.dir / "journal.md", "a", encoding="utf-8") as f:
            f.write(f"\n### {stamp}\n{text.strip()}\n")

    def journal_tail(self, n: int) -> str:
        path = self.dir / "journal.md"
        if not path.is_file():
            return ""
        entries = re.split(r"(?m)^### ", path.read_text())[1:]
        return "\n\n".join("### " + e.strip() for e in entries[-n:])

    def append_experiment(self, record: dict) -> None:
        with open(self.dir / "experiments.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def experiments(self) -> list[dict]:
        path = self.dir / "experiments.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def next_exp_id(self) -> int:
        experiments = self.experiments()
        return (max((e["exp_id"] for e in experiments), default=0)) + 1

    @classmethod
    def latest(cls, ar_dir: Path) -> "ResearchSession | None":
        root = Path(ar_dir) / ".vyvcode" / "research"
        if not root.is_dir():
            return None
        dirs = sorted(d.name for d in root.iterdir()
                      if d.is_dir() and (d / "state.json").is_file())
        if not dirs:
            return None
        return cls(ar_dir, session_id=dirs[-1])


# ── LOCK (one session per checkout, D16) ─────────────────────────────────────


def _lock_path(ar_dir: Path) -> Path:
    return Path(ar_dir) / ".vyvcode" / "research" / "LOCK"


def acquire_lock(ar_dir: Path, session_id: str, out=print) -> None:
    path = _lock_path(ar_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            holder = json.loads(path.read_text())
            pid = int(holder.get("pid", -1))
        except (json.JSONDecodeError, ValueError):
            holder, pid = {}, -1
        if pid > 0 and _pid_alive(pid):
            raise ResearchError(
                f"session {holder.get('session_id')} is active (pid {pid}); "
                "one session per checkout — use --dir for a parallel checkout"
            )
        out(f"clearing stale LOCK (pid {pid} is gone)")
        path.unlink()
    path.write_text(json.dumps({"pid": os.getpid(), "session_id": session_id}) + "\n")


def release_lock(ar_dir: Path) -> None:
    _lock_path(ar_dir).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── preflight ────────────────────────────────────────────────────────────────


def preflight(ar_dir: Path, runner=subprocess.run) -> list[str]:
    errors: list[str] = []
    try:
        proc = runner(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            errors.append(AMD_POINTER)
    except (OSError, subprocess.TimeoutExpired):
        errors.append(AMD_POINTER)
    try:
        proc = runner(["uv", "--version"], capture_output=True, text=True,
                      timeout=30, check=False)
        if proc.returncode != 0:
            errors.append("uv is required (https://docs.astral.sh/uv/)")
    except (OSError, subprocess.TimeoutExpired):
        errors.append("uv is required (https://docs.astral.sh/uv/)")
    target = Path(ar_dir)
    probe = target if target.exists() else target.parent
    try:
        if shutil.disk_usage(probe).free < 20 * 1024**3:
            errors.append(f"need ≥20 GB free on {probe}")
    except OSError:
        errors.append(f"cannot stat disk space on {probe}")
    return errors


def gpu_alive(runner=subprocess.run) -> bool:
    """Between-runs sanity: don't burn experiments into a dead device."""
    try:
        proc = runner(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip()) \
        and "ERR" not in proc.stdout


# ── summary-block parser (ground truth) ──────────────────────────────────────


def parse_summary(log_text: str) -> dict | None:
    """Nine anchored fields from run.log; no val_bpb line ⇒ crash (None)."""
    fields: dict[str, float | int] = {}
    for name in SUMMARY_FIELDS:
        match = re.search(rf"(?m)^{name}:\s+([\d.]+)\s*$", log_text)
        if match:
            value = float(match.group(1))
            fields[name] = int(value) if name in _INT_FIELDS else value
    if "val_bpb" not in fields:
        return None
    return fields


@dataclass
class TrainResult:
    status: str  # ok | crash
    reason: str = ""
    fields: dict = field(default_factory=dict)
    log_path: Path | None = None

    @property
    def val_bpb(self) -> float | None:
        return self.fields.get("val_bpb")


def run_training(
    ar_dir: Path,
    log_path: Path,
    timeout_min: float,
    env: dict | None = None,
    on_start=None,
    popen=subprocess.Popen,
) -> TrainResult:
    """uv run train.py > run.log 2>&1 in its own process group; SIGKILL the
    whole group on deadline breach — no orphaned CUDA processes."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as log_fh:
        proc = popen(
            ["uv", "run", "train.py"],
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=ar_dir,
            env={**os.environ, **env} if env else None,
        )
        if on_start is not None:
            try:
                on_start(os.getpgid(proc.pid))
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            kill_pgid(proc.pid)
            proc.wait()
            return TrainResult("crash", "timeout", log_path=log_path)
    text = log_path.read_text(errors="replace")
    fields = parse_summary(text)
    if fields is None:
        tail = "\n".join(text.splitlines()[-50:])
        return TrainResult("crash", f"no summary block; log tail:\n{tail}",
                           log_path=log_path)
    return TrainResult("ok", fields=fields, log_path=log_path)


def kill_pgid(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── results.tsv (byte-compatible with upstream) ──────────────────────────────


def _sanitize_description(text: str) -> str:
    """One tsv cell: no tabs, no newlines, no commas (upstream's rule)."""
    text = re.sub(r"[\t\n\r,]+", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def tsv_init(path: Path) -> None:
    path.write_text(TSV_HEADER + "\n")


def tsv_append(path: Path, commit: str, val_bpb: float, memory_gb: float,
               status: str, description: str) -> None:
    row = (
        f"{commit}\t{val_bpb:.6f}\t{memory_gb:.1f}\t{status}\t"
        f"{_sanitize_description(description)}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(row)


def tsv_tail(path: Path, n: int = 5) -> str:
    if not path.is_file():
        return "(no results.tsv)"
    lines = path.read_text().rstrip("\n").splitlines()
    return "\n".join(lines[:1] + lines[1:][-n:])


# ── program.md strategy block (§6.2) ─────────────────────────────────────────


def ensure_strategy_block(program_path: Path) -> bool:
    """Insert the delimited region at the end if absent. Returns True if added."""
    text = program_path.read_text(encoding="utf-8")
    if STRATEGY_BEGIN in text:
        return False
    block = (
        f"\n{STRATEGY_BEGIN}\n"
        "## Strategy (auto-managed by VyvCode strategist)\n"
        "(no strategist pass yet — first pass runs after "
        "VYVCODE_AR_STRATEGY_EVERY experiments)\n"
        f"{STRATEGY_END}\n"
    )
    program_path.write_text(text.rstrip("\n") + "\n" + block)
    return True


def replace_strategy_block(program_path: Path, new_contents: str) -> None:
    """Replace only the text between markers; reject marker tampering upstream."""
    text = program_path.read_text(encoding="utf-8")
    begin = text.index(STRATEGY_BEGIN) + len(STRATEGY_BEGIN)
    end = text.index(STRATEGY_END)
    program_path.write_text(
        text[:begin] + "\n" + new_contents.strip() + "\n" + text[end:]
    )


def read_strategy_block(program_path: Path) -> str:
    text = program_path.read_text(encoding="utf-8")
    if STRATEGY_BEGIN not in text:
        return ""
    begin = text.index(STRATEGY_BEGIN) + len(STRATEGY_BEGIN)
    return text[begin : text.index(STRATEGY_END)].strip()


# ── setup (§4.2) ─────────────────────────────────────────────────────────────


def _fresh_tag(ar_dir: Path) -> str:
    base = _dt.datetime.now(_dt.UTC).strftime("%b%d").lower()
    tag = base
    suffix = 1
    existing = _git(ar_dir, "branch", "--list", "autoresearch/*").stdout
    while f"autoresearch/{tag}" in existing:
        suffix += 1
        tag = f"{base}-{suffix}"
    return tag


def _exclude_untracked(ar_dir: Path) -> None:
    """results.tsv + .vyvcode stay untracked via .git/info/exclude (not .gitignore
    — don't dirty the tree)."""
    exclude = Path(ar_dir) / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text() if exclude.is_file() else ""
    additions = [e for e in ("results.tsv", ".vyvcode/") if e not in current]
    if additions:
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(additions) + "\n")


def setup(
    cfg: VyvConfig,
    ar_dir: Path | None = None,
    out=print,
    ask_user=None,
    runner=subprocess.run,
    train_fn=None,
    grill_fn=None,
) -> str:
    # 1. resolve dir
    cwd = cfg.project_root
    if ar_dir is not None:
        target = Path(ar_dir).expanduser()
    elif is_ar_checkout(cwd):
        target = cwd
    else:
        target = cfg.ar_dir

    if train_fn is None:
        def train_fn(log):
            return run_training(target, log, cfg.ar_run_timeout_min)

    # 2. preflight
    errors = preflight(target, runner=runner)
    if errors:
        raise ResearchError("preflight failed:\n- " + "\n- ".join(errors))

    # 1b. clone if needed
    if not is_ar_checkout(target):
        if target.exists() and any(target.iterdir()):
            raise ResearchError(
                f"{target} exists, is not an autoresearch checkout, and is not "
                "empty — refusing to clone into it"
            )
        out(f"cloning {cfg.ar_repo} → {target}")
        proc = runner(["git", "clone", cfg.ar_repo, str(target)],
                      capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ResearchError(f"clone failed: {proc.stderr.strip()}")

    # 3. env + data
    out("uv sync…")
    proc = runner(["uv", "sync"], cwd=target, capture_output=True, text=True,
                  check=False)
    if proc.returncode != 0:
        raise ResearchError(f"uv sync failed: {proc.stderr.strip()[-800:]}")
    tokenizer_ready = (AR_CACHE / "tokenizer" / "tokenizer.pkl").is_file()
    data_ready = (AR_CACHE / "data").is_dir() and any((AR_CACHE / "data").iterdir())
    if not (tokenizer_ready and data_ready):
        out("preparing data (shards + tokenizer)…")
        proc = runner(["uv", "run", "prepare.py"], cwd=target,
                      capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ResearchError(f"prepare.py failed: {proc.stderr.strip()[-800:]}")

    # 4. fresh branch from a clean tree
    dirty = _git(target, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise ResearchError(
            "refusing to start: the checkout has uncommitted tracked changes\n"
            + dirty
        )
    tag = _fresh_tag(target)
    branch = f"autoresearch/{tag}"
    _git(target, "checkout", "-b", branch)

    # 6/7. tsv untracked + strategy block committed (before baselines so the
    # baseline sha includes the delimiters)
    _exclude_untracked(target)
    if ensure_strategy_block(target / "program.md"):
        _git(target, "add", "program.md")
        _git(target, "commit", "-m", "vyvcode: insert auto-managed strategy block")

    session = ResearchSession(target, tag=tag)
    acquire_lock(target, session.session_id, out=out)
    try:
        # 5. double baseline — the spread is PJ's empirical noise floor (D13)
        sha = _head_sha(target)
        out("baseline run 1/2 (untouched train.py)…")
        first = train_fn(session.dir / "logs" / "baseline_1.log")
        out("baseline run 2/2 (noise check)…")
        second = train_fn(session.dir / "logs" / "baseline_2.log")
        for i, result in enumerate((first, second), 1):
            if result.status != "ok":
                raise ResearchError(
                    f"baseline run {i} crashed ({result.reason.splitlines()[0]}); "
                    f"see {result.log_path}"
                )
        best = min(first.val_bpb, second.val_bpb)
        spread = abs(first.val_bpb - second.val_bpb)
        out(
            f"baseline val_bpb: {first.val_bpb:.6f} / {second.val_bpb:.6f} "
            f"(spread {spread:.6f} — your noise floor for VYVCODE_AR_EPSILON; "
            f"current ε={cfg.ar_epsilon})"
        )

        tsv_init(session.tsv_path)
        mem1 = first.fields.get("peak_vram_mb", 0.0) / 1024
        mem2 = second.fields.get("peak_vram_mb", 0.0) / 1024
        tsv_append(session.tsv_path, sha, first.val_bpb, mem1, "keep", "baseline")
        tsv_append(session.tsv_path, sha, second.val_bpb, mem2, "discard",
                   "baseline repeat (noise check)")

        session.state["baseline"] = {"sha": sha, "val_bpb": best, "spread": spread}
        session.state["best"] = {"sha": sha, "val_bpb": best, "exp_id": 0}
        session.save()
        session.journal(
            f"setup complete on {branch}: baselines {first.val_bpb:.6f} / "
            f"{second.val_bpb:.6f}, spread {spread:.6f}"
        )

        # §4.3 first-setup grill (2 rounds, capped)
        if ask_user is not None:
            _setup_grill(cfg, session, ask_user, out, grill_fn=grill_fn)
    finally:
        release_lock(target)

    return (
        f"setup complete: {branch} @ {sha}, baseline {best:.6f} "
        f"(spread {spread:.6f}), session {session.session_id}. "
        "Start with /vyvcode:autoresearch start"
    )


GRILL_SEED = """\
First autoresearch setup for this checkout. Settle the session parameters.
Frontier seeds: tonight's time/experiment budget; ε (keep threshold) given the
measured baseline noise spread of {spread:.6f}; strategist cadence (currently
every {every}); custom research directions to seed the strategy block; whether
program.md's human sections need edits before launch.
"""


def _setup_grill(cfg, session, ask_user, out, grill_fn=None) -> None:
    from vyvcode.grill import run_grill
    from vyvcode.models import llm_for

    grill_fn = grill_fn or (
        lambda seed: run_grill(
            cfg, llm_for("communicator", cfg), seed, mode="goal",
            ask_user=ask_user, out=out, rounds_cap=2,
        )
    )
    seed = GRILL_SEED.format(
        spread=session.state["baseline"]["spread"], every=cfg.ar_strategy_every
    )
    try:
        result = grill_fn(seed)
    except Exception as exc:
        session.journal(f"setup grill skipped: {exc}")
        return
    decisions = _brief_items(result.brief_md, "decisions")
    assumed = _brief_items(result.brief_md, "assumed")
    session.state["decisions"] = decisions
    session.state["assumed"] = assumed
    session.save()
    session.journal(
        "setup grill: " + "; ".join(decisions + [f"assumed: {a}" for a in assumed])
    )


def _brief_items(brief_md: str, key: str) -> list[str]:
    match = re.search(rf"(?m)^{key}:.*\n((?:[ \t]+-.*\n?)*)", brief_md)
    if not match:
        return []
    return [line.strip().lstrip("- ").strip()
            for line in match.group(1).splitlines() if line.strip().startswith("-")]


# ── command dispatch (§3) ────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="/vyvcode:autoresearch", add_help=False,
                                     exit_on_error=False)
    sub = parser.add_subparsers(dest="cmd")
    p_setup = sub.add_parser("setup", add_help=False, exit_on_error=False)
    p_setup.add_argument("--dir", default=None)
    p_start = sub.add_parser("start", add_help=False, exit_on_error=False)
    p_start.add_argument("--experiments", type=int, default=None)
    p_start.add_argument("--hours", type=float, default=None)
    p_start.add_argument("--freeform", action="store_true")
    sub.add_parser("status", add_help=False, exit_on_error=False)
    p_stop = sub.add_parser("stop", add_help=False, exit_on_error=False)
    p_stop.add_argument("--now", action="store_true")
    sub.add_parser("report", add_help=False, exit_on_error=False)
    return parser


def dispatch(cfg: VyvConfig, arg: str, out=print, ask_user=None) -> str:
    try:
        args = _build_parser().parse_args(arg.split())
    except argparse.ArgumentError as exc:
        return f"autoresearch: {exc}. Subcommands: setup start status stop report"
    if args.cmd is None:
        return "autoresearch subcommands: setup [--dir PATH] | start [--experiments N] [--hours H] [--freeform] | status | stop [--now] | report"

    try:
        if args.cmd == "setup":
            ar_dir = Path(args.dir).expanduser() if args.dir else None
            return setup(cfg, ar_dir=ar_dir, out=out,
                         ask_user=ask_user or _default_ask)
        # everything below needs an existing checkout
        target = cfg.project_root
        if not is_ar_checkout(target):
            return SETUP_HINT
        if args.cmd == "start":
            return start(cfg, target, out=out,
                         max_experiments=args.experiments, max_hours=args.hours,
                         freeform=args.freeform)
        if args.cmd == "status":
            return render_status(cfg, target)
        if args.cmd == "stop":
            return request_stop(target, now=args.now)
        if args.cmd == "report":
            return regenerate_report(cfg, target, out=out)
    except ResearchError as exc:
        return f"autoresearch: {exc}"
    return "autoresearch: unknown subcommand"


def _default_ask() -> str:
    return input("answers> ")


def render_status(cfg: VyvConfig, ar_dir: Path) -> str:
    session = ResearchSession.latest(ar_dir)
    if session is None:
        return "no autoresearch session in this checkout (run setup)"
    state = session.state
    lines = [f"session {state['session_id']} on {state['branch']}"]
    counts = state["counts"]
    lines.append(
        f"experiments: {counts['run']} run / {counts['kept']} kept / "
        f"{counts['crashed']} crashed; crash streak {state['crash_streak']}"
    )
    baseline, best = state.get("baseline"), state.get("best")
    if baseline and best:
        delta = best["val_bpb"] - baseline["val_bpb"]
        pct = 100 * delta / baseline["val_bpb"] if baseline["val_bpb"] else 0
        lines.append(
            f"val_bpb: baseline {baseline['val_bpb']:.6f} → best "
            f"{best['val_bpb']:.6f} (Δ{delta:+.6f}, {pct:+.2f}%)"
        )
    budget = []
    if cfg.ar_max_experiments:
        budget.append(f"{cfg.ar_max_experiments - counts['run']} experiments left")
    if cfg.ar_max_hours:
        budget.append(f"cap {cfg.ar_max_hours}h")
    lines.append("budget: " + (", ".join(budget) or "until stopped"))
    if state.get("stop"):
        lines.append(f"stop requested: {state['stop']}")
    lines.append(tsv_tail(session.tsv_path, 5))
    return "\n".join(lines)


def request_stop(ar_dir: Path, now: bool = False) -> str:
    session = ResearchSession.latest(ar_dir)
    if session is None:
        return "no autoresearch session in this checkout"
    session.reload()
    session.state["stop"] = "now" if now else "graceful"
    session.save()
    if now and session.state.get("train_pgid"):
        try:
            os.killpg(int(session.state["train_pgid"]), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, TypeError):
            pass
        return "stop --now: killed the in-flight training run; experiment discarded"
    return "graceful stop requested: the in-flight run will finish, then report"


def gzip_log(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


# ── loop / report entry points (B14 / B15) ───────────────────────────────────


def start(cfg: VyvConfig, ar_dir: Path, out=print, max_experiments=None,
          max_hours=None, freeform: bool = False) -> str:
    raise ResearchError("loop lands in B14")


def regenerate_report(cfg: VyvConfig, ar_dir: Path, out=print) -> str:
    raise ResearchError("report lands in B15")
