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
