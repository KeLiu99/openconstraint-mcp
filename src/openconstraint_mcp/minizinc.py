from __future__ import annotations

import json
import subprocess
from typing import Any

from .runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from .schemas import SolverInfo, SolverList


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


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
