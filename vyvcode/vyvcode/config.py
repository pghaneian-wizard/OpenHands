"""Configuration for VyvCode.

Single source of truth for every knob. Precedence, highest first:

    process env  >  <project>/.env  >  <project>/vyvcode.toml  >  hardcoded defaults

Nothing outside this module reads ``os.environ`` directly. Secrets (``*_API_KEY``)
are never read from ``vyvcode.toml`` — env or ``.env`` only — and never appear in
logs, run artifacts, or memory files (see :func:`redact`).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROLES = ("communicator", "planner", "coder", "reviewer")  # startup-probed
EXTRA_ROLES = ("researcher", "strategist")  # probed lazily (autoresearch only)
ALL_ROLES = ROLES + EXTRA_ROLES

_ROLE_DEFAULTS: dict[str, dict[str, str | None]] = {
    "communicator": {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "key_env": "OPENAI_API_KEY",
        "base_url": None,
    },
    "planner": {
        "model": "anthropic/claude-fable-5",
        "effort": "max",
        "key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
    },
    "coder": {
        "model": "openai/kimi-k3",
        "effort": "max",
        "key_env": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.ai/v1",
    },
    "reviewer": {
        "model": "anthropic/claude-opus-5",
        "effort": "xhigh",
        "key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
    },
    "researcher": {
        "model": "openai/kimi-k3",
        "effort": "max",
        "key_env": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.ai/v1",
    },
    "strategist": {
        "model": "anthropic/claude-fable-5",
        "effort": "max",
        "key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
    },
}

# Autoresearch knobs (addendum §4.1): env var suffix -> (default, parser)
_AR_KNOB_DEFAULTS: dict[str, tuple[object, str]] = {
    "AR_REPO": ("https://github.com/karpathy/autoresearch", "str"),
    "AR_DIR": ("~/autoresearch", "str"),
    "AR_MAX_EXPERIMENTS": (0, "int"),
    "AR_MAX_HOURS": (0.0, "float"),
    "AR_STRATEGY_EVERY": (10, "int"),
    "AR_EPSILON": (0.0, "float"),
    "AR_RUN_TIMEOUT_MIN": (10.0, "float"),
    "AR_CRASH_STREAK_LIMIT": (3, "int"),
}

# Orchestration knobs: env var suffix -> (default, parser)
_KNOB_DEFAULTS: dict[str, tuple[object, str]] = {
    "MAX_PARALLEL_CODERS": (4, "int"),
    "MAX_REVIEW_CYCLES": (5, "int"),
    "PLAN_GATE": (False, "bool"),
    "OPTIMIZER_MIN_TOKENS": (60, "int"),
    "GRILL_ROUNDS_PLAN": (4, "int"),
    "GRILL_ROUNDS_GOAL": (2, "int"),
    "CODER_MAX_ITER": (200, "int"),
    "CODER_MAX_BUDGET": (None, "float_opt"),
    "DEFAULT_MODE": ("fast", "str"),
}


@dataclass(frozen=True)
class RoleConfig:
    name: str
    model: str
    effort: str
    api_key: str | None
    api_key_env: str
    base_url: str | None
    fallbacks: tuple[str, ...] = ()

    @property
    def usage_id(self) -> str:
        return f"vyvcode-{self.name}"


@dataclass(frozen=True)
class VyvConfig:
    project_root: Path
    roles: Mapping[str, RoleConfig]
    max_parallel_coders: int
    max_review_cycles: int
    plan_gate: bool
    optimizer_min_tokens: int
    grill_rounds_plan: int
    grill_rounds_goal: int
    coder_max_iter: int
    coder_max_budget: float | None
    default_mode: str
    memory_enabled: bool
    memsearch_provider: str = "onnx"
    memsearch_model: str = "gpahal/bge-m3-onnx-int8"
    ar_repo: str = "https://github.com/karpathy/autoresearch"
    ar_dir: Path = Path("~/autoresearch")
    ar_max_experiments: int = 0
    ar_max_hours: float = 0.0
    ar_strategy_every: int = 10
    ar_epsilon: float = 0.0
    ar_run_timeout_min: float = 10.0
    ar_crash_streak_limit: int = 3
    secret_values: tuple[str, ...] = field(default=(), repr=False)

    @property
    def runs_dir(self) -> Path:
        return self.project_root / ".vyvcode" / "runs"

    @property
    def memory_dir(self) -> Path:
        return self.project_root / ".memsearch" / "memory"


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _toml_lookup(toml_cfg: Mapping, env_name: str) -> str | None:
    """Map a VYVCODE_* env name onto its vyvcode.toml table/key, if any."""
    if not env_name.startswith("VYVCODE_"):
        return None  # provider API keys are env/.env only, never toml
    suffix = env_name[len("VYVCODE_") :]
    if suffix == "MEMORY":
        section, key = "memory", "enabled"
    elif suffix.startswith("AR_"):
        section, key = "research", suffix[len("AR_") :].lower()
    elif suffix.split("_", 1)[0].lower() in ALL_ROLES:
        section, key = "models", suffix.lower()
    else:
        section, key = "orchestration", suffix.lower()
    value = toml_cfg.get(section, {}).get(key)
    if value is None:
        return None
    return str(value)


class ConfigError(Exception):
    pass


# Knobs whose value must be at least 1 to be usable at all: each one sizes a
# loop or a thread pool, and 0 or a negative number fails deep inside a run
# (ThreadPoolExecutor rejects max_workers < 1) rather than here.
_MIN_ONE_KNOBS = frozenset(
    {
        "MAX_PARALLEL_CODERS",
        "MAX_REVIEW_CYCLES",
        "GRILL_ROUNDS_PLAN",
        "GRILL_ROUNDS_GOAL",
        "CODER_MAX_ITER",
    }
)


def _parse_number(var: str, raw: str, kind: str) -> object:
    try:
        if kind == "int":
            value = int(raw)
        elif kind == "float":
            value = float(raw)
        else:  # float_opt
            return float(raw) if raw.strip() else None
    except ValueError:
        expected = "an integer" if kind == "int" else "a number"
        raise ConfigError(f"{var}={raw!r} is not {expected}") from None
    suffix = var[len("VYVCODE_") :]
    if suffix in _MIN_ONE_KNOBS and value < 1:
        raise ConfigError(f"{var}={raw!r} must be 1 or greater")
    if value < 0:
        raise ConfigError(f"{var}={raw!r} must not be negative")
    return value


def load_config(
    project_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> VyvConfig:
    """Build the frozen VyvConfig for a project directory.

    ``env`` defaults to ``os.environ``; tests pass a plain dict. Neither the
    process environment nor any global state is mutated.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    proc_env: dict[str, str] = dict(env if env is not None else os.environ)

    env_file = root / ".env"
    file_env: dict[str, str] = {}
    if env_file.is_file():
        # python-dotenv quirk: "X=   # note" parses to value "# note" (the
        # inline comment survives only when the value is empty). Treat those
        # as unset.
        file_env = {
            k: v
            for k, v in dotenv_values(env_file).items()
            if v is not None and not v.lstrip().startswith("#")
        }

    toml_path = root / "vyvcode.toml"
    toml_cfg: Mapping = {}
    if toml_path.is_file():
        with open(toml_path, "rb") as f:
            toml_cfg = tomllib.load(f)

    def get(name: str, default: str | None = None) -> str | None:
        for source in (proc_env, file_env):
            value = source.get(name)
            if value is not None and value != "":
                return value
        value = _toml_lookup(toml_cfg, name)
        if value is not None and value != "":
            return value
        return default

    def get_clearable(name: str, default: str | None = None) -> str | None:
        """Like get(), but an explicitly empty value clears the default."""
        for source in (proc_env, file_env):
            value = source.get(name)
            if value is not None:
                return value or None
        value = _toml_lookup(toml_cfg, name)
        if value is not None:
            return value or None
        return default

    roles: dict[str, RoleConfig] = {}
    for role in ALL_ROLES:
        d = _ROLE_DEFAULTS[role]
        prefix = f"VYVCODE_{role.upper()}"
        model = get(f"{prefix}_MODEL", d["model"])
        base_url = get_clearable(f"{prefix}_BASE_URL", d["base_url"])
        key_env = get(f"{prefix}_API_KEY_ENV") or _infer_key_env(
            model, base_url, d["key_env"]
        )
        raw_fallbacks = get(f"{prefix}_FALLBACKS", "") or ""
        roles[role] = RoleConfig(
            name=role,
            model=model,
            effort=get(f"{prefix}_EFFORT", d["effort"]),
            api_key=get(key_env),
            api_key_env=key_env,
            base_url=base_url,
            fallbacks=tuple(m.strip() for m in raw_fallbacks.split(",") if m.strip()),
        )

    knobs: dict[str, object] = {}
    for suffix, (default, kind) in _KNOB_DEFAULTS.items():
        raw = get(f"VYVCODE_{suffix}")
        if raw is None:
            knobs[suffix] = default
        elif kind == "bool":
            knobs[suffix] = _parse_bool(raw)
        elif kind in ("int", "float", "float_opt"):
            knobs[suffix] = _parse_number(f"VYVCODE_{suffix}", raw, kind)
        else:
            knobs[suffix] = raw

    ar_knobs: dict[str, object] = {}
    for suffix, (default, kind) in _AR_KNOB_DEFAULTS.items():
        raw = get(f"VYVCODE_{suffix}")
        if raw is None:
            ar_knobs[suffix] = default
        elif kind in ("int", "float"):
            ar_knobs[suffix] = _parse_number(f"VYVCODE_{suffix}", raw, kind)
        else:
            ar_knobs[suffix] = raw

    memory_raw = get("VYVCODE_MEMORY", "true") or "true"
    secrets = tuple(
        sorted({r.api_key for r in roles.values() if r.api_key and len(r.api_key) >= 8})
    )

    return VyvConfig(
        project_root=root,
        roles=roles,
        max_parallel_coders=knobs["MAX_PARALLEL_CODERS"],  # type: ignore[arg-type]
        max_review_cycles=knobs["MAX_REVIEW_CYCLES"],  # type: ignore[arg-type]
        plan_gate=knobs["PLAN_GATE"],  # type: ignore[arg-type]
        optimizer_min_tokens=knobs["OPTIMIZER_MIN_TOKENS"],  # type: ignore[arg-type]
        grill_rounds_plan=knobs["GRILL_ROUNDS_PLAN"],  # type: ignore[arg-type]
        grill_rounds_goal=knobs["GRILL_ROUNDS_GOAL"],  # type: ignore[arg-type]
        coder_max_iter=knobs["CODER_MAX_ITER"],  # type: ignore[arg-type]
        coder_max_budget=knobs["CODER_MAX_BUDGET"],  # type: ignore[arg-type]
        default_mode=knobs["DEFAULT_MODE"],  # type: ignore[arg-type]
        memory_enabled=_parse_bool(memory_raw),
        # Empty string = defer to memsearch's own config instead of forcing.
        memsearch_provider=get_clearable("VYVCODE_MEMSEARCH_PROVIDER", "onnx") or "",
        memsearch_model=get_clearable(
            "VYVCODE_MEMSEARCH_MODEL", "gpahal/bge-m3-onnx-int8"
        ) or "",
        ar_repo=ar_knobs["AR_REPO"],  # type: ignore[arg-type]
        ar_dir=Path(str(ar_knobs["AR_DIR"])).expanduser(),
        ar_max_experiments=ar_knobs["AR_MAX_EXPERIMENTS"],  # type: ignore[arg-type]
        ar_max_hours=ar_knobs["AR_MAX_HOURS"],  # type: ignore[arg-type]
        ar_strategy_every=ar_knobs["AR_STRATEGY_EVERY"],  # type: ignore[arg-type]
        ar_epsilon=ar_knobs["AR_EPSILON"],  # type: ignore[arg-type]
        ar_run_timeout_min=ar_knobs["AR_RUN_TIMEOUT_MIN"],  # type: ignore[arg-type]
        ar_crash_streak_limit=ar_knobs["AR_CRASH_STREAK_LIMIT"],  # type: ignore[arg-type]
        secret_values=secrets,
    )


def _infer_key_env(model: str | None, base_url: str | None, role_default: str) -> str:
    """Pick the provider key env for a (possibly overridden) model string."""
    model = model or ""
    if base_url and "moonshot" in base_url:
        return "MOONSHOT_API_KEY"
    if model.startswith(("anthropic/", "claude")):
        return "ANTHROPIC_API_KEY"
    if model.startswith("openrouter/"):
        return "OPENROUTER_API_KEY"
    if model.startswith(("openai/", "gpt")):
        return "OPENAI_API_KEY"
    return role_default


# ── Redaction ────────────────────────────────────────────────────────────────

REDACTED = "«redacted»"

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password)(\s*[=:]\s*)(?!«)(\S+)",
    ),
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Strip anything secret-shaped plus every known live secret value.

    Applied to logs, metrics, run artifacts, memory digests, reports, and any
    prompt crossing a provider boundary. Over-redaction is acceptable; leakage
    is not.
    """
    for value in secrets:
        if value:
            text = text.replace(value, REDACTED)
    text = _SECRET_PATTERNS[0].sub(REDACTED, text)
    text = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text
