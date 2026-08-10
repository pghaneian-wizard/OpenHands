"""B14 loop: decision table, guard, timeout, crash streak, stop semantics."""

import json
import subprocess
import sys

import pytest

from vyvcode import research
from vyvcode.config import load_config
from vyvcode.research import (
    ResearchSession,
    TrainResult,
    request_stop,
    run_experiment,
    run_loop,
    run_training,
    tsv_append,
    tsv_init,
)

from tests.test_research import make_ar_checkout


def prepared(tmp_path, env=None):
    make_ar_checkout(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "autoresearch/test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    research._exclude_untracked(tmp_path)
    session = ResearchSession(tmp_path, tag="test")
    session.state["baseline"] = {"sha": sha, "val_bpb": 0.9931, "spread": 0.001}
    session.state["best"] = {"sha": sha, "val_bpb": 0.9931, "exp_id": 0}
    session.state["device_total_mb"] = 97887.0
    session.save()
    tsv_init(session.tsv_path)
    tsv_append(session.tsv_path, sha, 0.9931, 17.8, "keep", "baseline")
    cfg = load_config(tmp_path, env=env or {})
    return cfg, session, sha


def editing_researcher(ar_dir, smuggle=(), smuggle_on_reprompt=False):
    def fn(task):
        fn.calls.append(task)
        if task.startswith("<program_md>"):
            (ar_dir / "train.py").write_text(f"# edit {len(fn.calls)}\n")
            for name in smuggle:
                (ar_dir / name).write_text("smuggled\n")
        elif "Harness guard" in task and smuggle_on_reprompt:
            for name in smuggle:
                (ar_dir / name).write_text("smuggled again\n")
        return f"did work\nHYPOTHESIS: change number {len(fn.calls)}"

    fn.calls = []
    fn.last_tokens = 123
    return fn


def scripted_trains(values):
    """values: floats (ok) or 'crash' markers."""
    queue = list(values)

    def fn(log_path, on_start):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("scripted run log\n")
        if on_start:
            on_start(4242)
        value = queue.pop(0)
        if value == "crash":
            return TrainResult("crash", "ZeroDivisionError: boom",
                               log_path=log_path)
        return TrainResult(
            "ok", fields={"val_bpb": value, "peak_vram_mb": 18000.0},
            log_path=log_path,
        )

    fn.queue = queue
    return fn


def head_sha(path):
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestDecisionTable:
    def test_strictly_lower_keeps_and_advances_branch(self, tmp_path):
        cfg, session, base_sha = prepared(tmp_path)
        run_experiment(cfg, tmp_path, session, editing_researcher(tmp_path),
                       scripted_trains([0.9800]), out=lambda _: None)

        assert session.state["best"]["val_bpb"] == 0.9800
        assert session.state["best"]["sha"] == head_sha(tmp_path) != base_sha
        assert session.state["counts"] == {"run": 1, "kept": 1, "discarded": 0,
                                           "crashed": 0}
        last = session.tsv_path.read_text().splitlines()[-1]
        assert "\tkeep\tchange number 1" in last
        assert "\t0.980000\t" in last

    def test_tie_is_discard_and_resets(self, tmp_path):
        cfg, session, base_sha = prepared(tmp_path)
        run_experiment(cfg, tmp_path, session, editing_researcher(tmp_path),
                       scripted_trains([0.9931]), out=lambda _: None)

        assert session.state["best"]["sha"] == base_sha
        assert head_sha(tmp_path) == base_sha
        assert session.state["counts"]["discarded"] == 1

    def test_epsilon_requires_margin(self, tmp_path):
        cfg, session, base_sha = prepared(
            tmp_path, env={"VYVCODE_AR_EPSILON": "0.002"}
        )
        run_experiment(cfg, tmp_path, session, editing_researcher(tmp_path),
                       scripted_trains([0.9921]), out=lambda _: None)

        assert session.state["counts"]["discarded"] == 1  # only 0.001 better

    def test_crash_logs_zeros_resets_and_bumps_streak(self, tmp_path):
        cfg, session, base_sha = prepared(tmp_path)
        run_experiment(cfg, tmp_path, session, editing_researcher(tmp_path),
                       scripted_trains(["crash"]), out=lambda _: None)

        last = session.tsv_path.read_text().splitlines()[-1]
        assert "\t0.000000\t0.0\tcrash\t" in last
        assert "(ZeroDivisionError: boom)" in last
        assert head_sha(tmp_path) == base_sha
        assert session.state["crash_streak"] == 1
        assert (session.dir / "logs" / "exp_001.log.gz").is_file()

    def test_crash_tail_reaches_next_researcher_context(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        researcher = editing_researcher(tmp_path)
        run_experiment(cfg, tmp_path, session, researcher,
                       scripted_trains(["crash"]), out=lambda _: None)
        run_experiment(cfg, tmp_path, session, researcher,
                       scripted_trains([0.99]), out=lambda _: None)

        assert "last_crash_log_tail" in researcher.calls[1]
        assert "scripted run log" in researcher.calls[1]


class TestGuard:
    def test_planted_prepare_edit_reverted_then_experiment_proceeds(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        researcher = editing_researcher(tmp_path, smuggle=("prepare.py",))

        run_experiment(cfg, tmp_path, session, researcher,
                       scripted_trains([0.98]), out=lambda _: None)

        assert (tmp_path / "prepare.py").read_text() == "# fixed\n"
        assert any("Harness guard" in c for c in researcher.calls)
        assert "guard_violation" in (session.dir / "journal.md").read_text()
        assert session.state["counts"]["kept"] == 1

    def test_second_violation_abandons_experiment(self, tmp_path):
        cfg, session, base_sha = prepared(tmp_path)
        researcher = editing_researcher(
            tmp_path, smuggle=("prepare.py", "pyproject.toml"),
            smuggle_on_reprompt=True,
        )
        trains = scripted_trains([0.90])

        run_experiment(cfg, tmp_path, session, researcher, trains,
                       out=lambda _: None)

        assert len(trains.queue) == 1  # training never ran
        assert head_sha(tmp_path) == base_sha
        assert (tmp_path / "prepare.py").read_text() == "# fixed\n"
        assert not (tmp_path / "pyproject.toml").exists()  # stray file cleaned
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        assert "pyproject.toml" not in committed  # cannot be smuggled into git
        last = session.tsv_path.read_text().splitlines()[-1]
        assert "\tdiscard\t" in last
        assert session.state["counts"]["discarded"] == 1
        records = session.experiments()
        assert records[-1]["reason"] == "guard violation"


class TestTimeout:
    def test_timeout_sigkills_process_group(self, tmp_path):
        make_ar_checkout(tmp_path)
        stub = tmp_path / "stubborn.py"
        stub.write_text(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('starting', flush=True)\n"
            "time.sleep(60)\n"
        )
        pids = []

        def popen(argv, **kwargs):
            proc = subprocess.Popen([sys.executable, str(stub)], **kwargs)
            pids.append(proc.pid)
            return proc

        result = run_training(tmp_path, tmp_path / "run.log", timeout_min=0.02,
                              popen=popen)

        assert result.status == "crash"
        assert result.reason == "timeout"
        gone = subprocess.run(["ps", "-p", str(pids[0])], capture_output=True)
        assert gone.returncode != 0  # SIGKILL took the group down


class TestLoopControl:
    def test_crash_streak_of_three_triggers_strategist_and_reset(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        triggers = []

        run_loop(
            cfg, tmp_path, session, out=lambda _: None,
            max_experiments=3,
            researcher_fn=editing_researcher(tmp_path),
            train_fn=scripted_trains(["crash", "crash", "crash"]),
            strategist_fn=lambda cfg, s, trigger, out: triggers.append(trigger),
            gpu_check=lambda: True,
        )

        assert triggers == ["crash_streak"]
        assert session.reload().state["crash_streak"] == 0
        assert session.state["counts"]["crashed"] == 3

    def test_graceful_stop_finishes_in_flight_experiment(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        trains = scripted_trains([0.98, 0.97, 0.96])

        def stopping_researcher(task):
            if task.startswith("<program_md>"):
                stopping_researcher.context_calls += 1
                n = stopping_researcher.context_calls
                (tmp_path / "train.py").write_text(f"# unique edit {n}\n")
                if n == 2:
                    request_stop(tmp_path)  # graceful, mid-experiment 2
            return "did work\nHYPOTHESIS: unique change"

        stopping_researcher.context_calls = 0
        stopping_researcher.last_tokens = 0

        run_loop(cfg, tmp_path, session, out=lambda _: None,
                 researcher_fn=stopping_researcher, train_fn=trains,
                 gpu_check=lambda: True)

        assert session.reload().state["counts"]["run"] == 2  # exp 2 completed
        assert len(trains.queue) == 1

    def test_stop_now_discards_in_flight(self, tmp_path):
        cfg, session, base_sha = prepared(tmp_path)

        def killing_train(log_path, on_start):
            log_path.write_text("partial\n")
            if on_start:
                on_start(4242)
            request_stop(tmp_path, now=True)
            return TrainResult("crash", "killed", log_path=log_path)

        run_loop(cfg, tmp_path, session, out=lambda _: None,
                 researcher_fn=editing_researcher(tmp_path),
                 train_fn=killing_train, gpu_check=lambda: True)

        session.reload()
        assert session.state["counts"]["run"] == 0
        assert head_sha(tmp_path) == base_sha
        assert session.experiments()[-1]["decision"] == "aborted"
        assert len(session.tsv_path.read_text().splitlines()) == 2  # header+baseline

    def test_gpu_gone_pauses_loop(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        printed = []

        run_loop(cfg, tmp_path, session, out=printed.append,
                 researcher_fn=editing_researcher(tmp_path),
                 train_fn=scripted_trains([]), gpu_check=lambda: False)

        assert any("GPU" in p for p in printed)
        assert session.state["counts"]["run"] == 0

    def test_harness_error_journals_and_continues(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)

        def flaky_researcher(task):
            flaky_researcher.calls += 1
            if flaky_researcher.calls == 1:
                raise RuntimeError("transient explosion")
            return editing_researcher(tmp_path)(task)

        flaky_researcher.calls = 0
        flaky_researcher.last_tokens = 0

        run_loop(cfg, tmp_path, session, out=lambda _: None,
                 max_experiments=2,
                 researcher_fn=flaky_researcher,
                 train_fn=scripted_trains([0.98]), gpu_check=lambda: True)

        session.reload()
        assert "harness_error" in (session.dir / "journal.md").read_text()
        assert session.state["counts"]["run"] == 2
        assert session.state["counts"]["kept"] == 1
