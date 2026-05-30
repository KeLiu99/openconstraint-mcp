from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from .runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from .schemas import (
    CheckResult,
    CheckStatus,
    SolveResult,
    SolverInfo,
    SolverList,
    SolveStatus,
)

DEFAULT_SOLVER: str = "cp-sat"
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000
# Named separately from the solve budget so the check call site doesn't read as
# a solve constant. A compile-check is far cheaper than a solve, but reusing the
# same value keeps the two tools' timeout semantics aligned.
DEFAULT_CHECK_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


def _parse_status(stdout: str, returncode: int, timed_out: bool) -> SolveStatus:
    if timed_out:
        return "timeout"
    # FlatZinc status markers always occupy their own line, so match whole
    # stripped lines rather than substrings — otherwise a model whose output
    # block prints a rule of dashes/equals or the literal marker text would be
    # misclassified.
    lines = {line.strip() for line in stdout.splitlines()}
    if "=====ERROR=====" in lines:
        return "error"
    if "=====UNSATISFIABLE=====" in lines:
        return "unsatisfiable"
    if "=====UNBOUNDED=====" in lines:
        return "unbounded"
    if "=====UNSATorUNBOUNDED=====" in lines:
        return "unsat_or_unbounded"
    if "=====UNKNOWN=====" in lines:
        return "unknown"
    if "==========" in lines:
        return "optimal"
    if "----------" in lines:
        return "satisfied"
    if returncode != 0:
        return "error"
    return "unknown"


def _coerce_to_text(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def list_solvers() -> SolverList:
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    try:
        completed = subprocess.run(
            [str(binary), "--solvers-json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = stderr or str(exc)
        raise MiniZincExecutionError(
            f"Managed MiniZinc binary at {binary} failed to list solvers: {detail}. "
            "The runtime may be corrupt — try reinstalling with "
            "`openconstraint-mcp install-runtime`."
        ) from exc
    raw: list[dict[str, Any]] = json.loads(completed.stdout)
    solvers = [
        SolverInfo(
            id=str(entry.get("id", "")),
            name=str(entry.get("name", entry.get("id", ""))),
            version=entry.get("version"),
            tags=list(entry.get("tags", [])),
        )
        for entry in raw
    ]
    return SolverList(solvers=solvers)


class _RunOutcome(NamedTuple):
    timed_out: bool
    returncode: int  # meaningful only when timed_out is False
    stdout: str
    stderr: str
    elapsed_ms: int


def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
) -> _RunOutcome:
    """Run the managed MiniZinc binary on ``model`` and report the raw outcome.

    Shared by ``solve_model`` (``extra_args=()``) and ``check_model``
    (``extra_args=("-c",)``). The model is written into a private temp dir and
    ``subprocess.run`` is pinned to that dir via ``cwd`` so cwd-relative
    ``include`` statements resolve onto emptiness rather than the server's
    working directory. The model file is always the last command argument.
    """
    if not model.strip():
        raise ValueError("model must not be empty")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    subprocess_timeout = (timeout_ms / 1000) + 5
    with tempfile.TemporaryDirectory(prefix="openconstraint-mcp-") as tmp:
        tmp_dir = Path(tmp)
        model_file = tmp_dir / "model.mzn"
        model_file.write_text(model, encoding="utf-8")
        cmd = [
            str(binary),
            "--solver",
            solver,
            "--time-limit",
            str(timeout_ms),
            *extra_args,
            str(model_file),
        ]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=subprocess_timeout,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
            return _RunOutcome(
                timed_out=True,
                returncode=0,
                stdout=_coerce_to_text(exc.stdout),
                stderr=_coerce_to_text(exc.stderr),
                elapsed_ms=elapsed_ms,
            )
        except OSError as exc:
            raise MiniZincExecutionError(
                f"Managed MiniZinc binary at {binary} failed to execute: {exc}. "
                "The runtime may be corrupt — try reinstalling with "
                "`openconstraint-mcp install-runtime`."
            ) from exc
        elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
        return _RunOutcome(
            timed_out=False,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_ms=elapsed_ms,
        )


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult:
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=()
    )
    if outcome.timed_out:
        return SolveResult(
            status="timeout",
            solver=solver,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    status = _parse_status(outcome.stdout, outcome.returncode, timed_out=False)
    return SolveResult(
        status=status,
        solver=solver,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def check_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult:
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=("-c",)
    )
    if outcome.timed_out:
        return CheckResult(
            status="timeout",
            solver=solver,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    # A pure `-c` compile emits no FlatZinc status markers, so the process
    # return code is the whole signal here — unlike solve_model, do not route
    # this through _parse_status.
    status: CheckStatus = "ok" if outcome.returncode == 0 else "error"
    return CheckResult(
        status=status,
        solver=solver,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )
