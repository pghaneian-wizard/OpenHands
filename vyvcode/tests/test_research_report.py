"""B15: strategist scope, report golden, progress.png, memory digest, resume."""

import subprocess
import sys
from pathlib import Path

from vyvcode import memory, research
from vyvcode.research import (
    ResearchSession,
    default_strategist_pass,
    finish_session,
    progress_png,
    read_strategy_block,
    render_research_report,
    run_loop,
    tsv_append,
)

from tests.test_memory import FakeMemsearch
from tests.test_research import make_ar_checkout
from tests.test_research_loop import editing_researcher, prepared, scripted_trains

GOLDEN = Path(__file__).parent / "golden" / "research-report.md"

VALID_BLOCK = """\
## Strategy (auto-managed by VyvCode strategist)
READ: baseline noise is 0.001; nothing outside it kept yet.
PURSUE: 1. untie embeddings. 2. lr 0.05 with 2x warmup.
AVOID: depth changes — two crashes.
WILDCARD: replace Muon with plain AdamW once.
"""


def scripted_session(tmp_path):
    """Deterministic session dir for the report golden."""
    cfg, old_session, sha = prepared(tmp_path)
    session = ResearchSession(tmp_path, session_id="ar_20260810_220000_test")
    session.state.update({
        "session_id": "ar_20260810_220000_test",
        "tag": "test",
        "branch": "autoresearch/test",
        "baseline": dict(old_session.state["baseline"], sha="aaa0000"),
        "best": {"sha": "beef123", "val_bpb": 0.9880, "exp_id": 4},
        "counts": {"run": 6, "kept": 2, "discarded": 3, "crashed": 1},
        "strategist_passes": 1,
    })
    session.save()
    for record in [
        {"exp_id": 1, "decision": "keep", "hypothesis": "untie embeddings",
         "val_bpb": 0.9910, "num_params_M": 561.2, "peak_vram_mb": 18000.0,
         "mfu": 41.0, "tokens_spent": 1000},
        {"exp_id": 2, "decision": "discard", "hypothesis": "wider mlp",
         "val_bpb": 0.9950, "tokens_spent": 900},
        {"exp_id": 3, "decision": "crash", "hypothesis": "depth 12",
         "reason": "OOM", "tokens_spent": 800},
        {"exp_id": 4, "decision": "keep", "hypothesis": "lr 0.05 warmup 2x",
         "val_bpb": 0.9880, "num_params_M": 561.2, "peak_vram_mb": 19000.0,
         "mfu": 43.5, "tokens_spent": 1100},
        {"exp_id": 5, "decision": "discard", "hypothesis": "rope theta sweep",
         "val_bpb": 0.9905, "tokens_spent": 700},
        {"exp_id": 6, "decision": "discard", "hypothesis": "no value embeds",
         "val_bpb": 0.9890, "tokens_spent": 600},
    ]:
        session.append_experiment(record)
    tsv_append(session.tsv_path, "aaa1111", 0.9910, 17.6, "keep", "untie embeddings")
    tsv_append(session.tsv_path, "bbb2222", 0.9950, 17.9, "discard", "wider mlp")
    tsv_append(session.tsv_path, "ccc3333", 0.0, 0.0, "crash", "depth 12 (OOM)")
    tsv_append(session.tsv_path, "beef123", 0.9880, 18.6, "keep", "lr 0.05 warmup 2x")
    return cfg, session


