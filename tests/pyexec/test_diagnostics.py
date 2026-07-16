from __future__ import annotations

from openconstraint_mcp.pyexec.core import _result_from_child
from openconstraint_mcp.pyexec.diagnostics import (
    cpsat_result_diagnostic,
    experiment_attempt_diagnostic,
    experiment_diagnostic,
    save_failure_diagnostic,
)
from openconstraint_mcp.pyexec.jobs import (
    CpsatJobRegistry,
    _CpsatJobRecord,
    _CpsatJobRequest,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
    CpsatStatus,
)
from openconstraint_mcp.shared.childrun import ChildExecutionResult


def _result(
    status: CpsatStatus,
    *,
    solution: dict | None = None,
    timed_out: bool = False,
    truncated: bool = False,
    return_code: int | None = 0,
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution=solution,
        objective=None,
        stdout="",
        stderr="",
        return_code=return_code,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=10,
    )


# --- cpsat_result_diagnostic ------------------------------------------------


def test_clean_optimal_with_solution_is_none() -> None:
    assert cpsat_result_diagnostic(_result("optimal", solution={"x": 1})) is None


def test_optimal_with_empty_solution_is_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("optimal", solution={}))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_optimal_with_missing_solution_is_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("feasible", solution=None))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_infeasible_maps_to_infeasible() -> None:
    assert cpsat_result_diagnostic(_result("infeasible")).category == "infeasible"  # type: ignore[union-attr]


def test_unknown_maps_to_unknown() -> None:
    assert cpsat_result_diagnostic(_result("unknown")).category == "unknown"  # type: ignore[union-attr]


def test_timeout_with_incumbent() -> None:
    diag = cpsat_result_diagnostic(_result("timeout", solution={"x": 1}, timed_out=True))
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_timeout_without_incumbent() -> None:
    diag = cpsat_result_diagnostic(_result("timeout", timed_out=True))
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"


def test_truncation_maps_to_output_truncated() -> None:
    diag = cpsat_result_diagnostic(_result("error", truncated=True, return_code=0))
    assert diag is not None
    assert diag.category == "output_truncated"


def test_timeout_wins_over_truncation() -> None:
    diag = cpsat_result_diagnostic(
        _result("timeout", solution={"x": 1}, timed_out=True, truncated=True)
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"
    assert diag.details == {"truncated": True}


def test_error_maps_to_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("error", return_code=1))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_result_from_child_wires_diagnostic_onto_result() -> None:
    # A non-zero exit with no parseable JSON is a child error; the builder sets
    # the diagnostic as its single tail.
    child = ChildExecutionResult(
        stdout="boom",
        stderr="Traceback ...",
        return_code=1,
        timed_out=False,
        truncated=False,
        duration_ms=7,
    )
    result = _result_from_child(child)
    assert result.status == "error"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "child_process_error"


def test_result_from_child_clean_solution_has_no_diagnostic() -> None:
    child = ChildExecutionResult(
        stdout='{"status": "optimal", "solution": {"x": 1}, "objective": 1}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=7,
    )
    result = _result_from_child(child)
    assert result.status == "optimal"
    assert result.diagnostic is None


# --- checker report diagnostic (via run path result contract) ---------------


def _checker(
    status: str, *, truncated: bool = False, timed_out: bool = False
) -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=timed_out,
        truncated=truncated,
    )


# --- save_failure_diagnostic ------------------------------------------------


def test_save_failure_checker_rejection_is_checker_failed() -> None:
    diag = save_failure_diagnostic(_result("optimal", solution={"x": 1}), _checker("rejected"))
    assert diag.category == "checker_failed"


def test_save_failure_timeout_result_surfaces_timeout() -> None:
    diag = save_failure_diagnostic(_result("timeout", timed_out=True), None)
    assert diag.category == "timeout_no_incumbent"


def test_save_failure_clean_result_rejected_by_gate_is_not_verified() -> None:
    # A clean optimal result that failed a reported/expectation gate: no more
    # specific category, so a generic not_verified.
    diag = save_failure_diagnostic(_result("optimal", solution={"x": 1}), None)
    assert diag.category == "not_verified"


# --- experiment_attempt_diagnostic ------------------------------------------


