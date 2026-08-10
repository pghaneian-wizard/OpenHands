"""Golden-file test: synthetic run artifacts in → report.md matches fixture (§14)."""

from pathlib import Path

from vyvcode.config import load_config
from vyvcode.planner import parse_masterplan
from vyvcode.report import render_report
from vyvcode.reviewer import Issue, ReviewOutcome
from vyvcode.run_state import Run
from vyvcode.swarm import PhaseOutcome, SwarmOutcome

from tests.test_planner import HEADER, phase_block

GOLDEN = Path(__file__).parent / "golden" / "report.md"

BRIEF = """\
# BRIEF
goal: build a todo CLI
context: fresh repo
decisions:
  - storage: sqlite
assumed:
  - api: CLI-only (cap-forced recommendation)
constraints:
  - python
success_criteria:
  - todo add persists an item
"""


def synthetic_env(tmp_path):
    cfg = load_config(tmp_path, env={})
    run = Run(cfg.runs_dir, "todo cli", run_id="run_20260810_120000_todo-cli")
    (run.dir / "BRIEF.md").write_text(BRIEF)
    plan = parse_masterplan(
        HEADER
        + phase_block("P1", "core", [], ["core.py"], "- run: `test -f core.py`")
        + phase_block("P2", "auth", ["P1"], ["auth.py"], "- run: `test -f auth.py`")
        + phase_block("F", "final", ["P2"], ["README.md"], "- run: `pytest -q`")
    )
    swarm = SwarmOutcome(
        base_branch="main",
        integration_branch="vyvcode/run_20260810_120000_todo-cli/integration",
        integration_dir=tmp_path,
        phases={
            "P1": PhaseOutcome("P1", status="MERGED", attempts=1,
                               report={"blocked_on": ["auth schema owned by P2"]}),
            "P2": PhaseOutcome("P2", status="RETRY_EXHAUSTED", attempts=2,
                               notes=["acceptance failed: test -f auth.py"]),
            "F": PhaseOutcome("F", status="SKIPPED"),
        },
    )
    review = ReviewOutcome(
        approved=True, escalated=False, cycles=2,
        history=[], outstanding=[],
        minors=[Issue("R3", "minor", "naming nit", files=["core.py"])],
        suite=[{"cmd": "pytest -q", "passed": True, "output": ""}],
    )
    return cfg, run, plan, swarm, review


class TestBriefParsing:
    def test_flag_named_assumption_survives_and_unindented_items_are_found(
        self, tmp_path
    ):
        from vyvcode.report import _brief_list

        brief = "assumed:\n- --dry-run defaults to on\n  - api: CLI-only\n"

        assert _brief_list(brief, "assumed") == [
            "--dry-run defaults to on",
            "api: CLI-only",
        ]


class TestRedaction:
    def test_secret_carried_in_the_brief_never_reaches_the_report(
        self, tmp_path
    ):
        # The BRIEF is model-authored from an interview whose explorer reads
        # the repo, so a key can ride in from a .env the explorer opened. The
        # report is written to disk and printed to the terminal.
        cfg, run, plan, swarm, review = synthetic_env(tmp_path)
        (run.dir / "BRIEF.md").write_text(
            BRIEF.replace("goal: build a todo CLI",
                          "goal: ship with OPENAI_API_KEY=sk-live-abc123def456"),
            encoding="utf-8",
        )

        report = render_report(cfg, run, plan, swarm, review)

        assert "sk-live-abc123def456" not in report


class TestReport:
    def test_report_matches_golden_fixture(self, tmp_path):
        cfg, run, plan, swarm, review = synthetic_env(tmp_path)

        rendered = render_report(cfg, run, plan, swarm, review)

        assert rendered == GOLDEN.read_text()

    def test_report_backs_every_section_with_artifacts(self, tmp_path):
        cfg, run, plan, swarm, review = synthetic_env(tmp_path)

        rendered = render_report(cfg, run, plan, swarm, review)

        assert "goal: build a todo CLI" in rendered
        assert "| P2 | auth | RETRY_EXHAUSTED |" in rendered
        assert "assumed: api: CLI-only" in rendered
        assert "dead phase P2" in rendered
        assert "[R3] naming nit" in rendered
        assert "P1 blocked_on: auth schema owned by P2" in rendered
        assert "git switch vyvcode/run_20260810_120000_todo-cli/integration" in rendered
