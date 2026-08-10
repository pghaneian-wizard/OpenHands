"""REPL chrome: slash-command completion, styled prompt, status toolbar.

Kept separate from repl.py so the loop stays testable without a terminal.
"""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style

from vyvcode.commands import RAW_PREFIX
from vyvcode.config import VyvConfig

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


def bottom_toolbar(cfg: VyvConfig):
    """One-line status: project dir and the four probed role models."""
    roles = " · ".join(
        f"{name} {cfg.roles[name].model.split('/')[-1]}"
        for name in ("communicator", "planner", "coder", "reviewer")
    )
    return f" {cfg.project_root}  |  {roles} "
