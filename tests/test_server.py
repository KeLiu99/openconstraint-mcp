from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.server import create_server


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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("list_available_solvers", {})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "bad config" in message


SAMPLE_PROBLEM = (
    "Schedule 5 nurses across 3 shifts over 7 days so each shift has at least "
    "one nurse and nobody works two shifts in a row."
)

_UNSAT_CORE_MODEL = (
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "\n"
    "constraint x + y > 5;\n"
    "constraint x + y < 3;\n"
    "constraint x != y;\n"
    "\n"
    "solve satisfy;\n"
)

_UNSAT_CORE_STDOUT = (
    "FznSubProblem:  hard cons: 0    soft cons: 3   leaves: 3      "
    "branches: 4    Built tree in 0.01 seconds.\n"
    "MUS: 1 2\n"
    "Brief: int_lin_le, int_lin_le\n"
    "Traces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;"
    "redefinitions.mzn|10|1|10|5|\n"
)


async def _get_prompt_text(prompt_name: str, arguments: dict[str, str]) -> str:
    mcp = create_server()
    result = await mcp.get_prompt(prompt_name, arguments)
    return "\n".join(
        message.content.text  # type: ignore[union-attr]
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_is_listed() -> None:
    mcp = create_server()
    prompts = await mcp.list_prompts()

    names = {prompt.name for prompt in prompts}
    assert "solve_constraint_problem" in names

    prompt = next(p for p in prompts if p.name == "solve_constraint_problem")
    argument_names = {arg.name for arg in (prompt.arguments or [])}
    assert "problem" in argument_names


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_echoes_user_problem() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_guides_minizinc_drafting() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    for substring in (
        "you",
        "draft",
        "MiniZinc",
        "check_minizinc_model",
        "solve_minizinc_model",
        "check-runtime",
        "install-runtime",
    ):
        assert substring in text, f"prompt missing required guidance: {substring!r}"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_does_not_recommend_bare_path_minizinc() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The managed-runtime invariant in AGENTS.md forbids recommending an
    # arbitrary `$PATH`-resolved `minizinc`. The fallback must route users
    # through the openconstraint-mcp CLI instead.
    assert "minizinc --solver cp-sat model.mzn" not in text, (
        "fallback must not recommend a bare PATH-based minizinc invocation"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_passes_through_brace_input() -> None:
    problem_with_braces = "Allocate workers across shifts {1..3} with budget constraints"

    text = await _get_prompt_text("solve_constraint_problem", {"problem": problem_with_braces})

    assert problem_with_braces in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_preserves_local_first_boundary() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    for forbidden in (
        "the server will generate",
        "the server calls",
        "server-side LLM",
        "LangChain",
        "LangGraph",
    ):
        assert forbidden not in text, (
            f"prompt must not imply server-side LLM coupling: {forbidden!r}"
        )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_orders_check_before_solve() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # Pin the order on the single recommended-loop line that names both tools,
    # not a whole-prompt first-index comparison: the execute step and CLI
    # walkthrough also mention solve_minizinc_model, so a global comparison
    # could pass or fail for the wrong reasons.
    loop_lines = [
        line
        for line in text.splitlines()
        if "check_minizinc_model" in line and "solve_minizinc_model" in line
    ]
    assert len(loop_lines) == 1, "expected one recommended-loop line naming both tools in order"
    loop_line = loop_lines[0]
    assert loop_line.index("check_minizinc_model") < loop_line.index("solve_minizinc_model")


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_timeout_branch_does_not_auto_solve() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The timeout branch must not silently regress to "treat timeout as ok".
    # Anchor on stable keywords for the three options the LLM should offer the
    # user rather than exact prose: simplify, raise timeout_ms, or solve anyway.
    for keyword in ("timeout_ms", "simplify", "anyway"):
        assert keyword in text, f"timeout branch missing guidance: {keyword!r}"


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-content dict from a FastMCP call_tool result.

    FastMCP returns a tuple of ``(content_blocks, structured_content)`` from
    ``call_tool``; the structured payload is the second element.
    """
    return result[1]


@pytest.mark.asyncio
async def test_solve_minizinc_model_tool_is_listed() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_minizinc_model" in names

    tool = next(t for t in tools if t.name == "solve_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "solver", "timeout_ms"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_solve_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="x = 3;\n----------\n==========\n",
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "optimal"
    assert structured["solver"] == "cp-sat"


@pytest.mark.asyncio
async def test_solve_minizinc_model_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_server()
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_model", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_non_positive_timeout_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_server()
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
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="",
            stderr="MiniZinc: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


@pytest.mark.asyncio
async def test_check_minizinc_model_tool_is_listed() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "check_minizinc_model" in names

    tool = next(t for t in tools if t.name == "check_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "solver", "timeout_ms"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_check_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["solver"] == "cp-sat"


@pytest.mark.asyncio
async def test_check_minizinc_model_compile_error_returns_structured_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
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
    mcp = create_server()
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("check_minizinc_model", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_find_unsat_core_tool_is_listed() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "find_unsat_core" in names

    tool = next(t for t in tools if t.name == "find_unsat_core")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "timeout_ms"} <= set(properties.keys())
    assert "solver" not in properties


@pytest.mark.asyncio
async def test_find_unsat_core_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout=_UNSAT_CORE_STDOUT,
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    result = await mcp.call_tool("find_unsat_core", {"model": _UNSAT_CORE_MODEL})

    structured = _structured(result)
    assert structured["status"] == "mus_found"
    assert len(structured["core"]) == 2


@pytest.mark.asyncio
async def test_find_unsat_core_runtime_missing_surfaces_actionable_error(
    fake_runtime_dir: Path,
) -> None:
    mcp = create_server()
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("find_unsat_core", {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_find_unsat_core_non_positive_timeout_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "find_unsat_core",
            {"model": "constraint false;\nsolve satisfy;", "timeout_ms": 0},
        )
    assert "positive" in str(exc_info.value)
