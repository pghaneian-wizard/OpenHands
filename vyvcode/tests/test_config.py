"""Config loader: precedence (env > .env > toml > defaults) and redaction."""

from pathlib import Path

import pytest

from vyvcode.config import REDACTED, ConfigError, load_config, redact


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestPrecedence:
    def test_defaults_apply_with_empty_environment(self, tmp_path):
        cfg = load_config(tmp_path, env={})

        assert cfg.roles["communicator"].model == "gpt-5.6-sol"
        assert cfg.roles["planner"].model == "anthropic/claude-fable-5"
        assert cfg.roles["planner"].effort == "max"
        assert cfg.roles["coder"].base_url == "https://api.moonshot.ai/v1"
        assert cfg.roles["reviewer"].model == "anthropic/claude-opus-5"
        assert cfg.max_parallel_coders == 4
        assert cfg.max_review_cycles == 5
        assert cfg.plan_gate is False
        assert cfg.coder_max_budget is None

    def test_process_env_beats_dotenv_beats_toml(self, tmp_path):
        _write(tmp_path / ".env", "VYVCODE_CODER_MODEL=from-dotenv\n")
        _write(
            tmp_path / "vyvcode.toml",
            '[models]\ncoder_model = "from-toml"\nreviewer_model = "toml-reviewer"\n'
            "[orchestration]\nmax_review_cycles = 9\n",
        )

        cfg = load_config(tmp_path, env={"VYVCODE_CODER_MODEL": "from-env"})

        assert cfg.roles["coder"].model == "from-env"
        assert cfg.roles["reviewer"].model == "toml-reviewer"
        assert cfg.max_review_cycles == 9

    def test_unusable_knob_values_raise_config_error_naming_the_var(self, tmp_path):
        for value in ("0", "-1", "four"):
            with pytest.raises(ConfigError, match="VYVCODE_MAX_PARALLEL_CODERS"):
                load_config(tmp_path, env={"VYVCODE_MAX_PARALLEL_CODERS": value})

    def test_bad_float_knob_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="VYVCODE_AR_MAX_HOURS"):
            load_config(tmp_path, env={"VYVCODE_AR_MAX_HOURS": "overnight"})

    def test_dotenv_empty_value_with_inline_comment_is_unset(self, tmp_path):
        # python-dotenv keeps the comment as the value when the value is empty:
        # "X=   # note" parses to "# note". Must read as unset, not crash float().
        _write(
            tmp_path / ".env",
            "VYVCODE_CODER_MAX_BUDGET=   # optional $ cap per phase\n"
            "OPENROUTER_API_KEY=   # optional fallback routing\n",
        )

        cfg = load_config(tmp_path, env={})

        assert cfg.coder_max_budget is None

    def test_dotenv_beats_toml(self, tmp_path):
        _write(tmp_path / ".env", "VYVCODE_REVIEWER_MODEL=dotenv-reviewer\n")
        _write(tmp_path / "vyvcode.toml", '[models]\nreviewer_model = "toml-reviewer"\n')

        cfg = load_config(tmp_path, env={})

        assert cfg.roles["reviewer"].model == "dotenv-reviewer"

    def test_role_key_resolution_and_fallbacks(self, tmp_path):
        cfg = load_config(
            tmp_path,
            env={
                "ANTHROPIC_API_KEY": "sk-ant-test12345678",
                "MOONSHOT_API_KEY": "sk-moon-test1234567",
                "VYVCODE_CODER_FALLBACKS": "openrouter/a, openrouter/b",
            },
        )

        assert cfg.roles["planner"].api_key == "sk-ant-test12345678"
        assert cfg.roles["coder"].api_key == "sk-moon-test1234567"
        assert cfg.roles["communicator"].api_key is None
        assert cfg.roles["coder"].fallbacks == ("openrouter/a", "openrouter/b")

    def test_model_override_reinfers_provider_key(self, tmp_path):
        cfg = load_config(
            tmp_path,
            env={
                "ANTHROPIC_API_KEY": "sk-ant-test12345678",
                "VYVCODE_CODER_MODEL": "anthropic/claude-fable-5",
                "VYVCODE_CODER_BASE_URL": "",
            },
        )

        assert cfg.roles["coder"].api_key_env == "ANTHROPIC_API_KEY"
        assert cfg.roles["coder"].api_key == "sk-ant-test12345678"

    def test_secrets_never_read_from_toml(self, tmp_path):
        _write(tmp_path / "vyvcode.toml", '[models]\nanthropic_api_key = "sk-bad"\n')

        cfg = load_config(tmp_path, env={})

        assert cfg.roles["planner"].api_key is None


class TestRedaction:
    def test_known_secret_values_replaced(self):
        out = redact("key is sk-ant-abc123def456 ok", secrets=("sk-ant-abc123def456",))

        assert "sk-ant-abc123def456" not in out
        assert REDACTED in out

    def test_sk_pattern_redacted_without_registration(self):
        out = redact("planted sk-AAAAbbbbCCCC1234 in a digest")

        assert "sk-AAAAbbbbCCCC1234" not in out
        assert REDACTED in out

    def test_key_value_assignments_masked(self):
        out = redact("MOONSHOT_API_KEY=sk-live-xyz and token: abc.def.ghi")

        assert "sk-live-xyz" not in out
        assert "abc.def.ghi" not in out

    def test_plain_prose_untouched(self):
        text = "Fix the parser in vyvcode/planner.py and rerun pytest."

        assert redact(text) == text

    def test_config_collects_secret_values_for_redaction(self, tmp_path):
        cfg = load_config(tmp_path, env={"ANTHROPIC_API_KEY": "sk-ant-test12345678"})

        assert "sk-ant-test12345678" in cfg.secret_values
