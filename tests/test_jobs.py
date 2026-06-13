from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from openconstraint_mcp.jobs import JobRegistry, JobRejectedError
from openconstraint_mcp.schemas import SolveResult


def _solve_result(status: str = "satisfied") -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x = 1;\n",
        stderr="",
        elapsed_ms=3,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=None,
    )


class _FakeProc:
    """An opaque process-handle stand-in passed through on_start/terminate.

    ``poll`` reports the process as already exited so the real
    ``_terminate_process_tree`` is a no-op on handles that outlive their solve
    (e.g. a shutdown that races a worker's finalize in tests that don't mock
    termination).
    """

    def poll(self) -> int:
        return 0


def _wait_until_terminal(registry: JobRegistry, job_id: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


def _patch_solve(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.jobs.solve_model_cancellable", fake)


def _patch_terminate(monkeypatch: pytest.MonkeyPatch, recorder: list[Any]) -> None:
    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        recorder.append(proc)

    monkeypatch.setattr("openconstraint_mcp.jobs._terminate_process_tree", _fake_terminate)


def test_submit_returns_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="var 1..5: x;\nsolve satisfy;")
        assert isinstance(job_id, str)
        assert job_id
    finally:
        registry.shutdown()


def test_fast_solve_reaches_succeeded_with_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result("optimal"))
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
    finally:
        registry.shutdown()


def test_solve_status_error_reaches_succeeded_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # D1.9: a structured solver `error` verdict is a SUCCEEDED job with the result
    # attached — `failed` is reserved for the absence of a result.
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result("error"))
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "error"
    finally:
        registry.shutdown()


def test_timed_out_result_reaches_timeout_state(monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out = SolveResult(
        status="timeout",
        solver="cp-sat",
        return_code=None,
        timed_out=True,
        stdout="",
        stderr="",
        elapsed_ms=9,
    )
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: timed_out)
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "timeout"
        assert registry.get(job_id).result is not None
    finally:
        registry.shutdown()


def test_runner_exception_reaches_failed_with_none_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise RuntimeError("managed binary blew up")

    _patch_solve(monkeypatch, _boom)
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "failed"
        status = registry.get(job_id)
        assert status.result is None
        assert status.message is not None
        assert "blew up" in status.message
    finally:
        registry.shutdown()


def test_cancel_running_job_reaches_cancelled_and_terminates_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        on_start(proc)
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the "process" dying unblocks the solve

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs._terminate_process_tree", _fake_terminate)

    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)
        registry.cancel(job_id)
        assert _wait_until_terminal(registry, job_id) == "cancelled"
        assert terminated == handles
        assert registry.get(job_id).result is None
    finally:
        release.set()
        registry.shutdown()


def test_get_unknown_job_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="unknown"):
            registry.get("does-not-exist")
    finally:
        registry.shutdown()


def test_submit_beyond_running_capacity_enqueues(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    started = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=2)
    try:
        running_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)
        queued_id = registry.submit(model="solve satisfy;")
        # The second submit cannot run (the only worker is busy) → it waits queued.
        assert registry.get(queued_id).state == "queued"
        assert registry.get(running_id).state == "running"
    finally:
        release.set()
        registry.shutdown()


def test_submit_beyond_queue_capacity_rejects_without_starting_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    solve_calls = 0
    lock = threading.Lock()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        nonlocal solve_calls
        with lock:
            solve_calls += 1
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=1)
    try:
        registry.submit(model="solve satisfy;")  # running
        assert started.wait(timeout=3)
        registry.submit(model="solve satisfy;")  # queued (fills the 1-slot queue)
        with pytest.raises(JobRejectedError):
            registry.submit(model="solve satisfy;")  # over capacity → rejected
        # The rejected submit must not have started a worker/solve.
        with lock:
            assert solve_calls == 1
    finally:
        release.set()
        registry.shutdown()


