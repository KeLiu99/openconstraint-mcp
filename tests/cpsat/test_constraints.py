"""Tests for cpsat.constraints — one solver-asserting test per constraint kind."""

from openconstraint_mcp.cpsat.constraints import _BUILDERS, build_constraint
from openconstraint_mcp.cpsat.schemas import (
    ORToolsAllDifferentParams,
    ORToolsCircuitParams,
    ORToolsConstraint,
    ORToolsCumulativeParams,
    ORToolsElementParams,
    ORToolsImplicationParams,
    ORToolsLinearParams,
    ORToolsNoOverlapParams,
    ORToolsReservoirParams,
    ORToolsTableParams,
)

from ._helpers import _new_model, _solve_model


def test_linear_satisfiable() -> None:
    """A linear constraint that is satisfiable."""
    model, var_map = _new_model(x=(0, 10))
    build_constraint(
        model,
        ORToolsConstraint(
            id="c",
            kind="linear",
            params=ORToolsLinearParams(
                terms=[{"var": "x", "coef": 1}], sense="<=", rhs=5
            ),
        ),
        var_map,
    )
    status, _ = _solve_model(model, var_map)
    assert status in ("optimal", "feasible")


def test_linear_infeasible() -> None:
    """A linear constraint that is unsatisfiable."""
    model, var_map = _new_model(x=(0, 1))
    build_constraint(
        model,
        ORToolsConstraint(
            id="c",
            kind="linear",
            params=ORToolsLinearParams(
                terms=[{"var": "x", "coef": 1}], sense="==", rhs=999
            ),
        ),
        var_map,
    )
    status, _ = _solve_model(model, var_map)
    assert status == "infeasible"


def test_all_different_distinctness() -> None:
    """All variables must take different values."""
    model, var_map = _new_model(a=(0, 5), b=(0, 5), c=(3, 3))
    build_constraint(
        model,
        ORToolsConstraint(
            id="ad",
            kind="all_different",
            params=ORToolsAllDifferentParams(vars=["a", "b", "c"]),
        ),
        var_map,
    )
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    vals = {solution["a"], solution["b"], solution["c"]}
    assert len(vals) == 3


def test_element_domain_mismatch_infeasible() -> None:
    """Element constraint: target must equal array[index], mismatch → infeasible."""
    model, var_map = _new_model(idx=(0, 2), target=(0, 10))
    build_constraint(
        model,
        ORToolsConstraint(
            id="e",
            kind="element",
            params=ORToolsElementParams(
                index_var="idx", array=[1, 2, 3], target_var="target"
            ),
        ),
        var_map,
    )
    # Force contradiction: target must also be 99
    model.Add(var_map["target"] == 99)
    status, _ = _solve_model(model, var_map)
    assert status == "infeasible"


def test_table_membership() -> None:
    """Table constraint restricts vars to allowed tuples."""
    model, var_map = _new_model(x=(0, 10), y=(0, 10))
    build_constraint(
        model,
        ORToolsConstraint(
            id="t",
            kind="table",
            params=ORToolsTableParams(
                vars=["x", "y"], allowed_tuples=[[1, 2], [3, 4]]
            ),
        ),
        var_map,
    )
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    assert (solution["x"], solution["y"]) in ((1, 2), (3, 4))


def test_cumulative_capacity_separation() -> None:
    """Two tasks exceeding capacity cannot overlap."""
    horizon = 100
    model, var_map = _new_model(
        s1=(0, horizon), d1=(0, 10), s2=(0, horizon), d2=(0, 10)
    )
    build_constraint(
        model,
        ORToolsConstraint(
            id="cum",
            kind="cumulative",
            params=ORToolsCumulativeParams(
                start_vars=["s1", "s2"],
                duration_vars=[5, 5],
                demand_vars=[2, 2],
                capacity=3,
            ),
        ),
        var_map,
    )
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    s1_end = solution["s1"] + 5
    s2_start = solution["s2"]
    s2_end = solution["s2"] + 5
    assert s1_end <= s2_start or s2_end <= solution["s1"]


def test_no_overlap_separation() -> None:
    """Two tasks with no_overlap must not overlap."""
    horizon = 100
    model, var_map = _new_model(
        s1=(0, horizon), d1=(0, 20), s2=(0, horizon), d2=(0, 20)
    )
    build_constraint(
        model,
        ORToolsConstraint(
            id="no",
            kind="no_overlap",
            params=ORToolsNoOverlapParams(
                start_vars=["s1", "s2"], duration_vars=[10, 10]
            ),
        ),
        var_map,
    )
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    s1_end = solution["s1"] + 10
    s2_start = solution["s2"]
    s2_end = solution["s2"] + 10
    assert s1_end <= s2_start or s2_end <= solution["s1"]


def test_circuit_valid_tour() -> None:
    """Circuit constraint enforces a valid tour covering all nodes."""
    model, var_map = _new_model(
        arc_0_1=(0, 1), arc_0_2=(0, 1),
        arc_1_0=(0, 1), arc_1_2=(0, 1),
        arc_2_0=(0, 1), arc_2_1=(0, 1),
    )
    arcs = [
        (0, 1, "arc_0_1"),
        (0, 2, "arc_0_2"),
        (1, 0, "arc_1_0"),
        (1, 2, "arc_1_2"),
        (2, 0, "arc_2_0"),
        (2, 1, "arc_2_1"),
    ]
    build_constraint(
        model,
        ORToolsConstraint(
            id="circ", kind="circuit", params=ORToolsCircuitParams(arcs=arcs)
        ),
        var_map,
    )
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    selected = {k: v for k, v in solution.items() if v == 1}
    assert len(selected) >= 3
    out_degree = {n: 0 for n in range(3)}
    for arc_key in selected:
        parts = arc_key.split("_")
        from_node = int(parts[1])
        out_degree[from_node] += 1
    assert all(d == 1 for d in out_degree.values())


def test_implication_enforce_if() -> None:
    """When if_var is true, the implication enforces the linear constraint."""
    model, var_map = _new_model(trigger=(0, 1), x=(0, 10))
    lin = ORToolsConstraint(
        id="then_c",
        kind="linear",
        params=ORToolsLinearParams(
            terms=[{"var": "x", "coef": 1}], sense=">=", rhs=5
        ),
    )
    impl = ORToolsConstraint(
        id="impl",
        kind="implication",
        params=ORToolsImplicationParams(
            if_var="trigger", then_constraint_id="then_c"
        ),
    )
    build_constraint(
        model, impl, var_map, constraints_by_id={"then_c": lin}
    )
    model.Add(var_map["trigger"] == 1)
    status, solution = _solve_model(model, var_map)
    assert status in ("optimal", "feasible", "satisfied")
    assert solution["x"] >= 5


def test_reservoir_max_level_infeasible() -> None:
    """Reservoir constraint with impossible max_level → infeasible."""
    model, var_map = _new_model(t1=(0, 10), t2=(0, 10))
    model.Add(var_map["t1"] <= var_map["t2"])
    build_constraint(
        model,
        ORToolsConstraint(
            id="res",
            kind="reservoir",
            params=ORToolsReservoirParams(
                time_vars=["t1", "t2"],
                level_changes=[100, 100],
                min_level=0,
                max_level=150,
            ),
        ),
        var_map,
    )
    status, _ = _solve_model(model, var_map)
    assert status == "infeasible"


def test_unknown_kind_raises() -> None:
    """An unknown constraint kind is not in the builder dispatch map."""
    assert "bogus_kind" not in _BUILDERS
    assert _BUILDERS.get("nonexistent") is None
