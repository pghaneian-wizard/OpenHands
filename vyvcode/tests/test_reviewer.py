"""Reviewer loop: fix waves, malformed verdicts, escalation, minors (§11)."""

import json
from pathlib import Path

import pytest

from vyvcode.config import load_config
from vyvcode.planner import parse_masterplan
from vyvcode.reviewer import (
    VerdictError,
    cluster_issues,
    parse_verdict,
    review_loop,
)
from vyvcode.run_state import Run
from vyvcode.swarm import PhaseOutcome, SwarmOutcome

from tests.test_planner import HEADER, phase_block


def verdict_json(verdict, issues, cycle=1):
    payload = {"verdict": verdict, "cycle": cycle, "issues": issues}
    return f"Review notes here.\n\n```json\n{json.dumps(payload)}\n```"


def issue(id_, severity, files=None, phase="P1"):
    return {
        "id": id_, "severity": severity, "phase": phase,
        "files": files or [], "problem": f"problem {id_}",
        "required_fix": f"fix {id_}",
    }


@pytest.fixture
def env(tmp_path):
    cfg = load_config(tmp_path, env={"VYVCODE_MAX_REVIEW_CYCLES": "5"})
    run = Run(cfg.runs_dir, "demo")
    for state in ("GRILL", "BRIEF", "PLANNING", "EXECUTING", "REVIEWING"):
        run.to(state)
    plan = parse_masterplan(
        HEADER + phase_block("P1", "core", [], ["core.py"], "- run: `true`")
    )
    swarm = SwarmOutcome(
        base_branch="main",
        integration_branch="vyvcode/x/integration",
        integration_dir=Path(tmp_path),
        phases={"P1": PhaseOutcome(phase_id="P1", status="MERGED")},
    )
    return cfg, run, plan, swarm


GREEN = [{"cmd": "true", "passed": True, "output": ""}]


class TestParseVerdict:
    def test_valid_verdict_parses(self):
        verdict = parse_verdict(verdict_json("APPROVED", [issue("R1", "minor")]))

        assert verdict.verdict == "APPROVED"
        assert verdict.minors[0].id == "R1"
        assert verdict.blocking == []

    def test_missing_block_rejected(self):
        with pytest.raises(VerdictError, match="exactly one"):
            parse_verdict("all good, ship it")

    def test_trailing_prose_after_block_rejected(self):
        text = verdict_json("APPROVED", []) + "\nP.S. nice work"

        with pytest.raises(VerdictError, match="final content"):
            parse_verdict(text)

    @pytest.mark.parametrize(
        "body",
        [
            '{"verdict": "APPROVED", "cycle": "one", "issues": []}',
            '{"verdict": "APPROVED", "cycle": 1, "issues": "none"}',
            "[1, 2]",
        ],
    )
    def test_malformed_shapes_raise_verdict_error_not_a_crash(self, body):
        # Only VerdictError is caught by the review loop's re-emit path; any
        # other exception kills a run that already paid for grill+plan+swarm.
        with pytest.raises(VerdictError):
            parse_verdict(f"```json\n{body}\n```")

    def test_null_issues_reads_as_no_issues(self):
        # A common serialization of "nothing to report"; rejecting it would
        # burn a re-emit cycle and then fabricate a synthetic blocker.
        verdict = parse_verdict(
            '```json\n{"verdict": "APPROVED", "cycle": 1, "issues": null}\n```'
        )

        assert verdict.verdict == "APPROVED"
        assert verdict.issues == []

    def test_bad_severity_rejected(self):
        with pytest.raises(VerdictError, match="severity"):
            parse_verdict(verdict_json("APPROVED", [issue("R1", "catastrophic")]))


class TestClustering:
    def test_overlapping_files_merge_disjoint_stay_apart(self):
        from vyvcode.reviewer import Issue

        issues = [
            Issue("R1", "major", "p", files=["a.py"]),
            Issue("R2", "major", "p", files=["a.py", "b.py"]),
            Issue("R3", "major", "p", files=["c.py"]),
            Issue("R4", "major", "p"),
        ]

        clusters = cluster_issues(issues)

        sizes = sorted(len(c) for c in clusters)
        assert sizes == [1, 1, 2]


