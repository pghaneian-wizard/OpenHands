"""Session token ledger and the surfaces that render it (toolbar, live panel)."""

import threading

from vyvcode.config import load_config
from vyvcode.usage import UsageLedger


class FakeMetrics:
    def __init__(self, prompt=0, completion=0, cost=0.0):
        self.accumulated_cost = cost
        self.accumulated_token_usage = type(
            "Usage", (), {"prompt_tokens": prompt, "completion_tokens": completion}
        )()


class FakeLLM:
    def __init__(self, model, prompt=0, completion=0, cost=0.0):
        self.model = model
        self.metrics = FakeMetrics(prompt, completion, cost)


class TestLedger:
    def test_totals_per_role_sum_every_instance_of_that_role(self):
        ledger = UsageLedger()
        # The swarm builds one LLM per phase, all of them the coder role.
        ledger.register("coder", FakeLLM("openai/kimi-k3", 1000, 200, 0.10))
        ledger.register("coder", FakeLLM("openai/kimi-k3", 500, 100, 0.05))
        ledger.register("planner", FakeLLM("anthropic/claude-fable-5", 300, 50, 0.02))

        rows = {row.role: row for row in ledger.snapshot()}

        assert rows["coder"].tokens == 1800
        assert rows["coder"].model == "openai/kimi-k3"
        assert round(rows["coder"].cost, 2) == 0.15
        assert rows["planner"].tokens == 350
        assert ledger.total_tokens() == 2150

    def test_totals_keep_the_prompt_and_completion_halves_apart(self):
        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 1000, 200))
        ledger.register("coder", FakeLLM("openai/kimi-k3", 500, 100))
        ledger.register("planner", FakeLLM("anthropic/claude-fable-5", 300, 50))

        coder = {row.role: row for row in ledger.snapshot()}["coder"]
        totals = ledger.totals()

        assert (coder.input_tokens, coder.output_tokens) == (1500, 300)
        assert (totals.total, totals.input_tokens, totals.output_tokens) == (
            2150,
            1800,
            350,
        )

    def test_snapshot_reflects_live_growth_without_reregistering(self):
        ledger = UsageLedger()
        llm = FakeLLM("openai/kimi-k3")
        ledger.register("coder", llm)

        llm.metrics = FakeMetrics(prompt=900, completion=100, cost=0.4)

        assert ledger.total_tokens() == 1000

    def test_roles_without_spend_are_not_listed(self):
        ledger = UsageLedger()
        ledger.register("reviewer", FakeLLM("anthropic/claude-opus-5"))

        assert ledger.snapshot() == []

    def test_registration_is_thread_safe(self):
        ledger = UsageLedger()

        def register(n):
            ledger.register("coder", FakeLLM("m", prompt=n))

        threads = [threading.Thread(target=register, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ledger.total_tokens() == sum(range(40))


class TestToolbar:
    def test_toolbar_shows_per_model_tokens_once_spent(self, tmp_path):
        from vyvcode.tui import bottom_toolbar

        cfg = load_config(tmp_path, env={})
        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 12_000, 300, 0.42))

        text = bottom_toolbar(cfg, ledger)

        assert "kimi-k3" in text
        assert "12.3k" in text
        assert "$0.42" in text

    def test_toolbar_breaks_the_total_into_input_and_output(self, tmp_path):
        from vyvcode.tui import bottom_toolbar

        cfg = load_config(tmp_path, env={})
        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 12_000, 300, 0.42))

        text = bottom_toolbar(cfg, ledger)

        assert "Total: 12.3k" in text
        assert "Input: 12.0k" in text
        assert "Output: 300" in text

    def test_toolbar_falls_back_to_configured_models_before_any_spend(self, tmp_path):
        from vyvcode.tui import bottom_toolbar

        cfg = load_config(tmp_path, env={})

        text = bottom_toolbar(cfg, UsageLedger())

        assert "gpt-5.6-sol" in text
        assert "kimi-k3" in text


