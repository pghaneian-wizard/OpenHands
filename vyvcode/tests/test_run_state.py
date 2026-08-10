"""State machine: transitions, persistence, abort, status rendering."""

import pytest

from vyvcode.run_state import (
    Run,
    StateError,
    abort_active,
    active_run,
    new_run_id,
    render_status,
)


class TestStateMachine:
    def test_full_happy_path(self, tmp_path):
        run = Run(tmp_path, "build a todo app")

        for state in (
            "GRILL", "BRIEF", "PLANNING", "EXECUTING", "MERGING",
            "REVIEWING", "FIXING", "REVIEWING", "REPORTING", "DONE",
        ):
            run.to(state)

        assert run.state == "DONE"
        reloaded = Run.load(run.dir)
        assert reloaded.state == "DONE"
        assert [s for _, s in reloaded._data["history"]][-1] == "DONE"

    def test_invalid_transition_raises(self, tmp_path):
        run = Run(tmp_path, "x")

        with pytest.raises(StateError, match="IDLE → EXECUTING"):
            run.to("EXECUTING")

    def test_abort_allowed_from_any_live_state_but_not_terminal(self, tmp_path):
        run = Run(tmp_path, "x")
        run.to("GRILL")
        run.to("BRIEF")

        run.to("ABORTED")

        with pytest.raises(StateError):
            run.to("ABORTED")

    def test_is_aborted_seen_across_instances(self, tmp_path):
        run = Run(tmp_path, "x")
        other = Run.load(run.dir)

        run.to("GRILL")
        run.to("ABORTED")

        assert other.is_aborted is True


class TestHelpers:
    def test_run_id_format_and_slug(self):
        run_id = new_run_id("Add a --json flag!")

        assert run_id.startswith("run_")
        assert run_id.endswith("_add-a-json-flag")

    def test_active_run_ignores_terminal_runs(self, tmp_path):
        done = Run(tmp_path, "first")
        for state in ("GRILL", "BRIEF", "PLANNING", "EXECUTING", "REVIEWING",
                      "REPORTING", "DONE"):
            done.to(state)

        assert active_run(tmp_path) is None

        live = Run(tmp_path, "zecond")
        live.to("GRILL")

        assert active_run(tmp_path).run_id == live.run_id

    def test_active_run_clears_run_whose_process_died(self, tmp_path):
        crashed = Run(tmp_path, "crashy")
        crashed.to("GRILL")
        crashed._data["pid"] = 999999999  # guaranteed-dead pid
        crashed._save()

        assert active_run(tmp_path) is None
        assert Run.load(crashed.dir).state == "ABORTED"

    def test_active_run_clears_legacy_run_without_pid(self, tmp_path):
        stale = Run(tmp_path, "old-format")
        stale.to("GRILL")
        del stale._data["pid"]
        stale._save()

        assert active_run(tmp_path) is None

    def test_abort_active_and_status_render(self, tmp_path):
        assert render_status(tmp_path) == "IDLE — no runs yet"
        assert abort_active(tmp_path) is None

        run = Run(tmp_path, "goal")
        run.to("GRILL")
        run.set_phase("P1", name="core", status="BUILDING")

        status = render_status(tmp_path)
        assert run.run_id in status
        assert "P1 core: BUILDING" in status

        assert abort_active(tmp_path) == run.run_id
        assert Run.load(run.dir).state == "ABORTED"
