"""The model-visible result text leads with a `Diagnostic:` line when the result
carries a diagnostic, and adds nothing on a clean success."""

from __future__ import annotations

from openconstraint_mcp.protocol_text.results import (
    format_cpsat_experiment_content,
    format_solve_result_content,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatPythonExperimentResult,
)
from openconstraint_mcp.schemas.diagnostics import Diagnostic
from openconstraint_mcp.schemas.minizinc import SolveResult


def _solve(status: str, diagnostic: Diagnostic | None) -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="",
        stderr="",
        elapsed_ms=5,
        diagnostic=diagnostic,
    )


def test_solve_text_leads_with_diagnostic_when_present() -> None:
    text = format_solve_result_content(
        _solve("unsatisfiable", Diagnostic(category="infeasible", message="no solution exists"))
    )
    assert text.startswith("Diagnostic: infeasible — no solution exists\n\n")


def test_solve_text_clean_success_has_no_diagnostic_line() -> None:
    text = format_solve_result_content(_solve("satisfied", None))
    assert not text.startswith("Diagnostic:")
    assert text.startswith("Status: satisfied")


def test_experiment_text_leads_with_no_winner_diagnostic() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[],
        elapsed_ms=5,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
        diagnostic=Diagnostic(category="no_winner", message="no attempt was accepted"),
    )
    text = format_cpsat_experiment_content(result)
    assert text.startswith("Diagnostic: no_winner — no attempt was accepted\n\n")
