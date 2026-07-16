from __future__ import annotations

import pytest

from openconstraint_mcp.minizinc.core import (
    _build_check_result,
    _build_inspection_result,
    _build_solve_result,
    _build_unsat_core_result,
    _RunOutcome,
)
from openconstraint_mcp.minizinc.diagnostics import (
    check_diagnostic,
    classify_minizinc_stderr,
    inspection_diagnostic,
    solve_diagnostic,
    unsat_core_diagnostic,
)
from openconstraint_mcp.schemas.minizinc import (
    CheckerReport,
    CheckResult,
    ModelInspectionResult,
    SolveResult,
    UnsatCoreResult,
)

# --- stderr classifier ------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Error: cannot find solver 'wombat'", "solver_unavailable"),
        ("MiniZinc: type error: no function 'foo'", "type_error"),
        ("Error: symbol error: variable 'n' is undefined", "missing_data"),
        ("No value given for parameter 'capacity'", "missing_data"),
        ("Error: feature not supported by this solver", "unsupported_feature"),
        ("syntax error, unexpected ')'", "syntax_or_compile_error"),
        # Unrecognized but non-empty error text is a generic compile error.
        ("Error: something entirely novel went wrong", "syntax_or_compile_error"),
        # No text at all: no safe signal.
        ("", "unknown"),
        ("   \n  ", "unknown"),
    ],
)
def test_classify_minizinc_stderr(stderr: str, expected: str) -> None:
    category, message = classify_minizinc_stderr(stderr)
    assert category == expected
    assert message


# --- solve_diagnostic -------------------------------------------------------


def _solve(status: str, **kw: object) -> SolveResult:
    defaults: dict[str, object] = {
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "elapsed_ms": 5,
    }
    defaults.update(kw)
    return SolveResult(status=status, **defaults)  # type: ignore[arg-type]


def test_solve_clean_success_has_no_diagnostic() -> None:
    assert solve_diagnostic(_solve("satisfied", solution={"x": 1}, solutions=[{"x": 1}])) is None


def test_solve_success_status_with_nonzero_return_code_is_diagnostic() -> None:
    diag = solve_diagnostic(
        _solve("satisfied", solution={"x": 1}, solutions=[{"x": 1}], return_code=1, stderr="boom")
    )
    assert diag is not None
    assert diag.category == "syntax_or_compile_error"
    assert diag.details == {"solver": "cp-sat", "return_code": 1}


def test_solve_unsatisfiable_maps_to_infeasible() -> None:
    diag = solve_diagnostic(_solve("unsatisfiable"))
    assert diag is not None
    assert diag.category == "infeasible"


def test_solve_unbounded_maps_to_unbounded() -> None:
    assert solve_diagnostic(_solve("unbounded")).category == "unbounded"  # type: ignore[union-attr]


def test_solve_unsat_or_unbounded_maps_to_infeasible_or_unbounded() -> None:
    diag = solve_diagnostic(_solve("unsat_or_unbounded"))
    assert diag is not None
    assert diag.category == "infeasible_or_unbounded"


def test_solve_timeout_with_incumbent() -> None:
    diag = solve_diagnostic(_solve("timeout", timed_out=True, solutions=[{"x": 1}]))
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_checker_timeout_does_not_mask_solve_timeout_with_incumbent() -> None:
    result = _solve("timeout", timed_out=True, solution={"x": 1}, solutions=[{"x": 1}])
    result.checker = CheckerReport(status="timeout", checks=[], transcript="")
    diag = solve_diagnostic(result)
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_solve_timeout_without_incumbent() -> None:
    diag = solve_diagnostic(_solve("timeout", timed_out=True))
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"


def test_solve_error_runs_stderr_classifier() -> None:
    diag = solve_diagnostic(_solve("error", return_code=1, stderr="Error: type error: bad"))
    assert diag is not None
    assert diag.category == "type_error"


def test_solve_unknown_status_maps_to_unknown() -> None:
    assert solve_diagnostic(_solve("unknown")).category == "unknown"  # type: ignore[union-attr]


def test_solve_truncated_with_solutions_maps_to_output_truncated() -> None:
    # Truncation is surfaced even on a status that would otherwise be a clean None,
    # so a partial enumeration is never reported as a plain success.
    diag = solve_diagnostic(
        _solve(
            "satisfied",
            solution={"x": 1},
            solutions=[{"x": 1}],
            return_code=None,
            truncated=True,
        )
    )
    assert diag is not None
    assert diag.category == "output_truncated"
    assert diag.details == {"truncated": True, "solver": "cp-sat", "return_code": None}


