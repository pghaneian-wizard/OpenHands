"""Shared scripted fakes for agent-level tests (runbook §15's FakeLLM)."""

from __future__ import annotations

import json

from litellm.types.utils import ModelResponse
from openhands.sdk import LLM, Message, TextContent
from openhands.sdk.llm.llm_response import LLMResponse
from openhands.sdk.llm.message import MessageToolCall
from openhands.sdk.llm.utils.metrics import MetricsSnapshot
from pydantic import PrivateAttr


def text_message(text: str) -> Message:
    return Message(role="assistant", content=[TextContent(text=text)])


def tool_call_message(
    name: str, arguments: dict, call_id: str = "call_1", text: str = "working"
) -> Message:
    return Message(
        role="assistant",
        content=[TextContent(text=text)],
        tool_calls=[
            MessageToolCall(
                id=call_id,
                name=name,
                arguments=json.dumps(arguments),
                origin="completion",
            )
        ],
    )


class ScriptedChatLLM:
    """Chat-only fake: completion() pops scripted reply strings, records history."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.histories: list[list[Message]] = []

    def completion(self, messages, **kwargs):
        self.histories.append(list(messages))
        if not self.replies:
            raise AssertionError("ScriptedChatLLM: script exhausted")
        return type("R", (), {"message": text_message(self.replies.pop(0))})()


class ScriptedLLM(LLM):
    """An LLM whose completion() pops pre-scripted assistant Messages."""

    _script: list = PrivateAttr(default_factory=list)

    @classmethod
    def make(cls, messages: list[Message]) -> "ScriptedLLM":
        llm = cls(model="scripted/fake", usage_id="scripted")
        llm._script = list(messages)
        return llm

    def completion(self, messages, tools=None, **kwargs):
        if not self._script:
            raise AssertionError("ScriptedLLM: script exhausted")
        return LLMResponse(
            message=self._script.pop(0),
            metrics=MetricsSnapshot(),
            raw_response=ModelResponse(),
        )
