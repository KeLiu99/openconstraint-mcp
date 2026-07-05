"""Shared CP-SAT diagnostic-incumbent eligibility gate.

Single source of truth for whether a ``CpsatPythonResult`` carries an
incumbent worth checking diagnostically: status is usable (``timeout`` is a
recovered partial — reportable, not savable; the save path's stricter
``{optimal, feasible}`` gate still rejects it) and the solution is non-empty.

Used by both the explicit-experiment orchestrator (which layers its own
optimization-mode objective check on top) and the background-job registry
(which uses the rejection reason as ``checker_skipped_reason``), so the
accept/reject verdict and the displayed reason can never disagree.

Dependency-light leaf: imports only ``schemas``. Never imports ``minizinc``,
``runtime``, or pyexec siblings.
"""

from __future__ import annotations

from ..schemas import CpsatPythonResult

# Statuses whose result is a usable diagnostic incumbent.
DIAGNOSTIC_ACCEPT_STATUSES: frozenset[str] = frozenset({"optimal", "feasible", "timeout"})


def diagnostic_incumbent_eligibility(result: CpsatPythonResult) -> tuple[bool, str | None]:
    """Return ``(eligible, reject_reason)``; ``reject_reason`` is set iff not eligible."""
    if result.status not in DIAGNOSTIC_ACCEPT_STATUSES:
        return False, f"status={result.status!r}"
    if not result.solution:
        return False, "solution is missing or empty"
    return True, None
