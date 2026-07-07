"""The ``diagnostic`` field is additive: every result/job schema defaults it to
None, so a clean construction (no diagnostic passed) dumps ``diagnostic: null``
and pre-diagnostic callers keep working unchanged."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    SaveVerifiedPythonResult,
)
from openconstraint_mcp.schemas.diagnostics import Diagnostic
from openconstraint_mcp.schemas.minizinc import (
    CheckResult,
    ModelInspectionResult,
    SaveVerifiedModelResult,
    SolveJobStatus,
    SolveResult,
    UnsatCoreResult,
)
from openconstraint_mcp.schemas.portfolio import (
    PortfolioAttempt,
    PortfolioJobStatus,
    PortfolioSolveResult,
)

# The 15 schemas that gained an optional `diagnostic` field (Result Coverage).
_DIAGNOSTIC_SCHEMAS: list[type[BaseModel]] = [
    CheckResult,
    ModelInspectionResult,
    SolveResult,
    UnsatCoreResult,
    SaveVerifiedModelResult,
    SolveJobStatus,
    PortfolioAttempt,
    PortfolioSolveResult,
    PortfolioJobStatus,
    CpsatPythonResult,
    CpsatCheckerReport,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonJobStatus,
    SaveVerifiedPythonResult,
]


@pytest.mark.parametrize("model_cls", _DIAGNOSTIC_SCHEMAS, ids=lambda c: c.__name__)
def test_diagnostic_field_is_optional_and_typed(model_cls: type[BaseModel]) -> None:
    field = model_cls.model_fields["diagnostic"]
    # Defaulted (additive): no existing construction site is forced to pass it.
    assert not field.is_required()
    assert field.default is None
    assert field.annotation == (Diagnostic | None)


def test_clean_solve_result_dumps_diagnostic_null() -> None:
    result = SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="",
        stderr="",
        elapsed_ms=5,
    )
    assert result.diagnostic is None
    assert result.model_dump()["diagnostic"] is None


def test_diagnostic_round_trips_when_present() -> None:
    result = SolveResult(
        status="unsatisfiable",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="",
        stderr="",
        elapsed_ms=5,
        diagnostic=Diagnostic(category="infeasible", message="no solution exists"),
    )
    reloaded = SolveResult.model_validate(result.model_dump())
    assert reloaded.diagnostic is not None
    assert reloaded.diagnostic.category == "infeasible"
