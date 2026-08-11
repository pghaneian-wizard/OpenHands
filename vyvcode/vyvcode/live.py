"""Live status panel for a pipeline run.

A run is otherwise opaque: stages pass silently and several coders work at
once, with interleaved agent output the only signal. ``RunMonitor`` keeps a
block pinned below that output — a stage progress bar, the per-phase coder
table, and this session's spend per model. It degrades to plain lines when
stdout is not a terminal, so piped and scripted runs (and the tests) keep
their line-per-event log.
"""

from __future__ import annotations

import sys
import threading
import time
import zlib
from dataclasses import dataclass, field

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BAR_WIDTH = 24
_WORD_EVERY = 24  # refreshes between activity words (~6s at 4/s)

# The pipeline's stages in order; the bar fills as the run walks them.
STAGES = ("GRILL", "BRIEF", "PLANNING", "EXECUTING", "REVIEWING", "REPORTING", "DONE")

# Which role does the talking in each stage — the activity line's fallback
# before any spend lands. Once tokens move, the line follows the spender
# instead: a stage's nominal owner and its actual worker diverge (a grill
# NEEDS-FACT hands the coder-role explorer the terminal for as long as it
# takes, while the stage stays GRILL).
STAGE_ROLES = {
    "GRILL": "communicator",
    "BRIEF": "communicator",
    "PLANNING": "planner",
    "EXECUTING": "coder",
    "REVIEWING": "reviewer",
    "REPORTING": "communicator",
}

# Phase states that stop the clock.
_DONE_STATES = ("merged", "failed", "skipped", "exhausted")

# Rotating activity words. A run is long and mostly silent; a word that moves
# tells you the harness is alive, and a different word per phase makes four
# parallel coders legible at a glance.
ACTIVITY_WORDS = (
    "Percolating", "Shimmying", "Pollinating", "Infusing", "Marinating",
    "Conjuring", "Untangling", "Distilling", "Kindling", "Wrangling",
    "Orchestrating", "Fermenting", "Crystallizing", "Simmering", "Whittling",
    "Galvanizing", "Tessellating", "Burnishing", "Sculpting", "Weaving",
    "Forging", "Hatching", "Germinating", "Brewing", "Refracting",
    "Calibrating", "Harmonizing", "Annealing", "Cultivating", "Alchemizing",
    "Synthesizing", "Unfurling", "Splicing", "Chiseling", "Kneading",
    "Reticulating", "Transmuting", "Coalescing", "Divining", "Threading",
    "Braiding", "Tempering", "Quenching", "Smelting", "Etching",
    "Engraving", "Gilding", "Steeping", "Proofing", "Folding",
    "Excavating", "Prospecting", "Charting", "Surveying", "Noodling",
    "Grokking", "Honing", "Tuning", "Bootstrapping", "Summoning",
    "Bewitching", "Levitating", "Percussing", "Fomenting", "Effervescing",
)

_PULSE = "✦✧✦✧"

# Palette. The panel is read at a glance while agent output scrolls past it,
# so nothing informational goes below grey70 — `dim` and `bright_black` are
# near-invisible on most terminal themes, which is where this started.
STYLE_TEXT = "grey70"  # durations, token counts, the activity suffix
STYLE_MUTED = "grey50"  # the unfilled tail of the bar: present, subordinate
STYLE_WORD = "cyan"  # activity word body, under the sweeping band
STYLE_BAR = "bright_cyan"  # filled bar cells
STYLE_HEAD = "bold bright_white"  # the breathing leading cell
STYLE_STAGE = "bold bright_cyan"  # the stage name
STYLE_AGENT = "bold bright_white"  # which role and model owns a row
STYLE_TOKENS = "yellow"  # per-model token counts, read apart from their names
STYLE_TOTAL = "bold yellow"  # the session total
STYLE_COST = "bold green"  # dollars
STYLE_MODE = "bold magenta"  # the active-mode badge

_MODE_MARK = "◎"

_STATE_STYLES = {
    "merged": "bold green",
    "done": "bold green",
    "green": "bold green",
    "failed": "bold red",
    "exhausted": "bold red",
    "skipped": "yellow",
}


