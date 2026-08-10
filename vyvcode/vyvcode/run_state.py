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
import re
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


class Run:
    """One pipeline run: its directory, persisted state, phase table, cycles."""

    def __init__(self, runs_dir: Path, goal_text: str = "", run_id: str | None = None):
        self.run_id = run_id or new_run_id(goal_text)
        self.dir = Path(runs_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "phases").mkdir(exist_ok=True)
        (self.dir / "review").mkdir(exist_ok=True)
        if self.state_path.is_file():
            self._data = json.loads(self.state_path.read_text())
        else:
            self._data = {
                "run_id": self.run_id,
                "state": "IDLE",
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
        self._data["updated"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        self.state_path.write_text(json.dumps(self._data, indent=2) + "\n")

    def reload(self) -> "Run":
        self._data = json.loads(self.state_path.read_text())
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

    @property
    def is_aborted(self) -> bool:
        """Re-reads disk so an abort from another process is seen mid-run."""
        try:
            return json.loads(self.state_path.read_text())["state"] == "ABORTED"
        except (OSError, json.JSONDecodeError, KeyError):
            return False

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
    return [Run.load(d) for d in dirs]


def latest_run(runs_dir: Path) -> Run | None:
    runs = all_runs(runs_dir)
    return runs[-1] if runs else None


def active_run(runs_dir: Path) -> Run | None:
    """Newest run still mid-flight, if any (one active pipeline at a time)."""
    run = latest_run(runs_dir)
    if run is not None and run.state not in TERMINAL_STATES:
        return run
    return None


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
