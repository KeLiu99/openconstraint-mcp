"""Integration tests for pyexec/experiment.py — runs real ortools scripts.

Deliberately tiny and fast: a trivial two-variable optimization problem, solved
by two distinct explicit source variants (proving the multi-attempt path end to
end, not just with mocks), plus one script that reads the cooperative
``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` protocol for real, plus two on-disk
``script_path`` attempts raced in parallel from two sibling directories, each
reading its own sibling data file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.experiment import run_cpsat_python_experiment
from openconstraint_mcp.schemas.cpsat import CpsatPythonExperimentAttempt
from openconstraint_mcp.shared.hashing import path_sha256

# maximize x + y subject to x + 2y <= 10, x,y in [0, 10]; unique optimum x=10, y=0.
_BASELINE = """
import json
from ortools.sat.python import cp_model

model = cp_model.CpModel()
x = model.new_int_var(0, 10, "x")
y = model.new_int_var(0, 10, "y")
model.add(x + 2 * y <= 10)
model.maximize(x + y)

solver = cp_model.CpSolver()
solver.parameters.num_workers = 1
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
objective = solver.objective_value if solved else None
print(json.dumps({
    "status": status_map.get(status, "error"),
    "objective": objective,
    "solution": {"x": solver.value(x), "y": solver.value(y)},
}))
"""

# Same problem, an equivalent redundant-constraint reformulation.
_REDUNDANT_CONSTRAINT_VARIANT = """
import json
from ortools.sat.python import cp_model

model = cp_model.CpModel()
x = model.new_int_var(0, 10, "x")
y = model.new_int_var(0, 10, "y")
model.add(x + 2 * y <= 10)
model.add(x <= 10)  # redundant, but exercises a distinct source variant
model.maximize(x + y)

solver = cp_model.CpSolver()
solver.parameters.num_workers = 1
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
objective = solver.objective_value if solved else None
print(json.dumps({
    "status": status_map.get(status, "error"),
    "objective": objective,
    "solution": {"x": solver.value(x), "y": solver.value(y)},
}))
"""

# Reads the cooperative config protocol for num_workers; identical result either way.
_READS_CONFIG = """
import json
import os
from ortools.sat.python import cp_model

config_path = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")
num_workers = 1
if config_path:
    with open(config_path) as f:
        num_workers = json.load(f).get("num_workers", 1)

model = cp_model.CpModel()
x = model.new_int_var(0, 10, "x")
y = model.new_int_var(0, 10, "y")
model.add(x + 2 * y <= 10)
model.maximize(x + y)

solver = cp_model.CpSolver()
solver.parameters.num_workers = num_workers
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
objective = solver.objective_value if solved else None
print(json.dumps({
    "status": status_map.get(status, "error"),
    "objective": objective,
    "solution": {"num_workers": num_workers},
}))
"""


@pytest.mark.integration
def test_experiment_runs_two_explicit_source_variants() -> None:
    attempts = [
        CpsatPythonExperimentAttempt(name="baseline", source=_BASELINE),
        CpsatPythonExperimentAttempt(
            name="redundant_constraint", source=_REDUNDANT_CONSTRAINT_VARIANT
        ),
    ]

    result = run_cpsat_python_experiment(attempts, objective_sense="maximize")

    assert result.status == "winner"
    assert result.winner is not None
    assert result.winner.status == "optimal"
    assert result.winner.objective == 10
    assert len(result.attempts) == 2
    assert all(attempt.accepted for attempt in result.attempts)
    names = {attempt.name for attempt in result.attempts}
    assert names == {"baseline", "redundant_constraint"}
    # Both variants reach the same unique optimum, so either may win the real
    # subprocess timing tie-break; the tie-break precedence itself (status,
    # then duration_ms, then attempt order) is unit-tested deterministically
    # in test_experiment.py with mocked durations.
    assert result.winner_name in {"baseline", "redundant_constraint"}


@pytest.mark.integration
def test_experiment_config_protocol_reaches_real_child_process() -> None:
    attempts = [
        CpsatPythonExperimentAttempt(
            name="one_worker", source=_READS_CONFIG, config={"num_workers": 1}
        ),
        CpsatPythonExperimentAttempt(
            name="two_workers", source=_READS_CONFIG, config={"num_workers": 2}
        ),
    ]

    result = run_cpsat_python_experiment(attempts, objective_sense="maximize")

    assert result.status == "winner"
    assert all(attempt.accepted for attempt in result.attempts)
    assert all(attempt.config_sha256 is not None for attempt in result.attempts)
    by_name = {attempt.name: attempt for attempt in result.attempts}
    assert by_name["one_worker"].config_sha256 != by_name["two_workers"].config_sha256


# The file-based variant reads its sibling data file with a bare relative
# open() and takes the objective's y-weight from sys.argv[1]. The open()
# resolves only because the attempt's child runs with cwd set to its own
# script's parent directory, and argv is populated only because the attempt's
# `args` reached run_cpsat_python_file.
_FILE_VARIANT = """
import json
import sys
from ortools.sat.python import cp_model

