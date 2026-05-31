"""Real-runtime checks for ``check_model`` and ``find_unsat_core``.

These exercise the actual managed MiniZinc binary, so they prove what the
mocked unit tests cannot: that a `-c` compile returns 0 / non-zero for
valid / invalid models, and that a clean compile leaves stdout empty (the
`.fzn` is written to a file, not streamed). They are marked ``integration``
and excluded from ``just check``; run them with ``just integration`` on a
machine where ``install-runtime`` has placed a runtime.
"""

from __future__ import annotations

import pytest

from openconstraint_mcp.minizinc import check_model, find_unsat_core, solve_model
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
