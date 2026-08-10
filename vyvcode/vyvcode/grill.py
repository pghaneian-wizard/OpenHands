"""The grill engine (runbook §8): mechanized frontier interview on the
Communicator model.

Protocol markers the harness parses:
- a round = one or more ``❓ **Qn**`` blocks, each with a ``➡️`` recommendation
- ``NEEDS-FACT: <what>`` — harness dispatches a read-only kimi-k3 explorer
- ``FRONTIER-EMPTY`` — terminal reply, followed by (brainstorm only) the design
  spec and always the BRIEF
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openhands.sdk import Message, TextContent

from vyvcode.assets import assets_root
from vyvcode.config import VyvConfig, redact
from vyvcode.models import llm_for
from vyvcode.optimizer import response_text
from vyvcode.run_state import slugify
from vyvcode.subagents import READ_ONLY_TOOLS, run_agent_task

FRONTIER_EMPTY = "FRONTIER-EMPTY"
_NEEDS_FACT = re.compile(r"(?m)^\s*NEEDS-FACT:\s*(.+)$")
_QUESTION_MARK = "❓"

SKILLS_DIR = assets_root() / "skills"

BRIEF_SCHEMA = """\
# BRIEF
goal: <one sentence>
context: <project facts that matter>
decisions:            # every settled Q → A pair, compressed
  - <topic>: <decision>
assumed:              # cap-forced recommendations, if any
  - <topic>: <assumption>
constraints:          # tech stack givens, hard requirements, non-goals
success_criteria:     # observable, testable
"""

_PROTOCOL = f"""
Harness protocol (your replies are machine-parsed):
- Every reply is either (a) a ROUND: one or more ❓ **Qn** blocks, each
  followed by a ➡️ recommended answer, plus optional NEEDS-FACT: lines for
  environment lookups; or (b) the TERMINAL reply.
- The terminal reply starts with the exact line {FRONTIER_EMPTY} and then
  contains the complete BRIEF in this schema (omit `assumed:` only when no
  question was cap-forced):

{BRIEF_SCHEMA}
- Never mix a round and the terminal reply.
- The user may answer "all recommended" to accept every ➡️ in the round.
- If the harness tells you the round cap is reached, resolve every open
  question with your own ➡️ recommendation, list each under `assumed:`, and
  emit the terminal reply.
