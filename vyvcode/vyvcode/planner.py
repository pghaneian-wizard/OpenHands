"""MasterPlanner invocation + MasterPlan.md parser/validator (runbook §9).

The planner inspects the repo read-only and emits MasterPlan.md in the §9.3
schema. The parser is strict: unique ids, resolvable acyclic depends_on,
est_files disjoint across dependency-unrelated phases (the parallel-safety
invariant D5), and at least one machine-runnable acceptance line per phase.
One regeneration attempt with the errors quoted back; a second failure aborts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from vyvcode.config import VyvConfig
from vyvcode.models import llm_for
from vyvcode.subagents import READ_ONLY_TOOLS, agent_prompt, run_agent_task

_PHASE_HEADING = re.compile(r"(?m)^## Phase\s+([A-Za-z0-9_\-]+)\s*:\s*(.*)$")
_YAML_FENCE = re.compile(r"```yaml\s*\n(.*?)```", re.S)
_ACCEPTANCE_RUN = re.compile(r"-\s*run:\s*`([^`]+)`")


class PlanValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Phase:
    id: str
    name: str
    depends_on: list[str]
    worktree_branch: str
    est_files: list[str]
    acceptance: list[str]
    body: str
    interfaces: str = ""


@dataclass
class MasterPlan:
    raw: str
    phases: dict[str, Phase]
    order: list[str] = field(default_factory=list)  # topological


def _section(body: str, heading: str) -> str:
    match = re.search(rf"(?m)^### {re.escape(heading)}\s*$", body)
    if not match:
        return ""
    rest = body[match.end() :]
    nxt = re.search(r"(?m)^### ", rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def parse_masterplan(text: str) -> MasterPlan:
    errors: list[str] = []
    headings = list(_PHASE_HEADING.finditer(text))
    if not headings:
        raise PlanValidationError(["no '## Phase <id>: <name>' sections found"])

    phases: dict[str, Phase] = {}
    for i, match in enumerate(headings):
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[start:end]
        label, name = match.group(1), match.group(2).strip()

        yaml_match = _YAML_FENCE.search(body)
        if not yaml_match:
            errors.append(f"phase {label}: missing ```yaml block")
            continue
        try:
            meta = yaml.safe_load(yaml_match.group(1)) or {}
        except yaml.YAMLError as exc:
            errors.append(f"phase {label}: invalid yaml ({exc})")
            continue

        phase_id = str(meta.get("id", "")).strip()
        if not phase_id:
            errors.append(f"phase {label}: yaml block lacks 'id'")
            continue
        if phase_id in phases:
            errors.append(f"duplicate phase id {phase_id}")
            continue

        depends_on = [str(d) for d in (meta.get("depends_on") or [])]
        est_files = [str(f) for f in (meta.get("est_files") or [])]
        branch = str(meta.get("worktree_branch", "")).strip()
        if not branch:
            errors.append(f"phase {phase_id}: yaml block lacks 'worktree_branch'")

        for path in est_files:
            posix = PurePosixPath(path)
            if posix.is_absolute() or ".." in posix.parts:
                errors.append(f"phase {phase_id}: est_files path escapes repo: {path}")

        acceptance = _ACCEPTANCE_RUN.findall(_section(body, "Acceptance"))
        if not acceptance:
            errors.append(
                f"phase {phase_id}: Acceptance section needs at least one "
                "'- run: `<command>`' line"
            )

        phases[phase_id] = Phase(
            id=phase_id,
            name=name,
            depends_on=depends_on,
            worktree_branch=branch,
            est_files=est_files,
            acceptance=acceptance,
            body=body.strip(),
            interfaces=_section(body, "Interfaces provided"),
        )

    for phase in phases.values():
        for dep in phase.depends_on:
            if dep not in phases:
                errors.append(f"phase {phase.id}: unknown dependency {dep}")

    order: list[str] = []
    if not errors:
        order = _topo_sort(phases, errors)
    if not errors:
        _check_disjoint_files(phases, errors)
    if errors:
        raise PlanValidationError(errors)
    return MasterPlan(raw=text, phases=phases, order=order)


def _topo_sort(phases: dict[str, Phase], errors: list[str]) -> list[str]:
    indegree = {pid: 0 for pid in phases}
    for phase in phases.values():
        for dep in phase.depends_on:
            indegree[phase.id] += 1
    ready = sorted(pid for pid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while ready:
        pid = ready.pop(0)
        order.append(pid)
        for phase in sorted(phases.values(), key=lambda p: p.id):
            if pid in phase.depends_on:
                indegree[phase.id] -= 1
                if indegree[phase.id] == 0:
                    ready.append(phase.id)
        ready.sort()
    if len(order) != len(phases):
        stuck = sorted(set(phases) - set(order))
        errors.append(f"dependency cycle involving: {', '.join(stuck)}")
    return order


def _reachable(phases: dict[str, Phase], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for dep in phases[current].depends_on:
            if dep in phases and dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _check_disjoint_files(phases: dict[str, Phase], errors: list[str]) -> None:
    """D5: phases with no dependency path between them must not share files."""
    ancestors = {pid: _reachable(phases, pid) for pid in phases}
    ids = sorted(phases)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if a in ancestors[b] or b in ancestors[a]:
                continue
            overlap = set(phases[a].est_files) & set(phases[b].est_files)
            if overlap:
                errors.append(
                    f"parallel phases {a} and {b} share files: "
                    f"{', '.join(sorted(overlap))} — merge them or move the "
                    "shared file into an earlier phase both depend on"
                )


# ── Generation ───────────────────────────────────────────────────────────────

_TASK_TEMPLATE = """\
Produce MasterPlan.md for the BRIEF below. Inspect the repository first with
your read tools so the plan reflects reality. Your final message must be the
complete MasterPlan.md content and nothing else.

<brief>
{brief}
</brief>
{memory}
"""

_RETRY_TEMPLATE = """\
Your previous MasterPlan.md was rejected by the validator. Fix every error
and emit the complete corrected MasterPlan.md as your final message.

Validator errors:
{errors}
"""


def default_planner_runner(cfg: VyvConfig):
    def runner(task: str) -> str:
        return run_agent_task(
            llm_for("planner", cfg),
            system_prompt=agent_prompt("vyvcode-masterplanner"),
            task=task,
            workspace=cfg.project_root,
            tools=READ_ONLY_TOOLS,
        )

    return runner


def generate_plan(
    cfg: VyvConfig,
    run,
    brief_md: str,
    memory_context: str = "",
    runner=None,
) -> MasterPlan:
    """BRIEF → validated MasterPlan; one retry with errors quoted, then abort."""
    runner = runner or default_planner_runner(cfg)
    memory = f"\nPRIOR CONTEXT (project memory)\n{memory_context}\n" if memory_context else ""
    task = _TASK_TEMPLATE.format(brief=brief_md, memory=memory)

    text = runner(task)
    try:
        plan = parse_masterplan(text)
    except PlanValidationError as first:
        retry_task = task + "\n" + _RETRY_TEMPLATE.format(
            errors="\n".join(f"- {e}" for e in first.errors)
        )
        text = runner(retry_task)
        plan = parse_masterplan(text)  # second failure propagates to the caller

    (run.dir / "MasterPlan.md").write_text(plan.raw)
    plans_dir = cfg.project_root / "docs" / "vyvcode" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / f"{run.run_id}-MasterPlan.md").write_text(plan.raw)
    return plan