def activity_word(seed: str, rotation: int) -> str:
    """A stable word for this seed at this rotation; different per seed."""
    offset = zlib.crc32(seed.encode()) % len(ACTIVITY_WORDS)
    step = rotation * 7  # coprime with the list length: no short cycles
    return ACTIVITY_WORDS[(offset + step) % len(ACTIVITY_WORDS)]


def shimmer(word: str, tick: int):
    """A bright band sweeping across the word, Rich Text in, Rich Text out."""
    from rich.text import Text

    text = Text()
    head = tick % (len(word) + 6) - 3
    for index, char in enumerate(word):
        distance = abs(index - head)
        if distance == 0:
            style = "bold bright_white"
        elif distance == 1:
            style = "bold bright_cyan"
        elif distance == 2:
            style = "bright_cyan"
        else:
            style = STYLE_WORD
        text.append(char, style=style)
    return text



@dataclass
class PhaseStatus:
    phase_id: str
    name: str = ""
    role: str = "coder"  # which configured role is doing this work
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
class RunMonitor:
    """Thread-safe run status, rendered live when on a terminal."""

    out: object = print
    enabled: bool | None = None  # None = auto-detect a terminal
    ledger: object = None  # defaults to the process-wide usage ledger
    cfg: object = None  # optional: lets the activity line name the working model
    mode: str = ""  # the command that started this run, e.g. "/goal"
    phases: dict[str, PhaseStatus] = field(default_factory=dict)
    stage_name: str = ""
    started: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _live: object = field(default=None, repr=False)
    _tick: int = 0
    _seen_tokens: dict = field(default_factory=dict, repr=False)
    _spender: tuple | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())
        if self.ledger is None:
            from vyvcode.usage import ledger as process_ledger

            self.ledger = process_ledger

    # ── lifecycle ────────────────────────────────────────────────────────
    def __enter__(self) -> "SwarmMonitor":
        if self.enabled:
            try:
                from rich.live import Live

                from vyvcode.quiet import shared_console

                self._live = Live(
                    console=shared_console(),
                    refresh_per_second=6,
                    transient=True,
                    get_renderable=self._next_frame,
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
    def stage(self, name: str) -> None:
        """Advance the run to a named pipeline stage."""
        with self._lock:
            self.stage_name = name
            if self.started is None:
                self.started = time.monotonic()
        self._refresh()

    def activity_line(self) -> str:
        """Claude-Code-style status: what, for how long, at what cost."""
        with self._lock:
            stage, started = self.stage_name, self.started
        if not stage:
            return ""
        word = activity_word(stage, self._tick // _WORD_EVERY)
        bits = [_fmt_duration(time.monotonic() - started) if started else "0s"]
        tokens = self.ledger.total_tokens() if self.ledger is not None else 0
        if tokens:
            bits.append(f"↓ {_fmt_tokens(tokens)} tokens")
        bits.append(self._working_on(stage))
        return f"{word}… (" + " · ".join(bits) + ")"

    def _activity_suffix(self, stage: str) -> str:
        with self._lock:
            started = self.started
        bits = [_fmt_duration(time.monotonic() - started) if started else "0s"]
        tokens = self.ledger.total_tokens() if self.ledger is not None else 0
        if tokens:
            bits.append(f"↓ {_fmt_tokens(tokens)} tokens")
        bits.append(self._working_on(stage))
        return "(" + " · ".join(bits) + ")"

    def _working_on(self, stage: str) -> str:
        from vyvcode.usage import short_model

        roles = getattr(self.cfg, "roles", {}) if self.cfg is not None else {}
        spender = self._spend_leader()
        if spender is not None:
            role, model = spender
            if role in roles:
                return f"{role} {short_model(model)} at {roles[role].effort} effort"
            return f"{role} {short_model(model)}"
        role = STAGE_ROLES.get(stage, "")
        if role not in roles:
            return stage
        r = roles[role]
        return f"{role} {short_model(r.model)} at {r.effort} effort"

    def _spend_leader(self) -> tuple | None:
        """The (role, model) whose tokens last grew — who is really working.

        Sticky between updates: a long single call reports no delta until it
        lands, and the line should keep naming its owner rather than snap back
        to the stage default.
        """
        if self.ledger is None:
            return None
        rows = self.ledger.snapshot()
        with self._lock:
            best, best_delta = None, 0
            for row in rows:
                key = (row.role, row.model)
                delta = row.tokens - self._seen_tokens.get(key, 0)
                self._seen_tokens[key] = row.tokens
                if delta > best_delta:
                    best, best_delta = key, delta
            if best is not None:
                self._spender = best
            return self._spender

    def progress_line(self) -> str:
        """The stage bar as plain text (also what non-terminal runs print)."""
        with self._lock:
            name = self.stage_name
        if not name:
            return ""
        done = STAGES.index(name) + 1 if name in STAGES else 0
        filled = int(_BAR_WIDTH * done / len(STAGES))
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        return f"{bar} {done}/{len(STAGES)} {name}"

    def _head_row(self, head):
        """The activity line, with the mode badge pinned to the right margin."""
        badge = self.mode_line()
        if not badge:
            return head
        from rich.table import Table
        from rich.text import Text

        row = Table.grid(expand=True)
        row.add_column()
        row.add_column(justify="right")
        row.add_row(head, Text(badge, style=STYLE_MODE))
        return row

    def _bar(self, tick: int):
        """Progress bar with a comet head that keeps moving while a stage works."""
        from rich.text import Text

        with self._lock:
            name = self.stage_name
        done = STAGES.index(name) + 1 if name in STAGES else 0
        filled = int(_BAR_WIDTH * done / len(STAGES))
        text = Text()
        for index in range(_BAR_WIDTH):
            if index < filled - 1:
                text.append("▰", style=STYLE_BAR)
            elif index == filled - 1:
                # the leading cell breathes so a long stage never looks stalled
                text.append("▰", style=STYLE_HEAD if tick % 8 < 4 else STYLE_BAR)
            else:
                text.append("▱", style=STYLE_MUTED)
        text.append(f"  {done}/{len(STAGES)} ", style=STYLE_TEXT)
        text.append(name, style=STYLE_STAGE)
        return text

    def mode_line(self) -> str:
        """`◎ /goal active (56s)` — which mode owns the session, and for how long."""
        if not self.mode:
            return ""
        with self._lock:
            started = self.started
        elapsed = _fmt_duration(time.monotonic() - started) if started else "0s"
        return f"{_MODE_MARK} {self.mode} active ({elapsed})"

    def agent_label(self, role: str) -> str:
        """`role model` when that role is configured, else the bare role."""
        roles = getattr(self.cfg, "roles", {}) if self.cfg is not None else {}
        if role not in roles:
            return role
        from vyvcode.usage import short_model

        return f"{role} {short_model(roles[role].model)}"

    def _row_label(self, status: PhaseStatus):
        """Who is working, then what on. Never repeats the identifier."""
        from rich.text import Text

        seen: list[str] = []
        for part in (status.phase_id, status.name):
            if part and part not in seen:
                seen.append(part)
        text = Text()  # empty, so a base style never bleeds into the task text
        text.append(self.agent_label(status.role), style=STYLE_AGENT)
        if seen:
            text.append("  ·  ", style=STYLE_MUTED)
            text.append(" ".join(seen), style=STYLE_TEXT)
        return text

    def usage_line(self) -> str:
        """This session's spend, one entry per model."""
        rows = self.ledger.snapshot() if self.ledger is not None else []
        if not rows:
            return ""
        from vyvcode.usage import fmt_tokens, short_model

        parts = " · ".join(
            f"{row.role} {short_model(row.model)} {fmt_tokens(row.tokens)}"
            for row in rows
        )
        cost = sum(row.cost for row in rows)
        total = fmt_tokens(sum(row.tokens for row in rows))
        return parts + f"  |  {total} tok" + (f" ${cost:.2f}" if cost else "")

    def usage_text(self):
        """`usage_line` for the panel: token counts coloured apart from names."""
        rows = self.ledger.snapshot() if self.ledger is not None else []
        if not rows:
            return None
        from rich.text import Text

        from vyvcode.usage import fmt_tokens, short_model

        text = Text()
        for index, row in enumerate(rows):
            if index:
                text.append(" · ", style=STYLE_MUTED)
            text.append(f"{row.role} {short_model(row.model)} ", style=STYLE_TEXT)
            text.append(fmt_tokens(row.tokens), style=STYLE_TOKENS)
        text.append("  |  ", style=STYLE_MUTED)
        text.append(fmt_tokens(sum(row.tokens for row in rows)), style=STYLE_TOTAL)
        text.append(" tok", style=STYLE_TEXT)
        cost = sum(row.cost for row in rows)
        if cost:
            text.append(f" ${cost:.2f}", style=STYLE_COST)
        return text

    def register(self, phase_id: str, name: str, role: str = "coder") -> None:
        with self._lock:
            self.phases.setdefault(phase_id, PhaseStatus(phase_id, name, role))

    def start(
        self,
        phase_id: str,
        name: str = "",
        attempt: int = 1,
        role: str = "",
    ) -> None:
        with self._lock:
            status = self.phases.setdefault(phase_id, PhaseStatus(phase_id, name))
            status.name = name or status.name
            status.role = role or status.role
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
    def _next_frame(self):
        """Called by Live on every refresh: advances every animation."""
        self._tick += 1
        return self._render()

    def _refresh(self) -> None:
        if self._live is None:
            return
        try:
            self._live.refresh()
        except Exception:
            pass

    def _render(self):
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text

        blocks = []
        with self._lock:
            stage = self.stage_name
        if stage:
            head = Text()
            head.append(_PULSE[self._tick % len(_PULSE)] + " ", style="bold magenta")
            head.append(shimmer(activity_word(stage, self._tick // _WORD_EVERY),
                                self._tick))
            head.append("… ", style=STYLE_TEXT)
            head.append(self._activity_suffix(stage), style=STYLE_TEXT)
            blocks.append(self._head_row(head))
            blocks.append(self._bar(self._tick))
        table = Table.grid(padding=(0, 2))
        table.add_column(width=3)
        table.add_column()  # the label carries its own styles (agent, then task)
        table.add_column()
        table.add_column(justify="right", style=STYLE_TEXT)
        table.add_column(justify="right", style=STYLE_TEXT)
        frame = _SPINNER[self._tick % len(_SPINNER)]
        with self._lock:
            rows = sorted(self.phases.values(), key=lambda s: s.phase_id)
            for status in rows:
                state_style = _STATE_STYLES.get(status.state, STYLE_TEXT)
                if status.running:
                    mark = Text(frame, style=STYLE_BAR)
                    detail = activity_word(
                        status.phase_id, self._tick // _WORD_EVERY
                    ) + "…"
                    detail_style = STYLE_WORD
                else:
                    mark = Text(_state_mark(status.state), style=state_style)
                    detail = status.state
                    detail_style = state_style
                if status.attempt > 1:
                    detail += f" (attempt {status.attempt})"
                table.add_row(
                    mark,
                    self._row_label(status),
                    Text(detail, style=detail_style),
                    _fmt_duration(status.elapsed) if status.started else "",
                    f"{_fmt_tokens(status.tokens)} tok" if status.tokens else "",
                )
            if rows:
                blocks.append(table)
        usage = self.usage_text()
        if usage is not None:
            blocks.append(usage)
        return Group(*blocks)

    def _print_summary(self) -> None:
        with self._lock:
            rows = sorted(self.phases.values(), key=lambda s: s.phase_id)
        usage = self.usage_line()
        if usage:
            self.out(f"  spend: {usage}")
        if not rows or not any(s.started for s in rows):
            return
        for status in rows:
            if not status.started:
                continue
            self.out(
                f"  {self._row_label(status).plain}: {status.state} in "
                f"{_fmt_duration(status.elapsed)}"
                + (f", {_fmt_tokens(status.tokens)} tokens" if status.tokens else "")
            )
        swarm_tokens = sum(s.tokens for s in rows)
        if swarm_tokens:
            swarm_cost = sum(s.cost for s in rows)
            spend = f", ${swarm_cost:.2f}" if swarm_cost else ""
            self.out(f"  swarm total: {_fmt_tokens(swarm_tokens)} tokens{spend}")


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


# The swarm was the first consumer and still names it this way.
SwarmMonitor = RunMonitor