"""

_BRAINSTORM_EXTRA = """
Brainstorm mode: your terminal reply contains, after the FRONTIER-EMPTY line
and before the BRIEF, the validated design spec under a `# DESIGN SPEC`
heading (purpose, approaches considered, chosen design, components, data
flow, error handling, testing).
"""

_NUDGE = (
    "Protocol violation: emit either a round of ❓ questions (each with a ➡️ "
    f"recommendation) or the terminal reply starting with {FRONTIER_EMPTY}."
)

_CAP_DIRECTIVE = (
    "Round cap reached. Do not ask further questions: resolve every open "
    "question with your own ➡️ recommendation, record each under `assumed:`, "
    f"and emit the terminal reply ({FRONTIER_EMPTY} + BRIEF) now."
)


class GrillError(Exception):
    pass


@dataclass
class GrillResult:
    brief_md: str
    transcript: str
    rounds: int
    forced_assumptions: bool
    spec_md: str | None = None


def _skill_body(name: str) -> str:
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def system_prompt(mode: str) -> str:
    parts = [_skill_body("vyvcode-grill")]
    if mode == "brainstorm":
        parts.append(_skill_body("vyvcode-brainstorm"))
        parts.append(_BRAINSTORM_EXTRA)
    parts.append(_PROTOCOL)
    return "\n\n".join(parts)


def gather_context(project_root: Path) -> str:
    """Auto-gathered project context: git log, tree top level, README head."""

    def run(*argv: str) -> str:
        try:
            proc = subprocess.run(
                list(argv), cwd=project_root, capture_output=True, text=True,
                timeout=15, check=False,
            )
            return proc.stdout.strip()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # ValueError covers UnicodeDecodeError from text=True: a latin-1
            # commit message or filename must not break context gathering.
            return ""

    parts = []
    log = run("git", "log", "--oneline", "-20")
    if log:
        parts.append(f"## git log -20\n{log}")
    tree = run("ls", "-1")
    if tree:
        parts.append(f"## top-level files\n{tree}")
    for name in ("README.md", "README.rst", "README"):
        readme = project_root / name
        if readme.is_file():
            head = "\n".join(readme.read_text(errors="replace").splitlines()[:40])
            parts.append(f"## {name} (head)\n{head}")
            break
    return "\n\n".join(parts) or "(empty project directory)"


def default_explorer(cfg: VyvConfig, question: str) -> str:
    """Throwaway read-only kimi-k3 explorer for NEEDS-FACT lookups."""
    return run_agent_task(
        llm_for("coder", cfg, usage_id="vyvcode-grill-explorer"),
        system_prompt=(
            "You are a read-only repository explorer. Answer the question "
            "using only read commands (ls, cat, grep, git log/show). Never "
            "modify anything. Reply with the fact, tersely."
        ),
        task=question,
        workspace=cfg.project_root,
        tools=READ_ONLY_TOOLS,
        max_iterations=15,
    )


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextContent(text=text)])


def _is_terminal(reply: str) -> bool:
    """Does this reply end the interview?

    The marker alone is not enough: the harness's own nudge and cap directives
    quote it, so a model that echoes the instruction while still asking
    questions would otherwise end the interview and then fail the BRIEF check,
    discarding every answered round.
    """
    if FRONTIER_EMPTY not in reply:
        return False
    first_line = next((ln for ln in reply.splitlines() if ln.strip()), "")
    return first_line.strip().startswith(FRONTIER_EMPTY) or "# BRIEF" in reply


def run_grill(
    cfg: VyvConfig,
    llm,
    seed: str,
    mode: str,
    ask_user,
    out,
    explorer=None,
    rounds_cap: int | None = None,
) -> GrillResult:
    """Round loop per §8. ``ask_user()`` collects one free-text reply per round."""
    explorer = explorer or (lambda q: default_explorer(cfg, q))
    history = [_msg("system", system_prompt(mode)), _msg("user", seed)]
    transcript: list[str] = [f"# Grill transcript ({mode})\n\n## seed\n{seed}"]
    rounds = 0
    nudges = 0
    force_attempts = 0
    forced = False

    while True:
        reply = response_text(llm.completion(messages=history)).strip()
        history.append(_msg("assistant", reply))
        transcript.append(f"\n## model\n{reply}")

        if _is_terminal(reply):
            spec, brief = _extract_terminal(reply, mode)
            return GrillResult(
                brief_md=brief,
                transcript="\n".join(transcript),
                rounds=rounds,
                forced_assumptions=forced,
                spec_md=spec,
            )

        if _QUESTION_MARK not in reply:
            nudges += 1  # reset below once a well-formed round arrives
            if nudges > 2:
                raise GrillError("model failed to follow the round protocol")
            history.append(_msg("user", _NUDGE))
            transcript.append(f"\n## harness\n{_NUDGE}")
            continue

        nudges = 0  # a lifetime budget would abort a long, healthy interview
        rounds += 1
        if forced:
            # Cap directive already sent; the model asked again instead of
            # terminating. Push once more, then give up loudly.
            force_attempts += 1
            if force_attempts > 2:
                raise GrillError("model ignored the round cap directive")
            history.append(_msg("user", _CAP_DIRECTIVE))
            transcript.append(f"\n## harness\n{_CAP_DIRECTIVE}")
            continue

        out(reply)  # grill rounds render verbatim — they are for the user

        # The explorer is a real agent with terminal access and the answer is
        # typed by hand, so both can carry a key. Neither may reach the
        # provider on the next round or the transcript on disk (§16).
        fact_lines = []
        for question in _NEEDS_FACT.findall(reply):
            finding = redact(explorer(question), cfg.secret_values)
            fact_lines.append(f"FACT ({question}): {finding}")
            transcript.append(f"\n## explorer\nQ: {question}\nA: {finding}")

        answer = redact(ask_user(), cfg.secret_values)
        transcript.append(f"\n## user\n{answer}")
        parts = [answer, *fact_lines]
        if rounds_cap is not None and rounds >= rounds_cap:
            forced = True
            parts.append(_CAP_DIRECTIVE)
            transcript.append(f"\n## harness\n{_CAP_DIRECTIVE}")
        history.append(_msg("user", "\n\n".join(parts)))


def _extract_terminal(reply: str, mode: str) -> tuple[str | None, str]:
    after = reply.split(FRONTIER_EMPTY, 1)[1]
    brief_idx = after.find("# BRIEF")
    if brief_idx < 0 or "goal:" not in after[brief_idx:]:
        raise GrillError("terminal reply lacks a valid BRIEF (# BRIEF with goal:)")
    brief = after[brief_idx:].strip() + "\n"
    spec = None
    if mode == "brainstorm":
        spec_idx = after.find("# DESIGN SPEC")
        if 0 <= spec_idx < brief_idx:
            spec = after[spec_idx:brief_idx].strip() + "\n"
    return spec, brief


def standalone(cfg: VyvConfig, topic: str, ask_user=None, out=print) -> str:
    """`/vyvcode:grill` — interview only, no handoff (runbook §1.3)."""
    ask_user = ask_user or (lambda: input("answers> "))
    seed = (
        f"Grill topic: {topic}\n\nPROJECT CONTEXT\n{gather_context(cfg.project_root)}"
    )
    result = run_grill(
        cfg, llm_for("communicator", cfg), seed, mode="grill",
        ask_user=ask_user, out=out, rounds_cap=cfg.grill_rounds_plan,
    )
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d_%H%M%S")
    dest = cfg.project_root / ".vyvcode" / "grill" / f"{stamp}_{slugify(topic)}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(result.transcript + "\n\n" + result.brief_md)
    return f"grill complete after {result.rounds} round(s); transcript: {dest}"
