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
# The optional experiment-log write rides inside this same commit step; it
# gets no stage of its own.
SAVE_STAGES = (
    "Validating save request",
    "MiniZinc verification (check, then solve) and save are running",
    "MiniZinc finished; save decision made",
    "Save request complete",
)
CPSAT_PYTHON_STAGES = (
    "Preparing CP-SAT Python source",
    "Running CP-SAT Python in child process",
    "Child finished; parsing result",
    "CP-SAT Python execution complete",
)
CPSAT_EXPERIMENT_STAGES = (
    "Validating experiment attempts and admission budget",
    "Running attempts in child processes",
    "Attempts finished; selecting winner",
    "Experiment complete",
)


def cpsat_save_stages(with_checker: bool) -> tuple[str, str, str, str]:
    """Return the CP-SAT save milestone messages, checker-aware at stage 2."""
    if with_checker:
        return (
            "Validating save request and CP-SAT Python source",
            "Re-running CP-SAT Python and any configured checker "
            "if earlier gates pass to evaluate the save gate",
            "Child finished; save decision made",
            "Save complete",
        )
    return (
        "Validating save request and CP-SAT Python source",
        "Re-running CP-SAT Python to evaluate the save gate",
        "Child finished; save decision made",
        "Save complete",
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
