"""State machine: transitions, persistence, abort, status rendering."""

import json
import os

import pytest

from vyvcode.run_state import (
    Run,
    StateError,
    abort_active,
    active_run,
    all_runs,
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

    def test_phase_update_never_resurrects_a_run_aborted_elsewhere(self, tmp_path):
        # /vyvcode:stop runs in a second process while the swarm keeps writing
        # phase rows; the swarm's whole-document save must not undo the abort.
        swarm_view = Run(tmp_path, "long build")
        swarm_view.to("GRILL")
        swarm_view.to("BRIEF")
        swarm_view.to("PLANNING")
        swarm_view.to("EXECUTING")

        Run.load(swarm_view.dir).to("ABORTED")  # the other process
        swarm_view.set_phase("P1", status="MERGED")

        assert Run.load(swarm_view.dir).state == "ABORTED"
        assert swarm_view.is_aborted

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

    def test_corrupt_state_file_does_not_brick_the_dispatcher(self, tmp_path):
        good = Run(tmp_path, "healthy")
        good.to("GRILL")
        good.to("BRIEF")
        broken = tmp_path / "run_29990101_000000_broken"
        broken.mkdir()
        (broken / "state.json").write_text("{ truncated", encoding="utf-8")

        # A half-written state.json must not take down status/stop/pipeline.
        assert [r.run_id for r in all_runs(tmp_path)] == [good.run_id]
        assert render_status(tmp_path) == f"{good.run_id}: BRIEF"
        assert abort_active(tmp_path) == good.run_id

    def test_save_is_atomic_leaving_no_partial_file(self, tmp_path):
        run = Run(tmp_path, "atomic")
        run.to("GRILL")

        leftovers = [p.name for p in run.dir.iterdir() if p.name != "state.json"]

        assert json.loads(run.state_path.read_text(encoding="utf-8"))["state"] == "GRILL"
        assert not [n for n in leftovers if n.startswith("state.json")]

    def test_reboot_makes_a_recycled_pid_count_as_dead(self, tmp_path):
        stale = Run(tmp_path, "survived a reboot")
        stale.to("GRILL")
        stale._data["pid"] = os.getpid()  # pid now belongs to an unrelated process
        stale._data["boot"] = "0000-boot-id-from-a-previous-boot"
        stale._save()

        assert active_run(tmp_path) is None

    def test_two_runs_in_the_same_second_get_distinct_ids(self, tmp_path):
        first = Run(tmp_path, "same goal text")
        first.to("GRILL")

        second = Run(tmp_path, "same goal text")

        assert second.run_id != first.run_id
        assert second.state == "IDLE"
        assert first.reload().state == "GRILL"

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
