"""Shared test helpers for cpsat tests."""

from __future__ import annotations

from ortools.sat.python import cp_model


def _new_model(**vars: tuple[int, int]) -> tuple[cp_model.CpModel, dict[str, cp_model.IntVar]]:
    """Create a fresh CpModel with integer variables.

    ``vars`` keys are variable ids, values are ``(lower, upper)`` bounds.
    """
    model = cp_model.CpModel()
    var_map: dict[str, cp_model.IntVar] = {}
    for name, (lo, hi) in vars.items():
        var_map[name] = model.NewIntVar(lo, hi, name)
    return model, var_map


def _solve_model(
    model: cp_model.CpModel,
    var_map: dict[str, cp_model.IntVar],
) -> tuple[str, dict[str, int]]:
    """Solve the model and return ``(status_name, solution_dict)``.

    ``status_name`` is one of ``"optimal"``, ``"feasible"``, ``"infeasible"``,
    ``"unknown"``, or ``"model_invalid"``.
    """
    solver = cp_model.CpSolver()
    status_code = solver.Solve(model)
    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
        cp_model.MODEL_INVALID: "model_invalid",
    }
    status = status_map.get(status_code, "unknown")
    solution = {
        var_id: solver.Value(cp_var)
        for var_id, cp_var in var_map.items()
    }
    return status, solution
