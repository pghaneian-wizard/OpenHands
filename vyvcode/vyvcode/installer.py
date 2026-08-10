"""Install VyvCode's agent definitions and skills into a target project.

Copies ``vyvcode/agents/*.md`` → ``<project>/.agents/agents/`` and
``vyvcode/skills/**`` → ``<project>/.agents/skills/``, both auto-discovered by
the SDK. Agent templates carry ``{{ROLE_MODEL}}`` placeholders; the installer
stamps in the resolved VyvConfig values so env overrides always win.

Skip if present and identical; on mismatch, back up theirs to ``*.bak`` and
overwrite — VyvCode's copies are canonical (runbook §7).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from vyvcode.config import VyvConfig

ASSETS_ROOT = Path(__file__).resolve().parent.parent


def _stamps(cfg: VyvConfig) -> dict[str, str]:
    return {
        "{{COMMUNICATOR_MODEL}}": cfg.roles["communicator"].model,
        "{{PLANNER_MODEL}}": cfg.roles["planner"].model,
        "{{CODER_MODEL}}": cfg.roles["coder"].model,
        "{{REVIEWER_MODEL}}": cfg.roles["reviewer"].model,
        "{{RESEARCHER_MODEL}}": cfg.roles["researcher"].model,
        "{{STRATEGIST_MODEL}}": cfg.roles["strategist"].model,
        "{{CODER_MAX_ITER}}": str(cfg.coder_max_iter),
        "{{CODER_MAX_BUDGET}}": (
            "" if cfg.coder_max_budget is None else str(cfg.coder_max_budget)
        ),
    }


def _render_agent(text: str, stamps: dict[str, str]) -> str:
    for placeholder, value in stamps.items():
        text = text.replace(placeholder, value)
    # A budget line stamped empty is invalid frontmatter; drop it entirely.
    lines = [
        line
        for line in text.splitlines()
        if line.strip() != "max_budget_per_run:"
    ]
    return "\n".join(lines) + "\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _place(dest: Path, content: bytes, log: list[str]) -> None:
    rel = dest.name
    if dest.exists():
        if _digest(dest.read_bytes()) == _digest(content):
            log.append(f"skip {dest} (identical)")
            return
        backup = dest.with_suffix(dest.suffix + ".bak")
        backup.write_bytes(dest.read_bytes())
        log.append(f"backup {dest} -> {backup.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    log.append(f"install {dest}")


def install_assets(cfg: VyvConfig, assets_root: Path | None = None) -> list[str]:
    root = assets_root or ASSETS_ROOT
    stamps = _stamps(cfg)
    log: list[str] = []

    agents_src = root / "agents"
    agents_dst = cfg.project_root / ".agents" / "agents"
    for src in sorted(agents_src.glob("*.md")):
        rendered = _render_agent(src.read_text(encoding="utf-8"), stamps)
        _place(agents_dst / src.name, rendered.encode("utf-8"), log)

    skills_src = root / "skills"
    skills_dst = cfg.project_root / ".agents" / "skills"
    for src in sorted(p for p in skills_src.rglob("*") if p.is_file()):
        _place(skills_dst / src.relative_to(skills_src), src.read_bytes(), log)

    return log
