"""Full pipeline orchestration: grill → BRIEF → plan → swarm → review → report.

Wired across B6–B10; this stub keeps the dispatcher importable until then.
"""

from __future__ import annotations

from vyvcode.config import VyvConfig


def run_pipeline(cfg: VyvConfig, mode: str, raw_text: str) -> str:
    raise NotImplementedError("pipeline lands in B6–B10")
