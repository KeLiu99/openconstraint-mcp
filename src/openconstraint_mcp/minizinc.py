from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from .schemas import SolveResult, SolverInfo, SolverList, SolveStatus

DEFAULT_SOLVER: str = "cp-sat"
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


def _parse_status(stdout: str, returncode: int, timed_out: bool) -> SolveStatus:
    if timed_out:
        return "timeout"
    if "=====ERROR=====" in stdout:
        return "error"
    if "=====UNSATISFIABLE=====" in stdout:
        return "unsatisfiable"
    if "=====UNBOUNDED=====" in stdout:
        return "unbounded"
    if "=====UNSATorUNBOUNDED=====" in stdout:
        return "unsat_or_unbounded"
    if "=====UNKNOWN=====" in stdout:
        return "unknown"
    if "==========" in stdout:
        return "optimal"
    if "----------" in stdout:
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


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult:
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
        model_file.write_text(model)
        cmd = [
            str(binary),
            "--solver",
            solver,
            "--time-limit",
            str(timeout_ms),
            str(model_file),
        ]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=subprocess_timeout,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
            return SolveResult(
                status="timeout",
                solver=solver,
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
        status = _parse_status(completed.stdout, completed.returncode, timed_out=False)
        return SolveResult(
            status=status,
            solver=solver,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_ms=elapsed_ms,
        )
