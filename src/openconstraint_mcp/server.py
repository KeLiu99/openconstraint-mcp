from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from .mcp_descriptions import (
    CHECK_MINIZINC_FILES_DESC,
    CHECK_MINIZINC_MODEL_DESC,
    CHECK_RUNTIME_DESC,
    FIND_UNSAT_CORE_DESC,
    FIND_UNSAT_CORE_FILES_DESC,
    LIST_AVAILABLE_SOLVERS_DESC,
    MCP_SERVER_INSTRUCTIONS,
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


def _solve_call_result(result: SolveResult) -> CallToolResult:
    """Wrap a SolveResult as prose text content plus the full structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=_format_solve_result_content(result))],
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
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
    ) -> Annotated[CallToolResult, SolveResult]:
        try:
            return _solve_call_result(
                solve_model(
                    model,
                    solver=solver,
                    data=data,
                    timeout_ms=timeout_ms,
                    free_search=free_search,
                    parallel=parallel,
                    random_seed=random_seed,
                    all_solutions=all_solutions,
                )
            )
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
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
    ) -> Annotated[CallToolResult, SolveResult]:
        try:
            return _solve_call_result(
                solve_model_path(
                    Path(model_path),
                    solver=solver,
                    data_path=Path(data_path) if data_path is not None else None,
                    timeout_ms=timeout_ms,
                    free_search=free_search,
                    parallel=parallel,
                    random_seed=random_seed,
                    all_solutions=all_solutions,
                )
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