def test_retention_cap_evicts_oldest_terminal_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    # max_running_jobs=1 forces sequential completion, so "oldest terminal" is
    # deterministic (submission order == completion order).
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=2)
    try:
        first = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, first)
        second = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, second)
        third = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, third)

        with pytest.raises(ValueError, match="unknown"):
            registry.get(first)
        assert registry.get(second).state == "succeeded"
        assert registry.get(third).state == "succeeded"
        assert len(registry.list()) == 2
    finally:
        registry.shutdown()


def test_list_returns_one_status_per_retained_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry(max_running_jobs=2)
    try:
        ids = {registry.submit(model="solve satisfy;") for _ in range(3)}
        for job_id in ids:
            _wait_until_terminal(registry, job_id)
        listed = {status.job_id for status in registry.list()}
        assert listed == ids
    finally:
        registry.shutdown()


def test_running_job_reports_advancing_elapsed_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contract (README / SolveJobStatus docstring): while a job is `running` only
    # `state` and `elapsed_ms` advance. elapsed_ms is frozen at finalize, so a
    # live read must derive it from started_at_ms rather than the stored field.
    started = threading.Event()
    release = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1)
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)

        first = registry.get(job_id)
        assert first.state == "running"
        assert first.elapsed_ms is not None

        time.sleep(0.03)
        second = registry.get(job_id)
        assert second.state == "running"
        assert second.elapsed_ms is not None
        assert second.elapsed_ms > first.elapsed_ms  # advances between reads
    finally:
        release.set()
        registry.shutdown()


def test_shutdown_terminates_a_running_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        on_start(proc)
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # let the blocked worker unwind so shutdown can join it

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs._terminate_process_tree", _fake_terminate)

    registry = JobRegistry()
    registry.submit(model="solve satisfy;")
    assert started.wait(timeout=3)
    registry.shutdown()

    assert terminated == handles


def test_shutdown_finalizes_a_queued_job_as_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A queued (never-started) job must not be left in `queued` after shutdown:
    # its future is cancellable, so shutdown finalizes it as `cancelled`.
    started = threading.Event()
    release = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()  # unblock the running worker so shutdown can join the pool

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs._terminate_process_tree", _fake_terminate)

    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    registry.submit(model="solve satisfy;")  # occupies the only worker
    assert started.wait(timeout=3)
    queued_id = registry.submit(model="solve satisfy;")  # cannot start → queued
    assert registry.get(queued_id).state == "queued"

    registry.shutdown()

    status = registry.get(queued_id)
    assert status.state == "cancelled"
    assert status.result is None


def test_shutdown_terminates_a_child_launched_after_its_handle_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Launch-window race: a worker is `running` (its future can no longer be
    # cancelled) but has not yet recorded its process handle when shutdown takes
    # its handle snapshot. shutdown must still stop that child — it marks the
    # record cancel_requested, so the worker's own on_start terminates the process
    # at launch instead of letting shutdown block on the full solve timeout.
    worker_running = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []
    registry = JobRegistry(max_running_jobs=1)

    def _racing_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        worker_running.set()  # state == running; handle NOT yet recorded
        # Order the "launch" strictly after shutdown's handle snapshot: wait until
        # shutdown has marked this job cancel_requested. Bounded, so the pre-fix
        # bug surfaces as an assertion failure below rather than hanging here.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with registry._lock:
                record = next(iter(registry._records.values()), None)
                marked = record is not None and record.cancel_requested
            if marked:
                break
            time.sleep(0.001)
        on_start(proc)  # records handle; terminates iff cancel_requested is set
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)

    _patch_solve(monkeypatch, _racing_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs._terminate_process_tree", _fake_terminate)

    job_id = registry.submit(model="solve satisfy;")
    assert worker_running.wait(timeout=3)

    shutdown_done = threading.Event()

    def _run_shutdown() -> None:
        registry.shutdown()
        shutdown_done.set()

    threading.Thread(target=_run_shutdown, name="shutdown").start()

    assert shutdown_done.wait(timeout=5), "shutdown hung waiting on the launching child"
    assert set(terminated) == set(handles)  # the late-launched child was terminated
    assert registry.get(job_id).state == "cancelled"
