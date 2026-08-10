"""Full pipeline orchestration (runbook §1.1):

    optimize → grill → BRIEF → MasterPlan → swarm → review loop → report

Every stage is injectable so the integration tests run the whole machine on
scripted fakes. State transitions persist through run_state.Run.
"""

from __future__ import annotations

import datetime as _dt

from vyvcode import memory
from vyvcode.config import VyvConfig, redact
from vyvcode.grill import gather_context, run_grill
from vyvcode.models import llm_for
from vyvcode.optimizer import optimize
from vyvcode.planner import PlanValidationError, generate_plan
from vyvcode.run_state import TERMINAL_STATES, Run, slugify

GRILL_CAPS_BY_MODE = {"brainstorm": None, "plan": "plan", "goal": "goal"}


class PipelineAborted(Exception):
    pass


def _cap_for(cfg: VyvConfig, mode: str) -> int | None:
    kind = GRILL_CAPS_BY_MODE[mode]
    if kind is None:
        return None
    return cfg.grill_rounds_plan if kind == "plan" else cfg.grill_rounds_goal


def run_pipeline(
    cfg: VyvConfig,
    mode: str,
    raw_text: str,
    ask_user=None,
    out=print,
    communicator=None,
    explorer=None,
    planner_runner=None,
    swarm_fn=None,
    review_fn=None,
    report_fn=None,
) -> str:
    """Run the full pipeline; returns the closing line for the REPL."""
    ask_user = ask_user or (lambda: input("answers> "))
    raw_text = redact(raw_text, cfg.secret_values)  # never toward a model or disk
    run = Run(cfg.runs_dir, raw_text or mode)
    (run.dir / "input.raw.md").write_text(raw_text + "\n")

    communicator = communicator or llm_for("communicator", cfg)
    optimized = optimize(raw_text, communicator, cfg).text
    (run.dir / "input.opt.md").write_text(optimized + "\n")

    def check_abort() -> None:
        if run.is_aborted:
            raise PipelineAborted(run.run_id)

    try:
        # ── grill ────────────────────────────────────────────────────────
        run.to("GRILL")
        seed = "\n\n".join(
            part
            for part in (
                f"Task ({mode}): {optimized}",
                f"PROJECT CONTEXT\n{gather_context(cfg.project_root)}",
                _memory_block(cfg, optimized),
            )
            if part
        )
        grill_result = run_grill(
            cfg, communicator, seed, mode=mode, ask_user=ask_user, out=out,
            explorer=explorer, rounds_cap=_cap_for(cfg, mode),
        )
        (run.dir / "grill.md").write_text(grill_result.transcript + "\n")
        (run.dir / "BRIEF.md").write_text(grill_result.brief_md)
        if grill_result.spec_md:
            specs = cfg.project_root / "docs" / "vyvcode" / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            date = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
            (specs / f"{date}-{slugify(raw_text)}-design.md").write_text(
                grill_result.spec_md
            )
        run.to("BRIEF")
        check_abort()

        # ── plan ─────────────────────────────────────────────────────────
        run.to("PLANNING")
        try:
            plan = generate_plan(
                cfg, run, grill_result.brief_md,
                memory_context=memory.context_for(cfg, optimized),
                runner=planner_runner,
            )
        except PlanValidationError as exc:
            run.to("ABORTED")
            details = "\n".join(f"  - {e}" for e in exc.errors)
            return (
                "plan rejected twice by the validator; run aborted.\n"
                f"Errors:\n{details}"
            )
        out(f"MasterPlan: {len(plan.phases)} phase(s), order {' → '.join(plan.order)}")

        if cfg.plan_gate:
            run.to("PLAN_GATE")
            out(plan.raw)
            answer = ask_user()
            if answer.strip().lower() not in ("go", "yes", "y"):
                run.to("ABORTED")
                return "plan gate declined; run aborted"
        check_abort()

        # ── build ────────────────────────────────────────────────────────
        run.to("EXECUTING")
        if swarm_fn is None:
            from vyvcode.swarm import execute_swarm as swarm_fn  # noqa: PLW0127
        swarm_outcome = swarm_fn(cfg, run, plan, out=out)
        check_abort()

        # ── review ───────────────────────────────────────────────────────
        run.to("REVIEWING")
        if review_fn is None:
            from vyvcode.reviewer import review_loop as review_fn  # noqa: PLW0127
        review_outcome = review_fn(cfg, run, plan, swarm_outcome, out=out)

        # ── report ───────────────────────────────────────────────────────
        if run.state != "ESCALATED":
            run.to("REPORTING")
        if report_fn is None:
            from vyvcode.report import render_report as report_fn  # noqa: PLW0127
        report_md = report_fn(cfg, run, plan, swarm_outcome, review_outcome)
        (run.dir / "report.md").write_text(report_md)
        out(report_md)
        if run.state == "REPORTING":
            run.to("DONE")
        digest = memory.make_digest(cfg, communicator, report_md, goal=optimized)
        memory.write_digest(cfg, run.run_id, digest, goal=optimized)
        return f"run {run.run_id}: {run.state}"

    except (PipelineAborted, EOFError):
        # EOFError: stdin closed at the answers> prompt (Ctrl-D) — same as abort.
        if run.state not in TERMINAL_STATES:
            run.to("ABORTED")
        return f"run {run.run_id} aborted; artifacts in {run.dir}"
    except BaseException:
        # Crash (or Ctrl-C): don't strand the run mid-flight — it would block
        # every future pipeline until manually stopped.
        if run.state not in TERMINAL_STATES:
            run.to("ABORTED")
        raise


def _memory_block(cfg: VyvConfig, query: str) -> str:
    chunks = memory.context_for(cfg, query)
    if not chunks:
        return ""
    return f"PRIOR CONTEXT (project memory)\n{chunks}"
