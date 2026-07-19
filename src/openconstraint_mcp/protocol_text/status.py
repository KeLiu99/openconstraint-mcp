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
# blocking call, so stage 2 spans the whole pipeline and stages 3-4 hold for
# both outcomes — a committed save and a not_verified refusal. The optional
# experiment-log write rides inside the commit step, with no stage of its own.
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
    rerun = (
        "Re-running CP-SAT Python, then the checker if earlier gates pass, "
        "to evaluate the save gate"
        if with_checker
        else "Re-running CP-SAT Python to evaluate the save gate"
    )
    return (
        "Validating save request and CP-SAT Python source",
        rerun,
        "Child finished; save decision made",
        "Save complete",
    )


def solve_stages(with_checker: bool) -> tuple[str, str, str, str]:
    """Return the solve-family milestone messages, checker-aware at stages 2-3."""
    if with_checker:
        running = "MiniZinc solve with solution checker is running"
        parsing = "MiniZinc finished; parsing solve and checker streams"
    else:
        running = "MiniZinc solve is running"
        parsing = "MiniZinc finished; parsing solve stream"
    return "Validating solve request", running, parsing, "Solve complete"
