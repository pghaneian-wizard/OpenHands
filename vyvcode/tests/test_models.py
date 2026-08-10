"""Model layer: role factory, registry, and probe failure paths (no network)."""

import pytest

from vyvcode.config import load_config
from vyvcode.models import (
    build_registry,
    coder_llm,
    llm_for,
    probe_role,
    render_probe_table,
    roles_alive,
    run_probe,
)

ENV = {
    "OPENAI_API_KEY": "sk-oa-test123456789",
    "ANTHROPIC_API_KEY": "sk-ant-test12345678",
    "MOONSHOT_API_KEY": "sk-moon-test1234567",
}


@pytest.fixture
def cfg(tmp_path):
    return load_config(tmp_path, env=dict(ENV))


class TestFactory:
    def test_role_values_flow_into_llm(self, cfg):
        llm = llm_for("coder", cfg)

        assert llm.model == "openai/kimi-k3"
        assert llm.base_url == "https://api.moonshot.ai/v1"
        assert llm.reasoning_effort == "max"
        assert llm.usage_id == "vyvcode-coder"

    def test_each_swarm_coder_gets_own_instance(self, cfg):
        a = coder_llm(cfg, "P1")
        b = coder_llm(cfg, "P2")

        assert a is not b
        assert a.usage_id == "vyvcode-coder-P1"
        assert b.usage_id == "vyvcode-coder-P2"

    def test_registry_holds_all_four_roles(self, cfg):
        registry = build_registry(cfg)

        for usage_id in (
            "vyvcode-communicator",
            "vyvcode-planner",
            "vyvcode-coder",
            "vyvcode-reviewer",
        ):
            assert registry.get(usage_id) is not None


class TestProbe:
    def test_missing_key_fails_without_network(self, tmp_path):
        cfg = load_config(tmp_path, env={})

        result = probe_role(cfg, "planner")

        assert result.ok is False
        assert "ANTHROPIC_API_KEY" in result.error
        assert "set ANTHROPIC_API_KEY" in result.suggestion

    def test_provider_404_surfaces_error_and_suggestion(self, cfg, monkeypatch):
        def raise_404(self, **kwargs):
            raise RuntimeError("404 model claude-opus-5 not found")

        monkeypatch.setattr(
            "openhands.sdk.llm.llm.LLM.completion", raise_404, raising=True
        )

        result = probe_role(cfg, "reviewer")

        assert result.ok is False
        assert "404" in result.error
        assert result.suggestion == "try VYVCODE_REVIEWER_MODEL=anthropic/claude-fable-5"

    def test_probe_error_redacts_secret_values(self, cfg, monkeypatch):
        def raise_with_key(self, **kwargs):
            raise RuntimeError("auth failed for key sk-ant-test12345678")

        monkeypatch.setattr(
            "openhands.sdk.llm.llm.LLM.completion", raise_with_key, raising=True
        )

        result = probe_role(cfg, "planner")

        assert "sk-ant-test12345678" not in result.error

    def test_run_probe_table_and_alive_set(self, cfg, monkeypatch):
        def ok_only_for_moonshot(self, **kwargs):
            if self.base_url and "moonshot" in self.base_url:
                return object()
            raise RuntimeError("401 invalid key")

        monkeypatch.setattr(
            "openhands.sdk.llm.llm.LLM.completion", ok_only_for_moonshot, raising=True
        )

        results = run_probe(cfg)
        table = render_probe_table(results)

        assert roles_alive(results) == {"coder"}
        assert "OK" in table and "FAIL" in table
        assert table.count("\n") == 3
