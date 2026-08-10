"""Optimizer invariants (§5): protection, fallback, bypass, metrics, compression."""

import json
from types import SimpleNamespace

import pytest
from openhands.sdk import Message, TextContent

from vyvcode.config import load_config
from vyvcode.optimizer import (
    _PLACEHOLDER_RE,
    count_tokens,
    optimize,
    protect,
    restore,
)


class FakeLLM:
    """Scripted communicator: transform(protected_input) -> rewritten text."""

    def __init__(self, transform):
        self.transform = transform
        self.calls = []

    def completion(self, messages, **kwargs):
        user_text = messages[-1].content[0].text
        inner = user_text.split("<input>\n", 1)[1].rsplit("\n</input>", 1)[0]
        self.calls.append(inner)
        return SimpleNamespace(
            message=Message(
                role="assistant", content=[TextContent(text=self.transform(inner))]
            )
        )


@pytest.fixture
def cfg(tmp_path):
    return load_config(tmp_path, env={"VYVCODE_OPTIMIZER_MIN_TOKENS": "1"})


FENCE = "```python\ndef addd(a, b):\n    # thsi is delibrately mispeled\n    return a+b\n```"


class TestProtection:
    def test_protect_restore_roundtrip_is_identity(self):
        raw = (
            "see src/api/foo.py and https://example.com/x?a=1 set MY_VAR "
            'and "quoted str" plus `inline()` then\nverbatim: keep thsi exactly\n'
            'File "app.py", line 3\nValueError: boom\ndone'
        )

        protected, spans = protect(raw)

        assert restore(protected, spans) == raw
        assert "src/api/foo.py" not in protected
        assert "MY_VAR" not in protected
        assert "keep thsi exactly" not in protected

    def test_code_fence_survives_byte_identical_while_prose_is_fixed(self, cfg):
        raw = f"please plese fix teh function below\n{FENCE}\nthanks so mcuh"
        llm = FakeLLM(
            lambda inner: "Fix the function below.\n"
            + _PLACEHOLDER_RE.search(inner).group(0)
        )

        result = optimize(raw, llm, cfg)

        assert result.fallback is False
        assert FENCE in result.text
        assert "plese" not in result.text
        assert "thanks" not in result.text


class TestFallback:
    def test_placeholder_loss_forces_raw_fallback(self, cfg):
        raw = f"fix this\n{FENCE}\nplease"
        llm = FakeLLM(lambda inner: "Fix this.")  # drops the placeholder

        result = optimize(raw, llm, cfg)

        assert result.fallback is True
        assert result.text == raw
        assert "placeholder" in result.reason

    def test_token_growth_forces_raw_fallback(self, cfg):
        raw = "short prose message with no protected spans at all here"
        llm = FakeLLM(lambda inner: inner + " padded far beyond the original " * 20)

        result = optimize(raw, llm, cfg)

        assert result.fallback is True
        assert result.text == raw
        assert "grew" in result.reason

    def test_llm_exception_forces_raw_fallback(self, cfg):
        def boom(inner):
            raise RuntimeError("provider down")

        result = optimize("some plain message here", FakeLLM(boom), cfg)

        assert result.fallback is True
        assert result.text == "some plain message here"


class TestBypassAndMetrics:
    def test_bypass_returns_verbatim_without_llm(self, cfg):
        raw = "teh !raw text stays exactly as typed"

        result = optimize(raw, llm=None, cfg=cfg, bypass=True)

        assert result.skipped is True
        assert result.text == raw

    def test_below_min_tokens_skips_rewrite(self, tmp_path):
        cfg = load_config(tmp_path, env={"VYVCODE_OPTIMIZER_MIN_TOKENS": "60"})

        result = optimize("tiny", llm=None, cfg=cfg)

        assert result.skipped is True

    def test_metrics_line_written(self, cfg, tmp_path):
        metrics = tmp_path / "metrics.jsonl"
        llm = FakeLLM(lambda inner: "Compressed.")

        optimize("a plain prose message that gets rewritten", llm, cfg, metrics_path=metrics)

        record = json.loads(metrics.read_text().splitlines()[-1])
        assert {"ts", "tokens_in", "tokens_out", "saved_pct", "fallback"} <= set(record)
        assert record["fallback"] is False


class TestCompression:
    def test_rambling_input_compresses_25pct_with_constraints_intact(self, cfg):
        constraints = [
            "port 8443",
            "use Postgres",
            "never delete the logs directory",
            "--strict flag",
            "run migrations before seeding",
        ]
        filler = (
            "So I was thinking, and I know this might sound a bit odd, but maybe, "
            "if you have time, it would be kind of great, really great actually, "
            "if we could possibly look into the following thing together. "
        )
        raw = (
            filler * 6
            + "We should listen on port 8443 for the server. "
            + filler
            + "For storage we definitely want to use Postgres and not anything else. "
            + filler
            + "Whatever happens, never delete the logs directory. "
            + filler
            + "The CLI needs a --strict flag for validation. "
            + filler
            + "And remember to run migrations before seeding the database. "
            + filler * 4
        )
        assert count_tokens(raw) >= 300

        compressed = (
            "Build the server: listen on port 8443. Use Postgres for storage. "
            "Never delete the logs directory. Add a --strict flag to the CLI. "
            "Run migrations before seeding."
        )
        _, spans = protect(raw)

        def transform(inner):
            out = compressed
            for key, value in spans.items():
                out = out.replace(value, key)
            return out

        result = optimize(raw, FakeLLM(transform), cfg)

        assert result.fallback is False
        assert result.saved_pct >= 25
        for constraint in constraints:
            assert constraint.lower() in result.text.lower()
