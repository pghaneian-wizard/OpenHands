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
    input_tokens: int = 0
    output_tokens: int = 0


def _io_of(llm) -> tuple[int, int]:
    """(prompt, completion) for one instance — the two halves are priced apart."""
    usage = getattr(getattr(llm, "metrics", None), "accumulated_token_usage", None)
    if usage is None:
        return (0, 0)
    return (getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0))


def _tokens_of(llm) -> int:
    return sum(_io_of(llm))


@dataclass(frozen=True)
class TokenSplit:
    """A token count and the two halves it is made of."""

    total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.total)


def split_of(rows) -> TokenSplit:
    """Sum any iterable carrying ``tokens`` / ``input_tokens`` / ``output_tokens``."""
    rows = list(rows)
    return TokenSplit(
        total=sum(row.tokens for row in rows),
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
    )


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
            pairs = [_io_of(llm) for llm in llms]
            prompt = sum(p for p, _ in pairs)
            completion = sum(c for _, c in pairs)
            if not prompt + completion:
                continue
            rows.append(
                RoleUsage(
                    role=role,
                    model=str(getattr(llms[0], "model", "?")),
                    tokens=prompt + completion,
                    cost=sum(_cost_of(llm) for llm in llms),
                    input_tokens=prompt,
                    output_tokens=completion,
                )
            )
        return sorted(rows, key=lambda r: _rank(r.role))

    def total_tokens(self) -> int:
        return sum(row.tokens for row in self.snapshot())

    def totals(self) -> "TokenSplit":
        """Session spend as one atomic read: total, and its two halves."""
        return split_of(self.snapshot())

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


def fmt_split(split: TokenSplit) -> str:
    """The spend readout: one total, then where it went."""
    return (
        f"Total: {fmt_tokens(split.total)}"
        f"  Input: {fmt_tokens(split.input_tokens)}"
        f"  Output: {fmt_tokens(split.output_tokens)}"
    )


def short_model(model: str) -> str:
    """Drop the provider prefix: the role already says who is speaking."""
    return model.split("/")[-1]
