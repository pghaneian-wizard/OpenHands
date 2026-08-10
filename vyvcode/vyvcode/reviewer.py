"""Review-until-clean loop (runbook §11).

"Reviewer is happy" is precisely: verdict == APPROVED **and** the harness's
own full-suite run is green — the machine check backs the model's word.
Malformed verdicts get one "re-emit the json block only" retry, then count as
CHANGES_REQUIRED with a synthetic blocker. Minors never block; they flow to
the final report as a punch list. Cycle cap → ESCALATED with the outstanding
issues verbatim.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

from vyvcode.config import VyvConfig
from vyvcode.models import coder_llm, llm_for
from vyvcode.subagents import READ_ONLY_TOOLS, agent_prompt, run_agent_task
from vyvcode.swarm import CODER_TOOLS, _run_shell

_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.S)
_SEVERITIES = ("blocker", "major", "minor")

REEMIT_PROMPT = (
    "Your verdict block was missing or malformed. Re-emit ONLY the fenced "
    "json verdict block, nothing else."
)

FIX_TASK_TEMPLATE = """\
You are a fix coder for run {run_id}, working on the integration branch in
this worktree. The reviewer requires these fixes:

{issues}

<relevant_masterplan_sections>
{sections}
</relevant_masterplan_sections>

Apply exactly the required fixes. Before committing, re-run the acceptance
commands / tests relevant to each issue and make them pass. Commit the result.
Do not touch anything the issues don't cover.
"""


@dataclass
class Issue:
    id: str
    severity: str
    problem: str
    phase: str = ""
    files: list[str] = field(default_factory=list)
    required_fix: str = ""

    def render(self) -> str:
        files = f" files: {', '.join(self.files)}" if self.files else ""
        return (
            f"[{self.id}] {self.severity} (phase {self.phase or '?'}){files}\n"
            f"  problem: {self.problem}\n  required_fix: {self.required_fix}"
        )


@dataclass
class Verdict:
    verdict: str
    cycle: int
    issues: list[Issue]
    synthetic: bool = False

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity in ("blocker", "major")]

    @property
    def minors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "minor"]


@dataclass
class ReviewOutcome:
    approved: bool
    escalated: bool
    cycles: int
    history: list[Verdict]
    minors: list[Issue]
    outstanding: list[Issue]
    suite: list[dict] = field(default_factory=list)


class VerdictError(Exception):
    pass


def parse_verdict(text: str) -> Verdict:
    """Strict §11.2 contract: exactly one fenced json block, at the end."""
    blocks = _JSON_FENCE.findall(text)
    if len(blocks) != 1:
        raise VerdictError(f"expected exactly one fenced json block, got {len(blocks)}")
    if not text.rstrip().endswith("```"):
        raise VerdictError("verdict block is not the final content of the message")
    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise VerdictError(f"verdict json unparseable: {exc}") from exc
    # Every shape below is a VerdictError, never a raw TypeError: the review
    # loop only retries on VerdictError, and anything else kills a run that
    # already paid for the grill, the plan, and the whole swarm.
    if not isinstance(data, dict):
        raise VerdictError(f"verdict json must be an object, got {type(data).__name__}")
    if data.get("verdict") not in ("APPROVED", "CHANGES_REQUIRED"):
        raise VerdictError(f"invalid verdict value: {data.get('verdict')!r}")
    raw_issues = data.get("issues") or []
    if not isinstance(raw_issues, list):
        raise VerdictError(f"issues must be a list, got {type(raw_issues).__name__}")
    issues = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            raise VerdictError(f"each issue must be an object, got {raw!r}")
        severity = raw.get("severity")
        if severity not in _SEVERITIES:
            raise VerdictError(f"invalid severity: {severity!r}")
        issues.append(
            Issue(
                id=str(raw.get("id", "?")),
                severity=severity,
                problem=str(raw.get("problem", "")),
                phase=str(raw.get("phase", "")),
                files=_file_list(raw.get("files")),
                required_fix=str(raw.get("required_fix", "")),
            )
        )
    try:
        cycle = int(data.get("cycle", 0))
    except (TypeError, ValueError):
        raise VerdictError(f"cycle must be a number, got {data.get('cycle')!r}") from None
    return Verdict(verdict=data["verdict"], cycle=cycle, issues=issues)


def _file_list(raw) -> list[str]:
    """A bare string is one path, not a list of characters."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        raise VerdictError(f"files must be a list, got {type(raw).__name__}")
    return [str(f) for f in raw]


