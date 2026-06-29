"""Integration tests for pyexec/sweep.py — runs the real assignment example.

Proves the seed env protocol end-to-end: the sweep injects
OPENCONSTRAINT_MCP_CPSAT_SEED and the example script (which reads it) honors it.
The tiny model solves optimally and shares one optimum across seeds, so this does
NOT assert the objectives differ — only that both seeds run and produce a
structured winner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.sweep import run_cpsat_python_sweep

_EXAMPLES = Path(__file__).parent.parent.parent / "examples" / "cpsat_python"


@pytest.mark.integration
def test_sweep_runs_assignment_example_over_two_seeds() -> None:
    source = (_EXAMPLES / "assignment.py").read_text()

    result = run_cpsat_python_sweep(source, seeds=[1, 2], objective_sense="minimize")

    assert result.status == "winner"
    assert result.winner is not None
    assert result.winner.status == "optimal"
    # Both attempted seeds appear in the table.
    assert {a.seed for a in result.attempts} == {1, 2}
    assert all(a.accepted for a in result.attempts)
    # The winning seed is one of the two; its attempt row matches winner_seed.
    assert result.winner_seed in {1, 2}
    assert result.attempts[result.winner_index].seed == result.winner_seed  # type: ignore[index]


@pytest.mark.integration
def test_sweep_runs_clinic_roster_example_with_checker() -> None:
    source = (_EXAMPLES / "clinic_roster_sweep.py").read_text()
    checker = (_EXAMPLES / "clinic_roster_checker.py").read_text()

    result = run_cpsat_python_sweep(
        source,
        seeds=[3, 7, 11, 19, 23, 31, 47, 59],
        objective_sense="minimize",
        per_run_timeout_ms=1500,
        problem="Build a 7-day urgent-care clinic nurse roster.",
        checker=checker,
        checker_timeout_ms=500,
    )

    assert result.status == "winner"
    assert result.winner is not None
    assert result.winner.status == "optimal"
    assert result.winner.objective == 16
    assert all(attempt.accepted for attempt in result.attempts)
    assert all(attempt.checker_status == "accepted" for attempt in result.attempts)
