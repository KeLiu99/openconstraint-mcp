"""Integration tests for pyexec/experiment.py — runs real ortools scripts.

Deliberately tiny and fast: a trivial two-variable optimization problem, solved
by two distinct explicit source variants (proving the multi-attempt path end to
end, not just with mocks), plus one script that reads the cooperative
``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` protocol for real.
"""

from __future__ import annotations

import pytest

from openconstraint_mcp.pyexec.experiment import run_cpsat_python_experiment
from openconstraint_mcp.schemas import CpsatPythonExperimentAttempt

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
    # Both variants reach the same unique optimum; the earlier attempt order wins the tie.
    assert result.winner_name == "baseline"


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
