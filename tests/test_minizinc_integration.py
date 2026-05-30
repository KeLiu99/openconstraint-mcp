"""Real-runtime checks for ``check_model``.

These exercise the actual managed MiniZinc binary, so they prove what the
mocked unit tests cannot: that a `-c` compile returns 0 / non-zero for
valid / invalid models, and that a clean compile leaves stdout empty (the
`.fzn` is written to a file, not streamed). They are marked ``integration``
and excluded from ``just check``; run them with ``just pytest -m integration``
on a machine where ``install-runtime`` has placed a runtime.
"""

from __future__ import annotations

import pytest

from openconstraint_mcp.minizinc import check_model
from openconstraint_mcp.runtime import is_runtime_installed

pytestmark = pytest.mark.integration


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
