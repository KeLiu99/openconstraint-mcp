from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _CONTEXT_UNAVAILABLE_MESSAGE,
    _report_status,
    _run_blocking,
    create_mcp_server,
)
from tests.minizinc.helpers import (
    UNSAT_CORE_MODEL,
    UNSAT_CORE_STDOUT,
    FakeCompletedProcess,
    checker_pass,
    checker_violation,
    solution_obj,
    solution_obj_json_only,
    stream,
)


@pytest.mark.asyncio
async def test_list_available_solvers_surfaces_actionable_error_on_binary_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[str(fake_minizinc_binary), "--solvers-json"],
            stderr="bad config\n",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("list_available_solvers", {})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "bad config" in message


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-content dict from a FastMCP call_tool result.

    FastMCP returns direct ``CallToolResult`` objects for tools that control
    their own model-visible content, and a tuple of
    ``(content_blocks, structured_content)`` for ordinary structured returns.
    """
    if isinstance(result, CallToolResult):
        assert result.structuredContent is not None
        return result.structuredContent
    return result[1]


def _content_text(result: Any) -> str:
    if isinstance(result, CallToolResult):
        content = result.content
    else:
        content = result[0]
    return "\n".join(block.text for block in content if hasattr(block, "text"))


def _record_data_run(
    monkeypatch: pytest.MonkeyPatch, completed: FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to capture the inline-data file contents at call time.

    The runtime deletes its temp dir on return, so the ``data.dzn`` written from
    an inline ``data`` argument must be read inside the fake, before cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        cmd = args[0]
        data_contents: str | None = None
        data_path = next((Path(arg) for arg in cmd if str(arg).endswith(".dzn")), None)
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        calls.append({"data_contents": data_contents})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_list_available_solvers_surfaces_capabilities(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Happy path (the other server test only covers the failure path): the richer
    # SolverInfo flows through to structured content with no per-field wiring.
    payload = json.dumps(
        [
            {
                "id": "org.gecode.gecode",
                "name": "Gecode",
                "version": "6.3.0",
                "stdFlags": ["-a", "-f", "-n", "-p", "-r"],
            },
            {
                "id": "com.google.or-tools.cpsat",
                "name": "OR-Tools CP-SAT",
                "version": "9.10",
                "stdFlags": ["-a", "-i", "-f", "-p", "-r"],
            },
        ]
    )

    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(stdout=payload, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool("list_available_solvers", {})

    structured = _structured(result)
    by_id = {solver["id"]: solver for solver in structured["solvers"]}
    gecode_caps = by_id["org.gecode.gecode"]["capabilities"]
    cpsat_caps = by_id["com.google.or-tools.cpsat"]["capabilities"]

    # gecode declares -n and is allowlisted; or-tools declares -a/-f/-p/-r but not
    # -n and is not allowlisted, so the conservative gate holds across the boundary.
    assert gecode_caps["supports_num_solutions"] is True
    assert gecode_caps["std_flags"] == ["-a", "-f", "-n", "-p", "-r"]
    assert cpsat_caps["supports_num_solutions"] is False
    assert cpsat_caps["supports_parallel"] is True
    assert "Detailed solver capabilities" in structured["capability_note"]


# A distinctive `--cp-profiler` flag in gecode's stdFlags lets a test prove the
# default text view does NOT dump raw std_flags values (only the field name).
_LIST_SOLVERS_PAYLOAD = json.dumps(
    [
        {
            "id": "org.gecode.gecode",
            "name": "Gecode",
            "version": "6.3.0",
            "stdFlags": ["-a", "-f", "-n", "-p", "-r", "--cp-profiler"],
        },
        {
            "id": "cp-sat",
            "name": "OR Tools CP-SAT",
            "version": "9.15",
            "stdFlags": ["-a", "-i", "-f", "-p", "-r"],
        },
    ]
)


def _patch_list_solvers_run(
    monkeypatch: pytest.MonkeyPatch, payload: str = _LIST_SOLVERS_PAYLOAD
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(stdout=payload, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)


@pytest.mark.asyncio
async def test_list_available_solvers_text_lists_every_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model-visible text must carry a complete row per solver, so a client
    # cannot silently drop entries it would otherwise summarize away.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "| id | name | version |" in text
    assert "capability hint" not in text.lower()
    for token in (
        "org.gecode.gecode",
        "Gecode",
        "6.3.0",
        "cp-sat",
        "OR Tools CP-SAT",
        "9.15",
    ):
        assert token in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_requires_complete_inventory(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The presentation requirement is what steers clients away from "additional
    # solvers" elisions; assert the binding instruction is present.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "copy the solver inventory table below" in text
    assert "Do not omit rows" in text
    assert "convert it to bullets" in text
    assert "summarize, group" in text
    assert "additional solvers" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_notes_num_solutions_support(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The routing note names exactly the two gated solvers and flags the default
    # cp-sat as unsupported.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "is supported only by" in text
    assert "org.chuffed.chuffed" in text
    assert "org.gecode.gecode" in text
    assert "cp-sat` solver does not support it" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_cautions_external_mip_setup(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A declared MIP solver entry is not a guarantee it can run; the caution must
    # name the commercial/external solvers and the separate-setup requirement.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "runtime configuration" in text
    for solver in ("CPLEX", "Gurobi", "Xpress", "SCIP", "COIN-BC"):
        assert solver in text
    assert "separate installed binaries" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_points_to_structured_capabilities_without_dumping_flags(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The text advertises where the rich capability metadata lives (structured, on
    # request) but must NOT dump raw std_flags values into the default view — the
    # distinctive `--cp-profiler` flag from the payload proves no flag dump.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "ask for them explicitly" in text
    assert "Detailed solver capabilities are available on request" in text
    assert "capabilities.supports_all_solutions" in text
    assert "for each solver" in text
    assert "std_flags" in text
    assert "--cp-profiler" not in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_minizinc_model" in names

    tool = next(t for t in tools if t.name == "solve_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {
        "model",
        "data",
        "solver",
        "timeout_ms",
        "free_search",
        "parallel",
        "random_seed",
        "all_solutions",
        "num_solutions",
        "checker",
    } <= set(properties.keys())


@pytest.mark.asyncio
async def test_solve_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=stream(
                solution_obj("x = 3;\n", {"x": 3}),
                {"type": "statistics", "statistics": {"solveTime": 0.01}},
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    # A single `satisfy` that finds a solution emits no status object; the
    # return-code fallback resolves a clean exit with a solution to "satisfied".
    assert structured["status"] == "satisfied"
    assert structured["solver"] == "cp-sat"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    # The structured solution fields are reconstructed from the stream's
    # output.json payload; a satisfaction model carries no _objective.
    assert structured["solution"] == {"x": 3}
    assert structured["solutions"] == [{"x": 3}]
    assert structured["objective"] is None
    assert structured["checker"] is None
    # Parsed statistics objects flow through FastMCP structured content.
    assert structured["statistics"] == {"solveTime": "0.01"}

    # The model-visible default content also highlights non-empty statistics,
    # instead of relying on clients to notice them inside raw JSON.
    text = _content_text(result)
    lower_text = text.lower()
    assert "Final answer requirement" in text
    assert "entire Statistics section" in text
    assert "do not omit" in lower_text
    assert "summarize" in lower_text
    assert "selected fields" in lower_text
    assert "Statistics:" in text
    assert "- solveTime: 0.01" in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_accepts_inline_checker(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;",
            "checker": _CHECKER_SRC,
        },
    )

    structured = _structured(result)
    assert structured["status"] == "satisfied"
    assert structured["checker"]["status"] == "completed"
    assert structured["checker"]["checks"] == [{"violation": False, "output": "CORRECT\n"}]
    assert '"type": "checker"' in structured["checker"]["transcript"]
    text = _content_text(result)
    assert text.startswith("Checker status: completed")
    assert "not interpreted" in text or "NOT interpreted" in text
    assert '"statistics"' not in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_shows_solution_when_model_has_no_output_item(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model with no explicit `output` item streams only the json section. The
    # MCP-visible text must still carry a human solution block (not just status and
    # statistics), so the solution never disappears from the model-visible content.
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=stream(solution_obj_json_only({"x": 5, "_objective": 5})),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve maximize x;"},
    )

    assert _structured(result)["solution"] == {"x": 5}
    text = _content_text(result)
    assert "Stdout:" in text
    assert "x = 5" in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;", "data": "n = 3;"},
    )

    assert calls[0]["data_contents"] == "n = 3;"
    assert _structured(result)["status"] == "satisfied"


@pytest.mark.asyncio
async def test_solve_minizinc_model_omits_visible_statistics_when_empty(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    assert _structured(result)["statistics"] == {}
    assert "Final answer requirement" not in _content_text(result)
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
async def test_solve_minizinc_model_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_model", {"model": "solve satisfy;"})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.asyncio
async def test_solve_minizinc_model_empty_model_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_model", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_non_positive_timeout_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "timeout_ms": 0},
        )
    assert "positive" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_compile_error_returns_structured_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout="",
            stderr="MiniZinc: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


@pytest.mark.asyncio
async def test_solve_minizinc_model_forwards_search_flags_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..5: x;\nsolve satisfy;",
            "free_search": True,
            "parallel": 2,
            "random_seed": 7,
            "all_solutions": True,
        },
    )

    cmd = calls[0]["cmd"]
    assert "-f" in cmd and "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == "7"


@pytest.mark.asyncio
async def test_solve_minizinc_model_invalid_parallel_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad parallel")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "parallel": 0},
        )
    assert "parallel" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_num_solutions_forwards_n_flag_for_supported_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..5: x;\nsolve satisfy;",
            "solver": "org.chuffed.chuffed",
            "num_solutions": 2,
        },
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-n") + 1] == "2"


@pytest.mark.asyncio
async def test_solve_minizinc_model_num_solutions_rejected_for_default_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "num_solutions": 2},
        )
    message = str(exc_info.value)
    assert "num_solutions" in message
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.asyncio
async def test_check_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "check_minizinc_model" in names

    tool = next(t for t in tools if t.name == "check_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "data", "solver", "timeout_ms"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_check_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["solver"] == "cp-sat"


@pytest.mark.asyncio
async def test_check_minizinc_model_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0))

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "int: n;\nvar 1..n: x;\nsolve satisfy;", "data": "n = 3;"},
    )

    assert calls[0]["data_contents"] == "n = 3;"
    assert _structured(result)["status"] == "ok"


@pytest.mark.asyncio
async def test_check_minizinc_model_compile_error_returns_structured_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


@pytest.mark.asyncio
async def test_check_minizinc_model_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("check_minizinc_model", {"model": "solve satisfy;"})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.asyncio
async def test_check_minizinc_model_empty_model_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("check_minizinc_model", {"model": ""})
    assert "empty" in str(exc_info.value)


_SOLVE_ONLY_CONTROLS = {"free_search", "parallel", "random_seed", "all_solutions", "num_solutions"}

# A single-line interface object as the managed binary emits under
# `--model-interface-only`: one required array param and one output var.
_INSPECT_STDOUT = (
    '{"type": "interface", "input": {"weight": {"type": "int", "dim": 1}}, '
    '"output": {"take": {"type": "bool", "dim": 1}}, "method": "max", '
    '"has_output_item": false, "included_files": [], "globals": []}'
)


@pytest.mark.asyncio
async def test_inspect_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "inspect_minizinc_model" in names

    tool = next(t for t in tools if t.name == "inspect_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "data", "solver", "timeout_ms"} <= set(properties.keys())
    # Decision 8: inspection exposes no solve-only search controls.
    assert not (_SOLVE_ONLY_CONTROLS & set(properties.keys()))


@pytest.mark.asyncio
async def test_inspect_minizinc_files_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "inspect_minizinc_files" in names

    tool = next(t for t in tools if t.name == "inspect_minizinc_files")
    properties = tool.inputSchema.get("properties", {})
    assert {"model_path", "data_path", "solver", "timeout_ms"} <= set(properties.keys())
    assert not (_SOLVE_ONLY_CONTROLS & set(properties.keys()))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["inspect_minizinc_model", "inspect_minizinc_files"])
async def test_inspect_tools_output_schema_advertises_field_names(tool_name: str) -> None:
    # Concern 3: with no Pydantic aliases the advertised outputSchema must list the
    # public field names base_type/is_set/is_optional for InterfaceType — never
    # MiniZinc's raw type/set/optional — so a client validating structured output
    # against it does not fail.
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    tool = next(t for t in tools if t.name == tool_name)
    assert tool.outputSchema is not None
    type_props = tool.outputSchema["$defs"]["InterfaceType"]["properties"]
    assert set(type_props) == {"base_type", "dim", "is_set", "is_optional"}
    assert "type" not in type_props
    assert "set" not in type_props


@pytest.mark.asyncio
async def test_inspect_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(stdout=_INSPECT_STDOUT, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "inspect_minizinc_model",
        {
            "model": "array[1..3] of int: weight;\narray[1..3] of var bool: take;\n"
            "solve maximize sum(i in 1..3)(weight[i] * take[i]);"
        },
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["interface"]["method"] == "max"
    # The structured payload carries the public field names, matching the
    # advertised outputSchema (Concern 3).
    weight = structured["interface"]["required_parameters"]["weight"]
    assert set(weight) == {"base_type", "dim", "is_set", "is_optional"}
    assert weight["base_type"] == "int"
    assert weight["dim"] == 1


@pytest.mark.asyncio
async def test_inspect_minizinc_model_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("inspect_minizinc_model", {"model": "solve satisfy;"})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.asyncio
async def test_inspect_minizinc_model_empty_model_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("inspect_minizinc_model", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_find_unsat_core_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "find_unsat_core" in names

    tool = next(t for t in tools if t.name == "find_unsat_core")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "data", "timeout_ms"} <= set(properties.keys())
    assert "solver" not in properties


@pytest.mark.asyncio
async def test_find_unsat_core_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=UNSAT_CORE_STDOUT,
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool("find_unsat_core", {"model": UNSAT_CORE_MODEL})

    structured = _structured(result)
    assert structured["status"] == "mus_found"
    assert len(structured["core"]) == 2


@pytest.mark.asyncio
async def test_find_unsat_core_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(
        monkeypatch,
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "find_unsat_core",
        {"model": UNSAT_CORE_MODEL, "data": "lo = 5;"},
    )

    assert calls[0]["data_contents"] == "lo = 5;"
    assert _structured(result)["status"] == "mus_found"


@pytest.mark.asyncio
async def test_find_unsat_core_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("find_unsat_core", {"model": "solve satisfy;"})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.asyncio
async def test_find_unsat_core_empty_model_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("find_unsat_core", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_find_unsat_core_non_positive_timeout_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "find_unsat_core",
            {"model": "constraint false;\nsolve satisfy;", "timeout_ms": 0},
        )
    assert "positive" in str(exc_info.value)


# --- path-based file tools -------------------------------------------------

_FILE_MODEL_SRC = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"


def _record_run_capturing_cwd(
    monkeypatch: pytest.MonkeyPatch, completed: FakeCompletedProcess
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        calls.append({"cmd": list(args[0]), "cwd": kwargs.get("cwd")})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_file_tools_are_listed_with_expected_properties() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    for name in ("solve_minizinc_files", "check_minizinc_files", "find_unsat_core_files"):
        assert name in by_name
    assert "solve_and_check_minizinc_files" not in by_name

    for name in ("solve_minizinc_files", "check_minizinc_files"):
        properties = by_name[name].inputSchema.get("properties", {})
        assert {"model_path", "data_path", "solver", "timeout_ms"} <= set(properties.keys())
        # The two-mode flag was removed: file tools always run CLI-style.
        assert "allow_local_includes" not in properties

    # Search-control flags are solve-only at the MCP surface: present on the
    # solve file tool, absent from the compile-check file tool.
    _search_flags = {"free_search", "parallel", "random_seed", "all_solutions", "num_solutions"}
    solve_files_props = by_name["solve_minizinc_files"].inputSchema.get("properties", {})
    assert _search_flags <= set(solve_files_props.keys())
    assert "checker_path" in solve_files_props
    check_files_props = by_name["check_minizinc_files"].inputSchema.get("properties", {})
    assert not (_search_flags & set(check_files_props.keys()))
    assert "checker_path" not in check_files_props

    findmus_props = by_name["find_unsat_core_files"].inputSchema.get("properties", {})
    assert {"model_path", "data_path", "timeout_ms"} <= set(findmus_props.keys())
    assert "solver" not in findmus_props
    assert "allow_local_includes" not in findmus_props


@pytest.mark.asyncio
async def test_solve_minizinc_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    structured = _structured(result)
    assert structured["status"] == "satisfied"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    assert structured["solution"] == {"x": 3}
    assert structured["solutions"] == [{"x": 3}]
    assert structured["objective"] is None
    assert structured["checker"] is None
    # The statistics field is exposed even when no statistics object was emitted.
    assert structured["statistics"] == {}
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
async def test_solve_minizinc_files_visible_content_includes_statistics(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                solution_obj("x = 3;\n", {"x": 3}),
                {"type": "statistics", "statistics": {"failures": 2}},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    assert _structured(result)["statistics"] == {"failures": "2"}
    text = _content_text(result)
    lower_text = text.lower()
    assert "Final answer requirement" in text
    assert "entire Statistics section" in text
    assert "do not omit" in lower_text
    assert "summarize" in lower_text
    assert "selected fields" in lower_text
    assert "Statistics:" in text
    assert "- failures: 2" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_forwards_search_flags_to_runtime(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "parallel": 3, "all_solutions": True},
    )

    cmd = calls[0]["cmd"]
    assert "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "3"


@pytest.mark.asyncio
async def test_solve_minizinc_files_num_solutions_forwards_n_flag_for_supported_solver(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "solver": "org.gecode.gecode", "num_solutions": 2},
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-n") + 1] == "2"


@pytest.mark.asyncio
async def test_solve_minizinc_files_num_solutions_rejected_for_default_solver(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An EXISTING model file is required: path validation runs before the gate.
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "num_solutions": 2},
        )
    message = str(exc_info.value)
    assert "num_solutions" in message
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.asyncio
async def test_check_minizinc_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0))

    mcp = create_mcp_server()
    result = await mcp.call_tool("check_minizinc_files", {"model_path": str(model_path)})

    assert _structured(result)["status"] == "ok"


@pytest.mark.asyncio
async def test_find_unsat_core_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(UNSAT_CORE_MODEL)
    _record_run_capturing_cwd(
        monkeypatch, FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0)
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("find_unsat_core_files", {"model_path": str(model_path)})

    structured = _structured(result)
    assert structured["status"] == "mus_found"
    assert len(structured["core"]) == 2


@pytest.mark.asyncio
async def test_solve_minizinc_files_runs_from_model_parent(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    assert calls[0]["cwd"] == str(model_path.resolve().parent)


@pytest.mark.asyncio
async def test_solve_minizinc_files_path_not_found_surfaces_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a missing path")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    missing = tmp_path / "nope.mzn"

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_files", {"model_path": str(missing)})

    message = str(exc_info.value)
    assert "does not exist" in message
    assert "nope.mzn" in message


@pytest.mark.asyncio
async def test_solve_minizinc_files_runtime_missing_surfaces_actionable_error(
    tmp_path: Path,
    fake_runtime_dir: Path,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


# --- solution checker on solve_minizinc_files -------------------------------

_CHECKER_MODEL_SRC = "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;\n"
_CHECKER_SRC = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)


def _write_solve_and_check_inputs(tmp_path: Path) -> tuple[Path, Path]:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_CHECKER_MODEL_SRC)
    checker_path = tmp_path / "model.mzc.mzn"
    checker_path.write_text(_CHECKER_SRC)
    return model_path, checker_path


@pytest.mark.asyncio
async def test_solution_checker_is_folded_into_solve_minizinc_files() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_and_check_minizinc_files" not in names

    tool = next(t for t in tools if t.name == "solve_minizinc_files")
    properties = tool.inputSchema.get("properties", {})
    assert {"model_path", "checker_path", "data_path", "solver", "timeout_ms"} <= set(properties)
    assert _SOLVE_ONLY_CONTROLS <= set(properties)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    structured = _structured(result)
    assert structured["checker"]["status"] == "completed"
    assert structured["status"] == "satisfied"
    assert structured["checker"]["checks"] == [{"violation": False, "output": "CORRECT\n"}]
    # The raw transcript (checker objects intact) is the authoritative record.
    assert '"type": "checker"' in structured["checker"]["transcript"]
    # The model-visible text leads with the checker verdict and the solve status.
    text = _content_text(result)
    assert "completed" in text
    assert "satisfied" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_text_notes_author_text_not_adjudicated(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model-visible text must warn that author CORRECT/INCORRECT text is NOT
    # interpreted by the server (only a constraint-style rejection is a violation),
    # so a client never treats the verbatim verdict as a server judgement.
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_pass("INCORRECT\n"),
                solution_obj("x = 3; y = 1;\n", {"x": 3, "y": 1}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    text = _content_text(result).lower()
    assert "not interpreted" in text or "not adjudicated" in text
    assert "checks" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_visible_content_includes_statistics(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The solve portion reuses _format_solve_result_content, so the Statistics
    # section and its copy-entire-section requirement appear verbatim whenever the
    # nested solve carries statistics.
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "statistics", "statistics": {"failures": 2}},
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    assert _structured(result)["statistics"] == {"failures": "2"}
    text = _content_text(result)
    assert "entire Statistics section" in text
    assert "Statistics:" in text
    assert "- failures: 2" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_omits_statistics_when_empty(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    assert _structured(result)["statistics"] == {}
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_violation_surfaces_in_structured(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_violation(),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    structured = _structured(result)
    assert structured["checker"]["status"] == "violation"
    assert structured["checker"]["checks"][0]["violation"] is True
    # The rejected solution stays in solutions (fact 5).
    assert structured["solutions"] == [{"x": 1, "y": 2}]


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_runs_from_model_parent_with_checker_flag(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        FakeCompletedProcess(
            stdout=stream(
                checker_pass("CORRECT\n"), solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2})
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--solution-checker") + 1] == str(checker_path.resolve())
    assert calls[0]["cwd"] == str(model_path.resolve().parent)


@pytest.mark.asyncio
async def test_solve_minizinc_files_bad_checker_suffix_surfaces_actionable_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_CHECKER_MODEL_SRC)
    bad_checker = tmp_path / "checker.mzn"  # wrong suffix
    bad_checker.write_text(_CHECKER_SRC)

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a bad checker suffix")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "checker_path": str(bad_checker)},
        )
    assert "mzc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_runtime_missing_surfaces_actionable_error(
    tmp_path: Path,
    fake_runtime_dir: Path,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "checker_path": str(checker_path)},
        )

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


# --- _report_status: dual-channel (progress + log) milestone helper ---------


class _FakeStatusContext:
    """Records report_progress/info calls; optionally raises from both."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.progress_calls: list[tuple[float, float | None, str | None]] = []
        self.info_messages: list[str] = []
        self._fail_with = fail_with

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.progress_calls.append((progress, total, message))

    async def info(self, message: str, **extra: object) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.info_messages.append(message)


