"""Session-wide token ledger, grouped by role.

Every model call in VyvCode goes through an ``LLM`` built by
:func:`vyvcode.models.llm_for`, and each instance accumulates its own metrics.
Registering the instances here means spend is captured no matter which call
path produced it — optimizer, grill, planner, any of the parallel coders, the
reviewer — without threading a counter through every stage.

Instances are held, not copied: a snapshot re-reads their live metrics, so the
numbers keep climbing while an agent works.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# Display order; anything unexpected sorts after these.
_ROLE_ORDER = (
    "communicator",
    "planner",
    "coder",
    "reviewer",
    "researcher",
    "strategist",
)


@dataclass(frozen=True)
class RoleUsage:
    role: str
    model: str
    tokens: int
    cost: float


def _tokens_of(llm) -> int:
    usage = getattr(getattr(llm, "metrics", None), "accumulated_token_usage", None)
    if usage is None:
        return 0
    return getattr(usage, "prompt_tokens", 0) + getattr(usage, "completion_tokens", 0)


def _cost_of(llm) -> float:
    return getattr(getattr(llm, "metrics", None), "accumulated_cost", 0.0) or 0.0


class UsageLedger:
    """Live token and cost totals per role, summed over every LLM instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instances: dict[str, list] = {}

    def register(self, role: str, llm) -> None:
        with self._lock:
            self._instances.setdefault(role, []).append(llm)

    def snapshot(self) -> list[RoleUsage]:
        """Roles that have actually spent something, in display order."""
        with self._lock:
            items = [(role, list(llms)) for role, llms in self._instances.items()]
        rows = []
        for role, llms in items:
            tokens = sum(_tokens_of(llm) for llm in llms)
            if not tokens:
                continue
            rows.append(
                RoleUsage(
                    role=role,
                    model=str(getattr(llms[0], "model", "?")),
                    tokens=tokens,
                    cost=sum(_cost_of(llm) for llm in llms),
                )
            )
        return sorted(rows, key=lambda r: _rank(r.role))

    def total_tokens(self) -> int:
        return sum(row.tokens for row in self.snapshot())

    def total_cost(self) -> float:
        return sum(row.cost for row in self.snapshot())

    def reset(self) -> None:
        with self._lock:
            self._instances.clear()


def _rank(role: str) -> tuple[int, str]:
    return (
        _ROLE_ORDER.index(role) if role in _ROLE_ORDER else len(_ROLE_ORDER),
        role,
    )


# One ledger per process: the REPL toolbar and the run panel read the same one.
ledger = UsageLedger()


def fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def short_model(model: str) -> str:
    """Drop the provider prefix: the role already says who is speaking."""
    return model.split("/")[-1]
