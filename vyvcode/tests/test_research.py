"""Autoresearch B13: config, alias, fingerprint, LOCK, parser, tsv, setup."""

import json
import subprocess

import pytest

from vyvcode import research
from vyvcode.commands import parse_line
from vyvcode.config import load_config
from vyvcode.research import (
    ResearchError,
    ResearchSession,
    TrainResult,
    acquire_lock,
    ensure_strategy_block,
    is_ar_checkout,
    parse_summary,
    preflight,
    read_strategy_block,
    release_lock,
    replace_strategy_block,
    setup,
    tsv_append,
    tsv_init,
)

GOLDEN_SUMMARY = """\
step 00612 (100.0%) | loss: 3.021 | lrm: 0.00 | dt: 480ms
Time budget: 300s
val_bpb:          0.991234
training_seconds: 300.2
total_seconds:    341.7
peak_vram_mb:     18234.5
mfu_percent:      41.20
total_tokens_M:   312.4
num_steps:        612
num_params_M:     561.2
depth:            8
"""


def make_ar_checkout(path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=path, check=True, capture_output=True,
        )

    git("init", "-b", "master")
    (path / "prepare.py").write_text("# fixed\n")
    (path / "train.py").write_text("# the agent's file\n")
    (path / "program.md").write_text("# Program\nHuman instructions.\n")
    git("add", ".")
    git("commit", "-m", "init")
    return git


def ok_runner(commands_log=None):
    """Subprocess stub: every preflight/clone/sync call succeeds."""

    def runner(argv, **kwargs):
        if commands_log is not None:
            commands_log.append(argv)
        stdout = "NVIDIA RTX PRO 6000\n" if argv[0] == "nvidia-smi" else "ok\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return runner


