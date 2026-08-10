"""Memory: digest write with anchor + redaction, recall from chunks, misses (§12)."""

import datetime as dt
import json

import pytest

from vyvcode import memory
from vyvcode.config import load_config
from tests.fakes import ScriptedChatLLM


@pytest.fixture
def cfg(tmp_path):
    return load_config(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-real12345678"})


class FakeMemsearch:
    """Stands in for the memsearch CLI subprocess layer."""

    def __init__(self, search_results=None):
        self.search_results = search_results or []
        self.calls = []

    def __call__(self, cfg, *args, timeout=300):
        self.calls.append(args)
        verb = args[0]
        if verb == "index":
            return 0, "indexed"
        if verb == "search":
            return 0, json.dumps(self.search_results)
        if verb == "expand":
            return 0, "EXPANDED FULL SECTION"
        return 1, "unknown"


class TestWritePath:
    def test_digest_lands_in_todays_file_with_anchor(self, cfg, monkeypatch):
        fake = FakeMemsearch()
        monkeypatch.setattr(memory, "_memsearch", fake)

        path = memory.write_digest(
            cfg, "run_x", "- built the thing\n- tests green", goal="build the thing"
        )

        today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        assert path.name == f"{today}.md"
        text = path.read_text()
        assert "<!-- session:run_x -->" in text
        assert "run_x — build the thing" in text
        assert "- built the thing" in text
        assert fake.calls[0][0] == "index"

    def test_planted_secret_redacted_before_write(self, cfg, monkeypatch):
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch())

        path = memory.write_digest(
            cfg, "run_y",
            "- leaked key sk-FAKEabcdef123456 and real sk-ant-real12345678",
            goal="x",
        )

        text = path.read_text()
        assert "sk-FAKEabcdef123456" not in text
        assert "sk-ant-real12345678" not in text

    def test_memory_disabled_writes_nothing(self, tmp_path, monkeypatch):
        cfg = load_config(tmp_path, env={"VYVCODE_MEMORY": "false"})
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch())

        assert memory.write_digest(cfg, "run_z", "- digest", goal="g") is None
        assert not cfg.memory_dir.exists()

    def test_make_digest_falls_back_to_report_head_on_llm_failure(self, cfg):
        class Boom:
            def completion(self, **kw):
                raise RuntimeError("down")

        digest = memory.make_digest(cfg, Boom(), "# Report\nBuilt X\nTests green", "g")

        assert digest.startswith("- ")
        assert "Built X" in digest


class TestFailureIsolation:
    def test_unwritable_memory_dir_does_not_kill_a_finished_run(self, tmp_path):
        # write_digest runs after the run reaches DONE; an OSError here would
        # surface an already-successful run to the user as a traceback.
        cfg = load_config(tmp_path, env={})
        cfg.memory_dir.parent.mkdir(parents=True)
        cfg.memory_dir.write_text("a file where the directory should be")

        assert memory.write_digest(cfg, "run_x", "- did a thing", goal="g") is None

    def test_undecodable_subprocess_output_degrades_to_empty(
        self, tmp_path, monkeypatch
    ):
        # A stray non-UTF-8 byte in memsearch's stdout raises
        # UnicodeDecodeError, which is not an OSError and escaped the handler.
        cfg = load_config(tmp_path, env={})
        monkeypatch.setattr(memory, "_memsearch_bin", lambda: "/bin/true")

        def boom(*a, **k):
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte")

        monkeypatch.setattr(memory.subprocess, "run", boom)

        assert memory.search(cfg, "anything") == []


class TestReadPath:
    CHUNK = {
        "score": 0.9,
        "source": "/proj/.memsearch/memory/2026-08-01.md",
        "heading": "run_a",
        "content": "decided to use sqlite for storage",
        "chunk_hash": "abc123",
    }

    def test_recall_answers_from_chunks_citing_date(self, cfg, monkeypatch):
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch([self.CHUNK]))
        llm = ScriptedChatLLM(["We chose sqlite (memory 2026-08-01.md)."])

        answer = memory.recall(cfg, "what storage did we pick", llm=llm)

        assert "sqlite" in answer
        prompt = llm.histories[0][-1].content[0].text
        assert "2026-08-01.md" in prompt
        assert "decided to use sqlite" in prompt

    def test_recall_refuses_on_miss(self, cfg, monkeypatch):
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch([]))

        assert memory.recall(cfg, "anything", llm=None) == "not in memory"

    def test_long_top_chunk_gets_expanded(self, cfg, monkeypatch):
        long_chunk = dict(self.CHUNK, content="x" * 600)
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch([long_chunk]))
        llm = ScriptedChatLLM(["answer"])

        memory.recall(cfg, "q", llm=llm)

        prompt = llm.histories[0][-1].content[0].text
        assert "EXPANDED FULL SECTION" in prompt

    def test_context_for_formats_and_caps(self, cfg, monkeypatch):
        monkeypatch.setattr(memory, "_memsearch", FakeMemsearch([self.CHUNK]))

        context = memory.context_for(cfg, "storage")

        assert context.startswith("[2026-08-01.md]")
        assert "sqlite" in context

    def test_search_failure_degrades_to_empty(self, cfg, monkeypatch):
        monkeypatch.setattr(
            memory, "_memsearch", lambda cfg, *a, timeout=300: (1, "milvus down")
        )

        assert memory.search(cfg, "q") == []
        assert memory.context_for(cfg, "q") == ""
