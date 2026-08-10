"""Pipeline glue: state walk to DONE on scripted stages, artifacts on disk."""

from vyvcode.config import load_config
from vyvcode.pipeline import run_pipeline
from vyvcode.run_state import latest_run
from tests.fakes import ScriptedChatLLM

from tests.test_planner import valid_plan

TERMINAL = """\
FRONTIER-EMPTY

# BRIEF
goal: build the demo
context: empty repo
decisions:
  - stack: python
constraints:
  - none
success_criteria:
  - tests pass
"""


class TestPipelineGlue:
    def test_walks_to_done_with_all_artifacts(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        communicator = ScriptedChatLLM([TERMINAL])
        printed = []

        closing = run_pipeline(
            cfg,
            mode="goal",
            raw_text="tiny goal",
            ask_user=lambda: "all recommended",
            out=printed.append,
            communicator=communicator,
            planner_runner=lambda task: valid_plan(),
            swarm_fn=lambda cfg, run, plan, out: {"phases": {}},
            review_fn=lambda cfg, run, plan, swarm, out: {"verdict": "APPROVED"},
            report_fn=lambda cfg, run, plan, swarm, review: "# report\nall good\n",
        )

        run = latest_run(cfg.runs_dir)
        assert run.state == "DONE"
        assert closing == f"run {run.run_id}: DONE"
        for artifact in (
            "input.raw.md", "input.opt.md", "grill.md", "BRIEF.md",
            "MasterPlan.md", "report.md",
        ):
            assert (run.dir / artifact).is_file(), artifact
        assert "# report" in (run.dir / "report.md").read_text()
        states = [s for _, s in run._data["history"]]
        assert states == [
            "GRILL", "BRIEF", "PLANNING", "EXECUTING", "REVIEWING",
            "REPORTING", "DONE",
        ]

    def test_plan_rejection_aborts_run_with_errors_shown(self, tmp_path):
        cfg = load_config(tmp_path, env={})

        closing = run_pipeline(
            cfg,
            mode="goal",
            raw_text="tiny goal",
            ask_user=lambda: "",
            out=lambda _: None,
            communicator=ScriptedChatLLM([TERMINAL]),
            planner_runner=lambda task: "not a plan at all",
        )

        assert "plan rejected twice" in closing
        assert latest_run(cfg.runs_dir).state == "ABORTED"

    def test_failure_before_the_first_transition_still_aborts_the_run(
        self, tmp_path, monkeypatch
    ):
        # Building the communicator happens before any state transition; if it
        # throws, the run must not be left stranded in IDLE, blocking every
        # later pipeline command for the rest of the session.
        import pytest

        import vyvcode.pipeline as pipeline_mod
        from vyvcode.run_state import active_run

        cfg = load_config(tmp_path, env={})

        def exploding_llm_for(role, cfg, **kwargs):
            raise RuntimeError("bad model config")

        monkeypatch.setattr(pipeline_mod, "llm_for", exploding_llm_for)

        with pytest.raises(RuntimeError, match="bad model config"):
            run_pipeline(
                cfg,
                mode="goal",
                raw_text="tiny goal",
                ask_user=lambda: "",
                out=lambda _: None,
            )

        assert latest_run(cfg.runs_dir).state == "ABORTED"
        assert active_run(cfg.runs_dir) is None

    def test_eof_at_answers_prompt_aborts_gracefully(self, tmp_path):
        from tests.test_grill import ROUND_1

        cfg = load_config(tmp_path, env={})

        def eof_ask_user():
            raise EOFError

        closing = run_pipeline(
            cfg,
            mode="goal",
            raw_text="tiny goal",
            ask_user=eof_ask_user,
            out=lambda _: None,
            communicator=ScriptedChatLLM([ROUND_1, TERMINAL]),
            explorer=lambda q: "n/a",
        )

        assert "aborted" in closing
        assert latest_run(cfg.runs_dir).state == "ABORTED"

    def test_unexpected_crash_marks_run_aborted_not_stranded(self, tmp_path):
        import pytest

        cfg = load_config(tmp_path, env={})

        def exploding_planner(task):
            raise RuntimeError("boom mid-pipeline")

        with pytest.raises(RuntimeError, match="boom mid-pipeline"):
            run_pipeline(
                cfg,
                mode="goal",
                raw_text="tiny goal",
                ask_user=lambda: "",
                out=lambda _: None,
                communicator=ScriptedChatLLM([TERMINAL]),
                planner_runner=exploding_planner,
            )

        assert latest_run(cfg.runs_dir).state == "ABORTED"
