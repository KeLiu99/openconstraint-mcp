"""Text embedded into MCP tool results.

Unlike ``descriptions.py`` (documentation a client reads *about* a tool before
calling it), these strings are emitted *by* a tool into ``CallToolResult.content``
and read by the model *after* a call, interleaved with runtime data such as the
statistics table or solver inventory. They are model-visible presentation policy,
not tool documentation. This is a leaf module: ``server`` imports it, and it
imports nothing internal.
"""

from __future__ import annotations

STATS_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the entire Statistics section below into "
    "the user-facing answer. Do not omit it, summarize it, or replace it with "
    "only selected fields."
)
SOLVER_INVENTORY_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the solver inventory table below into the "
    "user-facing answer. Do not omit rows, convert it to bullets or prose, "
    'summarize, group, or replace rows with phrases like "additional solvers".'
)
SOLVER_NUM_SOLUTIONS_NOTE = (
    "`num_solutions` is supported only by `org.chuffed.chuffed` and "
    "`org.gecode.gecode`; the default `cp-sat` solver does not support it."
)
SOLVER_RUNTIME_CONFIG_CAUTION = (
    "Caution: solver entries come from the MiniZinc runtime configuration. "
    "Commercial or external MIP solvers such as CPLEX, Gurobi, Xpress, SCIP, and "
    "COIN-BC may still require separate installed binaries, licenses, or "
    "solver-specific setup before they can successfully solve a model."
)
SOLUTION_CHECK_NON_ADJUDICATION_NOTE = (
    "Note: a checker's author `CORRECT`/`INCORRECT` text is surfaced verbatim in "
    "each `checks` entry and is NOT interpreted by the server — only a "
    "constraint-style rejection (a nested UNSATISFIABLE) counts as a `violation`. "
    "`solve.solutions` includes checker-rejected solutions, so on a violation "
    "consult the per-solution `checks`. The checker never proves optimality; "
    "`solve.status` remains the proof of completeness."
)
SOLVER_CAPABILITY_METADATA_NOTE = (
    "To inspect detailed solver capabilities, ask for them explicitly. The "
    "structured result includes `capabilities.supports_all_solutions`, "
    "`supports_free_search`, `supports_parallel`, `supports_random_seed`, "
    "`supports_num_solutions`, and advisory `std_flags` for each solver."
)