def scripted_train(values):
    results = [
        TrainResult("ok", fields={"val_bpb": v, "peak_vram_mb": 18234.5})
        for v in values
    ]

    def train_fn(log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("stub")
        return results.pop(0)

    return train_fn


class TestConfigAndAlias:
    def test_research_roles_and_knobs_load(self, tmp_path):
        cfg = load_config(tmp_path, env={
            "MOONSHOT_API_KEY": "sk-moon-test1234567",
            "VYVCODE_AR_EPSILON": "0.002",
            "VYVCODE_AR_STRATEGY_EVERY": "5",
        })

        assert cfg.roles["researcher"].model == "openai/kimi-k3"
        assert cfg.roles["researcher"].api_key == "sk-moon-test1234567"
        assert cfg.roles["strategist"].model == "anthropic/claude-fable-5"
        assert cfg.ar_epsilon == 0.002
        assert cfg.ar_strategy_every == 5
        assert cfg.ar_crash_streak_limit == 3
        assert cfg.ar_dir.is_absolute()

    def test_vyvecode_is_silent_alias_everywhere(self):
        assert parse_line("/vyvecode:autoresearch setup").kind == "autoresearch"
        assert parse_line("/vyvcode:autoresearch start --hours 1").arg == "start --hours 1"
        assert parse_line("/vyvecode:status").kind == "status"
        assert parse_line("/vyvecode:grill topic").kind == "grill"

    def test_startup_probe_roles_exclude_research_roles(self):
        from vyvcode.config import ROLES

        assert "researcher" not in ROLES
        assert "strategist" not in ROLES


class TestFingerprintAndLock:
    def test_fingerprint(self, tmp_path):
        assert is_ar_checkout(tmp_path) is False
        make_ar_checkout(tmp_path)
        assert is_ar_checkout(tmp_path) is True

    def test_lock_contention_and_stale_recovery(self, tmp_path, monkeypatch):
        acquire_lock(tmp_path, "ar_one")

        with pytest.raises(ResearchError, match="one session per checkout"):
            acquire_lock(tmp_path, "ar_two")

        monkeypatch.setattr(research, "_pid_alive", lambda pid: False)
        notices = []
        acquire_lock(tmp_path, "ar_three", out=notices.append)

        assert any("stale" in n for n in notices)
        release_lock(tmp_path)


class TestSummaryParser:
    def test_golden_block_yields_all_nine_fields(self):
        fields = parse_summary(GOLDEN_SUMMARY)

        assert fields["val_bpb"] == 0.991234
        assert fields["peak_vram_mb"] == 18234.5
        assert fields["num_steps"] == 612
        assert fields["depth"] == 8
        assert set(fields) == set(research.SUMMARY_FIELDS)

    def test_missing_val_bpb_is_crash(self):
        assert parse_summary("Traceback (most recent call last):\nboom\n") is None

    def test_truncated_block_still_crash_without_val_bpb(self):
        assert parse_summary("training_seconds: 300.0\ndepth: 8\n") is None

    def test_noise_lines_do_not_confuse_anchoring(self):
        noisy = "note: val_bpb: 9.9 (interim)\n" + GOLDEN_SUMMARY

        assert parse_summary(noisy)["val_bpb"] == 0.991234


class TestTsv:
    def test_rows_are_byte_exact_with_tabs_and_crash_zeros(self, tmp_path):
        path = tmp_path / "results.tsv"
        tsv_init(path)
        tsv_append(path, "abc1234", 0.991234, 17.8, "keep", "baseline")
        tsv_append(path, "def5678", 0.0, 0.0, "crash", "bad idea, with commas\tand tabs")

        assert path.read_text() == (
            "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
            "abc1234\t0.991234\t17.8\tkeep\tbaseline\n"
            "def5678\t0.000000\t0.0\tcrash\tbad idea with commas and tabs\n"
        )


class TestStrategyBlock:
    def test_insert_replace_read_and_idempotence(self, tmp_path):
        program = tmp_path / "program.md"
        program.write_text("# Program\nHuman text.\n")

        assert ensure_strategy_block(program) is True
        assert ensure_strategy_block(program) is False
        replace_strategy_block(program, "PURSUE: try lr 0.05")

        text = program.read_text()
        assert text.startswith("# Program\nHuman text.\n")
        assert read_strategy_block(program) == "PURSUE: try lr 0.05"
        assert text.count(research.STRATEGY_BEGIN) == 1


class TestPreflight:
    def test_no_gpu_fails_with_amd_pointer(self, tmp_path):
        def runner(argv, **kwargs):
            rc = 1 if argv[0] == "nvidia-smi" else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

        errors = preflight(tmp_path, runner=runner)

        assert any("andyluo7/autoresearch" in e for e in errors)


class TestSetup:
    def test_setup_produces_branch_baselines_tsv_markers_state(self, tmp_path, monkeypatch):
        make_ar_checkout(tmp_path)
        monkeypatch.setattr(research, "AR_CACHE", tmp_path / "cache")
        (tmp_path / "cache" / "tokenizer").mkdir(parents=True)
        (tmp_path / "cache" / "tokenizer" / "tokenizer.pkl").write_text("t")
        (tmp_path / "cache" / "data").mkdir()
        (tmp_path / "cache" / "data" / "shard0.bin").write_text("d")
        cfg = load_config(tmp_path, env={})
        printed = []

        message = setup(
            cfg, out=printed.append, ask_user=None,
            runner=ok_runner(), train_fn=scripted_train([0.9931, 0.9942]),
        )

        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch.startswith("autoresearch/")
        tsv = (tmp_path / "results.tsv").read_text().splitlines()
        assert len(tsv) == 3
        assert "\tkeep\tbaseline" in tsv[1]
        assert "baseline repeat (noise check)" in tsv[2]
        assert research.STRATEGY_BEGIN in (tmp_path / "program.md").read_text()
        assert "results.tsv" in (tmp_path / ".git" / "info" / "exclude").read_text()

        session = ResearchSession.latest(tmp_path)
        assert session.state["baseline"]["val_bpb"] == 0.9931
        assert session.state["baseline"]["spread"] == pytest.approx(0.0011)
        assert session.state["best"]["val_bpb"] == 0.9931
        assert not research._lock_path(tmp_path).is_file()
        assert "spread" in message
        assert any("noise floor" in p for p in printed)

    def test_dirty_tree_refused(self, tmp_path, monkeypatch):
        make_ar_checkout(tmp_path)
        monkeypatch.setattr(research, "AR_CACHE", tmp_path / "cache")
        (tmp_path / "cache" / "tokenizer").mkdir(parents=True)
        (tmp_path / "cache" / "tokenizer" / "tokenizer.pkl").write_text("t")
        (tmp_path / "cache" / "data").mkdir()
        (tmp_path / "cache" / "data" / "s.bin").write_text("d")
        (tmp_path / "train.py").write_text("# uncommitted edit\n")
        cfg = load_config(tmp_path, env={})

        with pytest.raises(ResearchError, match="uncommitted"):
            setup(cfg, out=lambda _: None, runner=ok_runner(),
                  train_fn=scripted_train([0.99, 0.99]))

    def test_refuses_clone_into_nonempty_dir(self, tmp_path):
        target = tmp_path / "not-ar"
        target.mkdir()
        (target / "junk.txt").write_text("x")
        cfg = load_config(tmp_path / "elsewhere", env={})
        (tmp_path / "elsewhere").mkdir()

        with pytest.raises(ResearchError, match="refusing to clone"):
            setup(cfg, ar_dir=target, out=lambda _: None, runner=ok_runner(),
                  train_fn=scripted_train([0.99, 0.99]))

    def test_setup_grill_records_decisions_and_assumed(self, tmp_path, monkeypatch):
        make_ar_checkout(tmp_path)
        monkeypatch.setattr(research, "AR_CACHE", tmp_path / "cache")
        (tmp_path / "cache" / "tokenizer").mkdir(parents=True)
        (tmp_path / "cache" / "tokenizer" / "tokenizer.pkl").write_text("t")
        (tmp_path / "cache" / "data").mkdir()
        (tmp_path / "cache" / "data" / "s.bin").write_text("d")
        cfg = load_config(tmp_path, env={})

        class FakeGrill:
            brief_md = (
                "# BRIEF\ngoal: overnight run\ndecisions:\n"
                "  - budget: until morning\nassumed:\n  - epsilon: keep 0.0\n"
            )

        setup(
            cfg, out=lambda _: None, ask_user=lambda: "all recommended",
            runner=ok_runner(), train_fn=scripted_train([0.99, 0.991]),
            grill_fn=lambda seed: FakeGrill(),
        )

        session = ResearchSession.latest(tmp_path)
        assert session.state["decisions"] == ["budget: until morning"]
        assert session.state["assumed"] == ["epsilon: keep 0.0"]


class TestDispatch:
    def test_non_checkout_gets_setup_hint(self, tmp_path):
        cfg = load_config(tmp_path, env={})

        assert "setup" in research.dispatch(cfg, "status")
        assert research.dispatch(cfg, "status") == research.SETUP_HINT

    def test_bare_command_lists_subcommands(self, tmp_path):
        cfg = load_config(tmp_path, env={})

        message = research.dispatch(cfg, "")

        assert "setup" in message and "stop" in message

    def test_status_and_stop_on_session(self, tmp_path):
        make_ar_checkout(tmp_path)
        cfg = load_config(tmp_path, env={})
        session = ResearchSession(tmp_path, tag="aug10")
        session.state["baseline"] = {"sha": "abc", "val_bpb": 0.9931, "spread": 0.001}
        session.state["best"] = {"sha": "abc", "val_bpb": 0.9899, "exp_id": 7}
        session.state["counts"] = {"run": 9, "kept": 2, "discarded": 6, "crashed": 1}
        session.save()
        tsv_init(session.tsv_path)
        tsv_append(session.tsv_path, "abc", 0.9931, 17.8, "keep", "baseline")

        status = research.dispatch(cfg, "status")
        assert "9 run / 2 kept / 1 crashed" in status
        assert "-0.32%" in status or "-0.003200" in status

        stop_message = research.dispatch(cfg, "stop")
        assert "graceful" in stop_message
        assert json.loads(session.state_path.read_text())["stop"] == "graceful"
