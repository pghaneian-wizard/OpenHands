"""Final report assembly (runbook §14): rendered from run artifacts only.

Terse. Tables over prose. Nothing the artifacts can't back.
"""

from __future__ import annotations

import re

from vyvcode.config import VyvConfig


def _brief_field(brief_md: str, key: str) -> str:
    match = re.search(rf"(?m)^{key}:\s*(.*)$", brief_md)
    return match.group(1).strip() if match else ""


def _brief_list(brief_md: str, key: str) -> list[str]:
    match = re.search(rf"(?m)^{key}:.*\n((?:[ \t]+-.*\n?)*)", brief_md)
    if not match:
        return []
    return [
        line.strip().lstrip("- ").strip()
        for line in match.group(1).splitlines()
        if line.strip().startswith("-")
    ]


def _stack_line(plan) -> str:
    match = re.search(r"(?m)^stack:\s*(.*)$", plan.raw)
    return match.group(1).strip() if match else "?"


def render_report(cfg: VyvConfig, run, plan, swarm_outcome, review_outcome) -> str:
    brief_path = run.dir / "BRIEF.md"
    brief = brief_path.read_text() if brief_path.is_file() else ""
    lines: list[str] = []

    # 1. Built
    lines += [f"# VyvCode report — {run.run_id}", ""]
    lines += ["## Built", ""]
    lines += [f"- goal: {_brief_field(brief, 'goal') or '?'}"]
    lines += [f"- stack: {_stack_line(plan)}", ""]
    lines += ["| phase | name | status | files |", "|---|---|---|---|"]
    for pid in plan.order:
        outcome = swarm_outcome.phases.get(pid)
        status = outcome.status if outcome else "?"
        files = ", ".join(plan.phases[pid].est_files)
        lines += [f"| {pid} | {plan.phases[pid].name} | {status} | {files} |"]
    lines += [""]

    # 2. Verified
    verdict = (
        "ESCALATED"
        if review_outcome.escalated
        else ("APPROVED" if review_outcome.approved else "CHANGES_REQUIRED")
    )
    lines += ["## Verified", ""]
    lines += [f"- review cycles: {review_outcome.cycles}"]
    lines += [f"- final verdict: {verdict}"]
    if review_outcome.suite:
        passed = sum(1 for r in review_outcome.suite if r["passed"])
        lines += [f"- full suite: {passed}/{len(review_outcome.suite)} passed"]
        for result in review_outcome.suite:
            mark = "PASS" if result["passed"] else "FAIL"
            lines += [f"  - {mark} `{result['cmd']}`"]
    lines += [""]

    # 3. How to run
    lines += ["## How to run", ""]
    lines += [f"- checkout: `git switch {swarm_outcome.integration_branch}`"]
    final_ids = [p for p in plan.order if p.upper() in ("F", "PF")]
    for pid in final_ids:
        for cmd in plan.phases[pid].acceptance:
            lines += [f"- verify: `{cmd}`"]
    lines += ["- set any credentials/env the built project's .env.example lists "
              "(the pipeline never handles real secrets for you)", ""]

    # 4. Deviations & assumptions
    assumed = _brief_list(brief, "assumed")
    dead = swarm_outcome.dead
    lines += ["## Deviations & assumptions", ""]
    for item in assumed:
        lines += [f"- assumed: {item}"]
    for pid in dead:
        outcome = swarm_outcome.phases[pid]
        note = outcome.notes[-1].splitlines()[0] if outcome.notes else outcome.status
        lines += [f"- dead phase {pid} ({outcome.status}): {note}"]
    if not assumed and not dead:
        lines += ["- none"]
    lines += [""]

    # 5. Punch list
    lines += ["## Punch list", ""]
    blocked = []
    for pid, outcome in swarm_outcome.phases.items():
        for item in (outcome.report or {}).get("blocked_on", []):
            blocked.append(f"- {pid} blocked_on: {item}")
    minors = [f"- [{i.id}] {i.problem} ({', '.join(i.files) or 'no files'})"
              for i in review_outcome.minors]
    outstanding = [f"- OUTSTANDING [{i.id}] {i.severity}: {i.problem}"
                   for i in review_outcome.outstanding]
    lines += outstanding + minors + blocked or []
    if not (outstanding or minors or blocked):
        lines += ["- empty"]
    lines += [""]

    # 6. Where everything lives
    lines += ["## Where everything lives", ""]
    lines += [f"- integration branch: {swarm_outcome.integration_branch} "
              f"(base: {swarm_outcome.base_branch})"]
    try:
        run_dir = run.dir.relative_to(cfg.project_root)
    except ValueError:
        run_dir = run.dir
    lines += [f"- run artifacts: {run_dir}"]
    lines += [f"- plan: docs/vyvcode/plans/{run.run_id}-MasterPlan.md"]
    if cfg.memory_enabled:
        lines += ["- memory: digest appended to .memsearch/memory/ (today's file)"]
    lines += [""]
    return "\n".join(lines)
