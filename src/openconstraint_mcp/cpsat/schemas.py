"""Core OR-Tools CP-SAT structured request/response models and validators.

All field types are integer-only; domain layers scale floats before entering here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

Mode = Literal["satisfy", "optimize"]
Domain = Literal["bool", "integer"]
Sense = Literal["<=", ">=", "=="]
ObjectiveSense = Literal["min", "max"]
ConstraintKind = Literal[
    "linear",
    "all_different",
    "element",
    "table",
    "cumulative",
    "circuit",
    "no_overlap",
    "implication",
    "reservoir",
]
SolverStatus = Literal[
    "optimal",
    "feasible",
    "satisfied",
    "infeasible",
    "timeout_best",
    "timeout_no_solution",
    "error",
]

# ---------------------------------------------------------------------------
# Variable
# ---------------------------------------------------------------------------


class ORToolsVariable(BaseModel):
    """A decision variable for the CP-SAT model.

    ``domain="bool"`` produces a 0/1 integer variable (bounds are ignored).
    ``domain="integer"`` requires ``lower <= upper``.
    """

    id: str = Field(..., min_length=1)
    domain: Domain = "integer"
    lower: int = 0
    upper: int = 1
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> ORToolsVariable:
        if self.domain == "integer" and self.lower > self.upper:
            raise ValueError(
                f"integer variable '{self.id}': lower ({self.lower}) > upper ({self.upper})"
            )
        return self


# ---------------------------------------------------------------------------
# Linear term
# ---------------------------------------------------------------------------


class ORToolsLinearTerm(BaseModel):
    """A single term ``coef * var`` in a linear expression."""

    var: str = Field(..., min_length=1)
    coef: int


# ---------------------------------------------------------------------------
# Per-kind constraint params
# ---------------------------------------------------------------------------


class ORToolsLinearParams(BaseModel):
    """``sum(coef * var) sense rhs``."""

    terms: list[ORToolsLinearTerm] = Field(..., min_length=1)
    sense: Sense
    rhs: int


class ORToolsAllDifferentParams(BaseModel):
    """All listed variables must take distinct values."""

    vars: list[str] = Field(..., min_length=2)


class ORToolsElementParams(BaseModel):
    """``target == array[index]``."""

    index_var: str = Field(..., min_length=1)
    array: list[int] = Field(..., min_length=1)
    target_var: str = Field(..., min_length=1)


class ORToolsTableParams(BaseModel):
    """Variables must match one of the allowed tuples."""

    vars: list[str] = Field(..., min_length=1)
    allowed_tuples: list[list[int]] = Field(..., min_length=1)


class ORToolsCumulativeParams(BaseModel):
    """Resource capacity over time (scheduling)."""

    start_vars: list[str] = Field(..., min_length=1)
    duration_vars: list[str] | list[int] = Field(..., min_length=1)
    demand_vars: list[str] | list[int] = Field(..., min_length=1)
    capacity: int = Field(..., ge=0)


class ORToolsCircuitParams(BaseModel):
    """A single directed tour covering every node exactly once (routing)."""

    arcs: list[tuple[int, int, str]] = Field(..., min_length=1)


class ORToolsNoOverlapParams(BaseModel):
    """Tasks must not overlap in time (disjunctive scheduling)."""

    start_vars: list[str] = Field(..., min_length=1)
    duration_vars: list[str] | list[int] = Field(..., min_length=1)


class ORToolsImplicationParams(BaseModel):
    """When ``if_var`` is true, ``then_constraint_id`` is enforced.

    Referencing a constraint here makes it **template-only**: it is *only* enforced
    under the condition, never standalone.  A user who wants a constraint *both*
    globally enforced and conditionally referenced must declare it twice under
    distinct ids.
    """

    if_var: str = Field(..., min_length=1)
    then_constraint_id: str = Field(..., min_length=1)


class ORToolsReservoirParams(BaseModel):
    """Inventory/stock with time-stamped level changes."""

    time_vars: list[str] = Field(..., min_length=1)
    level_changes: list[int] = Field(..., min_length=1)
    min_level: int = 0
    max_level: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Constraint (kind-keyed params dispatch)
# ---------------------------------------------------------------------------

_PARAMS_BY_KIND: dict[str, type[BaseModel]] = {
    "linear": ORToolsLinearParams,
    "all_different": ORToolsAllDifferentParams,
    "element": ORToolsElementParams,
    "table": ORToolsTableParams,
    "cumulative": ORToolsCumulativeParams,
    "circuit": ORToolsCircuitParams,
    "no_overlap": ORToolsNoOverlapParams,
    "implication": ORToolsImplicationParams,
    "reservoir": ORToolsReservoirParams,
}


class ORToolsConstraint(BaseModel):
    """A single constraint, carrying kind-specific ``params``.

    The ``@model_validator(mode="before")`` enforces ``kind`` ↔ ``params``
    consistency so builders can trust they match.
    """

    id: str = Field(..., min_length=1)
    kind: ConstraintKind
    params: (
        ORToolsLinearParams
        | ORToolsAllDifferentParams
        | ORToolsElementParams
        | ORToolsTableParams
        | ORToolsCumulativeParams
        | ORToolsCircuitParams
        | ORToolsNoOverlapParams
        | ORToolsImplicationParams
        | ORToolsReservoirParams
    )
    metadata: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_params_by_kind(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        kind = values.get("kind")
        params = values.get("params")
        if isinstance(kind, str) and isinstance(params, dict):
            model_cls = _PARAMS_BY_KIND.get(kind)
            if model_cls is None:
                raise ValueError(
                    f"Unknown constraint kind '{kind}'; must be one of "
                    f"{sorted(_PARAMS_BY_KIND)}"
                )
            values["params"] = model_cls(**params)
        return values


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------


class ORToolsObjective(BaseModel):
    """Linear objective ``sum(coef * var)``, with an optional priority/weight for
    lexicographic multi-objective (smaller ``priority`` solved first).
    """

    sense: ObjectiveSense
    terms: list[ORToolsLinearTerm] = Field(..., min_length=1)
    priority: int = Field(default=1, ge=1)
    weight: int = Field(default=1, ge=1)


# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------


class ORToolsSearchConfig(BaseModel):
    """Solver controls.

    A timed-out solve always returns its best-found solution; ``status``
    distinguishes ``timeout_best`` from ``optimal``/``feasible``.
    """

    timeout_ms: Annotated[int | None, Field(default=None, ge=1)] = None
    num_workers: Annotated[int | None, Field(default=None, ge=1)] = None
    random_seed: Annotated[int | None, Field(default=None, ge=0)] = None
    max_solutions: Annotated[int, Field(default=1, ge=1)] = 1
    warm_start: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Solve request
# ---------------------------------------------------------------------------


class ORToolsSolveRequest(BaseModel):
    """The complete structured CP-SAT solve input."""

    mode: Mode
    variables: list[ORToolsVariable] = Field(..., min_length=1)
    constraints: list[ORToolsConstraint] = []
    objective: ORToolsObjective | list[ORToolsObjective] | None = None
    search: ORToolsSearchConfig | None = None

    @model_validator(mode="after")
    def _check_mode_objective_consistency(self) -> ORToolsSolveRequest:
        if self.mode == "optimize":
            if self.objective is None:
                raise ValueError("mode='optimize' requires an objective")
            if isinstance(self.objective, list) and len(self.objective) == 0:
                raise ValueError("mode='optimize' with an empty objective list")
        elif self.mode == "satisfy":
            if self.objective is not None:
                raise ValueError("mode='satisfy' must not have an objective")
        return self

    @model_validator(mode="after")
    def _check_implication_references(self) -> ORToolsSolveRequest:
        """Every ``then_constraint_id`` must reference a present **linear** constraint."""
        constr_by_id = {c.id: c for c in self.constraints}
        for c in self.constraints:
            if c.kind != "implication":
                continue
            target_id: str = c.params.then_constraint_id  # type: ignore[union-attr]
            target = constr_by_id.get(target_id)
            if target is None:
                raise ValueError(
                    f"implication '{c.id}': then_constraint_id '{target_id}' not found "
                    f"in constraints"
                )
            if target.kind != "linear":
                raise ValueError(
                    f"implication '{c.id}': then_constraint_id '{target_id}' is "
                    f"kind='{target.kind}'; only 'linear' is supported"
                )
        return self

    @model_validator(mode="after")
    def _check_same_sense_per_priority(self) -> ORToolsSolveRequest:
        """Within one priority level objectives must share a sense."""
        obj_list = self.objective
        if not isinstance(obj_list, list):
            return self
        by_prio: dict[int, ObjectiveSense] = {}
        for i, obj in enumerate(obj_list):
            prev = by_prio.get(obj.priority)
            if prev is not None and prev != obj.sense:
                raise ValueError(
                    f"objectives at index 0 and {i} share priority {obj.priority} "
                    f"but have opposite sense ('{prev}' vs '{obj.sense}'); "
                    f"use hand-signed coefficients or separate priorities"
                )
            by_prio[obj.priority] = obj.sense
        return self


# ---------------------------------------------------------------------------
# Solution / result
# ---------------------------------------------------------------------------


class ORToolsSolutionVariable(BaseModel):
    """The value of one variable in a solution."""

    id: str
    value: int
    metadata: dict[str, Any] | None = None


class ORToolsSolution(BaseModel):
    """A single complete solution."""

    variables: list[ORToolsSolutionVariable]


class ORToolsSolveResult(BaseModel):
    """The result of a CP-SAT solve."""

    status: SolverStatus
    objective_value: int | None = None
    objective_values: list[int] = []
    optimality_gap: float | None = None
    solve_time_ms: int = Field(..., ge=0)
    solutions: list[ORToolsSolution] = []
    message: str | None = None
