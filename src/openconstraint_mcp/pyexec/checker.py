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

Two entry points share one execution tail: ``run_checker`` (inline source,
copied to a temp dir and run there) and ``run_checker_file`` (an on-disk
checker, run in place with ``cwd`` set to its own directory so a relative
sibling reference file resolves).

Imports only: ``shared.childrun`` (shared executor), ``schemas`` (checker
report type), ``childproc`` (tracker type), ``shared.job_errors``
(``exception_summary``, for the shared infrastructure-failure report), and the
dependency-light siblings ``diagnostics``, ``env_vars``, and ``script_path``.
Never imports ``core``, ``minizinc``, or ``runtime``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from subprocess import Popen
from typing import TypedDict

from ..schemas.cpsat import CpsatCheckerReport, CpsatPythonResult
from ..shared.childproc import ChildProcessTracker
from ..shared.childrun import execute_child
from ..shared.job_errors import exception_summary
from .diagnostics import checker_report_diagnostic
from .env_vars import CPSAT_CONFIG_ENV_VAR, CPSAT_SEED_ENV_VAR
from .script_path import validate_script_path

_ACCEPTED_STATUS = "accepted"
_REJECTED_STATUS = "rejected"
_ERROR_STATUS = "error"
_VALID_CHECKER_STATUSES = frozenset({_ACCEPTED_STATUS, _REJECTED_STATUS, _ERROR_STATUS})


class _CheckerKw(TypedDict):
    stdout: str
    stderr: str
    duration_ms: int


def _finalize(report: CpsatCheckerReport) -> CpsatCheckerReport:
    """Set the structured diagnostic on a built report — the single tail every
    checker-report factory funnels through so a failed verdict is never emitted
    without its ``checker_failed`` diagnostic."""
    report.diagnostic = checker_report_diagnostic(report)
    return report


def _error(
    msg: str,
    *,
    stdout: str,
    stderr: str,
    duration_ms: int,
    truncated: bool = False,
) -> CpsatCheckerReport:
    return _finalize(
        CpsatCheckerReport(
            status="error",
            errors=[msg],
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=False,
            truncated=truncated,
        )
    )


def checker_infrastructure_report(exc: Exception) -> CpsatCheckerReport:
    """Turn a checker-phase infrastructure failure into a diagnosed error report.

    A temp-file write or spawn failure AFTER the model run must not discard the
    completed model result, so it becomes a ``status="error"`` verdict rather
    than an exception. Shared by the synchronous checked runner
    (``core.run_cpsat_python_file_checked``) and the background-job registry so
    both surface the identical error string and nested diagnostic for the same
    fault.
    """
    return _error(
        f"checker infrastructure error: {exception_summary(exc)}",
        stdout="",
        stderr="",
        duration_ms=0,
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

    return _finalize(
        CpsatCheckerReport(
            status=raw_status,  # type: ignore[arg-type]
            errors=list(raw_errors),
            details=raw_details,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=False,
            truncated=False,
        )
    )


def _write_payload_file(
    directory: Path, run_result: CpsatPythonResult, problem: str | None
) -> Path:
    """Write the checker input payload into ``directory`` and return its path."""
    payload = {
        "problem": problem,
        "solution": run_result.solution or {},
        "objective": run_result.objective,
        "solver_status": run_result.status,
    }
    payload_file = directory / "payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    return payload_file


def _execute_checker(
    checker_script: Path,
    payload_file: Path,
    *,
    cwd: Path,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
    on_start: Callable[[Popen[str]], None] | None,
) -> CpsatCheckerReport:
    """Run the checker child and normalize its outcome into a report.

    The single execution + verdict-parsing tail shared by the inline
    (``run_checker``) and path-based (``run_checker_file``) entry points, so the
    two can only differ in where the checker script lives and which directory it
    runs from. Both ``checker_script`` and ``payload_file`` are absolute, so a
    caller-chosen ``cwd`` never changes which files the child is handed.
    """
    # The checker gets its inputs through the payload JSON file, not env vars; a
    # stale OPENCONSTRAINT_MCP_CPSAT_SEED/_CONFIG from the server's own launch
    # environment would leak into the checker and could change its behaviour, so
    # both protocol vars are deleted from the child's environment.
    child_result = execute_child(
        [sys.executable, "-u", str(checker_script), str(payload_file)],
        cwd=cwd,
        timeout_ms=timeout_ms,
        tracker=tracker,
        on_start=on_start,
        env={CPSAT_SEED_ENV_VAR: None, CPSAT_CONFIG_ENV_VAR: None},
    )

    kw: _CheckerKw = {
        "stdout": child_result.stdout,
        "stderr": child_result.stderr,
        "duration_ms": child_result.duration_ms,
    }

    if child_result.timed_out:
        return _finalize(
            CpsatCheckerReport(
                status="timeout",
                errors=["checker timed out"],
                timed_out=True,
                truncated=child_result.truncated,
                **kw,
            )
        )
    if child_result.truncated:
        return _error("checker output was truncated", truncated=True, **kw)
    if child_result.return_code != 0:
        return _error(f"checker exited with non-zero code: {child_result.return_code}", **kw)

    parsed = _parse_final_line_json(child_result.stdout)
    return _normalize_checker_result(parsed, **kw)


def run_checker(
    checker: str,
    run_result: CpsatPythonResult,
    *,
    problem: str | None,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
    on_start: Callable[[Popen[str]], None] | None = None,
) -> CpsatCheckerReport:
    """Execute an inline checker script against a CP-SAT solution.

    Writes the checker source and the payload JSON to temporary files, then
    invokes the checker through ``execute_child`` with ``cwd`` set to that temp
    directory — an inline snippet has no sibling files to find. Parses the
    checker's stdout for the final JSON object and normalizes any malformed
    output to ``status="error"``. ``on_start`` is forwarded to ``execute_child``
    so a caller (the background-job registry) can capture the checker child's
    ``Popen`` handle for targeted cancellation.

    For a checker that already exists on disk, use ``run_checker_file`` instead
    — it runs the checker in its own directory so a relative read of a sibling
    reference file resolves.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        checker_script = tmp / "checker.py"
        checker_script.write_text(checker, encoding="utf-8")
        payload_file = _write_payload_file(tmp, run_result, problem)
        return _execute_checker(
            checker_script,
            payload_file,
            cwd=tmp,
            timeout_ms=timeout_ms,
            tracker=tracker,
            on_start=on_start,
        )


def run_checker_file(
    checker_path: Path,
    run_result: CpsatPythonResult,
    *,
    problem: str | None,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
    on_start: Callable[[Popen[str]], None] | None = None,
) -> CpsatCheckerReport:
    """Execute an on-disk checker script against a CP-SAT solution, in place.

    The path-based counterpart to ``run_checker``: the checker is NOT copied to
    a temp directory, and runs with ``cwd`` set to its own parent directory, so
    a checker that reads a relative sibling reference file resolves (mirroring
    ``run_cpsat_python_file``). The payload JSON still lives in a temp file,
    passed as an absolute path in ``argv[1]``, so the checker directory is never
    written to.

    ``checker_path`` is validated (exists / regular file / non-empty / UTF-8)
    with a ``ValueError`` naming ``checker_path`` before any child is spawned.
    """
    resolved = validate_script_path(checker_path, parameter="checker_path")
    with tempfile.TemporaryDirectory() as tmp_dir:
        payload_file = _write_payload_file(Path(tmp_dir), run_result, problem)
        return _execute_checker(
            resolved,
            payload_file,
            cwd=resolved.parent,
            timeout_ms=timeout_ms,
            tracker=tracker,
            on_start=on_start,
        )
