from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from .minizinc.core import (
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_INSPECT_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    MiniZincExecutionError,
    check_model,
    check_model_path,
    find_unsat_core_path,
    inspect_model,
    inspect_model_path,
    list_solvers,
    solve_model,
    solve_model_path,
)
from .minizinc.core import find_unsat_core as _find_unsat_core
from .protocol_text.descriptions import (
    CHECK_MINIZINC_FILES_DESCRIPTION,
    CHECK_MINIZINC_MODEL_DESCRIPTION,
    CHECK_RUNTIME_DESCRIPTION,
    FIND_UNSAT_CORE_DESCRIPTION,
    FIND_UNSAT_CORE_FILES_DESCRIPTION,
    INSPECT_MINIZINC_FILES_DESCRIPTION,
    INSPECT_MINIZINC_MODEL_DESCRIPTION,
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
)
from .protocol_text.prompts import SOLVE_CONSTRAINT_PROBLEM_PROMPT
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas import (
    CheckResult,
    ModelInspectionResult,
    RuntimeStatus,
    SolveResult,
    SolverList,
    UnsatCoreResult,
)

_PACKAGE_NAME = "openconstraint-mcp"
_PREFERRED_STAT_KEYS = (
    "objective",
    "objectiveBound",
    "nSolutions",
    "failures",
    "propagations",
    "solveTime",
)
_STATS_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the entire Statistics section below into "
    "the user-facing answer. Do not omit it, summarize it, or replace it with "
    "only selected fields."
)
_SOLVER_INVENTORY_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the solver inventory table below into the "
    "user-facing answer. Do not omit rows, convert it to bullets or prose, "
    'summarize, group, or replace rows with phrases like "additional solvers".'
)
_SOLVER_NUM_SOLUTIONS_NOTE = (
    "`num_solutions` is supported only by `org.chuffed.chuffed` and "
    "`org.gecode.gecode`; the default `cp-sat` solver does not support it."
)
_SOLVER_RUNTIME_CONFIG_CAUTION = (
    "Caution: solver entries come from the MiniZinc runtime configuration. "
    "Commercial or external MIP solvers such as CPLEX, Gurobi, Xpress, SCIP, and "
    "COIN-BC may still require separate installed binaries, licenses, or "
    "solver-specific setup before they can successfully solve a model."
)
_SOLVER_CAPABILITY_METADATA_NOTE = (
    "To inspect detailed solver capabilities, ask for them explicitly. The "
    "structured result includes `capabilities.supports_all_solutions`, "
    "`supports_free_search`, `supports_parallel`, `supports_random_seed`, "
    "`supports_num_solutions`, and advisory `std_flags` for each solver."
)


def _homepage_url() -> str | None:
    """Return the project ``Homepage`` URL from package metadata, or ``None``.

    The build populates ``Project-URL`` entries from ``pyproject.toml``'s
    ``[project.urls]``, each formatted ``"<label>, <url>"`` — so the value
    carries a leading space after the comma that must be stripped. Single
    source of truth: switching to a dedicated site is a ``pyproject.toml`` edit
    only. Returns ``None`` if the package metadata is absent (cosmetic field;
    must not crash boot).
    """
    try:
        entries = metadata.metadata(_PACKAGE_NAME).get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for entry in entries:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage":
            return url.strip()
    return None


def _server_version() -> str:
    """Return the installed package version, or ``"unknown"`` if unavailable."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def _log_boot_diagnostic() -> None:
    """Print a one-time startup banner (version, runtime dir, install state).

    Writes to ``stderr`` only: over stdio, ``stdout`` is the JSON-RPC channel,
    so any stray write there corrupts the protocol. This only *reads* the
    already-resolved runtime status — it never downloads or installs anything.
    """
    status = get_runtime_status()
    lines = [
        f"{_PACKAGE_NAME} {_server_version()}",
        f"runtime dir: {status.runtime_dir}",
    ]
    if status.installed:
        lines.append(f"runtime: installed ({status.minizinc_binary})")
    else:
        lines.append(f"runtime: NOT installed → run `{_PACKAGE_NAME} install-runtime`")
    print("\n".join(lines), file=sys.stderr, flush=True)


def _format_solve_result_content(result: SolveResult) -> str:
    """Return model-visible solve output that leads with the solution, stats last."""
    lines = [
        f"Status: {result.status}",
        f"Solver: {result.solver}",
        f"Return code: {result.return_code}",
        f"Timed out: {str(result.timed_out).lower()}",
        f"Elapsed: {result.elapsed_ms} ms",
    ]

    if result.stdout:
        lines.extend(["", "Stdout:", result.stdout.rstrip()])
    if result.stderr:
        lines.extend(["", "Stderr:", result.stderr.rstrip()])

    if result.statistics:
        lines.extend(["", _STATS_PRESENTATION_REQUIREMENT, "Statistics:"])
        seen: set[str] = set()
        for key in _PREFERRED_STAT_KEYS:
            value = result.statistics.get(key)
            if value is not None:
                lines.append(f"- {key}: {value}")
                seen.add(key)
        for key, value in result.statistics.items():
            if key not in seen:
                lines.append(f"- {key}: {value}")

    return "\n".join(lines)


def _wrap_solve_result(result: SolveResult) -> CallToolResult:
    """Wrap a SolveResult as prose text content plus the full structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=_format_solve_result_content(result))],
        structuredContent=result.model_dump(mode="json"),
    )


