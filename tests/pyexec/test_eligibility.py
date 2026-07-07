"""Unit tests for pyexec/eligibility.py — the shared diagnostic-incumbent gate."""

from __future__ import annotations

import pytest

from openconstraint_mcp.pyexec.eligibility import diagnostic_incumbent_eligibility
from openconstraint_mcp.schemas.cpsat import CpsatPythonResult, CpsatStatus


def _result(status: CpsatStatus, solution: dict | None) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution=solution,
        objective=None,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=status == "timeout",
        truncated=False,
        duration_ms=10,
    )


@pytest.mark.parametrize("status", ["optimal", "feasible", "timeout"])
def test_usable_status_with_solution_is_eligible(status: CpsatStatus) -> None:
    eligible, reason = diagnostic_incumbent_eligibility(_result(status, {"x": 1}))
    assert eligible is True
    assert reason is None


@pytest.mark.parametrize("status", ["infeasible", "unknown", "error"])
def test_unusable_status_is_rejected_with_status_reason(status: CpsatStatus) -> None:
    eligible, reason = diagnostic_incumbent_eligibility(_result(status, {"x": 1}))
    assert eligible is False
    assert reason == f"status={status!r}"


@pytest.mark.parametrize("solution", [None, {}])
def test_missing_or_empty_solution_is_rejected(solution: dict | None) -> None:
    eligible, reason = diagnostic_incumbent_eligibility(_result("optimal", solution))
    assert eligible is False
    assert reason == "solution is missing or empty"
