from __future__ import annotations

import json
import subprocess
from typing import Any

from .runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from .schemas import SolverInfo, SolverList


def list_solvers() -> SolverList:
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    completed = subprocess.run(
        [str(binary), "--solvers-json"],
        capture_output=True,
        text=True,
        check=True,
    )
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