class TestStrategist:
    def test_cadence_pass_at_ten_in_twelve_experiment_session(self, tmp_path):
        cfg, session, _ = prepared(
            tmp_path, env={"VYVCODE_AR_STRATEGY_EVERY": "10"}
        )
        research.ensure_strategy_block(tmp_path / "program.md")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-am", "add strategy block"],
                       cwd=tmp_path, check=True, capture_output=True)
        session.state["best"]["sha"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        session.save()
        values = [0.99 - i * 0.0001 for i in range(12)]

        run_loop(
            cfg, tmp_path, session, out=lambda _: None, max_experiments=12,
            researcher_fn=editing_researcher(tmp_path),
            train_fn=scripted_trains(values),
            strategist_fn=lambda cfg, s, trigger, out: default_strategist_pass(
                cfg, s, trigger, out, runner=lambda task: VALID_BLOCK
            ),
            gpu_check=lambda: True,
        )

        session.reload()
        assert session.state["strategist_passes"] == 1
        assert session.state["counts"]["run"] == 12
        assert "untie embeddings" in read_strategy_block(tmp_path / "program.md")
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout
        assert "strategy: pass 1" in log

    def test_marker_tamper_rejected_then_skipped(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        research.ensure_strategy_block(tmp_path / "program.md")
        before = read_strategy_block(tmp_path / "program.md")
        calls = []

        def tampering_runner(task):
            calls.append(task)
            return f"{research.STRATEGY_END}\nescape the region"

        default_strategist_pass(cfg, session, "cadence", out=lambda _: None,
                                runner=tampering_runner)

        assert len(calls) == 2  # one retry
        assert read_strategy_block(tmp_path / "program.md") == before
        assert session.state["strategist_passes"] == 0
        assert "skipped" in (session.dir / "journal.md").read_text()


class TestReport:
    def test_report_matches_golden(self, tmp_path):
        cfg, session = scripted_session(tmp_path)

        assert render_research_report(cfg, session) == GOLDEN.read_text()

    def test_progress_png_nontrivial(self, tmp_path):
        cfg, session = scripted_session(tmp_path)

        png = progress_png(session, python_argv=[sys.executable])

        assert png is not None
        assert png.stat().st_size > 10_000

    def test_finish_session_writes_report_and_memory(self, tmp_path, monkeypatch):
        cfg, session = scripted_session(tmp_path)
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch())

        closing = finish_session(cfg, tmp_path, session, out=lambda _: None,
                                 plot_fn=lambda s: None)

        assert "report:" in closing
        assert (session.dir / "report.md").read_text().startswith(
            "# Autoresearch report"
        )
        memory_files = list((tmp_path / ".memsearch" / "memory").glob("*.md"))
        assert len(memory_files) == 1
        digest = memory_files[0].read_text()
        assert "untie embeddings" in digest
        assert "dead end" in digest


class TestResume:
    def test_resume_continues_numbering(self, tmp_path):
        cfg, session, _ = prepared(tmp_path)
        run_loop(cfg, tmp_path, session, out=lambda _: None, max_experiments=2,
                 researcher_fn=editing_researcher(tmp_path),
                 train_fn=scripted_trains([0.99, 0.989]),
                 strategist_fn=lambda *a, **k: None, gpu_check=lambda: True)
        assert session.reload().state["counts"]["run"] == 2

        # resume: cap counts total run, so allow 3 and expect exactly one more
        run_loop(cfg, tmp_path, session, out=lambda _: None, max_experiments=3,
                 researcher_fn=editing_researcher(tmp_path),
                 train_fn=scripted_trains([0.9885]),
                 strategist_fn=lambda *a, **k: None, gpu_check=lambda: True)

        records = session.experiments()
        assert [e["exp_id"] for e in records] == [1, 2, 3]


class TestFreeform:
    def test_freeform_opens_researcher_session_with_program(self, tmp_path, monkeypatch):
        make_ar_checkout(tmp_path)
        from vyvcode.config import load_config

        cfg = load_config(tmp_path, env={})
        seen = {}

        def fake_fast_path(cfg_, text, llm=None, workspace=None):
            seen["text"] = text
            seen["workspace"] = workspace

        monkeypatch.setattr("vyvcode.commands.run_fast_path", fake_fast_path)
        monkeypatch.setattr(
            "vyvcode.models.llm_for", lambda role, cfg_, **kw: f"llm:{role}"
        )

        message = research.run_freeform(cfg, tmp_path, out=lambda _: None)

        assert "Human instructions." in seen["text"]
        assert seen["workspace"] == tmp_path
        assert "freeform" in message
