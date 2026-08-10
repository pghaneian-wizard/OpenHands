"""Persistent semantic memory via memsearch (runbook §12). Wired in B9."""

from __future__ import annotations

from vyvcode.config import VyvConfig


def context_for(cfg: VyvConfig, query: str, cap_tokens: int = 1500) -> str:
    """Top memory chunks for a topic, injected before grill and planning."""
    return ""  # B9


def recall(cfg: VyvConfig, query: str) -> str:
    raise NotImplementedError("memory lands in B9")


def write_digest(cfg: VyvConfig, run_id: str, digest_md: str) -> None:
    """Append a run digest to today's memory file and reindex."""
    return None  # B9
