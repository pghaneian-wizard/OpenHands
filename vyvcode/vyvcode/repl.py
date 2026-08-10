"""The interactive loop: readline history, Ctrl-C cancels the step, Ctrl-D exits."""

from __future__ import annotations

import sys
from collections.abc import Iterable

from vyvcode import __version__
from vyvcode.commands import Handlers, execute, parse_line
from vyvcode.config import VyvConfig

BANNER = (
    f"vyvcode {__version__} — /vyvcode:brainstorm /plan /goal /vyvcode:grill "
    "/memory-recall /vyvcode:status /vyvcode:stop !raw · exit/Ctrl-D exits"
)


def repl_loop(lines: Iterable[str], handlers, out=print) -> None:
    """Core loop, decoupled from input source so tests can feed a list."""
    for line in lines:
        parsed = parse_line(line)
        if parsed.kind == "empty":
            continue
        if parsed.kind == "exit":
            return
        try:
            message = execute(parsed, handlers)
        except KeyboardInterrupt:
            out("cancelled")
            continue
        except NotImplementedError as exc:
            out(str(exc))
            continue
        except Exception as exc:
            # A broken command must not kill the REPL; the run is already
            # marked ABORTED by the pipeline's own crash handling.
            out(f"error: {type(exc).__name__}: {exc}")
            continue
        if message:
            out(message)


def _interactive_lines(history_path, cfg: VyvConfig) -> Iterable[str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    from vyvcode.tui import PROMPT, PROMPT_STYLE, SlashCompleter, bottom_toolbar

    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_path)),
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=PROMPT_STYLE,
        bottom_toolbar=lambda: bottom_toolbar(cfg),
        refresh_interval=1.0,  # keeps the spend line current while idle
    )
    while True:
        try:
            yield session.prompt(PROMPT)
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
        lines = _interactive_lines(cfg.project_root / ".vyvcode" / "history", cfg)
    else:
        lines = (line.rstrip("\n") for line in sys.stdin)
    repl_loop(lines, handlers)
    return 0