@pytest.mark.asyncio
async def test_report_status_noops_without_context() -> None:
    await _report_status(None, 1, "validating request")


@pytest.mark.asyncio
async def test_report_status_sends_progress_without_total_by_default() -> None:
    ctx = _FakeStatusContext()

    await _report_status(ctx, 2, "solve is running")  # type: ignore[arg-type]

    assert ctx.progress_calls == [(2, None, "solve is running")]


@pytest.mark.asyncio
async def test_report_status_sends_info_log_with_same_message() -> None:
    ctx = _FakeStatusContext()

    await _report_status(ctx, 2, "solve is running")  # type: ignore[arg-type]

    assert ctx.info_messages == ["solve is running"]


@pytest.mark.asyncio
async def test_report_status_noops_when_request_context_unavailable() -> None:
    # FastMCP raises this exact ValueError for direct calls outside a real
    # JSON-RPC request (the mode every mcp.call_tool test in this file uses).
    ctx = _FakeStatusContext(fail_with=ValueError(_CONTEXT_UNAVAILABLE_MESSAGE))

    await _report_status(ctx, 1, "validating request")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_report_status_does_not_swallow_unrelated_value_error() -> None:
    boom = ValueError("unexpected internal error")
    ctx = _FakeStatusContext(fail_with=boom)

    with pytest.raises(ValueError) as exc_info:
        await _report_status(ctx, 1, "validating request")  # type: ignore[arg-type]

    assert exc_info.value is boom


