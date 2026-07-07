"""MiniZinc-specific structured-diagnostic classification.

Maps a built MiniZinc result model to a :class:`Diagnostic`, including the one
piece the generic ``schemas.diagnostics`` leaf deliberately does not own: a
conservative string-pattern classifier over MiniZinc's compiler/solver stderr.
The classifier is intentionally coarse (patterns backed by tests, ``unknown``
fallback) so it never guesses a subtype it cannot defend.

Lives in the ``minizinc`` package — left of ``schemas`` in the import graph —
so it may take result models (``SolveResult`` etc.) directly, unlike the
schemas leaf whose helpers must stay on primitives to avoid an import cycle.
"""

from __future__ import annotations

from ..schemas.diagnostics import (
    Diagnostic,
    DiagnosticCategory,
    checker_diagnostic,
    timeout_diagnostic,
)
from ..schemas.minizinc import (
    CheckResult,
    ModelInspectionResult,
    SolveResult,
    UnsatCoreResult,
)

# Conservative, ordered stderr patterns. First match wins; the checks are
# lowercase substring tests so casing in MiniZinc's output does not matter. The
# order is most-specific-first: a "type error" line that also mentions an
# undefined identifier is a type error, not missing data.
_STDERR_PATTERNS: tuple[tuple[DiagnosticCategory, tuple[str, ...]], ...] = (
    (
        "solver_unavailable",
        ("cannot find solver", "solver not found", "unknown solver", "no solver"),
    ),
    (
        "type_error",
        ("type error", "type-inst", "type inst", "type mismatch", "not a subtype"),
    ),
    (
        "missing_data",
        (
            "no value given",
            "undefined identifier",
            "is undefined",
            "symbol error",
            "not defined",
        ),
    ),
    (
        "unsupported_feature",
        ("not supported", "unsupported", "cannot be used", "is not available"),
    ),
    (
        "syntax_or_compile_error",
        ("syntax error", "parse error", "syntax:"),
    ),
)


def classify_minizinc_stderr(stderr: str) -> tuple[DiagnosticCategory, str]:
    """Classify a nonzero-rc / ``error`` MiniZinc run from its stderr text.

    Returns ``(category, message)``. Conservative: a recognized pattern wins,
    otherwise any non-empty error text is a generic ``syntax_or_compile_error``
    (a compile-time failure with an unrecognized shape), and truly empty text
    is ``unknown`` (no safe signal). The raw stderr always remains on the result
    for the client to read.
    """
    haystack = stderr.lower()
    for category, needles in _STDERR_PATTERNS:
        if any(needle in haystack for needle in needles):
            return category, f"MiniZinc reported a {category.replace('_', ' ')}"
    if stderr.strip():
        return "syntax_or_compile_error", "MiniZinc reported a compile error"
    return "unknown", "MiniZinc failed without a recognizable diagnostic; see stderr"


def _error_diagnostic(stderr: str, *, solver: str, return_code: int | None) -> Diagnostic:
    category, message = classify_minizinc_stderr(stderr)
    return Diagnostic(
        category=category,
        message=message,
        details={"solver": solver, "return_code": return_code},
    )


def _base_solve_diagnostic(result: SolveResult) -> Diagnostic | None:
    """Diagnose a solve from status/timeout/return code alone (no checker)."""
    if result.timed_out or result.status == "timeout":
        return timeout_diagnostic(
            has_incumbent=bool(result.solutions), details={"solver": result.solver}
        )
    if result.status == "error" or (
        isinstance(result.return_code, int) and result.return_code != 0
    ):
        return _error_diagnostic(
            result.stderr, solver=result.solver, return_code=result.return_code
        )
    if result.status in ("satisfied", "optimal"):
        return None
    if result.status == "unsatisfiable":
        return Diagnostic(
            category="infeasible",
            message="the model is unsatisfiable",
            details={"solver": result.solver},
        )
    if result.status == "unbounded":
        return Diagnostic(
            category="unbounded",
            message="the model is unbounded",
            details={"solver": result.solver},
        )
    if result.status == "unsat_or_unbounded":
        return Diagnostic(
            category="infeasible_or_unbounded",
            message="the model is unsatisfiable or unbounded (the solver could not tell which)",
            details={"solver": result.solver},
        )
    # status == "unknown", no subprocess timeout: no solution and no proof.
    return Diagnostic(
        category="unknown",
        message="solver returned no solution and no completeness proof (status=unknown)",
        details={"solver": result.solver},
    )


def solve_diagnostic(result: SolveResult) -> Diagnostic | None:
    """Diagnose a built ``SolveResult``, folding in an attached checker verdict.

    A supplied checker that did not pass elevates the diagnostic to
    ``checker_failed`` — but not over a solve timeout, and only when the solve
    actually produced a solution to check. With no solutions a checker's
    ``no_solution`` verdict just echoes the base ``infeasible`` /
    ``timeout_no_incumbent``, which is the more useful category and is kept.
    """
    base = _base_solve_diagnostic(result)
    if base is not None and base.category in ("timeout_no_incumbent", "timeout_with_incumbent"):
        return base
    if result.checker is not None and result.solutions:
        checker_diag = checker_diagnostic(
            result.checker.status, details={"solve_status": result.status}
        )
        if checker_diag is not None:
            return checker_diag
    return base


def check_diagnostic(result: CheckResult) -> Diagnostic | None:
    """Diagnose a compile-check result (``ok`` is clean)."""
    if result.status == "timeout":
        return timeout_diagnostic(has_incumbent=False, details={"solver": result.solver})
    if result.status == "error":
        return _error_diagnostic(result.stderr, solver=result.solver, return_code=None)
    return None


def inspection_diagnostic(result: ModelInspectionResult) -> Diagnostic | None:
    """Diagnose a model-interface inspection (``ok`` is clean)."""
    if result.status == "timeout":
        return timeout_diagnostic(has_incumbent=False, details={"solver": result.solver})
    if result.status == "error":
        return _error_diagnostic(result.stderr, solver=result.solver, return_code=None)
    return None


def unsat_core_diagnostic(result: UnsatCoreResult) -> Diagnostic | None:
    """Diagnose a findMUS run.

    ``mus_found`` is clean (a MUS was reported). ``timeout`` is
    ``timeout_no_incumbent`` (findMUS has no incumbent notion). ``no_core`` is
    ``unknown`` — NOT ``infeasible`` — because no MUS was reported (a tight time
    limit can also surface here). ``error`` runs through the stderr classifier.
    """
    if result.status == "mus_found":
        return None
    if result.status == "timeout":
        return timeout_diagnostic(has_incumbent=False)
    if result.status == "error":
        return _error_diagnostic(result.stderr, solver="org.minizinc.findmus", return_code=None)
    return Diagnostic(
        category="unknown",
        message="findMUS reported no minimal unsatisfiable subset",
    )
