"""Protocol-agnostic child-process executor.

Extracted from ``core.py`` so both ``core.py`` and ``checker.py`` can share
the same timeout / output-cap / process-tree-kill loop without coupling two
sibling leaves through the orchestrator.

This module knows nothing about the CP-SAT result protocol. ``execute_child``
returns a raw ``ChildExecutionResult`` (stdout/stderr/return_code plus the
timeout/truncation flags and wall-clock duration); each caller parses that raw
output into its own contract — ``core.py`` into ``CpsatPythonResult``,
``checker.py`` into ``CpsatCheckerReport``.

Imports only: ``proc`` (process-group launch + tree-kill) and ``childproc``
(``ChildProcessTracker`` type). Never imports ``schemas``, ``minizinc``, or
``runtime``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen

from ..shared.childproc import ChildProcessTracker
from ..shared.proc import popen_process_group, terminate_process_tree

MAX_OUTPUT_BYTES: int = 1 * 1024 * 1024  # 1 MiB

_POLL_INTERVAL_S: float = 0.05


@dataclass(frozen=True)
class ChildExecutionResult:
    """Raw outcome of one child-process run, with no protocol parsing applied.

    ``return_code`` is the child's actual exit status (so a killed child reports
    its signal-derived negative code, e.g. ``-15`` for SIGTERM on POSIX); callers
    that need a contract-level value map it themselves. ``timed_out`` and
    ``truncated`` record *why* a still-running child was killed; both are False on
    a clean exit. ``stdout``/``stderr`` are already capped at ``MAX_OUTPUT_BYTES``.
    """

    stdout: str
    stderr: str
    return_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int


def python_script_argv(script: Path) -> list[str]:
    # -u: unbuffered child stdout/stderr so prints reach the capture files as
    # they happen (not on a full buffer). This is what lets a flushed
    # intermediate result block survive a timeout kill (see core's partial
    # recovery on the timeout path).
    return [sys.executable, "-u", str(script)]


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


def execute_child(
    argv: list[str],
    cwd: Path,
    *,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
) -> ChildExecutionResult:
    """Run a prepared child command, capturing bounded output.

    Shared core for ``core.py`` entry points and the checker adapter. Handles the
    timeout / output-cap / tree-kill run loop and returns the raw process result;
    protocol parsing is the caller's job. Raises ``ValueError`` on a non-positive
    ``timeout_ms`` — the single gate, so a zero/negative cap is rejected before any
    child is spawned.

    ``tracker`` wiring (register then unregister on every exit path) is handled
    here, so callers never orphan a live child on an exception.

    ``env`` is an optional overlay merged ON TOP of the parent's ``os.environ``
    (the child still inherits the server's full environment, with these keys
    overriding/adding). The seeded save replay uses it to inject
    ``OPENCONSTRAINT_MCP_CPSAT_SEED``. A key mapped to ``None`` is explicitly
    deleted from the inherited environment instead of being left alone — this is
    what lets a caller force-clear a protocol var the *parent* process happens to
    have inherited from its own launch environment, rather than silently letting
    it pass through to the child. ``env=None`` (as opposed to a dict with ``None``
    values) leaves the inherited environment completely untouched (the default
    subprocess behaviour).
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    child_env: dict[str, str] | None = None
    if env is not None:
        merged_env = dict(os.environ)
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value
        child_env = merged_env
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
                argv,
                stdout=stdout_f,
                stderr=stderr_f,
                # No stdin: over stdio the server's stdin is the JSON-RPC channel, so
                # a script that reads input()/sys.stdin would steal protocol bytes or
                # block the server. DEVNULL gives the child an immediate EOF instead.
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
                env=child_env,
            )

            if tracker is not None:
                tracker.register(proc)
            try:
                # on_start fires inside the reaping guard: if the callback raises
                # (e.g. the job registry's cancel hook hits a transient OS error
                # while terminating the child), the finally below still kills and
                # reaps the process instead of orphaning a live child.
                if on_start is not None:
                    on_start(proc)
                deadline = start + timeout_s
                while proc.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        terminate_process_tree(proc)
                        timed_out = True
                        break
                    # Cap on the *combined* stdout+stderr size so neither stream
                    # alone, nor the two together, can balloon the parent's memory.
                    try:
                        combined = stdout_path.stat().st_size + stderr_path.stat().st_size
                    except OSError:
                        combined = 0
                    if combined > MAX_OUTPUT_BYTES:
                        terminate_process_tree(proc)
                        truncated = True
                        break
                    time.sleep(_POLL_INTERVAL_S)

                # Wait for the process to be fully reaped after kill or natural exit.
                proc.wait()
            finally:
                # Guarantee the child is dead and reaped on every exit path,
                # including an exception from on_start that skipped the loop.
                if proc.poll() is None:
                    terminate_process_tree(proc)
                    proc.wait()
                if tracker is not None:
                    tracker.unregister(proc)

        return_code = proc.returncode
        elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)

        raw_stdout, stdout_len = _read_capped(stdout_path)
        raw_stderr, stderr_len = _read_capped(stderr_path)

    # A burst writer can overrun the cap before the poll loop observes it — on a
    # clean exit, or on a timeout where the deadline (checked first) fires before
    # the size check. Recompute from the on-disk size so ``truncated`` is reliable;
    # ``_read_capped`` has already capped the returned text, so a False here would
    # mislabel partial output as complete.
    if stdout_len + stderr_len > MAX_OUTPUT_BYTES:
        truncated = True

    return ChildExecutionResult(
        stdout=raw_stdout,
        stderr=raw_stderr,
        return_code=return_code,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=elapsed_ms,
    )
