"""Asset installer + SDK discovery of installed agents and skills (§7/B5)."""

from vyvcode.assets import assets_root
from vyvcode.config import load_config
from vyvcode.installer import install_assets


class TestAssetsRoot:
    def test_wheel_layout_assets_inside_package(self, tmp_path):
        pkg = tmp_path / "site-packages" / "vyvcode"
        (pkg / "skills").mkdir(parents=True)
        (pkg / "agents").mkdir()

        assert assets_root(pkg) == pkg

    def test_checkout_layout_assets_beside_package(self, tmp_path):
        pkg = tmp_path / "repo" / "vyvcode"
        pkg.mkdir(parents=True)
        (tmp_path / "repo" / "skills").mkdir()

        assert assets_root(pkg) == pkg.parent

    def test_default_resolves_to_real_skills_and_agents(self):
        root = assets_root()

        assert (root / "skills" / "vyvcode-grill" / "SKILL.md").is_file()
        assert (root / "agents" / "vyvcode-coder.md").is_file()

AGENT_NAMES = (
    "vyvcode-communicator",
    "vyvcode-masterplanner",
    "vyvcode-coder",
    "vyvcode-reviewer",
    "vyvcode-researcher",
    "vyvcode-strategist",
)
SKILL_NAMES = ("vyvcode-grill", "vyvcode-brainstorm", "stop-slop")


class TestInstaller:
    def test_install_produces_both_subtrees_with_stamped_models(self, tmp_path):
        cfg = load_config(tmp_path, env={"VYVCODE_CODER_MODEL": "openai/custom-coder"})

        install_assets(cfg)

        agents = tmp_path / ".agents" / "agents"
        skills = tmp_path / ".agents" / "skills"
        for name in AGENT_NAMES:
            assert (agents / f"{name}.md").is_file()
        for name in SKILL_NAMES:
            assert (skills / name / "SKILL.md").is_file()

        coder = (agents / "vyvcode-coder.md").read_text()
        assert "model: openai/custom-coder" in coder
        assert "max_iteration_per_run: 200" in coder
        assert "{{" not in coder
        assert "max_budget_per_run" not in coder  # unset budget line dropped

    def test_budget_stamped_when_configured(self, tmp_path):
        cfg = load_config(tmp_path, env={"VYVCODE_CODER_MAX_BUDGET": "12.5"})

        install_assets(cfg)

        coder = (tmp_path / ".agents" / "agents" / "vyvcode-coder.md").read_text()
        assert "max_budget_per_run: 12.5" in coder

    def test_second_run_skips_identical_and_backs_up_modified(self, tmp_path):
        cfg = load_config(tmp_path, env={})
        install_assets(cfg)

        target = tmp_path / ".agents" / "skills" / "stop-slop" / "SKILL.md"
        target.write_text("locally modified")

        log = install_assets(cfg)

        assert any(line.startswith("skip") for line in log)
        assert target.with_suffix(".md.bak").read_text() == "locally modified"
        assert "Core Rules" in target.read_text()


class TestSdkDiscovery:
    def test_sdk_loads_all_agents_and_skills_by_name(self, tmp_path):
        from openhands.sdk.skills import load_project_skills
        from openhands.sdk.subagent import load_agents_from_dir

        cfg = load_config(tmp_path, env={})
        install_assets(cfg)

        agents = load_agents_from_dir(tmp_path / ".agents" / "agents")
        assert sorted(a.name for a in agents) == sorted(AGENT_NAMES)
        coder = next(a for a in agents if a.name == "vyvcode-coder")
        assert coder.model == "openai/kimi-k3"
        assert coder.max_iteration_per_run == 200

        skills = load_project_skills(tmp_path)
        assert set(SKILL_NAMES) <= {s.name for s in skills}