# --- milestone progress: schema stability and per-family stage schedules ----


def _tool_fn(mcp: Any, name: str) -> Any:
    """Return a registered tool's underlying (decorated) function for direct calls."""
    return mcp._tool_manager.get_tool(name).fn


def _assert_dual_channel_schedule(ctx: _FakeStatusContext, final_message: str) -> None:
    """Assert the standard four-stage schedule went out on both channels."""
    assert [call[0] for call in ctx.progress_calls] == [1, 2, 3, 4]
    assert all(call[1] is None for call in ctx.progress_calls)
    assert ctx.progress_calls[-1][2] == final_message
    assert ctx.info_messages == [call[2] for call in ctx.progress_calls]


@pytest.mark.asyncio
async def test_tool_schemas_do_not_expose_context_or_progress_arguments() -> None:
    # The ctx parameter is protocol plumbing: FastMCP must keep it (and any
    # progress-token argument) out of every advertised tool input schema.
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    for tool in tools:
        properties = set(tool.inputSchema.get("properties", {}).keys())
        assert not ({"ctx", "context", "progress_token", "progressToken"} & properties), tool.name


@pytest.mark.asyncio
async def test_solve_minizinc_model_reports_stage_schedule(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "solve_minizinc_model")(
        model="var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", ctx=ctx
    )

    _assert_dual_channel_schedule(ctx, "Solve complete")
    assert ctx.progress_calls[1][2] == "MiniZinc solve is running"


