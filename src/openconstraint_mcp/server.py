from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .mcp_descriptions import (
    CHECK_MINIZINC_FILES_DESC,
    CHECK_MINIZINC_MODEL_DESC,
    CHECK_RUNTIME_DESC,
    FIND_UNSAT_CORE_DESC,
    FIND_UNSAT_CORE_FILES_DESC,
    LIST_AVAILABLE_SOLVERS_DESC,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESC,
    SOLVE_MINIZINC_FILES_DESC,
    SOLVE_MINIZINC_MODEL_DESC,
)
from .minizinc import (
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    MiniZincExecutionError,
    check_model,
    check_model_path,
    find_unsat_core_path,
    list_solvers,
    solve_model,
    solve_model_path,
)
from .minizinc import find_unsat_core as _find_unsat_core
from .prompts import SOLVE_CONSTRAINT_PROBLEM_PROMPT
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas import (
    CheckResult,
    RuntimeStatus,
    SolveResult,
    SolverList,
    UnsatCoreResult,
)


def create_mcp_server() -> FastMCP:
    """Build a fresh FastMCP server and register all tools and prompts."""
    mcp: FastMCP[Any] = FastMCP("openconstraint-mcp")

    @mcp.tool(description=CHECK_RUNTIME_DESC)
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description=LIST_AVAILABLE_SOLVERS_DESC)
    def list_available_solvers() -> SolverList:
        try:
            return list_solvers()
        except (RuntimeMissingError, MiniZincExecutionError) as exc:
            # v0: surface the error message as a plain MCP error so the
            # client sees something actionable. Future versions should return a
            # structured error envelope (e.g. {"code": "runtime_missing",
            # "hint": "run install-runtime"}) so MCP clients can branch on it
            # programmatically rather than parsing the message string.
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=SOLVE_MINIZINC_MODEL_DESC)
    def solve_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    ) -> SolveResult:
        try:
            return solve_model(model, solver=solver, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=CHECK_MINIZINC_MODEL_DESC)
    def check_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    ) -> CheckResult:
        try:
            return check_model(model, solver=solver, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=FIND_UNSAT_CORE_DESC)
    def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        try:
            return _find_unsat_core(model, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=CHECK_MINIZINC_FILES_DESC)
    def check_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    ) -> CheckResult:
        try:
            return check_model_path(
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=SOLVE_MINIZINC_FILES_DESC)
    def solve_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    ) -> SolveResult:
        try:
            return solve_model_path(
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=FIND_UNSAT_CORE_FILES_DESC)
    def find_unsat_core_files(
        model_path: str,
        data_path: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        try:
            return find_unsat_core_path(
                Path(model_path),
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.prompt(
        name="solve_constraint_problem",
        description=SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESC,
    )
    def solve_constraint_problem(problem: str) -> str:
        return SOLVE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    return mcp


def run_stdio() -> None:
    """Create the MCP server and run it over stdio for CLI/client use."""
    create_mcp_server().run(transport="stdio")
