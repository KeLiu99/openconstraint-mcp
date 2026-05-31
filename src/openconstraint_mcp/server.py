from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .minizinc import (
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    MiniZincExecutionError,
    check_model,
    list_solvers,
    solve_model,
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


def create_server() -> FastMCP:
    mcp: FastMCP[Any] = FastMCP("openconstraint-mcp")

    @mcp.tool(description="Report whether the managed MiniZinc runtime is installed.")
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description="List solvers available in the managed MiniZinc runtime.")
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

    @mcp.tool(
        description=(
            "Run a complete MiniZinc model through the managed local MiniZinc "
            "runtime. The `model` argument must be complete MiniZinc source — "
            "declarations, constraints, exactly one `solve` statement, and an "
            "`output` block. The optional `data` argument supplies MiniZinc "
            "data (`.dzn` contents) as text, supplied to the runtime as a data "
            "file alongside the model; omit it for models that need no external "
            "data. Returns a SolveResult "
            "with the run's status plus the runtime's raw stdout and stderr so "
            "the caller can revise and retry on MiniZinc errors."
        )
    )
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

    @mcp.tool(
        description=(
            "Compile-check a complete MiniZinc model through the managed local "
            "MiniZinc runtime without solving it. Flattens (compiles) the "
            "`model` for the chosen solver — catching syntax, type, "
            "missing-include, invalid-domain, and unsupported-construct errors "
            "— and returns a CheckResult with the check's status plus the "
            "runtime's raw stdout and stderr, so the caller can repair the "
            "model before calling `solve_minizinc_model`. The optional `data` "
            "argument supplies MiniZinc data (`.dzn` contents) as text, supplied "
            "as a data file alongside the model; a parameterized model needs it "
            "to flatten, so pass the same `data` you will pass to "
            "`solve_minizinc_model`. Omit it "
            "for models that need no external data. A status of `ok` means the "
            "model compiles, not that it is satisfiable."
        )
    )
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

    @mcp.tool(
        description=(
            "Diagnose why a MiniZinc model is unsatisfiable by computing a "
            "minimal unsatisfiable subset (MUS) of its constraints via the "
            "managed runtime's findMUS tool (org.minizinc.findmus). Use it when "
            "solve_minizinc_model returns status 'unsatisfiable' to localize the "
            "conflict. The optional `data` argument supplies MiniZinc data "
            "(`.dzn` contents) as text, supplied as a data file alongside the "
            "model; pass the SAME `data` you passed to the solve that proved "
            "unsat, or a parameterized model cannot flatten. Omit it for models "
            "that need no external data. "
            "Returns an UnsatCoreResult whose status is 'mus_found', "
            "'no_core' (findMUS finished without reporting a MUS), 'error' (see "
            "stderr), or 'timeout'. `core` is a best-effort structured list of the "
            "conflicting constraints (source span + text) resolved from the "
            "MODEL FILE only; `stdout` preserves findMUS's raw output verbatim and "
            "is authoritative (a decision variable assigned in `data` acts as a "
            "constraint, so a MUS member can originate in the data file and appear "
            "in stdout but not in `core`). The reported subset is MINIMAL — no "
            "constraint can be dropped while staying unsatisfiable — but NOT "
            "necessarily the globally smallest, and a model may have several."
        )
    )
    def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        try:
            return _find_unsat_core(model, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.prompt(
        name="solve_constraint_problem",
        description=(
            "Guide the MCP client's LLM through translating a natural-language "
            "constraint or optimization problem into MiniZinc and running it "
            "through the local managed runtime (via solve_minizinc_model when "
            "available, otherwise by walking the user through the "
            "openconstraint-mcp CLI to set up and invoke the managed runtime "
            "manually — never via a bare PATH-based minizinc)."
        ),
    )
    def solve_constraint_problem(problem: str) -> str:
        return SOLVE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    return mcp


def run_stdio() -> None:
    create_server().run(transport="stdio")