def _synthetic(cycle: int, problem: str) -> Verdict:
    return Verdict(
        verdict="CHANGES_REQUIRED",
        cycle=cycle,
        issues=[
            Issue(
                id="R0", severity="blocker", problem=problem,
                required_fix="produce a parseable verdict / make the suite pass",
            )
        ],
        synthetic=True,
    )


# ── review inputs (§11.1) ────────────────────────────────────────────────────


def _capture(argv: list[str], cwd) -> str:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False
        )
        return proc.stdout.strip() or "(empty)"
    except (OSError, subprocess.TimeoutExpired):
        return "(unavailable)"


def build_review_task(cfg: VyvConfig, run, plan, swarm_outcome, cycle: int,
                      suite: list[dict]) -> str:
    integration = swarm_outcome.integration_dir
    diff = _capture(
        ["git", "diff", f"{swarm_outcome.base_branch}...HEAD"], integration
    )
    tree = _capture(["git", "ls-files"], integration)
    reports = []
    for pid, outcome in swarm_outcome.phases.items():
        reports.append(
            f"[{pid}] status={outcome.status} attempts={outcome.attempts} "
            f"report={json.dumps(outcome.report)} notes={outcome.notes}"
        )
    brief_path = run.dir / "BRIEF.md"
    brief = brief_path.read_text() if brief_path.is_file() else "(missing)"
    suite_text = "\n".join(
        f"{'PASS' if r['passed'] else 'FAIL'} {r['cmd']}\n{r.get('output', '')[-300:]}"
        for r in suite
    ) or "(no suite run yet)"
    dead = ", ".join(swarm_outcome.dead) or "none"
    return (
        f"Review cycle {cycle} for run {run.run_id}. You are in the "
        f"integration worktree on {swarm_outcome.integration_branch}.\n\n"
        f"Dead phases (RETRY_EXHAUSTED/SKIPPED — call these out): {dead}\n\n"
        f"<brief>\n{brief}\n</brief>\n\n"
        f"<masterplan>\n{plan.raw}\n</masterplan>\n\n"
        f"<file_tree>\n{tree}\n</file_tree>\n\n"
        f"<phase_reports>\n{chr(10).join(reports)}\n</phase_reports>\n\n"
        f"<full_suite>\n{suite_text}\n</full_suite>\n\n"
        f"<diff>\n{diff}\n</diff>\n"
    )


# ── default runners ──────────────────────────────────────────────────────────


def default_reviewer_runner(cfg: VyvConfig, swarm_outcome):
    def runner(task: str) -> str:
        return run_agent_task(
            llm_for("reviewer", cfg),
            system_prompt=agent_prompt("vyvcode-reviewer"),
            task=task,
            workspace=swarm_outcome.integration_dir,
            tools=READ_ONLY_TOOLS,
        )

    return runner


def default_fix_runner(cfg: VyvConfig, swarm_outcome):
    def runner(prompt: str, cluster_id: str) -> str:
        return run_agent_task(
            coder_llm(cfg, f"fix-{cluster_id}"),
            system_prompt=agent_prompt("vyvcode-coder"),
            task=prompt,
            workspace=swarm_outcome.integration_dir,
            tools=CODER_TOOLS,
            max_iterations=cfg.coder_max_iter,
        )

    return runner


def default_suite_runner(plan, swarm_outcome):
    final_ids = [p for p in plan.order if p.upper() in ("F", "PF")]
    commands: list[str] = []
    if final_ids:
        commands = plan.phases[final_ids[0]].acceptance
    else:
        for pid in swarm_outcome.merged:
            commands.extend(plan.phases[pid].acceptance)

    def runner() -> list[dict]:
        return [_run_shell(cmd, swarm_outcome.integration_dir) for cmd in commands]

    return runner


# ── clustering ───────────────────────────────────────────────────────────────


def cluster_issues(issues: list[Issue]) -> list[list[Issue]]:
    """Union-find on file sets: disjoint clusters can be fixed in parallel."""
    clusters: list[tuple[set[str], list[Issue]]] = []
    for issue in issues:
        files = set(issue.files)
        hit = [c for c in clusters if files and c[0] & files]
        if hit:
            merged_files = files.union(*(c[0] for c in hit))
            merged_issues = [i for c in hit for i in c[1]] + [issue]
            clusters = [c for c in clusters if c not in hit]
            clusters.append((merged_files, merged_issues))
        else:
            clusters.append((files, [issue]))
    return [issues for _, issues in clusters]


