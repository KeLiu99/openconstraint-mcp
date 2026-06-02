from __future__ import annotations

import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.server import (
    _homepage_url,
    _lifespan,
    _server_version,
    create_mcp_server,
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_mcp_server()
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


def test_mcp_server_instructions_route_constraint_tasks() -> None:
    mcp = create_mcp_server()
    instructions = mcp.instructions or ""

    for substring in (
        "constraint programming",
        "optimization",
        "knapsack",
        "solve_constraint_problem",
        "check_minizinc_model",
        "solve_minizinc_model",
        "check_minizinc_files",
        "solve_minizinc_files",
        "managed local MiniZinc runtime",
        "bare PATH minizinc",
    ):
        assert substring in instructions


def test_mcp_server_instructions_present_solution_in_problem_terms() -> None:
    mcp = create_mcp_server()
    instructions = mcp.instructions or ""

    # The non-prompt fallback path must carry the same presentation contract as
    # the solve_constraint_problem prompt: state the solution in the terms of
    # the user's problem rather than dumping the raw JSON SolveResult, plus a
    # statistics summary.
    lower = instructions.lower()
    assert "terms of the user's problem" in lower
    assert "json" in lower
    assert "statistics" in lower


async def _get_prompt_text(prompt_name: str, arguments: dict[str, str]) -> str:
    mcp = create_mcp_server()
    result = await mcp.get_prompt(prompt_name, arguments)
    return "\n".join(
        message.content.text  # type: ignore[union-attr]
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_is_listed() -> None:
    mcp = create_mcp_server()
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
async def test_solve_constraint_problem_prompt_notes_inline_data_for_check_and_solve() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The prompt as a whole still references inline data and names both tools.
    assert "data" in text
    assert "check_minizinc_model" in text
    assert "solve_minizinc_model" in text

    # There is a note that the same data flows to both the check and the solve.
    data_notes = [line for line in text.splitlines() if "data" in line and "both" in line]
    assert data_notes, "prompt should note passing the same data to both check and solve"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_routes_existing_files_to_file_tools() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # When the user already has MiniZinc files (.mzn + optional .dzn) on disk,
    # the prompt should route to the path-based tools and pass paths — not the
    # pasted file contents, which would break relative `include`s — validating
    # before solving.
    assert "check_minizinc_files" in text
    assert "solve_minizinc_files" in text
    assert "model_path" in text
    assert ".dzn" in text or "data_path" in text
    assert text.index("check_minizinc_files") < text.index("solve_minizinc_files"), (
        "the file branch should check before it solves"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_timeout_branch_does_not_auto_solve() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The timeout branch must not silently regress to "treat timeout as ok".
    # Anchor on stable keywords for the three options the LLM should offer the
    # user rather than exact prose: simplify, raise timeout_ms, or solve anyway.
    for keyword in ("timeout_ms", "simplify", "anyway"):
        assert keyword in text, f"timeout branch missing guidance: {keyword!r}"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_explains_result_fields() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The explain step must guide the LLM to read the new deterministic fields.
    # Keyword presence, not exact wording, to avoid brittleness. `timed_out` and
    # `return_code` are new to the prompt, so they pin the new caveat rather than
    # the pre-existing "timeout" mention in the validation branch.
    assert "statistics" in text
    assert "stdout" in text
    assert any(keyword in text for keyword in ("timed_out", "return_code")), (
        "explain step should note a timeout/return-code caveat"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_instructs_structured_result_presentation() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The explain step must frame the final answer as a structured summary the
    # client presents to the user, not just "interpret these fields". Pin the
    # framing on a single line that names both presenting and structure, so the
    # pre-existing "Present the complete MiniZinc model" line cannot satisfy it.
    presentation_lines = [
        line
        for line in text.splitlines()
        if "present" in line.lower() and "structured" in line.lower()
    ]
    assert presentation_lines, (
        "explain step should instruct presenting a structured result summary"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_solution_block_is_status_conditioned() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The solution must be shown as a block read verbatim from stdout, never
    # paraphrased or inferred by the model.
    assert "verbatim" in text

    # Showing a solution is conditional on a solution-bearing status. The
    # unsatisfiable/error/timeout branch must say there is no solution to show
    # rather than fabricating one, so a line ties the two together.
    no_solution_lines = [
        line
        for line in text.splitlines()
        if "unsatisfiable" in line and "solution" in line
    ]
    assert no_solution_lines, (
        "explain step should note unsat/error/timeout have no solution block to show"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_leads_with_result_not_workflow_narration() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The user-facing answer must open with the result, not with MCP prompt,
    # workflow, or tool names. Pin the directive on a single "lead with" line.
    lead_lines = [line for line in text.splitlines() if "lead with" in line.lower()]
    assert lead_lines, "explain step should tell the client to lead with the result"

    lower = text.lower()
    # The "do not narrate internal names" instruction and its escape hatch.
    assert "narrat" in lower
    assert "workflow" in lower
    assert "implementation details" in lower


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_requires_statistics_when_present() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # A real client (Codex) dropped the statistics summary because the prompt
    # framed it as a soft, best-effort nicety. The directive must make a stats
    # summary non-optional whenever the `statistics` map is non-empty. Pin the
    # requirement on a single line so the soft "may be empty" caveat cannot
    # satisfy it.
    stats_required_lines = [
        line
        for line in text.splitlines()
        if "statistics" in line.lower()
        and ("required" in line.lower() or "do not omit" in line.lower())
    ]
    assert stats_required_lines, (
        "explain step should require a statistics summary when the map is non-empty"
    )


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_avoids_repeated_headings() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # A real client (Claude Code) emitted the "Solver statistics" heading twice.
    # The prompt must tell the client to use each heading at most once.
    heading_lines = [
        line
        for line in text.splitlines()
        if "heading" in line.lower()
        and ("once" in line.lower() or "repeat" in line.lower())
    ]
    assert heading_lines, "presentation guidance should forbid repeating section headings"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_avoids_speculative_commentary() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # A real client (Claude Code) padded the default answer with value-density
    # and greedy commentary. The prompt must discourage speculative algorithm
    # commentary by default while leaving an escape hatch when the user asks.
    commentary_lines = [
        line
        for line in text.splitlines()
        if "commentary" in line.lower() or "speculat" in line.lower()
    ]
    assert commentary_lines, (
        "presentation guidance should discourage speculative algorithm commentary"
    )
    assert "unless the user" in text.lower(), "the no-commentary default needs an escape hatch"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_offers_item_table_when_applicable() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # When the user problem supplies item-like data and the solution selects
    # among it, the client should render a concise table/list of chosen items.
    table_lines = [line for line in text.splitlines() if "table" in line.lower()]
    assert table_lines, "presentation guidance should allow a table-style item summary"

    lower = text.lower()
    assert "item-like" in lower or "selected-item" in lower, (
        "the item-table guidance should be conditioned on item-like data"
    )


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


def _record_data_run(
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to capture the inline-data file contents at call time.

    The runtime deletes its temp dir on return, so the ``data.dzn`` written from
    an inline ``data`` argument must be read inside the fake, before cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        cmd = args[0]
        data_contents: str | None = None
        data_path = next((Path(arg) for arg in cmd if str(arg).endswith(".dzn")), None)
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        calls.append({"data_contents": data_contents})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_solve_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_minizinc_model" in names

    tool = next(t for t in tools if t.name == "solve_minizinc_model")
    properties = tool.inputSchema.get("properties", {})
    assert {"model", "data", "solver", "timeout_ms"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_solve_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="x = 3;\n----------\n==========\n%%%mzn-stat: solveTime=0.01\n%%%mzn-stat-end\n",
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "optimal"
    assert structured["solver"] == "cp-sat"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    # Parsed %%%mzn-stat: pairs flow through FastMCP structured content.
    assert structured["statistics"] == {"solveTime": "0.01"}


@pytest.mark.asyncio
async def test_solve_minizinc_model_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(
        monkeypatch,
        _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;", "data": "n = 3;"},
    )

    assert calls[0]["data_contents"] == "n = 3;"
    assert _structured(result)["status"] == "optimal"


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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

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
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="",
            stderr="MiniZinc: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


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
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

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
    calls = _record_data_run(monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0))

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
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("check_minizinc_model", {"model": ""})
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
    def _fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout=_UNSAT_CORE_STDOUT,
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool("find_unsat_core", {"model": _UNSAT_CORE_MODEL})

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
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "find_unsat_core",
        {"model": _UNSAT_CORE_MODEL, "data": "lo = 5;"},
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

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
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompletedProcess
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        calls.append({"cmd": list(args[0]), "cwd": kwargs.get("cwd")})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_file_tools_are_listed_with_expected_properties() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    for name in ("solve_minizinc_files", "check_minizinc_files", "find_unsat_core_files"):
        assert name in by_name

    for name in ("solve_minizinc_files", "check_minizinc_files"):
        properties = by_name[name].inputSchema.get("properties", {})
        assert {"model_path", "data_path", "solver", "timeout_ms"} <= set(properties.keys())
        # The two-mode flag was removed: file tools always run CLI-style.
        assert "allow_local_includes" not in properties

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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    structured = _structured(result)
    assert structured["status"] == "optimal"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    # The statistics field is exposed even when no stat lines were emitted.
    assert structured["statistics"] == {}


@pytest.mark.asyncio
async def test_check_minizinc_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

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
    model_path.write_text(_UNSAT_CORE_MODEL)
    _record_run_capturing_cwd(
        monkeypatch, _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0)
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)
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


# --- website_url metadata --------------------------------------------------


def _expected_homepage_from_metadata() -> str | None:
    """Parse the ``Homepage`` Project-URL the same way the server should.

    Derived from live ``importlib.metadata`` so the test does not hardcode the
    URL literal: when the dedicated homepage launches, only ``pyproject.toml``
    changes and this expectation tracks it automatically.
    """
    for entry in metadata.metadata("openconstraint-mcp").get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage":
            return url.strip()
    return None


def test_homepage_url_returns_declared_homepage() -> None:
    url = _homepage_url()

    assert url is not None
    # Load-bearing: the comma-split leaves a leading space (' https://…'); this
    # assertion fails if the parse forgets to strip, catching a shared bug.
    assert url.startswith("https://")
    assert url == _expected_homepage_from_metadata()


def test_server_advertises_homepage_as_website_url() -> None:
    assert create_mcp_server().website_url == _homepage_url()


def test_homepage_url_none_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> object:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.metadata", _raise)

    assert _homepage_url() is None


def test_server_version_unknown_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> str:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.version", _raise)

    assert _server_version() == "unknown"


# --- lifespan boot diagnostic ----------------------------------------------


@pytest.mark.asyncio
async def test_boot_diagnostic_warns_when_runtime_missing(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _lifespan(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert _server_version() in err
    assert str(fake_runtime_dir) in err
    assert "NOT installed" in err
    assert "install-runtime" in err


@pytest.mark.asyncio
async def test_boot_diagnostic_reports_installed_runtime(
    fake_minizinc_binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _lifespan(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert "installed" in err
    assert str(fake_minizinc_binary) in err


@pytest.mark.asyncio
async def test_boot_diagnostic_writes_nothing_to_stdout(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Over stdio, stdout is the JSON-RPC channel; the banner must never land
    # there or it corrupts the protocol.
    async with _lifespan(create_mcp_server()):
        pass

    assert capsys.readouterr().out == ""


def test_lifespan_is_wired_into_server() -> None:
    assert create_mcp_server().settings.lifespan is _lifespan
