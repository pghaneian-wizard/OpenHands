"""Persistent semantic memory via memsearch (runbook §12).

Markdown under ``<project>/.memsearch/memory/YYYY-MM-DD.md`` is the source of
truth; Milvus Lite (``<project>/.memsearch/milvus.db``) is a local, rebuildable
shadow index. All memsearch interaction goes through its CLI. Memory is
best-effort: indexing/search failures degrade to empty results, never crash a
run. Every digest passes the redaction filter before touching disk.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

from openhands.sdk import Message, TextContent

from vyvcode.config import VyvConfig, redact
from vyvcode.optimizer import count_tokens, response_text

DIGEST_PROMPT = """\
Digest this run report into 3-8 terse markdown bullets for project memory:
goal, key decisions, stack, outcomes, open issues, and file/branch names.
Output only the bullets.
"""

RECALL_PROMPT = """\
Answer the user's question ONLY from the memory chunks below. Cite the memory
date (from the chunk's source filename) for every fact you use. If the answer
is not in the chunks, reply exactly: not in memory. Never invent.
"""


def _memsearch_bin() -> str | None:
    candidate = Path(sys.executable).with_name("memsearch")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("memsearch")


def _flags(cfg: VyvConfig) -> list[str]:
    flags = ["--milvus-uri", str(cfg.project_root / ".memsearch" / "milvus.db")]
    if cfg.memsearch_provider:
        flags += ["-p", cfg.memsearch_provider]
    if cfg.memsearch_model:
        flags += ["-m", cfg.memsearch_model]
    return flags


def _memsearch(cfg: VyvConfig, *args: str, timeout: int = 300) -> tuple[int, str]:
    binary = _memsearch_bin()
    if binary is None:
        return 127, "memsearch binary not found"
    try:
        proc = subprocess.run(
            [binary, *args],
            cwd=cfg.project_root, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ── write path (§12.2) ───────────────────────────────────────────────────────


def index(cfg: VyvConfig) -> bool:
    rc, _ = _memsearch(cfg, "index", *_flags(cfg), str(cfg.memory_dir))
    return rc == 0


def make_digest(cfg: VyvConfig, llm, report_md: str, goal: str) -> str:
    """3-8 bullet digest via the Communicator; degrades to report head lines."""
    try:
        reply = response_text(
            llm.completion(
                messages=[
                    Message(role="system", content=[TextContent(text=DIGEST_PROMPT)]),
                    Message(role="user", content=[TextContent(text=report_md)]),
                ]
            )
        ).strip()
        if reply:
            return reply
    except Exception:
        pass
    head = [line for line in report_md.splitlines() if line.strip()][:6]
    return "\n".join(f"- {line.lstrip('#- ').strip()}" for line in head)


def write_digest(
    cfg: VyvConfig, run_id: str, digest_md: str, goal: str = ""
) -> Path | None:
    if not cfg.memory_enabled:
        return None
    now = _dt.datetime.now(_dt.UTC)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.memory_dir / f"{now:%Y-%m-%d}.md"
    goal_line = redact(goal.splitlines()[0][:120] if goal else run_id,
                       cfg.secret_values)
    entry = (
        f"\n<!-- session:{run_id} -->\n"
        f"## {now:%H:%M} {run_id} — {goal_line}\n"
        f"{redact(digest_md, cfg.secret_values).strip()}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    index(cfg)
    return path


# ── read path (§12.3 / §12.4) ────────────────────────────────────────────────


def search(cfg: VyvConfig, query: str, k: int = 5) -> list[dict]:
    rc, output = _memsearch(cfg, "search", *_flags(cfg), "-j", "-k", str(k), query)
    if rc != 0:
        return []
    try:
        results = json.loads(output)
        return results if isinstance(results, list) else []
    except json.JSONDecodeError:
        return []


def expand(cfg: VyvConfig, chunk_hash: str) -> str:
    rc, output = _memsearch(cfg, "expand", *_flags(cfg), chunk_hash)
    return output.strip() if rc == 0 else ""


def context_for(cfg: VyvConfig, query: str, cap_tokens: int = 1500) -> str:
    """Top-5 memory chunks injected before grill and planning; never overrides
    the current BRIEF."""
    if not cfg.memory_enabled:
        return ""
    chunks = []
    for result in search(cfg, query, k=5):
        source = Path(str(result.get("source", ""))).name
        chunks.append(f"[{source}] {result.get('content', '').strip()}")
    text = "\n\n".join(chunks)
    while text and count_tokens(text) > cap_tokens:
        text = text[: int(len(text) * 0.8)]
    return text


def recall(cfg: VyvConfig, query: str, llm=None) -> str:
    """`/memory-recall`: L1 search → L2 expand → answer with sources."""
    results = search(cfg, query, k=5)
    if not results:
        return "not in memory"

    top = results[0]
    if len(str(top.get("content", ""))) >= 500 and top.get("chunk_hash"):
        expanded = expand(cfg, str(top["chunk_hash"]))
        if expanded:
            top = dict(top, content=expanded)
            results = [top, *results[1:]]

    chunks = "\n\n".join(
        f"[source: {Path(str(r.get('source', ''))).name}]\n{r.get('content', '')}"
        for r in results
    )
    if llm is None:
        from vyvcode.models import llm_for

        llm = llm_for("communicator", cfg)
    try:
        return response_text(
            llm.completion(
                messages=[
                    Message(role="system", content=[TextContent(text=RECALL_PROMPT)]),
                    Message(
                        role="user",
                        content=[TextContent(text=f"Question: {query}\n\n{chunks}")],
                    ),
                ]
            )
        ).strip()
    except Exception:
        return f"(communicator unavailable — raw memory chunks)\n\n{chunks}"