def _fix_prompt(run, plan, cluster: list[Issue]) -> str:
    phases = {i.phase for i in cluster if i.phase and i.phase in plan.phases}
    sections = "\n\n".join(plan.phases[p].body for p in sorted(phases)) or "(n/a)"
    return FIX_TASK_TEMPLATE.format(
        run_id=run.run_id,
        issues="\n\n".join(i.render() for i in cluster),
        sections=sections,
    )


# ── the loop (§11.3) ─────────────────────────────────────────────────────────


def review_loop(
    cfg: VyvConfig,
    run,
    plan,
    swarm_outcome,
    out=print,
    reviewer_runner=None,
    fix_runner=None,
    suite_runner=None,
) -> ReviewOutcome:
    reviewer_runner = reviewer_runner or default_reviewer_runner(cfg, swarm_outcome)
    fix_runner = fix_runner or default_fix_runner(cfg, swarm_outcome)
    suite_runner = suite_runner or default_suite_runner(plan, swarm_outcome)

    history: list[Verdict] = []
    minors: list[Issue] = []
    suite = suite_runner()

    cycle = 1
    while cycle <= cfg.max_review_cycles:
        # Persisted, so /vyvcode:status shows review progress and a crash
        # mid-review leaves a record of how many cycles were consumed.
        run.bump_cycle()
        cycle_dir = run.dir / "review" / f"cycle-{cycle}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        task = build_review_task(cfg, run, plan, swarm_outcome, cycle, suite)
        text = reviewer_runner(task)
        (cycle_dir / "review.md").write_text(text)
        try:
            verdict = parse_verdict(text)
        except VerdictError:
            retry_text = reviewer_runner(REEMIT_PROMPT)
            (cycle_dir / "review-reemit.md").write_text(retry_text)
            try:
                verdict = parse_verdict(retry_text)
            except VerdictError as exc:
                verdict = _synthetic(cycle, f"reviewer output unparseable: {exc}")
        history.append(verdict)
        minors.extend(verdict.minors)
        (cycle_dir / "verdict.json").write_text(
            json.dumps(
                {"verdict": verdict.verdict, "cycle": verdict.cycle,
                 "synthetic": verdict.synthetic,
                 "issues": [vars(i) for i in verdict.issues]},
                indent=2,
            ) + "\n"
        )

        # An empty suite is not a green suite: with nothing merged there are no
        # acceptance commands, and all([]) would approve a run in which every
        # single phase died, without executing one command.
        suite_green = bool(suite) and all(r["passed"] for r in suite)
        if verdict.verdict == "APPROVED" and not verdict.blocking and suite_green:
            out(f"Review cycle {cycle}: APPROVED, full suite green")
            return ReviewOutcome(
                approved=True, escalated=False, cycles=cycle, history=history,
                minors=minors, outstanding=[], suite=suite,
            )

        work = list(verdict.blocking)
        if verdict.verdict == "APPROVED" and not verdict.blocking and not suite_green:
            problem = (
                "reviewer approved but no acceptance command ran at all"
                if not suite
                else "reviewer approved but the full suite is red"
            )
            work = _synthetic(cycle, problem).issues
            history[-1] = Verdict(
                verdict="CHANGES_REQUIRED", cycle=cycle,
                issues=verdict.issues + work, synthetic=True,
            )

        clusters = cluster_issues(work)
        out(
            f"Review cycle {cycle}: {len(work)} blocker/major issue(s) → "
            f"dispatching {len(clusters)} fix cluster(s)"
        )
        run.to("FIXING")
        # Serial by necessity: every fix coder is handed the same integration
        # worktree and told to commit, so two at once contend on
        # .git/index.lock and stage each other's half-written edits. The
        # reviewer's per-issue file lists are advisory, not an isolation
        # boundary like the phase worktrees the swarm hands out.
        for n, cluster in enumerate(clusters):
            fix_runner(_fix_prompt(run, plan, cluster), f"c{cycle}-{n}")
        run.to("REVIEWING")

        suite = suite_runner()
        cycle += 1

    run.to("ESCALATED")
    outstanding = [i for v in history[-1:] for i in v.blocking]
    out(
        f"ESCALATED after {cfg.max_review_cycles} cycle(s); "
        f"{len(outstanding)} issue(s) outstanding on {swarm_outcome.integration_branch}"
    )
    return ReviewOutcome(
        approved=False, escalated=True, cycles=cfg.max_review_cycles,
        history=history, minors=minors, outstanding=outstanding, suite=suite,
    )
