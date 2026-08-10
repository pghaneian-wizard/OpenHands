"""Inbound token optimizer: the Communicator's rewrite pass (runbook §5).

Every user message crosses this before any downstream model sees it:

    raw → bypass check → protect spans → LLM rewrite → reinsert → verify → metrics

Hard rules enforced here, not trusted to the model:
- protected spans (code, paths, URLs, env vars, quotes, error lines,
  ``verbatim:`` lines) come back byte-identical or the whole rewrite is
  discarded in favor of the raw input;
- the rewrite must not grow the token count;
- any verification failure falls back to the raw input and logs why.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from openhands.sdk import Message, TextContent

from vyvcode.config import VyvConfig

# Verbatim from runbook §5.2. <style> is filled with the stop-slop rules.
OPTIMIZER_PROMPT = """\
You compress and correct a developer's message before it is sent to other AI
models. Rewrite the text between <input> tags.

Rules:
- Fix all spelling and grammar.
- Shrink aggressively: remove filler, repetition, hedging, and politeness.
  Prefer imperative phrasing.
- LOSSLESS: every requirement, constraint, name, number, file, flag, negation,
  and ordering in the input MUST appear in the output. If unsure whether
  something is load-bearing, keep it.
- Do not touch placeholder tokens of the form ⟦V<n>⟧. Keep each exactly once,
  in a position that preserves its original meaning.
- Apply the stop-slop rules provided in <style> (they govern YOUR prose:
  active voice, no filler phrases, no em dashes, no formulaic contrasts).
