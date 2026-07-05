from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.core import MiniZincExecutionError
from openconstraint_mcp.runtime import RuntimeMissingError

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _as_mcp_error,
    create_mcp_server,
)
from openconstraint_mcp.shared.job_errors import JobRejectedError

# --- _as_mcp_error: the single (domain exc -> RuntimeError) translation home ---
#
# RuntimeMissingError and MiniZincExecutionError both subclass RuntimeError, so a
# bare `pytest.raises(RuntimeError)` cannot tell a *translated* plain RuntimeError
# apart from the domain subclass passing through untouched. Every translation
# assertion therefore pins `type(...) is RuntimeError` plus the `__cause__` chain.


def _no_subprocess(*args: object, **kwargs: object) -> None:
    raise AssertionError("subprocess.run must not be invoked: validation precedes it")


def _tool_fn(name: str) -> Any:
    """Return a registered tool's underlying (decorated) function.

    Reaches past ``mcp.call_tool`` — which re-wraps a tool's exception in its own
    error type — to the decorated function itself, the only seam where a test can
    observe the exact ``RuntimeError`` type and its preserved ``__cause__``.
    """
    return create_mcp_server()._tool_manager.get_tool(name).fn


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeMissingError("runtime missing; run install-runtime"),
        MiniZincExecutionError("managed binary failed: bad config"),
        ValueError("model must not be empty"),
    ],
    ids=["runtime_missing", "execution_error", "value_error"],
)
def test_as_mcp_error_translates_default_domain_exceptions(exc: Exception) -> None:
    @_as_mcp_error()
    def tool() -> None:
        raise exc

    with pytest.raises(RuntimeError) as exc_info:
        tool()

    assert type(exc_info.value) is RuntimeError
    assert str(exc_info.value) == str(exc)
    assert exc_info.value.__cause__ is exc


def test_as_mcp_error_does_not_translate_unlisted_exception() -> None:
    boom = KeyError("unexpected")

    @_as_mcp_error()
    def tool() -> None:
        raise boom

    with pytest.raises(KeyError) as exc_info:
        tool()
    assert exc_info.value is boom


def test_as_mcp_error_narrow_set_translates_a_listed_type() -> None:
    boom = MiniZincExecutionError("bad config")

    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def tool() -> None:
        raise boom

    with pytest.raises(RuntimeError) as exc_info:
        tool()
    assert type(exc_info.value) is RuntimeError
    assert exc_info.value.__cause__ is boom


def test_as_mcp_error_narrow_set_skips_unlisted_value_error() -> None:
    # The list_available_solvers caught set omits ValueError; it must propagate.
    boom = ValueError("model must not be empty")

    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def tool() -> None:
        raise boom

    with pytest.raises(ValueError) as exc_info:
        tool()
    assert exc_info.value is boom


def test_as_mcp_error_returns_value_on_success() -> None:
    @_as_mcp_error()
    def tool() -> str:
        return "ok"

    assert tool() == "ok"


@pytest.mark.asyncio
async def test_as_mcp_error_translates_domain_exception_from_async_tool() -> None:
    # The async branch must translate inside the coroutine: a sync wrapper would
    # return the un-awaited coroutine from `try` and never reach `except`.
    boom = ValueError("model must not be empty")

    @_as_mcp_error()
    async def tool() -> None:
        raise boom

    with pytest.raises(RuntimeError) as exc_info:
        await tool()

    assert type(exc_info.value) is RuntimeError
    assert str(exc_info.value) == str(boom)
    assert exc_info.value.__cause__ is boom


@pytest.mark.asyncio
async def test_as_mcp_error_returns_value_from_async_tool_on_success() -> None:
    @_as_mcp_error()
    async def tool() -> str:
        return "ok"

    assert await tool() == "ok"


@pytest.mark.asyncio
async def test_as_mcp_error_does_not_translate_unlisted_exception_from_async_tool() -> None:
    boom = KeyError("not in the caught set")

    @_as_mcp_error()
    async def tool() -> None:
        raise boom

    with pytest.raises(KeyError) as exc_info:
        await tool()
    assert exc_info.value is boom


def test_as_mcp_error_preserves_signature_for_fastmcp() -> None:
    # FastMCP derives each tool's schema from the wrapped function's signature;
    # functools.wraps must keep it visible through the decorator.
    def tool(model: str, timeout_ms: int = 5) -> bool:
        return True

    decorated = _as_mcp_error()(tool)

    assert decorated.__wrapped__ is tool  # type: ignore[attr-defined]
    assert decorated.__name__ == "tool"
    assert inspect.signature(decorated) == inspect.signature(tool)


