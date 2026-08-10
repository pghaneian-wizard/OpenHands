"""Grill engine: convergence, NEEDS-FACT explorer, cap forcing, protocol errors (§8)."""

import pytest

from vyvcode.config import load_config
from vyvcode.grill import GrillError, run_grill
from tests.fakes import ScriptedChatLLM

ROUND_1 = """\
❓ **Q1** - **Storage**: sqlite or postgres?

➡️ sqlite

NEEDS-FACT: does the repo already have a database dependency?
"""

ROUND_2 = """\
❓ **Q2** - **API style**: REST or CLI-only?

➡️ CLI-only
"""

TERMINAL = """\
FRONTIER-EMPTY

# BRIEF
goal: build a todo CLI
context: fresh repo
decisions:
  - storage: sqlite
  - api: CLI-only
constraints:
  - python
success_criteria:
  - `todo add` persists an item
"""

TERMINAL_ASSUMED = """\
FRONTIER-EMPTY

# BRIEF
goal: build a todo CLI
context: fresh repo
decisions:
  - storage: sqlite
assumed:
  - api: CLI-only (cap-forced recommendation)
constraints:
  - python
success_criteria:
  - `todo add` persists an item
"""


@pytest.fixture
def cfg(tmp_path):
    return load_config(tmp_path, env={})


class TestGrillLoop:
    def test_converges_with_explorer_and_verbatim_rounds(self, cfg):
        llm = ScriptedChatLLM([ROUND_1, ROUND_2, TERMINAL])
        explored = []
        shown = []

        result = run_grill(
            cfg, llm, seed="build todos", mode="plan",
            ask_user=lambda: "all recommended",
            out=shown.append,
            explorer=lambda q: explored.append(q) or "no db dependency found",
        )

        assert result.rounds == 2
        assert result.forced_assumptions is False
        assert "goal: build a todo CLI" in result.brief_md
        assert explored == ["does the repo already have a database dependency?"]
        assert shown == [ROUND_1.strip(), ROUND_2.strip()]
        fact_round = llm.histories[1][-1].content[0].text
        assert "FACT (does the repo already have a database dependency?)" in fact_round
        assert "## explorer" in result.transcript

    def test_default_explorer_toolset_registers_and_runs(self, tmp_path):
        # The real explorer requests READ_ONLY_TOOLS; a missing registration
        # only surfaces at Conversation start (KeyError: not registered).
        from tests.fakes import ScriptedLLM, text_message

        from vyvcode.subagents import READ_ONLY_TOOLS, run_agent_task

        answer = run_agent_task(
            ScriptedLLM.make([text_message("nothing relevant found")]),
            "you are an explorer",
            "look around",
            tmp_path,
            tools=READ_ONLY_TOOLS,
        )

        assert answer == "nothing relevant found"

    def test_cap_forces_assumed_section(self, cfg):
        llm = ScriptedChatLLM([ROUND_1, TERMINAL_ASSUMED])

        result = run_grill(
            cfg, llm, seed="build todos", mode="goal",
            ask_user=lambda: "all recommended",
            out=lambda _: None,
            explorer=lambda q: "none",
            rounds_cap=1,
        )

        assert result.forced_assumptions is True
        assert "assumed:" in result.brief_md
        cap_message = llm.histories[1][-1].content[0].text
        assert "Round cap reached" in cap_message

    def test_protocol_violation_after_nudges_raises(self, cfg):
        llm = ScriptedChatLLM(["hello", "still chatting", "nope"])

        with pytest.raises(GrillError, match="round protocol"):
            run_grill(
                cfg, llm, seed="x", mode="plan",
                ask_user=lambda: "", out=lambda _: None, explorer=lambda q: "",
            )

    def test_terminal_without_brief_raises(self, cfg):
        llm = ScriptedChatLLM(["FRONTIER-EMPTY\n\nno brief here"])

        with pytest.raises(GrillError, match="BRIEF"):
            run_grill(
                cfg, llm, seed="x", mode="plan",
                ask_user=lambda: "", out=lambda _: None, explorer=lambda q: "",
            )
