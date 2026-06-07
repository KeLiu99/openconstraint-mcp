from __future__ import annotations

import functools
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

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
from .protocol_text.results import (
    SOLUTION_CHECK_NON_ADJUDICATION_NOTE,
    SOLVER_CAPABILITY_METADATA_NOTE,
    SOLVER_INVENTORY_PRESENTATION_REQUIREMENT,
    SOLVER_NUM_SOLUTIONS_NOTE,
    SOLVER_RUNTIME_CONFIG_CAUTION,
    STATS_PRESENTATION_REQUIREMENT,
)
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
        lines.extend(["", STATS_PRESENTATION_REQUIREMENT, "Statistics:"])
        preferred = [k for k in _PREFERRED_STAT_KEYS if k in result.statistics]
        others = [k for k in result.statistics if k not in _PREFERRED_STAT_KEYS]
        lines.extend([f"- {key}: {result.statistics[key]}" for key in (preferred + others)])

    solve_text = "\n".join(lines)
    if result.checker is None:
        return solve_text

    violations = sum(1 for check in result.checker.checks if check.violation)
    return "\n".join(
        [
            f"Checker status: {result.checker.status}",
            f"Solve status: {result.status}",
            f"Solutions produced: {len(result.solutions)}",
            f"Violations: {violations}",
            "",
            SOLUTION_CHECK_NON_ADJUDICATION_NOTE,
            "",
            solve_text,
        ]
    )


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
            SOLVER_INVENTORY_PRESENTATION_REQUIREMENT,
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
            SOLVER_CAPABILITY_METADATA_NOTE,
            "",
            SOLVER_NUM_SOLUTIONS_NOTE,
            "",
            SOLVER_RUNTIME_CONFIG_CAUTION,
        ]
    )


def _wrap_solver_list(result: SolverList) -> CallToolResult:
    """Wrap a SolverList as a complete-inventory text block plus structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=_format_solver_list_content(result))],
        structuredContent=result.model_dump(mode="json"),
    )


_P = ParamSpec("_P")
_R = TypeVar("_R")

# The domain exceptions every solving/checking tool translates: the managed
# runtime is absent, the managed binary failed, or an argument was rejected.
_DEFAULT_MCP_ERROR_TYPES: tuple[type[Exception], ...] = (
    RuntimeMissingError,
    MiniZincExecutionError,
    ValueError,
)


def _as_mcp_error(
    *exc_types: type[Exception],
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Translate a tool's domain exceptions into a plain MCP ``RuntimeError``.

    The single home for the ``(domain exception -> RuntimeError)`` invariant: on
    any of ``exc_types`` (defaulting to the runtime/execution/value triad), the
    domain message is re-raised verbatim as a ``RuntimeError`` with the original
    exception preserved as ``__cause__``. Pass a narrower tuple to catch less —
    ``list_available_solvers`` takes no user arguments, so it deliberately does
    not translate ``ValueError`` (a ``ValueError`` there is a real bug, not an
    actionable user message).

    v0 policy is to surface the message string so MCP clients see something
    actionable. A future structured error envelope (e.g. ``{"code":
    "runtime_missing", "hint": "run install-runtime"}``) that lets clients branch
    programmatically instead of parsing the message belongs here, in this one
    place. ``functools.wraps`` preserves the wrapped tool's signature and
    annotations so FastMCP derives an unchanged schema from the decorated tool.
    """
    caught = exc_types or _DEFAULT_MCP_ERROR_TYPES

    def _decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return fn(*args, **kwargs)
            except caught as exc:
                raise RuntimeError(str(exc)) from exc

        return _wrapper

    return _decorator


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
    # Narrower than the default: this tool takes no user arguments, so a
    # ValueError would be a real bug, not an actionable user message.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def list_available_solvers() -> Annotated[CallToolResult, SolverList]:
        return _wrap_solver_list(list_solvers())

    @mcp.tool(description=SOLVE_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    def solve_minizinc_model(
        model: str,
        data: str | None = None,
        checker: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        result = solve_model(
            model,
            solver=solver,
            data=data,
            checker=checker,
            timeout_ms=timeout_ms,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        return _wrap_solve_result(result)

    @mcp.tool(description=CHECK_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    def check_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    ) -> CheckResult:
        return check_model(model, solver=solver, data=data, timeout_ms=timeout_ms)

    @mcp.tool(description=INSPECT_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    def inspect_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    ) -> ModelInspectionResult:
        return inspect_model(model, solver=solver, data=data, timeout_ms=timeout_ms)

    @mcp.tool(description=FIND_UNSAT_CORE_DESCRIPTION)
    @_as_mcp_error()
    def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        return _find_unsat_core(model, data=data, timeout_ms=timeout_ms)

    @mcp.tool(description=CHECK_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    def check_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    ) -> CheckResult:
        return check_model_path(
            Path(model_path),
            solver=solver,
            data_path=Path(data_path) if data_path is not None else None,
            timeout_ms=timeout_ms,
        )

    @mcp.tool(description=INSPECT_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    def inspect_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    ) -> ModelInspectionResult:
        return inspect_model_path(
            Path(model_path),
            solver=solver,
            data_path=Path(data_path) if data_path is not None else None,
            timeout_ms=timeout_ms,
        )

    @mcp.tool(description=SOLVE_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    def solve_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        checker_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        result = solve_model_path(
            Path(model_path),
            solver=solver,
            data_path=Path(data_path) if data_path is not None else None,
            checker_path=Path(checker_path) if checker_path is not None else None,
            timeout_ms=timeout_ms,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        return _wrap_solve_result(result)

    @mcp.tool(description=FIND_UNSAT_CORE_FILES_DESCRIPTION)
    @_as_mcp_error()
    def find_unsat_core_files(
        model_path: str,
        data_path: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    ) -> UnsatCoreResult:
        return find_unsat_core_path(
            Path(model_path),
            data_path=Path(data_path) if data_path is not None else None,
            timeout_ms=timeout_ms,
        )

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