def test_solve_truncated_without_solutions_maps_to_output_truncated() -> None:
    diag = solve_diagnostic(_solve("unknown", return_code=None, truncated=True))
    assert diag is not None
    assert diag.category == "output_truncated"


def test_solve_timeout_wins_over_truncation() -> None:
    # Both flags can be true (a burst overrun after a deadline kill); the timeout
    # category keeps precedence, mirroring the pyexec output-cap ordering.
    diag = solve_diagnostic(
        _solve("timeout", timed_out=True, solutions=[{"x": 1}], return_code=None, truncated=True)
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_checker_error_does_not_mask_output_truncated() -> None:
    # A truncated run commonly breaks the checker transcript too (its status
    # degrades to "error"); the output cap — not the checker echo — is the honest
    # signal, so truncation keeps precedence just below timeout.
    result = _solve(
        "satisfied",
        solution={"x": 1},
        solutions=[{"x": 1}],
        return_code=None,
        truncated=True,
    )
    result.checker = CheckerReport(status="error", checks=[], transcript="")
    diag = solve_diagnostic(result)
    assert diag is not None
    assert diag.category == "output_truncated"


def test_checker_violation_on_a_solved_model_elevates_to_checker_failed() -> None:
    result = _solve("satisfied", solution={"x": 1}, solutions=[{"x": 1}])
    result.checker = CheckerReport(status="violation", checks=[], transcript="")
    diag = solve_diagnostic(result)
    assert diag is not None
    assert diag.category == "checker_failed"
    assert diag.details == {"checker_status": "violation", "solve_status": "satisfied"}


def test_checker_no_solution_does_not_mask_infeasible() -> None:
    # An unsatisfiable solve produces no solutions, so the checker reports
    # no_solution — but the base infeasible category is more useful and wins.
    result = _solve("unsatisfiable")
    result.checker = CheckerReport(status="no_solution", checks=[], transcript="")
    diag = solve_diagnostic(result)
    assert diag is not None
    assert diag.category == "infeasible"


# --- check_diagnostic / unsat_core_diagnostic -------------------------------


def test_check_ok_is_clean() -> None:
    assert (
        check_diagnostic(
            CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=1)
        )
        is None
    )


def test_check_error_classifies_stderr() -> None:
    diag = check_diagnostic(
        CheckResult(
            status="error", solver="cp-sat", stdout="", stderr="type error here", elapsed_ms=1
        )
    )
    assert diag is not None
    assert diag.category == "type_error"


def test_unsat_core_mus_found_is_clean() -> None:
    result = UnsatCoreResult(
        status="mus_found", core=[], message="", stdout="", stderr="", elapsed_ms=1
    )
    assert unsat_core_diagnostic(result) is None


def test_unsat_core_no_core_is_unknown_not_infeasible() -> None:
    result = UnsatCoreResult(
        status="no_core", core=[], message="", stdout="", stderr="", elapsed_ms=1
    )
    diag = unsat_core_diagnostic(result)
    assert diag is not None
    assert diag.category == "unknown"


def test_unsat_core_timeout_is_timeout_no_incumbent() -> None:
    result = UnsatCoreResult(
        status="timeout", core=[], message="", stdout="", stderr="", elapsed_ms=1
    )
    diag = unsat_core_diagnostic(result)
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"


# --- output-cap truncation on the analysis paths ----------------------------


def test_check_truncated_maps_to_output_truncated() -> None:
    # A burst writer can overrun the 1 MiB cap and still exit 0, so the rc-driven
    # "ok" verdict stands — but the capped output must not read as a plain success.
    diag = check_diagnostic(
        CheckResult(
            status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=1, truncated=True
        )
    )
    assert diag is not None
    assert diag.category == "output_truncated"
    assert diag.details == {"truncated": True, "solver": "cp-sat"}


def test_check_timeout_wins_over_truncation() -> None:
    diag = check_diagnostic(
        CheckResult(
            status="timeout", solver="cp-sat", stdout="", stderr="", elapsed_ms=1, truncated=True
        )
    )
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"


def test_check_truncation_wins_over_stderr_classification() -> None:
    # A truncation tree-kill exits nonzero (status "error"); the cap — not the
    # stderr classifier's generic compile-error guess — is the honest diagnostic.
    diag = check_diagnostic(
        CheckResult(
            status="error",
            solver="cp-sat",
            stdout="",
            stderr="output exceeded the 1 MiB cap; process stopped\n",
            elapsed_ms=1,
            truncated=True,
        )
    )
    assert diag is not None
    assert diag.category == "output_truncated"


