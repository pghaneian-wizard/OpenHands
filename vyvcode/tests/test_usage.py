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
