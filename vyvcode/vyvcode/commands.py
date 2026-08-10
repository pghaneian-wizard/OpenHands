"""Command dispatch (runbook §6) and the plain-message fast path (D1).

Commands are matched before optimization; the argument text is optimized after
matching. ``!raw`` bypasses the optimizer entirely.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from vyvcode.config import VyvConfig, redact
from vyvcode.models import llm_for
from vyvcode.optimizer import optimize
from vyvcode.run_state import abort_active, active_run, render_status

RAW_PREFIX = "!raw"

PIPELINE_COMMANDS = {
    "/vyvcode:brainstorm": "brainstorm",
    "/plan": "plan",
    "/goal": "goal",
}

SIMPLE_COMMANDS = {
    "/vyvcode:grill": "grill",
    "/memory-recall": "memory",
    "/vyvcode:status": "status",
    "/vyvcode:stop": "stop",
    "/vyvcode:autoresearch": "autoresearch",
}

# Common typo: /vyvecode:* is a silent alias of /vyvcode:*.
ALIAS_PREFIX = "/vyvecode:"
CANONICAL_PREFIX = "/vyvcode:"

# Bare exit words leave the REPL instead of being sent to the coder as a task.
EXIT_WORDS = frozenset({"exit", "quit", "q"})


def known_commands() -> list[str]:
    return sorted([*PIPELINE_COMMANDS, *SIMPLE_COMMANDS, RAW_PREFIX])


@dataclass(frozen=True)
class Parsed:
    kind: str  # pipeline | grill | memory | status | stop | raw | chat | unknown | empty | exit
    arg: str = ""
    mode: str | None = None


def parse_line(line: str) -> Parsed:
    stripped = line.strip()
    if not stripped:
        return Parsed("empty")
    if stripped.lower() in EXIT_WORDS:
        return Parsed("exit")
    if stripped == RAW_PREFIX or stripped.startswith(RAW_PREFIX + " "):
        return Parsed("raw", stripped[len(RAW_PREFIX) :].strip())
    if stripped.startswith("/"):
        token, _, rest = stripped.partition(" ")
        if token.startswith(ALIAS_PREFIX):
            token = CANONICAL_PREFIX + token[len(ALIAS_PREFIX) :]
        if token in PIPELINE_COMMANDS:
            return Parsed("pipeline", rest.strip(), mode=PIPELINE_COMMANDS[token])
        if token in SIMPLE_COMMANDS:
            return Parsed(SIMPLE_COMMANDS[token], rest.strip())
        return Parsed("unknown", token)
    return Parsed("chat", stripped)


class Handlers:
    """Real command handlers. Tests substitute a recording fake."""

    def __init__(self, cfg: VyvConfig, alive: set[str] | None = None):
        self.cfg = cfg
        self.alive = alive  # None = no probe gating (tests / --no-probe)
        self._communicator = None

    # lazy: only built when a message actually needs optimizing
    def communicator_llm(self):
        if self._communicator is None:
            self._communicator = llm_for("communicator", self.cfg)
        return self._communicator

    def _optimized(self, text: str, bypass: bool = False) -> str:
        # Keys never ride inside prompt text, toward any provider (§16).
        text = redact(text, self.cfg.secret_values)
        result = optimize(text, self.communicator_llm(), self.cfg, bypass=bypass)
        return result.text

    def _require_roles(self, roles: set[str]) -> str | None:
        if self.alive is None:
            return None
        dead = sorted(roles - self.alive)
        if dead:
            return (
                f"refused: role(s) {', '.join(dead)} failed the startup probe; "
                "fix keys/models (see table above) or restart"
            )
        return None

    def chat(self, text: str, bypass: bool = False) -> str | None:
        refusal = self._require_roles({"communicator", "coder"})
        if refusal:
            return refusal
        if bypass:
            message = redact(text, self.cfg.secret_values)
        else:
            message = self._optimized(text)
        run_fast_path(self.cfg, message)
        from vyvcode import memory

        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d_%H%M%S")
        memory.write_digest(
            self.cfg, f"fast_{stamp}",
            f"- fast-path task completed by single coder: {message}",
            goal=message,
        )
        return None

    def pipeline(self, mode: str, arg: str) -> str | None:
        refusal = self._require_roles({"communicator", "planner", "coder", "reviewer"})
        if refusal:
            return refusal
        current = active_run(self.cfg.runs_dir)
        if current is not None:
            return (
                f"refused: run {current.run_id} is active "
                "(/vyvcode:status to inspect, /vyvcode:stop to abort)"
            )
        from vyvcode.pipeline import run_pipeline

        return run_pipeline(self.cfg, mode=mode, raw_text=arg)

    def grill(self, arg: str) -> str | None:
        # Drives the communicator plus the coder-backed NEEDS-FACT explorer.
        refusal = self._require_roles({"communicator", "coder"})
        if refusal:
            return refusal
        from vyvcode.grill import standalone

        return standalone(self.cfg, self._optimized(arg))

    def memory(self, arg: str) -> str | None:
        from vyvcode.memory import recall

        return recall(self.cfg, arg)

    def status(self) -> str:
        return render_status(self.cfg.runs_dir)

    def stop(self) -> str:
        run_id = abort_active(self.cfg.runs_dir)
        if run_id is None:
            return "no active run"
        return f"aborted {run_id}; worktrees preserved for inspection"

    def autoresearch(self, arg: str) -> str | None:
        from vyvcode.research import dispatch

        return dispatch(self.cfg, arg)

    def unknown(self, cmd: str) -> str:
        return f"unknown command {cmd}. Valid: {', '.join(known_commands())}"


def execute(parsed: Parsed, handlers) -> str | None:
    if parsed.kind == "empty":
        return None
    if parsed.kind == "pipeline":
        return handlers.pipeline(parsed.mode, parsed.arg)
    if parsed.kind == "raw":
        return handlers.chat(parsed.arg, bypass=True)
    if parsed.kind == "chat":
        return handlers.chat(parsed.arg)
    if parsed.kind == "unknown":
        return handlers.unknown(parsed.arg)
    if parsed.kind in ("grill", "memory", "autoresearch"):
        return getattr(handlers, parsed.kind)(parsed.arg)
    if parsed.kind in ("status", "stop"):
        return getattr(handlers, parsed.kind)()
    raise ValueError(f"unhandled parse kind {parsed.kind!r}")


# ── Fast path (D1): one coder, no pipeline ──────────────────────────────────


def run_fast_path(
    cfg: VyvConfig,
    text: str,
    llm=None,
    workspace: Path | None = None,
) -> None:
    """Single vyvcode-coder CodeAct session in the project dir, full auto."""
    from openhands.sdk import Conversation
    from openhands.sdk.security.confirmation_policy import NeverConfirm
    from openhands.tools.preset.default import get_default_agent

    from vyvcode.quiet import quiet_visualizer

    agent = get_default_agent(llm=llm or llm_for("coder", cfg), cli_mode=True)
    conversation = Conversation(
        agent=agent,
        workspace=str(workspace or cfg.project_root),
        visualizer=quiet_visualizer(),
    )
    try:
        conversation.set_confirmation_policy(NeverConfirm())
        conversation.send_message(text)
        conversation.run()
    finally:
        conversation.close()
