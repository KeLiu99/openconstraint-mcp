"""Real-subprocess smoke test for the flexible job shop example workflow.

``examples/flexible_job_shop/checker.py`` advertises a specific way to be run:
`payload["problem"]` may name a data file, which the checker resolves next to
its own `__file__` -- and that only works under a PATH-BASED checker run
(`checker_path`, i.e. `run_cpsat_python_file_checked` or
`submit_cpsat_python_file_job`). ``tests/test_flexible_job_shop_checker.py``
covers the grading logic by importing the checker directly, and proves the
NEGATIVE half of that claim (a copied checker cannot resolve the filename), but
importing a function never exercises the tool that is supposed to invoke it.

This test closes the positive half end to end, with no mocks: the real MCP tool
spawns the real model script, parses its real stdout, builds the checker payload
from it, and spawns the real checker in place. The seam it guards is the one
between two separately-tested artifacts -- a model script's printed `solution`
object and ``checker.py``'s expectations of it -- which every mocked test of
``run_cpsat_python_file_checked`` supplies both sides of and therefore cannot
check. That seam has broken before: the models once printed a SUMMARY of the
schedule rather than the schedule itself, the regression
``test_compact_summary_solution_yields_error_status`` was written for. It is
parametrized over more than ``model.py`` because that history is per-file, not
per-directory: each model script owns its own copy of the output tail, so a
mistake in one script's copy needs its own run to catch, not a neighbor's.

Marked ``integration``: it spawns real children (excluded from ``just check``,
run with ``just integration``). It needs no managed MiniZinc runtime -- the
CP-SAT path runs on ``sys.executable``, whose venv ships ``ortools``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.server import create_mcp_server

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "flexible_job_shop"

# The proven optimum of the 10x6 Brandimarte mk01 instance. Every parametrized
# script here reaches it in ~0.1s single-worker at seed 42, so the 10s in-model
# cap below is a wide margin rather than a race -- the assertion pins a property
# of the INSTANCE, not a solver-performance timing.
_MK01_OPTIMUM = 40


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script_name",
    [
        "model.py",
        # The one *search-order* ablation (add_decision_strategy over the direct
        # optional-interval encoding) -- non-trivial solver behavior that no other
        # test exercises, so it gets its own share of this seam check rather than
        # relying on model.py alone to stand in for every formulation.
        "model_earliest_start_branching.py",
    ],
)
async def test_mk01_model_and_checker_reach_an_accepted_verdict_through_the_mcp_tool(
    script_name: str,
) -> None:
    mcp = create_mcp_server("full")

    # Note the two independent channels: `args` tells the MODEL which instance to
    # solve, `problem` tells the CHECKER which instance to grade against. The
    # model does not get to pick its own ground truth. Both are bare filenames
    # resolved next to their own script, which is exactly what the path-based
    # tool's cwd contract makes work. The results-dir argument is deliberately
    # omitted so the run writes nothing into the checkout.
    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(_EXAMPLE_DIR / script_name),
            "checker_path": str(_EXAMPLE_DIR / "checker.py"),
            "args": ["data_mk01.json", "10"],
            "problem": "data_mk01.json",
            "timeout_ms": 60_000,
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    checker: dict[str, Any] = result["checker"]

    assert result["status"] == "optimal"
    assert result["objective"] == _MK01_OPTIMUM
    assert checker["status"] == "accepted", checker["errors"]
    # The checker graded the SIBLING data file, not inline JSON -- the filename
    # channel this whole path-based workflow exists for.
    assert checker["details"]["instance_source"] == "file:data_mk01.json"