class TestLoop:
    def test_run_where_every_phase_died_is_not_approved_on_an_empty_suite(self, env):
        # With nothing merged there are no acceptance commands, and all([])
        # is True — a run in which every phase failed used to be reported as
        # a clean success with zero commands executed.
        cfg, run, plan, swarm = env
        swarm.phases["P1"] = PhaseOutcome(phase_id="P1", status="RETRY_EXHAUSTED")

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda prompt: verdict_json("APPROVED", [], cycle=1),
            fix_runner=lambda prompt, cid: "fixed",
            suite_runner=lambda: [],
        )

        assert outcome.approved is False

    def test_two_changes_required_then_approved_drives_two_fix_waves(self, env):
        cfg, run, plan, swarm = env
        reviews = [
            verdict_json("CHANGES_REQUIRED", [
                issue("R1", "major", ["a.py"]),
                issue("R2", "blocker", ["b.py"]),
                issue("R3", "minor"),
            ], cycle=1),
            verdict_json("CHANGES_REQUIRED", [issue("R4", "major", ["a.py"])], cycle=2),
            verdict_json("APPROVED", [issue("R5", "minor")], cycle=3),
        ]
        fixes = []

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda task: reviews.pop(0),
            fix_runner=lambda prompt, cid: fixes.append((cid, prompt)),
            suite_runner=lambda: list(GREEN),
        )

        assert outcome.approved is True
        assert outcome.cycles == 3
        assert len([c for c, _ in fixes if c.startswith("c1-")]) == 2
        assert len([c for c, _ in fixes if c.startswith("c2-")]) == 1
        assert [m.id for m in outcome.minors] == ["R3", "R5"]
        assert (run.dir / "review" / "cycle-3" / "verdict.json").is_file()
        assert run.state == "REVIEWING"

    def test_malformed_verdict_retries_then_synthesizes_blocker(self, env):
        cfg, run, plan, swarm = env
        reviews = [
            "no json here",
            "still no json",  # the re-emit retry
            verdict_json("APPROVED", [], cycle=2),
        ]
        prompts = []
        fixes = []

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda task: prompts.append(task) or reviews.pop(0),
            fix_runner=lambda prompt, cid: fixes.append(prompt),
            suite_runner=lambda: list(GREEN),
        )

        assert outcome.approved is True
        assert outcome.history[0].synthetic is True
        assert outcome.history[0].issues[0].id == "R0"
        assert "re-emit" in prompts[1].lower()
        assert len(fixes) == 1  # the synthetic blocker got a fix coder

    def test_cycle_cap_escalates_with_outstanding_issues(self, env):
        cfg, run, plan, swarm = env
        cfg = load_config(cfg.project_root, env={"VYVCODE_MAX_REVIEW_CYCLES": "2"})

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda task: verdict_json(
                "CHANGES_REQUIRED", [issue("R9", "blocker", ["x.py"])]
            ),
            fix_runner=lambda prompt, cid: None,
            suite_runner=lambda: list(GREEN),
        )

        assert outcome.escalated is True
        assert outcome.approved is False
        assert outcome.cycles == 2
        assert [i.id for i in outcome.outstanding] == ["R9"]
        assert run.state == "ESCALATED"

    def test_minors_never_block(self, env):
        cfg, run, plan, swarm = env
        fixes = []

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda task: verdict_json(
                "APPROVED", [issue("R1", "minor"), issue("R2", "minor")]
            ),
            fix_runner=lambda prompt, cid: fixes.append(cid),
            suite_runner=lambda: list(GREEN),
        )

        assert outcome.approved is True
        assert fixes == []
        assert len(outcome.minors) == 2

    def test_approved_with_red_suite_forces_fix_cycle(self, env):
        cfg, run, plan, swarm = env
        red = [{"cmd": "pytest", "passed": False, "output": "1 failed"}]
        suites = [list(red), list(GREEN), list(GREEN)]
        reviews = [
            verdict_json("APPROVED", [], cycle=1),
            verdict_json("APPROVED", [], cycle=2),
        ]
        fixes = []

        outcome = review_loop(
            cfg, run, plan, swarm, out=lambda _: None,
            reviewer_runner=lambda task: reviews.pop(0),
            fix_runner=lambda prompt, cid: fixes.append(prompt),
            suite_runner=lambda: suites.pop(0),
        )

        assert outcome.approved is True
        assert outcome.cycles == 2
        assert len(fixes) == 1
        assert "suite is red" in fixes[0]
