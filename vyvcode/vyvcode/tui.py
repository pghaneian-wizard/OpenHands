"""REPL chrome: slash-command completion, styled prompt, status toolbar.

Kept separate from repl.py so the loop stays testable without a terminal.
"""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style

from vyvcode.commands import RAW_PREFIX
from vyvcode.config import VyvConfig
from vyvcode.usage import fmt_tokens
from vyvcode.usage import ledger as usage_ledger
from vyvcode.usage import short_model

# Command -> one-line help, shown in the completion menu (README table).
COMMAND_HELP: dict[str, str] = {
    "/vyvcode:brainstorm": "full pipeline, unbounded grill + design spec",
    "/plan": "full pipeline, grill capped at 4 rounds",
    "/goal": "full pipeline, grill capped at 2 rounds",
    "/vyvcode:grill": "standalone interview, no handoff",
    "/memory-recall": "search project memory, answer with dates",
    "/vyvcode:status": "show active run state",
    "/vyvcode:stop": "gracefully abort the active run",
    "/vyvcode:autoresearch": "overnight ML-research loop",
    RAW_PREFIX: "bypass the optimizer, send text verbatim",
    "exit": "leave the REPL (also: quit, Ctrl-D)",
}

PROMPT = [("class:prompt", "vyvcode> ")]

PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "bottom-toolbar": "italic bg:ansiblack ansiwhite",
        "completion-menu.completion": "bg:ansiblack ansiwhite",
        "completion-menu.completion.current": "bg:ansicyan ansiblack",
        "completion-menu.meta.completion": "bg:ansiblack ansibrightblack",
        "completion-menu.meta.completion.current": "bg:ansicyan ansiblack",
    }
)


class SlashCompleter(Completer):
    """Complete the leading command token; silent for plain chat text."""

    def get_completions(self, document, complete_event):
        stripped = document.text_before_cursor.lstrip()
        if not stripped.startswith(("/", "!")) or " " in stripped:
            return
        for command, help_text in COMMAND_HELP.items():
            if command.startswith(stripped) and command != stripped:
                yield Completion(
                    command,
                    start_position=-len(stripped),
                    display=command,
                    display_meta=help_text,
                )


def bottom_toolbar(cfg: VyvConfig, ledger=None):
    """One-line status: project dir, then this session's spend per model.

    Before anything has been spent there is nothing to total, so the line
    shows which model each role is configured to use instead.
    """
    ledger = ledger if ledger is not None else usage_ledger
    rows = ledger.snapshot()
    if rows:
        models = " · ".join(
            f"{row.role} {short_model(row.model)} {fmt_tokens(row.tokens)}"
            for row in rows
        )
        cost = ledger.total_cost()
        total = f"  |  {fmt_tokens(ledger.total_tokens())} tok"
        if cost:
            total += f" ${cost:.2f}"
    else:
        models = " · ".join(
            f"{role} {short_model(cfg.roles[role].model)}"
            for role in ("communicator", "planner", "coder", "reviewer")
        )
        total = ""
    return f" {cfg.project_root}  |  {models}{total} "
