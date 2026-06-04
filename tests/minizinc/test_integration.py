"""Real-runtime checks for ``check_model`` and ``find_unsat_core``.

These exercise the actual managed MiniZinc binary, so they prove what the
mocked unit tests cannot: that a `-c` compile returns 0 / non-zero for
valid / invalid models, and that a clean compile leaves stdout empty (the
`.fzn` is written to a file, not streamed). They are marked ``integration``
and excluded from ``just check``; run them with ``just integration`` on a
machine where ``install-runtime`` has placed a runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.minizinc.core import (
    check_model,
    check_model_path,
    find_unsat_core,
    find_unsat_core_path,
    solve_model,
    solve_model_path,
)
from openconstraint_mcp.runtime import is_runtime_installed

pytestmark = pytest.mark.integration

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

# Parameterized model: `n` is undeclared data, and it *bounds the domain* of
# `x` (`var 1..n`), so without the data file the model cannot even flatten. A
# clean run that prints `x=4` therefore proves the bundled binary honored the
# positional data file.
_PARAM_MODEL = 'int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;\noutput ["x=\\(x)\\n"];\n'

# Parameterized *unsat* model: the conflicting bounds live in the data
# (`lo > hi`), so findMUS can only reach `mus_found` if the data file was read.
# The conflict is phrased over `x + y` rather than direct bounds on a single
# variable: a direct `x >= lo`/`x <= hi` contradiction is caught during
# flattening and folded into findMUS's hard background ("Background is not
# satisfiable, exiting"), leaving no soft constraints to minimize. Phrasing it
# over a sum keeps both constraints soft so findMUS isolates them as a MUS.
_PARAM_UNSAT_MODEL = (
    "int: lo;\n"
    "int: hi;\n"
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "constraint x + y > lo;\n"
    "constraint x + y < hi;\n"
    "constraint x != y;\n"
    "solve satisfy;\n"
)


@pytest.fixture(autouse=True)
def _require_runtime() -> None:
    if not is_runtime_installed():
        pytest.skip("managed MiniZinc runtime not installed")


def test_valid_model_compiles_with_empty_stdout() -> None:
    result = check_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert result.status == "ok"
    # A clean `-c` compile writes the FlatZinc to a file, not stdout.
    assert result.stdout == ""


def test_invalid_model_reports_compile_error() -> None:
    result = check_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert result.stderr.strip()


def test_find_unsat_core_reports_conflicting_constraints() -> None:
    result = find_unsat_core(_UNSAT_CORE_MODEL)

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    assert result.status == "mus_found"
    assert any("x + y > 5" in source for source in normalized_sources)
    assert any("x + y < 3" in source for source in normalized_sources)
    assert all("x != y" not in source for source in normalized_sources)


def test_solve_model_honors_inline_data() -> None:
    result = solve_model(_PARAM_MODEL, data="n = 4;")

    # `n` was supplied only through the inline data file; the constraint forces
    # `x = n = 4`.
    assert result.status in {"satisfied", "optimal"}
    assert "x=4" in result.stdout


# Keys observed in `--statistics` output on the pinned runtime
# (MiniZinc 2.9.7 / cp-sat). The exact key set is solver- and version-defined,
# so accept any one of a small set rather than hardcode a single key a patch
# release might rename (compile-stat keys like `flatIntVars` also vary by run).
_ACCEPTED_STAT_KEYS = {"flatTime", "method", "paths", "solveTime", "failures"}


def test_solve_model_emits_statistics() -> None:
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    # Status is classified from the stream, not from stdout markers.
    assert result.status in {"satisfied", "optimal"}
    # `--statistics` makes the {"type":"statistics"} stream objects appear; they
    # parse into the structured view with a recognizable key.
    assert result.statistics
    assert _ACCEPTED_STAT_KEYS & result.statistics.keys()
    # The json-stream transport keeps raw stat lines out of the reconstructed
    # human stdout — statistics are sibling stream objects, not output text.
    assert "%%%mzn-stat:" not in result.stdout


def test_solve_model_optimization_returns_structured_solution() -> None:
    # Unique-optimum maximization: `x` is forced to 5, so both the objective and
    # the solution variables are deterministic and the stream reports
    # OPTIMAL_SOLUTION — the Phase-1 single-solution structured-solve smoke.
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve maximize x;")

    assert result.status == "optimal"
    assert result.objective == 5
    # The structured solution carries the model variable with _objective stripped.
    assert result.solution == {"x": 5}
    assert "_objective" not in result.solution
    # `solutions` preserves emission order and ends at the best (== `solution`).
    assert result.solutions
    assert result.solutions[-1] == result.solution
    # This model has no explicit `output` item, so the stream carries only the json
    # section. The human stdout must still be synthesized so the MCP-visible result
    # is never solution-less; the `_objective` artifact stays out of it.
    assert "x = 5" in result.stdout
    assert "_objective" not in result.stdout


def test_solve_model_satisfaction_returns_solution() -> None:
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    # A single `satisfy` emits a solution but no status object → satisfied, and a
    # satisfaction model carries no objective.
    assert result.status == "satisfied"
    assert result.objective is None
    assert result.solution is not None
    assert result.solution["x"] > 2
    assert result.solutions and result.solutions[-1] == result.solution
    # No explicit `output` item → json-only solution object; the synthesized human
    # stdout still names the variable, so the solution does not vanish from the
    # model-visible text.
    assert f"x = {result.solution['x']}" in result.stdout


# --- Phase 2: solver/search-control flags ----------------------------------

# `x < y` over 1..3 has exactly three solutions: (1,2), (1,3), (2,3).
_ALL_SOLUTIONS_MODEL = "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;\n"


def test_solve_model_all_solutions_enumerates_multiple() -> None:
    result = solve_model(_ALL_SOLUTIONS_MODEL, all_solutions=True)

    # `-a` enumerates every solution and the stream ends in ALL_SOLUTIONS, which
    # maps to `satisfied`; `solution` is the last enumerated entry of `solutions`.
    assert result.status == "satisfied"
    assert len(result.solutions) >= 2
    assert result.solution == result.solutions[-1]
    assert result.objective is None


def test_solve_model_random_seed_runs_cleanly() -> None:
    # `random_seed` is accepted and the solve completes; the seed effect is
    # solver-internal, so assert only a clean satisfied result.
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", random_seed=12345)

    assert result.status == "satisfied"
    assert result.solution is not None


def test_solve_model_negative_random_seed_surfaces_as_error() -> None:
    # cp-sat's seed must be a non-negative int32; a negative seed is rejected at
    # runtime as a `{"type":"status","status":"ERROR"}` verdict. Our parser must
    # map that to `status="error"` (not the silent "unknown" fallback), so a bad
    # parameter is visibly an error rather than an empty no-solution result.
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", random_seed=-5)

    assert result.status == "error"
    assert result.solution is None


def test_solve_model_parallel_runs_cleanly() -> None:
    # `parallel=2` requests two search threads; assert it solves, not a specific
    # threading effect (solver-dependent).
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", parallel=2)

    assert result.status == "satisfied"
    assert result.solution is not None


def test_solve_model_free_search_runs_cleanly() -> None:
    # `free_search=True` lets the solver use its own search; assert only that the
    # default managed solver accepts the flag and solves.
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", free_search=True)

    assert result.status == "satisfied"
    assert result.solution is not None


def test_check_model_honors_inline_data() -> None:
    # Without data, `var 1..n` has an unbound domain and the model cannot
    # flatten — a clean `ok` proves the data file was read.
    result = check_model(_PARAM_MODEL, data="n = 4;")

    assert result.status == "ok"


def test_find_unsat_core_honors_inline_data() -> None:
    result = find_unsat_core(_PARAM_UNSAT_MODEL, data="lo = 5;\nhi = 3;")

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    assert result.status == "mus_found"
    assert any("x + y > lo" in source for source in normalized_sources)
    assert any("x + y < hi" in source for source in normalized_sources)


# --- path-based file tools: CLI-style include behavior ---------------------

# A stdlib include (`globals.mzn`) is resolved from the solver's library path,
# never the model's directory.
_STDLIB_INCLUDE_MODEL = (
    'include "globals.mzn";\n'
    "array[1..3] of var 1..3: x;\n"
    "constraint alldifferent(x);\n"
    "solve satisfy;\n"
)


def test_stdlib_include_compiles(tmp_path: Path) -> None:
    model_path = tmp_path / "stdlib.mzn"
    model_path.write_text(_STDLIB_INCLUDE_MODEL)

    result = check_model_path(model_path)

    # A global-constraint model compiles via the stdlib include.
    assert result.status == "ok"


def test_relative_local_include_resolves(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "helpers.mzn").write_text("int: helper_bound = 5;\n")
    entry = proj / "entry.mzn"
    entry.write_text(
        'include "helpers.mzn";\nvar 1..helper_bound: x;\nconstraint x > 2;\nsolve satisfy;\n'
    )

    result = solve_model_path(entry)

    # File tools run from the model's own directory, so a relative sibling
    # include resolves like the MiniZinc CLI — the core of the file-tool design.
    assert result.status in {"ok", "satisfied", "optimal"}


def test_find_unsat_core_path_resolves_entry_basename(tmp_path: Path) -> None:
    model_path = tmp_path / "conflict.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)

    result = find_unsat_core_path(model_path)

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    # The real model basename (`conflict.mzn`) must match findMUS's trace token
    # for the structured core to resolve the entry-file spans.
    assert result.status == "mus_found"
    assert any("x + y > 5" in source for source in normalized_sources)
    assert any("x + y < 3" in source for source in normalized_sources)
    assert all("x != y" not in source for source in normalized_sources)