with open("data.json") as f:
    bound = json.load(f)["bound"]
y_weight = int(sys.argv[1])

model = cp_model.CpModel()
x = model.new_int_var(0, bound, "x")
y = model.new_int_var(0, bound, "y")
model.add(x + y <= bound)
model.maximize(x + y_weight * y)

solver = cp_model.CpSolver()
solver.parameters.num_workers = 1
status = solver.solve(model)

status_map = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "unknown",
}
solved = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
print(json.dumps({
    "status": status_map.get(status, "error"),
    "objective": solver.objective_value if solved else None,
    "solution": {"x": solver.value(x), "y": solver.value(y)},
}))
"""

# Two sibling directories, each holding a BYTE-IDENTICAL copy of the script
# next to its OWN data.json. Because the scripts are identical and the `args`
# are identical, the per-attempt child `cwd` is the only thing that can make
# the two attempts produce different objectives. With y_weight=2 the optimum
# of `max x + 2y s.t. x + y <= bound` is y=bound, x=0 -> objective 2*bound:
# wide (bound=10) -> 20, narrow (bound=4) -> 8. Both are decided by the data,
# never by subprocess wall-clock timing.
_WIDE_BOUND = 10
_NARROW_BOUND = 4


def _write_isolated_variants(tmp_path: Path) -> dict[str, Path]:
    """Lay out wide/ and narrow/ script+data pairs; return each script path."""
    scripts: dict[str, Path] = {}
    for name, bound in (("wide", _WIDE_BOUND), ("narrow", _NARROW_BOUND)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "data.json").write_text(json.dumps({"bound": bound}), encoding="utf-8")
        script = directory / "variant.py"
        script.write_text(_FILE_VARIANT, encoding="utf-8")
        scripts[name] = script
    return scripts


@pytest.mark.integration
def test_parallel_script_path_attempts_each_read_their_own_directorys_data(
    tmp_path: Path,
) -> None:
    scripts = _write_isolated_variants(tmp_path)

    result = run_cpsat_python_experiment(
        [
            CpsatPythonExperimentAttempt(name="wide", script_path=str(scripts["wide"]), args=["2"]),
            CpsatPythonExperimentAttempt(
                name="narrow", script_path=str(scripts["narrow"]), args=["2"]
            ),
        ],
        objective_sense="maximize",
        max_parallel_attempts=2,
    )

    by_name = {attempt.name: attempt for attempt in result.attempts}
    assert by_name["wide"].objective == 2 * _WIDE_BOUND
    assert by_name["narrow"].objective == 2 * _NARROW_BOUND


@pytest.mark.integration
def test_parallel_script_path_attempts_report_winner_and_file_provenance(
    tmp_path: Path,
) -> None:
    scripts = _write_isolated_variants(tmp_path)

    result = run_cpsat_python_experiment(
        [
            CpsatPythonExperimentAttempt(
                name="narrow", script_path=str(scripts["narrow"]), args=["2"]
            ),
            CpsatPythonExperimentAttempt(name="wide", script_path=str(scripts["wide"]), args=["2"]),
        ],
        objective_sense="maximize",
        max_parallel_attempts=2,
    )

    assert result.status == "winner"
    assert result.winner_name == "wide"
    assert result.winner is not None
    assert result.winner.objective == 2 * _WIDE_BOUND
    assert all(attempt.accepted for attempt in result.attempts)
    assert all(attempt.used_script_path for attempt in result.attempts)
    by_name = {attempt.name: attempt for attempt in result.attempts}
    assert by_name["wide"].source_sha256 == path_sha256(scripts["wide"])
    assert by_name["narrow"].source_sha256 == path_sha256(scripts["narrow"])
