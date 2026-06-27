from __future__ import annotations

import functools
import inspect
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar, cast

from anyio import to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent

from .childproc import ChildProcessTracker
from .job_errors import JobRejectedError
from .jobs import JobRegistry
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
    save_verified_model,
    solve_model,
    solve_model_path,
)
from .minizinc.core import find_unsat_core as _find_unsat_core
from .portfolio_jobs import PortfolioJobRegistry
from .protocol_text import status
from .protocol_text.descriptions import (
    CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION,
    CANCEL_PORTFOLIO_JOB_DESCRIPTION,
    CANCEL_SOLVE_JOB_DESCRIPTION,
    CHECK_MINIZINC_FILES_DESCRIPTION,
    CHECK_MINIZINC_MODEL_DESCRIPTION,
    CHECK_RUNTIME_DESCRIPTION,
    FIND_UNSAT_CORE_DESCRIPTION,
    FIND_UNSAT_CORE_FILES_DESCRIPTION,
    GET_CPSAT_PYTHON_JOB_DESCRIPTION,
    GET_PORTFOLIO_JOB_DESCRIPTION,
    GET_SOLVE_JOB_DESCRIPTION,
    INSPECT_MINIZINC_FILES_DESCRIPTION,
    INSPECT_MINIZINC_MODEL_DESCRIPTION,
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    LIST_CPSAT_PYTHON_JOBS_DESCRIPTION,
    LIST_PORTFOLIO_JOBS_DESCRIPTION,
    LIST_SOLVE_JOBS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    RUN_CPSAT_PYTHON_DESCRIPTION,
    RUN_CPSAT_PYTHON_FILE_DESCRIPTION,
    SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION,
    SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
    SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION,
    SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION,
    SUBMIT_PORTFOLIO_JOB_DESCRIPTION,
    SUBMIT_SOLVE_JOB_DESCRIPTION,
)
from .protocol_text.prompts import SOLVE_CONSTRAINT_PROBLEM_PROMPT, SOLVE_CPSAT_PYTHON_PROMPT
from .protocol_text.results import (
    SOLUTION_CHECK_NON_ADJUDICATION_NOTE,
    SOLVER_CAPABILITY_METADATA_NOTE,
    SOLVER_INVENTORY_PRESENTATION_REQUIREMENT,
    SOLVER_NUM_SOLUTIONS_NOTE,
    SOLVER_RUNTIME_CONFIG_CAUTION,
    STATS_PRESENTATION_REQUIREMENT,
)
from .pyexec.core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    run_cpsat_python,
    run_cpsat_python_file,
)
from .pyexec.jobs import CpsatJobRegistry
from .pyexec.save import SaveVerifiedPythonResult, save_verified_cpsat_python
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas import (
    CheckResult,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    ModelInspectionResult,
    PortfolioJobStatus,
    RuntimeStatus,
    SaveVerifiedModelResult,
    SolveJobStatus,
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
    runtime_status = get_runtime_status()
    lines = [
        f"{_PACKAGE_NAME} {_server_version()}",
        f"runtime dir: {runtime_status.runtime_dir}",
    ]
    if runtime_status.installed:
        lines.append(f"runtime: installed ({runtime_status.minizinc_binary})")
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


def _format_save_result_content(result: SaveVerifiedModelResult) -> str:
    """Return model-visible save output: the outcome and target first, files after.

    Deliberately concise — the verifying ``SolveResult`` (solutions, statistics,
    checker transcript) rides in ``structuredContent``; the text content states
    what happened, where, and which files exist.
    """
    lines = [
        f"Status: {result.status}",
        f"Target directory: {result.target_dir}",
        result.message,
    ]
    if result.files:
        lines.extend(["", "Saved files:"])
        lines.extend(
            f"- {artifact.path} ({artifact.role}, sha256 {artifact.sha256})"
            for artifact in result.files
        )
    lines.extend(["", f"Check status: {result.check.status}"])

    solve = result.solve
    if solve is None:
        return "\n".join(lines)

    lines.append(f"Solve status: {solve.status}")
    if solve.checker is not None:
        lines.append(f"Checker status: {solve.checker.status}")
    return "\n".join(lines)


def _wrap_save_result(result: SaveVerifiedModelResult) -> CallToolResult:
    """Wrap a SaveVerifiedModelResult as concise text plus full structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=_format_save_result_content(result))],
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


# The exact message the SDK raises from Context.report_progress/Context.log
# when no real JSON-RPC request is active (direct white-box calls in tests,
# in-process invocation). _report_status swallows only this case.
_CONTEXT_UNAVAILABLE_MESSAGE = "Context is not available outside of a request"


async def _report_status(
    ctx: Context | None,
    progress: float,
    message: str,
    *,
    total: float | None = None,
) -> None:
    """Send one status milestone to the client on both feedback channels.

    Emits ``notifications/progress`` (delivered only when the request carried
    ``_meta.progressToken``; the SDK no-ops otherwise) and an ``info``-level
    ``notifications/message`` log (delivered regardless of token), so clients
    that never send a progress token still see activity state. ``total`` is
    omitted by default on purpose: these are indeterminate stage counters, not
    a solver completion percentage, and reporting a total would invite a
    misleading percent-complete UI. Outside a real request the SDK raises
    ``ValueError(_CONTEXT_UNAVAILABLE_MESSAGE)``; only that exact case is
    swallowed — any other error from tool code still propagates.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress, total, message)
        await ctx.info(message)
    except ValueError as exc:
        if str(exc) != _CONTEXT_UNAVAILABLE_MESSAGE:
            raise


