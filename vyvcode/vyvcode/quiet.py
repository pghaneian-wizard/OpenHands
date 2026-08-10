"""REPL noise control: SDK log level, known-noise warnings, quiet visualizer.

The SDK's default conversation visualizer prints the full system prompt and
echoes the user's own message every turn; its loggers default to INFO. The
REPL calls ``silence_sdk_noise`` before any SDK import (the SDK reads
``LOG_LEVEL`` at import time) and passes ``quiet_visualizer()`` to every
Conversation so only agent activity — actions, observations, agent messages,
errors — reaches the terminal.
"""

from __future__ import annotations

import os
import warnings

# Known-harmless UserWarnings re-raised by the SDK every single turn.
NOISE_WARNING_PATTERNS = (
    r"Cost calculation failed:.*",  # litellm has no price entry for kimi-k3
    r"tmux is not installed.*",  # subprocess terminal fallback works fine
)


def silence_sdk_noise() -> None:
    """Quiet SDK loggers and per-turn UserWarnings. Call before SDK imports."""
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    for pattern in NOISE_WARNING_PATTERNS:
        warnings.filterwarnings("ignore", message=pattern)


_CONSOLE = None


def shared_console():
    """One rich Console for agent output and the live panel.

    Two Consoles writing to the same terminal corrupt each other's cursor
    handling, so the swarm's live table and the SDK visualizer must share one.
    """
    global _CONSOLE
    if _CONSOLE is None:
        from rich.console import Console

        _CONSOLE = Console()
    return _CONSOLE


def quiet_visualizer():
    """Default visualizer minus the system-prompt dump and user-message echo."""
    from openhands.sdk.conversation.visualizer import DefaultConversationVisualizer
    from openhands.sdk.event import SystemPromptEvent

    class QuietVisualizer(DefaultConversationVisualizer):
        def __init__(self) -> None:
            super().__init__(skip_user_messages=True)
            self._console = shared_console()

        def on_event(self, event) -> None:
            if isinstance(event, SystemPromptEvent):
                return
            super().on_event(event)

    return QuietVisualizer()
