"""Live status panel for the coder swarm.

A parallel swarm is otherwise opaque: several coders work at once and the only
signal is interleaved agent output. ``SwarmMonitor`` keeps a table pinned below
that output showing, per phase, what it is doing, for how long, and what it has
spent. It degrades to plain lines when stdout is not a terminal, so piped and
scripted runs (and the tests) keep their line-per-event log.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Phase states that stop the clock.
_DONE_STATES = ("merged", "failed", "skipped", "exhausted")


@dataclass
class PhaseStatus:
    phase_id: str
    name: str = ""
    state: str = "waiting"
    attempt: int = 0
    started: float | None = None
    ended: float | None = None
    tokens: int = 0
    cost: float = 0.0
    steps: int = 0

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.ended or time.monotonic()) - self.started

    @property
    def running(self) -> bool:
        return self.started is not None and self.ended is None


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


@dataclass
class SwarmMonitor:
    """Thread-safe status of every phase, rendered live when on a terminal."""

    out: object = print
    enabled: bool | None = None  # None = auto-detect a terminal
    phases: dict[str, PhaseStatus] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _live: object = field(default=None, repr=False)
    _tick: int = 0

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())

    # ── lifecycle ────────────────────────────────────────────────────────
    def __enter__(self) -> "SwarmMonitor":
        if self.enabled:
            try:
                from rich.live import Live

                from vyvcode.quiet import shared_console

                self._live = Live(
                    self._render(),
                    console=shared_console(),
                    refresh_per_second=4,
                    transient=True,
                )
                self._live.__enter__()
            except Exception:  # a panel is never worth failing a run over
                self._live = None
        return self

    def __exit__(self, *exc_info) -> None:
        live, self._live = self._live, None
        if live is not None:
            try:
                live.__exit__(*exc_info)
            except Exception:
                pass
        self._print_summary()

    # ── updates (called from worker threads) ─────────────────────────────
    def register(self, phase_id: str, name: str) -> None:
        with self._lock:
            self.phases.setdefault(phase_id, PhaseStatus(phase_id, name))

    def start(self, phase_id: str, name: str = "", attempt: int = 1) -> None:
        with self._lock:
            status = self.phases.setdefault(phase_id, PhaseStatus(phase_id, name))
            status.name = name or status.name
            status.state = "coding"
            status.attempt = attempt
            status.started = time.monotonic()
            status.ended = None
        self._refresh()

    def progress(self, phase_id: str, tokens: int = 0, cost: float = 0.0) -> None:
        with self._lock:
            status = self.phases.get(phase_id)
            if status is None:
                return
            status.steps += 1
            status.tokens = max(status.tokens, tokens)
            status.cost = max(status.cost, cost)
        self._refresh()

    def finish(self, phase_id: str, state: str) -> None:
        with self._lock:
            status = self.phases.get(phase_id)
            if status is None:
                return
            status.state = state
            status.ended = time.monotonic()
        self._refresh()

    def log(self, message: str) -> None:
        """Emit a line above the panel."""
        if self._live is not None:
            try:
                self._live.console.print(message, highlight=False)
                return
            except Exception:
                pass
        self.out(message)

    # ── rendering ────────────────────────────────────────────────────────
    def _refresh(self) -> None:
        if self._live is None:
            return
        try:
            self._tick += 1
            self._live.update(self._render())
        except Exception:
            pass

    def _render(self):
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(width=3)
        table.add_column(style="bold")
        table.add_column()
        table.add_column(justify="right")
        table.add_column(justify="right")
        frame = _SPINNER[self._tick % len(_SPINNER)]
        with self._lock:
            rows = sorted(self.phases.values(), key=lambda s: s.phase_id)
            for status in rows:
                mark = frame if status.running else _state_mark(status.state)
                label = f"{status.phase_id} {status.name}".strip()
                detail = status.state
                if status.attempt > 1:
                    detail += f" (attempt {status.attempt})"
                table.add_row(
                    mark,
                    label,
                    detail,
                    _fmt_duration(status.elapsed) if status.started else "",
                    f"{_fmt_tokens(status.tokens)} tok" if status.tokens else "",
                )
        return table

    def _print_summary(self) -> None:
        with self._lock:
            rows = sorted(self.phases.values(), key=lambda s: s.phase_id)
            total_tokens = sum(s.tokens for s in rows)
            total_cost = sum(s.cost for s in rows)
        if not rows or not any(s.started for s in rows):
            return
        for status in rows:
            if not status.started:
                continue
            self.out(
                f"  {status.phase_id} {status.name}: {status.state} in "
                f"{_fmt_duration(status.elapsed)}"
                + (f", {_fmt_tokens(status.tokens)} tokens" if status.tokens else "")
            )
        if total_tokens:
            spend = f", ${total_cost:.2f}" if total_cost else ""
            self.out(f"  swarm total: {_fmt_tokens(total_tokens)} tokens{spend}")


def _state_mark(state: str) -> str:
    if state in ("merged", "done", "green"):
        return "✔"
    if state in ("failed", "exhausted"):
        return "✘"
    if state == "skipped":
        return "–"
    return "·"


def token_reporter(llm, monitor: SwarmMonitor | None, phase_id: str):
    """SDK event callback that mirrors an agent's spend into the monitor."""
    if monitor is None:
        return None

    def on_event(_event) -> None:
        usage = getattr(getattr(llm, "metrics", None), "accumulated_token_usage", None)
        tokens = 0
        if usage is not None:
            tokens = (
                getattr(usage, "prompt_tokens", 0)
                + getattr(usage, "completion_tokens", 0)
            )
        cost = getattr(getattr(llm, "metrics", None), "accumulated_cost", 0.0) or 0.0
        monitor.progress(phase_id, tokens=tokens, cost=cost)

    return on_event