def _format_solver_list_content(result: SolverList) -> str:
    """Return model-visible solver inventory: a complete id/name/version table.

    Leads with a presentation requirement so a client renders every row instead
    of summarizing or grouping, then appends advisory notes. The full
    ``capabilities`` object is deliberately kept out of this text — it lives in
    ``structuredContent`` and is surfaced only on request — so the default
    presentation stays compact and never dumps raw ``std_flags``.
    """
    return "\n".join(
        [
            _SOLVER_INVENTORY_PRESENTATION_REQUIREMENT,
            "",
            "| id | name | version |",
            "| --- | --- | --- |",
            *(
                f"| {solver.id} | {solver.name} | "
                f"{solver.version if solver.version is not None else '<unknown version>'} |"
                for solver in result.solvers
            ),
            "",
            result.capability_note,
            _SOLVER_CAPABILITY_METADATA_NOTE,
            "",
            _SOLVER_NUM_SOLUTIONS_NOTE,
            "",
            _SOLVER_RUNTIME_CONFIG_CAUTION,
        ]
    )


def _wrap_solver_list(result: SolverList) -> CallToolResult:
    """Wrap a SolverList as a complete-inventory text block plus structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=_format_solver_list_content(result))],
        structuredContent=result.model_dump(mode="json"),
    )


@asynccontextmanager
async def _lifespan(server: FastMCP[Any]) -> AsyncIterator[None]:
    """Server lifespan: emit the boot diagnostic on startup; no teardown."""
    _log_boot_diagnostic()
    yield


def create_mcp_server() -> FastMCP:
    """Build a fresh FastMCP server and register all tools and prompts."""
    mcp: FastMCP[Any] = FastMCP(
        "openconstraint-mcp",
        instructions=MCP_SERVER_INSTRUCTIONS,
        website_url=_homepage_url(),
        lifespan=_lifespan,
    )

    @mcp.tool(description=CHECK_RUNTIME_DESCRIPTION)
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description=LIST_AVAILABLE_SOLVERS_DESCRIPTION)
    def list_available_solvers() -> Annotated[CallToolResult, SolverList]:
        try:
            return _wrap_solver_list(list_solvers())
        except (RuntimeMissingError, MiniZincExecutionError) as exc:
            # v0: surface the error message as a plain MCP error so the
            # client sees something actionable. Future versions should return a
            # structured error envelope (e.g. {"code": "runtime_missing",
            # "hint": "run install-runtime"}) so MCP clients can branch on it
            # programmatically rather than parsing the message string.
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=SOLVE_MINIZINC_MODEL_DESCRIPTION)
    def solve_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        try:
            result = solve_model(
                model,
                solver=solver,
                data=data,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
            )
            return _wrap_solve_result(result)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=CHECK_MINIZINC_MODEL_DESCRIPTION)
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

    @mcp.tool(description=INSPECT_MINIZINC_MODEL_DESCRIPTION)
    def inspect_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    ) -> ModelInspectionResult:
        try:
            return inspect_model(model, solver=solver, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=FIND_UNSAT_CORE_DESCRIPTION)
    def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        try:
            return _find_unsat_core(model, data=data, timeout_ms=timeout_ms)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=CHECK_MINIZINC_FILES_DESCRIPTION)
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

    @mcp.tool(description=INSPECT_MINIZINC_FILES_DESCRIPTION)
    def inspect_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    ) -> ModelInspectionResult:
        try:
            return inspect_model_path(
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=SOLVE_MINIZINC_FILES_DESCRIPTION)
    def solve_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        try:
            result = solve_model_path(
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
            )

            return _wrap_solve_result(result)
        except (RuntimeMissingError, MiniZincExecutionError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @mcp.tool(description=FIND_UNSAT_CORE_FILES_DESCRIPTION)
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
        description=SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    )
    def solve_constraint_problem(problem: str) -> str:
        return SOLVE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    return mcp


def run_stdio() -> None:
    """Create the MCP server and run it over stdio for CLI/client use."""
    create_mcp_server().run(transport="stdio")
