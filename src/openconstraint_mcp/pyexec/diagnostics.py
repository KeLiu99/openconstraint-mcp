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


def output_contract_diagnostic(*, field: str, reason: str, return_code: int | None) -> Diagnostic:
    """Diagnose a child whose final JSON object violates the stdout envelope.

    Built from PRIMITIVES only — the offending ``field``, why it was rejected,
    and the child's exit code. It never inspects or mutates a result model, so
    the executor stays free to decide *which* failure a run had before choosing
    between this and ``cpsat_result_diagnostic``. ``details`` carries exactly
    ``field``/``reason``/``return_code``: the field name is the whole point (a
    client repairs its emit block from it), and there is no public result field
    transporting it.
    """
    return Diagnostic(
        category="child_process_error",
        message=(
            f"the CP-SAT child's final JSON object violates the stdout contract: `{field}` {reason}"
        ),
        details={"field": field, "reason": reason, "return_code": return_code},
    )


def _run_diagnostic(result: CpsatPythonResult) -> Diagnostic | None:
    """Return the result's OWN diagnostic, recomputing only when it carries none.

    The executor already diagnosed the run in ``_result_from_child``, and for a
    stdout-envelope violation that diagnostic is strictly richer than a
    recompute: it names the offending ``field``/``reason``, which
    ``cpsat_result_diagnostic`` structurally cannot see (the violation is a
    private executor return value, deliberately never a public result field).
    Recomputing would silently downgrade it to the generic child-error message
    on the experiment/save routes — exactly the routes that tell a client to
    repair the script.

    Preferring the carried value changes nothing else: every other producer sets
    exactly ``cpsat_result_diagnostic(result)`` (``_result_from_child`` without a
    violation, ``_spawn_failure_result``) or leaves the field unset
    (``experiment._script_invalidated_result``), which falls through to the
    recompute.
    """
    return result.diagnostic or cpsat_result_diagnostic(result)


def checker_report_diagnostic(report: CpsatCheckerReport) -> Diagnostic | None:
    """Diagnose a CP-SAT checker report (``accepted`` is clean).

    ``rejected``/``error``/``timeout`` — and a truncated report, which the
    checker normalizes to ``error`` — map to ``checker_failed`` with the verdict
    preserved in ``details``.
    """
    return checker_diagnostic(
        report.status, details={"truncated": report.truncated, "timed_out": report.timed_out}
    )


def _optional_checker_diagnostic(report: CpsatCheckerReport | None) -> Diagnostic | None:
    """``checker_report_diagnostic`` widened to accept "no checker ran"."""
    return checker_report_diagnostic(report) if report is not None else None


def checked_result_diagnostic(
    result: CpsatPythonResult | None, checker: CpsatCheckerReport | None
) -> Diagnostic | None:
    """Compose the top-level diagnostic for a run that also ran a checker.

    Precedence: a run TIMEOUT wins (the incumbent is unproven, so the checker's
    verdict on it is secondary), else a FAILED checker overrides the
    run-derived diagnostic, else the run-derived diagnostic stands. A clean run
    with an ``accepted`` checker yields ``None`` — the clean-success signal.

    Shared by the background-job registry (``CpsatJobRegistry._job_diagnostic``)
    and the synchronous ``run_cpsat_python_file_checked`` runner, so the two
    can never disagree about which failure a client sees first.
    """
    diagnostic = result.diagnostic if result is not None else None
    if diagnostic is not None and diagnostic.category in (
        "timeout_no_incumbent",
        "timeout_with_incumbent",
    ):
        return diagnostic
    checker_diag = _optional_checker_diagnostic(checker)
    if checker_diag is not None:
        return checker_diag
    return diagnostic


def save_failure_diagnostic(
    run_result: CpsatPythonResult, checker: CpsatCheckerReport | None
) -> Diagnostic:
    """Diagnose a ``save_verified_cpsat_python`` gate failure.

    Ordered most-specific-first: a failed checker gate is ``checker_failed``;
    otherwise the run result's own diagnostic (timeout, truncation, child error,
    envelope violation, infeasible) is surfaced — carried through by
    ``_run_diagnostic`` so a field-specific contract error keeps its ``field``;
    otherwise — a clean ``optimal``/``feasible`` result that a
    reported/expectation gate rejected (e.g. objective below threshold) — a
    generic ``not_verified``.
    """
    checker_diag = _optional_checker_diagnostic(checker)
    if checker_diag is not None:
        return checker_diag
    base = _run_diagnostic(run_result)
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
    more specific category (timeout, truncation, child error, envelope
    violation, infeasible) — e.g. an ``optimal`` result rejected by the
    optimization-mode gate for a missing/non-numeric objective — maps to
    ``not_verified`` with the attempt's own ``message``.

    The run-derived categories come from ``_run_diagnostic``, so an attempt that
    violated the stdout envelope keeps the ``field`` naming the broken key. The
    row carries no ``stdout``, and its ``stderr_tail`` is empty for a script
    that ran fine and merely printed the wrong shape, so this is the only place
    a client can learn what to repair.
    """
    if accepted:
        return _run_diagnostic(result)
    if checker_status is not None and checker_status_is_failure(checker_status):
        return checker_diagnostic(checker_status)
    base = _run_diagnostic(result)
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
