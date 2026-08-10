"""Coder swarm: worktrees, wave scheduling, merge queue (runbook §10).

Branch topology:

    <base>                                # user's branch at run start
     └─ vyvcode/<run_id>/integration      # all merges land here
         ├─ vyvcode/<run_id>/P1-<slug>    # worktree .vyvcode/worktrees/<run_id>/P1
         └─ …

Invariants enforced here, not trusted to any model:
- dirty base tree (tracked modifications) refuses to start;
- a phase is DONE only when PHASE_REPORT.json says done, the worktree is
  committed clean, and the harness itself re-ran every acceptance command;
- merges are serialized; conflicts go to a dedicated integration coder whose
  result is re-verified by the harness;
- one automatic retry per phase with the failure prepended, then
  RETRY_EXHAUSTED — dependents are skipped and the run stays alive.

All git invocations are argv lists (shell=False). Acceptance commands are the
one deliberate exception: planner-authored `run:` lines execute under the
shell — that trust boundary is documented in ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from vyvcode.config import VyvConfig
from vyvcode.models import coder_llm
from vyvcode.run_state import slugify
from vyvcode.subagents import agent_prompt, run_agent_task

CODER_TOOLS = ("terminal", "file_editor", "glob", "grep")
ACCEPTANCE_TIMEOUT = 600
GIT_IDENTITY = ("-c", "user.name=vyvcode", "-c", "user.email=vyvcode@local")

PHASE_REPORT_SCHEMA = (
    '{"phase_id": "<id>", "status": "done|failed", "commits": <n>, '
    '"files_changed": ["..."], "acceptance": [{"cmd": "...", "passed": true}], '
    '"notes": ["..."], "blocked_on": ["..."]}'
)

# Verbatim from runbook §10.3.
CODER_TASK_TEMPLATE = """\
You are coding agent {phase_id} for run {run_id}. Your working directory is a
dedicated git worktree on branch {branch}. Build EXACTLY your phase; nothing
outside it.

<your_phase>
{phase_section}
</your_phase>
<contracts>
{contracts}
</contracts>
<stack>
{stack}
</stack>
<brief_excerpt>
{brief_excerpt}
</brief_excerpt>

Rules:
- Full auto. Never ask questions; resolve ambiguity in the direction of the
  contracts, and record any judgment call in PHASE_REPORT notes.
- Only create/modify files in your est_files scope plus tests for them. If you
  believe you must touch another phase's file, STOP that edit and record it as
  a `blocked_on` note instead — the reviewer will handle cross-phase issues.
- Write tests for your acceptance criteria and make them pass. Run them.
- Commit your work on your branch (small commits are fine), and finish by
  writing PHASE_REPORT.json (schema: {report_schema}) in your worktree root.
"""

INTEGRATION_TASK_TEMPLATE = """\
You are the integration coder for run {run_id}. The merge of branch
{branch} into {integration_branch} conflicted. Your working directory is the
integration worktree, mid-merge with conflict markers present.

Conflicted files:
{conflicted}

<incoming_phase>
{phase_section}
</incoming_phase>
<other_phase_sections>
{other_sections}
</other_sections>
<contracts>
{contracts}
</contracts>

