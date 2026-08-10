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

    secrets: tuple[str, ...] = ()  # set from cfg by setup/start; used by journal()

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

    def save(self, preserve_stop: bool = True) -> None:
        # A stop flag set by another process (request_stop) must survive our
        # own saves, or the loop clobbers it and never halts.
        if preserve_stop and not self.state.get("stop") and self.state_path.is_file():
            try:
                disk_stop = json.loads(self.state_path.read_text()).get("stop")
            except (OSError, json.JSONDecodeError):
                disk_stop = None
            if disk_stop:
                self.state["stop"] = disk_stop
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    def reload(self) -> "ResearchSession":
        self.state = json.loads(self.state_path.read_text())
        return self

    def reload_stop_only(self) -> None:
        """Pick up a cross-process stop flag without clobbering local state."""
        try:
            self.state["stop"] = json.loads(self.state_path.read_text()).get("stop")
        except (OSError, json.JSONDecodeError):
            pass

    def journal(self, text: str) -> None:
        from vyvcode.config import redact

        stamp = _dt.datetime.now(_dt.UTC).strftime("%H:%M")
        with open(self.dir / "journal.md", "a", encoding="utf-8") as f:
            f.write(f"\n### {stamp}\n{redact(text.strip(), self.secrets)}\n")

    def write_best(self) -> None:
        (self.dir / "best.json").write_text(
            json.dumps(self.state["best"], indent=2) + "\n"
        )

    def size_warning(self, limit_bytes: int = 1 << 30) -> str | None:
        if self.state.get("size_warned"):
            return None
        total = sum(f.stat().st_size for f in self.dir.rglob("*") if f.is_file())
        if total > limit_bytes:
            self.state["size_warned"] = True
            self.save()
            return f"session dir {self.dir} exceeds 1 GB ({total >> 20} MB)"
        return None

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
    session.secrets = cfg.secret_values
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
        session.write_best()
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


# ── researcher turn (§5.2) ───────────────────────────────────────────────────

REASK_HYPOTHESIS = (
    "Your last message lacked the required final line. Reply with ONLY the "
    "line: HYPOTHESIS: <one sentence stating the change and expected effect>"
)

_HYPOTHESIS_RE = re.compile(r"(?m)^\s*HYPOTHESIS:\s*(.+)$")

GUARD_REPROMPT = (
    "Harness guard: you modified files other than train.py ({paths}). Those "
    "changes were reverted. train.py is the ONLY editable file — finish this "
    "experiment by editing train.py only, then restate your HYPOTHESIS line."
)


def build_context(session: ResearchSession, ar_dir: Path,
                  crash_tail: str = "") -> str:
    parts = [
        "<program_md>\n" + (Path(ar_dir) / "program.md").read_text() + "\n</program_md>",
        "<results_tsv>\n"
        + (session.tsv_path.read_text() if session.tsv_path.is_file() else "(empty)")
        + "</results_tsv>",
    ]
    journal = session.journal_tail(3)
    if journal:
        parts.append(f"<journal_recent>\n{journal}\n</journal_recent>")
    if crash_tail:
        parts.append(f"<last_crash_log_tail>\n{crash_tail}\n</last_crash_log_tail>")
    parts.append(
        "<current_train_py>\n" + (Path(ar_dir) / "train.py").read_text()
        + "\n</current_train_py>"
    )
    parts.append(
        "Implement ONE experiment now by editing train.py in this checkout, "
        "then end with your HYPOTHESIS line."
    )
    return "\n\n".join(parts)


def make_researcher(cfg: VyvConfig, ar_dir: Path):
    """Fresh Conversation per experiment; token spend read from SDK metrics."""
    from vyvcode.models import llm_for
    from vyvcode.subagents import agent_prompt, run_agent_task

    def researcher(task: str) -> str:
        llm = llm_for("researcher", cfg)
        reply = run_agent_task(
            llm,
            system_prompt=agent_prompt("vyvcode-researcher"),
            task=task,
            workspace=ar_dir,
            tools=("terminal", "file_editor", "glob", "grep"),
            max_iterations=40,
        )
        usage = getattr(llm.metrics, "accumulated_token_usage", None)
        researcher.last_tokens = (
            (getattr(usage, "prompt_tokens", 0) or 0)
            + (getattr(usage, "completion_tokens", 0) or 0)
            if usage is not None else 0
        )
        return reply

    researcher.last_tokens = 0
    return researcher