def test_accepted_attempt_has_no_diagnostic() -> None:
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}), accepted=True, checker_status=None, message=None
    )
    assert diag is None


def test_accepted_timeout_attempt_surfaces_timeout_with_incumbent() -> None:
    diag = experiment_attempt_diagnostic(
        _result("timeout", solution={"x": 1}, timed_out=True),
        accepted=True,
        checker_status=None,
        message=None,
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_rejected_missing_objective_attempt_is_not_verified() -> None:
    # An optimal result rejected by the optimization-mode acceptance gate for a
    # missing objective: cpsat_result_diagnostic is clean, so not_verified with
    # the attempt message.
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}),
        accepted=False,
        checker_status=None,
        message="objective is missing or non-numeric",
    )
    assert diag is not None
    assert diag.category == "not_verified"
    assert diag.message == "objective is missing or non-numeric"


def test_rejected_by_checker_attempt_is_checker_failed() -> None:
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}),
        accepted=False,
        checker_status="rejected",
        message="checker rejected",
    )
    assert diag is not None
    assert diag.category == "checker_failed"


# --- experiment_diagnostic --------------------------------------------------


def _attempt_row(status: CpsatStatus) -> CpsatPythonExperimentAttemptResult:
    return CpsatPythonExperimentAttemptResult(
        index=0,
        name="a",
        source_sha256="0" * 64,
        timeout_ms=1000,
        status=status,
        objective=None,
        accepted=False,
        timed_out=False,
        truncated=False,
        duration_ms=1,
    )


def test_no_winner_experiment_maps_to_no_winner() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[_attempt_row("infeasible"), _attempt_row("unknown")],
        elapsed_ms=5,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
    )
    diag = experiment_diagnostic(result)
    assert diag is not None
    assert diag.category == "no_winner"
    assert diag.details == {"attempts": 2, "statuses": ["infeasible", "unknown"]}


# --- CpsatPythonJobStatus wrapper diagnostic --------------------------------


def _cpsat_record(
    state: str,
    *,
    result: CpsatPythonResult | None = None,
    checker: CpsatCheckerReport | None = None,
    checker_skipped_reason: str | None = None,
    message: str | None = None,
) -> _CpsatJobRecord:
    return _CpsatJobRecord(
        job_id="job-1",
        request=_CpsatJobRequest(source="print()", script_path=None, timeout_ms=1000),
        submitted_at_ms=0,
        state=state,  # type: ignore[arg-type]
        result=result,
        checker_report=checker,
        checker_skipped_reason=checker_skipped_reason,
        message=message,
    )


def test_cpsat_failed_job_maps_to_job_failed() -> None:
    diag = CpsatJobRegistry._job_diagnostic(_cpsat_record("failed", message="worker died"))
    assert diag is not None
    assert diag.category == "job_failed"


def test_cpsat_succeeded_job_derives_from_result() -> None:
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(_cpsat_record("succeeded", result=result))
    assert diag is None


def test_cpsat_job_checker_rejection_overrides_result_diagnostic() -> None:
    # Clean optimal result, but the job-level checker rejected -> checker_failed.
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("succeeded", result=result, checker=_checker("rejected"))
    )
    assert diag is not None
    assert diag.category == "checker_failed"


def test_cpsat_job_checker_rejection_does_not_mask_timeout_incumbent() -> None:
    result = _result("timeout", solution={"x": 1}, timed_out=True)
    result.diagnostic = cpsat_result_diagnostic(result)
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("timeout", result=result, checker=_checker("rejected"))
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_cpsat_job_checker_skipped_reason_keeps_result_diagnostic() -> None:
    # A skipped checker adds no diagnostic; the result-derived one (None here for
    # a clean optimal) stands.
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("succeeded", result=result, checker_skipped_reason="result not eligible")
    )
    assert diag is None


def test_winner_experiment_surfaces_winner_diagnostic() -> None:
    # A clean optimal winner carries no diagnostic, so neither does the experiment.
    winner = _result("optimal", solution={"x": 1})
    winner.diagnostic = None
    row = _attempt_row("optimal")
    row.name = "w"
    row.accepted = True
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="w",
        winner=winner,
        attempts=[row],
        elapsed_ms=5,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
    )
    assert experiment_diagnostic(result) is None
