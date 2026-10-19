"""Unit tests for the focus_calc job layer (no NATS, no pyaraucaria).

The JobManager is transport-agnostic: we inject recording publishers
and a fake clock, and register a fake in-memory method via
monkeypatching ``create_method``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ocabox_tcs.services.focus_calc_svc import jobs as jobs_mod
from ocabox_tcs.services.focus_calc_svc.jobs import JobManager
from ocabox_tcs.services.focus_calc_svc.methods import MethodResult, Suggestion


class FakeMethod:
    name = "fake"
    iterative = True
    suggests_next = True

    async def update(self, files: list[str], params: dict[str, Any]) -> MethodResult:
        return MethodResult(
            method=self.name,
            status="ok" if len(files) >= 3 else "partial",
            best_focus=15000.0 + len(files),
            files_used=len(files),
            suggestion=Suggestion(kind="next_position", position=100.0 * len(files)),
        )


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def harness(monkeypatch, tmp_path):
    results: list[tuple[str, dict]] = []
    states: list[tuple[str, dict]] = []

    async def pub_result(eid: str, payload: dict) -> None:
        results.append((eid, payload))

    async def pub_state(eid: str, payload: dict) -> None:
        states.append((eid, payload))

    monkeypatch.setattr(jobs_mod, "create_method", lambda name: FakeMethod())
    clock = FakeClock()
    mgr = JobManager(
        pub_result, pub_state,
        default_methods=["fake"],
        default_idle_timeout_s=60.0,
        max_jobs=2,
        clock=clock,
    )
    return mgr, results, states, clock, tmp_path


def _touch_fits(directory: Path, name: str) -> None:
    (directory / name).write_bytes(b"SIMPLE  =                    T")


async def test_open_publishes_opened_state(harness):
    mgr, _results, states, _clock, tmp = harness
    await mgr.handle_message("zb08.test", {"action": "open", "path": str(tmp)})
    assert states[0][1]["event"] == "opened"
    assert states[0][1]["effective_id"] == "zb08.test"
    assert "zb08.test" in mgr.jobs


async def test_implicit_open_via_path(harness):
    mgr, _results, states, _clock, tmp = harness
    await mgr.handle_message("j1", {"path": str(tmp)})
    assert states[0][1]["event"] == "opened"


async def test_open_without_path_rejected(harness):
    mgr, _results, states, _clock, _tmp = harness
    await mgr.handle_message("j1", {"action": "open"})
    assert states[0][1]["event"] == "rejected"
    assert not mgr.jobs


async def test_files_trigger_results_per_round(harness):
    mgr, results, _states, _clock, tmp = harness
    _touch_fits(tmp, "a.fits")   # present before open → ingested at open
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    assert len(results) == 1
    assert results[0][1]["status"] == "partial"
    assert results[0][1]["suggestion"]["kind"] == "next_position"

    _touch_fits(tmp, "b.fits")
    _touch_fits(tmp, "c.fits")
    await mgr.tick()             # poll discovers 2 new frames → round 2
    assert len(results) == 2
    assert results[1][1]["files_used"] == 3
    assert results[1][1]["status"] == "ok"
    assert results[1][1]["seq"] == 2


async def test_stop_closes_with_final_results(harness):
    mgr, _results, states, _clock, tmp = harness
    _touch_fits(tmp, "a.fits")
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    await mgr.handle_message("j1", {"action": "stop"})
    closed = states[-1][1]
    assert closed["event"] == "closed"
    assert closed["reason"] == "stop requested"
    assert "fake" in closed["final"]
    assert not mgr.jobs


async def test_idle_timeout_closes_job(harness):
    mgr, _results, states, clock, tmp = harness
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    clock.t += 61.0
    await mgr.tick()
    assert states[-1][1]["event"] == "closed"
    assert "idle" in states[-1][1]["reason"]


async def test_reused_id_gets_hash_suffix(harness):
    mgr, _results, states, _clock, tmp = harness
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    await mgr.handle_message("j1", {"action": "stop"})
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    reopened = states[-1][1]
    assert reopened["event"] == "opened"
    assert reopened["job_id"] == "j1"
    assert reopened["effective_id"].startswith("j1.h")
    assert len(reopened["effective_id"]) == len("j1.h") + 8


async def test_job_limit_rejects(harness):
    mgr, _results, states, _clock, tmp = harness
    await mgr.handle_message("j1", {"action": "open", "path": str(tmp)})
    await mgr.handle_message("j2", {"action": "open", "path": str(tmp)})
    await mgr.handle_message("j3", {"action": "open", "path": str(tmp)})
    assert states[-1][1]["event"] == "rejected"
    assert "limit" in states[-1][1]["reason"]


async def test_message_for_unknown_job_is_dropped(harness):
    mgr, results, states, _clock, _tmp = harness
    await mgr.handle_message("ghost", {"action": "stop"})
    assert not results and not states