def test_inspection_truncated_maps_to_output_truncated() -> None:
    diag = inspection_diagnostic(
        ModelInspectionResult(
            status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=1, truncated=True
        )
    )
    assert diag is not None
    assert diag.category == "output_truncated"


def test_unsat_core_truncated_no_core_maps_to_output_truncated() -> None:
    # A truncated findMUS transcript may have lost the MUS lines beyond the cap,
    # so this no_core must not read as a completed "no MUS reported" verdict.
    result = UnsatCoreResult(
        status="no_core", core=[], message="", stdout="", stderr="", elapsed_ms=1, truncated=True
    )
    diag = unsat_core_diagnostic(result)
    assert diag is not None
    assert diag.category == "output_truncated"


def test_unsat_core_truncated_mus_found_is_flagged() -> None:
    # Even a MUS parsed from capped output may be missing members; the cap wins
    # over mus_found's clean None.
    result = UnsatCoreResult(
        status="mus_found", core=[], message="", stdout="", stderr="", elapsed_ms=1, truncated=True
    )
    diag = unsat_core_diagnostic(result)
    assert diag is not None
    assert diag.category == "output_truncated"


# --- builders wire the diagnostic onto the result ---------------------------


def test_build_check_result_sets_diagnostic() -> None:
    outcome = _RunOutcome(
        timed_out=False, returncode=1, stdout="", stderr="type error: bad", elapsed_ms=3
    )
    result = _build_check_result(outcome, solver="cp-sat")
    assert result.status == "error"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "type_error"


def test_build_solve_result_clean_leaves_diagnostic_none_before_checker() -> None:
    # A satisfy model with one solution, clean exit: _build_solve_result itself
    # does not set the diagnostic (the shared tail does), so it stays None here.
    outcome = _RunOutcome(
        timed_out=False,
        returncode=0,
        stdout='{"type": "solution", "output": {"json": {"x": 1}}}\n',
        stderr="",
        elapsed_ms=3,
    )
    result = _build_solve_result(outcome, solver="cp-sat")
    assert result.diagnostic is None


def test_build_inspection_result_error_sets_diagnostic() -> None:
    outcome = _RunOutcome(
        timed_out=False, returncode=1, stdout="", stderr="Error: type error", elapsed_ms=2
    )
    result = _build_inspection_result(outcome, solver="cp-sat")
    assert result.status == "error"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "type_error"


def test_build_unsat_core_result_timeout_sets_diagnostic() -> None:
    outcome = _RunOutcome(timed_out=True, returncode=-1, stdout="", stderr="", elapsed_ms=2)
    result = _build_unsat_core_result(outcome, "constraint false;")
    assert result.status == "timeout"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "timeout_no_incumbent"


def test_build_check_result_propagates_clean_exit_truncation() -> None:
    # Clean exit (rc 0) with the cap overrun: "ok" stands (the verdict is
    # rc-driven), but truncated rides onto the result and drives the diagnostic.
    outcome = _RunOutcome(
        timed_out=False, returncode=0, stdout="", stderr="", elapsed_ms=3, truncated=True
    )
    result = _build_check_result(outcome, solver="cp-sat")
    assert result.status == "ok"
    assert result.truncated is True
    assert result.diagnostic is not None
    assert result.diagnostic.category == "output_truncated"


def test_build_inspection_result_propagates_truncation() -> None:
    # rc 0 with a complete interface object (a stderr flood caused the cap): the
    # parsed interface is kept and the cap still surfaces as the diagnostic.
    interface_json = (
        '{"type": "interface", "input": {}, "output": {}, "method": "sat", '
        '"has_output_item": false, "included_files": [], "globals": []}'
    )
    outcome = _RunOutcome(
        timed_out=False,
        returncode=0,
        stdout=interface_json,
        stderr="",
        elapsed_ms=2,
        truncated=True,
    )
    result = _build_inspection_result(outcome, solver="cp-sat")
    assert result.status == "ok"
    assert result.interface is not None
    assert result.truncated is True
    assert result.diagnostic is not None
    assert result.diagnostic.category == "output_truncated"


def test_build_unsat_core_result_propagates_truncation() -> None:
    outcome = _RunOutcome(
        timed_out=False, returncode=0, stdout="", stderr="", elapsed_ms=2, truncated=True
    )
    result = _build_unsat_core_result(outcome, "constraint false;")
    assert result.status == "no_core"
    assert result.truncated is True
    assert result.diagnostic is not None
    assert result.diagnostic.category == "output_truncated"
