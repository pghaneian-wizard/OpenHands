"""Run directory, state machine, and resumability (runbook §13).

Every pipeline run owns ``.vyvcode/runs/<run_id>/`` with ``state.json``
persisted after each transition. The machine:

    IDLE → GRILL → BRIEF → PLANNING → [PLAN_GATE] → EXECUTING ⇄ MERGING
         → REVIEWING ⇄ FIXING → REPORTING → DONE
    any-state → ABORTED          REVIEWING → ESCALATED (cycle cap)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

STATES = (
    "IDLE",
    "GRILL",
    "BRIEF",
    "PLANNING",
    "PLAN_GATE",
    "EXECUTING",
    "MERGING",
    "REVIEWING",
    "FIXING",
    "REPORTING",
    "DONE",
    "ABORTED",
    "ESCALATED",
)

TERMINAL_STATES = frozenset({"DONE", "ABORTED", "ESCALATED"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "IDLE": frozenset({"GRILL"}),
    "GRILL": frozenset({"BRIEF"}),
    "BRIEF": frozenset({"PLANNING"}),
    "PLANNING": frozenset({"PLAN_GATE", "EXECUTING"}),
    "PLAN_GATE": frozenset({"EXECUTING"}),
    "EXECUTING": frozenset({"MERGING", "REVIEWING"}),
    "MERGING": frozenset({"EXECUTING", "REVIEWING"}),
    "REVIEWING": frozenset({"FIXING", "REPORTING", "ESCALATED"}),
    "FIXING": frozenset({"REVIEWING"}),
    "REPORTING": frozenset({"DONE"}),
}


class StateError(Exception):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%d_%H%M%S")


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "run"


def new_run_id(goal_text: str) -> str:
    return f"run_{_now()}_{slugify(goal_text)}"


def _unique_run_id(runs_dir: Path, goal_text: str) -> str:
    """A run id no existing run owns.

    Ids carry a whole-second timestamp, so two runs started in the same second
    with the same goal collide; without this the second run would adopt the
    first one's state.json and its finished state.
    """
    base = new_run_id(goal_text)
    candidate, suffix = base, 1
    while (runs_dir / candidate / "state.json").is_file():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _boot_id() -> str:
    """Identifier of the current boot; empty when the platform has none."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""


class Run:
    """One pipeline run: its directory, persisted state, phase table, cycles."""

    def __init__(self, runs_dir: Path, goal_text: str = "", run_id: str | None = None):
        runs_dir = Path(runs_dir)
        if run_id is None:
            run_id = _unique_run_id(runs_dir, goal_text)
        self.run_id = run_id
        self.dir = runs_dir / self.run_id
        if self.state_path.is_file():
            self._data = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "phases").mkdir(exist_ok=True)
        (self.dir / "review").mkdir(exist_ok=True)
        self._data = {
            "run_id": self.run_id,
            "state": "IDLE",
            # Owner process; active_run checks liveness. The boot id makes a
            # recycled pid after a reboot read as dead rather than alive.
            "pid": os.getpid(),
            "boot": _boot_id(),
            "goal": goal_text,
            "created": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "updated": None,
            "phases": {},
            "review_cycle": 0,
            "history": [],
        }
        self._save()

    # ── persistence ──────────────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def _save(self) -> None:
        # This writes the whole document, so a stale in-memory state would undo
        # an abort written by another process (/vyvcode:stop). An abort wins.
        if self._data["state"] != "ABORTED" and self._disk_state() == "ABORTED":
            self._data["state"] = "ABORTED"
        self._data["updated"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        # Write-then-rename: a crash mid-write must never leave a truncated
        # state.json behind, which would be unparseable forever after.
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def reload(self) -> "Run":
        self._data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return self

    @classmethod
    def load(cls, run_dir: Path) -> "Run":
        run_dir = Path(run_dir)
        return cls(run_dir.parent, run_id=run_dir.name)

    # ── state machine ────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._data["state"]

    def to(self, new_state: str) -> None:
        if new_state not in STATES:
            raise StateError(f"unknown state {new_state!r}")
        current = self.state
        if new_state == "ABORTED":
            allowed = current not in TERMINAL_STATES
        else:
            allowed = new_state in _TRANSITIONS.get(current, frozenset())
        if not allowed:
            raise StateError(f"invalid transition {current} → {new_state}")
        self._data["state"] = new_state
        self._data["history"].append(
            [_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"), new_state]
        )
        self._save()

    def _disk_state(self) -> str | None:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))["state"]
        except (OSError, ValueError, KeyError):
            return None

    @property
    def is_aborted(self) -> bool:
        """Re-reads disk so an abort from another process is seen mid-run."""
        return self._disk_state() == "ABORTED"

    # ── phase table + review cycles ──────────────────────────────────────

    @property
    def phases(self) -> dict[str, dict]:
        return self._data["phases"]

    def set_phase(self, phase_id: str, **fields) -> None:
        self._data["phases"].setdefault(phase_id, {}).update(fields)
        self._save()

    @property
    def review_cycle(self) -> int:
        return self._data["review_cycle"]

    def bump_cycle(self) -> int:
        self._data["review_cycle"] += 1
        self._save()
        return self._data["review_cycle"]

    # ── rendering ────────────────────────────────────────────────────────

    def render_status(self) -> str:
        lines = [f"{self.run_id}: {self.state}"]
        for phase_id, info in sorted(self._data["phases"].items()):
            status = info.get("status", "?")
            name = info.get("name", "")
            lines.append(f"  {phase_id} {name}: {status}")
        if self._data["review_cycle"]:
            lines.append(f"  review cycle: {self._data['review_cycle']}")
        return "\n".join(lines)


# ── module-level helpers used by the dispatcher ──────────────────────────────


def all_runs(runs_dir: Path) -> list[Run]:
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    dirs = sorted(
        (d for d in runs_dir.iterdir() if (d / "state.json").is_file()),
        key=lambda d: d.name,
    )
    runs = []
    for d in dirs:
        try:
            runs.append(Run.load(d))
        except (OSError, ValueError, KeyError):
            continue  # unreadable run: skip it rather than break every command
    return runs


def latest_run(runs_dir: Path) -> Run | None:
    runs = all_runs(runs_dir)
    return runs[-1] if runs else None


def _owner_alive(data: Mapping) -> bool:
    """Is the process that started this run still running?"""
    boot, current_boot = data.get("boot"), _boot_id()
    if current_boot and boot is not None and boot != current_boot:
        return False  # different boot: the pid means nothing now
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def active_run(runs_dir: Path) -> Run | None:
    """Newest run still mid-flight, if any (one active pipeline at a time).

    A non-terminal run whose owning process is gone is a crash leftover:
    mark it ABORTED so it stops blocking new runs.
    """
    run = latest_run(runs_dir)
    if run is None or run.state in TERMINAL_STATES:
        return None
    if not _owner_alive(run._data):
        run.to("ABORTED")
        return None
    return run


def abort_active(runs_dir: Path) -> str | None:
    run = active_run(runs_dir)
    if run is None:
        return None
    run.to("ABORTED")
    return run.run_id


def render_status(runs_dir: Path) -> str:
    run = latest_run(runs_dir)
    if run is None:
        return "IDLE — no runs yet"
    return run.render_status()