class TestActivityWords:
    def test_word_is_stable_for_a_seed_and_rotates_over_time(self):
        from vyvcode.live import ACTIVITY_WORDS, activity_word

        first = activity_word("P1", rotation=0)
        again = activity_word("P1", rotation=0)
        later = activity_word("P1", rotation=1)

        assert first == again  # no flicker between refreshes
        assert first != later  # but it does move on
        assert first in ACTIVITY_WORDS

    def test_parallel_phases_get_different_words(self):
        from vyvcode.live import activity_word

        words = {activity_word(pid, rotation=0) for pid in ("P1", "P2", "P3", "P4")}

        assert len(words) == 4

    def test_every_word_is_a_gerund(self):
        from vyvcode.live import ACTIVITY_WORDS

        assert len(ACTIVITY_WORDS) >= 40
        assert len(set(ACTIVITY_WORDS)) == len(ACTIVITY_WORDS)
        assert all(w.endswith("ing") and w[0].isupper() for w in ACTIVITY_WORDS)

    def test_activity_line_carries_elapsed_tokens_and_the_working_model(self, tmp_path):
        from vyvcode.live import RunMonitor

        cfg = load_config(tmp_path, env={})
        ledger = UsageLedger()
        ledger.register("planner", FakeLLM("anthropic/claude-fable-5", 3_000, 100))
        monitor = RunMonitor(enabled=False, ledger=ledger, cfg=cfg)

        monitor.stage("PLANNING")
        line = monitor.activity_line()

        assert line.endswith(")")
        assert "…" in line
        assert "3.1k tokens" in line
        assert "claude-fable-5" in line
        assert "max effort" in line

    # The grill stage belongs to the communicator, but a NEEDS-FACT dispatch
    # hands an explorer (the coder role) the terminal for as long as it takes.
    # The header must follow the spend, not the stage's nominal owner.
    def test_activity_line_names_who_is_actually_spending(self, tmp_path):
        from vyvcode.live import RunMonitor

        cfg = load_config(tmp_path, env={})
        ledger = UsageLedger()
        coder = FakeLLM("openai/kimi-k3")
        ledger.register("coder", coder)
        monitor = RunMonitor(enabled=False, ledger=ledger, cfg=cfg)
        monitor.stage("GRILL")

        coder.metrics = FakeMetrics(prompt=5_000, completion=500)
        line = monitor.activity_line()

        assert "coder kimi-k3" in line
        assert "communicator" not in line

    def test_activity_line_falls_back_to_the_stage_role_before_any_spend(self, tmp_path):
        from vyvcode.live import RunMonitor

        cfg = load_config(tmp_path, env={})
        monitor = RunMonitor(enabled=False, ledger=UsageLedger(), cfg=cfg)
        monitor.stage("GRILL")

        line = monitor.activity_line()

        assert "communicator" in line

    def test_activity_line_moves_on_when_the_spend_does(self, tmp_path):
        from vyvcode.live import RunMonitor

        cfg = load_config(tmp_path, env={})
        ledger = UsageLedger()
        coder = FakeLLM("openai/kimi-k3")
        talker = FakeLLM("openai/gpt-5.6-sol")
        ledger.register("coder", coder)
        ledger.register("communicator", talker)
        monitor = RunMonitor(enabled=False, ledger=ledger, cfg=cfg)
        monitor.stage("GRILL")

        coder.metrics = FakeMetrics(prompt=5_000, completion=500)
        assert "coder kimi-k3" in monitor.activity_line()

        talker.metrics = FakeMetrics(prompt=2_000, completion=200)
        line = monitor.activity_line()

        assert "communicator gpt-5.6-sol" in line
        assert "kimi-k3" not in line


class TestReadability:
    # bright_black and dim render near-invisible on most terminal themes, and
    # the panel is read at a glance while output scrolls past it.
    def test_no_panel_style_falls_below_the_legible_floor(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False)
        monitor.stage("GRILL")
        blocks = monitor._render().renderables
        styles = {
            str(span.style)
            for block in blocks
            if hasattr(block, "spans")
            for span in block.spans
        }

        assert styles
        assert not [s for s in styles if "bright_black" in s or "dim" in s]

    def test_every_letter_of_the_activity_word_stays_legible(self):
        from vyvcode.live import shimmer

        for tick in range(24):
            styles = {str(span.style) for span in shimmer("Harmonizing", tick).spans}

            assert not [s for s in styles if "bright_black" in s or "dim" in s]


class TestProgress:
    def test_stage_progress_renders_a_bar_and_advances(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False)

        monitor.stage("GRILL")
        first = monitor.progress_line()
        monitor.stage("REPORTING")
        later = monitor.progress_line()

        assert "GRILL" in first
        assert first.count("█") < later.count("█")
        assert "REPORTING" in later

    def test_summary_reports_spend_per_model(self, capsys):
        from vyvcode.live import RunMonitor

        printed = []
        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 20_000, 1_000, 0.30))
        monitor = RunMonitor(out=printed.append, enabled=False, ledger=ledger)

        monitor.stage("GRILL")
        monitor.__exit__(None, None, None)

        assert any("kimi-k3" in line and "21.0k" in line for line in printed)


