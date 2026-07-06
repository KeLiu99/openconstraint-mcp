"""Integration tests for pyexec/jobs.py — real child processes.

Mirrors tests/test_jobs_integration.py for the MiniZinc job registry.
Tagged @pytest.mark.integration so they run only under ``just integration``.

Per AGENTS.md: "solver-flag/status changes need a real-binary integration
test" — the cancel kill must be asserted against a real child, not just
mocked argv.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.jobs import CpsatJobRegistry


def _wait_until_terminal(registry: CpsatJobRegistry, job_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


_TRIVIAL_SOURCE = """
import json, sys
print(json.dumps({"status": "optimal", "objective": 42, "solution": {"x": 42}}))
"""

_SLEEP_SOURCE = """
import time, sys
sys.stdout.flush()
time.sleep(60)
print("done")
"""


@pytest.mark.integration
def test_submit_source_real_child_reaches_succeeded() -> None:
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_TRIVIAL_SOURCE)
        state = _wait_until_terminal(registry, job_id)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
        assert status.result.solution == {"x": 42}
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_cancel_running_real_child_terminates_and_reports_cancelled() -> None:
    """A real cancel kills the child process tree and finalizes as 'cancelled'."""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_SLEEP_SOURCE)
        # Wait for the child to start
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if registry.get(job_id).state == "running":
                break
            time.sleep(0.05)
        registry.cancel(job_id)
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_cancel_during_real_checker_child_reports_cancelled_without_result(
    tmp_path: Path,
) -> None:
    """Cancelling while the CHECKER child runs kills it and finalizes as
    'cancelled' with result=None — the completed solver result is discarded."""
    marker = tmp_path / "checker-started"
    checker = f"""
import time, pathlib
pathlib.Path({str(marker)!r}).write_text("started", encoding="utf-8")
time.sleep(60)
print('{{"status":"accepted","errors":[]}}')
"""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_TRIVIAL_SOURCE, checker=checker)
        # The solver child is trivial; the marker file appearing means the job
        # is now inside its checker phase (still state 'running').
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "checker child never started"
        assert registry.get(job_id).state == "running"
        registry.cancel(job_id)
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
        assert status.checker is None
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_submit_file_real_child_reaches_succeeded(tmp_path: Path) -> None:
    script = tmp_path / "sol.py"
    script.write_text(_TRIVIAL_SOURCE, encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script)
        state = _wait_until_terminal(registry, job_id)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
    finally:
        registry.shutdown()


_EXAMPLES = Path(__file__).parent.parent.parent / "examples" / "social_golfers" / "cpsat_best"


@pytest.mark.integration
def test_submit_file_with_real_checker_reaches_optimal_and_accepted() -> None:
    """End-to-end: a real example's solver + checker, run as a background job."""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(
            _EXAMPLES / "solution.py",
            # solution.py's own max_time_in_seconds defaults to 60s; keep our
            # subprocess cap and wait window comfortably above that so the
            # example's internal search cap can never trip our timeout first.
            timeout_ms=90_000,
            checker=(_EXAMPLES / "checker.py").read_text(encoding="utf-8"),
        )
        state = _wait_until_terminal(registry, job_id, timeout=120.0)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
        assert status.checker is not None
        assert status.checker.status == "accepted"
        assert status.checker.errors == []
    finally:
        registry.shutdown()
