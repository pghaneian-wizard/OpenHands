"""MasterPlan parser/validator rejections and the generate-with-retry loop (§9)."""

import pytest

from vyvcode.config import load_config
from vyvcode.planner import PlanValidationError, generate_plan, parse_masterplan
from vyvcode.run_state import Run

HEADER = (
    "# MasterPlan — demo\nrun: run_x\nstack: python\n\n"
    "## Stack rationale\n- python: fits the brief\n"
)

ACCEPT = "- run: `true` → expect: exit 0"


def phase_block(pid, name, deps, files, acceptance=ACCEPT):
    deps_yaml = ", ".join(deps)
    files_yaml = ", ".join(files)
    return f"""
## Phase {pid}: {name}
```yaml
id: {pid}
depends_on: [{deps_yaml}]
worktree_branch: vyvcode/run_x/{pid}-{name}
est_files: [{files_yaml}]
```
### Objective
Build {name}.
### Deliverables
The {name} unit.
### Interfaces provided
def api_{pid}() -> None
### Steps
1. build it
### Acceptance
{acceptance}
"""


def valid_plan():
    return (
        HEADER
        + phase_block("P1", "core", [], ["src/core.py"])
        + phase_block("P2", "auth", ["P1"], ["src/auth.py"])
        + phase_block("P3", "api", ["P1"], ["src/api.py"])
        + phase_block("P4", "wiring", ["P2", "P3"], ["src/main.py"])
        + phase_block("F", "final", ["P4"], ["README.md"])
    )


class TestParser:
    def test_valid_plan_parses_with_topo_order(self):
        plan = parse_masterplan(valid_plan())

        assert set(plan.phases) == {"P1", "P2", "P3", "P4", "F"}
        assert plan.order[0] == "P1"
        assert plan.order.index("P4") > plan.order.index("P2")
        assert plan.order.index("P4") > plan.order.index("P3")
        assert plan.order[-1] == "F"
        assert plan.phases["P2"].acceptance == ["true"]
        assert "def api_P1" in plan.phases["P1"].interfaces

    def test_dependency_cycle_rejected(self):
        text = (
            HEADER
            + phase_block("P1", "a", ["P2"], ["a.py"])
            + phase_block("P2", "b", ["P1"], ["b.py"])
        )

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("cycle" in e for e in exc.value.errors)

    def test_repeated_dependency_is_not_a_cycle(self):
        # Indegree counted every element of depends_on but was decremented once
        # per resolved dependency, so a duplicate stalled the topological sort
        # and the plan was rejected for a cycle that does not exist.
        text = (
            HEADER
            + phase_block("P1", "a", [], ["a.py"])
            + phase_block("P2", "b", ["P1", "P1"], ["b.py"])
        )

        plan = parse_masterplan(text)

        assert plan.order == ["P1", "P2"]

    def test_run_command_in_prose_is_not_an_acceptance_command(self):
        # Acceptance commands are executed with shell=True; only real list
        # items may become one, never a phrase quoting a command mid-sentence.
        prose = (
            "Do NOT - run: `rm -rf /tmp/wipe-me` under any circumstances.\n"
            "- run: `true` → expect: exit 0\n"
        )
        text = HEADER + phase_block("P1", "a", [], ["a.py"], acceptance=prose)

        plan = parse_masterplan(text)

        assert plan.phases["P1"].acceptance == ["true"]

    def test_parallel_phases_sharing_files_rejected(self):
        text = (
            HEADER
            + phase_block("P1", "core", [], ["core.py"])
            + phase_block("P2", "a", ["P1"], ["shared.py"])
            + phase_block("P3", "b", ["P1"], ["shared.py"])
        )

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("share files" in e and "shared.py" in e for e in exc.value.errors)

    def test_dependent_phases_may_share_files(self):
        text = (
            HEADER
            + phase_block("P1", "core", [], ["core.py"])
            + phase_block("P2", "ext", ["P1"], ["core.py"])
        )

        plan = parse_masterplan(text)

        assert plan.order == ["P1", "P2"]

    def test_missing_acceptance_rejected(self):
        text = HEADER + phase_block("P1", "core", [], ["core.py"], acceptance="none")

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("Acceptance" in e for e in exc.value.errors)

    def test_phase_id_escaping_the_run_directory_rejected(self):
        # The phase id becomes a path component for the worktree and the
        # per-phase artifact directory, so it must not be able to climb out.
        block = phase_block("P1", "a", [], ["a.py"]).replace("id: P1", "id: ../../out")
        text = HEADER + block

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("id" in e for e in exc.value.errors)

    def test_est_files_escaping_repo_rejected(self):
        text = HEADER + phase_block("P1", "core", [], ["../evil.py"])

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("escapes repo" in e for e in exc.value.errors)

    def test_unknown_dependency_rejected(self):
        text = HEADER + phase_block("P1", "core", ["P9"], ["core.py"])

        with pytest.raises(PlanValidationError) as exc:
            parse_masterplan(text)

        assert any("unknown dependency P9" in e for e in exc.value.errors)


class TestGenerate:
    def test_invalid_then_valid_regenerates_once_with_errors_quoted(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        run = Run(cfg.runs_dir, "demo")
        outputs = [HEADER + phase_block("P1", "core", ["P9"], ["core.py"]), valid_plan()]
        tasks = []

        def runner(task):
            tasks.append(task)
            return outputs[len(tasks) - 1]

        plan = generate_plan(cfg, run, "# BRIEF\ngoal: demo\n", runner=runner)

        assert len(tasks) == 2
        assert "unknown dependency P9" in tasks[1]
        assert (run.dir / "MasterPlan.md").read_text() == plan.raw
        committed = cfg.project_root / "docs" / "vyvcode" / "plans"
        assert (committed / f"{run.run_id}-MasterPlan.md").is_file()

    def test_second_failure_aborts_with_errors(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        run = Run(cfg.runs_dir, "demo")
        bad = HEADER + phase_block("P1", "core", [], ["core.py"], acceptance="none")

        with pytest.raises(PlanValidationError):
            generate_plan(cfg, run, "# BRIEF\ngoal: demo\n", runner=lambda t: bad)
