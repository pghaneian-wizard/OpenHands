"""No-network integration: /goal builds a python CLI todo app end to end (§15).

All four roles are scripted; the swarm, merge queue, completion gates, review
loop, report, and memory write path are the real machinery on a real git repo.
"""

import json
import subprocess
import sys

import pytest

from vyvcode import memory
from vyvcode.config import load_config
from vyvcode.pipeline import run_pipeline
from vyvcode.reviewer import review_loop
from vyvcode.run_state import latest_run
from vyvcode.swarm import execute_swarm

from tests.fakes import ScriptedChatLLM
from tests.test_memory import FakeMemsearch
from tests.test_planner import HEADER
from tests.test_swarm import commit_all, make_repo, write_report

GRILL_TERMINAL = """\
FRONTIER-EMPTY

# BRIEF
goal: build a python CLI todo app
context: fresh repo
decisions:
  - storage: json file
  - interface: argparse CLI
constraints:
  - python stdlib only
success_criteria:
  - todo.py add/list round-trips an item
"""

PLAN = (
    HEADER
    + """
## Phase P1: todo core
```yaml
id: P1
depends_on: []
worktree_branch: vyvcode/it/P1-core
est_files: [todo.py]
```
### Objective
The todo CLI.
### Deliverables
todo.py with add/list.
### Interfaces provided
todo.py CLI: add <text>, list
### Steps
1. write it
### Acceptance
- run: `python3 todo.py add milk && python3 todo.py list | grep milk` → expect: exit 0

## Phase F: final
```yaml
id: F
depends_on: [P1]
worktree_branch: vyvcode/it/F-final
est_files: [README.md]
```
### Objective
Wrap up.
### Deliverables
README.
### Interfaces provided
(none)
### Steps
1. document
### Acceptance
- run: `python3 todo.py add eggs && python3 todo.py list | grep eggs` → expect: exit 0
"""
)

TODO_PY = """\
import json, sys, pathlib
DB = pathlib.Path("todos.json")
def load():
    return json.loads(DB.read_text()) if DB.exists() else []
def main():
    cmd = sys.argv[1]
    items = load()
    if cmd == "add":
        items.append(" ".join(sys.argv[2:]))
        DB.write_text(json.dumps(items))
    elif cmd == "list":
        print("\\n".join(items))
main()
"""

APPROVED = (
    'looks solid\n\n```json\n'
    + json.dumps({"verdict": "APPROVED", "cycle": 1, "issues": [
        {"id": "R1", "severity": "minor", "phase": "P1", "files": ["todo.py"],
         "problem": "no --help text", "required_fix": "add argparse help"}]})
    + "\n```"
)


def coder(prompt, worktree, phase_id):
    if phase_id == "P1":
        (worktree / "todo.py").write_text(TODO_PY)
    else:
        (worktree / "README.md").write_text("# todo\npython3 todo.py add <text>\n")
    commit_all(worktree, f"{phase_id} work")
    write_report(worktree, phase_id)
    return "built"


class TestFullPipeline:
    def test_goal_pipeline_reaches_done_with_report_and_memory(
        self, tmp_path, monkeypatch
    ):
        make_repo(tmp_path)
        cfg = load_config(tmp_path, env={"VYVCODE_MAX_PARALLEL_CODERS": "2"})
        fake_memsearch = FakeMemsearch()
        monkeypatch.setattr(memory, "_memsearch", fake_memsearch)
        communicator = ScriptedChatLLM([
            GRILL_TERMINAL,
            "- built a stdlib todo CLI (todo.py)\n- suite green\n- branch merged",
        ])
        printed = []

        closing = run_pipeline(
            cfg,
            mode="goal",
            raw_text="build a python CLI todo app",
            ask_user=lambda: "all recommended",
            out=printed.append,
            communicator=communicator,
            planner_runner=lambda task: PLAN,
            swarm_fn=lambda cfg, run, plan, out: execute_swarm(
                cfg, run, plan, out=out, coder_runner=coder
            ),
            review_fn=lambda cfg, run, plan, swarm, out: review_loop(
                cfg, run, plan, swarm, out=out,
                reviewer_runner=lambda task: APPROVED,
                fix_runner=lambda prompt, cid: None,
            ),
        )

        run = latest_run(cfg.runs_dir)
        assert run.state == "DONE"
        assert closing == f"run {run.run_id}: DONE"

        report = (run.dir / "report.md").read_text()
        assert "final verdict: APPROVED" in report
        assert "| P1 | todo core | MERGED |" in report
        assert "full suite: 1/1 passed" in report
        assert "[R1] no --help text" in report

        # The harness's own suite run really executed todo.py on integration.
        integration = tmp_path / ".vyvcode" / "worktrees" / run.run_id / "integration"
        assert (integration / "todo.py").is_file()
        listing = subprocess.run(
            ["git", "ls-tree", "--name-only", f"vyvcode/{run.run_id}/integration"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert {"todo.py", "README.md"} <= set(listing)

        memory_files = list(cfg.memory_dir.glob("*.md"))
        assert len(memory_files) == 1
        digest = memory_files[0].read_text()
        assert f"<!-- session:{run.run_id} -->" in digest
        assert "stdlib todo CLI" in digest


class TestReplSmoke:
    def test_piped_repl_status_and_unknown_command(self, tmp_path):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        proc = subprocess.run(
            [sys.executable, "-m", "vyvcode.cli", "--project", str(tmp_path),
             "--no-probe"],
            input="/vyvcode:status\n/frobnicate\n",
            capture_output=True, text=True, timeout=120, env=env,
            cwd="/home/pj/vyvcode/vyvcode",
        )

        assert proc.returncode == 0
        assert "IDLE — no runs yet" in proc.stdout
        assert "unknown command /frobnicate" in proc.stdout
