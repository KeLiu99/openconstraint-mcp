"""CP-SAT-specific structured-diagnostic classification.

Maps built CP-SAT result models (run, checker, experiment, save) to a
:class:`Diagnostic`. Lives in the ``pyexec`` package (which may import
``schemas`` but never ``minizinc``/``runtime`` — see
``tests/pyexec/test_import_boundary.py``), so it may take result models
directly. The generic timeout/checker/job invariants are reused from
``schemas.diagnostics``; only the CP-SAT-shaped rules live here.
"""

from __future__ import annotations

from pydantic import JsonValue

from ..schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from ..schemas.diagnostics import (
    Diagnostic,
    checker_diagnostic,
    checker_status_is_failure,
    timeout_diagnostic,
)


def cpsat_result_diagnostic(result: CpsatPythonResult) -> Diagnostic | None:
    """Diagnose a CP-SAT child run.

    Precedence (most-specific-first): a timeout (with/without incumbent) wins
    over truncation, which wins over a plain child error. A clean
    ``optimal``/``feasible`` with a non-empty solution is None; the same status
    with a missing/empty solution is ``child_process_error`` (valid JSON that
    violates the solution contract save/job/experiment flows expect).
    ``infeasible`` maps to ``infeasible`` and ``unknown`` to ``unknown``.
    """
    if result.timed_out or result.status == "timeout":
        return timeout_diagnostic(
            has_incumbent=bool(result.solution),
            details={"truncated": result.truncated},
        )
    if result.truncated:
        return Diagnostic(
            category="output_truncated",
            message="the CP-SAT child's output exceeded the byte cap and was truncated",
            details={"truncated": True, "return_code": result.return_code},
        )
    if result.status == "error":
        return Diagnostic(
            category="child_process_error",
            message="the CP-SAT child failed or emitted malformed output",
            details={"return_code": result.return_code},
        )
    if result.status in ("optimal", "feasible"):
        if not result.solution:
            return Diagnostic(
                category="child_process_error",
                message=(
                    f"the child reported {result.status!r} but emitted no solution; "
                    "the result violates the solve contract"
                ),
                details={"status": result.status},
            )
        return None
    if result.status == "infeasible":
        return Diagnostic(category="infeasible", message="the model is infeasible")
    # status == "unknown": no solution proven and no more specific signal.
    return Diagnostic(
        category="unknown",
        message="the CP-SAT solver returned status=unknown (no solution proven)",
    )


def checker_report_diagnostic(report: CpsatCheckerReport) -> Diagnostic | None:
    """Diagnose a CP-SAT checker report (``accepted`` is clean).

    ``rejected``/``error``/``timeout`` — and a truncated report, which the
    checker normalizes to ``error`` — map to ``checker_failed`` with the verdict
    preserved in ``details``.
    """
    return checker_diagnostic(
        report.status, details={"truncated": report.truncated, "timed_out": report.timed_out}
    )


def save_failure_diagnostic(
    run_result: CpsatPythonResult, checker: CpsatCheckerReport | None
) -> Diagnostic:
    """Diagnose a ``save_verified_cpsat_python`` gate failure.

    Ordered most-specific-first: a failed checker gate is ``checker_failed``;
    otherwise the run result's own diagnostic (timeout, truncation, child error,
    infeasible) is surfaced; otherwise — a clean ``optimal``/``feasible`` result
    that a reported/expectation gate rejected (e.g. objective below threshold) —
    a generic ``not_verified``.
    """
    if checker is not None:
        checker_diag = checker_report_diagnostic(checker)
        if checker_diag is not None:
            return checker_diag
    base = cpsat_result_diagnostic(run_result)
    if base is not None:
        return base
    return Diagnostic(
        category="not_verified",
        message="the CP-SAT result did not pass the save verification gate; nothing was written",
    )


def experiment_attempt_diagnostic(
    result: CpsatPythonResult,
    *,
    accepted: bool,
    checker_status: str | None,
    message: str | None,
) -> Diagnostic | None:
    """Diagnose one experiment attempt row.

    Clean accepted attempts stay diagnostic-free; accepted timeout incumbents
    keep their timeout diagnostic. A rejected attempt whose result matches no
    more specific category (timeout, truncation, child error, infeasible) — e.g.
    an ``optimal`` result rejected by the optimization-mode gate for a
    missing/non-numeric objective — maps to ``not_verified`` with the attempt's
    own ``message``.
    """
    if accepted:
        return cpsat_result_diagnostic(result)
    if checker_status is not None and checker_status_is_failure(checker_status):
        return checker_diagnostic(checker_status)
    base = cpsat_result_diagnostic(result)
    if base is not None:
        return base
    return Diagnostic(
        category="not_verified",
        message=message or "the attempt was not accepted",
    )


def experiment_diagnostic(result: CpsatPythonExperimentResult) -> Diagnostic | None:
    """Diagnose an experiment: ``no_winner``, else the winner's own diagnostic.

    A winner's diagnostic is derived from its embedded ``CpsatPythonResult`` — a
    clean ``optimal``/``feasible`` winner carries None, while a ``timeout``
    winner surfaces ``timeout_with_incumbent`` — so the experiment never invents
    a status the winning run did not have.
    """
    if result.status == "no_winner":
        statuses: list[JsonValue] = [
            s for s in sorted({str(row.status) for row in result.attempts})
        ]
        return Diagnostic(
            category="no_winner",
            message="no attempt was accepted by the experiment's selection gate",
            details={"attempts": len(result.attempts), "statuses": statuses},
        )
    return result.winner.diagnostic if result.winner is not None else None