@pytest.mark.asyncio
async def test_solve_minizinc_model_with_checker_reports_checker_stage_messages(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> FakeCompletedProcess:
        return FakeCompletedProcess(
            stdout=stream(checker_pass("CORRECT\n"), solution_obj("x = 3;\n", {"x": 3})),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "solve_minizinc_model")(
        model="var 1..5: x;\nconstraint x > 2;\nsolve satisfy;",
        checker="output [\"ok\"];",
        ctx=ctx,
    )

    assert ctx.progress_calls[1][2] == "MiniZinc solve with solution checker is running"
    assert ctx.progress_calls[2][2] == "MiniZinc finished; parsing solve and checker streams"


@pytest.mark.asyncio
async def test_check_minizinc_model_reports_stage_schedule(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *a, **k: FakeCompletedProcess(stdout="", stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "check_minizinc_model")(model="solve satisfy;", ctx=ctx)

    _assert_dual_channel_schedule(ctx, "Check complete")


@pytest.mark.asyncio
async def test_inspect_minizinc_model_reports_stage_schedule(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *a, **k: FakeCompletedProcess(stdout=_INSPECT_STDOUT, stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "inspect_minizinc_model")(model="solve satisfy;", ctx=ctx)

    _assert_dual_channel_schedule(ctx, "Inspection complete")


@pytest.mark.asyncio
async def test_find_unsat_core_reports_stage_schedule(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *a, **k: FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "find_unsat_core")(model=UNSAT_CORE_MODEL, ctx=ctx)

    _assert_dual_channel_schedule(ctx, "Unsat-core analysis complete")


@pytest.mark.asyncio
async def test_check_minizinc_files_reports_stage_schedule(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text("solve satisfy;")
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *a, **k: FakeCompletedProcess(stdout="", stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "check_minizinc_files")(model_path=str(model_path), ctx=ctx)

    _assert_dual_channel_schedule(ctx, "Check complete")


@pytest.mark.asyncio
async def test_check_minizinc_files_missing_path_still_raises_after_early_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Path validation behavior is unchanged: the domain ValueError still
    # surfaces through the decorated tool as its translated RuntimeError; only
    # the pre-call stages were emitted before it fired.
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a missing path")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    with pytest.raises(RuntimeError) as exc_info:
        await _tool_fn(mcp, "check_minizinc_files")(
            model_path=str(tmp_path / "nope.mzn"), ctx=ctx
        )

    assert "does not exist" in str(exc_info.value)
    assert [call[0] for call in ctx.progress_calls] == [1, 2]


@pytest.mark.asyncio
async def test_check_minizinc_model_compile_error_still_reports_final_stage(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A compile error is a normal structured result, so the client still gets
    # the closing milestone before the response.
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.subprocess.run",
        lambda *a, **k: FakeCompletedProcess(
            stdout="", stderr="Error: type error: undefined identifier 'xz'\n", returncode=1
        ),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    result = await _tool_fn(mcp, "check_minizinc_model")(
        model="var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;", ctx=ctx
    )

    assert result.status == "error"
    assert ctx.progress_calls[-1] == (4, None, "Check complete")


@pytest.mark.asyncio
async def test_run_blocking_executes_off_the_event_loop_thread() -> None:
    # The blocking MiniZinc call must leave the event loop free, or queued
    # status notifications sit unwritten until the solve ends (PR #34 review).
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _blocking() -> str:
        seen.append(threading.get_ident())
        return "done"

    result = await _run_blocking(_blocking)

    assert result == "done"
    assert seen and seen[0] != loop_thread