- Output ONLY the rewritten text. No preamble, no commentary, no quotes.
"""

# Minimal built-in style rules; superseded by the vendored stop-slop skill
# once B5's installer has placed it (load_style_block prefers the real files).
_BUILTIN_STYLE = """\
Core rules: active voice. Delete filler phrases (just, really, basically,
actually, simply, very). No em dashes. No formulaic contrasts ("not X, but Y").
No hedging. No restating the request back. Short words over long ones.
"""

_PLACEHOLDER = "⟦V{n}⟧"
_PLACEHOLDER_RE = re.compile(r"⟦V\d+⟧")

# Priority order matters: fences before inline code, URLs before bare paths.
# Single-quoted spans are protected only when space-free so apostrophes in
# prose ("don't ... user's") cannot pair up and freeze a chunk of the message.
_PROTECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```.*?```", re.S),
    re.compile(r"(?m)verbatim:.*$"),
    re.compile(
        r'(?m)^.*(?:Traceback \(most recent call last\)|\w+Error\b|\bException\b'
        r'|File "[^"]+", line \d+).*$'
    ),
    re.compile(r"`[^`\n]+`"),
    re.compile(r'"[^"\n]*"'),
    re.compile(r"'[^'\s]+'"),
    re.compile(r"https?://[^\s⟦⟧]+"),
    re.compile(r"(\.{0,2}/)?[\w.\-]+(/[\w.\-]+)+"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"),
)


@dataclass(frozen=True)
class OptimizeResult:
    text: str
    skipped: bool = False
    fallback: bool = False
    reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def saved_pct(self) -> float:
        if self.tokens_in == 0:
            return 0.0
        return round(100.0 * (self.tokens_in - self.tokens_out) / self.tokens_in, 1)


_ENCODER = None


def count_tokens(text: str) -> int:
    """tiktoken o200k_base; close enough across all three providers (§5.1)."""
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception:
            _ENCODER = False
    if _ENCODER:
        return len(_ENCODER.encode(text))
    return max(1, len(text) // 4)


def protect(text: str) -> tuple[str, dict[str, str]]:
    """Replace protected spans with ⟦Vn⟧ placeholders, in priority order.

    Later patterns can match lines that already contain placeholders, so a
    stored span value may itself contain earlier placeholders; restore()
    expands iteratively.
    """
    spans: dict[str, str] = {}
    counter = 0

    def stash(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        key = _PLACEHOLDER.format(n=counter)
        spans[key] = match.group(0)
        return key

    for pattern in _PROTECT_PATTERNS:
        text = pattern.sub(stash, text)
    return text, spans


def restore(text: str, spans: dict[str, str]) -> str:
    for _ in range(len(spans) + 1):
        if not _PLACEHOLDER_RE.search(text):
            break
        for key, value in spans.items():
            text = text.replace(key, value)
    return text


def load_style_block(project_root: Path) -> str:
    """stop-slop rules: installed skill first, packaged copy second, builtin last."""
    candidates = [
        project_root / ".agents" / "skills" / "stop-slop",
        Path(__file__).resolve().parent.parent / "skills" / "stop-slop",
    ]
    for skill_dir in candidates:
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            parts = [skill_md.read_text(encoding="utf-8")]
            phrases = skill_dir / "references" / "phrases.md"
            if phrases.is_file():
                parts.append(phrases.read_text(encoding="utf-8"))
            return "\n\n".join(parts)
    return _BUILTIN_STYLE


def response_text(resp) -> str:
    """Extract plain text from an SDK LLMResponse (or anything shaped like one)."""
    message = getattr(resp, "message", resp)
    content = getattr(message, "content", None)
    if content is None:
        return str(message)
    texts = [c.text for c in content if getattr(c, "text", None)]
    return "".join(texts)


def optimize(
    raw: str,
    llm,
    cfg: VyvConfig,
    metrics_path: Path | None = None,
    bypass: bool = False,
    style_block: str | None = None,
) -> OptimizeResult:
    tokens_in = count_tokens(raw)

    def done(result: OptimizeResult) -> OptimizeResult:
        _append_metrics(metrics_path or cfg.project_root / ".vyvcode" / "metrics.jsonl", result)
        return result

    if bypass:
        return done(OptimizeResult(raw, skipped=True, reason="bypass",
                                   tokens_in=tokens_in, tokens_out=tokens_in))
    if tokens_in < cfg.optimizer_min_tokens:
        return done(OptimizeResult(raw, skipped=True, reason="below-min-tokens",
                                   tokens_in=tokens_in, tokens_out=tokens_in))

    protected, spans = protect(raw)
    top_level = [k for k in spans if k in protected]

    style = style_block if style_block is not None else load_style_block(cfg.project_root)
    messages = [
        Message(role="system", content=[TextContent(text=OPTIMIZER_PROMPT)]),
        Message(
            role="user",
            content=[TextContent(
                text=f"<style>\n{style}\n</style>\n<input>\n{protected}\n</input>"
            )],
        ),
    ]

    try:
        rewritten = response_text(llm.completion(messages=messages)).strip()
    except Exception as exc:
        return done(OptimizeResult(raw, fallback=True, reason=f"llm-error: {exc}",
                                   tokens_in=tokens_in, tokens_out=tokens_in))

    for key in top_level:
        if rewritten.count(key) != 1:
            return done(OptimizeResult(raw, fallback=True,
                                       reason=f"placeholder {key} lost or duplicated",
                                       tokens_in=tokens_in, tokens_out=tokens_in))
    if not set(_PLACEHOLDER_RE.findall(rewritten)) <= set(spans):
        return done(OptimizeResult(raw, fallback=True, reason="unknown placeholder invented",
                                   tokens_in=tokens_in, tokens_out=tokens_in))

    final = restore(rewritten, spans)

    for value in (spans[k] for k in top_level):
        if restore(value, spans) not in final:
            return done(OptimizeResult(raw, fallback=True, reason="protected span mutated",
                                       tokens_in=tokens_in, tokens_out=tokens_in))
    tokens_out = count_tokens(final)
    if tokens_out > tokens_in:
        return done(OptimizeResult(raw, fallback=True, reason="rewrite grew token count",
                                   tokens_in=tokens_in, tokens_out=tokens_in))

    return done(OptimizeResult(final, tokens_in=tokens_in, tokens_out=tokens_out))


def _append_metrics(path: Path, result: OptimizeResult) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "saved_pct": result.saved_pct,
            "fallback": result.fallback,
            "skipped": result.skipped,
            "reason": result.reason,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # metrics must never break the pipeline