def extract_hypothesis(reply: str) -> str | None:
    matches = _HYPOTHESIS_RE.findall(reply or "")
    return matches[-1].strip() if matches else None


# ── only-train.py guard (§5.4) ───────────────────────────────────────────────


def guard_offenders(ar_dir: Path) -> list[str]:
    """Every changed/untracked path that isn't train.py (excluded junk aside)."""
    porcelain = _git(ar_dir, "status", "--porcelain").stdout.splitlines()
    offenders = []
    for line in porcelain:
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path != "train.py":
            offenders.append(path)
    return offenders


def revert_offenders(ar_dir: Path, offenders: list[str]) -> None:
    tracked = _git(ar_dir, "ls-files", *offenders, check=False).stdout.split("\n")
    tracked = [t for t in tracked if t]
    for path in offenders:
        if path in tracked:
            _git(ar_dir, "checkout", "--", path, check=False)
        else:
            target = Path(ar_dir) / path
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)


def _reset_to_best(ar_dir: Path, best_sha: str) -> None:
    _git(ar_dir, "reset", "--hard", best_sha)
    _git(ar_dir, "clean", "-fd", check=False)  # respects .git/info/exclude


# ── the experiment loop (§5.1) ───────────────────────────────────────────────


def _device_total_mb(runner=subprocess.run) -> float | None:
    try:
        proc = runner(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def start(cfg: VyvConfig, ar_dir: Path, out=print, max_experiments=None,
          max_hours=None, freeform: bool = False, researcher_fn=None,
          strategist_fn=None, train_fn=None, gpu_check=gpu_alive,
          report_fn=None) -> str:
    if freeform:
        return run_freeform(cfg, ar_dir, out=out)
    session = ResearchSession.latest(ar_dir)
    if session is None or not session.state.get("baseline"):
        return "no prepared session (run /vyvcode:autoresearch setup first)"
    session.reload()
    session.secrets = cfg.secret_values
    session.state["stop"] = None
    session.save(preserve_stop=False)
    acquire_lock(ar_dir, session.session_id, out=out)
    try:
        run_loop(
            cfg, ar_dir, session, out=out,
            max_experiments=max_experiments, max_hours=max_hours,
            researcher_fn=researcher_fn, strategist_fn=strategist_fn,
            train_fn=train_fn, gpu_check=gpu_check,
        )
    finally:
        release_lock(ar_dir)
    closing = finish_session(cfg, ar_dir, session, out=out, report_fn=report_fn)
    return closing


def run_loop(cfg: VyvConfig, ar_dir: Path, session: ResearchSession, out=print,
             max_experiments=None, max_hours=None, researcher_fn=None,
             strategist_fn=None, train_fn=None, gpu_check=gpu_alive) -> None:
    import time as _time

    researcher_fn = researcher_fn or make_researcher(cfg, ar_dir)
    train_fn = train_fn or (
        lambda log, on_start: run_training(
            ar_dir, log, cfg.ar_run_timeout_min, on_start=on_start
        )
    )
    exp_cap = max_experiments if max_experiments is not None else cfg.ar_max_experiments
    hour_cap = max_hours if max_hours is not None else cfg.ar_max_hours
    started = _time.monotonic()

    while True:
        session.reload()
        if session.state.get("stop"):
            out(f"stop requested ({session.state['stop']})")
            break
        if exp_cap and session.state["counts"]["run"] >= exp_cap:
            out(f"experiment cap reached ({exp_cap})")
            break
        if hour_cap and (_time.monotonic() - started) >= hour_cap * 3600:
            out(f"wall-clock cap reached ({hour_cap}h)")
            break
        if not gpu_check():
            session.journal("GPU disappeared or reports ERR — loop paused")
            out("GPU gone/ERR — pausing the loop (fix the device, then start again)")
            break
        try:
            run_experiment(cfg, ar_dir, session, researcher_fn, train_fn, out)
        except ResearchError:
            raise
        except Exception as exc:  # §7.2: overnight resilience
            best = session.state["best"]
            _reset_to_best(ar_dir, best["sha"])
            session.state["counts"]["run"] += 1
            session.state["counts"]["discarded"] += 1
            session.save()
            session.journal(f"harness_error: {redact_exc(cfg, exc)}")
            session.append_experiment({
                "ts": _now_iso(), "exp_id": session.next_exp_id(),
                "decision": "discard", "reason": f"harness_error: {exc}",
            })
            out(f"harness error (experiment discarded, loop continues): {exc}")
            continue

        _maybe_strategist(cfg, session, strategist_fn, out)
        warning = session.size_warning()
        if warning:
            session.journal(warning)
            out(warning)


def redact_exc(cfg: VyvConfig, exc: Exception) -> str:
    from vyvcode.config import redact

    return redact(str(exc), cfg.secret_values)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def run_experiment(cfg: VyvConfig, ar_dir: Path, session: ResearchSession,
                   researcher_fn, train_fn, out) -> None:
    exp_id = session.next_exp_id()
    best = session.state["best"]
    crash_tail = session.state.get("last_crash_tail", "") \
        if session.state.get("crash_streak", 0) > 0 else ""

    # 1 ▸ researcher turn
    reply = researcher_fn(build_context(session, ar_dir, crash_tail))
    hypothesis = extract_hypothesis(reply)
    if hypothesis is None:
        reply = researcher_fn(REASK_HYPOTHESIS)
        hypothesis = extract_hypothesis(reply)
    if hypothesis is None:
        diff_stat = _git(ar_dir, "diff", "--stat", "HEAD", check=False).stdout.strip()
        hypothesis = f"synthesized from diff: {diff_stat.splitlines()[-1] if diff_stat else 'no diff'}"
        session.journal(f"exp {exp_id}: researcher omitted HYPOTHESIS; synthesized")

    # 2 ▸ guard: only train.py
    offenders = guard_offenders(ar_dir)
    if offenders:
        revert_offenders(ar_dir, offenders)
        session.journal(f"exp {exp_id}: guard_violation ({', '.join(offenders)})")
        researcher_fn(GUARD_REPROMPT.format(paths=", ".join(offenders)))
        offenders = guard_offenders(ar_dir)
        if offenders:
            revert_offenders(ar_dir, offenders)
            _reset_to_best(ar_dir, best["sha"])
            _record(cfg, session, exp_id, sha="-", hypothesis=hypothesis,
                    result=None, decision="discard", reason="guard violation",
                    tokens=getattr(researcher_fn, "last_tokens", 0), out=out)
            return

    if not _git(ar_dir, "diff", "--name-only", "HEAD").stdout.strip():
        _record(cfg, session, exp_id, sha="-", hypothesis=hypothesis,
                result=None, decision="discard", reason="no change made",
                tokens=getattr(researcher_fn, "last_tokens", 0), out=out)
        return

    # 3 ▸ harness commit + ground-truth run
    _git(ar_dir, "add", "train.py")
    _git(ar_dir, "commit", "-m", f"exp {exp_id:03d}: {hypothesis[:120]}")
    sha = _head_sha(ar_dir)

    def on_start(pgid: int) -> None:
        session.state["train_pgid"] = pgid
        session.save()

    log_path = Path(ar_dir) / "run.log"
    result = train_fn(log_path, on_start)
    session.state["train_pgid"] = None

    if result.log_path and Path(result.log_path).is_file():
        gzip_log(Path(result.log_path), session.dir / "logs" / f"exp_{exp_id:03d}.log.gz")

    session.reload_stop_only()
    if session.state.get("stop") == "now":
        _reset_to_best(ar_dir, best["sha"])
        session.journal(f"exp {exp_id}: aborted by stop --now; discarded in flight")
        session.append_experiment({
            "ts": _now_iso(), "exp_id": exp_id, "sha": sha,
            "hypothesis": hypothesis, "decision": "aborted",
            "reason": "stop --now",
        })
        return

    # 4/5 ▸ decide per upstream rules (§5.5)
    if result.status == "crash":
        session.state["crash_streak"] += 1
        session.state["last_crash_tail"] = "\n".join(
            (Path(result.log_path).read_text(errors="replace").splitlines()[-50:])
            if result.log_path and Path(result.log_path).is_file() else [result.reason]
        )
        _reset_to_best(ar_dir, best["sha"])
        _record(cfg, session, exp_id, sha, hypothesis, result, "crash",
                result.reason.splitlines()[0],
                tokens=getattr(researcher_fn, "last_tokens", 0), out=out)
        return

    session.state["crash_streak"] = 0
    session.state["last_crash_tail"] = ""
    epsilon = cfg.ar_epsilon
    if result.val_bpb < best["val_bpb"] - epsilon:
        session.state["best"] = {"sha": sha, "val_bpb": result.val_bpb,
                                 "exp_id": exp_id}
        session.write_best()
        _vram_soft_check(cfg, session, result, exp_id)
        _record(cfg, session, exp_id, sha, hypothesis, result, "keep", "",
                tokens=getattr(researcher_fn, "last_tokens", 0), out=out)
    else:
        _reset_to_best(ar_dir, best["sha"])
        _record(cfg, session, exp_id, sha, hypothesis, result, "discard",
                "equal-or-worse",
                tokens=getattr(researcher_fn, "last_tokens", 0), out=out)


def _vram_soft_check(cfg: VyvConfig, session: ResearchSession, result: TrainResult,
                     exp_id: int) -> None:
    total = session.state.get("device_total_mb")
    if total is None:
        total = _device_total_mb()
        session.state["device_total_mb"] = total
    peak = result.fields.get("peak_vram_mb")
    if total and peak and peak > 0.95 * total:
        session.journal(
            f"exp {exp_id}: kept but peak_vram_mb {peak:.0f} is >95% of device "
            f"{total:.0f} — soft constraint, flagging for the strategist"
        )


def _record(cfg: VyvConfig, session: ResearchSession, exp_id: int, sha: str,
            hypothesis: str, result: TrainResult | None, decision: str,
            reason: str, tokens: int, out=print) -> None:
    fields = result.fields if result else {}
    val = fields.get("val_bpb", 0.0) if decision != "crash" else 0.0
    mem_gb = (
        fields.get("peak_vram_mb", 0.0) / 1024
        if result is not None and result.status == "ok" else 0.0
    )
    description = hypothesis if decision != "crash" else f"{hypothesis} ({reason})"
    tsv_append(session.tsv_path, sha, val, mem_gb, decision, description)

    counts = session.state["counts"]
    counts["run"] += 1
    key = {"keep": "kept", "discard": "discarded", "crash": "crashed"}[decision]
    counts[key] += 1
    session.save()

    session.append_experiment({
        "ts": _now_iso(), "exp_id": exp_id, "sha": sha, "hypothesis": hypothesis,
        "val_bpb": fields.get("val_bpb"), "peak_vram_mb": fields.get("peak_vram_mb"),
        "mfu": fields.get("mfu_percent"), "num_params_M": fields.get("num_params_M"),
        "decision": decision, "reason": reason, "tokens_spent": tokens,
    })
    session.journal(
        f"exp {exp_id:03d} [{decision}] {hypothesis}"
        + (f" — {reason}" if reason else "")
        + (f" — val_bpb {fields['val_bpb']:.6f}" if fields.get("val_bpb") else "")
    )

    best = session.state["best"]
    if decision == "keep":
        line = (f"exp {exp_id:03d} ▸ \"{hypothesis}\" ▸ val_bpb "
                f"{fields['val_bpb']:.6f} ▸ KEEP (new best)")
    elif decision == "discard" and fields.get("val_bpb"):
        delta = fields["val_bpb"] - best["val_bpb"]
        line = (f"exp {exp_id:03d} ▸ \"{hypothesis}\" ▸ val_bpb "
                f"{fields['val_bpb']:.6f} (best {best['val_bpb']:.6f}, "
                f"Δ{delta:+.6f}) ▸ DISCARD")
    elif decision == "crash":
        line = f"exp {exp_id:03d} ▸ \"{hypothesis}\" ▸ CRASH ({reason})"
    else:
        line = f"exp {exp_id:03d} ▸ \"{hypothesis}\" ▸ DISCARD ({reason})"
    out(line)


def _maybe_strategist(cfg: VyvConfig, session: ResearchSession, strategist_fn,
                      out) -> None:
    session.reload()
    trigger = None
    if session.state["crash_streak"] >= cfg.ar_crash_streak_limit:
        trigger = "crash_streak"
        session.state["crash_streak"] = 0
        session.save()
    elif (cfg.ar_strategy_every
          and session.state["counts"]["run"] > 0
          and session.state["counts"]["run"] % cfg.ar_strategy_every == 0):
        trigger = "cadence"
    if trigger is None:
        return
    if strategist_fn is None:
        strategist_fn = default_strategist_pass
    try:
        strategist_fn(cfg, session, trigger, out)
    except Exception as exc:
        session.journal(f"strategist pass failed ({trigger}): {redact_exc(cfg, exc)}")


# ── strategist (§6) ──────────────────────────────────────────────────────────

STRATEGIST_TASK = """\
Strategist pass (trigger: {trigger}) for autoresearch session {session_id}.
Baseline noise spread: {spread:.6f} — do not chase deltas inside it.

<results_tsv>
{tsv}
</results_tsv>

<journal_recent>
{journal}
</journal_recent>

<diff_baseline_to_best>
{diff}
</diff_baseline_to_best>

<current_strategy_block>
{block}
</current_strategy_block>

Write the replacement strategy block now.
"""

STRATEGIST_RETRY = (
    "Your output tampered with the strategy markers or was empty. Output ONLY "
    "the block contents: plain markdown, no <!-- vyvcode:strategy --> markers, "
    "no preamble."
)


def default_strategist_pass(cfg: VyvConfig, session: ResearchSession,
                            trigger: str, out=print, runner=None) -> None:
    ar_dir = session.ar_dir
    if runner is None:
        from vyvcode.models import llm_for
        from vyvcode.subagents import READ_ONLY_TOOLS, agent_prompt, run_agent_task

        def runner(task: str) -> str:
            return run_agent_task(
                llm_for("strategist", cfg),
                system_prompt=agent_prompt("vyvcode-strategist"),
                task=task,
                workspace=ar_dir,
                tools=READ_ONLY_TOOLS,
            )

    baseline = session.state["baseline"]
    best = session.state["best"]
    diff = _git(ar_dir, "diff", f"{baseline['sha']}..{best['sha']}", "--",
                "train.py", check=False).stdout
    task = STRATEGIST_TASK.format(
        trigger=trigger,
        session_id=session.session_id,
        spread=baseline.get("spread", 0.0),
        tsv=session.tsv_path.read_text() if session.tsv_path.is_file() else "(none)",
        journal=session.journal_tail(15),
        diff=diff or "(best == baseline, no accumulated diff)",
        block=read_strategy_block(ar_dir / "program.md"),
    )

    def valid(text: str) -> bool:
        return bool(text.strip()) and "vyvcode:strategy" not in text

    reply = runner(task)
    if not valid(reply):
        reply = runner(STRATEGIST_RETRY)
    if not valid(reply):
        session.journal(
            f"strategist pass ({trigger}) skipped: output tampered with the "
            "markers twice"
        )
        return

    replace_strategy_block(ar_dir / "program.md", reply)
    session.state["strategist_passes"] += 1
    _git(ar_dir, "add", "program.md")
    _git(ar_dir, "commit", "-m",
         f"strategy: pass {session.state['strategist_passes']}")
    # The strategy commit is now HEAD; best must follow it or the next
    # discard's reset --hard would rewind the strategy away.
    session.state["best"]["sha"] = _head_sha(ar_dir)
    session.save()
    session.write_best()
    session.journal(f"strategist pass {session.state['strategist_passes']} "
                    f"({trigger}) rewrote the strategy block")
    out(f"strategist pass {session.state['strategist_passes']} ({trigger})")


# ── report + progress plot (§6.4) ────────────────────────────────────────────

_PLOT_SCRIPT = """\
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

tsv_path, out_path = sys.argv[1], sys.argv[2]
rows = [l.split("\\t") for l in open(tsv_path).read().splitlines()[1:]]
xs, ys, keeps, best_line = [], [], [], []
best = None
for i, row in enumerate(rows):
    val, status = float(row[1]), row[3]
    if val > 0:
        xs.append(i)
        ys.append(val)
        if status == "keep":
            keeps.append((i, val))
        best = val if best is None or (status == "keep" and val < best) else best
    best_line.append(best)
fig, ax = plt.subplots(figsize=(10, 5), dpi=120)
ax.plot(xs, ys, "o-", color="#888", alpha=0.6, label="experiments")
if keeps:
    ax.plot(*zip(*keeps), "o", color="#2a7", markersize=10, label="keeps")
ax.step(range(len(best_line)), best_line, where="post", color="#27c",
        label="best so far")
ax.set_xlabel("experiment index")
ax.set_ylabel("val_bpb (lower is better)")
ax.legend()
fig.tight_layout()
fig.savefig(out_path)
"""


def progress_png(session: ResearchSession, python_argv: list[str] | None = None,
                 runner=subprocess.run) -> Path | None:
    """Render progress.png from results.tsv using the AR checkout's own env
    (matplotlib is already in upstream's dependency set — no new packages)."""
    if not session.tsv_path.is_file():
        return None
    script = session.dir / "_plot.py"
    script.write_text(_PLOT_SCRIPT)
    out_path = session.dir / "progress.png"
    argv = (python_argv or ["uv", "run", "python"]) + [
        str(script), str(session.tsv_path), str(out_path)
    ]
    proc = runner(argv, cwd=session.ar_dir, capture_output=True, text=True,
                  timeout=120, check=False)
    if proc.returncode != 0 or not out_path.is_file():
        session.journal(f"progress.png skipped: {proc.stderr.strip()[-300:]}")
        return None
    return out_path


def render_research_report(cfg: VyvConfig, session: ResearchSession) -> str:
    state = session.reload().state
    baseline, best = state["baseline"], state["best"]
    counts = state["counts"]
    experiments = session.experiments()
    keeps = [e for e in experiments if e.get("decision") == "keep"]
    crashes = [e for e in experiments if e.get("decision") == "crash"]

    delta = best["val_bpb"] - baseline["val_bpb"]
    pct = 100 * delta / baseline["val_bpb"] if baseline["val_bpb"] else 0.0
    lines = [f"# Autoresearch report — {state['session_id']}", ""]
    lines += [
        f"- branch: {state['branch']} (best sha {best['sha']}, exp {best['exp_id']})",
        f"- val_bpb: baseline {baseline['val_bpb']:.6f} → best "
        f"{best['val_bpb']:.6f} (Δ{delta:+.6f}, {pct:+.2f}%); "
        f"noise spread {baseline.get('spread', 0.0):.6f}",
        f"- experiments: {counts['run']} run / {counts['kept']} kept / "
        f"{counts['discarded']} discarded / {counts['crashed']} crashed",
        f"- strategist passes: {state['strategist_passes']}",
        "",
    ]

    lines += ["## Keeps", ""]
    if keeps:
        lines += ["| exp | hypothesis | val_bpb | Δ |", "|---|---|---|---|"]
        previous = baseline["val_bpb"]
        for e in keeps:
            delta_k = e["val_bpb"] - previous
            lines += [f"| {e['exp_id']:03d} | {e['hypothesis']} | "
                      f"{e['val_bpb']:.6f} | {delta_k:+.6f} |"]
            previous = e["val_bpb"]
    else:
        lines += ["- none (baseline stands)"]
    lines += [""]

    first_ok = next((e for e in experiments if e.get("val_bpb")), None)
    last_keep = keeps[-1] if keeps else None
    if first_ok and last_keep:
        lines += ["## Drift (first → best)", ""]
        for label, key in (("params_M", "num_params_M"),
                           ("peak_vram_mb", "peak_vram_mb"), ("mfu_%", "mfu")):
            a, b = first_ok.get(key), last_keep.get(key)
            if a is not None and b is not None:
                lines += [f"- {label}: {a} → {b}"]
        lines += [""]

    if crashes:
        lines += ["## Crashes", ""]
        for e in crashes[:10]:
            lines += [f"- exp {e['exp_id']:03d}: {e['hypothesis']} — {e['reason']}"]
        lines += [""]

    tokens = sum(e.get("tokens_spent") or 0 for e in experiments)
    lines += ["## Spend", "", f"- researcher tokens (SDK metrics): {tokens}", ""]

    lines += [
        "## Resume", "",
        f"- /vyvcode:autoresearch start in this checkout continues the session "
        f"on {state['branch']} from exp {counts['run'] + 1}",
        f"- artifacts: {session.dir.name}/ (state.json, journal.md, "
        "experiments.jsonl, logs/, progress.png)",
        "",
    ]
    return "\n".join(lines)


def _memory_digest(session: ResearchSession) -> str:
    state = session.state
    baseline, best = state["baseline"], state["best"]
    keeps = [e for e in session.experiments() if e.get("decision") == "keep"]
    dead = [e for e in session.experiments()
            if e.get("decision") in ("discard", "crash")]
    bullets = [
        f"- autoresearch session {state['session_id']} on {state['branch']}",
        f"- val_bpb baseline {baseline['val_bpb']:.6f} → best "
        f"{best['val_bpb']:.6f} over {state['counts']['run']} experiments",
    ]
    for e in keeps[:4]:
        bullets.append(f"- win: {e['hypothesis']}")
    for e in dead[:2]:
        bullets.append(f"- dead end: {e['hypothesis']} ({e['decision']})")
    return "\n".join(bullets[:8])


def finish_session(cfg: VyvConfig, ar_dir: Path, session: ResearchSession,
                   out=print, report_fn=None, plot_fn=None) -> str:
    from dataclasses import replace as _replace

    from vyvcode import memory

    session.reload()
    report_fn = report_fn or render_research_report
    report = report_fn(cfg, session)
    (session.dir / "report.md").write_text(report)
    (plot_fn or progress_png)(session)
    out(report)

    ar_cfg = _replace(cfg, project_root=Path(ar_dir))
    memory.write_digest(
        ar_cfg, session.session_id, _memory_digest(session),
        goal=f"autoresearch {session.state['tag']}",
    )
    counts = session.state["counts"]
    return (
        f"session {session.session_id}: {counts['run']} run / "
        f"{counts['kept']} kept / {counts['crashed']} crashed — report: "
        f"{session.dir / 'report.md'}"
    )


def run_freeform(cfg: VyvConfig, ar_dir: Path, out=print) -> str:
    """D12 escape hatch: upstream's freeform style — one researcher session
    with program.md as context, full tools, no harness loop."""
    from vyvcode.commands import run_fast_path
    from vyvcode.models import llm_for

    program = (Path(ar_dir) / "program.md").read_text()
    out("freeform mode: handing the checkout to the researcher (upstream style)")
    run_fast_path(
        cfg,
        f"You are in an autoresearch checkout. Follow these research org "
        f"instructions:\n\n{program}",
        llm=llm_for("researcher", cfg),
        workspace=ar_dir,
    )
    return "freeform session ended"


def regenerate_report(cfg: VyvConfig, ar_dir: Path, out=print) -> str:
    session = ResearchSession.latest(ar_dir)
    if session is None or not session.state.get("baseline"):
        return "no autoresearch session in this checkout (run setup)"
    report = render_research_report(cfg, session)
    (session.dir / "report.md").write_text(report)
    png = progress_png(session)
    out(report)
    return (
        f"report regenerated: {session.dir / 'report.md'}"
        + (f", {png}" if png else " (progress.png skipped, see journal)")
    )