class TestPhaseRows:
    # A row used to read "task task": the phase id was joined to a name that
    # repeated it, and nothing on the row said which agent was working.
    def test_row_names_the_working_agent_and_the_task(self, tmp_path):
        from vyvcode.live import RunMonitor

        cfg = load_config(tmp_path, env={})
        monitor = RunMonitor(enabled=False, cfg=cfg)
        monitor.register("task", "add a health endpoint", role="coder")

        label = monitor._row_label(monitor.phases["task"]).plain

        assert "coder" in label
        assert cfg.roles["coder"].model.split("/")[-1] in label
        assert "add a health endpoint" in label
        assert "task task" not in label

    def test_identifier_is_not_repeated_when_it_is_also_the_name(self, tmp_path):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False, cfg=load_config(tmp_path, env={}))
        monitor.register("task", "task")

        assert monitor._row_label(monitor.phases["task"]).plain.count("task") == 1

    def test_swarm_rows_keep_the_phase_id_beside_the_phase_name(self, tmp_path):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False, cfg=load_config(tmp_path, env={}))
        monitor.register("P1", "auth API")

        label = monitor._row_label(monitor.phases["P1"]).plain

        assert "P1" in label
        assert "auth API" in label

    def test_row_falls_back_to_the_role_alone_without_a_config(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False)
        monitor.register("P1", "auth API", role="reviewer")

        assert "reviewer" in monitor._row_label(monitor.phases["P1"]).plain


class TestUsageColors:
    def test_token_counts_are_styled_apart_from_the_role_and_model(self, tmp_path):
        from vyvcode.live import STYLE_TOKENS, RunMonitor

        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 80_000, 8_400, 0.12))
        ledger.register("planner", FakeLLM("anthropic/claude-fable-5", 100, 4))
        monitor = RunMonitor(enabled=False, ledger=ledger, cfg=load_config(tmp_path, env={}))

        text = monitor.usage_text()
        token_spans = [s for s in text.spans if str(s.style) == STYLE_TOKENS]

        assert "coder kimi-k3" in text.plain
        assert {text.plain[s.start : s.end] for s in token_spans} == {"88.4k", "104"}

    # The right-hand readout used to be one number; a total alone hides which
    # half of the spend is growing, and the two are priced differently.
    def test_session_total_is_split_into_input_and_output(self, tmp_path):
        from vyvcode.live import STYLE_INPUT, STYLE_OUTPUT, STYLE_TOTAL, RunMonitor

        ledger = UsageLedger()
        ledger.register("coder", FakeLLM("openai/kimi-k3", 80_000, 8_400, 0.12))
        monitor = RunMonitor(enabled=False, ledger=ledger)

        text = monitor.usage_text()
        styled = {str(s.style): text.plain[s.start : s.end] for s in text.spans}

        assert "Total: 88.4k  Input: 80.0k  Output: 8.4k" in text.plain
        assert styled[STYLE_TOTAL] == "88.4k"
        assert styled[STYLE_INPUT] == "80.0k"
        assert styled[STYLE_OUTPUT] == "8.4k"

    def test_phase_rows_carry_the_same_split_on_their_right(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False, ledger=UsageLedger())
        monitor.register("P1", "auth API")
        monitor.progress("P1", tokens=5_500, input_tokens=5_000, output_tokens=500)

        cell = [c for c in monitor._render().renderables[0].columns[-1].cells][0]

        assert cell.plain == "Total: 5.5k  Input: 5.0k  Output: 500"

    def test_usage_text_is_empty_before_any_spend(self):
        from vyvcode.live import RunMonitor

        assert RunMonitor(enabled=False, ledger=UsageLedger()).usage_text() is None


class TestModeBadge:
    def test_active_mode_is_named_with_its_elapsed_time(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False, mode="/goal")
        monitor.stage("GRILL")

        line = monitor.mode_line()

        assert line.startswith("◎ /goal active (")
        assert line.endswith("s)")

    def test_no_badge_without_a_mode(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False)
        monitor.stage("GRILL")

        assert monitor.mode_line() == ""

    def test_badge_sits_to_the_right_of_the_activity_line(self):
        from vyvcode.live import RunMonitor

        monitor = RunMonitor(enabled=False, mode="/plan")
        monitor.stage("PLANNING")

        head = monitor._render().renderables[0]
        cells = [cell for row in head.columns for cell in row.cells]

        assert head.columns[1].justify == "right"
        assert any("/plan active" in getattr(cell, "plain", "") for cell in cells)

    def test_pipeline_modes_map_back_to_their_slash_commands(self):
        from vyvcode.commands import command_for_mode

        assert command_for_mode("goal") == "/goal"
        assert command_for_mode("brainstorm") == "/vyvcode:brainstorm"


class TestTaskLabel:
    def test_long_requests_are_trimmed_to_one_line(self):
        from vyvcode.commands import task_label

        label = task_label("add a health endpoint\nthen wire it into the router " * 3)

        assert "\n" not in label
        assert len(label) <= 48
        assert label.endswith("…")

    def test_short_requests_are_kept_whole(self):
        from vyvcode.commands import task_label

        assert task_label("  fix the  parser  ") == "fix the parser"
