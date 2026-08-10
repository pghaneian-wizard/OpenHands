"""B16 e2e: stubbed setup → 6 experiments → report + memory, artifacts consistent."""

import json

from vyvcode import memory, research
from vyvcode.config import load_config
from vyvcode.research import ResearchSession, finish_session, run_loop, setup

from tests.test_memory import FakeMemsearch
from tests.test_research import make_ar_checkout, ok_runner, scripted_train
from tests.test_research_loop import editing_researcher, scripted_trains


class TestEndToEnd:
    def test_six_experiment_session_all_artifacts_consistent(
        self, tmp_path, monkeypatch
    ):
        make_ar_checkout(tmp_path)
        monkeypatch.setattr(research, "AR_CACHE", tmp_path / "cache")
        (tmp_path / "cache" / "tokenizer").mkdir(parents=True)
        (tmp_path / "cache" / "tokenizer" / "tokenizer.pkl").write_text("t")
        (tmp_path / "cache" / "data").mkdir()
        (tmp_path / "cache" / "data" / "s.bin").write_text("d")
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch())
        cfg = load_config(tmp_path, env={"VYVCODE_AR_STRATEGY_EVERY": "0"})

        setup(cfg, out=lambda _: None, ask_user=None, runner=ok_runner(),
              train_fn=scripted_train([0.9931, 0.9940]))
        session = ResearchSession.latest(tmp_path)

        # 2 keep, 3 discard, 1 crash
        run_loop(
            cfg, tmp_path, session, out=lambda _: None, max_experiments=6,
            researcher_fn=editing_researcher(tmp_path),
            train_fn=scripted_trains(
                [0.9910, 0.9950, "crash", 0.9880, 0.9920, 0.9990]
            ),
            strategist_fn=lambda *a, **k: None, gpu_check=lambda: True,
        )
        closing = finish_session(cfg, tmp_path, session, out=lambda _: None,
                                 plot_fn=lambda s: None)

        # counts ↔ tsv ↔ jsonl ↔ best.json mutual consistency
        state = session.reload().state
        assert state["counts"] == {"run": 6, "kept": 2, "discarded": 3,
                                   "crashed": 1}

        tsv_lines = session.tsv_path.read_text().rstrip("\n").splitlines()
        assert len(tsv_lines) == 1 + 2 + 6  # header + 2 baselines + 6 experiments
        statuses = [line.split("\t")[3] for line in tsv_lines[3:]]
        assert statuses.count("keep") == 2
        assert statuses.count("discard") == 3
        assert statuses.count("crash") == 1

        records = session.experiments()
        assert [e["exp_id"] for e in records] == [1, 2, 3, 4, 5, 6]
        assert [e["decision"] for e in records] == [
            "keep", "discard", "crash", "keep", "discard", "discard",
        ]

        best = json.loads((session.dir / "best.json").read_text())
        assert best == state["best"]
        assert best["val_bpb"] == 0.9880
        assert best["exp_id"] == 4
        keep_rows = [line for line in tsv_lines if "\tkeep\t" in line]
        assert any(best["sha"] in line for line in keep_rows)

        journal = (session.dir / "journal.md").read_text()
        for exp_id in range(1, 7):
            assert f"exp {exp_id:03d}" in journal

        report = (session.dir / "report.md").read_text()
        assert "6 run / 2 kept / 3 discarded / 1 crashed" in report
        assert "0.988000" in report
        assert "report:" in closing

        digest_files = list((tmp_path / ".memsearch" / "memory").glob("*.md"))
        assert digest_files and "0.988000" in digest_files[0].read_text()

    def test_journal_redacts_planted_secret(self, tmp_path):
        make_ar_checkout(tmp_path)
        session = ResearchSession(tmp_path, tag="t")
        session.secrets = ("sk-ant-real12345678",)

        session.journal("researcher said key sk-ant-real12345678 and sk-FAKEabc12345")

        journal = (session.dir / "journal.md").read_text()
        assert "sk-ant-real12345678" not in journal
        assert "sk-FAKEabc12345" not in journal
