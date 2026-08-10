"""Dispatch table (§6), probe gating, single-run guard, and the fast path."""

import pytest

from vyvcode.commands import Handlers, parse_line, run_fast_path
from vyvcode.config import load_config
from vyvcode.repl import repl_loop
from vyvcode.run_state import Run


class RecordingHandlers:
    def __init__(self):
        self.calls = []

    def pipeline(self, mode, arg):
        self.calls.append(("pipeline", mode, arg))
        return f"pipeline:{mode}"

    def chat(self, text, bypass=False):
        self.calls.append(("chat", text, bypass))
        return "chat"

    def grill(self, arg):
        self.calls.append(("grill", arg))
        return "grill"

    def memory(self, arg):
        self.calls.append(("memory", arg))
        return "memory"

    def status(self):
        self.calls.append(("status",))
        return "status"

    def stop(self):
        self.calls.append(("stop",))
        return "stop"

    def unknown(self, cmd):
        self.calls.append(("unknown", cmd))
        return f"unknown:{cmd}"


class TestDispatchTable:
    def test_every_table_row_routes_correctly(self):
        handlers = RecordingHandlers()
        out_lines = []

        repl_loop(
            [
                "/vyvcode:brainstorm build a thing",
                "/plan add tests",
                "/goal ship it",
                "/vyvcode:grill the schema",
                "/memory-recall postgres decision",
                "/vyvcode:status",
                "/vyvcode:stop",
                "!raw keep thsi exactly",
                "fix the login bug",
                "/nope",
                "   ",
            ],
            handlers,
            out=out_lines.append,
        )

        assert handlers.calls == [
            ("pipeline", "brainstorm", "build a thing"),
            ("pipeline", "plan", "add tests"),
            ("pipeline", "goal", "ship it"),
            ("grill", "the schema"),
            ("memory", "postgres decision"),
            ("status",),
            ("stop",),
            ("chat", "keep thsi exactly", True),
            ("chat", "fix the login bug", False),
            ("unknown", "/nope"),
        ]
        assert "unknown:/nope" in out_lines

    @pytest.mark.parametrize("word", ["exit", "quit", "q", "Exit"])
    def test_exit_words_leave_loop_before_any_model_call(self, word):
        handlers = RecordingHandlers()
        out_lines = []

        repl_loop([word, "this line must never run"], handlers, out=out_lines.append)

        assert handlers.calls == []

    def test_raw_prefix_stripped_and_marked(self):
        parsed = parse_line("!raw don't touch teh spelling")

        assert parsed.kind == "raw"
        assert parsed.arg == "don't touch teh spelling"

    def test_unknown_command_lists_valid_ones(self, tmp_path):
        handlers = Handlers(load_config(tmp_path, env={}))

        message = handlers.unknown("/frobnicate")

        assert "/plan" in message
        assert "/vyvcode:brainstorm" in message


class TestGuards:
    def test_second_pipeline_refused_while_run_active(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        run = Run(cfg.runs_dir, "first job")
        run.to("GRILL")
        handlers = Handlers(cfg)

        message = handlers.pipeline("plan", "second job")

        assert message.startswith("refused")
        assert run.run_id in message
        assert "/vyvcode:stop" in message

    def test_chat_refused_when_communicator_dead(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        handlers = Handlers(cfg, alive={"coder"})

        message = handlers.chat("hello")

        assert message.startswith("refused")
        assert "communicator" in message

    def test_pipeline_refused_when_any_role_dead(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        handlers = Handlers(cfg, alive={"communicator", "coder"})

        message = handlers.pipeline("goal", "x")

        assert message.startswith("refused")
        assert "planner" in message and "reviewer" in message


class TestSecretBoundary:
    def test_pasted_key_redacted_before_any_model_sees_it(self, tmp_path, monkeypatch):
        import vyvcode.commands as commands_mod

        cfg = load_config(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-test12345678"})
        handlers = Handlers(cfg)
        seen = {}

        def fake_optimize(text, llm, cfg_, bypass=False, **kw):
            seen["text"] = text
            return type("R", (), {"text": text})()

        monkeypatch.setattr(commands_mod, "optimize", fake_optimize)
        monkeypatch.setattr(commands_mod, "run_fast_path", lambda *a, **k: None)
        monkeypatch.setattr(
            "vyvcode.memory.write_digest", lambda *a, **k: None
        )
        handlers._communicator = object()

        handlers.chat("use my key sk-ant-test12345678 for the deploy")

        assert "sk-ant-test12345678" not in seen["text"]


class TestFastPath:
    def test_fast_path_edits_file_via_mocked_coder(self, tmp_path):
        from openhands.tools.terminal import TerminalTool

        from tests.fakes import ScriptedLLM, text_message, tool_call_message

        cfg = load_config(tmp_path, env={})
        target = tmp_path / "hello.txt"
        llm = ScriptedLLM.make(
            [
                tool_call_message(
                    TerminalTool.name, {"command": f"printf hi > {target}"}
                ),
                text_message("Done."),
            ]
        )

        run_fast_path(cfg, "create hello.txt containing hi", llm=llm, workspace=tmp_path)

        assert target.read_text() == "hi"


class TestSlashCompleter:
    def _completions(self, text):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        from vyvcode.tui import SlashCompleter

        return [
            c.text
            for c in SlashCompleter().get_completions(
                Document(text, len(text)), CompleteEvent()
            )
        ]

    def test_bare_slash_lists_every_command(self):
        shown = self._completions("/")

        for command in ["/vyvcode:brainstorm", "/plan", "/goal", "/memory-recall"]:
            assert command in shown

    def test_prefix_filters(self):
        shown = self._completions("/vyvcode:s")

        assert shown == ["/vyvcode:status", "/vyvcode:stop"]

    def test_bang_completes_raw(self):
        assert self._completions("!") == ["!raw"]

    def test_no_completion_after_command_token(self):
        assert self._completions("/plan add tests ") == []

    def test_plain_text_not_completed(self):
        assert self._completions("fix the login bug") == []

    def test_help_covers_every_dispatchable_command(self):
        from vyvcode.commands import PIPELINE_COMMANDS, RAW_PREFIX, SIMPLE_COMMANDS
        from vyvcode.tui import COMMAND_HELP

        assert set(COMMAND_HELP) >= {
            *PIPELINE_COMMANDS,
            *SIMPLE_COMMANDS,
            RAW_PREFIX,
        }


class TestQuietVisualizer:
    def test_system_prompt_and_user_messages_hidden_agent_text_shown(self, capsys):
        from openhands.sdk.event import MessageEvent, SystemPromptEvent
        from openhands.sdk.llm import Message, TextContent

        from vyvcode.quiet import quiet_visualizer

        visualizer = quiet_visualizer()
        visualizer.on_event(
            SystemPromptEvent(
                source="agent",
                system_prompt=TextContent(text="SECRET SYSTEM PROMPT"),
                tools=[],
            )
        )
        visualizer.on_event(
            MessageEvent(
                source="user",
                llm_message=Message(
                    role="user", content=[TextContent(text="USER SAID THIS")]
                ),
            )
        )
        visualizer.on_event(
            MessageEvent(
                source="agent",
                llm_message=Message(
                    role="assistant", content=[TextContent(text="AGENT ANSWER")]
                ),
            )
        )

        shown = capsys.readouterr().out
        assert "SECRET SYSTEM PROMPT" not in shown
        assert "USER SAID THIS" not in shown
        assert "AGENT ANSWER" in shown
