"""Subprocess executor for OR-Tools CP-SAT Python scripts.

The server executes user/LLM-provided Python in a child process using the
server's own interpreter (``sys.executable``), which ships ``ortools``.

Security posture: timeout + output cap + process-tree kill is a **robustness**
boundary, not a security sandbox. No network blocking, AST filtering, or
syscall restriction is applied. This is a local-only tool; a cloud deployment
would require a real sandbox.

Output contract (executor ↔ script): the script must print, as its **last**
stdout block, one JSON object:
    {"status": "<CpsatStatus value>", "objective": <number|null>, "solution": {...}}

The executor parses the last JSON object it finds in stdout and maps the
``status`` field to ``CpsatStatus``; any unrecognized value becomes ``"error"``.

The child runs unbuffered (``python -u``), so a script MAY print intermediate
result blocks of the same shape during search (e.g. one per improved solution
from a ``CpSolverSolutionCallback``). On a clean exit the final block wins as
usual; on a timeout the executor recovers the last intermediate block's
``solution``/``objective`` (status stays ``"timeout"`` — a partial is unproven).

Canonical emit snippet (inlined in scripts, never imported from here):

    import json
    status_map = {
        "OPTIMAL": "optimal",
        "FEASIBLE": "feasible",
        "INFEASIBLE": "infeasible",
        "UNKNOWN": "unknown",
        "MODEL_INVALID": "error",
    }
    print(json.dumps({
        "status": status_map.get(solver.StatusName(status), "error"),
        "objective": solver.ObjectiveValue() if model.HasObjective() else None,
        "solution": {v.Name(): solver.Value(v) for v in variables},
    }))
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..childproc import ChildProcessTracker
from ..proc import popen_process_group, terminate_process_tree

DEFAULT_PYEXEC_TIMEOUT_MS: int = 30_000
MAX_OUTPUT_BYTES: int = 1 * 1024 * 1024  # 1 MiB

CpsatStatus = Literal["optimal", "feasible", "infeasible", "unknown", "error", "timeout"]
VERIFIED_STATUSES: frozenset[CpsatStatus] = frozenset({"optimal", "feasible"})

# Statuses a script may legitimately report. "timeout" is executor-determined, so a
# script claiming it is treated as a contract violation and normalized to "error".
_SCRIPT_STATUSES: frozenset[str] = frozenset(
    {"optimal", "feasible", "infeasible", "unknown", "error"}
)

_POLL_INTERVAL_S: float = 0.05


class CpsatPythonResult(BaseModel):
    status: CpsatStatus
    solution: dict | None
    objective: float | int | None
    stdout: str
    stderr: str
    return_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int


def _parse_last_json(text: str) -> dict | None:
    """Return the last top-level JSON object found in ``text``, or ``None``.

    Scans forward, decoding each top-level object with ``raw_decode`` so trailing
    output after the final JSON block (a stray log line, a late callback) does not
    defeat parsing, and so a nested object (e.g. ``solution``) inside the payload
    is never mistaken for the result. The last object that decodes wins.
    """
    decoder = json.JSONDecoder()
    found: dict | None = None
    index = text.find("{")
    while index >= 0:
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        index = text.find("{", end)
    return found


def _normalize_status(raw: object) -> CpsatStatus:
    if isinstance(raw, str) and raw in _SCRIPT_STATUSES:
        return raw  # type: ignore[return-value]
    return "error"


def _normalize_objective(raw: object) -> float | int | None:
    """Accept only a real number; bool (an int subclass) and other types become None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw


def _extract_solution_objective(parsed: dict) -> tuple[dict | None, float | int | None]:
    """Pull the solution dict and numeric objective out of a parsed result block.

    One site for the shape rules so the clean-exit and timeout (partial-recovery)
    paths can never drift: ``solution`` must be a dict, ``objective`` a real number.
    """
    solution = parsed.get("solution") if isinstance(parsed.get("solution"), dict) else None
    objective = _normalize_objective(parsed.get("objective"))
    return solution, objective