async def _status_starting(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 1-2 immediately before the blocking solver/core call."""
    await _report_status(ctx, 1, stages[0])
    await _report_status(ctx, 2, stages[1])


async def _status_finished(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 3-4 once the blocking solver/core call has returned.

    Runs for every structured result — including ``status="error"`` /
    ``"timeout"`` / ``"unsatisfiable"`` — so clients always see a final
    milestone before the response. Domain exceptions raised by the core call
    skip this on purpose: obscuring the original error with a late
    notification is worse than ending the stream early.
    """
    await _report_status(ctx, 3, stages[2])
    await _report_status(ctx, 4, stages[3])


# fn is zero-arg, so run_sync's variadic *args (a TypeVarTuple) binds to the empty
# tuple; PyCharm mis-models that and reports a false "Parameter 'args' unfilled,
# expected '*tuple[]'". mypy passes — suppress both inspections that emit it.
# noinspection PyArgumentList,PyTypeChecker
async def _run_blocking[T](fn: Callable[[], T]) -> T:
    """Run a blocking solver/core call in a worker thread.

    A core call executed inline freezes the event loop for the whole solve,
    which deterministically strands the last queued status notification (the
    stdio writer task holds it but never gets scheduled) until the solve
    finishes — defeating mid-solve feedback for exactly the clients it exists
    for. Off-loop execution also keeps the server responsive to pings and
    concurrent requests during long solves. Core functions share no mutable
    state across calls — the MiniZinc path shells out, the CP-SAT path builds
    a fresh model/solver per call — so thread safety is not a concern.
    """
    return await to_thread.run_sync(fn)


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
        if inspect.iscoroutinefunction(fn):
            # Async tools raise their domain exceptions only when awaited, so
            # the translation must happen inside a coroutine wrapper — a sync
            # wrapper would return the coroutine from `try` without ever
            # entering `except`.
            coro_fn = cast("Callable[_P, Awaitable[Any]]", fn)

            @functools.wraps(fn)
            async def _async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> Any:
                try:
                    return await coro_fn(*args, **kwargs)
                except caught as exc:
                    raise RuntimeError(str(exc)) from exc

            return cast("Callable[_P, _R]", _async_wrapper)

        @functools.wraps(fn)
        def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return fn(*args, **kwargs)
            except caught as exc:
                raise RuntimeError(str(exc)) from exc

        return _wrapper

    return _decorator


def _make_lifespan(
    registry: JobRegistry,
    cpsat_registry: CpsatJobRegistry,
    child_tracker: ChildProcessTracker,
) -> Callable[[FastMCP[Any]], AbstractAsyncContextManager[None]]:
    """Build the server lifespan bound to the registries and ``child_tracker``.

    Teardown terminates every in-flight child so none is orphaned:
    ``registry.shutdown()`` covers MiniZinc background jobs (and portfolio attempts),
    ``cpsat_registry.shutdown()`` covers CP-SAT background jobs, and
    ``child_tracker.terminate_all()`` covers the synchronous tools' children.
    All three are independently guarded so a failure in one does not skip the others.
    """

    @asynccontextmanager
    async def _lifespan(server: FastMCP[Any]) -> AsyncIterator[None]:
        _log_boot_diagnostic()
        try:
            yield
        finally:
            try:
                registry.shutdown()
            finally:
                try:
                    cpsat_registry.shutdown()
                finally:
                    child_tracker.terminate_all()

    return _lifespan


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read an integer registry bound from ``os.environ[name]`` and enforce ``minimum``.

    Returns ``default`` when the variable is unset. Otherwise parses the value and
    requires an integer ``>= minimum``, raising a ``ValueError`` that NAMES the
    offending variable — for both a non-integer and an out-of-range value — so a
    malformed bound fails fast at boot instead of silently falling back to the
    default or reaching ``JobRegistry``'s parameter-named constructor check (which
    says ``max_running_jobs must be >= 1``, not which env var was wrong). The
    constructor's own guards stay as a backstop.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def create_mcp_server() -> FastMCP:
    """Build a fresh FastMCP server and register all tools and prompts."""
    # The single server-owned job registry (D1.1): one instance per server,
    # captured by the job-tool closures and torn down by the lifespan. This is
    # the deliberate, bounded exception to "no global mutable state". The three
    # bounds default to today's values and are overridable via env vars (a
    # malformed value fails fast at boot, naming the variable).
    registry = JobRegistry(
        max_running_jobs=_env_int("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", 4, minimum=1),
        max_queued_jobs=_env_int("OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS", 16, minimum=0),
        max_retained_terminal=_env_int("OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL", 64, minimum=1),
    )
    # The server-owned background-portfolio registry: it drives the SAME `registry`
    # for attempts and selects a winner lazily on each poll, so it owns no worker
    # pool and cannot starve the attempt pool. Retention of finished portfolio
    # records is bounded; the dominant capacity bound is the solve registry's.
    portfolios = PortfolioJobRegistry(registry)
    # The server-owned CP-SAT background-job registry (the deliberate, bounded
    # exception to "no global mutable state", like `registry`): parallel to the
    # MiniZinc registry but for CP-SAT Python jobs. Bounds are independently
    # overridable via CP-SAT-prefixed env vars; defaults match the MiniZinc values.
    cpsat_registry = CpsatJobRegistry(
        max_running_jobs=_env_int("OPENCONSTRAINT_MCP_CPSAT_MAX_RUNNING_JOBS", 4, minimum=1),
        max_queued_jobs=_env_int("OPENCONSTRAINT_MCP_CPSAT_MAX_QUEUED_JOBS", 16, minimum=0),
        max_retained_terminal=_env_int(
            "OPENCONSTRAINT_MCP_CPSAT_MAX_RETAINED_TERMINAL", 64, minimum=1
        ),
    )
    # The server-owned tracker of in-flight SYNCHRONOUS-tool children (the
    # bounded "no global mutable state" exception, like `registry`): the sync
    # MiniZinc/CP-SAT tools register their child while it runs so the lifespan can
    # terminate it on teardown instead of orphaning it. Background-job children
    # are handled by `registry.shutdown()` / `cpsat_registry.shutdown()`.
    child_tracker = ChildProcessTracker()
    mcp: FastMCP[Any] = FastMCP(
        "openconstraint-mcp",
        instructions=MCP_SERVER_INSTRUCTIONS,
        website_url=_homepage_url(),
        lifespan=_make_lifespan(registry, cpsat_registry, child_tracker),
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
    async def solve_minizinc_model(
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
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        stages = status.solve_stages(checker is not None)
        await _status_starting(ctx, stages)
        result = await _run_blocking(
            functools.partial(
                solve_model,
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
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, stages)
        return _wrap_solve_result(result)

    @mcp.tool(description=CHECK_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def check_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CheckResult:
        await _status_starting(ctx, status.CHECK_STAGES)
        result = await _run_blocking(
            functools.partial(
                check_model,
                model,
                solver=solver,
                data=data,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CHECK_STAGES)
        return result

    @mcp.tool(description=INSPECT_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def inspect_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> ModelInspectionResult:
        await _status_starting(ctx, status.INSPECT_STAGES)
        result = await _run_blocking(
            functools.partial(
                inspect_model,
                model,
                solver=solver,
                data=data,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.INSPECT_STAGES)
        return result

    @mcp.tool(description=FIND_UNSAT_CORE_DESCRIPTION)
    @_as_mcp_error()
    async def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> UnsatCoreResult:
        await _status_starting(ctx, status.UNSAT_CORE_STAGES)
        result = await _run_blocking(
            functools.partial(
                _find_unsat_core, model, data=data, timeout_ms=timeout_ms, tracker=child_tracker
            )
        )
        await _status_finished(ctx, status.UNSAT_CORE_STAGES)
        return result

    @mcp.tool(description=SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def save_verified_minizinc_model(
        model: str,
        target_dir: str,
        data: str | None = None,
        checker: str | None = None,
        problem: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
        overwrite: bool = False,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SaveVerifiedModelResult]:
        await _status_starting(ctx, status.SAVE_STAGES)
        result = await _run_blocking(
            functools.partial(
                save_verified_model,
                model,
                target_dir=Path(target_dir),
                data=data,
                checker=checker,
                problem=problem,
                solver=solver,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
                overwrite=overwrite,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.SAVE_STAGES)
        return _wrap_save_result(result)

    @mcp.tool(description=CHECK_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def check_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CheckResult:
        await _status_starting(ctx, status.CHECK_STAGES)
        result = await _run_blocking(
            functools.partial(
                check_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CHECK_STAGES)
        return result

    @mcp.tool(description=INSPECT_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def inspect_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> ModelInspectionResult:
        await _status_starting(ctx, status.INSPECT_STAGES)
        result = await _run_blocking(
            functools.partial(
                inspect_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.INSPECT_STAGES)
        return result

    @mcp.tool(description=SOLVE_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def solve_minizinc_files(
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
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        stages = status.solve_stages(checker_path is not None)
        await _status_starting(ctx, stages)
        result = await _run_blocking(
            functools.partial(
                solve_model_path,
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
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, stages)
        return _wrap_solve_result(result)

    @mcp.tool(description=FIND_UNSAT_CORE_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def find_unsat_core_files(
        model_path: str,
        data_path: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> UnsatCoreResult:
        await _status_starting(ctx, status.UNSAT_CORE_STAGES)
        result = await _run_blocking(
            functools.partial(
                find_unsat_core_path,
                Path(model_path),
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.UNSAT_CORE_STAGES)
        return result

    @mcp.tool(description=SUBMIT_SOLVE_JOB_DESCRIPTION)
    # Validation raises ValueError; a full bounded queue raises JobRejectedError
    # (a direct RuntimeError subclass, NOT in the default caught set). A gated
    # control (free_search/parallel/random_seed/all_solutions) makes admission
    # resolve solver capabilities via list_solvers(), so the runtime/binary triad
    # can fire here too — exactly as for submit_portfolio_job. All must surface as
    # actionable MCP errors.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError, ValueError, JobRejectedError)
    def submit_solve_job(
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
    ) -> SolveJobStatus:
        job_id = registry.submit(
            model=model,
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
        return registry.get(job_id)

    @mcp.tool(description=GET_SOLVE_JOB_DESCRIPTION)
    # An unknown job_id is the only domain error (ValueError); the registry reads
    # touch no runtime, so the narrower caught set is honest.
    @_as_mcp_error(ValueError)
    def get_solve_job(job_id: str) -> SolveJobStatus:
        return registry.get(job_id)

    @mcp.tool(description=CANCEL_SOLVE_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_solve_job(job_id: str) -> SolveJobStatus:
        return registry.cancel(job_id)

    @mcp.tool(description=LIST_SOLVE_JOBS_DESCRIPTION)
    # Takes no arguments and only reads the registry, so no domain exception is
    # reachable — nothing to translate.
    def list_solve_jobs() -> list[SolveJobStatus]:
        return registry.list()

    @mcp.tool(description=SUBMIT_PORTFOLIO_JOB_DESCRIPTION)
    # Admission runs plan validation, the capability gate, and an atomic batch
    # admission synchronously — so it can raise the runtime/binary triad (resolving
    # a gated control), a plan ValueError, or JobRejectedError when the batch exceeds
    # the bounded queue. All must surface as actionable MCP errors.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError, ValueError, JobRejectedError)
    def submit_portfolio_job(
        models: list[str],
        solvers: list[str],
        data: str | None = None,
        checker: str | None = None,
        seed_count: int = 1,
        seeds: list[int] | None = None,
        per_attempt_timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> PortfolioJobStatus:
        job_id = portfolios.submit(
            models=models,
            solvers=solvers,
            data=data,
            checker=checker,
            seed_count=seed_count,
            seeds=seeds,
            per_attempt_timeout_ms=per_attempt_timeout_ms,
            free_search=free_search,
            parallel=parallel,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        return portfolios.get(job_id)

    @mcp.tool(description=GET_PORTFOLIO_JOB_DESCRIPTION)
    # An unknown job_id is the only domain error (ValueError); the registry reads
    # touch no runtime, so the narrower caught set is honest.
    @_as_mcp_error(ValueError)
    def get_portfolio_job(job_id: str) -> PortfolioJobStatus:
        return portfolios.get(job_id)

    @mcp.tool(description=CANCEL_PORTFOLIO_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_portfolio_job(job_id: str) -> PortfolioJobStatus:
        return portfolios.cancel(job_id)

    @mcp.tool(description=LIST_PORTFOLIO_JOBS_DESCRIPTION)
    # Takes no arguments and only reads the registry, so no domain exception is
    # reachable — nothing to translate.
    def list_portfolio_jobs() -> list[PortfolioJobStatus]:
        return portfolios.list()

    @mcp.tool(
        name="submit_cpsat_python_job",
        description=SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION,
    )
    @_as_mcp_error(ValueError, JobRejectedError)
    def submit_cpsat_python_job(
        source: str,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    ) -> CpsatPythonJobStatus:
        job_id = cpsat_registry.submit_source(source, timeout_ms=timeout_ms)
        return cpsat_registry.get(job_id)

    @mcp.tool(
        name="submit_cpsat_python_file_job",
        description=SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION,
    )
    @_as_mcp_error(ValueError, JobRejectedError)
    def submit_cpsat_python_file_job(
        script_path: str,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    ) -> CpsatPythonJobStatus:
        job_id = cpsat_registry.submit_file(Path(script_path), timeout_ms=timeout_ms)
        return cpsat_registry.get(job_id)

    @mcp.tool(name="get_cpsat_python_job", description=GET_CPSAT_PYTHON_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def get_cpsat_python_job(job_id: str) -> CpsatPythonJobStatus:
        return cpsat_registry.get(job_id)

    @mcp.tool(name="cancel_cpsat_python_job", description=CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_cpsat_python_job(job_id: str) -> CpsatPythonJobStatus:
        return cpsat_registry.cancel(job_id)

    @mcp.tool(name="list_cpsat_python_jobs", description=LIST_CPSAT_PYTHON_JOBS_DESCRIPTION)
    def list_cpsat_python_jobs() -> list[CpsatPythonJobStatus]:
        return cpsat_registry.list()

    @mcp.tool(name="run_cpsat_python", description=RUN_CPSAT_PYTHON_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_tool(
        source: str,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CpsatPythonResult:
        await _status_starting(ctx, status.CPSAT_PYTHON_STAGES)
        result = await _run_blocking(
            functools.partial(
                run_cpsat_python, source, timeout_ms=timeout_ms, tracker=child_tracker
            )
        )
        await _status_finished(ctx, status.CPSAT_PYTHON_STAGES)
        return result

    @mcp.tool(name="run_cpsat_python_file", description=RUN_CPSAT_PYTHON_FILE_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_file_tool(
        script_path: str,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CpsatPythonResult:
        await _status_starting(ctx, status.CPSAT_PYTHON_STAGES)
        result = await _run_blocking(
            functools.partial(
                run_cpsat_python_file,
                Path(script_path),
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CPSAT_PYTHON_STAGES)
        return result

    @mcp.tool(name="save_verified_cpsat_python", description=SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def save_verified_cpsat_python_tool(
        source: str,
        target_dir: str,
        problem: str | None = None,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        overwrite: bool = False,
        ctx: Context | None = None,
    ) -> SaveVerifiedPythonResult:
        await _status_starting(ctx, status.CPSAT_PYTHON_SAVE_STAGES)
        result = await _run_blocking(
            functools.partial(
                save_verified_cpsat_python,
                source,
                target_dir=Path(target_dir),
                problem=problem,
                timeout_ms=timeout_ms,
                overwrite=overwrite,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CPSAT_PYTHON_SAVE_STAGES)
        return result

    @mcp.prompt(
        name="solve_constraint_problem",
        description=SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    )
    def solve_constraint_problem(problem: str) -> str:
        return SOLVE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    @mcp.prompt(
        name="solve_cpsat_python",
        description=SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION,
    )
    def solve_cpsat_python_prompt(problem: str) -> str:
        return SOLVE_CPSAT_PYTHON_PROMPT.format(problem=problem)

    return mcp


def run_stdio() -> None:
    """Create the MCP server and run it over stdio for CLI/client use."""
    create_mcp_server().run(transport="stdio")
