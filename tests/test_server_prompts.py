from __future__ import annotations

import pytest

from openconstraint_mcp.protocol_text.descriptions import (
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    RUN_CPSAT_PYTHON_DESCRIPTION,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
)

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    create_mcp_server,
)


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


def test_solve_minizinc_model_description_nudges_portfolio_for_hard_instances() -> None:
    assert "submit_portfolio_job" in SOLVE_MINIZINC_MODEL_DESCRIPTION


def test_run_cpsat_python_description_nudges_portfolio_for_hard_instances() -> None:
    assert "submit_portfolio_job" in RUN_CPSAT_PYTHON_DESCRIPTION


SAMPLE_PROBLEM = (
    "Schedule 5 nurses across 3 shifts over 7 days so each shift has at least "
    "one nurse and nobody works two shifts in a row."
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


def test_backend_routing_presents_minizinc_and_cpsat_as_peers() -> None:
    # The two backends are peers with a when-to-use heuristic; no routing text
    # may reinstate a blanket "prefer X" default for natural-language problems.
    combined = (
        MCP_SERVER_INSTRUCTIONS
        + SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION
        + SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION
    )
    assert "prefer" not in combined.lower()

    # The server instructions route both backend prompts and both run paths.
    assert "solve_constraint_problem" in MCP_SERVER_INSTRUCTIONS
    assert "solve_cpsat_python" in MCP_SERVER_INSTRUCTIONS
    assert "run_cpsat_python" in MCP_SERVER_INSTRUCTIONS

    # Selection heuristic markers: CP-SAT Python (zero-install) vs MiniZinc
    # (rich globals, .dzn data, checker verification, portfolio racing).
    lower = MCP_SERVER_INSTRUCTIONS.lower()
    assert "zero-install" in lower
    assert "portfolio" in lower
    assert ".dzn" in MCP_SERVER_INSTRUCTIONS

    # Each prompt description names the other backend's prompt as its peer.
    assert "solve_cpsat_python" in SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION
    assert "solve_constraint_problem" in SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION


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


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_step6_broadens_hard_problem_exploration() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # Step 6 must frame exploration around the general "hard problem, best
    # approach not knowable in advance" case, not just "one solver too slow",
    # and must name the concrete portfolio knobs a client can vary.
    for needle in (
        "submit_portfolio_job",
        "get_portfolio_job",
        "symmetry-breaking",
        "seed_count",
        "free_search",
        "per_attempt_timeout_ms",
    ):
        assert needle in text, f"step 6 exploration guidance should mention {needle}"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_step6_nudges_cross_backend() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # Step 6 should point at the CP-SAT Python path for an especially hard
    # instance, since neither backend dominates for every problem shape.
    assert "run_cpsat_python" in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_offers_save_only_on_user_request() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The save tool appears, but only as the optional post-success step gated
    # on the user's explicit ask — never as a required part of the solve loop.
    assert "save_verified_minizinc_model" in text
    normalized = " ".join(text.split())
    assert "asks" in normalized and "save" in normalized
    assert "only if" in normalized.lower()


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_save_step_follows_result_presentation() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The save mention lives after the result-presentation step, so it cannot
    # read as a pre-solve requirement.
    assert text.index("save_verified_minizinc_model") > text.index("Present the result")


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_save_step_mentions_portfolio_result() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The portfolio_result mention belongs in the save step, after the save
    # tool itself is introduced, not earlier as a pre-solve requirement.
    assert "portfolio_result" in text
    assert text.index("portfolio_result") > text.index("save_verified_minizinc_model")


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_save_step_keeps_path_choice_client_side() -> None:
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The client obtains the explicit absolute directory from the user (or its
    # own picker); the server never opens a dialog — no OS UI is implied
    # server-side.
    save_block_lines = [
        line for line in text.splitlines() if "dialog" in line.lower() or "picker" in line.lower()
    ]
    assert save_block_lines, "save guidance should address who owns the path choice"
    assert "target_dir" in text
    assert "absolute" in text
    normalized = " ".join(text.split()).lower()
    assert "opens no file dialog" in normalized


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_is_registered() -> None:
    mcp = create_mcp_server()
    prompts = await mcp.list_prompts()
    names = {p.name for p in prompts}
    assert "solve_cpsat_python" in names


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_substitutes_problem() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_mentions_run_cpsat_python() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    assert "run_cpsat_python" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_teaches_seed_protocol() -> None:
    # The client-facing protocol must not drift from the env-var contract the
    # save replay relies on: read OPENCONSTRAINT_MCP_CPSAT_SEED, fall back to
    # 42, single worker.
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in text
    assert "42" in text
    assert "num_workers = 1" in text
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_nudges_cross_backend() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})

    # The result-presentation step should point at the MiniZinc portfolio path
    # for an especially hard instance, since neither backend dominates for
    # every problem shape.
    assert "submit_portfolio_job" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_states_json_output_contract() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    # Must describe the required JSON output format
    assert '"status"' in text
    assert '"solution"' in text
    assert '"objective"' in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_forbids_network_and_file_mutation() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    assert "network" in lower
    assert "file" in lower


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_states_local_child_process_execution() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    assert "child process" in lower or "subprocess" in lower or "local" in lower


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_documents_save_gate_options() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    # All three gates must be named in the save step
    assert "reported" in text
    assert "expectation" in text.lower()
    assert "checker" in text.lower()
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_expectation_gate_documents_no_optimality_proof() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    # The prompt must explicitly state the threshold is NOT a proof of global optimality.
    assert "does not prove" in lower or "not prove" in lower or "not an optimality proof" in lower
    # Must name both sense options
    assert "maximize" in lower
    assert "minimize" in lower


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_checker_gate_documents_payload_format() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    # Checker receives the payload path as sys.argv[1]
    assert "sys.argv[1]" in text
    # Payload keys that the checker must read
    assert "solver_status" in text
    assert "solution" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_checker_gate_documents_output_contract() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    # Checker must emit JSON with status/errors/details
    assert '"accepted"' in text or "accepted" in text
    assert '"rejected"' in text or "rejected" in text
    assert "errors" in text
    # Only accepted + empty errors is the passing verdict
    assert "empty" in text.lower()
    assert "passing" in text.lower() or "only" in text.lower()


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_checker_gate_safety_boundary() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    # The server executes the checker locally and does not sandbox it — this
    # must be documented so the client knows to generate safe validation code.
    assert "sandbox" in lower
    assert "network" in lower
    assert "local" in lower


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_discourages_replay_config_for_ordinary_solves() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    # For a single problem instance, the prompt must steer toward a concrete,
    # self-contained script over a named scenario resolved via `config` — the
    # cooperative config protocol is reserved for explicit multi-attempt or
    # configured experiments, not the default modeling style for a one-off save.
    assert "single" in normalized and "hardcode" in normalized
    assert "not the default modeling style" in normalized
    assert "explicit multi-attempt" in normalized


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_documents_file_replay_workflow() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})

    # The manual replay workflow must route through the existing file tool
    # instead of promising a dedicated inspect/rerun tool, and must name the
    # checked-replay limitation plus its save-tool workaround.
    assert "run_cpsat_python_file" in text
    assert ".openconstraint-model.json" in text
    assert "replay-config.json" in text
    normalized = " ".join(text.split()).lower()
    assert "reported" in normalized and "level" in normalized
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_solve_cpsat_python_prompt_save_step_gated_on_user_request() -> None:
    text = await _get_prompt_text("solve_cpsat_python", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()
    # Save is optional — the user must ask
    assert "only if" in normalized or "if the user" in normalized
    # Save must not be framed as a required solve-loop step
    save_idx = text.index("save_verified_cpsat_python")
    run_idx = text.index("run_cpsat_python")
    assert save_idx > run_idx, "save step must appear after the run step"


def test_solve_descriptions_state_checker_suffix_and_nested_report() -> None:
    # The protocol descriptions must state plainly that checking is a solve option,
    # requires a `.mzc`/`.mzc.mzn` checker on the path side, and returns the nested
    # report fields clients need to inspect.
    combined = SOLVE_MINIZINC_MODEL_DESCRIPTION + SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "checker" in combined.lower()
    assert ".mzc" in SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "CheckerReport" in combined
    assert "transcript" in combined