Resolve every conflict so both phases' contracts hold, run both phases'
acceptance commands to confirm, then `git add` the resolved files and complete
the merge commit (`git commit --no-edit`). Do not touch anything unrelated.
"""


class SwarmError(Exception):
    pass


@dataclass
class PhaseOutcome:
    phase_id: str
    status: str = "PENDING"  # DISPATCHED DONE FAILED MERGED RETRY_EXHAUSTED SKIPPED
    attempts: int = 0
    report: dict | None = None
    acceptance: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SwarmOutcome:
    base_branch: str
    integration_branch: str
    integration_dir: Path
    phases: dict[str, PhaseOutcome]
    full_suite: list[dict] | None = None
    aborted: bool = False

    @property
    def merged(self) -> list[str]:
        return [p for p, o in self.phases.items() if o.status == "MERGED"]

    @property
    def dead(self) -> list[str]:
        return [
            p
            for p, o in self.phases.items()
            if o.status in ("RETRY_EXHAUSTED", "SKIPPED")
        ]


# ── git plumbing ─────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise SwarmError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc


def _require_clean_base(root: Path) -> str:
    if _git(root, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise SwarmError(f"{root} is not a git repository; the swarm needs one")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise SwarmError(
            "refusing to start: the base tree has uncommitted tracked changes — "
            "an auto-pipeline on top of uncommitted work is how you lose it.\n"
            + dirty
        )
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return branch


def _run_shell(command: str, cwd: Path) -> dict:
    """Planner-authored acceptance line: deliberate shell=True trust boundary."""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=ACCEPTANCE_TIMEOUT, check=False,
        )
        output = (proc.stdout + proc.stderr)[-2000:]
        return {"cmd": command, "passed": proc.returncode == 0, "output": output}
    except subprocess.TimeoutExpired:
        return {"cmd": command, "passed": False, "output": "timeout"}


# ── completion gate (§10.4) ──────────────────────────────────────────────────


def verify_phase(worktree: Path, phase) -> tuple[bool, PhaseOutcome]:
    outcome = PhaseOutcome(phase_id=phase.id)
    report_path = worktree / "PHASE_REPORT.json"
    if not report_path.is_file():
        outcome.notes.append("PHASE_REPORT.json missing")
        return False, outcome
    try:
        outcome.report = json.loads(report_path.read_text())
    except json.JSONDecodeError as exc:
        outcome.notes.append(f"PHASE_REPORT.json unparseable: {exc}")
        return False, outcome
    if outcome.report.get("status") != "done":
        outcome.notes.append(f"report status: {outcome.report.get('status')}")
        return False, outcome

    # Tracked changes only: the coder is told to run its own tests, so the
    # worktree legitimately holds untracked build artifacts (__pycache__/,
    # .pytest_cache/, coverage files). Those are not uncommitted work.
    porcelain = _git(
        worktree, "status", "--porcelain", "--untracked-files=no"
    ).stdout.splitlines()
    stray = [line for line in porcelain if not line.endswith("PHASE_REPORT.json")]
    if stray:
        outcome.notes.append("worktree not committed clean: " + "; ".join(stray[:5]))
        return False, outcome

    ok = True
    for command in phase.acceptance:  # never trust the agent's own claim — re-run
        result = _run_shell(command, worktree)
        outcome.acceptance.append(result)
        if not result["passed"]:
            ok = False
            outcome.notes.append(
                f"acceptance failed: {command}\n{result['output'][-500:]}"
            )
    return ok, outcome


# ── coder invocation ─────────────────────────────────────────────────────────


def default_coder_runner(cfg: VyvConfig):
    def runner(prompt: str, workspace: Path, phase_id: str) -> str:
        return run_agent_task(
            coder_llm(cfg, phase_id),
            system_prompt=agent_prompt("vyvcode-coder"),
            task=prompt,
            workspace=workspace,
            tools=CODER_TOOLS,
            max_iterations=cfg.coder_max_iter,
        )

    return runner


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "ratelimit" in text


def _with_backoff(fn, sleep_fn=time.sleep, max_tries: int = 5):
    """§4.3: 429 → exponential backoff with jitter, then give up loudly."""
    for attempt in range(max_tries):
        try:
            return fn()
        except Exception as exc:
            if not _is_rate_limit(exc) or attempt == max_tries - 1:
                raise
            sleep_fn(min(60, 2**attempt) + random.random())
    raise AssertionError("unreachable")


def _brief_excerpt(brief_md: str) -> str:
    lines = []
    keep = False
    for line in brief_md.splitlines():
        if line.startswith(("constraints:", "success_criteria:")):
            keep = True
        elif line and not line.startswith((" ", "-", "\t")):
            keep = False
        if keep:
            lines.append(line)
    return "\n".join(lines) or brief_md


def _stack_block(plan) -> str:
    header = plan.raw.split("## Phase", 1)[0]
    return header.strip()


def _coder_prompt(run_id: str, phase, plan, brief_md: str, failure: str | None) -> str:
    contracts = "\n\n".join(
        f"[{dep}]\n{plan.phases[dep].interfaces}"
        for dep in phase.depends_on
        if plan.phases[dep].interfaces
    ) or "(no upstream contracts)"
    prompt = CODER_TASK_TEMPLATE.format(
        phase_id=phase.id,
        run_id=run_id,
        branch=phase.worktree_branch,
        phase_section=f"## Phase {phase.id}: {phase.name}\n{phase.body}",
        contracts=contracts,
        stack=_stack_block(plan),
        brief_excerpt=_brief_excerpt(brief_md),
        report_schema=PHASE_REPORT_SCHEMA,
    )
    if failure:
        prompt = f"previous attempt failed:\n{failure}\n\n{prompt}"
    return prompt


# ── merge queue (§10.5) ──────────────────────────────────────────────────────


def default_integration_runner(cfg: VyvConfig):
    def runner(prompt: str, workspace: Path, phase_id: str) -> str:
        return run_agent_task(
            coder_llm(cfg, f"integration-{phase_id}"),
            system_prompt=agent_prompt("vyvcode-coder"),
            task=prompt,
            workspace=workspace,
            tools=CODER_TOOLS,
            max_iterations=cfg.coder_max_iter,
        )

    return runner


def _merge_phase(
    run_id: str,
    integration_dir: Path,
    integration_branch: str,
    phase,
    plan,
    outcome: PhaseOutcome,
    merged_ids: list[str],
    integration_runner,
    out,
) -> bool:
    merge = _git(
        integration_dir, "merge", "--no-ff", "-m",
        f"vyvcode: merge {phase.id} ({phase.name})", phase.worktree_branch,
        check=False,
    )
    if merge.returncode == 0:
        return True

    # One path per line: splitting on whitespace shreds paths containing spaces.
    conflicted = _git(
        integration_dir, "diff", "--name-only", "--diff-filter=U"
    ).stdout.splitlines()
    if not conflicted:  # not a content conflict — surface the real error
        outcome.notes.append(f"merge failed: {merge.stderr.strip()}")
        _git(integration_dir, "merge", "--abort", check=False)
        return False

    out(f"{phase.id} merge conflict on {', '.join(conflicted)} → integration coder")
    others = [
        pid
        for pid in merged_ids
        if set(plan.phases[pid].est_files) & set(conflicted)
    ]
    prompt = INTEGRATION_TASK_TEMPLATE.format(
        run_id=run_id,
        branch=phase.worktree_branch,
        integration_branch=integration_branch,
        conflicted="\n".join(conflicted),
        phase_section=phase.body,
        other_sections="\n\n".join(plan.phases[p].body for p in others) or "(none)",
        contracts="\n\n".join(
            plan.phases[p].interfaces for p in (*others, *phase.depends_on)
            if plan.phases[p].interfaces
        ) or "(none)",
    )
    try:
        integration_runner(prompt, integration_dir, phase.id)
    except Exception as exc:
        outcome.notes.append(f"integration coder failed: {exc}")
        _git(integration_dir, "merge", "--abort", check=False)
        return False

    # Verify the coder's claim: merge concluded, tree clean, acceptance green.
    still_merging = (
        _git(integration_dir, "rev-parse", "-q", "--verify", "MERGE_HEAD",
             check=False).returncode == 0
    )
    dirty = _git(integration_dir, "status", "--porcelain").stdout.strip()
    if still_merging or dirty:
        outcome.notes.append("integration coder left the merge unfinished")
        _git(integration_dir, "merge", "--abort", check=False)
        return False
    for pid in (phase.id, *others):
        for command in plan.phases[pid].acceptance:
            result = _run_shell(command, integration_dir)
            if not result["passed"]:
                outcome.notes.append(
                    f"post-merge acceptance failed ({pid}): {command}"
                )
                return False
    return True


# ── scheduler (§10.2) ────────────────────────────────────────────────────────


def execute_swarm(
    cfg: VyvConfig,
    run,
    plan,
    out=print,
    coder_runner=None,
    integration_runner=None,
    sleep_fn=time.sleep,
) -> SwarmOutcome:
    root = cfg.project_root
    base_branch = _require_clean_base(root)
    integration_branch = f"vyvcode/{run.run_id}/integration"
    worktrees_dir = root / ".vyvcode" / "worktrees" / run.run_id
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    integration_dir = worktrees_dir / "integration"

    _git(root, "branch", integration_branch)
    _git(root, "worktree", "add", str(integration_dir), integration_branch)

    # First commit on integration: the MasterPlan itself.
    plan_dest = integration_dir / "docs" / "vyvcode" / "plans"
    plan_dest.mkdir(parents=True, exist_ok=True)
    (plan_dest / f"{run.run_id}-MasterPlan.md").write_text(plan.raw)
    _git(integration_dir, "add", "docs/vyvcode/plans")
    _git(integration_dir, "commit", "-m", f"vyvcode: MasterPlan for {run.run_id}")

    coder_runner = coder_runner or default_coder_runner(cfg)
    integration_runner = integration_runner or default_integration_runner(cfg)
    brief_md = (run.dir / "BRIEF.md").read_text() if (run.dir / "BRIEF.md").is_file() else ""

    outcomes = {pid: PhaseOutcome(phase_id=pid) for pid in plan.phases}
    outcome = SwarmOutcome(
        base_branch=base_branch,
        integration_branch=integration_branch,
        integration_dir=integration_dir,
        phases=outcomes,
    )

    def mark(pid: str, status: str) -> None:
        outcomes[pid].status = status
        run.set_phase(pid, name=plan.phases[pid].name, status=status)

    def dead_set() -> set[str]:
        return {
            p for p, o in outcomes.items()
            if o.status in ("RETRY_EXHAUSTED", "SKIPPED")
        }

    def ready_phases() -> list[str]:
        merged = {p for p, o in outcomes.items() if o.status == "MERGED"}
        ready = []
        for pid in plan.order:
            o = outcomes[pid]
            if o.status != "PENDING":
                continue
            deps = plan.phases[pid].depends_on
            if any(d in dead_set() for d in deps):
                mark(pid, "SKIPPED")
                out(f"{pid} skipped (dead dependency)")
                continue
            if all(d in merged for d in deps):
                ready.append(pid)
        return ready

    def build_phase(pid: str, failure: str | None) -> tuple[str, bool, PhaseOutcome]:
        phase = plan.phases[pid]
        worktree = worktrees_dir / pid
        prompt = _coder_prompt(run.run_id, phase, plan, brief_md, failure)
        (run.dir / "phases" / pid).mkdir(parents=True, exist_ok=True)
        (run.dir / "phases" / pid / f"task-attempt{outcomes[pid].attempts}.md").write_text(prompt)
        _with_backoff(
            lambda: coder_runner(prompt, worktree, pid), sleep_fn=sleep_fn
        )
        ok, verified = verify_phase(worktree, phase)
        (run.dir / "phases" / pid / "verify.json").write_text(
            json.dumps({"ok": ok, "acceptance": verified.acceptance,
                        "notes": verified.notes}, indent=2) + "\n"
        )
        return pid, ok, verified

    executor = ThreadPoolExecutor(max_workers=cfg.max_parallel_coders)
    in_flight: dict = {}
    try:
        while True:
            if run.is_aborted:
                outcome.aborted = True
                break
            for pid in ready_phases():
                if len(in_flight) >= cfg.max_parallel_coders:
                    break
                phase = plan.phases[pid]
                # Cut the phase branch from integration at dispatch time so
                # later waves include merged dependencies. Main thread only —
                # shared-repo git mutations are never done from workers.
                worktree = worktrees_dir / pid
                _git(root, "worktree", "add", "-b", phase.worktree_branch,
                     str(worktree), integration_branch)
                failure = outcomes[pid].notes[-1] if outcomes[pid].notes else None
                outcomes[pid].attempts += 1
                mark(pid, "DISPATCHED")
                out(f"{pid} {phase.name} ▸ building (attempt {outcomes[pid].attempts})")
                in_flight[executor.submit(build_phase, pid, failure)] = pid

            if not in_flight:
                break

            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                pid = in_flight.pop(future)
                phase = plan.phases[pid]
                try:
                    _, ok, verified = future.result()
                except Exception as exc:
                    ok, verified = False, PhaseOutcome(
                        phase_id=pid, notes=[f"coder error: {exc}"]
                    )
                verified.attempts = outcomes[pid].attempts
                verified.status = outcomes[pid].status
                outcomes[pid] = verified

                if ok:
                    if _merge_phase(
                        run.run_id, integration_dir, integration_branch, phase,
                        plan, outcomes[pid], outcome.merged, integration_runner, out,
                    ):
                        mark(pid, "MERGED")
                        out(f"{pid} {phase.name} ▸ tests green ▸ merged")
                        continue
                    ok = False  # merge-level failure falls through to retry logic

                if outcomes[pid].attempts >= 2:
                    mark(pid, "RETRY_EXHAUSTED")
                    out(f"{pid} {phase.name} ▸ RETRY_EXHAUSTED")
                else:
                    mark(pid, "PENDING")  # retried on the next scheduling pass
                    out(f"{pid} {phase.name} ▸ failed, retrying")
                    _git(root, "worktree", "remove", "--force",
                         str(worktrees_dir / pid), check=False)
                    _git(root, "branch", "-D", phase.worktree_branch, check=False)
    finally:
        executor.shutdown(wait=True)

    # Full Phase-F suite on integration after the last merge (§10.5).
    final_ids = [p for p in plan.order if p.upper() in ("F", "PF")]
    if final_ids and final_ids[0] in outcome.merged:
        outcome.full_suite = [
            _run_shell(cmd, integration_dir)
            for cmd in plan.phases[final_ids[0]].acceptance
        ]
    return outcome