# --- per-tool wiring: each tool carries the correct caught set --------------


@pytest.mark.parametrize(
    "tool_name",
    ["solve_minizinc_model", "check_minizinc_model", "inspect_minizinc_model", "find_unsat_core"],
)
@pytest.mark.asyncio
async def test_string_tools_translate_value_error_with_cause(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty model raises ValueError before the runtime gate; the default caught
    # set must convert it to a plain RuntimeError with the cause preserved. The
    # direct call passes no ctx, so the async wrappers must default it to None.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.Popen", _no_subprocess)
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model="")

    assert type(exc_info.value) is RuntimeError
    assert "empty" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "tool_name",
    [
        "solve_minizinc_files",
        "check_minizinc_files",
        "inspect_minizinc_files",
        "find_unsat_core_files",
    ],
)
@pytest.mark.asyncio
async def test_file_tools_translate_value_error_with_cause(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing model path raises ValueError before the runtime gate; same
    # translation invariant on the path-based tools.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.Popen", _no_subprocess)
    missing = tmp_path / "nope.mzn"
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model_path=str(missing))

    assert type(exc_info.value) is RuntimeError
    assert "does not exist" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_save_tool_translates_target_value_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A relative target_dir raises ValueError ahead of the runtime gate and any
    # subprocess; the default caught set converts it to a plain RuntimeError
    # whose message tells the client how to fix the call.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.Popen", _no_subprocess)
    fn = _tool_fn("save_verified_minizinc_model")

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model="solve satisfy;", target_dir="relative/project")

    assert type(exc_info.value) is RuntimeError
    assert "absolute" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_list_available_solvers_translates_execution_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = MiniZincExecutionError("bad config")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.server.list_solvers", _raise)
    fn = _tool_fn("list_available_solvers")

    with pytest.raises(RuntimeError) as exc_info:
        fn()

    assert type(exc_info.value) is RuntimeError
    assert str(exc_info.value) == "bad config"
    assert exc_info.value.__cause__ is boom


def test_list_available_solvers_does_not_translate_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its narrower caught set omits ValueError: a ValueError here is a real bug,
    # so it must propagate untouched rather than masquerade as an actionable
    # RuntimeError message.
    boom = ValueError("unexpected internal error")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.server.list_solvers", _raise)
    fn = _tool_fn("list_available_solvers")

    with pytest.raises(ValueError) as exc_info:
        fn()
    assert exc_info.value is boom


# --- background-job tools: unknown-id and queue-full translation ------------


@pytest.mark.parametrize("tool_name", ["get_solve_job", "cancel_solve_job"])
def test_job_lookup_tools_translate_unknown_id_with_cause(tool_name: str) -> None:
    # The registry raises ValueError for an unknown job_id; the default-caught
    # ValueError becomes a plain RuntimeError carrying the cause. These tools are
    # synchronous (fast registry reads), so they are called directly.
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        fn(job_id="does-not-exist")

    assert type(exc_info.value) is RuntimeError
    assert "unknown" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_submit_solve_job_translates_queue_full_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JobRejectedError subclasses RuntimeError but is NOT in the default caught
    # set, so submit_solve_job must name it explicitly; the assertion pins the
    # exact RuntimeError type to prove translation (not subclass passthrough).
    boom = JobRejectedError("Job queue is full (4 running + 16 queued).")

    def _raise(self: object, **kwargs: object) -> str:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.jobs.JobRegistry.submit", _raise)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="solve satisfy;")

    assert type(exc_info.value) is RuntimeError
    assert "queue is full" in str(exc_info.value)
    assert exc_info.value.__cause__ is boom


def test_submit_solve_job_translates_value_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty model raises ValueError in submit's up-front validation, before any
    # job or subprocess; the default caught set converts it to a plain RuntimeError.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.Popen", _no_subprocess)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="")

    assert type(exc_info.value) is RuntimeError
    assert "empty" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_submit_solve_job_translates_capability_gate_runtime_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gated control (free_search) makes admission resolve solver capabilities,
    # which runs list_solvers() — so an uninstalled/corrupt runtime raises
    # RuntimeMissingError at submit, BEFORE any job exists. The wrapper must
    # translate it like every other actionable runtime error (pin the exact
    # RuntimeError type to prove translation, not subclass passthrough).
    boom = RuntimeMissingError("runtime missing; run install-runtime")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.list_solvers", _raise)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="solve satisfy;", free_search=True)

    assert type(exc_info.value) is RuntimeError
    assert "install-runtime" in str(exc_info.value)
    assert exc_info.value.__cause__ is boom
