"""CP-SAT checker script executor.

Runs a caller-supplied checker Python script against a CP-SAT solution and
parses the checker protocol's output into a ``CpsatCheckerReport``.

Checker input protocol: the server writes a temporary JSON payload and passes
its path as the first positional argument to the checker. The payload schema:
    {
        "problem": str | null,
        "solution": dict,
        "objective": float | int | null,
        "solver_status": str  (CpsatStatus value)
    }

Checker output protocol: the checker must print, as its final stdout line,
one JSON object:
    {"status": "accepted" | "rejected" | "error", "errors": [...], "details": {...}}

Imports only: ``runner`` (shared executor), ``schemas`` (checker report type),
and ``childproc`` (tracker type). Never imports ``minizinc`` or ``runtime``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

from ..schemas import CpsatCheckerReport, CpsatPythonResult
from ..shared.childproc import ChildProcessTracker
from .runner import execute_child

_ACCEPTED_STATUS = "accepted"
_REJECTED_STATUS = "rejected"
_ERROR_STATUS = "error"
_VALID_CHECKER_STATUSES = frozenset({_ACCEPTED_STATUS, _REJECTED_STATUS, _ERROR_STATUS})


class _CheckerKw(TypedDict):
    stdout: str
    stderr: str
    duration_ms: int


def _error(
    msg: str,
    *,
    stdout: str,
    stderr: str,
    duration_ms: int,
    truncated: bool = False,
) -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="error",
        errors=[msg],
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=False,
        truncated=truncated,
    )


def _parse_final_line_json(text: str) -> dict | None:
    """Return the JSON object on the final non-empty stdout line, or ``None``.

    The checker protocol requires the verdict JSON to be the *final* stdout line.
    Unlike ``core.parse_last_json`` (which scans anywhere and tolerates trailing
    noise — acceptable for the display-only child objective), this rejects a
    verdict followed by any trailing content, so a malformed checker that prints
    an ``accepted`` object and then more output cannot pass the save gate.
    Trailing whitespace-only lines (e.g. ``print``'s newline) are skipped.
    """
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _normalize_checker_result(
    raw: object, *, stdout: str, stderr: str, duration_ms: int
) -> CpsatCheckerReport:
    """Parse checker JSON output; normalize any malformed form to status='error'."""
    kw: _CheckerKw = {"stdout": stdout, "stderr": stderr, "duration_ms": duration_ms}

    if not isinstance(raw, dict):
        return _error("checker did not emit a JSON object as its final stdout line", **kw)

    raw_status = raw.get("status")
    if not isinstance(raw_status, str) or raw_status not in _VALID_CHECKER_STATUSES:
        return _error(f"checker emitted unknown status: {raw_status!r}", **kw)

    raw_errors = raw.get("errors")
    if not isinstance(raw_errors, list):
        return _error("checker 'errors' field is not a list", **kw)
    if not all(isinstance(e, str) for e in raw_errors):
        return _error("checker 'errors' list contains a non-string entry", **kw)

    raw_details = raw.get("details")
    if raw_details is not None and not isinstance(raw_details, dict):
        return _error("checker 'details' field is not a dict", **kw)

    # A checker claiming "accepted" while carrying errors is self-contradictory.
    if raw_status == _ACCEPTED_STATUS and raw_errors:
        return _error(
            "checker returned accepted with a non-empty errors list (self-contradictory)", **kw
        )

    return CpsatCheckerReport(
        status=raw_status,  # type: ignore[arg-type]
        errors=list(raw_errors),
        details=raw_details,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=False,
        truncated=False,
    )


def run_checker(
    checker: str,
    run_result: CpsatPythonResult,
    *,
    problem: str | None,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
) -> CpsatCheckerReport:
    """Execute a checker script against a CP-SAT solution.

    Writes the checker source and the payload JSON to temporary files, then
    invokes the checker through ``execute_child``. Parses the checker's stdout
    for the final JSON object and normalizes any malformed output to
    ``status="error"``.
    """
    payload = {
        "problem": problem,
        "solution": run_result.solution or {},
        "objective": run_result.objective,
        "solver_status": run_result.status,
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        checker_script = tmp / "checker.py"
        checker_script.write_text(checker, encoding="utf-8")
        payload_file = tmp / "payload.json"
        payload_file.write_text(json.dumps(payload), encoding="utf-8")

        argv = [sys.executable, "-u", str(checker_script), str(payload_file)]

        child_result = execute_child(
            argv,
            cwd=tmp,
            timeout_ms=timeout_ms,
            tracker=tracker,
        )

    kw: _CheckerKw = {
        "stdout": child_result.stdout,
        "stderr": child_result.stderr,
        "duration_ms": child_result.duration_ms,
    }

    if child_result.timed_out:
        return CpsatCheckerReport(
            status="timeout",
            errors=["checker timed out"],
            timed_out=True,
            truncated=child_result.truncated,
            **kw,
        )
    if child_result.truncated:
        return _error("checker output was truncated", truncated=True, **kw)
    if child_result.return_code != 0:
        return _error(f"checker exited with non-zero code: {child_result.return_code}", **kw)

    parsed = _parse_final_line_json(child_result.stdout)
    return _normalize_checker_result(
        parsed,
        stdout=child_result.stdout,
        stderr=child_result.stderr,
        duration_ms=child_result.duration_ms,
    )
