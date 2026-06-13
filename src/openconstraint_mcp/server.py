from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar, cast

from anyio import to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent

from .jobs import JobRegistry, JobRejectedError
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
from .protocol_text.descriptions import (
    CANCEL_SOLVE_JOB_DESCRIPTION,
    CHECK_MINIZINC_FILES_DESCRIPTION,
    CHECK_MINIZINC_MODEL_DESCRIPTION,
    CHECK_RUNTIME_DESCRIPTION,
    FIND_UNSAT_CORE_DESCRIPTION,
    FIND_UNSAT_CORE_FILES_DESCRIPTION,
    GET_SOLVE_JOB_DESCRIPTION,
    INSPECT_MINIZINC_FILES_DESCRIPTION,
    INSPECT_MINIZINC_MODEL_DESCRIPTION,
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    LIST_SOLVE_JOBS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
    SUBMIT_SOLVE_JOB_DESCRIPTION,
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
    if result.solve is not None:
        lines.append(f"Solve status: {result.solve.status}")
        if result.solve.checker is not None:
            lines.append(f"Checker status: {result.solve.checker.status}")
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


# One four-stage milestone schedule per tool family, shared by the string- and
# path-based variants so the two cannot drift.
_CHECK_STAGES = (
    "Validating check request",
    "MiniZinc compile check is running",
    "MiniZinc finished; parsing check result",
    "Check complete",
)
_INSPECT_STAGES = (
    "Validating inspect request",
    "MiniZinc model interface analysis is running",
    "MiniZinc finished; parsing model interface",
    "Inspection complete",
)
_UNSAT_CORE_STAGES = (
    "Validating unsat-core request",
    "findMUS is running",
    "findMUS finished; parsing core",
    "Unsat-core analysis complete",
)
# The save family re-verifies (check, then solve) and commits inside one
# blocking call, so stage 2 spans the whole pipeline and stages 3-4 are honest
# for both outcomes — a committed save and a not_verified refusal.
_SAVE_STAGES = (
    "Validating save request",
    "MiniZinc verification (check, then solve) and save are running",
    "MiniZinc finished; save decision made",
    "Save request complete",
)


def _solve_stages(with_checker: bool) -> tuple[str, str, str, str]:
    """Return the solve-family milestone messages, checker-aware at stages 2-3."""
    if with_checker:
        return (
            "Validating solve request",
            "MiniZinc solve with solution checker is running",
            "MiniZinc finished; parsing solve and checker streams",
            "Solve complete",
        )
    return (
        "Validating solve request",
        "MiniZinc solve is running",
        "MiniZinc finished; parsing solve stream",
        "Solve complete",
    )


async def _status_starting(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 1-2 immediately before the blocking MiniZinc call."""
    await _report_status(ctx, 1, stages[0])
    await _report_status(ctx, 2, stages[1])


async def _status_finished(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 3-4 once the blocking MiniZinc call has returned.

    Runs for every structured result — including ``status="error"`` /
    ``"timeout"`` / ``"unsatisfiable"`` — so clients always see a final
    milestone before the response. Domain exceptions raised by the core call
    skip this on purpose: obscuring the original error with a late
    notification is worse than ending the stream early.
    """
    await _report_status(ctx, 3, stages[2])
    await _report_status(ctx, 4, stages[3])


async def _run_blocking[T](fn: Callable[[], T]) -> T:
    """Run a blocking MiniZinc core call in a worker thread.

    A core call executed inline freezes the event loop for the whole solve,
    which deterministically strands the last queued status notification (the
    stdio writer task holds it but never gets scheduled) until the solve
    finishes — defeating mid-solve feedback for exactly the clients it exists
    for. Off-loop execution also keeps the server responsive to pings and
    concurrent requests during long solves. Core functions are stateless
    subprocess wrappers, so thread safety is not a concern.
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
) -> Callable[[FastMCP[Any]], AbstractAsyncContextManager[None]]:
    """Build the server lifespan bound to ``registry``.

    Startup emits the boot diagnostic; teardown (after ``yield``) calls
    ``registry.shutdown()`` so any still-running solve children are terminated
    and the worker pool joined before the process exits. The registry is created
    in ``create_mcp_server`` and captured by the job-tool closures, so the
    lifespan must close over that same instance — hence a factory rather than a
    module-level function.
    """

    @asynccontextmanager
    async def _lifespan(server: FastMCP[Any]) -> AsyncIterator[None]:
        _log_boot_diagnostic()
        try:
            yield
        finally:
            registry.shutdown()

    return _lifespan


def create_mcp_server() -> FastMCP:
    """Build a fresh FastMCP server and register all tools and prompts."""
    # The single server-owned job registry (D1.1): one instance per server,
    # captured by the job-tool closures and torn down by the lifespan. This is
    # the deliberate, bounded exception to "no global mutable state".
    registry = JobRegistry()
    mcp: FastMCP[Any] = FastMCP(
        "openconstraint-mcp",
        instructions=MCP_SERVER_INSTRUCTIONS,
        website_url=_homepage_url(),
        lifespan=_make_lifespan(registry),
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
        stages = _solve_stages(checker is not None)
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
        await _status_starting(ctx, _CHECK_STAGES)
        result = await _run_blocking(
            functools.partial(check_model, model, solver=solver, data=data, timeout_ms=timeout_ms)
        )
        await _status_finished(ctx, _CHECK_STAGES)
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
        await _status_starting(ctx, _INSPECT_STAGES)
        result = await _run_blocking(
            functools.partial(
                inspect_model, model, solver=solver, data=data, timeout_ms=timeout_ms
            )
        )
        await _status_finished(ctx, _INSPECT_STAGES)
        return result

    @mcp.tool(description=FIND_UNSAT_CORE_DESCRIPTION)
    @_as_mcp_error()
    async def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> UnsatCoreResult:
        await _status_starting(ctx, _UNSAT_CORE_STAGES)
        result = await _run_blocking(
            functools.partial(_find_unsat_core, model, data=data, timeout_ms=timeout_ms)
        )
        await _status_finished(ctx, _UNSAT_CORE_STAGES)
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
        await _status_starting(ctx, _SAVE_STAGES)
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
            )
        )
        await _status_finished(ctx, _SAVE_STAGES)
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
        await _status_starting(ctx, _CHECK_STAGES)
        result = await _run_blocking(
            functools.partial(
                check_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        )
        await _status_finished(ctx, _CHECK_STAGES)
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
        await _status_starting(ctx, _INSPECT_STAGES)
        result = await _run_blocking(
            functools.partial(
                inspect_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        )
        await _status_finished(ctx, _INSPECT_STAGES)
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
        stages = _solve_stages(checker_path is not None)
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
        await _status_starting(ctx, _UNSAT_CORE_STAGES)
        result = await _run_blocking(
            functools.partial(
                find_unsat_core_path,
                Path(model_path),
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
            )
        )
        await _status_finished(ctx, _UNSAT_CORE_STAGES)
        return result

    @mcp.tool(description=SUBMIT_SOLVE_JOB_DESCRIPTION)
    # Validation raises ValueError; a full bounded queue raises JobRejectedError
    # (a direct RuntimeError subclass, NOT in the default caught set) — both must
    # surface as actionable MCP errors. The runtime is touched only in the worker,
    # so RuntimeMissingError/MiniZincExecutionError cannot reach this submit path.
    @_as_mcp_error(ValueError, JobRejectedError)
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
