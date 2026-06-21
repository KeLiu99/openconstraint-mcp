"""Client-facing progress-milestone text for the solving/checking tools.

Each tool family emits a four-stage schedule — validate, run, parse, done —
over the MCP progress and log notification channels. The wording lives here
alongside the tool descriptions and prompts so server.py carries the
orchestration (which stage fires when) and not the strings.
"""

# One four-stage milestone schedule per tool family, shared by the string- and
# path-based variants so the two cannot drift.
CHECK_STAGES = (
    "Validating check request",
    "MiniZinc compile check is running",
    "MiniZinc finished; parsing check result",
    "Check complete",
)
INSPECT_STAGES = (
    "Validating inspect request",
    "MiniZinc model interface analysis is running",
    "MiniZinc finished; parsing model interface",
    "Inspection complete",
)
UNSAT_CORE_STAGES = (
    "Validating unsat-core request",
    "findMUS is running",
    "findMUS finished; parsing core",
    "Unsat-core analysis complete",
)
# The save family re-verifies (check, then solve) and commits inside one
# blocking call, so stage 2 spans the whole pipeline and stages 3-4 are honest
# for both outcomes — a committed save and a not_verified refusal.
SAVE_STAGES = (
    "Validating save request",
    "MiniZinc verification (check, then solve) and save are running",
    "MiniZinc finished; save decision made",
    "Save request complete",
)

ORTOOLS_SOLVE_STAGES = (
    "Validating OR-Tools model",
    "OR-Tools CP-SAT solve is running",
    "OR-Tools finished; building result",
    "Solve complete",
)

BUDGET_ALLOCATION_STAGES = (
    "Validating budget allocation request",
    "OR-Tools CP-SAT solve is running",
    "OR-Tools finished; building result",
    "Budget allocation complete",
)

ASSIGNMENT_STAGES = (
    "Validating assignment request",
    "OR-Tools CP-SAT solve is running",
    "OR-Tools finished; building result",
    "Assignment complete",
)

SCHEDULING_STAGES = (
    "Validating scheduling request",
    "OR-Tools CP-SAT solve is running",
    "OR-Tools finished; building result",
    "Scheduling complete",
)

ROUTING_STAGES = (
    "Validating routing request",
    "OR-Tools CP-SAT solve is running",
    "OR-Tools finished; building result",
    "Routing complete",
)


def solve_stages(with_checker: bool) -> tuple[str, str, str, str]:
    """Return the solve-family milestone messages, checker-aware at stages 2-3."""
    if with_checker:
        return (
            "Validating solve request",
            "MiniZinc solve with solution checker is running",
            "MiniZinc finished; parsing solve and checker streams",
            "Solve complete",
        )
    return (
        "Validating solve request",
        "MiniZinc solve is running",
        "MiniZinc finished; parsing solve stream",
        "Solve complete",
    )
