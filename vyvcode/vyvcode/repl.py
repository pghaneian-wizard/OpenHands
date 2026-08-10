"""The interactive loop: readline history, Ctrl-C cancels the step, Ctrl-D exits."""

from __future__ import annotations

import sys
from collections.abc import Iterable

from vyvcode import __version__
from vyvcode.commands import Handlers, execute, parse_line
from vyvcode.config import VyvConfig

BANNER = (
    f"vyvcode {__version__} — /vyvcode:brainstorm /plan /goal /vyvcode:grill "
    "/memory-recall /vyvcode:status /vyvcode:stop !raw · Ctrl-D exits"
)


def repl_loop(lines: Iterable[str], handlers, out=print) -> None:
    """Core loop, decoupled from input source so tests can feed a list."""
    for line in lines:
        parsed = parse_line(line)
        if parsed.kind == "empty":
            continue
        try:
            message = execute(parsed, handlers)
        except KeyboardInterrupt:
            out("cancelled")
            continue
        except NotImplementedError as exc:
            out(str(exc))
            continue
        if message:
            out(message)


def _interactive_lines(history_path) -> Iterable[str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(history=FileHistory(str(history_path)))
    while True:
        try:
            yield session.prompt("vyvcode> ")
        except KeyboardInterrupt:
            continue  # Ctrl-C at an empty prompt: stay in the REPL
        except EOFError:
            return  # Ctrl-D


def run_repl(cfg: VyvConfig, do_probe: bool = True) -> int:
    print(BANNER)
    alive: set[str] | None = None
    if do_probe:
        from vyvcode.models import render_probe_table, roles_alive, run_probe

        results = run_probe(cfg)
        print(render_probe_table(results))
        alive = roles_alive(results)

    handlers = Handlers(cfg, alive=alive)
    if sys.stdin.isatty():
        lines = _interactive_lines(cfg.project_root / ".vyvcode" / "history")
    else:
        lines = (line.rstrip("\n") for line in sys.stdin)
    repl_loop(lines, handlers)
    return 0
