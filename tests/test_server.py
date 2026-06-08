from __future__ import annotations

import inspect
import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from openconstraint_mcp.minizinc.core import MiniZincExecutionError
from openconstraint_mcp.protocol_text.descriptions import (
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
)
from openconstraint_mcp.runtime import RuntimeMissingError

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _as_mcp_error,
    _homepage_url,
    _lifespan,
    _server_version,
    create_mcp_server,
)
from tests.minizinc.helpers import (
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


def test_list_available_solvers_description_documents_capabilities() -> None:
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "capabilities" in text
    for field in (
        "supports_all_solutions",
        "supports_free_search",
        "supports_parallel",
        "supports_random_seed",
        "supports_num_solutions",
        "std_flags",
    ):
        assert field in text, f"description should name the capability field {field}"
    assert "advisory" in text.lower()


def test_list_available_solvers_description_calls_out_conservative_num_solutions_gate() -> None:
    # supports_num_solutions is the conservative gate: only the two supported
    # solvers, explicitly not the default cp-sat.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "org.gecode.gecode" in text
    assert "org.chuffed.chuffed" in text
    assert "cp-sat" in text


def test_list_available_solvers_description_frames_std_flags_as_non_passthrough() -> None:
    # std_flags reports declared flags; it is not a surface for sending flags back
    # into the solve tools.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "solve_minizinc_model" in text
    assert "solve_minizinc_files" in text
    assert "passthrough" in text.lower()


def test_list_available_solvers_description_distinguishes_no_control_from_divergence() -> None:
    # Two distinct cases must stay separate: standard flags with no named control
    # (-i/-s/-t/-v) vs. the gist/-n allowlist divergence.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "gist" in text.lower()
    assert any(flag in text for flag in ("-i", "-s", "-t", "-v")), (
        "description should give a no-named-control flag example"
    )


def test_list_available_solvers_description_notes_complete_inventory_presentation() -> None:
    # The description must advertise the complete-inventory text presentation and
    # that the full capability metadata is structured, not printed by default.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "inventory" in text.lower()
    assert "not printed by default" in text


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
        "inspect_minizinc_model",
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
    # the user's problem rather than dumping the raw JSON SolveResult, plus the
    # complete Statistics section when present.
    lower = instructions.lower()
    assert "terms of the user's problem" in lower
    assert "json" in lower
    assert "item table" in lower
    assert "statistics" in lower
    assert "complete" in lower
    assert "condense" in lower


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
async def test_solve_constraint_problem_prompt_steers_num_solutions_to_supported_solver() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The recommended flow defaults to cp-sat, which does not support num_solutions;
    # without explicit steering an "N solutions" request lands on the gated solver.
    assert "num_solutions" in text
    assert "org.gecode.gecode" in text
    assert "org.chuffed.chuffed" in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_guides_multiple_optimal_solutions() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    assert "multiple optimal solutions" in normalized
    assert "proven optimum" in normalized
    assert "solve satisfy" in normalized
    assert "num_solutions" in normalized


def test_mcp_server_instructions_route_num_solutions_and_multiple_optima() -> None:
    assert "num_solutions" in MCP_SERVER_INSTRUCTIONS
    assert "org.gecode.gecode" in MCP_SERVER_INSTRUCTIONS
    assert "org.chuffed.chuffed" in MCP_SERVER_INSTRUCTIONS
    assert "not the default `cp-sat`" in MCP_SERVER_INSTRUCTIONS
    assert "multiple optimal solutions" in MCP_SERVER_INSTRUCTIONS
    assert "objective fixed" in MCP_SERVER_INSTRUCTIONS


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
async def test_solve_constraint_problem_prompt_explains_structured_solution_fields() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The explain step must name the new structured SolveResult fields so the
    # client builds tables and comparisons from them rather than re-parsing
    # stdout. Backticked tokens pin the field references — plain "solution"
    # appears throughout the prose, so it would not prove the new fields.
    for field in ("`solution`", "`solutions`", "`objective`"):
        assert field in text, f"prompt should reference the structured field {field}"


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
    assert presentation_lines, "explain step should instruct presenting a structured result summary"


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
        line for line in text.splitlines() if "unsatisfiable" in line and "solution" in line
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

    # Real clients dropped or compressed the statistics summary when the prompt
    # framed it as a soft, best-effort nicety. The directive must make the full
    # Statistics section non-optional whenever the `statistics` map is non-empty.
    stats_required_lines = [
        line
        for line in text.splitlines()
        if "statistics" in line.lower()
        and ("required" in line.lower() or "do not omit" in line.lower())
    ]
    assert stats_required_lines, (
        "explain step should require the Statistics section when the map is non-empty"
    )
    lower = text.lower()
    assert "copy the full section" in lower
    assert "selected fields" in lower


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_forbids_compressed_statistics() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    statistics_lines = [line.lower() for line in text.splitlines() if "statistics" in line.lower()]
    assert statistics_lines
    assert all("brief" not in line and "few" not in line for line in statistics_lines)
    assert "summarize it" in text.lower()


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_avoids_repeated_headings() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # A real client (Claude Code) emitted the "Solver statistics" heading twice.
    # The prompt must tell the client to use each heading at most once.
    heading_lines = [
        line
        for line in text.splitlines()
        if "heading" in line.lower() and ("once" in line.lower() or "repeat" in line.lower())
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
async def test_solve_constraint_problem_prompt_requires_item_table_when_applicable() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # When the user problem supplies item-like data and the solution selects
    # among it, the client should render a compact table, not degrade to a
    # prose-only list. Small item sets should show all item rows.
    table_lines = [line for line in text.splitlines() if "table" in line.lower()]
    assert table_lines, "presentation guidance should require a table-style item summary"

    lower = text.lower()
    assert "item-like" in lower or "selected-item" in lower, (
        "the item-table guidance should be conditioned on item-like data"
    )
    assert "prose-only list" in lower
    assert "one row per item" in lower
    assert "selected/count" in lower


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
            stdout=_UNSAT_CORE_STDOUT,
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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
        FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
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
    model_path.write_text(_UNSAT_CORE_MODEL)
    _record_run_capturing_cwd(
        monkeypatch, FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0)
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


def test_solve_descriptions_state_checker_suffix_and_nested_report() -> None:
    # The protocol descriptions must state plainly that checking is a solve option,
    # requires a `.mzc`/`.mzc.mzn` checker on the path side, and returns the nested
    # report fields clients need to inspect.
    combined = SOLVE_MINIZINC_MODEL_DESCRIPTION + SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "checker" in combined.lower()
    assert ".mzc" in SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "CheckerReport" in combined
    assert "transcript" in combined


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
def test_string_tools_translate_value_error_with_cause(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty model raises ValueError before the runtime gate; the default caught
    # set must convert it to a plain RuntimeError with the cause preserved.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _no_subprocess)
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="")

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
def test_file_tools_translate_value_error_with_cause(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing model path raises ValueError before the runtime gate; same
    # translation invariant on the path-based tools.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _no_subprocess)
    missing = tmp_path / "nope.mzn"
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        fn(model_path=str(missing))

    assert type(exc_info.value) is RuntimeError
    assert "does not exist" in str(exc_info.value)
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