def _read_capped(path: Path) -> tuple[str, int]:
    """Return capped text plus the file's byte length (from ``stat``, pre-cap).

    Reads at most ``MAX_OUTPUT_BYTES`` from the file, so a child that overran the
    cap between poll checks cannot force the parent to materialize the whole file
    in memory: the cap is applied at the read boundary, not after a full slurp.
    The pre-cap length comes from ``stat`` because the truncation flag needs the
    true on-disk size, which the capped read no longer reflects.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            data = f.read(MAX_OUTPUT_BYTES)
    except OSError:
        return "", 0
    return data.decode("utf-8", errors="replace"), size


def _validate_script_path(script_path: Path) -> Path:
    """Resolve and validate a CP-SAT Python script path before any subprocess.

    Mirrors the MiniZinc path tools' contract (``_validate_model_data_paths``):
    resolve to an absolute path (following a symlink the caller named), then
    reject a missing or non-regular file, and an empty/whitespace-only or
    non-UTF-8 script, with a clear ``ValueError`` naming the offending path. The
    resolved path is returned so the caller uses the same path for argv and its
    parent for ``cwd`` — a relative input can't then double-count its subdir.
    """
    script_path = script_path.resolve()
    if not script_path.exists():
        raise ValueError(f"script_path does not exist: {script_path}")
    if not script_path.is_file():
        raise ValueError(f"script_path is not a file: {script_path}")
    try:
        text = script_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{script_path} is not valid UTF-8") from exc
    if not text.strip():
        raise ValueError(f"script file is empty: {script_path}")
    return script_path


def run_cpsat_python(
    source: str,
    *,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> CpsatPythonResult:
    """Execute OR-Tools CP-SAT Python ``source`` in a child process.

    Writes ``source`` to a temporary file, runs it with ``sys.executable``
    (the server's own venv, which ships ``ortools``), and captures stdout/stderr
    to bounded temp files (max ``MAX_OUTPUT_BYTES`` each). Returns a
    ``CpsatPythonResult`` with the parsed solution and execution metadata.

    Raises ``ValueError`` on a non-positive ``timeout_ms`` — matching the
    MiniZinc path's ``_validate_model_and_timeout`` so a zero/negative cap is
    rejected up front rather than spawning a child only to kill it immediately.

    When a ``tracker`` is supplied (the server's per-run child tracker), the live
    child is registered for the duration of the run so an abrupt server teardown
    can terminate it instead of orphaning it; it is unregistered on every exit
    path (clean, timeout-kill, or output-cap kill).

    For an existing local file, use ``run_cpsat_python_file`` instead — it runs
    the script in its own directory so relative file/import references resolve.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        script = tmp / "script.py"
        script.write_text(source, encoding="utf-8")
        # Run from the temp dir: an inline snippet has no sibling files to find.
        return _execute_cpsat(
            _python_script_argv(script),
            cwd=tmp,
            timeout_ms=timeout_ms,
            tracker=tracker,
        )


def _python_script_argv(script: Path) -> list[str]:
    return [sys.executable, "-u", str(script)]


def run_cpsat_python_file(
    script_path: Path,
    *,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> CpsatPythonResult:
    """Execute an existing OR-Tools CP-SAT Python file in its own directory.

    The path-based counterpart to ``run_cpsat_python``: instead of pasting the
    full source, the caller passes a local script path. The script runs with
    ``cwd`` set to its parent directory, so a relative ``open()`` of a sibling
    data file or ``import`` of a helper module resolves — the iteration win over
    copying the whole file inline. Mirrors the MiniZinc file tools
    (``solve_model_path``), which likewise run from the model's directory so a
    relative ``include`` resolves.

    Validates the path (exists / regular file / non-empty / UTF-8) with a clear
    ``ValueError`` before any child is spawned. Same execution contract, output
    cap, timeout, and tree-kill as ``run_cpsat_python``.
    """
    resolved = _validate_script_path(script_path)
    return _execute_cpsat(
        _python_script_argv(resolved),
        cwd=resolved.parent,
        timeout_ms=timeout_ms,
        tracker=tracker,
    )


def _execute_cpsat(
    argv: list[str],
    cwd: Path,
    *,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
) -> CpsatPythonResult:
    """Run a prepared CP-SAT child command, capturing bounded output.

    Shared core of both entry points: ``run_cpsat_python`` writes source to a
    temp script, ``run_cpsat_python_file`` uses an existing file; each builds
    ``argv`` (``[sys.executable, "-u", <script>]``) and a ``cwd`` and delegates
    here for the timeout / output-cap / tree-kill run loop, partial recovery, and
    result parsing. Centralizing it keeps the two paths from drifting.

    Raises ``ValueError`` on a non-positive ``timeout_ms`` — the single gate, so
    a zero/negative cap is rejected before any child is spawned. ``tracker``
    wiring (register for the run, unregister on every exit path) is handled here.
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    timeout_s = timeout_ms / 1000.0
    start = time.monotonic()
    timed_out = False
    truncated = False

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stdout_path = tmp / "stdout.txt"
        stderr_path = tmp / "stderr.txt"

        with (
            stdout_path.open("w", encoding="utf-8") as stdout_f,
            stderr_path.open("w", encoding="utf-8") as stderr_f,
        ):
            proc = popen_process_group(
                # -u: unbuffered child stdout/stderr so prints reach the capture
                # files as they happen (not on a full buffer) and a flushed
                # intermediate result survives the timeout kill — see the partial
                # recovery in the timeout branch below.
                argv,
                stdout=stdout_f,
                stderr=stderr_f,
                # No stdin: over stdio the server's stdin is the JSON-RPC channel, so
                # a script that reads input()/sys.stdin would steal protocol bytes or
                # block the server. DEVNULL gives the child an immediate EOF instead.
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
            )

            if tracker is not None:
                tracker.register(proc)
            try:
                deadline = start + timeout_s
                while proc.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        terminate_process_tree(proc)
                        timed_out = True
                        break
                    # Check combined output size to enforce cap
                    try:
                        combined = stdout_path.stat().st_size + stderr_path.stat().st_size
                    except OSError:
                        combined = 0
                    if combined > MAX_OUTPUT_BYTES:
                        terminate_process_tree(proc)
                        truncated = True
                        break
                    time.sleep(_POLL_INTERVAL_S)

                # Wait for process to be fully reaped after kill or natural exit
                proc.wait()
            finally:
                if tracker is not None:
                    tracker.unregister(proc)

        return_code = proc.returncode
        elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)

        raw_stdout, stdout_len = _read_capped(stdout_path)
        raw_stderr, stderr_len = _read_capped(stderr_path)

    # A burst writer can overrun the cap before the poll loop observes it — on a
    # clean exit, or on a timeout where the deadline (checked first) fires before
    # the size check. Recompute from the on-disk size so ``truncated`` is reliable
    # on both the clean-exit and timeout paths; ``_read_capped`` has already capped
    # the returned text, so a False here would mislabel partial output as complete.
    if stdout_len + stderr_len > MAX_OUTPUT_BYTES:
        truncated = True

    if timed_out:
        # Recover the best-so-far if the script emitted intermediate result blocks
        # (e.g. one per improved solution from a CpSolverSolutionCallback). The
        # last block wins; -u above is what lets it survive the kill. Status stays
        # the executor-owned "timeout" — a partial is unproven, never "optimal".
        partial = _parse_last_json(raw_stdout)
        solution, objective = (
            _extract_solution_objective(partial) if partial is not None else (None, None)
        )
        return CpsatPythonResult(
            status="timeout",
            solution=solution,
            objective=objective,
            stdout=raw_stdout,
            stderr=raw_stderr,
            # The child was killed; its exit code (SIGTERM -> -15 on POSIX) is not a
            # real return code. Report null to match the documented contract and the
            # MiniZinc-path tools, so clients don't misread a timeout as a child error.
            return_code=None,
            timed_out=True,
            truncated=truncated,
            duration_ms=elapsed_ms,
        )

    if truncated:
        return CpsatPythonResult(
            status="error",
            solution=None,
            objective=None,
            stdout=raw_stdout,
            stderr=raw_stderr,
            return_code=return_code,
            timed_out=False,
            truncated=True,
            duration_ms=elapsed_ms,
        )

    parsed = _parse_last_json(raw_stdout)
    if parsed is None or return_code != 0:
        return CpsatPythonResult(
            status="error",
            solution=None,
            objective=None,
            stdout=raw_stdout,
            stderr=raw_stderr,
            return_code=return_code,
            timed_out=False,
            truncated=False,
            duration_ms=elapsed_ms,
        )

    status = _normalize_status(parsed.get("status"))
    solution, objective = _extract_solution_objective(parsed)

    return CpsatPythonResult(
        status=status,
        solution=solution,
        objective=objective,
        stdout=raw_stdout,
        stderr=raw_stderr,
        return_code=return_code,
        timed_out=False,
        truncated=False,
        duration_ms=elapsed_ms,
    )
