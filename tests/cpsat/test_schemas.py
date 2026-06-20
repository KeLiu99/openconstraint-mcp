"""Tests for cpsat.schemas — core request/response models and validators."""

import pytest
from pydantic import ValidationError

from openconstraint_mcp.cpsat.schemas import (
    ORToolsConstraint,
    ORToolsLinearParams,
    ORToolsLinearTerm,
    ORToolsObjective,
    ORToolsSolveRequest,
    ORToolsSolveResult,
    ORToolsVariable,
)


def test_minimal_satisfy_round_trip() -> None:
    """A trivial satisfy model round-trips through validation."""
    req = ORToolsSolveRequest(
        mode="satisfy",
        variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=10)],
    )
    assert req.mode == "satisfy"
    assert req.variables[0].id == "x"


def test_linear_params_parse_from_dict() -> None:
    """A dict params is coerced into the matching ORToolsLinearParams model."""
    c = ORToolsConstraint(
        id="c1",
        kind="linear",
        params={
            "terms": [{"var": "x", "coef": 3}],
            "sense": "<=",
            "rhs": 42,
        },
    )
    assert isinstance(c.params, ORToolsLinearParams)
    assert c.params.rhs == 42


def test_kind_params_mismatch_rejected() -> None:
    """kind="cumulative" with no-overlap-shaped params is rejected."""
    with pytest.raises(ValidationError, match="Field required"):
        ORToolsConstraint(
            id="c1",
            kind="cumulative",
            params={
                "start_vars": ["a"],
                "duration_vars": [5],
            },
        )


def test_unknown_kind_rejected() -> None:
    """A string not in the kind literal is rejected."""
    with pytest.raises(ValidationError, match="kind"):
        ORToolsConstraint(id="c1", kind="bogus", params={})


def test_optimize_without_objective_rejected() -> None:
    """mode='optimize' without an objective is rejected."""
    with pytest.raises(ValidationError, match="objective"):
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=10)],
        )


def test_satisfy_with_objective_rejected() -> None:
    """mode='satisfy' with an objective is rejected."""
    with pytest.raises(ValidationError, match="objective"):
        ORToolsSolveRequest(
            mode="satisfy",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=10)],
            objective=ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)]),
        )


def test_integer_var_with_lower_greater_upper_rejected() -> None:
    """An integer variable with lower > upper is rejected."""
    with pytest.raises(ValidationError, match="lower"):
        ORToolsVariable(id="x", domain="integer", lower=10, upper=5)


def test_multi_objective_list_accepted() -> None:
    """A list of objectives with different priorities is accepted."""
    req = ORToolsSolveRequest(
        mode="optimize",
        variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
        objective=[
            ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=1),
            ORToolsObjective(sense="max", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=2),
        ],
    )
    assert len(req.objective) == 2  # type: ignore[arg-type]


def test_same_priority_opposite_sense_rejected() -> None:
    """Two objectives at the same priority with opposite sense are rejected."""
    with pytest.raises(ValidationError, match="share priority"):
        ORToolsSolveRequest(
            mode="optimize",
            variables=[ORToolsVariable(id="x", domain="integer", lower=0, upper=100)],
            objective=[
                ORToolsObjective(
                    sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=1
                ),
                ORToolsObjective(
                    sense="max", terms=[ORToolsLinearTerm(var="y", coef=1)], priority=1
                ),
            ],
        )


def test_same_priority_same_sense_pair_accepted() -> None:
    """Two objectives at the same priority with the same sense are accepted."""
    req = ORToolsSolveRequest(
        mode="optimize",
        variables=[
            ORToolsVariable(id="x", domain="integer", lower=0, upper=100),
            ORToolsVariable(id="y", domain="integer", lower=0, upper=100),
        ],
        objective=[
            ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="x", coef=1)], priority=1),
            ORToolsObjective(sense="min", terms=[ORToolsLinearTerm(var="y", coef=2)], priority=1),
        ],
    )
    assert len(req.objective) == 2  # type: ignore[arg-type]


def test_implication_dangling_then_constraint_rejected() -> None:
    """An implication referencing a non-existent constraint id is rejected."""
    with pytest.raises(ValidationError, match="then_constraint_id"):
        ORToolsSolveRequest(
            mode="satisfy",
            variables=[ORToolsVariable(id="x", domain="bool")],
            constraints=[
                ORToolsConstraint(
                    id="impl",
                    kind="implication",
                    params={"if_var": "x", "then_constraint_id": "missing_c"},
                ),
            ],
        )


def test_implication_targeting_non_linear_rejected() -> None:
    """An implication targeting a non-linear constraint is rejected."""
    with pytest.raises(ValidationError, match="then_constraint_id"):
        ORToolsSolveRequest(
            mode="satisfy",
            variables=[
                ORToolsVariable(id="x", domain="bool"),
                ORToolsVariable(id="a", domain="integer", lower=0, upper=10),
                ORToolsVariable(id="b", domain="integer", lower=0, upper=10),
            ],
            constraints=[
                ORToolsConstraint(
                    id="ad",
                    kind="all_different",
                    params={"vars": ["a", "b"]},
                ),
                ORToolsConstraint(
                    id="impl",
                    kind="implication",
                    params={"if_var": "x", "then_constraint_id": "ad"},
                ),
            ],
        )


def test_implication_targeting_linear_accepted() -> None:
    """An implication targeting a present linear constraint is accepted."""
    req = ORToolsSolveRequest(
        mode="satisfy",
        variables=[
            ORToolsVariable(id="x", domain="bool"),
            ORToolsVariable(id="a", domain="integer", lower=0, upper=10),
        ],
        constraints=[
            ORToolsConstraint(
                id="lin",
                kind="linear",
                params={"terms": [{"var": "a", "coef": 1}], "sense": "<=", "rhs": 5},
            ),
            ORToolsConstraint(
                id="impl",
                kind="implication",
                params={"if_var": "x", "then_constraint_id": "lin"},
            ),
        ],
    )
    assert len(req.constraints) == 2


def test_solve_result_defaults() -> None:
    """ORToolsSolveResult defaults: objective_value is None, solutions is empty list."""
    result = ORToolsSolveResult(status="infeasible", solve_time_ms=10)
    assert result.objective_value is None
    assert result.solutions == []
    assert result.objective_values == []
