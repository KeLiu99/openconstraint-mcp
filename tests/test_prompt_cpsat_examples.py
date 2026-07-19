"""The solve_cpsat_python prompt's code examples must be copyable as-is.

These tests validate the examples AFTER template rendering (when doubled
braces have become normal Python braces) and run them in-process against the
pinned OR-Tools dependency — no subprocess, no network, no managed runtime.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import textwrap

from openconstraint_mcp.protocol_text.prompts import SOLVE_CPSAT_PYTHON_PROMPT

_CONTRACT_KEYS = {"status", "objective", "solution", "best_objective_bound"}


def _rendered_code_fences() -> list[str]:
    rendered = SOLVE_CPSAT_PYTHON_PROMPT.format(problem="toy problem")
    fences: list[str] = []
    inside = False
    buf: list[str] = []
    for line in rendered.splitlines():
        # startswith, not ==, so an opening fence with a language tag
        # (```python) still toggles instead of silently joining the body.
        if line.strip().startswith("```"):
            if inside:
                fences.append(textwrap.dedent("\n".join(buf)))
                buf = []
            inside = not inside
        elif inside:
            buf.append(line)
    return fences


def _run_capturing_stdout(source: str) -> list[str]:
    # run_cpsat_python clears the replay-protocol env vars before executing a
    # script; mirror that here so an inherited OPENCONSTRAINT_MCP_CPSAT_SEED
    # (e.g. a non-integer value) cannot change what the example does.
    saved_seed = os.environ.pop("OPENCONSTRAINT_MCP_CPSAT_SEED", None)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(source, "<prompt-example>", "exec"), {})
    finally:
        if saved_seed is not None:
            os.environ["OPENCONSTRAINT_MCP_CPSAT_SEED"] = saved_seed
    return [line for line in out.getvalue().strip().splitlines() if line]


def test_cpsat_prompt_code_fences_have_no_placeholders() -> None:
    fences = _rendered_code_fences()
    assert len(fences) == 2, "expected the main example and the callback variant"
    for fence in fences:
        assert "..." not in fence, "a copyable example must not contain placeholders"


def test_cpsat_prompt_main_example_runs_and_emits_contract_json() -> None:
    main = _rendered_code_fences()[0]

    payload = json.loads(_run_capturing_stdout(main)[-1])

    assert set(payload) == _CONTRACT_KEYS
    assert payload["status"] == "optimal"
    assert payload["objective"] == 22.0
    assert payload["solution"] == {"x": 2, "y": 10}


def test_cpsat_prompt_main_example_suppresses_objective_when_infeasible() -> None:
    # OR-Tools returns 0.0 (not an exception) from objective_value and
    # best_objective_bound on an infeasible solve, so the example's guards —
    # not the properties themselves — are what keep fabricated values out of
    # the emitted JSON. Drive the same script infeasible and prove all three
    # value fields come back empty.
    main = _rendered_code_fences()[0]
    marker = "model.maximize(x + 2 * y)"
    assert marker in main
    infeasible = main.replace(marker, "model.add(x >= 11)\n" + marker)

    payload = json.loads(_run_capturing_stdout(infeasible)[-1])

    assert payload["status"] == "infeasible"
    assert payload["objective"] is None
    assert payload["solution"] == {}
    assert payload["best_objective_bound"] is None


def test_cpsat_prompt_callback_example_substitutes_for_plain_solve() -> None:
    # The prompt instructs replacing the plain solve line with the callback
    # block, so the two fences must compose into one runnable script that
    # emits intermediate JSON lines before the authoritative final line.
    main, callback = _rendered_code_fences()
    plain_solve = "status_code = solver.solve(model)"
    assert plain_solve in main
    assert callback.rstrip().endswith("model.has_objective()))")

    lines = _run_capturing_stdout(main.replace(plain_solve, callback))

    assert len(lines) >= 2, "callback should emit at least one intermediate line"
    intermediate = json.loads(lines[0])
    assert intermediate["status"] == "feasible"
    assert set(intermediate) == _CONTRACT_KEYS
    final = json.loads(lines[-1])
    assert final["status"] == "optimal"
