"""Integration tests for pyexec/core.py — runs real ortools scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import VERIFIED_STATUSES, run_cpsat_python

_EXAMPLES = Path(__file__).parent.parent.parent / "examples" / "cpsat_python"


@pytest.mark.integration
def test_run_cpsat_python_solves_assignment_example() -> None:
    source = (_EXAMPLES / "assignment.py").read_text()
    result = run_cpsat_python(source)

    assert result.status in VERIFIED_STATUSES
    assert result.solution is not None
    assert len(result.solution) > 0
    assert result.timed_out is False
    assert result.truncated is False


@pytest.mark.integration
def test_run_cpsat_python_solves_scheduling_example() -> None:
    source = (_EXAMPLES / "scheduling.py").read_text()
    result = run_cpsat_python(source)

    assert result.status in VERIFIED_STATUSES
    assert result.solution is not None
    assert "makespan" in result.solution
    assert result.timed_out is False
    assert result.truncated is False


@pytest.mark.integration
def test_run_cpsat_python_timeout_recovers_unflushed_partial() -> None:
    # The intermediate JSON is printed WITHOUT flush=True: it only survives the
    # timeout kill because the executor launches the child with -u (unbuffered).
    # Drop the -u and this returns solution=None — the test proves it is load-bearing.
    source = (
        "import json, time\n"
        "print(json.dumps({'status': 'feasible', 'objective': 5, 'solution': {'x': 2}}))\n"
        "time.sleep(30)\n"
    )
    result = run_cpsat_python(source, timeout_ms=300)

    assert result.timed_out is True
    assert result.status == "timeout"
    assert result.solution == {"x": 2}
    assert result.objective == 5
    # The child is killed (SIGTERM); its exit code (-15 on POSIX) must not leak —
    # the contract reports null on timeout. This asserts the override over a real kill.
    assert result.return_code is None
