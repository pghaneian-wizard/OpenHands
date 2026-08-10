"""Final report assembly from run artifacts (runbook §14). B10."""

from __future__ import annotations

from vyvcode.config import VyvConfig


def render_report(cfg: VyvConfig, run, plan, swarm_outcome, review_outcome) -> str:
    raise NotImplementedError("report lands in B10")
