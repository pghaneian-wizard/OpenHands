"""Swarm scheduler: waves, conflict path, lie detection, retry exhaustion (§10)."""

import json
import subprocess
import threading

import pytest

from vyvcode.config import load_config
from vyvcode.planner import parse_masterplan
from vyvcode.run_state import Run
from vyvcode.swarm import SwarmError, execute_swarm

from tests.test_planner import HEADER, phase_block


def make_repo(path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=path, check=True, capture_output=True,
        )

    git("init", "-b", "main")
    (path / "README.md").write_text("demo\n")
    git("add", ".")
    git("commit", "-m", "init")
    return git


def commit_all(worktree, message="work"):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", "."],
        cwd=worktree, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message],
        cwd=worktree, check=True, capture_output=True,
    )


def write_report(worktree, phase_id, status="done"):
    (worktree / "PHASE_REPORT.json").write_text(
        json.dumps({"phase_id": phase_id, "status": status, "commits": 1,
                    "files_changed": [], "acceptance": [], "notes": [],
                    "blocked_on": []})
    )


DIAMOND = (
    HEADER
    + phase_block("P1", "core", [], ["core.py"], "- run: `test -f core.py`")
    + phase_block("P2", "auth", ["P1"], ["auth.py"], "- run: `test -f auth.py`")
    + phase_block("P3", "api", ["P1"], ["api.py"], "- run: `test -f api.py`")
    + phase_block("P4", "wiring", ["P2", "P3"], ["main.py"], "- run: `test -f main.py`")
)


@pytest.fixture
def repo(tmp_path):
    make_repo(tmp_path)
    return tmp_path


def make_run(cfg, goal="demo"):
    run = Run(cfg.runs_dir, goal)
    for state in ("GRILL", "BRIEF", "PLANNING", "EXECUTING"):
        run.to(state)
    (run.dir / "BRIEF.md").write_text(
        "# BRIEF\ngoal: demo\nconstraints:\n  - python\nsuccess_criteria:\n  - works\n"
    )
    return run


class TestScheduler:
    def test_diamond_executes_in_waves_with_two_coder_cap(self, repo):
        cfg = load_config(repo, env={"VYVCODE_MAX_PARALLEL_CODERS": "2"})
        run = make_run(cfg)
        plan = parse_masterplan(DIAMOND)
        lock = threading.Lock()
        active = 0
        max_active = 0
        started = []

        def runner(prompt, worktree, phase_id):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                started.append(phase_id)
            filename = {"P1": "core.py", "P2": "auth.py",
                        "P3": "api.py", "P4": "main.py"}[phase_id]
            (worktree / filename).write_text(f"# {phase_id}\n")
            commit_all(worktree, f"{phase_id} work")
            write_report(worktree, phase_id)
            with lock:
                active -= 1
            return "done"

        outcome = execute_swarm(cfg, run, plan, out=lambda _: None, coder_runner=runner)

        assert sorted(outcome.merged) == ["P1", "P2", "P3", "P4"]
        assert max_active <= 2
        assert started[0] == "P1"
        assert set(started[1:3]) == {"P2", "P3"}
        assert started[3] == "P4"
        listing = subprocess.run(
            ["git", "ls-tree", "--name-only", outcome.integration_branch],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert {"core.py", "auth.py", "api.py", "main.py"} <= set(listing)

    def test_dirty_base_tree_refused(self, repo):
        cfg = load_config(repo, env={})
        run = make_run(cfg)
        (repo / "README.md").write_text("uncommitted edit\n")

        with pytest.raises(SwarmError, match="uncommitted"):
            execute_swarm(cfg, run, parse_masterplan(DIAMOND), out=lambda _: None,
                          coder_runner=lambda *a: "unused")

    def test_conflict_dispatches_integration_coder(self, repo):
        cfg = load_config(repo, env={"VYVCODE_MAX_PARALLEL_CODERS": "2"})
        run = make_run(cfg)
        plan = parse_masterplan(
            HEADER
            + phase_block("P1", "core", [], ["shared.txt"], "- run: `test -f shared.txt`")
            + phase_block("P2", "a", ["P1"], ["a.py"], "- run: `test -f a.py`")
            + phase_block("P3", "b", ["P1"], ["b.py"], "- run: `test -f b.py`")
        )
        integration_calls = []
        # Both P2 and P3 must be cut from the same integration point for their
        # illegal shared.txt rewrites to conflict; the barrier keeps both in
        # flight until each has started.
        barrier = threading.Barrier(2, timeout=30)
        files = {"P2": "a.py", "P3": "b.py"}

        def runner(prompt, worktree, phase_id):
            if phase_id == "P1":
                (worktree / "shared.txt").write_text("base\n")
            else:
                barrier.wait()
                (worktree / "shared.txt").write_text(f"from-{phase_id}\n")
                (worktree / files[phase_id]).write_text("x\n")
            commit_all(worktree, phase_id)
            write_report(worktree, phase_id)
            return "done"

        def integration_runner(prompt, worktree, phase_id):
            integration_calls.append(phase_id)
            (worktree / "shared.txt").write_text("merged\n")
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                cwd=worktree, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "--no-edit"],
                cwd=worktree, check=True, capture_output=True,
            )
            return "resolved"

        outcome = execute_swarm(
            cfg, run, plan, out=lambda _: None,
            coder_runner=runner, integration_runner=integration_runner,
        )

        assert sorted(outcome.merged) == ["P1", "P2", "P3"]
        assert len(integration_calls) == 1

    def test_lying_agent_caught_then_retry_exhausted_keeps_run_alive(self, repo):
        cfg = load_config(repo, env={"VYVCODE_MAX_PARALLEL_CODERS": "2"})
        run = make_run(cfg)
        plan = parse_masterplan(
            HEADER
            + phase_block("P1", "core", [], ["core.py"], "- run: `test -f core.py`")
            + phase_block("P2", "dep", ["P1"], ["dep.py"], "- run: `test -f dep.py`")
        )
        prompts = []

        def runner(prompt, worktree, phase_id):
            prompts.append((phase_id, prompt))
            # Claims done, commits nothing that satisfies acceptance: a lie.
            (worktree / "unrelated.txt").write_text("noise\n")
            commit_all(worktree, "noise")
            write_report(worktree, phase_id, status="done")
            return "totally passed all tests"

        outcome = execute_swarm(cfg, run, plan, out=lambda _: None, coder_runner=runner)

        assert outcome.phases["P1"].status == "RETRY_EXHAUSTED"
        assert outcome.phases["P1"].attempts == 2
        assert outcome.phases["P2"].status == "SKIPPED"
        assert outcome.merged == []
        retry_prompt = [p for pid, p in prompts if pid == "P1"][1]
        assert "previous attempt failed" in retry_prompt
        assert "acceptance failed" in retry_prompt

    def test_retry_succeeds_on_second_attempt(self, repo):
        cfg = load_config(repo, env={})
        run = make_run(cfg)
        plan = parse_masterplan(
            HEADER + phase_block("P1", "core", [], ["core.py"], "- run: `test -f core.py`")
        )
        attempts = []

        def runner(prompt, worktree, phase_id):
            attempts.append(prompt)
            if len(attempts) == 2:
                (worktree / "core.py").write_text("ok\n")
            else:
                (worktree / "wrong.txt").write_text("nope\n")
            commit_all(worktree, "attempt")
            write_report(worktree, phase_id)
            return "done"

        outcome = execute_swarm(cfg, run, plan, out=lambda _: None, coder_runner=runner)

        assert outcome.phases["P1"].status == "MERGED"
        assert outcome.phases["P1"].attempts == 2
        assert len(attempts) == 2
