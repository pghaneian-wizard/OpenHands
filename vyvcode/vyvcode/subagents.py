"""Shared plumbing for one-shot SDK agent tasks (planner, explorer, coders).

``run_agent_task`` builds an Agent from a role LLM + system prompt + tool
names, runs one task to completion under NeverConfirm, and returns the agent's
final message text.
"""

from __future__ import annotations

import re
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

READ_ONLY_TOOLS = ("terminal", "glob", "grep")

_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

_tools_registered = False


def agent_prompt(name: str) -> str:
    """System prompt = the packaged agent definition's body (frontmatter stripped)."""
    text = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return _FRONTMATTER.sub("", text).strip()


def _ensure_tools_registered() -> None:
    global _tools_registered
    if not _tools_registered:
        from openhands.tools.preset.default import register_default_tools

        register_default_tools(enable_browser=False)
        _tools_registered = True


def last_agent_text(conversation) -> str:
    from openhands.sdk.event import MessageEvent

    for event in reversed(list(conversation.state.events)):
        if isinstance(event, MessageEvent) and event.source == "agent":
            message = event.llm_message
            return "".join(
                c.text for c in message.content if getattr(c, "text", None)
            )
    return ""


def run_agent_task(
    llm,
    system_prompt: str,
    task: str,
    workspace: Path,
    tools: tuple[str, ...] = READ_ONLY_TOOLS,
    max_iterations: int | None = None,
) -> str:
    from openhands.sdk import Agent, Conversation, Tool
    from openhands.sdk.security.confirmation_policy import NeverConfirm

    _ensure_tools_registered()
    agent = Agent(
        llm=llm,
        tools=[Tool(name=name) for name in tools],
        system_prompt=system_prompt,
    )
    kwargs: dict = {}
    if max_iterations is not None:
        kwargs["max_iteration_per_run"] = max_iterations
    conversation = Conversation(agent=agent, workspace=str(workspace), **kwargs)
    try:
        conversation.set_confirmation_policy(NeverConfirm())
        conversation.send_message(task)
        conversation.run()
        return last_agent_text(conversation)
    finally:
        conversation.close()
