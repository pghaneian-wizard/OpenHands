"""Role → LLM factory, registry wiring, and the startup liveness probe.

Every model string, effort value, base URL and key comes from VyvConfig; this
module never invents one. Each parallel coder gets its own LLM instance via
``usage_id`` override — LLM objects are never shared across threads.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from openhands.sdk import LLM, Message, TextContent
from openhands.sdk.llm import LLMRegistry
from openhands.sdk.llm.fallback_strategy import FallbackStrategy
from openhands.sdk.llm.llm_profile_store import LLMProfileStore

from vyvcode.config import ROLES, VyvConfig, redact

PROBE_PROMPT = "Reply with the single word: ok"
PROBE_MAX_TOKENS = 16


def llm_for(
    role: str,
    cfg: VyvConfig,
    usage_id: str | None = None,
    max_output_tokens: int | None = None,
) -> LLM:
    r = cfg.roles[role]
    kwargs: dict = {
        "model": r.model,
        "api_key": r.api_key,
        "base_url": r.base_url,
        "reasoning_effort": r.effort,
        "usage_id": usage_id or r.usage_id,
    }
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = max_output_tokens
    if r.fallbacks:
        kwargs["fallback_strategy"] = _fallback_strategy(role, cfg)
    return LLM(**kwargs)


def coder_llm(cfg: VyvConfig, phase_id: str) -> LLM:
    """Dedicated LLM instance for one swarm coder (own client, own spend line)."""
    return llm_for("coder", cfg, usage_id=f"vyvcode-coder-{phase_id}")


def build_registry(cfg: VyvConfig) -> LLMRegistry:
    registry = LLMRegistry()
    for role in ROLES:
        registry.add(llm_for(role, cfg))
    return registry


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _fallback_strategy(role: str, cfg: VyvConfig) -> FallbackStrategy:
    """Persist keyless fallback profiles and point a FallbackStrategy at them.

    Profiles carry no api_key on disk; LiteLLM resolves provider keys from the
    process environment at call time.
    """
    r = cfg.roles[role]
    store_dir = cfg.project_root / ".vyvcode" / "profiles"
    store = LLMProfileStore(store_dir)
    names: list[str] = []
    for model in r.fallbacks:
        name = f"vyvcode-{role}-fb-{_slug(model)}"
        store.save(
            name,
            LLM(model=model, reasoning_effort=r.effort, usage_id=name),
        )
        names.append(name)
    return FallbackStrategy(fallback_llms=names, profile_store_dir=store_dir)


# ── Startup probe (§4.4) ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeResult:
    role: str
    model: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    suggestion: str | None = None


def _suggestion_for(role: str, model: str) -> str:
    var = f"VYVCODE_{role.upper()}_MODEL"
    if "claude-opus-5" in model:
        return f"try {var}=anthropic/claude-fable-5"
    return f"override with {var} (and {var.replace('_MODEL', '_EFFORT')} if the effort value was rejected)"


def probe_role(cfg: VyvConfig, role: str) -> ProbeResult:
    r = cfg.roles[role]
    if not r.api_key:
        return ProbeResult(
            role=role,
            model=r.model,
            ok=False,
            error=f"no API key: {r.api_key_env} is not set",
            suggestion=f"set {r.api_key_env} in the environment or .env",
        )
    llm = llm_for(role, cfg, max_output_tokens=PROBE_MAX_TOKENS)
    start = time.monotonic()
    try:
        llm.completion(
            messages=[Message(role="user", content=[TextContent(text=PROBE_PROMPT)])]
        )
    except Exception as exc:  # provider error surfaces verbatim, minus secrets
        return ProbeResult(
            role=role,
            model=r.model,
            ok=False,
            error=redact(str(exc), cfg.secret_values),
            suggestion=_suggestion_for(role, r.model),
        )
    return ProbeResult(
        role=role,
        model=r.model,
        ok=True,
        latency_ms=int((time.monotonic() - start) * 1000),
    )


def run_probe(cfg: VyvConfig, roles: tuple[str, ...] = ROLES) -> list[ProbeResult]:
    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        return list(pool.map(lambda role: probe_role(cfg, role), roles))


def roles_alive(results: list[ProbeResult]) -> set[str]:
    return {r.role for r in results if r.ok}


def render_probe_table(results: list[ProbeResult]) -> str:
    role_w = max(len(r.role) for r in results)
    model_w = max(len(r.model) for r in results)
    lines = []
    for r in results:
        if r.ok:
            status = f"OK   {r.latency_ms} ms"
        else:
            status = f"FAIL {r.error} — {r.suggestion}"
        lines.append(f"{r.role:<{role_w}}  {r.model:<{model_w}}  {status}")
    return "\n".join(lines)
